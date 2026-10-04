#!/usr/bin/env python3
"""
Стыковка статических сканов по опорным плоскостям.

Оба скана приводятся к канонической системе (planes.py: +Z вверх, начало в
сканере). Ищется поза B → A: x_A = Rz(yaw) · x_B + t — 4 степени свободы,
остаточный наклон добирает финальный ICP (6 DOF).

Автоматический режим для пары:
  1. yaw-кандидаты: преобладающее направление стен (mod 90°) → 4 варианта,
     либо полный перебор по гистограмме азимутов (неманхэттенские сцены);
  2. сдвиг по Z — по слою пола/потолка; в горизонтали — голосование по парам
     сонаправленных стен (n·t = c_A − c_B) по двум осям;
  3. оценка каждой гипотезы — перекрытие воксельной занятости;
  4. ICP-уточнение лучших гипотез, флаг неоднозначности.

Ручной режим: пары плоскостей / точек → solve_pose().

Usage:
    python plane_register.py A.e57 B.e57 [--up auto] [--top 3] [-o pose.json]
"""

import sys
import json
import argparse
from pathlib import Path

import numpy as np

from planes import analyze_scan, Plane


# ── параметры ──────────────────────────────────────────────────────────────
SCORE_VOXEL   = 0.10    # м — сетка оценки перекрытия
VOTE_BIN      = 0.05    # м — бин голосования сдвига
VOTE_PEAKS    = 6       # кандидатов сдвига на ось
NORMAL_TOL    = np.radians(6)
ICP_VOXELS    = (0.08, 0.04)
ICP_MAX_DIST  = 0.25
WALL_MIN_AREA = 0.8     # м² — стены, участвующие в голосовании


# ── геометрия ──────────────────────────────────────────────────────────────
def rot_z(yaw):
    c, s = np.cos(yaw), np.sin(yaw)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def make_T(R, t):
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t
    return T


def transform(pts, T):
    return pts @ T[:3, :3].T + T[:3, 3]


def wrap(a):
    return (a + np.pi) % (2 * np.pi) - np.pi


def walls(planes, min_area=WALL_MIN_AREA):
    return [p for p in planes if p.kind == 'wall' and p.area >= min_area]


def azimuth(n):
    return float(np.arctan2(n[1], n[0]))


def dominant_direction(planes):
    """Манхэттенское направление стен (mod 90°) по площади. → (рад, сила 0..1)."""
    ws = walls(planes)
    if not ws:
        return None, 0.0
    a = np.array([azimuth(p.normal) for p in ws])
    w = np.array([p.area for p in ws])
    z = np.sum(w * np.exp(4j * a)) / w.sum()
    return float(np.angle(z) / 4), float(abs(z))


def azimuth_histogram(planes, bins=360):
    h = np.zeros(bins)
    for p in walls(planes):
        k = int(np.round(np.degrees(azimuth(p.normal)))) % bins
        h[k] += p.area
    # сглаживание ±2°
    return sum(np.roll(h, d) * wgt for d, wgt in ((-2, .25), (-1, .5), (0, 1), (1, .5), (2, .25)))


def yaw_candidates(PA, PB, mode='manhattan', top=4):
    """Кандидаты yaw (рад), отсортированные по согласию направлений стен."""
    hA, hB = azimuth_histogram(PA), azimuth_histogram(PB)
    corr = np.array([np.dot(hA, np.roll(hB, k)) for k in range(360)])
    if mode == 'manhattan':
        dA, sA = dominant_direction(PA)
        dB, sB = dominant_direction(PB)
        if dA is not None and dB is not None and min(sA, sB) > 0.5:
            base = dA - dB
            cands = [wrap(base + k * np.pi / 2) for k in range(4)]
            # уточнение каждого по корреляции в окне ±3°
            out = []
            for y in cands:
                k0 = int(np.round(np.degrees(y))) % 360
                ks = [(k0 + d) % 360 for d in range(-3, 4)]
                kb = max(ks, key=lambda k: corr[k])
                out.append((corr[kb], wrap(np.radians(kb))))
            out.sort(key=lambda x: -x[0])
            return [y for _, y in out]
    # полный перебор: пики корреляции
    peaks = [k for k in range(360)
             if corr[k] == max(corr[(k + d) % 360] for d in range(-5, 6)) and corr[k] > 0]
    peaks.sort(key=lambda k: -corr[k])
    return [wrap(np.radians(k)) for k in peaks[:top]]


