"""
Панели инструментов (правая часть окна).

Инструмент ленты бывает трёх видов:
  action — без параметров, срабатывает сразу;
  tool   — открывает свою панель: область, параметры, оценка, «Выполнить», результат;
  mode   — панель + режим мыши (замеры, рамка, сечение…), внизу «Готово».
Shift + щелчок по кнопке инструмента — выполнить сразу с прошлыми настройками.

Здесь — общие части (RunBar, ScopeBar, ToolPanel) и панели инструментов, у которых не
было своей панели: автостыковка, кандидаты позы, качество, экспорт, упаковка.
Панели чистки, контроля и поверхности показывают свои разделы через _Panel.focus.
"""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QVBoxLayout, QProgressBar, QComboBox, QCheckBox, QSlider,
                               QLineEdit, QFileDialog, QDoubleSpinBox, QHBoxLayout, QLabel)

from . import widgets as W
from .inspector import _Panel


class RunBar(QFrame):
    """Закреплённый низ панели: основная кнопка, прогресс, «Остановить»."""
    run = Signal()
    stop = Signal()

    def __init__(self, text='Выполнить', icon='mdi6.play', tip=None):
        super().__init__()
        self.setObjectName('SecStick')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(6)
        self.b_run = W.button(text, self.run.emit, primary=True, icon=W.THEME.icon(icon, 'onaccent'),
                              tip=(tip or text) + ' (Shift + щелчок по кнопке на ленте — с прошлыми настройками)')
        lay.addWidget(self.b_run)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.b_stop = W.button('Остановить', self.stop.emit, icon=W.THEME.icon('mdi6.stop'),
                               tip='Остановить (Esc); уже посчитанное сохранится')
        self.msg = W.label('', 'Hint', wrap=True)
        lay.addWidget(self.progress)
        lay.addWidget(self.msg)
        lay.addWidget(self.b_stop)
        self.set_running(False)

    def set_running(self, on, cancellable=False):
        self.b_run.setEnabled(not on)
        self.progress.setVisible(on)
        self.msg.setVisible(on)
        self.b_stop.setVisible(on and cancellable)
        self.b_stop.setEnabled(True)
        if on:
            self.progress.setValue(0)
            self.msg.setText('')

    def set_progress(self, frac, msg):
        self.progress.setValue(int(1000 * max(0.0, min(1.0, frac))))
        self.msg.setText(msg)


class DoneBar(QFrame):
    """Низ панели режима: «Готово» (выйти из режима, результат остаётся)."""
    done = Signal()

    def __init__(self, text='Готово'):
        super().__init__()
        self.setObjectName('SecStick')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.addWidget(W.button(text, self.done.emit, icon=W.THEME.icon('mdi6.check'), tip=f'{text} (Esc)'))


class ScopeBar(QFrame):
    """Область действия: сегменты + строка «что попадёт»."""
    changed = Signal(object)

    def __init__(self, items, current=None):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.seg = W.Segmented(items, current)
        self.seg.changed.connect(self.changed.emit)
        self.info = W.label('', 'KV_v', wrap=True)
        lay.addWidget(self.seg)
        lay.addWidget(self.info)

    def value(self):
        return self.seg.value()

    def set_value(self, v):
        self.seg.set_value(v)

    def set_info(self, text):
        self.info.setText(text)


