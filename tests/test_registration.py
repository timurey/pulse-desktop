"""
Синтетика: лучевая «съёмка» комнаты с двух позиций → восстановление позы.

Запуск:  .venv/bin/python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import planes                                    # noqa: E402
import plane_register as pr                      # noqa: E402


# комната 9×6×3 м (пол z=0) + колонна + выступ, чтобы сцена была несимметричной
ROOM = (np.array([0.0, 0.0, 0.0]), np.array([9.0, 6.0, 3.0]))
OBSTACLES = [
    (np.array([6.0, 1.0, 0.0]), np.array([6.6, 1.6, 3.0])),     # колонна
    (np.array([0.0, 4.5, 0.0]), np.array([2.5, 6.0, 1.0])),     # шкаф/выступ
    (np.array([3.5, 0.0, 1.2]), np.array([5.0, 0.3, 2.2])),     # полка у стены
]
TRIPOD_H = 1.4
FLIP = np.diag([1.0, -1.0, -1.0])                # экспорт HMI: «вверх» = −Z


def ray_cast(origin, dirs):
    lo, hi = ROOM
    with np.errstate(divide='ignore', invalid='ignore'):
        t_room = np.where(dirs > 0, (hi - origin) / dirs, (lo - origin) / dirs)
    t = np.nanmin(np.where(t_room > 0, t_room, np.inf), axis=1)
    for blo, bhi in OBSTACLES:
        with np.errstate(divide='ignore', invalid='ignore'):
            t1 = (blo - origin) / dirs
            t2 = (bhi - origin) / dirs
        tmin = np.nanmax(np.minimum(t1, t2), axis=1)
        tmax = np.nanmin(np.maximum(t1, t2), axis=1)
        hit = (tmax >= tmin) & (tmin > 0)
        t = np.where(hit & (tmin < t), tmin, t)
    return origin + dirs * t[:, None]


def scan(pos_xy, yaw, n=400_000, seed=0):
    """Точки в локальной системе сканера (как в экспорте HMI, Z вниз)."""
    rng = np.random.default_rng(seed)
    d = rng.normal(size=(n, 3))
    d /= np.linalg.norm(d, axis=1)[:, None]
    d = d[d[:, 2] > -np.cos(np.radians(12))]      # мёртвый конус под штативом
    origin = np.array([pos_xy[0], pos_xy[1], TRIPOD_H])
    world = ray_cast(origin, d)
    world += rng.normal(scale=0.005, size=world.shape)
    local = (world - origin) @ pr.rot_z(-yaw).T   # мир → сканер (Z вверх)
    return local @ FLIP.T, origin


def gt_pose(oA, yawA, oB, yawB):
    """x_A = T · x_B в исходных (перевёрнутых) системах."""
    T_B_to_w = pr.make_T(pr.rot_z(yawB) @ FLIP.T, oB)
    T_w_to_A = np.linalg.inv(pr.make_T(pr.rot_z(yawA) @ FLIP.T, oA))
    return T_w_to_A @ T_B_to_w


def analyze(pts, name):
    up, _ = planes.detect_up(pts)
    R = planes.up_rotation(up)
    canon = pts @ R.T
    down, pl = planes.extract_planes(canon)
    return {'scan': name, 'up': up, 'R_up': R.tolist(),
            'layers': planes.horizontal_layers(canon), 'planes': pl, 'down': down}


class TestUp(unittest.TestCase):
    def test_up_rotation_is_proper(self):
        for lab in planes.AXES:
            R = planes.up_rotation(lab)
            i, s = planes.AXES[lab]
            up = np.zeros(3); up[i] = s
            np.testing.assert_allclose(R @ up, [0, 0, 1], atol=1e-9)
            self.assertAlmostEqual(np.linalg.det(R), 1.0, places=9)

    def test_detect_up_flipped(self):
        pts, _ = scan((3.0, 2.0), 0.4)
        self.assertEqual(planes.detect_up(pts)[0], '-z')


class TestSolve(unittest.TestCase):
    def test_solve_translation_planes(self):
        rng = np.random.default_rng(1)
        t_true = np.array([1.2, -0.7, 0.3])
        cons = []
        for n in ([1, 0, 0], [0, 1, 0], [0, 0, 1], [-1, 0, 0]):
            n = np.array(n, float)
            cB = rng.uniform(-3, -1)
            cons.append(('plane', n, cB + n @ t_true, n, cB, 1.0))
        t, info = pr.solve_translation(cons, np.eye(3))
        np.testing.assert_allclose(t, t_true, atol=1e-9)
        self.assertEqual(info['weak'], [])

    def test_corridor_is_degenerate(self):
        cons = [('plane', np.array([0, 1.0, 0]), -1.0, np.array([0, 1.0, 0]), -1.5, 1.0),
                ('plane', np.array([0, -1.0, 0]), -1.0, np.array([0, -1.0, 0]), -0.5, 1.0),
                ('plane', np.array([0, 0, 1.0]), -1.4, np.array([0, 0, 1.0]), -1.4, 1.0)]
        _, info = pr.solve_translation(cons, np.eye(3))
        self.assertEqual(len(info['weak']), 1)
        self.assertGreater(abs(info['weak'][0][0]), 0.99)   # вдоль коридора (X)


class TestRegisterPair(unittest.TestCase):
    CASES = [
        ((2.0, 1.8), 0.3, (6.8, 3.5), -1.1),
        # противоположные углы прямоугольной комнаты: симметрия на 180°
        ((8.5, 5.5), 0.7, (0.5, 0.5), 2.2),
        ((8.5, 0.5), 2.9, (0.5, 5.5), -0.3),
    ]

    def test_two_positions(self):
        for oA_xy, yawA, oB_xy, yawB in self.CASES:
            with self.subTest(A=oA_xy, B=oB_xy):
                A, oA = scan(oA_xy, yawA, seed=1)
                B, oB = scan(oB_xy, yawB, seed=2)
                resA, resB = analyze(A, 'A'), analyze(B, 'B')
                r = pr.register_pair(resA, resB, verbose=False)
                T_gt = gt_pose(oA, yawA, oB, yawB)
                dt, dang = pr.pose_delta(np.array(r['T']), T_gt)
                self.assertLess(dt, 0.03, r)
                self.assertLess(dang, 0.5, r)
                self.assertGreater(r['margin'], 0.05, r)


if __name__ == '__main__':
    unittest.main()
