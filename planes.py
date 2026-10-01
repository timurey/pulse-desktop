#!/usr/bin/env python3
"""
Опорные плоскости статического скана: вертикаль, пол/потолок, стены.

Все функции работают в «канонической» системе скана: начало — центр сканера,
+Z — вверх. Исходная система облака не меняется: `up_rotation()` даёт матрицу
перехода исходная → каноническая, позы в проекте хранятся в исходных системах.

Вертикаль со знаком определяется по «надиру»: под сканером всегда есть пустой
конус (платформа + штатив заслоняют ~10°), а чуть шире уже виден пол/земля.

Usage:
    python planes.py scan.e57 [--up auto|+x|-z|...] [--voxel 0.05]
"""

import sys
import json
import argparse
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np


# ── параметры ──────────────────────────────────────────────────────────────
VOXEL          = 0.05    # м — вокселизация для поиска плоскостей
RANSAC_DIST    = 0.03    # м — порог инлайера плоскости
RANSAC_ITER    = 1000
MIN_PLANE_AREA = 0.5     # м² — меньше не считаем опорной плоскостью
MAX_PLANES     = 150
GRID_CC        = 0.10    # м — сетка для разбиения копланарных кусков на связные
HORIZ_COS      = 0.97    # |n·up| выше → горизонтальная (≈14°)
VERT_COS       = 0.15    # |n·up| ниже → вертикальная (≈81°)

AXES = {'+x': (0, 1.0), '-x': (0, -1.0), '+y': (1, 1.0),
        '-y': (1, -1.0), '+z': (2, 1.0), '-z': (2, -1.0)}


# ── загрузка ───────────────────────────────────────────────────────────────
def load_points(path) -> np.ndarray:
    """Nx3 float64 из .e57 / .pcd / .ply / .las / .laz / .npz (deskewed)."""
    path = Path(path)
    suf = path.suffix.lower()
    if suf == '.e57':
        import pye57
        e57 = pye57.E57(str(path), mode='r')
        parts = []
        for i in range(e57.scan_count):
            raw = e57.read_scan_raw(i)
            parts.append(np.column_stack([raw['cartesianX'], raw['cartesianY'],
                                          raw['cartesianZ']]))
        e57.close()
        pts = np.vstack(parts)
    elif suf in ('.pcd', '.ply'):
        import open3d as o3d
        pts = np.asarray(o3d.io.read_point_cloud(str(path)).points)
    elif suf in ('.las', '.laz'):
        import laspy
        las = laspy.read(str(path))
        pts = np.column_stack([las.x, las.y, las.z])
    elif suf == '.npz':
        data = np.load(path, allow_pickle=True)
        pts = np.vstack([np.asarray(p)[:, :3] for p in data['points']])
    else:
        raise ValueError(f"Unsupported format: {suf}")
    pts = np.asarray(pts, dtype=np.float64)
    return pts[np.isfinite(pts).all(axis=1)]


# ── вертикаль ──────────────────────────────────────────────────────────────
def detect_up(pts: np.ndarray):
    """
    Возвращает (label, info): label — '+x' … '-z', направление «вверх».

    Надир: пустой конус 10° и непустое кольцо 10–20° (пол/земля у штатива).
    Среди шести осей выбирается та, противоположная которой похожа на надир.
    """
    r = np.linalg.norm(pts, axis=1)
    ok = r > 0.2
    u = pts[ok] / r[ok, None]
    c10, c20 = np.cos(np.radians(10)), np.cos(np.radians(20))
    stats = {}
    for label, (i, s) in AXES.items():
        down = -s * u[:, i]                      # косинус с направлением «вниз»
        stats[label] = (int((down > c10).sum()), int((down > c20).sum()))
    total = len(u)
    best, best_score = None, -1.0
    for label, (n10, n20) in stats.items():
        ring = n20 - n10
        if n10 > 1e-4 * total or ring < 20:
            continue
        score = ring / (n10 + 1)
        if score > best_score:
            best, best_score = label, score
    if best is None:
        raise RuntimeError(f"Не удалось определить вертикаль: {stats}")
    return best, {'nadir_cone10': stats[best][0], 'nadir_cone20': stats[best][1],
                  'all': stats}


def up_rotation(label: str) -> np.ndarray:
    """Собственная матрица поворота R: R @ up = +Z (исходная → каноническая)."""
    i, s = AXES[label]
    up = np.zeros(3); up[i] = s
    z = np.array([0.0, 0.0, 1.0])
    if np.allclose(up, z):
        return np.eye(3)
    if np.allclose(up, -z):
        return np.diag([1.0, -1.0, -1.0])        # 180° вокруг X
    axis = np.cross(up, z); axis /= np.linalg.norm(axis)
    ang = np.arccos(np.clip(up @ z, -1, 1))
    K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]],
                  [-axis[1], axis[0], 0]])
    return np.eye(3) + np.sin(ang) * K + (1 - np.cos(ang)) * K @ K


