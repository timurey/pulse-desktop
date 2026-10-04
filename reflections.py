#!/usr/bin/env python3
"""
Удаление зеркальных отражений (стёкла окон, глянцевый пол).

VLP-16 видит в стекле отражение помещения: за окном появляется «вторая
комната» — зеркальная копия настоящей. Признак отражения: точка за зеркалом,
отражённая обратно относительно плоскости зеркала, попадает на реальную
поверхность помещения (или за неё, в невидимую сканеру зону). Точка,
действительно видимая сквозь стекло (улица), после отражения оказывается в
пустом пространстве, которое сканер видел, — её оставляем.

Зеркала-кандидаты:
  - окна (openings.py): стекло утоплено в проём, глубина ищется перебором
    по максимуму совпадения отражённых точек с реальными;
  - пол: точки ниже пола, отражённые относительно его плоскости (приямки
    при этом остаются — их отражение висит в пустом воздухе).

Usage:
    python reflections.py scan.e57 [-o cleaned.ply]
"""

import argparse
from pathlib import Path

import numpy as np

from planes import analyze_scan, load_points
from openings import find_openings
from plane_register import RangeImage


GLASS_DEPTHS   = np.arange(0.0, 0.85, 0.05)   # м — глубина стекла от грани стены
MATCH_DIST     = 0.05      # м — «совпало с реальной поверхностью»
MIN_PEAK       = 0.30      # доля совпадений, чтобы считать окно зеркалом
FREE_MARGIN    = 0.15      # м — запас при проверке «отражение в пустоте»
MIN_WINDOW_AREA = 1.0     # м² — меньшие проёмы как зеркала не рассматриваем
FRONT_GAP      = 0.05      # м — «перед стеной/над полом» (как прежний отбор облака)
MIN_THROUGH    = 1000      # точек сквозь проём (в даунсемпле), иначе статистики мало


def _through_aperture(pts, n, c, corners, pad=0.05):
    """Маска точек за плоскостью, луч к которым проходит через прямоугольник проёма."""
    dist = pts @ n - c
    with np.errstate(divide='ignore', invalid='ignore'):
        s = c / (pts @ n)
    hit = pts * s[:, None]
    C = np.asarray(corners)
    u, v = C[1] - C[0], C[3] - C[0]
    a = (hit - C[0]) @ u / (u @ u)
    b = (hit - C[0]) @ v / (v @ v)
    return (dist < -0.1) & (s > 0) & (a > -pad) & (a < 1 + pad) & (b > -pad) & (b < 1 + pad)


def _mirror(P, n, c):
    return P - 2 * ((P @ n) - c)[:, None] * n


class SurfaceIndex:
    """
    «Есть ли реальная точка скана ближе MATCH_DIST» — индекс строится один раз
    на скан (тензорный поиск Open3D). Раньше для каждого окна и каждой глубины
    стекла строилась своя структура по облаку (на фасаде с 79 окнами — минута).
    """

    def __init__(self, pts):
        import open3d as o3d
        self._o3d = o3d
        self.nns = o3d.core.nns.NearestNeighborSearch(o3d.core.Tensor(
            np.ascontiguousarray(pts, dtype=np.float32)))
        self.nns.hybrid_index(MATCH_DIST)

    def near(self, Q):
        if len(Q) == 0:
            return np.zeros(0, bool)
        _, _, cnt = self.nns.hybrid_search(self._o3d.core.Tensor(
            np.ascontiguousarray(Q, dtype=np.float32)), MATCH_DIST, 1)
        return cnt.numpy() > 0


def _ghost_mask(cand, n, c_mirror, surf, ri, strict=False, c_front=None):
    """
    Призраки среди cand: отражение легло на поверхность или за неё.
    strict=True — только «легло на поверхность» (для пола: иначе стенки
    приямков, отражённые в невидимую сканеру зону, ошибочно удаляются).
    surf — SurfaceIndex по облаку скана. c_front — плоскость стены/пола (n·x = c_front):
    совпадением считается только отражение, легшее перед ней, — как сравнение с
    облаком «перед стеной» (иначе отражения в откос проёма совпадают с рамой).
    """
    mir = _mirror(cand, n, c_mirror)
    on_surface = surf.near(mir)
    if c_front is not None:
        on_surface &= (mir @ n - c_front) > 2 * FRONT_GAP
    idx, r = ri._index(mir)
    rA = ri.img[idx]
    with np.errstate(invalid='ignore'):
        in_free = np.isfinite(rA) & (r < rA - np.maximum(FREE_MARGIN, 0.03 * rA))
    ghost = on_surface if strict else (on_surface | ~in_free)
    return ghost, float(on_surface.mean())


