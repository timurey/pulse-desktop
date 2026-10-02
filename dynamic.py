#!/usr/bin/env python3
"""
Точки движущихся объектов (люди, открытая и закрытая дверь, машина у фасада) —
«неподтверждённые» точки, которые затем можно удалить при чистке.

Два признака (оценка 0…1, больше — вероятнее движущийся объект):

  по полуоборотам (pass_scores) — при реконструкции скана из bag. Каждое направление
      сканер видит один раз за полуоборот платформы, т.е. ~2·N раз за запись из N
      оборотов. Неподвижная поверхность есть на всех проходах; человек — на немногих,
      а на остальных проходах сканер видел насквозь стену за ним.
      Оценка = проходы «насквозь» / (насквозь + «есть что-то на этой дальности или ближе»).
      Хранится рядом со сканом: <скан>.dyn.npy (float16, в порядке точек файла).

  между сканами (cross_scan_scores) — по текущим позам: точка лежит в пустоте,
      которую видел другой скан (RangeImage), против сканов, у которых здесь та же
      поверхность. Работает на любых сканах (и экспорте HMI), но только в перекрытии.

Найденное (find) — точки res['down'] с оценкой не ниже порога, собранные в группы
(одиночные точки шума отбрасываются); удаление — Session.drop_points.
"""

import numpy as np

import plane_register as pr

PASS_RES_DEG = 0.75        # ячейка карты дальности прохода: лучи VLP-16 идут через 2°,
                           # с минимумом по 3×3 соседям проход покрывает направление
PASS_MARGIN = 0.10         # «насквозь» — дальше точки на max(10 см, 3 %)
PASS_REL = 0.03
MIN_KNOWN = 3              # минимум проходов (сканов) со сведениями о направлении
GROUP_VOXEL = 0.2          # группировка найденных точек
GROUP_MIN = 6              # точек в вокселе группы, чтобы она не считалась шумом
BIG_PLANE = 3.0            # м²: точки плоскостей больше этого сравнение сканов не отмечает —
                           # их расхождение — ошибка совмещения (слой «Качество»), не движение;
                           # человек, мебель и дверь (~1.8 м²) остаются
DYN_VERSION = 1


# ── карты дальности ────────────────────────────────────────────────────────
def _dir_index(P, res, W, H):
    r = np.linalg.norm(P, axis=1)
    az = np.arctan2(P[:, 1], P[:, 0])
    el = np.arcsin(np.clip(P[:, 2] / np.maximum(r, 1e-9), -1, 1))
    i = ((az + np.pi) / res).astype(np.int64) % W
    j = np.clip(((el + np.pi / 2) / res).astype(np.int64), 0, H - 1)
    return j * W + i, r


def _min3x3(img):
    out = img.copy()
    for dj in (-1, 0, 1):
        sh = np.roll(img, dj, axis=0)
        if dj == 1:
            sh[0] = np.inf
        elif dj == -1:
            sh[-1] = np.inf
        for di in (-1, 0, 1):
            out = np.minimum(out, np.roll(sh, di, axis=1))
    return out


def pass_scores(parts, pass_ids, P_eval, res_deg=PASS_RES_DEG, margin=PASS_MARGIN, rel=PASS_REL):
    """
    parts — облака кадров (система лидара при θ=0, сканер в начале), pass_ids — номер
    прохода (полуоборота) каждого кадра; P_eval — точки, для которых нужна оценка (в той же
    системе). → (оценка float32 0…1 для P_eval, число проходов).
    """
    res = np.radians(res_deg)
    W, H = int(round(2 * np.pi / res)), int(round(np.pi / res))
    pass_ids = np.asarray(pass_ids)
    passes = np.unique(pass_ids)
    idx_e, r_e = _dir_index(np.asarray(P_eval, np.float64), res, W, H)
    through = np.zeros(len(P_eval), np.int32)
    known = np.zeros(len(P_eval), np.int32)
    tol = np.maximum(margin, rel * r_e)
    for p in passes:
        pts = [np.asarray(parts[k], np.float64) for k in np.flatnonzero(pass_ids == p) if len(parts[k])]
        if not pts:
            continue
        idx, r = _dir_index(np.vstack(pts), res, W, H)
        img = np.full(W * H, np.inf)
        np.minimum.at(img, idx, r)
        img = _min3x3(img.reshape(H, W)).ravel()
        rp = img[idx_e]
        k = np.isfinite(rp)
        known += k
        through += k & (rp > r_e + tol)
    score = np.where(known >= MIN_KNOWN, through / np.maximum(known, 1), 0.0).astype(np.float32)
    return score, int(len(passes))


def pass_id(angle_unwrapped_rad):
    """Номер полуоборота платформы для угла (развёрнутого), рад."""
    return np.floor(np.asarray(angle_unwrapped_rad) / np.pi).astype(np.int64)


