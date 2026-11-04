"""Замеры и нулевой уровень (measure.py, Session.zero / measures)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np                                # noqa: E402
import measure as M                               # noqa: E402
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