class ToolPanel(_Panel):
    """Панель инструмента: [область] → параметры → оценка → «Выполнить» → результат."""
    action = Signal(str, object)

    def __init__(self, title, run_text='Выполнить', run_icon='mdi6.play', scopes=None, scope=None):
        super().__init__(title, closable=True)
        self.scope = None
        if scopes:
            s = self.add(W.Section('Область'))
            self.scope = ScopeBar(scopes, scope)
            self.scope.changed.connect(lambda v: self.action.emit('scope', v))
            s.add(self.scope)
        self.params_sec = self.add(W.Section('Параметры'))
        self.est = W.label('', 'KV_v', wrap=True)
        self.result_sec = W.Section('Результат')
        self.result = W.label('', 'KV_v', wrap=True)
        self.result_sec.add(self.result)
        self.result_sec.setVisible(False)
        self.runbar = RunBar(run_text, run_icon)
        self.runbar.run.connect(lambda: self.action.emit('run', None))
        self.runbar.stop.connect(lambda: self.action.emit('stop', None))
        self.foot.addWidget(self.runbar)

    def finish_params(self):
        self.params_sec.add(self.est)
        self.add(self.result_sec)
        self.finish()

    def set_estimate(self, text):
        self.est.setText(text)
        self.est.setVisible(bool(text))

    def set_result(self, text):
        self.result.setText(text)
        self.result_sec.setVisible(bool(text))

    def state(self):
        """Параметры для запоминания между запусками."""
        return {}

    def restore(self, d):
        pass


# ── автостыковка ───────────────────────────────────────────────────────────
class AutoPanel(ToolPanel):
    METHODS = [('все пары', 'all'), ('по дереву', 'tree'), ('внутри групп', 'within'),
               ('группы между собой', 'groups')]
    HINTS = {'all': 'Каждый скан с каждым. Надёжно, но время растёт как квадрат числа сканов.',
             'tree': 'Все пары внутри каждой группы и по два крупных скана соседних групп. Быстрее на больших '
                     'объектах.',
             'within': 'Только пары внутри группы (с подгруппами); связи между группами не трогаются.',
             'groups': 'Каждая группа — жёсткое целое (облако по внутренним связям); стыкуются дочерние группы '
                       'выбранной ветки или группы верхнего уровня.'}

    def __init__(self):
        super().__init__('Автостыковка', 'Выполнить стыковку', 'mdi6.graph-outline',
                         scopes=[('весь проект', 'project'), ('выбранная группа', 'group'),
                                 ('выделенные', 'selection')], scope='project')
        p = self.params_sec
        self.method = QComboBox()
        for text, v in self.METHODS:
            self.method.addItem(text, v)
        self.method.activated.connect(lambda _: self.set_method(self.method.currentData(), emit=True))
        self.method_row = p.add(W.field('Какие пары', self.method))
        self.hint = W.label('', 'Hint', wrap=True)
        p.add(self.hint)
        self.reuse = QCheckBox('Использовать уже посчитанные пары')
        self.reuse.setChecked(True)
        self.reuse.setToolTip('Посчитанные раньше пары не пересчитываются: быстрее, остановленная стыковка '
                              'продолжается с места')
        self.reuse.toggled.connect(lambda _: self.action.emit('params', None))
        p.add(self.reuse)
        self.finish_params()
        self.set_method('tree')

    def set_method(self, m, emit=False):
        i = self.method.findData(m)
        if i < 0:
            return
        self.method.setCurrentIndex(i)
        self.hint.setText(self.HINTS[m])
        self.reuse.setVisible(m != 'groups')
        if emit:
            self.action.emit('params', None)

    def method_value(self):
        return self.method.currentData()

    def set_selection_mode(self, on):
        """Область «выделенные»: всегда все пары среди выделенных — выбор способа не нужен."""
        self.method_row.setVisible(not on)
        self.hint.setVisible(not on)

    def state(self):
        return {'method': self.method_value(), 'reuse': self.reuse.isChecked()}

    def restore(self, d):
        if d.get('method'):
            self.set_method(d['method'])
        if 'reuse' in d:
            self.reuse.setChecked(bool(d['reuse']))


# ── кандидаты позы ─────────────────────────────────────────────────────────
class CandPanel(ToolPanel):
    def __init__(self):
        super().__init__('Кандидаты позы', 'Найти кандидатов', 'mdi6.target')
        p = self.params_sec
        self.scan = QComboBox()
        self.scan.activated.connect(lambda _: self.action.emit('params', None))
        p.add(W.field('Скан, для которого искать позу', self.scan))
        p.add(W.label('Гипотезы относительно размещённых сканов: по плоскостям и по «уличным» точкам, видимым '
                      'сквозь проёмы (для фасадов). Список — в нижней панели «Кандидаты»: строка — посмотреть '
                      'позу, «Принять» — разместить скан.', 'Hint', wrap=True))
        self.finish_params()

    def set_scans(self, items, current):
        self.scan.clear()
        for sid, text in items:
            self.scan.addItem(text, sid)
        i = self.scan.findData(current)
        self.scan.setCurrentIndex(max(0, i))

    def scan_id(self):
        return self.scan.currentData()


