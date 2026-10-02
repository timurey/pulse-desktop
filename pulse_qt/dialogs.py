"""Диалоги: импорт со сканера, импорт bag, экспорт склейки, простые вопросы."""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QLabel, QLineEdit,
                               QComboBox, QDoubleSpinBox, QScrollArea, QWidget, QPushButton, QFileDialog,
                               QButtonGroup)

from . import widgets as W
from . import bg

TILT_MODES = [('по полу и стенам', 'geometry'), ('по IMU (плата осью X вверх)', 'imu_x'),
              ('не исправлять', 'none')]


def _spin(value, lo, hi, step, dec=2, suffix=''):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(value)
    s.setButtonSymbols(QDoubleSpinBox.NoButtons)
    if suffix:
        s.setSuffix(suffix)
    return s


def _path_field(text, caption, parent, directory=True):
    e = QLineEdit(text)

    def browse():
        if directory:
            p = QFileDialog.getExistingDirectory(parent, caption, e.text() or str(Path.home()))
        else:
            p = QFileDialog.getSaveFileName(parent, caption, e.text())[0]
        if p:
            e.setText(p)
    b = W.tool(W.THEME.icon('mdi6.folder-outline'), tip='Выбрать…', cb=browse, icon_only=True)
    return e, W.hbox(e, b, spacing=4)


class _Modal(QDialog):
    def __init__(self, parent, title, icon):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QFrame()
        head.setObjectName('MHead')
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 12, 12, 12)
        hl.setSpacing(10)
        ic = QLabel()
        ic.setPixmap(W.THEME.icon(icon, 'ink').pixmap(20, 20))
        hl.addWidget(ic)
        hl.addWidget(W.label(title, 'MTitle'))
        hl.addStretch(1)
        self.head = hl
        lay.addWidget(head)
        self.body = QHBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        lay.addLayout(self.body, 1)
        foot = QFrame()
        foot.setObjectName('MFoot')
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 12, 16, 12)
        fl.setSpacing(8)
        self.hint = W.label('', 'Hint')
        fl.addWidget(self.hint, 1)
        self.b_cancel = W.button('Отмена', self.reject)
        self.b_ok = W.button('OK', self.accept, primary=True)
        fl.addWidget(self.b_cancel)
        fl.addWidget(self.b_ok)
        lay.addWidget(foot)


def _opts_panel():
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(14, 14, 14, 14)
    lay.setSpacing(12)
    return w, lay


class _ReconOptions:
    """Параметры реконструкции bag — общие для импорта со сканера и из папки."""

    def __init__(self, lay, group_default, dest_caption, dest_default, defaults, parent):
        lay.addWidget(W.label('РЕКОНСТРУКЦИЯ', 'STitle'))
        self.tilt = QComboBox()
        for text, _ in TILT_MODES:
            self.tilt.addItem(text)
        lay.addWidget(W.field('Наклон оси', self.tilt))
        self.voxel = _spin(defaults['voxel'], 0.002, 0.2, 0.005, 3)
        self.rmax = _spin(defaults['max_range'], 5, 200, 5, 0)
        g = QGridLayout()
        g.setSpacing(6)
        g.addWidget(W.field('Воксель, м', self.voxel), 0, 0)
        g.addWidget(W.field('Дальность, м', self.rmax), 0, 1)
        lay.addLayout(g)
        self.group = QLineEdit(group_default)
        lay.addWidget(W.field('Группа в дереве', self.group))
        self.dest, row = _path_field(dest_default, dest_caption, parent)
        lay.addWidget(W.field(dest_caption, row))
        self.auto = W.Switch(True)
        lay.addWidget(W.hbox(QLabel('Затем автостыковка'), None, self.auto))
        lay.addStretch(1)

    def values(self):
        return {'tilt': TILT_MODES[max(0, self.tilt.currentIndex())][1], 'voxel': self.voxel.value(),
                'max_range': self.rmax.value(), 'group': self.group.text().strip(),
                'dest': Path(self.dest.text().strip()), 'auto': self.auto.isChecked()}