# ── между сканами ──────────────────────────────────────────────────────────
def _range_image(sc):
    """RangeImage скана по его очищенному даунсемплу (кеш на скане)."""
    key = (len(sc.down), sc.clean, len(sc.erase), len(getattr(sc, 'drop', ())))
    c = getattr(sc, '_rimg', None)
    if c is None or c[0] != key:
        c = (key, pr.RangeImage(sc.down))
        sc._rimg = c
    return c[1]


def cross_scan_scores(session, scan, others=None):
    """
    Оценка для точек scan.res['down'] по другим размещённым сканам.
    → (оценка float32, число сканов со сведениями о точке int).
    """
    down = scan.res['down']
    T = session.Tc(scan)
    through = np.zeros(len(down), np.int32)
    confirm = np.zeros(len(down), np.int32)
    if T is None:
        return np.zeros(len(down), np.float32), confirm
    Pc = pr.transform(down, T)
    others = others if others is not None else [s for s in session.placed() if s is not scan and s.analyzed]
    for A in others:
        TA = session.Tc(A)
        if TA is None:
            continue
        PA = pr.transform(Pc, np.linalg.inv(TA))         # в канон. систему A: сканер A в начале
        t, c = _range_image(A).masks(PA)
        through += t
        confirm += c
    n = through + confirm
    score = np.where(n >= 1, through / np.maximum(n, 1), 0.0).astype(np.float32)
    big = np.zeros(len(down), bool)
    for p in scan.planes:
        if p.area >= BIG_PLANE:
            big[np.asarray(p.inliers)] = True
    score[big] = 0.0
    return score, n


# ── оценка по проходам: из файла рядом со сканом ───────────────────────────
def dyn_path(scan_path):
    from pathlib import Path
    p = Path(scan_path)
    return p.with_name(p.stem + '.dyn.npy')


def load_pass_scores(scan):
    """
    Оценка по полуоборотам для точек scan.res['down'] (ближайшая точка файла) или None,
    если скан реконструирован без неё (экспорт HMI, старая реконструкция).
    """
    c = getattr(scan, '_dyn_pass', None)
    if c is not None:
        return c if len(c) else None
    f = dyn_path(scan.path)
    if not f.exists():
        scan._dyn_pass = np.zeros(0, np.float32)
        return None
    import open3d as o3d
    import planes
    s = np.load(f).astype(np.float32)
    P = planes.load_points(scan.path)
    if len(P) != len(s):
        scan._dyn_pass = np.zeros(0, np.float32)
        return None
    Pc = np.ascontiguousarray(P @ np.asarray(scan.R_up).T, np.float32)
    nns = o3d.core.nns.NearestNeighborSearch(o3d.core.Tensor(Pc))
    nns.knn_index()
    idx, _ = nns.knn_search(o3d.core.Tensor(np.ascontiguousarray(scan.res['down'], np.float32)), 1)
    out = s[idx.numpy()[:, 0]]
    scan._dyn_pass = out
    return out


# ── найденное ──────────────────────────────────────────────────────────────
def group_filter(P, mask, voxel=GROUP_VOXEL, min_pts=GROUP_MIN):
    """Оставить в маске только точки, у которых в их вокселе не меньше min_pts отмеченных."""
    if not mask.any():
        return mask
    q = np.floor(P[mask] / voxel).astype(np.int64)
    _, inv, cnt = np.unique(q, axis=0, return_inverse=True, return_counts=True)
    out = mask.copy()
    out[np.flatnonzero(mask)] = cnt[inv.ravel()] >= min_pts
    return out


def find(session, scan, thr=0.6, use_cross=True, use_pass=True):
    """
    Маска точек scan.res['down'], похожих на движущиеся объекты; уже удалённые
    (отражения, ручная чистка) не учитываются. → (маска, сведения).
    """
    down = scan.res['down']
    score = np.zeros(len(down), np.float32)
    info = {'cross': None, 'pass': None}
    if use_cross:
        sc, n = cross_scan_scores(session, scan)
        score = np.maximum(score, sc)
        info['cross'] = int((n > 0).sum())
    if use_pass:
        ps = load_pass_scores(scan)
        if ps is not None:
            score = np.maximum(score, ps)
            info['pass'] = int((ps > 0).sum())
    scan.dyn = score
    return mask(scan, thr), info


def mask(scan, thr):
    """Найденное по уже посчитанной оценке scan.dyn (смена порога — без пересчёта)."""
    import manual_clean
    from scan_session import voxel_keys
    down = scan.res['down']
    if scan.dyn is None or len(scan.dyn) != len(down):
        return np.zeros(len(down), bool)
    alive = ~scan.ghost_mask() if scan.clean else np.ones(len(down), bool)
    if scan.erase:
        alive &= ~manual_clean.inside_regions(down, scan.erase)
    if len(getattr(scan, 'drop', ())):
        alive &= ~np.isin(voxel_keys(down), scan.drop)
    return group_filter(down, alive & (scan.dyn >= thr))
