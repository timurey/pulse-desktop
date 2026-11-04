"""Правая панель вкладки «Контроль»: нулевой уровень, сечение, замеры."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QLabel, QDoubleSpinBox, QSlider,
                               QLineEdit)

import measure
from . import widgets as W
from .inspector import _Panel


def _num(v):
    """Число в поле ввода: без лишних нулей, точка → запятая не нужна (локаль ок)."""
    return f'{float(v):.3f}'


_fval = QDoubleValidator(-1e6, 1e6, 3)


class _MeasureRow(QFrame):
    """Строка замера: значение, детали; H — отметка в подписи, ✎ — правка
    названия, примечания и координат вершин числами (общая система, метры)."""

    def __init__(self, i, m, z0, panel):
        super().__init__()
        self.setObjectName('PairRow')
        self.i, self.m, self.z0, self.panel = i, m, z0, panel
        self._edit = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 4, 6)
        lay.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        num = QLabel(str(i + 1))
        num.setFixedSize(18, 18)
        num.setAlignment(Qt.AlignCenter)
        num.setStyleSheet('background:#ffd84a; color:#16181d; border-radius:5px; font-size:11px; font-weight:600;')
        top.addWidget(num, 0, Qt.AlignTop)
        txt = QVBoxLayout()
        txt.setSpacing(1)
        head = (m.get('name') or measure.TITLES.get(m['type'], m['type']))
        a = QLabel(head)
        a.setStyleSheet('font-size:12px; font-weight:600;')
        a.setWordWrap(True)
        txt.addWidget(a)
        val = measure.label(m, z0)
        if val:
            b = W.label(val, 'KV_v', wrap=True)
            txt.addWidget(b)
        for line in measure.details(m, z0)[1:]:
            c = W.label(line, 'Hint', wrap=True)
            txt.addWidget(c)
        if m.get('note'):
            txt.addWidget(W.label('примечание: ' + m['note'], 'Hint', wrap=True))
        top.addLayout(txt, 1)
        t = W.THEME
        top.addWidget(W.tool(t.icon('mdi6.alpha-h-circle-outline', 'ink3' if m.get('with_h') else 'line2'),
                             tip='Отметка H=±X.XXX первой точки в подписи', icon_only=True,
                             cb=lambda: panel.action.emit('measure_h', i)), 0, Qt.AlignTop)
        top.addWidget(W.tool(t.icon('mdi6.pencil-outline', 'ink3'), tip='Правка: название, примечание, координаты',
                             icon_only=True, cb=self._toggle_edit), 0, Qt.AlignTop)
        top.addWidget(W.tool(t.icon('mdi6.close', 'ink3'), tip='Удалить замер', icon_only=True,
                             cb=lambda: panel.action.emit('measure_delete', i)), 0, Qt.AlignTop)
        lay.addLayout(top)

    def _toggle_edit(self):
        if self._edit is None:
            self._edit = _MeasureEdit(self.m, self.i, self.panel)
            self.layout().addWidget(self._edit)
            self._edit.show()
        else:
            self._edit.setParent(None)
            self._edit.deleteLater()
            self._edit = None


class _MeasureEdit(QFrame):
    """Раскрытая правка замера: название, примечание, XYZ вершин, «Применить»."""

    def __init__(self, m, index, panel):
        super().__init__()
        self.m, self.index, self.panel = m, index, panel
        self.setObjectName('SecBody')
        g = QVBoxLayout(self)
        g.setContentsMargins(2, 2, 2, 4)
        g.setSpacing(4)
        self.name_e = QLineEdit(m.get('name', ''))
        self.name_e.setPlaceholderText('Название')
        g.addWidget(self.name_e)
        self.note_e = QLineEdit(m.get('note', ''))
        self.note_e.setPlaceholderText('Примечание')
        g.addWidget(self.note_e)
        self.pts = []
        for k, p in enumerate(m.get('pts', [])):
            row = QHBoxLayout()
            row.setSpacing(3)
            row.addWidget(QLabel(f'Т{k + 1}'))
            xyz = []
            for c in range(3):
                e = QLineEdit(_num(p[c]))
                e.setFixedWidth(64)
                e.setAlignment(Qt.AlignRight)
                e.setValidator(_fval)
                xyz.append(e)
                row.addWidget(e)
            self.pts.append(xyz)
            g.addLayout(row)
        g.addWidget(W.button('Применить', self._apply,
                             icon=W.THEME.icon('mdi6.check'),
                             tip='Enter в поле — тоже применить (правки координат попадают в Ctrl+Z)'))
        for e in [x for xyz in self.pts for x in xyz] + [self.name_e, self.note_e]:
            e.returnPressed.connect(self._apply)

    def _apply(self):
        m, panel = self.m, self.panel
        try:
            new_pts = [[[float(e.text().replace(',', '.')) for e in xyz] for xyz in self.pts]]
        except ValueError:
            return                                   # незаполненное поле — не применять
        if new_pts[0] == [list(map(float, p)) for p in m.get('pts', [])] \
                and self.name_e.text() == m.get('name', '') \
                and self.note_e.text() == m.get('note', ''):
            return                                   # ничего не поменялось
        panel.action.emit('measure_edit', {'index': self.index,
                                           'pts': new_pts[0],
                                           'name': self.name_e.text().strip(),
                                           'note': self.note_e.text().strip()})


class _BoxRow(QFrame):
    """Строка бокса в списке: глаз-чек (режет + каркас), имя, удаление."""

    def __init__(self, b, panel):
        super().__init__()
        self.setObjectName('PairRow')
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 4, 4)
        lay.setSpacing(6)
        self.eye = W.Switch(bool(b.get('on')))
        self.eye.toggled.connect(lambda v: panel.action.emit('box_on', (b['id'], v)))
        self.eye.setToolTip('Бокс режет облако (и каркас виден)')
        lay.addWidget(self.eye)
        name = QLabel(b.get('name', b['id']))
        name.setStyleSheet('font-size:12px;')
        name.setWordWrap(True)
        lay.addWidget(name, 1)
        lay.addWidget(W.tool(W.THEME.icon('mdi6.close', 'ink3'), tip='Удалить бокс', icon_only=True,
                             cb=lambda: panel.action.emit('box_delete', b['id'])))


class _BoxFields(QFrame):
    """Поля выбранного бокса: имя, центр, размер, поворот; «Применить»."""

    apply = Signal(dict)

    def __init__(self):
        super().__init__()
        self.setObjectName('SecBody')
        g = QVBoxLayout(self)
        g.setContentsMargins(2, 2, 2, 4)
        g.setSpacing(4)
        self.name_e = QLineEdit()
        g.addWidget(self.name_e)
        self.spins = {}
        row1 = QHBoxLayout()
        row1.setSpacing(3)
        row1.addWidget(QLabel('центр'))
        for k in ('x', 'y', 'z'):
            sb = QDoubleSpinBox()
            sb.setRange(-1e6, 1e6)
            sb.setDecimals(3)
            sb.setSingleStep(0.1)
            sb.setKeyboardTracking(False)
            row1.addWidget(sb)
            self.spins['c' + k] = sb
        g.addLayout(row1)
        row2 = QHBoxLayout()
        row2.setSpacing(3)
        row2.addWidget(QLabel('размер'))
        for k in ('x', 'y', 'z'):
            sb = QDoubleSpinBox()
            sb.setRange(0.05, 1e6)
            sb.setDecimals(2)
            sb.setSingleStep(0.1)
            sb.setMinimumWidth(72)
            sb.setKeyboardTracking(False)
            row2.addWidget(sb)
            self.spins['s' + k] = sb
        g.addLayout(row2)
        row3 = QHBoxLayout()
        row3.setSpacing(3)
        row3.addWidget(QLabel('поворот'))
        sb = QDoubleSpinBox()
        sb.setRange(-180, 180)
        sb.setDecimals(1)
        sb.setSuffix('°')
        sb.setKeyboardTracking(False)
        self.spins['yaw'] = sb
        row3.addWidget(sb)
        row3.addStretch(1)
        g.addLayout(row3)
        g.addWidget(W.button('Применить', self._apply, icon=W.THEME.icon('mdi6.check')))
        self.box_id = None

    def set_box(self, b):
        if b is None:
            self.box_id = None
            self.setVisible(False)
            return
        self.box_id = b['id']
        self.name_e.setText(b.get('name', ''))
        vals = {'cx': b['center'][0], 'cy': b['center'][1], 'cz': b['center'][2],
                'sx': b['size'][0], 'sy': b['size'][1], 'sz': b['size'][2],
                'yaw': b.get('yaw_deg', 0.0)}
        for k, sb in self.spins.items():
            sb.blockSignals(True)
            sb.setValue(float(vals[k]))
            sb.blockSignals(False)
        self.setVisible(True)

    def _apply(self):
        if self.box_id is None:
            return
        self.apply.emit({'id': self.box_id,
                         'name': self.name_e.text().strip(),
                         'center': [self.spins['cx'].value(), self.spins['cy'].value(), self.spins['cz'].value()],
                         'size': [self.spins['sx'].value(), self.spins['sy'].value(), self.spins['sz'].value()],
                         'yaw_deg': self.spins['yaw'].value()})


class _PlanRow(QFrame):
    """Строка плана: глаз — активен, имя, DXF, удаление."""

    def __init__(self, pl, active, panel):
        super().__init__()
        self.setObjectName('PairRow')
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 4, 4)
        lay.setSpacing(6)
        self.eye = W.Switch(bool(active))
        self.eye.toggled.connect(lambda v: panel.action.emit('plan_eye', (pl['id'], v)))
        self.eye.setToolTip('Активировать план (сечение + его замеры) / завершить')
        lay.addWidget(self.eye)
        name = QLabel(pl.get('name', pl['id']))
        name.setStyleSheet('font-size:12px;' + ('font-weight:600;' if active else ''))
        name.setWordWrap(True)
        lay.addWidget(name, 1)
        st = pl.get('state', {})
        if st.get('base') == 'z' or abs((st.get('n0') or [0, 0, 1])[2]) > 0.9:
            lay.addWidget(W.label(f"H={st.get('c', 0):.2f}", 'Hint'))
        lay.addWidget(W.tool(W.THEME.icon('mdi6.file-cad-box', 'ink3'), tip='Экспорт плана в DXF',
                             icon_only=True, cb=lambda: panel.action.emit('plan_dxf', pl['id'])))
        lay.addWidget(W.tool(W.THEME.icon('mdi6.close', 'ink3'), tip='Удалить план (замеры останутся)',
                             icon_only=True, cb=lambda: panel.action.emit('plan_delete', pl['id'])))


class ControlPanel(_Panel):
    action = Signal(str, object)
    MEASURES = [('расстояние', 'dist'), ('отметка', 'height'), ('до плоскости', 'plane'), ('ломаная', 'poly')]

    def __init__(self):
        from .tools import DoneBar
        super().__init__('Контроль', closable=True)
        t = W.THEME
        s0 = self.add(W.Section('Нулевой уровень'), 'zero')
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
        s0.add(W.hbox(W.button('По полу', lambda: self.action.emit('zero_floor', None),
                               icon=t.icon('mdi6.floor-plan'), tip='Пол опорного скана (по умолчанию)'),
                      W.button('Кликом', lambda: self.action.emit('zero_click', None),
                               icon=t.icon('mdi6.cursor-default-click-outline'),
                               tip='Щелчок по точке облака — её высота станет ±0.000'), spacing=6))
        s0.add(W.label('Высоту можно ввести числом в поле выше. Отметки замеров считаются от этого уровня.',
                       'Hint', wrap=True))
        sb = self.add(W.Section('Боксы отсечения'), 'cbox')
        self.box_list = QVBoxLayout()
        self.box_list.setSpacing(4)
        sb.add(self.box_list)
        self.box_empty = W.label('боксов нет — «Бокс» на ленте «Контроль» или «Кликом»', 'Hint', wrap=True)
        sb.add(self.box_empty)
        sb.add(W.hbox(W.button('Бокс по облаку', lambda: self.action.emit('box_gather', None),
                               icon=t.icon('mdi6.cube-outline'), tip='По габариту видимых сканов'),
                      W.button('Бокс кликом', lambda: self.action.emit('box_click', None),
                               icon=t.icon('mdi6.cursor-default-click-outline'), tip='Клик по модели — центр нового бокса'),
                      spacing=6))
        self.box_fields = _BoxFields()
        self.box_fields.apply.connect(lambda d: self.action.emit('box_edit', d))
        sb.add(self.box_fields)
        self.box_fields.setVisible(False)
        sp = self.add(W.Section('Планы'), 'plans')
        self.plan_list = QVBoxLayout()
        self.plan_list.setSpacing(4)
        sp.add(self.plan_list)
        self.plan_empty = W.label('планов нет — сохраните текущее сечение', 'Hint', wrap=True)
        sp.add(self.plan_empty)
        sp.add(W.hbox(W.button('Сохранить сечение', lambda: self.action.emit('plan_add', None),
                               icon=t.icon('mdi6.map-plus'), tip='Текущее сечение — новый план'),
                      W.button('Завершить план', lambda: self.action.emit('plan_done', None),
                               icon=t.icon('mdi6.map-marker-minus'),
                               tip='Выйти из плана: все замеры, обзор'), spacing=6))
        sp.add(W.label('Активный план: на сцене только его замеры, новые привязываются к нему; '
                       'камера — вид вдоль плоскости. DXF плана — в меню строки.', 'Hint', wrap=True))
        ss = self.add(W.Section('Сечение'), 'section')
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
        s1 = self.add(W.Section('Замеры'), 'measure')
        self.m_type = W.Segmented(self.MEASURES, 'dist')
        self.m_type.changed.connect(lambda v: self.action.emit('measure_type', v))
        s1.add(self.m_type)
        self.mode_hint = W.label('', 'KV_v', wrap=True)
        s1.add(self.mode_hint)
        self.snap_sw = W.Switch(True)
        self.snap_sw.toggled.connect(lambda v: self.action.emit('snap', v))
        s1.add(W.hbox(QLabel('Привязка: вершины, центры элементов (B)'), None, self.snap_sw))
        self.list = QVBoxLayout()
        self.list.setSpacing(6)
        s1.add(self.list)
        self.empty = W.label('замеров нет — выберите тип замера и щёлкайте по облаку', 'Hint', wrap=True)
        s1.add(self.empty)
        s1.add(W.hbox(W.button('Экспорт CSV…', lambda: self.action.emit('measures_csv', None),
                               icon=t.icon('mdi6.file-delimited-outline'), tip='Все замеры по вершинам (Excel)'),
                      W.button('Экспорт DXF…', lambda: self.action.emit('measures_dxf', None),
                               icon=t.icon('mdi6.file-cad-box'), tip='Замеры в 3D-DXF (метры, слои IZM_* — как у Оттиска)'),
                      spacing=6))
        s1.add(W.button('Очистить замеры', lambda: self.action.emit('measure_clear', None),
                        icon=t.icon('mdi6.delete-sweep-outline')))
        self.finish()
        for k in ('zero', 'section', 'measure'):
            b = self.add_foot(DoneBar(), k)
            b.done.connect(lambda k=k: self.action.emit('done', k))
        self.focus(None)

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

    def set_plans(self, plans, active_id):
        while self.plan_list.count():
            w = self.plan_list.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for pl in plans:
            self.plan_list.addWidget(_PlanRow(pl, pl['id'] == active_id, self))
        self.plan_empty.setVisible(not plans)

    def set_boxes(self, boxes, sel_id):
        """Список боксов + поля выбранного (sel_id или первый включённый)."""
        while self.box_list.count():
            w = self.box_list.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for b in boxes:
            self.box_list.addWidget(_BoxRow(b, self))
        self.box_empty.setVisible(not boxes)
        cur = next((b for b in boxes if b['id'] == sel_id), None) or \
            (boxes[0] if boxes else None)
        self.box_fields.set_box(cur)

    def set_measures(self, measures, z0):
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for i, m in enumerate(measures):
            self.list.addWidget(_MeasureRow(i, m, z0, self))
        self.empty.setVisible(not measures)
