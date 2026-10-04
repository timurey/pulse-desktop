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
import dynamic
import surface
import measure
import section
from scan_session import Session
from scan_tree import KINDS

from . import bg
from . import widgets as W
from .theme import scan_rgb, pick_rgb, ui_font_path
from .cloud_view import CloudView
from .tree_panel import TreePanel, short
from .inspector import Inspector, ManualPanel, CleanPanel
from .surface_panel import SurfacePanel
from .ribbon import Ribbon
from .control_panel import ControlPanel
from .dock import Dock
from .dialogs import ScannerImportDialog, BagImportDialog, ExportDialog, AskDialog, PackDialog

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


ICP_WEIGHT = 10.0          # вес связи после автоподгонки относительно грубых ручных


def user_dir(*sub):
    """Папка пользователя Pulse (~/Pulse/…): туда — то, что нельзя писать рядом с программой."""
    return Path.home().joinpath('Pulse', *sub)


def manual_path():
    """Файл руководства: в сборке — <программа>/docs/manual/README.md, из исходников — docs/manual."""
    import sys
    bases = [Path(getattr(sys, '_MEIPASS', '')), Path(__file__).resolve().parent.parent]
    for b in bases:
        f = b / 'docs' / 'manual' / 'README.md'
        if f.exists():
            return f
    return None


def quick_voxel():
    from scan_session import DISPLAY_VOXEL
    return max(DISPLAY_VOXEL, 0.03)