# ── решение позы по ограничениям ───────────────────────────────────────────
def solve_translation(constraints, R, prior=None):
    """
    Линейный МНК для t при известном повороте R.
      ('plane', nA, cA, nB, cB, w):  n·t = cA − cB,  n = норм(nA + R nB)
      ('point', pA, pB, P, w):       P t = P (pA − R pB)   (P — проектор, 3×3)
    → (t, info): info['eig'] — собственные числа нормальной матрицы,
      info['weak'] — плохо обусловленные направления.
    """
    rows, rhs, wts = [], [], []
    for c in constraints:
        if c[0] == 'plane':
            _, nA, cA, nB, cB, w = c
            n = np.asarray(nA) + R @ np.asarray(nB)
            n /= np.linalg.norm(n)
            rows.append(n); rhs.append(cA - cB); wts.append(w)
        elif c[0] == 'point':
            _, pA, pB, P, w = c
            P = np.asarray(P, dtype=float)
            b = P @ (np.asarray(pA) - R @ np.asarray(pB))
            for i in range(3):
                if np.linalg.norm(P[i]) > 1e-9:
                    rows.append(P[i]); rhs.append(b[i]); wts.append(w)
    A = np.array(rows).reshape(-1, 3); b = np.array(rhs); W = np.array(wts)
    N = A.T @ (A * W[:, None])
    eig, vec = np.linalg.eigh(N)
    scale = max(eig[-1], 1e-12)
    weak = [vec[:, i].tolist() for i in range(3) if eig[i] < 1e-3 * scale]
    if prior is not None:
        # слабое притяжение к начальному сдвигу: определённые направления решают
        # ограничения, неопределённые остаются там, где скан поставил пользователь
        A2 = np.vstack([A, np.eye(3)])
        b2 = np.concatenate([b, np.asarray(prior, float)])
        W2 = np.concatenate([W, np.full(3, 1e-6 * max(scale, 1.0))])
    else:
        A2, b2, W2 = A, b, W
    t = np.linalg.lstsq(A2 * np.sqrt(W2)[:, None], b2 * np.sqrt(W2), rcond=None)[0]
    resid = A @ t - b
    return t, {'eig': eig.tolist(), 'weak': weak,
               'rms': float(np.sqrt(np.mean(resid ** 2))) if len(resid) else 0.0}


def kabsch_rotation(pairs_planes, pairs_points=(), min_spread=0.2):
    """
    Полный поворот B→A по парам нормалей плоскостей и (≥3) парам точек.
    None, если направления не задают поворот (все нормали параллельны и точек мало).
    """
    va, vb, w = [], [], []
    for a, b in pairs_planes:
        va.append(np.asarray(a.normal, float)); vb.append(np.asarray(b.normal, float))
        w.append(min(a.area, b.area))
    pts = [(np.asarray(p[0], float), np.asarray(p[1], float)) for p in pairs_points if len(p) == 2]
    if len(pts) >= 3:
        PA = np.array([p[0] for p in pts]); PB = np.array([p[1] for p in pts])
        PA -= PA.mean(0); PB -= PB.mean(0)
        for x, y in zip(PA, PB):
            n = np.linalg.norm(x)
            if n > 1e-6:
                va.append(x / n); vb.append(y / max(np.linalg.norm(y), 1e-9)); w.append(10.0 * n)
    if len(va) < 2:
        return None
    VA = np.array(va) * np.sqrt(w)[:, None]
    sv = np.linalg.svd(VA, compute_uv=False)
    if sv[1] < min_spread * sv[0]:
        return None
    H = (np.array(vb) * np.array(w)[:, None]).T @ np.array(va)
    U, _, Vt = np.linalg.svd(H)
    D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
    return Vt.T @ D @ U.T


def split_yaw_tilt(R):
    """R = Rz(yaw) · R_tilt; R_tilt не содержит поворота вокруг вертикали."""
    yaw = float(np.arctan2(R[1, 0], R[0, 0]))
    return yaw, rot_z(-yaw) @ np.asarray(R)


