#!/usr/bin/env python3
"""
Качество совмещения склейки: где одна и та же поверхность из разных сканов
расходится («толстая» стена, двоение пола).

Два способа (результат одинаковой формы — ячейки с толщиной):
  plane_cells — по найденным плоскостям: плоскости разных сканов, лежащие на одной
                поверхности, объединяются; общая плоскость; в ячейках ~20 см для каждого
                скана медиана расстояния до неё; толщина = max − min по сканам.
                Видно, где именно стены расходятся (например, к дальнему концу).
  local_cells — по всем точкам: воксели ~20 см с точками хотя бы двух сканов, плоский
                участок у каждого скана; толщина = разброс средних положений сканов
                вдоль общей нормали. Работает и вне больших плоскостей.

Толщина считается один раз; порог (ползунок) применяется отдельно —
`over_threshold` / `colors` / `problem_areas`. Всё — в общей системе сеанса.
"""

from dataclasses import dataclass, field

import numpy as np

import plane_register as pr
import planes as planes_mod
import manual_clean

KINDS = ('wall', 'floor', 'ceiling')
MIN_CELL_PTS = 3            # точек скана в ячейке, чтобы его медиана что-то значила


@dataclass
class Quality:
    method: str
    cell: float
    P: np.ndarray                       # точки ячеек (общая система), M×3
    cell_of: np.ndarray                 # номер ячейки для каждой точки, M
    thick: np.ndarray                   # толщина ячейки, м, K
    center: np.ndarray                  # центр ячейки, K×3
    normal: np.ndarray                  # нормаль поверхности, K×3
    scans: list                         # для каждой ячейки — кортеж id сканов
    kind: list                          # вид поверхности ячейки
    info: dict = field(default_factory=dict)

    def __len__(self):
        return len(self.thick)


def _empty(method, cell, info=None):
    z3 = np.zeros((0, 3))
    return Quality(method, cell, z3, np.zeros(0, int), np.zeros(0), z3, z3, [], [], info or {})


def _clean_plane_mask(sc):
    """Точки res['down'], которые не отражения и не удалены вручную."""
    down = sc.res['down']
    drop = sc.ghost_mask() if sc.clean else np.zeros(len(down), bool)
    if sc.erase:
        drop = drop | manual_clean.inside_regions(down, sc.erase)
    keep_fn = getattr(sc, 'keep_mask', None)
    if keep_fn is not None and getattr(sc, 'drop', None) is not None and len(sc.drop):
        drop = drop | ~keep_fn(down)
    return ~drop


def _pose(session, sc, poses):
    """Поза скана: из poses (подмена — например, подвижный в ручной стыковке) или сеанса."""
    if poses and sc.id in poses:
        return np.asarray(poses[sc.id], float)
    return session.Tc(sc)


def _scans(session, scan_ids=None, poses=None):
    out = []
    for sc in session.scans:
        placed = sc.pose is not None or (poses and sc.id in poses)
        if not placed or not sc.analyzed or (not sc.visible and scan_ids is None):
            continue
        if scan_ids is not None and sc.id not in scan_ids:
            continue
        out.append(sc)
    return out


# ── по плоскостям ──────────────────────────────────────────────────────────
def _plane_list(session, scans, min_area, poses=None):
    out = []
    for sc in scans:
        T = _pose(session, sc, poses)
        R, t = T[:3, :3], T[:3, 3]
        down = sc.res['down']
        keep = _clean_plane_mask(sc)
        for p in sc.planes:
            if p.area < min_area or p.kind not in KINDS:
                continue
            idx = np.asarray(p.inliers)
            idx = idx[keep[idx]]
            if len(idx) < 30:
                continue
            n = R @ np.asarray(p.normal, float)
            out.append({'scan': sc.id, 'kind': p.kind, 'n': n, 'c': float(p.offset + n @ t),
                        'P': pr.transform(down[idx], T)})
    return out


