#!/usr/bin/env python3
"""
Подгонка геометрического элемента по щелчку: плоскость, сфера, окружность, цилиндр.

Точки — облако в канонической системе скана (сканер в начале координат: нормали
ориентируем на него, приём из surface.py). Всё в метрах.

Конвейер (идеи метрологии GOM, своими реализациями):
  1. патч вокруг щелчка — BFS по вокселям (без scipy, приём voxel_keys);
  2. нормали патча — Open3D по соседям, ориентация на сканер;
  3. робастная оценка — RANSAC с оценкой LMedS (медиана остатков, без порога);
  4. обрастание области — BFS с двойным критерием: |остаток| ≤ полосы шума И
     нормаль согласована с нормалью модели (это останавливает рост на ребре);
  5. «клиппинг-рефит» — гауссова подгонка с отбраковкой: fit → выбросить
     |остаток| > k·σ → refit, до стабилизации. Даёт σ (RMS) и размах остатков
     (отклонение формы — «плоскостность», «цилиндричность»).

Результат — dict элемента (jsonable, кроме 'used'): kind, params, sigma, span
[min, max] остатка, n точек, extent, center, area (плоскость) + 'used'
(индексы в P, для отрисовки инлайеров).

Usage (внутри сеанса/GUI):
    el = fits.fit_click(sc.down, p_canon, kind='auto')
"""

from collections import deque

import numpy as np

VOXEL = 0.05                # м — воксель сетки соседства (обычно voxel облака P)
PATCH_FIRST = 8_000         # точек в первом патче: возле щелчка щёлкнутая
                            # поверхность — большинство (LMedS это требует)
PATCH_RETRY = 30_000        # повтор, если оценка не зацепилась
PATCH_RETRY2 = 60_000       # и ещё раз, шире
MIN_REGION = 60             # точек — меньше не элемент
FIT_SUBSAMPLE = 60_000      # больше — подгонка по прореженной выборке

K_CLIP = 3.0                # клиппинг: выбрасывать |остаток| > K_CLIP·σ
BAND_SIGMA = 3.5            # полоса обрастания: |остаток| ≤ BAND_SIGMA·σ …
BAND_R = 0.004              # … но не меньше 0.4 % характерного радиуса
PLANE_FLOOR = 2e-4          # … у плоскости пол — от диагонали облака
COS_MAX = {'plane': float(np.cos(np.radians(25))),
           'cylinder': float(np.cos(np.radians(32))),
           'sphere': float(np.cos(np.radians(38))),
           'circle': float(np.cos(np.radians(25)))}
GROW_ROUNDS = 30
CURVED_OVER_FLAT = 1.1      # кривая подгонка обязана обыграть плоскость по RMS
RANSAC_ITER = 256
RANSAC_SCORE = 512          # точек в подвыборке для оценки медианы
DEGENERATE = 1e-9

MASK21 = (1 << 21) - 1
KEY_BASE = np.array([1 << 42, 1 << 21, 1], np.int64)


# ── каркас клиппинг-рефита ──────────────────────────────────────────────────

def clipped_refit(P, idx, fit_fn, res_fn, k=K_CLIP):
    """
    Гауссова подгонка с отбраковкой выбросов: fit → отбросить |остаток| > k·σ →
    refit, пока набор не стабилизируется (≤12 раундов). fit_fn(idx) → модель или
    None (вырожденность — вернуть последний хороший результат). → dict(model,
    sigma, span [min, max], used) или None.
    """
    used = np.asarray(idx, np.int64)
    result = None
    for _ in range(12):
        model = fit_fn(used)
        if model is None:
            return result
        e = res_fn(model, P[used])
        ok = np.isfinite(e)
        if not ok.any():
            return result
        sigma = float(np.sqrt(np.mean(e[ok] ** 2)))
        result = {'model': model, 'sigma': sigma,
                  'span': [float(e[ok].min()), float(e[ok].max())], 'used': used}
        if k <= 0 or sigma < DEGENERATE:
            return result
        keep = ok & (np.abs(e) <= k * sigma)
        if keep.all() or keep.sum() < 10:
            return result
        used = used[keep]
    return result


# ── плоскость ───────────────────────────────────────────────────────────────

