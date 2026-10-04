"""Правая панель: инспектор (скан / группа / проект), ручная стыковка, чистка."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QWidget, QSlider,
                               QComboBox, QLabel, QCheckBox, QDoubleSpinBox)

from . import widgets as W


class _Panel(QFrame):
    """Заголовок + прокручиваемое содержимое + (необязательно) закреплённый низ."""
    closed = Signal()

    def __init__(self, title, closable=False):
        super().__init__()
        self.setObjectName('Panel')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QFrame()
        head.setObjectName('PHead')
        head.setFixedHeight(40)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(14, 0, 8, 0)
        self.title = W.label(title.upper(), 'PTitle')
        hl.addWidget(self.title)
        hl.addStretch(1)
        self.head_lay = hl
        if closable:
            hl.addWidget(W.tool(W.THEME.icon('mdi6.close'), tip='Закрыть (Esc)', cb=self.closed.emit,
                                icon_only=True))
        lay.addWidget(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setMinimumWidth(10)
        body = QWidget()
        body.setMinimumWidth(10)
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        self.scroll.setWidget(body)
        lay.addWidget(self.scroll, 1)
        self.foot = QVBoxLayout()
        self.foot.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.foot)

    def add(self, sec):
        self.body.addWidget(sec)
        return sec

    def finish(self):
        self.body.addStretch(1)


# ── инспектор ──────────────────────────────────────────────────────────────
class Inspector(_Panel):
    action = Signal(str)

    def __init__(self):
        super().__init__('Проект')
        t = W.THEME
        # заголовок объекта
        self.sec_head = self.add(W.Section())
        self.swatch = W.Swatch(size=12)
        self.name = W.label('', 'H1', wrap=True)
        self.sec_head.add(W.hbox(self.swatch, self.name, spacing=8))
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        self.chip_state = W.chip()
        self.chip_path = W.chip()
        self.chips.addWidget(self.chip_state)
        self.chips.addWidget(self.chip_path)
        self.chips.addStretch(1)
        self.sec_head.add(self.chips)
        # разделы «ключ — значение»
        self.kv = {}
        for key, title in (('project', 'Сводка'), ('source', 'Реконструкция из bag'),
                           ('analysis', 'Анализ'), ('links', 'Связи')):
            sec = self.add(W.Section(title))
            kv = W.KV()
            sec.add(kv)
            self.kv[key] = (sec, kv)
        # горизонт и план проекта
        self.sec_level = self.add(W.Section('Горизонт и план'))
        g = QGridLayout()
        g.setSpacing(6)
        g.addWidget(W.button('По горизонту', lambda: self.action.emit('level_project'),
                             icon=t.icon('mdi6.angle-acute'),
                             tip='Выровнять весь проект по полу и стенам (сохраняется в проекте)'), 0, 0)
        g.addWidget(W.button('Сбросить', lambda: self.action.emit('level_reset'),
                             icon=t.icon('mdi6.restore')), 0, 1)
        g.addWidget(W.button('План по стенам', lambda: self.action.emit('align_plan'),
                             icon=t.icon('mdi6.grid'),
                             tip='Повернуть план так, чтобы стены шли вдоль осей X/Y'), 1, 0, 1, 2)
        self.sec_level.add(g)
        self.plan_step = W.Segmented([('0.1°', 0.1), ('1°', 1.0), ('5°', 5.0)], 1.0)
        self.sec_level.add(W.hbox(W.label('Поворот плана'), None,
                                  W.tool(t.icon('mdi6.rotate-left'), tip='Повернуть план против часовой',
                                         cb=lambda: self.action.emit('rotate_plan+'), icon_only=True),
                                  W.tool(t.icon('mdi6.rotate-right'), tip='Повернуть план по часовой',
                                         cb=lambda: self.action.emit('rotate_plan-'), icon_only=True),
                                  self.plan_step))
        # действия
        self.sec_act = self.add(W.Section())
        self.act_lay = QGridLayout()
        self.act_lay.setSpacing(6)
        self.sec_act.add(self.act_lay)
        self.finish()

    def _actions(self, items):
        while self.act_lay.count():
            w = self.act_lay.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        t = W.THEME
        for i, (text, name, ic) in enumerate(items):
            b = W.button(text, lambda n=name: self.action.emit(n), icon=t.icon(ic))
            self.act_lay.addWidget(b, i // 2, i % 2)
        self.sec_act.setVisible(False)               # команды — на ленте

    def show_project(self, rows, has_scans):
        self.title.setText('ПРОЕКТ')
        self.sec_head.setVisible(False)
        self._kv('project', rows)
        for k in ('source', 'analysis', 'links'):
            self._kv(k, [])
        self.sec_level.setVisible(False)             # команды — на ленте («Стыковка»)
        self._actions([('Импорт со сканера', 'import_scanner', 'mdi6.access-point'),
                       ('Импорт bag', 'import_bags', 'mdi6.folder-open-outline'),
                       ('Добавить облако', 'add_scan', 'mdi6.plus'),
                       ('Автостыковка', 'auto', 'mdi6.graph-outline')] +
                      ([('Экспорт склейки', 'export', 'mdi6.export-variant')] if has_scans else []))

    def show_group(self, name, kind, n, rows):
        self.title.setText('ГРУППА')
        self.sec_head.setVisible(True)
        self.swatch.setVisible(False)
        self.name.setText(name)
        W.set_chip(self.chip_state, kind, 'mut')
        W.set_chip(self.chip_path, f'сканов: {n}', 'mut')
        self._kv('project', rows)
        for k in ('source', 'analysis', 'links'):
            self._kv(k, [])
        self.sec_level.setVisible(False)
        self._actions([('Переименовать', 'rename', 'mdi6.pencil-outline'),
                       ('Только эта ветка', 'only', 'mdi6.eye-check-outline'),
                       ('Новая подгруппа', 'new_group', 'mdi6.folder-plus-outline'),
                       ('Экспорт ветки', 'export_branch', 'mdi6.export-variant')])

    def show_scan(self, name, color, state, path, source, analysis, links, placed, is_ref):
        self.title.setText('СКАН')
        self.sec_head.setVisible(True)
        self.swatch.setVisible(True)
        self.swatch.set_color(color)
        self.name.setText(name)
        W.set_chip(self.chip_state, *state)
        W.set_chip(self.chip_path, path, 'mut')
        self._kv('project', [])
        self._kv('source', source)
        self._kv('analysis', analysis)
        self._kv('links', links)
        self.sec_level.setVisible(False)
        acts = []
        if placed and not is_ref:
            acts.append(('Сделать опорным', 'make_ref', 'mdi6.anchor'))
        if placed and not is_ref:
            acts.append(('Уточнить стыковку', 'refine', 'mdi6.auto-fix'))
        acts += [('Ручная стыковка', 'manual', 'mdi6.vector-combine'),
                 ('Кандидаты позы', 'candidates', 'mdi6.target'),
                 ('Чистка скана', 'clean', 'mdi6.selection-drag'),
                 ('Встать в точку', 'fly_to', 'mdi6.airplane'),
                 ('Экспорт скана', 'export_branch', 'mdi6.export-variant')]
        self._actions(acts)

    def _kv(self, key, rows):
        sec, kv = self.kv[key]
        kv.set(rows)
        sec.setVisible(bool(rows))


# ── ручная стыковка ────────────────────────────────────────────────────────
class _PairRow(QFrame):
    def __init__(self, n, text, color, on_remove=None):
        super().__init__()
        self.setObjectName('PairRow')
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 6, 6)
        lay.setSpacing(8)
        num = QLabel(str(n) if n else '…')
        num.setFixedSize(18, 18)
        num.setAlignment(Qt.AlignCenter)
        num.setStyleSheet(f'background:{color.name()}; color:#16181d; border-radius:5px; '
                          f'font-size:11px; font-weight:600;')
        lay.addWidget(num)
        l = QLabel(text)
        l.setWordWrap(True)
        l.setStyleSheet(f"font-size:12px; color:{W.THEME.q('ink2').name()};")
        lay.addWidget(l, 1)
        if on_remove is not None:
            lay.addWidget(W.tool(W.THEME.icon('mdi6.close', 'ink3'), tip='Удалить пару', cb=on_remove,
                                 icon_only=True))


class ManualPanel(_Panel):
    action = Signal(str, object)

    def __init__(self):
        super().__init__('Ручная стыковка', closable=True)
        t = W.THEME
        # сканы
        s1 = self.add(W.Section())
        self.fixed = QComboBox()
        self.moving = QComboBox()
        self.fixed.setToolTip('Неподвижный — уже размещённый скан')
        self.moving.setToolTip('Подвижный — скан, позу которого подбираем')
        g = QGridLayout()
        g.setSpacing(6)
        g.addWidget(W.field('Неподвижный', self.fixed), 0, 0)
        g.addWidget(W.field('Подвижный', self.moving), 0, 1)
        s1.add(g)
        s1.add(W.label('Ctrl/⌘ + клик по облаку: признак сначала в неподвижном, затем такой же '
                       'в подвижном. Shift + тянуть — сдвиг подвижного, Alt + тянуть — поворот.',
                       'Hint', wrap=True))
        self.fixed.activated.connect(lambda i: self.action.emit('restart', None))
        self.moving.activated.connect(lambda i: self.action.emit('restart', None))
        # пары объектов
        s2 = self.add(W.Section('Пары объектов'))
        self.kind = W.Segmented([('авто', 'auto'), ('плоскость', 'plane'), ('проём', 'opening'),
                                 ('точка', 'point')], 'auto')
        s2.add(W.field('Признак под курсором', self.kind))
        self.pairs_box = QVBoxLayout()
        self.pairs_box.setSpacing(6)
        s2.add(self.pairs_box)
        self.no_pairs = W.label('пар пока нет', 'Hint')
        s2.add(self.no_pairs)
        self.auto_solve = QCheckBox('Решать сразу, когда пар достаточно')
        self.auto_solve.setChecked(True)
        s2.add(self.auto_solve)
        g3 = QGridLayout()
        g3.setSpacing(6)
        g3.addWidget(W.button('Решить', lambda: self.action.emit('solve', None)), 0, 0)
        g3.addWidget(W.button('ICP', lambda: self.action.emit('icp', None),
                              tip='Уточнить позу по всем размещённым сканам'), 0, 1)
        g3.addWidget(W.button('Сбросить', lambda: self.action.emit('clear', None), tip='Удалить все пары'), 0, 2)
        s2.add(g3)
        s2.add(W.label('СТЕПЕНИ СВОБОДЫ', 'STitle'))
        dof = QGridLayout()
        dof.setSpacing(6)
        self.dof = {}
        for i, (k, text) in enumerate((('z', 'Z'), ('yaw', 'Поворот'), ('xy1', 'XY 1'),
                                       ('xy2', 'XY 2'))):
            l = QLabel(text)
            l.setAlignment(Qt.AlignCenter)
            l.setFixedHeight(40)
            l.setProperty('dof', 'no')
            dof.addWidget(l, 0, i)
            self.dof[k] = l
        s2.add(dof)
        self.dof_text = W.label('', 'Hint', wrap=True)
        s2.add(self.dof_text)
        # автоподгонка
        sf = self.add(W.Section('Подгонка'))
        self.fit_target = W.Segmented([('к неподвижному', 'fixed'), ('ко всем размещённым', 'all')], 'fixed')
        sf.add(self.fit_target)
        sf.add(W.label('«Автоподгонка» на ленте: захват до 60 см, затем 25 и 8 см; при закреплённой опорной '
                       'точке — только поворот вокруг неё.', 'Hint', wrap=True))
        self.fit_label = W.label('', 'KV_v', wrap=True)
        sf.add(self.fit_label)
        # опорная точка: совместить одну точку, закрепить, затем только поворачивать вокруг неё
        sp = self.add(W.Section('Опорная точка'))
        self.pivot_hint = W.label('«Опорная точка» на ленте, затем Ctrl/⌘ + клик: характерная точка в неподвижном, затем та же точка в '
                                  'подвижном — скан сдвинется, точка закрепится; дальше скан только '
                                  'поворачивается вокруг неё (транспортиры в 3D-виде тянутся мышью).',
                                  'Hint', wrap=True)
        sp.add(self.pivot_hint)
        self.pivot_box = QWidget()
        pl = QGridLayout(self.pivot_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setHorizontalSpacing(6)
        pl.setVerticalSpacing(6)
        self.angles = {}
        for i, (k, text, color) in enumerate((('yaw', 'Z · рыскание', '#5b9bff'), ('roll', 'X · крен', '#ff6b6b'),
                                              ('pitch', 'Y · тангаж', '#5fd38d'))):
            lab = QLabel(text)
            lab.setStyleSheet(f'color:{color}; font-size:12px;')
            sb = QDoubleSpinBox()
            sb.setRange(-180.0, 180.0)
            sb.setDecimals(3)
            sb.setSingleStep(0.1)
            sb.setSuffix('°')
            sb.setKeyboardTracking(False)
            sb.setAlignment(Qt.AlignRight)
            sb.valueChanged.connect(lambda v, k=k: self.action.emit('pivot_angle', (k, v)))
            minus = W.button('−', lambda k=k: self.action.emit('pivot_step', (k, -1)), pad=True)
            plus = W.button('+', lambda k=k: self.action.emit('pivot_step', (k, 1)), pad=True)
            for b_ in (minus, plus):
                b_.setFixedWidth(34)
            pl.addWidget(lab, i, 0)
            pl.addWidget(sb, i, 1)
            pl.addWidget(minus, i, 2)
            pl.addWidget(plus, i, 3)
            self.angles[k] = sb
        pl.setColumnStretch(1, 1)
        g4 = QGridLayout()
        g4.setSpacing(6)
        g4.addWidget(W.button('Снять точку', lambda: self.action.emit('pivot_clear', None),
                              icon=t.icon('mdi6.close')), 0, 0)
        pl.addLayout(g4, 3, 0, 1, 4)
        self.pivot_box.setVisible(False)
        sp.add(self.pivot_box)
        # подвижка
        s3 = self.add(W.Section())
        self.step_m = W.Segmented([('1 см', 0.01), ('5 см', 0.05), ('20 см', 0.2)], 0.05)
        self.step_deg = W.Segmented([('0.1°', 0.1), ('1°', 1.0), ('5°', 5.0)], 1.0)
        s3.add(W.label('ПОДВИЖКА', 'STitle'))
        s3.add(W.hbox(W.label('шаг сдвига'), None, self.step_m))
        s3.add(W.hbox(W.label('шаг поворота'), None, self.step_deg))
        pad = QGridLayout()
        pad.setSpacing(6)
        n = lambda **kw: (lambda: self.action.emit('nudge', kw))
        cells = [('mdi6.rotate-left', 'yaw', n(dyaw=1), 'повернуть против часовой'),
                 ('mdi6.arrow-up', 'Y+', n(dy=1), None),
                 ('mdi6.rotate-right', 'yaw', n(dyaw=-1), 'повернуть по часовой'),
                 ('mdi6.arrow-left', 'X−', n(dx=-1), None),
                 ('mdi6.target', 'ICP', lambda: self.action.emit('icp', None), 'уточнить ICP'),
                 ('mdi6.arrow-right', 'X+', n(dx=1), None),
                 ('mdi6.arrow-collapse-up', 'Z+', n(dz=1), 'выше'),
                 ('mdi6.arrow-down', 'Y−', n(dy=-1), None),
                 ('mdi6.arrow-collapse-down', 'Z−', n(dz=-1), 'ниже')]
        for i, (ic, text, cb, tip) in enumerate(cells):
            pad.addWidget(W.button(text, cb, icon=t.icon(ic), pad=True, tip=tip), i // 3, i % 3)
        s3.add(pad)
        tilt = QGridLayout()
        tilt.setSpacing(6)
        for i, (text, kw, tip) in enumerate((('крен −', {'droll': -1}, 'вокруг оси X'),
                                             ('крен +', {'droll': 1}, 'вокруг оси X'),
                                             ('тангаж −', {'dpitch': -1}, 'вокруг оси Y'),
                                             ('тангаж +', {'dpitch': 1}, 'вокруг оси Y'))):
            tilt.addWidget(W.button(text, n(**kw), pad=True, tip=tip), 0, i)
        s3.add(tilt)
        s3.add(W.hbox(W.button('По горизонту', lambda: self.action.emit('level_moving', None),
                               icon=t.icon('mdi6.angle-acute'), tip='Выровнять подвижный по полу и стенам'),
                      W.button('По стенам', lambda: self.action.emit('snap_yaw', None),
                               icon=t.icon('mdi6.grid'), tip='Довернуть подвижный, чтобы стены совпали')))
        self.full_rot = QCheckBox('Полный поворот при решении (учитывать наклон)')
        self.full_rot.setChecked(True)
        s3.add(self.full_rot)
        self.finish()
        # оценка позы — закреплена внизу панели
        s4 = W.Section('Оценка позы', sticky=True)
        self.score = W.KV()
        s4.add(self.score)
        self.b_cancel = W.button('Отмена', lambda: self.action.emit('cancel', None))
        self.b_accept = W.button('Принять позу', lambda: self.action.emit('accept', None), primary=True,
                                 icon=W.THEME.icon('mdi6.check', 'onaccent'))
        g2 = QGridLayout()
        g2.setSpacing(6)
        g2.addWidget(self.b_cancel, 0, 0)
        g2.addWidget(self.b_accept, 0, 1)
        s4.add(g2)
        self.foot.addWidget(s4)

    def set_scans(self, items, fixed, moving):
        """items: [(id, подпись, QColor, размещён)]."""
        for cb, cur, only_placed in ((self.fixed, fixed, True), (self.moving, moving, False)):
            cb.blockSignals(True)
            cb.clear()
            for sid, text, color, placed in items:
                if only_placed and not placed:
                    continue
                px = QPixmap(10, 10)
                px.fill(color)
                cb.addItem(QIcon(px), text, sid)
            i = cb.findData(cur)
            if i >= 0:
                cb.setCurrentIndex(i)
            cb.blockSignals(False)

    def current(self):
        return self.fixed.currentData(), self.moving.currentData()

    def set_pairs(self, rows, pending):
        """rows: [(текст, QColor)]; pending: (текст, QColor) или None."""
        while self.pairs_box.count():
            w = self.pairs_box.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for i, (text, color) in enumerate(rows):
            self.pairs_box.addWidget(_PairRow(i + 1, text, color,
                                              lambda i=i: self.action.emit('remove_pair', i)))
        if pending is not None:
            self.pairs_box.addWidget(_PairRow(0, pending[0] + '  ↔  ?', pending[1]))
        self.no_pairs.setVisible(not rows and pending is None)

    def set_dof(self, st):
        vals = {'z': st.get('z'), 'yaw': st.get('yaw'), 'xy1': st.get('xy_rank', 0) >= 1,
                'xy2': st.get('xy_rank', 0) >= 2}
        for k, l in self.dof.items():
            want = 'ok' if vals[k] else 'no'
            if l.property('dof') != want:
                l.setProperty('dof', want)
                W.restyle(l)
        self.dof_text.setText(st.get('text', ''))

    def set_pivot(self, state, angles=None, picking=False):
        """state: None — нет точки; 'A' — выбрана в неподвижном; 'fixed' — закреплена."""
        self.pivot_box.setVisible(state == 'fixed')
        self.pivot_hint.setVisible(state != 'fixed')
        if state == 'A':
            self.pivot_hint.setText('Точка в неподвижном выбрана. Ctrl/⌘ + клик по той же точке в подвижном.')
        elif picking:
            self.pivot_hint.setText('Ctrl/⌘ + клик: характерная точка в неподвижном скане.')
        elif state is None:
            self.pivot_hint.setText('«Опорная точка» на ленте, затем Ctrl/⌘ + клик: характерная точка в '
                                    'неподвижном, затем та же точка в подвижном — скан сдвинется, точка '
                                    'закрепится; дальше скан только поворачивается вокруг неё.')
        if angles is not None:
            for k, sb in self.angles.items():
                sb.blockSignals(True)
                sb.setValue(float(angles[k]))
                sb.blockSignals(False)

    def set_score(self, rows):
        self.score.set(rows)


# ── чистка ─────────────────────────────────────────────────────────────────
class CleanPanel(_Panel):
    action = Signal(str, object)

    def __init__(self):
        super().__init__('Чистка', closable=True)
        s1 = self.add(W.Section('Скан'))
        self.scan = QComboBox()
        self.scan.activated.connect(lambda i: self.action.emit('scan', self.scan.currentData()))
        s1.add(self.scan)
        self.sw_clean = W.Switch(True)
        self.sw_clean.toggled.connect(lambda v: self.action.emit('clean', v))
        s1.add(W.hbox(QLabel('Убирать зеркальные отражения'), None, self.sw_clean))
        self.report = W.KV()
        s1.add(self.report)
        # движущиеся объекты: неподтверждённые точки
        sd = self.add(W.Section('Движущиеся объекты'))
        self.cb_cross = QCheckBox('между сканами (по текущим позам)')
        self.cb_cross.setChecked(True)
        self.cb_pass = QCheckBox('по полуоборотам (сканы из bag)')
        self.cb_pass.setChecked(True)
        sd.add(self.cb_cross)
        sd.add(self.cb_pass)
        self.dyn_slider = QSlider(Qt.Horizontal)
        self.dyn_slider.setRange(30, 95)
        self.dyn_slider.setValue(60)
        self.dyn_slider.setFocusPolicy(Qt.NoFocus)
        self.dyn_slider.setToolTip('Порог оценки: меньше — находит больше (и больше ложных)')
        self.dyn_value = W.label('0.60', 'KV_v')
        self.dyn_value.setFixedWidth(34)
        self.dyn_slider.valueChanged.connect(self._dyn_thr)
        sd.add(W.hbox(W.label('порог'), self.dyn_slider, self.dyn_value, spacing=6))
        self.sw_dyn_all = W.Switch(False)
        sd.add(W.hbox(QLabel('Все видимые сканы'), None, self.sw_dyn_all))
        self.dyn_label = W.label('', 'KV_v', wrap=True)
        sd.add(self.dyn_label)
        sd.add(W.label('«Найти» и «Удалить найденное» — на ленте; найденное — пурпурным, удаление отменяется Ctrl+Z. Сравнение сканов пропускает '
                       'большие плоскости (пол, стены): их расхождение — ошибка совмещения, см. слой '
                       '«Качество». «По полуоборотам» есть только у сканов, импортированных из bag этой '
                       'версией (иначе — переимпортируйте bag).', 'Hint', wrap=True))
        s2 = self.add(W.Section('Выделение прямоугольником'))
        self.sw_all = W.Switch(False)
        s2.add(W.hbox(QLabel('Все видимые сканы'), None, self.sw_all))
        s2.add(W.label('«Рамка» на ленте (R). Левая кнопка — прямоугольник, Shift — добавить. Удаляется всё в прямоугольнике '
                       'на всю глубину взгляда; вид — кубом навигации, масштаб — колесом.', 'Hint', wrap=True))
        self.sel_label = W.label('', 'KV_v')
        s2.add(self.sel_label)
        self.finish()

    def dyn_threshold(self):
        return self.dyn_slider.value() / 100.0

    def _dyn_thr(self, v):
        self.dyn_value.setText(f'{v / 100:.2f}')
        self.action.emit('dyn_thr', v / 100.0)

    def set_scans(self, items, current):
        self.scan.blockSignals(True)
        self.scan.clear()
        for sid, text, color in items:
            px = QPixmap(10, 10)
            px.fill(color)
            self.scan.addItem(QIcon(px), text, sid)
        i = self.scan.findData(current)
        if i >= 0:
            self.scan.setCurrentIndex(i)
        self.scan.blockSignals(False)
