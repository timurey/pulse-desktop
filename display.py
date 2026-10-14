#!/usr/bin/env python3
"""
Настройки отображения облака: у скана (Scan.display) и у группы дерева (Group.display).

    color_mode  'auto' — цвет скана из палитры; 'solid' — свой цвет (color); 'height' — по
                высоте от нулевого уровня; 'range' — по расстоянию от своего сканера
    color       '#rrggbb'
    size        размер точек, px (иначе общий ползунок)
    rmin, rmax  расстояние от своего сканера, м
    zmin, zmax  высота от своего сканера (канон. система скана, Z вверх), м — не зависит
                от стыковки
    apply       обрезка (rmin…zmax) действует и в расчётах (стыковка, качество, сетка, экспорт),
                иначе — только на экране

Все поля необязательные; у скана действуют свои значения поверх значений его групп
(effective: от корня к скану).
"""

import json

import numpy as np

KEYS = ('color_mode', 'color', 'size', 'rmin', 'rmax', 'zmin', 'zmax', 'apply')
LIMITS = ('rmin', 'rmax', 'zmin', 'zmax')
MODES = ('auto', 'solid', 'height', 'range')


def clean(d):
    """Без пустых значений и значений по умолчанию; None — настроек нет."""
    if not d:
        return None
    out = {k: v for k, v in d.items() if k in KEYS and v is not None}
    if out.get('color_mode') == 'auto':
        out.pop('color_mode')
    if not out.get('apply'):
        out.pop('apply', None)
    return out or None


def merge(*ds):
    """Наложение настроек: следующие поверх предыдущих."""
    out = {}
    for d in ds:
        for k, v in (d or {}).items():
            if v is not None:
                out[k] = v
    return out


def has_limits(d):
    return any(d.get(k) is not None for k in LIMITS)


def calc_filter(d):
    """Обрезка для расчётов (только при apply) — словарь пределов или None."""
    if not d or not d.get('apply') or not has_limits(d):
        return None
    return {k: float(d[k]) for k in LIMITS if d.get(k) is not None}


def signature(f):
    return None if not f else json.dumps(f, sort_keys=True)


def mask(P_canon, d):
    """Маска точек (канон. система скана) внутри пределов дальности и высоты."""
    P = np.asarray(P_canon)
    m = np.ones(len(P), bool)
    if not d or not has_limits(d):
        return m
    if d.get('rmin') is not None or d.get('rmax') is not None:
        r = np.linalg.norm(P, axis=1)
        if d.get('rmin') is not None:
            m &= r >= float(d['rmin'])
        if d.get('rmax') is not None:
            m &= r <= float(d['rmax'])
    if d.get('zmin') is not None:
        m &= P[:, 2] >= float(d['zmin'])
    if d.get('zmax') is not None:
        m &= P[:, 2] <= float(d['zmax'])
    return m


def hex_rgb(h, default=(0.8, 0.8, 0.8)):
    try:
        h = str(h).lstrip('#')
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except (ValueError, TypeError):
        return default


def rgb_hex(c):
    return '#' + ''.join(f'{int(round(max(0, min(1, x)) * 255)):02x}' for x in c[:3])


# шкала «синий → голубой → зелёный → жёлтый → красный»
_RAMP = np.array([[0.19, 0.33, 0.85], [0.10, 0.70, 0.90], [0.20, 0.78, 0.35], [0.98, 0.85, 0.20],
                  [0.90, 0.25, 0.20]])


def ramp(t):
    """t ∈ [0, 1] → цвета N×3 uint8."""
    t = np.clip(np.asarray(t, float), 0, 1) * (len(_RAMP) - 1)
    i = np.minimum(t.astype(int), len(_RAMP) - 2)
    f = (t - i)[:, None]
    return np.round(255 * (_RAMP[i] * (1 - f) + _RAMP[i + 1] * f)).astype(np.uint8)


def colors(P_canon, P_common_z, d, z0=0.0):
    """
    Цвет на точку для режимов 'height' (P_common_z — высоты точек в общей системе,
    отсчёт от нулевого уровня z0) и 'range'. Иначе None (один цвет на облако).
    Шкала — 2…98 % значений, чтобы одиночные выбросы её не растягивали.
    """
    mode = (d or {}).get('color_mode', 'auto')
    if mode == 'height':
        v = np.asarray(P_common_z, float) - z0
    elif mode == 'range':
        v = np.linalg.norm(np.asarray(P_canon), axis=1)
    else:
        return None
    if not len(v):
        return np.zeros((0, 3), np.uint8)
    lo, hi = np.percentile(v, [2, 98])
    return ramp((v - lo) / max(hi - lo, 1e-6))