def level_correction(normals, areas, max_dev_deg=12.0, iters=4):
    """
    Поворот (вокруг горизонтальных осей), делающий горизонтальные плоскости
    горизонтальными, а стены — вертикальными. Плоскости, отклонённые больше
    max_dev_deg от горизонтали/вертикали (скаты, уклон улицы), не учитываются.
    normals — в текущей системе. → (R_corr 3×3, info).
    """
    N = np.asarray(normals, float).reshape(-1, 3)
    W = np.asarray(areas, float)
    R = np.eye(3)
    cmax = np.cos(np.radians(max_dev_deg))
    smax = np.sin(np.radians(max_dev_deg))
    used = (0, 0)
    for _ in range(iters):
        M = N @ R.T
        rows, rhs, wts = [], [], []
        nh = nv = 0
        for n, w in zip(M, W):
            if abs(n[2]) > cmax:                       # пол / потолок / земля
                # хотим n_x = n_y = 0:  ω_y n_z = -n_x ;  -ω_x n_z = -n_y
                rows += [[0, n[2]], [-n[2], 0]]
                rhs += [-n[0], -n[1]]
                wts += [w, w]
                nh += 1
            elif abs(n[2]) < smax:                     # стена
                # хотим n_z = 0:  ω_x n_y - ω_y n_x = -n_z
                rows.append([n[1], -n[0]])
                rhs.append(-n[2])
                wts.append(w)
                nv += 1
        if len(rows) < 2:
            break
        A = np.array(rows) * np.sqrt(wts)[:, None]
        b = np.array(rhs) * np.sqrt(wts)
        (wx, wy), *_ = np.linalg.lstsq(A, b, rcond=None)
        om = np.array([wx, wy, 0.0])
        ang = np.linalg.norm(om)
        if ang < 1e-9:
            break
        k = om / ang
        K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        R = (np.eye(3) + np.sin(ang) * K + (1 - np.cos(ang)) * K @ K) @ R
        used = (nh, nv)
    tilt = float(np.degrees(np.arccos(np.clip(R[2, 2], -1, 1))))
    return R, {'tilt_deg': tilt, 'horizontal': used[0], 'walls': used[1]}


def solve_pose(pairs_planes, pairs_points=(), yaw=None, T_init=None, full=True):
    """
    Поза B→A по ручным парам.
      pairs_planes: [(PlaneA, PlaneB), ...]
      pairs_points: [(pA, pB) или (pA, pB, P)]
    full=True — если пары задают наклон (≥2 непараллельных направления), поворот
    находится целиком (6 степеней свободы). Иначе ищется только поворот вокруг
    вертикали, а наклон подвижного скана берётся из T_init (не меняется).
    """
    R_init = np.eye(3) if T_init is None else np.asarray(T_init)[:3, :3]
    y0, R_tilt = split_yaw_tilt(R_init)
    R = kabsch_rotation(pairs_planes, pairs_points) if (full and yaw is None) else None
    mode = '6dof' if R is not None else 'yaw'
    if R is None:
        if yaw is None:
            diffs, w = [], []
            for a, b in pairs_planes:
                if a.kind == 'wall' and b.kind == 'wall':
                    nb = R_tilt @ np.asarray(b.normal)
                    diffs.append(azimuth(a.normal) - azimuth(nb))
                    w.append(min(a.area, b.area))
            if not diffs and len(pairs_points) >= 2:
                # yaw по двум точкам в горизонтали
                (a0, b0), (a1, b1) = [(np.asarray(p[0]), R_tilt @ np.asarray(p[1]))
                                      for p in pairs_points[:2]]
                da, db = (a1 - a0)[:2], (b1 - b0)[:2]
                diffs = [np.arctan2(da[1], da[0]) - np.arctan2(db[1], db[0])]
                w = [1.0]
            if not diffs:
                if T_init is None:
                    raise ValueError("Нужна хотя бы одна пара стен или две пары точек для yaw")
                yaw = y0                                  # поворот не задан — как есть
            else:
                yaw = float(np.angle(np.sum(np.array(w) * np.exp(1j * np.array(diffs)))))
        R = rot_z(yaw) @ R_tilt
    cons = [('plane', a.normal, a.offset, b.normal, b.offset, min(a.area, b.area))
            for a, b in pairs_planes]
    for pp in pairs_points:
        P = pp[2] if len(pp) > 2 else np.eye(3)
        cons.append(('point', pp[0], pp[1], P, 10.0))
    t, info = solve_translation(cons, R, None if T_init is None else np.asarray(T_init)[:3, 3])
    info['yaw_deg'] = float(np.degrees(np.arctan2(R[1, 0], R[0, 0])))
    info['rotation'] = mode
    info['tilt_deg'] = float(np.degrees(np.arccos(np.clip(R[2, 2], -1, 1))))
    return make_T(R, t), info