def find_reflections(pts, res, verbose=False):
    """
    pts — облако в канонической системе скана (любой плотности),
    res — planes.analyze_scan. → (маска призраков для pts, отчёт).
    """
    import open3d as o3d
    down = res['down']
    ri = RangeImage(down)
    surf = SurfaceIndex(down)            # отражённые точки ложатся на сторону сканера —
                                         # сравнение со всем облаком равносильно «перед окном»
    ghost = np.zeros(len(pts), bool)
    report = []
    for o in find_openings(res):
        if o.kind != 'window' or o.width * o.height < MIN_WINDOW_AREA:
            continue
        n = np.asarray(o.normal)
        c = float(n @ np.asarray(o.corners)[0])
        thr_d = down[_through_aperture(down, n, c, o.corners)]
        if len(thr_d) < MIN_THROUGH:
            continue
        # глубина стекла — по максимуму совпадения
        best = (0.0, 0.0)
        for g in GLASS_DEPTHS:
            _, frac = _ghost_mask(thr_d, n, c - g, surf, ri, c_front=c)
            best = max(best, (frac, g))
        frac, g = best
        is_mirror = frac >= MIN_PEAK
        rep = {'kind': 'window', 'size': [o.width, o.height], 'center': o.center,
               'glass_depth': g, 'match': frac, 'mirror': is_mirror, 'removed': 0}
        if is_mirror:
            m = _through_aperture(pts, n, c, o.corners) & ((pts @ n) < c - g)
            gm, _ = _ghost_mask(pts[m], n, c - g, surf, ri, c_front=c)
            idx = np.nonzero(m)[0][gm]
            ghost[idx] = True
            rep['removed'] = int(len(idx))
        report.append(rep)
        if verbose:
            print(f"   окно {o.width:.2f}×{o.height:.2f}: совпадение {frac:.2f} при стекле "
                  f"{g:.2f} м → {'ЗЕРКАЛО, убрано ' + str(rep['removed']) if is_mirror else 'не зеркало'}")
    # пол
    fz = res['layers'].get('floor_z')
    if fz is not None:
        n = np.array([0.0, 0.0, 1.0])
        below_d = down[down[:, 2] < fz - 0.1]
        if len(below_d) >= 50:
            _, frac = _ghost_mask(below_d, n, fz, surf, ri, c_front=fz)
            rep = {'kind': 'floor', 'floor_z': fz, 'match': frac, 'mirror': frac >= MIN_PEAK,
                   'removed': 0}
            if rep['mirror']:
                m = pts[:, 2] < fz - 0.1
                gm, _ = _ghost_mask(pts[m], n, fz, surf, ri, strict=True, c_front=fz)
                idx = np.nonzero(m)[0][gm]
                ghost[idx] = True
                rep['removed'] = int(len(idx))
            report.append(rep)
            if verbose:
                print(f"   пол: совпадение отражения {frac:.2f} → "
                      f"{'ЗЕРКАЛО, убрано ' + str(rep['removed']) if rep['mirror'] else 'не зеркало'}")
    return ghost, report


def clean_scan(path, up='auto', verbose=False):
    """→ (точки без отражений в ИСХОДНОЙ системе скана, отчёт)."""
    res = analyze_scan(path, up, cache=False)
    R = np.asarray(res['R_up'])
    pts = load_points(path)
    ghost, report = find_reflections(pts @ R.T, res, verbose)
    return pts[~ghost], report


def main():
    ap = argparse.ArgumentParser(description="Удаление зеркальных отражений")
    ap.add_argument('scans', nargs='+')
    ap.add_argument('--up', default='auto')
    ap.add_argument('-o', '--output', help='для одного скана: файл .ply/.pcd с очищенным облаком')
    args = ap.parse_args()
    for s in args.scans:
        print(Path(s).name)
        pts, report = clean_scan(s, args.up, verbose=True)
        if args.output and len(args.scans) == 1:
            import fileio
            fileio.write_cloud(args.output, pts)


if __name__ == '__main__':
    main()
