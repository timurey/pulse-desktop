"""
3D-вид облаков точек: VTK рисует вне экрана (на GPU), Qt показывает готовый кадр.

Почему не QVTKRenderWindowInteractor: в Python-обёртке VTK он всегда рисует в
отдельное нативное окно — поверх него нельзя положить полупрозрачные панели
(куб навигации, координаты, подсказки), а снимок окна для проверки выходит
чёрным. Чтение кадра из видеопамяти занимает ~10 мс даже на Retina-экране.

Навигация своя (как в САПР): левая кнопка — вращение вокруг вертикали
(поворотный стол, без завала горизонта), правая/средняя — сдвиг, колесо —
масштаб к точке под курсором, двойной клик — центр вращения в точку облака.
Режимы окна (ручная стыковка, выделение, полёт) перехватывают мышь через
`mouse_hook` до навигации.

Камера описывается базисом R (столбцы x, y, z; z — от центра к глазу), как
`get_model_matrix` в старом окне Open3D, — так `view_cube.py` работает без изменений.
"""

import time

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal, QPointF, QRectF
from PySide6.QtGui import QImage, QPainter, QColor, QPen
from PySide6.QtWidgets import QWidget

import vtkmodules.vtkRenderingOpenGL2                    # noqa: F401  (регистрация OpenGL)
from vtkmodules.vtkCommonCore import vtkPoints, vtkUnsignedCharArray
from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
from vtkmodules.vtkCommonMath import vtkMatrix4x4
from vtkmodules.vtkFiltersSources import vtkSphereSource
from vtkmodules.vtkRenderingCore import vtkRenderer, vtkRenderWindow, vtkActor, vtkPolyDataMapper
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy

from view_cube import look_from, unit


def _vtk_matrix(T):
    m = vtkMatrix4x4()
    T = np.asarray(T, float)
    for i in range(4):
        for j in range(4):
            m.SetElement(i, j, T[i, j])
    return m


def _np_matrix(m):
    return np.array([[m.GetElement(i, j) for j in range(4)] for i in range(4)])


def _cells(n, per=1):
    """Ячейки vtkCellArray: n ячеек по per индексов подряд."""
    ca = vtkCellArray()
    off = np.arange(0, n * per + 1, per, dtype=np.int64)
    conn = np.arange(n * per, dtype=np.int64)
    ca.SetData(numpy_to_vtkIdTypeArray(off, deep=True), numpy_to_vtkIdTypeArray(conn, deep=True))
    return ca


def points_polydata(P):
    P = np.ascontiguousarray(P, np.float32)
    pts = vtkPoints()
    pts.SetData(numpy_to_vtk(P, deep=True))
    pd = vtkPolyData()
    pd.SetPoints(pts)
    pd.SetVerts(_cells(len(P)))
    return pd


def lines_polydata(P, segs, colors=None):
    """P — вершины, segs — пары индексов, colors — RGB 0..1 на отрезок."""
    P = np.ascontiguousarray(P, np.float32)
    segs = np.asarray(segs, np.int64).reshape(-1, 2)
    pts = vtkPoints()
    pts.SetData(numpy_to_vtk(P, deep=True))
    ca = vtkCellArray()
    off = np.arange(0, 2 * len(segs) + 1, 2, dtype=np.int64)
    ca.SetData(numpy_to_vtkIdTypeArray(off, deep=True),
               numpy_to_vtkIdTypeArray(segs.ravel().copy(), deep=True))
    pd = vtkPolyData()
    pd.SetPoints(pts)
    pd.SetLines(ca)
    if colors is not None:
        c = (np.clip(np.asarray(colors, float), 0, 1) * 255).astype(np.uint8)
        arr = numpy_to_vtk(np.ascontiguousarray(c), deep=True)
        arr.SetName('colors')
        pd.GetCellData().SetScalars(arr)
    return pd


class _Item:
    __slots__ = ('actor', 'P', 'T', 'kind', 'size')

    def __init__(self, actor, P, T, kind, size):
        self.actor, self.P, self.T, self.kind, self.size = actor, P, T, kind, size