# ── голосование сдвига ─────────────────────────────────────────────────────
def _vote_peaks(values, weights, bin_size=VOTE_BIN, n_peaks=VOTE_PEAKS):
    if len(values) == 0:
        return [0.0]
    lo, hi = values.min() - 3 * bin_size, values.max() + 3 * bin_size
    edges = np.arange(lo, hi + bin_size, bin_size)
    h, _ = np.histogram(values, bins=edges, weights=weights)
    h = np.convolve(h, [.25, .5, 1, .5, .25], mode='same')
    order = np.argsort(-h)
    peaks = []
    for k in order:
        if h[k] <= 0:
            break
        v = 0.5 * (edges[k] + edges[k + 1])
        if all(abs(v - p) > 3 * bin_size for p in peaks):
            # уточнение: взвешенное среднее голосов рядом с пиком
            m = np.abs(values - v) < 1.5 * bin_size
            if m.any():
                v = float(np.average(values[m], weights=weights[m]))
            peaks.append(v)
        if len(peaks) >= n_peaks:
            break
    return peaks or [0.0]


def translation_candidates(PA, PB, layersA, layersB, R, yaw_dir):
    """Кандидаты t для заданного поворота. yaw_dir — манхэттенская ось в A."""
    e1 = np.array([np.cos(yaw_dir), np.sin(yaw_dir), 0.0])
    e2 = np.array([-e1[1], e1[0], 0.0])
    votes = {0: ([], []), 1: ([], [])}
    WA, WB = walls(PA), walls(PB)
    nB_rot = [R @ np.asarray(b.normal) for b in WB]
    for a in WA:
        nA = np.asarray(a.normal)
        for b, nb in zip(WB, nB_rot):
            if np.arccos(np.clip(nA @ nb, -1, 1)) > NORMAL_TOL:
                continue
            s = a.offset - b.offset                     # n·t = s
            for ax, e in ((0, e1), (1, e2)):
                proj = nA @ e
                if abs(proj) > 0.9:
                    votes[ax][0].append(s / proj)
                    # вес: меньшая площадь × похожесть размеров
                    sim = min(a.extent[0], b.extent[0]) / max(a.extent[0], b.extent[0], 1e-6)
                    votes[ax][1].append(min(a.area, b.area) * (0.5 + sim))
    peaks = [_vote_peaks(np.array(votes[ax][0]), np.array(votes[ax][1])) for ax in (0, 1)]
    # вертикаль: пол к полу, иначе потолок к потолку
    tz = []
    if layersA['floor_z'] is not None and layersB['floor_z'] is not None:
        tz.append(layersA['floor_z'] - layersB['floor_z'])
    if layersA['ceiling_z'] is not None and layersB['ceiling_z'] is not None:
        tz.append(layersA['ceiling_z'] - layersB['ceiling_z'])
    # пол может оказаться верхом мебели под штативом — проверяем и потолок
    if len(tz) == 2 and abs(tz[0] - tz[1]) < 0.1:
        tz = [0.5 * (tz[0] + tz[1])]
    tz = tz or [0.0]
    cands = []
    for s1 in peaks[0]:
        for s2 in peaks[1]:
            for z in tz:
                t = s1 * e1 + s2 * e2
                t[2] = z
                cands.append(t)
    return cands, {'votes_e1': len(votes[0][0]), 'votes_e2': len(votes[1][0])}


# ── оценка перекрытия ──────────────────────────────────────────────────────
def _voxel_keys(pts, size):
    ijk = np.floor(pts / size).astype(np.int64) + (1 << 20)
    return (ijk[:, 0] << 42) | (ijk[:, 1] << 21) | ijk[:, 2]


