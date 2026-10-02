"""
Главное окно Pulse Scan (Qt + VTK) — тонкий слой над scan_session.Session.

Раскладка (макет Claude Design «Pulse Scan»): панель инструментов; слева дерево
проекта и слои; в центре 3D-вид с плашками (путь вида, куб навигации,
координаты, инструменты вида) и нижняя панель (пары, циклы, кандидаты, журнал);
справа инспектор / ручная стыковка / чистка; строка состояния.

Логика сеанса, как и в scan_gui.py, — в scan_session.py; здесь только виджеты,
сцена и вызовы. Фоновые задачи — bg.run (результат в главном потоке).
"""

import json
import platform
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QMainWindow, QWidget, QFrame, QHBoxLayout, QVBoxLayout, QSplitter, QLabel, QSlider,
                               QStackedWidget, QProgressBar, QMenu, QFileDialog, QMessageBox, QApplication,
                               QToolButton)

import plane_register as pr
import manual_clean
import quality
from scan_session import Session
from scan_tree import KINDS

from . import bg
from . import widgets as W
from .theme import scan_rgb, pick_rgb, ui_font_path
from .cloud_view import CloudView
from .tree_panel import TreePanel, short
from .inspector import Inspector, ManualPanel, CleanPanel
from .dock import Dock
from .dialogs import ScannerImportDialog, BagImportDialog, ExportDialog, AskDialog

KIND_HUE = {'floor': (0.30, 0.75, 0.40), 'ceiling': (0.35, 0.55, 1.0), 'wall': (1.0, 0.62, 0.20),
            'other': (0.6, 0.6, 0.6)}
OPEN_COLOR = {'window': (0.90, 0.35, 0.90), 'door': (0.20, 0.80, 0.95)}
KIND_RU = {'wall': 'стена', 'floor': 'пол', 'ceiling': 'потолок'}


def fdesc(f):
    if f is None:
        return ''
    if f['type'] == 'plane':
        return KIND_RU.get(f.get('kind'), 'плоскость') + f" {f['area']:.1f} м²"
    if f['type'] == 'opening':
        return f"проём {f['size'][0]:.2f}×{f['size'][1]:.2f}"
    return 'точка'


def incompatible(fa, fb):
    if fa['type'] != fb['type'] and 'point' not in (fa['type'], fb['type']):
        return f"{fdesc(fa)} и {fdesc(fb)} — разные типы признаков"
    if fa['type'] == 'plane' and fb['type'] == 'plane' and fa.get('kind') != fb.get('kind'):
        return f"{fdesc(fa)} ↔ {fdesc(fb)}: разные виды плоскостей"
    return None


def convex_hull_2d(P):
    P = np.unique(np.round(np.asarray(P, float), 3), axis=0)
    if len(P) < 3:
        return P
    P = P[np.lexsort((P[:, 1], P[:, 0]))]
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in P:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in P[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1])


def quick_voxel():
    from scan_session import DISPLAY_VOXEL
    return max(DISPLAY_VOXEL, 0.03)


