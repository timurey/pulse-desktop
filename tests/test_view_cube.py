"""Куб навигации: попадание мышью и направления привязки (без окна)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from view_cube import ViewCube, look_from, slerp_dir, FACES, EDGES   # noqa: E402


class TestViewCube(unittest.TestCase):
    def test_center_hits_facing_face(self):
        vc = ViewCube()
        for i, (n, _, _) in enumerate(FACES):
            R = look_from(n, up_hint=(0, 1, 0))
            self.assertEqual(vc.hit(R, 160, 160, 80, 80), ('face', i))

    def test_snap_directions(self):
        self.assertTrue(np.allclose(ViewCube.snap_direction(('face', 0)), [0, 0, 1]))
        for i in range(len(EDGES)):
            d = ViewCube.snap_direction(('edge', i))
            self.assertAlmostEqual(np.linalg.norm(d), 1.0)
            self.assertEqual(int(np.sum(np.abs(d) > 1e-6)), 2)          # ребро — 2 оси
        self.assertTrue(np.allclose(np.abs(ViewCube.snap_direction(('corner', 7))), 1 / np.sqrt(3)))

    def test_look_from_and_slerp(self):
        R = look_from([0, 0, 1], up_hint=[0, 1, 0])
        self.assertTrue(np.allclose(R[:, 2], [0, 0, 1]))
        self.assertTrue(np.allclose(R[:, 1], [0, 1, 0]))
        self.assertAlmostEqual(np.linalg.det(R), 1.0)
        m = slerp_dir([1, 0, 0], [0, 1, 0], 0.5)
        self.assertTrue(np.allclose(m, [np.sqrt(.5), np.sqrt(.5), 0]))
        self.assertTrue(np.isfinite(slerp_dir([1, 0, 0], [-1, 0, 0], 0.5)).all())

    def test_render(self):
        img = ViewCube().render(look_from([1, -1, 1]), 120, 120)
        self.assertEqual(img.size, (120, 120))


if __name__ == '__main__':
    unittest.main()
