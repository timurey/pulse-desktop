#!/usr/bin/env python3
"""
Замеры по облаку и нулевой уровень (общая система сеанса, Z вверх, метры).

Нулевой уровень — горизонтальная плоскость z = z0. По умолчанию — пол опорного скана
(самая большая найденная плоскость «пол»); можно задать точкой облака или числом.
Отметки считаются от него и пишутся как на чертежах: +2.450 / −0.150.

Замеры (dict, хранятся в проекте):
  dist   — две точки: длина, горизонтальная проекция, перепад высоты, уклон;
  height — одна точка: отметка от нулевого уровня;
  plane  — плоскость (n, c: n·x = c) и точка: расстояние по нормали и основание перпендикуляра;
  poly   — ломаная: длина, углы в вершинах; замкнутая — площадь (по плоскости контура).
"""

import numpy as np

import plane_register as pr


# ── нулевой уровень ────────────────────────────────────────────────────────
def default_zero(session):
    """z пола опорного скана в общей системе (None — пол не найден)."""
    ref = session.ref if session.scans else None
    if ref is None or not ref.analyzed or session.Tc(ref) is None:
        return None
    floors = [p for p in ref.planes if p.kind == 'floor']
    if not floors:
        return None
    p = max(floors, key=lambda q: q.area)
    T = session.Tc(ref)
    c = pr.transform(np.asarray(p.centroid)[None], T)[0]
    return float(c[2])


def elevation(z, z0):
    """Отметка как на чертежах: +2.450 / −0.150 / ±0.000."""
    h = float(z) - float(z0)
    if abs(h) < 0.0005:
        return '±0.000'
    return f"{'+' if h > 0 else '−'}{abs(h):.3f}"


# ── вычисления ─────────────────────────────────────────────────────────────
def dist(p1, p2):
    p1, p2 = np.asarray(p1, float), np.asarray(p2, float)
    d = p2 - p1
    L = float(np.linalg.norm(d))
    hor = float(np.hypot(d[0], d[1]))
    return {'length': L, 'horizontal': hor, 'dz': float(d[2]),
            'slope_deg': float(np.degrees(np.arctan2(abs(d[2]), hor))) if L > 0 else 0.0}


def point_plane(p, n, c):
    """Расстояние от точки до плоскости n·x = c и основание перпендикуляра."""
    p, n = np.asarray(p, float), np.asarray(n, float)
    n = n / np.linalg.norm(n)
    s = float(n @ p - c)
    return {'distance': abs(s), 'foot': (p - s * n).tolist()}


def polyline(pts, closed=False):
    """Длина, углы между звеньями в вершинах; у замкнутой — площадь (формула Ньюэлла)."""
    P = np.asarray(pts, float)
    segs = P[1:] - P[:-1]
    if closed and len(P) >= 3:
        segs = np.vstack([segs, P[0] - P[-1]])
    L = float(np.linalg.norm(segs, axis=1).sum()) if len(segs) else 0.0
    angles = []
    k = len(P) if closed else len(P) - 1
    for i in range(1, k) if not closed else range(len(P)):
        a = P[i - 1] - P[i]
        b = P[(i + 1) % len(P)] - P[i]
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na > 1e-9 and nb > 1e-9:
            angles.append(float(np.degrees(np.arccos(np.clip(a @ b / (na * nb), -1, 1)))))
    out = {'length': L, 'angles': angles}
    if closed and len(P) >= 3:
        nrm = np.zeros(3)
        for i in range(len(P)):
            q, r = P[i], P[(i + 1) % len(P)]
            nrm += np.cross(q, r)
        out['area'] = float(np.linalg.norm(nrm) / 2)
        hz = np.zeros(3)                                 # площадь проекции на горизонталь (план)
        for i in range(len(P)):
            q, r = P[i].copy(), P[(i + 1) % len(P)].copy()
            q[2] = r[2] = 0
            hz += np.cross(q, r)
        out['area_plan'] = float(abs(hz[2]) / 2)
    return out


def evaluate(m, z0=0.0):
    """Значения замера m (dict с 'type', 'pts', ...). → dict для подписи и списка."""
    t, P = m['type'], m['pts']
    if t == 'dist' and len(P) == 2:
        return dist(P[0], P[1])
    if t == 'height' and len(P) == 1:
        return {'elevation': elevation(P[0][2], z0), 'h': float(P[0][2] - z0)}
    if t == 'plane' and len(P) == 1 and 'plane' in m:
        return point_plane(P[0], m['plane']['n'], m['plane']['c'])
    if t == 'poly' and len(P) >= 2:
        return polyline(P, m.get('closed', False))
    return {}


