#!/usr/bin/env python3
"""
Куб навигации (ViewCube) для scan_gui: грани, рёбра и углы активны —
клик ставит камеру с этой стороны, перетаскивание вращает вид. Оси X/Y/Z
нарисованы у угла куба. Порт куба из веб-превью HMI (orangepi/hmi/templates/preview.html),
но в системе Z-вверх (общая система инструмента).

Модуль без зависимости от окна: геометрия, рисование в картинку (Pillow),
определение попадания мышью и направление привязки.
"""

import numpy as np
from PIL import Image, ImageDraw, ImageFont

VERTS = np.array([[x, y, z] for z in (-1, 1) for y in (-1, 1) for x in (-1, 1)], float)
# индекс = x>0 | (y>0)<<1 | (z>0)<<2

# грани: нормаль (направление, откуда смотрит камера), подпись, вершины по кругу
FACES = [
    ((0, 0, 1), 'ВЕРХ', (4, 5, 7, 6)),
    ((0, 0, -1), 'НИЗ', (0, 2, 3, 1)),
    ((1, 0, 0), 'ПРАВО', (1, 3, 7, 5)),
    ((-1, 0, 0), 'ЛЕВО', (0, 4, 6, 2)),
    ((0, -1, 0), 'ПЕРЕД', (0, 1, 5, 4)),
    ((0, 1, 0), 'ЗАД', (2, 6, 7, 3)),
]
EDGES = [(a, b) for a in range(8) for b in range(a + 1, 8)
         if np.sum(np.abs(VERTS[a] - VERTS[b])) == 2]          # 12 рёбер
AXES = [((1, 0, 0), (214, 48, 48), 'X'), ((0, 1, 0), (34, 160, 34), 'Y'),
        ((0, 0, 1), (34, 85, 238), 'Z')]

HOVER = (77, 166, 255)
FACE_FILL = (200, 214, 232)
EDGE_COLOR = (74, 88, 112)
LABEL_COLOR = (26, 32, 48)
BG = (255, 255, 255)
SIZE = 0.72          # половина ребра куба в «единицах» холста


def unit(v):
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


