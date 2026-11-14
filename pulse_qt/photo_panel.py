"""Панель «Фото» и окно снимка: привязка фото к облаку, окраска (photos.py).

Координаты на снимке — пиксели ОРИГИНАЛА (могут быть отрицательными и больше
w/h: ручки тянутся за края кадра). Холст держит поля PAD вокруг фото, слои
(заливки/точки/контуры/ручки) перерисовываются поверх копии фото без его
пересборки — мгновенно при драге ручек.
"""
import numpy as np
from PySide6.QtCore import Qt, Signal, QRectF, QPointF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QLabel,
                               QVBoxLayout, QWidget)

from . import widgets as W
from .inspector import _Panel
from .theme import Theme

DISP_PX = 1800                # длинная сторона фото на холсте


class PhotoCanvas(QWidget):
    """Холст одного фото: лупа, быстрый слой поверхностей/ручек, события мыши."""

    PAD = 70                  # поля вокруг фото (виджет-пиксели) — ручки за кадром
    MAG = 3.0                 # увеличение лупы
    MAG_R = 85.0              # радиус лупы (виджет-пиксели)
    canvas_event = Signal(str, float, float)     # kind ∈ press/move/release, (u, v) ориг. px

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setMinimumSize(240, 240)
        self._img = None                 # QImage показанного фото
        self._ow = self._oh = 0          # размер оригинала
        self._k = 1.0                    # виджет-пикселей на пиксель оригинала
        self._ox = self._oy = 0.0        # левый верх фото в виджете
        self._wires = []                 # динамический слой (см. set_wires)
        self._cursor = None              # позиция мыши (лупа)
        self._press = None
        self._drag = False

    # ── фото и раскладка ─────────────────────────────────────────────────
    def set_photo(self, path, ow, oh):
        """Загрузить снимок; (ow, oh) — размер оригинала (система координат)."""
        if path is None:
            self._img = None
            self._ow = self._oh = 0
        else:
            import photos as ph
            arr = ph.load_image(path, DISP_PX)
            h, w = arr.shape[:2]
            self._img = QImage(np.ascontiguousarray(arr).data, w, h, 3 * w,
                               QImage.Format_RGB888).copy()
            self._ow, self._oh = int(ow) or w, int(oh) or h
        self._relayout()
        self.update()

    def has_photo(self):
        return self._img is not None

    def _relayout(self):
        if self._img is None:
            return
        aw = max(self.width() - 2 * self.PAD, 20)
        ah = max(self.height() - 2 * self.PAD, 20)
        self._k = min(aw / self._ow, ah / self._oh)
        dw, dh = self._ow * self._k, self._oh * self._k
        self._ox = (self.width() - dw) / 2
        self._oy = (self.height() - dh) / 2

    def resizeEvent(self, ev):
        self._relayout()
        super().resizeEvent(ev)

    def _xy(self, u, v):
        """Пиксели оригинала → виджет."""
        return QPointF(self._ox + u * self._k, self._oy + v * self._k)

    def _uv(self, pos):
        """Виджет → пиксели оригинала (без обрезки: ручки за кадром)."""
        return (pos.x() - self._ox) / self._k, (pos.y() - self._oy) / self._k

    def fit_size(self, max_w, max_h):
        """Размер окна под фото с полями."""
        if self._img is None:
            return 800, 600
        k = min((max_w - 2 * self.PAD - 24) / self._ow, (max_h - 2 * self.PAD - 64) / self._oh)
        return int(self._ow * k) + 2 * self.PAD + 24, int(self._oh * k) + 2 * self.PAD + 64

    # ── слой поверхностей/ручек ──────────────────────────────────────────
    def set_wires(self, wires):
        """Быстрый слой: список примитивов в пикселях оригинала.

        ('fill',  poly Nx2, rgb 0-255, alpha)      — заливка оболочки поверхности
        ('dots',  pts Nx2, rgb, alpha, r_screen)   — фактура точек (проёмы видны)
        ('line',  pts Nx2, rgb, w_screen, closed)  — контур
        ('handles', pts Nx2, rgb, r_screen)        — ручки (кружки с ободком)
        ('crosses', pts Nx2, rgb, r_screen)        — маркеры пар/репроекции
        """
        self._wires = list(wires or [])
        self.update()

    # ── отрисовка ────────────────────────────────────────────────────────
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), W.THEME.q('canvas'))
        if self._img is None:
            p.setPen(W.THEME.q('ink3'))
            p.drawText(self.rect(), Qt.AlignCenter, 'нет фото')
            return
        dw, dh = self._ow * self._k, self._oh * self._k
        p.drawImage(QRectF(self._ox, self._oy, dw, dh), self._img)
        p.setClipRect(QRectF(self._ox, self._oy, dw, dh))
        self._draw_wires(p)
        p.setClipping(False)
        if self._cursor is not None:
            self._draw_loupe(p)

    def _draw_wires(self, p):
        k = self._k
        for w in self._wires:
            kind = w[0]
            pts = w[1]
            rgb = w[2]
            col = QColor(rgb[0], rgb[1], rgb[2])
            if kind == 'fill':
                col.setAlpha(w[3])
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                poly = [self._xy(u, v) for u, v in pts]
                if len(poly) >= 3:
                    p.drawPolygon(poly)
            elif kind == 'dots':
                col.setAlpha(w[3])
                pen = QPen(col)
                pen.setWidth(max(1, int(round(w[4] * 2))))
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                pts_w = [self._xy(u, v) for u, v in pts]
                p.drawPoints(pts_w)
            elif kind == 'line':
                pen = QPen(col)
                pen.setWidth(max(1, int(round(w[3]))))
                p.setPen(pen)
                pts_w = [self._xy(u, v) for u, v in pts]
                if w[4]:
                    pts_w.append(pts_w[0])
                if len(pts_w) >= 2:
                    p.drawPolyline(pts_w)
            elif kind == 'handles':
                r = w[3]
                for u, v in pts:
                    c = self._xy(u, v)
                    p.setPen(QPen(QColor(255, 255, 255), 2))
                    p.setBrush(col)
                    p.drawEllipse(c, r, r)
            elif kind == 'crosses':
                r = w[3]
                if len(w) > 4:
                    col.setAlpha(w[4])
                pen = QPen(col)
                pen.setWidth(2)
                p.setPen(pen)
                for u, v in pts:
                    c = self._xy(u, v)
                    p.drawLine(c.x() - r, c.y(), c.x() + r, c.y())
                    p.drawLine(c.x(), c.y() - r, c.x(), c.y() + r)

    def _draw_loupe(self, p):
        """Лупа ×MAG у курсора: фото + слой, разворот у краёв виджета."""
        cur = self._cursor
        r = self.MAG_R
        cx = min(max(cur.x(), r + 4), self.width() - r - 4)
        cy = min(max(cur.y(), r + 4), self.height() - r - 4)
        # источник — показанное фото: окно r/MAG виджет-пикселей вокруг курсора
        kd = self._img.width() / self._ow                 # показанный px на пиксель оригинала
        sx = (cur.x() - self._ox) / self._k * kd
        sy = (cur.y() - self._oy) / self._k * kd
        half = r / self.MAG / self._k * kd
        src = QRectF(sx - half, sy - half, 2 * half, 2 * half)
        src = src.intersected(QRectF(0, 0, self._img.width(), self._img.height()))
        p.save()
        circle = QPainterPath()
        circle.addEllipse(QPointF(cx, cy), r, r)
        p.setClipPath(circle)                             # клип фиксирован в координатах виджета
        p.drawImage(QRectF(cx - r, cy - r, 2 * r, 2 * r), self._img, src)
        p.translate(cx, cy)
        p.scale(self.MAG, self.MAG)
        p.translate(-cur.x(), -cur.y())
        self._draw_wires(p)                               # слой в лупе тем же трансформом
        p.restore()
        pen = QPen(QColor(46, 204, 113))
        pen.setWidth(2)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.drawLine(int(cx - 10), int(cy), int(cx + 10), int(cy))
        p.drawLine(int(cx), int(cy - 10), int(cx), int(cy + 10))

    # ── мышь: ОДИН mouseMoveEvent (второй затёр бы первого) ─────────────
    def mousePressEvent(self, ev):
        if self._img is not None and ev.button() == Qt.LeftButton:
            self._press = ev.position()
            self._drag = True
            u, v = self._uv(ev.position())
            self.canvas_event.emit('press', u, v)

    def mouseMoveEvent(self, ev):
        self._cursor = ev.position()
        if self._img is not None and self._drag:
            u, v = self._uv(ev.position())
            self.canvas_event.emit('move', u, v)
        self.update()

    def mouseReleaseEvent(self, ev):
        if self._img is not None and ev.button() == Qt.LeftButton and self._drag:
            self._drag = False
            u, v = self._uv(ev.position())
            self.canvas_event.emit('release', u, v)

    def leaveEvent(self, ev):
        self._cursor = None
        self.update()
        super().leaveEvent(ev)