class _BagRow(QFrame):
    toggled = Signal()

    def __init__(self, b, state):
        super().__init__()
        self.setObjectName('BagRow')
        self.b = b
        self.enabled_ = not b['recording']
        self.on = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)
        self.cb = QLabel()
        self.cb.setFixedSize(16, 16)
        lay.addWidget(self.cb)
        name = QLabel(b['name'])
        name.setObjectName('Mono')
        name.setStyleSheet('font-size: 12px;')
        lay.addWidget(name, 1)
        tm = QLabel(b['mtime'][11:16])
        tm.setObjectName('Mono')
        tm.setStyleSheet('font-size: 12px;')
        lay.addWidget(tm)
        sz = QLabel(b.get('size', ''))
        sz.setObjectName('Mono')
        sz.setStyleSheet('font-size: 12px;')
        sz.setMinimumWidth(72)
        sz.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(sz)
        if b['recording']:
            st = ('идёт запись', 'mut')
        elif state == 'скачан':
            st = ('скачан', 'ok')
        elif state == 'частично':
            st = ('частично', 'warn')
        else:
            st = ('новый', 'acc')
        c = W.chip(*st)
        c.setMinimumWidth(84)
        lay.addWidget(c)
        if not self.enabled_:
            self.setEnabled(False)
        self.setCursor(Qt.PointingHandCursor if self.enabled_ else Qt.ArrowCursor)
        self._paint()

    def set_on(self, on):
        self.on = on and self.enabled_
        self._paint()

    def _paint(self):
        t = W.THEME
        if self.on:
            self.cb.setStyleSheet(f"background:{t.q('accent').name()}; border-radius:4px;")
            self.cb.setPixmap(t.icon('mdi6.check', 'onaccent').pixmap(14, 14))
        else:
            self.cb.setStyleSheet(f"border:1px solid {t.q('line2').name()}; border-radius:4px;")
            self.cb.setPixmap(t.icon('mdi6.check', 'bg1').pixmap(1, 1))

    def mousePressEvent(self, ev):
        if self.enabled_ and ev.button() == Qt.LeftButton:
            self.set_on(not self.on)
            self.toggled.emit()


