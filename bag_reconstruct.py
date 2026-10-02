#!/usr/bin/env python3
"""
Реконструкция статического скана из bag (MCAP) — облако одной точки стояния.

Тот же проверенный путь, что `world_map.py` на Mac:
  - угол платформы: JointState по времени записи bag (log_time — часы хоста,
    тот же домен, что у лидара; read_bag из offline_deskew);
  - deskew каждой точки VLP-16 (offline_deskew.deskew_cloud) — в систему
    лидара при θ платформы = 0 (вертикаль — ось X лидара);
  - наклон оси вращения — по геометрии самого скана: пол/земля горизонтальны,
    стены вертикальны (plane_register.level_correction). IMU здесь только для
    диагностики: плату IMU переставляли (сейчас Z вверх), а прежняя логика
    world_map.read_imu_tilt рассчитана на установку X вверх (режим tilt='imu_x');
  - кадры без достаточного числа отсчётов энкодера пропускаются;
  - фильтр дальности, вокселизация.
Без ROS: нужны mcap, mcap-ros2-support, zstandard (кросс-платформенно).

Usage:
    python bag_reconstruct.py BAG_DIR_OR_MCAP [-o scan.ply] [--voxel 0.01] [--max-range 60]
    python bag_reconstruct.py bags/ --out-dir scans/          # все bag'и папки
"""

import io
import sys
import json
import time
import argparse
import contextlib
from pathlib import Path

import numpy as np

from offline_deskew import (read_bag, parse_pointcloud2, deskew_cloud,
                            ROTATION_AXIS, ANGLE_OFFSET_DEG, ROTATION_CENTER,
                            INVERT_ROTATION, ENCODER_TIME_OFFSET_MS, MOUNT_RPY_DEG, MOUNT_AXES)

DEFAULTS = {'voxel': 0.01, 'min_range': 0.3, 'max_range': 60.0, 'min_enc_samples': 5}


# ── поиск bag'ов ─────────────────────────────────────────────────────────
def _mcaps(p):
    return list(p.glob('*.mcap')) + list(p.glob('*.mcap.zstd'))


def is_bag(p):
    """
    Bag: файл .mcap[.zstd] или папка rosbag2 (metadata.yaml), либо папка ровно с одним
    .mcap и без вложенных bag'ов. Папка с несколькими bag'ами bag'ом не считается.
    """
    p = Path(p)
    if p.is_file():
        return p.name.endswith('.mcap') or p.name.endswith('.mcap.zstd')
    if not p.is_dir():
        return False
    if (p / 'metadata.yaml').exists() and _mcaps(p):
        return True
    has_sub = any(c.is_dir() and _mcaps(c) for c in p.iterdir())
    return len(_mcaps(p)) == 1 and not has_sub


def find_bags(path):
    """Bag (папка rosbag2 или .mcap) или папка с bag'ами → список bag'ов."""
    p = Path(path)
    if is_bag(p):
        return [p]
    if p.is_dir():
        return sorted(c for c in p.iterdir() if is_bag(c))
    return []


def bag_name(bag):
    p = Path(bag)
    name = p.name
    for suf in ('.mcap.zstd', '.mcap'):
        if name.endswith(suf):
            name = name[:-len(suf)]
            if name.endswith('_0'):
                name = name[:-2]
    return name


