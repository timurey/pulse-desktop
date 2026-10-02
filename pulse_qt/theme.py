"""
Тема окна: палитры (тёмная / светлая) из макета Claude Design, таблица стилей Qt,
шрифты IBM Plex (лежат в pulse_qt/fonts, лицензия OFL) и значки (qtawesome, MDI).

Цвета в макете заданы в OKLCH; здесь они переводятся в sRGB один раз при старте.
"""

import math
from pathlib import Path

from PySide6.QtGui import QColor, QFontDatabase, QFont, QIcon

FONT_DIR = Path(__file__).resolve().parent / 'fonts'
SANS = 'IBM Plex Sans'
MONO = 'IBM Plex Mono'


# ── цвет ───────────────────────────────────────────────────────────────────
def oklch(L, C, h, a=1.0):
    """OKLCH → (r, g, b, a) 0..255 (sRGB, с обрезкой гаммы)."""
    hr = math.radians(h)
    la, lb = C * math.cos(hr), C * math.sin(hr)
    l_ = L + 0.3963377774 * la + 0.2158037573 * lb
    m_ = L - 0.1055613458 * la - 0.0638541728 * lb
    s_ = L - 0.0894841775 * la - 1.2914855480 * lb
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    rgb = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
           -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
           -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)

    def enc(x):
        x = min(1.0, max(0.0, x))
        return 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
    return tuple(int(round(enc(c) * 255)) for c in rgb) + (int(round(a * 255)),)


def hexc(c):
    return '#%02x%02x%02x' % c[:3]


def rgba(c):
    return f'rgba({c[0]},{c[1]},{c[2]},{c[3] / 255:.3f})'


# оттенки сканов: в дереве (образец), в 3D-виде и в таблицах — один и тот же цвет
SCAN_HUES = [250, 70, 150, 25, 300, 200, 110, 340, 45, 175]


def scan_rgb(i, dark=True):
    """Цвет скана №i: (r, g, b) 0..1 для VTK и QColor для интерфейса."""
    c = oklch(0.74 if dark else 0.62, 0.14, SCAN_HUES[i % len(SCAN_HUES)])
    return tuple(x / 255 for x in c[:3]), QColor(*c[:3])


PICK_HUES = [150, 70, 300, 25, 200, 110]


def pick_rgb(k):
    c = oklch(0.75, 0.16, PICK_HUES[k % len(PICK_HUES)])
    return tuple(x / 255 for x in c[:3]), QColor(*c[:3])


def _palette(dark):
    if dark:
        t = dict(bg=oklch(.17, .008, 250), bg1=oklch(.205, .008, 250), bg2=oklch(.235, .009, 250),
                 bg3=oklch(.27, .01, 250), line=oklch(.30, .01, 250), line2=oklch(.36, .012, 250),
                 ink=oklch(.94, .004, 250), ink2=oklch(.78, .008, 250), ink3=oklch(.62, .01, 250),
                 canvas=oklch(.13, .008, 250), grid=oklch(.24, .01, 250),
                 accent=oklch(.72, .13, 250), accentbg=oklch(.72, .13, 250, .16),
                 warn=oklch(.75, .13, 70), warnbg=oklch(.72, .13, 70, .16),
                 ok=oklch(.74, .13, 150), okbg=oklch(.72, .13, 150, .16),
                 bad=oklch(.68, .17, 25), badbg=oklch(.68, .17, 25, .16),
                 onaccent=oklch(.99, 0, 0), glass=oklch(.205, .008, 250, .88))
    else:
        t = dict(bg=oklch(.965, .003, 250), bg1=oklch(.99, .002, 250), bg2=oklch(.945, .004, 250),
                 bg3=oklch(.91, .005, 250), line=oklch(.88, .006, 250), line2=oklch(.80, .008, 250),
                 ink=oklch(.22, .01, 250), ink2=oklch(.40, .01, 250), ink3=oklch(.52, .01, 250),
                 canvas=oklch(.95, .004, 250), grid=oklch(.86, .006, 250),
                 accent=oklch(.55, .15, 250), accentbg=oklch(.55, .15, 250, .12),
                 warn=oklch(.55, .15, 70), warnbg=oklch(.55, .15, 70, .12),
                 ok=oklch(.52, .14, 150), okbg=oklch(.55, .15, 150, .12),
                 bad=oklch(.55, .18, 25), badbg=oklch(.55, .18, 25, .12),
                 onaccent=oklch(.99, 0, 0), glass=oklch(.99, .002, 250, .9))
    t['dark'] = dark
    return t


