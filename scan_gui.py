#!/usr/bin/env python3
"""
Интерактивный инструмент стыковки статических сканов (Open3D GUI).

Вкладки:
  Проект    - открыть/сохранить проект, добавить сканы, видимость, опорный скан, вид
  Пары      - автостыковка всех пар, граф рёбер: принять / отклонить / как решит автоматика
  Ручная    - стыковка пары: Ctrl(Cmd)+клик по облакам выбирает признаки (плоскость,
              проём, точка) поочерёдно в неподвижном и подвижном скане; «Решить»,
              ICP, подвижка кнопками, «Принять»
  Кандидаты - автоматические гипотезы позы для трудного скана (фасад): просмотр,
              выбор, доработка вручную
  Чистка    - зеркальные отражения (показать/убирать), экспорт склейки

Сцена рисуется в общей системе (каноническая система опорного скана, Z вверх).

Usage:
    python scan_gui.py [project.json | scan1.e57 scan2.e57 ...] [--web]

    --web - показывать окно в браузере (http://localhost:8888) вместо нативного
            окна; нужно, если нативное окно Open3D чёрное (macOS 15 + Metal)
"""

import os
import sys
import time
import shutil
import platform
import threading
import subprocess
import traceback
from pathlib import Path

import numpy as np
import open3d as o3d

from scan_session import Session, Scan, PALETTE
import plane_register as pr

gui = o3d.visualization.gui
rendering = o3d.visualization.rendering

SYSTEM = platform.system()          # Darwin | Windows | Linux

# Шрифт с кириллицей. PULSE_GUI_FONT — путь к своему .ttf, если найденный не подходит.
FONT_CANDIDATES = {
    'Darwin': ['/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
               '/Library/Fonts/Arial Unicode.ttf',
               '/System/Library/Fonts/Supplemental/Arial.ttf'],
    'Windows': [os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', n)
                for n in ('segoeui.ttf', 'arial.ttf', 'tahoma.ttf')],
    'Linux': ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
              '/usr/share/fonts/dejavu/DejaVuSans.ttf',
              '/usr/share/fonts/TTF/DejaVuSans.ttf',
              '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',
              '/usr/share/fonts/noto/NotoSans-Regular.ttf',
              '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf'],
}


def find_font():
    env = os.environ.get('PULSE_GUI_FONT')
    if env and Path(env).exists():
        return env
    for p in FONT_CANDIDATES.get(SYSTEM, []):
        if Path(p).exists():
            return p
    if shutil.which('fc-match'):                         # Linux: спросить fontconfig
        try:
            p = subprocess.run(['fc-match', '-f', '%{file}', 'sans:lang=ru'],
                               capture_output=True, text=True, timeout=5).stdout.strip()
            if p and Path(p).exists():
                return p
        except Exception:                                # noqa: BLE001
            pass
    return None


FONT = find_font()
PANEL_W = 26          # ширина панели, em
PICK_COLORS = [(0.9, 0.1, 0.1), (0.1, 0.6, 0.1), (0.1, 0.3, 0.9), (0.9, 0.6, 0.0),
               (0.6, 0.1, 0.8), (0.0, 0.7, 0.7), (0.5, 0.5, 0.0), (0.9, 0.3, 0.6)]


def screen_size():
    """Размер основного экрана в логических точках; при неудаче 1280×800."""
    try:
        if SYSTEM == 'Darwin':
            out = subprocess.run(['osascript', '-e',
                                  'tell application "Finder" to get bounds of window of desktop'],
                                 capture_output=True, text=True, timeout=5).stdout
            x0, y0, x1, y1 = (int(v) for v in out.strip().split(','))
            return x1 - x0, y1 - y0
        if SYSTEM == 'Windows':
            import ctypes
            u = ctypes.windll.user32
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(0)   # логические пиксели
            except Exception:                            # noqa: BLE001
                pass
            return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        if shutil.which('xrandr'):
            out = subprocess.run(['xrandr', '--current'], capture_output=True, text=True,
                                 timeout=5).stdout
            for line in out.splitlines():
                if '*' in line:
                    w, h = line.split()[0].split('x')
                    return int(w), int(h)
    except Exception:                                    # noqa: BLE001
        pass
    return 1280, 800


def capture_window(rect, path):
    """
    Снимок области экрана (окна) средствами ОС. Возвращает True при успехе.
    macOS — screencapture (нужно разрешение «Запись экрана» у терминала),
    Linux — ImageMagick import, Windows — Pillow ImageGrab (если установлен).
    """
    x, y, w, h = rect
    try:
        if SYSTEM == 'Darwin':
            subprocess.run(['screencapture', '-x', '-R', f'{x},{y},{w},{h}', str(path)],
                           timeout=10, check=True)
            return True
        if SYSTEM == 'Linux' and shutil.which('import'):
            subprocess.run(['import', '-window', 'root', '-crop', f'{w}x{h}+{x}+{y}', str(path)],
                           timeout=10, check=True)
            return True
        from PIL import ImageGrab                        # Windows (и др.), если есть Pillow
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
        return True
    except Exception as e:                               # noqa: BLE001
        print('снимок окна недоступен:', e)
        return False


def setup_fonts(app):
    if FONT is None:
        return
    # Open3D: язык, отличный от известных (ja/ko/th/vi/zh), загружает диапазон
    # «латиница + кириллица». add_typeface_for_code_points в 0.19 не работает
    # (таблица символов уничтожается раньше построения атласа) — не использовать.
    # В интерфейсе — только латиница/кириллица/Latin-1: стрелки, ✓ и т.п. не отрисуются.
    font = gui.FontDescription(FONT)
    font.add_typeface_for_language(FONT, 'ru')
    app.set_font(gui.Application.DEFAULT_FONT_ID, font)


