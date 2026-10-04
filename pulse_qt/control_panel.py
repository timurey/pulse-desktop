"""Правая панель вкладки «Контроль»: нулевой уровень, сечение, замеры."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel, QDoubleSpinBox, QSlider

import measure
from . import widgets as W
from .inspector import _Panel


class _MeasureRow(QFrame):
    def __init__(self, i, m, z0, panel):
        super().__init__()
        self.setObjectName('PairRow')
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 4, 6)
        lay.setSpacing(6)
        num = QLabel(str(i + 1))
        num.setFixedSize(18, 18)
        num.setAlignment(Qt.AlignCenter)
        num.setStyleSheet('background:#ffd84a; color:#16181d; border-radius:5px; font-size:11px; font-weight:600;')
        lay.addWidget(num, 0, Qt.AlignTop)
        txt = QVBoxLayout()
        txt.setSpacing(1)
        a = QLabel(f"{measure.TITLES.get(m['type'], m['type'])}: {measure.label(m, z0)}")
        a.setStyleSheet('font-size:12px;')
        a.setWordWrap(True)
        txt.addWidget(a)
        for line in measure.details(m, z0)[1:]:
            b = W.label(line, 'Hint', wrap=True)
            txt.addWidget(b)
        lay.addLayout(txt, 1)
        lay.addWidget(W.tool(W.THEME.icon('mdi6.close', 'ink3'), tip='Удалить замер', icon_only=True,
                             cb=lambda: panel.action.emit('measure_delete', i)), 0, Qt.AlignTop)


class ControlPanel(_Panel):
    action = Signal(str, object)

    def __init__(self):
        super().__init__('Контроль')
        s0 = self.add(W.Section('Нулевой уровень'))
        self.zero_src = W.label('', 'Hint', wrap=True)
        s0.add(self.zero_src)
        self.zero_z = QDoubleSpinBox()
        self.zero_z.setRange(-1000, 1000)
        self.zero_z.setDecimals(3)
        self.zero_z.setSingleStep(0.01)
        self.zero_z.setSuffix(' м')
        self.zero_z.setKeyboardTracking(False)
        self.zero_z.setAlignment(Qt.AlignRight)
        self.zero_z.setToolTip('Высота нулевого уровня в общей системе (Z опорного скана)')
        self.zero_z.valueChanged.connect(lambda v: self.action.emit('zero_set', v))
        s0.add(W.hbox(W.label('высота, z'), None, self.zero_z))
        self.sw_zero = W.Switch(True)
        self.sw_zero.toggled.connect(lambda v: self.action.emit('zero_show', v))
        s0.add(W.hbox(QLabel('Показывать уровень и сетку на нём'), None, self.sw_zero))
        s0.add(W.label('«По полу», «Кликом», «Числом» — на ленте. Отметки замеров считаются от этого уровня.',
                       'Hint', wrap=True))
        ss = self.add(W.Section('Сечение'))
        self.sec_mode = W.Segmented([('выкл.', 'off'), ('срез', 'slice'), ('отсечение', 'clip')], 'off')
        self.sec_mode.changed.connect(lambda v: self.action.emit('sec', {'mode': v}))
        ss.add(self.sec_mode)
        self.sec_base = W.Segmented([('гориз.', 'z'), ('по X', 'x'), ('по Y', 'y'), ('плоскость', 'plane')], 'z')
        self.sec_base.changed.connect(lambda v: self.action.emit('sec_base', v))
        ss.add(W.field('Основа плоскости', self.sec_base))
        self.sec_pos = QDoubleSpinBox()
        self.sec_pos.setRange(-1000, 1000)
        self.sec_pos.setDecimals(3)
        self.sec_pos.setSingleStep(0.05)
        self.sec_pos.setSuffix(' м')
        self.sec_pos.setKeyboardTracking(False)
        self.sec_pos.setAlignment(Qt.AlignRight)
        self.sec_pos.valueChanged.connect(lambda v: self.action.emit('sec_pos', v))
        self.sec_pos_lbl = W.label('отметка')
        ss.add(W.hbox(self.sec_pos_lbl, None, self.sec_pos))
        self.sec_slider = QSlider(Qt.Horizontal)
        self.sec_slider.setRange(0, 1000)
        self.sec_slider.setFocusPolicy(Qt.NoFocus)
        self.sec_slider.valueChanged.connect(lambda v: self.action.emit('sec_slider', v / 1000.0))
        ss.add(self.sec_slider)
        self.sec_thick = QDoubleSpinBox()
        self.sec_thick.setRange(0.5, 200)
        self.sec_thick.setDecimals(1)
        self.sec_thick.setSingleStep(1)
        self.sec_thick.setSuffix(' см')
        self.sec_thick.setKeyboardTracking(False)
        self.sec_thick.setAlignment(Qt.AlignRight)
        self.sec_thick.valueChanged.connect(lambda v: self.action.emit('sec', {'thick': v / 100.0}))
        self.sec_thick_row = W.hbox(W.label('толщина среза'), None, self.sec_thick)
        ss.add(self.sec_thick_row)
        self.sec_flip = W.Switch(False)
        self.sec_flip.toggled.connect(lambda v: self.action.emit('sec', {'flip': v}))
        self.sec_flip_row = W.hbox(QLabel('Показывать другую сторону'), None, self.sec_flip)
        ss.add(self.sec_flip_row)
        self.sec_angles = {}
        for k in ('a', 'b'):
            sb = QDoubleSpinBox()
            sb.setRange(-180, 180)
            sb.setDecimals(2)
            sb.setSingleStep(0.5)
            sb.setSuffix('°')
            sb.setKeyboardTracking(False)
            sb.setAlignment(Qt.AlignRight)
            sb.valueChanged.connect(lambda v, k=k: self.action.emit('sec', {k: v}))
            lab = W.label('')
            ss.add(W.hbox(lab, None, sb))
            self.sec_angles[k] = (lab, sb)
        ss.add(W.button('Сбросить поворот', lambda: self.action.emit('sec', {'a': 0.0, 'b': 0.0})))
        self.sec_desc = W.label('', 'Hint', wrap=True)
        ss.add(self.sec_desc)
        s1 = self.add(W.Section('Замеры'))
        self.mode_hint = W.label('', 'KV_v', wrap=True)
        s1.add(self.mode_hint)
        self.list = QVBoxLayout()
        self.list.setSpacing(6)
        s1.add(self.list)
        self.empty = W.label('замеров нет — выберите инструмент на ленте («Расстояние», «Отметка», «До плоскости», '
                             '«Ломаная»), затем щёлкайте по облаку', 'Hint', wrap=True)
        s1.add(self.empty)
        self.finish()

    def set_zero(self, z, source, show):
        self.zero_z.blockSignals(True)
        self.zero_z.setValue(z)
        self.zero_z.blockSignals(False)
        self.zero_src.setText(source)
        self.sw_zero.setChecked(show)

    def set_section(self, st, pos, pos_label, frac, angle_labels, desc):
        """Состояние сечения в поля (без обратных сигналов)."""
        widgets = [self.sec_pos, self.sec_slider, self.sec_thick] + [sb for _, sb in self.sec_angles.values()]
        for w in widgets:
            w.blockSignals(True)
        for seg, val in ((self.sec_mode, st['mode']), (self.sec_base, st['base'])):
            seg.group.blockSignals(True)
            seg.group.button(seg.values.index(val)).setChecked(True)
            seg.group.blockSignals(False)
        self.sec_pos.setValue(pos)
        self.sec_pos_lbl.setText(pos_label)
        self.sec_slider.setValue(int(round(1000 * min(1, max(0, frac)))))
        self.sec_thick.setValue(st['thick'] * 100)
        self.sec_flip.setChecked(bool(st.get('flip')))
        for (k, (lab, sb)), text in zip(self.sec_angles.items(), angle_labels):
            lab.setText(text)
            sb.setValue(st[k])
        for w in widgets:
            w.blockSignals(False)
        self.sec_thick_row.setVisible(st['mode'] == 'slice')
        self.sec_flip_row.setVisible(st['mode'] == 'clip')
        self.sec_desc.setText(desc)

    def set_measures(self, measures, z0):
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for i, m in enumerate(measures):
            self.list.addWidget(_MeasureRow(i, m, z0, self))
        self.empty.setVisible(not measures)
