#!/usr/bin/env python3
"""
Чертёж сечения: полоса точек у плоскости сечения → контуры (marching squares)
→ линии и дуги → SVG / DXF (ezdxf). Точечный путь: облако, сетка не нужна.

Вход — состояние сечения (section.py, общая система, режим «срез») и размещённые
сканы сеанса (точки с чисткой). Лист («sheet») — dict:
  chains   — [(замкнутость, точки Nx2 в метрах), …] — контуры в системе
             плоскости (u — вправо, v — вверх на бумаге);
  pieces   — [[('line', p0, p1) | ('arc', p0, p1, центр, по_часовой), …], …]
             — то же, упрощённое до линий и дуг;
  openings — [{'kind', 'label', 'at', 'dir', 'width'}, …] — проёмы, которые
             пересекает плоскость среза;
  bounds   — (lo, hi) — габарит контуров, м;  n_points — точек в полосе;
  title, date, stats.

Координаты листа в метрах; экспорт пишет миллиметры (1:1), v — вверх на бумаге
(SVG/DXF получают y = −v).
"""

import datetime
from collections import deque

import numpy as np

import section as sec

DEFAULT_TOL = 0.03          # м — ячейка сетки занятости (допуск чертежа)
DEFAULT_THICK = None        # толщина полосы — из состояния сечения
MIN_CHAIN = 0.3             # м — короче контур выбрасывается
GRID_STEP = 1.0             # м — сетка листа
ARC_MAX_R = 100.0           # м — «дуга» большего радиуса — это линия
ARC_MIN_TURN = 10.0         # ° — меньший поворот не стоит дуги


# ── система плоскости ───────────────────────────────────────────────────────

def plane_frame(n):
    """
    Оси листа по нормали плоскости: v — вверх (Z), насколько возможно;
    u — горизонталь. Горизонтальной плоскости (план) — всегда стандартная
    ориентация карты: X вправо, Y вверх; от знака нормали вид не меняется
    (иначе лист открывается зеркальным или повёрнутым — «вид снизу»).
    → (u, v).
    """
    n = np.asarray(n, float)
    n = n / np.linalg.norm(n)
    if abs(n[2]) > 0.9:                                    # план: вид сверху
        return np.array([1.0, 0, 0]), np.array([0, 1.0, 0])
    v = np.array([0, 0, 1.0]) - (np.array([0, 0, 1.0]) @ n) * n
    v /= np.linalg.norm(v)
    u = np.cross(v, n)                                     # (u, v, n) правые — вид с +n
    return u / np.linalg.norm(u), v


def band_points(session, st):
    """Точки размещённых видимых сканов в полосе среза (общая система);
    включённые боксы отсечения (clip.py) AND-ятся поверх — чертёж комнаты.
    sc.down запускает ленивый анализ сам — ждать «проанализировано» не нужно."""
    import clip
    out = []
    for sc in session.placed():
        if not sc.visible:
            continue
        T = session.Tc(sc)
        P = sc.down @ T[:3, :3].T + T[:3, 3]
        P = P[clip.combined_mask(P, session, section_keep=sec.keep_mask(P, st))]
        if len(P):
            out.append(P)
    return np.vstack(out) if out else np.zeros((0, 3))


# ── сетка занятости и контуры ───────────────────────────────────────────────

def _occupancy(uv, cell):
    """Сетка занятости точек. → (маска, lo)."""
    lo = uv.min(axis=0) - cell
    hi = uv.max(axis=0) + cell
    W = int(np.ceil((hi[0] - lo[0]) / cell)) + 2
    H = int(np.ceil((hi[1] - lo[1]) / cell)) + 2
    if W * H > 20_000_000:
        raise ValueError(f'полоса сечения слишком велика: {W}×{H} ячеек — '
                         'увеличьте допуск или уменьшите высоту среза')
    ij = np.floor((uv - lo) / cell).astype(int)
    ok = (ij[:, 0] >= 1) & (ij[:, 0] < W - 1) & (ij[:, 1] >= 1) & (ij[:, 1] < H - 1)
    mask = np.zeros((H, W), bool)
    mask[ij[ok, 1], ij[ok, 0]] = True
    return mask, lo


