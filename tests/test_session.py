"""
Логика интерактивного инструмента (scan_session.py) на синтетических сканах.

Запуск:  .venv/bin/python -m unittest discover -s tests -v
"""
import sys
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import open3d as o3d                              # noqa: E402
import plane_register as pr                       # noqa: E402
from scan_session import Session                  # noqa: E402
from test_registration import scan, gt_pose, FLIP # noqa: E402


POSES = {'A.ply': ((2.0, 1.8), 0.3), 'B.ply': ((6.8, 3.5), -1.1), 'C.ply': ((4.5, 4.0), 2.0)}


def write_scans(tmp):
    origins = {}
    for i, (name, (xy, yaw)) in enumerate(POSES.items()):
        pts, o = scan(xy, yaw, seed=i + 1)
        o3d.io.write_point_cloud(str(Path(tmp) / name),
                                 o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts)))
        origins[name] = (o, yaw)
    return origins


def pose_err(T, name, origins, canon=False):
    """T — поза в исходных системах (scan.pose) или канонических (canon=True, Tc)."""
    oA, yA = origins['A.ply']
    o, y = origins[name]
    gt = gt_pose(oA, yA, o, y)
    if canon:                                   # синтетика: исходная = FLIP · каноническая
        F = pr.make_T(FLIP, np.zeros(3))
        gt = F @ gt @ F
    return pr.pose_delta(np.asarray(T), gt)


