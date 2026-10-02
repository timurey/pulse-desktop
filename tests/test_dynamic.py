"""
Движущиеся объекты (dynamic.py) на синтетической комнате: «человек» есть только на части
проходов одного скана (по полуоборотам) или только в одном из двух сканов (между сканами).
Удаление найденного (Session.drop_points): отмена, сохранение и загрузка проекта.
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import open3d as o3d                              # noqa: E402
import dynamic                                    # noqa: E402
import test_registration as tr                    # noqa: E402
from scan_session import Session                  # noqa: E402

PERSON = (np.array([4.0, 2.8, 0.0]), np.array([4.4, 3.2, 1.8]))


def in_box(P, box, pad=0.05):
    lo, hi = box
    return np.all((P >= lo - pad) & (P <= hi + pad), axis=1)


def with_person(fn):
    tr.OBSTACLES.append(PERSON)
    try:
        return fn()
    finally:
        tr.OBSTACLES.pop()                         # добавлен последним


def ray_pass(origin, n, seed, person):
    rng = np.random.default_rng(seed)
    d = rng.normal(size=(n, 3))
    d /= np.linalg.norm(d, axis=1)[:, None]
    d = d[d[:, 2] > -np.cos(np.radians(12))]
    cast = lambda: tr.ray_cast(origin, d)
    return with_person(cast) if person else cast()


class TestPassScores(unittest.TestCase):
    def test_person_on_two_of_twenty_passes(self):
        origin = np.array([2.0, 1.8, tr.TRIPOD_H])
        parts, pids = [], []
        for p in range(20):
            W = ray_pass(origin, 60000, p, person=p < 2)
            parts.append(W - origin)                     # система сканера: сканер в начале
            pids.append(p)
        P = np.vstack(parts)
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
        Pv = np.asarray(pc.voxel_down_sample(0.03).points)
        score, n = dynamic.pass_scores(parts, pids, Pv)
        self.assertEqual(n, 20)
        person = in_box(Pv + origin, PERSON, pad=0.02)
        self.assertGreater(person.sum(), 50)
        self.assertGreater(np.median(score[person]), 0.7)
        self.assertLess(np.percentile(score[~person], 99), 0.1)


class TestCrossScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        poses = {'A.ply': ((2.0, 1.8), 0.3, False), 'B.ply': ((6.8, 3.5), -1.1, True)}
        for i, (name, (xy, yaw, person)) in enumerate(poses.items()):
            make = lambda: tr.scan(xy, yaw, seed=i + 1)
            pts, _ = with_person(make) if person else make()
            o3d.io.write_point_cloud(str(cls.tmp / name), o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts)))
        cls.origins = {n: v for n, v in poses.items()}

    def _session(self):
        s = Session.from_scans([str(self.tmp / n) for n in ('A.ply', 'B.ply')])
        s.run_auto()
        for sc in s.scans:
            sc.analyze()
        self.assertTrue(all(sc.pose is not None for sc in s.scans))
        return s

    def _world(self, s, sc, P):
        """Канон. система скана → мир синтетики (по известной позиции сканера)."""
        (x, y), yaw, _ = self.origins[sc.id]
        return P @ tr.pr.rot_z(yaw).T + np.array([x, y, tr.TRIPOD_H])

    def test_person_found_and_dropped(self):
        s = self._session()
        A, B = s.scans
        m, info = dynamic.find(s, B, 0.6, use_cross=True, use_pass=True)
        self.assertIsNone(info['pass'])                  # у синтетики нет .dyn.npy
        W = self._world(s, B, B.res['down'])
        person = in_box(W, PERSON)
        self.assertGreater(person.sum(), 20)
        self.assertGreater(m[person].mean(), 0.6)        # большая часть «человека» найдена
        self.assertLess(m[~person].mean(), 0.01)         # стены почти не задеты
        mA, _ = dynamic.find(s, A, 0.6)
        self.assertLess(mA.mean(), 0.01)
        # удаление, отмена, сохранение
        n_before = len(B.down)
        n = s.drop_points([(B, B.res['down'][m])])
        self.assertGreater(n, 0)
        self.assertLess(len(B.down), n_before)
        self.assertFalse(dynamic.mask(B, 0.6).any())     # удалённое больше не находится
        p = self.tmp / 'p.json'
        s.save(p)
        s2 = Session.from_project(p)
        np.testing.assert_array_equal(s2.by_id('B.ply').drop, B.drop)
        s.undo_erase()
        self.assertEqual(len(B.drop), 0)
        self.assertEqual(len(B.down), n_before)


if __name__ == '__main__':
    unittest.main()