def _dilate(mask):
    """Расширение на одну ячейку по 4 направлениям — закрывает точечные дыры."""
    out = mask.copy()
    for ax in (0, 1):
        for sh in (-1, 1):
            out |= np.roll(mask, sh, axis=ax)
    return out


OFF8 = ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))


def _skeleton(mask):
    """
    Скелет бинарной маски (Zhang–Suen): полоса точек одной поверхности даёт
    одну осевую линию, а не контур «туда-обратно» по её краям.
    """
    m = mask.copy()

    def step(first):
        mark = []
        idx = np.argwhere(m)
        for i, j in idx:
            P = [m[i + di, j + dj] for di, dj in
                 ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))]
            B = sum(P)
            if not 2 <= B <= 6:
                continue
            seq = P + P[:1]
            A = sum(1 for a, b in zip(seq, seq[1:]) if not a and b)
            if A != 1:
                continue
            P2, P4, P6, P8 = P[0], P[2], P[4], P[6]
            if first:
                if P2 * P4 * P6 or P4 * P6 * P8:
                    continue
            elif P2 * P4 * P8 or P2 * P6 * P8:
                continue
            mark.append((i, j))
        for i, j in mark:
            m[i, j] = False
        return bool(mark)

    m = np.pad(m, 1)
    while step(True) or step(False):
        pass
    return m[1:-1, 1:-1]


def skeleton_paths(mask, cell, lo, min_len=MIN_CHAIN):
    """
    Осевые линии маски: скелет → граф (узлы — концы и развилки) → пути между
    узлами; замкнутые петли без узлов — целиком. → [(замкнутость, Nx2 в метрах)].
    """
    sk = _skeleton(mask)
    S = set(map(tuple, np.argwhere(sk)))
    if not S:
        return []

    def nbs(p):
        return [(p[0] + di, p[1] + dj) for di, dj in OFF8 if (p[0] + di, p[1] + dj) in S]

    deg = {p: len(nbs(p)) for p in S}
    nodes = {p for p in S if deg[p] != 2}
    out = []
    visited = set()

    def emit(path, closed):
        pts = np.array([[lo[0] + (c + 0.5) * cell, lo[1] + (r + 0.5) * cell]
                        for r, c in path], float)
        if closed:
            pts = np.vstack([pts, pts[:1]])
        if _length(pts) >= min_len:
            out.append((closed, pts))

    walked = set()                                        # направленные пары пикселей
    for nd in nodes:
        for nb in nbs(nd):
            if (nd, nb) in walked:
                continue
            path, prev, cur = [nd], nd, nb
            while True:
                walked.add((prev, cur))
                path.append(cur)
                if cur in nodes:
                    break
                nxt = [q for q in nbs(cur) if q != prev]
                if not nxt:
                    break                                 # тупик
                prev, cur = cur, nxt[0]
            visited.update(path)
            emit(path, False)
    for p in S:                                           # замкнутые петли без узлов
        if p in visited or deg.get(p) != 2:
            continue
        path, prev, cur = [], None, p
        while True:
            visited.add(cur)
            path.append(cur)
            nxt = [q for q in nbs(cur) if q != prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            if cur == p:
                break
        if len(path) > 2 and path[0] in nbs(path[-1]):
            emit(path, True)
    return out


def _length(pts):
    return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))


