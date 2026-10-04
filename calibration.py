#!/usr/bin/env python3
"""
Калибровка устройства для реконструкции bag'а: углы крепления лидара, смещение от оси,
поправка наклона оси вращения.

Откуда берётся (по порядку):
  1. из самого bag'а — топик /pulse/calibration (std_msgs/String, JSON), который Pi пишет
     в начале записи из своего calib.json; так запись несёт калибровку, с которой снята;
  2. иначе — по дате записи из таблицы HISTORY (записи до появления топика);
  3. ручная замена (override) — поверх любого источника.

Формат (dict): mount_rpy_deg [r, p, y], rotation_center [x, y, z], angle_offset_deg,
tilt_R (3×3 или None), tilt_deg, плюс сведения: source, label, date и т. п.
"""

import io
import json
import re
import contextlib
import hashlib
from pathlib import Path

CALIB_TOPIC = '/pulse/calibration'
FORMAT_VERSION = 1

# Таблица калибровок по дате записи (YYYY-MM-DD, с какого дня действует) — для bag'ов без топика.
# Кронштейн лидара заменён между записями 2026-10-02 18:50 и 2026-10-04 20:05:
#   старый — yaw −1.6° (проверено calibrate_mount.py на static_20260922_231157; записи
#            2026-10-01…02 дают −1.38…−1.43°);
#   новый  — yaw −0.6° (static_20261004_*: −0.50…−0.62°, веб-HMI после исправления — −0.60°).
HISTORY = [
    ('2000-01-01', {'label': 'старый кронштейн', 'mount_rpy_deg': [0.0, 0.0, -1.6],
                    'rotation_center': [0.0, 0.0, -0.0108], 'angle_offset_deg': 0.0}),
    ('2026-10-03', {'label': 'новый кронштейн (2026-10-04)', 'mount_rpy_deg': [0.0, 0.0, -0.6],
                    'rotation_center': [0.0, 0.0, -0.0108], 'angle_offset_deg': 0.0}),
]

KEYS = ('mount_rpy_deg', 'rotation_center', 'angle_offset_deg')


def bag_date(bag):
    """Дата записи 'YYYY-MM-DD': из имени (static_YYYYMMDD_…), иначе по первому сообщению."""
    m = re.search(r'(20\d{2})(\d{2})(\d{2})', Path(bag).name)
    if m:
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    try:
        from offline_deskew import resolve_mcap_path
        from mcap.reader import make_reader
        import datetime
        with open(resolve_mcap_path(bag), 'rb') as f:
            st = make_reader(f).get_summary().statistics
            return datetime.datetime.fromtimestamp(st.message_start_time / 1e9).strftime('%Y-%m-%d')
    except Exception:                                    # noqa: BLE001
        return None


def from_history(date):
    """Калибровка из таблицы для даты (None — самая ранняя запись таблицы)."""
    entry = HISTORY[0]
    for d, c in HISTORY:
        if date is not None and date >= d:
            entry = (d, c)
    d, c = entry
    out = {k: c[k] for k in KEYS}
    out.update(tilt_R=None, tilt_deg=0.0, source='table', label=c['label'], valid_from=d)
    return out


def _normalize(raw):
    """JSON из /pulse/calibration (или calib.json Pi) → формат модуля."""
    rpy = raw.get('mount_rpy_deg')
    if rpy is None and 'mount_yaw_deg' in raw:          # calib.json веб-HMI хранит только yaw
        rpy = [0.0, 0.0, float(raw['mount_yaw_deg'])]
    out = {'mount_rpy_deg': [float(x) for x in rpy] if rpy is not None else None,
           'rotation_center': [float(x) for x in raw['rotation_center']] if raw.get('rotation_center') else None,
           'angle_offset_deg': float(raw['angle_offset_deg']) if raw.get('angle_offset_deg') is not None else None,
           'tilt_R': raw.get('R', raw.get('tilt_R')), 'tilt_deg': float(raw.get('tilt_deg', 0.0) or 0.0)}
    for k in ('yaw_bag', 'yaw_date', 'yaw_sigma_mm', 'date', 'calib_version'):
        if k in raw:
            out[k] = raw[k]
    return out


def read_from_bag(bag):
    """Калибровка из топика /pulse/calibration (первое сообщение) или None."""
    try:
        from offline_deskew import resolve_mcap_path
        from mcap_ros2.reader import read_ros2_messages
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            for m in read_ros2_messages(str(resolve_mcap_path(bag)), topics=[CALIB_TOPIC]):
                return _normalize(json.loads(m.ros_msg.data))
    except Exception:                                    # noqa: BLE001
        return None
    return None


def resolve(bag, override=None):
    """
    Калибровка для реконструкции bag'а: из bag'а, иначе по таблице дат; override (dict с
    любыми из KEYS / tilt_R) — поверх. Недостающие в bag'е поля берутся из таблицы.
    """
    date = bag_date(bag)
    base = from_history(date)
    got = read_from_bag(bag)
    if got is not None:
        out = dict(base)
        out.update({k: v for k, v in got.items() if v is not None})
        out['source'] = 'bag'
        out['label'] = 'из записи'
    else:
        out = base
    if override:
        override = dict(override)
        yaw = override.pop('mount_yaw_deg', None)
        if yaw is not None:                              # только yaw, крен и тангаж — из калибровки
            out['mount_rpy_deg'] = list(out['mount_rpy_deg'][:2]) + [float(yaw)]
        out.update({k: v for k, v in override.items() if v is not None})
        out['source'] += '+ручная'
    out['bag_date'] = date
    out['fingerprint'] = fingerprint(out)
    return out


def deskew_params(c):
    """Параметры deskew_cloud из калибровки."""
    import numpy as np
    return {'mount_rpy_deg': list(c['mount_rpy_deg']), 'rotation_center': np.asarray(c['rotation_center'], float),
            'angle_offset_deg': float(c['angle_offset_deg'])}


def fingerprint(c):
    """Короткий отпечаток значимых для геометрии полей: смена калибровки → пересчёт скана."""
    key = {k: c.get(k) for k in KEYS}
    key['tilt_R'] = None if c.get('tilt_R') is None else [[round(float(x), 6) for x in r] for r in c['tilt_R']]
    return hashlib.md5(json.dumps(key, sort_keys=True).encode()).hexdigest()[:10]


def describe(c):
    r, p, y = c['mount_rpy_deg']
    s = f"yaw {y:+.2f}°"
    if abs(r) > 1e-6 or abs(p) > 1e-6:
        s = f"крен {r:+.2f}°, тангаж {p:+.2f}°, " + s
    src = {'bag': 'из записи', 'table': f"по дате — {c.get('label', '')}"}.get(c['source'].split('+')[0], c['source'])
    return f"{s} ({src}{', ручная поправка' if '+ручная' in c['source'] else ''})"
