"""Левая панель: дерево проекта (группы и сканы) и слои отображения."""

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
                               QAbstractItemView, QLabel, QFrame, QMenu, QToolButton, QLineEdit)

from scan_tree import Group
from . import widgets as W

ROLE = Qt.UserRole


def short(sid):
    """static_20260922_231157.e57 -> 231157"""
    from pathlib import Path
    stem = Path(sid).stem
    return stem.split('_')[-1] if '_' in stem else stem


class _Row(QWidget):
    """Строка дерева: глаз, цвет, имя, чип состояния."""

    def __init__(self, panel, key, text, visible, color=None, chip=None, group=False, count=None):
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 10, 0)
        lay.setSpacing(6)
        t = W.THEME
        self.eye = QToolButton()
        self.eye.setObjectName('IconBtn')
        self.eye.setFixedSize(22, 22)
        self.eye.setIconSize(QSize(16, 16))
        self.eye.setIcon(t.icon('mdi6.eye-outline' if visible else 'mdi6.eye-off-outline',
                                'ink3' if visible else 'line2'))
        self.eye.setStyleSheet('QToolButton{min-width:22px;max-width:22px;min-height:22px;max-height:22px}')
        self.eye.setToolTip('Показать / скрыть')
        self.eye.setFocusPolicy(Qt.NoFocus)
        self.eye.clicked.connect(lambda: panel.visibilityToggled.emit(key, not visible))
        lay.addWidget(self.eye)
        if color is not None:
            lay.addWidget(W.Swatch(color))
        name = QLabel(text)
        if group:
            name.setStyleSheet('font-weight: 500;')
        else:
            name.setObjectName('Mono')
        if not visible:
            name.setStyleSheet(name.styleSheet() + f"color: {W.THEME.q('ink3').name()};")
        lay.addWidget(name, 1)
        if count is not None:
            c = QLabel(str(count))
            c.setObjectName('Hint')
            lay.addWidget(c)
        if chip:
            lay.addWidget(W.chip(*chip))