def _dedupe(chains, dist):
    """
    Убрать дубли: скелет полосы чётной ширины оставляет две параллельные линии
    (артефакт Чжана–Сюэна), отростки у развилок повторяют основную линию.
    Контур, у которого ≥ 80 % точек в пределах dist от более длинного, — дубль.
    """
    order = sorted(range(len(chains)), key=lambda i: -_length(chains[i][1]))
    keep = []
    for i in order:
        p = chains[i][1]
        dup = False
        for j in keep:
            q = chains[j][1]
            step = max(1, len(q) // 400)                  # подвыборка для скорости
            d2 = ((p[:, None, 0] - q[::step][None, :, 0]) ** 2 +
                  (p[:, None, 1] - q[::step][None, :, 1]) ** 2)
            near = d2.min(axis=1) < dist ** 2
            if near.mean() > 0.8:
                dup = True
                break
        if not dup:
            keep.append(i)
    return [chains[i] for i in sorted(keep)]


# ── упрощение: Дуглас–Пекер, склейка коллинеарных, дуги ────────────────────

def _rdp(pts, eps):
    """Дуглас–Пекер (итеративный)."""
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = pts[j] - pts[i]
        L = np.linalg.norm(seg)
        q = pts[i + 1:j] - pts[i]
        d = (seg[0] * q[:, 1] - seg[1] * q[:, 0]) / (L or 1.0)   # 2D «векторное»
        k = int(np.argmax(np.abs(d)))
        if abs(d[k]) > eps:
            k += i + 1
            keep[k] = True
            stack.append((i, k))
            stack.append((k, j))
    return pts[keep]


def _simplify(pts, eps):
    """Контур → вершины полилинии: РДП (для замкнутого — от двух крайних)."""
    if len(pts) < 4:
        return pts
    closed = np.allclose(pts[0], pts[-1])
    if not closed:
        return _rdp(pts, eps)
    q = pts[:-1]
    e = np.linalg.norm(q - q.mean(axis=0), axis=1)
    i, j = int(np.argmax(e)), int(np.argmin(e))
    if i > j:
        i, j = j, i
    a = _rdp(np.vstack([q[i:], q[:i + 1]]), eps)
    b = _rdp(np.vstack([q[j:], q[:j + 1]]), eps)
    return np.vstack([a[:-1], b])                         # без дублей стыка


def _circle3(p0, p1, p2):
    """Окружность по трём точкам. → (центр, радиус) | None."""
    ax, ay = p0
    bx, by = p1
    cx, cy = p2
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    c = np.array([ux, uy])
    return c, float(np.linalg.norm(p0 - c))


def _pieces(pts, eps):
    """Вершины полилинии → элементы линия/дуга: дуга — подряд идущие вершины,
    ложащиеся на окружность с RMS ≤ eps и поворачивающие в одну сторону."""
    closed = np.allclose(pts[0], pts[-1])
    ring = pts[:-1] if closed else pts
    n = len(ring)
    out, i = [], 0
    while i < n - 1:
        # пробуем дугу: от вершины i насколько можно
        best = None
        j = i + 2
        while j < min(i + 40, n + (1 if closed else 0)):
            # вершины i..j включительно (для замкнутого j может заходить за 0)
            idx = list(range(i, j + 1)) if j < n else list(range(i, n)) + list(range(0, j - n + 1))
            q = ring[idx]
            c3 = _circle3(*q[:3])
            if c3 is None:
                break
            c, r = c3
            if not (eps < r < ARC_MAX_R):
                break
            d = q - c
            ang = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
            turn = ang[-1] - ang[0]
            rms = float(np.sqrt(np.mean((np.linalg.norm(d, axis=1) - r) ** 2)))
            if rms > eps * 0.5 or abs(np.degrees(turn)) < ARC_MIN_TURN:
                break
            cross = d[:-1, 0] * d[1:, 1] - d[:-1, 1] * d[1:, 0]
            if np.sign(cross[0]) != np.sign(cross).all():
                break
            best = (j if j <= n else n, c, cross[0] < 0)
            j += 1
        if best is not None and best[0] - i >= 4:
            j, c, ccw = best
            out.append(('arc', ring[i], ring[j % n], c, ccw))
            i = j
        else:
            a, b = ring[i], ring[(i + 1) % n]
            prev = out[-1] if out else None
            # продлить предыдущую линию, только если b на её прямой
            # (иначе слияние срежет угол — L превратится в хорду)
            if prev is not None and prev[0] == 'line' and \
                    np.linalg.norm(prev[2] - a) <= 1e-9:
                d = prev[2] - prev[1]
                L = np.linalg.norm(d)
                off = abs(d[0] * (b[1] - prev[1][1]) - d[1] * (b[0] - prev[1][0])) / (L or 1.0)
                if off <= eps:
                    out[-1] = ('line', prev[1], b)
                    i += 1
                    continue
            out.append(('line', a, b))
            i += 1
    if out and closed:
        # замкнуть: слить последний элемент с первым, если оба линии
        if out[0][0] == 'line' and out[-1][0] == 'line' and \
                np.linalg.norm(out[-1][2] - out[0][1]) < 1e-9:
            out[0] = ('line', out[-1][1], out[0][2])
            out.pop()
    return out


# ── проёмы на листе ─────────────────────────────────────────────────────────

def openings_at(session, st, u_ax, v_ax, origin):
    """Проёмы, пересекаемые плоскостью среза: маркеры в системе листа.
    Дубликаты (один проём из двух сканов) убираются по близости."""
    n = sec.normal(st)
    c = float(st['c'])
    RU = {'door': 'Дверь', 'window': 'Окно'}
    out, placed = [], []
    for sc in session.placed():
        if not sc.visible:
            continue
        T = session.Tc(sc)
        for o in sc.openings:
            C = np.asarray(o.corners) @ T[:3, :3].T + T[:3, 3]
            d = C @ n - c
            if not (d.min() < 0.15 and d.max() > -0.15):
                continue
            w_dir = C[1] - C[0]
            L = np.linalg.norm(w_dir)
            if L < 1e-6:
                continue
            w_dir /= L
            mid = 0.5 * (C[0] + C[1])                      # середина нижней грани
            at = np.array([(mid - origin) @ u_ax, (mid - origin) @ v_ax])
            if any(np.linalg.norm(at - q) < 0.3 for q in placed):
                continue
            placed.append(at)
            d_uv = np.array([w_dir @ u_ax, w_dir @ v_ax])
            d_uv /= np.linalg.norm(d_uv) + 1e-12
            out.append({'kind': o.kind, 'width': float(o.width),
                        'label': f"{RU.get(o.kind, o.kind)} {o.width:.2f} м",
                        'at': at, 'dir': d_uv})
    return out


def _spacing(uv, sample=2000):
    """Медианный шаг точек в плоскости (по подвыборке) — для ячейки сетки."""
    idx = np.unique(np.linspace(0, len(uv) - 1, min(sample, len(uv))).astype(int))
    q = uv[idx]
    d = np.linalg.norm(q[:, None] - q[None], axis=2)
    np.fill_diagonal(d, np.inf)
    return float(np.median(d.min(axis=1)))


# ── сборка листа ────────────────────────────────────────────────────────────

def _sheet_from_uv(uv, tol, min_len, simplify, title, origin):
    """Общая часть: точки в координатах листа (u, v) → dict листа (без
    openings; n_points заполняет вызывающий). None, если контуров нет."""
    # ячейка — не мельче шага точек (даунсемпл анализа ~5 см), иначе маска дырявая
    cell = max(tol, 1.4 * _spacing(uv))
    mask, lo = _occupancy(uv, cell)
    mask = _dilate(mask)                                   # закрыть дыры дискретизации
    chains = _dedupe(skeleton_paths(mask, cell, lo, min_len), 1.6 * cell)
    if not chains:
        return None
    all_pts = np.vstack([pts for _, pts in chains])
    sheet = {'chains': [(cl, pts.copy()) for cl, pts in chains],
             'bounds': (all_pts.min(axis=0), all_pts.max(axis=0)),
             'n_points': int(len(uv)), 'tol': float(tol), 'cell': float(cell),
             'origin': np.asarray(origin, float).tolist(),
             'title': title,
             'date': datetime.date.today().strftime('%d.%m.%Y')}
    sheet['pieces'] = [_pieces(_simplify(pts, cell), cell) for _, pts in chains] \
        if simplify else [[('line', pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
                          for _, pts in chains]
    sheet['openings'] = []                                 # заполняет build() для среза
    lens = [float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1))) for _, pts in chains]
    sheet['stats'] = (len(chains), sum(lens), 0)
    return sheet


