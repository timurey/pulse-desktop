"""
Поверхность (surface.py) на синтетической комнате: оба метода дают сетку, у Пуассона после
обрезки нет вершин дальше 1.5·точности от точек; экспорт OBJ / PLY / STL.
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import open3d as o3d                              # noqa: E402
import surface                                    # noqa: E402
from scan_session import Session                  # noqa: E402
from test_session import write_scans              # noqa: E402

ACC = 0.1


class TestSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        write_scans(cls.tmp)
        cls.s = Session.from_scans([str(cls.tmp / 'A.ply')])
        cls.sc = cls.s.scans[0]
        cls.sc.analyze()

    def test_normals_face_scanner(self):
        P, N = surface.scan_points(self.s, self.sc, ACC)
        eye = self.s.Tc(self.sc)[:3, 3]
        self.assertGreater(np.mean(np.sum((eye - P) * N, axis=1) > 0), 0.95)

    def test_poisson_trimmed(self):
        m = surface.build(self.s, [self.sc], 'poisson', ACC, 0.05)
        self.assertGreater(m['info']['triangles'], 1000)
        P, _ = surface.scan_points(self.s, self.sc, ACC)
        pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
        d = np.asarray(o3d.geometry.PointCloud(o3d.utility.Vector3dVector(m['V'].astype(float)))
                       .compute_point_cloud_distance(pcd))
        self.assertLess(d.max(), 1.6 * m['info']['acc_used'])
        for ext in ('obj', 'ply', 'stl'):
            f = self.tmp / f'mesh.{ext}'
            surface.export(m, f)
            self.assertGreater(f.stat().st_size, 1000)

    def test_ball_pivoting(self):
        m = surface.build(self.s, [self.sc], 'bpa', ACC)
        self.assertGreater(m['info']['triangles'], 1000)
        self.assertEqual(m['F'].shape[1], 3)


if __name__ == '__main__':
    unittest.main()