class ScannerImportDialog(_Modal):
    """Записи со сканера по дням съёмки, галочки, параметры загрузки и реконструкции."""

    def __init__(self, parent, host, dest_default, defaults):
        super().__init__(parent, 'Импорт со сканера', 'mdi6.access-point')
        self.resize(820, 560)
        self.bags = []
        self.rows = {}
        self.chosen = set()
        self.day = None
        self.host = QLineEdit(host)
        self.host.setFixedWidth(170)
        self.host.setObjectName('Mono')
        self.host.returnPressed.connect(self.refresh)
        self.dot = QLabel()
        self.dot.setFixedSize(7, 7)
        self.dot.setObjectName('LiveDotOff')
        self.count = W.label('', 'Lbl')
        self.head.addWidget(self.dot)
        self.head.addWidget(self.host)
        self.head.addWidget(self.count)
        self.head.addWidget(W.tool(W.THEME.icon('mdi6.refresh'), tip='Обновить список', cb=self.refresh,
                                   icon_only=True))
        left = QFrame()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        days = QFrame()
        days.setObjectName('Days')
        self.days_lay = QHBoxLayout(days)
        self.days_lay.setContentsMargins(12, 10, 12, 10)
        self.days_lay.setSpacing(6)
        ll.addWidget(days)
        self.list = QScrollArea()
        self.list.setWidgetResizable(True)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list_w = QWidget()
        self.list_lay = QVBoxLayout(self.list_w)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(0)
        self.list.setWidget(self.list_w)
        ll.addWidget(self.list, 1)
        sel = W.hbox(W.button('Отметить все', self.mark_all), W.button('Снять', self.unmark_all), None,
                     margins=(12, 8, 12, 8))
        ll.addWidget(sel)
        left.setStyleSheet('')
        self.body.addWidget(left, 1)
        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background:{W.THEME.q('line').name()};")
        self.body.addWidget(sep)
        opts, ol = _opts_panel()
        opts.setFixedWidth(250)
        self.opts = _ReconOptions(ol, '', 'Скачать в', dest_default, defaults, self)
        self.opts.dest.textChanged.connect(lambda _: self._fill())
        self.body.addWidget(opts)
        self.hint.setText('Докачка при обрыве · Esc в окне — остановить загрузку')
        self.b_ok.setIcon(W.THEME.icon('mdi6.download', 'onaccent'))
        self._update_ok()
        self.refresh()

    def refresh(self):
        from scanner_client import ScannerClient
        host = self.host.text().strip() or 'pulse.local'
        self.count.setText('подключение…')
        self.dot.setObjectName('LiveDotOff')
        W.restyle(self.dot)
        self.client = ScannerClient(host)
        client = self.client
        bg.run(client.bags, lambda bags: self._got(client, bags), self._fail)

    def _fail(self, e):
        self.count.setText('нет связи')
        self.hint.setText(str(e))
        self.bags = []
        self._days()
        self._fill()

    def _got(self, client, bags):
        if client is not self.client:
            return
        self.bags = bags
        self.dot.setObjectName('LiveDot')
        W.restyle(self.dot)
        self.count.setText(f'{len(bags)} записей')
        self.hint.setText('Докачка при обрыве · Esc в окне — остановить загрузку')
        self._days()
        self._fill()

    def _days(self):
        while self.days_lay.count():
            w = self.days_lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        days = sorted({b['mtime'][:10] for b in self.bags}, reverse=True)
        if self.day not in days:
            self.day = days[0] if days else None
            if self.day:
                self.opts.group.setText(self.day)
        self._day_group = QButtonGroup(self)
        for d in days[:8]:
            n = sum(b['mtime'].startswith(d) for b in self.bags)
            b = QPushButton(f'{d} · {n}')
            b.setProperty('day', True)
            b.setCheckable(True)
            b.setChecked(d == self.day)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, d=d: self._set_day(d))
            self._day_group.addButton(b)
            self.days_lay.addWidget(b)
        self.days_lay.addStretch(1)

    def _set_day(self, d):
        self.day = d
        self.opts.group.setText(d)
        self._fill()

    def _fill(self):
        from scanner_client import local_state
        while self.list_lay.count():
            w = self.list_lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.rows = {}
        dest = self.opts.dest.text().strip()
        for b in self.bags:
            if self.day and not b['mtime'].startswith(self.day):
                continue
            st = local_state(b['name'], dest, b.get('size_b')) if dest else ''
            r = _BagRow(b, st)
            r.set_on(b['name'] in self.chosen)
            r.toggled.connect(lambda r=r: self._toggle(r))
            self.rows[b['name']] = r
            self.list_lay.addWidget(r)
        if not self.rows:
            e = W.label('Записей нет' if self.bags or self.count.text() != 'нет связи'
                        else 'Сканер недоступен. Проверьте адрес и сеть.', 'Hint')
            e.setAlignment(Qt.AlignCenter)
            e.setContentsMargins(0, 40, 0, 40)
            self.list_lay.addWidget(e)
        self.list_lay.addStretch(1)
        self._update_ok()

    def _toggle(self, r):
        (self.chosen.add if r.on else self.chosen.discard)(r.b['name'])
        self._update_ok()

    def mark_all(self):
        for n, r in self.rows.items():
            if r.enabled_:
                r.set_on(True)
                self.chosen.add(n)
        self._update_ok()

    def unmark_all(self):
        for n, r in self.rows.items():
            r.set_on(False)
            self.chosen.discard(n)
        self._update_ok()

    def _names(self):
        return [b['name'] for b in self.bags if b['name'] in self.chosen and not b['recording']]

    def _update_ok(self):
        names = set(self._names())
        mb = sum((b.get('size_b') or 0) for b in self.bags if b['name'] in names) / 1e6
        self.b_ok.setText(f'Скачать и импортировать · {len(names)} · {mb:.1f} МБ')
        self.b_ok.setEnabled(bool(names))

    def values(self):
        v = self.opts.values()
        v['names'] = self._names()
        v['client'] = self.client
        v['host'] = self.host.text().strip() or 'pulse.local'
        return v


