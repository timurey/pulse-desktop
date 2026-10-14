"""
«Принять позу» (Session.accept_pose): скан остаётся там, где его поставили, даже если у него
есть противоречащие связи (например, ошибочная автоматическая пара, принятая вручную) —
такие связи отключаются (reject_conflicts); manual_conflict предупреждает о них.
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


class TestAcceptPose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        write_scans(cls.tmp)
        cls.paths = [str(cls.tmp / n) for n in POSES]

    def test_wrong_accepted_pair(self):
        s = Session.from_scans(self.paths)
        for sc in s.scans:
            sc.analyze()
        s.run_auto(by_tree=False)
        A, B = s.by_id('A.ply'), s.by_id('B.ply')
        T_good = s.Tc(B).copy()
        # ошибочная пара A–B (5 м, 90°), принятая пользователем: B уезжает
        i = next(k for k, e in enumerate(s.edges) if {e['A'], e['B']} == {'A.ply', 'B.ply'})
        e = s.edges[i]
        bad = np.asarray(e['T_canon']) @ pr.make_T(pr.rot_z(np.pi / 2) if hasattr(pr, 'rot_z') else
                                                   np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1.0]]), [5, 0, 0])
        e['T_canon'] = bad.tolist()
        e['user'] = 'accept'
        s.recompute_poses()
        self.assertGreater(pr.pose_delta(s.Tc(B), T_good)[0], 0.1)        # граф тянет B к ошибочной паре
        # ручная поза B у A — на правильном месте: противоречащая пара отключается, B остаётся
        s.accept_pose(B, T_good, anchor=A)
        dt, da = pr.pose_delta(s.Tc(B), T_good)
        self.assertLess(dt, 0.02)
        self.assertLess(da, 0.3)
        self.assertEqual(e.get('user'), 'reject')
        self.assertIn(e, [c[0] for c in s.last_conflicts])
        # повторное «Принять пару» — предупреждение о расхождении с ручной связью
        self.assertIsNotNone(s.manual_conflict(i))


if __name__ == '__main__':
    unittest.main()