def fit_plane_tls(P, idx):
    """Точный TLS: центроид + собственный вектор минимального собственного
    значения ковариации (гауссова подгонка плоскости — без итераций)."""
    Q = P[np.asarray(idx, np.int64)]
    if len(Q) < 3:
        return None
    q = Q - Q.mean(axis=0)
    _, V = np.linalg.eigh(q.T @ q / len(Q))
    n = V[:, 0]
    if not np.isfinite(n).all() or np.linalg.norm(n) < 1e-12:
        return None
    # нормаль — на сканер (как в planes.py: n·0 > c), сканер в начале координат
    c = float(n @ Q.mean(axis=0))
    if c > 0:
        n, c = -n, -c
    return {'n': n, 'c': c}


def plane_res(m, Q):
    return Q @ m['n'] - m['c']


def plane_axes(n):
    """Оси (u, v) в плоскости: v — вверх (Z), насколько возможно."""
    n = np.asarray(n, float)
    u = np.cross(n, [0, 1.0, 0]) if abs(n[2]) > 0.9 else np.cross(n, [0, 0, 1.0])
    u /= np.linalg.norm(u)
    return u, np.cross(n, u)


def rms_plane(P, idx):
    """RMS точек до их TLS-плоскости — с чем сравнивается кривая подгонка."""
    m = fit_plane_tls(P, idx)
    if m is None:
        return np.inf
    return float(np.sqrt(np.mean(plane_res(m, P[np.asarray(idx, np.int64)]) ** 2)))


# ── сфера ───────────────────────────────────────────────────────────────────

def fit_sphere_alg(P, idx):
    """Алгебраическая подгонка (Куп): 2q·a + t = |q|² в МНК → центр, радиус."""
    Q = P[np.asarray(idx, np.int64)]
    if len(Q) < 4:
        return None
    q = Q - Q.mean(axis=0)
    b = np.sum(q * q, axis=1)
    A = np.column_stack([2 * q, np.ones(len(q))])
    try:
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    a, t = sol[:3], sol[3]
    r2 = t + a @ a
    if not np.isfinite(r2) or r2 <= 0:
        return None
    return {'c': Q.mean(axis=0) + a, 'r': float(np.sqrt(r2))}


def sphere_res(m, Q):
    return np.linalg.norm(Q - m['c'], axis=1) - m['r']


def refine_sphere(P, idx, m, iters=150):
    """Геометрическое уточнение фиксированной точкой c ← x̄ − r̄·ū (МНК по
    ортогональным расстояниям Σ(d−r)²)."""
    Q = P[np.asarray(idx, np.int64)]
    c, r = np.asarray(m['c'], float), float(m['r'])
    m_c = Q.mean(axis=0)
    for _ in range(iters):
        d = Q - c
        L = np.linalg.norm(d, axis=1)
        good = L > 1e-12
        if not good.any():
            break
        u = d[good] / L[good, None]
        r_new = float(L[good].mean())
        c_new = m_c - r_new * u.mean(axis=0)
        done = np.linalg.norm(c_new - c) < 1e-10 * max(1.0, r)
        c, r = c_new, r_new
        if done:
            break
    if not (np.isfinite(c).all() and np.isfinite(r)):
        return m
    return {'c': c, 'r': r}


def fit_sphere_center(P, idx, r, start, iters=50):
    """Центр сферы известного радиуса — Гаусс–Ньютон по Σ(|p−c|−r)²."""
    Q = P[np.asarray(idx, np.int64)]
    c = np.asarray(start['c'], float)
    for _ in range(iters):
        d = Q - c
        L = np.linalg.norm(d, axis=1)
        good = L > 1e-12
        u = d[good] / L[good, None]
        e = L[good] - r
        try:
            step = np.linalg.solve(u.T @ u, u.T @ e)
        except np.linalg.LinAlgError:
            return None
        c = c + step
        if np.linalg.norm(step) < 1e-10 * max(1.0, r):
            break
    return {'c': c, 'r': float(r)} if np.isfinite(c).all() else None


