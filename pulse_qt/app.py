"""Запуск окна: шрифты, тема, сеанс из аргументов командной строки."""

import os
import sys

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from . import bg
from . import widgets
from .theme import Theme, load_fonts, SANS


def make_app(argv=None):
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName('Pulse Scan')
    app.setOrganizationName('Pulse')
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


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):              # русский текст в консоли Windows
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                                # noqa: BLE001
            pass
    args = sys.argv[1:] if argv is None else argv
    app, theme = make_app()
    from .main_window import MainWindow
    win = MainWindow(session_from_args(args), theme)
    prefs = QSettings('Pulse', 'PulseScan')
    if prefs.value('point_px'):
        win.set_point_size(int(float(prefs.value('point_px'))))
    if prefs.value('parallel') in (True, 'true', '1', 1):
        win.toggle_projection(True)
    win.show()
    code = app.exec()
    sys.stdout.flush()
    sys.stderr.flush()
    # как в scan_gui: без финализации интерпретатора (фоновые потоки анализа)
    os._exit(code)