def build(session, st, tol=DEFAULT_TOL, min_len=MIN_CHAIN, simplify=True,
          with_openings=True):
    """
    Чертёж по сечению сеанса. st — состояние section.py (режим «срез»).
    → dict листа (см. заголовок модуля) или None, если точек в полосе нет.
    """
    if st is None or st.get('mode') != 'slice':
        raise ValueError('чертёж строится по срезу — включите режим «срез»')
    P = band_points(session, st)
    if len(P) < 100:
        return None
    n = sec.normal(st)
    u_ax, v_ax = plane_frame(n)
    mid = P.mean(axis=0)
    origin = mid - (mid @ n - st['c']) * n                 # точка плоскости
    uv = np.column_stack([(P - origin) @ u_ax, (P - origin) @ v_ax])
    sheet = _sheet_from_uv(uv, tol, min_len, simplify,
                           f'Сечение: {sec.describe(st, session.zero_z() if session.scans else 0.0)}',
                           origin)
    if sheet is None:
        return None
    sheet['openings'] = openings_at(session, st, u_ax, v_ax, origin) if with_openings else []
    sheet['stats'] = (sheet['stats'][0], sheet['stats'][1], len(sheet['openings']))
    return sheet


# ── проекция элементов на плоскость листа ──────────────────────────────────