# ── плоскости ──────────────────────────────────────────────────────────────
@dataclass
class Plane:
    id: int
    kind: str              # floor | ceiling | wall | other
    normal: list           # единичная, смотрит на сканер (n·0 > c)
    offset: float          # c в n·x = c; -c = расстояние от сканера
    centroid: list
    area: float            # м², по занятым ячейкам сетки
    extent: list           # [ширина вдоль u, высота вдоль v], м
    u_axis: list           # оси 2D-системы плоскости
    v_axis: list
    n_points: int
    rms: float
    inliers: np.ndarray = field(default=None, repr=False)   # индексы в даунсемпле

    def to_json(self):
        d = asdict(self)
        d.pop('inliers')
        return d


def plane_axes(normal):
    """u — горизонталь в плоскости (для стен), v = n × u."""
    n = np.asarray(normal, dtype=float)
    z = np.array([0.0, 0.0, 1.0])
    u = np.cross(z, n)
    if np.linalg.norm(u) < 1e-6:                 # горизонтальная плоскость
        u = np.array([1.0, 0.0, 0.0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return u, v / np.linalg.norm(v)


def grid_components(uv: np.ndarray, cell: float):
    """Связные компоненты (8-связность) точек на 2D-сетке. → (labels, n)."""
    ij = np.floor(uv / cell).astype(np.int64)
    keys, inv = np.unique(ij, axis=0, return_inverse=True)
    inv = inv.ravel()
    index = {tuple(k): n for n, k in enumerate(keys)}
    comp = -np.ones(len(keys), dtype=np.int64)
    n_comp = 0
    for start in range(len(keys)):
        if comp[start] >= 0:
            continue
        comp[start] = n_comp
        q = deque([start])
        while q:
            k = q.popleft()
            i, j = keys[k]
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    nb = index.get((i + di, j + dj))
                    if nb is not None and comp[nb] < 0:
                        comp[nb] = n_comp
                        q.append(nb)
        n_comp += 1
    return comp[inv], n_comp


def _make_plane(pid, pts, idx, n, floor_z, ceil_z):
    sub = pts[idx]
    centroid = sub.mean(axis=0)
    # уточнение нормали по SVD
    _, _, vt = np.linalg.svd(sub - centroid, full_matrices=False)
    n = vt[2] if vt[2] @ n >= 0 else -vt[2]
    c = float(n @ centroid)
    if c > 0:                                    # нормаль к сканеру
        n, c = -n, -c
    resid = sub @ n - c
    u, v = plane_axes(n)
    uv = np.column_stack([(sub - centroid) @ u, (sub - centroid) @ v])
    cells = np.unique(np.floor(uv / GRID_CC).astype(np.int64), axis=0)
    area = len(cells) * GRID_CC ** 2
    lo, hi = np.percentile(uv, 1, axis=0), np.percentile(uv, 99, axis=0)
    cz = abs(n[2])
    if cz > HORIZ_COS:
        # пол смотрит вверх (на сканер сверху), потолок — вниз
        kind = 'floor' if n[2] > 0 else 'ceiling'
    elif cz < VERT_COS:
        kind = 'wall'
    else:
        kind = 'other'
    return Plane(pid, kind, n.tolist(), c, centroid.tolist(), float(area),
                 (hi - lo).tolist(), u.tolist(), v.tolist(), int(len(idx)),
                 float(np.sqrt(np.mean(resid ** 2))), inliers=np.asarray(idx))


def extract_planes(pts_canon: np.ndarray, voxel=VOXEL, max_planes=MAX_PLANES,
                   min_area=MIN_PLANE_AREA, seed=0):
    """
    Итеративный RANSAC + разбиение копланарных точек на связные куски.
    Вход — облако в канонической системе. → (down_pts, planes).
    """
    import open3d as o3d
    o3d.utility.random.seed(seed)
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts_canon))
    pcd = pcd.voxel_down_sample(voxel)
    down = np.asarray(pcd.points)
    remaining = np.arange(len(down))
    min_pts = int(min_area / voxel ** 2 * 0.5)
    planes = []
    misses = 0
    while len(planes) < max_planes and len(remaining) > min_pts and misses < 8:
        sub = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(down[remaining]))
        model, inl = sub.segment_plane(RANSAC_DIST, 3, RANSAC_ITER)
        inl = np.asarray(inl)
        if len(inl) < min_pts:
            break
        n = np.asarray(model[:3], dtype=float)
        n /= np.linalg.norm(n)
        idx = remaining[inl]
        u, v = plane_axes(n)
        uv = np.column_stack([down[idx] @ u, down[idx] @ v])
        labels, n_comp = grid_components(uv, GRID_CC * 1.5)
        taken = np.zeros(len(idx), dtype=bool)
        added = 0
        for k in range(n_comp):
            m = labels == k
            if m.sum() < min_pts:
                continue
            p = _make_plane(len(planes), down, idx[m], n, None, None)
            if p.area >= min_area:
                planes.append(p)
                taken |= m
                added += 1
        # убираем все инлайеры (и мелкие куски, чтобы RANSAC не находил их снова)
        remaining = np.setdiff1d(remaining, idx, assume_unique=True)
        misses = 0 if added else misses + 1
    planes.sort(key=lambda p: -p.area)
    for i, p in enumerate(planes):
        p.id = i
    return down, planes