class Viewport(QWidget):
    """3D-вид с плашками поверх."""

    def __init__(self, win):
        super().__init__()
        t = W.THEME
        self.view = CloudView(self)
        self.crumb = W.Glass(self)
        self.crumb_icon = QLabel()
        self.crumb_text = QLabel('')
        self.crumb.lay.addWidget(self.crumb_icon)
        self.crumb.lay.addWidget(self.crumb_text)
        self.cube = W.ViewCubeWidget(self.view, ui_font_path(), self)
        self.cube.set_style(t.cube_style())
        self.coord = W.Glass(self)
        self.scale_bar = QFrame()
        self.scale_bar.setFixedHeight(5)
        self.scale_bar.setStyleSheet(f"border:1px solid {t.q('ink3').name()}; border-top:0;")
        self.scale_text = QLabel('1 м')
        self.scale_text.setObjectName('GlassMono')
        self.coord_text = QLabel('')
        self.coord_text.setObjectName('GlassMono')
        self.coord.lay.addWidget(self.scale_bar)
        self.coord.lay.addWidget(self.scale_text)
        self.coord.lay.addSpacing(6)
        self.coord.lay.addWidget(self.coord_text)
        self.tools = W.Glass(self)
        self.tools.lay.setContentsMargins(4, 4, 4, 4)
        self.tools.lay.setSpacing(2)
        self.b_fit = W.tool(t.icon('mdi6.fit-to-screen-outline'), tip='Показать всё', cb=win.fit_all,
                            icon_only=True)
        self.b_proj = W.tool(t.icon('mdi6.perspective-less', 'ink2', 'accent'),
                             tip='Ортогональная проекция (O); выключено — перспектива',
                             cb=win.toggle_projection, checkable=True, icon_only=True)
        # размер точек — всегда на виду: ползунок 1…8 пикселей ([ и ] — с клавиатуры)
        size_icon = QLabel()
        size_icon.setPixmap(t.icon('mdi6.dots-grid', 'ink3').pixmap(16, 16))
        size_icon.setToolTip('Размер точек')
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(1, 8)
        self.size_slider.setFixedWidth(84)
        self.size_slider.setValue(int(round(self.view.point_px)))
        self.size_slider.setToolTip('Размер точек, пикс. ([ и ])')
        self.size_slider.setFocusPolicy(Qt.NoFocus)
        self.size_slider.valueChanged.connect(lambda v: win.set_point_size(v))
        self.size_text = QLabel(f'{int(self.view.point_px)}')
        self.size_text.setObjectName('GlassMono')
        self.size_text.setFixedWidth(12)
        self.b_shot = W.tool(t.icon('mdi6.camera-outline'), tip='Скриншот (F12)', cb=win.save_screenshot,
                             icon_only=True)

        def vsep():
            f = QFrame()
            f.setObjectName('ToolSep')
            f.setFixedSize(1, 20)
            return f
        for b in (self.b_fit, self.b_proj, vsep(), size_icon, self.size_slider, self.size_text, vsep(),
                  self.b_shot):
            self.tools.lay.addWidget(b)
        self.tools.lay.setSpacing(6)
        self.tools.lay.setContentsMargins(6, 4, 4, 4)
        self.banner = QFrame(self)
        self.banner.setObjectName('Banner')
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(12, 6, 12, 6)
        self.banner_text = QLabel('')
        bl.addWidget(self.banner_text)
        self.banner.hide()
        self.view.cameraChanged.connect(self.update_overlays)

    def set_banner(self, text):
        self.banner_text.setText(text)
        self.banner.setVisible(bool(text))
        self.banner.adjustSize()
        self._place()

    def resizeEvent(self, ev):
        self.view.setGeometry(0, 0, self.width(), self.height())
        self._place()

    def _place(self):
        w, h = self.width(), self.height()
        m = 12
        self.crumb.adjustSize()
        self.crumb.move(m, m)
        self.cube.move(w - self.cube.width() - m, m)
        self.coord.adjustSize()
        self.coord.move(m, h - self.coord.height() - m)
        self.tools.adjustSize()
        self.tools.move(w - self.tools.width() - m, h - self.tools.height() - m)
        if self.banner.isVisible():
            self.banner.adjustSize()
            self.banner.move(max(m, (w - self.banner.width()) // 2), m + self.crumb.height() + 8)

    def update_overlays(self):
        v = self.view
        R = v.basis()
        t = W.THEME
        if v.fly:
            text, ic = 'Полёт · WASD, Space/E — вверх, C/Q — вниз, колесо — скорость', 'mdi6.airplane'
        elif R[2, 2] > 0.995:
            text, ic = 'Вид сверху · общая система · Z вверх', 'mdi6.map-outline'
        else:
            text, ic = '3D · общая система · Z вверх', 'mdi6.cube-outline'
        if v.parallel and not v.fly:
            text += ' · ортогональная'
        if text != self.crumb_text.text():
            self.crumb_text.setText(text)
            self.crumb_icon.setPixmap(t.icon(ic).pixmap(16, 16))
        c = v.eye() if v.fly else v.center()
        self.coord_text.setText(f'x {c[0]:.2f}   y {c[1]:.2f}   z {c[2]:.2f}')
        # масштабная линейка: метров на 56 пикселей у центра вращения
        mpp = v.world_per_px()
        target = 56 * mpp
        nice = min((x for x in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100) if x >= target * 0.6), default=100)
        px = int(np.clip(nice / max(mpp, 1e-9), 16, 120))
        self.scale_bar.setFixedWidth(px)
        self.scale_text.setText(f'{nice:g} м')
        self._place()


class MainWindow(QMainWindow):
    def __init__(self, session=None, theme=None):
        super().__init__()
        self.theme = theme
        self.s = session or Session()
        self.settings = QSettings('Pulse', 'PulseScan')
        self.busy = False
        self.analyzing = False
        self._load_gen = 0
        self._after_analysis = None
        self.scanner_host = self.settings.value('scanner_host', 'pulse.local')
        self.scanner_ok = None
        self._dl_cancel = False
        self.downloading = False
        self._saved_sig = None
        self._closing_ok = False
        # ручной режим
        self.fixed = self.moving = None
        self.T_moving = None
        self._manual_T0 = None                   # поза подвижного при входе в ручную стыковку
        self.pairs = []
        self.pending = None
        self._drag = None
        self._score_pending = False
        self._score_again = False
        self.manual_score = None
        # кандидаты
        self.candidates = []
        self.cand_scan = None
        # чистка
        self.mode = 'inspect'                    # inspect | manual | clean
        self.select_mode = False
        self._rect = None
        self.selection = {}
        self._sel_frusta = []
        self.ghost_shown = set()
        self.clean_scan = None
        # дерево, слои
        self.tree_sel = None
        self._vis_backup = None
        # качество совмещения: результат, ключ состояния, для которого он посчитан
        self.qual = {'res': None, 'key': None, 'busy': False, 'areas': []}
        self._qual_timer = QTimer(self)
        self._qual_timer.setSingleShot(True)
        self._qual_timer.timeout.connect(self._quality_areas)
        self.setWindowTitle('Pulse Scan')
        self._build()
        self._shortcuts()
        self._dirty_timer = QTimer(self)
        self._dirty_timer.timeout.connect(self._update_title)
        self._dirty_timer.start(1500)
        geo = self.settings.value('geometry')
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            scr = QApplication.primaryScreen().availableGeometry()
            self.resize(min(1600, int(scr.width() * 0.92)), min(1000, int(scr.height() * 0.9)))
        self.refresh_all()
        if self.s.scans:
            QTimer.singleShot(50, self.load_all)

    # ── построение окна ──────────────────────────────────────────────────
    def _build(self):
        t = W.THEME
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._topbar())
        self.split = QSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.tree_panel = TreePanel()
        self.tree_panel.session = self.s
        self.tree_panel.status_of = self.status_chip
        self.tree_panel.color_of = lambda sid: self.scan_color(sid)[1]
        self.tree_panel.selected.connect(self.on_tree_select)
        self.tree_panel.visibilityToggled.connect(self.on_tree_visible)
        self.tree_panel.moved.connect(self.on_tree_move_drop)
        self.tree_panel.action.connect(self.on_tree_action)
        self.tree_panel.layerToggled.connect(self.on_layer)
        self.tree_panel.qualityChanged.connect(lambda thr, m: self.quality_refresh())
        self.tree_panel.setMinimumWidth(220)
        self.split.addWidget(self.tree_panel)
        self.vsplit = QSplitter(Qt.Vertical)
        self.vsplit.setChildrenCollapsible(False)
        self.vp = Viewport(self)
        self.view = self.vp.view
        self.view.mouse_hook = self._mouse_hook
        self.view.dblclick_hook = self._dblclick
        self.view.statusMessage.connect(self.set_status)
        self.vp.cube.described.connect(lambda s: self.set_status(s, log=False))
        self.vsplit.addWidget(self.vp)
        self.dock = Dock()
        self.dock.pairSelected.connect(self.on_pair_selected)
        self.dock.pairAction.connect(self.on_pair_action)
        self.dock.candSearch.connect(self.on_cand_search)
        self.dock.candSelected.connect(self.on_cand_selected)
        self.dock.candAction.connect(self.on_cand_action)
        self.dock.qualitySelected.connect(self.on_quality_selected)
        self.dock.setMinimumHeight(120)
        self.vsplit.addWidget(self.dock)
        self.vsplit.setStretchFactor(0, 1)
        self.vsplit.setSizes([700, 210])
        self.split.addWidget(self.vsplit)
        self.right = QStackedWidget()
        self.right.setObjectName('PanelRight')
        self.right.setMinimumWidth(330)
        self.inspector = Inspector()
        self.inspector.action.connect(self.on_inspector_action)
        self.manual = ManualPanel()
        self.manual.action.connect(self.on_manual_action)
        self.manual.closed.connect(self.on_manual_cancel)
        self.clean = CleanPanel()
        self.clean.action.connect(self.on_clean_action)
        self.clean.closed.connect(lambda: self.set_mode('inspect'))
        for w in (self.inspector, self.manual, self.clean):
            self.right.addWidget(w)
        rw = QFrame()
        rw.setObjectName('PanelRight')
        rl = QVBoxLayout(rw)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.right)
        self.split.addWidget(rw)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([272, 900, 350])
        lay.addWidget(self.split, 1)
        lay.addWidget(self._statusbar())
        self.setCentralWidget(root)
        self.view.set_background(t.rgb('canvas'))
        self._mac_menubar()

    def _topbar(self):
        t = W.THEME
        bar = QFrame()
        bar.setObjectName('TopBar')
        bar.setFixedHeight(48)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(12, 0, 8, 0)
        hl.setSpacing(10)
        mark = QFrame()
        mark.setObjectName('Mark')
        mark.setFixedSize(22, 22)
        hl.addWidget(mark)
        hl.addWidget(W.label('Pulse Scan', 'AppName'))
        self.proj_name = W.label('новый проект', 'ProjName')
        self.proj_name.setMaximumWidth(260)
        hl.addWidget(self.proj_name)
        self.dirty_dot = QFrame()
        self.dirty_dot.setObjectName('DirtyDot')
        self.dirty_dot.setFixedSize(7, 7)
        self.dirty_dot.setToolTip('Есть несохранённые изменения')
        self.dirty_dot.hide()
        hl.addWidget(self.dirty_dot)
        hl.addStretch(1)

        def group(*btns):
            g = QWidget()
            gl = QHBoxLayout(g)
            gl.setContentsMargins(6, 0, 6, 0)
            gl.setSpacing(2)
            for b in btns:
                gl.addWidget(b)
            return g

        def sep():
            s = QFrame()
            s.setObjectName('ToolSep')
            s.setFixedSize(1, 24)
            return s
        ic = t.icon
        self.b_scanner = W.tool(ic('mdi6.access-point'), 'Со сканера', self.on_import_scanner,
                                tip='Скачать записи со сканера и импортировать')
        self.b_bag = W.tool(ic('mdi6.folder-open-outline'), 'Bag', self.on_import_bags,
                            tip='Импорт bag из папки')
        self.b_auto = W.tool(ic('mdi6.graph-outline'), 'Автостыковка', self.on_auto,
                             tip='Подобрать пары и позы сканов')
        am = QMenu(self.b_auto)
        self.a_reuse = am.addAction('Использовать уже посчитанные пары')
        self.a_reuse.setCheckable(True)
        self.a_reuse.setChecked(True)
        self.a_bytree = am.addAction('По дереву: пары внутри групп и соседних (быстрее)')
        self.a_bytree.setCheckable(True)
        self.a_bytree.setChecked(True)
        am.addSeparator()
        am.addAction('Запустить', self.on_auto)
        self.b_auto.setMenu(am)
        self.b_auto.setPopupMode(QToolButton.MenuButtonPopup)
        self.b_auto.setProperty('menuarrow', True)
        self.b_manual = W.tool(ic('mdi6.vector-combine', 'ink2', 'accent'), 'Ручная',
                               lambda: self.start_manual(), checkable=True,
                               tip='Ручная стыковка выбранного скана')
        self.b_top = W.tool(ic('mdi6.map-outline'), tip='Вид сверху (T)', cb=self.view_top, icon_only=True)
        self.b_3d = W.tool(ic('mdi6.cube-outline'), tip='Вид 3D', cb=self.view_3d, icon_only=True)
        self.b_fly = W.tool(ic('mdi6.airplane', 'ink2', 'accent'), tip='Полёт (F)', cb=self.toggle_fly,
                            checkable=True, icon_only=True)
        self.b_clean = W.tool(ic('mdi6.selection-drag', 'ink2', 'accent'), 'Чистка',
                              lambda: self.set_mode('inspect' if self.mode == 'clean' else 'clean'),
                              checkable=True, tip='Чистка отражений и выделение прямоугольником (R)')
        self.b_export = W.tool(ic('mdi6.export-variant'), 'Экспорт', self.on_export,
                               tip='Экспорт склейки')
        hl.addWidget(group(self.b_scanner, self.b_bag))
        hl.addWidget(sep())
        hl.addWidget(group(self.b_auto, self.b_manual))
        hl.addWidget(sep())
        hl.addWidget(group(self.b_top, self.b_3d, self.b_fly))
        hl.addWidget(sep())
        hl.addWidget(group(self.b_clean, self.b_export))
        hl.addStretch(1)
        self.b_save = W.tool(ic('mdi6.content-save-outline'), tip='Сохранить (Ctrl+S)', cb=self.on_save,
                             icon_only=True)
        self.b_theme = W.tool(ic('mdi6.white-balance-sunny' if t.dark else 'mdi6.weather-night'),
                              tip='Светлая / тёмная тема', cb=self.toggle_theme, icon_only=True)
        self.b_menu = W.tool(ic('mdi6.menu'), tip='Меню', icon_only=True)
        self.b_menu.setMenu(self._file_menu())
        self.b_menu.setPopupMode(QToolButton.InstantPopup)
        for b in (self.b_save, self.b_theme, self.b_menu):
            hl.addWidget(b)
        return bar

    def _file_menu(self, m=None):
        t = W.THEME
        m = m or QMenu(self)
        for item in (
                ('Новый проект', self.on_new_project, 'mdi6.file-plus-outline', 'Ctrl+N'),
                ('Открыть проект…', self.on_open, 'mdi6.folder-outline', 'Ctrl+O'),
                ('Сохранить', self.on_save, 'mdi6.content-save-outline', 'Ctrl+S'),
                ('Сохранить как…', self.on_save_as, 'mdi6.content-save-edit-outline', 'Ctrl+Shift+S'),
                None,
                ('Импорт со сканера…', self.on_import_scanner, 'mdi6.access-point', None),
                ('Импорт bag…', self.on_import_bags, 'mdi6.folder-open-outline', None),
                ('Добавить облако…', self.on_add_scan, 'mdi6.plus', None),
                ('Экспорт склейки…', self.on_export, 'mdi6.export-variant', None),
                None,
                ('Скриншот', self.save_screenshot, 'mdi6.camera-outline', 'F12'),
                ('Клавиши и мышь', self.show_help, 'mdi6.keyboard-outline', None)):
            if item is None:
                m.addSeparator()
                continue
            text, cb, ic, key = item
            # клавиши — отдельными QShortcut (_shortcuts); здесь только подпись справа
            a = m.addAction(t.icon(ic), f'{text}\t{key}' if key else text)
            a.triggered.connect(lambda _=False, cb=cb: cb())
        return m

    def _mac_menubar(self):
        if platform.system() != 'Darwin':
            return
        mb = self.menuBar()
        mb.clear()
        self._file_menu(mb.addMenu('Файл'))
        v = mb.addMenu('Вид')
        v.addAction('Вид сверху', self.view_top)
        v.addAction('Вид 3D', self.view_3d)
        v.addAction('Показать всё', self.fit_all)
        v.addAction('Полёт', self.toggle_fly)
        v.addAction('Светлая / тёмная тема', self.toggle_theme)

    def _statusbar(self):
        t = W.THEME
        bar = QFrame()
        bar.setObjectName('StatusBar')
        bar.setFixedHeight(28)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(12, 0, 12, 0)
        hl.setSpacing(14)
        self.st_icon = QLabel()
        self.st_icon.setPixmap(t.icon('mdi6.information-outline', 'ink3').pixmap(14, 14))
        hl.addWidget(self.st_icon)
        self.st_text = QLabel('Откройте проект или импортируйте сканы')
        self.st_text.setMinimumWidth(80)
        hl.addWidget(self.st_text, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(120)
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.hide()
        hl.addWidget(self.progress)
        self.st_cache = QLabel('')
        hl.addWidget(self.st_cache)
        self.st_points = QLabel('')
        self.st_points.setObjectName('Mono')
        hl.addWidget(self.st_points)
        self.st_dot = QFrame()
        self.st_dot.setFixedSize(7, 7)
        self.st_dot.setObjectName('LiveDotOff')
        hl.addWidget(self.st_dot)
        self.st_host = QLabel(self.scanner_host)
        hl.addWidget(self.st_host)
        hl.addWidget(QLabel('Qt · VTK'))
        return bar

    def _shortcuts(self):
        for key, fn in (('Ctrl+N', self.on_new_project), ('Ctrl+O', self.on_open), ('Ctrl+S', self.on_save),
                        ('Ctrl+Shift+S', self.on_save_as), ('F12', self.save_screenshot),
                        ('Escape', self.on_escape), ('R', self.toggle_select), ('F', self.toggle_fly),
                        ('T', self.view_top), ('O', self.toggle_projection),
                        ('[', lambda: self.set_point_size(self.view.point_px - 1)),
                        (']', lambda: self.set_point_size(self.view.point_px + 1)),
                        ('Delete', self.on_erase), ('Backspace', self.on_erase),
                        ('Ctrl+Z', self.on_undo_erase)):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(fn)

    # ── тема ─────────────────────────────────────────────────────────────
    def toggle_theme(self):
        """Тема меняется пересборкой окна: значки рисуются цветами темы при создании."""
        R, c, d = self.view.basis(), self.view.center(), self.view.distance()
        fly = self.view.fly
        par, px = self.view.parallel, self.view.point_px
        status = self.st_text.text()
        sizes = (self.split.sizes(), self.vsplit.sizes())
        layers = {k: sw.isChecked() for k, sw in self.tree_panel.switches.items()}
        q_thr, q_method = self.tree_panel.q_slider.value(), self.tree_panel.q_method.value()
        self.theme.set(not self.theme.dark)
        self.settings.setValue('dark', self.theme.dark)
        QApplication.instance().setStyleSheet(self.theme.qss())
        self.theme.apply_palette(QApplication.instance())
        self._build()                                    # прежний центральный виджет Qt удалит сам
        self.split.setSizes(sizes[0])
        self.vsplit.setSizes(sizes[1])
        self.refresh_all()
        self.redraw_all()
        self.view.set_basis(R, c, d)
        self.set_point_size(px)
        if par:
            self.toggle_projection(True)
        if fly:
            self.toggle_fly()
        self.set_mode(self.mode, force=True)
        tp = self.tree_panel
        tp.q_slider.setValue(q_thr)
        tp.q_method.group.button(tp.q_method.values.index(q_method)).setChecked(True)
        for k, on in layers.items():
            tp.switches[k].setChecked(on, emit=on != (k == 'grid'))
        self.quality_refresh()
        self.set_status(status, log=False)

    # ── состояние ────────────────────────────────────────────────────────
    def set_status(self, msg, log=True):
        self.st_text.setText(msg)
        self.st_text.setToolTip(msg)
        if log:
            self.dock.add_log(msg)

    def _set_progress(self, frac, msg):
        self.progress.show()
        self.progress.setValue(int(1000 * max(0.0, min(1.0, frac))))
        self.set_status(msg, log=False)

    def run_bg(self, title, fn, done=None):
        """fn(progress) в потоке; done(результат) — в главном. Одна операция за раз."""
        if self.busy:
            self.set_status('Подождите: выполняется другая операция')
            return False
        self.busy = True
        self.set_status(title)
        self.progress.setValue(0)
        self.progress.show()

        def progress(frac, msg):
            bg.post(lambda: self._set_progress(frac, msg))

        def ok(r):
            self.busy = False
            self.progress.hide()
            if done:
                done(r)

        def fail(e):
            self.busy = False
            self.progress.hide()
            self.set_status(f'Ошибка: {e}')
        bg.run(lambda: fn(progress), ok, fail)
        return True

    def dirty(self):
        return bool(self.s.scans) and self.s.state_signature() != self._saved_sig

    def _update_title(self):
        p = self.s.project_path
        self.proj_name.setText(Path(p).name if p else 'новый проект')
        self.proj_name.setToolTip(str(p or ''))
        self.dirty_dot.setVisible(self.dirty())
        self.setWindowTitle(f"{Path(p).name if p else 'Новый проект'} — Pulse Scan")

    def scan_index(self, sid):
        for i, sc in enumerate(self.s.scans):
            if sc.id == sid:
                return i
        return 0

    def scan_color(self, sid):
        return scan_rgb(self.scan_index(sid), self.theme.dark)

    def has_manual_edge(self, sid):
        return any(e.get('method') == 'manual' and sid in (e['A'], e['B']) and self.s.edge_active(e)
                   for e in self.s.edges)

    def status_chip(self, sid):
        sc = self.s.by_id(sid)
        if sc is None:
            return None
        if sid == self.s.frame:
            return ('опорный', 'acc')
        if sc.pose is None:
            return ('не размещён', 'warn')
        if self.has_manual_edge(sid):
            return ('вручную', 'ok')
        return ('размещён', 'mut')

    def refresh_all(self):
        self.tree_panel.session = self.s
        self.tree_panel.rebuild()
        self.refresh_pairs()
        self.refresh_inspector()
        self.refresh_combos()
        self.refresh_stats()
        self._update_title()

    def refresh_stats(self):
        n = len(self.s.scans)
        cached = sum(1 for sc in self.s.scans if sc.analyzed)
        self.st_cache.setText(f'анализ {cached}/{n}' if n else '')
        pts = sum(len(getattr(sc, '_display', None) if getattr(sc, '_display', None) is not None else [])
                  for sc in self.s.scans)
        self.st_points.setText(f'{pts / 1e6:.2f} млн точек на экране' if pts else '')

    def refresh_combos(self):
        items = [(sc.id, short(sc.id)) for sc in self.s.scans]
        unplaced = [sc.id for sc in self.s.scans if sc.pose is None]
        self.dock.set_cand_scans(items, self.cand_scan.id if self.cand_scan else (unplaced[0] if unplaced else None))

    # ── сцена ────────────────────────────────────────────────────────────
    def pose_of(self, sc):
        if sc is self.moving and self.T_moving is not None:
            return self.T_moving
        return self.s.Tc(sc)

    def _show_scan(self, sc):
        name = f'scan:{sc.id}'
        T = self.pose_of(sc)
        if T is None or getattr(sc, '_display', None) is None:
            self.view.remove(name)
            return
        self.view.set_cloud(name, sc._display, self.scan_color(sc.id)[0], T, sc.visible)
        if sc.id in self.selection:
            self.selection.pop(sc.id)
            self._draw_selection()

    def redraw_all(self):
        for sc in self.s.scans:
            self._show_scan(sc)
        ids = {f'scan:{sc.id}' for sc in self.s.scans}
        for n in [n for n in self.view.items if n.startswith('scan:') and n not in ids]:
            self.view.remove(n)
        for sid in list(self.ghost_shown):
            self._draw_ghosts(self.s.by_id(sid))
        self._draw_selection()
        self.draw_features()
        self.update_grid()
        self.refresh_stats()
        self.quality_refresh()

    def apply_visibility(self):
        for sc in self.s.scans:
            self.view.set_visible(f'scan:{sc.id}', sc.visible)
        self.draw_features()
        for sid in list(self.ghost_shown):
            self._draw_ghosts(self.s.by_id(sid))
        self.quality_refresh()

    def _visible_bbox(self):
        pts = []
        for sc in self.s.scans:
            T = self.pose_of(sc)
            d = getattr(sc, '_display', None)
            if T is None or not sc.visible or d is None or not len(d):
                continue
            pts.append(pr.transform(d[::40], T))
        if not pts:
            return np.array([-5, -5, -2.0]), np.array([5, 5, 2.0])
        P = np.vstack(pts)
        return np.percentile(P, 1, axis=0), np.percentile(P, 99, axis=0)

    def update_grid(self):
        if not self.tree_panel.layer('grid') or not self.s.scans:
            self.view.remove('grid')
            return
        lo, hi = self._visible_bbox()
        pad = 2.0
        self.view.set_grid(lo[2] - 0.02, lo[:2] - pad, hi[:2] + pad, self.theme.rgb('grid'), 1.0,
                           0.9 if self.theme.dark else 1.0)

    def view_top(self):
        if self.view.fly:
            self.toggle_fly()
        lo, hi = self._visible_bbox()
        R = self.view.basis()
        up = R[:, 1] if R[2, 2] > 0.99 else (0, 1, 0)
        self.view.fit(lo, hi, (0, 0, 1), up)

    def view_3d(self):
        if self.view.fly:
            self.toggle_fly()
        lo, hi = self._visible_bbox()
        self.view.fit(lo, hi, (-0.6, -0.6, 0.55), (0, 0, 1))

    def fit_all(self):
        R = self.view.basis()
        lo, hi = self._visible_bbox()
        self.view.fit(lo, hi, R[:, 2], R[:, 1])

    def set_point_size(self, px):
        px = int(np.clip(px, 1, 8))
        self.view.set_point_size(px)
        self.settings.setValue('point_px', px)
        sl = self.vp.size_slider
        if sl.value() != px:
            sl.blockSignals(True)
            sl.setValue(px)
            sl.blockSignals(False)
        self.vp.size_text.setText(str(px))

    def toggle_projection(self, on=None):
        on = (not self.view.parallel) if on is None else bool(on)
        if on and self.view.fly:
            self.toggle_fly()
        self.view.set_parallel(on)
        self.vp.b_proj.setChecked(on)
        self.settings.setValue('parallel', on)
        self.set_status('проекция: ортогональная (размеры без перспективных искажений)' if on
                        else 'проекция: перспектива', log=False)

    # ── загрузка ─────────────────────────────────────────────────────────
    def load_all(self):
        """Сначала показать сканы (кеш или сырые), затем анализ остальных в фоне."""
        self._load_gen += 1
        gen = self._load_gen
        sess = self.s

        def work(progress):
            t0 = time.time()
            n_cache = sess.load_cached()
            todo = [sc for sc in sess.scans if not sc.analyzed]
            for i, sc in enumerate(sess.scans):
                if gen != self._load_gen:
                    return None
                progress(i / max(1, len(sess.scans)), f'загрузка {short(sc.id)}')
                sc._display = sess.display_points(sc) if sc.analyzed else sc.quick_points(quick_voxel())
            return n_cache, todo, time.time() - t0

        def shown(r):
            if r is None or gen != self._load_gen:
                return
            n_cache, todo, dt = r
            self.after_load()
            msg = f'открыто за {dt:.1f} c: из кеша {n_cache} из {len(sess.scans)}'
            if todo:
                self.set_status(msg + f'; анализ остальных {len(todo)} в фоне…')
                self._analyze_bg(todo, gen)
            else:
                self.set_status(msg)
        self.run_bg('открытие проекта…', work, shown)

    def _analyze_bg(self, todo, gen):
        sess = self.s
        self.analyzing = True
        t0 = time.time()

        def work():
            for i, sc in enumerate(todo):
                if gen != self._load_gen:
                    return None
                bg.post(lambda i=i, sc=sc: self._set_progress(
                    i / len(todo), f'анализ сканов в фоне: {i + 1}/{len(todo)} ({short(sc.id)})…'))
                sc.analyze()
                sc._display = sess.display_points(sc)
                bg.post(lambda sc=sc: (self._show_scan(sc), self.draw_features(), self.refresh_stats()))
            return sess.flush_cache()

        def done(n):
            self.analyzing = False
            self.progress.hide()
            if n is None:
                return
            dt = time.time() - t0
            if n:
                msg = f'анализ готов за {dt:.0f} c, сохранён в кеш проекта ({n} скан.)'
            elif sess.project_path and str(sess.project_path).lower().endswith('.json'):
                msg = f'анализ готов за {dt:.0f} c. Сохраните проект как .pulse, чтобы не считать снова'
            else:
                msg = f'анализ готов за {dt:.0f} c (кеш запишется при сохранении проекта)'
            self.set_status(msg)
            self.refresh_inspector()
            self.refresh_clean()
            nxt, self._after_analysis = self._after_analysis, None
            if nxt is not None and gen == self._load_gen:
                nxt()

        def fail(e):
            self.analyzing = False
            self.progress.hide()
            self.set_status(f'ошибка фонового анализа: {e}')
        bg.run(work, done, fail)

    def after_load(self):
        self._saved_sig = self.s.state_signature()
        self.redraw_all()
        self.refresh_all()
        self.view_top()

    # ── проект ───────────────────────────────────────────────────────────
    def _start_dir(self):
        p = self.s.project_path
        return str(Path(p).resolve().parent) if p else self.settings.value('last_dir', str(Path.home()))

    def confirm_discard(self, what='Продолжить'):
        if not self.confirm_leave_manual(what):
            return False
        if not self.dirty():
            return True
        r = QMessageBox.question(self, 'Несохранённые изменения',
                                 f'В проекте есть несохранённые изменения. {what} без сохранения?',
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return r == QMessageBox.Yes

    def _reset_state(self):
        self._load_gen += 1
        self.view.clear()
        self.fixed = self.moving = None
        self.T_moving = None
        self.pairs, self.pending = [], None
        self.selection, self._sel_frusta = {}, []
        self.ghost_shown = set()
        self.candidates, self.cand_scan = [], None
        self.tree_sel = None
        self._vis_backup = None
        self.set_mode('inspect')

    def on_new_project(self):
        if not self.confirm_discard('Создать новый'):
            return
        self._reset_state()
        self.s = Session()
        self._saved_sig = self.s.state_signature()
        self.refresh_all()
        self.dock.set_candidates([])
        self.set_status('Новый проект: «Со сканера», «Bag» или «Добавить облако»')

    def on_open(self):
        if not self.confirm_discard('Открыть другой проект'):
            return
        p, _ = QFileDialog.getOpenFileName(self, 'Открыть проект', self._start_dir(),
                                           'Проект Pulse (*.pulse *.json)')
        if p:
            self.open_project(p)

    def open_project(self, path):
        self._reset_state()
        self.s = Session.from_project(path)
        self.settings.setValue('last_dir', str(Path(path).resolve().parent))
        self.refresh_all()
        self.load_all()

    def on_save(self):
        if not self.s.project_path:
            return self.on_save_as()
        self.s.save()
        self._saved_sig = self.s.state_signature()
        self._update_title()
        self.set_status(f'Сохранено: {self.s.project_path}')
        return True

    def on_save_as(self):
        p, _ = QFileDialog.getSaveFileName(self, 'Сохранить проект', self._start_dir(),
                                           'Проект с кешем анализа (*.pulse);;Проект без кеша (*.json)')
        if not p:
            return False
        if Path(p).suffix.lower() not in ('.pulse', '.json'):
            p += '.pulse'
        self.s.save(p)
        self._saved_sig = self.s.state_signature()
        self._update_title()
        self.set_status(f'Сохранено: {p}')
        return True

    def on_add_scan(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Добавить облако или bag', self._start_dir(),
                                                'Облака и bag (*.e57 *.pcd *.ply *.las *.laz *.mcap *.zstd)')
        clouds = [p for p in paths if not p.endswith(('.mcap', '.zstd'))]
        bags = [p for p in paths if p.endswith(('.mcap', '.zstd'))]
        if clouds:
            self._ingest([self.s.add_scan(p) for p in clouds])
        if bags:
            self.import_bags_dialog(bags[0])

    # ── импорт ───────────────────────────────────────────────────────────
    def _bags_dir(self):
        if self.s.project_path:
            return Path(self.s.project_path).resolve().parent / 'bags'
        return Path.home() / 'Pulse' / 'bags'

    def _scans_dir(self, first_bag):
        if self.s.project_path:
            return Path(self.s.project_path).resolve().parent / 'scans'
        b = Path(first_bag).resolve()
        return (b.parent.parent if b.is_file() else b.parent) / 'scans'

    def on_import_scanner(self):
        import bag_reconstruct as br
        if self.busy:
            self.set_status('Подождите: выполняется другая операция')
            return
        dlg = ScannerImportDialog(self, self.scanner_host, str(self._bags_dir()), br.DEFAULTS)
        ok = dlg.exec()
        self._set_scanner(dlg.host.text().strip() or 'pulse.local', bool(dlg.bags))
        if not ok:
            return
        v = dlg.values()
        if v['names']:
            self.download_and_import(v)

    def _set_scanner(self, host, ok):
        self.scanner_host = host
        self.settings.setValue('scanner_host', host)
        self.st_host.setText(host)
        self.st_dot.setObjectName('LiveDot' if ok else 'LiveDotOff')
        W.restyle(self.st_dot)

    def download_and_import(self, v):
        names, client, dest = v['names'], v['client'], v['dest']
        self._dl_cancel = False
        self.downloading = True

        def work(progress):
            try:
                paths = []
                for k, n in enumerate(names):
                    def prog(f, msg, k=k):
                        progress((k + f) / len(names), f'[{k + 1}/{len(names)}] {msg}  (Esc — остановить)')
                    paths.append(client.download(n, dest, prog, cancel=lambda: self._dl_cancel))
                return paths
            finally:
                self.downloading = False

        def done(paths):
            self.set_status(f'скачано записей: {len(paths)} → {dest}')
            self.import_bags(paths, self._scans_dir(paths[0]), v)
        self.run_bg(f'загрузка {len(names)} записей со сканера…', work, done)

    def on_import_bags(self):
        p = QFileDialog.getExistingDirectory(self, 'Папка bag или папка с bag-ами', self._start_dir())
        if p:
            self.import_bags_dialog(p)

    def import_bags_dialog(self, path):
        import bag_reconstruct as br
        bags = br.find_bags(path)
        if not bags:
            self.set_status(f'bag не найден: {path}')
            return
        group = Path(path).name if len(bags) > 1 else 'Импорт'
        dlg = BagImportDialog(self, bags, group, str(self._scans_dir(bags[0])), br.DEFAULTS)
        if dlg.exec():
            v = dlg.values()
            self.import_bags(bags, v['dest'], v)

    def import_bags(self, bags, out_dir, v):
        import bag_reconstruct as br
        gname = v.get('group') or 'Импорт'
        gid = self.s.tree.add_group('root', gname, 'прочее')
        params = {'voxel': v['voxel'], 'min_range': br.DEFAULTS['min_range'], 'max_range': v['max_range'],
                  'tilt': v['tilt']}
        added = []

        def work(progress):
            for k, b in enumerate(bags):
                name = br.bag_name(b)
                out = Path(out_dir) / f'{name}.ply'
                meta_path = out.with_suffix('.json')
                meta = None
                if out.exists() and meta_path.exists():
                    try:
                        old = json.loads(meta_path.read_text(encoding='utf-8'))
                        if old.get('params') == params and Path(b).stat().st_mtime <= out.stat().st_mtime:
                            meta = old
                    except Exception:                    # noqa: BLE001
                        meta = None
                if meta is None:
                    def prog(f, msg, k=k):
                        progress((k + f) / len(bags), f'[{k + 1}/{len(bags)}] {msg}')
                    try:
                        P, meta = br.reconstruct(b, params['voxel'], params['min_range'], params['max_range'],
                                                 params['tilt'], prog)
                    except Exception as e:               # noqa: BLE001
                        bg.post(lambda e=e, name=name: self.set_status(f'{name}: пропущен ({e})'))
                        continue
                    br.save_scan(P, out, meta)
                bg.post(lambda out=out, meta=meta: added.append(self._add_imported(out, meta, gid)))
            return True

        def done(_):
            QTimer.singleShot(0, lambda: self._import_done(added, out_dir, v.get('auto', True)))
        self.run_bg(f'импорт {len(bags)} bag…', work, done)

    def _import_done(self, added, out_dir, auto):
        scans = [sc for sc in added if sc is not None]
        self.refresh_all()
        self.set_status(f'импортировано сканов: {len(scans)} → {out_dir}; анализ в фоне…')
        self._ingest(scans, show=False, then_auto=auto)

    def _add_imported(self, path, meta, gid):
        if self.s.by_id(Path(path).name) is not None:
            return None
        sc = self.s.add_scan(path, source=meta, group=gid)
        sc._display = sc.quick_points(quick_voxel())
        self.s.apply_visibility()
        self._show_scan(sc)
        self.tree_panel.rebuild()
        if len(self.s.scans) == 1:
            self.view_top()
        return sc

    def _ingest(self, scans, show=True, then_auto=False):
        if not scans:
            return
        if show:
            for sc in scans:
                if getattr(sc, '_display', None) is None:
                    sc._display = sc.quick_points(quick_voxel())
                self._show_scan(sc)
            self.refresh_all()
            if len(self.s.scans) == len(scans):
                self.view_top()
        todo = [sc for sc in scans if not sc.analyzed]
        if then_auto:
            self._after_analysis = self.on_auto
        if todo:
            self._analyze_bg(todo, self._load_gen)
        elif then_auto:
            self._after_analysis = None
            self.on_auto()

    # ── дерево ───────────────────────────────────────────────────────────
    def on_tree_select(self, key):
        self.tree_sel = key
        self.refresh_inspector()
        sc = self.s.by_id(key) if key else None
        if sc is not None and self.mode == 'clean':
            self.clean_scan = sc
            self.refresh_clean()

    def on_tree_visible(self, key, vis):
        self._vis_backup = None
        self.s.set_visible(key, vis)
        self.apply_visibility()
        self.tree_panel.rebuild()

    def on_tree_move_drop(self, key, gid):
        if not self.s.move_node(key, gid):
            self.set_status('нельзя переместить сюда (группу нельзя вложить в саму себя)')
            return
        self.apply_visibility()
        self.tree_panel.rebuild()
        g = self.s.tree.group(gid)
        self.set_status(f'перемещено в «{g.name if g and g is not self.s.tree.root else "корень"}»')

    def _selected_group(self, key=None):
        t = self.s.tree
        key = key if key is not None else self.tree_sel
        if key is None:
            return 'root'
        if t.group(key) is not None:
            return key
        p = t.parent(key)
        return p.id if p is not None else 'root'

    def on_tree_action(self, name, key):
        t = self.s.tree
        if name == 'new_group':
            parent = self._selected_group(key)
            d = AskDialog(self, 'Новая группа', [('Имя', 'text', '', None), ('Тип', 'combo', 'комната', KINDS)],
                          'mdi6.folder-plus-outline', 'Создать')
            if d.exec():
                nm, kind = d.values()
                gid = t.add_group(parent, nm.strip() or 'Группа', kind)
                self.tree_sel = gid
                self.tree_panel.rebuild()
                self.tree_panel.select(gid)
                self.set_status(f'группа «{t.group(gid).name}» создана; сканы — перетаскиванием в неё')
        elif name == 'rename':
            g = t.group(key)
            if g is None or g is t.root:
                return
            kinds = KINDS if g.kind in KINDS else KINDS + [g.kind]
            d = AskDialog(self, 'Переименовать группу', [('Имя', 'text', g.name, None),
                                                         ('Тип', 'combo', g.kind, kinds)])
            if d.exec():
                nm, kind = d.values()
                t.rename(g.id, nm.strip() or g.name, kind)
                self.tree_panel.rebuild()
                self.refresh_inspector()
        elif name == 'delete_group':
            g = t.group(key)
            if g is None or g is t.root:
                return
            self.s.delete_group(g.id)
            self.tree_sel = None
            self.apply_visibility()
            self.tree_panel.rebuild()
            self.set_status(f'группа «{g.name}» удалена, её содержимое перешло к родителю')
        elif name == 'move':
            choices = t.group_choices()
            labels = [lab for _, lab in choices]
            d = AskDialog(self, 'Переместить в группу', [('Группа', 'combo', labels[0], labels)],
                          'mdi6.folder-move-outline', 'Переместить')
            if d.exec():
                gid = dict((lab, i) for i, lab in choices).get(d.values()[0])
                if gid is not None:
                    self.on_tree_move_drop(key, gid)
        elif name == 'make_ref':
            self.make_ref(key)
        elif name == 'only':
            self._vis_backup = None
            self.s.only_show([key])
            self.apply_visibility()
            self.tree_panel.rebuild()
        elif name == 'show_all':
            self.show_all()
        elif name == 'export_branch':
            self.on_export(key)
        elif name == 'manual':
            self.start_manual(moving_id=key)
        elif name == 'candidates':
            self.on_cand_search(key)
        elif name == 'fly_to':
            self.fly_to_scan(key)
        elif name == 'focus':
            self.focus_node(key)

    def focus_node(self, key):
        ids = self.s.tree.scans_in(key) if self.s.tree.group(key) is not None else [key]
        pts = []
        for sid in ids:
            sc = self.s.by_id(sid)
            T = self.pose_of(sc) if sc else None
            d = getattr(sc, '_display', None) if sc else None
            if T is not None and d is not None and len(d):
                pts.append(pr.transform(d[::20], T))
        if pts:
            P = np.vstack(pts)
            R = self.view.basis()
            self.view.fit(np.percentile(P, 1, axis=0), np.percentile(P, 99, axis=0), R[:, 2], R[:, 1])

    def make_ref(self, sid):
        sc = self.s.by_id(sid) if sid else None
        if sc is None or sc.pose is None:
            self.set_status('опорным может быть только размещённый скан')
            return
        self._with_plan_change(lambda: self.s.set_frame(sc.id))
        self.redraw_all()
        self.refresh_all()
        self.set_status(f'опорный скан: {sc.id}')

    def show_all(self):
        self._vis_backup = None
        self.s.show_all()
        self.apply_visibility()
        self.tree_panel.rebuild()

    def only_show(self, ids):
        t = self.s.tree
        if self._vis_backup is None:
            self._vis_backup = ({g.id: g.visible for g in t.groups()}, {l.scan: l.visible for l in t.leaves()})
        self.s.only_show(list(ids))
        self.apply_visibility()
        self.tree_panel.rebuild()

    def restore_visibility(self):
        if self._vis_backup is None:
            return
        groups, leaves = self._vis_backup
        t = self.s.tree
        for g in t.groups():
            g.visible = groups.get(g.id, True)
        for l in t.leaves():
            l.visible = leaves.get(l.scan, True)
        self._vis_backup = None
        self.s.apply_visibility()
        self.apply_visibility()
        self.tree_panel.rebuild()

    # ── инспектор ────────────────────────────────────────────────────────
    def refresh_inspector(self):
        key = self.tree_sel
        s = self.s
        sc = s.by_id(key) if key else None
        g = s.tree.group(key) if key else None
        if sc is not None:
            col = self.scan_color(sc.id)[1]
            path = s.tree.path(sc.id) or ''
            src = []
            m = sc.source or {}
            if m:
                lv = m.get('level', {})
                src = [('кадров', f"{m.get('frames_used', '?')} / {m.get('frames_total', '?')}"),
                       ('оборотов', m.get('rotations', '?')),
                       ('наклон оси', f"{lv.get('tilt_deg', 0)}° · {lv.get('method', '')}"),
                       ('угол платформы', m.get('angle_source', '?')),
                       ('точек', f"{m.get('points', 0) / 1e6:.2f} млн"),
                       ('обработан', m.get('processed', ''))]
            ana = []
            if sc.analyzed:
                pl = sc.planes
                ops = sc.openings
                odesc = ', '.join(f"{'окно' if o.kind == 'window' else 'дверь'} {o.width:.2f}×{o.height:.2f}"
                                  for o in ops[:3]) + (' …' if len(ops) > 3 else '')
                refl = [r for r in sc.reflect_report if r.get('mirror')]
                ana = [('плоскостей', len(pl)), ('проёмов', odesc or 'нет'),
                       ('отражения', f"{len(refl)} · убрано {sum(r.get('removed', 0) for r in refl):,} т."
                        if refl else 'нет'),
                       ('ручная чистка', f'{len(sc.erase)} обл.' if sc.erase else 'нет'),
                       ('вертикаль файла', sc.up)]
            else:
                ana = [('анализ', 'выполняется в фоне…')]
            edges = [e for e in s.edges if sc.id in (e['A'], e['B'])]
            act = [e for e in edges if s.edge_active(e)]
            links = [('рёбер', f'{len(act)} активных из {len(edges)}')]
            auto = [e for e in act if e.get('method') != 'manual']
            if auto:
                best = max(auto, key=lambda e: e.get('score', 0))
                other = best['B'] if best['A'] == sc.id else best['A']
                links.append(('лучшая пара', f"{short(other)} · {best['score']:+.2f}"))
            T = s.Tc(sc)
            if T is not None:
                links.append(('наклон оси', f'{Session.tilt_deg(T):.2f}°'))
                links.append(('положение', f'{T[0, 3]:.2f}, {T[1, 3]:.2f}, {T[2, 3]:.2f}'))
            self.inspector.show_scan(sc.id, col, self.status_chip(sc.id), path, src, ana, links,
                                     sc.pose is not None, sc.id == s.frame)
        elif g is not None and g is not s.tree.root:
            ids = s.tree.scans_in(g.id)
            placed = sum(1 for i in ids if s.by_id(i) and s.by_id(i).pose is not None)
            self.inspector.show_group(g.name, g.kind, len(ids), [('размещено', f'{placed} из {len(ids)}'),
                                                                  ('путь', s.tree.path(g.id) or g.name)])
        else:
            loops = s.loops() if s.edges else []
            bad = [l for l in loops if not l['ok']]
            rows = [('сканов', len(s.scans)), ('размещено', len(s.placed())),
                    ('опорный', short(s.frame) if s.frame else '—'),
                    ('рёбер', f'{len(s.active_edges())} активных из {len(s.edges)}'),
                    ('циклов', f'{len(loops)}, несогласованных {len(bad)}'),
                    ('горизонт', f'поправка {np.degrees(np.arccos(np.clip(s.level[2, 2], -1, 1))):.2f}°'),
                    ('файл', s.project_path or 'не сохранён')]
            self.inspector.show_project(rows, bool(s.scans))

    def on_inspector_action(self, name):
        key = self.tree_sel
        if name in ('rename', 'only', 'new_group', 'export_branch', 'make_ref', 'manual', 'candidates',
                    'fly_to'):
            return self.on_tree_action(name, key)
        if name == 'clean':
            self.clean_scan = self.s.by_id(key)
            return self.set_mode('clean')
        {'import_scanner': self.on_import_scanner, 'import_bags': self.on_import_bags,
         'add_scan': self.on_add_scan, 'auto': self.on_auto, 'export': self.on_export,
         'level_project': self.on_level_project, 'level_reset': self.on_level_reset,
         'align_plan': self.on_align_plan, 'rotate_plan+': lambda: self.on_rotate_plan(1),
         'rotate_plan-': lambda: self.on_rotate_plan(-1)}.get(name, lambda: None)()

    def _with_plan_change(self, fn):
        L0 = self.s.level.copy()
        r = fn()
        if self.T_moving is not None:
            D = np.eye(4)
            D[:3, :3] = self.s.level @ L0.T
            self.T_moving = D @ self.T_moving
        return r

    def on_level_project(self):
        info = self._with_plan_change(self.s.level_project)
        self.redraw_all()
        self.refresh_inspector()
        self.set_status(f"горизонт проекта: поправка {info['tilt_deg']:.2f}° по {info['horizontal']} гориз. "
                        f"и {info['walls']} верт. плоскостям (сохраняется в проекте)")

    def on_level_reset(self):
        self._with_plan_change(self.s.reset_level)
        self.redraw_all()
        self.refresh_inspector()
        self.set_status('горизонт проекта сброшен')

    def on_align_plan(self):
        d = self._with_plan_change(self.s.align_plan_to_walls)
        if d is None:
            self.set_status('нет стен для выравнивания плана')
            return
        self.redraw_all()
        self.view_top()
        self.set_status(f'план повёрнут на {d:+.2f}°: стены вдоль осей X/Y (сохраняется в проекте)')

    def on_rotate_plan(self, sign):
        st = self.inspector.plan_step.value() * sign
        self._with_plan_change(lambda: self.s.rotate_plan(st))
        self.redraw_all()
        self.set_status(f'план повёрнут на {st:+.2f}°')

    # ── режимы правой панели ─────────────────────────────────────────────
    def set_mode(self, mode, force=False):
        if mode == self.mode and not force:
            return
        if self.mode == 'clean' and mode != 'clean' and self.select_mode:
            self.toggle_select()
        if self.mode == 'manual' and mode != 'manual' and self.moving is not None and not force:
            if self.on_manual_cancel() and mode != 'inspect':
                self.set_mode(mode)
            else:
                self.b_clean.setChecked(self.mode == 'clean')
            return
        self.mode = mode
        self.right.setCurrentWidget({'inspect': self.inspector, 'manual': self.manual,
                                     'clean': self.clean}[mode])
        self.b_manual.setChecked(mode == 'manual')
        self.b_clean.setChecked(mode == 'clean')
        if mode == 'clean':
            if self.clean_scan is None or self.clean_scan not in self.s.scans:
                sel = self.s.by_id(self.tree_sel) if self.tree_sel else None
                self.clean_scan = sel or (self.s.scans[0] if self.s.scans else None)
            self.refresh_clean()
        self._banner()

    def _banner(self):
        if self.view.fly:
            text = ''
        elif self.select_mode:
            text = 'Выделение: тяните левой кнопкой, Shift — добавить, Delete — удалить, Esc — выйти'
        elif self.mode == 'manual' and self.moving is not None:
            text = (f'Ручная стыковка {short(self.fixed.id)} ← {short(self.moving.id)} · '
                    f'Ctrl/⌘ + клик — признак, Shift + тянуть — сдвиг, Alt + тянуть — поворот')
        else:
            text = ''
        self.vp.set_banner(text)

    # ── пары ─────────────────────────────────────────────────────────────
    def refresh_pairs(self):
        rows = []
        for e in self.s.edges:
            active = self.s.edge_active(e)
            user = e.get('user')
            if user == 'reject':
                st = ('отклонено', 'bad')
            elif e.get('method') == 'manual':
                st = ('вручную', 'acc') if active else ('вручную', 'mut')
            elif user == 'accept':
                st = ('принято', 'ok')
            else:
                st = ('активно', 'ok') if active else ('неактивно', 'mut')
            ab = f"{short(e['A'])} – {short(e['B'])}"
            if e.get('method') == 'manual':
                rows.append((ab, '—', '—', '—', '—', e.get('source', 'ручная'), st))
            else:
                rows.append((ab, f"{e.get('score', 0):+.2f}", f"{e.get('violations', 0):.3f}",
                             f"{e.get('margin', 0):.2f}",
                             f"{e['rmse'] * 100:.1f} см" if e.get('rmse') is not None else '—',
                             e.get('method', ''), st))
        self.dock.set_pairs(rows)
        loops = self.s.loops() if self.s.edges else []
        lrows = []
        worst = 0.0
        for l in loops:
            lrows.append(('→'.join(short(x) for x in l['loop']), f"{l['dt'] * 100:.1f}", f"{l['deg']:.2f}",
                          ('согласован', 'ok') if l['ok'] else ('расхождение', 'bad')))
            worst = max(worst, l['dt'])
        bad = sum(1 for l in loops if not l['ok'])
        if not loops:
            chip = ('', 'mut')
        elif bad:
            chip = (f'несогласованных циклов: {bad}', 'bad')
        else:
            chip = (f'невязка циклов ≤ {worst * 100:.1f} см', 'ok')
        self.dock.set_loops(lrows, chip)

    def on_auto(self):
        if not self.s.scans:
            self.set_status('нет сканов для стыковки')
            return
        if self.analyzing:
            self._after_analysis = self.on_auto
            self.set_status('автостыковка начнётся после фонового анализа')
            return
        reuse = self.a_reuse.isChecked()
        by_tree = self.a_bytree.isChecked() and not self.s.tree.is_flat()

        def work(progress):
            self.s.run_auto(progress, reuse=reuse, by_tree=by_tree)
            return True

        def done(_):
            self.redraw_all()
            self.refresh_all()
            self.set_status(f'Автостыковка: размещено {len(self.s.placed())} из {len(self.s.scans)}')
        self.run_bg('автостыковка…', work, done)

    def on_pair_selected(self, i):
        if 0 <= i < len(self.s.edges) and self.mode != 'manual':
            e = self.s.edges[i]
            self.only_show({e['A'], e['B']})
            self.set_status(f"пара {short(e['A'])} – {short(e['B'])}: показаны только эти два скана "
                            f"(Esc — вернуть видимость)", log=False)

    def on_pair_action(self, name, i):
        if not (0 <= i < len(self.s.edges)):
            return
        e = self.s.edges[i]
        if name == 'show':
            return self.on_pair_selected(i)
        if name == 'manual':
            a, b = self.s.by_id(e['A']), self.s.by_id(e['B'])
            fixed, moving = (a, b) if a.id == self.s.frame or b.pose is None else (a, b)
            return self.start_manual(fixed.id, moving.id)
        self.s.set_edge_user(i, {'accept': 'accept', 'reject': 'reject', 'auto': None}[name])
        self.redraw_all()
        self.refresh_all()

    # ── ручная стыковка ──────────────────────────────────────────────────
    def manual_items(self):
        return [(sc.id, short(sc.id), self.scan_color(sc.id)[1], sc.pose is not None) for sc in self.s.scans]

    def start_manual(self, fixed_id=None, moving_id=None, T_init=None):
        """Начать ручную стыковку; по умолчанию подвижный — выбранный в дереве или неразмещённый."""
        if self.mode == 'manual' and self.moving is not None and fixed_id is None and moving_id is None:
            if not self.on_manual_cancel():
                self.b_manual.setChecked(True)          # остались в ручной стыковке
            return
        if self.moving is not None and (fixed_id, moving_id) != (self.fixed.id, self.moving.id):
            if not self.confirm_leave_manual('Перейти к другой паре сканов'):
                self.manual.set_scans(self.manual_items(), self.fixed.id, self.moving.id)
                self.b_manual.setChecked(True)
                return
        s = self.s
        if moving_id is None:
            sel = s.by_id(self.tree_sel) if self.tree_sel else None
            if sel is not None and sel.id != s.frame:
                moving_id = sel.id
            else:
                un = [sc.id for sc in s.scans if sc.pose is None]
                moving_id = un[0] if un else next((sc.id for sc in s.scans if sc.id != s.frame), None)
        if fixed_id is None:
            fixed_id = s.frame if s.frame != moving_id else next(
                (sc.id for sc in s.placed() if sc.id != moving_id), None)
        fixed, moving = s.by_id(fixed_id), s.by_id(moving_id)
        if fixed is None or moving is None or fixed is moving or fixed.pose is None:
            self.b_manual.setChecked(False)
            self.set_status('Нужны размещённый неподвижный и другой подвижный скан')
            return
        if not (fixed.analyzed and moving.analyzed):
            self.b_manual.setChecked(False)
            self.set_status('Дождитесь окончания анализа этих сканов')
            return
        self._clear_markers()
        self.fixed, self.moving = fixed, moving
        self.T_moving = np.asarray(T_init) if T_init is not None else (
            s.Tc(moving) if moving.pose is not None else s.Tc(fixed))
        self._manual_T0 = self.T_moving.copy()
        self.pairs, self.pending = [], None
        self.manual_score = None
        self.mode = 'inspect'
        self.set_mode('manual')
        self.manual.set_scans(self.manual_items(), fixed.id, moving.id)
        self.only_show({fixed.id, moving.id})
        self.redraw_all()
        self.refresh_manual()
        self.live_score()
        self._banner()
        self.set_status('Ctrl/⌘ + клик по облаку: признак в неподвижном, затем такой же в подвижном')

    def refresh_manual(self):
        rows = [(f'{fdesc(a)}  ↔  {fdesc(b)}', pick_rgb(i)[1]) for i, (a, b) in enumerate(self.pairs)]
        pending = (fdesc(self.pending), pick_rgb(len(self.pairs))[1]) if self.pending else None
        self.manual.set_pairs(rows, pending)
        if self.fixed is not None:
            self.manual.set_dof(self.s.dof_status(self.fixed, self.moving, self.pairs))
        self._score_rows()

    def _score_rows(self):
        r = self.manual_score
        rows = []
        if r is None:
            rows.append(('оценка', '…'))
        elif 'error' in r:
            rows.append(('ошибка', r['error']))
        elif r.get('none'):
            rows.append(('оценка', 'нет других размещённых сканов'))
        else:
            rows += [('оценка', f"{r['score']:+.3f}"), ('совпало точек', f"{r['n_close']:,}".replace(',', ' ')),
                     ('нарушений', f"{r['violations']:.3f}")]
        if self.T_moving is not None:
            rows.append(('наклон', f'{Session.tilt_deg(self.T_moving):.2f}°'))
        self.manual.set_score(rows)

    def on_manual_action(self, name, arg):
        if name == 'restart':
            f, m = self.manual.current()
            if f and m and f != m:
                return self.start_manual(f, m)
            return
        if self.moving is None:
            return
        if name == 'nudge':
            st, sd = self.manual.step_m.value(), self.manual.step_deg.value()
            kw = arg
            self.T_moving = Session.nudge(self.T_moving, kw.get('dx', 0) * st, kw.get('dy', 0) * st,
                                          kw.get('dz', 0) * st, kw.get('dyaw', 0) * sd,
                                          kw.get('droll', 0) * sd, kw.get('dpitch', 0) * sd)
            self._update_moving()
            self.live_score()
        elif name == 'solve':
            self.on_manual_solve()
        elif name == 'icp':
            self.on_manual_icp()
        elif name == 'clear':
            self.pairs, self.pending = [], None
            self._clear_markers()
            self.refresh_manual()
        elif name == 'remove_pair':
            if 0 <= arg < len(self.pairs):
                self.pairs.pop(arg)
                self._redraw_markers()
                self._pairs_changed()
        elif name == 'level_moving':
            T2, info = self.s.level_pose(self.moving, self.T_moving)
            if info['horizontal'] + info['walls'] == 0:
                self.set_status('нет подходящих плоскостей (пол/земля, стены) для выравнивания')
                return
            before = Session.tilt_deg(self.T_moving)
            self.T_moving = T2
            self._update_moving()
            self.live_score()
            self.set_status(f"выровнено по {info['horizontal']} гориз. и {info['walls']} верт. плоскостям: "
                            f"наклон {before:.2f}° → {Session.tilt_deg(T2):.2f}°")
        elif name == 'snap_yaw':
            T2, d = self.s.snap_yaw_to_walls(self.moving, self.T_moving)
            if d is None:
                self.set_status('нет стен для доворота')
                return
            self.T_moving = T2
            self._update_moving()
            self.live_score()
            self.set_status(f'подвижный довёрнут по стенам на {d:+.2f}°')
        elif name == 'accept':
            self.on_manual_accept()
        elif name == 'cancel':
            self.on_manual_cancel()

    def _update_moving(self):
        if self.moving is None:
            return
        n = f'scan:{self.moving.id}'
        if self.view.has(n):
            self.view.set_transform(n, self.T_moving)
        else:
            self._show_scan(self.moving)
        for name in list(self.view.items):
            if name.endswith(':B') and name.startswith('pick:') or name in (
                    f'feat:planes:{self.moving.id}', f'feat:openings:{self.moving.id}'):
                self.view.set_transform(name, self.T_moving)
        self._score_rows()

    def live_score(self):
        if self.moving is None:
            return
        if self._score_pending:
            self._score_again = True
            return
        self._score_pending = True
        moving, T = self.moving, self.T_moving.copy()

        def work():
            r = self.s.score_pose(moving, T)
            return {'none': True} if r is None else r

        def done(r):
            self._score_pending = False
            if moving is self.moving:
                self.manual_score = r
                self._score_rows()
            if self._score_again:
                self._score_again = False
                self.live_score()

        def fail(e):
            self._score_pending = False
            self.manual_score = {'error': str(e)}
            self._score_rows()
        bg.run(work, done, fail)

    def _pairs_changed(self):
        self.refresh_manual()
        if self.manual.auto_solve.isChecked() and self.pairs and \
                self.s.dof_status(self.fixed, self.moving, self.pairs)['text'].startswith('всё'):
            self.on_manual_solve()

    def on_manual_solve(self):
        if not self.pairs:
            self.set_status('сначала выберите пары объектов (Ctrl/⌘ + клик)')
            return
        fixed, moving, pairs, T0 = self.fixed, self.moving, list(self.pairs), self.T_moving
        full = self.manual.full_rot.isChecked()

        def done(r):
            T, info = r
            if moving is not self.moving:
                return
            self.T_moving = T
            self._update_moving()
            extra = ''
            if info.get('weak'):
                extra = ' — есть неопределённые направления, уточните ICP или добавьте пару'
            if info.get('method') == 'opening':
                side = ('с одной стороны стены' if info['same_side']
                        else f"сквозь стену, толщина {info['delta']:.2f} м")
                extra = f" — проём {side}, нарушений {info['violations']:.3f}"
            rot = 'полный поворот' if info.get('rotation') == '6dof' else 'только вокруг вертикали'
            self.set_status(f"решено ({rot}): yaw {np.degrees(np.arctan2(T[1, 0], T[0, 0])):.1f}°, "
                            f"наклон {Session.tilt_deg(T):.2f}°{extra}")
            self.live_score()
        self.run_bg('решение позы…', lambda p: self.s.solve_manual(fixed, moving, pairs, T_init=T0, full=full),
                    done)

    def on_manual_icp(self):
        if self.moving is None:
            return
        fixed, moving, T0 = self.fixed, self.moving, self.T_moving
        others = [s for s in self.s.placed() if s is not moving]

        def done(r):
            T, info = r
            if moving is not self.moving:
                return
            self.T_moving = T
            self._update_moving()
            dt, da = info['shift']
            self.set_status(f"ICP: fitness {info['fitness']:.2f}, rmse {info['rmse'] * 100:.1f} см, "
                            f"сдвиг {dt * 100:.1f} см {da:.2f}°")
            self.live_score()
        self.run_bg('ICP…', lambda p: self.s.refine_icp(others or [fixed], moving, T0), done)

    def on_manual_accept(self):
        if self.moving is None:
            return
        moving, fixed, T = self.moving, self.fixed, self.T_moving

        def done(_):
            self.on_manual_cancel(ask=False)
            self.set_status(f'{short(moving.id)}: поза принята (ручное ребро к {short(fixed.id)})')
        self.run_bg('пересчёт графа…', lambda p: self.s.accept_pose(moving, T, anchor=fixed, method='ручная'),
                    done)

    def manual_dirty(self):
        """Поза подвижного изменена после входа в ручную стыковку и не принята."""
        return (self.moving is not None and self.T_moving is not None and self._manual_T0 is not None
                and not np.allclose(self.T_moving, self._manual_T0, atol=1e-7))

    def confirm_leave_manual(self, what='Выйти из ручной стыковки'):
        """
        Перед уходом из ручной стыковки с непринятой позой — вопрос пользователю.
        True — можно уходить (поза принята или изменения отброшены), False — остаться.
        """
        if not self.manual_dirty():
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle('Поза не принята')
        box.setText(f'Поза скана {short(self.moving.id)} изменена, но не принята.')
        box.setInformativeText(f'{what}: принять новую позу или отказаться от изменений?')
        b_acc = box.addButton('Принять позу', QMessageBox.AcceptRole)
        b_drop = box.addButton('Не принимать', QMessageBox.DestructiveRole)
        b_stay = box.addButton('Остаться', QMessageBox.RejectRole)
        box.setDefaultButton(b_acc)
        box.setEscapeButton(b_stay)
        box.exec()
        if box.clickedButton() is b_acc:
            return self._accept_now()
        if box.clickedButton() is b_drop:
            self.set_status(f'{short(self.moving.id)}: изменения позы не приняты')
            return True
        return False

    def _accept_now(self):
        """Принять позу подвижного сразу (без фоновой задачи) — перед уходом из ручной стыковки."""
        if self.busy:
            self.set_status('Подождите окончания текущей операции и примите позу ещё раз')
            return False
        moving, fixed, T = self.moving, self.fixed, self.T_moving
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.s.accept_pose(moving, T, anchor=fixed, method='ручная')
        except Exception as e:                           # noqa: BLE001
            self.set_status(f'поза не принята: {e}')
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self._manual_T0 = T.copy()
        self.set_status(f'{short(moving.id)}: поза принята (ручное ребро к {short(fixed.id)})')
        return True

    def on_manual_cancel(self, ask=True):
        """Выйти из ручной стыковки. → False, если пользователь решил остаться."""
        if ask and not self.confirm_leave_manual('Выйти из ручной стыковки'):
            return False
        self.moving = self.fixed = None
        self.T_moving = None
        self._manual_T0 = None
        self.pairs, self.pending = [], None
        self._clear_markers()
        self.mode = 'manual'
        self.set_mode('inspect', force=True)
        self.restore_visibility()
        self.redraw_all()
        self.refresh_all()
        return True

    def _scan_of(self, name):
        return self.s.by_id(name[5:]) if name and name.startswith('scan:') else None

    def pick_world(self, W_, scan):
        """Признак в точке W_ (общая система) скана scan — вызывается и из тестов."""
        if self.moving is None or scan not in (self.fixed, self.moving):
            return None
        T = self.pose_of(scan)
        p = pr.transform(np.asarray(W_)[None], np.linalg.inv(T))[0]
        kind = self.manual.kind.value()
        f = self.s.pick_feature(scan, p, kind)
        if f is None:
            self.set_status(f'здесь нет признака «{dict(auto="авто", plane="плоскость", opening="проём", point="точка")[kind]}»')
            return None
        k = len(self.pairs)
        if scan is self.fixed:
            self.pending = f
            self._draw_feature(f, self.s.Tc(self.fixed), pick_rgb(k)[0], f'pick:{k}:A')
            self.set_status(f'неподвижный: {fdesc(f)} — теперь такой же признак в подвижном')
            self.refresh_manual()
            return f
        if self.pending is None:
            self.set_status('сначала выберите признак в неподвижном скане')
            return None
        why = incompatible(self.pending, f)
        if why:
            self.set_status(f'пара не принята: {why}. Выберите другой признак в подвижном')
            return None
        self.pairs.append((self.pending, f))
        self._draw_feature(f, self.T_moving, pick_rgb(k)[0], f'pick:{k}:B')
        self.pending = None
        self.set_status(f'пара {k + 1}: {fdesc(self.pairs[-1][0])} ↔ {fdesc(f)}')
        self._pairs_changed()
        return f

    def _draw_feature(self, f, T, color, name):
        scan = self.s.by_id(f['scan'])
        if f['type'] == 'plane':
            pl = next(p for p in scan.planes if p.id == f['index'])
            self.view.set_cloud(name, scan.res['down'][pl.inliers], color, T, size=5)
        elif f['type'] == 'opening':
            o = scan.openings[f['index']]
            self.view.set_lines(name, np.asarray(o.corners), [[0, 1], [1, 2], [2, 3], [3, 0], [0, 2], [1, 3]],
                                None, T, 4, color)
        else:
            self.view.set_sphere(name, f['p'], 0.06, color, T)

    def _clear_markers(self):
        self.view.remove_prefix('pick:')

    def _redraw_markers(self):
        self._clear_markers()
        for k, (fa, fb) in enumerate(self.pairs):
            c = pick_rgb(k)[0]
            self._draw_feature(fa, self.s.Tc(self.fixed), c, f'pick:{k}:A')
            self._draw_feature(fb, self.T_moving, c, f'pick:{k}:B')

    # ── мышь в 3D-виде ───────────────────────────────────────────────────
    def _mouse_hook(self, kind, ev):
        mods = ev.modifiers()
        pos = ev.position()
        x, y = pos.x(), pos.y()
        manual = self.mode == 'manual' and self.moving is not None and self.T_moving is not None
        if kind == 'press' and ev.button() == Qt.LeftButton:
            if manual and mods & (Qt.ControlModifier | Qt.MetaModifier):
                # сначала — скан, который ожидается по шагу (неподвижный, затем подвижный):
                # сканы перекрываются, и ближайшая к глазу точка часто чужая
                want = self.moving if self.pending is not None else self.fixed
                hit = self.view.pick_point(x, y, names={f'scan:{want.id}'}) or \
                    self.view.pick_point(x, y, names={f'scan:{self.fixed.id}', f'scan:{self.moving.id}'})
                if hit is None:
                    self.set_status('мимо облака')
                else:
                    self.pick_world(hit[1], self._scan_of(hit[0]))
                return True
            if manual and mods & (Qt.ShiftModifier | Qt.AltModifier):
                z0 = float(self.T_moving[2, 3])
                self._drag = {'mode': 'rot' if mods & Qt.AltModifier else 'move', 'T0': self.T_moving.copy(),
                              'x0': x, 'g0': self.view.ray_plane_z(x, y, z0), 'z0': z0}
                return True
            if self.select_mode and not mods & (Qt.ControlModifier | Qt.MetaModifier):
                self._rect = [x, y, x, y, bool(mods & Qt.ShiftModifier)]
                self.view.rubber = tuple(self._rect[:4])
                self.view.update()
                return True
        if kind == 'press' and manual and ev.button() == Qt.RightButton and mods & Qt.ShiftModifier:
            self._drag = {'mode': 'rot', 'T0': self.T_moving.copy(), 'x0': x, 'g0': None, 'z0': 0}
            return True
        if kind == 'move':
            if self._drag is not None:
                d = self._drag
                if d['mode'] == 'move':
                    g = self.view.ray_plane_z(x, y, d['z0'])
                    if g is None or d['g0'] is None:
                        return True
                    delta = g - d['g0']
                    if np.linalg.norm(delta) > 200:
                        return True
                    self.T_moving = Session.nudge(d['T0'], delta[0], delta[1], 0.0)
                else:
                    self.T_moving = Session.nudge(d['T0'], dyaw_deg=-0.3 * (x - d['x0']))
                self._update_moving()
                return True
            if self._rect is not None:
                self._rect[2], self._rect[3] = x, y
                self.view.rubber = tuple(self._rect[:4])
                self.view.update()
                return True
        if kind == 'release':
            if self._drag is not None:
                self._drag = None
                self.live_score()
                return True
            if self._rect is not None:
                r = self._rect
                self._rect = None
                self.view.rubber = None
                self.view.update()
                if abs(r[2] - r[0]) > 3 and abs(r[3] - r[1]) > 3:
                    self.select_rect(r[:4], add=r[4])
                return True
        return False

    def _dblclick(self, x, y):
        hit = self.view.pick_point(x, y)
        if hit is not None and not self.view.fly:
            self.view.set_center(hit[1])
            self.set_status(f'центр вращения: {hit[1][0]:.2f}, {hit[1][1]:.2f}, {hit[1][2]:.2f}', log=False)

    # ── полёт ────────────────────────────────────────────────────────────
    def toggle_fly(self):
        on = not self.view.fly
        if on and self.view.basis()[2, 2] > 0.95 and self.s.frame:
            return self.fly_to_scan(self.s.frame)
        self.view.set_fly(on)
        self.b_fly.setChecked(on)
        self.vp.b_proj.setChecked(self.view.parallel)     # полёт — только в перспективе
        self.view.setFocus()
        self._banner()
        self.set_status(f'полёт: WASD, Space/E — вверх, C/Q — вниз, мышь — обзор, колесо — скорость '
                        f'({self.view.fly_speed:.1f} м/с), Esc — выход' if on else 'полёт выключен', log=False)

    def fly_to_scan(self, sid=None):
        sc = self.s.by_id(sid or self.s.frame)
        if sc is None or self.s.Tc(sc) is None:
            return
        T = self.s.Tc(sc)
        self.view.set_fly(True, eye=T[:3, 3], forward=T[:3, 0])
        self.b_fly.setChecked(True)
        self.vp.b_proj.setChecked(False)
        self.view.setFocus()
        self._banner()
        self.set_status(f'полёт из точки {short(sc.id)}: WASD, мышь — обзор, Esc — выход', log=False)

    # ── качество совмещения ──────────────────────────────────────────────
    def _quality_key(self):
        _, method = self.tree_panel.quality_params()
        return (method, self.s.state_signature(), tuple((sc.id, sc.visible, sc.analyzed) for sc in self.s.scans))

    def quality_refresh(self):
        """
        Слой качества: пересчёт в фоне, только если изменились позы, видимость, чистка
        или способ; иначе (ползунок порога) — перекраска уже посчитанного.
        """
        if not self.tree_panel.layer('quality'):
            self.view.remove('qual')
            self.dock.set_quality([])
            return
        key = self._quality_key()
        if self.qual['res'] is not None and self.qual['key'] == key:
            self._quality_draw()
            return
        if self.qual['busy']:
            return                                       # по окончании расчёта проверим ключ снова
        if len([sc for sc in self.s.placed() if sc.visible]) < 2:
            self.view.remove('qual')
            self.tree_panel.q_info.setText('нужны хотя бы два видимых размещённых скана')
            self.dock.set_quality([])
            return
        self.qual['busy'] = True
        self.tree_panel.q_info.setText('пересчёт…')
        method = key[0]
        sess = self.s
        t0 = time.time()

        def done(res):
            self.qual['busy'] = False
            if sess is not self.s:
                return
            self.qual['res'], self.qual['key'] = res, key
            self.tree_panel.q_info.setText(
                f"{'по плоскостям' if method == 'planes' else 'локально'}: ячеек {len(res):,}, "
                f'{time.time() - t0:.1f} c'.replace(',', ' '))
            self.quality_refresh()                       # ключ мог измениться за время расчёта

        def fail(e):
            self.qual['busy'] = False
            self.tree_panel.q_info.setText(f'ошибка: {e}')
        bg.run(lambda: quality.compute(sess, method), done, fail)

    def _quality_draw(self):
        thr, _ = self.tree_panel.quality_params()
        P, C = quality.colors(self.qual['res'], thr)
        self.view.set_cloud('qual', P, (1, 0.8, 0.2), colors=C, size=self.view.point_px + 1, on_top=True)
        self._qual_timer.start(150)                      # список мест — после остановки ползунка

    def _quality_areas(self):
        res = self.qual['res']
        if res is None or not self.tree_panel.layer('quality'):
            return
        thr, _ = self.tree_panel.quality_params()
        areas = quality.problem_areas(res, thr)
        self.qual['areas'] = areas
        kind_ru = {'wall': 'стена', 'floor': 'пол', 'ceiling': 'потолок', 'local': 'участок'}
        rows = [(kind_ru.get(a['kind'], a['kind']), f"{a['max'] * 100:.1f}", f"{a['median'] * 100:.1f}",
                 f"{a['area']:.2f}", ', '.join(short(x) for x in a['scans'])) for a in areas]
        self.dock.set_quality(rows, None if rows else f'толще {thr * 100:.1f} см мест нет')

    def on_quality_selected(self, i):
        areas = self.qual.get('areas') or []
        if not (0 <= i < len(areas)) or self.view.fly:
            return
        res, a = self.qual['res'], areas[i]
        C = res.center[a['cells']]
        lo, hi = C.min(axis=0) - 1.0, C.max(axis=0) + 1.0
        R = self.view.basis()
        self.view.fit(lo, hi, R[:, 2], R[:, 1])
        self.set_status(f"{a['kind']}: до {a['max'] * 100:.1f} см, сканы "
                        f"{', '.join(short(x) for x in a['scans'])}", log=False)

    # ── найденные объекты ────────────────────────────────────────────────
    def on_layer(self, key, on):
        if key == 'quality':
            if on:
                self.dock.tabs.setCurrentIndex(4)
            self.quality_refresh()
        elif key == 'grid':
            self.update_grid()
        elif key == 'ghosts':
            self.ghost_shown = {sc.id for sc in self.s.scans if sc.analyzed} if on else set()
            self.view.remove_prefix('ghost:')
            for sid in self.ghost_shown:
                self._draw_ghosts(self.s.by_id(sid))
        else:
            self.draw_features()
            if on and any(not s.analyzed for s in self.s.scans if s.visible):
                self.set_status('объекты появятся у остальных сканов после фонового анализа')

    def _feature_geoms(self, sc):
        key = (sc.clean, len(sc.erase))
        cache = getattr(sc, '_feat_qt', None)
        if cache is not None and cache[0] == key:
            return cache[1]
        down = sc.res['down']
        drop = sc.ghost_mask() if sc.clean else np.zeros(len(down), bool)
        if sc.erase:
            drop = drop | manual_clean.inside_regions(down, sc.erase)
        pts, segs, cols = [], [], []
        for p in sc.planes:
            if p.area < 1.0 or drop[p.inliers].mean() > 0.5:
                continue
            q = down[p.inliers]
            u, v, c0 = np.asarray(p.u_axis), np.asarray(p.v_axis), np.asarray(p.centroid)
            hull = convex_hull_2d(np.column_stack([(q - c0) @ u, (q - c0) @ v]))
            if len(hull) < 3:
                continue
            P = c0 + hull[:, :1] * u + hull[:, 1:] * v + 0.01 * np.asarray(p.normal)
            k0, n = len(pts), len(P)
            pts.extend(P)
            segs.extend([[k0 + i, k0 + (i + 1) % n] for i in range(n)])
            cols.extend([KIND_HUE.get(p.kind, KIND_HUE['other'])] * n)
        planes = (np.array(pts), segs, cols)
        pts, segs, cols = [], [], []
        for o in sc.openings:
            if sc.erase and manual_clean.inside_regions(np.asarray(o.center)[None], sc.erase)[0]:
                continue
            C = np.asarray(o.corners) + 0.02 * np.asarray(o.normal)
            k0 = len(pts)
            pts.extend(C)
            segs.extend([[k0, k0 + 1], [k0 + 1, k0 + 2], [k0 + 2, k0 + 3], [k0 + 3, k0], [k0, k0 + 2],
                         [k0 + 1, k0 + 3]])
            cols.extend([OPEN_COLOR.get(o.kind, (0.8, 0.2, 0.2))] * 6)
        opens = (np.array(pts), segs, cols)
        sc._feat_qt = (key, (planes, opens))
        return planes, opens

    def draw_features(self):
        show_p, show_o = self.tree_panel.layer('planes'), self.tree_panel.layer('openings')
        for sc in self.s.scans:
            T = self.pose_of(sc)
            for kind, on in (('planes', show_p), ('openings', show_o)):
                name = f'feat:{kind}:{sc.id}'
                if not on or T is None or not sc.visible or not sc.analyzed:
                    self.view.remove(name)
                    continue
                g = self._feature_geoms(sc)[0 if kind == 'planes' else 1]
                if g[1]:
                    self.view.set_lines(name, g[0], g[1], g[2], T, 2.5 if kind == 'planes' else 3)
                else:
                    self.view.remove(name)

    # ── чистка ───────────────────────────────────────────────────────────
    def refresh_clean(self):
        items = [(sc.id, short(sc.id), self.scan_color(sc.id)[1]) for sc in self.s.scans]
        sc = self.clean_scan
        self.clean.set_scans(items, sc.id if sc else None)
        if sc is None:
            self.clean.report.set([])
            return
        self.clean.sw_clean.setChecked(sc.clean)
        rows = []
        if sc.analyzed:
            for r in sc.reflect_report:
                if r['kind'] == 'window':
                    w, h = r['size']
                    rows.append((f'окно {w:.2f}×{h:.2f}', f"{r['match']:.2f} · "
                                 f"{'зеркало, −' + format(r['removed'], ',') if r['mirror'] else 'нет'}"))
                else:
                    rows.append(('пол', f"{r['match']:.2f} · "
                                 f"{'зеркало, −' + format(r['removed'], ',') if r['mirror'] else 'нет'}"))
            if not rows:
                rows = [('отражения', 'не найдены')]
        else:
            rows = [('анализ', 'выполняется…')]
        self.clean.report.set(rows)
        self.clean.b_ghosts.setText('Скрыть отражения' if sc.id in self.ghost_shown else 'Показать отражения красным')
        self.clean.b_select.setChecked(self.select_mode)

    def on_clean_action(self, name, arg):
        sc = self.clean_scan
        if name == 'scan':
            self.clean_scan = self.s.by_id(arg)
            self.refresh_clean()
        elif name == 'clean' and sc is not None:
            sc.clean = bool(arg)
            if sc.analyzed:
                sc._display = self.s.display_points(sc)
            self._show_scan(sc)
            self.draw_features()
        elif name == 'ghosts' and sc is not None and sc.analyzed:
            if sc.id in self.ghost_shown:
                self.ghost_shown.discard(sc.id)
                self.view.remove(f'ghost:{sc.id}')
            else:
                self.ghost_shown.add(sc.id)
                self._draw_ghosts(sc)
            self.refresh_clean()
        elif name == 'select':
            self.toggle_select()
        elif name == 'erase':
            self.on_erase()
        elif name == 'unselect':
            self.clear_selection()
        elif name == 'undo':
            self.on_undo_erase()
        elif name == 'reset_scan' and sc is not None:
            self.s.clear_erase(sc)
            self._refresh_scans([sc.id])
            self.set_status(f'{short(sc.id)}: ручная чистка сброшена')

    def _draw_ghosts(self, sc):
        if sc is None:
            return
        name = f'ghost:{sc.id}'
        T = self.s.Tc(sc)
        if T is None or not sc.analyzed or not sc.visible:
            self.view.remove(name)
            return
        self.view.set_cloud(name, sc.res['down'][sc.ghost_mask()], self.theme.rgb('bad'), T)

    def toggle_select(self):
        if self.mode != 'clean':
            self.set_mode('clean')
        self.select_mode = not self.select_mode
        self.clean.b_select.setChecked(self.select_mode)
        if not self.select_mode:
            self._rect = None
            self.view.rubber = None
            self.view.update()
        self._banner()

    def _select_targets(self):
        out = []
        for sc in self.s.scans:
            T = self.pose_of(sc)
            if T is None or not sc.visible or getattr(sc, '_display', None) is None:
                continue
            if self.clean.sw_all.isChecked() or sc is self.clean_scan:
                out.append((sc, T))
        return out

    def select_rect(self, rect, add=False):
        V, P, W_, H = self.view.view_proj()
        x0, y0, x1, y1 = rect
        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        near = [self.view.unproject(x, y, -1.0) for x, y in corners]
        far = [self.view.unproject(x, y, 0.5) for x, y in corners]
        planes = manual_clean.frustum_planes(near, far)
        if not add:
            self.selection, self._sel_frusta = {}, []
        self._sel_frusta.append(planes)
        n = 0
        for sc, Ts in self._select_targets():
            m = manual_clean.in_rect(pr.transform(sc._display, Ts), V, P, W_, H, rect)
            if sc.id in self.selection:
                m = m | self.selection[sc.id]
            if m.any():
                self.selection[sc.id] = m
                n += int(m.sum())
        self._draw_selection()
        self.clean.sel_label.setText(f'выделено {n:,} точек в {len(self.selection)} скан(ах)'.replace(',', ' ')
                                     if self.selection else 'ничего не выделено')
        return n

    def _draw_selection(self):
        self.view.remove_prefix('sel:')
        for sc in self.s.scans:
            m = self.selection.get(sc.id)
            if m is None or not m.any():
                continue
            self.view.set_cloud(f'sel:{sc.id}', sc._display[m], self.theme.rgb('bad'), self.pose_of(sc), size=4)

    def clear_selection(self):
        self.selection, self._sel_frusta = {}, []
        self._draw_selection()
        self.clean.sel_label.setText('')

    def _refresh_scans(self, ids):
        for sid in ids:
            sc = self.s.by_id(sid)
            if sc is not None and sc.analyzed:
                sc._display = self.s.display_points(sc)
        self.redraw_all()
        self.refresh_inspector()

    def on_erase(self):
        if not self.selection:
            return
        targets = [(sc, T) for sc, T in self._select_targets() if sc.id in self.selection]
        rec = self.s.erase_region(targets, self._sel_frusta)
        ids = [sid for sid, _ in rec]
        self.clear_selection()
        self._refresh_scans(ids)
        self.set_status(f"удалено из: {', '.join(short(i) for i in ids)} (Ctrl+Z — отменить)")

    def on_undo_erase(self):
        rec = self.s.undo_erase()
        if not rec:
            self.set_status('нечего отменять')
            return
        self._refresh_scans([sid for sid, _ in rec])
        self.set_status('удаление отменено')

    # ── кандидаты ────────────────────────────────────────────────────────
    def on_cand_search(self, sid):
        sc = self.s.by_id(sid) if sid else None
        if sc is None:
            return
        self.dock.tabs.setCurrentIndex(2)
        self.dock.set_cand_scans([(x.id, short(x.id)) for x in self.s.scans], sc.id)

        def done(c):
            self.candidates, self.cand_scan = c, sc
            self.dock.set_candidates([(str(i + 1), f"{x['score']:+.3f}", f"{x['n_close']:,}".replace(',', ' '),
                                       f"{x['violations']:.3f}", x['method']) for i, x in enumerate(c)])
            self.set_status(f'кандидатов: {len(c)} — выберите строку, чтобы посмотреть')
        self.run_bg(f'поиск кандидатов для {short(sc.id)}…', lambda p: self.s.candidates(sc, p), done)

    def on_cand_selected(self, i):
        if not (0 <= i < len(self.candidates)):
            return
        sc = self.cand_scan
        if getattr(sc, '_display', None) is None:
            return
        sc.visible = True
        self.view.set_cloud(f'scan:{sc.id}', sc._display, self.scan_color(sc.id)[0], self.candidates[i]['Tc'])

    def on_cand_action(self, name, i):
        if not (0 <= i < len(self.candidates)):
            self.set_status('выберите кандидата в таблице')
            return
        sc, T = self.cand_scan, self.candidates[i]['Tc']
        if name == 'manual':
            return self.start_manual(self.s.frame, sc.id, T)

        def done(_):
            self.redraw_all()
            self.refresh_all()
            self.set_status(f'{short(sc.id)}: кандидат принят')
        self.run_bg('пересчёт графа…', lambda p: self.s.accept_pose(sc, T, anchor=self.s.ref, method='кандидат'),
                    done)

    # ── экспорт ──────────────────────────────────────────────────────────
    def on_export(self, key=None):
        if not self.s.placed():
            self.set_status('нет размещённых сканов для экспорта')
            return
        scopes = [('все размещённые сканы', None)]
        if key:
            ids = self.s.tree.scans_in(key) if self.s.tree.group(key) is not None else [key]
            name = self.s.tree.group(key).name if self.s.tree.group(key) is not None else short(key)
            scopes.insert(0, (f'ветка «{name}» ({len(ids)} скан.)', ids))
        base = Path(self.s.project_path).with_suffix('') if self.s.project_path else Path.home() / 'merged'
        dlg = ExportDialog(self, scopes, str(base) + ('_' + short(key) if key else '_merged') + '.e57')
        if not dlg.exec():
            return
        v = dlg.values()
        self.run_bg('экспорт…', lambda p: self.s.export(str(v['path']), v['voxel'], p, v['frame'],
                                                        scan_ids=v['scan_ids']),
                    lambda n: self.set_status(f"Экспорт: {v['path']} ({n:,} точек)".replace(',', ' ')))

    # ── прочее ───────────────────────────────────────────────────────────
    def on_escape(self):
        if self.downloading:
            self._dl_cancel = True
            self.set_status('загрузка останавливается… (повторный импорт продолжит с места)')
        elif self.view.fly:
            self.toggle_fly()
        elif self.selection:
            self.clear_selection()
            self.set_status('выделение снято')
        elif self.select_mode:
            self.toggle_select()
        elif self.pending is not None:
            self.pending = None
            self.view.remove(f'pick:{len(self.pairs)}:A')
            self.refresh_manual()
            self.set_status('незаконченная пара отменена')
        elif self.mode == 'clean':
            self.set_mode('inspect')
        elif self._vis_backup is not None and self.mode != 'manual':
            self.restore_visibility()
            self.set_status('видимость восстановлена', log=False)

    def show_help(self):
        QMessageBox.information(self, 'Клавиши и мышь', (
            'Вид: левая кнопка — вращение, правая/средняя или Shift + левая — сдвиг, колесо — масштаб к курсору, '
            'двойной клик — центр вращения. Куб навигации — клик по грани, ребру или углу.\n\n'
            'T — вид сверху, F — полёт (WASD, Space/E — вверх, C/Q — вниз, Shift — быстрее), Esc — выход.\n\n'
            'Ручная стыковка: Ctrl/⌘ + клик — признак (сначала в неподвижном), Shift + тянуть — сдвиг подвижного, '
            'Alt + тянуть или Shift + правая — поворот.\n\n'
            'Чистка: R — выделение прямоугольником (Shift — добавить), Delete — удалить, Ctrl+Z — отменить.\n\n'
            'F12 — скриншот; Ctrl+S — сохранить.'))

    def save_screenshot(self, out_dir=None):
        out = Path(out_dir or Path(__file__).resolve().parent.parent / 'screenshots')
        out.mkdir(exist_ok=True)
        stem = out / time.strftime('qt_%Y%m%d_%H%M%S')
        self.grab().save(f'{stem}_window.png')
        img = self.view.grab_scene()
        if img is not None:
            img.save(f'{stem}_scene.png')
        Path(f'{stem}_ui.txt').write_text(self.ui_state(), encoding='utf-8')
        self.set_status(f'скриншот: {stem.name}_window.png / _scene.png / _ui.txt')
        return str(stem)

    def ui_state(self):
        lines = [f'окно {self.width()}×{self.height()}, режим {self.mode}, тема '
                 f'{"тёмная" if self.theme.dark else "светлая"}',
                 f'статус: {self.st_text.text()}', '', 'сканы:']
        lines += [f"  {'[x]' if s.visible else '[ ]'} {s.id}  {(self.status_chip(s.id) or ('',))[0]}"
                  for s in self.s.scans]
        lines += ['', f'рёбер: {len(self.s.edges)}, активных: {len(self.s.active_edges())}']
        if self.moving is not None:
            lines += ['', f'ручная: {self.fixed.id} <- {self.moving.id}'] + \
                     [f'  {i + 1}. {fdesc(a)} <-> {fdesc(b)}' for i, (a, b) in enumerate(self.pairs)] + \
                     ['поза подвижного:', np.array2string(self.T_moving, precision=3)]
        return '\n'.join(lines) + '\n'

    def closeEvent(self, ev):
        if not self._closing_ok and not self.confirm_leave_manual('Закрыть окно'):
            ev.ignore()
            return
        if self.dirty() and not self._closing_ok:
            box = QMessageBox(self)
            box.setWindowTitle('Закрыть')
            box.setText('В проекте есть несохранённые изменения. Сохранить перед выходом?')
            b_save = box.addButton('Сохранить и выйти', QMessageBox.AcceptRole)
            b_quit = box.addButton('Выйти без сохранения', QMessageBox.DestructiveRole)
            box.addButton('Отмена', QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is b_save:
                if not self.on_save():
                    ev.ignore()
                    return
            elif box.clickedButton() is not b_quit:
                ev.ignore()
                return
        self._load_gen += 1
        self.settings.setValue('geometry', self.saveGeometry())
        ev.accept()
