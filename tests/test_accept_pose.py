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


    def test_rerun_drops_stale_user_flag(self):
        s = Session.from_scans(self.paths)
        for sc in s.scans:
            sc.analyze()
        s.run_auto(by_tree=False)
        e = next(e for e in s.edges if {e['A'], e['B']} == {'A.ply', 'B.ply'})
        good = np.asarray(e['T_canon']).copy()
        e['user'] = 'reject'                                   # решение по той же позе — переносится
        s.run_auto(by_tree=False, reuse=False)
        e = next(e for e in s.edges if {e['A'], e['B']} == {'A.ply', 'B.ply'})
        self.assertEqual(e.get('user'), 'reject')
        e['T_canon'] = (good @ pr.make_T(np.eye(3), [3.0, 0, 0])).tolist()
        e['user'] = 'accept'                                   # «принято» для другой (ошибочной) позы
        s.run_auto(by_tree=False, reuse=False)
        e = next(e for e in s.edges if {e['A'], e['B']} == {'A.ply', 'B.ply'})
        self.assertNotIn('user', e)
        self.assertEqual(s.auto_pairs('all', ['A.ply', 'C.ply']), [('A.ply', 'C.ply')])


    def test_thin_rule_and_hint(self):
        import scan_project as sp
        base = {'score': 0.04, 'margin': 0.5, 'violations': 0.005, 'close': 0.083}
        self.assertFalse(sp.edge_ok(base))                                   # без толщины — мало перекрытия
        self.assertTrue(sp.edge_ok(dict(base, thick=0.022, thick_cells=354)))
        self.assertFalse(sp.edge_ok(dict(base, thick=0.06, thick_cells=900)))   # толсто
        self.assertFalse(sp.edge_ok(dict(base, thick=0.02, thick_cells=40)))    # мало ячеек
        self.assertFalse(sp.edge_ok(dict(base, violations=0.03, thick=0.02, thick_cells=900)))
        self.assertFalse(sp.edge_ok(dict(base, close=0.02, thick=0.006, thick_cells=450)))  # клочок через проём
        # подсказка «текущая поза» — уточняется широкой подгонкой от сдвига на 40 см
        s = Session.from_scans(self.paths)
        for sc in s.scans:
            sc.analyze()
        s.run_auto(by_tree=False)
        A, B = s.by_id('A.ply'), s.by_id('B.ply')
        from scan_session import _clean_res
        good = np.linalg.inv(s.Tc(A)) @ s.Tc(B)
        hint = good @ pr.make_T(np.eye(3), [0.4, -0.2, 0.0])
        e = pr.register_pair(_clean_res(A), _clean_res(B), verbose=False, hints=[hint])
        self.assertLess(pr.pose_delta(np.asarray(e['T_canon']), good)[0], 0.03)
        self.assertIn(e['hypothesis'], ('текущая поза', 'плоскости'))
        th, n = s.pair_thick(A, B, good)
        self.assertGreater(n, 50)
        self.assertLess(th, 0.02)


if __name__ == '__main__':
    unittest.main()