def horizontal_layers(pts_canon: np.ndarray, voxel=VOXEL, bin_size=0.05):
    """
    Высоты пола/земли и потолка по гистограмме точек горизонтальных поверхностей.
    Работает, даже когда RANSAC не выделил пол целиком. → dict.
    """
    import open3d as o3d
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts_canon))
    pcd = pcd.voxel_down_sample(voxel)
    pcd.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 3, max_nn=30))
    p = np.asarray(pcd.points)
    nz = np.abs(np.asarray(pcd.normals)[:, 2])
    z = p[nz > HORIZ_COS, 2]
    out = {'floor_z': None, 'floor_area': 0.0, 'ceiling_z': None, 'ceiling_area': 0.0}
    for key, sel in (('floor', z < -0.3), ('ceiling', z > 0.3)):
        zz = z[sel]
        if len(zz) < 50:
            continue
        bins = np.arange(zz.min(), zz.max() + bin_size, bin_size)
        if len(bins) < 2:
            bins = np.array([zz.min(), zz.min() + bin_size])
        h, e = np.histogram(zz, bins=bins)
        # пол — самый нижний мощный слой, потолок — самый верхний
        strong = np.where(h > 0.3 * h.max())[0]
        k = strong[0] if key == 'floor' else strong[-1]
        layer = zz[(zz >= e[k] - bin_size) & (zz < e[k + 1] + bin_size)]
        out[f'{key}_z'] = float(np.median(layer))
        out[f'{key}_area'] = float(len(layer) * voxel ** 2)
    return out


# ── анализ скана целиком ───────────────────────────────────────────────────
def analyze_scan(path, up='auto', voxel=VOXEL, cache=True):
    """
    Полный анализ: вертикаль, слои пола/потолка, плоскости.
    Результат кешируется в <scan>.planes.json (без индексов инлайеров).
    → dict с ключами up, R_up, layers, planes, down (облако в канонической системе).
    """
    path = Path(path)
    pts = load_points(path)
    if up == 'auto':
        up_label, up_info = detect_up(pts)
    else:
        up_label, up_info = up, {}
    R = up_rotation(up_label)
    canon = pts @ R.T
    layers = horizontal_layers(canon, voxel)
    down, planes = extract_planes(canon, voxel)
    res = {'scan': path.name, 'up': up_label, 'up_info': up_info,
           'R_up': R.tolist(), 'voxel': voxel, 'n_points': int(len(pts)),
           'layers': layers, 'planes': planes, 'down': down}
    if cache:
        out = path.with_suffix('.planes.json')
        js = {k: v for k, v in res.items() if k not in ('planes', 'down')}
        js['planes'] = [p.to_json() for p in planes]
        out.write_text(json.dumps(js, indent=1, ensure_ascii=False))
        res['cache'] = str(out)
    return res


def summarize(res):
    L = res['layers']
    walls = [p for p in res['planes'] if p.kind == 'wall']
    hor = [p for p in res['planes'] if p.kind in ('floor', 'ceiling')]
    az = sorted(round(float(np.degrees(np.arctan2(p.normal[1], p.normal[0]))) % 360)
                for p in walls[:10])
    fz = f"{L['floor_z']:+.2f}" if L['floor_z'] is not None else '—'
    cz = f"{L['ceiling_z']:+.2f}" if L['ceiling_z'] is not None else '—'
    return (f"{res['scan']}: up={res['up']}  пол {fz} м  потолок {cz} м  "
            f"плоскостей {len(res['planes'])} (гориз {len(hor)}, стен {len(walls)}, "
            f"S стен {sum(p.area for p in walls):.0f} м²)  азимуты стен {az}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('scans', nargs='+')
    ap.add_argument('--up', default='auto', choices=['auto'] + list(AXES))
    ap.add_argument('--voxel', type=float, default=VOXEL)
    args = ap.parse_args()
    for s in args.scans:
        res = analyze_scan(s, args.up, args.voxel)
        print(summarize(res))


if __name__ == '__main__':
    main()