class BagImportDialog(_Modal):
    def __init__(self, parent, bags, group_default, out_default, defaults):
        import bag_reconstruct as br
        super().__init__(parent, 'Импорт bag', 'mdi6.folder-open-outline')
        self.resize(720, 440)
        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setFrameShape(QFrame.NoFrame)
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(14, 14, 14, 14)
        ll.setSpacing(4)
        ll.addWidget(W.label(f'НАЙДЕНО BAG: {len(bags)}', 'STitle'))
        for b in bags:
            l = QLabel(br.bag_name(b))
            l.setObjectName('Mono')
            l.setToolTip(str(b))
            ll.addWidget(l)
        ll.addStretch(1)
        left.setWidget(lw)
        self.body.addWidget(left, 1)
        opts, ol = _opts_panel()
        opts.setFixedWidth(260)
        self.opts = _ReconOptions(ol, group_default, 'Папка сканов', out_default, defaults, self)
        self.body.addWidget(opts)
        self.b_ok.setText(f'Импортировать · {len(bags)}')
        self.hint.setText('Уже реконструированные с теми же параметрами берутся из папки сканов')

    def values(self):
        return self.opts.values()


class ExportDialog(_Modal):
    def __init__(self, parent, scopes, default_path):
        """scopes: [(подпись, список id или None)]."""
        super().__init__(parent, 'Экспорт склейки', 'mdi6.export-variant')
        self.resize(520, 360)
        w, lay = _opts_panel()
        self.scopes = scopes
        self.scope = QComboBox()
        for text, _ in scopes:
            self.scope.addItem(text)
        lay.addWidget(W.field('Что экспортировать', self.scope))
        self.voxel = _spin(0.01, 0.0, 0.5, 0.005, 3)
        self.fmt = QComboBox()
        for f in ('E57', 'PLY', 'PCD'):
            self.fmt.addItem(f)
        g = QGridLayout()
        g.setSpacing(6)
        g.addWidget(W.field('Воксель, м (0 — без прореживания)', self.voxel), 0, 0)
        g.addWidget(W.field('Формат', self.fmt), 0, 1)
        lay.addLayout(g)
        self.frame = QComboBox()
        self.frame.addItem('выровненная система: Z вверх (рекомендуется)', 'common')
        self.frame.addItem('исходная система опорного скана', 'ref')
        lay.addWidget(W.field('Система координат', self.frame))
        self.path, row = _path_field(default_path, 'Файл', self, directory=False)
        lay.addWidget(W.field('Файл', row))
        self.fmt.currentTextChanged.connect(self._ext)
        lay.addStretch(1)
        self.body.addWidget(w, 1)
        self.b_ok.setText('Экспорт')
        self.hint.setText('Только размещённые сканы; чистка отражений и ручная чистка применяются')

    def _ext(self, f):
        p = Path(self.path.text() or 'merged')
        self.path.setText(str(p.with_suffix('.' + f.lower())))

    def values(self):
        p = Path(self.path.text().strip())
        if p.suffix.lower() not in ('.e57', '.ply', '.pcd'):
            p = p.with_suffix('.' + self.fmt.currentText().lower())
        return {'path': p, 'voxel': self.voxel.value(), 'frame': self.frame.currentData(),
                'scan_ids': self.scopes[self.scope.currentIndex()][1]}


class AskDialog(_Modal):
    """Простой вопрос: поля [(подпись, 'text'|'combo', значение, варианты)]."""

    def __init__(self, parent, title, fields, icon='mdi6.pencil-outline', ok='OK'):
        super().__init__(parent, title, icon)
        w, lay = _opts_panel()
        self.ws = []
        for caption, kind, value, options in fields:
            if kind == 'text':
                e = QLineEdit(value or '')
            else:
                e = QComboBox()
                for o in options:
                    e.addItem(o)
                if value in options:
                    e.setCurrentText(value)
            self.ws.append((kind, e))
            lay.addWidget(W.field(caption, e))
        lay.addStretch(1)
        self.body.addWidget(w, 1)
        self.b_ok.setText(ok)
        self.resize(420, 120 + 64 * len(fields))

    def values(self):
        return [e.text() if k == 'text' else e.currentText() for k, e in self.ws]