def sphere_from4(A, B, C, D):
    """Точная сфера по 4 точкам (около копланарных → None)."""
    q = np.stack([B - A, C - A, D - A])
    rhs = np.sum(q * q, axis=1) / 2
    try:
        c_rel = np.linalg.solve(q, rhs)
    except np.linalg.LinAlgError:
        return None
    r = float(np.linalg.norm(c_rel))
    return {'c': A + c_rel, 'r': r} if r > 0 else None


# ── окружность (2D, в базисе плоскости) ─────────────────────────────────────

def fit_circle2d(uv, r_fixed=None):
    """Окружность на 2D-точках: алгебраическая (Куп) + геометрическое уточнение
    фиксированной точкой; при r_fixed — уточняется только центр. → (центр, r)."""
    uv = np.asarray(uv, float)
    if len(uv) < 3:
        return None
    q = uv - uv.mean(axis=0)
    b = np.sum(q * q, axis=1)
    A = np.column_stack([2 * q, np.ones(len(q))])
    try:
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    a, t = sol[:2], sol[2]
    r2 = t + a @ a
    if not np.isfinite(r2) or r2 <= 0:
        return None
    c = uv.mean(axis=0) + a
    r = float(r_fixed) if r_fixed is not None else float(np.sqrt(r2))
    for _ in range(60):
        d = uv - c
        L = np.linalg.norm(d, axis=1)
        good = L > 1e-12
        if not good.any():
            break
        u = d[good] / L[good, None]
        if r_fixed is None:
            r = float(L[good].mean())
        c = uv.mean(axis=0) - r * u.mean(axis=0)
    return c, r


def fit_circle_in_plane(P, idx, r_fixed=None):
    """
    Окружность в наилучшей плоскости точек: TLS-плоскость → проекция (u, v) →
    2D-окружность с клиппинг-рефитом. Модель: {n, center (3D), r, c_plane}.
    """
    pl = fit_plane_tls(P, idx)
    if pl is None:
        return None
    u_ax, v_ax = plane_axes(pl['n'])
    m0 = P[np.asarray(idx, np.int64)].mean(axis=0)

    def fit(u_idx):
        Q = P[u_idx]
        uv = np.column_stack([(Q - m0) @ u_ax, (Q - m0) @ v_ax])
        got = fit_circle2d(uv, r_fixed)
        if got is None:
            return None
        (cu, cv), r = got
        return {'n': pl['n'], 'center': m0 + cu * u_ax + cv * v_ax, 'r': r,
                'c2d': np.array([cu, cv])}

    def res(m, Q):
        uv = np.column_stack([(Q - m0) @ u_ax, (Q - m0) @ v_ax])
        return np.linalg.norm(uv - m['c2d'], axis=1) - m['r']

    out = clipped_refit(P, idx, fit, res)
    if out is None:
        return None
    m = out['model']
    out['model'] = {'n': m['n'], 'center': m['center'], 'r': m['r'],
                    'c_plane': float(m['n'] @ m['center'])}
    return out


# ── цилиндр ─────────────────────────────────────────────────────────────────

def cylinder_axis(normals):
    """Ось цилиндра по нормалям: нормали радиальны → лежат в плоскости ⊥ оси;
    ось — собственный вектор минимального разброса нормалей."""
    N = np.asarray(normals, float)
    if len(N) < 10:
        return None
    _, V = np.linalg.eigh(N.T @ N / len(N))
    a = V[:, 0]
    n = np.linalg.norm(a)
    return a / n if n > 1e-12 else None


def cylinder_from(P, idx, N, r_fixed=None):
    """Цилиндр по точкам и их нормалям (N — на всё облако, индексируется idx):
    ось из нормалей, центр и радиус — окружность на проекции ⊥ оси."""
    Q = P[np.asarray(idx, np.int64)]
    a = cylinder_axis(N[idx])
    if a is None:
        return None
    u_ax = np.cross(a, [0, 0, 1.0])
    if np.linalg.norm(u_ax) < 1e-6:
        u_ax = np.cross(a, [0, 1.0, 0])
    u_ax /= np.linalg.norm(u_ax)
    v_ax = np.cross(a, u_ax)
    m = Q.mean(axis=0)
    uv = np.column_stack([(Q - m) @ u_ax, (Q - m) @ v_ax])
    got = fit_circle2d(uv, r_fixed)
    if got is None:
        return None
    (cu, cv), r = got
    return {'p': m + cu * u_ax + cv * v_ax, 'a': a, 'r': float(r)}