class Viewport(QWidget):
    """3D-вид с плашками поверх."""

    def __init__(self, win):
        super().__init__()
        t = W.THEME
        self.win = win
        self.labels = []                         # [(QLabel, точка мира)] — подписи замеров
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
        self.b_top = W.tool(t.icon('mdi6.map-outline'), tip='Вид сверху (T)', cb=win.view_top, icon_only=True)
        self.b_3d = W.tool(t.icon('mdi6.cube-outline'), tip='Вид 3D', cb=win.view_3d, icon_only=True)
        self.b_fly = W.tool(t.icon('mdi6.airplane', 'ink2', 'accent'), tip='Полёт (F)', cb=win.toggle_fly,
                            checkable=True, icon_only=True)
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
        for b in (self.b_top, self.b_3d, self.b_fly, vsep(), self.b_fit, self.b_proj, vsep(), size_icon, self.size_slider, self.size_text, vsep(),
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

    def set_labels(self, items):
        """Подписи в точках мира: [(точка, текст)] — плашки поверх 3D-вида."""
        for lab, _ in self.labels:
            lab.hide()
            lab.deleteLater()
        self.labels = []
        for pt, text in items:
            lab = QLabel(text, self)
            lab.setObjectName('MLabel')
            lab.adjustSize()
            lab.show()
            self.labels.append((lab, np.asarray(pt, float)))
        self._place_labels()

    def _place_labels(self):
        if not self.labels:
            return
        P = np.array([p for _, p in self.labels])
        s_, _, front = self.view.project(P)
        for (lab, _), (x, y), f in zip(self.labels, s_, front):
            ok = f and -50 < x < self.width() + 50 and -20 < y < self.height() + 20
            lab.setVisible(bool(ok))
            if ok:
                lab.move(int(x - lab.width() / 2), int(y - lab.height() - 6))

    def flash(self, text, ms=3500):
        """Короткое заметное сообщение над 3D-видом (не перетирается фоновым статусом)."""
        self._flash = text
        self.set_banner(text)
        QTimer.singleShot(ms, lambda t=text: self._unflash(t))

    def _unflash(self, text):
        if getattr(self, '_flash', None) == text:
            self._flash = None
            self.win._banner()

    def set_banner(self, text):
        if getattr(self, '_flash', None) and text != self._flash:
            return                                       # пока показано сообщение — не заменять
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
        z0 = getattr(self.win, 'z0', None)
        el = '' if z0 is None else f'   отм. {measure.elevation(c[2], z0)}'
        self.coord_text.setText(f'x {c[0]:.2f}   y {c[1]:.2f}   z {c[2]:.2f}{el}')
        self._place_labels()
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
        self.pivot = None                        # опорная точка: {'c', 'T0', 'p_loc', 'ang', 'r'}
        self.pivot_pick = False                  # режим выбора опорной точки (Ctrl+клик)
        self.pivot_A = None                      # выбранная точка в неподвижном (общая система)
        self._ring_drag = None                   # перетаскивание кольца транспортира
        self._icp_refined = False                # последняя поза — из автоподгонки (точная связь)
        self._fit_base = None                    # толщина пары при входе в ручную стыковку
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_thickness)
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
        self.dyn_masks = {}                      # найденные движущиеся объекты: id скана → маска res['down']
        self.measure_mode = None                 # замер: dist | height | plane | poly | zero
        self._mpts, self._mplane, self._mclick = [], None, None
        self.zero_show = True
        self.z0 = None
        self.points_hidden = False               # «Поверхность»: показывать только сетки
        self.plan_step = 1.0                     # шаг поворота плана, град
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
        self.tree_panel.layerToggled.connect(lambda k, v: self._sync_ribbon())
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
        self.vp.cube.homeRequested.connect(self.view_3d)
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
        self.surface_panel = SurfacePanel()
        self.surface_panel.action.connect(self.on_surface_action)
        self.surface_panel.closed.connect(lambda: self.set_mode('inspect'))
        self.control = ControlPanel()
        self.control.action.connect(self.on_control_action)
        for w in (self.inspector, self.manual, self.clean, self.surface_panel, self.control):
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
        """Лента: вкладки-этапы; на ленте команды, параметры и состояние — в правой панели."""
        t = W.THEME
        rb = Ribbon()
        self.ribbon = rb
        mark = QFrame()
        mark.setObjectName('Mark')
        mark.setFixedSize(20, 20)
        rb.brand.addWidget(mark)
        rb.brand.addWidget(W.label('Pulse Scan', 'AppName'))
        self.proj_name = W.label('новый проект', 'ProjName')
        self.proj_name.setMaximumWidth(240)
        rb.brand.addWidget(self.proj_name)
        self.dirty_dot = QFrame()
        self.dirty_dot.setObjectName('DirtyDot')
        self.dirty_dot.setFixedSize(7, 7)
        self.dirty_dot.setToolTip('Есть несохранённые изменения')
        self.dirty_dot.hide()
        rb.brand.addWidget(self.dirty_dot)
        sel = lambda: self.tree_sel
        rb_ = {}

        # ── Проект ──
        pg = rb.add_tab('project', 'Проект')
        g = rb.add_group(pg, 'Файл')
        g.big('mdi6.file-plus-outline', 'Новый', self.on_new_project, 'Новый проект (Ctrl+N)')
        g.big('mdi6.folder-outline', 'Открыть', self.on_open, 'Открыть проект (Ctrl+O)')
        g.big('mdi6.content-save-outline', 'Сохранить', self.on_save, 'Сохранить (Ctrl+S)')
        g.small('mdi6.content-save-edit-outline', 'Сохранить как…', self.on_save_as)
        g.small('mdi6.package-variant-closed', 'Упаковать для передачи…', self.on_pack,
                'Проект, все сканы и кеш анализа — одной папкой или zip-файлом для другого человека')
        g = rb.add_group(pg, 'Импорт')
        g.big('mdi6.access-point', 'Со\nсканера', self.on_import_scanner, 'Скачать записи со сканера и импортировать')
        g.big('mdi6.folder-open-outline', 'Bag', self.on_import_bags, 'Импорт bag из папки')
        g.big('mdi6.cloud-upload-outline', 'Облако', self.on_add_scan, 'Добавить облако (e57, ply, pcd, las)')
        g = rb.add_group(pg, 'Дерево')
        g.small('mdi6.folder-plus-outline', 'Новая группа', lambda: self.on_tree_action('new_group', sel()))
        g.small('mdi6.folder-plus', 'Сгруппировать', self.group_selected,
                'Группа из выделенных в дереве (Shift / Ctrl(⌘) + щелчок) — Ctrl/⌘+G')
        g.small('mdi6.folder-move-outline', 'В группу…', lambda: self.on_tree_action('move', sel()) if sel()
                else self.set_status('выберите скан или группу в дереве'))
        g.small('mdi6.eye-check-outline', 'Только выбранное', lambda: self.on_tree_action('only', sel()) if sel()
                else self.set_status('выберите скан или группу в дереве'))
        g.small('mdi6.eye-outline', 'Показать всё', self.show_all)
        g.small('mdi6.delete-outline', 'Удалить из проекта', lambda: self.remove_selected(),
                'Выделенные сканы (группы — вместе со сканами); файлы на диске остаются — ⌘/Ctrl+Delete')

        # ── Стыковка ──
        pg = rb.add_tab('reg', 'Стыковка')
        g = rb.add_group(pg, 'Автоматически')
        am = QMenu(self)
        self.a_reuse = am.addAction('Использовать уже посчитанные пары')
        self.a_reuse.setCheckable(True)
        self.a_reuse.setChecked(True)
        self.a_bytree = am.addAction('По дереву: пары внутри групп и соседних (быстрее)')
        self.a_bytree.setCheckable(True)
        self.a_bytree.setChecked(True)
        self.b_auto = g.big('mdi6.graph-outline', 'Авто-\nстыковка', self.on_auto, 'Подобрать пары и позы всех сканов',
                            menu=am)
        g.small('mdi6.folder-network-outline', 'Внутри групп', self.on_auto_within,
                'Выделена группа в дереве — пары только внутри неё (с подгруппами); ничего не выделено — '
                'внутри каждой группы. Связи между группами не трогаются. Также: правая кнопка на группе')
        g.small('mdi6.vector-arrange-above', 'Группы между собой', self.on_auto_groups,
                'Каждая группа — жёсткое целое (облако по внутренним связям); стыкуются дочерние группы '
                'выбранной ветки (или верхние группы)')
        g.small('mdi6.target', 'Кандидаты позы', lambda: self.on_cand_search(
            sel() or next((x.id for x in self.s.scans if x.pose is None), None)))
        g = rb.add_group(pg, 'Вручную')
        self.b_manual = g.big('mdi6.vector-combine', 'Ручная', lambda: self.start_manual(),
                              'Ручная стыковка выбранного скана', checkable=True)
        g.big('mdi6.auto-fix', 'Авто-\nподгонка', self.ribbon_autofit,
              'Точная подгонка (ICP) после грубой ручной стыковки; в дереве — «Уточнить стыковку» выбранного скана')
        self.b_pivot = g.small('mdi6.crosshairs-gps', 'Опорная точка', self.ribbon_pivot,
                               'Совместить одну точку, закрепить и поворачивать скан вокруг неё', checkable=True)
        g.small('mdi6.function-variant', 'Решить по парам', lambda: self.on_manual_action('solve', None))
        g = rb.add_group(pg, 'Граф')
        g.small('mdi6.anchor', 'Сделать опорным', lambda: self.make_ref(sel()))
        g.small('mdi6.check', 'Принять пару', lambda: self._pair_cmd('accept'))
        g.small('mdi6.close', 'Отклонить пару', lambda: self._pair_cmd('reject'))
        g = rb.add_group(pg, 'Горизонт и план')
        g.big('mdi6.angle-acute', 'По\nгоризонту', self.on_level_project,
              'Выровнять весь проект по полу и стенам (сохраняется в проекте)')
        g.small('mdi6.grid', 'План по стенам', self.on_align_plan)
        g.small('mdi6.rotate-left', 'Повернуть ◀', lambda: self.on_rotate_plan(1))
        g.small('mdi6.rotate-right', 'Повернуть ▶', lambda: self.on_rotate_plan(-1))
        sm = QMenu(self)
        for v in (0.1, 1.0, 5.0):
            a = sm.addAction(f'шаг {v:g}°')
            a.triggered.connect(lambda _=False, v=v: self._set_plan_step(v))
        self.b_plan_step = g.small('mdi6.ruler', f'шаг {self.plan_step:g}°', menu=sm)
        g.small('mdi6.restore', 'Сбросить горизонт', self.on_level_reset)

        # ── Контроль ──
        pg = rb.add_tab('control', 'Контроль')
        g = rb.add_group(pg, 'Совмещение')
        rb_['quality'] = g.big('mdi6.texture-box', 'Качество\nсовмещения',
                               lambda: self._toggle_layer('quality'),
                               'Подсветить места, где поверхность из разных сканов толще порога', checkable=True)
        g.small('mdi6.sync', 'Циклы графа', lambda: self.dock.tabs.setCurrentIndex(1))
        g.small('mdi6.format-list-bulleted', 'Список мест', lambda: self.dock.tabs.setCurrentIndex(4))
        g = rb.add_group(pg, 'Объекты')
        rb_['planes'] = g.small('mdi6.layers-outline', 'Поверхности', lambda: self._toggle_layer('planes'),
                                checkable=True)
        rb_['openings'] = g.small('mdi6.window-closed-variant', 'Проёмы', lambda: self._toggle_layer('openings'),
                                  checkable=True)
        rb_['ghosts'] = g.small('mdi6.blur', 'Отражения', lambda: self._toggle_layer('ghosts'), checkable=True)
        g = rb.add_group(pg, 'Сечения')
        self.b_slice = g.big('mdi6.box-cutter', 'Срез', lambda: self.section_toggle('slice'),
                             'Показать только полосу облака у плоскости (план этажа, разрез)', checkable=True)
        self.b_clip = g.big('mdi6.flip-to-back', 'Отсечение', lambda: self.section_toggle('clip'),
                            'Скрыть всё по одну сторону плоскости (например, выше 2.5 м)', checkable=True)
        g.small('mdi6.arrow-expand-vertical', 'Горизонтально', lambda: self.on_control_action('sec_base', 'z'))
        g.small('mdi6.alpha-x-box-outline', 'По X', lambda: self.on_control_action('sec_base', 'x'))
        g.small('mdi6.alpha-y-box-outline', 'По Y', lambda: self.on_control_action('sec_base', 'y'))
        g.small('mdi6.wall', 'По стене / полу', lambda: self.on_control_action('sec_base', 'plane'),
                'Щелчок по найденной стене или полу — плоскость сечения параллельна ей')
        g.small('mdi6.swap-vertical', 'Перевернуть', lambda: self.on_control_action('sec_flip', None),
                'Отсечение: показать другую сторону')
        g = rb.add_group(pg, 'Нулевой уровень')
        self.b_zero = g.big('mdi6.format-vertical-align-bottom', 'Нулевой\nуровень',
                            lambda: self.on_control_action('zero_show', not self.zero_show),
                            'Показать нулевой уровень (сетка 1 м на нём); отметки считаются от него', checkable=True)
        g.small('mdi6.floor-plan', 'По полу', lambda: self.on_control_action('zero_floor', None),
                'Пол опорного скана (по умолчанию)')
        g.small('mdi6.cursor-default-click-outline', 'Кликом', lambda: self.set_measure_mode('zero'),
                'Щелчок по точке облака — её высота станет ±0.000')
        g.small('mdi6.numeric', 'Числом…', lambda: self.on_control_action('zero_number', None))
        g = rb.add_group(pg, 'Замеры')
        rm = {}
        rm['dist'] = g.big('mdi6.ruler', 'Расстояние', lambda: self.set_measure_mode('dist'),
                           'Расстояние между двумя точками облака (длина, по горизонтали, перепад, уклон)',
                           checkable=True)
        rm['height'] = g.small('mdi6.arrow-expand-vertical', 'Отметка', lambda: self.set_measure_mode('height'),
                               'Высота точки от нулевого уровня', checkable=True)
        rm['plane'] = g.small('mdi6.format-align-middle', 'До плоскости', lambda: self.set_measure_mode('plane'),
                              'Щелчок по стене / полу, затем по точке — расстояние по нормали', checkable=True)
        rm['poly'] = g.small('mdi6.vector-polyline', 'Ломаная / площадь', lambda: self.set_measure_mode('poly'),
                             'Точки подряд; щелчок по первой — замкнуть (площадь); двойной щелчок или Enter — '
                             'закончить', checkable=True)
        g.small('mdi6.delete-sweep-outline', 'Очистить замеры', lambda: self.on_control_action('measure_clear', None))
        self._rb_measure = rm

        # ── Чистка ──
        pg = rb.add_tab('clean', 'Чистка')
        g = rb.add_group(pg, 'Движущиеся объекты')
        g.big('mdi6.walk', 'Найти', self.on_dyn_find, 'Найти точки движущихся объектов')
        g.small('mdi6.delete-outline', 'Удалить найденное', self.on_dyn_delete)
        g = rb.add_group(pg, 'Отражения')
        g.small('mdi6.blur', 'Показать красным', lambda: self.on_clean_action('ghosts', None))
        g = rb.add_group(pg, 'Вручную')
        self.b_select = g.big('mdi6.selection-drag', 'Рамка', self.toggle_select,
                              'Выделение прямоугольником (R); Shift — добавить', checkable=True)
        g.small('mdi6.delete-outline', 'Удалить (Del)', self.on_erase)
        g.small('mdi6.selection-off', 'Снять выделение', self.clear_selection)
        g.small('mdi6.undo', 'Отменить (Ctrl+Z)', self.on_undo_erase)
        g.small('mdi6.restore', 'Сбросить скан', lambda: self.on_clean_action('reset_scan', None))

        # ── Результат ──
        pg = rb.add_tab('result', 'Результат')
        g = rb.add_group(pg, 'Облако')
        g.big('mdi6.export-variant', 'Экспорт\nсклейки', self.on_export, 'Экспорт склейки (E57 / PLY / PCD)')
        g.small('mdi6.file-tree-outline', 'Экспорт ветки', lambda: self.on_export(sel()) if sel()
                else self.set_status('выберите ветку или скан в дереве'))
        g = rb.add_group(pg, 'Поверхность β')
        self.b_build = g.big('mdi6.vector-triangle', 'Построить', lambda: self.on_surface_action('build', None),
                             'Экспериментально: сетка (полигоны) по сканам; параметры — в правой панели')
        g.small('mdi6.export', 'Экспорт сетки', self._export_last_mesh)
        g = rb.add_group(pg, 'Документы')
        g.big('mdi6.camera-outline', 'Скриншот', self.save_screenshot, 'Скриншот окна и 3D-вида (F12)')
        rb.finish()
        self._rb_toggles = rb_

        # быстрые кнопки справа
        ic = t.icon
        self.b_save = W.tool(ic('mdi6.content-save-outline'), tip='Сохранить (Ctrl+S)', cb=self.on_save,
                             icon_only=True)
        self.b_theme = W.tool(ic('mdi6.white-balance-sunny' if t.dark else 'mdi6.weather-night'),
                              tip='Светлая / тёмная тема', cb=self.toggle_theme, icon_only=True)
        self.b_menu = W.tool(ic('mdi6.menu'), tip='Меню', icon_only=True)
        self.b_menu.setMenu(self._file_menu())
        self.b_menu.setPopupMode(QToolButton.InstantPopup)
        for b in (self.b_save, self.b_theme, self.b_menu):
            rb.quick.addWidget(b)
        rb.tabChanged.connect(self.on_tab)
        if self.settings.value('ribbon_collapsed') in (True, 'true'):
            rb.toggle_collapsed()
        return rb

    # ── лента: вкладки ↔ правая панель ───────────────────────────────────
    TAB_OF_MODE = {'manual': 'reg', 'clean': 'clean', 'surface': 'result', 'control': 'control'}

    def on_tab(self, key):
        self.settings.setValue('ribbon_collapsed', self.ribbon.collapsed)
        if key in ('project', 'reg', 'control'):
            self._free_tab = key
        want = {'clean': 'clean', 'result': 'surface', 'control': 'control'}.get(key)
        if want is None:
            want = 'manual' if (key == 'reg' and self.moving is not None) else 'inspect'
        if want != self.mode:
            self.set_mode(want)
        self._sync_ribbon()

    def _sync_ribbon(self):
        """Вкладка и переключатели ленты — по состоянию окна."""
        rb = getattr(self, 'ribbon', None)
        if rb is None:
            return
        tab = self.TAB_OF_MODE.get(self.mode)
        if tab is None and rb.current() in ('clean', 'result', 'control'):
            tab = getattr(self, '_free_tab', 'project')
        if tab is not None and rb.current() != tab:
            rb.set_tab(tab)
        self.b_manual.setChecked(self.mode == 'manual')
        self.b_select.setChecked(self.select_mode)
        self.b_pivot.setChecked(self.pivot is not None or self.pivot_pick)
        for k, b in self._rb_toggles.items():
            b.setChecked(self.tree_panel.layer(k))
        for k, b in self._rb_measure.items():
            b.setChecked(self.measure_mode == k)
        self.b_zero.setChecked(self.zero_show)
        st = self.s.section or {}
        self.b_slice.setChecked(st.get('mode') == 'slice')
        self.b_clip.setChecked(st.get('mode') == 'clip')

    def _toggle_layer(self, key):
        sw = self.tree_panel.switches[key]
        sw.setChecked(not sw.isChecked(), emit=True)
        self._sync_ribbon()

    def ribbon_autofit(self):
        if self.moving is not None:
            return self.on_autofit()
        if self.tree_sel and self.s.by_id(self.tree_sel) is not None:
            return self.refine_scan(self.tree_sel)
        self.set_status('выберите размещённый скан в дереве или начните ручную стыковку')

    def ribbon_pivot(self):
        if self.moving is None:
            self.start_manual()
            if self.moving is None:
                self._sync_ribbon()
                return
        if self.pivot is not None:
            self.on_manual_action('pivot_clear', None)
        else:
            self.on_manual_action('pivot_pick', None)
        self._sync_ribbon()

    def _pair_cmd(self, state):
        i = self.dock.selected_pair()
        if i < 0:
            self.dock.tabs.setCurrentIndex(0)
            self.set_status('выберите пару в таблице «Пары»')
            return
        self.on_pair_action(state, i)

    # ── нулевой уровень и замеры ─────────────────────────────────────────
    MEASURE_HINTS = {'dist': 'Расстояние: щёлкните две точки облака',
                     'height': 'Отметка: щёлкните точку облака',
                     'plane': 'До плоскости: щёлкните по стене или полу, затем по точке',
                     'poly': 'Ломаная: щёлкайте точки; по первой — замкнуть, двойной щелчок / Enter — закончить',
                     'zero': 'Нулевой уровень: щёлкните точку облака — её высота станет ±0.000',
                     'secplane': 'Сечение: щёлкните по стене или полу — плоскость встанет параллельно ей'}

    def set_measure_mode(self, mode):
        if mode is not None and mode == self.measure_mode:
            mode = None
        if mode is not None and self.mode != 'control':
            self.set_mode('control')
        self.measure_mode = mode
        self._mpts, self._mplane = [], None
        self.view.remove_prefix('mplane')
        self._draw_measures()
        self.control.mode_hint.setText(self.MEASURE_HINTS.get(mode, ''))
        self._banner()
        if mode:
            self.set_status(self.MEASURE_HINTS[mode] + ' (Esc — отмена)', log=False)

    def refresh_control(self):
        self.z0 = self.s.zero_z() if self.s.scans else 0.0
        if self.s.scans:
            self._refresh_section_panel()
            self._draw_section()
        z = self.s.zero
        src = {'floor': 'пол опорного скана', 'point': 'по точке облака', 'manual': 'задан числом'}
        text = (src.get(z['source'], z['source']) if z else
                ('пол опорного скана (по умолчанию)' if measure.default_zero(self.s) is not None
                 else 'пол не найден — уровень 0 общей системы'))
        self.control.set_zero(self.z0, text, self.zero_show)
        self.control.set_measures(self.s.measures, self.z0)

    def _set_zero(self, z, source):
        self.s.zero = None if source is None else {'z': float(z), 'source': source}
        self.z0 = self.s.zero_z()
        self.update_grid()
        self._draw_measures()
        self.refresh_control()
        self.vp.update_overlays()
        self.set_status(f'нулевой уровень: z = {self.z0:.3f} м ({source or "пол опорного скана"})')

    # ── сечение ──────────────────────────────────────────────────────────
    def _section_state(self):
        if self.s.section is None:
            self.s.section = section.default('z', self.s.zero_z() if self.s.scans else 0.0)
        return self.s.section

    def section_toggle(self, mode):
        st = self._section_state()
        if self.mode != 'control':
            self.set_mode('control')
        self.set_section(mode='off' if st['mode'] == mode else mode)

    def set_section(self, **kw):
        """Изменить сечение (mode, base, n0, a, b, c, thick, flip) и применить к виду."""
        st = self._section_state()
        for k, v in kw.items():
            st[k] = float((v + 180.0) % 360.0 - 180.0) if k in ('a', 'b') else v
        self.view.set_section(st)
        self._draw_section()
        self._refresh_section_panel()
        self._sync_ribbon()

    def _sec_extent(self):
        """Диапазон положения плоскости вдоль нормали по видимым точкам (для ползунка)."""
        lo, hi = self._visible_bbox()
        n = section.normal(self._section_state())
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        d = corners @ n
        return float(d.min()), float(d.max())

    def _refresh_section_panel(self):
        st = self._section_state()
        z0 = self.s.zero_z() if self.s.scans else 0.0
        dmin, dmax = self._sec_extent()
        n = section.normal(st)
        horiz = st['base'] == 'z' and n[2] > 0.99
        pos = st['c'] - z0 if horiz else st['c']
        frac = (st['c'] - dmin) / max(1e-6, dmax - dmin)
        labels = (['вокруг X', 'вокруг Y'] if abs(np.asarray(st['n0'])[2]) > 0.9
                  else ['азимут (вокруг Z)', 'наклон'])
        self.control.set_section(st, pos, 'отметка от нуля' if horiz else 'положение c', frac, labels,
                                 section.describe(st, z0))

    def _draw_section(self):
        """Рамка плоскости сечения и транспортиры доворота (на вкладке «Контроль»)."""
        self.view.remove_prefix('sect:')
        st = self.s.section
        self._sec_c = None
        if not st or st.get('mode', 'off') == 'off' or not self.s.scans:
            return
        lo, hi = self._visible_bbox()
        C = section.outline(st, lo, hi)
        col = (0.36, 0.61, 1.0)
        self.view.set_lines('sect:frame', C, [[0, 1], [1, 2], [2, 3], [3, 0]], None, None, 1.5, col, 0.9)
        if self.mode == 'control':
            self._sec_c = section.center(st, lo, hi)
            self._sec_r = float(np.clip(0.18 * np.linalg.norm(hi - lo), 0.5, 10.0))
            spec = self._protractor()
            if spec is not None:
                self._draw_rings('sect:', spec)

    def on_control_action(self, name, arg):
        if name == 'sec':
            if self.s.section is not None or arg.get('mode', 'off') != 'off':
                self.set_section(**arg)
            return
        if name == 'sec_base':
            if arg == 'plane':
                self.set_measure_mode('secplane')
                return
            st = self._section_state()
            z0 = self.s.zero_z() if self.s.scans else 0.0
            lo, hi = self._visible_bbox()
            mid = 0.5 * (lo + hi)
            n0 = np.array(section.BASES[arg])
            c = z0 + 2.5 if arg == 'z' else float(mid @ n0)
            self.set_section(base=arg, n0=n0.tolist(), a=0.0, b=0.0, c=c,
                             mode=st['mode'] if st['mode'] != 'off' else ('clip' if arg == 'z' else 'slice'))
            return
        if name == 'sec_pos':
            st = self._section_state()
            z0 = self.s.zero_z() if self.s.scans else 0.0
            n = section.normal(st)
            self.set_section(c=float(arg + z0 if (st['base'] == 'z' and n[2] > 0.99) else arg))
            return
        if name == 'sec_slider':
            dmin, dmax = self._sec_extent()
            self.set_section(c=dmin + arg * (dmax - dmin))
            return
        if name == 'sec_flip':
            st = self._section_state()
            self.set_section(flip=not st.get('flip'))
            return
        if name == 'zero_show':
            self.zero_show = bool(arg)
            self.update_grid()
            self._draw_measures()
            self.refresh_control()
            self._sync_ribbon()
        elif name == 'zero_floor':
            if measure.default_zero(self.s) is None:
                self.set_status('пол опорного скана не найден')
                return
            self._set_zero(0, None)
        elif name == 'zero_set':
            self._set_zero(arg, 'manual')
        elif name == 'zero_number':
            d = AskDialog(self, 'Нулевой уровень', [('Высота нулевого уровня z, м (общая система)', 'text',
                                                     f'{self.s.zero_z():.3f}', None)], 'mdi6.numeric', 'Задать')
            if d.exec():
                try:
                    self._set_zero(float(d.values()[0].replace(',', '.')), 'manual')
                except ValueError:
                    self.set_status('нужно число, например 0.150')
        elif name == 'measure_delete':
            if 0 <= arg < len(self.s.measures):
                self.s.measures.pop(arg)
                self._draw_measures()
                self.refresh_control()
        elif name == 'measure_clear':
            self.s.measures = []
            self._mpts, self._mplane = [], None
            self._draw_measures()
            self.refresh_control()

    def measure_click(self, x, y):
        """Щелчок по облаку в режиме замера (вызывается и из тестов)."""
        hit = self.view.pick_point(x, y, names={n for n in self.view.items if n.startswith('scan:')})
        if hit is None:
            self.set_status('мимо облака', log=False)
            return None
        name, p = hit
        p = np.asarray(p, float)
        return self.measure_point(p, name)

    def measure_point(self, p, cloud=None):
        mode = self.measure_mode
        if mode == 'secplane':
            sc = self.s.by_id(cloud[5:]) if cloud and cloud.startswith('scan:') else None
            if sc is None or not sc.analyzed:
                return None
            T = self.s.Tc(sc)
            f = self.s.pick_feature(sc, pr.transform(p[None], np.linalg.inv(T))[0], 'plane')
            if f is None:
                self.set_status('здесь нет найденной плоскости — щёлкните по стене или полу')
                return None
            n = T[:3, :3] @ np.asarray(f['normal'])
            self.set_measure_mode(None)
            st = self._section_state()
            st.update(base='plane', n0=n.tolist(), a=0.0, b=0.0, c=float(n @ p))
            if st['mode'] == 'off':
                st['mode'] = 'slice'
            self.set_section()
            self.set_status(f"сечение параллельно: {KIND_RU.get(f.get('kind'), 'плоскость')} {f['area']:.1f} м²")
            return p
        if mode == 'zero':
            self._set_zero(p[2], 'point')
            self.set_measure_mode(None)
            return p
        if mode == 'plane' and self._mplane is None:
            sc = self.s.by_id(cloud[5:]) if cloud and cloud.startswith('scan:') else None
            if sc is None or not sc.analyzed:
                return None
            T = self.s.Tc(sc)
            f = self.s.pick_feature(sc, pr.transform(p[None], np.linalg.inv(T))[0], 'plane')
            if f is None:
                self.set_status('здесь нет найденной плоскости — щёлкните по стене или полу')
                return None
            n = T[:3, :3] @ np.asarray(f['normal'])
            c = float(f['offset'] + n @ T[:3, 3])
            self._mplane = {'n': n.tolist(), 'c': c, 'name': f"{KIND_RU.get(f.get('kind'), 'плоскость')} "
                            f"{f['area']:.1f} м² ({short(sc.id)})", 'scan': sc.id, 'index': f['index']}
            pl = next(q for q in sc.planes if q.id == f['index'])
            self.view.set_cloud('mplane', sc.res['down'][pl.inliers], (0.36, 0.61, 1.0), T, size=4)
            self.set_status(f"плоскость: {self._mplane['name']} — теперь точка")
            return p
        self._mpts.append(p.tolist())
        need = {'dist': 2, 'height': 1, 'plane': 1}.get(mode)
        if mode == 'poly' and len(self._mpts) >= 4:
            s_, _, _ = self.view.project(np.array([self._mpts[0], self._mpts[-1]]))
            if np.hypot(*(s_[0] - s_[1])) < 10:          # щелчок по первой — замкнуть
                self._mpts.pop()
                return self.measure_finish(closed=True)
        if need is not None and len(self._mpts) >= need:
            m = {'type': mode, 'pts': self._mpts}
            if mode == 'plane':
                m['plane'] = self._mplane
                self.view.remove('mplane')
            self.s.measures.append(m)
            self._mpts, self._mplane = [], None
            self.refresh_control()
            self.set_status(f"{measure.TITLES[mode]}: {measure.label(m, self.z0)}")
        self._draw_measures()
        return p

    def measure_finish(self, closed=False):
        if self.measure_mode != 'poly' or len(self._mpts) < 2:
            return None
        m = {'type': 'poly', 'pts': self._mpts, 'closed': bool(closed and len(self._mpts) >= 3)}
        self.s.measures.append(m)
        self._mpts = []
        self._draw_measures()
        self.refresh_control()
        self.set_status(f"Ломаная: {measure.label(m, self.z0)}")
        return m

    def _draw_measures(self):
        """Замеры поверх облаков: линии, точки, подписи; текущий незаконченный — голубым."""
        self.view.remove_prefix('meas:')
        z0 = self.s.zero_z() if self.s.scans else 0.0
        self.z0 = z0
        items = [(m, False) for m in self.s.measures]
        if self._mpts:
            items.append(({'type': self.measure_mode, 'pts': self._mpts}, True))
        labels = []
        for k, (m, live) in enumerate(items):
            P = np.asarray(m['pts'], float)
            col = (0.45, 0.85, 1.0) if live else (1.0, 0.85, 0.29)
            segs, V = [], list(P)
            t = m['type']
            if t in ('dist', 'poly') and len(P) >= 2:
                segs = [[i, i + 1] for i in range(len(P) - 1)]
                if m.get('closed'):
                    segs.append([len(P) - 1, 0])
            elif t == 'height':
                V.append([P[0][0], P[0][1], z0])
                segs = [[0, 1]]
            elif t == 'plane' and 'plane' in m:
                V.append(measure.point_plane(P[0], m['plane']['n'], m['plane']['c'])['foot'])
                segs = [[0, 1]]
            if segs:
                self.view.set_lines(f'meas:l{k}', np.array(V), segs, None, None, 2.5, col, on_top=True)
            self.view.set_cloud(f'meas:p{k}', P, col, size=7, on_top=True)
            if not live:
                txt = f'{k + 1}  {measure.label(m, z0)}'
                at = P.mean(axis=0) if t != 'height' else P[0]
                labels.append((at, txt))
        if self.zero_show and self.s.scans and self.tree_panel.layer('grid'):
            lo, hi = self._visible_bbox()
            labels.append(([lo[0] - 1.5, lo[1] - 1.5, z0], '±0.000'))
        self.vp.set_labels(labels)

    def _set_plan_step(self, v):
        self.plan_step = v
        self.b_plan_step.setText(f'шаг {v:g}°')

    def _export_last_mesh(self):
        if not self.s.meshes:
            self.set_status('сеток нет — сначала «Построить»')
            return
        self.on_surface_action('mesh_export', list(self.s.meshes)[-1])

    def _file_menu(self, m=None):
        t = W.THEME
        m = m or QMenu(self)
        for item in (
                ('Новый проект', self.on_new_project, 'mdi6.file-plus-outline', 'Ctrl+N'),
                ('Открыть проект…', self.on_open, 'mdi6.folder-outline', 'Ctrl+O'),
                ('Сохранить', self.on_save, 'mdi6.content-save-outline', 'Ctrl+S'),
                ('Сохранить как…', self.on_save_as, 'mdi6.content-save-edit-outline', 'Ctrl+Shift+S'),
                ('Упаковать для передачи…', self.on_pack, 'mdi6.package-variant-closed', None),
                None,
                ('Импорт со сканера…', self.on_import_scanner, 'mdi6.access-point', None),
                ('Импорт bag…', self.on_import_bags, 'mdi6.folder-open-outline', None),
                ('Добавить облако…', self.on_add_scan, 'mdi6.plus', None),
                ('Экспорт склейки…', self.on_export, 'mdi6.export-variant', None),
                None,
                ('Скриншот', self.save_screenshot, 'mdi6.camera-outline', 'F12'),
                ('Руководство пользователя', self.show_manual, 'mdi6.book-open-outline', 'F1'),
                ('Клавиши и мышь', self.show_help, 'mdi6.keyboard-outline', None),
                ('О программе', self.show_about, 'mdi6.information-outline', None)):
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
                        ('Ctrl+Z', self.on_undo_erase), ('Ctrl+G', self.group_selected),
                        ('F1', self.show_manual),
                        ('Ctrl+Delete', lambda: self.remove_selected()),
                        ('Ctrl+Backspace', lambda: self.remove_selected()),
                        ('Return', self.measure_finish),
                        ('Enter', self.measure_finish)):
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
        from .version import __version__
        self.setWindowTitle(f"{Path(p).name if p else 'Новый проект'} — Pulse Scan {__version__}")

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
        """
        Поза показа скана. Неразмещённый показывается в своей системе: его сканер — в точке
        опорного (общая система без сдвига); с этого положения начинается и ручная стыковка.
        """
        if sc is self.moving and self.T_moving is not None:
            return self.T_moving
        T = self.s.Tc(sc)
        if T is None and self.s.scans and self.s.ref.pose is not None:
            T = self.s.Tc(self.s.ref)
        return T

    def _show_scan(self, sc):
        name = f'scan:{sc.id}'
        T = self.pose_of(sc)
        if T is None or getattr(sc, '_display', None) is None:
            self.view.remove(name)
            return
        self.view.set_cloud(name, sc._display, self.scan_color(sc.id)[0], T, sc.visible and not self.points_hidden)
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
        self._draw_meshes()
        self._draw_measures()
        self.view.set_section(self.s.section)
        self._draw_section()

    def apply_visibility(self):
        for sc in self.s.scans:
            self.view.set_visible(f'scan:{sc.id}', sc.visible and not self.points_hidden)
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
        self.z0 = self.s.zero_z() if self.s.scans else None
        z = self.z0 if (self.z0 is not None and self.zero_show) else lo[2] - 0.02
        self.view.set_grid(z, lo[:2] - pad, hi[:2] + pad, self.theme.rgb('grid'), 1.0,
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

    def on_pack(self):
        """Упаковать проект для передачи другому человеку (Session.pack)."""
        if not self.s.scans:
            self.set_status('в проекте нет сканов')
            return
        name = Path(self.s.project_path).stem if self.s.project_path else 'проект'
        base = Path(self.s.project_path).resolve().parent if self.s.project_path else Path.home() / 'Desktop'
        missing = [short(sc.id) for sc in self.s.scans if not Path(sc.path).exists()]
        mb = sum(Path(sc.path).stat().st_size for sc in self.s.scans if Path(sc.path).exists()) / 1e6
        d = PackDialog(self, name, base, len(self.s.scans) - len(missing), mb, missing)
        if not d.exec():
            return
        v = d.values()
        if not v['zip'] and v['path'].exists() and any(v['path'].iterdir()):
            QMessageBox.warning(self, 'Упаковать', f'Папка не пуста:\n{v["path"]}\nВыберите новую папку.')
            return
        sess = self.s

        def done(r):
            msg = (f"упаковано: {r['path']} — сканов {r['scans']}, {r['size'] / 1e6:.0f} МБ"
                   + (f"; не найдены: {', '.join(short(x) for x in r['missing'])}" if r['missing'] else ''))
            self.set_status(msg)
            box = QMessageBox(self)
            box.setWindowTitle('Проект упакован')
            box.setText(f"{'Zip-файл' if v['zip'] else 'Папка'} готов{'' if v['zip'] else 'а'}: "
                        f"{Path(r['path']).name if v['zip'] else Path(r['path']).parent.name}")
            box.setInformativeText(f"Сканов: {r['scans']}, {r['size'] / 1e6:.0f} МБ. Передайте "
                                   f"{'файл' if v['zip'] else 'папку целиком'}; получатель открывает "
                                   f"{r['project']} в Pulse Scan.")
            b_show = box.addButton('Показать в папке', QMessageBox.ActionRole)
            box.addButton('OK', QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is b_show:
                from PySide6.QtCore import QUrl
                from PySide6.QtGui import QDesktopServices
                target = Path(r['path']).parent
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        self.run_bg('упаковка проекта…', lambda p: sess.pack(v['path'], v['zip'], p), done)

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
        ov = v.get('calib_override')
        prm = dict(voxel=v['voxel'], min_range=br.DEFAULTS['min_range'], max_range=v['max_range'], tilt=v['tilt'])
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
                        want = br.expected_params(b, calib_override=ov, **prm)
                        if old.get('params') == want and Path(b).stat().st_mtime <= out.stat().st_mtime:
                            meta = old
                    except Exception:                    # noqa: BLE001
                        meta = None
                if meta is None:
                    def prog(f, msg, k=k):
                        progress((k + f) / len(bags), f'[{k + 1}/{len(bags)}] {msg}')
                    try:
                        P, meta = br.reconstruct(b, prm['voxel'], prm['min_range'], prm['max_range'],
                                                 prm['tilt'], prog, calib_override=ov)
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
        if self.mode == 'surface':
            self.refresh_surface()

    def on_tree_visible(self, key, vis):
        if key.startswith('mesh:'):
            return self._mesh_tree_visible(key, vis)
        self._vis_backup = None
        self.s.set_visible(key, vis)
        if vis and self.points_hidden:                   # включили скан — показать точки
            self.set_points_visible(True)
            self.set_status('точки сканов снова показаны', log=False)
        self.apply_visibility()
        self.tree_panel.rebuild()

    def _mesh_tree_visible(self, key, vis):
        """Глаз в ветви «Поверхности»: точки сканов, все сетки, одна сетка."""
        tp = self.tree_panel
        if key == tp.POINTS:
            self.set_points_visible(vis)
        elif key == tp.MESHES:
            for m in self.s.meshes.values():
                m['visible'] = vis
            if vis and not tp.layer('mesh'):
                tp.switches['mesh'].setChecked(True)
            self._draw_meshes()
        else:
            m = self.s.meshes.get(key[5:])
            if m is not None:
                m['visible'] = vis
                if vis and not tp.layer('mesh'):
                    tp.switches['mesh'].setChecked(True)
                self._draw_meshes()
        self.refresh_surface()
        tp.rebuild()

    def on_tree_move_drop(self, keys, gid, index=None):
        keys = [keys] if isinstance(keys, str) else list(keys)
        if not self.s.move_nodes(keys, gid, index):
            self.set_status('нельзя переместить сюда (группу нельзя вложить в саму себя)')
            return
        self.apply_visibility()
        self.tree_panel.expand_next = {gid}
        self.tree_panel.rebuild()
        self.tree_panel.select_keys(keys)
        g = self.s.tree.group(gid)
        where = g.name if g and g is not self.s.tree.root else 'корень'
        what = f'{len(keys)} узл.' if len(keys) > 1 else short(keys[0]) if self.s.by_id(keys[0]) else 'группа'
        self.set_status(f'{what} → «{where}»' + ('' if index is None else f', позиция {index + 1}'))

    def remove_selected(self, key=None):
        """Удалить из проекта выделенные сканы (и сканы выделенных групп вместе с группами)."""
        t = self.s.tree
        keys = self.tree_panel.selected_keys()
        if key is not None and key not in keys:
            keys = [key]
        if not keys:
            self.set_status('выделите сканы в дереве')
            return
        ids, groups = [], []
        for k in keys:
            if t.group(k) is not None:
                if k != 'root':
                    groups.append(k)
                    ids += t.scans_in(k)
            elif self.s.by_id(k) is not None:
                ids.append(k)
        ids = list(dict.fromkeys(ids))
        if not ids and not groups:
            return
        names = ', '.join(short(i) for i in ids[:6]) + (' …' if len(ids) > 6 else '')
        gtxt = f" и групп: {len(groups)}" if groups else ''
        ref = ' Среди них опорный — опорным станет другой размещённый скан.' if self.s.frame in ids else ''
        r = QMessageBox.question(self, 'Удалить из проекта',
                                 f'Удалить из проекта сканов: {len(ids)}{gtxt}?\n{names}\n\nФайлы сканов на диске '
                                 f'остаются; связи, ручная чистка и место в дереве удаляются.{ref} Вернуть — '
                                 f'закрыть проект без сохранения.',
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        if self.moving is not None and (self.moving.id in ids or self.fixed.id in ids):
            self.on_manual_cancel(ask=False)
        for sid in ids:
            for pre in ('scan:', 'feat:planes:', 'feat:openings:', 'ghost:', 'sel:', 'dyn:', 'mesh:'):
                self.view.remove(pre + sid)
            self.dyn_masks.pop(sid, None)
            self.selection.pop(sid, None)
            self.ghost_shown.discard(sid)
        if self.clean_scan is not None and self.clean_scan.id in ids:
            self.clean_scan = None
        if self.cand_scan is not None and self.cand_scan.id in ids:
            self.candidates, self.cand_scan = [], None
            self.dock.set_candidates([])
        removed = self.s.remove_scans(ids)
        for g in groups:
            if t.group(g) is not None:
                self.s.delete_group(g)
        self.tree_sel = None
        self.redraw_all()
        self.refresh_all()
        msg = f'удалено из проекта: {len(removed)} скан.' + (f', групп {len(groups)}' if groups else '') + \
              f'; размещено {len(self.s.placed())} из {len(self.s.scans)}'
        self.set_status(msg)
        self.vp.flash(msg)

    def group_selected(self):
        """Группа из выделенных в дереве сканов / групп (на месте первого)."""
        keys = self.tree_panel.selected_keys()
        if not keys:
            self.set_status('выделите сканы в дереве (Shift / Ctrl(⌘) + щелчок)')
            return
        d = AskDialog(self, f'Группа из выделенных ({len(keys)})',
                      [('Имя', 'text', '', None), ('Тип', 'combo', 'комната', KINDS)],
                      'mdi6.folder-plus', 'Создать')
        if not d.exec():
            return
        nm, kind = d.values()
        keys = [k for k in keys if self.s.tree.node(k) is not None]     # дерево могло измениться
        gid = self.s.group_nodes(keys, nm.strip() or 'Группа', kind) if keys else None
        if gid is None:
            QMessageBox.warning(self, 'Группа не создана',
                                'Не удалось создать группу из выделенного: узлы не найдены в дереве или '
                                'группу нельзя вложить саму в себя. Выделите сканы ещё раз.')
            return
        self.apply_visibility()
        self.tree_panel.expand_next = {gid}
        self.tree_panel.rebuild()
        self.tree_panel.select_keys([gid])
        msg = f'группа «{self.s.tree.group(gid).name}»: {len(keys)} узл.'
        self.set_status(msg)
        self.vp.flash(msg)

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
        elif name == 'group_sel':
            self.group_selected()
        elif name in ('points_on', 'points_off'):
            self.set_points_visible(name == 'points_on')
        elif name == 'mesh_toggle':
            m = self.s.meshes.get(key[5:])
            if m is not None:
                self._mesh_tree_visible(key, not (m['visible'] and self.tree_panel.layer('mesh')))
        elif name == 'mesh_export':
            self.on_surface_action('mesh_export', key[5:])
        elif name == 'mesh_delete':
            self.on_surface_action('mesh_delete', key[5:])
        elif name == 'mesh_delete_all':
            if QMessageBox.question(self, 'Удалить сетки', f'Удалить все построенные сетки ({len(self.s.meshes)})?',
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
                self.s.meshes.clear()
                self._draw_meshes()
                self.set_points_visible(True)
                self.refresh_surface()
                self.tree_panel.rebuild()
        elif name in ('auto_group', 'auto_subgroups'):
            self.tree_panel.select_keys([key])
            self.tree_sel = key
            (self.on_auto_within if name == 'auto_group' else self.on_auto_groups)()
        elif name == 'remove':
            self.remove_selected(key)
        elif name == 'move':
            keys = self.tree_panel.selected_keys() or [key]
            choices = t.group_choices()
            labels = [lab for _, lab in choices]
            d = AskDialog(self, 'Переместить в группу', [('Группа', 'combo', labels[0], labels)],
                          'mdi6.folder-move-outline', 'Переместить')
            if d.exec():
                gid = dict((lab, i) for i, lab in choices).get(d.values()[0])
                if gid is not None:
                    self.on_tree_move_drop(keys, gid)
        elif name == 'make_ref':
            self.make_ref(key)
        elif name == 'only':
            self._vis_backup = None
            sel = self.tree_panel.selected_keys()
            self.s.only_show(sel if key in sel and len(sel) > 1 else [key])
            self.apply_visibility()
            self.tree_panel.rebuild()
        elif name == 'show_all':
            self.show_all()
        elif name == 'export_branch':
            self.on_export(key)
        elif name == 'manual':
            self.start_manual(moving_id=key)
        elif name == 'refine':
            self.refine_scan(key)
        elif name == 'candidates':
            self.on_cand_search(key)
        elif name == 'fly_to':
            self.fly_to_scan(key)
        elif name == 'focus':
            self.focus_node(key)

    def refine_scan(self, sid):
        """Уточнить стыковку размещённого скана: ручная стыковка с соседом по графу + автоподгонка."""
        sc = self.s.by_id(sid)
        if sc is None or sc.pose is None or sid == self.s.frame:
            self.set_status('уточнить можно размещённый скан, кроме опорного')
            return
        cand = [e for e in self.s.active_edges() if sid in (e['A'], e['B'])]
        cand.sort(key=lambda e: (e.get('method') != 'manual', -e.get('score', 0)))   # сначала ручные связи
        partner = next((e['B'] if e['A'] == sid else e['A'] for e in cand), self.s.frame)
        self.start_manual(partner, sid)
        if self.moving is sc:
            self.on_autofit()

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
                       ('калибровка', __import__('calibration').describe(m['calibration'])
                        if m.get('calibration') else 'до учёта калибровки (переимпортируйте bag)'),
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
                       ('ручная чистка', ', '.join(x for x in (f'{len(sc.erase)} обл.' if sc.erase else '',
                                                               f'{len(sc.drop)} вокс.' if len(sc.drop) else '') if x)
                        or 'нет'),
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
                    'fly_to', 'refine'):
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
        st = self.plan_step * sign
        self._with_plan_change(lambda: self.s.rotate_plan(st))
        self.redraw_all()
        self.set_status(f'план повёрнут на {st:+.2f}°')

    # ── режимы правой панели ─────────────────────────────────────────────
    def set_mode(self, mode, force=False):
        if mode == self.mode and not force:
            return
        if self.mode == 'clean' and mode != 'clean' and self.select_mode:
            self.toggle_select()
        if self.mode == 'clean' and mode != 'clean' and self.dyn_masks:
            self.dyn_masks = {}
            self.view.remove_prefix('dyn:')
        if self.mode == 'manual' and mode != 'manual' and self.moving is not None and not force:
            if self.on_manual_cancel() and mode != 'inspect':
                self.set_mode(mode)
            self._sync_ribbon()
            return
        self.mode = mode
        self.right.setCurrentWidget({'inspect': self.inspector, 'manual': self.manual, 'clean': self.clean,
                                     'surface': self.surface_panel, 'control': self.control}[mode])
        if mode != 'control' and self.measure_mode:
            self.set_measure_mode(None)
        if mode != 'control':
            self._draw_section()
        if mode == 'control':
            self.refresh_control()
        if mode == 'surface':
            self.refresh_surface()
        if mode == 'clean':
            if self.clean_scan is None or self.clean_scan not in self.s.scans:
                sel = self.s.by_id(self.tree_sel) if self.tree_sel else None
                self.clean_scan = sel or (self.s.scans[0] if self.s.scans else None)
            self.refresh_clean()
        self._banner()

    def _banner(self):
        self._sync_ribbon()
        if self.view.fly:
            text = ''
        elif self.select_mode:
            text = 'Выделение: тяните левой кнопкой, Shift — добавить, Delete — удалить, Esc — выйти'
        elif self.measure_mode:
            text = self.MEASURE_HINTS[self.measure_mode] + ' · Esc — отмена'
        elif self.mode == 'manual' and self.moving is not None and self.pivot is not None:
            text = (f'{short(self.fixed.id)} ← {short(self.moving.id)} · опорная точка закреплена: тяните '
                    f'кольца транспортира (Shift — точнее), Alt + тянуть — рыскание')
        elif self.mode == 'manual' and self.moving is not None and self.pivot_pick:
            text = 'Опорная точка: Ctrl/⌘ + клик в неподвижном, затем та же точка в подвижном'
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
                             {'group': 'группы', 'planes+icp': 'плоскости+ICP'}.get(e.get('method'), e.get('method', '')),
                             st))
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

    def _scope_group(self):
        """Выбранная в дереве группа (или группа выбранного скана); None — весь проект."""
        if not self.tree_sel:
            return None
        if self.s.tree.group(self.tree_sel) is not None:
            return None if self.tree_sel == 'root' else self.tree_sel
        p = self.s.tree.parent(self.tree_sel)
        return None if p is None or p is self.s.tree.root else p.id

    def on_auto_within(self):
        gid = self._scope_group()
        pairs = self.s.within_group_pairs(gid)
        if not pairs:
            self.set_status('нет пар внутри групп: сгруппируйте сканы в дереве')
            return
        if self.analyzing:
            self._after_analysis = self.on_auto_within
            self.set_status('стыковка начнётся после фонового анализа')
            return
        reuse = self.a_reuse.isChecked()
        name = self.s.tree.group(gid).name if gid else 'все группы'

        def done(_):
            self.redraw_all()
            self.refresh_all()
            self.set_status(f'стыковка внутри групп ({name}, пар {len(pairs)}): размещено '
                            f'{len(self.s.placed())} из {len(self.s.scans)}')
        self.run_bg(f'стыковка внутри групп ({name}, пар {len(pairs)})…',
                    lambda p: self.s.run_auto(p, reuse=reuse, combos=pairs), done)

    def on_auto_groups(self):
        gid = self._scope_group()
        if self.analyzing:
            self._after_analysis = self.on_auto_groups
            self.set_status('стыковка начнётся после фонового анализа')
            return

        def done(out):
            self.redraw_all()
            self.refresh_all()
            ok = [f"«{a}» ← «{b}» {e['score']:+.2f}" + ('' if e['auto_ok'] else ' (не принято)') for a, b, e in out]
            self.set_status('группы между собой: ' + '; '.join(ok) + f'; размещено {len(self.s.placed())} из '
                            f'{len(self.s.scans)}')

        def work(p):
            return self.s.register_groups(gid, p)
        if self.run_bg('стыковка групп между собой…', work, done) is False:
            return

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
        self.pivot, self.pivot_pick, self.pivot_A, self._icp_refined = None, False, None, False
        self._fit_base = None
        self.view.remove_prefix('pivot:')
        self.manual.set_pivot(None)
        self.manual.fit_label.setText('')
        self._fit_timer.start(50)
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
        if name == 'nudge' and self.pivot is not None:
            kw = arg
            if any(kw.get(k) for k in ('dx', 'dy', 'dz')):
                self.set_status('опорная точка закреплена: сдвиг недоступен («Снять точку» — освободить)')
                return
            sd = self.manual.step_deg.value()
            a = self.pivot['ang']
            self._set_pivot_angles(roll=a['roll'] + kw.get('droll', 0) * sd, pitch=a['pitch'] + kw.get('dpitch', 0) * sd,
                                   yaw=a['yaw'] + kw.get('dyaw', 0) * sd)
            return
        if name == 'nudge':
            st, sd = self.manual.step_m.value(), self.manual.step_deg.value()
            kw = arg
            self._icp_refined = False
            self.T_moving = Session.nudge(self.T_moving, kw.get('dx', 0) * st, kw.get('dy', 0) * st,
                                          kw.get('dz', 0) * st, kw.get('dyaw', 0) * sd,
                                          kw.get('droll', 0) * sd, kw.get('dpitch', 0) * sd)
            self._update_moving()
            self.live_score()
        elif name == 'solve':
            self.on_manual_solve()
        elif name in ('icp', 'autofit'):
            self.on_autofit()
        elif name == 'pivot_pick':
            self.pivot_pick = not self.pivot_pick and self.pivot is None
            self.pivot_A = None
            self.view.remove_prefix('pivot:')
            self.manual.set_pivot('fixed' if self.pivot else None, picking=self.pivot_pick)
            self._banner()
            if self.pivot_pick:
                self.set_status('Ctrl/⌘ + клик: характерная точка в неподвижном скане')
        elif name == 'pivot_clear':
            self.pivot, self.pivot_pick, self.pivot_A = None, False, None
            self.view.remove_prefix('pivot:')
            self.manual.set_pivot(None)
            self._banner()
            self.set_status('опорная точка снята')
        elif name == 'pivot_angle':
            k, v = arg
            if self.pivot is not None:
                self._set_pivot_angles(**{k: v})
        elif name == 'pivot_step':
            k, sgn = arg
            if self.pivot is not None:
                self._set_pivot_angles(**{k: self.pivot['ang'][k] + sgn * self.manual.step_deg.value()})
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
            self.T_moving = self._pivot_absorb(T2)
            self._update_moving()
            self.live_score()
            self.set_status(f"выровнено по {info['horizontal']} гориз. и {info['walls']} верт. плоскостям: "
                            f"наклон {before:.2f}° → {Session.tilt_deg(T2):.2f}°")
        elif name == 'snap_yaw':
            T2, d = self.s.snap_yaw_to_walls(self.moving, self.T_moving)
            if d is None:
                self.set_status('нет стен для доворота')
                return
            self.T_moving = self._pivot_absorb(T2)
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
            self.T_moving = self._pivot_absorb(T)
            self._update_moving()
            self._fit_timer.start(10)
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
        weight, method = (ICP_WEIGHT, 'автоподгонка') if self._icp_refined else (1.0, 'ручная')

        def done(_):
            self.on_manual_cancel(ask=False)
            self.set_status(f'{short(moving.id)}: поза принята ({method}, связь с {short(fixed.id)}'
                            f"{', вес ×' + str(int(weight)) if weight != 1 else ''})")
        self.run_bg('пересчёт графа…', lambda p: self.s.accept_pose(moving, T, anchor=fixed, method=method,
                                                                    weight=weight), done)

    # ── автоподгонка и опорная точка ─────────────────────────────────────
    def _fit_targets(self):
        if self.manual.fit_target.value() == 'all':
            return [s for s in self.s.placed() if s is not self.moving] or [self.fixed]
        return [self.fixed]

    def on_autofit(self):
        """ICP после грубой ручной стыковки; при опорной точке — только поворот вокруг неё."""
        if self.moving is None:
            return
        moving, T0, targets = self.moving, self.T_moving.copy(), self._fit_targets()
        center = None if self.pivot is None else self.pivot['c']

        def done(r):
            T, info = r
            if moving is not self.moving:
                return
            if center is not None:
                self.T_moving = self._pivot_absorb(T)
            else:
                self.T_moving = T
            self._icp_refined = True
            self._update_moving()
            dt, da = info['shift']
            if center is not None:
                self.set_status(f"подгонка поворота вокруг точки: {da:.2f}°, rmse {info['rmse'] * 100:.1f} см")
            else:
                self.set_status(f"автоподгонка: сдвиг {dt * 100:.1f} см, поворот {da:.2f}°, "
                                f"rmse {info['rmse'] * 100:.1f} см")
            self.live_score()
            self._fit_timer.start(10)
        self.run_bg('автоподгонка (ICP)…', lambda p: self.s.refine_icp(targets, moving, T0, wide=True, center=center),
                    done)

    def _fit_thickness(self):
        """Толщина пары (одни и те же поверхности) для текущей позы подвижного — в фоне."""
        if self.moving is None or getattr(self, '_fit_busy', False):
            if self.moving is not None:
                self._fit_timer.start(400)
            return
        self._fit_busy = True
        moving, T, targets = self.moving, self.T_moving.copy(), self._fit_targets()
        t = self.manual.fit_label.text()
        if t and not t.endswith('…'):
            self.manual.fit_label.setText(t + ' · пересчёт…')

        def done(r):
            self._fit_busy = False
            if moving is not self.moving:
                return
            med, n = r
            if med is None:
                self.manual.fit_label.setText('общих поверхностей с неподвижным нет — сначала грубо совместите')
                return
            if self._fit_base is None:
                self._fit_base = med
            txt = f'расхождение поверхностей: {med * 100:.1f} см ({n} ячеек)'
            if abs(self._fit_base - med) > 1e-4:
                txt += f'; было {self._fit_base * 100:.1f} см'
            self.manual.fit_label.setText(txt)

        def fail(e):
            self._fit_busy = False
        bg.run(lambda: self.s.pair_thickness(targets, moving, T), done, fail)

    def _pivot_absorb(self, T2):
        """Поза после поворота другим инструментом: опорная точка возвращается на место, углы — в поля."""
        pv = self.pivot
        if pv is None:
            self._icp_refined = False
            return T2
        T2 = np.asarray(T2, float).copy()
        T2[:3, 3] = pv['c'] - T2[:3, :3] @ pv['p_loc']
        R = T2[:3, :3] @ pv['T0'][:3, :3].T
        pv['ang'] = {'yaw': float(np.degrees(np.arctan2(R[1, 0], R[0, 0]))),
                     'pitch': float(np.degrees(np.arcsin(np.clip(-R[2, 0], -1, 1)))),
                     'roll': float(np.degrees(np.arctan2(R[2, 1], R[2, 2])))}
        self.manual.set_pivot('fixed', pv['ang'])
        self._draw_pivot()
        return T2

    def _set_pivot_angles(self, **kw):
        pv = self.pivot
        for k, v in kw.items():
            pv['ang'][k] = float((v + 180.0) % 360.0 - 180.0)
        a = pv['ang']
        self.T_moving = Session.rotate_about(pv['T0'], pv['c'], a['roll'], a['pitch'], a['yaw'])
        self._icp_refined = False
        self.manual.set_pivot('fixed', a)
        self._update_moving()
        self._draw_pivot()
        self.live_score()
        self._fit_timer.start(500)

    def _pivot_picked(self, W_, scan):
        """Ctrl+клик в режиме опорной точки: сначала неподвижный, затем подвижный."""
        if self.pivot_A is None:
            if scan is not self.fixed:
                self.set_status('сначала точка в неподвижном скане')
                return
            self.pivot_A = np.asarray(W_, float)
            self._draw_pivot()
            self.manual.set_pivot('A')
            self.set_status('теперь Ctrl/⌘ + клик по той же точке в подвижном скане')
            return
        if scan is not self.moving:
            self.set_status('теперь та же точка в подвижном скане')
            return
        d = self.pivot_A - np.asarray(W_, float)
        T = self.T_moving.copy()
        T[:3, 3] += d                                    # совместить точки
        self.T_moving = T
        c = self.pivot_A
        self.pivot = {'c': c, 'T0': T.copy(), 'p_loc': np.linalg.inv(T)[:3, :3] @ c + np.linalg.inv(T)[:3, 3],
                      'ang': {'yaw': 0.0, 'roll': 0.0, 'pitch': 0.0},
                      'r': float(np.clip(0.12 * self.view.distance(), 0.4, 15.0))}
        self.pivot_pick, self.pivot_A = False, None
        self._icp_refined = False
        self.manual.set_pivot('fixed', self.pivot['ang'])
        self._update_moving()
        self._draw_pivot()
        self._banner()
        self.live_score()
        self._fit_timer.start(100)
        self.set_status(f'точка закреплена (сдвиг {np.linalg.norm(d) * 100:.1f} см): поворачивайте '
                        f'транспортирами, полями углов или «Автоподгонкой»')

    PIVOT_AXES = {'yaw': (np.array([0, 0, 1.0]), np.array([1, 0, 0.0]), np.array([0, 1, 0.0]), (0.36, 0.61, 1.0)),
                  'roll': (np.array([1, 0, 0.0]), np.array([0, 1, 0.0]), np.array([0, 0, 1.0]), (1.0, 0.42, 0.42)),
                  'pitch': (np.array([0, 1, 0.0]), np.array([0, 0, 1.0]), np.array([1, 0, 0.0]), (0.37, 0.83, 0.55))}

    # ── транспортиры: общие для опорной точки и сечения ──────────────────
    def _protractor(self):
        """Активный набор колец: опорная точка (ручная стыковка) или сечение (вкладка «Контроль»)."""
        if self.mode == 'manual' and self.pivot is not None:
            return {'owner': 'pivot', 'c': self.pivot['c'], 'r': self.pivot['r'], 'axes': self.PIVOT_AXES,
                    'ang': self.pivot['ang'], 'set': lambda k, v: self._set_pivot_angles(**{k: v})}
        st = self.s.section
        if self.mode == 'control' and st and st.get('mode', 'off') != 'off' and getattr(self, '_sec_c', None) is not None:
            n0 = np.asarray(st['n0'], float)
            n0 /= np.linalg.norm(n0)
            u, v = section.axes(n0)
            ax = {}
            for k, a, col in (('a', u, (0.36, 0.61, 1.0)), ('b', v, (1.0, 0.62, 0.25))):
                e1 = n0 - (n0 @ a) * a
                e1 /= np.linalg.norm(e1)
                ax[k] = (a, e1, np.cross(a, e1), col)
            return {'owner': 'section', 'c': self._sec_c, 'r': self._sec_r, 'axes': ax,
                    'ang': {'a': st['a'], 'b': st['b']}, 'set': lambda k, val: self.set_section(**{k: val})}
        return None

    @staticmethod
    def _ring_pts(spec, k, n=180):
        _, u, v, _ = spec['axes'][k]
        t = np.linspace(0, 2 * np.pi, n, endpoint=False)
        return spec['c'] + spec['r'] * (np.cos(t)[:, None] * u + np.sin(t)[:, None] * v)

    def _draw_rings(self, prefix, spec):
        """Кольца с делениями 5°/15°/90°, стрелкой и дугой текущего угла."""
        c, r = spec['c'], spec['r']
        for k, (n_, u, v, col) in spec['axes'].items():
            P, segs, cols = [], [], []
            ring = self._ring_pts(spec, k)
            m = len(ring)
            P.extend(ring)
            segs += [[i, (i + 1) % m] for i in range(m)]
            cols += [col] * m
            for deg in range(0, 360, 5):
                a = np.radians(deg)
                e = np.cos(a) * u + np.sin(a) * v
                ln = 0.16 if deg % 90 == 0 else (0.09 if deg % 15 == 0 else 0.045)
                P += [c + r * e, c + r * (1 - ln) * e]
                segs.append([len(P) - 2, len(P) - 1])
                cols.append(col)
            ang = np.radians(spec['ang'][k])
            e = np.cos(ang) * u + np.sin(ang) * v
            P += [c, c + r * 1.08 * e]
            segs.append([len(P) - 2, len(P) - 1])
            cols.append((1.0, 1.0, 1.0))
            ts = np.linspace(0, ang, max(2, int(abs(np.degrees(ang)) * 2) + 2))
            arc = [c + r * 1.04 * (np.cos(t) * u + np.sin(t) * v) for t in ts]
            k0 = len(P)
            P += arc
            segs += [[k0 + i, k0 + i + 1] for i in range(len(arc) - 1)]
            cols += [(1.0, 0.9, 0.3)] * (len(arc) - 1)
            self.view.set_lines(f'{prefix}{k}', np.array(P), segs, cols, None, 2.0, on_top=True)

    def _draw_pivot(self):
        """Маркер опорной точки и три транспортира."""
        self.view.remove_prefix('pivot:')
        if self.pivot is None:
            if self.pivot_A is not None:
                self.view.set_sphere('pivot:A', self.pivot_A, 0.05, (1.0, 0.85, 0.2))
            return
        c, r = self.pivot['c'], self.pivot['r']
        self.view.set_sphere('pivot:c', c, max(0.02, r * 0.03), (1.0, 0.85, 0.2))
        self._draw_rings('pivot:', {'c': c, 'r': r, 'axes': self.PIVOT_AXES, 'ang': self.pivot['ang']})

    def _ring_hit(self, x, y, tol=9):
        spec = self._protractor()
        if spec is None:
            return None
        best = None
        for k in spec['axes']:
            s_, _, front = self.view.project(self._ring_pts(spec, k, 360))
            d = np.hypot(s_[:, 0] - x, s_[:, 1] - y)
            d[~front] = np.inf
            if d.min() < tol and (best is None or d.min() < best[0]):
                best = (d.min(), k)
        return None if best is None else best[1]

    def _ring_angle(self, k, x, y, spec=None):
        """Угол точки под курсором в плоскости кольца, рад (None — кольцо видно с ребра)."""
        spec = spec or self._protractor()
        n_, u, v, _ = spec['axes'][k]
        o, dvec = self.view.ray(x, y)
        den = dvec @ n_
        if abs(den) < 0.15:
            return None
        q = o + ((spec['c'] - o) @ n_) / den * dvec - spec['c']
        return float(np.arctan2(q @ v, q @ u))

    def _ring_points(self, k, n=180):
        """Кольцо опорной точки (для тестов)."""
        return self._ring_pts({'c': self.pivot['c'], 'r': self.pivot['r'], 'axes': self.PIVOT_AXES}, k, n)

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
        self.pivot, self.pivot_pick, self.pivot_A = None, False, None
        self.view.remove_prefix('pivot:')
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
        if self.measure_mode and kind in ('press', 'release') and ev.button() == Qt.LeftButton:
            if kind == 'press':
                self._mclick = (x, y)
                return False                             # перетаскивание — по-прежнему вращение вида
            if kind == 'release' and self._mclick is not None:
                x0, y0 = self._mclick
                self._mclick = None
                if abs(x - x0) + abs(y - y0) < 5:
                    self.measure_click(x, y)
                return False
        if kind == 'press' and ev.button() == Qt.LeftButton:
            if manual and self.pivot_pick and mods & (Qt.ControlModifier | Qt.MetaModifier):
                want = self.fixed if self.pivot_A is None else self.moving
                hit = self.view.pick_point(x, y, names={f'scan:{want.id}'})
                if hit is None:
                    self.set_status('мимо облака')
                else:
                    self._pivot_picked(hit[1], want)
                return True
            spec = self._protractor()
            if spec is not None and not mods & (Qt.ControlModifier | Qt.MetaModifier):
                k = self._ring_hit(x, y)
                if k is not None:
                    self._ring_drag = {'k': k, 'a0': self._ring_angle(k, x, y, spec), 'v0': spec['ang'][k], 'x0': x}
                    return True
            if manual and self.pivot is not None and not mods & (Qt.ControlModifier | Qt.MetaModifier):
                if mods & Qt.ShiftModifier and not mods & Qt.AltModifier:
                    self.set_status('опорная точка закреплена: сдвиг недоступен («Снять точку» — освободить)')
                    return True
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
        if kind == 'move' and self._ring_drag is not None:
            g = self._ring_drag
            a = self._ring_angle(g['k'], x, y)
            if a is None or g['a0'] is None:
                delta = 0.25 * (x - g['x0'])             # кольцо с ребра — по горизонтали мыши
            else:
                delta = float(np.degrees(np.angle(np.exp(1j * (a - g['a0'])))))
            if mods & Qt.ShiftModifier:
                delta *= 0.1                             # Shift — точнее
            spec = self._protractor()
            if spec is not None:
                spec['set'](g['k'], g['v0'] + delta)
            return True
        if kind == 'release' and self._ring_drag is not None:
            self._ring_drag = None
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
                elif self.pivot is not None:               # Alt+тянуть — поворот вокруг опорной точки
                    a = self.pivot['ang']
                    self._set_pivot_angles(yaw=d.setdefault('yaw0', a['yaw']) - 0.3 * (x - d['x0']))
                    return True
                else:
                    self.T_moving = Session.nudge(d['T0'], dyaw_deg=-0.3 * (x - d['x0']))
                self._icp_refined = False
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
                self._fit_timer.start(300)
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
        if self.measure_mode == 'poly':
            self.measure_finish()
            return
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
        self.vp.b_fly.setChecked(on)
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
        self.vp.b_fly.setChecked(True)
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

    # ── поверхность (экспериментально) ───────────────────────────────────
    def _surface_targets(self):
        if self.surface_panel.params()['scope'] == 'scene':
            return [sc for sc in self.s.placed() if sc.visible and sc.analyzed]
        sc = self.s.by_id(self.tree_sel) if self.tree_sel else None
        sc = sc if sc is not None and sc.pose is not None else self.s.ref if self.s.scans else None
        return [sc] if sc is not None and sc.analyzed else []

    def refresh_surface(self):
        sp = self.surface_panel
        tg = self._surface_targets()
        if sp.params()['scope'] == 'scene':
            sp.target.setText(f'видимые размещённые сканы: {len(tg)}' if tg else 'нет видимых размещённых сканов')
        else:
            sp.target.setText(f'скан {short(tg[0].id)} (выберите другой в дереве)' if tg else
                              'выберите размещённый скан в дереве')
        sig = self.s.state_signature()
        rows = []
        for key, m in self.s.meshes.items():
            i = m['info']
            sub = (f"{'Пуассон' if i['method'] == 'poisson' else 'ball pivoting'} · {i['acc'] * 100:.0f} см · "
                   f"{i['triangles']:,} треуг. · {i['seconds']} c".replace(',', ' '))
            if i['acc_used'] > i['acc'] * 1.01:
                sub += f" · загрублено до {i['acc_used'] * 100:.1f} см"
            if m['sig'] != sig:
                sub += ' · позы изменились — постройте заново'
            rows.append((key, m['title'], sub, m['visible']))
        sp.set_meshes(rows)
        sp.sw_points.setChecked(not self.points_hidden)

    def _mesh_finished(self):
        self._mesh_building = False
        self.b_build.setText('Построить')

    def _draw_meshes(self, force=False):
        show = self.tree_panel.layer('mesh')
        names = set()
        for key, m in self.s.meshes.items():
            name = f'mesh:{key}'
            names.add(name)
            if self.view.has(name) and not force:
                self.view.set_visible(name, show and m['visible'])
                continue
            if show and m['visible']:
                self.view.set_mesh(name, m['V'], m['F'], m['color'])
        for n in [n for n in self.view.items if n.startswith('mesh:') and n not in names]:
            self.view.remove(n)

    def on_surface_action(self, name, arg):
        sp = self.surface_panel
        if name == 'scope':
            self.refresh_surface()
        elif name == 'build':
            tg = self._surface_targets()
            if not tg:
                self.set_status('нечего строить: выберите размещённый скан или включите видимые сканы')
                return
            if self.busy and getattr(self, '_mesh_building', False):
                return self.on_surface_action('stop', None)
            prm = sp.params()
            sess = self.s
            sig = sess.state_signature()
            self._mesh_cancel = False

            def work(p):
                try:
                    return surface.build(sess, tg, prm['method'], prm['acc'], prm['trim'], p,
                                         cancel=lambda: self._mesh_cancel)
                except surface.Cancelled:
                    return None
                finally:
                    bg.post(self._mesh_finished)

            def done(m):
                if m is None:
                    self.set_status('построение сетки остановлено')
                    return
                if sess is not self.s:
                    return
                if len(tg) == 1:
                    key, title = tg[0].id, f'скан {short(tg[0].id)}'
                    color = (0.80, 0.80, 0.77)
                else:
                    k = 1 + sum(1 for x in sess.meshes if x.startswith('scene'))
                    key, title, color = f'scene{k}', f'сцена {k} ({len(tg)} скан.)', (0.80, 0.80, 0.77)
                sess.meshes[key] = dict(m, title=title, color=color, visible=True, sig=sig)
                self.view.remove(f'mesh:{key}')
                if not self.tree_panel.layer('mesh'):
                    self.tree_panel.switches['mesh'].setChecked(True)
                self._draw_meshes()
                self.set_points_visible(False)           # иначе сетка не видна среди точек
                V = m['V']
                if len(V):
                    R = self.view.basis()
                    self.view.fit(np.percentile(V, 1, axis=0), np.percentile(V, 99, axis=0), R[:, 2], R[:, 1])
                self.refresh_surface()
                i = m['info']
                msg = (f"{title}: {i['triangles']:,} треугольников за {i['seconds']} c. Точки сканов скрыты — "
                       f"глаз «Точки сканов» в дереве (ветвь «Поверхности») вернёт их").replace(',', ' ')
                self.set_status(msg)
                self.vp.flash(msg, 6000)
                self.tree_panel.rebuild()
                it = self.tree_panel._items.get('mesh:' + key)
                if it is not None:
                    self.tree_panel.tree.scrollToItem(it)
            if self.run_bg(f"поверхность ({'Пуассон' if prm['method'] == 'poisson' else 'ball pivoting'}, "
                           f"{prm['acc'] * 100:.0f} см)…", work, done):
                self._mesh_building = True
                self.b_build.setText('Остановить')
        elif name == 'mesh_visible':
            key, vis = arg
            self._mesh_tree_visible('mesh:' + key, vis)
        elif name == 'mesh_delete':
            self.s.meshes.pop(arg, None)
            self._draw_meshes()
            if not self.s.meshes and self.points_hidden:   # сеток больше нет — вернуть точки
                self.set_points_visible(True)
            self.refresh_surface()
            self.tree_panel.rebuild()
        elif name == 'mesh_export':
            m = self.s.meshes.get(arg)
            if m is None:
                return
            base = Path(self.s.project_path).with_suffix('') if self.s.project_path else Path.home() / 'mesh'
            path, _ = QFileDialog.getSaveFileName(self, 'Экспорт сетки', f'{base}_{short(arg)}.obj',
                                                  'OBJ (*.obj);;PLY (*.ply);;STL (*.stl)')
            if path:
                if Path(path).suffix.lower() not in ('.obj', '.ply', '.stl'):
                    path += '.obj'
                surface.export(m, path)
                self.set_status(f'сетка сохранена: {path}')
        elif name == 'points':
            self.set_points_visible(arg)
        elif name == 'stop':
            self._mesh_cancel = True
            self.set_status('построение сетки останавливается…')

    # ── найденные объекты ────────────────────────────────────────────────
    def set_points_visible(self, on):
        """Точки сканов на экране (сетки иначе тонут в точках, по которым построены)."""
        self.points_hidden = not on
        self.tree_panel.switches['points'].setChecked(on)
        self.surface_panel.sw_points.setChecked(on)
        self.tree_panel.points_hidden = not on
        self.apply_visibility()
        self.tree_panel.rebuild()

    def on_layer(self, key, on):
        if key == 'points':
            if self.points_hidden == on:
                self.set_points_visible(on)
            return
        if key == 'mesh':
            self._draw_meshes()
            self.tree_panel.meshes_layer_on = on
            self.tree_panel.rebuild()
            return
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
        elif name == 'dyn_find':
            self.on_dyn_find()
        elif name == 'dyn_thr':
            self._dyn_apply_threshold(arg)
        elif name == 'dyn_delete':
            self.on_dyn_delete()

    # ── движущиеся объекты ───────────────────────────────────────────────
    def _dyn_targets(self):
        if self.clean.sw_dyn_all.isChecked():
            return [sc for sc in self.s.scans if sc.visible and sc.analyzed]
        sc = self.clean_scan
        return [sc] if sc is not None and sc.analyzed else []

    def on_dyn_find(self):
        targets = self._dyn_targets()
        if not targets:
            self.set_status('нет проанализированного скана для поиска')
            return
        use_cross, use_pass = self.clean.cb_cross.isChecked(), self.clean.cb_pass.isChecked()
        if not (use_cross or use_pass):
            self.set_status('отметьте хотя бы один признак: между сканами или по полуоборотам')
            return
        thr = self.clean.dyn_threshold()
        sess = self.s

        def work(progress):
            out = {}
            for k, sc in enumerate(targets):
                progress(k / len(targets), f'движущиеся объекты: {short(sc.id)}')
                cross = use_cross and sc.pose is not None and len(sess.placed()) > 1
                out[sc.id] = dynamic.find(sess, sc, thr, use_cross=cross, use_pass=use_pass)
            return out

        def done(res):
            self.dyn_masks = {sid: m for sid, (m, _) in res.items()}
            no_pass = [short(sid) for sid, (_, info) in res.items() if use_pass and info['pass'] is None]
            self._dyn_draw()
            n = sum(int(m.sum()) for m in self.dyn_masks.values())
            msg = f'найдено {n:,} точек в {sum(1 for m in self.dyn_masks.values() if m.any())} скан.'.replace(',', ' ')
            if no_pass:
                msg += f"; нет данных полуоборотов: {', '.join(no_pass[:6])}{' …' if len(no_pass) > 6 else ''}"
            self.clean.dyn_label.setText(msg)
            self.set_status(msg + ' — «Удалить найденное» уберёт их (Ctrl+Z — вернуть)')
        self.run_bg('поиск движущихся объектов…', work, done)

    def _dyn_apply_threshold(self, thr):
        if not self.dyn_masks:
            return
        self.dyn_masks = {sid: dynamic.mask(self.s.by_id(sid), thr) for sid in self.dyn_masks
                          if self.s.by_id(sid) is not None}
        self._dyn_draw()
        n = sum(int(m.sum()) for m in self.dyn_masks.values())
        self.clean.dyn_label.setText(f'найдено {n:,} точек (порог {thr:.2f})'.replace(',', ' '))

    def _dyn_draw(self):
        self.view.remove_prefix('dyn:')
        for sid, m in self.dyn_masks.items():
            sc = self.s.by_id(sid)
            T = self.pose_of(sc) if sc is not None else None
            if T is None or not m.any() or not sc.visible:
                continue
            self.view.set_cloud(f'dyn:{sid}', sc.res['down'][m], (0.95, 0.30, 0.90), T,
                                size=self.view.point_px + 2, on_top=True)

    def on_dyn_delete(self):
        items = [(self.s.by_id(sid), self.s.by_id(sid).res['down'][m]) for sid, m in self.dyn_masks.items()
                 if self.s.by_id(sid) is not None and m.any()]
        if not items:
            self.set_status('нечего удалять: сначала «Найти»')
            return
        n = self.s.drop_points(items)
        self.dyn_masks = {}
        self.view.remove_prefix('dyn:')
        self._refresh_scans([sc.id for sc, _ in items])
        self.clean.dyn_label.setText('')
        self.set_status(f"удалено вокселей 5 см: {n} из {', '.join(short(sc.id) for sc, _ in items)} "
                        f"(Ctrl+Z — вернуть)")

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
        if self.measure_mode:
            if self._mpts or self._mplane is not None:
                self._mpts, self._mplane = [], None
                self._draw_measures()
                self.set_status('замер отменён')
            else:
                self.set_measure_mode(None)
            return
        if getattr(self, '_mesh_building', False):
            self._mesh_cancel = True
            self.set_status('построение сетки останавливается…')
        elif self.downloading:
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

    def show_about(self):
        from .version import __version__, RELEASE_DATE
        import vtkmodules.vtkCommonCore as vc
        import open3d
        from PySide6 import __version__ as pyside
        QMessageBox.about(self, 'О программе', (
            f'<h3>Pulse Scan {__version__}</h3><p>Обработка сканов лидара Pulse: импорт bag, стыковка, '
            f'контроль, чистка, экспорт.</p><p>Сборка от {RELEASE_DATE}.<br>Qt (PySide6) {pyside} · '
            f'VTK {vc.vtkVersion.GetVTKVersion()} · Open3D {open3d.__version__} · Python {platform.python_version()}'
            f'</p><p>Проекты, скриншоты: {user_dir()}</p>'))

    def show_manual(self):
        """Руководство (docs/manual/README.md, в сборке — рядом с программой) в отдельном окне."""
        from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout
        from PySide6.QtCore import QUrl
        f = manual_path()
        if f is None:
            self.set_status('руководство не найдено (docs/manual/README.md)')
            return
        d = QDialog(self)
        d.setWindowTitle('Руководство пользователя — Pulse Scan')
        d.resize(980, 860)
        lay = QVBoxLayout(d)
        lay.setContentsMargins(0, 0, 0, 0)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setSearchPaths([str(f.parent)])
        tb.document().setDefaultStyleSheet('img { max-width: 860px; } code { font-family: "IBM Plex Mono"; }')
        tb.setSource(QUrl.fromLocalFile(str(f)))
        tb.setStyleSheet('QTextBrowser { padding: 18px 28px; font-size: 14px; }')
        lay.addWidget(tb)
        d.show()

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
        out = Path(out_dir or user_dir('Скриншоты'))
        out.mkdir(parents=True, exist_ok=True)
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
