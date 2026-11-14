"""Фотопривязка и окраска облака точек по статичным фото (математика без GUI).

numpy + Pillow; OpenCV (opencv-python-headless) опциональна — функции краёв
имеют numpy-fallback, тесты cv2-этапов помечаются skipIf по HAS_CV2.

Конвенции
---------
- Камера: поза T — 4×4, ``T[:3, :3] = R``, ``T[:3, 3] = C`` (центр камеры).
  Оси камеры — СТОЛБЦЫ R: ``R[:, 0]`` — вправо кадра, ``R[:, 1]`` — вниз кадра,
  ``R[:, 2]`` — вперёд от камеры (в сцену). Точка в системе камеры:
  ``X = (P − C) @ R``, глубина — ``X[:, 2]`` (положительная перед камерой).
- Пиксель: ``u = cx + f·X0/X2``, ``v = cy + f·X1/X2``, главная точка в центре
  кадра, ``f = focal_px(f35, w, h)``.
- Фокусное 35 мм — конвенция ПО ДИАГОНАЛИ кадра: ``f = f35·hypot(w, h)/43.27``
  (не по ширине: портретные снимки иначе врут на треть).
- Пары для PnP: ``uv`` — N×2 пиксели, ``P`` — N×3 точки в той системе, в которой
  выражена поза (в сеансе — каноническая система якорного скана).
- Панорама 360° (equirectangular, кадр 2:1): поза та же 4×4, фокусного нет —
  ``lon = atan2(X0, X2)`` от «вперёд» (вправо, ось R[:, 0]), ``lat = arcsin(X1/r)``
  вниз от горизонта (ось R[:, 1] смотрит вниз кадра); центр кадра — forward,
  верх — зенит. Каждая точка «в кадре», кроме ближней зоны (``EPS_Z_PANO``).
"""
import json
import math
import re

import numpy as np

try:                                        # opencv-python-headless — опциональна
    import cv2
    HAS_CV2 = True
except Exception:                           # pragma: no cover - зависит от окружения
    cv2 = None
    HAS_CV2 = False

MAX_IMG_PX = 4200          # длинная сторона декодированного изображения в памяти
REND_PX = 1600             # длинная сторона изображения для окраски
EPS_Z = 0.05               # м — ближе к камере точки не красим (штатив/камера)
EPS_Z_PANO = 0.35          # м — то же для панорамы 360° (селфи-палка/руки в надире)
DT_CLIP = 25.0             # px — обрезка невязки: вне покрытия фото позу не тянет
FOCAL_GRID = (14, 16, 18, 20, 23, 26, 30, 35, 40, 50, 65)   # экв. 35 мм, мм
F35_DIAG = 43.27           # мм — диагональ полного кадра


# ---------------------------------------------------------------------------
# изображения и фокусное
# ---------------------------------------------------------------------------

def image_size(path):
    """(w, h) файла фото — по заголовку, без декодирования пикселей."""
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def load_image(path, max_px=MAX_IMG_PX):
    """Фото как H×W×3 uint8; EXIF-поворот применён, длинная сторона ≤ max_px."""
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        w, h = im.size
        k = max_px / max(w, h)
        if k < 1.0:
            im = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
        return np.asarray(im.convert('RGB'))


