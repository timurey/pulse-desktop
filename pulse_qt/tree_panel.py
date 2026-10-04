"""Левая панель: дерево проекта (группы и сканы) и слои отображения."""

from PySide6.QtCore import Qt, Signal, QSize, QItemSelectionModel
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QSlider,
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
    moved = Signal(list, str, object)              # ключи узлов, id группы, позиция (None — в конец)

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setAnimated(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)   # Shift / Ctrl(⌘) — несколько
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

    def dropEvent(self, ev):
        """
        Перетаскивание выделенных узлов: над / под элементом — на это место (порядок в
        группе), на группу — в неё (в конец), на пустое место — в конец корня.
        """
        keys = [it.data(0, ROLE) for it in self.selectedItems()]
        ev.ignore()                                # дерево перестраивается из сеанса
        if not keys:
            return
        gid, index = self.drop_target(self.itemAt(ev.position().toPoint()), self.dropIndicatorPosition())
        if gid in keys:
            return
        self.moved.emit(keys, gid, index)

    def drop_target(self, tgt, pos):
        """Куда класть: (id группы, позиция или None) по элементу и отметке вставки Qt."""
        P = QAbstractItemView.DropIndicatorPosition
        if tgt is None or pos == P.OnViewport:
            return 'root', None
        if pos == P.OnItem and tgt.data(0, ROLE + 1):
            return tgt.data(0, ROLE), None             # на группу — в неё
        if pos == P.BelowItem and tgt.data(0, ROLE + 1) and tgt.isExpanded() and tgt.childCount():
            return tgt.data(0, ROLE), 0                # под раскрытой группой — первым в ней
        par = tgt.parent() if tgt.parent() is not None else self.invisibleRootItem()
        gid = par.data(0, ROLE) if par is not self.invisibleRootItem() else 'root'
        return gid, par.indexOfChild(tgt) + (0 if pos == P.AboveItem else 1)