class OverlapScorer:
    """Доля вокселей B, совпавших с занятыми вокселями A (с допуском ±1 воксель)."""

    def __init__(self, ptsA, size=SCORE_VOXEL):
        self.size = size
        base = np.unique(np.floor(ptsA / size).astype(np.int64), axis=0)
        offs = np.array([[i, j, k] for i in (-1, 0, 1) for j in (-1, 0, 1) for k in (-1, 0, 1)])
        dil = (base[:, None, :] + offs[None]).reshape(-1, 3)
        self.keysA = np.unique(_voxel_keys(dil * size + size / 2, size))
        self.nA = len(base)

    def score(self, ptsB_down, T):
        p = transform(ptsB_down, T)
        keys = np.unique(_voxel_keys(p, self.size))
        hit = np.isin(keys, self.keysA, assume_unique=True).sum()
        return hit / max(1, min(len(keys), self.nA)), int(hit)


class RangeImage:
    """
    Сферическая карта минимальных дальностей скана (сканер в начале координат).
    Точка другого скана, оказавшаяся заметно ближе первой поверхности в том же
    направлении, лежит в пространстве, которое этот сканер видел пустым, —
    противоречие. Это различает симметричные гипотезы (поворот комнаты на 180°).
    """

    def __init__(self, pts, res_deg=0.5):
        self.res = np.radians(res_deg)
        self.W = int(round(2 * np.pi / self.res))
        self.H = int(round(np.pi / self.res))
        idx, r = self._index(pts)
        img = np.full(self.W * self.H, np.inf)
        np.minimum.at(img, idx, r)
        # минимум по соседям 3×3: на косых поверхностях дальность внутри
        # ячейки сильно меняется, одна точка A не задаёт границу пустоты
        img = img.reshape(self.H, self.W)
        out = img.copy()
        for dj in (-1, 0, 1):
            sh = np.roll(img, dj, axis=0)
            if dj == 1:
                sh[0] = np.inf
            elif dj == -1:
                sh[-1] = np.inf
            for di in (-1, 0, 1):
                out = np.minimum(out, np.roll(sh, di, axis=1))
        self.img = out.ravel()

    def _index(self, P):
        r = np.linalg.norm(P, axis=1)
        az = np.arctan2(P[:, 1], P[:, 0])
        el = np.arcsin(np.clip(P[:, 2] / np.maximum(r, 1e-9), -1, 1))
        i = ((az + np.pi) / self.res).astype(np.int64) % self.W
        j = np.clip(((el + np.pi / 2) / self.res).astype(np.int64), 0, self.H - 1)
        return j * self.W + i, r

    def masks(self, P, margin=0.15, rel=0.03):
        """
        На точку: (видел насквозь — точка в пустоте, которую видел этот сканер;
                   подтвердил — у этого сканера здесь поверхность на той же дальности).
        Точки, закрытые от сканера (дальше его поверхности), — ни то, ни другое.
        """
        idx, r = self._index(P)
        rA = self.img[idx]
        known = np.isfinite(rA)
        tol = np.maximum(margin, rel * np.where(known, rA, 0))
        with np.errstate(invalid="ignore"):
            through = known & (r < rA - tol)
            confirm = known & (np.abs(r - rA) <= tol)
        return through, confirm

    def violations(self, P, margin=0.15, rel=0.03):
        idx, r = self._index(P)
        rA = self.img[idx]
        known = np.isfinite(rA)
        with np.errstate(invalid="ignore"):
            bad = r < rA - np.maximum(margin, rel * rA)
        return float((bad & known).sum() / max(1, known.sum()))


class ConsistencyScorer:
    """
    Оценка гипотезы позы B→A:
      близость — доля точек B ближе CLOSE_DIST к облаку A;
      нарушения — доля точек, попавших в свободное пространство другого скана
      (в обе стороны). score = близость − VIOL_WEIGHT · нарушения.
    """
    CLOSE_DIST = 0.05
    VIOL_WEIGHT = 8.0

    def __init__(self, downA, downB, n_probe=30000, seed=0):
        import open3d as o3d
        rng = np.random.default_rng(seed)
        pick = lambda P: P if len(P) <= n_probe else P[rng.choice(len(P), n_probe, replace=False)]
        self.A, self.B = pick(downA), pick(downB)
        self.pcdA = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(downA))
        self.riA, self.riB = RangeImage(downA), RangeImage(downB)

    MIN_CLOSE = 0.01      # меньше — сканы не перекрываются вовсе, гипотеза пустая

    def score(self, T):
        import open3d as o3d
        PB = transform(self.B, T)
        d = np.asarray(o3d.geometry.PointCloud(
            o3d.utility.Vector3dVector(PB)).compute_point_cloud_distance(self.pcdA))
        close = float((d < self.CLOSE_DIST).mean())
        Ti = np.linalg.inv(T)
        viol = 0.5 * (self.riA.violations(PB) + self.riB.violations(transform(self.A, Ti)))
        s = close - self.VIOL_WEIGHT * viol
        if close < self.MIN_CLOSE:
            s -= 1.0
        return s, close, viol