def cylinder_res(m, Q):
    d = Q - m['p']
    t = d @ m['a']
    return np.linalg.norm(d - t[:, None] * m['a'], axis=1) - m['r']


def cylinder_normal(m, Q):
    """Радиальные нормали поверхности цилиндра в точках Q."""
    d = Q - m['p']
    t = d @ m['a']
    w = d - t[:, None] * m['a']
    L = np.linalg.norm(w, axis=1)
    L[L < 1e-12] = 1e-12
    return w / L[:, None]


# ── нормали ─────────────────────────────────────────────────────────────────

def patch_normals(P, idx, radius):
    """Нормали точек по соседям (Open3D), ориентированные на сканер
    (в канонической системе сканер в начале координат: N·p < 0)."""
    import open3d as o3d
    Q = P[np.asarray(idx, np.int64)]
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(Q))
    pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=radius, max_nn=30))
    N = np.asarray(pc.normals)
    N[(N * Q).sum(axis=1) > 0] *= -1
    return N


# ── воксельная сетка соседства ──────────────────────────────────────────────

class VoxelGraph:
    """Соседство точек через воксели: точки в смежных вокселях (26 соседей) —
    соседи по поверхности. Построение векторное, без scipy."""

    OFFS = np.array([[i, j, k] for i in (-1, 0, 1) for j in (-1, 0, 1)
                     for k in (-1, 0, 1) if (i, j, k) != (0, 0, 0)], np.int64)

    def __init__(self, P, voxel):
        self.P = np.asarray(P, float)
        self.voxel = float(voxel)
        q = np.floor(self.P / self.voxel).astype(np.int64) + (1 << 20)
        keys = (q[:, 0] << 42) | (q[:, 1] << 21) | q[:, 2]
        self.vox_of = np.empty(len(self.P), np.int64)          # точка → воксель
        self.keys, self.vox_of[:] = np.unique(keys, return_inverse=True)
        # точки каждого вокселя — непрерывным куском (np.unique сортирует)
        self.order = np.argsort(self.vox_of, kind='stable')
        counts = np.bincount(self.vox_of, minlength=len(self.keys))
        self.start = np.concatenate([[0], np.cumsum(counts)])
        self.index = {int(k): i for i, k in enumerate(self.keys)}

    def points_of(self, vi):
        return self.order[self.start[vi]:self.start[vi + 1]]

    def _cell(self, vi):
        k = int(self.keys[vi])
        return np.array([(k >> 42) - (1 << 20), ((k >> 21) & MASK21) - (1 << 20),
                         (k & MASK21) - (1 << 20)], np.int64)

    def patch(self, seed_idx, limit):
        """BFS по вокселям от точки: до limit точек (патч вокруг щелчка)."""
        vi0 = int(self.vox_of[seed_idx])
        seen = {vi0}
        q = deque([vi0])
        out = [self.points_of(vi0)]
        n = len(out[0])
        while q and n < limit:
            vi = q.popleft()
            cell = self._cell(vi)
            for off in self.OFFS:
                nk = int((cell + off + (1 << 20)) @ KEY_BASE)
                nb = self.index.get(nk)
                if nb is None or nb in seen:
                    continue
                seen.add(nb)
                q.append(nb)
                pts = self.points_of(nb)
                out.append(pts)
                n += len(pts)
                if n >= limit:
                    break
        return np.concatenate(out)

    def grow_bfs(self, seed_voxel, accept_fn):
        """BFS по вокселям: accept_fn(точки) → маска принятых; воксель
        расширяет фронт, только если в нём есть принятая точка (затравка —
        всегда). → маска принятых точек (len(P), bool)."""
        accepted = np.zeros(len(self.P), bool)
        q = deque([seed_voxel])
        seen = {seed_voxel}
        while q:
            vi = q.popleft()
            pts = self.points_of(vi)
            fresh = pts[~accepted[pts]]
            if len(fresh):
                ok = fresh[accept_fn(fresh)]
                accepted[ok] = True
            if vi != seed_voxel and not accepted[pts].any():
                continue
            cell = self._cell(vi)
            for off in self.OFFS:
                nk = int((cell + off + (1 << 20)) @ KEY_BASE)
                nb = self.index.get(nk)
                if nb is not None and nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        return accepted


