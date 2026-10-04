"""Правая панель вкладки «Контроль»: нулевой уровень, замеры (позже — сечения)."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel, QDoubleSpinBox

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

    def set_measures(self, measures, z0):
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for i, m in enumerate(measures):
            self.list.addWidget(_MeasureRow(i, m, z0, self))
        self.empty.setVisible(not measures)
