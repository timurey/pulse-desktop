#!/usr/bin/env python3
"""
Проверка мыши в окне Qt без человека: события мыши посылаются в 3D-вид напрямую.

    python tests/qt_interact.py X.pulse

Проверяется: колесо (масштаб), левая кнопка (вращение), правая (сдвиг), двойной клик
(центр вращения), Ctrl+клик (признак в ручной стыковке), Shift+тянуть (сдвиг подвижного),
Alt+тянуть (поворот подвижного), рамка выделения в режиме чистки, Esc.
Выход 0 — всё прошло.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PySide6.QtCore import Qt, QPointF, QPoint, QEvent
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication


def mouse(view, kind, x, y, button=Qt.LeftButton, mods=Qt.NoModifier):
    t = {'press': QEvent.MouseButtonPress, 'move': QEvent.MouseMove, 'release': QEvent.MouseButtonRelease,
         'dbl': QEvent.MouseButtonDblClick}[kind]
    buttons = Qt.NoButton if kind == 'release' else button
    pos = QPointF(x, y)
    ev = QMouseEvent(t, pos, view.mapToGlobal(pos), button if kind != 'move' else Qt.NoButton, buttons, mods)
    QApplication.sendEvent(view, ev)


def drag(view, x0, y0, x1, y1, button=Qt.LeftButton, mods=Qt.NoModifier, steps=6):
    mouse(view, 'press', x0, y0, button, mods)
    for k in range(1, steps + 1):
        f = k / steps
        mouse(view, 'move', x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, button, mods)
    mouse(view, 'release', x1, y1, button, mods)


def wheel(view, x, y, dy):
    pos = QPointF(x, y)
    ev = QWheelEvent(pos, view.mapToGlobal(pos), QPoint(0, 0), QPoint(0, dy), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    QApplication.sendEvent(view, ev)


def main():
    from pulse_qt.app import make_app, session_from_args
    app, theme = make_app()
    from pulse_qt.main_window import MainWindow
    import plane_register as pr
    w = MainWindow(session_from_args([sys.argv[1]]), theme)
    w.resize(1400, 880)
    w.show()
    fails = []

    def check(name, ok, info=''):
        print(f"{'OK ' if ok else 'ОШИБКА'} {name} {info}", flush=True)
        if not ok:
            fails.append(name)

    def pump(sec=0.2):
        t = time.time()
        while time.time() - t < sec:
            app.processEvents()
            time.sleep(0.01)

    def wait(cond, timeout=120):
        t = time.time()
        while not cond() and time.time() - t < timeout:
            pump(0.1)
        return cond()

    wait(lambda: not w.busy and not w.analyzing and w.view.items)
    v = w.view
    pump(0.5)
    W, H = v.width(), v.height()
    cx, cy = W / 2, H / 2

    d0 = v.distance()
    wheel(v, cx, cy, 240)
    check('колесо приближает', v.distance() < d0 * 0.9, f'{d0:.1f} → {v.distance():.1f}')

    w.view_3d()
    pump()
    R0 = v.basis().copy()
    drag(v, cx, cy, cx + 120, cy)
    R1 = v.basis()
    yaw = np.degrees(np.arctan2(R1[1, 2], R1[0, 2]) - np.arctan2(R0[1, 2], R0[0, 2]))
    check('левая кнопка вращает вокруг вертикали', abs(yaw) > 10 and abs(R1[2, 0]) < 1e-6,
          f'поворот {yaw:.1f}°, крен оси x {R1[2, 0]:.2e}')

    c0 = v.center().copy()
    drag(v, cx, cy, cx + 80, cy + 40, Qt.RightButton)
    check('правая кнопка сдвигает', np.linalg.norm(v.center() - c0) > 0.1)

    w.view_top()
    pump()
    sess = w.s
    fixed = sess.ref
    # двойной клик по точке опорного скана — центр вращения
    P = pr.transform(fixed._display[::200], sess.Tc(fixed))
    s, depth, front = v.project(P)
    inside = front & (s[:, 0] > 50) & (s[:, 0] < W - 50) & (s[:, 1] > 50) & (s[:, 1] < H - 50)
    k = np.flatnonzero(inside)[len(np.flatnonzero(inside)) // 2]
    mouse(v, 'dbl', s[k, 0], s[k, 1])
    check('двойной клик — центр вращения на облаке', np.linalg.norm(v.center()[:2] - P[k, :2]) < 1.0,
          f'{v.center()[:2]} vs {P[k, :2]}')

    # ручная стыковка: Ctrl+клик по полу опорного скана
    moving = next(x for x in sess.scans if x.id != sess.frame and x.pose is not None)
    w.start_manual(fixed.id, moving.id)
    pump(0.5)
    w.view_top()
    pump()
    floor = max((p for p in fixed.planes if p.kind == 'floor'), key=lambda p: p.area)
    Fp = pr.transform(fixed.res['down'][floor.inliers][::50], sess.Tc(fixed))
    s, depth, front = v.project(Fp)
    ok = front & (s[:, 0] > 0) & (s[:, 0] < W) & (s[:, 1] > 0) & (s[:, 1] < H)
    # точка пола, которую сверху не загораживает подвижный скан
    k = np.flatnonzero(ok)[0]
    w.manual.kind.group.button(1).click()                # «плоскость»
    mouse(v, 'press', s[k, 0], s[k, 1], Qt.LeftButton, Qt.ControlModifier)
    mouse(v, 'release', s[k, 0], s[k, 1], Qt.LeftButton, Qt.ControlModifier)
    check('Ctrl+клик выбирает признак', w.pending is not None,
          f"{w.pending and w.pending['type']} · {w.st_text.text()}")

    T0 = w.T_moving.copy()
    drag(v, cx, cy, cx + 100, cy, Qt.LeftButton, Qt.ShiftModifier)
    dt = w.T_moving[:3, 3] - T0[:3, 3]
    check('Shift+тянуть сдвигает подвижный', np.linalg.norm(dt[:2]) > 0.2 and abs(dt[2]) < 1e-9,
          f'сдвиг {dt.round(2)}')
    T1 = w.T_moving.copy()
    drag(v, cx, cy, cx + 100, cy, Qt.LeftButton, Qt.AltModifier)
    ang = np.degrees(np.arctan2(w.T_moving[1, 0], w.T_moving[0, 0]) - np.arctan2(T1[1, 0], T1[0, 0]))
    check('Alt+тянуть поворачивает подвижный', abs(ang) > 5, f'{ang:.1f}°')
    check('камера не двигалась при перетаскивании скана', np.allclose(v.basis()[:, 2], [0, 0, 1], atol=1e-3))
    w.on_manual_action('nudge', {'dz': 1})
    check('кнопка Z+ поднимает', abs(w.T_moving[2, 3] - T1[2, 3] - 0.05) < 1e-6)
    wait(lambda: not w._score_pending, 30)
    w.on_manual_cancel()
    pump()
    check('отмена ручной — поза не изменилась', np.allclose(sess.Tc(moving), T0),
          'и видимость восстановлена' if all(x.visible for x in sess.scans) else 'видимость НЕ восстановлена')

    # чистка: рамка вокруг центра опорного скана
    w.clean_scan = fixed
    w.set_mode('clean')
    w.toggle_select()
    w.view_top()
    pump()
    c = sess.Tc(fixed)[:3, 3]
    s, _, _ = v.project(c[None])
    x, y = s[0]
    drag(v, x - 30, y - 30, x + 30, y + 30)
    n = sum(int(m.sum()) for m in w.selection.values())
    check('рамка выделяет точки', n > 0, f'{n} точек')
    w.on_escape()
    check('Esc снимает выделение', not w.selection)
    w.on_escape()
    check('второй Esc выходит из выделения', not w.select_mode)

    w.toggle_fly()
    pump(0.3)
    check('полёт включается', v.fly and abs(v.basis()[2, 2]) < 0.5)
    w.on_escape()
    check('Esc выходит из полёта', not v.fly)

    print('ИТОГ:', 'всё прошло' if not fails else f'ошибок {len(fails)}: {fails}', flush=True)
    w._closing_ok = True
    w.close()
    os._exit(1 if fails else 0)


if __name__ == '__main__':
    main()
