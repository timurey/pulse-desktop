"""Запуск окна: шрифты, тема, сеанс из аргументов командной строки."""

import os
import sys

from PySide6.QtCore import QSettings, QObject
from PySide6.QtWidgets import QApplication

from . import bg
from . import widgets
from .theme import Theme, load_fonts, SANS


def make_app(argv=None):
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv[:1])
    # Open3D загружается в главном потоке заранее (предосторожность): модули логики импортируют
    # его лениво, и первым мог оказаться фоновый поток загрузки сканов, а библиотека с
    # GUI-модулями на macOS не обязана быть к этому готова.
    import open3d                                        # noqa: F401
    app.setApplicationName('Pulse Scan')
    app.setOrganizationName('Pulse')
    import sys as _sys
    from pathlib import Path as _P
    from PySide6.QtGui import QIcon
    for base in (_P(getattr(_sys, '_MEIPASS', '')), _P(__file__).resolve().parent.parent):
        if (base / 'packaging' / 'icon.png').exists():
            app.setWindowIcon(QIcon(str(base / 'packaging' / 'icon.png')))
            break
    load_fonts()
    f = app.font()
    f.setFamily(SANS)
    f.setPixelSize(13)
    app.setFont(f)
    dark = QSettings('Pulse', 'PulseScan').value('dark', True)
    theme = Theme(dark not in (False, 'false', '0', 0))
    widgets.set_theme(theme)
    app.setStyleSheet(theme.qss())
    theme.apply_palette(app)
    bg.init()
    return app, theme


def session_from_args(args):
    from scan_session import Session
    if args and args[0].lower().endswith(('.json', '.pulse')):
        return Session.from_project(args[0])
    if args:
        return Session.from_scans(args)
    return Session()


class _FileOpen(QObject):
    """Событие открытия файла от системы (macOS отдаёт файл так, а не аргументом)."""

    def __init__(self, win):
        super().__init__()
        self.win = win

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.FileOpen:
            path = ev.file()
            if path.lower().endswith(('.pulse', '.json')):
                if self.win.confirm_discard('Открыть другой проект'):
                    self.win.open_project(path)
            elif path:
                self.win._ingest([self.win.s.add_scan(path)])
            return True
        return False


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):              # русский текст в консоли Windows
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                                # noqa: BLE001
            pass
    args = sys.argv[1:] if argv is None else argv
    if '--version' in args:
        from .version import __version__
        print(f'Pulse Scan {__version__}')
        return
    if '--selftest' in args:
        from .selftest import run
        os._exit(run(args))
    app, theme = make_app()
    from .main_window import MainWindow
    win = MainWindow(session_from_args(args), theme)
    prefs = QSettings('Pulse', 'PulseScan')
    if prefs.value('point_px'):
        win.set_point_size(int(float(prefs.value('point_px'))))
    if prefs.value('parallel') in (True, 'true', '1', 1):
        win.toggle_projection(True)
    win.show()
    opener = _FileOpen(win)                              # macOS: двойной щелчок по .pulse в Finder
    app.installEventFilter(opener)
    code = app.exec()
    sys.stdout.flush()
    sys.stderr.flush()
    # как в scan_gui: без финализации интерпретатора (фоновые потоки анализа)
    os._exit(code)
