#!/usr/bin/env python3
"""
Проёмы (окна, двери) в стенах статического скана.

Для каждой крупной стены (planes.py) точки, лежащие в её плоскости, ложатся на
2D-сетку (u — горизонталь вдоль стены, v — вверх). Проём — пустая область,
окружённая стеной (окно) или примыкающая к низу стены (дверь), при условии
что пустота не объясняется тенью: лучи сканера к ней не перекрыты предметами.
Сквозь проём сканер обычно видит что-то за стеной (застеклённое окно частично
прозрачно для VLP-16) — это фиксируется как признак.

Описание проёма: 4 угла в канонической системе скана, центр, ширина × высота,
низ над полом, нормаль стены. Пара проёмов из двух сканов задаёт 4 пары точек
(с точностью до толщины стены по нормали, если стена видна с разных сторон).

Usage:
    python openings.py scan.e57 [--up auto]
"""

import sys
import json
import argparse
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

from planes import analyze_scan


CELL         = 0.05     # м — сетка стены
PLANE_TOL    = 0.06     # м — точки ближе к плоскости считаем стеной
MIN_W, MAX_W = 0.4, 3.5     # м — ширина проёма
MIN_H, MAX_H = 0.4, 3.0     # м — высота проёма
MIN_FILL     = 0.6      # доля пустых ячеек в прямоугольнике проёма
MAX_SHADOW   = 0.3      # доля ячеек, перекрытых предметами перед стеной
WALL_MIN_AREA = 3.0     # м² — стены, в которых ищем проёмы


@dataclass
class Opening:
    wall_id: int
    kind: str            # window | door
    corners: list        # 4×3, канон. система: низ-лево, низ-право, верх-право, верх-лево
    center: list
    width: float
    height: float
    sill: float | None   # низ проёма над полом, м
    normal: list         # нормаль стены (к сканеру)
    through: float       # доля лучей, ушедших за плоскость стены (видно сквозь)
    fill: float

    def to_json(self):
        return asdict(self)


def _components(mask):
    """4-связные компоненты True-ячеек 2D-маски. → (labels, n)."""
    H, W = mask.shape
    lab = -np.ones((H, W), dtype=np.int64)
    n = 0
    for i0, j0 in zip(*np.nonzero(mask)):
        if lab[i0, j0] >= 0:
            continue
        lab[i0, j0] = n
        q = deque([(i0, j0)])
        while q:
            i, j = q.popleft()
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                a, b = i + di, j + dj
                if 0 <= a < H and 0 <= b < W and mask[a, b] and lab[a, b] < 0:
                    lab[a, b] = n
                    q.append((a, b))
        n += 1
    return lab, n