class ViewCube:
    def __init__(self, font_path=None, style=None):
        self.font_path = font_path
        # цвета (для тем нового окна): bg, face, edge, label, hover — RGB-кортежи
        self.style = {'bg': BG, 'face': FACE_FILL, 'edge': EDGE_COLOR, 'label': LABEL_COLOR,
                      'hover': HOVER, **(style or {})}
        self.hover = None             # ('face'|'edge'|'corner', index)
        self._fonts = {}

    # ── проекция ────────────────────────────────────────────────────────
    @staticmethod
    def _project(P, R, W, H):
        """
        R — 3×3: столбцы x, y, z камеры в мире (как у get_model_matrix основного вида:
        камера смотрит вдоль −z, вверх +y). Перспектива с глаза на расстоянии 3.5.
        """
        x_c, y_c, z_c = R[:, 0], R[:, 1], R[:, 2]
        eye = z_c * 3.5
        rel = P - eye
        depth = -(rel @ z_c)
        s = min(W, H) * 0.78
        sx = W * 0.5 + (rel @ x_c) * s / depth
        sy = H * 0.5 - (rel @ y_c) * s / depth
        return np.column_stack([sx, sy]), depth

    def _font(self, px):
        px = max(6, int(px))
        if px not in self._fonts:
            try:
                self._fonts[px] = ImageFont.truetype(self.font_path, px) if self.font_path \
                    else ImageFont.load_default()
            except Exception:                            # noqa: BLE001
                self._fonts[px] = ImageFont.load_default()
        return self._fonts[px]

    def _geometry(self, R, W, H):
        P = VERTS * SIZE
        pts, depth = self._project(P, R, W, H)
        return pts, depth

    # ── рисование ───────────────────────────────────────────────────────
    def _visible_faces(self, R):
        e = R[:, 2]
        return [i for i, (n, _, _) in enumerate(FACES) if np.dot(n, e) > -0.05]

    def _draw_axes(self, d, R, W, H, k, which):
        """Оси от угла (−X, −Y, −Z) вдоль рёбер куба. which: 'hidden' | 'visible'."""
        o = np.array([-1.0, -1.0, -1.0]) * SIZE
        oP, od = self._project(o[None], R, W, H)
        if od[0] <= 0:
            return
        vis_f = self._visible_faces(R)
        for ax, col, lab in AXES:
            # ребро вдоль оси от угла 0 видно, если принадлежит видимой грани
            j = {0: 1, 1: 2, 2: 4}[int(np.argmax(ax))]
            edge_vis = any(0 in FACES[i][2] and j in FACES[i][2] for i in vis_f)
            if (which == 'visible') != edge_vis:
                continue
            tip = o + np.asarray(ax) * SIZE * 2.4
            tP, td = self._project(tip[None], R, W, H)
            if td[0] <= 0:
                continue
            c = col if edge_vis else _mix(self.style['bg'], col, 0.45)
            a0, a1 = oP[0], tP[0]
            d.line([tuple(a0), tuple(a1)], fill=c, width=max(2, int(1.6 * k)))
            if np.linalg.norm(a1 - a0) > 4:
                v = unit(a1 - a0)
                nrm = np.array([-v[1], v[0]])
                ah = 6 * k
                d.polygon([tuple(a1), tuple(a1 - v * ah + nrm * ah * 0.5),
                           tuple(a1 - v * ah - nrm * ah * 0.5)], fill=c)
                lp = a1 + v * 9 * k
                d.text((float(lp[0]), float(lp[1])), lab, fill=c, font=self._font(12 * k),
                       anchor='mm')

    def render(self, R, W, H, rgba=False):
        """Картинка куба (RGB, W×H) для ориентации камеры R; rgba=True — прозрачный фон."""
        st = self.style
        HOVER, FACE_FILL, EDGE_COLOR, LABEL_COLOR = st['hover'], st['face'], st['edge'], st['label']
        img = Image.new('RGBA', (W, H), (0, 0, 0, 0) if rgba else tuple(st['bg']) + (255,))
        d = ImageDraw.Draw(img)
        pts, depth = self._geometry(R, W, H)
        e = R[:, 2]                                   # направление на глаз
        k = min(W, H) / 160.0                         # масштаб относительно 160 px веб-версии
        self._draw_axes(d, R, W, H, k, 'hidden')      # скрытые оси — под полупрозрачными гранями
        layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        dl = ImageDraw.Draw(layer)
        order = sorted(range(len(FACES)), key=lambda i: -np.mean(depth[list(FACES[i][2])]))
        labels = []
        for i in order:
            n, label, vi = FACES[i]
            ndot = float(np.dot(n, e))
            hov = self.hover == ('face', i)
            if ndot < -0.05 and not hov:
                continue
            poly = [tuple(pts[j]) for j in vi]
            if hov:
                fill = tuple(HOVER) + (235,)
            else:
                b = max(0.55, min(1.0, 0.6 + 0.45 * ndot))
                fill = _mix(st['bg'], FACE_FILL, b) + (215,)
            dl.polygon(poly, fill=fill, outline=tuple(EDGE_COLOR) + (255,))
            if ndot > 0.4:
                labels.append((i, ndot, hov))
        img = Image.alpha_composite(img, layer)
        d = ImageDraw.Draw(img)
        for i, ndot, hov in labels:
            vi = FACES[i][2]
            c = np.mean(pts[list(vi)], axis=0)
            q = pts[list(vi)]
            width = 0.5 * (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3]))
            height = 0.5 * (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1]))
            px = min(width, height) * 0.24
            f = self._font(px)
            tw = d.textlength(FACES[i][1], font=f)
            if tw > 0.85 * min(width, height * 2):        # подогнать под грань
                f = self._font(px * 0.85 * min(width, height * 2) / tw)
            alpha = min(1.0, 0.35 + (ndot - 0.4) * 2.0)
            col = (255, 255, 255) if hov else _mix(st['bg'], LABEL_COLOR, alpha)
            d.text((float(c[0]), float(c[1])), FACES[i][1], fill=col, font=f, anchor='mm')
        for i, (a, b) in enumerate(EDGES):
            if self.hover == ('edge', i):
                d.line([tuple(pts[a]), tuple(pts[b])], fill=HOVER, width=max(3, int(4 * k)))
        if self.hover and self.hover[0] == 'corner':
            p = pts[self.hover[1]]
            r = 6 * k
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=HOVER)
        self._draw_axes(d, R, W, H, k, 'visible')
        return img if rgba else img.convert('RGB')

    # ── попадание мышью ─────────────────────────────────────────────────
    def hit(self, R, W, H, mx, my):
        """→ ('face'|'edge'|'corner', index) или None. Приоритет: угол, ребро, грань."""
        pts, depth = self._geometry(R, W, H)
        e = R[:, 2]
        k = min(W, H) / 160.0
        m = np.array([mx, my], float)
        # видимые вершины/рёбра — принадлежащие видимым граням
        vis_faces = [i for i, (n, _, _) in enumerate(FACES) if np.dot(n, e) > -0.05]
        vis_v = {j for i in vis_faces for j in FACES[i][2]}
        best = None
        for j in vis_v:
            dist = np.linalg.norm(pts[j] - m)
            if dist < 9 * k and (best is None or dist < best[0]):
                best = (dist, ('corner', j))
        if best:
            return best[1]
        for i, (a, b) in enumerate(EDGES):
            if a not in vis_v or b not in vis_v:
                continue
            p0, p1 = pts[a], pts[b]
            dv = p1 - p0
            L2 = float(dv @ dv)
            if L2 < 1:
                continue
            t = np.clip((m - p0) @ dv / L2, 0, 1)
            dist = np.linalg.norm(m - p0 - t * dv)
            if dist < 7 * k and (best is None or dist < best[0]):
                best = (dist, ('edge', i))
        if best:
            return best[1]
        for i in sorted(vis_faces, key=lambda i: np.mean(depth[list(FACES[i][2])])):
            if _in_poly(m, pts[list(FACES[i][2])]):
                return ('face', i)
        return None

    @staticmethod
    def snap_direction(h):
        """Направление «от центра к глазу» для привязки к грани/ребру/углу."""
        kind, i = h
        if kind == 'face':
            return unit(FACES[i][0])
        if kind == 'edge':
            a, b = EDGES[i]
            return unit(VERTS[a] + VERTS[b])
        return unit(VERTS[i])

    @staticmethod
    def describe(h):
        """Подпись для строки состояния: «вид: верх-право» и т.п."""
        if h is None:
            return ''
        names = [('право', 'лево'), ('зад', 'перед'), ('верх', 'низ')]
        v = ViewCube.snap_direction(h)
        parts = [names[a][0 if v[a] > 0 else 1] for a in (2, 1, 0) if abs(v[a]) > 1e-6]
        return 'вид: ' + '-'.join(parts)