def exif_focal(path):
    """Эквивалент 35 мм (мм, float) из EXIF-тега 41989 (под-IFD Exif) или None."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            sub = ex.get_ifd(0x8769)        # Exif SubIFD — там лежит 41989
            v = sub.get(41989) if sub is not None else None
            if v is None:
                v = ex.get(41989)
            if v is None:
                return None
            return float(v)
    except Exception:
        return None


def is_pano(path):
    """Панорама 360° (equirectangular, полный охват)?

    Да, если соотношение сторон 2:1 (допуск 1.9–2.1) или XMP GPano объявляет
    ``ProjectionType="equirectangular"`` И кадр не обрезан (FullPano* покрывает
    всё изображение) — обрезанную панораму полноэкранная формула не описывает.
    """
    from PIL import Image
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        return False
    if h > 0 and 1.9 <= w / h <= 2.1:
        return True
    try:
        with open(path, 'rb') as f:
            head = f.read(262144)
    except OSError:
        return False
    if not re.search(rb'ProjectionType\s*=\s*"equirectangular"', head):
        return False

    def tag(name):
        m = re.search(name + rb'="(\d+)"', head)
        return int(m.group(1)) if m else None

    vals = (tag(b'CroppedAreaLeftPixels'), tag(b'CroppedAreaTopPixels'),
            tag(b'FullPanoWidthPixels'), tag(b'FullPanoHeightPixels'))
    if None in vals:
        return False
    fl, ft, fw, fh = vals
    return fl == 0 and ft == 0 and fw == w and fh == h


def focal_px(f35, w, h=None):
    """Фокусное в пикселях из эквивалента 35 мм (конвенция по диагонали кадра)."""
    if not f35:
        return None
    d = max(w, h) if h is None else math.hypot(w, h)
    return f35 * d / F35_DIAG


def look_R(forward, up=(0.0, 0.0, 1.0)):
    """R камеры (столбцы right/down/forward), смотрящей вдоль forward.

    up — подсказка «верха» мира (не обязана быть точной).
    """
    f = np.asarray(forward, float)
    f = f / np.linalg.norm(f)
    u = np.asarray(up, float)
    r = np.cross(f, u)
    if np.linalg.norm(r) < 1e-9:               # смотрим вдоль вертикали
        r = np.cross(f, (0.0, 1.0, 0.0))
    r = r / np.linalg.norm(r)
    d = np.cross(f, r)
    return np.column_stack([r, d, f])


def T_of(R, C):
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = C
    return T


# ---------------------------------------------------------------------------
# проекция
# ---------------------------------------------------------------------------

def project(P, T, f35, w, h):
    """Пинхол-проекция точек P (N×3) при позе T.

    Возвращает (uv N×2 float, z N глубина, ok N bool «в кадре и перед камерой»).
    """
    f = focal_px(f35, w, h)
    if f is None:
        raise ValueError('фокусное не задано')
    X = (np.asarray(P, float) - T[:3, 3]) @ T[:3, :3]
    z = X[:, 2]
    with np.errstate(divide='ignore', invalid='ignore'):
        u = (w - 1) / 2 + f * X[:, 0] / z
        v = (h - 1) / 2 + f * X[:, 1] / z
    uv = np.column_stack([u, v])
    ok = (z > EPS_Z) & (u >= 0) & (u < w) & (v >= 0) & (v < h)
    ok &= np.isfinite(uv).all(axis=1)
    return uv, z, ok


def residuals(uv, P, T, f35, w, h):
    """Пиксельная ошибка пар (N,) при позе T; «за камерой» — 1e3."""
    p2, z, _ = project(P, T, f35, w, h)
    r = np.hypot(*(p2 - np.asarray(uv, float)).T)
    r = np.where(z > EPS_Z, r, 1e3)
    return r


def sigma_px(uv, P, T, f35, w, h):
    return float(np.sqrt(np.mean(residuals(uv, P, T, f35, w, h) ** 2)))


# ---------------------------------------------------------------------------
# проекция панорамы 360° (equirectangular)
# ---------------------------------------------------------------------------

def project_pano(P, T, w, h):
    """Equirect-проекция точек P (N×3) при позе T (панорама 360°, кадр 2:1).

    Возвращает (uv N×2 float, r N дальность, ok N bool): ok = r > EPS_Z_PANO
    (ближняя зона — селфи-палка/руки — не красится и не считается парой).
    Вне ближней зоны точка всегда «в кадре»: весь мир вокруг камеры.
    """
    X = (np.asarray(P, float) - T[:3, 3]) @ T[:3, :3]
    r = np.linalg.norm(X, axis=1)
    lon = np.arctan2(X[:, 0], X[:, 2])
    lat = np.arcsin(np.clip(X[:, 1] / np.maximum(r, 1e-12), -1.0, 1.0))
    uv = np.column_stack([(0.5 + lon / (2.0 * math.pi)) * w,
                          (0.5 + lat / math.pi) * h])
    ok = (r > EPS_Z_PANO) & np.isfinite(uv).all(axis=1)
    return uv, r, ok


def rays_pano(uv, w, h):
    """Единичные направления пикселей панорамы (N×3) в системе камеры
    (обратная к project_pano)."""
    uv = np.asarray(uv, float)
    lon = (uv[:, 0] / w - 0.5) * 2.0 * math.pi
    lat = (uv[:, 1] / h - 0.5) * math.pi
    cl = np.cos(lat)
    return np.column_stack([cl * np.sin(lon), np.sin(lat), cl * np.cos(lon)])


def residuals_pano(uv, P, T, w, h):
    """Пиксельная ошибка пар панорамы (N,); du — через шов (wrap-around),
    dv — с обрезкой h/4 (полюса); ближняя зона — 1e3."""
    p2, r, _ = project_pano(P, T, w, h)
    d = p2 - np.asarray(uv, float)
    d[:, 0] = (d[:, 0] + w / 2) % w - w / 2
    d[:, 1] = np.clip(d[:, 1], -h / 4, h / 4)
    rr = np.hypot(d[:, 0], d[:, 1])
    return np.where(r > EPS_Z_PANO, rr, 1e3)


def sigma_pano(uv, P, T, w, h):
    return float(np.sqrt(np.mean(residuals_pano(uv, P, T, w, h) ** 2)))


# ---------------------------------------------------------------------------
# вращения
# ---------------------------------------------------------------------------

def rotvec_R(rv):
    """R = exp([rv]×) (Родригес)."""
    th = float(np.linalg.norm(rv))
    if th < 1e-12:
        return np.eye(3)
    k = rv / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1.0 - np.cos(th)) * (K @ K)


def rotvec(R):
    """rv = log(R) (робастно у 0 и π)."""
    c = min(1.0, max(-1.0, (np.trace(R) - 1.0) / 2))
    th = float(np.arccos(c))
    w = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
    if th < 1e-9:
        return w * 0.5
    if math.pi - th < 1e-6:
        A = (R + np.eye(3)) / 2
        d = np.diag(A)
        i = int(np.argmax(d))
        k = A[:, i] / max(math.sqrt(d[i]), 1e-300)
        k = k / np.linalg.norm(k)
        if np.dot(k, w) < 0:
            k = -k
        return k * th
    return w / (2 * np.sin(th)) * th


def _so3(R):
    """Проекция на SO(3) (шумное «почти вращение»)."""
    U, _, Vt = np.linalg.svd(R)
    return U @ Vt


def rot_err_deg(Ra, Rb):
    """Угол между поворотами, градусы."""
    dR = Ra @ Rb.T
    c = min(1.0, max(-1.0, (np.trace(dR) - 1.0) / 2))
    return float(np.degrees(np.arccos(c)))


# ---------------------------------------------------------------------------
# решение позы из пар (PnP)
# ---------------------------------------------------------------------------

def _solve_dlt(uv, P, f35, w, h):
    """DLT c нормировкой знака (зеркальное решение исключено до проекции на SO(3))."""
    f = focal_px(f35, w, h)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    xn = (uv[:, 0] - cx) / f
    yn = (uv[:, 1] - cy) / f
    Xh = np.hstack([np.asarray(P, float), np.ones((len(P), 1))])
    A = np.zeros((2 * len(P), 12))
    for i in range(len(P)):
        row = Xh[i]
        A[2 * i, 0:4] = row
        A[2 * i, 8:12] = -xn[i] * row
        A[2 * i + 1, 4:8] = row
        A[2 * i + 1, 8:12] = -yn[i] * row
    _, S, Vt = np.linalg.svd(A)
    if 2 * len(P) >= 12 and S[-1] > 0.05 * S[0]:
        return None                             # вырожденная конфигурация
    M = Vt[-1].reshape(3, 4)
    if np.linalg.det(M[:, :3]) < 0:             # знак ДО проекции на SO(3)
        M = -M
    Rc = _so3(M[:, :3])                         # world→camera
    _, S2, _ = np.linalg.svd(M[:, :3])
    if S2[1] < 1e-12:
        return None
    t = M[:, 3] / S2[1]
    C = -Rc.T @ t
    return T_of(Rc.T, C)


def _plane_of(P):
    """(c, u_ax, v_ax, n, coplanar) — плоскость точек SVD-базисом."""
    c = P.mean(axis=0)
    _, S, Vt = np.linalg.svd(P - c)
    coplanar = S[2] <= 0.02 * max(S[0], 1e-9)
    return c, Vt[0], Vt[1], Vt[2], coplanar


def _solve_planar(uv, P, f35, w, h):
    """Копланарные пары: гомография в нормированные координаты → кандидаты позы.

    Возвращает список T (обычно два — стороны плоскости; точки самой плоскости
    их НЕ различают, выбор за caller по hint-облаку/глубинам).
    """
    c, u_ax, v_ax, _, coplanar = _plane_of(P)
    if not coplanar:
        return []
    f = focal_px(f35, w, h)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    xy = (P - c) @ np.column_stack([u_ax, v_ax])
    xn = (uv[:, 0] - cx) / f
    yn = (uv[:, 1] - cy) / f
    Hn = dlt_h(xy, np.column_stack([xn, yn]))
    if Hn is None:
        return []
    return _decompose_hn(Hn, c, u_ax, v_ax)


def _decompose_hn(Hn, origin, u_ax, v_ax):
    """Гомография «локальные координаты плоскости → нормированный кадр» → позы.

    Разложение даёт позу в локальной системе плоскости; здесь она переводится
    в систему точек (origin + базис u_ax/v_ax). Два кандидата — ± масштаб.
    """
    h1, h2, h3 = Hn[:, 0], Hn[:, 1], Hn[:, 2]
    n = (np.linalg.norm(h1) + np.linalg.norm(h2)) / 2
    if n < 1e-12:
        return []
    W = np.column_stack([u_ax, v_ax, np.cross(u_ax, v_ax)])   # локаль → мир
    L = np.eye(4)                                             # мир → локаль
    L[:3, :3] = W.T
    L[:3, 3] = -W.T @ origin
    out = []
    for s in (1.0, -1.0):
        Rd = _so3(np.column_stack([s * h1 / n, s * h2 / n,
                                   np.cross(s * h1 / n, s * h2 / n)]))
        Td = np.eye(4)                                        # локаль → камера
        Td[:3, :3] = Rd
        Td[:3, 3] = s * h3 / n
        M = Td @ L                                            # мир → камера
        C = -M[:3, :3].T @ M[:3, 3]
        out.append(T_of(M[:3, :3].T, C))
    return out


def _pick_pose(cands, uv, P, f35, w, h, hint=None):
    """Лучший кандидат: все глубины пар > EPS_Z и минимум σ; тай-брейк — hint-облако."""
    valid = []
    for T0 in cands:
        T = _refine(uv, P, T0, f35, w, h)
        _, z, _ = project(P, T, f35, w, h)
        if (z > EPS_Z).all():
            valid.append((T, sigma_px(uv, P, T, f35, w, h)))
    if not valid:
        return None, None
    sg = [v[1] for v in valid]
    best = int(np.argmin(sg))
    if hint is not None and len(valid) > 1 and sg[best] < sg[1 - best] + 1e-6:
        # копланарные пары не различают сторон — глядим на всё облако
        counts = [visible_count(v[0], hint, f35, w, h) for v in valid]
        best = int(np.argmax(counts))
    return valid[best]


def visible_count(T, P, f35, w, h):
    """Число точек P в кадре и перед камерой — критерий выбора кандидата позы."""
    _, _, ok = project(P, T, f35, w, h)
    return int(ok.sum())


def _wahba(uv, P, C, f35, w, h):
    """Поворот при известном центре камеры (≥3 пары), SVD-Вахба."""
    f = focal_px(f35, w, h)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    cam = np.column_stack([(uv[:, 0] - cx) / f, (uv[:, 1] - cy) / f, np.ones(len(uv))])
    cam /= np.linalg.norm(cam, axis=1, keepdims=True)
    world = P - C
    world /= np.linalg.norm(world, axis=1, keepdims=True)
    M = cam.T @ world                            # Σ a_i b_iᵀ
    U, _, Vt = np.linalg.svd(M)
    D = np.diag([1.0, 1.0, np.linalg.det(U @ Vt)])
    Rc = U @ D @ Vt                              # world→camera
    return T_of(Rc.T, C)


def _refine(uv, P, T0, f35, w, h, iters=30):
    """Гаусс–Ньютон по пиксельным невязкам пинхола (обёртка над _refine_res)."""

    def res(T):
        p2, z, _ = project(P, T, f35, w, h)
        r = p2 - uv
        r[z <= EPS_Z] = 1e3
        return r.ravel()

    return _refine_res(res, T0, iters)


def _refine_res(res, T0, iters=30):
    """Гаусс–Ньютон по невязкам res(T) → (N,) (численный якобиан, 6-DOF).

    Поворот параметризуется МАЛЫМ инкрементом rotvec_R(δ) @ R (перецентровка
    на каждом шаге) — глобальный rvec имеет сингулярность при повороте π
    (камера «вдоль −Z» с миром Z-вверх — ровно этот случай).
    """
    R, C = T0[:3, :3].copy(), T0[:3, 3].copy()
    T = T_of(R, C)
    r0 = res(T)
    best = (float(np.mean(r0 ** 2)), T)
    hC, hR = 1e-6, 1e-8
    for _ in range(iters):
        J = np.empty((len(r0), 6))
        for i in range(3):
            J[:, i] = (res(T_of(R, C + hC * np.eye(3)[i])) - r0) / hC
        for i in range(3):
            J[:, 3 + i] = (res(T_of(rotvec_R(hR * np.eye(3)[i]) @ R, C)) - r0) / hR
        lam = 1e-8 + 1e-9 * float(np.mean(np.abs(J)))
        Hm = J.T @ J + lam * np.eye(6)
        try:
            dx = np.linalg.solve(Hm, -J.T @ r0)
        except np.linalg.LinAlgError:
            break
        moved = False
        k = 1.0
        for _ in range(6):                       # урезка шага при ухудшении
            Tn = T_of(rotvec_R(k * dx[3:6]) @ R, C + k * dx[:3])
            rn = res(Tn)
            if float(np.mean(rn ** 2)) <= best[0]:
                R, C, T, r0 = rotvec_R(k * dx[3:6]) @ R, C + k * dx[:3], Tn, rn
                if float(np.mean(rn ** 2)) < best[0]:
                    best = (float(np.mean(rn ** 2)), Tn)
                moved = True
                break
            k *= 0.5
        if not moved or best[0] < 1e-6:
            break
    return best[1]


def z_advice():
    return ('не сходится: проверьте фокусное (EXIF/подбор), добавьте пары '
            'на непараллельной поверхности или непараллельную вторую поверхность')


def solve(uv, P, f35, w, h, center=None, hint=None):
    """Поза камеры из пар «пиксель ↔ точка» (≥4; с известным center — ≥3).

    Возвращает (T, σ_px) или (None, None). Пары в якорной системе; hint —
    опорное облако (например, прореженный якорный скан) для выбора стороны
    плоскости у копланарных пар.
    """
    uv = np.asarray(uv, float)
    P = np.asarray(P, float)
    if len(uv) < 4 or f35 is None:
        return None, None
    if center is not None and len(uv) >= 3:
        T0 = _wahba(uv, P, np.asarray(center, float), f35, w, h)
        T = _refine(uv, P, T0, f35, w, h)
        _, z, _ = project(P, T, f35, w, h)
        if (z <= EPS_Z).any():
            return None, None
        return T, sigma_px(uv, P, T, f35, w, h)
    if not _plane_of(P)[4]:                     # некопланарные пары — DLT
        T0 = _solve_dlt(uv, P, f35, w, h)
        if T0 is not None:
            T = _refine(uv, P, T0, f35, w, h)
            _, z, _ = project(P, T, f35, w, h)
            if (z > EPS_Z).all():
                return T, sigma_px(uv, P, T, f35, w, h)
        return None, None
    # копланарные пары — гомография, две стороны плоскости
    return _pick_pose(_solve_planar(uv, P, f35, w, h), uv, P, f35, w, h, hint)


def _wahba_pano(uv, P, C, w, h):
    """Поворот панорамы при известном центре камеры (≥3 пары), SVD-Вахба
    на направлениях лучей (аналог _wahba для пинхола)."""
    cam = rays_pano(uv, w, h)
    world = np.asarray(P, float) - np.asarray(C, float)
    world /= np.linalg.norm(world, axis=1, keepdims=True)
    M = cam.T @ world                            # Σ a_i b_iᵀ
    U, _, Vt = np.linalg.svd(M)
    D = np.diag([1.0, 1.0, np.linalg.det(U @ Vt)])
    Rc = U @ D @ Vt                              # world→camera
    return T_of(Rc.T, C)


def _pano_yaw_R(psi):
    """R камеры (right/down/forward) с «вперёд» [cos ψ, sin ψ, 0] — горизонт."""
    return np.column_stack([[math.sin(psi), -math.cos(psi), 0.0],
                            [0.0, 0.0, -1.0],
                            [math.cos(psi), math.sin(psi), 0.0]])


def solve_pano(uv, P, w, h, center=None):
    """Поза панорамы 360° из пар «пиксель ↔ точка» (≥4; с известным center — ≥3).

    Возвращает (T, σ_px). Фокусного нет: поза = центр + поворот. Центр известен
    (камера у сканера) — поворот по Вахбе; иначе 6-DOF Гаусс–Ньютон со стартом
    ψ по круговой медиане азимутов пар (мульти-старт ±60°), лучший по σ.
    """
    uv = np.asarray(uv, float)
    P = np.asarray(P, float)
    if len(uv) < (3 if center is not None else 4):
        return None, None

    def res(T):
        p2, r, _ = project_pano(P, T, w, h)
        d = p2 - uv
        d[:, 0] = (d[:, 0] + w / 2) % w - w / 2  # шов: невязка через край кадра
        d[:, 1] = np.clip(d[:, 1], -h / 4, h / 4)
        d[r <= EPS_Z_PANO] = 1e3
        return d.ravel()

    if center is not None:
        T0 = _wahba_pano(uv, P, center, w, h)
        T = _refine_res(res, T0)
        return T, sigma_pano(uv, P, T, w, h)
    # старт: центр — центроид пар, ψ — круговая медиана (aw = ψ − lon)
    C0 = P.mean(axis=0)
    lon = (uv[:, 0] / w - 0.5) * 2.0 * math.pi
    aw = np.arctan2(P[:, 1] - C0[1], P[:, 0] - C0[0])
    dpsi = aw + lon
    psi0 = math.atan2(float(np.median(np.sin(dpsi))), float(np.median(np.cos(dpsi))))
    best = None
    for d in (0.0, math.radians(60.0), math.radians(-60.0)):
        T = _refine_res(res, T_of(_pano_yaw_R(psi0 + d), C0))
        sg = sigma_pano(uv, P, T, w, h)
        if best is None or sg < best[1]:
            best = (T, sg)
    return best


def z_advice_pano():
    return ('не сходится: пары по разным стенам (не в один ряд), подальше от '
            'полюсов панорамы; попробуйте «камера у сканера»')


def estimate_focal(uv, P, w, h, center=None, hint=None):
    """Автоподбор эквивалента 35 мм по репроекции: сетка + локальное уточнение.

    Одна копланарная поверхность фокусное не определяет (гомография вбирает
    ошибку) — требуется ≥4 некомпланарных пары. Возвращает (f35, σ) или None.
    """
    uv = np.asarray(uv, float)
    P = np.asarray(P, float)
    best = None
    for f35 in FOCAL_GRID:
        T, sg = solve(uv, P, f35, w, h, center=center, hint=hint)
        if T is not None and (best is None or sg < best[1]):
            best = (f35, sg)
    if best is None:
        return None
    f35, _ = best
    step = 1.0
    for _ in range(12):                          # локальный спуск по сетке 1 мм
        improved = False
        for df in (-step, step):
            cand = f35 + df
            if cand < 8 or cand > 120:
                continue
            T, sg = solve(uv, P, cand, w, h, center=center, hint=hint)
            if T is not None and sg < best[1] - 1e-9:
                best, f35, improved = (cand, sg), cand, True
                break
        if not improved:
            step *= 0.5
            if step < 0.05:
                break
    return best


# ---------------------------------------------------------------------------
# гомография и поверхности
# ---------------------------------------------------------------------------

def _collinear3(pts):
    """Хотя бы одна тройка точек вырожденно коллинеарна (для 4 углов)."""
    import itertools
    for i, j, k in itertools.combinations(range(len(pts)), 3):
        a, b, c = pts[i], pts[j], pts[k]
        area = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        if area < 1e-9 * (np.ptp(pts[:, 0]) + np.ptp(pts[:, 1]) + 1e-9):
            return True
    return False


def dlt_h(src2d, dst2d):
    """Гомография 3×3 по ≥4 парам 2D (DLT, нормировка Харли). None при вырождении."""
    src = np.asarray(src2d, float)
    dst = np.asarray(dst2d, float)
    if len(src) < 4 or len(src) != len(dst):
        return None
    if len(src) == 4 and (_collinear3(src) or _collinear3(dst)):
        return None                                 # 3 угла на одной прямой

    def _norm(p):
        c = p.mean(axis=0)
        d = np.sqrt(np.mean(np.sum((p - c) ** 2, axis=1))) or 1.0
        s = math.sqrt(2) / d
        T = np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1]])
        return (np.column_stack([p, np.ones(len(p))]) @ T.T)[:, :2], T

    sn, Ts = _norm(src)
    dn, Td = _norm(dst)
    A = np.zeros((2 * len(sn), 9))
    for i, ((x, y), (X, Y)) in enumerate(zip(sn, dn)):
        A[2 * i] = [x, y, 1, 0, 0, 0, -X * x, -X * y, -X]
        A[2 * i + 1] = [0, 0, 0, x, y, 1, -Y * x, -Y * y, -Y]
    _, _, Vt = np.linalg.svd(A)
    Hn = Vt[-1].reshape(3, 3)
    H = np.linalg.inv(Td) @ Hn @ Ts
    scale = max(np.ptp(dst[:, 0]), np.ptp(dst[:, 1]), 1.0)
    err = float(np.mean(np.hypot(*(apply_h(H, src) - dst).T)))
    if err > 0.05 * scale:                      # вырожденные соответствия
        return None
    if abs(H[2, 2]) > 1e-12:
        H = H / H[2, 2]
    return H


def apply_h(H, pts):
    """N×2 точки через гомографию → N×2."""
    p = np.column_stack([np.asarray(pts, float), np.ones(len(pts))])
    q = p @ H.T
    return q[:, :2] / q[:, 2:3]


def plane_pose_from_h(H, origin, u_ax, v_ax, n, f35, w, h):
    """Гомография «локальные координаты плоскости → пиксели» → 2 кандидата позы.

    origin/u_ax/v_ax/n — в системе, в которой нужна поза. n не участвует в
    разложении (гомография задаёт базис с точностью до стороны); caller
    выбирает кандидата по видимым инлайнерам/стороне.
    """
    f = focal_px(f35, w, h)
    K = np.array([[f, 0, (w - 1) / 2], [0, f, (h - 1) / 2], [0, 0, 1]])
    Hn = np.linalg.inv(K) @ H
    return _decompose_hn(Hn, origin, u_ax, v_ax)


H_PERMS = ([0, 1, 2, 3], [1, 2, 3, 0], [2, 3, 0, 1], [3, 0, 1, 2],
           [3, 2, 1, 0], [2, 1, 0, 3], [1, 0, 3, 2], [0, 3, 2, 1])


def poly_area_signed(pts):
    """Знаковая площадь полигона (шнурование): >0 — обход против часовой в осях x,y."""
    a = np.asarray(pts, float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))


def orientation_ok(T, local4_world, corners_px, f35, w, h):
    """Поза не зеркалит поверхность: обход её четырёх углов сохраняет направление
    обхода ручек (в зеркальном решении направление противоположно — пользователь
    видит «отражённый фасад»)."""
    uv, _, ok = project(np.asarray(local4_world, float), T, f35, w, h)
    if not ok.all():
        return False
    a_proj = poly_area_signed(uv)
    a_corn = poly_area_signed(np.asarray(corners_px, float))
    if abs(a_corn) < 1e-6:
        return True
    return (a_proj > 0) == (a_corn > 0)


def pick_plane_pose(cands, inliers, f35, w, h, quad_px=None, ref_uv=None, tol=12.0,
                    local4_world=None):
    """Кандидата позы из plane_poses_from_rect выбираем по инлайнерам поверхности.

    Углы все кандидаты проектируют на рамку ручек ТОЧНО (гомография по ним и
    построена) — различает остальное:
    - ориентация: зеркальные решения (обход углов поверхности против обхода
      ручек) отбрасываются, если заданы quad_px и local4_world;
    - ref_uv (пиксели инлайнеров через ВЫРОВНЕННЫЙ пользователем слой) —
      побеждает поза, воспроизводящая этот слой (основной критерий);
    - иначе: доля инлайнеров внутри рамки ручек ≥ 0.9 И **максимальная площадь**
      их проекции (без площади все кандидаты «внутри» равны, и выигрывает
      сжатая поза — проверено на реальном фото);
    - тай-брейк — число инлайнеров в кадре.
    """
    best, best_key = None, None
    quad = None if quad_px is None else np.asarray(quad_px, float)
    for T in cands:
        if quad is not None and local4_world is not None and \
                not orientation_ok(T, local4_world, quad, f35, w, h):
            continue
        uv, _, ok = project(inliers, T, f35, w, h)
        in_frame = int(ok.sum())
        if in_frame < 4:
            continue
        if ref_uv is not None:
            near = ok & (np.hypot(*(uv - np.asarray(ref_uv, float)).T) < tol)
            key = (float(near.mean()), in_frame)
        elif quad is not None:
            pts = uv[ok]
            inside = float(point_in_poly(quad, pts).mean())
            area = poly_area(convex_hull_2d(pts)) if len(pts) >= 3 else 0.0
            key = (inside >= 0.9, area * inside, in_frame)
        else:
            key = (in_frame, )
        if best_key is None or key > best_key:
            best, best_key = T, key
    return best


def plane_poses_from_rect(local4, px4, origin, u_ax, v_ax, f35, w, h):
    """Позы из 4 углов с перебором 8 симметрийных порядков соответствий.

    Порядок, которым пользователь расставил анонимные ручки, не обязан быть
    верным — перебираем симметрии и отдаём всех кандидатов (выбор за caller
    по числу видимых точек поверхности).
    """
    out = []
    for perm in H_PERMS:
        H = dlt_h(np.asarray(local4, float)[perm], px4)
        if H is None:
            continue
        out += plane_pose_from_h(H, origin, u_ax, v_ax, None, f35, w, h)
    return out


def rect8(hw, hh):
    """8 опорных точек прямоугольника (±hw/±hh): углы в чётных строках,
    середины рёбер в нечётных; порядок обхода TL, T-mid, TR, R-mid, BR, B-mid, BL, L-mid.
    """
    cs = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    rows = []
    for i in range(4):
        x1, y1 = cs[i]
        x2, y2 = cs[(i + 1) % 4]
        rows.append([x1, y1])
        rows.append([(x1 + x2) / 2, (y1 + y2) / 2])
    return np.asarray(rows, float)


def rect8_corners(r8):
    """Углы (4×2) из 8-точечного прямоугольника."""
    return np.asarray(r8, float)[::2]


def rect8_perm(local4):
    """8-точечный прямоугольник из 4 углов (порядок обхода фиксирован)."""
    return rect8(*_span_xy(local4))


def rect8_from_uv(lo, hi):
    """8 ручек прямоугольника пикселей lo..hi (углы + середины, порядок rect8)."""
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    hw, hh = (hi[0] - lo[0]) / 2, (hi[1] - lo[1]) / 2
    return rect8(hw, hh) + np.array([cx, cy])


def rect8_default(w, h, margin=0.1):
    """Стартовые ручки без позы: 10–90 % кадра (слой сразу виден на фасаде)."""
    w, h = float(w), float(h)
    return rect8_from_uv((w * margin, h * margin), (w * (1 - margin), h * (1 - margin)))


def rect4_local(hull):
    """4 угла охвата оболочки в её локальных координатах (порядок обхода rect8)."""
    hw = float(np.max(np.abs(np.asarray(hull, float)[:, 0])) or 1e-6)
    hh = float(np.max(np.abs(np.asarray(hull, float)[:, 1])) or 1e-6)
    return rect8(hw, hh)[::2]


def _span_xy(local4):
    a = np.asarray(local4, float)
    return float(np.ptp(a[:, 0])), float(np.ptp(a[:, 1]))


def convex_hull_2d(P):
    """Выпуклая оболочка N×2 (монотонная цепь Эндрю), обход против часовой."""
    P = np.asarray(P, float)
    P = P[np.lexsort((P[:, 1], P[:, 0]))]
    P = np.unique(P, axis=0)
    if len(P) < 3:
        return P

    def half(pts):
        out = []
        for p in pts:
            while len(out) >= 2:
                a, b = out[-2], out[-1]
                if (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) <= 1e-12:
                    out.pop()
                else:
                    break
            out.append(p)
        return out

    lower = half(P)
    upper = half(P[::-1])
    return np.asarray(lower[:-1] + upper[:-1])


def poly_area(pts):
    """Площадь замкнутого полигона (шнурование)."""
    a = np.asarray(pts, float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1))))


def point_in_poly(poly, pts):
    """N×2 точки внутри полигона M×2 (even-odd, векторно)."""
    poly = np.asarray(poly, float)
    pts = np.asarray(pts, float)
    x, y = pts[:, 0], pts[:, 1]
    inside = np.zeros(len(pts), bool)
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        cond = (y1 > y) != (y2 > y)
        with np.errstate(divide='ignore', invalid='ignore'):
            xint = (x2 - x1) * (y - y1) / (y2 - y1) + x1
        inside ^= cond & (x < xint)
    return inside


def plane_mask(hull_px, w, h):
    """Булева маска (h×w) пикселей внутри полигона (в координатах пикселей)."""
    if HAS_CV2:
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [np.asarray(hull_px, np.float32).round().astype(np.int32)], 1)
        return m.astype(bool)
    xs = np.arange(w)
    ys = np.arange(h)
    grid = np.column_stack([np.tile(xs, h), np.repeat(ys, w)])
    return point_in_poly(hull_px, grid).reshape(h, w)


def resample_loop(pts, n=60):
    """n точек, равномерно по длине замкнутого контура."""
    a = np.asarray(pts, float)
    if len(a) < 2:
        return a
    d = np.hypot(*np.diff(np.vstack([a, a[:1]]), axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(d)])
    total = cum[-1]
    if total < 1e-9:
        return np.repeat(a[:1], n, axis=0)
    t = np.linspace(0, total, n, endpoint=False)
    out = np.empty((n, 2))
    j = 0
    for i, ti in enumerate(t):
        while j < len(d) - 1 and cum[j + 1] < ti:
            j += 1
        k = (ti - cum[j]) / max(d[j], 1e-12)
        out[i] = a[j] + k * (a[(j + 1) % len(a)] - a[j])
    return out


# ---------------------------------------------------------------------------
# края фото и уточнение позы
# ---------------------------------------------------------------------------

def photo_edges(img):
    """Маска краёв (Canny 60/150 по блюренному серому; без cv2 — соболь)."""
    g = np.asarray(img, float).mean(axis=2)
    if HAS_CV2:
        return cv2.Canny(cv2.GaussianBlur(g.astype(np.uint8), (5, 5), 1.4), 60, 150) > 0
    kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], float)
    ky = kx.T
    def conv(m, k):
        p = np.pad(m, 1)
        return sum(k[i, j] * p[i:i + m.shape[0], j:j + m.shape[1]]
                   for i in range(3) for j in range(3))
    gx, gy = conv(g, kx), conv(g, ky)
    mag = np.hypot(gx, gy)
    thr = np.percentile(mag, 97)
    return mag >= max(thr, 1.0)


def dist_transform(mask):
    """Точная L1-дистанция (в пикселях) до ближайшей True, float (h×w).

    Двухпроходный алгоритм (вперёд/назад), сепарабельные проходы через
    np.minimum.accumulate — без scipy.
    """
    m = np.asarray(mask, bool)
    INF = np.float32(1e9)
    d = np.where(m, 0.0, INF).astype(np.float32)
    h, w = d.shape
    ar_w = np.arange(w, dtype=np.float32)
    ar_h = np.arange(h, dtype=np.float32)

    def row_pass(r):
        return np.minimum.accumulate(r - ar_w) + ar_w

    def row_pass_rev(r):
        return np.minimum.accumulate((r + ar_w)[::-1])[::-1] - ar_w

    for i in range(h):                          # вперёд: сверху и слева
        if i:
            d[i] = np.minimum(d[i], d[i - 1] + 1)
        d[i] = row_pass(d[i])
    for i in range(h - 1, -1, -1):              # назад: снизу и справа
        if i < h - 1:
            d[i] = np.minimum(d[i], d[i + 1] + 1)
        d[i] = row_pass_rev(d[i])
    return d


def _dt_at(dt, u, v):
    """Билинейная выборка DT в дробных пиксельных координатах (для якобиана)."""
    h, w = dt.shape
    u = np.clip(np.asarray(u, float), 0, w - 1.001)
    v = np.clip(np.asarray(v, float), 0, h - 1.001)
    ui = u.astype(int)
    vi = v.astype(int)
    fu, fv = u - ui, v - vi
    d = dt.astype(np.float64)
    return ((1 - fu) * (1 - fv) * d[vi, ui] + fu * (1 - fv) * d[vi, ui + 1]
            + (1 - fu) * fv * d[vi + 1, ui] + fu * fv * d[vi + 1, ui + 1])


def refine_pose(T0, P, dt, f35, w, h, iters=30):
    """Уточнение позы по дистанционной карте краёв (Гаусс–Ньютон).

    Невязка точки — dt в её пикселе, ОБРЕЗАНА DT_CLIP (вне покрытия фото позу
    не тянет); носители сигнала фиксируются на итерации. Инкрементальный
    поворот (см. _refine). Возвращает (T, средняя невязка, n_used).
    """

    def support(T):
        uv, _, ok = project(P, T, f35, w, h)
        u = np.clip(uv[:, 0].astype(int), 0, dt.shape[1] - 1)
        v = np.clip(uv[:, 1].astype(int), 0, dt.shape[0] - 1)
        use = ok & (dt[v, u] < DT_CLIP)         # носители сигнала
        return np.where(use)[0]

    def res_idx(T, idx):
        uv, _, ok = project(P[idx], T, f35, w, h)
        val = np.minimum(_dt_at(dt, uv[:, 0], uv[:, 1]), DT_CLIP)
        return np.where(ok, val, DT_CLIP)

    R, C = T0[:3, :3].copy(), T0[:3, 3].copy()
    T = T_of(R, C)
    idx = support(T)
    best = (float(res_idx(T, idx).mean()) if len(idx) else 1e9, T, len(idx))
    hC, hR = 1e-5, 1e-7
    for _ in range(iters):
        if len(idx) < 6:
            break
        r0 = res_idx(T, idx)
        J = np.empty((len(idx), 6))
        for i in range(3):
            J[:, i] = (res_idx(T_of(R, C + hC * np.eye(3)[i]), idx) - r0) / hC
        for i in range(3):
            J[:, 3 + i] = (res_idx(T_of(rotvec_R(hR * np.eye(3)[i]) @ R, C), idx) - r0) / hR
        lam = 1e-8 * max(1.0, float(np.mean(np.abs(J))))
        Hm = J.T @ J + lam * np.eye(6)
        try:
            dx = np.linalg.solve(Hm, -J.T @ r0)
        except np.linalg.LinAlgError:
            break
        moved = False
        k = 1.0
        for _ in range(5):
            Tn = T_of(rotvec_R(k * dx[3:6]) @ R, C + k * dx[:3])
            rn = res_idx(Tn, idx)
            if float(rn.mean()) <= best[0]:
                R, C, T = rotvec_R(k * dx[3:6]) @ R, C + k * dx[:3], Tn
                nidx = support(T)               # носители обновляем после принятого шага
                best = (float(res_idx(T, nidx).mean()), Tn, len(nidx))
                idx = nidx
                moved = True
                break
            k *= 0.5
        if not moved:
            break
    return best[1], best[0], best[2]


# ---------------------------------------------------------------------------
# окраска
# ---------------------------------------------------------------------------

def colorize(P, entries):
    """Цвет точек P (N×3, якорная система) по привязанным фото.

    entries: [{'img': H×W×3 uint8, 'T': поза, 'f35': float,
               'surfaces': [{'mask': bool(h,w), 'origin': 3, 'n': 3, 'sigma': м}]}];
    панорама 360° — {'img', 'T', 'proj': 'pano360'} (без f35 и поверхностей).
    Пиксель внутри маски поверхности красит только точки своей плоскости ±3σ;
    z-буфер по пикселям (за стенами не красит); из нескольких фото точку красит
    ближайшая камера. Возвращает (rgb N×3 uint8, painted N bool).
    """
    P = np.asarray(P, float)
    N = len(P)
    rgb = np.zeros((N, 3), np.uint8)
    painted = np.zeros(N, bool)
    best_d = np.full(N, np.inf)
    for e in entries:
        img = e['img']
        h, w = img.shape[:2]
        T = e['T']
        if e.get('proj') == 'pano360':           # панорама 360°: весь мир «в кадре»
            uv, z, ok = project_pano(P, T, w, h)
            cand = ok.copy()
            surfaces = []
        else:
            uv, z, ok = project(P, T, e['f35'], w, h)
            cand = ok.copy()
            surfaces = e.get('surfaces') or []
        u = np.clip(uv[:, 0].astype(int), 0, w - 1)
        v = np.clip(uv[:, 1].astype(int), 0, h - 1)
        if surfaces:
            owner = np.zeros((h, w), np.int32)   # битовая маска: каким плоскостям принадлежит пиксель
            for si, s in enumerate(surfaces):
                owner[s['mask']] |= 1 << si
            bits = np.where(cand, owner[v, u], 0)
            allowed = bits == 0                  # свободный пиксель — красит любая точка
            for si, s in enumerate(surfaces):
                n = np.asarray(s['n'], float)
                r = np.abs((P - np.asarray(s['origin'], float)) @ n)
                allowed |= ((bits >> si) & 1).astype(bool) & (r <= 3.0 * max(s['sigma'], 1e-4))
            cand &= allowed
        if not cand.any():
            continue
        idx = np.where(cand)[0]
        pix = v[idx] * w + u[idx]
        order = np.lexsort((z[idx], pix))
        pp = pix[order]
        first = np.concatenate([[True], pp[1:] != pp[:-1]])
        win = idx[order][first]                  # победитель пикселя — ближайший вдоль луча
        d = np.linalg.norm(P[win] - T[:3, 3], axis=1)
        upd = d < best_d[win]
        wsel = win[upd]
        rgb[wsel] = img[v[wsel], u[wsel]]
        painted[wsel] = True
        best_d[wsel] = d[upd]
    return rgb, painted


KEY_BASE = np.array([1 << 42, 1 << 21, 1], np.int64)


def _vkeys(P, voxel):
    q = np.floor(np.asarray(P, float) / voxel).astype(np.int64)
    return q @ KEY_BASE


def join_colors(P_dst, P_src, rgb_src, painted_src, voxel=0.05):
    """Перенос цвета с прореженных точек на полное разрешение по воксельным ключам.

    Переносится и валидность: непокрашенные src-точки ничего не дают
    («воксель существует» ≠ «значение осмысленно»).
    """
    src = np.asarray(painted_src, bool)
    rgb_dst = np.zeros((len(P_dst), 3), np.uint8)
    painted = np.zeros(len(P_dst), bool)
    if not src.any():
        return rgb_dst, painted
    keys = _vkeys(P_src[src], voxel)
    uk, first = np.unique(keys, return_index=True)
    kd = _vkeys(P_dst, voxel)
    pos = np.searchsorted(uk, kd)
    pos = np.clip(pos, 0, len(uk) - 1)
    hit = uk[pos] == kd
    colors = rgb_src[src][first]
    rgb_dst[hit] = colors[pos[hit]]
    painted |= hit
    return rgb_dst, painted


def fingerprint(items):
    """Ключ кеша окраски: [(id, abspath, mtime, size, pose(округл. до 6), f35, visible, hs_json)]."""
    parts = []
    for it in items:
        pose = it[4]
        parts += [it[0], str(it[1]), round(it[2] or 0, 3), int(it[3] or 0),
                  None if pose is None else np.round(np.asarray(pose, float), 6).tobytes(),
                  None if it[5] is None else round(float(it[5]), 3),
                  bool(it[6]), json.dumps(it[7] or {}, sort_keys=True)]
    return hash(tuple(parts))


# ---------------------------------------------------------------------------
# проёмы поверхности (дыры в инлайнерах) — для точного уточнения позы
# ---------------------------------------------------------------------------

def occupancy_holes(loc, cell=None, min_cells=8, max_frac=0.5):
    """Проёмы поверхности: дыры в маске занятости инлайнеров.

    loc — N×2 локальные координаты точек на плоскости (м). Сетка занятости;
    свободные ячейки, достижимые с границы, — «снаружи»; замкнутые пустоты
    внутри — проёмы (окна, двери). Возвращает список M×2 локальных координат
    точек на границах проёмов (центры граничных ячеек) — порядок не важен,
    это опорные точки для уточнения по краям фото.
    """
    from collections import deque
    a = np.asarray(loc, float)
    if len(a) < 20:
        return []
    lo = a.min(axis=0)
    hi = a.max(axis=0)
    ext = np.maximum(hi - lo, 1e-6)
    if cell is None:
        cell = max(0.08, float(ext.max()) / 170.0)
    nx = int(np.ceil(ext[0] / cell)) + 3
    ny = int(np.ceil(ext[1] / cell)) + 3
    occ = np.zeros((ny, nx), bool)
    ij = np.floor((a - lo) / cell).astype(int) + 1
    ok = (ij[:, 0] >= 0) & (ij[:, 0] < nx) & (ij[:, 1] >= 0) & (ij[:, 1] < ny)
    occ[ij[ok, 1], ij[ok, 0]] = True
    # закрыть щели между проходами сканера (иначе проёмы «соединяются» снаружи):
    # дилатация занятости на 1 ячейку по 8 соседям
    d1 = occ.copy()
    d1[1:, :] |= occ[:-1, :]
    d1[:-1, :] |= occ[1:, :]
    d1[:, 1:] |= occ[:, :-1]
    d1[:, :-1] |= occ[:, 1:]
    d2 = d1.copy()
    d2[1:, 1:] |= d1[:-1, :-1]
    d2[1:, :-1] |= d1[:-1, 1:]
    d2[:-1, 1:] |= d1[1:, :-1]
    d2[:-1, :-1] |= d1[1:, 1:]
    occ = d2
    free = ~occ
    # «снаружи» — свободное, достижимое от рамки сетки
    out = np.zeros_like(free)
    dq = deque()
    for x in range(nx):
        for y in (0, ny - 1):
            if free[y, x] and not out[y, x]:
                out[y, x] = True
                dq.append((y, x))
    for y in range(ny):
        for x in (0, nx - 1):
            if free[y, x] and not out[y, x]:
                out[y, x] = True
                dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            yy, xx = y + dy, x + dx
            if 0 <= yy < ny and 0 <= xx < nx and free[yy, xx] and not out[yy, xx]:
                out[yy, xx] = True
                dq.append((yy, xx))
    holes = free & ~out
    if not holes.any():
        return []
    # компоненты дыр
    lab = np.zeros_like(holes, np.int32)
    comps = []
    cur = 0
    ys, xs = np.nonzero(holes)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if lab[y0, x0]:
            continue
        cur += 1
        lab[y0, x0] = cur
        dq = deque([(y0, x0)])
        cells = []
        while dq:
            y, x = dq.popleft()
            cells.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                yy, xx = y + dy, x + dx
                if 0 <= yy < ny and 0 <= xx < nx and holes[yy, xx] and not lab[yy, xx]:
                    lab[yy, xx] = cur
                    dq.append((yy, xx))
        comps.append(cells)
    area = float(occ.size)
    pts = []
    for cells in comps:
        if len(cells) < min_cells or len(cells) > max_frac * area:
            continue                                   # шум / «половина стены» — не проём
        for y, x in cells:
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                yy, xx = y + dy, x + dx
                if 0 <= yy < ny and 0 <= xx < nx and occ[yy, xx]:
                    pts.append((lo[0] + (x - 1 + 0.5) * cell,
                                lo[1] + (y - 1 + 0.5) * cell))
                    break
    return pts


def holes_zones(hole_pts_px, margin=10.0):
    """Зоны внимания для уточнения: bbox каждого проёма в пикселях + margin."""
    out = []
    a = np.asarray(hole_pts_px, float)
    if len(a) < 3:
        return out
    # группировка по близости (проёмы разделены занятыми ячейками)
    used = np.zeros(len(a), bool)
    for i in range(len(a)):
        if used[i]:
            continue
        grp = [i]
        used[i] = True
        changed = True
        while changed:
            changed = False
            for j in range(len(a)):
                if used[j]:
                    continue
                if np.min(np.hypot(*(a[grp] - a[j]).T)) < 40.0:
                    used[j] = True
                    grp.append(j)
                    changed = True
        g = a[grp]
        out.append((g[:, 0].min() - margin, g[:, 1].min() - margin,
                    g[:, 0].max() + margin, g[:, 1].max() + margin))
    return out


def snap_point(gray, u, v, radius=18):
    """Уточнить клик по фото до угла (суб-пиксель): хорошие углы в радиусе → ближайший.

    Для точек пар: щелчок по углу окна получает точность долей пикселя.
    Без cv2 — возвращает исходную точку.
    """
    if not HAS_CV2 or gray is None:
        return float(u), float(v)
    g = np.ascontiguousarray(gray)
    if g.dtype != np.uint8:
        g = np.clip(g, 0, 255).astype(np.uint8)
    h, w = g.shape[:2]
    x0, y0 = int(max(0, u - radius)), int(max(0, v - radius))
    x1, y1 = int(min(w, u + radius + 1)), int(min(h, v + radius + 1))
    if x1 - x0 < 5 or y1 - y0 < 5:
        return float(u), float(v)
    roi = g[y0:y1, x0:x1]
    pts = cv2.goodFeaturesToTrack(roi, maxCorners=12, qualityLevel=0.05,
                                  minDistance=5, blockSize=5)
    if pts is None:
        return float(u), float(v)
    pts = np.asarray(pts, np.float32).reshape(-1, 2)
    d = np.hypot(pts[:, 0] + x0 - u, pts[:, 1] + y0 - v)
    i = int(np.argmin(d))
    if d[i] > radius:
        return float(u), float(v)
    try:                                          # суб-пиксель
        p2 = cv2.cornerSubPix(g, pts[i:i + 1].reshape(-1, 1, 2), (5, 5), (-1, -1),
                              (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.03))
        return float(p2[0, 0, 0] + x0), float(p2[0, 0, 1] + y0)
    except Exception:                             # noqa: BLE001
        return float(pts[i, 0] + x0), float(pts[i, 1] + y0)
