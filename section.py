#!/usr/bin/env python3
"""
Сечение облака плоскостью (общая система, Z вверх).

  slice — срез: видна только полоса точек толщиной thick вокруг плоскости;
  clip  — отсечение: скрыто всё по одну сторону плоскости (например, выше 2.5 м).

Основа плоскости: 'z' (горизонтальная, положение — отметка от нулевого уровня),
'x' / 'y' (вертикальные вдоль осей), 'plane' (по найденной стене / полу).
Доворот — углы a, b (градусы) вокруг двух осей самой плоскости:
  у горизонтальной — вокруг X и Y; у вертикальной — вокруг Z (азимут) и горизонтали
  в плоскости (наклон). Положение — смещение c вдоль нормали (n·x = c).

Состояние — dict (хранится в проекте): mode, base, n0, a, b, c, thick, flip.
"""

import numpy as np

BASES = {'z': (0.0, 0.0, 1.0), 'x': (1.0, 0.0, 0.0), 'y': (0.0, 1.0, 0.0)}


def default(base='z', z0=0.0, h=2.5):
    return {'mode': 'off', 'base': base, 'n0': list(BASES.get(base, (0, 0, 1))), 'a': 0.0, 'b': 0.0,
            'c': float(z0 + h) if base == 'z' else 0.0, 'thick': 0.10, 'flip': False}


def axes(n0):
    """Две оси доворота в плоскости с нормалью n0 (см. заголовок)."""
    n0 = np.asarray(n0, float)
    n0 = n0 / np.linalg.norm(n0)
    if abs(n0[2]) > 0.9:                                 # горизонтальная
        return np.array([1.0, 0, 0]), np.array([0, 1.0, 0])
    u = np.array([0, 0, 1.0])                            # вертикальная: азимут и наклон
    v = np.cross(u, n0)
    return u, v / np.linalg.norm(v)


def _rot(axis, deg):
    a = np.radians(deg)
    k = np.asarray(axis, float)
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(a) * K + (1 - np.cos(a)) * K @ K


def normal(st):
    """Нормаль плоскости с учётом доворота."""
    n0 = np.asarray(st['n0'], float)
    n0 = n0 / np.linalg.norm(n0)
    u, v = axes(n0)
    n = _rot(u, st['a']) @ _rot(v, st['b']) @ n0
    return n / np.linalg.norm(n)


def keep_mask(P, st):
    """Маска видимых точек P (общая система) при сечении st."""
    if st is None or st.get('mode', 'off') == 'off':
        return np.ones(len(P), bool)
    n = normal(st)
    d = np.asarray(P, float) @ n - st['c']
    if st['mode'] == 'slice':
        return np.abs(d) <= st['thick'] / 2
    return d >= 0 if st.get('flip') else d <= 0


def vtk_planes(st):
    """[(точка, нормаль)] плоскостей отсечения VTK (VTK оставляет сторону, куда смотрит нормаль)."""
    if st is None or st.get('mode', 'off') == 'off':
        return []
    n = normal(st)
    c = float(st['c'])
    if st['mode'] == 'slice':
        h = st['thick'] / 2
        return [(n * (c - h), n), (n * (c + h), -n)]
    return [(n * c, n)] if st.get('flip') else [(n * c, -n)]


def center(st, lo, hi):
    """Точка плоскости ближе всего к центру коробки lo..hi — для рамки и транспортиров."""
    n = normal(st)
    m = 0.5 * (np.asarray(lo, float) + np.asarray(hi, float))
    return m - (m @ n - st['c']) * n


def outline(st, lo, hi, pad=1.0):
    """Четыре угла прямоугольника плоскости, покрывающего коробку lo..hi."""
    n = normal(st)
    c = center(st, lo, hi)
    e = np.asarray(hi, float) - np.asarray(lo, float)
    r = 0.5 * float(np.linalg.norm(e)) + pad
    u = np.cross(n, [0, 0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0, 0])
    u = u - (u @ n) * n
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return np.array([c + r * (su * u + sv * v) for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1))])


def describe(st, z0=0.0):
    if st is None or st.get('mode', 'off') == 'off':
        return 'сечение выключено'
    n = normal(st)
    what = 'срез' if st['mode'] == 'slice' else 'отсечение'
    if abs(n[2]) > 0.99 and st['base'] == 'z':
        import measure
        side = '' if st['mode'] == 'slice' else (' (видно ниже)' if not st.get('flip') else ' (видно выше)')
        return f"{what} на отметке {measure.elevation(st['c'] * np.sign(n[2]), z0)}{side}"
    return f"{what}: {st['base'].upper() if st['base'] != 'plane' else 'по плоскости'}, c = {st['c']:.3f} м"