class CloudView(QWidget):
    cameraChanged = Signal()
    statusMessage = Signal(str)

    FOV = 50.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(200, 160)
        self.rw = vtkRenderWindow()
        self.rw.SetOffScreenRendering(1)
        self.rw.SetMultiSamples(0)
        self.ren = vtkRenderer()
        self.ren.SetNearClippingPlaneTolerance(0.0005)
        self.rw.AddRenderer(self.ren)
        self.cam = self.ren.GetActiveCamera()
        self.cam.SetViewAngle(self.FOV)
        self.items = {}
        self.point_px = 2.0                  # размер точки облака в логических пикселях
        self.parallel = False                # ортогональная проекция
        self._img = None
        self._pending = False
        self._buf = vtkUnsignedCharArray()
        self._drag = None                    # навигация: ('orbit'|'pan', x, y)
        self.mouse_hook = None               # fn(event_type, ev) -> True, если событие поглощено
        self.dblclick_hook = None            # fn(x, y) — двойной клик
        self.rubber = None                   # (x0, y0, x1, y1) рамка выделения (логические пиксели)
        self.bg = (0.08, 0.09, 0.11)
        self.bg2 = None
        # полёт
        self.fly = False
        self.fly_keys = set()
        self.fly_speed = 1.5
        self._fly_last = None
        self._fly_timer = QTimer(self)
        self._fly_timer.setInterval(16)
        self._fly_timer.timeout.connect(self._fly_tick)
        self.look_at([0, 0, 0], [0, -8, 8], [0, 0, 1])

    # ── сцена ────────────────────────────────────────────────────────────
    def has(self, name):
        return name in self.items

    def remove(self, name):
        it = self.items.pop(name, None)
        if it is not None:
            self.ren.RemoveActor(it.actor)
            self.update_view()

    def remove_prefix(self, prefix):
        for n in [n for n in self.items if n.startswith(prefix)]:
            self.ren.RemoveActor(self.items.pop(n).actor)
        self.update_view()

    def clear(self):
        for it in self.items.values():
            self.ren.RemoveActor(it.actor)
        self.items = {}
        self.update_view()

    def _add(self, name, pd, T, kind, P, color=None, size=None, width=None, scalars=False, opacity=1.0):
        old = self.items.pop(name, None)
        if old is not None:
            self.ren.RemoveActor(old.actor)
        m = vtkPolyDataMapper()
        m.SetInputData(pd)
        if scalars:
            m.SetScalarModeToUseCellData()
            m.SetColorModeToDirectScalars()
            m.ScalarVisibilityOn()
        else:
            m.ScalarVisibilityOff()
        a = vtkActor()
        a.SetMapper(m)
        pr = a.GetProperty()
        pr.SetLighting(False)
        if color is not None:
            pr.SetColor(*color)
        if opacity < 1.0:
            pr.SetOpacity(opacity)
        dpr = self.devicePixelRatioF()
        if kind == 'points':
            pr.SetPointSize((size or self.point_px) * dpr)
        if width:
            pr.SetLineWidth(width * dpr)
            pr.SetRenderLinesAsTubes(False)
        if T is not None:
            a.SetUserMatrix(_vtk_matrix(T))
        self.ren.AddActor(a)
        self.items[name] = _Item(a, P, None if T is None else np.asarray(T, float), kind, size)
        self.update_view()
        return a

    def set_cloud(self, name, P, color, T=None, visible=True, size=None):
        P = np.asarray(P)
        if len(P) == 0:
            self.remove(name)
            return None
        a = self._add(name, points_polydata(P), T, 'points', P, color, size)
        a.SetVisibility(bool(visible))
        return a

    def set_lines(self, name, P, segs, colors=None, T=None, width=2.0, color=None, opacity=1.0):
        if len(segs) == 0:
            self.remove(name)
            return None
        return self._add(name, lines_polydata(P, segs, colors), T, 'lines', np.asarray(P),
                         color, width=width, scalars=colors is not None, opacity=opacity)

    def set_sphere(self, name, center, radius, color, T=None):
        s = vtkSphereSource()
        s.SetCenter(*np.asarray(center, float))
        s.SetRadius(radius)
        s.SetThetaResolution(16)
        s.SetPhiResolution(12)
        s.Update()
        return self._add(name, s.GetOutput(), T, 'mesh', np.asarray(center)[None], color)

    def set_transform(self, name, T):
        it = self.items.get(name)
        if it is not None:
            it.T = np.asarray(T, float)
            it.actor.SetUserMatrix(_vtk_matrix(T))
            self.update_view()

    def set_visible(self, name, vis):
        it = self.items.get(name)
        if it is not None:
            it.actor.SetVisibility(bool(vis))
            self.update_view()

    def is_visible(self, name):
        it = self.items.get(name)
        return it is not None and bool(it.actor.GetVisibility())

    def set_color(self, name, color):
        it = self.items.get(name)
        if it is not None:
            it.actor.GetProperty().SetColor(*color)
            self.update_view()

    def set_point_size(self, px):
        self.point_px = float(px)
        dpr = self.devicePixelRatioF()
        for it in self.items.values():
            if it.kind == 'points' and it.size is None:
                it.actor.GetProperty().SetPointSize(self.point_px * dpr)
        self.update_view()

    def set_background(self, rgb, rgb2=None):
        self.bg, self.bg2 = rgb, rgb2
        self.ren.SetBackground(*rgb)
        if rgb2 is not None:
            self.ren.SetBackground2(*rgb2)
            self.ren.GradientBackgroundOn()
        else:
            self.ren.GradientBackgroundOff()
        self.update_view()

    def set_grid(self, z, lo, hi, color, step=1.0, opacity=0.5):
        """Сетка 1 м на высоте z в пределах lo..hi (x, y)."""
        if hi[0] - lo[0] <= 0 or hi[1] - lo[1] <= 0:
            self.remove('grid')
            return
        x0, x1 = np.floor(lo[0] / step) * step, np.ceil(hi[0] / step) * step
        y0, y1 = np.floor(lo[1] / step) * step, np.ceil(hi[1] / step) * step
        xs = np.arange(x0, x1 + 1e-6, step)
        ys = np.arange(y0, y1 + 1e-6, step)
        if len(xs) * len(ys) > 40000:
            return
        P, segs = [], []
        for x in xs:
            segs.append([len(P), len(P) + 1])
            P += [[x, y0, z], [x, y1, z]]
        for y in ys:
            segs.append([len(P), len(P) + 1])
            P += [[x0, y, z], [x1, y, z]]
        a = self.set_lines('grid', np.array(P), segs, None, None, 1.0, color, opacity)
        if a is not None:
            a.SetPickable(False)

    # ── отрисовка ────────────────────────────────────────────────────────
    def update_view(self):
        """Перерисовать в ближайшем цикле событий (несколько изменений — один кадр)."""
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self.render_now)

    def render_now(self):
        self._pending = False
        dpr = self.devicePixelRatioF()
        w, h = max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))
        if not self.isVisible() and self._img is not None:
            return
        self.rw.SetSize(w, h)
        self.ren.ResetCameraClippingRange()
        self.rw.Render()
        self.rw.GetRGBACharPixelData(0, 0, w - 1, h - 1, 0, self._buf)
        a = vtk_to_numpy(self._buf)
        if a.size != w * h * 4:
            return
        a = np.ascontiguousarray(a.reshape(h, w, 4)[::-1])
        img = QImage(a.data, w, h, 4 * w, QImage.Format_RGBX8888).copy()   # альфа фона VTK = 0
        img.setDevicePixelRatio(dpr)
        self._img = img
        self.update()
        self.cameraChanged.emit()

    def paintEvent(self, ev):
        p = QPainter(self)
        if self._img is not None:
            p.drawImage(QRectF(0, 0, self.width(), self.height()), self._img)
        else:
            p.fillRect(self.rect(), QColor.fromRgbF(*self.bg))
        if self.rubber is not None:
            x0, y0, x1, y1 = self.rubber
            r = QRectF(QPointF(min(x0, x1), min(y0, y1)), QPointF(max(x0, x1), max(y0, y1)))
            c = QColor(235, 70, 60)
            p.setPen(QPen(c, 1.5, Qt.DashLine))
            c.setAlpha(40)
            p.fillRect(r, c)
            p.drawRect(r)
        p.end()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.update_view()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.update_view()

    def grab_scene(self):
        """Текущий кадр 3D-вида (QImage) — для скриншотов и тестов."""
        self.render_now()
        return self._img

    # ── камера ───────────────────────────────────────────────────────────
    def look_at(self, center, eye, up):
        self.cam.SetFocalPoint(*map(float, center))
        self.cam.SetPosition(*map(float, eye))
        self.cam.SetViewUp(*map(float, up))
        self.cam.OrthogonalizeViewUp()
        self.update_view()

    def basis(self):
        """R: столбцы x, y, z камеры в мире (z — от центра к глазу)."""
        pos, foc = np.array(self.cam.GetPosition()), np.array(self.cam.GetFocalPoint())
        z = unit(pos - foc)
        y = np.array(self.cam.GetViewUp())
        y = unit(y - (y @ z) * z)
        x = np.cross(y, z)
        return np.column_stack([x, y, z])

    def center(self):
        return np.array(self.cam.GetFocalPoint())

    def eye(self):
        return np.array(self.cam.GetPosition())

    def distance(self):
        return float(np.linalg.norm(self.eye() - self.center()))

    def set_basis(self, R, center=None, dist=None):
        c = self.center() if center is None else np.asarray(center, float)
        d = self.distance() if dist is None else float(dist)
        self.look_at(c, c + R[:, 2] * d, R[:, 1])

    def set_view_dir(self, e, up_hint=(0, 0, 1)):
        """Смотреть на центр вращения с направления e (в полёте — меняется только взгляд)."""
        R = look_from(e, up_hint)
        if self.fly:
            eye = self.eye()
            self.look_at(eye - R[:, 2], eye, R[:, 1])
            return
        self.set_basis(R)

    def fit(self, lo, hi, direction=(0, 0, 1), up_hint=(0, 1, 0)):
        """Показать коробку lo..hi с направления direction."""
        lo, hi = np.asarray(lo, float), np.asarray(hi, float)
        c = 0.5 * (lo + hi)
        R = look_from(direction, up_hint)
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        q = (corners - c) @ R                            # углы коробки в осях камеры
        aspect = max(0.2, self.width() / max(1, self.height()))
        half_h = max(np.abs(q[:, 1]).max(), np.abs(q[:, 0]).max() / aspect, 0.5)
        d = half_h / np.tan(np.radians(self.FOV / 2)) * 1.08 + np.abs(q[:, 2]).max()
        if self.parallel:
            self.cam.SetParallelScale(half_h * 1.08)
        self.look_at(c, c + R[:, 2] * d, R[:, 1])

    # ── проекция ─────────────────────────────────────────────────────────
    def set_parallel(self, on):
        """Ортогональная (True) или перспективная проекция; видимый масштаб у центра сохраняется."""
        on = bool(on)
        if on == self.parallel:
            return
        half = np.tan(np.radians(self.FOV / 2))
        if on:
            self.cam.SetParallelScale(max(0.05, self.distance() * half))
            self.cam.ParallelProjectionOn()
        else:
            R, c = self.basis(), self.center()
            d = max(0.5, self.cam.GetParallelScale() / half)
            self.cam.ParallelProjectionOff()
            self.look_at(c, c + R[:, 2] * d, R[:, 1])
        self.parallel = on
        self.update_view()

    def world_per_px(self):
        """Метров на пиксель экрана у центра вращения."""
        if self.parallel:
            return 2 * self.cam.GetParallelScale() / max(1, self.height())
        return 2 * self.distance() * np.tan(np.radians(self.FOV / 2)) / max(1, self.height())

    def orbit(self, dx_deg, dy_deg):
        R = self.basis()
        rz = np.radians(-dx_deg)
        Rz = np.array([[np.cos(rz), -np.sin(rz), 0], [np.sin(rz), np.cos(rz), 0], [0, 0, 1]])
        R = Rz @ R
        ax = R[:, 0]
        a = np.radians(dy_deg)
        K = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
        Rx = np.eye(3) + np.sin(a) * K + (1 - np.cos(a)) * K @ K
        R2 = Rx @ R
        if R2[2, 1] < 0.0005:                            # не переворачиваться через зенит/надир
            R2 = R
        R2 = look_from(R2[:, 2], R2[:, 1])
        if self.fly:
            eye = self.eye()
            self.look_at(eye - R2[:, 2], eye, R2[:, 1])
        else:
            self.set_basis(R2)

    def pan(self, dx_px, dy_px):
        R = self.basis()
        d = (-dx_px * R[:, 0] + dy_px * R[:, 1]) * self.world_per_px()
        self.look_at(self.center() + d, self.eye() + d, R[:, 1])

    def zoom_at(self, x, y, factor):
        """factor < 1 — ближе; точка под курсором остаётся на месте."""
        o, v = self.ray(x, y)
        R = self.basis()
        c = self.center()
        denom = v @ R[:, 2]
        if abs(denom) < 1e-9:
            target = c
        else:
            t = ((c - o) @ R[:, 2]) / denom
            target = o + t * v if t > 0 else c
        if self.parallel:
            # ортогональная: меняется масштаб, точка под курсором остаётся на месте
            sc = self.cam.GetParallelScale() * factor
            if sc < 0.02 or sc > 5000:
                return
            shift = (target - c) * (1 - factor)
            shift -= (shift @ R[:, 2]) * R[:, 2]
            self.cam.SetParallelScale(sc)
            self.look_at(c + shift, self.eye() + shift, R[:, 1])
            return
        eye = target + (self.eye() - target) * factor
        foc = target + (c - target) * factor
        if np.linalg.norm(eye - foc) < 0.05:
            return
        self.look_at(foc, eye, R[:, 1])

    def set_center(self, p):
        """Новый центр вращения (взгляд перенаправляется, глаз на месте)."""
        R = self.basis()
        self.look_at(p, self.eye(), R[:, 1] if abs(R[2, 2]) > 0.98 else (0, 0, 1))

    # ── экранная математика ──────────────────────────────────────────────
    def view_proj(self):
        """(view 4×4, proj 4×4, W, H) в логических пикселях виджета (как в manual_clean)."""
        W, H = max(1, self.width()), max(1, self.height())
        V = _np_matrix(self.cam.GetViewTransformMatrix())
        self.ren.ResetCameraClippingRange()
        P = _np_matrix(self.cam.GetProjectionTransformMatrix(W / H, -1, 1))
        return V, P, W, H

    def unproject(self, x, y, ndc_z):
        V, P, W, H = self.view_proj()
        nx, ny = 2 * x / W - 1, 1 - 2 * y / H
        q = np.linalg.inv(P @ V) @ np.array([nx, ny, ndc_z, 1.0])
        return q[:3] / q[3]

    def ray(self, x, y):
        p0 = self.unproject(x, y, -1.0)
        p1 = self.unproject(x, y, 0.5)
        return p0, unit(p1 - p0)

    def ray_plane_z(self, x, y, z0):
        """Пересечение луча под курсором с горизонтальной плоскостью z = z0."""
        o, v = self.ray(x, y)
        if abs(v[2]) < 1e-9:
            return None
        t = (z0 - o[2]) / v[2]
        g = o + t * v
        return g if np.isfinite(g).all() and t > 0 else None

    def project(self, Pw):
        """Мир → (экранные xy, глубина вдоль взгляда, перед камерой)."""
        V, P, W, H = self.view_proj()
        Ph = np.column_stack([Pw, np.ones(len(Pw))])
        clip = Ph @ (P @ V).T
        w = clip[:, 3]
        depth = -(Ph @ V[2])                             # глубина вдоль взгляда (и в ортогональной)
        front = (w > 1e-9) & (depth > 0)
        ndc = clip[:, :2] / np.where(w > 1e-9, w, 1.0)[:, None]
        return np.column_stack([(ndc[:, 0] + 1) * 0.5 * W, (1 - ndc[:, 1]) * 0.5 * H]), depth, front

    def pick_point(self, x, y, tol_px=7, names=None):
        """Ближайшая к глазу точка облаков под курсором. → (имя, точка мира) или None."""
        best = None
        for n, it in self.items.items():
            if it.kind != 'points' or not it.actor.GetVisibility():
                continue
            if names is not None and n not in names:
                continue
            P = it.P if it.T is None else it.P @ it.T[:3, :3].T + it.T[:3, 3]
            s, depth, front = self.project(P)
            m = front & (np.abs(s[:, 0] - x) < tol_px) & (np.abs(s[:, 1] - y) < tol_px)
            if not m.any():
                continue
            idx = np.flatnonzero(m)
            # ближайшие к курсору, из них — ближайшая к глазу
            d2 = (s[idx, 0] - x) ** 2 + (s[idx, 1] - y) ** 2
            near = idx[d2 <= d2.min() + (tol_px * 0.5) ** 2]
            k = near[np.argmin(depth[near])]
            if best is None or depth[k] < best[0]:
                best = (depth[k], n, P[k])
        return None if best is None else (best[1], best[2])

    # ── мышь ─────────────────────────────────────────────────────────────
    def mousePressEvent(self, ev):
        self.setFocus()
        if self.mouse_hook and self.mouse_hook('press', ev):
            return
        b = ev.button()
        x, y = ev.position().x(), ev.position().y()
        if b == Qt.LeftButton and not (ev.modifiers() & Qt.ShiftModifier):
            self._drag = ('orbit', x, y)
        elif b in (Qt.RightButton, Qt.MiddleButton) or b == Qt.LeftButton:
            self._drag = ('pan', x, y)

    def mouseMoveEvent(self, ev):
        if self.mouse_hook and self.mouse_hook('move', ev):
            return
        if self._drag is None:
            return
        mode, x0, y0 = self._drag
        x, y = ev.position().x(), ev.position().y()
        dx, dy = x - x0, y - y0
        self._drag = (mode, x, y)
        if mode == 'orbit':
            k = 0.18 if self.fly else 0.3
            self.orbit(dx * k, dy * k)
        else:
            if self.fly:
                R = self.basis()
                d = (-dx * R[:, 0] + dy * R[:, 1]) * 0.01
                self.look_at(self.center() + d, self.eye() + d, R[:, 1])
            else:
                self.pan(dx, dy)

    def mouseReleaseEvent(self, ev):
        if self.mouse_hook and self.mouse_hook('release', ev):
            self._drag = None
            return
        self._drag = None

    def mouseDoubleClickEvent(self, ev):
        if self.dblclick_hook and ev.button() == Qt.LeftButton:
            self.dblclick_hook(ev.position().x(), ev.position().y())

    def wheelEvent(self, ev):
        if self.mouse_hook and self.mouse_hook('wheel', ev):
            return
        steps = ev.angleDelta().y() / 120.0 or ev.pixelDelta().y() / 40.0
        if self.fly:
            self.fly_speed = float(np.clip(self.fly_speed * 1.25 ** steps, 0.1, 50))
            self.statusMessage.emit(f'полёт: скорость {self.fly_speed:.1f} м/с')
            return
        x, y = ev.position().x(), ev.position().y()
        self.zoom_at(x, y, 0.85 ** steps)

    # ── полёт (WASD) ─────────────────────────────────────────────────────
    FLY_KEYS = {Qt.Key_W, Qt.Key_A, Qt.Key_S, Qt.Key_D, Qt.Key_Space, Qt.Key_E, Qt.Key_C, Qt.Key_Q,
                Qt.Key_Shift}

    def set_fly(self, on, eye=None, forward=None):
        if on and self.parallel:
            self.set_parallel(False)
        self.fly = on
        self.fly_keys.clear()
        self._fly_last = None
        if on:
            e = self.eye() if eye is None else np.asarray(eye, float)
            f = -self.basis()[:, 2] if forward is None else unit(forward)
            if abs(f[2]) > 0.95:                         # сверху «шутер» бессмыслен — взгляд к горизонту
                f = unit(np.array([f[0], f[1], 0]) if np.hypot(f[0], f[1]) > 1e-6 else np.array([0, 1.0, 0]))
            self.look_at(e + f, e, (0, 0, 1))
            self._fly_timer.start()
        else:
            self._fly_timer.stop()
            # центр вращения — в нескольких метрах перед глазом
            R = self.basis()
            e = self.eye()
            self.look_at(e - R[:, 2] * 4.0, e, R[:, 1])

    def keyPressEvent(self, ev):
        if self.fly and ev.key() in self.FLY_KEYS and not ev.isAutoRepeat():
            self.fly_keys.add(ev.key())
            return
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        if self.fly and ev.key() in self.FLY_KEYS and not ev.isAutoRepeat():
            self.fly_keys.discard(ev.key())
            return
        super().keyReleaseEvent(ev)

    def focusOutEvent(self, ev):
        self.fly_keys.clear()
        super().focusOutEvent(ev)

    def _fly_tick(self):
        if not self.fly or not self.fly_keys:
            self._fly_last = None
            return
        now = time.time()
        dt = 0.0 if self._fly_last is None else min(0.1, now - self._fly_last)
        self._fly_last = now
        has = self.fly_keys.__contains__
        R = self.basis()
        fwd = -R[:, 2]
        fwd = unit(np.array([fwd[0], fwd[1], 0.0])) if np.hypot(fwd[0], fwd[1]) > 1e-6 else np.array([0, 1.0, 0])
        right = np.array([fwd[1], -fwd[0], 0.0])
        up = np.array([0, 0, 1.0])
        d = (has(Qt.Key_W) - has(Qt.Key_S)) * fwd + (has(Qt.Key_D) - has(Qt.Key_A)) * right + \
            ((has(Qt.Key_Space) or has(Qt.Key_E)) - (has(Qt.Key_C) or has(Qt.Key_Q))) * up
        if not d.any():
            return
        v = self.fly_speed * (4.0 if has(Qt.Key_Shift) else 1.0) * dt
        self.look_at(self.center() + v * d, self.eye() + v * d, R[:, 1])