def _keys2(P, c0, u, v, cell):
    q = np.floor(np.column_stack([(P - c0) @ u, (P - c0) @ v]) / cell).astype(np.int64)
    return q[:, 0] * 1_000_003 + q[:, 1]


def plane_cells(session, cell=0.2, min_area=1.0, max_angle_deg=5.0, max_offset=0.25,
                scan_ids=None, progress=None, poses=None):
    scans = _scans(session, scan_ids, poses)
    pl = _plane_list(session, scans, min_area, poses)
    if len(pl) < 2:
        return _empty('planes', cell, {'clusters': 0})
    # объединение плоскостей разных сканов в одну поверхность (union-find)
    parent = list(range(len(pl)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    cos_max = np.cos(np.radians(max_angle_deg))
    N = np.array([p['n'] for p in pl])
    C = np.array([p['c'] for p in pl])
    for i in range(len(pl)):
        if progress and i % 20 == 0:
            progress(0.5 * i / len(pl), 'поиск общих поверхностей')
        cand = np.flatnonzero((N[i + 1:] @ N[i] > cos_max) & (np.abs(C[i + 1:] - C[i]) < max_offset)) + i + 1
        if not len(cand):
            continue
        u, v = planes_mod.plane_axes(N[i])
        c0 = N[i] * C[i]
        ki = None
        for j in cand:
            if pl[j]['scan'] == pl[i]['scan'] or pl[j]['kind'] != pl[i]['kind'] or find(i) == find(j):
                continue
            if ki is None:
                ki = set(np.unique(_keys2(pl[i]['P'], c0, u, v, cell)).tolist())
            kj = np.unique(_keys2(pl[j]['P'], c0, u, v, cell))
            if sum(1 for k in kj.tolist() if k in ki) >= 3:
                parent[find(j)] = find(i)
    groups = {}
    for i in range(len(pl)):
        groups.setdefault(find(i), []).append(i)
    P_all, cell_all, thick, center, normal, sc_list, kinds = [], [], [], [], [], [], []
    n_cl = 0
    for gi, members in enumerate(groups.values()):
        if len({pl[m]['scan'] for m in members}) < 2:
            continue
        if progress:
            progress(0.5 + 0.5 * gi / max(1, len(groups)), 'толщина поверхностей')
        P = np.vstack([pl[m]['P'] for m in members])
        lab_names = sorted({pl[m]['scan'] for m in members})
        lab = np.concatenate([np.full(len(pl[m]['P']), lab_names.index(pl[m]['scan'])) for m in members])
        c0 = P.mean(axis=0)
        _, _, Vt = np.linalg.svd(P - c0, full_matrices=False)
        n = Vt[2]
        n_mean = np.mean([pl[m]['n'] for m in members], axis=0)
        if n @ n_mean < 0:
            n = -n
        u, v = planes_mod.plane_axes(n)
        key = _keys2(P, c0, u, v, cell)
        d = (P - c0) @ n
        res = _cells_from_labels(P, key, lab, d, len(lab_names), max_offset)
        if res is None:
            continue
        cells, cell_of, th = res
        n_cl += 1
        base = sum(len(t) for t in thick)
        for k, idx in enumerate(cells):
            center.append(P[idx].mean(axis=0))
            normal.append(n)
            sc_list.append(tuple(lab_names[s] for s in np.unique(lab[idx])))
            kinds.append(pl[members[0]]['kind'])
        P_all.append(P[cell_of >= 0])
        cell_all.append(cell_of[cell_of >= 0] + base)
        thick.append(th)
    if not thick:
        return _empty('planes', cell, {'clusters': 0})
    return Quality('planes', cell, np.vstack(P_all), np.concatenate(cell_all), np.concatenate(thick),
                   np.array(center), np.array(normal), sc_list, kinds, {'clusters': n_cl})


def _cells_from_labels(P, key, lab, d, n_lab, max_spread=np.inf):
    """
    Ячейки (key) с точками хотя бы двух сканов (lab) по MIN_CELL_PTS:
    толщина = max − min медиан d по сканам. Расхождение больше max_spread — это уже
    разные поверхности (пол комнаты и земля снаружи на близкой высоте), не ошибка
    совмещения: такие ячейки не учитываются.
    → (списки индексов, cell_of, толщины) или None.
    """
    order = np.lexsort((lab, key))
    k_s, l_s = key[order], lab[order]
    # границы групп (ячейка, скан)
    brk = np.flatnonzero((np.diff(k_s) != 0) | (np.diff(l_s) != 0)) + 1
    starts = np.concatenate([[0], brk])
    ends = np.concatenate([brk, [len(order)]])
    meds = {}
    for a, b in zip(starts, ends):
        if b - a < MIN_CELL_PTS:
            continue
        meds.setdefault(int(k_s[a]), []).append(float(np.median(d[order[a:b]])))
    good = {k: max(m) - min(m) for k, m in meds.items() if len(m) >= 2 and max(m) - min(m) <= max_spread}
    if not good:
        return None
    ks = np.array(sorted(good))
    pos = np.searchsorted(ks, key)
    pos_c = np.clip(pos, 0, len(ks) - 1)
    hit = ks[pos_c] == key
    cell_of = np.where(hit, pos_c, -1)
    cells = [np.flatnonzero(cell_of == i) for i in range(len(ks))]
    return cells, cell_of, np.array([good[k] for k in ks])


# ── локально по вокселям ───────────────────────────────────────────────────
def local_cells(session, voxel=0.2, min_pts=6, flat_ratio=0.12, scan_ids=None, progress=None, poses=None):
    scans = _scans(session, scan_ids, poses)
    if len(scans) < 2:
        return _empty('local', voxel)
    Ps, labs = [], []
    for i, sc in enumerate(scans):
        P = pr.transform(sc.down, _pose(session, sc, poses))
        Ps.append(P)
        labs.append(np.full(len(P), i))
    P = np.vstack(Ps)
    lab = np.concatenate(labs)
    q = np.floor(P / voxel).astype(np.int64)
    q -= q.min(axis=0)
    span = q.max(axis=0) + 1
    vkey = (q[:, 0] * span[1] + q[:, 1]) * span[2] + q[:, 2]
    if progress:
        progress(0.2, 'группировка по вокселям')
    # пары (воксель, скан): суммы для среднего и ковариации
    pk = vkey * len(scans) + lab
    upk, inv, cnt = np.unique(pk, return_inverse=True, return_counts=True)
    S1 = np.zeros((len(upk), 3))
    S2 = np.zeros((len(upk), 3, 3))
    np.add.at(S1, inv, P)
    np.add.at(S2, inv, P[:, :, None] * P[:, None, :])
    ok = cnt >= min_pts
    mean = S1 / cnt[:, None]
    cov = S2 / cnt[:, None, None] - mean[:, :, None] * mean[:, None, :]
    if progress:
        progress(0.5, 'плоские участки')
    w, V = np.linalg.eigh(cov)                          # по возрастанию
    flat = ok & (w[:, 0] <= flat_ratio * np.maximum(w[:, 1], 1e-12))
    nrm = V[:, :, 0]
    pair_vox = upk // len(scans)
    # воксели, где плоско хотя бы у двух сканов
    fv, fcnt = np.unique(pair_vox[flat], return_counts=True)
    vox = fv[fcnt >= 2]
    if not len(vox):
        return _empty('local', voxel)
    sel = flat & np.isin(pair_vox, vox)
    pv, pm, pn, ps = pair_vox[sel], mean[sel], nrm[sel], (upk[sel] % len(scans))
    order = np.argsort(pv, kind='stable')
    pv, pm, pn, ps = pv[order], pm[order], pn[order], ps[order]
    brk = np.flatnonzero(np.diff(pv) != 0) + 1
    starts = np.concatenate([[0], brk])
    ends = np.concatenate([brk, [len(pv)]])
    thick, center, normal, sc_list = [], [], [], []
    keep_vox = []
    for a, b in zip(starts, ends):
        ns = pn[a:b].copy()
        ns[ns @ ns[0] < 0] *= -1                         # нормали сканов — в одну сторону
        n = ns.mean(axis=0)
        n /= max(np.linalg.norm(n), 1e-12)
        if np.min(np.abs(pn[a:b] @ n)) < np.cos(np.radians(20)):
            continue                                     # сканы видят здесь разное (край, угол)
        proj = pm[a:b] @ n
        thick.append(float(proj.max() - proj.min()))
        center.append(pm[a:b].mean(axis=0))
        normal.append(n)
        sc_list.append(tuple(scans[int(s)].id for s in ps[a:b]))
        keep_vox.append(pv[a])
    if not thick:
        return _empty('local', voxel)
    keep_vox = np.array(keep_vox)
    pos = np.searchsorted(keep_vox, vkey)
    pos_c = np.clip(pos, 0, len(keep_vox) - 1)
    hit = keep_vox[pos_c] == vkey
    if progress:
        progress(0.9, 'точки ячеек')
    return Quality('local', voxel, P[hit], pos_c[hit], np.array(thick), np.array(center), np.array(normal),
                   sc_list, ['local'] * len(thick), {'voxels': len(thick)})


def compute(session, method='planes', **kw):
    return plane_cells(session, **kw) if method == 'planes' else local_cells(session, **kw)


# ── порог ──────────────────────────────────────────────────────────────────
def heat(t, thr):
    """Цвет толщины t ≥ thr: жёлтый (thr) → красный (3·thr и больше). → N×3 uint8."""
    f = np.clip((np.asarray(t) - thr) / max(2 * thr, 1e-9), 0, 1)[:, None]
    yellow = np.array([255, 214, 40.0])
    red = np.array([235, 40, 40.0])
    return (yellow * (1 - f) + red * f).astype(np.uint8)


def colors(q, thr):
    """Точки ячеек толще порога и их цвета. → (точки M'×3, цвета M'×3 uint8)."""
    if q is None or not len(q):
        return np.zeros((0, 3)), np.zeros((0, 3), np.uint8)
    m = q.thick[q.cell_of] > thr
    return q.P[m], heat(q.thick[q.cell_of[m]], thr)


def problem_areas(q, thr, top=50):
    """
    Связные группы ячеек толще порога (соседство по сетке ячеек в 3D),
    по убыванию максимальной толщины. → [dict(cells, max, median, area, center, scans, kind)].
    """
    if q is None or not len(q):
        return []
    bad = np.flatnonzero(q.thick > thr)
    if not len(bad):
        return []
    key = {tuple(k): i for i, k in zip(bad, np.floor(q.center[bad] / q.cell).astype(np.int64).tolist())}
    parent = {i: i for i in bad.tolist()}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    offs = [(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) > (0, 0, 0)]
    for k, i in key.items():
        for o in offs:
            j = key.get((k[0] + o[0], k[1] + o[1], k[2] + o[2]))
            if j is not None:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    groups = {}
    for i in bad.tolist():
        groups.setdefault(find(i), []).append(i)
    out = []
    for cells in groups.values():
        cells = np.array(cells)
        t = q.thick[cells]
        scans = sorted({s for c in cells for s in q.scans[c]})
        kinds = [q.kind[c] for c in cells]
        out.append({'cells': cells, 'max': float(t.max()), 'median': float(np.median(t)),
                    'area': float(len(cells) * q.cell ** 2), 'center': q.center[cells].mean(axis=0),
                    'scans': scans, 'kind': max(set(kinds), key=kinds.count)})
    out.sort(key=lambda g: -g['max'] * np.sqrt(g['area']))
    return out[:top]