PARALLEL_COS = 0.35   # плоскость с |n·n_листа| больше этого параллельна листу
                      # (проектируется пятном) — в чертёж не попадает


def projection_paper(session, paper='z', paper_el=None):
    """
    Плоскость листа для проекции. paper: 'z' — горизонтальная (план участка,
    проекция вдоль вертикали); 'elem' — плоскость элемента paper_el (фасад,
    развёртка). → (u_ax, v_ax, n, origin).
    """
    if paper == 'elem':
        if paper_el is None or paper_el['kind'] != 'plane':
            raise ValueError('плоскость листа «по элементу» нужна плоскость')
        # нормаль/центр элемента — в канон. системе его скана: переводим в общую
        # (иначе у повёрнутых сканов лист «не совпадает сам с собой» и всё
        # оказывается «параллельным листу» и пропускается)
        sc = session.by_id(paper_el.get('scan'))
        if sc is None or sc.pose is None:
            raise ValueError('скан элемента не размещён')
        T = session.Tc(sc)
        n_w = T[:3, :3] @ np.asarray(paper_el['params']['n'], float)
        origin = T[:3, :3] @ np.asarray(paper_el['center'], float) + T[:3, 3]
        n_w = n_w / np.linalg.norm(n_w)
        u_ax, v_ax = plane_frame(n_w)
        return u_ax, v_ax, n_w, origin
    u_ax, v_ax = np.array([1.0, 0, 0]), np.array([0, 1.0, 0])
    return u_ax, v_ax, np.array([0.0, 0, 1.0]), np.zeros(3)


def element_points(session, el, max_pts=400_000):
    """Точки элемента (инлайнеры |остаток| ≤ 3σ) в общей системе, с прореживанием
    до max_pts. → N×3 или None, если точек нет."""
    sc = session.by_id(el.get('scan'))
    if sc is None or sc.pose is None:
        return None
    idx = session.element_inliers(sc, el)
    if len(idx) < 30:
        return None
    P = sc.down[idx]
    if len(P) > max_pts:
        P = P[np.linspace(0, len(P) - 1, max_pts).astype(int)]
    T = session.Tc(sc)
    return P @ T[:3, :3].T + T[:3, 3]