class Theme:
    def __init__(self, dark=True):
        self.set(dark)

    def set(self, dark):
        self.dark = dark
        self.c = _palette(dark)

    def q(self, name):
        c = self.c[name]
        return QColor(c[0], c[1], c[2], c[3])

    def rgb(self, name):
        """(r, g, b) 0..1 — для VTK."""
        return tuple(x / 255 for x in self.c[name][:3])

    def icon(self, name, color='ink2', active='accent'):
        return icon(name, self.q(color), self.q(active))

    def apply_palette(self, app):
        """Палитра Qt под тему: выделение и фон там, куда таблица стилей не достаёт."""
        from PySide6.QtGui import QPalette
        pal = app.palette()
        for role, name in ((QPalette.Window, 'bg'), (QPalette.Base, 'bg1'), (QPalette.AlternateBase, 'bg2'),
                           (QPalette.Text, 'ink'), (QPalette.WindowText, 'ink'), (QPalette.ButtonText, 'ink'),
                           (QPalette.Button, 'bg2'), (QPalette.ToolTipBase, 'bg2'), (QPalette.ToolTipText, 'ink'),
                           (QPalette.PlaceholderText, 'ink3')):
            pal.setColor(role, self.q(name))
        hl = self.q('accent')
        bg = self.q('bg1')
        a = 0.16 if self.dark else 0.12
        mix = QColor(int(bg.red() + (hl.red() - bg.red()) * a), int(bg.green() + (hl.green() - bg.green()) * a),
                     int(bg.blue() + (hl.blue() - bg.blue()) * a))
        pal.setColor(QPalette.Highlight, mix)
        pal.setColor(QPalette.HighlightedText, self.q('ink'))
        app.setPalette(pal)

    def cube_style(self):
        c = self.c
        if self.dark:
            return {'bg': c['bg1'][:3], 'face': oklch(.42, .02, 250)[:3], 'edge': oklch(.55, .02, 250)[:3],
                    'label': c['ink'][:3], 'hover': c['accent'][:3]}
        return {'bg': c['bg1'][:3], 'face': oklch(.86, .02, 250)[:3], 'edge': oklch(.55, .02, 250)[:3],
                'label': c['ink'][:3], 'hover': c['accent'][:3]}

    # ── таблица стилей ───────────────────────────────────────────────────
    def qss(self):
        ink3, on = hexc(self.c['ink3']), hexc(self.c['onaccent'])
        img = {'chev_down': icon_file('mdi6.chevron-down', ink3), 'chev_right': icon_file('mdi6.chevron-right', ink3),
               'check': icon_file('mdi6.check-bold', on, 14)}
        bg1, acc = self.c['bg1'], self.c['accent']
        k = 0.16 if self.dark else 0.12
        selbg = '#%02x%02x%02x' % tuple(int(bg1[i] + (acc[i] - bg1[i]) * k) for i in range(3))
        c = {k: (hexc(v) if isinstance(v, tuple) and v[3] == 255 else rgba(v) if isinstance(v, tuple) else v)
             for k, v in self.c.items()}
        c['selbg'] = selbg
        return f"""
* {{ font-family: "{SANS}"; font-size: 13px; color: {c['ink']}; }}
QMainWindow, QDialog {{ background: {c['bg']}; }}
QToolTip {{ background: {c['bg2']}; color: {c['ink']}; border: 1px solid {c['line2']}; padding: 4px 6px; border-radius: 6px; }}

#TopBar {{ background: {c['bg1']}; border-bottom: 1px solid {c['line']}; }}
#AppName {{ font-weight: 600; }}
#ProjName {{ font-family: "{MONO}"; color: {c['ink3']}; }}
#Mark {{ background: {c['accent']}; border-radius: 6px; }}
#DirtyDot {{ background: {c['warn']}; border-radius: 3px; }}
#ToolSep {{ background: {c['line']}; }}

QToolButton {{ background: transparent; border: 0; border-radius: 7px; padding: 0 9px; color: {c['ink2']}; min-height: 32px; }}
QToolButton:hover {{ background: {c['bg3']}; color: {c['ink']}; }}
QToolButton:checked {{ background: {c['accentbg']}; color: {c['accent']}; }}
QToolButton:disabled {{ color: {c['ink3']}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolButton#IconBtn {{ padding: 0; min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; }}
QToolButton[menuarrow="true"] {{ padding-right: 4px; }}

#Panel {{ background: {c['bg1']}; }}
#PanelLeft {{ background: {c['bg1']}; border-right: 1px solid {c['line']}; }}
#PanelRight {{ background: {c['bg1']}; border-left: 1px solid {c['line']}; }}
#PHead {{ background: {c['bg1']}; border-bottom: 1px solid {c['line']}; }}
#PTitle, #STitle {{ font-size: 11px; font-weight: 600; letter-spacing: 1px; color: {c['ink3']}; }}
#Sec {{ background: transparent; border-bottom: 1px solid {c['line']}; }}
#SecStick {{ background: {c['bg1']}; border-top: 1px solid {c['line']}; }}
#Lbl {{ font-size: 11px; color: {c['ink3']}; }}
#Hint {{ font-size: 11px; color: {c['ink3']}; }}
#H1 {{ font-family: "{MONO}"; font-size: 15px; font-weight: 600; }}
#KV_k {{ font-family: "{MONO}"; font-size: 12px; color: {c['ink3']}; }}
#KV_v {{ font-family: "{MONO}"; font-size: 12px; color: {c['ink']}; }}
#Mono {{ font-family: "{MONO}"; }}

QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: 0; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c['line2']}; border-radius: 4px; min-height: 24px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {c['line2']}; border-radius: 4px; min-width: 24px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QPushButton {{ background: {c['bg2']}; border: 1px solid {c['line2']}; border-radius: 7px; padding: 0 12px; min-height: 30px; color: {c['ink']}; }}
QPushButton:hover {{ background: {c['bg3']}; }}
QPushButton:pressed {{ background: {c['line']}; }}
QPushButton:disabled {{ color: {c['ink3']}; background: {c['bg1']}; }}
QPushButton[primary="true"] {{ background: {c['accent']}; border-color: {c['accent']}; color: {c['onaccent']}; font-weight: 500; }}
QPushButton[primary="true"]:disabled {{ background: {c['bg3']}; border-color: {c['line2']}; color: {c['ink3']}; }}
QPushButton[pad="true"] {{ font-size: 11px; color: {c['ink2']}; min-height: 32px; padding: 0 6px; }}
QPushButton[pad="true"]:hover {{ color: {c['ink']}; }}

QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{ background: {c['bg']}; border: 1px solid {c['line2']}; border-radius: 7px; padding: 0 8px; min-height: 30px; font-size: 12px; selection-background-color: {c['accent']}; }}
QLineEdit:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {c['accent']}; }}
QComboBox::drop-down {{ border: 0; width: 22px; }}
QComboBox::down-arrow {{ image: url({img['chev_down']}); width: 14px; height: 14px; margin-right: 6px; }}
QComboBox QAbstractItemView {{ background: {c['bg1']}; border: 1px solid {c['line2']}; selection-background-color: {c['accentbg']}; selection-color: {c['ink']}; outline: 0; padding: 4px; }}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button, QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: 0; }}

QCheckBox {{ spacing: 8px; color: {c['ink2']}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {c['line2']}; background: {c['bg']}; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; image: url({img['check']}); }}

QTreeView, QTableView, QListView {{ background: transparent; border: 0; outline: 0; selection-background-color: {c['accentbg']}; }}
QTreeView {{ show-decoration-selected: 1; }}
QTreeView::item {{ height: 30px; border: 0; }}
QTreeView::item:hover, QTableView::item:hover {{ background: {c['bg2']}; }}
QTreeView::item:selected, QTableView::item:selected {{ background: {c['selbg']}; color: {c['ink']}; }}
QTreeView::branch {{ background: transparent; }}
QTreeView::branch:selected {{ background: {c['selbg']}; }}
QTreeView::branch:has-children:closed {{ image: url({img['chev_right']}); }}
QTreeView::branch:has-children:open {{ image: url({img['chev_down']}); }}
QHeaderView::section {{ background: {c['bg1']}; color: {c['ink3']}; font-size: 11px; font-weight: 500; border: 0; border-bottom: 1px solid {c['line']}; padding: 6px 12px; }}
QTableView {{ gridline-color: transparent; }}
QTableView::item {{ border-bottom: 1px solid {c['line']}; padding: 0 12px; }}
QTableCornerButton::section {{ background: {c['bg1']}; border: 0; }}

QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {c['ink3']}; padding: 0 10px; height: 34px; border: 0; border-bottom: 2px solid transparent; font-size: 12px; }}
QTabBar::tab:selected {{ color: {c['ink']}; border-bottom-color: {c['accent']}; }}
QTabBar::tab:hover {{ color: {c['ink']}; }}
QTabWidget::pane {{ border: 0; }}

#Dock {{ background: {c['bg1']}; border-top: 1px solid {c['line']}; }}
#DockTabs {{ border-bottom: 1px solid {c['line']}; }}
#StatusBar {{ background: {c['bg1']}; border-top: 1px solid {c['line']}; }}
#StatusBar QLabel {{ font-size: 11px; color: {c['ink3']}; }}
#LiveDot {{ background: {c['ok']}; border-radius: 3px; }}
#LiveDotOff {{ background: {c['ink3']}; border-radius: 3px; }}
QProgressBar {{ background: {c['bg3']}; border: 0; border-radius: 2px; max-height: 4px; min-height: 4px; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 2px; }}

QSplitter::handle {{ background: {c['line']}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

#Glass {{ background: {c['glass']}; border: 1px solid {c['line']}; border-radius: 8px; }}
#Glass QLabel {{ color: {c['ink2']}; font-size: 12px; background: transparent; }}
#GlassMono {{ font-family: "{MONO}"; font-size: 11px; color: {c['ink3']}; background: transparent; }}

QMenu {{ background: {c['bg1']}; border: 1px solid {c['line2']}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 10px; border-radius: 5px; }}
QMenu::item:selected {{ background: {c['accentbg']}; color: {c['ink']}; }}
QMenu::item:disabled {{ color: {c['ink3']}; }}
QMenu::separator {{ height: 1px; background: {c['line']}; margin: 4px 6px; }}
QMenu::icon {{ padding-left: 6px; }}
QMenuBar {{ background: {c['bg1']}; }}
QMenuBar::item:selected {{ background: {c['bg3']}; }}

#Modal {{ background: {c['bg1']}; border: 1px solid {c['line']}; border-radius: 12px; }}
#MHead, #MFoot {{ background: transparent; }}
#MHead {{ border-bottom: 1px solid {c['line']}; }}
#MFoot {{ border-top: 1px solid {c['line']}; }}
#MTitle {{ font-size: 15px; font-weight: 600; }}
#BagRow {{ border-bottom: 1px solid {c['line']}; }}
#BagRow:hover {{ background: {c['bg2']}; }}
#Days {{ border-bottom: 1px solid {c['line']}; }}
QPushButton[day="true"] {{ min-height: 24px; max-height: 26px; border-radius: 13px; background: transparent; color: {c['ink2']}; font-size: 12px; padding: 0 10px; }}
QPushButton[day="true"]:checked {{ background: {c['accentbg']}; border-color: {c['accent']}; color: {c['accent']}; }}
QPushButton[seg="true"] {{ min-height: 24px; max-height: 26px; border: 0; border-radius: 6px; background: transparent; color: {c['ink3']}; font-size: 12px; padding: 0 9px; }}
QPushButton[seg="true"]:checked {{ background: {c['bg1']}; color: {c['ink']}; }}
#Seg {{ background: {c['bg2']}; border: 1px solid {c['line']}; border-radius: 8px; }}
#PairRow {{ background: {c['bg2']}; border-radius: 7px; }}
QLabel[chip] {{ border-radius: 5px; padding: 1px 7px; font-size: 11px; min-height: 18px; max-height: 20px; }}
QLabel[chip="acc"] {{ background: {c['accentbg']}; color: {c['accent']}; }}
QLabel[chip="warn"] {{ background: {c['warnbg']}; color: {c['warn']}; }}
QLabel[chip="ok"] {{ background: {c['okbg']}; color: {c['ok']}; }}
QLabel[chip="bad"] {{ background: {c['badbg']}; color: {c['bad']}; }}
QLabel[chip="mut"] {{ background: {c['bg3']}; color: {c['ink3']}; }}
QLabel[dof="ok"] {{ background: {c['okbg']}; color: {c['ok']}; border-radius: 7px; font-size: 11px; }}
QLabel[dof="no"] {{ background: {c['bg2']}; color: {c['ink3']}; border-radius: 7px; font-size: 11px; border: 1px dashed {c['line2']}; }}
#Banner {{ background: {c['accent']}; border-radius: 8px; }}
#Banner QLabel {{ color: {c['onaccent']}; font-size: 12px; background: transparent; }}
"""