# ── обработка кадров (без ввода-вывода — тестируется синтетикой) ─────────
def process_frames(frames, angle_times, angle_values, R_tilt=None, voxel=0.01, min_range=0.3,
                   max_range=60.0, min_enc_samples=5, progress=None, deskew_params=None):
    """
    frames: [(stamp_ns, structured array x,y,z[,time])]; угол — времена (нс) и радианы.
    → (точки N×3 float32, статистика).
    """
    import open3d as o3d
    dp = dict(rotation_axis=ROTATION_AXIS, angle_offset_deg=ANGLE_OFFSET_DEG,
              rotation_center=ROTATION_CENTER, invert_rotation=INVERT_ROTATION,
              encoder_time_offset_ms=ENCODER_TIME_OFFSET_MS, mount_rpy_deg=MOUNT_RPY_DEG,
              mount_axes=MOUNT_AXES)
    dp.update(deskew_params or {})
    angle_times = np.asarray(angle_times, np.float64)
    angle_values = np.asarray(angle_values, np.float64)
    parts, used, skipped = [], 0, 0
    for i, (stamp, pts) in enumerate(frames):
        if progress and i % 10 == 0:
            progress(i / max(1, len(frames)), f'кадр {i + 1}/{len(frames)}')
        if len(pts) == 0:
            skipped += 1
            continue
        win = np.sum((angle_times >= stamp - 110_000_000) & (angle_times <= stamp))
        if win < min_enc_samples:          # мало отсчётов энкодера — deskew ненадёжен
            skipped += 1
            continue
        c = deskew_cloud(pts.copy(), stamp, angle_times, angle_values, **dp)
        c = c[np.isfinite(c).all(axis=1)]
        r = np.linalg.norm(c, axis=1)
        c = c[(r >= min_range) & (r <= max_range)]
        if len(c):
            parts.append(c.astype(np.float32))
            used += 1
    if not parts:
        raise ValueError('нет пригодных кадров (нет данных энкодера или облаков)')
    P = np.vstack(parts)
    if R_tilt is not None:
        P = (P @ np.asarray(R_tilt, np.float32).T)
    n_raw = len(P)
    if voxel and voxel > 0:
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P.astype(np.float64)))
        P = np.asarray(pc.voxel_down_sample(voxel).points, np.float32)
    span = float(np.ptp(np.unwrap(angle_values))) if len(angle_values) else 0.0
    return P, {'frames_total': len(frames), 'frames_used': used, 'frames_skipped': skipped,
               'points_raw': int(n_raw), 'points': int(len(P)),
               'rotations': round(span / (2 * np.pi), 2)}


# ── bag целиком ──────────────────────────────────────────────────────────
def imu_gravity(bag, n=300):
    """Средний вектор ускорения /imu (в единицах датчика) — для диагностики."""
    from offline_deskew import resolve_mcap_path
    from mcap_ros2.reader import read_ros2_messages
    acc = []
    try:
        for m in read_ros2_messages(str(resolve_mcap_path(bag)), topics=['/imu']):
            a = m.ros_msg.linear_acceleration
            acc.append([a.x, a.y, a.z])
            if len(acc) >= n:
                break
    except Exception:                                    # noqa: BLE001
        return None
    return np.mean(acc, axis=0) if acc else None


def level_by_geometry(P):
    """
    Поправка наклона оси вращения по геометрии скана. P — система лидара при θ=0
    (вертикаль ≈ +X). → (R 3×3 для точек P, информация).
    """
    import planes
    import plane_register as pr
    R_up = planes.up_rotation('+x')                      # система лидара → канон. (Z вверх)
    canon = np.asarray(P, np.float64) @ R_up.T
    _, pl = planes.extract_planes(canon, voxel=0.08, max_planes=60)
    Rc, info = pr.level_correction([p.normal for p in pl if p.area >= 1.0],
                                   [p.area for p in pl if p.area >= 1.0])
    if info['horizontal'] + info['walls'] < 3 or info['tilt_deg'] > 15:
        return np.eye(3), dict(info, applied=False)
    return R_up.T @ Rc @ R_up, dict(info, applied=True)