def build_projection(session, elements, paper='z', paper_el=None,
                     tol=DEFAULT_TOL, min_len=MIN_CHAIN, simplify=True):
    """
    Чертёж проекцией: точки выбранных элементов переносятся на плоскость листа
    ортогонально (не срезом) — участок с уклоном одной плоскостью сечения не
    ловится, а проекция ловит: периметр забора и фасады здания на одном листе.
    Плоскости, параллельные листу (пол на плане, фасад на своей развёртке),
    проектируются пятном и пропускаются (их признаки — 'skipped').
    → dict листа | None.
    """
    u_ax, v_ax, n_paper, origin = projection_paper(session, paper, paper_el)
    pts, skipped, used_names = [], [], []
    for el in elements:
        name = el.get('name', el.get('id', '?'))
        if not session.element_visible(el):
            skipped.append(name)
            continue
        sc = session.by_id(el.get('scan'))
        if sc is None or sc.pose is None:
            skipped.append(name)
            continue
        T = session.Tc(sc)
        if el['kind'] == 'plane':
            n_el = np.asarray(el['params']['n'], float)
            n_w = T[:3, :3] @ n_el                          # нормаль в общей системе
            if abs(n_w @ n_paper) > PARALLEL_COS:
                skipped.append(name)
                continue
        elif el['kind'] not in ('circle', 'cylinder'):
            skipped.append(name)
            continue
        P = element_points(session, el)
        if P is None:
            skipped.append(name)
            continue
        pts.append(P)
        used_names.append(name)
    if not pts:
        return None
    P = np.vstack(pts)
    if len(P) < 100:
        return None
    uv = np.column_stack([(P - origin) @ u_ax, (P - origin) @ v_ax])
    what = 'план участка (проекция)' if paper == 'z' else 'проекция на элемент'
    sheet = _sheet_from_uv(uv, tol, min_len, simplify,
                           f'Проекция: {what} · элементов {len(used_names)}', origin)
    if sheet is None:
        return None
    sheet['skipped'] = skipped
    sheet['elements'] = used_names
    return sheet


# ── SVG ─────────────────────────────────────────────────────────────────────

_SVG_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}mm" height="{h:.0f}mm" '
             'viewBox="0 0 {W:.1f} {H:.1f}">\n')


def _fmt(v):
    return f'{v:.2f}'.rstrip('0').rstrip('.')


