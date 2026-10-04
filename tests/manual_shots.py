#!/usr/bin/env python3
"""
Снимки окна для руководства (docs/manual/img/*.png) на реальном проекте.

    python tests/manual_shots.py X.pulse [--out docs/manual/img] [--dark]

Сканер подменяется заглушкой (список записей из кода). Тема пользователя не меняется.
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PySide6.QtCore import QSettings, QRect, QPoint

FAKE_BAGS = [
    {'name': 'static_20261004_201358', 'mtime': '2026-10-04 20:13:58', 'size': '113 MB', 'size_b': 113_000_000, 'recording': False},
    {'name': 'static_20261004_200910', 'mtime': '2026-10-04 20:09:10', 'size': '97 MB', 'size_b': 97_000_000, 'recording': False},
    {'name': 'static_20261004_200831', 'mtime': '2026-10-04 20:08:31', 'size': '100 MB', 'size_b': 100_000_000, 'recording': False},
    {'name': 'static_20261004_200533_863', 'mtime': '2026-10-04 20:05:33', 'size': '35 MB', 'size_b': 35_000_000, 'recording': False},
    {'name': 'static_20261004_202500', 'mtime': '2026-10-04 20:25:00', 'size': '12 MB', 'size_b': 12_000_000, 'recording': True},
    {'name': 'static_20261002_185054_665', 'mtime': '2026-10-02 18:50:54', 'size': '88 MB', 'size_b': 88_000_000, 'recording': False},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project')
    ap.add_argument('--out', default=str(Path(__file__).resolve().parent.parent / 'docs' / 'manual' / 'img'))
    ap.add_argument('--dark', action='store_true')
    ap.add_argument('--bags', default='/Users/timurey/Pulse/bags')
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    prefs = QSettings('Pulse', 'PulseScan')
    keep = {k: prefs.value(k) for k in ('dark', 'parallel', 'point_px', 'ribbon_collapsed', 'geometry')}
    prefs.setValue('dark', a.dark)
    prefs.setValue('parallel', False)
    prefs.setValue('ribbon_collapsed', False)
    import scanner_client
    scanner_client.ScannerClient.bags = lambda self: [dict(b) for b in FAKE_BAGS]
    import plane_register as pr
    from pulse_qt.app import make_app, session_from_args
    app, theme = make_app()
    from pulse_qt.main_window import MainWindow
    from pulse_qt import dialogs
    w = MainWindow(session_from_args([a.project]), theme)
    w.resize(1440, 900)
    w.move(40, 40)
    w.show()
    s, v, tp = w.s, w.view, w.tree_panel

    def pump(sec=0.3):
        t = time.time()
        while time.time() - t < sec:
            app.processEvents()
            time.sleep(0.02)

    def wait(cond, timeout=300):
        t = time.time()
        while not cond() and time.time() - t < timeout:
            pump(0.1)

    def shot(name, widget=None, rect=None):
        pump(0.25)
        v.render_now()
        pump(0.15)
        img = (widget or w).grab()
        if rect is not None:
            img = img.copy(rect)
        img.save(str(out / f'{name}.png'))
        print('  ', name, flush=True)

    def tab(key):
        w.ribbon.group.button(w.ribbon.keys.index(key)).click()
        pump(0.2)

    def ribbon_crop(name):
        r = w.ribbon.geometry()
        shot(name, rect=QRect(0, 0, w.width(), r.height() + 2))

    wait(lambda: not w.busy and not w.analyzing and v.items)
    pump(0.5)
    # 1. обзор
    tab('project')
    shot('01_overview')
    for k in ('project', 'reg', 'control', 'clean', 'result'):
        tab(k)
        ribbon_crop(f'ribbon_{k}')
    tab('project')
    # куб навигации
    c = w.vp.cube
    r = c.geometry()
    p0 = w.vp.mapTo(w, r.topLeft())
    shot('cube', rect=QRect(p0.x() - 6, p0.y() - 6, r.width() + 12, r.height() + 12))
    # 2. импорт со сканера
    import bag_reconstruct as br
    d = dialogs.ScannerImportDialog(w, 'pulse.local', str(Path.home() / 'Pulse' / 'bags'), br.DEFAULTS)
    d.show()
    wait(lambda: bool(d.bags), 20)
    d.mark_all()
    shot('02_import_scanner', widget=d)
    d.reject()
    # 3. импорт bag
    bags = br.find_bags(a.bags)[-4:] if Path(a.bags).exists() else []
    if bags:
        d = dialogs.BagImportDialog(w, bags, 'Объект', str(Path.home() / 'Pulse' / 'scans'), br.DEFAULTS)
        d.show()
        shot('03_import_bag', widget=d)
        d.reject()
    # 4. скан в инспекторе
    sc = next(x for x in s.scans if x.id != s.frame and x.pose is not None)
    tp.select(sc.id)
    pump(0.3)
    shot('04_scan_inspector')
    # 5. стыковка: пары, циклы
    tab('reg')
    w.dock.tabs.setCurrentIndex(0)
    shot('05_pairs')
    w.dock.tabs.setCurrentIndex(1)
    shot('05b_loops')
    w.dock.tabs.setCurrentIndex(0)
    # 6. ручная стыковка: пара «пол — пол»
    unpl = [x for x in s.scans if x.pose is None]
    moving = unpl[0] if unpl else s.scans[-1]
    fixed = s.by_id(s.frame)
    w.start_manual(fixed.id, moving.id)
    pump(0.5)
    fl_f = max((p for p in fixed.planes if p.kind == 'floor'), key=lambda p: p.area, default=None)
    fl_m = max((p for p in moving.planes if p.kind == 'floor'), key=lambda p: p.area, default=None)
    if fl_f is not None and fl_m is not None:
        w.pick_world(pr.transform(np.asarray(fl_f.centroid)[None], s.Tc(fixed))[0], fixed)
        w.pick_world(pr.transform(np.asarray(fl_m.centroid)[None], w.T_moving)[0], moving)
    wait(lambda: not w.busy and not w._score_pending, 60)
    pump(2.5)
    shot('06_manual')
    w.on_manual_cancel(ask=False)
    # 7. опорная точка и транспортиры — пара фасадов
    A = s.by_id('static_20260922_234415.e57') or fixed
    B = s.by_id('static_20260922_234551.e57') or moving
    w.start_manual(A.id, B.id)
    pump(0.5)
    pa = pr.transform(A.down, s.Tc(A))[len(A.down) // 3]
    w.on_manual_action('pivot_pick', None)
    w._pivot_picked(pa, A)
    w._pivot_picked(pa, B)
    w.view.fit(w.pivot['c'] - 4, w.pivot['c'] + 4, (-0.5, -0.7, 0.6), (0, 0, 1))
    pump(2.5)
    shot('07_pivot')
    w.on_manual_cancel(ask=False)
    # 8. качество совмещения
    tab('control')
    tp.switches['quality'].setChecked(True, emit=True)
    wait(lambda: w.qual['res'] is not None and not w.qual['busy'], 60)
    w.dock.tabs.setCurrentIndex(4)
    w.view_top()
    shot('08_quality')
    tp.switches['quality'].setChecked(False, emit=True)
    # 9. сечения
    fac = [g for g in s.tree.root.children if getattr(g, 'name', '') == 'фасад']
    if fac:
        s.set_visible(fac[0].id, False)
        w.apply_visibility()
    w.section_toggle('slice')
    w.on_control_action('sec_pos', 1.2)
    w.set_section(thick=0.1)
    w.set_point_size(3)
    w.view_top()
    shot('09_section_slice')
    w.section_toggle('clip')
    w.on_control_action('sec_pos', 2.0)
    w.set_point_size(2)
    w.view_3d()
    shot('10_section_clip')
    w.section_toggle('clip')
    # 10. замеры
    ref = s.ref
    w.only_show({ref.id})
    w.focus_node(ref.id)
    w.view.set_view_dir((-0.5, -0.7, 0.6))
    pump(0.3)
    P = pr.transform(ref._display[::60], s.Tc(ref))
    sxy, _, fr = v.project(P)
    ok = np.flatnonzero(fr & (sxy[:, 0] > 80) & (sxy[:, 0] < v.width() - 80) & (sxy[:, 1] > 80) & (sxy[:, 1] < v.height() - 80))
    w.set_measure_mode('dist')
    w.measure_point(P[ok[10]], f'scan:{ref.id}')
    w.measure_point(P[ok[-10]], f'scan:{ref.id}')
    w.set_measure_mode('height')
    w.measure_point(P[ok[len(ok) // 2]], f'scan:{ref.id}')
    wall = max((p for p in ref.planes if p.kind == 'wall'), key=lambda p: p.area)
    W = pr.transform(ref.res['down'][wall.inliers][:1], s.Tc(ref))[0]
    w.set_measure_mode('plane')
    w.measure_point(W, f'scan:{ref.id}')
    w.measure_point(P[ok[len(ok) // 3]], f'scan:{ref.id}')
    w.set_measure_mode(None)
    shot('11_measures')
    w.on_control_action('measure_clear', None)
    w.restore_visibility()
    if fac:
        s.set_visible(fac[0].id, True)
        w.apply_visibility()
    # 11. чистка
    tab('clean')
    w.clean.sw_dyn_all.setChecked(True)
    w.on_dyn_find()
    wait(lambda: not w.busy, 120)
    w.view_top()
    shot('12_clean')
    w.dyn_masks = {}
    w.view.remove_prefix('dyn:')
    w.clean.sw_dyn_all.setChecked(False)
    # 12. поверхность
    tab('result')
    tp.select(s.frame)
    w.on_surface_action('build', None)
    wait(lambda: not w.busy and s.meshes, 300)
    w.focus_node(s.frame)
    w.view.set_view_dir((-0.5, -0.7, 0.6))
    pump(0.5)
    shot('13_surface')
    w.set_points_visible(True)
    s.meshes.clear()
    w._draw_meshes()
    tp.rebuild()
    # 13. экспорт и передача
    d = dialogs.ExportDialog(w, [('все размещённые сканы', None)], str(Path.home() / 'Pulse' / 'объект_merged.e57'))
    d.show()
    shot('14_export', widget=d)
    d.reject()
    d = dialogs.PackDialog(w, 'объект', Path.home() / 'Desktop', len(s.scans), 237, [])
    d.show()
    shot('15_pack', widget=d)
    d.reject()
    for k, val in keep.items():
        if val is None:
            prefs.remove(k)
        else:
            prefs.setValue(k, val)
    prefs.sync()
    w._closing_ok = True
    w.close()
    os._exit(0)


if __name__ == '__main__':
    main()