class MultiScanScorer:
    """
    Как ConsistencyScorer, но A — несколько уже размещённых сканов (в общей системе).
    Свободное пространство проверяется по карте дальностей каждого сканера A
    в его собственной системе. Опционально — «окна» интереса: близость считается
    только для точек B рядом с заданными центрами (проёмами), где перекрытие
    между внутренним и фасадным сканом вообще возможно.
    """
    CLOSE_DIST = 0.05
    VIOL_WEIGHT = 8.0

    def __init__(self, scansA, downB, n_probe=30000, seed=0):
        """scansA: [(down_canon, T_common_from_scan)], downB — канон. система B."""
        import open3d as o3d
        rng = np.random.default_rng(seed)
        pick = lambda P, k: P if len(P) <= k else P[rng.choice(len(P), k, replace=False)]
        self.B = pick(downB, n_probe)
        allA = np.vstack([transform(d, T) for d, T in scansA])
        self.pcdA = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(allA)).voxel_down_sample(0.05)
        self.A = pick(np.asarray(self.pcdA.points), n_probe)
        self.ris = [(RangeImage(d), np.linalg.inv(T)) for d, T in scansA]
        self.riB = RangeImage(downB)

    def score(self, T):
        import open3d as o3d
        PB = transform(self.B, T)
        d = np.asarray(o3d.geometry.PointCloud(
            o3d.utility.Vector3dVector(PB)).compute_point_cloud_distance(self.pcdA))
        n_close = int((d < self.CLOSE_DIST).sum())
        vA = max(ri.violations(transform(PB, Ti)) for ri, Ti in self.ris)
        vB = self.riB.violations(transform(self.A, np.linalg.inv(T)))
        viol = 0.5 * (vA + vB)
        return n_close / len(PB) - self.VIOL_WEIGHT * viol, n_close, viol


# ── пара проёмов (окно изнутри ↔ окно снаружи) ─────────────────────────────
def pose_from_opening_pair(cA, nA, cB, nB, delta, same_side=False):
    """
    Поза B→A (канон. системы, только yaw + сдвиг) по паре проёмов.
    nA, nB — нормали стен, направленные к своим сканерам. Если проём виден с
    разных сторон стены (изнутри / снаружи), нормали после поворота
    противоположны, а центры разнесены на толщину стены delta вдоль −nA.
    same_side=True — оба скана видят проём с одной стороны (delta игнорируется).
    """
    nA, nB = np.asarray(nA, float), np.asarray(nB, float)
    target = nA if same_side else -nA
    yaw = np.arctan2(target[1], target[0]) - np.arctan2(nB[1], nB[0])
    R = rot_z(yaw)
    shift = 0.0 if same_side else delta
    t = np.asarray(cA) - shift * nA - R @ np.asarray(cB)
    return make_T(R, t)


def search_opening_pair(scorer, cA, nA, cB, nB, deltas=np.arange(0.15, 0.95, 0.05),
                        same_side=False):
    """Перебор толщины стены по согласованности. → список (score, n_close, viol, delta, T)."""
    out = []
    for d in (deltas if not same_side else [0.0]):
        T = pose_from_opening_pair(cA, nA, cB, nB, d, same_side)
        s, nc, v = scorer.score(T)
        out.append((s, nc, v, float(d), T))
    out.sort(key=lambda x: -x[0])
    return out


# ── ICP ────────────────────────────────────────────────────────────────────
def refine_icp(ptsA, ptsB, T0, voxels=ICP_VOXELS, max_dist=ICP_MAX_DIST, max_src=40000, dists=None):
    """ICP точка-плоскость (Tukey) по этапам voxels × dists (по умолчанию max_dist, max_dist/2)."""
    import open3d as o3d
    reg = o3d.pipelines.registration
    A = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(ptsA))
    B = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(ptsB))
    T = T0.copy()
    res = None
    rng = np.random.default_rng(0)
    for v, d in zip(voxels, dists or (max_dist, max_dist / 2)):
        a, b = A.voxel_down_sample(v), B.voxel_down_sample(v)
        if len(b.points) > max_src:              # скорость: источник прореживаем
            b = b.select_by_index(rng.choice(len(b.points), max_src, replace=False).tolist())
        a.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=v * 3, max_nn=30))
        res = reg.registration_icp(
            b, a, d, T, reg.TransformationEstimationPointToPlane(reg.TukeyLoss(k=d / 2)),
            reg.ICPConvergenceCriteria(max_iteration=60))
        T = res.transformation
    return T, float(res.fitness), float(res.inlier_rmse)