# ── качество совмещения ────────────────────────────────────────────────────
class QualityPanel(ToolPanel):
    def __init__(self):
        super().__init__('Качество совмещения', 'Показать', 'mdi6.texture-box')
        p = self.params_sec
        self.method = W.Segmented([('по плоскостям', 'planes'), ('локально', 'local')], 'planes')
        self.method.changed.connect(lambda _: self._changed())
        p.add(W.field('Способ', self.method))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(5, 150)                   # мм / 10 → 0.5…15 см
        self.slider.setValue(30)
        self.slider.setFocusPolicy(Qt.NoFocus)
        self.slider.setToolTip('Подсвечивать места, где поверхность из разных сканов толще порога')
        self.value_l = W.label('3.0 см', 'KV_v')
        self.value_l.setFixedWidth(46)
        self.value_l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.slider.valueChanged.connect(lambda _: self._changed())
        p.add(W.hbox(W.label('порог'), self.slider, self.value_l, spacing=6))
        p.add(W.label('По плоскостям — общие стены, пол и потолок разных сканов (медианы по ячейкам 20 см); '
                      'локально — воксели 20 см, плоские у каждого скана. Порог только перекрашивает, '
                      'толщина считается один раз. Места — в нижней панели «Качество», щелчок по строке — '
                      'показать место. Слой включается и выключается глазом «Качество совмещения» в слоях.',
                      'Hint', wrap=True))
        self.info = W.label('', 'KV_v', wrap=True)
        p.add(self.info)
        self.finish_params()

    def _changed(self):
        self.value_l.setText(f'{self.slider.value() / 10:.1f} см')
        self.action.emit('params', None)

    def params(self):
        return self.slider.value() / 1000.0, self.method.value()

    def state(self):
        return {'thr': self.slider.value(), 'method': self.method.value()}

    def restore(self, d):
        if 'thr' in d:
            self.slider.setValue(int(d['thr']))
        if 'method' in d:
            self.method.set_value(d['method'])


# ── экспорт ────────────────────────────────────────────────────────────────
def _spin(value, lo, hi, step, dec=3):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(value)
    s.setAlignment(Qt.AlignRight)
    return s


