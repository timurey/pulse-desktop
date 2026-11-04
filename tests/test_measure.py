"""Замеры и нулевой уровень (measure.py, Session.zero / measures)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np                                # noqa: E402
import measure as M                               # noqa: E402
import measure_export                             # noqa: E402
import test_registration as tr                    # noqa: E402
from scan_session import Session                  # noqa: E402
from test_session import write_scans              # noqa: E402


class TestMeasure(unittest.TestCase):
    def test_values(self):
        d = M.dist([0, 0, 0], [3, 4, 1])
        self.assertAlmostEqual(d['length'], 26 ** 0.5)
        self.assertAlmostEqual(d['horizontal'], 5.0)
        self.assertAlmostEqual(d['dz'], 1.0)
        rect = M.polyline([[0, 0, 0], [4, 0, 0], [4, 3, 0], [0, 3, 0]], closed=True)
        self.assertAlmostEqual(rect['area'], 12.0)
        self.assertAlmostEqual(rect['length'], 14.0)
        self.assertEqual(rect['angles'], [90.0] * 4)
        tilted = M.polyline([[0, 0, 0], [2, 0, 0], [2, 1, 1], [0, 1, 1]], closed=True)
        self.assertAlmostEqual(tilted['area'], 2 * 2 ** 0.5)          # наклонный прямоугольник
        self.assertAlmostEqual(tilted['area_plan'], 2.0)              # в плане
        self.assertAlmostEqual(M.point_plane([1, 2, 3], [0, 0, 2], 1.0)['distance'], 2.0)
        self.assertEqual(M.elevation(2.45, 0), '+2.450')
        self.assertEqual(M.elevation(-0.15, 0), '−0.150')
        self.assertEqual(M.elevation(1e-4, 0), '±0.000')

    def test_zero_and_saved(self):
        tmp = Path(tempfile.mkdtemp())
        write_scans(tmp)
        s = Session.from_scans([str(tmp / 'A.ply')])
        s.scans[0].analyze()
        self.assertAlmostEqual(s.zero_z(), -tr.TRIPOD_H, delta=0.02)   # пол — на высоте штатива ниже
        s.measures.append({'type': 'height', 'pts': [[0, 0, 1.0]]})
        self.assertEqual(M.label(s.measures[0], s.zero_z())[:3], '+2.')
        sig = s.state_signature()
        s.zero = {'z': 0.5, 'source': 'manual'}
        self.assertNotEqual(sig, s.state_signature())
        p = tmp / 'm.json'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertEqual(s2.zero, {'z': 0.5, 'source': 'manual'})
        self.assertEqual(len(s2.measures), 1)
        self.assertAlmostEqual(s2.zero_z(), 0.5)


if __name__ == '__main__':
    unittest.main()


class TestSnapTargets(unittest.TestCase):
    """Цели привязки (measure.collect_snap_targets): вершины, середины, центр элемента."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp()
        cls.origins = write_scans(cls.tmp)                 # реальная комната (анализ ленивый, но рабочий)
        from scan_session import Session
        cls.paths = [str(Path(cls.tmp) / n) for n in cls.origins]
        cls.s = Session.from_scans(cls.paths[:1])

    def test_vertices_and_mids(self):
        self.s.measures = [{'type': 'dist', 'pts': [[0, 0, 0], [2, 0, 0]]}]
        got = M.collect_snap_targets(self.s)
        kinds = [k for _, k in got]
        self.assertEqual(kinds.count('vertex'), 2)
        self.assertEqual(kinds.count('mid'), 1)
        mid = [p for p, k in got if k == 'mid'][0]
        np.testing.assert_allclose(mid, [1.0, 0, 0])

    def test_element_center_first(self):
        """Центр элемента — высший приоритет, идёт раньше вершины замера."""
        self.s.elements = [{'id': 'el1', 'scan': self.s.scans[0].id, 'kind': 'circle',
                            'name': 'Окружность 1', 'visible': True,
                            'params': {'center': [3.0, 0.0, 1.5], 'n': [0, 0, 1], 'r': 0.4,
                                       'c_plane': 1.5},
                            'sigma': 0.005, 'span': [0, 0.01], 'n': 100, 'center': [3.0, 0.0, 1.5]}]
        self.s.measures = [{'type': 'dist', 'pts': [[3.0, 0.0, 1.5], [3.0, 0.0, 1.5]]}]
        got = M.collect_snap_targets(self.s)
        self.assertEqual(got[0][1], 'el-center')
        np.testing.assert_allclose(got[0][0], [3.0, 0.0, 1.5])
        # скрытый элемент — цели нет
        self.s.elements[0]['visible'] = False
        got = M.collect_snap_targets(self.s)
        self.assertFalse([k for _, k in got if k.startswith('el')])
        self.s.elements[0]['visible'] = True

    def test_poly_closed_mids(self):
        """Замкнутая ломаная: середина замыкающего ребра тоже цель."""
        self.s.measures = [{'type': 'poly', 'pts': [[0, 0, 0], [2, 0, 0], [2, 2, 0]], 'closed': True}]
        got = M.collect_snap_targets(self.s)
        mids = [p for p, k in got if k == 'mid']
        self.assertEqual(len(mids), 3)                     # 2 рёбра + замыкающее
        self.assertTrue(any(np.allclose(m, [1.0, 1.0, 0]) for m in mids))  # замыкающее 2,2→0,0