def refine_rotation(ptsA, ptsB, T0, center, voxels=(0.12, 0.06, 0.03), dists=(0.5, 0.2, 0.06),
                    iters=25, max_src=40000):
    """
    ICP «только поворот вокруг точки center» (общая система): точка центра остаётся на
    месте. Линеаризованная точка-плоскость по малому углу ω:
      r_i = n_i·(s_i − d_i) + ω·((s_i − c) × n_i),  веса Тьюки.
    → (T, доля совпавших, rmse).
    """
    import open3d as o3d
    c = np.asarray(center, float)
    A = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(ptsA))
    B = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(ptsB))
    T = np.asarray(T0, float).copy()
    rng = np.random.default_rng(0)
    fit, rmse = 0.0, 0.0
    for v, dmax in zip(voxels, dists):
        a = A.voxel_down_sample(v)
        a.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=v * 3, max_nn=30))
        Pa, Na = np.asarray(a.points), np.asarray(a.normals)
        b = np.asarray(B.voxel_down_sample(v).points)
        if len(b) > max_src:
            b = b[rng.choice(len(b), max_src, replace=False)]
        nns = o3d.core.nns.NearestNeighborSearch(o3d.core.Tensor(np.ascontiguousarray(Pa, np.float32)))
        nns.knn_index()
        for _ in range(iters):
            S = b @ T[:3, :3].T + T[:3, 3]
            idx, d2 = nns.knn_search(o3d.core.Tensor(np.ascontiguousarray(S, np.float32)), 1)
            idx, d = idx.numpy()[:, 0], np.sqrt(d2.numpy()[:, 0])
            m = d < dmax
            if m.sum() < 50:
                break
            s_, D, n = S[m], Pa[idx[m]], Na[idx[m]]
            r0 = np.sum(n * (s_ - D), axis=1)
            J = np.cross(s_ - c, n)
            k = dmax / 2
            w = np.where(np.abs(r0) < k, (1 - (r0 / k) ** 2) ** 2, 0.0)
            H = (J * w[:, None]).T @ J + 1e-9 * np.eye(3)
            omega = -np.linalg.solve(H, (J * w[:, None]).T @ r0)
            ang = np.linalg.norm(omega)
            if ang < 1e-7:
                break
            K = np.array([[0, -omega[2], omega[1]], [omega[2], 0, -omega[0]], [-omega[1], omega[0], 0]]) / ang
            R = np.eye(3) + np.sin(ang) * K + (1 - np.cos(ang)) * K @ K
            T = rotate_about(T, c, R)
            if ang < 1e-5:
                break
        fit = float(m.mean())
        rmse = float(np.sqrt(np.mean(r0[np.abs(r0) < dmax] ** 2))) if len(r0) else 0.0
    return T, fit, rmse


def rotate_about(T, center, R):
    """Повернуть позу T (в общей системе) на R вокруг точки center: x' = R(x − c) + c."""
    c = np.asarray(center, float)
    M = make_T(R, c - R @ c)
    return M @ np.asarray(T, float)


def pose_delta(T1, T2):
    D = np.linalg.inv(T1) @ T2
    ang = np.degrees(np.arccos(np.clip((np.trace(D[:3, :3]) - 1) / 2, -1, 1)))
    return float(np.linalg.norm(D[:3, 3])), float(ang)


