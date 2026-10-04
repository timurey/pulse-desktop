"""Небольшие виджеты макета: переключатель, чип, сегменты, секции, куб навигации."""

import time

import numpy as np
from PySide6.QtCore import Qt, Signal, QSize, QRectF, QTimer
from PySide6.QtGui import QPainter, QColor, QImage, QPixmap
from PySide6.QtWidgets import (QWidget, QLabel, QHBoxLayout, QVBoxLayout, QGridLayout, QPushButton,
                               QToolButton, QFrame, QButtonGroup, QSizePolicy)

from view_cube import ViewCube, look_from, FACES

# общая тема (pulse_qt.app задаёт при старте) — виджеты рисуют себя её цветами
THEME = None


def set_theme(t):
    global THEME
    THEME = t


def restyle(w):
    """Применить динамическое свойство (chip=..., primary=...) к уже показанному виджету."""
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


class Switch(QWidget):
    toggled = Signal(bool)

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self._on = bool(checked)
        self.setFixedSize(30, 18)
        self.setCursor(Qt.PointingHandCursor)

    def isChecked(self):
        return self._on

    def setChecked(self, on, emit=False):
        on = bool(on)
        if on != self._on:
            self._on = on
            self.update()
            if emit:
                self.toggled.emit(on)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.isEnabled():
            self._on = not self._on
            self.update()
            self.toggled.emit(self._on)

    def paintEvent(self, ev):
        t = THEME
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(t.q('accent' if self._on else 'line2'))
        p.setBrush(t.q('accent' if self._on else 'bg3'))
        p.drawRoundedRect(r, 9, 9)
        p.setPen(Qt.NoPen)
        p.setBrush(t.q('bg1'))
        x = 14 if self._on else 3
        p.drawEllipse(QRectF(x, 3, 12, 12))
        p.end()


def chip(text='', kind='mut'):
    l = QLabel(text)
    l.setProperty('chip', kind)
    l.setAlignment(Qt.AlignCenter)
    l.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return l


def set_chip(l, text, kind):
    l.setText(text)
    if l.property('chip') != kind:
        l.setProperty('chip', kind)
        restyle(l)
    l.setVisible(bool(text))


def label(text, name='Lbl', wrap=False):
    l = QLabel(text)
    l.setObjectName(name)
    l.setWordWrap(wrap)
    return l


def button(text, cb=None, primary=False, icon=None, pad=False, tip=None):
    b = QPushButton(text)
    if primary:
        b.setProperty('primary', True)
    if pad:
        b.setProperty('pad', True)
    if icon is not None:
        b.setIcon(icon)
        b.setIconSize(QSize(16, 16))
    if cb is not None:
        b.clicked.connect(lambda _=False: cb())
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    return b


def tool(icon, text='', cb=None, tip=None, checkable=False, icon_only=False):
    b = QToolButton()
    b.setIcon(icon)
    b.setIconSize(QSize(20 if not icon_only else 18, 20 if not icon_only else 18))
    if text and not icon_only:
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    else:
        b.setObjectName('IconBtn')
    b.setCheckable(checkable)
    if cb is not None:
        b.clicked.connect(lambda _=False: cb())
    b.setToolTip(tip or text)
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)
    return b


class Section(QFrame):
    """Секция панели: заголовок КАПСОМ и содержимое; нижняя граница."""

    def __init__(self, title=None, sticky=False, spacing=10):
        super().__init__()
        self.setObjectName('SecStick' if sticky else 'Sec')
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(14, 14, 14, 14)
        self.lay.setSpacing(spacing)
        self.title = None
        if title:
            self.title = label(title.upper(), 'STitle')
            self.lay.addWidget(self.title)

    def add(self, w, stretch=0):
        if isinstance(w, QWidget):
            self.lay.addWidget(w, stretch)
        else:
            self.lay.addLayout(w, stretch)
        return w


class KV(QWidget):
    """Таблица «ключ — значение» моноширинным шрифтом."""

    def __init__(self):
        super().__init__()
        self.g = QGridLayout(self)
        self.g.setContentsMargins(0, 0, 0, 0)
        self.g.setHorizontalSpacing(12)
        self.g.setVerticalSpacing(6)
        self.g.setColumnStretch(1, 1)

    def set(self, rows):
        while self.g.count():
            w = self.g.takeAt(0).widget()
            if w is not None:
                w.hide()                                 # сразу: deleteLater рисует старое поверх нового
                w.deleteLater()
        for i, (k, v) in enumerate(rows):
            a, b = label(k, 'KV_k'), label(str(v), 'KV_v')
            b.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            b.setWordWrap(True)
            b.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.g.addWidget(a, i, 0, Qt.AlignTop)
            self.g.addWidget(b, i, 1)
        self.setVisible(bool(rows))