def icon_file(name, color, size=16):
    """Значок в PNG (для url() в таблице стилей); кеш во временной папке."""
    import tempfile
    d = Path(tempfile.gettempdir()) / 'pulse_qt_icons'
    d.mkdir(exist_ok=True)
    f = d / f"{name.replace('.', '_')}_{color.lstrip('#')}_{size}.png"
    if not f.exists():
        icon(name, QColor(color)).pixmap(size * 2, size * 2).save(str(f))
    return f.as_posix()


# ── шрифты ─────────────────────────────────────────────────────────────────
def load_fonts():
    """Подключить IBM Plex из pulse_qt/fonts. → True, если шрифты найдены."""
    ok = False
    for f in sorted(FONT_DIR.glob('*.ttf')):
        if QFontDatabase.addApplicationFont(str(f)) >= 0:
            ok = True
    return ok


def ui_font_path():
    """Файл шрифта с кириллицей для Pillow (куб навигации)."""
    p = FONT_DIR / 'IBMPlexSans.ttf'
    return str(p) if p.exists() else None


def mono_font(size=12):
    f = QFont(MONO)
    f.setPixelSize(size)
    f.setStyleHint(QFont.Monospace)
    return f


# ── значки ─────────────────────────────────────────────────────────────────
_ICON_FAILED = set()


def icon(name, color, active=None):
    """Значок MDI через qtawesome; неизвестное имя — пустой значок (не роняет окно)."""
    import qtawesome as qta
    try:
        kw = {'color': color}
        if active is not None:
            kw['color_active'] = active
            kw['color_on'] = active
        return qta.icon(name, **kw)
    except Exception:                                    # noqa: BLE001
        if name not in _ICON_FAILED:
            _ICON_FAILED.add(name)
            print(f'[pulse_qt] нет значка {name}')
        return QIcon()