class TestMeasureEdit(unittest.TestCase):
    """Ф3/Ф4: H-отметка в подписи, числовая правка координат, name/note."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp()
        cls.origins = write_scans(cls.tmp)
        cls.paths = [str(Path(cls.tmp) / n) for n in cls.origins]
        cls.s = Session.from_scans(cls.paths[:1])

    def test_with_h_label(self):
        m = {'type': 'dist', 'pts': [[0, 0, 1.2], [3, 0, 1.2]]}
        self.assertNotIn('H=', M.label(m, z0=0.0))
        m['with_h'] = True
        lab = M.label(m, z0=0.0)
        self.assertIn('H=+1.200', lab)
        m['pts'][0][2] = -0.15
        self.assertIn('H=−0.150', M.label(m, z0=0.0))

    def test_edit_recomputes(self):
        """Правка координат вершины меняет значение замера."""
        m = {'type': 'dist', 'pts': [[0, 0, 0], [2, 0, 0]]}
        self.assertAlmostEqual(M.evaluate(m, 0.0)['length'], 2.0)
        m['pts'][1] = [5.0, 0.0, 0.0]
        self.assertAlmostEqual(M.evaluate(m, 0.0)['length'], 5.0)

    def test_name_note_roundtrip(self):
        self.s.measures = [{'type': 'dist', 'pts': [[0, 0, 0], [1, 0, 0]],
                            'name': 'Ширина зала', 'note': 'по плинтусам', 'with_h': True}]
        p = Path(self.tmp) / 'mn.pulse'
        self.s.save(p)
        from scan_session import Session
        s2 = Session.from_project(p)
        m = s2.measures[0]
        self.assertEqual(m['name'], 'Ширина зала')
        self.assertEqual(m['note'], 'по плинтусам')
        self.assertTrue(m['with_h'])
        # старый проект без name/note — .get() ничего не ломает
        self.assertIsNone(self.s.measures[0].get('zzz'))


class TestExportMeasures(unittest.TestCase):
    """Ф6/Ф7: CSV по вершинам, DXF слои IZM_* (паттерн test_drawing)."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp()
        cls.origins = write_scans(cls.tmp)
        cls.paths = [str(Path(cls.tmp) / n) for n in cls.origins]
        cls.s = Session.from_scans(cls.paths[:1])
        cls.s.measures = [
            {'type': 'dist', 'pts': [[0, 0, 0], [2, 0, 0]], 'name': 'Ширина'},
            {'type': 'height', 'pts': [[1.0, 1.0, 1.2]], 'note': 'подоконник'},
            {'type': 'poly', 'pts': [[0, 0, 0], [2, 0, 0], [2, 2, 0]], 'closed': True},
        ]

    def test_csv_rows(self):
        rows = measure_export.csv_rows(self.s, z0=0.0)
        # строка на каждую вершину: 2 + 1 + 3
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0][1], 'Ширина')
        self.assertEqual(rows[0][2], 'Расстояние')
        self.assertEqual(rows[0][3], 'P1')
        self.assertAlmostEqual(float(rows[0][7]), 2.0, places=3)      # значение на P1
        self.assertEqual(rows[1][7], '')                              # на P2 значение пусто
        self.assertAlmostEqual(float(rows[2][7]), 1.2, places=3)      # height
        self.assertEqual(rows[3][2], 'Площадь')
        self.assertAlmostEqual(float(rows[3][7]), 2.0, places=3)      # треугольник ½·2·2
        self.assertEqual(rows[2][9], 'подоконник')

    def test_write_csv(self):
        out = Path(self.tmp) / 'z.csv'
        measure_export.write_csv(self.s, out)
        raw = out.read_bytes()
        self.assertEqual(raw[:3], b'\xef\xbb\xbf')                    # utf-8-sig
        text = raw.decode('utf-8-sig')
        self.assertIn('Ширина', text)
        self.assertEqual(text.count('\r\n'), 7)                       # заголовок + 6

    def test_write_dxf(self):
        import ezdxf
        out = Path(self.tmp) / 'z.dxf'
        measure_export.write_dxf(self.s, out)
        doc = ezdxf.readfile(out)
        self.assertEqual(doc.header['$INSUNITS'], 6)                  # метры
        layers = {l.dxf.name: l.color for l in doc.layers}
        for name, aci in measure_export.DXF_LAYERS.items():
            self.assertEqual(layers.get(name), aci, name)
        msp = doc.modelspace()
        dists = [e for e in msp if e.dxf.layer == 'IZM_DIST' and e.dxftype() == 'LINE']
        self.assertEqual(len(dists), 1)
        areas = [e for e in msp if e.dxf.layer == 'IZM_AREA']
        self.assertEqual(len(areas), 1)                               # closed poly
        self.assertTrue(areas[0].closed)
        points = [e for e in msp if e.dxf.layer == 'IZM_POINT']
        self.assertEqual(len(points), 1)
        texts = [e.dxf.text for e in msp if e.dxf.layer == 'IZM_TEXT']
        self.assertTrue(any('Ширина = 2.000' in t for t in texts))
        self.assertTrue(any('2.000 м²' in t for t in texts))


