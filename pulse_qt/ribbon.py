"""
Лента инструментов: вкладки-этапы (Проект → Стыковка → Контроль → Чистка → Результат),
в каждой — группы с подписью: крупные кнопки (значок над подписью) и столбцы малых.
Двойной щелчок по вкладке сворачивает ленту до строки вкладок.
"""

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QLabel, QToolButton,
                               QStackedWidget, QWidget, QButtonGroup)

from . import widgets as W


class _Tab(QToolButton):
    double = Signal()

    def mouseDoubleClickEvent(self, ev):
        self.double.emit()


class RibbonGroup(QFrame):
    """Группа ленты: кнопки и подпись снизу."""

    def __init__(self, title):
        super().__init__()
        self.setObjectName('RGroup')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 2)
        lay.setSpacing(2)
        self.row = QHBoxLayout()
        self.row.setSpacing(2)
        lay.addLayout(self.row, 1)
        cap = QLabel(title)
        cap.setObjectName('RCaption')
        cap.setAlignment(Qt.AlignCenter)
        lay.addWidget(cap)
        self._col = None

    def big(self, icon, text, cb=None, tip=None, checkable=False, menu=None):
        b = QToolButton()
        b.setObjectName('RBig')
        b.setIcon(W.THEME.icon(icon, 'ink2', 'accent'))
        b.setIconSize(QSize(26, 26))
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        b.setCheckable(checkable)
        b.setToolTip(tip or text.replace('\n', ' '))
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)
        if cb is not None:
            b.clicked.connect(lambda _=False: cb())
        if menu is not None:
            b.setMenu(menu)
            b.setPopupMode(QToolButton.MenuButtonPopup if cb is not None else QToolButton.InstantPopup)
        self._col = None
        self.row.addWidget(b)
        return b

    def small(self, icon, text, cb=None, tip=None, checkable=False, menu=None):
        """Малые кнопки складываются в столбцы по три."""
        if self._col is None or self._col.count() >= 3:
            w = QWidget()
            self._col = QVBoxLayout(w)
            self._col.setContentsMargins(0, 0, 0, 0)
            self._col.setSpacing(1)
            self._col.setAlignment(Qt.AlignTop)
            self.row.addWidget(w)
        b = QToolButton()
        b.setObjectName('RSmall')
        b.setIcon(W.THEME.icon(icon, 'ink2', 'accent'))
        b.setIconSize(QSize(16, 16))
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setCheckable(checkable)
        b.setToolTip(tip or text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)
        if cb is not None:
            b.clicked.connect(lambda _=False: cb())
        if menu is not None:
            b.setMenu(menu)
            b.setPopupMode(QToolButton.InstantPopup)
        self._col.addWidget(b)
        return b

    def widget(self, w):
        self._col = None
        self.row.addWidget(w)
        return w


class Ribbon(QFrame):
    tabChanged = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName('Ribbon')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        top = QFrame()
        top.setObjectName('RTabs')
        top.setFixedHeight(38)
        self.top = QHBoxLayout(top)
        self.top.setContentsMargins(12, 0, 8, 0)
        self.top.setSpacing(4)
        self.brand = QHBoxLayout()
        self.brand.setSpacing(10)
        self.top.addLayout(self.brand)
        self.top.addSpacing(18)
        self.tabs_lay = QHBoxLayout()
        self.tabs_lay.setSpacing(2)
        self.top.addLayout(self.tabs_lay)
        self.top.addStretch(1)
        self.quick = QHBoxLayout()
        self.quick.setSpacing(2)
        self.top.addLayout(self.quick)
        lay.addWidget(top)
        self.stack = QStackedWidget()
        self.stack.setObjectName('RPage')
        self.stack.setFixedHeight(92)
        lay.addWidget(self.stack)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.keys = []
        self.collapsed = False

    def add_tab(self, key, title):
        b = _Tab()
        b.setObjectName('RTab')
        b.setText(title)
        b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)
        b.setToolTip(f'{title} (двойной щелчок — свернуть/развернуть ленту)')
        i = len(self.keys)
        self.group.addButton(b, i)
        b.clicked.connect(lambda _=False, i=i: self.set_tab(self.keys[i], emit=True))
        b.double.connect(self.toggle_collapsed)
        self.tabs_lay.addWidget(b)
        page = QWidget()
        pl = QHBoxLayout(page)
        pl.setContentsMargins(6, 2, 6, 2)
        pl.setSpacing(0)
        page._lay = pl
        self.stack.addWidget(page)
        self.keys.append(key)
        if i == 0:
            b.setChecked(True)
        return page

    def add_group(self, page, title):
        g = RibbonGroup(title)
        if page._lay.count():
            sep = QFrame()
            sep.setObjectName('RSep')
            sep.setFixedWidth(1)
            page._lay.addWidget(sep)
        page._lay.addWidget(g)
        return g

    def finish(self):
        for i in range(self.stack.count()):
            self.stack.widget(i)._lay.addStretch(1)

    def current(self):
        return self.keys[self.stack.currentIndex()]

    def set_tab(self, key, emit=False):
        i = self.keys.index(key)
        self.group.button(i).setChecked(True)
        changed = self.stack.currentIndex() != i
        self.stack.setCurrentIndex(i)
        if self.collapsed and emit and not changed:
            self.toggle_collapsed()                      # щелчок по текущей вкладке свёрнутой ленты — развернуть
        if emit:
            self.tabChanged.emit(key)

    def toggle_collapsed(self):
        self.collapsed = not self.collapsed
        self.stack.setVisible(not self.collapsed)