class TreePanel(QFrame):
    selected = Signal(object)                      # ключ узла или None
    visibilityToggled = Signal(str, bool)
    moved = Signal(list, str, object)
    action = Signal(str, object)                   # имя действия, ключ
    layerToggled = Signal(str, bool)
    qualityChanged = Signal(float, str)            # порог, м; способ: planes | local

    LAYERS = [('planes', 'Поверхности', 'mdi6.layers-outline', False),
              ('openings', 'Проёмы', 'mdi6.window-closed-variant', False),
              ('ghosts', 'Отражения', 'mdi6.blur', False),
              ('grid', 'Сетка 1 м', 'mdi6.grid', True),
              ('points', 'Точки сканов', 'mdi6.dots-grid', True),
              ('mesh', 'Поверхность', 'mdi6.vector-triangle', True),
              ('quality', 'Качество совмещения', 'mdi6.texture-box', False)]

    def __init__(self):
        super().__init__()
        self.setObjectName('PanelLeft')
        self.session = None
        self.status_of = None
        self.color_of = None
        self._items = {}
        self.expand_next = set()                   # группы, которые раскрыть при перестройке (новые)
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
        # качество: порог толщины и способ — видны, пока слой включён
        self.q_box = QWidget()
        ql = QVBoxLayout(self.q_box)
        ql.setContentsMargins(26, 0, 0, 0)
        ql.setSpacing(6)
        self.q_slider = QSlider(Qt.Horizontal)
        self.q_slider.setRange(5, 150)                 # мм / 10 → 0.5…15 см
        self.q_slider.setValue(30)
        self.q_slider.setFocusPolicy(Qt.NoFocus)
        self.q_slider.setToolTip('Подсвечивать места, где поверхность из разных сканов толще порога')
        self.q_value = W.label('3.0 см', 'KV_v')
        self.q_value.setFixedWidth(46)
        self.q_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        ql.addWidget(W.hbox(W.label('порог'), self.q_slider, self.q_value, spacing=6))
        self.q_method = W.Segmented([('по плоскостям', 'planes'), ('локально', 'local')], 'planes')
        ql.addWidget(self.q_method)
        self.q_info = W.label('', 'Hint', wrap=True)
        ql.addWidget(self.q_info)
        self.q_box.setVisible(False)
        ll.addWidget(self.q_box)
        self.switches['quality'].toggled.connect(self.q_box.setVisible)
        self.q_slider.valueChanged.connect(self._q_changed)
        self.q_method.changed.connect(lambda _: self._q_changed())
        lay.addWidget(layers)

    def quality_params(self):
        return self.q_slider.value() / 1000.0, self.q_method.value()

    def _q_changed(self, *_):
        thr, m = self.quality_params()
        self.q_value.setText(f'{thr * 100:.1f} см')
        self.qualityChanged.emit(thr, m)

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

    def selected_keys(self):
        """Выделенные узлы в порядке дерева."""
        sel = {it.data(0, ROLE) for it in self.tree.selectedItems()}
        return [k for k in self._items if k in sel]

    def select_keys(self, keys):
        self.tree.clearSelection()
        for k in keys:
            it = self._items.get(k)
            if it is not None:
                it.setSelected(True)
        if keys and keys[-1] in self._items:             # текущий — без сброса остального выделения
            self.tree.setCurrentItem(self._items[keys[-1]], 0, QItemSelectionModel.NoUpdate)

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
        sel = set(self.selected_keys())
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
                it.setExpanded(first or flt != '' or node.id in expanded or node.id in self.expand_next)
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
        root.setFlags(root.flags() | Qt.ItemIsDropEnabled)   # перестановка и на верхнем уровне
        for ch in t.root.children:
            add(root, ch)
        if cur in self._items:
            tree.setCurrentItem(self._items[cur])
        for k in sel:
            if k in self._items:
                self._items[k].setSelected(True)
        tree.setDragEnabled(not flt)                # с фильтром порядок в списке неполный
        self.expand_next = set()
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
        many = self.selected_keys()
        if key is not None and key in many and len(many) > 1:
            act(f'Сгруппировать выделенные ({len(many)})  ⌘/Ctrl+G', 'group_sel', 'mdi6.folder-plus')
            act(f'Переместить выделенные ({len(many)}) в группу…', 'move', 'mdi6.folder-move-outline')
            act('Показать только выделенные', 'only', 'mdi6.eye-check-outline')
            m.addSeparator()
            act(f'Удалить из проекта ({len(many)})…  ⌘/Ctrl+Delete', 'remove', 'mdi6.delete-outline')
            m.addSeparator()
            act('Показать всё', 'show_all', 'mdi6.eye-outline')
            m.exec(self.tree.viewport().mapToGlobal(pos))
            return
        if key is not None:
            if is_group:
                act('Автостыковка внутри группы', 'auto_group', 'mdi6.folder-network-outline')
                act('Стыковка подгрупп между собой', 'auto_subgroups', 'mdi6.vector-arrange-above')
                m.addSeparator()
                act('Переименовать', 'rename', 'mdi6.pencil-outline')
                act('Удалить группу (содержимое — родителю)', 'delete_group', 'mdi6.folder-remove-outline')
                act('Удалить группу вместе со сканами…', 'remove', 'mdi6.delete-outline')
            else:
                act('Сделать опорным', 'make_ref', 'mdi6.anchor')
                act('Ручная стыковка', 'manual', 'mdi6.vector-combine')
                act('Уточнить стыковку (автоподгонка)', 'refine', 'mdi6.auto-fix')
                act('Найти кандидатов позы', 'candidates', 'mdi6.target')
                act('Встать в точку скана (полёт)', 'fly_to', 'mdi6.airplane')
                act('Удалить из проекта…  ⌘/Ctrl+Delete', 'remove', 'mdi6.delete-outline')
            m.addSeparator()
            act('Переместить в группу…', 'move', 'mdi6.folder-move-outline')
            act('Показать только это', 'only', 'mdi6.eye-check-outline')
            act('Экспорт ветки…', 'export_branch', 'mdi6.export-variant')
        m.addSeparator()
        act('Показать всё', 'show_all', 'mdi6.eye-outline')
        m.exec(self.tree.viewport().mapToGlobal(pos))
