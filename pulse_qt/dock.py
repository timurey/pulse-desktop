"""Нижняя панель: пары (рёбра графа), циклы, кандидаты позы, журнал."""

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QTabBar, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView, QPlainTextEdit, QMenu,
                               QWidget, QComboBox)

from . import widgets as W
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
                         ('mdi6.target', 'Кандидаты'), ('mdi6.text-box-outline', 'Журнал')):
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

    # ── пары ─────────────────────────────────────────────────────────────
    def set_pairs(self, rows):
        """rows: [(пара, оценка, нарушения, отрыв, rmse, метод, (состояние, вид чипа))]."""
        t = self.pairs
        sel = self.selected_pair()
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c in range(6):
                t.setItem(r, c, _cell(row[c], num=c in (1, 2, 3, 4), mono=c != 5))
            t.setCellWidget(r, 6, W.hbox(W.chip(*row[6]), None, margins=(10, 0, 0, 0)))
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

    # ── журнал ───────────────────────────────────────────────────────────
    def add_log(self, msg):
        self.log.appendPlainText(time.strftime('%H:%M:%S  ') + msg)