class ExportPanel(ToolPanel):
    def __init__(self):
        super().__init__('Экспорт склейки', 'Экспортировать', 'mdi6.export-variant')
        p = self.params_sec
        self.what = QComboBox()
        self.what.activated.connect(lambda _: self.action.emit('params', None))
        p.add(W.field('Что экспортировать', self.what))
        self.fmt = W.Segmented([('E57', 'e57'), ('PLY', 'ply'), ('PCD', 'pcd')], 'e57')
        self.fmt.changed.connect(self._ext)
        p.add(W.field('Формат', self.fmt))
        self.voxel = _spin(0.01, 0.0, 0.5, 0.005)
        self.voxel.setSuffix(' м')
        p.add(W.hbox(W.label('воксель (0 — без прореживания)'), None, self.voxel))
        self.frame = QComboBox()
        self.frame.addItem('выровненная система: Z вверх (рекомендуется)', 'common')
        self.frame.addItem('исходная система опорного скана', 'ref')
        p.add(W.field('Система координат', self.frame))
        self.path = QLineEdit()
        b = W.tool(W.THEME.icon('mdi6.folder-outline'), tip='Выбрать…', cb=self._browse, icon_only=True)
        p.add(W.field('Файл', W.hbox(self.path, b, spacing=4)))
        p.add(W.label('Только размещённые сканы, в полном разрешении; чистка отражений, ручная чистка и удалённые '
                      'движущиеся объекты применяются.', 'Hint', wrap=True))
        self.finish_params()
        self.scopes = []

    def set_scopes(self, scopes, current=0, default_path=None):
        """scopes: [(подпись, список id или None)]."""
        self.scopes = scopes
        self.what.clear()
        for text, _ in scopes:
            self.what.addItem(text)
        self.what.setCurrentIndex(current)
        if default_path:
            self.path.setText(str(Path(default_path).with_suffix('.' + self.fmt.value())))

    def _ext(self, f):
        p = Path(self.path.text() or 'merged')
        self.path.setText(str(p.with_suffix('.' + f)))

    def _browse(self):
        p = QFileDialog.getSaveFileName(self, 'Файл экспорта', self.path.text())[0]
        if p:
            self.path.setText(p)
            self._ext(self.fmt.value()) if Path(p).suffix.lower() not in ('.e57', '.ply', '.pcd') else None

    def values(self):
        p = Path(self.path.text().strip())
        if p.suffix.lower() not in ('.e57', '.ply', '.pcd'):
            p = p.with_suffix('.' + self.fmt.value())
        i = self.what.currentIndex()
        return {'path': p, 'voxel': self.voxel.value(), 'frame': self.frame.currentData(),
                'scan_ids': self.scopes[i][1] if 0 <= i < len(self.scopes) else None}

    def state(self):
        return {'fmt': self.fmt.value(), 'voxel': self.voxel.value(), 'frame': self.frame.currentData()}

    def restore(self, d):
        if 'fmt' in d:
            self.fmt.set_value(d['fmt'])
        if 'voxel' in d:
            self.voxel.setValue(float(d['voxel']))
        i = self.frame.findData(d.get('frame'))
        if i >= 0:
            self.frame.setCurrentIndex(i)


# ── элементы (fits.py) ──────────────────────────────────────────────────────
def element_stats(el):
    """Сводка элемента для списка: размер, σ, отклонение формы, точек."""
    k = el['kind']
    if k == 'plane':
        size = f"площадь {el.get('area', 0):.1f} м²"
    elif k == 'cylinder':
        size = f"Ø {2 * el['params']['r']:.3f} м · длина {2 * el['params']['half']:.2f} м"
    else:
        size = f"Ø {2 * el['params']['r']:.3f} м"
    form = (el['span'][1] - el['span'][0]) * 1000
    return f"{size} · σ {el['sigma'] * 1000:.0f} мм · форма {form:.0f} мм · {el['n']} т."


class _ElementRow(QFrame):
    """Строка списка элементов: имя, сводка, переименовать / удалить."""

    def __init__(self, el, scan_name, panel):
        super().__init__()
        self.setObjectName('PairRow')
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 4, 6)
        lay.setSpacing(6)
        info = QVBoxLayout()
        info.setSpacing(1)
        name = QLabel(el.get('name', el['id']))
        name.setStyleSheet('font-size:12px; font-weight:600;')
        info.addWidget(name)
        a = QLabel(f'{scan_name} · {element_stats(el)}')
        a.setStyleSheet('font-size:11px;')
        a.setWordWrap(True)
        info.addWidget(a)
        lay.addLayout(info, 1)
        lay.addWidget(W.tool(W.THEME.icon('mdi6.pencil-outline', 'ink3'), tip='Переименовать',
                             icon_only=True, cb=lambda: panel.action.emit('rename', el['id'])))
        lay.addWidget(W.tool(W.THEME.icon('mdi6.close', 'ink3'), tip='Удалить элемент',
                             icon_only=True, cb=lambda: panel.action.emit('delete', el['id'])))