# ── автоматическая стыковка пары ───────────────────────────────────────────
def register_pair(resA, resB, yaw_mode='manhattan', top=3, icp=True, verbose=True):
    """
    resA/resB — результаты planes.analyze_scan. Поза в канонических системах.
    → dict: T (B→A в исходных системах), T_canon, score, ambiguity, …
    """
    PA, PB = resA['planes'], resB['planes']
    downA, downB = resA['down'], resB['down']
    coarse = OverlapScorer(downA)
    rng = np.random.default_rng(0)
    probeB = downB if len(downB) < 60000 else downB[rng.choice(len(downB), 60000, replace=False)]
    scorer = ConsistencyScorer(downA, downB)
    dirA, _ = dominant_direction(PA)
    hyps = []
    for yaw in yaw_candidates(PA, PB, yaw_mode):
        R = rot_z(yaw)
        cands, _ = translation_candidates(PA, PB, resA['layers'], resB['layers'],
                                          R, dirA if dirA is not None else 0.0)
        # быстрый отсев по воксельному перекрытию, точная оценка — лучшим
        cands.sort(key=lambda t: -coarse.score(probeB, make_T(R, t))[0])
        for t in cands[:8]:
            T = make_T(R, t)
            s, close, viol = scorer.score(T)
            hyps.append({'T': T, 'score': s, 'close': close, 'viol': viol, 'yaw': yaw})
    hyps.sort(key=lambda h: -h['score'])
    uniq = []
    for h in hyps:
        if all(pose_delta(h['T'], u['T'])[0] > 0.3 or pose_delta(h['T'], u['T'])[1] > 5 for u in uniq):
            uniq.append(h)
        if len(uniq) >= top:
            break
    if verbose:
        for h in uniq:
            print(f"   гипотеза yaw={np.degrees(h['yaw']):7.1f}°  t={np.round(h['T'][:3, 3], 2)}  "
                  f"оценка={h['score']:.3f} (близко {h['close']:.3f}, нарушений {h['viol']:.3f})")
    if icp:
        for h in uniq:
            T, fit, rmse = refine_icp(downA, downB, h['T'])
            h['T0'] = h['T']
            h['T'], h['fitness'], h['rmse'] = T, fit, rmse
            h['icp_shift'] = pose_delta(h['T0'], T)
            h['score'], h['close'], h['viol'] = scorer.score(T)
        uniq.sort(key=lambda h: -h['score'])
        # разные стартовые гипотезы могут сойтись в одну позу — оставляем различные
        final = []
        for h in uniq:
            if all(pose_delta(h['T'], f['T'])[0] > 0.1 or pose_delta(h['T'], f['T'])[1] > 1
                   for f in final):
                final.append(h)
        uniq = final
    best = uniq[0]
    second = uniq[1]['score'] if len(uniq) > 1 else 0.0
    T_canon = best['T']
    RA, RB = np.asarray(resA['R_up']), np.asarray(resB['R_up'])
    # исходные системы: x_A = RA^T · T_canon · RB · x_B
    T = make_T(RA.T, np.zeros(3)) @ T_canon @ make_T(RB, np.zeros(3))
    out = {
        'A': resA['scan'], 'B': resB['scan'],
        'T': T.tolist(), 'T_canon': T_canon.tolist(),
        'yaw_deg': float(np.degrees(np.arctan2(T_canon[1, 0], T_canon[0, 0]))),
        'score': float(best['score']), 'close': best['close'], 'violations': best['viol'],
        'second_score': float(second),
        'margin': float(best['score'] - second),
        'method': 'planes+icp' if icp else 'planes',
    }
    if icp:
        out.update(fitness=best['fitness'], rmse=best['rmse'],
                   icp_shift_m=best['icp_shift'][0], icp_shift_deg=best['icp_shift'][1])
    return out


def main():
    ap = argparse.ArgumentParser(description="Стыковка двух сканов по плоскостям")
    ap.add_argument('A')
    ap.add_argument('B')
    ap.add_argument('--up', default='auto')
    ap.add_argument('--yaw', default='manhattan', choices=['manhattan', 'full'])
    ap.add_argument('--top', type=int, default=3)
    ap.add_argument('--no-icp', action='store_true')
    ap.add_argument('-o', '--output', help='JSON с позой B→A')
    args = ap.parse_args()
    resA = analyze_scan(args.A, args.up)
    resB = analyze_scan(args.B, args.up)
    print(f"{resA['scan']} ← {resB['scan']}")
    r = register_pair(resA, resB, args.yaw, args.top, not args.no_icp)
    print(json.dumps({k: v for k, v in r.items() if k not in ('T', 'T_canon')},
                     indent=1, ensure_ascii=False), encoding="utf-8")
    if args.output:
        Path(args.output).write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == '__main__':
    main()