class PhotoDialog(QDialog):
    """Немодальное окно снимка (живёт вместе с главным)."""
    closed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('Фото — Pulse Scan')
        self.setWindowFlag(Qt.WindowMinMaxButtonsHint, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        self.canvas = PhotoCanvas()
        lay.addWidget(self.canvas, 1)
        self.hint = W.label('', 'Hint', wrap=True)
        lay.addWidget(self.hint)

    def set_photo(self, path, ow, oh):
        self.canvas.set_photo(path, ow, oh)
        w, h = self.canvas.fit_size(self.screen().availableGeometry().width() * 0.9,
                                    self.screen().availableGeometry().height() * 0.9)
        self.resize(w, h)

    def set_hint(self, text):
        self.hint.setText(text)
        self.hint.setVisible(bool(text))

    def closeEvent(self, ev):
        self.closed.emit()
        super().closeEvent(ev)


class PhotoPanel(_Panel):
    """Панель инструмента «Фото»: список снимков, привязка, совмещение, окраска."""

    action = Signal(str, object)

    def __init__(self):
        super().__init__('Фото', closable=True)
        # ── список фото ──
        s = self.add(W.Section('Снимки'))
        top = W.hbox(W.button('Добавить…', lambda: self.action.emit('add', None),
                              primary=True, icon=W.THEME.icon('mdi6.plus')),
                     W.button('Окно фото', lambda: self.action.emit('window', None)))
        s.add(top)
        self.photo_rows = QVBoxLayout()
        self.photo_rows.setSpacing(4)
        s.add(self.photo_rows)
        self.no_photos = W.label('добавьте фото (JPEG со штатива или с рук; панорамы 360° '
                                 'распознаются автоматически)', 'Hint', wrap=True)
        s.add(self.no_photos)
        # ── тип кадра ──
        st = self.add(W.Section('Тип кадра'))
        self.proj = QComboBox()
        self.proj.addItem('Обычное (пинхол)', 'pinhole')
        self.proj.addItem('Панорама 360°', 'pano360')
        self.proj.activated.connect(lambda _: self.action.emit('proj', self.proj.currentData()))
        st.add(W.field('Проекция', self.proj))
        # ── фокусное ──
        sf = self.add(W.Section('Фокусное (экв. 35 мм)'))
        self.f35 = QDoubleSpinBox()
        self.f35.setRange(8, 120)
        self.f35.setDecimals(1)
        self.f35.setSingleStep(1)
        self.f35.setSuffix(' мм')
        self.f35.setKeyboardTracking(False)
        self.f35.setAlignment(Qt.AlignRight)
        self.f35.valueChanged.connect(lambda _: self._f35_changed())
        self.f35_src = W.label('', 'Hint')
        sf.add(W.hbox(self.f35, None, self.f35_src))
        sf.add(W.button('Подобрать по парам', lambda: self.action.emit('f35_fit', None),
                        tip='по репроекции пар точек; нужно ≥4 некопланарных пары'))
        self.f35_hint = W.label('', 'Hint', wrap=True)
        sf.add(self.f35_hint)
        self.sec_focal = sf
        # ── пары точек ──
        sp = self.add(W.Section('Привязка парами'))
        self.cb_scanner = QCheckBox('камера у сканера:')
        self.cb_scanner.setChecked(True)
        self.cb_scanner.setToolTip('панораму держали рядом со штативом: центр камеры '
                                   'фиксирован, хватает 3 пар, поза надёжнее; в списке — '
                                   'у какого скана стоял штатив')
        self.cb_scanner.toggled.connect(lambda _: self._bind_enable())
        self.scanner_scan = QComboBox()
        self.scanner_scan.setToolTip('скан, рядом со штативом которого держали панораму')
        self.row_scanner = W.hbox(self.cb_scanner, self.scanner_scan)
        sp.add(self.row_scanner)
        self.b_pair_mode = W.button('Добавить точку',
                                     lambda: self.action.emit('pair_mode', None),
                                     tip='щёлкните характерную точку на фото, затем ту же точку в 3D-виде')
        self.b_pair_mode.setCheckable(True)
        self.b_pair_mode.toggled.connect(lambda on: self.b_pair_mode.setText(
            'Клик по фото, затем по 3D' if on else 'Добавить точку'))
        sp.add(self.b_pair_mode)
        self.pair_rows = QVBoxLayout()
        self.pair_rows.setSpacing(4)
        sp.add(self.pair_rows)
        self.no_pairs = W.label('пар нет', 'Hint', wrap=True)
        sp.add(self.no_pairs)
        self.b_bind = W.button('Привязать', lambda: self.action.emit('bind', None), primary=True)
        sp.add(self.b_bind)
        self.bind_status = W.label('', 'Hint', wrap=True)
        sp.add(self.bind_status)
        # ── совмещение поверхности ──
        sh = self.add(W.Section('Совмещение поверхности'))
        self.el = QComboBox()
        self.el.activated.connect(lambda _: self.action.emit('el_sel', self.el.currentData()))
        sh.add(W.field('Поверхность (плоскость)', self.el))
        self.b_align = W.button('Совместить по фото', lambda: self.action.emit('align', None),
                                tip='слой поверхности на фото; тяните ручки: '
                                    'середины рёбер — масштаб/положение, углы — перспектива')
        self.b_align.setCheckable(True)
        sh.add(self.b_align)
        self.cb_magnet = QCheckBox('магнит OpenCV (кнопка «Уточнить по краям»)')
        self.cb_magnet.setChecked(True)
        sh.add(self.cb_magnet)
        b_r = W.hbox(W.button('Уточнить по краям', lambda: self.action.emit('refine_edges', None),
                              tip='один проход: сдвиг каждой ручки ≤ ±28 px экрана, '
                                  'по совпадению границы поверхности с краями фото'),
                     W.button('Применить позу', lambda: self.action.emit('apply_h', None),
                              primary=True))
        sh.add(b_r)
        self.h_status = W.label('', 'Hint', wrap=True)
        sh.add(self.h_status)
        self.sec_align = sh
        # ── окраска ──
        self.b_paint = W.button('Окрасить облака', lambda: self.action.emit('paint', None))
        self.b_paint.setCheckable(True)
        self.add_foot(self.b_paint)
        self.finish()
        self.focus(None)
        self._f35_sig = False
        self._pano = False
        self._pairs_count = 0

    def set_pano_mode(self, pano):
        """Режим панели под тип кадра: у панорамы 360° нет фокусного и ручек
        поверхностей, зато есть «камера у сканера»."""
        self._pano = bool(pano)
        i = self.proj.findData('pano360' if pano else 'pinhole')
        if i >= 0:
            self.proj.blockSignals(True)
            self.proj.setCurrentIndex(i)
            self.proj.blockSignals(False)
        self.sec_focal.setVisible(not pano)
        self.sec_align.setVisible(not pano)
        self.row_scanner.setVisible(pano)
        self._bind_enable()

    def set_scans(self, scans, current_id):
        """Список размещённых сканов для «камера у сканера» (id → позиция сканера
        в якорном каноне при привязке); выбор сохраняется, пока скан существует."""
        keep = self.scanner_scan.currentData()
        self.scanner_scan.blockSignals(True)
        self.scanner_scan.clear()
        for sid, title in scans:
            self.scanner_scan.addItem(title, sid)
        i = self.scanner_scan.findData(keep if keep in [s[0] for s in scans] else current_id)
        if i >= 0:
            self.scanner_scan.setCurrentIndex(i)
        self.scanner_scan.blockSignals(False)

    def scanner_scan_id(self):
        """Выбранный скан для «камера у сканера» (None — пусто)."""
        return self.scanner_scan.currentData()

    # ── обновление из MainWindow ─────────────────────────────────────────
    def set_photos(self, photos, current_id, missing=(), colors=None):
        while self.photo_rows.count():
            w = self.photo_rows.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self.no_photos.setVisible(not photos)
        colors = colors or {}
        for ph_ in photos:
            self.photo_rows.addWidget(_PhotoRow(ph_, ph_['id'] == current_id,
                                                ph_.get('name') in missing, self,
                                                colors.get(ph_['id'])))

    def set_pairs(self, pairs):
        while self.pair_rows.count():
            w = self.pair_rows.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self.no_pairs.setVisible(not pairs)
        self._pairs_count = len(pairs)
        self._bind_enable()
        for i, pr in enumerate(pairs):
            self.pair_rows.addWidget(_PairRow(i, pr, self))

    def at_scanner(self):
        """«Камера у сканера» (только для панорам): центр = опорный сканер."""
        return self._pano and self.cb_scanner.isChecked()

    def _bind_enable(self):
        """«Привязать»: панораме у сканера хватает 3 пар, остальным — 4."""
        self.b_bind.setEnabled(self._pairs_count >= (3 if self.at_scanner() else 4))

    def set_f35(self, f35, src, note=''):
        self._f35_sig = True
        self.f35.setValue(float(f35) if f35 else 26.0)
        self._f35_sig = False
        ru = {'exif': 'EXIF', 'manual': 'вручную', 'auto': 'подобрано'}.get(src, '—')
        self.f35_src.setText(ru)
        self.f35_hint.setText(note)

    def set_f35_enabled(self, on):
        self.f35.setEnabled(on)

    def set_elements(self, planes, current_id):
        self.el.clear()
        for el in planes:
            self.el.addItem(el.get('name') or el['id'], el['id'])
        i = self.el.findData(current_id)
        if i >= 0:
            self.el.setCurrentIndex(i)

    def set_bind_status(self, text):
        self.bind_status.setText(text)
        self.bind_status.setVisible(bool(text))

    def set_h_status(self, text):
        self.h_status.setText(text)
        self.h_status.setVisible(bool(text))

    def set_pair_mode(self, on):
        self.b_pair_mode.blockSignals(True)
        self.b_pair_mode.setChecked(on)
        self.b_pair_mode.blockSignals(False)
        self.b_pair_mode.setText('Клик по фото, затем по 3D' if on else 'Добавить точку')

    def set_align_mode(self, on):
        self.b_align.blockSignals(True)
        self.b_align.setChecked(on)
        self.b_align.blockSignals(False)
        self.b_align.setText('Совмещать…' if on else 'Совместить по фото')

    def set_paint(self, on):
        self.b_paint.blockSignals(True)
        self.b_paint.setChecked(on)
        self.b_paint.blockSignals(False)

    def magnet_on(self):
        return self.cb_magnet.isChecked()

    def _f35_changed(self):
        if not self._f35_sig:
            self.action.emit('f35', self.f35.value())

    def state(self):
        return {'magnet': self.cb_magnet.isChecked()}

    def restore(self, d):
        if 'magnet' in (d or {}):
            self.cb_magnet.setChecked(bool(d['magnet']))


class _PhotoRow(QWidget):
    """Строка фото: имя, метки (привязано/потеряно), выбор."""

    def __init__(self, ph_, current, missing, panel, color=None):
        super().__init__()
        self.panel = panel
        self.pid = ph_['id']
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        name = W.label(ph_.get('name', '?'), 'Lbl')
        if current:
            name.setStyleSheet('font-weight: 600;')
        dot = QLabel(' ')
        dot.setFixedSize(10, 10)
        rgb = color or (200, 200, 200)
        dot.setStyleSheet(f'background: rgb({rgb[0]},{rgb[1]},{rgb[2]}); border-radius: 5px;')
        dot.setToolTip('цвет точек этого фото в 3D и на снимке')
        tags = []
        if ph_.get('pose') is not None:
            tags.append('привязано')
        if (ph_.get('proj') or 'pinhole') == 'pano360':
            tags.append('360°')
        elif ph_.get('f35') is None:
            tags.append('без фокусного')
        if missing:
            tags.append('файл не найден')
        sub = W.label(' · '.join(tags), 'Hint')
        lay.addWidget(W.hbox(dot, name, None))
        if missing:
            lay.addWidget(sub)
        else:
            lay.addWidget(W.hbox(
                W.button('окно', lambda p=ph_: panel.action.emit('window', p['id']), pad=True),
                W.button('×', lambda p=ph_: panel.action.emit('del', p['id']), pad=True),
                None, sub))
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.panel.action.emit('select', self.pid)


class _PairRow(QWidget):
    """Строка пары: (u, v) → (x, y, z), удаление."""

    def __init__(self, i, pr, panel):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        a = W.label(f'фото ({pr["u"]:.0f}, {pr["v"]:.0f}) px', 'Mono')
        b = W.label(f'облако ({pr["x"]:.2f}, {pr["y"]:.2f}, {pr["z"]:.2f}) м', 'Mono')
        lay.addWidget(W.hbox(a, None,
                             W.button('×', lambda i=i: panel.action.emit('pair_del', i), pad=True)))
        lay.addWidget(b)