class FitPanel(_Panel):
    """Инструмент «Элемент»: подгонка плоскости / сферы / цилиндра / окружности
    по щелчку по облаку, список подогнанных элементов."""

    action = Signal(str, object)
    KINDS = [('авто', 'auto'), ('плоскость', 'plane'), ('сфера', 'sphere'),
             ('цилиндр', 'cylinder'), ('окружность', 'circle')]

    def __init__(self):
        super().__init__('Элемент', closable=True)
        from .tools import DoneBar                          # определён в этом же файле
        s = self.add(W.Section('Подгонка по щелчку'))
        self.kind = QComboBox()
        for text, v in self.KINDS:
            self.kind.addItem(text, v)
        self.kind.setToolTip('«авто» — плоская поверхность или изогнутая (сфера, '
                             'затем цилиндр) по виду поверхности под щелчком')
        self.kind.activated.connect(lambda _: self.action.emit('kind', self.kind.currentData()))
        s.add(W.field('Тип', self.kind))
        self.d_known = QCheckBox('известный диаметр — держать радиус')
        self.d_known.toggled.connect(lambda v: self._d_changed())
        s.add(self.d_known)
        self.d_val = QDoubleSpinBox()
        self.d_val.setRange(0.02, 100.0)
        self.d_val.setDecimals(3)
        self.d_val.setSingleStep(0.01)
        self.d_val.setValue(0.100)
        self.d_val.setSuffix(' м')
        self.d_val.setKeyboardTracking(False)
        self.d_val.setAlignment(Qt.AlignRight)
        self.d_val.setEnabled(False)
        self.d_val.valueChanged.connect(lambda _: self._d_changed())
        s.add(W.hbox(W.label('Ø'), None, self.d_val))
        self.hint = W.label('Щёлкните по поверхности облака — подгонится элемент; '
                            'σ и отклонение формы покажут, насколько поверхность ему соответствует. '
                            'Клик с перетаскиванием по-прежнему вращает вид.', 'Hint', wrap=True)
        s.add(self.hint)
        s1 = self.add(W.Section('Элементы'))
        self.list = QVBoxLayout()
        self.list.setSpacing(6)
        s1.add(self.list)
        self.empty = W.label('элементов нет', 'Hint', wrap=True)
        s1.add(self.empty)
        self.finish()
        b = self.add_foot(DoneBar(), 'done')
        b.done.connect(lambda: self.action.emit('done', None))
        self.focus(None)

    def _d_changed(self):
        self.d_val.setEnabled(self.d_known.isChecked())
        self.action.emit('diam', self.d_value())

    def d_value(self):
        """Известный диаметр (м) или None."""
        return self.d_val.value() if self.d_known.isChecked() else None

    def set_kind(self, v):
        i = self.kind.findData(v)
        if i >= 0:
            self.kind.setCurrentIndex(i)

    def set_elements(self, els, scan_names):
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for el in els:
            self.list.addWidget(_ElementRow(el, scan_names.get(el.get('scan'), '?'), self))
        self.empty.setVisible(not els)

    def state(self):
        return {'kind': self.kind.currentData(), 'd_known': self.d_known.isChecked(),
                'd': self.d_val.value()}

    def restore(self, d):
        if d.get('kind'):
            self.set_kind(d['kind'])
        if 'd_known' in d:
            self.d_known.setChecked(bool(d['d_known']))
        if 'd' in d:
            self.d_val.setValue(float(d['d']))