class App:
    def __init__(self, session=None):
        self.app = gui.Application.instance
        self.s = session or Session()
        self.busy = False
        # ручной режим
        self.fixed = None
        self.moving = None
        self.T_moving = None
        self.pairs = []
        self.pending = None
        self.ghost_shown = set()
        self.candidates = []
        self.cand_scan = None
        self._moving_markers = set()
        self._drag = None             # перетаскивание подвижного скана мышью
        self._score_pending = False
        # режим полёта (как в 3D-шутере)
        self.fly = False
        self.fly_speed = 1.5          # м/с
        self.fly_keys = set()
        self.fly_pos = np.zeros(3)
        self.fly_yaw = 0.0            # рад, от +X против часовой
        self.fly_pitch = 0.0
        self._fly_last = None
        self._look = None
        self._build()
        if self.s.scans:
            self.load_all()

    # ── построение окна ──────────────────────────────────────────────────
    def _build(self):
        W, H = screen_size()
        # окно не больше экрана: если macOS ужимает окно, буфер рендера Metal остаётся
        # прежнего размера - чёрный экран с полосами
        self.w = self.app.create_window("Стыковка сканов - Pulse", int(W * 0.92), int(H * 0.88))
        w = self.w
        em = w.theme.font_size
        self.em = em
        self.sw = gui.SceneWidget()
        self.sw.scene = rendering.Open3DScene(w.renderer)
        self.sw.scene.set_background([1, 1, 1, 1])
        # цвета без «выцветания»: линейная цветокоррекция вместо отключения постобработки
        # (отключение постобработки на Metal даёт чёрное окно)
        self.sw.scene.view.set_color_grading(rendering.ColorGrading(
            rendering.ColorGrading.Quality.HIGH, rendering.ColorGrading.ToneMapping.LINEAR))
        self.sw.scene.show_axes(False)
        self.sw.set_on_mouse(self._on_mouse)
        self.mat = rendering.MaterialRecord()
        self.mat.shader = 'defaultUnlit'
        self.mat.point_size = 2.0
        self.mat_big = rendering.MaterialRecord()
        self.mat_big.shader = 'defaultUnlit'
        self.mat_big.point_size = 5.0
        self.mat_mesh = rendering.MaterialRecord()
        self.mat_mesh.shader = 'defaultUnlit'

        self.panel = gui.Vert(0.4 * em, gui.Margins(0.5 * em, 0.5 * em, 0.5 * em, 0.5 * em))
        self.tabs = gui.TabControl()
        self.tabs.add_tab('Проект', self._tab_project())
        self.tabs.add_tab('Пары', self._tab_pairs())
        self.tabs.add_tab('Ручная', self._tab_manual())
        self.tabs.add_tab('Кандидаты', self._tab_candidates())
        self.tabs.add_tab('Чистка', self._tab_clean())
        self.panel.add_child(self.tabs)
        self.panel.add_stretch()
        self.status = gui.Label('Откройте проект или добавьте сканы')
        self.progress = gui.ProgressBar()
        self.progress.value = 0.0
        self.panel.add_child(self.status)
        self.panel.add_child(self.progress)
        w.add_child(self.sw)
        w.add_child(self.panel)
        w.set_on_layout(self._on_layout)
        w.set_on_key(self._on_key)
        w.set_on_tick_event(self._on_tick)

    def _on_layout(self, ctx):
        r = self.w.content_rect
        pw = PANEL_W * self.em
        self.panel.frame = gui.Rect(r.x, r.y, pw, r.height)
        self.sw.frame = gui.Rect(r.x + pw, r.y, r.width - pw, r.height)

    def _btn(self, text, cb):
        b = gui.Button(text)
        b.horizontal_padding_em = 0.4
        b.vertical_padding_em = 0.15
        b.set_on_clicked(cb)
        return b

    def _row(self, *widgets):
        h = gui.Horiz(0.3 * self.em)
        for x in widgets:
            h.add_child(x)
        return h

    # ── вкладка «Проект» ─────────────────────────────────────────────────
    def _tab_project(self):
        v = gui.Vert(0.3 * self.em)
        v.add_child(self._row(self._btn('Открыть проект...', self.on_open),
                              self._btn('Добавить скан...', self.on_add_scan)))
        v.add_child(self._row(self._btn('Сохранить', self.on_save),
                              self._btn('Сохранить как...', self.on_save_as)))
        v.add_child(gui.Label('Сканы (галочка - видимость):'))
        self.scan_list = gui.WidgetProxy()          # содержимое пересоздаётся целиком
        v.add_child(self.scan_list)
        self.ref_combo = gui.Combobox()
        self.ref_combo.set_on_selection_changed(self.on_ref_changed)
        v.add_child(self._row(gui.Label('Опорный:'), self.ref_combo))
        v.add_child(self._row(self._btn('Вид сверху', self.view_top),
                              self._btn('Вид 3D', self.view_3d),
                              self._btn('Все видимы', self.show_all)))
        v.add_child(self._btn('Скриншот (F12)', self.save_screenshot))
        v.add_child(self._row(self._btn('Выровнять проект по горизонту', self.on_level_project),
                              self._btn('Сбросить', self.on_level_reset)))
        self.fly_btn = self._btn('Режим полёта (F)', self.toggle_fly)
        self.fly_combo = gui.Combobox()
        v.add_child(self._row(self.fly_btn, self._btn('Встать в точку скана', self.fly_to_scan)))
        v.add_child(self._row(gui.Label('Скан:'), self.fly_combo))
        v.add_child(gui.Label('Полёт: W/S A/D - ходьба, Space/E - вверх, C/Q - вниз,\n'
                              'левая кнопка + мышь - обзор, колесо - скорость,\n'
                              'Shift - быстрее, Esc - выход'))
        ps = gui.Slider(gui.Slider.INT)
        ps.set_limits(1, 6)
        ps.int_value = 2
        ps.set_on_value_changed(self.on_point_size)
        v.add_child(self._row(gui.Label('Размер точек'), ps))
        return v

    def refresh_scan_list(self):
        new = gui.Vert(0.1 * self.em)
        for s in self.s.scans:
            cb = gui.Checkbox(f"{s.id}  {'опорный' if s.id == self.s.frame else ('размещён' if s.pose is not None else 'НЕ размещён')}")
            cb.checked = s.visible
            cb.set_on_checked(lambda c, s=s: self.on_visible(s, c))
            lab = gui.Label('##')
            lab.text_color = gui.Color(*s.color)
            new.add_child(self._row(lab, cb))
        self.scan_list.set_widget(new)
        self.ref_combo.clear_items()
        for s in self.s.scans:
            self.ref_combo.add_item(s.id)
        if self.s.frame:
            self.ref_combo.selected_text = self.s.frame
        self.w.set_needs_layout()

    # ── вкладка «Пары» ───────────────────────────────────────────────────
    def _tab_pairs(self):
        v = gui.Vert(0.3 * self.em)
        self.reuse_cb = gui.Checkbox('Использовать уже посчитанные пары')
        self.reuse_cb.checked = True
        v.add_child(self._btn('Автостыковка всех пар', self.on_auto))
        v.add_child(self.reuse_cb)
        v.add_child(gui.Label('+ активно  x отклонено  · неактивно\nоценка / нарушения / отрыв'))
        self.pair_list = gui.ListView()
        self.pair_list.set_max_visible_items(16)
        self.pair_list.set_on_selection_changed(self.on_pair_selected)
        v.add_child(self.pair_list)
        v.add_child(self._row(self._btn('Принять', lambda: self.on_pair_user('accept')),
                              self._btn('Отклонить', lambda: self.on_pair_user('reject')),
                              self._btn('Авто', lambda: self.on_pair_user(None))))
        self.loops_label = gui.Label('')
        v.add_child(self.loops_label)
        return v

    def refresh_pairs(self):
        items = []
        for e in self.s.edges:
            mark = '+' if self.s.edge_active(e) else ('x' if e.get('user') == 'reject' else '·')
            user = {'accept': ' [принято]', 'reject': ' [отклонено]'}.get(e.get('user'), '')
            if e.get('method') == 'manual':
                items.append(f"{mark} {_short(e['A'])}-{_short(e['B'])}  вручную ({e.get('source', '')}){user}")
            else:
                items.append(f"{mark} {_short(e['A'])}-{_short(e['B'])}  {e['score']:+.2f} / "
                             f"{e['violations']:.3f} / {e['margin']:.2f}{user}")
        self.pair_list.set_items(items)
        loops = self.s.loops()
        bad = [l for l in loops if not l['ok']]
        self.loops_label.text = (f"Циклов: {len(loops)}, несогласованных: {len(bad)}" +
                                 ''.join(f"\n x {'->'.join(_short(x) for x in l['loop'])}: "
                                         f"{l['dt'] * 100:.1f} см {l['deg']:.2f}°" for l in bad[:5]))

    # ── вкладка «Ручная» ─────────────────────────────────────────────────
    def _tab_manual(self):
        v = gui.Vert(0.3 * self.em)
        self.fixed_combo = gui.Combobox()
        self.moving_combo = gui.Combobox()
        v.add_child(self._row(gui.Label('Неподвижный'), self.fixed_combo))
        v.add_child(self._row(gui.Label('Подвижный  '), self.moving_combo))
        v.add_child(self._row(self._btn('Начать', self.on_manual_start),
                              self._btn('Сбросить пары', self.on_manual_clear)))
        self.pick_kind = gui.Combobox()
        for k in ('авто', 'плоскость', 'проём', 'точка'):
            self.pick_kind.add_item(k)
        v.add_child(self._row(gui.Label('Признак:'), self.pick_kind))
        v.add_child(gui.Label('Ctrl/Cmd + клик: сначала неподвижный, затем подвижный'))
        self.manual_pairs = gui.ListView()
        self.manual_pairs.set_max_visible_items(6)
        v.add_child(self.manual_pairs)
        self.auto_solve = gui.Checkbox('Решать сразу при изменении пар')
        self.auto_solve.checked = True
        v.add_child(self.auto_solve)
        v.add_child(gui.Label('Shift+тянуть - сдвиг; Alt(Option)+тянуть или Shift+правая - поворот'))
        self.dof_label = gui.Label('')
        v.add_child(self.dof_label)
        v.add_child(self._row(self._btn('Удалить выбранную', self.on_manual_delete_selected),
                              self._btn('Удалить последнюю', self.on_manual_undo)))
        v.add_child(self._row(
                              self._btn('Решить', self.on_manual_solve),
                              self._btn('ICP', self.on_manual_icp)))
        self.step = gui.NumberEdit(gui.NumberEdit.DOUBLE)
        self.step.double_value = 0.05
        self.step_deg = gui.NumberEdit(gui.NumberEdit.DOUBLE)
        self.step_deg.double_value = 0.5
        v.add_child(self._row(gui.Label('Шаг, м'), self.step, gui.Label('°'), self.step_deg))
        n = self._nudge
        v.add_child(self._row(self._btn('X-', lambda: n(dx=-1)), self._btn('X+', lambda: n(dx=1)),
                              self._btn('Y-', lambda: n(dy=-1)), self._btn('Y+', lambda: n(dy=1)),
                              self._btn('Z-', lambda: n(dz=-1)), self._btn('Z+', lambda: n(dz=1))))
        v.add_child(self._row(gui.Label('Поворот:'),
                              self._btn('yaw-', lambda: n(dyaw=-1)), self._btn('yaw+', lambda: n(dyaw=1)),
                              self._btn('крен-', lambda: n(droll=-1)), self._btn('крен+', lambda: n(droll=1)),
                              self._btn('тангаж-', lambda: n(dpitch=-1)), self._btn('тангаж+', lambda: n(dpitch=1))))
        v.add_child(gui.Label('крен - вокруг X, тангаж - вокруг Y (оси общей системы),\n'
                              'вращение вокруг точки сканера'))
        v.add_child(self._row(self._btn('Выровнять подвижный по горизонту', self.on_level_moving)))
        self.full_rot = gui.Checkbox('Полный поворот при решении (учитывать наклон)')
        self.full_rot.checked = True
        v.add_child(self.full_rot)
        self.tilt_label = gui.Label('')
        v.add_child(self.tilt_label)
        self.manual_score = gui.Label('')
        v.add_child(self.manual_score)
        v.add_child(self._row(self._btn('Оценить', self.on_manual_score),
                              self._btn('Принять позу', self.on_manual_accept),
                              self._btn('Отмена', self.on_manual_cancel)))
        return v

    def refresh_manual_combos(self):
        for cb in (self.fixed_combo, self.moving_combo, self.cand_combo, self.clean_combo,
                   self.fly_combo):
            cb.clear_items()
        for s in self.s.scans:
            if s.pose is not None:
                self.fixed_combo.add_item(s.id)
            self.moving_combo.add_item(s.id)
            self.cand_combo.add_item(s.id)
            self.clean_combo.add_item(s.id)
            if s.pose is not None:
                self.fly_combo.add_item(s.id)
        unplaced = [s.id for s in self.s.scans if s.pose is None]
        if unplaced:
            self.moving_combo.selected_text = unplaced[0]
            self.cand_combo.selected_text = unplaced[0]

    # ── вкладка «Кандидаты» ──────────────────────────────────────────────
    def _tab_candidates(self):
        v = gui.Vert(0.3 * self.em)
        self.cand_combo = gui.Combobox()
        v.add_child(self._row(gui.Label('Скан'), self.cand_combo))
        v.add_child(self._btn('Искать кандидатов', self.on_cand_search))
        v.add_child(gui.Label('оценка / совпало точек / нарушения / метод'))
        self.cand_list = gui.ListView()
        self.cand_list.set_max_visible_items(10)
        self.cand_list.set_on_selection_changed(self.on_cand_selected)
        v.add_child(self.cand_list)
        v.add_child(self._row(self._btn('Принять', self.on_cand_accept),
                              self._btn('Доработать вручную', self.on_cand_manual)))
        return v

    # ── вкладка «Чистка» ─────────────────────────────────────────────────
    def _tab_clean(self):
        v = gui.Vert(0.3 * self.em)
        self.clean_combo = gui.Combobox()
        self.clean_combo.set_on_selection_changed(lambda t, i: self.refresh_clean())
        v.add_child(self._row(gui.Label('Скан'), self.clean_combo))
        self.clean_cb = gui.Checkbox('Убирать зеркальные отражения')
        self.clean_cb.set_on_checked(self.on_clean_checked)
        v.add_child(self.clean_cb)
        v.add_child(self._btn('Показать/скрыть отражения (красным)', self.on_show_ghosts))
        self.clean_report = gui.Label('')
        v.add_child(self.clean_report)
        v.add_child(gui.Label('Экспорт склейки (только размещённые сканы)'))
        self.voxel = gui.NumberEdit(gui.NumberEdit.DOUBLE)
        self.voxel.double_value = 0.01
        v.add_child(self._row(gui.Label('Воксель, м'), self.voxel))
        self.export_common = gui.Checkbox('Экспорт в выровненной системе (Z вверх)')
        self.export_common.checked = False
        v.add_child(self.export_common)
        v.add_child(gui.Label('иначе - в исходной системе опорного скана'))
        v.add_child(self._btn('Экспорт... (.e57 / .pcd / .ply)', self.on_export))
        return v

    def refresh_clean(self):
        s = self.s.by_id(self.clean_combo.selected_text)
        if s is None:
            return
        self.clean_cb.checked = s.clean
        lines = []
        for r in s.reflect_report:
            if r['kind'] == 'window':
                w, h = r['size']
                lines.append(f"окно {w:.2f}×{h:.2f}: {r['match']:.2f} "
                             f"{'ЗЕРКАЛО, -' + str(r['removed']) if r['mirror'] else '-'}")
            else:
                lines.append(f"пол: {r['match']:.2f} "
                             f"{'ЗЕРКАЛО, -' + str(r['removed']) if r['mirror'] else '-'}")
        self.clean_report.text = '\n'.join(lines) or 'отражений не найдено'

    # ── фоновые задачи ───────────────────────────────────────────────────
    def run_bg(self, title, fn, done=None):
        if self.busy:
            self.set_status('Подождите: выполняется другая операция')
            return
        self.busy = True
        self.set_status(title)

        def progress(frac, msg):
            self.app.post_to_main_thread(self.w, lambda: self._set_progress(frac, msg))

        def work():
            try:
                res = fn(progress)
                err = None
            except Exception as e:                       # noqa: BLE001
                traceback.print_exc()
                res, err = None, e

            def finish():
                self.busy = False
                self.progress.value = 0.0
                if err is not None:
                    self.set_status(f'Ошибка: {err}')
                elif done:
                    done(res)
            self.app.post_to_main_thread(self.w, finish)
        threading.Thread(target=work, daemon=True).start()

    def _set_progress(self, frac, msg):
        self.progress.value = float(frac)
        self.set_status(msg)

    def set_status(self, msg):
        self.status.text = msg

    # ── сцена ────────────────────────────────────────────────────────────
    def load_all(self):
        """Анализ всех сканов (в фоне) и показ."""
        def work(progress):
            for i, s in enumerate(self.s.scans):
                progress(i / len(self.s.scans), f'анализ {s.id}')
                s.res
                s.ghost_mask()
                s._display = self.s.display_points(s)
            return True
        self.run_bg('анализ сканов...', work, lambda _: self.after_load())

    def after_load(self):
        self.redraw_all()
        self.refresh_scan_list()
        self.refresh_pairs()
        self.refresh_manual_combos()
        self.refresh_clean()
        self.view_top()
        self.w.set_needs_layout()
        self.w.post_redraw()
        self.set_status(f'Сканов: {len(self.s.scans)}, размещено: {len(self.s.placed())}')

    def _geom(self, s, color=None):
        pts = getattr(s, '_display', None)
        if pts is None:
            pts = s._display = self.s.display_points(s)
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
        pc.paint_uniform_color(color if color is not None else s.color)
        return pc

    def redraw_all(self):
        sc = self.sw.scene
        for s in self.s.scans:
            name = f'scan:{s.id}'
            if sc.has_geometry(name):
                sc.remove_geometry(name)
            T = self.s.Tc(s)
            if s is self.moving and self.T_moving is not None:
                T = self.T_moving
            if T is None:
                continue
            sc.add_geometry(name, self._geom(s), self.mat)
            sc.set_geometry_transform(name, T)
            sc.show_geometry(name, s.visible)
        for sid in list(self.ghost_shown):
            self._draw_ghosts(self.s.by_id(sid))
        self.sw.force_redraw()
        self.w.post_redraw()

    def on_point_size(self, v):
        self.mat.point_size = float(v)
        for s in self.s.scans:
            name = f'scan:{s.id}'
            if self.sw.scene.has_geometry(name):
                self.sw.scene.modify_geometry_material(name, self.mat)
        self.sw.force_redraw()

    def _bbox(self):
        pts = []
        for s in self.s.scans:
            T = self.T_moving if (s is self.moving and self.T_moving is not None) else self.s.Tc(s)
            if T is None or not s.visible or getattr(s, '_display', None) is None:
                continue
            d = s._display[::50]
            pts.append(pr.transform(d, T))
        if not pts:
            return o3d.geometry.AxisAlignedBoundingBox([-5, -5, -2], [5, 5, 2])
        P = np.vstack(pts)
        lo, hi = np.percentile(P, 1, axis=0), np.percentile(P, 99, axis=0)
        return o3d.geometry.AxisAlignedBoundingBox(lo, hi)

    def view_top(self):
        bb = self._bbox()
        c = bb.get_center()
        ext = bb.get_extent()
        self.sw.setup_camera(60, bb, c)
        self.sw.look_at(c, c + [0, 0, max(ext[0], ext[1]) * 0.95 + 5], [0, 1, 0])

    def view_3d(self):
        bb = self._bbox()
        c = bb.get_center()
        ext = bb.get_extent()
        self.sw.setup_camera(60, bb, c)
        d = max(ext) * 0.8 + 3
        self.sw.look_at(c, c + [-d, -d, d * 0.8], [0, 0, 1])

    def show_all(self):
        for s in self.s.scans:
            s.visible = True
        self.redraw_all()
        self.refresh_scan_list()

    def on_visible(self, s, checked):
        s.visible = checked
        name = f'scan:{s.id}'
        if self.sw.scene.has_geometry(name):
            self.sw.scene.show_geometry(name, checked)

    def only_show(self, ids):
        for s in self.s.scans:
            s.visible = s.id in ids
            name = f'scan:{s.id}'
            if self.sw.scene.has_geometry(name):
                self.sw.scene.show_geometry(name, s.visible)

    # ── проект ───────────────────────────────────────────────────────────
    def _file_dialog(self, mode, title, filters, on_done):
        dlg = gui.FileDialog(mode, title, self.w.theme)
        for ext, desc in filters:
            dlg.add_filter(ext, desc)
        dlg.set_path(str(Path.cwd()))
        dlg.set_on_cancel(self.w.close_dialog)

        def done(path):
            self.w.close_dialog()
            on_done(path)
        dlg.set_on_done(done)
        self.w.show_dialog(dlg)

    def on_open(self):
        self._file_dialog(gui.FileDialog.OPEN, 'Открыть проект',
                          [('.json', 'Проект (.json)')], self.open_project)

    def open_project(self, path):
        self.s = Session.from_project(path)
        self.sw.scene.clear_geometry()
        self.moving = self.fixed = None
        self.T_moving = None
        self.load_all()

    def on_add_scan(self):
        self._file_dialog(gui.FileDialog.OPEN, 'Добавить скан',
                          [('.e57 .pcd .ply .las .laz', 'Облака точек')], self.add_scan)

    def add_scan(self, path):
        self.s.add_scan(path)
        self.load_all()

    def on_save(self):
        if not self.s.project_path:
            return self.on_save_as()
        self.s.save()
        self.set_status(f'Сохранено: {self.s.project_path}')

    def on_save_as(self):
        self._file_dialog(gui.FileDialog.SAVE, 'Сохранить проект', [('.json', 'Проект')],
                          lambda p: (self.s.save(p), self.set_status(f'Сохранено: {p}')))

    def on_level_project(self):
        info = self.s.level_project()
        self.redraw_all()
        self.set_status(f"горизонт проекта: поправка {info['tilt_deg']:.2f} град по {info['horizontal']} "
                        f"гориз. и {info['walls']} верт. плоскостям (сохраняется в проекте)")

    def on_level_reset(self):
        self.s.reset_level()
        self.redraw_all()
        self.set_status('горизонт проекта сброшен')

    def on_ref_changed(self, text, idx):
        if text and text != self.s.frame and self.s.by_id(text).pose is not None:
            self.s.set_frame(text)
            self.redraw_all()
            self.refresh_scan_list()

    # ── пары ─────────────────────────────────────────────────────────────
    def on_auto(self):
        reuse = self.reuse_cb.checked

        def work(progress):
            self.s.run_auto(progress, reuse=reuse)
            return True

        def done(_):
            self.redraw_all()
            self.refresh_pairs()
            self.refresh_scan_list()
            self.refresh_manual_combos()
            self.set_status(f'Автостыковка: размещено {len(self.s.placed())} из {len(self.s.scans)}')
        self.run_bg('автостыковка...', work, done)

    def on_pair_selected(self, value, dbl):
        i = self.pair_list.selected_index
        if 0 <= i < len(self.s.edges):
            e = self.s.edges[i]
            self.only_show({e['A'], e['B']})
            self.set_status(f"Пара {e['A']} - {e['B']}: показаны только эти два скана "
                            f"(«Все видимы» - вернуть)")

    def on_pair_user(self, state):
        i = self.pair_list.selected_index
        if not (0 <= i < len(self.s.edges)):
            return
        self.s.set_edge_user(i, state)
        self.redraw_all()
        self.refresh_pairs()
        self.refresh_scan_list()
        self.refresh_manual_combos()
        self.pair_list.selected_index = i

    # ── ручная стыковка ──────────────────────────────────────────────────
    def on_manual_start(self, fixed_id=None, moving_id=None, T_init=None):
        fixed = self.s.by_id(fixed_id or self.fixed_combo.selected_text)
        moving = self.s.by_id(moving_id or self.moving_combo.selected_text)
        if fixed is None or moving is None or fixed is moving or fixed.pose is None:
            self.set_status('Выберите размещённый неподвижный и другой подвижный скан')
            return
        self.fixed, self.moving = fixed, moving
        self.T_moving = np.asarray(T_init) if T_init is not None else (
            self.s.Tc(moving) if moving.pose is not None else self.s.Tc(fixed))
        self.pairs, self.pending = [], None
        self._clear_markers()
        self.only_show({fixed.id, moving.id})
        self.redraw_all()
        self.refresh_manual()
        self.tilt_label.text = f'наклон подвижного: {Session.tilt_deg(self.T_moving):.2f} град'
        self.tabs.selected_tab_index = 2
        self.set_status('Ctrl/Cmd + клик по облаку: признак в неподвижном, затем такой же в подвижном')

    def _scan_under(self, W):
        """Какой из двух сканов ближе к точке W (общая система)."""
        best = None
        for s, T in ((self.fixed, self.s.Tc(self.fixed)), (self.moving, self.T_moving)):
            P = pr.transform(s._display, T)
            d = np.min(np.linalg.norm(P - W, axis=1))
            if best is None or d < best[0]:
                best = (d, s, T)
        return best[1], best[2]

    FLY_KEYS = {gui.KeyName.W, gui.KeyName.A, gui.KeyName.S, gui.KeyName.D,
                gui.KeyName.SPACE, gui.KeyName.C, gui.KeyName.E, gui.KeyName.Q,
                gui.KeyName.LEFT_SHIFT, gui.KeyName.RIGHT_SHIFT}

    def _on_key(self, ev):
        down = ev.type == gui.KeyEvent.Type.DOWN
        if down and ev.key == gui.KeyName.F12:
            self.save_screenshot()
            return True
        if down and ev.key == gui.KeyName.F and not getattr(ev, 'is_repeat', False):
            self.toggle_fly()
            return True
        if self.fly:
            if down and ev.key == gui.KeyName.ESCAPE:
                self.toggle_fly()
                return True
            if ev.key in self.FLY_KEYS:
                (self.fly_keys.add if down else self.fly_keys.discard)(ev.key)
                return True
        return False

    # ── режим полёта ─────────────────────────────────────────────────────
    def toggle_fly(self):
        self.fly = not self.fly
        self.fly_keys.clear()
        self._fly_last = None
        if self.fly:
            M = np.asarray(self.sw.scene.camera.get_model_matrix())    # камера → мир
            self.fly_pos = M[:3, 3].copy()
            fwd = -M[:3, 2]
            self.fly_yaw = float(np.arctan2(fwd[1], fwd[0]))
            self.fly_pitch = float(np.clip(np.arcsin(np.clip(fwd[2], -1, 1)), -1.5, 1.5))
            # сверху «шутер» бессмыслен — встаём в точку опорного скана
            if abs(self.fly_pitch) > 1.2:
                self.fly_to_scan(self.s.frame)
            self.fly_btn.text = 'Выйти из полёта (F/Esc)'
            self._apply_fly_camera()
            self.set_status(f'Полёт: скорость {self.fly_speed:.1f} м/с (колесо - изменить)')
        else:
            self.fly_btn.text = 'Режим полёта (F)'
            self.set_status('Полёт выключен')

    def fly_to_scan(self, sid=None):
        s = self.s.by_id(sid or self.fly_combo.selected_text or self.s.frame)
        if s is None or s.pose is None:
            return
        T = self.s.Tc(s)
        self.fly_pos = T[:3, 3].copy()                    # сканер ~ на высоте головы
        fwd = T[:3, 0]
        self.fly_yaw = float(np.arctan2(fwd[1], fwd[0]))
        self.fly_pitch = 0.0
        if not self.fly:
            self.toggle_fly()
        self._apply_fly_camera()

    def _fly_forward(self):
        cp = np.cos(self.fly_pitch)
        return np.array([cp * np.cos(self.fly_yaw), cp * np.sin(self.fly_yaw), np.sin(self.fly_pitch)])

    def _apply_fly_camera(self):
        eye = self.fly_pos
        self.sw.look_at(eye + self._fly_forward(), eye, [0, 0, 1])
        f = self.sw.frame
        if f.width > 0 and f.height > 0:
            # близкая плоскость отсечения — иначе стены вплотную обрезаются
            self.sw.scene.camera.set_projection(60, f.width / f.height, 0.03, 2000,
                                                rendering.Camera.FovType.Vertical)
        self.sw.force_redraw()

    def _on_tick(self):
        if not self.fly or not self.fly_keys:
            self._fly_last = None
            return False
        now = time.time()
        dt = 0.0 if self._fly_last is None else min(0.1, now - self._fly_last)
        self._fly_last = now
        K = gui.KeyName
        has = self.fly_keys.__contains__
        v = self.fly_speed * (4.0 if (has(K.LEFT_SHIFT) or has(K.RIGHT_SHIFT)) else 1.0) * dt
        fwd = np.array([np.cos(self.fly_yaw), np.sin(self.fly_yaw), 0.0])   # ходьба — по горизонтали
        right = np.array([np.sin(self.fly_yaw), -np.cos(self.fly_yaw), 0.0])
        up = np.array([0.0, 0.0, 1.0])
        d = (has(K.W) - has(K.S)) * fwd + (has(K.D) - has(K.A)) * right + \
            ((has(K.SPACE) or has(K.E)) - (has(K.C) or has(K.Q))) * up
        if not d.any():
            return False
        self.fly_pos = self.fly_pos + v * d
        self._apply_fly_camera()
        return True

    def _fly_mouse(self, ev):
        T = gui.MouseEvent.Type
        if ev.type == T.WHEEL:
            self.fly_speed = float(np.clip(self.fly_speed * (1.25 ** (-np.sign(ev.wheel_dy))), 0.1, 50))
            self.set_status(f'Полёт: скорость {self.fly_speed:.1f} м/с')
            return True
        if ev.type == T.BUTTON_DOWN:
            self._look = (ev.x, ev.y)
            return True
        if ev.type == T.DRAG and self._look is not None:
            dx, dy = ev.x - self._look[0], ev.y - self._look[1]
            self._look = (ev.x, ev.y)
            sens = np.radians(0.2)
            self.fly_yaw -= dx * sens
            self.fly_pitch = float(np.clip(self.fly_pitch - dy * sens, -1.5, 1.5))
            self._apply_fly_camera()
            return True
        if ev.type == T.BUTTON_UP:
            self._look = None
            return True
        return False

    # ── скриншот ─────────────────────────────────────────────────────────
    def save_screenshot(self, out_dir=None):
        """
        Три файла: снимок окна с экрана (нужно разрешение «Запись экрана» у
        приложения, из которого запущен инструмент), рендер 3D-сцены той же
        камерой и текстовое состояние интерфейса.
        """
        out = Path(out_dir or Path(__file__).resolve().parent / 'screenshots')
        out.mkdir(exist_ok=True)
        stem = out / time.strftime('shot_%Y%m%d_%H%M%S')
        f = self.w.os_frame
        capture_window((f.x, f.y, f.width, f.height), f'{stem}_window.png')
        img = self.app.render_to_image(self.sw.scene, self.sw.frame.width, self.sw.frame.height)
        o3d.io.write_image(f'{stem}_scene.png', img)
        Path(f'{stem}_ui.txt').write_text(self.ui_state(), encoding='utf-8')
        self.set_status(f'скриншот: {stem.name}_*.png / _ui.txt')
        return str(stem)

    def ui_state(self):
        tabs = ['Проект', 'Пары', 'Ручная', 'Кандидаты', 'Чистка']
        lines = [f'окно: {self.w.os_frame.width}×{self.w.os_frame.height}, '
                 f'3D-вид {self.sw.frame.width}×{self.sw.frame.height}',
                 f'вкладка: {tabs[self.tabs.selected_tab_index]}',
                 f'статус: {self.status.text}', '',
                 'сканы:'] + [f"  {'[x]' if s.visible else '[ ]'} {s.id}  "
                              f"{'опорный' if s.id == self.s.frame else ('размещён' if s.pose is not None else 'НЕ размещён')}"
                              for s in self.s.scans]
        lines += ['', f'рёбер: {len(self.s.edges)}, активных: {len(self.s.active_edges())}',
                  self.loops_label.text]
        if self.moving is not None:
            lines += ['', f'ручная: {self.fixed.id} <- {self.moving.id}',
                      'пары:'] + [f'  {i + 1}. {_fdesc(a)} <-> {_fdesc(b)}' for i, (a, b) in enumerate(self.pairs)]
            lines += [f'степени свободы: {self.dof_label.text}', f'оценка: {self.manual_score.text}',
                      'поза подвижного (общая система):', np.array2string(self.T_moving, precision=3)]
        if self.candidates:
            lines += ['', 'кандидаты:'] + [f"  {c['score']:+.3f} {c['n_close']} {c['violations']:.3f} {c['method']}"
                                           for c in self.candidates]
        return '\n'.join(lines) + '\n'

    def _ray_ground(self, ev, z0):
        """Пересечение луча под курсором с горизонтальной плоскостью z = z0 (общая система)."""
        x = ev.x - self.sw.frame.x
        y = ev.y - self.sw.frame.y
        cam = self.sw.scene.camera
        W, H = self.sw.frame.width, self.sw.frame.height
        # глубина 1.0 = бесконечная дальняя плоскость → NaN; берём 0 и 0.5
        p0 = np.asarray(cam.unproject(x, y, 0.0, W, H), float)
        p1 = np.asarray(cam.unproject(x, y, 0.5, W, H), float)
        d = p1 - p0
        if not (np.isfinite(p0).all() and np.isfinite(d).all()) or abs(d[2]) < 1e-9:
            return None
        g = p0 + (z0 - p0[2]) / d[2] * d
        return g if np.isfinite(g).all() else None

    def _rotate_drag(self, ev):
        # Alt+тянуть; на Linux Alt+тянуть часто перехватывает оконный менеджер —
        # поэтому также Shift + правая кнопка
        return ev.is_modifier_down(gui.KeyModifier.ALT) or (
            ev.is_modifier_down(gui.KeyModifier.SHIFT) and ev.is_button_down(gui.MouseButton.RIGHT))

    def _on_drag(self, ev):
        """Shift+тянуть - сдвиг подвижного скана в плане, Alt+тянуть - поворот вокруг вертикали."""
        shift = ev.is_modifier_down(gui.KeyModifier.SHIFT)
        alt = self._rotate_drag(ev)
        T = gui.MouseEvent.Type
        if ev.type == T.BUTTON_DOWN and (shift or alt):
            z0 = float(self.T_moving[2, 3])
            self._drag = {'mode': 'rot' if alt else 'move', 'T0': self.T_moving.copy(),
                          'x0': ev.x, 'g0': self._ray_ground(ev, z0), 'z0': z0}
            return True
        if ev.type == T.DRAG and self._drag is not None:
            d = self._drag
            if d['mode'] == 'move':
                g = self._ray_ground(ev, d['z0'])
                if g is None or d['g0'] is None:
                    return True                          # луч параллелен полу — пропустить
                delta = g - d['g0']
                if np.linalg.norm(delta) > 200:          # защита от вырожденного луча
                    return True
                self.T_moving = Session.nudge(d['T0'], delta[0], delta[1], 0.0, 0.0)
            else:
                self.T_moving = Session.nudge(d['T0'], dyaw_deg=0.3 * (ev.x - d['x0']))
            self._update_moving()
            return True
        if ev.type == T.BUTTON_UP and self._drag is not None:
            self._drag = None
            self.live_score()
            return True
        return False

    def _on_mouse(self, ev):
        if self.moving is not None and self.T_moving is not None and self._on_drag(ev):
            return gui.Widget.EventCallbackResult.CONSUMED
        mods = (gui.KeyModifier.CTRL, gui.KeyModifier.META)
        if (ev.type == gui.MouseEvent.Type.BUTTON_DOWN and self.moving is not None
                and any(ev.is_modifier_down(m) for m in mods)):
            x = ev.x - self.sw.frame.x
            y = ev.y - self.sw.frame.y

            def on_depth(depth):
                D = np.asarray(depth)
                # на Mac карта глубины бывает в другом разрешении, чем виджет (Retina,
                # Open3D issue #6999) - масштабируем координаты клика
                yi = min(D.shape[0] - 1, int(y * D.shape[0] / max(1, self.sw.frame.height)))
                xi = min(D.shape[1] - 1, int(x * D.shape[1] / max(1, self.sw.frame.width)))
                z = D[yi, xi]
                if z >= 1.0:
                    self.app.post_to_main_thread(self.w, lambda: self.set_status('мимо облака'))
                    return
                W = self.sw.scene.camera.unproject(x, y, z, self.sw.frame.width,
                                                   self.sw.frame.height)
                self.app.post_to_main_thread(self.w, lambda: self.pick_world(np.asarray(W)))
            self.sw.scene.scene.render_to_depth_image(on_depth)
            return gui.Widget.EventCallbackResult.HANDLED
        if self.fly and self._fly_mouse(ev):
            return gui.Widget.EventCallbackResult.CONSUMED
        return gui.Widget.EventCallbackResult.IGNORED

    def pick_world(self, W):
        """Выбор признака по точке W в общей системе (вызывается и из скриптов/тестов)."""
        if self.moving is None:
            return None
        scan, T = self._scan_under(W)
        p = pr.transform(np.asarray(W)[None], np.linalg.inv(T))[0]
        kind = {'авто': 'auto', 'плоскость': 'plane', 'проём': 'opening',
                'точка': 'point'}[self.pick_kind.selected_text or 'авто']
        f = self.s.pick_feature(scan, p, kind)
        if f is None:
            self.set_status(f'здесь нет признака «{self.pick_kind.selected_text}»')
            return None
        k = len(self.pairs)
        color = PICK_COLORS[k % len(PICK_COLORS)]
        if scan is self.fixed:
            self.pending = f
            self._draw_feature(f, self.s.Tc(self.fixed), color, f'pick:{k}:A')
            self.set_status(f'неподвижный: {_fdesc(f)} - теперь такой же признак в подвижном')
        else:
            if self.pending is None:
                self.set_status('сначала выберите признак в неподвижном скане')
                return None
            why = _incompatible(self.pending, f)
            if why:
                self.set_status(f'пара не принята: {why}. Выберите другой признак в подвижном')
                return None
            self.pairs.append((self.pending, f))
            self._draw_feature(f, self.T_moving, color, f'pick:{k}:B', moving=True)
            self.pending = None
            self.set_status(f'пара {k + 1}: {_fdesc(self.pairs[-1][0])} <-> {_fdesc(f)}')
            self._pairs_changed()
            return f
        self.refresh_manual()
        return f

    def _draw_feature(self, f, T, color, name, moving=False):
        sc = self.sw.scene
        scan = self.s.by_id(f['scan'])
        if f['type'] == 'plane':
            pl = next(p for p in scan.planes if p.id == f['index'])
            g = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(scan.res['down'][pl.inliers]))
            g.paint_uniform_color(color)
            mat = self.mat_big
        elif f['type'] == 'opening':
            o = scan.openings[f['index']]
            C = np.asarray(o.corners)
            g = o3d.geometry.LineSet(o3d.utility.Vector3dVector(C),
                                     o3d.utility.Vector2iVector([[0, 1], [1, 2], [2, 3], [3, 0], [0, 2], [1, 3]]))
            g.paint_uniform_color(color)
            mat = rendering.MaterialRecord()
            mat.shader = 'unlitLine'
            mat.line_width = 4
        else:
            g = o3d.geometry.TriangleMesh.create_sphere(0.06)
            g.translate(f['p'])
            g.paint_uniform_color(color)
            mat = self.mat_mesh
        if sc.has_geometry(name):
            sc.remove_geometry(name)
        sc.add_geometry(name, g, mat)
        sc.set_geometry_transform(name, T)
        if moving:
            self._moving_markers.add(name)

    def _clear_markers(self):
        sc = self.sw.scene
        for k in range(64):
            for side in 'AB':
                if sc.has_geometry(f'pick:{k}:{side}'):
                    sc.remove_geometry(f'pick:{k}:{side}')
        self._moving_markers = set()

    def _update_moving(self):
        sc = self.sw.scene
        name = f'scan:{self.moving.id}'
        if not sc.has_geometry(name):
            sc.add_geometry(name, self._geom(self.moving), self.mat)
        sc.set_geometry_transform(name, self.T_moving)
        for m in getattr(self, '_moving_markers', ()):
            if sc.has_geometry(m):
                sc.set_geometry_transform(m, self.T_moving)
        self.sw.force_redraw()
        self.w.post_redraw()
        self.tilt_label.text = f'наклон подвижного: {Session.tilt_deg(self.T_moving):.2f} град'

    def live_score(self):
        """Оценка текущей позы подвижного скана в фоне (без блокировки других действий)."""
        if self.moving is None or self._score_pending:
            return
        self._score_pending = True
        moving, T = self.moving, self.T_moving.copy()

        def work():
            try:
                r = self.s.score_pose(moving, T)
            except Exception as e:                       # noqa: BLE001
                r = {'error': str(e)}

            def show():
                self._score_pending = False
                if r is None:
                    self.manual_score.text = 'нет других размещённых сканов'
                elif 'error' in r:
                    self.manual_score.text = f"ошибка оценки: {r['error']}"
                else:
                    self.manual_score.text = (f"оценка {r['score']:+.3f}  совпало {r['n_close']}  "
                                              f"нарушений {r['violations']:.3f}")
            self.app.post_to_main_thread(self.w, show)
        threading.Thread(target=work, daemon=True).start()

    def _pairs_changed(self):
        self.refresh_manual()
        if self.auto_solve.checked and self.pairs and \
                self.s.dof_status(self.fixed, self.moving, self.pairs)['text'].startswith('всё'):
            self.on_manual_solve()

    def on_manual_delete_selected(self):
        i = self.manual_pairs.selected_index
        if not (0 <= i < len(self.pairs)):
            return
        self.pairs.pop(i)
        self._clear_markers()
        for k, (fa, fb) in enumerate(self.pairs):          # перерисовать маркеры с новыми номерами
            c = PICK_COLORS[k % len(PICK_COLORS)]
            self._draw_feature(fa, self.s.Tc(self.fixed), c, f'pick:{k}:A')
            self._draw_feature(fb, self.T_moving, c, f'pick:{k}:B', moving=True)
        self._pairs_changed()

    def refresh_manual(self):
        self.manual_pairs.set_items([f'{i + 1}. {_fdesc(a)} <-> {_fdesc(b)}'
                                     for i, (a, b) in enumerate(self.pairs)] +
                                    ([f'... {_fdesc(self.pending)} <-> ?'] if self.pending else []))
        if self.fixed is not None:
            self.dof_label.text = self.s.dof_status(self.fixed, self.moving, self.pairs)['text']

    def on_manual_clear(self):
        self.pairs, self.pending = [], None
        self._clear_markers()
        self.refresh_manual()

    def on_manual_undo(self):
        if self.pending is not None:
            self.pending = None
        elif self.pairs:
            k = len(self.pairs) - 1
            self.pairs.pop()
            for side in 'AB':
                n = f'pick:{k}:{side}'
                if self.sw.scene.has_geometry(n):
                    self.sw.scene.remove_geometry(n)
        self._pairs_changed()

    def on_manual_solve(self):
        if not self.pairs:
            return
        fixed, moving, pairs, T0 = self.fixed, self.moving, list(self.pairs), self.T_moving

        full = self.full_rot.checked

        def work(progress):
            return self.s.solve_manual(fixed, moving, pairs, T_init=T0, full=full)

        def done(r):
            T, info = r
            self.T_moving = T
            self._update_moving()
            extra = ''
            if info.get('weak'):
                extra = ' - есть неопределённые направления, уточните ICP или добавьте пару'
            if info.get('method') == 'opening':
                side = ('с одной стороны стены' if info['same_side']
                        else f"сквозь стену, толщина {info['delta']:.2f} м")
                extra = f" - проём {side}, нарушений {info['violations']:.3f}"
            rot = ('полный поворот' if info.get('rotation') == '6dof'
                   else 'только вокруг вертикали (пары не задают наклон)')
            self.set_status(f"решено ({rot}): yaw {np.degrees(np.arctan2(T[1, 0], T[0, 0])):.1f}, "
                            f"наклон {Session.tilt_deg(T):.2f} град{extra}")
            self.live_score()
        self.run_bg('решение позы...', work, done)

    def on_manual_icp(self):
        if self.moving is None:
            return
        fixed, moving, T0 = self.fixed, self.moving, self.T_moving
        others = [s for s in self.s.placed() if s is not moving]

        def work(progress):
            return self.s.refine_icp(others or [fixed], moving, T0)

        def done(r):
            T, info = r
            self.T_moving = T
            self._update_moving()
            dt, da = info['shift']
            self.set_status(f"ICP: fitness {info['fitness']:.2f}, rmse {info['rmse'] * 100:.1f} см, "
                            f"сдвиг {dt * 100:.1f} см {da:.2f}°")
            self.live_score()
        self.run_bg('ICP...', work, done)

    def _nudge(self, dx=0, dy=0, dz=0, dyaw=0, droll=0, dpitch=0):
        if self.moving is None:
            return
        st, sd = self.step.double_value, self.step_deg.double_value
        self.T_moving = Session.nudge(self.T_moving, dx * st, dy * st, dz * st, dyaw * sd,
                                      droll * sd, dpitch * sd)
        self._update_moving()
        self.live_score()

    def on_level_moving(self):
        if self.moving is None:
            return
        T2, info = self.s.level_pose(self.moving, self.T_moving)
        if info['horizontal'] + info['walls'] == 0:
            self.set_status('нет подходящих плоскостей (пол/земля, стены) для выравнивания')
            return
        before = Session.tilt_deg(self.T_moving)
        self.T_moving = T2
        self._update_moving()
        self.live_score()
        self.set_status(f"выровнено по {info['horizontal']} гориз. и {info['walls']} верт. плоскостям: "
                        f"поправка {info['tilt_deg']:.2f} град (наклон оси был {before:.2f}, "
                        f"стал {Session.tilt_deg(T2):.2f})")

    def on_manual_score(self):
        if self.moving is None:
            return
        moving, T = self.moving, self.T_moving
        self.run_bg('оценка...', lambda p: self.s.score_pose(moving, T),
                    lambda r: setattr(self.manual_score, 'text',
                                      'нет других размещённых сканов' if r is None else
                                      f"оценка {r['score']:+.3f}, совпало {r['n_close']}, "
                                      f"нарушений {r['violations']:.3f}"))

    def on_manual_accept(self):
        if self.moving is None:
            return
        moving, fixed, T = self.moving, self.fixed, self.T_moving

        def work(progress):
            self.s.accept_pose(moving, T, anchor=fixed, method='ручная')
            return True

        def done(_):
            self.on_manual_cancel()
            self.set_status(f'{moving.id}: поза принята (ручное ребро к {fixed.id})')
        self.run_bg('пересчёт графа...', work, done)

    def on_manual_cancel(self):
        self.moving = self.fixed = None
        self.T_moving = None
        self.pairs, self.pending = [], None
        self._clear_markers()
        self.show_all()
        self.refresh_pairs()
        self.refresh_manual_combos()

    # ── кандидаты ────────────────────────────────────────────────────────
    def on_cand_search(self):
        s = self.s.by_id(self.cand_combo.selected_text)
        if s is None:
            return

        def done(c):
            self.candidates = c
            self.cand_scan = s
            self.cand_list.set_items([
                f"{x['score']:+.3f} / {x['n_close']} / {x['violations']:.3f} / {x['method']}"
                for x in c])
            self.set_status(f'кандидатов: {len(c)} - выберите, чтобы посмотреть')
        self.run_bg('поиск кандидатов...', lambda p: self.s.candidates(s, p), done)

    def on_cand_selected(self, value, dbl):
        i = self.cand_list.selected_index
        if not (0 <= i < len(self.candidates)):
            return
        s = self.cand_scan
        name = f'scan:{s.id}'
        sc = self.sw.scene
        if not sc.has_geometry(name):
            sc.add_geometry(name, self._geom(s), self.mat)
        sc.set_geometry_transform(name, self.candidates[i]['Tc'])
        s.visible = True
        sc.show_geometry(name, True)
        self.sw.force_redraw()

    def on_cand_accept(self):
        i = self.cand_list.selected_index
        if not (0 <= i < len(self.candidates)):
            return
        s, T = self.cand_scan, self.candidates[i]['Tc']

        def work(progress):
            self.s.accept_pose(s, T, anchor=self.s.ref, method='кандидат')
            return True

        def done(_):
            self.redraw_all()
            self.refresh_pairs()
            self.refresh_scan_list()
            self.refresh_manual_combos()
            self.set_status(f'{s.id}: кандидат принят')
        self.run_bg('пересчёт графа...', work, done)

    def on_cand_manual(self):
        i = self.cand_list.selected_index
        if not (0 <= i < len(self.candidates)):
            return
        self.on_manual_start(self.s.frame, self.cand_scan.id, self.candidates[i]['Tc'])

    # ── чистка и экспорт ─────────────────────────────────────────────────
    def on_clean_checked(self, checked):
        s = self.s.by_id(self.clean_combo.selected_text)
        if s is None:
            return
        s.clean = checked
        s._display = self.s.display_points(s)
        self.redraw_all()

    def _draw_ghosts(self, s):
        name = f'ghost:{s.id}'
        sc = self.sw.scene
        if sc.has_geometry(name):
            sc.remove_geometry(name)
        T = self.s.Tc(s)
        if T is None:
            return
        g = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(s.res['down'][s.ghost_mask()]))
        g.paint_uniform_color((0.9, 0.1, 0.1))
        sc.add_geometry(name, g, self.mat)
        sc.set_geometry_transform(name, T)

    def on_show_ghosts(self):
        s = self.s.by_id(self.clean_combo.selected_text)
        if s is None:
            return
        if s.id in self.ghost_shown:
            self.ghost_shown.discard(s.id)
            if self.sw.scene.has_geometry(f'ghost:{s.id}'):
                self.sw.scene.remove_geometry(f'ghost:{s.id}')
        else:
            self.ghost_shown.add(s.id)
            self._draw_ghosts(s)
        self.sw.force_redraw()

    def on_export(self):
        voxel = self.voxel.double_value
        frame = 'common' if self.export_common.checked else 'ref'

        def go(path):
            self.run_bg('экспорт...', lambda p: self.s.export(path, voxel, p, frame),
                        lambda n: self.set_status(f'Экспорт: {path} ({n:,} точек)'))
        self._file_dialog(gui.FileDialog.SAVE, 'Экспорт склейки',
                          [('.e57', 'E57'), ('.pcd', 'PCD'), ('.ply', 'PLY')], go)


