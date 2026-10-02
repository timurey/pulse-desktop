#!/usr/bin/env python3
"""
Сквозная проверка окна Qt без человека: открыть проект, дождаться загрузки,
пройти основные состояния и сохранить снимки окна в OUT_DIR.

    python tests/qt_smoke.py X.pulse OUT_DIR [--light] [--keep]

Снимки: 01_overview, 02_scan, 03_features, 04_manual, 05_clean, 06_import, 07_3d, 08_theme.
Сервер сканера подменяется заглушкой (список записей из кода), сеть не нужна.
"""

import os
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PySide6.QtCore import QTimer, QSettings

FAKE_BAGS = [
    {'name': 'static_20261001_230136_646', 'mtime': '2026-10-01 23:01:36', 'size': '16.7 MB',
     'size_b': 16_700_000, 'recording': False},
    {'name': 'static_20261001_230131_189', 'mtime': '2026-10-01 23:01:31', 'size': '9.9 MB',
     'size_b': 9_900_000, 'recording': False},
    {'name': 'static_20261001_185259_134', 'mtime': '2026-10-01 18:52:59', 'size': '11.1 MB',
     'size_b': 11_100_000, 'recording': False},
    {'name': 'static_20261001_184303_731', 'mtime': '2026-10-01 18:43:03', 'size': '27.2 MB',
     'size_b': 27_200_000, 'recording': True},
    {'name': 'static_20260928_120001_001', 'mtime': '2026-09-28 12:00:01', 'size': '12.0 MB',
     'size_b': 12_000_000, 'recording': False},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project')
    ap.add_argument('out')
    ap.add_argument('--light', action='store_true')
    ap.add_argument('--keep', action='store_true', help='не закрывать окно в конце')
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    prefs = QSettings('Pulse', 'PulseScan')
    old_dark = prefs.value('dark', True)
    prefs.setValue('dark', not a.light)

    import scanner_client
    import plane_register as pr
    globals()['pr'] = pr
    scanner_client.ScannerClient.bags = lambda self: [dict(b) for b in FAKE_BAGS]

    from pulse_qt.app import make_app, session_from_args
    app, theme = make_app()
    from pulse_qt.main_window import MainWindow
    from pulse_qt import dialogs
    w = MainWindow(session_from_args([a.project]), theme)
    w.resize(1440, 900)
    w.show()
    t0 = time.time()
    log = []

    def shot(name):
        app.processEvents()
        w.view.render_now()
        app.processEvents()
        p = out / f'{name}.png'
        w.grab().save(str(p))
        log.append(f'{name}: {w.st_text.text()}')
        print(f'[{time.time() - t0:5.1f}s] {p.name}  |  {w.st_text.text()}', flush=True)

    def wait(cond, then, timeout=180):
        start = time.time()

        def tick():
            if cond():
                then()
            elif time.time() - start > timeout:
                print('ТАЙМАУТ ожидания', flush=True)
                finish(1)
            else:
                QTimer.singleShot(200, tick)
        tick()

    def finish(code=0):
        (out / 'log.txt').write_text('\n'.join(log) + '\n', encoding='utf-8')
        prefs.setValue('dark', old_dark)                # тема пользователя — как была
        prefs.sync()
        if not a.keep:
            w._closing_ok = True
            w.close()
            app.quit()
            os._exit(code)

    sess = w.s

    def step_overview():
        shot('01_overview')
        sc = next((x for x in sess.scans if x.id != sess.frame and x.pose is not None), sess.scans[0])
        w.tree_panel.select(sc.id)
        QTimer.singleShot(300, step_scan)

    def step_scan():
        shot('02_scan')
        w.tree_panel.switches['planes'].setChecked(True, emit=True)
        w.tree_panel.switches['openings'].setChecked(True, emit=True)
        QTimer.singleShot(400, step_features)

    def step_features():
        shot('03_features')
        w.tree_panel.switches['planes'].setChecked(False, emit=True)
        w.tree_panel.switches['openings'].setChecked(False, emit=True)
        unplaced = [x for x in sess.scans if x.pose is None]
        moving = unplaced[0] if unplaced else sess.scans[-1]
        w.start_manual(sess.frame, moving.id)
        # пара «пол ↔ пол» через выбор признаков (как Ctrl+клик)
        fx, mv = w.fixed, w.moving
        fl_f = max((p for p in fx.planes if p.kind == 'floor'), key=lambda p: p.area, default=None)
        fl_m = max((p for p in mv.planes if p.kind == 'floor'), key=lambda p: p.area, default=None)
        if fl_f is not None and fl_m is not None:
            w.pick_world(pr.transform(np.asarray(fl_f.centroid)[None], sess.Tc(fx))[0], fx)
            w.pick_world(pr.transform(np.asarray(fl_m.centroid)[None], w.T_moving)[0], mv)
        wait(lambda: not w.busy and not w._score_pending, step_manual, 60)

    def step_manual():
        shot('04_manual')
        w.on_manual_cancel()
        w.set_mode('clean')
        QTimer.singleShot(300, step_clean)

    def step_clean():
        w.view_top()
        app.processEvents()
        W_, H = w.view.width(), w.view.height()
        w.toggle_select()
        w.select_rect((W_ * 0.45, H * 0.45, W_ * 0.55, H * 0.55))
        shot('05_clean')
        w.clear_selection()
        w.toggle_select()
        w.set_mode('inspect')
        QTimer.singleShot(200, step_import)

    def step_import():
        import bag_reconstruct as br
        dlg = dialogs.ScannerImportDialog(w, 'pulse.local', str(out / 'bags'), br.DEFAULTS)
        dlg.show()

        def grab_dialog():
            dlg.mark_all()
            app.processEvents()
            dlg.grab().save(str(out / '06_import.png'))
            print(f'[{time.time() - t0:5.1f}s] 06_import.png  |  {dlg.b_ok.text()}', flush=True)
            log.append(f'06_import: {dlg.b_ok.text()}')
            dlg.reject()
            QTimer.singleShot(200, step_3d)
        wait(lambda: bool(dlg.bags), grab_dialog, 20)

    def step_3d():
        w.view_3d()
        shot('07_3d')
        w.toggle_theme()
        QTimer.singleShot(500, step_theme)

    def step_theme():
        shot('08_theme')
        w.toggle_theme()
        finish(0)

    wait(lambda: not w.busy and not w.analyzing and any(getattr(s, '_display', None) is not None
                                                        for s in sess.scans),
         lambda: QTimer.singleShot(300, step_overview))
    app.exec()


if __name__ == '__main__':
    main()
