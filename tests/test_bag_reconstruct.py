"""Реконструкция скана из кадров вращающейся платформы (синтетика, без bag-файлов)."""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bag_reconstruct as br                  # noqa: E402

DT = np.dtype([('x', 'f4'), ('y', 'f4'), ('z', 'f4'), ('time', 'f4')])
IDENT = dict(rotation_axis='x', angle_offset_deg=0.0, rotation_center=np.zeros(3),
             invert_rotation=False, encoder_time_offset_ms=0.0, mount_rpy_deg=[0, 0, 0],
             mount_axes=['+x', '+y', '+z'])


def rx(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def box_points(n, rng):
    """Точки на гранях коробки [-3,3]×[-2,4]×[-1.5,2.5] (система платформы при θ=0)."""
    lo, hi = np.array([-3, -2, -1.5]), np.array([3, 4, 2.5])
    P = rng.uniform(lo, hi, size=(n, 3))
    ax = rng.integers(0, 3, n)
    side = rng.integers(0, 2, n)
    P[np.arange(n), ax] = np.where(side, hi[ax], lo[ax])
    return P


class TestReconstruct(unittest.TestCase):
    def test_deskew_rotating_platform(self):
        rng = np.random.default_rng(0)
        omega = np.radians(240)                     # платформа 40 RPM
        t_ang = np.arange(0, 3.0, 0.01)             # энкодер 100 Гц
        ang = omega * t_ang
        frames = []
        for k in range(1, 29):                      # кадры лидара 10 Гц, stamp — конец кадра
            stamp = 0.1 * k
            Pw = box_points(3000, rng)
            dt = rng.uniform(-0.1, 0.0, len(Pw))    # время точки относительно stamp
            th = omega * (stamp + dt)
            Pl = np.einsum('nij,nj->ni', np.stack([rx(a) for a in th]), Pw)   # что видит лидар
            a = np.zeros(len(Pl), DT)
            a['x'], a['y'], a['z'], a['time'] = Pl[:, 0], Pl[:, 1], Pl[:, 2], dt
            frames.append((int(stamp * 1e9), a))
        P, st = br.process_frames(frames, t_ang * 1e9, ang, voxel=0, deskew_params=IDENT)
        self.assertEqual(st['frames_used'], 28)
        # каждая точка должна лечь на грань коробки
        lo, hi = np.array([-3, -2, -1.5]), np.array([3, 4, 2.5])
        d = np.minimum(np.abs(P - lo), np.abs(P - hi)).min(axis=1)
        self.assertLess(np.percentile(d, 99), 0.01)
        self.assertGreater(st['rotations'], 1.5)

    def test_skips_frames_without_encoder(self):
        a = np.zeros(10, DT)
        a['x'] = 1.0
        P, st = br.process_frames([(int(1e9), a), (int(5e9), a)],
                                  np.arange(0, 2.0, 0.01) * 1e9, np.zeros(200), voxel=0,
                                  deskew_params=IDENT)
        self.assertEqual(st['frames_used'], 1)      # кадр на 5 с — вне данных энкодера
        self.assertEqual(st['frames_skipped'], 1)

    def test_find_bags(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'b1').mkdir()
        (tmp / 'b1' / 'b1_0.mcap').write_bytes(b'')
        (tmp / 'b2.mcap').write_bytes(b'')
        (tmp / 'other').mkdir()
        self.assertEqual([p.name for p in br.find_bags(tmp)], ['b1', 'b2.mcap'])
        self.assertEqual(br.find_bags(tmp / 'b1'), [tmp / 'b1'])
        self.assertEqual(br.bag_name(tmp / 'b1' / 'b1_0.mcap'), 'b1')


if __name__ == '__main__':
    unittest.main()
