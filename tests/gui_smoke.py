#!/usr/bin/env python3
"""
Сквозная проверка окна scan_gui без участия человека: открывает проект,
вызывает обработчики кнопок, «кликает» по признакам (pick_world) и сохраняет
рендер сцены после каждого шага.

    .venv/bin/python tests/gui_smoke.py PROJECT.json OUT_DIR [--candidates SCAN_ID]
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import open3d as o3d                      # noqa: E402
import scan_gui                           # noqa: E402
from scan_session import Session          # noqa: E402
import plane_register as pr               # noqa: E402

gui = o3d.visualization.gui


def main():
    project, out = sys.argv[1], Path(sys.argv[2])
    cand = sys.argv[sys.argv.index('--candidates') + 1] if '--candidates' in sys.argv else None
    out.mkdir(parents=True, exist_ok=True)
    app = gui.Application.instance
    app.initialize()
    scan_gui.setup_fonts(app)
    a = scan_gui.App(Session.from_project(project))

    def tick(sec=0.3):
        t = time.time()
        while time.time() - t < sec:
            app.run_one_tick()

    def wait(label, timeout=1800):
        t = time.time()
        while (a.busy or a.analyzing) and time.time() - t < timeout:
            a.w.post_redraw()                # иначе цикл событий засыпает в фоне
            app.run_one_tick()
            time.sleep(0.02)
        tick(0.5)
        print(f"[{label}] статус: {a.status.text}", flush=True)

    def shot(name):
        tick(0.3)
        img = app.render_to_image(a.sw.scene, a.sw.frame.width, a.sw.frame.height)
        o3d.io.write_image(str(out / f'{name}.png'), img)
        print(f"   снимок {name}.png", flush=True)

    wait('загрузка')
    shot('01_loaded_top')
    a.view_3d()
    shot('02_loaded_3d')
    a.view_top()

    # пары: выбрать первую, отклонить, вернуть
    print('пар в списке:', len(a.s.edges))
    a.pair_list.selected_index = 0
    a.on_pair_selected('', False)
    shot('03_pair0_only')
    a.on_pair_user('reject')
    print('после отклонения:', a.s.edge_active(a.s.edges[0]))
    a.on_pair_user(None)
    print('после «Авто»:', a.s.edge_active(a.s.edges[0]))
    a.show_all()

    # ручная стыковка: 231829 к 231157 — «клики» по полу и двум стенам
    ids = [s.id for s in a.s.scans]
    fixed = next(i for i in ids if '231157' in i)
    moving = next(i for i in ids if '231829' in i)
    T_true = a.s.Tc(a.s.by_id(moving))
    a.on_manual_start(fixed, moving, T_init=Session.nudge(T_true, dx=0.4, dy=-0.3, dyaw_deg=4))
    shot('04_manual_start_offset')
    F, M = a.s.by_id(fixed), a.s.by_id(moving)
    walls = [p for p in F.planes if p.kind == 'wall']
    w2 = next(p for p in walls[1:] if abs(np.dot(p.normal, walls[0].normal)) < 0.3)
    picks = [p for p in F.planes if p.kind == 'floor'][:1] + [walls[0], w2]
    n_pairs = 0
    for pl in picks:
        if n_pairs >= 3:
            break
        Wf = pr.transform(np.asarray(F.res['down'][pl.inliers][len(pl.inliers) // 2])[None],
                          a.s.Tc(F))[0]
        fa = a.pick_world(Wf)
        # такой же признак в подвижном: плоскость того же вида, ближайшая в текущей позе
        same = [q for q in M.planes if q.kind == pl.kind]
        qb = min(same, key=lambda q: np.linalg.norm(
            pr.transform(np.asarray(q.centroid)[None], a.T_moving)[0] - Wf))
        Wm = pr.transform(np.asarray(M.res['down'][qb.inliers][len(qb.inliers) // 2])[None],
                          a.T_moving)[0]
        fb = a.pick_world(Wm)
        wait('авто-решение')
        if fa and fb and len(a.pairs) > n_pairs:
            n_pairs = len(a.pairs)
        print('   пара:', scan_gui._fdesc(fa), '↔', scan_gui._fdesc(fb), '|', a.dof_label.text)
    shot('05_manual_picked')
    a.on_manual_solve()
    wait('решить')
    print('   скриншот:', a.save_screenshot(out))
    print(open(a.save_screenshot(out) + '_ui.txt').read())
    a.on_manual_icp()
    wait('ICP')
    dt, da = pr.pose_delta(a.T_moving, T_true)
    print(f'   отличие от автоматической позы: {dt * 100:.1f} см, {da:.2f}°')
    shot('06_manual_solved')
    a._nudge(dx=1)
    a._nudge(dyaw=1)
    a.on_manual_score()
    wait('оценка')
    print('   ', a.manual_score.text)
    a.on_manual_cancel()

    # чистка: показать отражения первого скана
    a.clean_combo.selected_text = fixed
    a.refresh_clean()
    print('чистка:', a.clean_report.text.replace('\n', ' | '))
    a.on_show_ghosts()
    a.view_top()
    shot('07_ghosts')
    a.on_show_ghosts()

    if cand:
        cid = next(i for i in ids if cand in i)
        a.cand_combo.selected_text = cid
        a.on_cand_search()
        wait('кандидаты')
        print('кандидаты:', a.cand_list.selected_value, len(a.candidates))
        for k in range(min(3, len(a.candidates))):
            a.cand_list.selected_index = k
            a.on_cand_selected('', False)
            a.view_top()
            shot(f'08_cand{k}')

    # ручная чистка: события мыши через настоящий обработчик, включая вырожденную рамку
    ME = gui.MouseEvent.Type

    class Ev:
        def __init__(self, t, x, y):
            self.type, self.x, self.y = t, x, y

        def is_button_down(self, b):
            return b == gui.MouseButton.LEFT

        def is_modifier_down(self, m):
            return False
    handler = a._safe(a._on_mouse, None)
    f = a.sw.frame
    a.clean_combo.selected_text = fixed
    a.view_top()
    a.toggle_select()
    tick(0.3)
    # прямоугольник вокруг точек выбранного скана на экране
    import manual_clean
    cam = a.sw.scene.camera
    Fs = a.s.by_id(fixed)
    scr, _ = manual_clean.project(pr.transform(Fs._display, a.s.Tc(Fs)),
                                  np.asarray(cam.get_view_matrix()),
                                  np.asarray(cam.get_projection_matrix()), f.width, f.height)
    cx, cy = np.median(scr, axis=0)
    x0, y0 = f.x + cx - 60, f.y + cy - 40
    for e in (Ev(ME.BUTTON_DOWN, x0, y0), Ev(ME.DRAG, x0, y0), Ev(ME.DRAG, x0 + 1, y0),
              Ev(ME.DRAG, x0 + 120, y0 + 80), Ev(ME.BUTTON_UP, x0 + 120, y0 + 80)):
        handler(e)
        tick(0.05)
    print('выделение прямоугольником:', a.sel_label.text)
    assert a.selection, 'выделение не сработало'
    a.on_erase()
    a.on_undo_erase()
    a.toggle_select()

    # экспорт
    a.s.export(out / 'smoke_export.ply', 0.08)
    print('экспорт:', (out / 'smoke_export.ply').stat().st_size, 'байт')
    a._closed = True                 # как при закрытии окна пользователем
    a.w.close()
    tick(0.2)
    print('OK', flush=True)
    import os
    os._exit(0)                      # как scan_gui.main: без финализации (фоновые потоки)


if __name__ == '__main__':
    main()
