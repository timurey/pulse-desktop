"""
Синтетика для fits.py: подгонка элемента по щелчку.

Облака строятся вокруг «сканера» в начале координат (каноническая система),
с шагом ~как у даунсемпла анализа (0.03–0.05 м) и шумом 3–5 мм.

Запуск:  .venv312/bin/python -m unittest tests.test_fits -v
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fits                                        # noqa: E402


def ang_between(a, b):
    return float(np.degrees(np.arccos(np.clip(abs(np.asarray(a) @ np.asarray(b)), 0, 1))))


class TestPlane(unittest.TestCase):
    def cloud(self, seed=1):
        rng = np.random.default_rng(seed)
        g = np.arange(-2.0, 2.0, 0.05)
        X, Y = np.meshgrid(g, g)
        P = np.column_stack([X.ravel(), Y.ravel(), np.full(X.size, 3.0)])
        P += rng.normal(0, 0.005, P.shape)
        out = np.concatenate([P, P[rng.choice(len(P), len(P) // 5)] +
                              rng.normal(0, 0.3, (len(P) // 5, 3))])   # 20 % выбросов
        return out[rng.permutation(len(out))]

    def test_outliers_rejected(self):
        P = self.cloud()
        el = fits.fit_click(P, [0.0, 0.0, 3.0], kind='auto')
        self.assertIsNotNone(el)
        self.assertEqual(el['kind'], 'plane')
        self.assertLess(ang_between(el['params']['n'], [0, 0, -1]), 1.0)   # на сканер
        self.assertAlmostEqual(el['params']['c'], -3.0, delta=0.01)        # n·x = c, c < 0
        self.assertAlmostEqual(el['sigma'], 0.005, delta=0.004)
        self.assertLess(el['n'], 7000)                                      # без выбросов
        self.assertGreater(el['n'], 5500)

    def test_flat_is_not_sphere(self):
        P = self.cloud(seed=2)
        self.assertIsNone(fits.fit_click(P, [0.0, 0.0, 3.0], kind='sphere'))


class TestSphere(unittest.TestCase):
    C = np.array([2.0, 0.0, 1.4])
    R = 0.5

    def cap(self, n=6000, noise=0.005, seed=3):
        rng = np.random.default_rng(seed)
        to_scanner = -self.C / np.linalg.norm(self.C)
        u = rng.normal(size=(n * 4, 3))
        u /= np.linalg.norm(u, axis=1)[:, None]
        u = u[u @ to_scanner > np.cos(np.radians(60))][:n]      # шапка к сканеру
        P = self.C + self.R * u + rng.normal(0, noise, (len(u), 3))
        return P, self.C + self.R * to_scanner                  # облако и точка щелчка

    def test_sphere_cap(self):
        P, seed = self.cap()
        el = fits.fit_click(P, seed, kind='auto')
        self.assertIsNotNone(el)
        self.assertEqual(el['kind'], 'sphere')
        self.assertAlmostEqual(el['params']['r'], self.R, delta=0.01)
        self.assertLess(np.linalg.norm(np.array(el['params']['c']) - self.C), 0.01)
        self.assertLess(el['sigma'], 0.01)

    def test_known_diameter(self):
        P, seed = self.cap()
        el = fits.fit_click(P, seed, kind='sphere', d_fixed=2 * self.R)
        self.assertIsNotNone(el)
        self.assertAlmostEqual(el['params']['r'], self.R, delta=1e-6)
        self.assertLess(np.linalg.norm(np.array(el['params']['c']) - self.C), 0.01)


class TestCylinder(unittest.TestCase):
    R = 0.3

    def wall(self, noise=0.003, seed=4):
        rng = np.random.default_rng(seed)
        th = np.radians(np.linspace(100, 260, 240))             # 160° к сканеру
        z = np.arange(0.5, 2.5, 0.03)
        TH, Z = np.meshgrid(th, z)
        P = np.column_stack([2.0 + self.R * np.cos(TH.ravel()), self.R * np.sin(TH.ravel()),
                             Z.ravel()])
        P += rng.normal(0, noise, P.shape)
        return P, np.array([2.0 - self.R, 0.0, 1.5])

    def test_cylinder(self):
        P, seed = self.wall()
        el = fits.fit_click(P, seed, kind='cylinder')
        self.assertIsNotNone(el)
        self.assertAlmostEqual(el['params']['r'], self.R, delta=0.01)
        self.assertLess(ang_between(el['params']['a'], [0, 0, 1]), 1.0)
        self.assertLess(el['sigma'], 0.01)


class TestCircle(unittest.TestCase):
    C = np.array([2.0, 0.0, 1.5])
    R = 0.4

    def ring(self, n=3000, noise=0.003, seed=5):
        rng = np.random.default_rng(seed)
        th = rng.uniform(0, 2 * np.pi, n)
        P = self.C + np.column_stack([self.R * np.cos(th), self.R * np.sin(th),
                                      np.zeros(n)])
        P += rng.normal(0, noise, P.shape)
        return P, self.C + np.array([self.R, 0.0, 0.0])

    def test_circle(self):
        P, seed = self.ring()
        el = fits.fit_click(P, seed, kind='circle')
        self.assertIsNotNone(el)
        self.assertEqual(el['kind'], 'circle')
        self.assertAlmostEqual(el['params']['r'], self.R, delta=0.01)
        self.assertLess(np.linalg.norm(np.array(el['params']['center']) - self.C), 0.01)
        self.assertLess(ang_between(el['params']['n'], [0, 0, 1]), 2.0)


class TestGrowStopsAtEdge(unittest.TestCase):
    """Пол x=3 у стены: область плоскости не должна перетекать на стену."""

    def test_no_leak_to_wall(self):
        rng = np.random.default_rng(6)
        g = np.arange(-2.0, 2.0, 0.05)
        X, Y = np.meshgrid(g, g)
        floor = np.column_stack([X.ravel(), Y.ravel(), np.full(X.size, 3.0)])
        zz = np.arange(3.0, 5.0, 0.05)
        Z, Y2 = np.meshgrid(zz, g)
        wall = np.column_stack([np.full(Z.size, 2.0), Y2.ravel(), Z.ravel()])
        P = np.concatenate([floor, wall])
        P += rng.normal(0, 0.005, P.shape)
        P = P[rng.permutation(len(P))]
        el = fits.fit_click(P, [1.7, 0.0, 3.0], kind='plane')
        self.assertIsNotNone(el)
        used_z = P[el['used']][:, 2]
        self.assertLess(used_z.max(), 3.05)                     # точки стены не вошли
        self.assertGreater(el['n'], 5500)                       # вся плоскость — вошла


if __name__ == '__main__':
    unittest.main()
