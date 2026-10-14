"""
Запекание групп (baked.py, Session.bake / unbake): группа с зафиксированными внутренними
позами — один узел графа стыковки.
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
from test_session import write_scans, POSES, pose_err   # noqa: E402


class TestBake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.origins = write_scans(cls.tmp)
        cls.paths = [str(cls.tmp / n) for n in POSES]

    def _session(self):
        s = Session.from_scans(self.paths)
        g = s.tree.add_group('root', 'Комната', 'комната')
        s.move_node('A.ply', g)
        s.move_node('B.ply', g)
        for sc in s.scans:
            sc.analyze()
        s.run_auto(combos=s.within_group_pairs(g))
        return s, g

    def test_bake_register_unbake(self):
        s, g = self._session()
        A, B, C = (s.by_id(n) for n in POSES)
        rel0 = np.linalg.inv(s.Tc(A)) @ s.Tc(B)
        u = s.bake(g)
        self.assertEqual(u.id, f'group:{g}')
        self.assertEqual({x.id for x in s.units()}, {u.id, 'C.ply'})
        self.assertIs(s.unit_of('B.ply'), u)
        self.assertTrue(np.allclose(s.ref.pose, np.eye(4)))           # опорный скан в группе — на месте
        # автостыковка по узлам: одна пара (группа, C)
        self.assertEqual(s.auto_pairs('all'), [(u.id, 'C.ply')])
        r = s.run_auto(by_tree=False)
        self.assertEqual(r['pairs'], 1)
        dt, da = pose_err(C.pose, 'C.ply', self.origins)
        self.assertLess(dt, 0.05)
        self.assertLess(da, 1.0)
        # поза группы двигает все её сканы, взаимная поза не меняется
        T = s.Tc(u).copy()
        T[:3, 3] += [0.5, -0.2, 0.0]
        s.set_Tc(u, T)
        self.assertTrue(np.allclose(np.linalg.inv(s.Tc(A)) @ s.Tc(B), rel0, atol=1e-6))
        s.recompute_poses()
        self.assertTrue(np.allclose(np.linalg.inv(s.Tc(A)) @ s.Tc(B), rel0, atol=1e-6))
        # ручная поза группы → ребро к C и пересчёт по графу
        s.accept_pose(u, s.Tc(u), anchor=C)
        self.assertTrue(any(e.get('method') == 'manual' and e['B'] == u.id for e in s.edges))
        # сохранение и загрузка
        p = self.tmp / 'bake.pulse'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertIsNotNone(s2.tree.group(g).baked)
        self.assertEqual({x.id for x in s2.units()}, {u.id, 'C.ply'})
        # распекание: позы остаются, связи группы — на главном скане
        poses = {sc.id: sc.pose.copy() for sc in s.scans}
        root = u.root
        self.assertTrue(s.unbake(g))
        self.assertFalse(any(e['A'] == u.id or e['B'] == u.id for e in s.edges))
        self.assertTrue(any(e.get('method') == 'manual' and e['B'] == root for e in s.edges))
        for sc in s.scans:
            dt, da = pr.pose_delta(sc.pose, poses[sc.id])
            self.assertLess(dt, 0.02, sc.id)

    def test_cache(self):
        s, g = self._session()
        u = s.bake(g)
        u.analyze()
        n_planes = len(u.planes)
        p = self.tmp / 'cache.pulse'
        s.save(p)
        s2 = Session.from_project(p)
        s2.load_cached()
        u2 = s2.baked_unit(g)
        self.assertTrue(u2._load_cache())                  # анализ группы — из кеша проекта
        self.assertEqual(len(u2.planes), n_planes)
        # чистка скана группы меняет отпечаток — кеш не подходит
        sc = s2.by_id('A.ply')
        sc.erase = [np.array([[1.0, 0, 0, -100.0]] * 4)]
        u2._res = None
        self.assertFalse(u2._load_cache())

    def test_refuse(self):
        s, g = self._session()
        s.bake(g)
        with self.assertRaises(ValueError):
            s.move_node('C.ply', g)                        # в запечённую группу нельзя
        with self.assertRaises(ValueError):
            s.move_node('A.ply', 'root')                   # и из неё
        s3 = Session.from_scans(self.paths)
        g3 = s3.tree.add_group('root', 'Несвязанные', 'прочее')
        s3.move_node('A.ply', g3)
        s3.move_node('C.ply', g3)
        for sc in s3.scans:
            sc.analyze()
        with self.assertRaises(ValueError):
            s3.bake(g3)                                    # без связей внутри группы
        # удаление скана распекает его группу
        s.remove_scans(['B.ply'])
        self.assertIsNone(s.tree.group(g).baked)


if __name__ == '__main__':
    unittest.main()
