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
                               QLineEdit, QFileDialog, QDoubleSpinBox)

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
                         scopes=[('весь проект', 'project'), ('выбранная группа', 'group')], scope='project')
        p = self.params_sec
        self.method = QComboBox()
        for text, v in self.METHODS:
            self.method.addItem(text, v)
        self.method.activated.connect(lambda _: self.set_method(self.method.currentData(), emit=True))
        p.add(W.field('Какие пары', self.method))
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
