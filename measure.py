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
    """Короткая подпись значения замера (на 3D-виде)."""
    v = evaluate(m, z0)
    t = m['type']
    if t == 'dist' and v:
        s = f"{v['length']:.3f} м"
        if abs(v['dz']) > 0.005 and v['horizontal'] > 0.005:
            s += f"  (гор. {v['horizontal']:.3f}, Δh {v['dz']:+.3f})"
        return s
    if t == 'height' and v:
        return v['elevation']
    if t == 'plane' and v:
        return f"⟂ {v['distance']:.3f} м"
    if t == 'poly' and v:
        s = f"{v['length']:.2f} м"
        if 'area' in v:
            s = f"S {v['area']:.2f} м²" + (f" (план {v['area_plan']:.2f})" if abs(v['area'] - v['area_plan']) > 0.01 else '')
        return s
    return ''


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
