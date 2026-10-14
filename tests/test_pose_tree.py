"""
Дерево связей (Session.pose_tree): от какого узла скан получил позу, дополнительные связи
и их расхождение с итогом, причины, по которым скан не размещён.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_session import Session                  # noqa: E402
from test_session import write_scans, POSES       # noqa: E402


class TestPoseTree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        write_scans(cls.tmp)
        cls.paths = [str(cls.tmp / n) for n in POSES]

    def session(self):
        s = Session.from_scans(self.paths)
        for sc in s.scans:
            sc.analyze()
        s.run_auto(by_tree=False)
        return s

    def test_tree(self):
        s = self.session()
        t = s.pose_tree()
        self.assertEqual(t['ref'], s.frame)
        self.assertEqual(set(t['nodes']), {sc.id for sc in s.scans if sc.pose is not None})
        self.assertEqual(t['order'][0], s.frame)
        for n, nd in t['nodes'].items():
            if n == t['ref']:
                self.assertIsNone(nd['parent'])
                continue
            e = s.edges[nd['edge']]
            self.assertEqual({e['A'], e['B']}, {n, nd['parent']})
            self.assertTrue(s.edge_active(e))
            self.assertLess(t['order'].index(nd['parent']), t['order'].index(n))
            self.assertLess(t['links'][nd['edge']]['dt'], 0.05)
        n_act = len(s.active_edges())
        self.assertEqual(len(t['links']), n_act)
        self.assertEqual(sum(1 for l in t['links'].values() if l['tree']), len(t['nodes']) - 1)
        self.assertEqual(t['optimized'], n_act > len(t['nodes']) - 1)

    def test_manual_link_first(self):
        s = self.session()
        t = s.pose_tree()
        leaf = next(n for n in t['order'][1:])
        s.accept_pose(s.by_id(leaf), s.Tc(s.by_id(leaf)), anchor=s.ref)
        t = s.pose_tree()
        self.assertEqual(s.edges[t['nodes'][leaf]['edge']].get('method'), 'manual')
        self.assertEqual(t['nodes'][leaf]['parent'], s.frame)

    def test_unplaced(self):
        s = self.session()
        victim = next(sc.id for sc in s.scans if sc.id != s.frame)
        for e in s.edges:
            if victim in (e['A'], e['B']):
                e['user'] = 'reject'
        s.recompute_poses()
        t = s.pose_tree()
        self.assertNotIn(victim, t['nodes'])
        u = next(u for u in t['unplaced'] if u['id'] == victim)
        self.assertIn('отклонено вручную', u['reason'])
        self.assertTrue(u['edges'])
        s.edges = [e for e in s.edges if victim not in (e['A'], e['B'])]
        u = next(u for u in s.pose_tree()['unplaced'] if u['id'] == victim)
        self.assertIn('нет связей', u['reason'])

    def test_edge_reason(self):
        import scan_project as sp
        ok = {'score': 0.6, 'margin': 0.8, 'violations': 0.005, 'close': 0.7}
        self.assertTrue(sp.edge_reason(ok).startswith('принята'))
        self.assertIn('отрыв', sp.edge_reason(dict(ok, margin=0.05)))
        self.assertIn('оценка', sp.edge_reason(dict(ok, score=0.01)))
        self.assertIn('противоречий', sp.edge_reason(dict(ok, violations=0.1)))
        self.assertIn('сильная', sp.edge_reason(dict(ok, violations=0.03, close=0.9)))
        self.assertIn('циклы', sp.edge_reason(dict(ok, loop_rejected=True)))
        self.assertIn('малого перекрытия', sp.edge_reason(
            {'score': 0.04, 'margin': 0.5, 'violations': 0.005, 'close': 0.083, 'thick': 0.02, 'thick_cells': 300}))
        for r in (ok, dict(ok, margin=0.05), dict(ok, violations=0.1), dict(ok, violations=0.03, close=0.9)):
            self.assertEqual(sp.edge_reason(r).startswith('принята'), sp.edge_ok(r))


if __name__ == '__main__':
    unittest.main()