# ── чертёж сечения (drawing.py) ────────────────────────────────────────────
class DrawingPanel(ToolPanel):
    """Чертёж по срезу или проекцией выбранных элементов; SVG / DXF / PNG."""

    def __init__(self):
        super().__init__('Чертёж сечения', 'Построить', 'mdi6.file-document-outline')
        p = self.params_sec
        self.source = W.Segmented([('по срезу', 'slice'), ('проекция элементов', 'proj')], 'slice')
        self.source.changed.connect(lambda v: self.action.emit('source', v))
        p.add(W.field('Источник', self.source))
        # проекция: плоскость листа
        self.paper_box = W.Section('Плоскость листа (проекция)')
        self.paper = W.Segmented([('горизонт.', 'z'), ('по элементу', 'elem')], 'z')
        self.paper.changed.connect(lambda v: self.action.emit('paper', v))
        self.paper_box.add(W.field('Какая', self.paper))
        self.paper_el = QComboBox()
        self.paper_el.setToolTip('Плоскость листа встанет на выбранный элемент: '
                                 'фасад, линия забора — развёртка вдоль неё')
        self.paper_el.activated.connect(lambda _: self.action.emit('paper_el', None))
        self.paper_box.add(W.field('Элемент', self.paper_el))
        self.paper_note = W.label('', 'Hint', wrap=True)
        self.paper_box.add(self.paper_note)
        p.add(self.paper_box)
        self.paper_box.setVisible(False)
        # общий допуск
        self.tol = _spin(3.0, 2.0, 20.0, 1.0, 0)
        self.tol.setSuffix(' см')
        self.tol.setToolTip('Ячейка сетки занятости (допуск чертежа); не мельче шага точек')
        p.add(W.hbox(W.label('допуск'), None, self.tol))
        self.with_openings = QCheckBox('отмечать проёмы, которые пересекает срез')
        self.with_openings.setChecked(True)
        p.add(self.with_openings)
        self.with_grid = QCheckBox('сетка 1 м')
        self.with_grid.setChecked(True)
        p.add(self.with_grid)
        self.hint = W.label('', 'Hint', wrap=True)
        p.add(self.hint)
        self.finish_params()
        self.b_preview = W.button('Предпросмотр', lambda: self.action.emit('preview', None),
                                  icon=W.THEME.icon('mdi6.eye-outline'))
        self.b_svg = W.button('Сохранить SVG…', lambda: self.action.emit('svg', None),
                              icon=W.THEME.icon('mdi6.content-save-outline'))
        self.b_dxf = W.button('Сохранить DXF…', lambda: self.action.emit('dxf', None),
                              icon=W.THEME.icon('mdi6.content-save-outline'))
        self.b_png = W.button('Сохранить PNG…', lambda: self.action.emit('png', None),
                              icon=W.THEME.icon('mdi6.content-save-outline'))
        for b in (self.b_preview, self.b_svg, self.b_dxf, self.b_png):
            b.setEnabled(False)
            self.result_sec.add(b)
        self.result_sec.setVisible(True)

    def set_paper_elements(self, els):
        cur = self.paper_el.currentData()
        self.paper_el.blockSignals(True)
        self.paper_el.clear()
        for e in els:
            self.paper_el.addItem(f"{e.get('name', e['id'])}", e['id'])
        i = self.paper_el.findData(cur)
        self.paper_el.setCurrentIndex(max(0, i))
        self.paper_el.blockSignals(False)

    def paper_element_id(self):
        return self.paper_el.currentData() if self.paper.value() == 'elem' else None

    def values(self):
        return {'source': self.source.value(), 'paper': self.paper.value(),
                'paper_el': self.paper_element_id(),
                'tol': self.tol.value() / 100.0, 'openings': self.with_openings.isChecked(),
                'grid': self.with_grid.isChecked()}

    def set_source(self, src):
        self.paper_box.setVisible(src == 'proj')
        self.hint.setText(
            'Проекция: точки видимых элементов переносятся на плоскость листа ортогонально '
            '(не срезом) — участок с уклоном одной плоскостью сечения не ловится. Плоскости, '
            'параллельные листу (пол на плане), пропускаются: проектируются пятном.'
            if src == 'proj' else
            'Строится по режиму «Срез» (вкладка «Контроль»): горизонтальный — план этажа, '
            'вертикальный — разрез, по плоскости стены — развёртка фасада. Осевые линии, '
            'линии и дуги в истинном масштабе 1:1.')

    def set_sheet(self, sheet):
        """Результат построения: сводка и доступные кнопки."""
        if sheet is None:
            self.set_result('точек нет: для среза — подвинуите плоскость или увеличьте толщину; '
                            'для проекции — включите глаз у элементов в дереве')
            for b in (self.b_preview, self.b_svg, self.b_dxf, self.b_png):
                b.setEnabled(False)
            return
        n_ch, L, n_op = sheet['stats']
        extra = ''
        if sheet.get('skipped'):
            extra = f' · пропущены ({len(sheet["skipped"])}): {", ".join(sheet["skipped"][:3])}{"…" if len(sheet["skipped"]) > 3 else ""}'
        self.set_result(f'контуров {n_ch} · длина {L:.1f} м · проёмов {n_op} · '
                        f'допуск {sheet["cell"] * 100:.0f} см · {sheet["n_points"]} т.{extra}')
        for b in (self.b_preview, self.b_svg, self.b_dxf, self.b_png):
            b.setEnabled(True)

    def state(self):
        return {'source': self.source.value(), 'paper': self.paper.value(),
                'tol': self.tol.value(), 'openings': self.with_openings.isChecked(),
                'grid': self.with_grid.isChecked()}

    def restore(self, d):
        if d.get('source'):
            self.source.set_value(d['source'])
            self.paper_box.setVisible(d['source'] == 'proj')
        if d.get('paper'):
            self.paper.set_value(d['paper'])
        if 'tol' in d:
            self.tol.setValue(float(d['tol']))
        if 'openings' in d:
            self.with_openings.setChecked(bool(d['openings']))
        if 'grid' in d:
            self.with_grid.setChecked(bool(d['grid']))