class Segmented(QFrame):
    """Сегментированный выбор (0.1° / 1° / 5°)."""
    changed = Signal(object)

    def __init__(self, items, current=None):
        super().__init__()
        self.setObjectName('Seg')
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.values = []
        for i, (text, value) in enumerate(items):
            b = QPushButton(text)
            b.setProperty('seg', True)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            self.group.addButton(b, i)
            lay.addWidget(b)
            self.values.append(value)
            if value == current:
                b.setChecked(True)
        if self.group.checkedId() < 0 and self.values:
            self.group.button(0).setChecked(True)
        self.group.idClicked.connect(lambda i: self.changed.emit(self.values[i]))

    def value(self):
        return self.values[max(0, self.group.checkedId())]


class Swatch(QWidget):
    def __init__(self, color=None, size=10):
        super().__init__()
        self.c = color or QColor(128, 128, 128)
        self.setFixedSize(size, size)

    def set_color(self, c):
        self.c = c
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.c)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 3, 3)
        p.end()


def hbox(*ws, spacing=6, margins=(0, 0, 0, 0), stretch_last=False):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for x in ws:
        if x is None:
            lay.addStretch(1)
        elif isinstance(x, QWidget):
            lay.addWidget(x)
        else:
            lay.addLayout(x)
    if stretch_last:
        lay.addStretch(1)
    return w


def vbox(*ws, spacing=4, margins=(0, 0, 0, 0)):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for x in ws:
        if x is None:
            lay.addStretch(1)
        else:
            lay.addWidget(x)
    return w


def field(caption, w):
    """Подпись над полем ввода."""
    return vbox(label(caption), w, spacing=4)