class TestSession(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.origins = write_scans(cls.tmp)
        cls.paths = [str(Path(cls.tmp) / n) for n in POSES]

    def test_auto_save_load(self):
        s = Session.from_scans(self.paths)
        s.run_auto()
        for sc in s.scans[1:]:
            dt, da = pose_err(sc.pose, sc.id, self.origins)
            self.assertLess(dt, 0.03, sc.id)
            self.assertLess(da, 0.5, sc.id)
        # отклонить ребро → скан может остаться связанным через другие
        idx = next(i for i, e in enumerate(s.edges) if {e['A'], e['B']} == {'A.ply', 'B.ply'})
        s.set_edge_user(idx, 'reject')
        self.assertFalse(s.edge_active(s.edges[idx]))
        p = Path(self.tmp) / 'p.json'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertEqual(s2.edges[idx].get('user'), 'reject')
        for a, b in zip(s.scans, s2.scans):
            np.testing.assert_allclose(a.pose, b.pose, atol=1e-9)

    def _canon_point(self, s, sc, world_xyz, name=None):
        """Мировая точка комнаты → канон. система скана (как будто клик по ней)."""
        o, yaw = self.origins[name or sc.id]
        local = (np.asarray(world_xyz) - o) @ pr.rot_z(-yaw).T      # Z вверх, начало — сканер
        return local                                               # канон. = локальная Z-вверх

    def test_manual_planes(self):
        s = Session.from_scans(self.paths[:2])
        A, B = s.scans
        # «клики»: пол, стена x=0, стена y=0 — в обоих сканах
        clicks = [(1.0, 1.0, 0.0), (0.0, 2.0, 2.0), (3.0, 0.0, 2.0)]
        pairs = []
        for w in clicks:
            fa = s.pick_feature(A, self._canon_point(s, A, w))
            fb = s.pick_feature(B, self._canon_point(s, B, w))
            self.assertEqual(fa['type'], 'plane', fa)
            self.assertEqual(fb['type'], 'plane', fb)
            pairs.append((fa, fb))
            st = s.dof_status(A, B, pairs)
        self.assertEqual(st['text'], 'всё определено')
        self.assertIn('не хватает', s.dof_status(A, B, pairs[:2])['text'])
        Tc, info = s.solve_manual(A, B, pairs)
        dt, da = pose_err(Tc, 'B.ply', self.origins, canon=True)
        self.assertLess(dt, 0.02)
        self.assertLess(da, 0.5)
        T2, _ = s.refine_icp([A], B, Tc)
        s.accept_pose(B, T2, anchor=A)
        dt, da = pose_err(B.pose, 'B.ply', self.origins)
        self.assertLess(dt, 0.02)

    def test_manual_points_and_nudge(self):
        s = Session.from_scans(self.paths[:2])
        A, B = s.scans
        # точки на углах колонны (точные координаты; kind='point')
        corners = [(6.0, 1.0, 2.0), (6.6, 1.6, 0.5), (2.5, 4.5, 1.0)]
        pairs = [({'type': 'point', 'scan': 'A.ply', 'p': self._canon_point(s, A, w).tolist()},
                  {'type': 'point', 'scan': 'B.ply', 'p': self._canon_point(s, B, w).tolist()})
                 for w in corners]
        self.assertEqual(s.dof_status(A, B, pairs)['text'], 'всё определено')
        Tc, _ = s.solve_manual(A, B, pairs)
        dt, da = pose_err(Tc, 'B.ply', self.origins, canon=True)
        self.assertLess(dt, 1e-6)
        T2 = Session.nudge(Tc, dx=0.1, dyaw_deg=5)
        dt, da = pr.pose_delta(Tc, T2)
        self.assertAlmostEqual(da, 5, places=6)

    def test_tilt_level_and_6dof(self):
        """Скан B с наклоном 3° (ошибка IMU): выравнивание и решение с полным поворотом."""
        tilt = pr.make_T(np.eye(3), np.zeros(3))
        a = np.radians(3.0)
        Rt = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]]) @ \
            np.array([[np.cos(a / 2), 0, np.sin(a / 2)], [0, 1, 0], [-np.sin(a / 2), 0, np.cos(a / 2)]])
        tilt[:3, :3] = Rt
        pts, _ = scan(POSES['B.ply'][0], POSES['B.ply'][1], seed=2)
        F = pr.make_T(FLIP, np.zeros(3))
        # наклон в канонической системе (Z вверх): канон = FLIP · исходная
        tilted = pr.transform(pts, F @ tilt @ F)
        p = Path(self.tmp) / 'Bt.ply'
        o3d.io.write_point_cloud(str(p), o3d.geometry.PointCloud(o3d.utility.Vector3dVector(tilted)))
        s = Session.from_scans([self.paths[0], str(p)])
        A, B = s.scans
        oA, yA = self.origins['A.ply']
        oB, yB = self.origins['B.ply']
        Tc_true = F @ gt_pose(oA, yA, oB, yB) @ F @ np.linalg.inv(tilt)
        # 1) «наивная» поза (скан считается ровным) + сдвиг → выравнивание даёт истинный поворот
        T_naive = F @ gt_pose(oA, yA, oB, yB) @ F
        T_naive[:3, 3] += [0.3, 0.0, 0.0]
        lev, info = s.level_pose(B, T_naive)
        R_err = lev[:3, :3] @ Tc_true[:3, :3].T
        err_deg = np.degrees(np.arccos(np.clip((np.trace(R_err) - 1) / 2, -1, 1)))
        self.assertLess(err_deg, 0.3, info)
        self.assertGreater(info['tilt_deg'], 3.0)
        np.testing.assert_allclose(lev[:3, 3], T_naive[:3, 3])     # вращение вокруг сканера
        # 2) пары «пол + две стены» + полный поворот → поза с наклоном
        clicks = [(1.0, 1.0, 0.0), (0.0, 2.0, 2.0), (3.0, 0.0, 2.0)]
        pairs = []
        for w in clicks:
            fa = s.pick_feature(A, self._canon_point(s, A, w))
            pb = pr.transform(self._canon_point(s, B, w, 'B.ply')[None], tilt)[0]
            fb = s.pick_feature(B, pb)
            pairs.append((fa, fb))
        Tc, info = s.solve_manual(A, B, pairs, T_init=np.eye(4), full=True)
        self.assertEqual(info['rotation'], '6dof')
        dt, da = pr.pose_delta(Tc, Tc_true)
        self.assertLess(dt, 0.03, info)
        self.assertLess(da, 0.3, info)

    def test_project_portable(self):
        """Пути сканов в проекте относительные: папку можно перенести (другая машина/ОС)."""
        import shutil
        src = Path(tempfile.mkdtemp()) / 'proj'
        shutil.copytree(self.tmp, src)
        s = Session.from_scans([str(src / n) for n in POSES])
        s.save(src / 'p.json')
        raw = json.loads((src / 'p.json').read_text(encoding='utf-8'))
        self.assertEqual(raw['scans'][0]['path'], 'A.ply')
        dst = Path(tempfile.mkdtemp()) / 'moved'
        shutil.move(str(src), dst)
        s2 = Session.from_project(dst / 'p.json')
        self.assertTrue(all(Path(sc.path).exists() for sc in s2.scans))

    def test_export(self):
        s = Session.from_scans(self.paths)
        s.run_auto()
        out = Path(self.tmp) / 'm.ply'
        n = s.export(out, voxel=0.05)
        self.assertGreater(n, 1000)
        self.assertTrue(out.exists())


if __name__ == '__main__':
    unittest.main()