class TestPlans(unittest.TestCase):
    """Ф8/Ф9: сохранённые планы, привязка замеров, отвязка при удалении, 2D-DXF."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.origins = write_scans(self.tmp)
        self.paths = [str(Path(self.tmp) / n) for n in self.origins]
        self.s = Session.from_scans(self.paths[:1])

    def test_plan_roundtrip_and_detach(self):
        st = {'mode': 'slice', 'base': 'z', 'n0': [0, 0, 1.0], 'a': 0.0, 'b': 0.0,
              'c': 1.2, 'thick': 0.1, 'flip': False}
        pl = self.s.add_plan('1 этаж', st)
        self.s.active_plan = pl['id']
        self.s.measures.append({'type': 'dist', 'pts': [[0, 0, 1.2], [1, 0, 1.2]], 'plan': pl['id']})
        self.s.measures.append({'type': 'dist', 'pts': [[0, 0, 0.0], [1, 0, 0.0]]})
        p = Path(self.tmp) / 'pl.pulse'
        self.s.save(p)
        from scan_session import Session
        s2 = Session.from_project(p)
        self.assertEqual(s2.active_plan, pl['id'])
        self.assertEqual(len(s2.plans), 1)
        self.assertAlmostEqual(s2.plans[0]['state']['c'], 1.2)
        # удаление плана: замер остаётся, отвязывается; активный сбрасывается
        self.assertTrue(s2.remove_plan('plan1'))
        self.assertIsNone(s2.active_plan)
        self.assertEqual(len(s2.measures), 2)
        self.assertFalse([m for m in s2.measures if m.get('plan')])

    def test_plan_dxf_flat(self):
        import ezdxf
        import measure_export
        st = {'mode': 'slice', 'base': 'z', 'n0': [0, 0, 1.0], 'a': 0.0, 'b': 0.0,
              'c': 1.2, 'thick': 0.1, 'flip': False}
        pl = self.s.add_plan('Этаж', st)
        self.s.measures.append({'type': 'dist', 'pts': [[0, 0, 1.2], [3, 0, 1.2]],
                                'name': 'Коридор', 'plan': pl['id']})
        out = Path(self.tmp) / 'pl.dxf'
        measure_export.write_plan_dxf(self.s, pl, out, with_contour=False)
        doc = ezdxf.readfile(out)
        self.assertEqual(doc.header['$INSUNITS'], 6)
        msp = doc.modelspace()
        lines = [e for e in msp if e.dxftype() == 'LINE' and e.dxf.layer == 'PLAN_MEAS']
        self.assertEqual(len(lines), 1)
        # плоский: z = 0 у всех вершин
        for e in msp:
            if e.dxftype() == 'LINE':
                self.assertAlmostEqual(e.dxf.start.z, 0.0, places=9)
                self.assertAlmostEqual(e.dxf.end.z, 0.0, places=9)
        texts = [e.dxf.text for e in msp if e.dxf.layer == 'PLAN_TEXT']
        self.assertTrue(any('Коридор = 3.000' in t for t in texts))


class TestAreaOps(unittest.TestCase):
    """Ф10: split_polygon / merge_polygons геометрия."""

    SQ = [[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]]

    def test_split(self):
        res = M.split_polygon(self.SQ, [1.0, -5.0, 0], [1.0, 5.0, 0])
        self.assertIsNotNone(res)
        a, b = res
        self.assertEqual(len(a), 4)
        self.assertEqual(len(b), 4)
        for part in (a, b):
            self.assertAlmostEqual(float(np.ptp(np.asarray(part)[:, 0])), 1.0, places=6)

    def test_split_not_crossing(self):
        self.assertIsNone(M.split_polygon(self.SQ, [5.0, -5.0, 0], [5.0, 5.0, 0]))

    def test_split_concave_rejected(self):
        L = [[0, 0, 0], [4, 0, 0], [4, 4, 0], [3, 1, 0], [2, 4, 0], [0, 4, 0]]
        self.assertIsNone(M.split_polygon(L, [2.0, -5.0, 0], [2.0, 5.0, 0]))

    def test_merge_common_edge(self):
        B = [[0, 0, 0], [0, 2, 0], [-2, 2, 0], [-2, 0, 0]]
        out = M.merge_polygons(self.SQ, B)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 6)                      # 4 + 4 − 2 общие вершины стыка
        area = M.evaluate({'type': 'poly', 'pts': [list(p) for p in out], 'closed': True}, 0.0)
        self.assertAlmostEqual(area['area'], 8.0, places=6)

    def test_merge_no_common_edge(self):
        B = [[10, 0, 0], [10, 2, 0], [8, 2, 0], [8, 0, 0]]
        self.assertIsNone(M.merge_polygons(self.SQ, B))

    def test_merge_symmetric(self):
        B = [[0, 0, 0], [0, 2, 0], [-2, 2, 0], [-2, 0, 0]]
        out1 = M.merge_polygons(self.SQ, B)
        out2 = M.merge_polygons(B, self.SQ)
        self.assertAlmostEqual(
            M.evaluate({'type': 'poly', 'pts': [list(p) for p in out2], 'closed': True}, 0.0)['area'], 8.0)
        self.assertEqual(len(out1), len(out2))
