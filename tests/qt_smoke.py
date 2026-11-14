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
        w.tree_panel.switches['quality'].setChecked(True, emit=True)
        wait(lambda: w.qual['res'] is not None and not w.qual['busy'], step_quality, 60)

    def step_quality():
        QTimer.singleShot(400, lambda: (shot('03b_quality'), step_quality_local()))

    def step_quality_local():
        w.quality_panel.method.group.button(1).click()
        wait(lambda: w.qual['res'] is not None and w.qual['res'].method == 'local' and not w.qual['busy'],
             lambda: QTimer.singleShot(400, lambda: (shot('03c_quality_local'), step_quality_off())), 60)

    def step_quality_off():
        w.tree_panel.switches['quality'].setChecked(False, emit=True)
        w.quality_panel.method.group.button(0).click()
        step_manual_start()

    def step_manual_start():
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
        # движущиеся объекты во всех видимых сканах
        w.clean.sw_dyn_all.setChecked(True)
        w.on_dyn_find()
        wait(lambda: not w.busy, step_dynamic, 120)

    def step_dynamic():
        QTimer.singleShot(300, lambda: (shot('05b_dynamic'), step_dynamic_done()))

    def step_dynamic_done():
        w.clean.sw_dyn_all.setChecked(False)
        w.set_mode('surface')
        w.tree_panel.select(sess.frame)
        w.on_surface_action('build', None)               # опорный скан, Пуассон 5 см
        wait(lambda: not w.busy and sess.meshes, step_surface, 300)

    def step_surface():
        w.on_surface_action('points', False)
        w.focus_node(sess.frame)
        w.view.set_view_dir((-0.5, -0.7, 0.6))
        QTimer.singleShot(400, lambda: (shot('05c_surface'), step_surface_done()))

    def step_surface_done():
        w.on_surface_action('points', True)
        w.set_mode('inspect')
        QTimer.singleShot(200, step_cbox)

    def step_cbox():
        """Боксы отсечения и их удаление из дерева.

        Один бокс — GPU-режим: каркас и 6 ручек обязаны исчезнуть вместе с боксом
        (актёры остаются в сцене, если обходить только список живых боксов).
        Два бокса — CPU-маска: после удаления последнего облака должны вернуться
        к полному составу. Прокрутка дерева не должна падать в начало."""
        def pts():
            return sum(len(it.P) for n, it in w.view.items.items() if n.startswith('scan:'))
        def cbox_actors():
            return [n for n in w.view.items if n.startswith('cbox')]
        base = pts()

        b1 = w.s.add_box([0, 0, 0], [3.0, 3.0, 3.0])
        w.cbox_sel = b1['id']
        w.refresh_clip_boxes()
        w.tree_panel.rebuild()
        app.processEvents()
        one = sorted(cbox_actors())
        w.on_tree_action('cbox_delete', 'cbox:' + b1['id'])
        app.processEvents()
        gpu_ok = len(one) == 7 and not cbox_actors() and w.cbox_sel is None and not sess.clip_boxes

        b2 = w.s.add_box([0, 0, 0], [3.0, 3.0, 3.0])
        b3 = w.s.add_box([5.0, 0, 0], [3.0, 3.0, 3.0])
        w.cbox_sel = b2['id']
        w.refresh_clip_boxes()
        w.tree_panel.rebuild()
        app.processEvents()
        cut, actors = pts(), cbox_actors()
        shot('05d_cbox')
        tree, sb = w.tree_panel.tree, w.tree_panel.tree.verticalScrollBar()
        tree.setFixedHeight(140)                          # чтобы прокрутка точно была
        app.processEvents()
        sb.setValue(sb.maximum())
        scrolled = sb.value()
        w.tree_panel.select_keys([f'cbox:{b2["id"]}', f'cbox:{b3["id"]}'])
        w.remove_selected()
        app.processEvents()
        kept = sb.value()
        tree.setMinimumHeight(0)
        tree.setMaximumHeight(16777215)
        ok = (gpu_ok and len(actors) > 1 and cut < base and not cbox_actors()
              and not sess.clip_boxes and w.cbox_sel is None and pts() == base
              and (scrolled == 0 or kept > 0))
        msg = (f'1 бокс: актёров {len(one)} → {len(cbox_actors())}; 2 бокса: точек {cut} из {base}, '
               f'актёров {len(actors)} → {len(cbox_actors())}, точек {pts()}, прокрутка {scrolled}→{kept} '
               f'— {"ок" if ok else "ОШИБКА"}')
        log.append(f'05d_cbox: {msg}')
        print(f'[{time.time() - t0:5.1f}s] 05d_cbox.png  |  {msg}', flush=True)
        if not ok:
            finish(1)
            return
        QTimer.singleShot(200, step_photo)

    def step_photo():
        """Фото: добавление, пары через окно снимка + 3D, привязка, окраска-слой, дерево.

        Снимок — синтетический рендер облака с известной позы, поэтому поза
        обязана восстановиться: σ ≈ 0 (проверяем по числу пар и наличию pose)."""
        import photos as ph_
        import planes as pl_
        import numpy as np
        from PIL import Image as Img
        np_ = np

        win, hn, f35 = 900, 700, 24
        ref = sess.ref
        Pw = pr.transform(np_.asarray(pl_.load_points(ref.path)[::61] @ ref.R_up.T, float),
                          sess.Tc(ref))
        lo, hi = Pw.min(axis=0), Pw.max(axis=0)
        c0 = (lo + hi) / 2
        eye = c0 + np_.array([1.0, -1.0, 0.45]) * float(np_.linalg.norm(hi - lo))
        Ttrue = ph_.T_of(ph_.look_R(c0 - eye, (0, 0, 1)), eye)
        uv, zz, okp = ph_.project(Pw, Ttrue, f35, win, hn)
        sel = np_.where(okp)[0]
        img = np_.full((hn, win, 3), 235, np_.uint8)
        img[np_.clip(uv[sel, 1].astype(int), 0, hn - 1),
            np_.clip(uv[sel, 0].astype(int), 0, win - 1)] = np_.array([90, 60, 140], np_.uint8)
        path = out / 'photo_test.jpg'
        Img.fromarray(img).save(path, quality=94)
        # 6 точек пар: экстремумы по осям (как характерные углы)
        idx = []
        for ax in range(3):
            for sg in (-1, 1):
                idx.append(int(sel[np_.argmax((Pw[sel] - c0)[:, ax] * sg)]))
        pairs_uv, pairs_p = uv[idx], Pw[idx]

        old_get = dialogs.QFileDialog.getOpenFileNames
        dialogs.QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([str(path)], ''))
        try:
            w.on_photo_action('add', None)
        finally:
            dialogs.QFileDialog.getOpenFileNames = old_get
        app.processEvents()
        p = sess.photos[-1] if sess.photos else None
        added = p is not None and __import__('pathlib').Path(p['path']).exists()
        copied = p['path'] if p is not None else ''
        w._photo_cur = p['id'] if p else None
        w.on_photo_action('f35', float(f35))
        w.photo_panel.cb_magnet.setChecked(False)   # синтетические пары идеальны — магнит не нужен
        w.set_measure_mode('photo')
        for (u, v), q in zip(pairs_uv, pairs_p):
            w._photo_canvas_event('press', u, v)       # клик по окну фото
            w._photo_canvas_event('release', u, v)     # отпускание (клик без протяжки)
            w.photo_click_at('scan:' + ref.id, q)      # клик по 3D-виду
        npairs = len(p.get('pairs') or []) if p else 0
        w._photo_bind()

        def after_bind():
            pose_ok = p is not None and p.get('pose') is not None
            if pose_ok:
                T2 = sess.Tc(ref) @ np_.asarray(p['pose'], float)
                pose_err = ph_.rot_err_deg(T2[:3, :3], Ttrue[:3, :3])
            else:
                pose_err = 1e9
            w.photo_paint_set(True)

            def after_paint():
                r = sess.photo_rgb(sess.placed()[0]) if sess.placed() else None
                painted = int(r[1].sum()) if r is not None else 0
                layer_on = w._photo_paint and w.tree_panel.switches['photo'].isChecked()
                w.on_tree_visible('photo:*', False)
                hidden = all(not q_['visible'] for q_ in sess.photos)
                w.on_tree_visible('photo:*', True)
                shown = all(q_['visible'] for q_ in sess.photos)
                from PySide6.QtWidgets import QMessageBox
                old_q = QMessageBox.question
                QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
                try:
                    w._photo_cur = p['id']
                    w.on_tree_action('photo_delete', 'photo:' + p['id'])   # меню дерева
                finally:
                    QMessageBox.question = old_q
                gone = not sess.photos
                w.photo_paint_set(False)
                app.processEvents()
                try:                                    # за собой: копия фото в photos/
                    _cp = __import__('pathlib').Path(copied)
                    _cp.unlink(missing_ok=True)
                    _cp.parent.rmdir()                  # пустой каталог photos/ не оставляем
                except Exception:                       # noqa: BLE001
                    pass
                shot('05e_photo')
                ok = added and npairs >= 4 and pose_ok and pose_err < 1.0 and painted > 0 \
                    and layer_on and hidden and shown and gone
                msg = (f'фото добавлено {added}, пар {npairs}, поза {pose_ok} '
                       f'(ошибка {pose_err:.2f}°), окрашено точек {painted}, слой {layer_on}, '
                       f'глаз {hidden and shown}, удалено {gone} — {"ок" if ok else "ОШИБКА"}')
                log.append(f'05e_photo: {msg}')
                print(f'[{time.time() - t0:5.1f}s] 05e_photo.png  |  {msg}', flush=True)
                if not ok:
                    finish(1)
                    return
                QTimer.singleShot(200, step_photo_pano)

            wait(lambda: not w.busy and not getattr(w, '_photo_warm_busy', False), after_paint, 120)

        wait(lambda: not w.busy, after_bind, 120)

    def step_photo_pano():
        """Панорама 360°: автодетект (кадр 2:1), пары без фокусного, обе привязки
        («камера у сканера» с выбором скана И 6-DOF без центра), окраска.

        Снимок — equirect-рендер облака с позы ровно на сканере опорного скана,
        поэтому центр «у сканера» — истина и поза обязана восстановиться."""
        import photos as ph_
        import planes as pl_
        from PIL import Image as Img

        win, hn = 1200, 600
        ref = sess.ref
        Pw = pr.transform(np.asarray(pl_.load_points(ref.path)[::61] @ ref.R_up.T, float),
                          sess.Tc(ref))
        c0 = (Pw.min(axis=0) + Pw.max(axis=0)) / 2
        C = np.asarray(sess.Tc(ref), float)[:3, 3]      # сканер опорного скана (общая система)
        Ttrue = ph_.T_of(ph_.rotvec_R(np.array([0.05, -0.04, 0.8])), C)
        uv, _, _ = ph_.project_pano(Pw, Ttrue, win, hn)
        img = np.full((hn, win, 3), 235, np.uint8)
        img[np.clip(uv[:, 1].astype(int), 0, hn - 1),
            np.clip(uv[:, 0].astype(int), 0, win - 1)] = np.array([60, 130, 90], np.uint8)
        path = out / 'photo_pano_test.jpg'
        Img.fromarray(img).save(path, quality=94)      # кадр 2:1 → автодетект 360°
        idx = []                                       # 6 характерных точек пар
        for ax in range(3):
            for sg in (-1, 1):
                idx.append(int(np.argmax((Pw - c0)[:, ax] * sg)))
        pairs_uv, pairs_p = uv[idx], Pw[idx]

        old_get = dialogs.QFileDialog.getOpenFileNames
        dialogs.QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([str(path)], ''))
        try:
            w.on_photo_action('add', None)
        finally:
            dialogs.QFileDialog.getOpenFileNames = old_get
        app.processEvents()
        p = sess.photos[-1] if sess.photos else None
        w._photo_cur = p['id'] if p else None
        pano_ok = p is not None and p.get('proj') == 'pano360'
        no_focal = p is not None and not p.get('f35')
        sec_hidden = pano_ok and w.photo_panel.sec_focal.isHidden() \
            and w.photo_panel.sec_align.isHidden() \
            and not w.photo_panel.row_scanner.isHidden()
        combo_ok = pano_ok and w.photo_panel.scanner_scan.currentData() == ref.id
        w.photo_panel.cb_magnet.setChecked(False)      # синтетические пары идеальны
        if w.measure_mode:                             # после шага photo режим мог остаться
            w.set_measure_mode(None, keep=True)        # включённым, а это toggle
        w.set_measure_mode('photo')
        for (u, v), q in zip(pairs_uv, pairs_p):
            w._photo_canvas_event('press', u, v)
            w._photo_canvas_event('release', u, v)
            w.photo_click_at('scan:' + ref.id, q)
        npairs = len(p.get('pairs') or []) if p else 0
        w._photo_bind()                                # путь 1: «у сканера» (по умолчанию)

        def pose_check():
            if p is None or p.get('pose') is None:
                return False, 1e9, 1e9
            T2 = sess.Tc(ref) @ np.asarray(p['pose'], float)
            return (True, ph_.rot_err_deg(T2[:3, :3], Ttrue[:3, :3]),
                    float(np.linalg.norm(T2[:3, 3] - Ttrue[:3, 3])))

        def after_bind1():
            ok1, err1, cerr1 = pose_check()
            p['pose'] = None                           # путь 2: 6-DOF без центра
            w.photo_panel.cb_scanner.setChecked(False)
            w._photo_bind()

            def after_bind2():
                ok2, err2, cerr2 = pose_check()
                w.photo_paint_set(True)

                def after_paint():
                    r = sess.photo_rgb(sess.placed()[0]) if sess.placed() else None
                    painted = int(r[1].sum()) if r is not None else 0
                    from PySide6.QtWidgets import QMessageBox
                    old_q = QMessageBox.question
                    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
                    try:
                        w._photo_cur = p['id']
                        w.on_tree_action('photo_delete', 'photo:' + p['id'])
                    finally:
                        QMessageBox.question = old_q
                    gone = not sess.photos
                    w.photo_paint_set(False)
                    w.set_measure_mode(None, keep=True)    # за собой погасить режим пар
                    app.processEvents()
                    try:                                    # за собой: копия фото в photos/
                        _cp = Path(p['path'])
                        _cp.unlink(missing_ok=True)
                        _cp.parent.rmdir()
                    except Exception:                       # noqa: BLE001
                        pass
                    shot('05f_photo_pano')
                    ok = pano_ok and no_focal and sec_hidden and combo_ok and npairs >= 3 \
                        and ok1 and err1 < 1.0 and cerr1 < 0.05 \
                        and ok2 and err2 < 1.0 and cerr2 < 0.3 \
                        and painted > 0 and gone
                    msg = (f'детект 360° {pano_ok}, без фокусного {no_focal}, секции скрыты '
                           f'{sec_hidden}, скан в списке {combo_ok}, пар {npairs}; '
                           f'у сканера: поза {ok1} (поворот {err1:.2f}°, центр {cerr1:.2f} м); '
                           f'6-DOF: поза {ok2} (поворот {err2:.2f}°, центр {cerr2:.2f} м); '
                           f'окрашено {painted}, удалено {gone} — {"ок" if ok else "ОШИБКА"}')
                    log.append(f'05f_photo_pano: {msg}')
                    print(f'[{time.time() - t0:5.1f}s] 05f_photo_pano.png  |  {msg}', flush=True)
                    if not ok:
                        finish(1)
                        return
                    QTimer.singleShot(200, step_import)

                wait(lambda: not w.busy and not getattr(w, '_photo_warm_busy', False),
                     after_paint, 120)

            wait(lambda: not w.busy, after_bind2, 120)

        wait(lambda: not w.busy, after_bind1, 120)

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