def label(m, z0=0.0):
    """Короткая подпись значения замера (на 3D-виде); с флагом with_h —
    вторая строка «H=±X.XXX» (отметка первой точки от нулевого уровня)."""
    v = evaluate(m, z0)
    t = m['type']
    if t == 'dist' and v:
        s = f"{v['length']:.3f} м"
        if abs(v['dz']) > 0.005 and v['horizontal'] > 0.005:
            s += f"  (гор. {v['horizontal']:.3f}, Δh {v['dz']:+.3f})"
        return _with_h(s, m, z0)
    if t == 'height' and v:
        return v['elevation']
    if t == 'plane' and v:
        return _with_h(f"⟂ {v['distance']:.3f} м", m, z0)
    if t == 'poly' and v:
        s = f"{v['length']:.2f} м"
        if 'area' in v:
            s = f"S {v['area']:.2f} м²" + (f" (план {v['area_plan']:.2f})" if abs(v['area'] - v['area_plan']) > 0.01 else '')
        s = _with_h(s, m, z0)
        return s
    return ''


def _with_h(s, m, z0):
    """Вторая строка подписи «H=…» (кнопка H у замера)."""
    if m.get('with_h') and m.get('pts'):
        s = s or '·'
        return f"{s}\nH={elevation(m['pts'][0][2], z0)}"
    return s


def details(m, z0=0.0):
    """Подробности замера — строки для списка в панели."""
    v = evaluate(m, z0)
    t = m['type']
    if t == 'dist' and v:
        return [f"длина {v['length']:.3f} м", f"по горизонтали {v['horizontal']:.3f} м",
                f"перепад {v['dz']:+.3f} м", f"уклон {v['slope_deg']:.1f}°",
                f"отметки {elevation(m['pts'][0][2], z0)} → {elevation(m['pts'][1][2], z0)}"]
    if t == 'height' and v:
        p = m['pts'][0]
        return [f"отметка {v['elevation']}", f"x {p[0]:.3f}  y {p[1]:.3f}  z {p[2]:.3f}"]
    if t == 'plane' and v:
        return [f"до плоскости {v['distance']:.3f} м", m['plane'].get('name', '')]
    if t == 'poly' and v:
        out = [f"длина {v['length']:.3f} м, вершин {len(m['pts'])}"]
        if v.get('angles'):
            out.append('углы ' + ', '.join(f'{a:.1f}°' for a in v['angles'][:8]) + (' …' if len(v['angles']) > 8 else ''))
        if 'area' in v:
            out.append(f"площадь {v['area']:.3f} м² (в плане {v['area_plan']:.3f} м²)")
        return out
    return []


TITLES = {'dist': 'Расстояние', 'height': 'Отметка', 'plane': 'До плоскости', 'poly': 'Ломаная'}


# ── привязка (снап, osnap-механика «Оттиска») ──────────────────────────────

SNAP_KINDS = ('el-center', 'el-pt', 'vertex', 'mid')   # приоритет: раньше = важнее


