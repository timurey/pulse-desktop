"""Удаление сканов из проекта (Session.remove_scans)."""
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import plane_register as pr                       # noqa: E402
from scan_session import Session                  # noqa: E402
from test_session import write_scans, POSES       # noqa: E402


class TestRemove(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        write_scans(cls.tmp)
        cls.paths = [str(cls.tmp / n) for n in POSES]

    def _session(self):
        s = Session.from_scans(self.paths)
        s.run_auto()
        for sc in s.scans:
            sc.analyze()
        return s

    def test_remove_plain_and_saved(self):
        s = self._session()
        p = self.tmp / 'r.pulse'
        s.save(p)
        self.assertEqual(s.remove_scans(['C.ply']), ['C.ply'])
        self.assertIsNone(s.by_id('C.ply'))
        self.assertFalse(any('C.ply' in (e['A'], e['B']) for e in s.edges))
        self.assertNotIn('C.ply', [l.scan for l in s.tree.leaves()])
        s.save(p)
        with zipfile.ZipFile(p) as z:
            self.assertFalse(any('C.ply' in n for n in z.namelist()))      # кеш удалённого не перенесён
        s2 = Session.from_project(p)
        self.assertEqual([x.id for x in s2.scans], ['A.ply', 'B.ply'])

    def test_remove_reference(self):
        s = self._session()
        rel = np.linalg.inv(s.Tc(s.by_id('B.ply'))) @ s.Tc(s.by_id('C.ply'))
        s.remove_scans([s.frame])
        self.assertIn(s.frame, ('B.ply', 'C.ply'))
        self.assertTrue(all(x.pose is not None for x in s.scans))
        rel2 = np.linalg.inv(s.Tc(s.by_id('B.ply'))) @ s.Tc(s.by_id('C.ply'))
        dt, da = pr.pose_delta(rel, rel2)
        self.assertLess(dt, 0.02)                                         # взаимное положение то же
        self.assertLess(da, 0.2)


if __name__ == '__main__':
    unittest.main()
