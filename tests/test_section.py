"""Сечение (section.py): нормаль с доворотом, маска среза и отсечения, плоскости VTK."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import section as S                               # noqa: E402


class TestSection(unittest.TestCase):
    def test_masks(self):
        P = np.array([[0, 0, 0.0], [0, 0, 2.4], [0, 0, 2.6], [0, 0, 2.52]])
        st = S.default('z', 0.0, 2.5)
        st['mode'] = 'clip'
        np.testing.assert_array_equal(S.keep_mask(P, st), [True, True, False, False])   # видно ниже 2.5
        st['flip'] = True
        np.testing.assert_array_equal(S.keep_mask(P, st), [False, False, True, True])
        st.update(mode='slice', thick=0.1)
        np.testing.assert_array_equal(S.keep_mask(P, st), [False, False, False, True])   # ±5 см
        st['mode'] = 'off'
        self.assertTrue(S.keep_mask(P, st).all())

    def test_rotation_axes(self):
        st = S.default('x')
        st['a'] = 90.0                                   # вертикальная: «a» — азимут вокруг Z
        np.testing.assert_allclose(S.normal(st), [0, 1, 0], atol=1e-9)
        st = S.default('z')
        st['b'] = 30.0                                   # горизонтальная: «b» — вокруг Y
        n = S.normal(st)
        self.assertAlmostEqual(float(np.degrees(np.arccos(n[2]))), 30.0, places=6)

    def test_vtk_planes_match_mask(self):
        rng = np.random.default_rng(0)
        P = rng.uniform(-5, 5, size=(2000, 3))
        st = S.default('y')
        st.update(mode='slice', c=1.0, thick=0.5, a=20.0, b=-10.0)
        keep = np.ones(len(P), bool)
        for o, n in S.vtk_planes(st):                    # VTK оставляет сторону, куда смотрит нормаль
            keep &= (P - o) @ n >= 0
        np.testing.assert_array_equal(keep, S.keep_mask(P, st))


if __name__ == '__main__':
    unittest.main()
