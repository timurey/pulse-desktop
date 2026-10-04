#!/usr/bin/env python3
"""
Калибровка углов крепления лидара к платформе (MOUNT_RPY_DEG) по bag'у.

Каждое направление сканер видит дважды за оборот платформы — в двух полуоборотах.
При неверном угле крепления два полуоборота дают сдвинутые копии стен (двоение).
Скрипт делает deskew при разных углах и ищет те, при которых облака чётных и
нечётных полуоборотов совпадают лучше всего (медиана расстояния точка–плоскость).

Определяются тангаж (вокруг Y лидара) и рыскание (вокруг Z). Поворот вокруг оси
вращения платформы (X лидара, ROTATION_AXIS='x') двоения не даёт — им лишь
поворачивается всё облако, по полуоборотам он не определяется и не меняется.

Скрипт ТОЛЬКО считает и печатает рекомендацию. Константы offline_deskew.MOUNT_RPY_DEG и
TF velodyne в orangepi/ros2/slam_bringup/launch/slam_scanner.launch.py меняются вручную
и синхронно (см. CLAUDE.md).

Usage:
    python calibrate_mount.py BAG [--stride 4] [--range 4] [--roll R]
"""

import io
import sys
import time
import argparse
import contextlib

import numpy as np

from offline_deskew import read_bag, parse_pointcloud2, deskew_cloud, MOUNT_RPY_DEG
from dynamic import pass_id

VOXEL = 0.03
MAX_PAIR = 0.15            # дальше — не одна и та же поверхность (или не видна в другой половине)


def load_frames(bag, stride=4, min_range=0.3, max_range=20.0, min_enc=5):
    """Кадры bag'а (прорежённые) с номером полуоборота. → (кадры, времена угла, углы)."""
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        t, a, clouds = read_bag(bag)
    t, a = np.asarray(t, np.float64), np.asarray(a, np.float64)
    un = np.unwrap(a)
    out = []
    for stamp, msg in clouds:
        pts = parse_pointcloud2(msg)
        if len(pts) == 0:
            continue
        if np.sum((t >= stamp - 110_000_000) & (t <= stamp)) < min_enc:
            continue
        pts = pts[::stride]
        r = np.sqrt(pts['x'].astype(float) ** 2 + pts['y'].astype(float) ** 2 + pts['z'].astype(float) ** 2)
        pts = pts[(r >= min_range) & (r <= max_range)]
        if len(pts):
            out.append((stamp, pts, int(pass_id(np.interp(stamp, t, un)))))
    return out, t, a


def _voxel(P, v=VOXEL):
    q = np.floor(P / v).astype(np.int64)
    _, idx = np.unique(q, axis=0, return_index=True)
    return P[idx]


def halves(frames, t, a, rpy):
    """Облака чётных и нечётных полуоборотов при углах крепления rpy."""
    A, B = [], []
    for stamp, pts, pid in frames:
        c = deskew_cloud(pts.copy(), stamp, t, a, mount_rpy_deg=list(rpy))
        c = c[np.isfinite(c).all(axis=1)]
        (A if pid % 2 == 0 else B).append(c)
    return _voxel(np.vstack(A)), _voxel(np.vstack(B))


def mismatch(A, B):
    """Медиана |n·(b − a)| для точек B у поверхности A (и обратно). → (м, доля сопоставленных)."""
    import open3d as o3d
    out, frac = [], []
    for X, Y in ((A, B), (B, A)):
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(X))
        pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=VOXEL * 4, max_nn=20))
        N = np.asarray(pc.normals)
        nns = o3d.core.nns.NearestNeighborSearch(o3d.core.Tensor(np.ascontiguousarray(X, np.float32)))
        nns.knn_index()
        idx, d2 = nns.knn_search(o3d.core.Tensor(np.ascontiguousarray(Y, np.float32)), 1)
        idx, d = idx.numpy()[:, 0], np.sqrt(d2.numpy()[:, 0])
        m = d < MAX_PAIR
        dist = np.abs(np.sum(N[idx[m]] * (Y[m] - X[idx[m]]), axis=1))
        out.append(np.median(dist) if m.any() else np.inf)
        frac.append(m.mean())
    return float(np.mean(out)), float(np.mean(frac))


def calibrate(bag, stride=4, span=4.0, roll=None, log=print):
    frames, t, a = load_frames(bag, stride)
    if not frames:
        raise ValueError('нет пригодных кадров (данные энкодера?)')
    n_pass = len({f[2] for f in frames})
    r0, p0, y0 = MOUNT_RPY_DEG
    roll = r0 if roll is None else roll
    cache = {}

    def cost(p, y):
        k = (round(p, 4), round(y, 4))
        if k not in cache:
            cache[k] = mismatch(*halves(frames, t, a, (roll, p, y)))
        return cache[k][0]

    t0 = time.time()
    base = cost(p0, y0)
    log(f'кадров {len(frames)}, полуоборотов {n_pass}; оценка занимает ~{time.time() - t0:.1f} c')
    log(f'сейчас MOUNT_RPY_DEG = [{r0}, {p0}, {y0}]: расхождение полуоборотов {base * 1000:.1f} мм')
    p, y = p0, y0
    for step in (1.0, 0.5, 0.2, 0.1, 0.05, 0.02):           # поиск по сетке с уменьшающимся шагом
        lim = span if step == 1.0 else step * 3
        improved = True
        while improved:
            improved = False
            for dp, dy in ((step, 0), (-step, 0), (0, step), (0, -step)):
                q, z = p + dp, y + dy
                if abs(q - p0) > span or abs(z - y0) > span:
                    continue
                if cost(q, z) < cost(p, y) - 1e-6:
                    p, y, improved = q, z, True
        log(f'  шаг {step:.2f}°: тангаж {p:+.2f}°, рыскание {y:+.2f}° → {cost(p, y) * 1000:.2f} мм')
    best = cost(p, y)
    return {'current': [r0, p0, y0], 'best': [roll, round(p, 3), round(y, 3)],
            'mismatch_current_mm': base * 1000, 'mismatch_best_mm': best * 1000,
            'matched': cache[(round(p, 4), round(y, 4))][1], 'frames': len(frames), 'passes': n_pass,
            'evaluations': len(cache)}


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                                # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description='Калибровка MOUNT_RPY_DEG по совпадению полуоборотов')
    ap.add_argument('bag')
    ap.add_argument('--stride', type=int, default=4, help='брать каждую N-ю точку кадра (скорость)')
    ap.add_argument('--range', type=float, default=4.0, help='поиск ± градусов от текущих значений')
    ap.add_argument('--roll', type=float, default=None, help='крен (по полуоборотам не определяется)')
    a = ap.parse_args()
    r = calibrate(a.bag, a.stride, a.range, a.roll)
    print(f"\nрасхождение полуоборотов: {r['mismatch_current_mm']:.1f} мм → {r['mismatch_best_mm']:.1f} мм "
          f"(сопоставлено {r['matched'] * 100:.0f} % точек, оценок {r['evaluations']})")
    print(f"рекомендуется MOUNT_RPY_DEG = {r['best']}   (сейчас {r['current']})")
    print('Изменить вручную и синхронно: offline_deskew.MOUNT_RPY_DEG и TF velodyne в slam_scanner.launch.py.')


if __name__ == '__main__':
    main()