def _incompatible(fa, fb):
    """Причина, по которой признаки нельзя сопоставить, или None."""
    if fa['type'] != fb['type'] and 'point' not in (fa['type'], fb['type']):
        return f"{_fdesc(fa)} и {_fdesc(fb)} - разные типы признаков"
    if fa['type'] == 'plane' and fb['type'] == 'plane':
        ka, kb = fa.get('kind'), fb.get('kind')
        if ka != kb:
            return f"{_fdesc(fa)} <-> {_fdesc(fb)}: разные виды плоскостей"
    return None


def _short(sid):
    """static_20260922_231157.e57 -> 231157"""
    stem = Path(sid).stem
    return stem.split('_')[-1] if '_' in stem else stem


def _fdesc(f):
    if f is None:
        return ''
    if f['type'] == 'plane':
        return {'wall': 'стена', 'floor': 'пол', 'ceiling': 'потолок'}.get(f.get('kind'), 'плоскость') + \
            f" {f['area']:.1f} м²"
    if f['type'] == 'opening':
        return f"проём {f['size'][0]:.2f}×{f['size'][1]:.2f}"
    return 'точка'


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):              # русский текст в консоли Windows
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                                # noqa: BLE001
            pass
    argv = sys.argv[1:] if argv is None else argv
    if FONT is None:
        print('Не найден шрифт с кириллицей: укажите PULSE_GUI_FONT=/путь/к/шрифту.ttf')
    if '--web' in argv:
        # окно рендерится в фоне и показывается в браузере: http://localhost:8888
        # (обход чёрного нативного окна Open3D на macOS 15 + Metal)
        argv = [a for a in argv if a != '--web']
        o3d.visualization.webrtc_server.enable_webrtc()
        print('Откройте в браузере: http://localhost:8888', flush=True)
    app = gui.Application.instance
    app.initialize()
    setup_fonts(app)
    if argv and argv[0].endswith('.json'):
        sess = Session.from_project(argv[0])
    elif argv:
        sess = Session.from_scans(argv)
    else:
        sess = Session()
    App(sess)
    app.run()


if __name__ == '__main__':
    main()