class ProjectTree(QTreeWidget):
    moved = Signal(str, str)                       # ключ узла, id группы

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setAnimated(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

    def dropEvent(self, ev):
        src = self.currentItem()
        tgt = self.itemAt(ev.position().toPoint())
        ev.ignore()                                # дерево перестраивается из сеанса
        if src is None:
            return
        key = src.data(0, ROLE)
        if tgt is None:
            gid = 'root'
        else:
            tkey = tgt.data(0, ROLE)
            if tgt.data(0, ROLE + 1):              # группа
                gid = tkey
            else:
                p = tgt.parent()
                gid = p.data(0, ROLE) if p is not None else 'root'
        if gid != key:
            self.moved.emit(key, gid)


class TreePanel(QFrame):
    selected = Signal(object)                      # ключ узла или None
    visibilityToggled = Signal(str, bool)
    moved = Signal(str, str)
    action = Signal(str, object)                   # имя действия, ключ
    layerToggled = Signal(str, bool)

    LAYERS = [('planes', 'Поверхности', 'mdi6.layers-outline', False),
              ('openings', 'Проёмы', 'mdi6.window-closed-variant', False),
              ('ghosts', 'Отражения', 'mdi6.blur', False),
              ('grid', 'Сетка 1 м', 'mdi6.grid', True)]

    def __init__(self):
        super().__init__()
        self.setObjectName('PanelLeft')
        self.session = None
        self.status_of = None
        self.color_of = None
        self._items = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QFrame()
        head.setObjectName('PHead')
        head.setFixedHeight(40)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(14, 0, 8, 0)
        hl.addWidget(W.label('ПРОЕКТ', 'PTitle'))
        hl.addStretch(1)
        t = W.THEME
        self.b_group = W.tool(t.icon('mdi6.folder-plus-outline'), tip='Новая группа',
                              cb=lambda: self.action.emit('new_group', self.current()), icon_only=True)
        self.b_search = W.tool(t.icon('mdi6.magnify'), tip='Фильтр по имени', checkable=True, icon_only=True)
        hl.addWidget(self.b_group)
        hl.addWidget(self.b_search)
        lay.addWidget(head)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText('фильтр: часть имени скана')
        self.filter.setClearButtonEnabled(True)
        self.filter.setVisible(False)
        self.filter.textChanged.connect(lambda _: self.rebuild())
        self.b_search.toggled.connect(self._toggle_filter)
        fw = W.hbox(self.filter, margins=(10, 8, 10, 8))
        self._fw = fw
        fw.setVisible(False)
        lay.addWidget(fw)
        self.tree = ProjectTree(self)
        self.tree.itemSelectionChanged.connect(self._on_sel)
        self.tree.moved.connect(self.moved)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemDoubleClicked.connect(lambda it, c: self.action.emit('focus', it.data(0, ROLE)))
        lay.addWidget(self.tree, 1)
        self.empty = W.label('Нет сканов.\nИмпортируйте со сканера,\nиз bag или добавьте облако.',
                             'Hint', wrap=True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setContentsMargins(14, 30, 14, 30)
        lay.addWidget(self.empty, 1)
        layers = QFrame()
        layers.setObjectName('SecStick')
        ll = QVBoxLayout(layers)
        ll.setContentsMargins(14, 10, 14, 14)
        ll.setSpacing(8)
        ll.addWidget(W.label('СЛОИ', 'STitle'))
        self.switches = {}
        for key, text, ic, on in self.LAYERS:
            sw = W.Switch(on)
            sw.toggled.connect(lambda v, k=key: self.layerToggled.emit(k, v))
            icl = QLabel()
            icl.setPixmap(t.icon(ic).pixmap(18, 18))
            name = QLabel(text)
            name.setStyleSheet(f"color: {t.q('ink2').name()};")
            ll.addWidget(W.hbox(icl, name, None, sw, spacing=8))
            self.switches[key] = sw
        lay.addWidget(layers)

    def _toggle_filter(self, on):
        self._fw.setVisible(on)
        if on:
            self.filter.setFocus()
        else:
            self.filter.clear()

    def layer(self, key):
        return self.switches[key].isChecked()

    def current(self):
        it = self.tree.currentItem()
        return it.data(0, ROLE) if it is not None and it.isSelected() else None

    def select(self, key):
        it = self._items.get(key)
        if it is not None:
            self.tree.setCurrentItem(it)

    def _on_sel(self):
        self.selected.emit(self.current())

    def rebuild(self):
        """Перестроить дерево из сеанса, сохранив раскрытие и выбор."""
        s = self.session
        tree = self.tree
        expanded = {k for k, it in self._items.items() if it.isExpanded()}
        first = not self._items
        cur = self.current()
        tree.blockSignals(True)
        tree.clear()
        self._items = {}
        flt = self.filter.text().strip().lower()
        if s is None:
            tree.blockSignals(False)
            return
        t = s.tree

        def add(parent, node):
            if isinstance(node, Group):
                ids = t.scans_in(node.id)
                if flt and not any(flt in x.lower() for x in ids):
                    return
                it = QTreeWidgetItem(parent)
                it.setData(0, ROLE, node.id)
                it.setData(0, ROLE + 1, True)
                it.setSizeHint(0, QSize(10, 30))
                it.setFlags(it.flags() | Qt.ItemIsDropEnabled | Qt.ItemIsDragEnabled)
                self._items[node.id] = it
                row = _Row(self, node.id, node.name, node.visible, group=True, count=len(ids))
                row.setToolTip(f'{node.kind}: сканов {len(ids)}')
                tree.setItemWidget(it, 0, row)
                for ch in node.children:
                    add(it, ch)
                it.setExpanded(first or flt != '' or node.id in expanded)
            else:
                if flt and flt not in node.scan.lower():
                    return
                it = QTreeWidgetItem(parent)
                it.setData(0, ROLE, node.scan)
                it.setData(0, ROLE + 1, False)
                it.setSizeHint(0, QSize(10, 30))
                it.setFlags((it.flags() | Qt.ItemIsDragEnabled) & ~Qt.ItemIsDropEnabled)
                self._items[node.scan] = it
                st = self.status_of(node.scan) if self.status_of else None
                row = _Row(self, node.scan, short(node.scan), node.visible,
                           color=self.color_of(node.scan) if self.color_of else None, chip=st)
                row.setToolTip(node.scan)
                tree.setItemWidget(it, 0, row)

        root = tree.invisibleRootItem()
        for ch in t.root.children:
            add(root, ch)
        if cur in self._items:
            tree.setCurrentItem(self._items[cur])
        tree.blockSignals(False)
        self.empty.setVisible(not s.scans)
        self.tree.setVisible(bool(s.scans))

    def _menu(self, pos):
        it = self.tree.itemAt(pos)
        key = it.data(0, ROLE) if it is not None else None
        is_group = bool(it.data(0, ROLE + 1)) if it is not None else False
        t = W.THEME
        m = QMenu(self)

        def act(text, name, icon=None, enabled=True):
            a = m.addAction(t.icon(icon) if icon else QIcon(), text)
            a.setEnabled(enabled)
            a.triggered.connect(lambda: self.action.emit(name, key))
        act('Новая группа', 'new_group', 'mdi6.folder-plus-outline')
        if key is not None:
            if is_group:
                act('Переименовать', 'rename', 'mdi6.pencil-outline')
                act('Удалить группу (содержимое — родителю)', 'delete_group', 'mdi6.folder-remove-outline')
            else:
                act('Сделать опорным', 'make_ref', 'mdi6.anchor')
                act('Ручная стыковка', 'manual', 'mdi6.vector-combine')
                act('Найти кандидатов позы', 'candidates', 'mdi6.target')
                act('Встать в точку скана (полёт)', 'fly_to', 'mdi6.airplane')
            m.addSeparator()
            act('Переместить в группу…', 'move', 'mdi6.folder-move-outline')
            act('Показать только это', 'only', 'mdi6.eye-check-outline')
            act('Экспорт ветки…', 'export_branch', 'mdi6.export-variant')
        m.addSeparator()
        act('Показать всё', 'show_all', 'mdi6.eye-outline')
        m.exec(self.tree.viewport().mapToGlobal(pos))
