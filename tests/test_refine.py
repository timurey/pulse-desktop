"""
Уточнение стыковки на синтетической комнате: широкий ICP после грубой ручной стыковки,
ICP «только поворот вокруг опорной точки», толщина пары, вес принятой связи.
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import plane_register as pr                       # noqa: E402
from scan_session import Session                  # noqa: E402
from test_session import write_scans, POSES       # noqa: E402


class TestRefine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        write_scans(cls.tmp)
        cls.s = Session.from_scans([str(cls.tmp / n) for n in list(POSES)[:2]])
        cls.s.run_auto()
        for sc in cls.s.scans:
            sc.analyze()
        cls.A, cls.B = cls.s.scans
        cls.T_true = cls.s.Tc(cls.B).copy()

    def test_wide_icp_after_rough_manual(self):
        Tr = Session.nudge(self.T_true, dx=0.18, dy=-0.12, dz=0.05, dyaw_deg=2.0)   # «грубо вручную»
        th0, _ = self.s.pair_thickness([self.A], self.B, Tr)
        T2, info = self.s.refine_icp([self.A], self.B, Tr, wide=True)
        dt, da = pr.pose_delta(T2, self.T_true)
        self.assertLess(dt, 0.02)
        self.assertLess(da, 0.2)
        th1, n = self.s.pair_thickness([self.A], self.B, T2)
        self.assertGreater(th0, 0.05)
        self.assertLess(th1, 0.015)
        self.assertGreater(n, 50)

    def test_rotation_about_pivot(self):
        c = pr.transform(self.B.down, self.T_true)[len(self.B.down) // 2]
        Tb = Session.rotate_about(self.T_true, c, droll_deg=0.7, dpitch_deg=-0.5, dyaw_deg=1.5)
        p_loc = pr.transform(c[None], np.linalg.inv(Tb))[0]
        self.assertTrue(np.allclose(pr.transform(p_loc[None], Tb)[0], c))
        T2, _ = self.s.refine_icp([self.A], self.B, Tb, center=c)
        _, da = pr.pose_delta(T2, self.T_true)
        self.assertLess(da, 0.1)
        np.testing.assert_allclose(pr.transform(p_loc[None], T2)[0], c, atol=1e-9)   # точка на месте

    def test_accept_weight_saved(self):
        s = Session.from_scans([str(self.tmp / n) for n in list(POSES)[:2]])
        s.run_auto()
        A, B = s.scans
        e = s.accept_pose(B, s.Tc(B), anchor=A, method='автоподгонка', weight=10.0)
        self.assertEqual(e['weight'], 10.0)
        p = self.tmp / 'w.json'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertTrue(any(x.get('weight') == 10.0 for x in s2.edges))


if __name__ == '__main__':
    unittest.main()