class Glass(QFrame):
    """Полупрозрачная плашка поверх 3D-вида."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('Glass')
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(10, 5, 10, 5)
        self.lay.setSpacing(8)


# ── куб навигации поверх 3D-вида ───────────────────────────────────────────
def _snap_basis(R):
    """Базис, почти совпадающий с осями, — точно по осям (после поворотов на 90° копится ошибка)."""
    R = np.asarray(R, float)
    S = np.zeros((3, 3))
    for j in range(3):
        i = int(np.argmax(np.abs(R[:, j])))
        if abs(R[i, j]) < 0.999:
            return R
        S[i, j] = np.sign(R[i, j])
    return S


def _slerp_basis(R0, R1, t):
    """Плавный поворот базиса камеры R0 → R1 (по кратчайшей оси)."""
    Rd = R1 @ R0.T
    c = np.clip((np.trace(Rd) - 1) / 2, -1, 1)
    ang = np.arccos(c)
    if ang < 1e-6:
        return R1
    if abs(np.pi - ang) < 1e-4:                         # поворот на 180°: ось — собственный вектор
        w, v = np.linalg.eigh((Rd + np.eye(3)) / 2)
        ax = v[:, -1]
    else:
        ax = np.array([Rd[2, 1] - Rd[1, 2], Rd[0, 2] - Rd[2, 0], Rd[1, 0] - Rd[0, 1]]) / (2 * np.sin(ang))
    K = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
    a = ang * t
    return (np.eye(3) + np.sin(a) * K + (1 - np.cos(a)) * K @ K) @ R0

class ViewCubeWidget(QFrame):
    """
    Куб из view_cube.py (рисование Pillow) на стеклянной плашке: клик по грани,
    ребру или углу — плавный поворот вида; перетаскивание — вращение.
    """
    described = Signal(str)
    homeRequested = Signal()

    # «верх» экрана для видов по граням: сверху/снизу — X вправо (Y вверх / вниз), сбоку — Z вверх
    FACE_UP = {(0, 0, 1): (0, 1, 0), (0, 0, -1): (0, -1, 0)}

    def __init__(self, view, font_path, parent=None):
        super().__init__(parent)
        self.setObjectName('Glass')
        self.view = view
        self.cube = ViewCube(font_path)
        self.setFixedSize(132, 132)
        t = THEME
        self._btns = {}
        for key, ic, tip in (('home', 'mdi6.home-outline', 'Вид 3D (домой)'),
                             ('ccw', 'mdi6.rotate-left', 'Повернуть вид на −90° вокруг оси взгляда'),
                             ('cw', 'mdi6.rotate-right', 'Повернуть вид на +90° вокруг оси взгляда'),
                             ('up', 'mdi6.menu-up', 'Соседняя грань сверху'),
                             ('down', 'mdi6.menu-down', 'Соседняя грань снизу'),
                             ('left', 'mdi6.menu-left', 'Соседняя грань слева'),
                             ('right', 'mdi6.menu-right', 'Соседняя грань справа')):
            b = QToolButton(self)
            b.setObjectName('CubeBtn')
            b.setIcon(t.icon(ic, 'ink3', 'accent'))
            b.setIconSize(QSize(16, 16))
            b.setFixedSize(20, 20)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, k=key: self.command(k))
            self._btns[key] = b
        W_, H_ = self.width(), self.height()
        pos = {'home': (3, 3), 'ccw': (W_ - 45, 3), 'cw': (W_ - 23, 3), 'up': (W_ // 2 - 10, 2),
               'down': (W_ // 2 - 10, H_ - 22), 'left': (2, H_ // 2 - 10), 'right': (W_ - 22, H_ // 2 - 10)}
        for k, (x, y) in pos.items():
            self._btns[k].move(x, y)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._key = None
        self._pix = None
        self._drag = None
        self._anim = None
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        view.cameraChanged.connect(self.refresh)

    def set_style(self, style):
        self.cube.style.update(style)
        self.refresh(force=True)

    def refresh(self, force=False):
        R = self.view.basis()
        dpr = self.devicePixelRatioF()
        W, H = int(self.width() * dpr), int(self.height() * dpr)
        key = (np.round(R, 4).tobytes(), self.cube.hover, W, H)
        if not force and key == self._key:
            return
        self._key = key
        im = self.cube.render(R, W, H, rgba=True)
        q = QImage(im.tobytes(), W, H, 4 * W, QImage.Format_RGBA8888).copy()
        q.setDevicePixelRatio(dpr)
        self._pix = QPixmap.fromImage(q)
        self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self._pix is not None:
            p = QPainter(self)
            p.drawPixmap(0, 0, self._pix)
            p.end()

    def _hit(self, ev):
        x, y = ev.position().x(), ev.position().y()
        return self.cube.hit(self.view.basis(), self.width(), self.height(), x, y)

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & Qt.LeftButton:
            x, y = ev.position().x(), ev.position().y()
            dx, dy = x - self._drag[0], y - self._drag[1]
            if self._drag[2] or np.hypot(dx, dy) > 3:
                self._drag = (x, y, True)
                self.view.orbit(dx * 0.35, dy * 0.35)
                self.cube.hover = None
            return
        h = self._hit(ev)
        if h != self.cube.hover:
            self.cube.hover = h
            self.refresh()

    def leaveEvent(self, ev):
        if self.cube.hover is not None:
            self.cube.hover = None
            self.refresh()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag = (ev.position().x(), ev.position().y(), False)

    def mouseReleaseEvent(self, ev):
        if self._drag is not None and not self._drag[2]:
            h = self._hit(ev)
            if h is not None:
                self.snap(h)
        self._drag = None

    def snap(self, h):
        """Клик по грани / ребру / углу: вид строго по осям, «верх» экрана выровнен."""
        e1 = ViewCube.snap_direction(h)
        if h[0] == 'face':
            up = self.FACE_UP.get(tuple(int(round(x)) for x in FACES[h[1]][0]), (0, 0, 1))
        else:
            up = (0, 0, 1) if abs(e1[2]) < 0.99 else self.view.basis()[:, 1]
        self.animate_to(look_from(e1, up))
        self.described.emit(ViewCube.describe(h))

    def command(self, key):
        """Кнопки вокруг куба: домой, поворот ±90° вокруг оси взгляда, соседняя грань."""
        if key == 'home':
            self.homeRequested.emit()
            return
        R = self.view.basis()
        x, y, z = R[:, 0], R[:, 1], R[:, 2]
        if key in ('cw', 'ccw'):                         # поворот камеры вокруг оси взгляда
            th = np.radians(90 if key == 'cw' else -90)
            x2 = np.cos(th) * x + np.sin(th) * y
            R1 = np.column_stack([x2, np.cross(z, x2), z])
            self.described.emit('вид повёрнут на ' + ('+90°' if key == 'cw' else '−90°'))
        else:                                            # грань сверху / снизу / слева / справа — вперёд
            nz = {'up': y, 'down': -y, 'left': -x, 'right': x}[key]
            if key in ('up', 'down'):
                R1 = np.column_stack([x, np.cross(nz, x), nz])
            else:
                R1 = np.column_stack([np.cross(y, nz), y, nz])
        self.animate_to(_snap_basis(R1))

    def animate_to(self, R1, dur=0.32):
        self._anim = (self.view.basis().copy(), np.asarray(R1, float), time.time(), dur)
        self._timer.start()

    def _tick(self):
        if self._anim is None:
            self._timer.stop()
            return
        R0, R1, t0, dur = self._anim
        p = min(1.0, (time.time() - t0) / dur)
        ease = 1 - (1 - p) ** 3
        R = _slerp_basis(R0, R1, ease) if p < 1.0 else R1
        if self.view.fly:                                # в полёте — только взгляд
            eye = self.view.eye()
            self.view.look_at(eye - R[:, 2], eye, R[:, 1])
        else:
            self.view.set_basis(R)
        if p >= 1.0:
            self._anim = None
            self._timer.stop()