def wall_openings(wall, pts, floor_z=None):
    """Проёмы в одной стене. pts — облако скана в канонической системе."""
    n = np.asarray(wall.normal)
    c = wall.offset
    u_ax, v_ax = np.asarray(wall.u_axis), np.asarray(wall.v_axis)
    if v_ax[2] < 0:
        v_ax = -v_ax
        u_ax = -u_ax
    origin = np.asarray(wall.centroid)
    dist = pts @ n - c                                  # >0 — со стороны сканера
    on = np.abs(dist) < PLANE_TOL
    uv = np.column_stack([(pts[on] - origin) @ u_ax, (pts[on] - origin) @ v_ax])
    if len(uv) < 200:
        return []
    lo = np.percentile(uv, 0.5, axis=0)
    hi = np.percentile(uv, 99.5, axis=0)
    W = int(np.ceil((hi[0] - lo[0]) / CELL)) + 1
    H = int(np.ceil((hi[1] - lo[1]) / CELL)) + 1
    if W * H > 2_000_000:
        return []
    ij = np.floor((uv - lo) / CELL).astype(int)
    ok = (ij[:, 0] >= 0) & (ij[:, 0] < W) & (ij[:, 1] >= 0) & (ij[:, 1] < H)
    occ = np.zeros((H, W), bool)
    occ[ij[ok, 1], ij[ok, 0]] = True
    # закрываем мелкие дыры от дискретизации (разреженность на дальности)
    dil = occ.copy()
    for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        dil |= np.roll(np.roll(occ, di, 0), dj, 1)
    empty = ~dil
    # контур стены по строкам/столбцам: ячейка «внутри», если стена есть с обеих сторон
    rows_any = dil.any(axis=1)
    left = np.where(rows_any, dil.argmax(axis=1), W)
    right = np.where(rows_any, W - 1 - dil[:, ::-1].argmax(axis=1), -1)
    jj = np.arange(W)[None, :]
    inside_h = (jj > left[:, None]) & (jj < right[:, None])
    cols_any = dil.any(axis=0)
    top = np.where(cols_any, H - 1 - dil[::-1, :].argmax(axis=0), -1)
    ii = np.arange(H)[:, None]
    below_top = ii < top[None, :]
    cand = empty & inside_h & below_top
    lab, n_comp = _components(cand)

    # тени и «сквозные» лучи: для каждой ячейки — что видит сканер в её направлении
    front = pts[dist > PLANE_TOL]                       # перед стеной (между сканером и стеной)
    behind = pts[dist < -PLANE_TOL]                     # за стеной
    out = []
    for k in range(n_comp):
        cells = np.argwhere(lab == k)                   # (i=v, j=u)
        i0, j0 = cells.min(axis=0)
        i1, j1 = cells.max(axis=0)
        w, h = (j1 - j0 + 1) * CELL, (i1 - i0 + 1) * CELL
        if not (MIN_W <= w <= MAX_W and MIN_H <= h <= MAX_H):
            continue
        fill = len(cells) / ((i1 - i0 + 1) * (j1 - j0 + 1))
        if fill < MIN_FILL:
            continue
        # углы в 3D
        u0, u1 = lo[0] + j0 * CELL, lo[0] + (j1 + 1) * CELL
        v0, v1 = lo[1] + i0 * CELL, lo[1] + (i1 + 1) * CELL
        P = lambda u, v: origin + u * u_ax + v * v_ax + (c - origin @ n) * n
        corners = np.array([P(u0, v0), P(u1, v0), P(u1, v1), P(u0, v1)])
        center = corners.mean(axis=0)
        # направления из сканера на проём: доля перекрытых (тень) и сквозных
        d_c = center / np.linalg.norm(center)
        half = np.arctan2(0.5 * max(w, h), np.linalg.norm(center))
        cosw = np.cos(half)
        def in_cone(Q):
            r = np.linalg.norm(Q, axis=1)
            return Q[(Q @ d_c) / np.maximum(r, 1e-9) > cosw]
        f, b = in_cone(front), in_cone(behind)
        # «перед стеной» не считаем точки откосов/рамы (ближе 0.4 м к плоскости)
        f = f[(f @ n - c) > 0.4] if len(f) else f
        tot = len(f) + len(b) + 1
        shadow = len(f) / tot
        through = len(b) / tot
        if shadow > MAX_SHADOW:
            continue
        bottom_z = corners[:, 2].min()
        sill = None if floor_z is None else float(bottom_z - floor_z)
        if sill is not None and sill < 0.25:
            if h < 1.7:
                continue            # просвет под мебелью, не проём
            kind = 'door'
        else:
            kind = 'window'
        out.append(Opening(wall.id, kind, corners.tolist(), center.tolist(),
                           float(w), float(h), sill, n.tolist(),
                           float(through), float(fill)))
    return out


def find_openings(res, pts_canon=None):
    """res — planes.analyze_scan; pts_canon — плотное облако (по умолчанию res['down'])."""
    pts = res['down'] if pts_canon is None else pts_canon
    floor_z = res['layers'].get('floor_z')
    area = {p.id: p.area for p in res['planes']}
    found = []
    for p in res['planes']:
        if p.kind == 'wall' and p.area >= WALL_MIN_AREA:
            found += wall_openings(p, pts, floor_z)
    # один проём часто находится в нескольких почти совпадающих плоскостях
    # (стена, уступ, откос) — оставляем вариант из самой крупной стены
    found.sort(key=lambda o: -area[o.wall_id])
    out = []
    for o in found:
        dup = any(np.linalg.norm(np.subtract(o.center, q.center)) < 0.4 and
                  abs(o.width - q.width) < 0.4 and abs(o.height - q.height) < 0.4
                  for q in out)
        if not dup:
            out.append(o)
    return out


def main():
    ap = argparse.ArgumentParser(description="Поиск проёмов в стенах")
    ap.add_argument('scans', nargs='+')
    ap.add_argument('--up', default='auto')
    args = ap.parse_args()
    for s in args.scans:
        res = analyze_scan(s, args.up, cache=False)
        ops = find_openings(res)
        print(f"{Path(s).name}: проёмов {len(ops)}")
        for o in ops:
            sill = f"{o.sill:.2f}" if o.sill is not None else '—'
            print(f"   стена {o.wall_id:3d} {o.kind:6s} {o.width:.2f}×{o.height:.2f} м  "
                  f"низ {sill} м  центр {np.round(o.center, 2)}  сквозь {o.through:.2f}")
        out = Path(s).with_suffix('.openings.json')
        out.write_text(json.dumps([o.to_json() for o in ops], indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == '__main__':
    main()
