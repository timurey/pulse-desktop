"""Нижняя панель: пары (рёбра графа), циклы, кандидаты позы, журнал, качество, связи (дерево поз)."""

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QTabBar, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView, QPlainTextEdit, QMenu,
                               QWidget, QComboBox, QTreeWidget, QTreeWidgetItem, QSplitter, QLabel,
                               QScrollArea)

from . import widgets as W
from PySide6.QtGui import QPixmap, QPainter, QIcon

from .theme import mono_font


def _table(headers, numeric=()):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setShowGrid(False)
    t.setFocusPolicy(Qt.NoFocus)
    t.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    t.verticalHeader().setDefaultSectionSize(30)
    h = t.horizontalHeader()
    h.setHighlightSections(False)
    h.setSectionResizeMode(QHeaderView.ResizeToContents)
    h.setStretchLastSection(True)
    for c in numeric:
        it = t.horizontalHeaderItem(c)
        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return t


def _cell(text, num=False, mono=True):
    it = QTableWidgetItem(text)
    if mono:
        it.setFont(mono_font(12))
    it.setTextAlignment((Qt.AlignRight if num else Qt.AlignLeft) | Qt.AlignVCenter)
    return it


class Dock(QFrame):
    pairSelected = Signal(int)
    pairAction = Signal(str, int)
    candSearch = Signal(str)
    candSelected = Signal(int)
    candAction = Signal(str, int)
    autoRequested = Signal()
    qualitySelected = Signal(int)
    linkSelected = Signal(str)
    linkAction = Signal(str, object)
    TAB_LINKS = 5
    PAIR_TIPS = [
        'Два скана, взаимную позу которых нашла автостыковка или задали вы.',
        '<b>Оценка</b> — насколько хорошо сканы совпадают в найденной позе: доля точек одного скана '
        'ближе 5 см к другому минус 8 × доля нарушений.<br>'
        '<b>&gt; 0,5</b> — уверенное совпадение; <b>0,05…0,5</b> — частичное перекрытие (мало общих стен); '
        '<b>&lt; 0,05</b> — автоматически не принимается; отрицательная — поза противоречит сканам.',
        '<b>Нарушения</b> — доля точек, попавших туда, где другой скан видел пустое пространство '
        '(луч прошёл сквозь это место). При верной позе почти ноль.<br>'
        '<b>≤ 0,02</b> (2 %) — норма; до 0,05 допускается у сильной пары (между съёмками что-то '
        'сдвинули); больше — поза почти наверняка неверна.',
        '<b>Отрыв</b> — насколько лучшая поза лучше второй по оценке (однозначность решения).<br>'
        '<b>&gt; 0,5</b> — решение однозначное; <b>&lt; 0,1</b> — есть почти такая же другая поза '
        '(симметричная комната, коридор), автоматически не принимается.',
        '<b>RMSE</b> — средняя ошибка точек после точной подгонки (ICP) по совпавшим точкам.<br>'
        '<b>1–4 см</b> — норма для сканера; больше — сканы совпали неплотно. Сам по себе не говорит, '
        'что поза верна: смотрите оценку и нарушения.',
        '<b>Метод</b> — как найдена поза:<br>'
        '<b>плоскости+ICP</b> — автостыковка: гипотезы по стенам и полу, затем подгонка ICP;<br>'
        '<b>группы</b> — стыковка групп между собой (по объединённым облакам);<br>'
        '<b>ручная / автоподгонка / кандидат</b> — поза задана вами («Принять позу»).',
        '<b>Состояние</b> — используется ли пара для поз сканов:<br>'
        '<b>активно</b> — автоматика приняла; <b>неактивно</b> — не прошла проверку;<br>'
        '<b>принято</b> / <b>отклонено</b> — ваше решение; <b>вручную</b> — ваша ручная связь.<br>'
        'Наведите на ячейку, чтобы увидеть причину.',
        '']

    def __init__(self):
        super().__init__()
        self.setObjectName('Dock')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QFrame()
        head.setObjectName('DockTabs')
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 0, 10, 0)
        hl.setSpacing(8)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.setFocusPolicy(Qt.NoFocus)
        t = W.THEME
        for ic, text in (('mdi6.graph-outline', 'Пары'), ('mdi6.sync', 'Циклы'),
                         ('mdi6.target', 'Кандидаты'), ('mdi6.text-box-outline', 'Журнал'),
                         ('mdi6.texture-box', 'Качество'), ('mdi6.family-tree', 'Связи')):
            self.tabs.addTab(t.icon(ic, 'ink3'), text)
        hl.addWidget(self.tabs)
        hl.addStretch(1)
        self.loops_chip = W.chip('', 'ok')
        hl.addWidget(self.loops_chip)
        lay.addWidget(head)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.tabs.currentChanged.connect(self.stack.setCurrentIndex)
        # пары
        self.pairs = _table(['Пара', 'Оценка', 'Нарушения', 'Отрыв', 'RMSE', 'Метод', 'Состояние', ''],
                            numeric=(1, 2, 3, 4))
        for c, tip in enumerate(self.PAIR_TIPS):
            if tip:
                self.pairs.horizontalHeaderItem(c).setToolTip(tip)
        self.pairs.horizontalHeader().setMinimumSectionSize(64)
        self.pairs.horizontalHeader().setSectionResizeMode(6, QHeaderView.Fixed)
        self.pairs.setColumnWidth(6, 120)
        self.pairs.itemSelectionChanged.connect(self._pair_sel)
        self.pairs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.pairs.customContextMenuRequested.connect(self._pair_menu)
        self.pairs.cellClicked.connect(self._pair_click)
        self.pairs_empty = W.label('Пар нет. «Автостыковка» на панели инструментов подберёт пары '
                                   'и позы сканов.', 'Hint', wrap=True)
        self.pairs_empty.setAlignment(Qt.AlignCenter)
        pw = QWidget()
        pl = QVBoxLayout(pw)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self.pairs)
        pl.addWidget(self.pairs_empty)
        self.stack.addWidget(pw)
        # циклы
        self.loops = _table(['Цикл', 'Невязка, см', 'Угол, °', 'Состояние'], numeric=(1, 2))
        self.stack.addWidget(self.loops)
        # кандидаты
        cw = QWidget()
        cl = QVBoxLayout(cw)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(6)
        self.cand_scan = QComboBox()
        self.cand_scan.setMinimumWidth(180)
        bl.addWidget(W.label('Скан'))
        bl.addWidget(self.cand_scan)
        bl.addWidget(W.button('Искать', lambda: self.candSearch.emit(self.cand_scan.currentData() or ''),
                              icon=t.icon('mdi6.magnify')))
        bl.addStretch(1)
        bl.addWidget(W.button('Доработать вручную', lambda: self.candAction.emit('manual', self._cand_row())))
        bl.addWidget(W.button('Принять', lambda: self.candAction.emit('accept', self._cand_row()), primary=True))
        cl.addWidget(bar)
        self.cands = _table(['№', 'Оценка', 'Совпало точек', 'Нарушения', 'Метод'], numeric=(1, 2, 3))
        self.cands.itemSelectionChanged.connect(lambda: self.candSelected.emit(self._cand_row()))
        cl.addWidget(self.cands, 1)
        self.stack.addWidget(cw)
        # журнал
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono_font(11))
        self.log.setFrameShape(QFrame.NoFrame)
        self.log.setStyleSheet('background: transparent; padding: 6px 10px;')
        self.stack.addWidget(self.log)
        # качество совмещения
        qw = QWidget()
        ql = QVBoxLayout(qw)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.setSpacing(0)
        self.qual = _table(['№', 'Поверхность', 'Макс., см', 'Медиана, см', 'Площадь, м²', 'Сканы'],
                           numeric=(2, 3, 4))
        self.qual.itemSelectionChanged.connect(lambda: self.qualitySelected.emit(self._row(self.qual)))
        self.qual.cellClicked.connect(lambda r, c: self.qualitySelected.emit(r))
        self.qual_empty = W.label('Включите слой «Качество совмещения» в панели слоёв: здесь появятся '
                                  'места, где поверхность из разных сканов толще порога.', 'Hint', wrap=True)
        self.qual_empty.setAlignment(Qt.AlignCenter)
        ql.addWidget(self.qual, 1)
        ql.addWidget(self.qual_empty, 1)
        self.qual.setVisible(False)
        self.stack.addWidget(qw)
        # связи: от какого скана получена поза
        sp = QSplitter(Qt.Horizontal)
        sp.setChildrenCollapsible(False)
        self.links = QTreeWidget()
        self.links.setColumnCount(5)
        self.links.setHeaderLabels(['Скан', 'Поза от', 'Связь', 'Расхождение', 'Ещё связей'])
        self.links.setRootIsDecorated(True)
        self.links.setUniformRowHeights(True)
        self.links.setFocusPolicy(Qt.NoFocus)
        self.links.setFrameShape(QFrame.NoFrame)
        lh = self.links.header()
        lh.setSectionResizeMode(QHeaderView.ResizeToContents)
        lh.setStretchLastSection(True)
        self.links.itemSelectionChanged.connect(self._link_sel)
        self.links.setContextMenuPolicy(Qt.CustomContextMenu)
        self.links.customContextMenuRequested.connect(self._link_menu)
        sp.addWidget(self.links)
        self.link_info = QLabel('')
        self.link_info.setObjectName('Hint')
        self.link_info.setWordWrap(True)
        self.link_info.setTextFormat(Qt.RichText)
        self.link_info.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.link_info.setContentsMargins(12, 8, 12, 8)
        self.link_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setWidget(self.link_info)
        sp.addWidget(sc)
        self.link_info.setMinimumWidth(300)
        sp.setStretchFactor(0, 1)
        sp.setStretchFactor(1, 1)
        sp.setSizes([700, 500])
        self._link_rows = {}
        self.stack.addWidget(sp)

    # ── пары ─────────────────────────────────────────────────────────────
    def set_pairs(self, rows):
        """
        rows: [(пара, оценка, нарушения, отрыв, rmse, метод, (состояние, вид чипа)[, {колонка: подсказка}])].
        """
        t = self.pairs
        sel = self.selected_pair()
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            tips = row[7] if len(row) > 7 else {}
            for c in range(6):
                it = _cell(row[c], num=c in (1, 2, 3, 4), mono=c != 5)
                if tips.get(c):
                    it.setToolTip(tips[c])
                t.setItem(r, c, it)
            st = W.hbox(W.chip(*row[6]), None, margins=(10, 0, 0, 0))
            if tips.get(6):
                st.setToolTip(tips[6])
                for ch in st.findChildren(QLabel):
                    ch.setToolTip(tips[6])
            t.setCellWidget(r, 6, st)
            more = W.tool(W.THEME.icon('mdi6.dots-horizontal', 'ink3'), tip='Действия с парой',
                          cb=lambda r=r: self._pair_menu_at(r), icon_only=True)
            t.setCellWidget(r, 7, W.hbox(None, more, margins=(0, 0, 6, 0)))
        if 0 <= sel < len(rows):
            t.selectRow(sel)
        self.tabs.setTabText(0, f'Пары · {len(rows)}')
        self.pairs.setVisible(bool(rows))
        self.pairs_empty.setVisible(not rows)

    def selected_pair(self):
        r = self.pairs.selectionModel().selectedRows() if self.pairs.selectionModel() else []
        return r[0].row() if r else -1

    def _pair_sel(self):
        i = self.selected_pair()
        if i >= 0:
            self.pairSelected.emit(i)

    def _pair_click(self, r, c):
        if r == self.selected_pair():
            self.pairSelected.emit(r)

    def _pair_menu(self, pos):
        r = self.pairs.rowAt(pos.y())
        if r >= 0:
            self._pair_menu_at(r)

    def _pair_menu_at(self, r):
        t = W.THEME
        m = QMenu(self)
        for text, name, ic in (('Показать пару', 'show', 'mdi6.eye-outline'),
                               ('Принять', 'accept', 'mdi6.check'),
                               ('Отклонить', 'reject', 'mdi6.close'),
                               ('Как решит автоматика', 'auto', 'mdi6.auto-fix'),
                               ('Ручная стыковка этой пары', 'manual', 'mdi6.vector-combine')):
            a = m.addAction(t.icon(ic), text)
            a.triggered.connect(lambda _=False, n=name: self.pairAction.emit(n, r))
        from PySide6.QtGui import QCursor
        m.exec(QCursor.pos())

    def set_loops(self, rows, chip):
        t = self.loops
        t.setRowCount(len(rows))
        for r, (cyc, dt, deg, st) in enumerate(rows):
            t.setItem(r, 0, _cell(cyc))
            t.setItem(r, 1, _cell(dt, True))
            t.setItem(r, 2, _cell(deg, True))
            t.setCellWidget(r, 3, W.hbox(W.chip(*st), None, margins=(10, 0, 0, 0)))
        self.tabs.setTabText(1, f'Циклы · {len(rows)}')
        W.set_chip(self.loops_chip, *chip)

    # ── кандидаты ────────────────────────────────────────────────────────
    def set_cand_scans(self, items, current=None):
        self.cand_scan.blockSignals(True)
        self.cand_scan.clear()
        for sid, text in items:
            self.cand_scan.addItem(text, sid)
        i = self.cand_scan.findData(current)
        if i >= 0:
            self.cand_scan.setCurrentIndex(i)
        self.cand_scan.blockSignals(False)

    def set_candidates(self, rows):
        t = self.cands
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                t.setItem(r, c, _cell(v, num=c in (1, 2, 3), mono=c != 4))
        self.tabs.setTabText(2, f'Кандидаты · {len(rows)}' if rows else 'Кандидаты')

    def _cand_row(self):
        r = self.cands.selectionModel().selectedRows()
        return r[0].row() if r else -1

    # ── качество ─────────────────────────────────────────────────────────
    def set_quality(self, rows, empty_text=None):
        """rows: [(вид, макс, медиана, площадь, сканы)]."""
        t = self.qual
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            t.setItem(r, 0, _cell(str(r + 1)))
            t.setItem(r, 1, _cell(row[0], mono=False))
            for c, v in enumerate(row[1:4]):
                t.setItem(r, 2 + c, _cell(v, True))
            t.setItem(r, 5, _cell(row[4]))
        self.tabs.setTabText(4, f'Качество · {len(rows)}' if rows else 'Качество')
        t.setVisible(bool(rows))
        self.qual_empty.setVisible(not rows)
        if empty_text:
            self.qual_empty.setText(empty_text)

    @staticmethod
    def _row(table):
        r = table.selectionModel().selectedRows()
        return r[0].row() if r else -1

    # ── связи ────────────────────────────────────────────────────────────
    def set_links(self, rows, summary):
        """
        rows: [{'id', 'parent' (id строки или None), 'cols': 5 строк, 'state': 'ok'|'bad'|'mut',
                'info': текст (HTML) для правой части, 'links': [(номер связи, подпись)]}] —
        родитель раньше детей. summary — текст справа, пока строка не выбрана.
        """
        cur = self.selected_link()
        t = self.links
        t.blockSignals(True)
        t.clear()
        self._link_rows = {}
        items = {}
        th = W.THEME
        for row in rows:
            par = items.get(row['parent'])
            it = QTreeWidgetItem(par if par is not None else t, row['cols'])
            it.setData(0, Qt.UserRole, row['id'])
            if row.get('color') is not None:
                pm = QPixmap(10, 10)
                pm.fill(Qt.transparent)
                p = QPainter(pm)
                p.setRenderHint(QPainter.Antialiasing)
                p.setBrush(row['color'])
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(0, 0, 10, 10, 3, 3)
                p.end()
                it.setIcon(0, QIcon(pm))
            for c in (2, 3, 4):
                it.setFont(c, mono_font(12))
            if row.get('state') == 'bad':
                it.setForeground(3, th.q('bad'))
            elif row.get('state') == 'mut':
                for c in range(5):
                    it.setForeground(c, th.q('ink3'))
            items[row['id']] = it
            self._link_rows[row['id']] = row
        t.expandAll()
        t.blockSignals(False)
        self._link_summary = summary
        if cur in items:
            items[cur].setSelected(True)
            t.setCurrentItem(items[cur])
        self._link_sel(emit=False)
        n = sum(1 for r in rows if r.get('state') != 'mut')
        self.tabs.setTabText(self.TAB_LINKS, f'Связи · {n}' if rows else 'Связи')

    def selected_link(self):
        it = self.links.currentItem() if self.links.selectedItems() else None
        return it.data(0, Qt.UserRole) if it is not None else None

    def _link_sel(self, emit=True):
        k = self.selected_link()
        row = self._link_rows.get(k)
        self.link_info.setText(row['info'] if row else getattr(self, '_link_summary', ''))
        if emit and k:
            self.linkSelected.emit(k)

    def _link_menu(self, pos):
        it = self.links.itemAt(pos)
        if it is None:
            return
        row = self._link_rows.get(it.data(0, Qt.UserRole))
        if row is None:
            return
        t = W.THEME
        m = QMenu(self)
        uid = row['id']
        if row.get('edge') is not None:
            for text, name, ic in (('Показать пару', 'show', 'mdi6.eye-outline'),
                                   ('Ручная стыковка этой пары', 'manual', 'mdi6.vector-combine'),
                                   ('Отклонить эту связь', 'reject', 'mdi6.close')):
                a = m.addAction(t.icon(ic), text)
                a.triggered.connect(lambda _=False, n=name: self.linkAction.emit('pair_' + n, row['edge']))
        if row.get('links'):
            sm = m.addMenu(t.icon('mdi6.link-variant-off'), 'Отклонить связь…')
            for i, text in row['links']:
                a = sm.addAction(text)
                a.triggered.connect(lambda _=False, i=i: self.linkAction.emit('pair_reject', i))
        if row.get('placed'):
            m.addSeparator()
            a = m.addAction(t.icon('mdi6.anchor'), 'Сделать опорным')
            a.triggered.connect(lambda: self.linkAction.emit('ref', uid))
        if not m.isEmpty():
            from PySide6.QtGui import QCursor
            m.exec(QCursor.pos())

    # ── журнал ───────────────────────────────────────────────────────────
    def add_log(self, msg):
        self.log.appendPlainText(time.strftime('%H:%M:%S  ') + msg)