# ── упаковка ───────────────────────────────────────────────────────────────
class PackPanel(ToolPanel):
    def __init__(self):
        super().__init__('Упаковать для передачи', 'Упаковать', 'mdi6.package-variant-closed')
        p = self.params_sec
        self.about = W.label('', 'Hint', wrap=True)
        p.add(self.about)
        self.kind = W.Segmented([('одним zip-файлом', 'zip'), ('папкой', 'folder')], 'zip')
        self.kind.changed.connect(self._kind)
        p.add(W.field('Как упаковать', self.kind))
        self.path = QLineEdit()
        b = W.tool(W.THEME.icon('mdi6.folder-outline'), tip='Выбрать…', cb=self._browse, icon_only=True)
        p.add(W.field('Куда', W.hbox(self.path, b, spacing=4)))
        self.note = W.label('', 'Hint', wrap=True)
        p.add(self.note)
        self.b_show = W.button('Показать в папке', lambda: self.action.emit('show', None),
                               icon=W.THEME.icon('mdi6.folder-outline'))
        self.result_sec.add(self.b_show)
        self.finish_params()
        self.name, self.base = 'проект', Path.home()

    def set_project(self, name, base, n_scans, scans_mb, missing):
        self.name, self.base = name, Path(base)
        text = (f'Будет собрано: проект .pulse, сканы ({n_scans} шт., ~{scans_mb:.0f} МБ) в папке scans/, кеш '
                f'анализа и ПРОЧТИ.txt. Получатель открывает .pulse — без пересчёта анализа. Сетки поверхности '
                f'не входят (их можно экспортировать отдельно).')
        if missing:
            text += (f' Не найдены файлы сканов ({len(missing)}): {", ".join(missing[:4])}'
                     f'{" …" if len(missing) > 4 else ""} — в пакет не войдут.')
        self.about.setText(text)
        self._kind(self.kind.value())

    def _kind(self, k):
        if k == 'zip':
            self.path.setText(str(self.base / f'{self.name}.zip'))
            self.note.setText('Один файл — удобно отправить. Получатель распаковывает его и открывает .pulse.')
        else:
            self.path.setText(str(self.base / f'{self.name}_передача'))
            self.note.setText('Новая (пустая) папка: проект и scans/ внутри — удобно сразу продолжать работу.')

    def _browse(self):
        if self.kind.value() == 'zip':
            p = QFileDialog.getSaveFileName(self, 'Zip-файл', self.path.text(), 'Zip (*.zip)')[0]
        else:
            p = QFileDialog.getExistingDirectory(self, 'Папка, в которой создать пакет', str(self.base))
            if p:
                p = str(Path(p) / f'{self.name}_передача')
        if p:
            self.path.setText(p)

    def values(self):
        p = Path(self.path.text().strip())
        zip_ = self.kind.value() == 'zip'
        if zip_ and p.suffix.lower() != '.zip':
            p = p.with_suffix('.zip')
        return {'path': p, 'zip': zip_}

    def state(self):
        return {'kind': self.kind.value()}

    def restore(self, d):
        if d.get('kind'):
            self.kind.set_value(d['kind'])