def reconstruct(bag, voxel=DEFAULTS['voxel'], min_range=DEFAULTS['min_range'],
                max_range=DEFAULTS['max_range'], tilt='geometry', progress=None):
    """
    bag → (точки N×3 float32 в системе лидара при θ=0, метаданные).
    tilt: 'geometry' (по полу/стенам, по умолчанию) | 'imu_x' (как world_map: IMU осью X
    вверх) | 'none'.
    """
    t0 = time.time()
    if progress:
        progress(0.0, f'чтение {bag_name(bag)}')
    log = io.StringIO()
    with contextlib.redirect_stdout(log):          # read_bag печатает диагностику
        angle_times, angle_values, clouds = read_bag(bag)
        R_tilt, tilt_deg = (np.eye(3), 0.0)
        if tilt == 'imu_x':
            from world_map import read_imu_tilt
            R_tilt, tilt_deg = read_imu_tilt(bag)
    angle_source = next((l.split(':', 1)[1].strip() for l in log.getvalue().splitlines()
                         if l.startswith('Angle source:')), '')
    if len(angle_times) < 2:
        raise ValueError(f'в bag нет данных угла платформы ({angle_source or "нет топиков"})')
    frames = [(stamp, parse_pointcloud2(msg)) for stamp, msg in clouds]

    def prog(f, msg):
        if progress:
            progress(0.1 + 0.9 * f, f'{bag_name(bag)}: {msg}')
    P, stats = process_frames(frames, angle_times, angle_values, R_tilt, voxel, min_range,
                              max_range, progress=prog)
    level = {'method': tilt}
    if tilt == 'geometry':
        if progress:
            progress(0.97, f'{bag_name(bag)}: выравнивание по полу и стенам')
        R_geo, info = level_by_geometry(P)
        P = (P @ np.asarray(R_geo, np.float32).T)
        level.update(tilt_deg=round(info['tilt_deg'], 2), applied=info['applied'],
                     horizontal=info['horizontal'], walls=info['walls'])
    elif tilt == 'imu_x':
        level.update(tilt_deg=round(float(tilt_deg), 2), applied=True)
    g = imu_gravity(bag)
    imu_info = None if g is None else {
        'gravity': [round(float(x), 3) for x in g],
        'tilt_from_axis_deg': round(float(np.degrees(np.arccos(
            np.max(np.abs(g)) / max(np.linalg.norm(g), 1e-9)))), 2)}
    meta = {'bag': str(Path(bag).resolve()), 'name': bag_name(bag), 'angle_source': angle_source,
            'level': level, 'imu': imu_info,
            'params': {'voxel': voxel, 'min_range': min_range, 'max_range': max_range,
                       'tilt': tilt},
            'processed': time.strftime('%Y-%m-%d %H:%M:%S'),
            'seconds': round(time.time() - t0, 1), **stats}
    return P, meta


def save_scan(P, path, meta=None):
    """Облако → .ply (или .pcd/.e57); метаданные — рядом, <имя>.json."""
    import open3d as o3d
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == '.e57':
        import pye57
        e57 = pye57.E57(str(path), mode='w')
        P64 = np.asarray(P, np.float64)
        e57.write_scan_raw({'cartesianX': P64[:, 0], 'cartesianY': P64[:, 1],
                            'cartesianZ': P64[:, 2]})
        e57.close()
    else:
        o3d.io.write_point_cloud(str(path), o3d.geometry.PointCloud(
            o3d.utility.Vector3dVector(np.asarray(P, np.float64))))
    if meta is not None:
        path.with_suffix('.json').write_text(json.dumps(meta, indent=1, ensure_ascii=False),
                                             encoding='utf-8')
    return path


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                                # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description='Реконструкция статических сканов из bag')
    ap.add_argument('path', help='bag (папка rosbag2 или .mcap[.zstd]) или папка с bag\'ами')
    ap.add_argument('-o', '--output', help='файл скана (для одного bag)')
    ap.add_argument('--out-dir', default='scans', help='папка для сканов (по умолчанию scans/)')
    ap.add_argument('--format', default='ply', choices=['ply', 'pcd', 'e57'])
    ap.add_argument('--voxel', type=float, default=DEFAULTS['voxel'])
    ap.add_argument('--min-range', type=float, default=DEFAULTS['min_range'])
    ap.add_argument('--max-range', type=float, default=DEFAULTS['max_range'])
    ap.add_argument('--tilt', default='geometry', choices=['geometry', 'imu_x', 'none'],
                    help='поправка наклона: по полу/стенам (по умолчанию), по IMU осью X, нет')
    a = ap.parse_args()
    bags = find_bags(a.path)
    if not bags:
        sys.exit(f'bag не найден: {a.path}')
    for b in bags:
        P, meta = reconstruct(b, a.voxel, a.min_range, a.max_range, a.tilt,
                              progress=lambda f, m: None)
        out = Path(a.output) if (a.output and len(bags) == 1) else \
            Path(a.out_dir) / f"{meta['name']}.{a.format}"
        save_scan(P, out, meta)
        lv = meta['level']
        print(f"{meta['name']}: {meta['points']:,} точек, кадров {meta['frames_used']}/"
              f"{meta['frames_total']}, оборотов {meta['rotations']}, наклон {lv.get('tilt_deg', 0)} "
              f"град ({lv['method']}{'' if lv.get('applied', True) else ', не применён'}), "
              f"{meta['seconds']} c -> {out}")


if __name__ == '__main__':
    main()