def collect_snap_targets(session, circles=True):
    """
    Точки привязки в мировой системе: [(p, kind), …]. Приоритет вида — порядок
    SNAP_KINDS: центры/оси подогнанных элементов, вершины каркасов, вершины
    замеров, середины рёбер. Облако добавляет не здесь (оно в snap_pick через
    pick_point — тяжёлое, к подсветке на каждое движение не подключается).
    """
    import fits
    import numpy as np
    out = []

    def add(p, kind):
        out.append((np.asarray(p, float), kind))

    for el in getattr(session, 'elements', []):            # элементы: центр + каркас
        sc = session.by_id(el.get('scan'))
        if sc is None or sc.pose is None or not session.element_visible(el):
            continue
        T = session.Tc(sc)
        if el['kind'] in ('sphere', 'circle', 'cylinder'):
            add(T[:3, :3] @ np.asarray(el['center'], float) + T[:3, 3], 'el-center')
        pts, _ = fits.element_wire(el)
        for q in pts[::max(1, len(pts) // 16)]:            # вершины каркаса (до ~16 на элемент)
            add(T[:3, :3] @ q + T[:3, 3], 'el-pt')
    for m in getattr(session, 'measures', []):             # замеры: вершины, середины рёбер
        P = [np.asarray(q, float) for q in m.get('pts', [])]
        for q in P:
            add(q, 'vertex')
        if m['type'] in ('dist', 'poly') and len(P) >= 2:
            extra = range(len(P) - 1)
            if m.get('closed'):
                extra = list(extra) + [len(P) - 1]
            for i in extra:
                add(0.5 * (P[i] + P[(i + 1) % len(P)]), 'mid')
    return out


# ── разрезание / объединение площадей (Ф10) ────────────────────────────────

def split_polygon(pts, a, b, eps=1e-9):
    """
    Разрезать замкнутый контур прямой через точки a, b. → (контур 1, контур 2)
    или None, если пересечений не ровно два (линия не режет или невыпуклый
    случай с 4+ пересечениями).
    """
    P = np.asarray(pts, float)
    n = len(P)
    d = np.asarray(b, float) - np.asarray(a, float)
    L = np.linalg.norm(d)
    if L < 1e-12:
        return None
    # пересечение ищем в 2D-базисе плоскости контура (точки могут быть 3D)
    nrm = np.cross(P[1] - P[0], P[min(2, n - 1)] - P[0])
    Ln = np.linalg.norm(nrm)
    if Ln < 1e-9:
        return None
    nrm /= Ln
    u_ax = np.cross(nrm, [0, 0, 1.0])
    if np.linalg.norm(u_ax) < 1e-6:
        u_ax = np.cross(nrm, [0, 1.0, 0])
    u_ax /= np.linalg.norm(u_ax)
    v_ax = np.cross(nrm, u_ax)

    def flat(q):
        return np.array([np.asarray(q, float) @ u_ax, np.asarray(q, float) @ v_ax])

    d2, a2 = flat(d), flat(a)
    L2 = np.linalg.norm(d2)
    P2 = np.array([flat(q) for q in P])
    hits = []                                            # (номер ребра, точка на 3D-прямой)
    for i in range(n):
        e2 = P2[(i + 1) % n] - P2[i]
        M = np.column_stack([d2, -e2])
        det = np.linalg.det(M)
        if abs(det) < 1e-12:
            continue
        t, u = np.linalg.solve(M, P2[i] - a2)
        if -eps <= u <= 1 + eps:                         # точка на ребре (в базисе)
            hits.append((i, np.asarray(a, float) + (t / L2 if L2 > 0 else 0) * d))
    if len(hits) != 2:
        return None
    (i0, x0), (i1, x1) = hits
    if i0 == i1 and abs(float((x1 - x0) @ d)) < 1e-6:
        return None                                      # одно ребро дважды — касание
    # два обхода контура между точками пересечения
    def arc(i_from, x_from, i_to, x_to):
        out = [x_from]
        k = (i_from + 1) % n
        while k != (i_to + 1) % n:
            out.append(P[k])
            k = (k + 1) % n
        out.append(x_to)
        return out
    arc1 = arc(i0, x0, i1, x1)
    arc2 = arc(i1, x1, i0, x0)
    return [np.asarray(p, float) for p in arc1], [np.asarray(p, float) for p in arc2]


def merge_polygons(p1, p2, tol=0.01):
    """
    Объединить два замкнутых контура с общим ребром (в противоположных
    направлениях, вершины совпадают в пределах tol). → контур или None
    (общего ребра нет — полный boolean без shapely не делается намеренно).
    """
    A = [np.asarray(p, float) for p in p1]
    B = [np.asarray(p, float) for p in p2]
    nA, nB = len(A), len(B)
    for i in range(nA):
        a0, a1 = A[i], A[(i + 1) % nA]
        for j in range(nB):
            b1, b2 = B[j], B[(j + 1) % nB]
            # ребро A: a0→a1; в B — то же ребро в обратную сторону: b2→b1
            if np.linalg.norm(a0 - b2) < tol and np.linalg.norm(a1 - b1) < tol:
                out = [a1]                               # старт со второй вершины стыка
                k = (i + 2) % nA                         # A: после a1 до a0 (a1 уже в out)
                while k != i:
                    out.append(A[k])
                    k = (k + 1) % nA
                out.append(a0)
                k = (j + 2) % nB                         # B: после a0 до a1
                while k != j:
                    out.append(B[k])
                    k = (k + 1) % nB
                return out
    return None
