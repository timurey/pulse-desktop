"""
Качество совмещения (quality.py) на синтетической комнате: сдвиг одного скана
на 4 см по X должен дать «толстые» стены поперёк X и тонкие пол/потолок.
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality                                    # noqa: E402
from scan_session import Session                  # noqa: E402
from test_session import write_scans, POSES       # noqa: E402

SHIFT = 0.04


class TestQuality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        write_scans(cls.tmp)
        cls.s = Session.from_scans([str(Path(cls.tmp) / n) for n in list(POSES)[:2]])
        cls.s.run_auto()
        for sc in cls.s.scans:                     # в окне анализ всегда полный
            sc.analyze()
        assert all(sc.pose is not None for sc in cls.s.scans)

    def _by_normal(self, q):
        n = np.abs(q.normal)
        return q.thick[n[:, 0] > 0.9], q.thick[n[:, 2] > 0.9]

    def _shifted(self):
        B = self.s.scans[1]
        T0 = self.s.Tc(B)
        self.s.set_Tc(B, Session.nudge(T0, dx=SHIFT))
        return B, T0

    def test_aligned_is_thin(self):
        for method in ('planes', 'local'):
            q = quality.compute(self.s, method)
            self.assertGreater(len(q), 50, method)
            self.assertLess(np.median(q.thick), 0.015, method)

    def test_shift_shows_on_walls(self):
        B, T0 = self._shifted()
        try:
            for method in ('planes', 'local'):
                q = quality.compute(self.s, method)
                wx, fz = self._by_normal(q)
                self.assertGreater(len(wx), 5, method)
                self.assertAlmostEqual(float(np.median(wx)), SHIFT, delta=0.015, msg=method)
                self.assertLess(float(np.median(fz)), 0.015, method)
                # порог: подсвечиваются стены поперёк X, площади — по убыванию
                P, C = quality.colors(q, 0.025)
                self.assertGreater(len(P), 0)
                self.assertEqual(C.dtype, np.uint8)
                areas = quality.problem_areas(q, 0.025)
                self.assertTrue(areas)
                self.assertTrue(all(len(a['scans']) == 2 for a in areas))
                self.assertFalse(quality.problem_areas(q, 0.2))
        finally:
            self.s.set_Tc(B, T0)


if __name__ == '__main__':
    unittest.main()
