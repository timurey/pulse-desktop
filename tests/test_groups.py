"""
Стыковка внутри групп и групп между собой (Session.within_group_pairs / register_groups).
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_session import Session                  # noqa: E402
from test_session import write_scans, POSES, pose_err   # noqa: E402


class TestGroups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.origins = write_scans(cls.tmp)
        cls.paths = [str(cls.tmp / n) for n in POSES]

    def _session(self):
        s = Session.from_scans(self.paths)
        g1 = s.tree.add_group('root', 'Комната', 'комната')
        g2 = s.tree.add_group('root', 'Коридор', 'комната')
        s.move_node('A.ply', g1)
        s.move_node('B.ply', g1)
        s.move_node('C.ply', g2)
        for sc in s.scans:
            sc.analyze()
        return s, g1, g2

    def test_within_then_between(self):
        s, g1, g2 = self._session()
        pairs = s.within_group_pairs()
        self.assertEqual({tuple(sorted(p)) for p in pairs}, {('A.ply', 'B.ply')})
        s.run_auto(combos=pairs)
        self.assertIsNotNone(s.by_id('B.ply').pose)
        self.assertIsNone(s.by_id('C.ply').pose)               # группы ещё не связаны
        out = s.register_groups()
        self.assertEqual(len(out), 1)
        e = out[0][2]
        self.assertEqual(e['method'], 'group')
        self.assertTrue(e['auto_ok'])
        C = s.by_id('C.ply')
        self.assertIsNotNone(C.pose)
        dt, da = pose_err(C.pose, 'C.ply', self.origins)
        self.assertLess(dt, 0.05)
        self.assertLess(da, 1.0)
        # связь групп сохраняется в проекте и переживает новую автостыковку внутри групп
        p = self.tmp / 'g.json'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertTrue(any(x.get('method') == 'group' for x in s2.edges))
        s2.run_auto(combos=s2.within_group_pairs())
        self.assertTrue(any(x.get('method') == 'group' for x in s2.edges))
        self.assertIsNotNone(s2.by_id('C.ply').pose)

    def test_selected_group_pairs(self):
        s, g1, _ = self._session()
        self.assertEqual(len(s.within_group_pairs(g1)), 1)
        with self.assertRaises(ValueError):
            s.register_groups(g1)                              # внутри «Комнаты» подгрупп нет


if __name__ == '__main__':
    unittest.main()