# ── робастная оценка (RANSAC + LMedS) ───────────────────────────────────────

def _score_subset(patch):
    step = max(1, len(patch) // RANSAC_SCORE)
    return patch[::step][:RANSAC_SCORE]


def ransac_plane(P, patch, rng):
    """Плоскость по 3 точкам, оценка — медиана остатков (LMedS, без порога).
    → (модель, медиана) | (None, inf)."""
    best, best_med = None, np.inf
    n = len(patch)
    sub = _score_subset(patch)
    for _ in range(RANSAC_ITER):
        ids = patch[rng.integers(0, n, 3)]
        if len(np.unique(ids)) < 3:
            continue
        A, B, C = P[ids]
        nrm = np.cross(B - A, C - A)
        L = np.linalg.norm(nrm)
        if L < 1e-12:
            continue
        nrm /= L
        m = {'n': nrm, 'c': float(nrm @ A)}
        med = float(np.median(np.abs(plane_res(m, P[sub]))))
        if med < best_med:
            best, best_med = m, med
    return best, best_med


def ransac_sphere(P, patch, rng):
    best, best_med = None, np.inf
    n = len(patch)
    sub = _score_subset(patch)
    diag = float(np.linalg.norm(np.ptp(P[patch], axis=0)))
    for _ in range(RANSAC_ITER):
        ids = patch[rng.integers(0, n, 4)]
        if len(np.unique(ids)) < 4:
            continue
        m = sphere_from4(*P[ids])
        if m is None or not (0.02 * diag < m['r'] < 200 * diag):
            continue
        med = float(np.median(np.abs(sphere_res(m, P[sub]))))
        if med < best_med:
            best, best_med = m, med
    return best, best_med


def _consensus(P, patch, res_fn, model, med):
    """Инлайнеры — всё внутри max(3·1.4826·медиана, 1e-5·диагонали патча)."""
    diag = float(np.linalg.norm(np.ptp(P[patch], axis=0)))
    thr = max(3 * 1.4826 * med, 1e-5 * diag)
    e = np.abs(res_fn(model, P[patch]))
    return patch[e <= thr]


# ── обрастание области ──────────────────────────────────────────────────────

def grow_region(P, N, graph, patch, seed_idx, kind, model, sigma, r_fixed=None):
    """
    Область поверхности вокруг затравки: BFS по вокселям всего облака,
    членство — |остаток| ≤ полосы И |n·n_модели| ≥ COS_MAX[kind]; модель
    переуточняется каждый раунд (клиппинг-рефит по области), BFS каждый раунд
    с начала. Патч нужен только для масштаба полосы. N — нормали всего облака.
    → dict(model, sigma, span, used) | None.
    """
    diag = float(np.linalg.norm(np.ptp(P[patch], axis=0)))
    res_fn = {'plane': plane_res, 'sphere': sphere_res,
              'cylinder': cylinder_res, 'circle': plane_res}[kind]
    prev = MIN_REGION
    result = None
    for _ in range(GROW_ROUNDS):
        # окружность для тестов членства — как плоскость (n, c_plane)
        m_test = {'n': model['n'], 'c': model['c_plane']} if kind == 'circle' else model
        if kind == 'plane' or kind == 'circle':
            band = max(BAND_SIGMA * sigma, PLANE_FLOOR * diag)

            def model_normal(m, Q):
                return m['n']
        else:
            r = float(model.get('r', 0)) or diag
            band = max(BAND_SIGMA * sigma, BAND_R * r)
            if kind == 'sphere':
                def model_normal(m, Q):
                    d = Q - m['c']
                    L = np.linalg.norm(d, axis=1)
                    L[L < 1e-12] = 1e-12
                    return d / L[:, None]
            else:
                def model_normal(m, Q):
                    return cylinder_normal(m, Q)

        def accept(pts):
            e = res_fn(m_test, P[pts])
            nm = np.broadcast_to(model_normal(m_test, P[pts]), (len(pts), 3))
            align = np.abs(np.sum(N[pts] * nm, axis=1))
            return (np.abs(e) <= band) & (align >= COS_MAX[kind])

        accepted = graph.grow_bfs(int(graph.vox_of[seed_idx]), accept)
        idx = np.flatnonzero(accepted)
        if len(idx) < MIN_REGION:
            return None
        fit = _refit_kind(P, N, idx, kind, r_fixed)
        if fit is None:
            return result
        model, sigma = fit['model'], fit['sigma']
        result = fit
        if len(idx) - prev <= max(3, 0.002 * prev):
            break
        prev = len(idx)
    return result


# ── подгонки по типу (для клиппинг-рефита) ─────────────────────────────────

def _refit_kind(P, N, idx, kind, r_fixed=None):
    """Клиппинг-рефит области подгонкой своего типа. → dict(model, sigma,
    span, used) | None. N — нормали на всё облако (нужны цилиндру)."""
    if kind == 'plane':
        return clipped_refit(P, idx, lambda u: fit_plane_tls(P, u), plane_res)
    if kind == 'sphere':
        def fit(u):
            m = fit_sphere_alg(P, u)
            if m is None:
                return None
            m = refine_sphere(P, u, m)
            return fit_sphere_center(P, u, r_fixed, m) if r_fixed else m
        return clipped_refit(P, idx, fit, sphere_res)
    if kind == 'cylinder':
        def fit(u):
            sub = u if len(u) <= FIT_SUBSAMPLE else \
                u[np.linspace(0, len(u) - 1, FIT_SUBSAMPLE).astype(int)]
            return cylinder_from(P, sub, N, r_fixed)
        return clipped_refit(P, idx, fit, cylinder_res)
    if kind == 'circle':
        return fit_circle_in_plane(P, idx, r_fixed)
    return None


# ── верхнеуровневая точка входа ─────────────────────────────────────────────

def _classify(P, Nf, patch, seed_idx, radius=0.3):
    """Плоская ли поверхность под щелчком: разброс нормалей точек возле щелчка
    (не всего патча — патч у края захватывает соседнюю поверхность)."""
    near = patch[np.linalg.norm(P[patch] - P[seed_idx], axis=1) <= radius]
    N = Nf[near if len(near) >= 20 else patch[:200]]
    mean_n = N.mean(axis=0)
    L = np.linalg.norm(mean_n)
    if L < 1e-6:
        return 'curved'
    ang = np.degrees(np.arccos(np.clip(N @ (mean_n / L), -1, 1)))
    return 'flat' if np.median(ang) < 8 else 'curved'


def _curved_makes_sense(P, fit, kind):
    """Кривая подгонка обязана обыграть плоскость: RMS к плоскости ≥
    CURVED_OVER_FLAT·RMS кривой, иначе «радиус» взят из шума."""
    return rms_plane(P, fit['used']) >= CURVED_OVER_FLAT * fit['sigma']


def _circle_makes_sense(P, fit):
    """Окружность обязана обыграть прямую на тех же точках (в базисе плоскости)."""
    m = fit['model']
    u_ax, v_ax = plane_axes(m['n'])
    Q = P[fit['used']]
    uv = np.column_stack([(Q - m['center']) @ u_ax, (Q - m['center']) @ v_ax])
    A = np.column_stack([uv[:, 0], np.ones(len(uv))])
    try:
        coef, *_ = np.linalg.lstsq(A, uv[:, 1], rcond=None)
    except np.linalg.LinAlgError:
        return True
    lin_rms = float(np.sqrt(np.mean((uv[:, 1] - A @ coef) ** 2)))
    return lin_rms >= CURVED_OVER_FLAT * fit['sigma']


def _finish(P, kind, fit, voxel):
    """Модель → dict элемента (jsonable) + used (индексы в P)."""
    m, used = fit['model'], fit['used']
    el = {'kind': kind, 'sigma': float(fit['sigma']),
          'span': [float(fit['span'][0]), float(fit['span'][1])],
          'n': int(len(used))}
    if kind == 'plane':
        u_ax, v_ax = plane_axes(m['n'])
        Q = P[used]
        uv = np.column_stack([Q @ u_ax, Q @ v_ax])
        el['params'] = {'n': np.asarray(m['n']).tolist(), 'c': float(m['c'])}
        el['extent'] = [float(np.ptp(uv[:, 0])), float(np.ptp(uv[:, 1]))]
        el['area'] = float(len(used) * voxel ** 2)
        el['center'] = Q.mean(axis=0).tolist()
    elif kind == 'sphere':
        el['params'] = {'c': np.asarray(m['c']).tolist(), 'r': float(m['r'])}
        el['extent'] = [2 * float(m['r']), 2 * float(m['r'])]
        el['center'] = np.asarray(m['c']).tolist()
    elif kind == 'cylinder':
        t = (P[used] - m['p']) @ m['a']
        el['params'] = {'p': np.asarray(m['p']).tolist(), 'a': np.asarray(m['a']).tolist(),
                        'r': float(m['r']), 'half': float(max(np.abs(t).max(), 1e-3))}
        el['extent'] = [2 * float(m['r']), 2 * el['params']['half']]
        el['center'] = (np.asarray(m['p']) + np.asarray(m['a']) * float(np.median(t))).tolist()
    elif kind == 'circle':
        el['params'] = {'center': np.asarray(m['center']).tolist(),
                        'n': np.asarray(m['n']).tolist(), 'r': float(m['r'])}
        el['extent'] = [2 * float(m['r']), 2 * float(m['r'])]
        el['center'] = np.asarray(m['center']).tolist()
    el['used'] = used
    return el


def _estimate(P, N, patch, kind, rng):
    """Робастная оценка на патче под тип. → dict(model, sigma) | None."""
    if kind == 'plane':
        m, med = ransac_plane(P, patch, rng)
        if m is None:
            return None
        inl = _consensus(P, patch, plane_res, m, med)
        if len(inl) >= 10:
            fit = clipped_refit(P, inl, lambda u: fit_plane_tls(P, u), plane_res)
            if fit is not None:
                return fit
        return {'model': m, 'sigma': max(1.4826 * med, 1e-4)}
    if kind == 'sphere':
        m, med = ransac_sphere(P, patch, rng)
        if m is None:
            return None
        inl = _consensus(P, patch, sphere_res, m, med)
        if len(inl) < 10:
            return None

        def fit(u):
            alg = fit_sphere_alg(P, u)
            return refine_sphere(P, u, alg) if alg is not None else None
        return clipped_refit(P, inl, fit, sphere_res)
    if kind == 'cylinder':
        m = cylinder_from(P, patch, N)
        if m is None:
            return None
        med = float(np.median(np.abs(cylinder_res(m, P[patch]))))
        inl = _consensus(P, patch, cylinder_res, m, med)
        if len(inl) < 10:
            return None
        return clipped_refit(P, inl, lambda u: cylinder_from(P, u, N), cylinder_res)
    if kind == 'circle':
        return fit_circle_in_plane(P, patch)
    return None


# ── сохранённые элементы: остатки и каркас для отрисовки ───────────────────

def element_residual(el, Q):
    """Остаток точек Q до поверхности сохранённого элемента (по его params).
    У окружности — расстояние до самой кривой (не до диска)."""
    k, m = el['kind'], el['params']
    if k == 'plane':
        return Q @ np.asarray(m['n'], float) - float(m['c'])
    if k == 'sphere':
        return np.linalg.norm(Q - np.asarray(m['c'], float), axis=1) - float(m['r'])
    if k == 'cylinder':
        return cylinder_res({'p': np.asarray(m['p']), 'a': np.asarray(m['a']),
                             'r': float(m['r'])}, Q)
    if k == 'circle':
        c, n, r = np.asarray(m['center'], float), np.asarray(m['n'], float), float(m['r'])
        u_ax, v_ax = plane_axes(n)
        d = Q - c
        du, dv, dz = d @ u_ax, d @ v_ax, d @ n
        return np.hypot(np.hypot(du, dv) - r, dz)
    return np.zeros(len(Q))


def element_wire(el, seg=72):
    """
    Каркас элемента для отрисовки линиями. → (точки K×3, сегменты M×2) в системе
    элемента (канон. система его скана). Плоскость — прямоугольник extent,
    сфера — три больших круга, цилиндр — торцы и образующие, окружность — круг.
    """
    k, m = el['kind'], el['params']
    th = np.linspace(0, 2 * np.pi, seg, endpoint=False)

    def loop(pts):
        i0 = len(out)
        out.extend(pts)
        segs.extend([[i0 + i, i0 + (i + 1) % len(pts)] for i in range(len(pts))])

    out, segs = [], []
    if k == 'plane':
        c = np.asarray(el['center'], float)
        u_ax, v_ax = plane_axes(m['n'])
        w, h = el.get('extent', [1.0, 1.0])
        loop([c + su * u_ax * w / 2 + sv * v_ax * h / 2
              for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
    elif k == 'sphere':
        c, r = np.asarray(m['c'], float), float(m['r'])
        n = c / np.linalg.norm(c)                     # от сканера к центру
        u_ax, v_ax = plane_axes(n)
        for a, b in ((u_ax, v_ax), (u_ax, n), (v_ax, n)):
            loop([c + r * (np.cos(t) * a + np.sin(t) * b) for t in th])
    elif k == 'cylinder':
        p, a, r = np.asarray(m['p'], float), np.asarray(m['a'], float), float(m['r'])
        half = float(m.get('half', 0.1))
        u_ax = np.cross(a, [0, 0, 1.0])
        if np.linalg.norm(u_ax) < 1e-6:
            u_ax = np.cross(a, [0, 1.0, 0])
        u_ax /= np.linalg.norm(u_ax)
        v_ax = np.cross(a, u_ax)
        for t0 in (-half, half):
            loop([p + t0 * a + r * (np.cos(t) * u_ax + np.sin(t) * v_ax) for t in th])
        for t in (0, np.pi / 2, np.pi, 3 * np.pi / 2):
            d = r * (np.cos(t) * u_ax + np.sin(t) * v_ax)
            i0 = len(out)
            out.extend([p - half * a + d, p + half * a + d])
            segs.append([i0, i0 + 1])
    elif k == 'circle':
        c, n, r = np.asarray(m['center'], float), np.asarray(m['n'], float), float(m['r'])
        u_ax, v_ax = plane_axes(n)
        loop([c + r * (np.cos(t) * u_ax + np.sin(t) * v_ax) for t in th])
    return np.asarray(out, float), np.asarray(segs, np.int64)


def fit_click(P, seed, kind='auto', d_fixed=None, voxel=VOXEL,
              patch_first=PATCH_FIRST, patch_retry=PATCH_RETRY):
    """
    Элемент по щелчку. P — облако (канон. система скана), seed — точка щелчка,
    kind: auto | plane | sphere | cylinder | circle, d_fixed — известный
    диаметр (сфера/цилиндр/окружность: радиус держат). → dict элемента (см.
    заголовок модуля; 'used' — индексы в P) или None.
    """
    P = np.asarray(P, float)
    if len(P) < 100:
        return None
    r_fixed = d_fixed / 2.0 if d_fixed else None
    seed_idx = int(np.argmin(np.linalg.norm(P - np.asarray(seed, float), axis=1)))
    graph = VoxelGraph(P, voxel)
    rng = np.random.default_rng(0x5eed)
    for limit in (patch_first, patch_retry, PATCH_RETRY2):
        patch = graph.patch(seed_idx, limit)
        if len(patch) < 30:
            return None
        # нормали — один раз на всё облако: область растёт по всей поверхности
        N = patch_normals(P, np.arange(len(P)), radius=3 * voxel)
        if kind != 'auto':
            kinds = [kind]
        elif _classify(P, N, patch, seed_idx) == 'flat':
            kinds = ['plane']
        else:
            kinds = ['sphere', 'cylinder']
        for k in kinds:
            est = _estimate(P, N, patch, k, rng)
            if est is None:
                continue
            grown = grow_region(P, N, graph, patch, seed_idx, k,
                                est['model'], est['sigma'], r_fixed)
            if grown is None:
                continue
            fit = _refit_kind(P, N, grown['used'], k, r_fixed)
            if fit is None or len(fit['used']) < MIN_REGION:
                continue
            if k in ('sphere', 'cylinder') and r_fixed is None and \
                    not _curved_makes_sense(P, fit, k):
                continue
            if k == 'circle' and r_fixed is None and not _circle_makes_sense(P, fit):
                continue
            return _finish(P, k, fit, voxel)
    return None
