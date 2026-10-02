"""Правая панель «Поверхность β»: построение сетки (surface.py), список построенных, экспорт."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel, QSlider

from . import widgets as W
from .inspector import _Panel


class _MeshRow(QFrame):
    def __init__(self, key, title, sub, visible, panel):
        super().__init__()
        self.setObjectName('PairRow')
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 4, 6)
        lay.setSpacing(6)
        t = W.THEME
        eye = W.tool(t.icon('mdi6.eye-outline' if visible else 'mdi6.eye-off-outline', 'ink3'),
                     tip='Показать / скрыть', icon_only=True,
                     cb=lambda: panel.action.emit('mesh_visible', (key, not visible)))
        lay.addWidget(eye)
        txt = QVBoxLayout()
        txt.setSpacing(0)
        a = QLabel(title)
        a.setStyleSheet('font-size:12px;')
        b = W.label(sub, 'Hint', wrap=True)
        txt.addWidget(a)
        txt.addWidget(b)
        lay.addLayout(txt, 1)
        lay.addWidget(W.tool(t.icon('mdi6.export-variant', 'ink3'), tip='Экспорт сетки…', icon_only=True,
                             cb=lambda: panel.action.emit('mesh_export', key)))
        lay.addWidget(W.tool(t.icon('mdi6.close', 'ink3'), tip='Удалить', icon_only=True,
                             cb=lambda: panel.action.emit('mesh_delete', key)))


class SurfacePanel(_Panel):
    action = Signal(str, object)

    def __init__(self):
        super().__init__('Поверхность β', closable=True)
        s0 = self.add(W.Section())
        s0.add(W.label('Экспериментальный режим: сетка строится по точкам полного разрешения без '
                       'отражений и удалённого; хранится только до закрытия проекта, сохраняется экспортом.',
                       'Hint', wrap=True))
        s1 = self.add(W.Section('Что строить'))
        self.scope = W.Segmented([('выбранный скан', 'scan'), ('видимые сканы', 'scene')], 'scan')
        s1.add(self.scope)
        self.target = W.label('', 'KV_v', wrap=True)
        s1.add(self.target)
        self.scope.changed.connect(lambda _: self.action.emit('scope', None))
        s2 = self.add(W.Section('Метод'))
        self.method = W.Segmented([('Пуассон', 'poisson'), ('Ball pivoting', 'bpa')], 'poisson')
        s2.add(self.method)
        self.acc = QSlider(Qt.Horizontal)
        self.acc.setRange(1, 20)
        self.acc.setValue(5)
        self.acc.setFocusPolicy(Qt.NoFocus)
        self.acc_v = W.label('5 см', 'KV_v')
        self.acc_v.setFixedWidth(42)
        self.acc.valueChanged.connect(lambda v: self.acc_v.setText(f'{v} см'))
        s2.add(W.hbox(W.label('точность'), self.acc, self.acc_v, spacing=6))
        self.trim = QSlider(Qt.Horizontal)
        self.trim.setRange(0, 30)
        self.trim.setValue(10)
        self.trim.setFocusPolicy(Qt.NoFocus)
        self.trim_v = W.label('10 %', 'KV_v')
        self.trim_v.setFixedWidth(42)
        self.trim.valueChanged.connect(lambda v: self.trim_v.setText(f'{v} %'))
        self.trim_row = W.hbox(W.label('обрезка'), self.trim, self.trim_v, spacing=6)
        self.trim_row.setToolTip('Пуассон: доля вершин с наименьшей плотностью, которые срезаются '
                                 '(«пузыри» над окнами и дырами)')
        s2.add(self.trim_row)
        self.method.changed.connect(lambda m: self.trim_row.setVisible(m == 'poisson'))
        self.hint = W.label('Пуассон — гладкая поверхность, дыры и окна затягиваются (лишнее срезается); '
                            'ball pivoting — строго по точкам, дыры остаются, медленнее. Мельче точность — '
                            'дольше: комната 5 см ~10 с, 2 см ~20 с; вся сцена Пуассоном 5 см ~1,5 мин. Ball pivoting — '
                            'для отдельного скана; больше 800 тыс. точек — точность загрубляется. После '
                            'построения точки сканов скрываются (слой «Точки сканов»).', 'Hint', wrap=True)
        s2.add(self.hint)
        self.b_build = W.button('Построить', lambda: self.action.emit('build', None), primary=True)
        s2.add(self.b_build)
        s3 = self.add(W.Section('Построенные'))
        self.list = QVBoxLayout()
        self.list.setSpacing(6)
        s3.add(self.list)
        self.empty = W.label('пока нет', 'Hint')
        s3.add(self.empty)
        self.sw_points = W.Switch(True)                   # то же, что слой «Точки сканов»
        self.sw_points.toggled.connect(lambda v: self.action.emit('points', v))
        s3.add(W.hbox(QLabel('Показывать точки сканов'), None, self.sw_points))
        self.finish()

    def params(self):
        return {'scope': self.scope.value(), 'method': self.method.value(), 'acc': self.acc.value() / 100.0,
                'trim': self.trim.value() / 100.0}

    def set_meshes(self, rows):
        """rows: [(ключ, заголовок, подробности, видима)]."""
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for key, title, sub, vis in rows:
            self.list.addWidget(_MeshRow(key, title, sub, vis, self))
        self.empty.setVisible(not rows)
