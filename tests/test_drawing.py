"""
Чертёж сечения (drawing.py) на синтетической комнате 9×6×3.

Проверяется: контуры среза (длина стен ≈ периметр, замкнутость), колонна
(второй контур внутри), проём-маркер, экспорт SVG (парсится) и DXF (читается
ezdxf). Запуск: .venv312/bin/python -m unittest tests.test_drawing -v
"""

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import drawing                                     # noqa: E402
import section as sec                              # noqa: E402
from scan_session import Session                   # noqa: E402
from test_registration import scan                 # noqa: E402


def room_session(tmp):
    """Сеанс из одного скана комнаты (мир = канон. система, поза единичная)."""
    pts, _ = scan((4.5, 3.0), 0.0, seed=7)
    import fileio
    fileio.write_cloud(Path(tmp) / 'room.ply', pts)
    s = Session.from_scans([str(Path(tmp) / 'room.ply')])
    s.scans[0].pose = np.eye(4)
    return s


class TestDrawing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.s = room_session(cls.tmp)

    def _sheet(self, z=1.2):
        st = sec.default('z', z0=0.0)
        st.update(mode='slice', c=z, thick=0.30)
        return drawing.build(self.s, st)

    def test_contours(self):
        sheet = self._sheet()
        self.assertIsNotNone(sheet)
        n_ch, L, n_op = sheet['stats']
        per = 2 * (9.0 + 6.0)
        # осевые линии стен ≈ периметр, + колонна (две видимые грани ~1.2 м)
        self.assertGreater(L, per - 1.5)
        self.assertLess(L, per + 3.0)
        self.assertGreaterEqual(n_ch, 4)                   # 4 стены — 4 линии
        # габарит листа ≈ габарит комнаты (6×9, оси зависят от системы)
        lo, hi = sheet['bounds']
        dims = sorted([hi[0] - lo[0], hi[1] - lo[1]])
        self.assertAlmostEqual(dims[0], 6.0, delta=0.5)
        self.assertAlmostEqual(dims[1], 9.0, delta=0.5)

    def test_pieces_len(self):
        """Суммарная длина элементов (линии + дуги) ≈ периметру комнаты
        (пиксельный путь длиннее геометрии на лестнице, поэтому сравнение —
        с периметром, не с длиной цепочек)."""
        sheet = self._sheet()
        per = 2 * (9.0 + 6.0)
        tot_pc = 0.0
        for pieces in sheet['pieces']:
            for p in pieces:
                if p[0] == 'line':
                    tot_pc += float(np.linalg.norm(p[2] - p[1]))
                else:
                    _, a, b, c, _ = p
                    r = np.linalg.norm(a - c)
                    la = np.arctan2((a - c)[1], (a - c)[0])
                    lb = np.arctan2((b - c)[1], (b - c)[0])
                    tot_pc += r * float(np.abs((lb - la) % (2 * np.pi)))
        self.assertGreater(tot_pc, per - 2.0)
        self.assertLess(tot_pc, per + 3.0)

    def test_svg(self):
        sheet = self._sheet()
        out = Path(self.tmp) / 'plan.svg'
        drawing.write_svg(out, sheet, project='тест')
        root = ET.parse(out).getroot()
        self.assertEqual(root.tag[-3:], 'svg')
        ids = {g.get('id') for g in root.findall('{http://www.w3.org/2000/svg}g')}
        self.assertIn('contour', ids)
        self.assertIn('grid', ids)
        lines = root.findall('.//{http://www.w3.org/2000/svg}g[@id="contour"]/'
                             '{http://www.w3.org/2000/svg}line')
        self.assertGreater(len(lines), 3)                  # 4 стены как линии
        txt = out.read_text(encoding='utf-8')
        self.assertIn('масштаб 1:1', txt)

    def test_dxf(self):
        import ezdxf
        sheet = self._sheet()
        out = Path(self.tmp) / 'plan.dxf'
        drawing.write_dxf(out, sheet, project='тест')
        doc = ezdxf.readfile(out)
        msp = doc.modelspace()
        n_line = sum(1 for e in msp if e.dxftype() == 'LINE' and e.dxf.layer == 'contour')
        self.assertGreater(n_line, 3)
        self.assertGreater(len(msp.query('TEXT')), 0)

    def test_openings_marker(self):
        """Маркер проёма: синтетический проём в стене x=9."""
        import openings as op_mod
        sc = self.s.scans[0]
        wall = next(p for p in sc.res['planes'] if p.kind == 'wall' and
                    abs(np.asarray(p.normal)[0]) > 0.9)
        C = np.asarray(wall.centroid)
        op = op_mod.Opening(wall.id, 'door',
                            [[9.0, 2.0, 0.0], [9.0, 3.0, 0.0], [9.0, 3.0, 2.1], [9.0, 2.0, 2.1]],
                            [9.0, 2.5, 1.0], 1.0, 2.1, 0.0, [-1.0, 0.0, 0.0], 0.5, 0.9)
        sc._openings = [op]
        try:
            sheet = self._sheet(z=1.0)
            self.assertEqual(len(sheet['openings']), 1)
            o = sheet['openings'][0]
            self.assertIn('Дверь', o['label'])
            self.assertAlmostEqual(o['width'], 1.0, places=1)
        finally:
            sc._openings = None

    def test_empty_band(self):
        st = sec.default('z', z0=0.0)
        st.update(mode='slice', c=2.95, thick=0.05)        # над потолком
        self.assertIsNone(drawing.build(self.s, st))