def svg_text(sheet, project='', margin=0.1, grid=True):
    """Лист → текст SVG. Единицы — миллиметры, v — вверх на бумаге (y = −v).
    Слои — группы: contour, openings, grid, text."""
    lo, hi = sheet['bounds']
    W, H = (hi[0] - lo[0]) + 2 * margin, (hi[1] - lo[1]) + 2 * margin

    def X(u):                                          # u → x листа (мм)
        return (u - lo[0] + margin) * 1000

    def Y(v):                                          # v → y листа, вверх
        return (hi[1] - v + margin) * 1000

    # стили — атрибутами на группах: QtSvg (PNG, предпросмотр) не применяет
    # CSS-классы из <style>; stroke без атрибута — «none», линии исчезнут
    s = [_SVG_HEAD.format(w=W * 1000, h=H * 1000, W=W * 1000, H=H * 1000),
         f'  <title>{_esc(sheet["title"])}</title>\n']
    if grid:
        s.append('  <g id="grid" stroke="#c9c9c9" stroke-width="0.25" fill="none">\n')
        u0, v0 = np.floor(lo / GRID_STEP) * GRID_STEP
        u = u0
        while u <= hi[0] + GRID_STEP:
            s.append(f'    <line x1="{_fmt(X(u))}" y1="{_fmt(Y(lo[1]))}" '
                     f'x2="{_fmt(X(u))}" y2="{_fmt(Y(hi[1]))}"/>\n')
            u += GRID_STEP
        v = v0
        while v <= hi[1] + GRID_STEP:
            s.append(f'    <line x1="{_fmt(X(lo[0]))}" y1="{_fmt(Y(v))}" '
                     f'x2="{_fmt(X(hi[0]))}" y2="{_fmt(Y(v))}"/>\n')
            v += GRID_STEP
        s.append('  </g>\n')
    s.append('  <g id="contour" stroke="#0047AB" stroke-width="0.5" fill="none" '
             'stroke-linecap="round">\n')
    for pieces in sheet['pieces']:
        for p in pieces:
            if p[0] == 'line':
                _, a, b = p
                s.append(f'    <line x1="{_fmt(X(a[0]))}" y1="{_fmt(Y(a[1]))}" '
                         f'x2="{_fmt(X(b[0]))}" y2="{_fmt(Y(b[1]))}"/>\n')
            else:
                _, a, b, c, ccw = p
                r = np.linalg.norm(a - c) * 1000
                la = np.arctan2(-(a[1] - c[1]), a[0] - c[0])
                lb = np.arctan2(-(b[1] - c[1]), b[0] - c[0])
                if ccw:
                    la, lb = lb, la
                d = (lb - la) % (2 * np.pi)              # угол дуги, всегда вперёд
                large = 1 if d > np.pi else 0
                s.append(f'    <path d="M {_fmt(X(a[0]))} {_fmt(Y(a[1]))} '
                         f'A {_fmt(r)} {_fmt(r)} 0 {large} 0 '
                         f'{_fmt(X(b[0]))} {_fmt(Y(b[1]))}"/>\n')
    s.append('  </g>\n')
    if sheet['openings']:
        s.append('  <g id="openings" stroke="#c0392b" stroke-width="0.5" '
                 'stroke-dasharray="4 2" fill="none">\n')
        for o in sheet['openings']:
            u, v = o['at']
            du, dv = o['dir'] * o['width'] / 2
            s.append(f'    <line x1="{_fmt(X(u - du))}" y1="{_fmt(Y(v - dv))}" '
                     f'x2="{_fmt(X(u + du))}" y2="{_fmt(Y(v + dv))}"/>\n')
        s.append('  </g>\n  <g id="text" fill="#111111" '
                 'font-family="Helvetica" font-size="14px">\n')
        for o in sheet['openings']:
            s.append(f'    <text x="{_fmt(X(o["at"][0]))}" y="{_fmt(Y(o["at"][1]) - 4)}" '
                     f'text-anchor="middle">{_esc(o["label"])}</text>\n')
        s.append('  </g>\n')
    n_ch, L, n_op = sheet['stats']
    head = _esc(project or 'проект')
    s.append('  <g id="stamp" fill="#555555" font-family="Helvetica" font-size="12px">\n'
             f'    <text x="{_fmt(W * 1000 - 8)}" y="{_fmt(H * 1000 - 26)}" text-anchor="end">'
             f'{head} · {sheet["date"]} · масштаб 1:1 (мм)</text>\n'
             f'    <text x="{_fmt(W * 1000 - 8)}" y="{_fmt(H * 1000 - 10)}" text-anchor="end">'
             f'контуров {n_ch} · длина {L:.1f} м · проёмов {n_op} · допуск {sheet["cell"] * 100:.0f} см</text>\n'
             '  </g>\n</svg>\n')
    return ''.join(s)