def _mix(a, b, t):
    """Цвет между a (t=0) и b (t=1)."""
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _in_poly(p, poly):
    inside = False
    n = len(poly)
    for a in range(n):
        x1, y1 = poly[a]
        x2, y2 = poly[(a + 1) % n]
        if (y1 > p[1]) != (y2 > p[1]) and p[0] < (x2 - x1) * (p[1] - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def look_from(direction, up_hint=(0, 0, 1)):
    """
    Базис камеры (столбцы x, y, z как у get_model_matrix), смотрящей с направления
    `direction` (от центра к глазу). Для видов сверху/снизу «вверх» берётся из up_hint.
    """
    z = unit(direction)
    up = np.array([0, 0, 1.0])
    if abs(z @ up) > 0.99:
        up = unit(np.asarray(up_hint, float) - (np.asarray(up_hint, float) @ z) * z)
        if np.linalg.norm(up) < 1e-6:
            up = np.array([0, 1.0, 0])
    x = unit(np.cross(up, z))
    y = np.cross(z, x)
    return np.column_stack([x, y, z])


def slerp_dir(a, b, t):
    a, b = unit(a), unit(b)
    dot = float(np.clip(a @ b, -1, 1))
    if dot > 0.9995:
        return unit(a + t * (b - a))
    if dot < -0.9995:                                    # противоположные — через перпендикуляр
        perp = unit(np.cross(a, [0, 0, 1.0]) if abs(a[2]) < 0.9 else np.cross(a, [1.0, 0, 0]))
        return unit(np.cos(np.pi * t) * a + np.sin(np.pi * t) * perp)
    th = np.arccos(dot)
    return unit((np.sin((1 - t) * th) * a + np.sin(t * th) * b) / np.sin(th))