class TestFrame(unittest.TestCase):
    """Ориентация листа: план — всегда «вид сверху» (X вправо, Y вверх),
    независимо от знака нормали; вертикальный срез — вид с +n."""

    def test_horizontal_is_map_view(self):
        for nz in (1.0, -1.0):
            u, v = drawing.plane_frame(np.array([0.0, 0.0, nz]))
            np.testing.assert_allclose(u, [1, 0, 0], atol=1e-9)
            np.testing.assert_allclose(v, [0, 1, 0], atol=1e-9)

    def test_vertical_is_side_view(self):
        n = np.array([1.0, 0.0, 0.0])
        u, v = drawing.plane_frame(n)
        self.assertAlmostEqual(abs(v[2]), 1.0, places=9)   # v — вверх
        self.assertAlmostEqual(np.cross(u, v) @ n, 1.0, places=9)  # правые (u, v, n)


class TestProjection(unittest.TestCase):
    """Чертёж проекцией: элементы комнаты на горизонтальный лист и на плоскость стены."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.s = room_session(cls.tmp)
        A = cls.s.scans[0]
        tripod = np.array([4.5, 3.0, 1.4])                 # мир → канон. система скана
        # элементы: пол + четыре стены (щелчки в стороне от препятствий)
        clicks = {'floor': (1.0, 1.0, 0.0), 'wall_x0': (0.0, 2.0, 1.5),
                  'wall_x9': (9.0, 3.0, 1.5), 'wall_y0': (1.5, 0.0, 1.5),
                  'wall_y6': (4.0, 6.0, 1.5)}
        cls.els = {}
        for name, w in clicks.items():
            p = (np.asarray(w, float) - tripod)            # yaw = 0
            el = cls.s.add_element(A, p, kind='plane')
            assert el is not None, name
            el['name'] = name
            cls.els[name] = el

    def test_site_plan(self):
        """Горизонтальная проекция: стены дают периметр (~30 м), пол пропускается."""
        sheet = drawing.build_projection(self.s, list(self.els.values()), paper='z')
        self.assertIsNotNone(sheet)
        self.assertIn('floor', sheet['skipped'])           # пол ∥ листу — пятном
        self.assertNotIn('wall_x0', sheet['skipped'])
        n_ch, L, _ = sheet['stats']
        self.assertGreater(L, 28.0 - 2.0)
        self.assertLess(L, 30.0 + 4.0)
        lo, hi = sheet['bounds']
        dims = sorted([hi[0] - lo[0], hi[1] - lo[1]])
        self.assertAlmostEqual(dims[0], 6.0, delta=0.8)
        self.assertAlmostEqual(dims[1], 9.0, delta=0.8)

    def test_paper_by_element(self):
        """Лист по плоскости стены x=0: стены x (параллельны листу) — пропущены;
        пол и стены y (перпендикулярны) — линии."""
        sheet = drawing.build_projection(self.s, list(self.els.values()),
                                         paper='elem', paper_el=self.els['wall_x0'])
        self.assertIsNotNone(sheet)
        sk = sheet['skipped']
        for name in ('wall_x0', 'wall_x9'):
            self.assertIn(name, sk)
        for name in ('floor', 'wall_y0', 'wall_y6'):
            self.assertNotIn(name, sk)
        n_ch, L, _ = sheet['stats']
        # линии стен y и пол «на ребре» сходятся углами — цепочки могут слиться
        # в один профиль; стены короче 9 м: рост обрывается на препятствиях
        self.assertGreater(L, 9.0)
        lo, hi = sheet['bounds']
        self.assertAlmostEqual(hi[1] - lo[1], 3.0, delta=0.4)   # высота комнаты

    def test_invisible_element_not_used(self):
        """Элемент с выключенным глазом в проекцию не попадает."""
        self.s.set_element_visible(self.els['wall_y6']['id'], False)
        try:
            sheet = drawing.build_projection(self.s, list(self.els.values()), paper='z')
            self.assertIn('wall_y6', sheet['skipped'])
        finally:
            self.s.set_element_visible(self.els['wall_y6']['id'], True)


if __name__ == '__main__':
    unittest.main()
