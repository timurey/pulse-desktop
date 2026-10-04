"""Замеры и нулевой уровень (measure.py, Session.zero / measures)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