def _esc(t):
    return (str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def write_svg(path, sheet, project=''):
    """Лист → файл SVG (utf-8; путь через fileio.native_path — Windows)."""
    from pathlib import Path as _P
    from fileio import native_path
    with native_path(path, 'w') as p:
        _P(p).write_text(svg_text(sheet, project), encoding='utf-8')
    return path


# ── DXF (ezdxf) ─────────────────────────────────────────────────────────────

def write_dxf(path, sheet, project=''):
    """Лист → DXF (ezdxf, R2010), миллиметры, слои contour/grid/openings/text."""
    import ezdxf
    from fileio import native_path
    lo, hi = sheet['bounds']
    margin = 0.1

    def X(u):
        return (u - lo[0] + margin) * 1000

    def Y(v):
        return (hi[1] - v + margin) * 1000

    doc = ezdxf.new('R2010', setup=False)
    doc.header['$INSUNITS'] = 4                           # миллиметры
    doc.layers.add('contour', color=5)                    # ACI 5 — синий
    doc.layers.add('grid', color=8)
    doc.layers.add('openings', color=1)
    doc.layers.add('text', color=7)
    msp = doc.modelspace()
    u = np.floor(lo[0] / GRID_STEP) * GRID_STEP
    while u <= hi[0] + GRID_STEP:
        msp.add_line((X(u), Y(lo[1])), (X(u), Y(hi[1])), dxfattribs={'layer': 'grid'})
        u += GRID_STEP
    v = np.floor(lo[1] / GRID_STEP) * GRID_STEP
    while v <= hi[1] + GRID_STEP:
        msp.add_line((X(lo[0]), Y(v)), (X(hi[0]), Y(v)), dxfattribs={'layer': 'grid'})
        v += GRID_STEP
    for pieces in sheet['pieces']:
        for p in pieces:
            if p[0] == 'line':
                _, a, b = p
                msp.add_line((X(a[0]), Y(a[1])), (X(b[0]), Y(b[1])), dxfattribs={'layer': 'contour'})
            else:
                _, a, b, c, ccw = p
                r = float(np.linalg.norm(a - c) * 1000)
                la = np.degrees(np.arctan2(-(a[1] - c[1]), a[0] - c[0]))
                lb = np.degrees(np.arctan2(-(b[1] - c[1]), b[0] - c[0]))
                # ezdxf: дуга против часовой от start к end; y инвертирован —
                # направление поворота меняется
                if not ccw:
                    la, lb = lb, la
                msp.add_arc((X(c[0]), Y(c[1])), r, la, lb, dxfattribs={'layer': 'contour'})
    for o in sheet['openings']:
        u_, v_ = o['at']
        du, dv = o['dir'] * o['width'] / 2
        msp.add_line((X(u_ - du), Y(v_ - dv)), (X(u_ + du), Y(v_ + dv)),
                     dxfattribs={'layer': 'openings'})
        msp.add_text(o['label'], height=10,
                     dxfattribs={'layer': 'text'}).set_placement((X(u_), Y(v_) - 6))
    n_ch, L, n_op = sheet['stats']
    stamp = (f'{project or "проект"} · {sheet["date"]} · масштаб 1:1 (мм) · '
             f'контуров {n_ch} · длина {L:.1f} м · проёмов {n_op}')
    msp.add_text(stamp, height=8, dxfattribs={'layer': 'text'}).set_placement((0, -8))
    with native_path(path, 'w') as p:
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            doc.write(f)
    return path


def write_png(path, sheet, project='', max_px=3000):
    """
    Лист → PNG: SVG рендерится QtSvg (PySide6 уже в зависимостях окна).
    Длинная сторона — не больше max_px: лист в 10 м при 6 px/мм занял бы
    ~8 ГБ памяти под QImage.
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    lo, hi = sheet['bounds']
    W, H = (hi[0] - lo[0]) * 1000 + 200, (hi[1] - lo[1]) * 1000 + 200
    k = max_px / max(W, H)
    svg = svg_text(sheet, project).encode('utf-8')
    r = QSvgRenderer(svg)
    img = QImage(max(1, int(W * k)), max(1, int(H * k)), QImage.Format_ARGB32)
    img.fill(Qt.white)
    p = QPainter(img)
    r.render(p, QRectF(0, 0, img.width(), img.height()))
    p.end()
    from fileio import native_path
    with native_path(path, 'w') as q:
        if not img.save(str(q), 'PNG'):
            raise OSError(f'не удалось записать {path}')
    return path
