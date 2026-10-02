"""Ручная чистка прямоугольником: экранная проекция ⇔ область плоскостей; сеанс."""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import manual_clean as mc                     # noqa: E402


def gl_camera(eye, target, up, fov=60, aspect=1.5, near=0.1, far=100):
    f = np.asarray(target, float) - eye
    f /= np.linalg.norm(f)
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    V = np.eye(4)
    V[:3, :3] = np.vstack([r, u, -f])
    V[:3, 3] = -V[:3, :3] @ eye
    t = 1 / np.tan(np.radians(fov) / 2)
    P = np.array([[t / aspect, 0, 0, 0], [0, t, 0, 0],
                  [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)],
                  [0, 0, -1, 0]])
    return V, P


def unproject(x, y, z_ndc, V, P, W, H):
    ndc = np.array([2 * x / W - 1, 1 - 2 * y / H, z_ndc, 1.0])
    p = np.linalg.inv(P @ V) @ ndc
    return p[:3] / p[3]


class TestManualClean(unittest.TestCase):
    def test_rect_equals_frustum(self):
        W, H = 600, 400
        V, P = gl_camera(np.array([3.0, -4.0, 5.0]), [0, 0, 0], [0, 0, 1], aspect=W / H)
        rng = np.random.default_rng(0)
        pts = rng.uniform(-4, 4, size=(20000, 3))
        rect = (180, 120, 420, 300)
        m_screen = mc.in_rect(pts, V, P, W, H, rect)
        corners = [(rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])]
        near = [unproject(x, y, -1.0, V, P, W, H) for x, y in corners]
        far = [unproject(x, y, 0.5, V, P, W, H) for x, y in corners]
        planes = mc.frustum_planes(near, far)
        m_planes = mc.inside_regions(pts, [planes])
        self.assertGreater(m_screen.sum(), 1000)
        self.assertLess(np.sum(m_screen != m_planes), 5)        # на границе — округление

    def test_planes_to_local(self):
        planes = mc.frustum_planes([[0, 0, 5], [1, 0, 5], [1, 1, 5], [0, 1, 5]],
                                   [[0, 0, -5], [1, 0, -5], [1, 1, -5], [0, 1, -5]])
        T = np.eye(4)
        c, s = np.cos(0.7), np.sin(0.7)
        T[:3, :3] = [[c, -s, 0], [s, c, 0], [0, 0, 1]]
        T[:3, 3] = [2.0, -1.0, 0.3]
        loc = mc.planes_to_local(planes, T)
        rng = np.random.default_rng(1)
        p_local = rng.uniform(-3, 3, size=(5000, 3))
        p_common = p_local @ T[:3, :3].T + T[:3, 3]
        np.testing.assert_array_equal(mc.inside_regions(p_common, [planes]),
                                      mc.inside_regions(p_local, [loc]))


class TestSessionErase(unittest.TestCase):
    def test_erase_save_undo(self):
        import open3d as o3d
        from scan_session import Session
        from test_registration import scan
        tmp = Path(tempfile.mkdtemp())
        pts, _ = scan((2.0, 1.8), 0.3, seed=1)
        o3d.io.write_point_cloud(str(tmp / 'A.ply'),
                                 o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts)))
        s = Session.from_scans([str(tmp / 'A.ply')])
        A = s.scans[0]
        n0 = len(A.down)
        # вертикальная призма 1×1 м в общей системе у сканера
        planes = mc.frustum_planes([[0, 0, 9], [1, 0, 9], [1, 1, 9], [0, 1, 9]],
                                   [[0, 0, -9], [1, 0, -9], [1, 1, -9], [0, 1, -9]])
        s.erase_region([(A, s.Tc(A))], planes)
        n1 = len(A.down)
        self.assertLess(n1, n0)
        s.save(tmp / 'p.json')
        s2 = Session.from_project(tmp / 'p.json')
        self.assertEqual(len(s2.scans[0].down), n1)
        s.undo_erase()
        self.assertEqual(len(A.down), n0)
        self.assertGreater(s2.export(tmp / 'e.ply', voxel=0.05), 0)


if __name__ == '__main__':
    unittest.main()
