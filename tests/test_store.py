"""Проект-архив .pulse: кеш анализа, его валидность, совместимость."""
import os
import sys
import json
import time
import zipfile
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import open3d as o3d                       # noqa: E402
import project_store as ps                 # noqa: E402
import scan_project as sp                  # noqa: E402
from scan_session import Session           # noqa: E402
from test_registration import scan         # noqa: E402


class TestStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        for i, (xy, yaw) in enumerate([((2.0, 1.8), 0.3), ((6.8, 3.5), -1.1)]):
            pts, _ = scan(xy, yaw, seed=i + 1)
            o3d.io.write_point_cloud(str(cls.tmp / f'S{i}.ply'),
                                     o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts)))
        cls.paths = [str(cls.tmp / 'S0.ply'), str(cls.tmp / 'S1.ply')]

    def test_archive_cache_roundtrip(self):
        s = Session.from_scans(self.paths)
        s.run_auto()
        for sc in s.scans:
            sc.analyze()
        p = self.tmp / 'proj.pulse'
        s.save(p)
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
        self.assertIn('project.json', names)
        self.assertEqual(sum(n.endswith('arrays.npz') for n in names), 2)
        s2 = Session.from_project(p)
        self.assertEqual(s2.load_cached(), 2)
        for a, b in zip(s.scans, s2.scans):
            self.assertTrue(b.from_cache and b.analyzed)
            self.assertEqual(len(a.planes), len(b.planes))
            np.testing.assert_allclose(a.res['down'], b.res['down'], atol=1e-5)
            np.testing.assert_array_equal(a.ghost_mask(), b.ghost_mask())
            self.assertEqual(len(a.openings), len(b.openings))
            np.testing.assert_allclose(a.pose, b.pose)
            self.assertEqual(len(a.down), len(b.down))

    def test_cache_invalidated_by_file_change(self):
        s = Session.from_scans(self.paths)
        for sc in s.scans:
            sc.analyze()
        p = self.tmp / 'inv.pulse'
        s.save(p)
        time.sleep(0.01)
        os.utime(self.paths[0], None)                  # только дата — кеш остаётся (сумма та же)
        with open(self.paths[1], 'r+b') as f:          # содержимое изменилось при том же размере
            f.seek(-64, 2)
            b = f.read(1)
            f.seek(-64, 2)
            f.write(bytes([b[0] ^ 0xFF]))
        s2 = Session.from_project(p)
        self.assertEqual(s2.load_cached(), 1)
        self.assertTrue(s2.scans[0].from_cache)
        self.assertFalse(s2.scans[1].from_cache)
        with open(self.paths[1], 'r+b') as f:          # вернуть как было
            f.seek(-64, 2)
            f.write(b)

    def test_update_cache_keeps_project(self):
        s = Session.from_scans(self.paths[:1])
        p = self.tmp / 'upd.pulse'
        s.save(p)                                       # без анализа — кеша нет
        with zipfile.ZipFile(p) as z:
            before = z.read('project.json')
        s.frame = 'изменено-но-не-сохранено'
        s.scans[0].analyze()
        self.assertEqual(s.flush_cache(), 1)
        with zipfile.ZipFile(p) as z:
            self.assertEqual(z.read('project.json'), before)
            self.assertTrue(any(n.endswith('arrays.npz') for n in z.namelist()))

    def test_cli_and_json_compat(self):
        s = Session.from_scans(self.paths)
        for sc in s.scans:
            sc.analyze()
        p = self.tmp / 'cli.pulse'
        s.save(p)
        proj = sp.load_project(p)                       # командная строка читает архив
        self.assertTrue(Path(proj['scans'][0]['path']).exists())
        sp.save_project(proj, p)                        # и пишет, не теряя кеш
        self.assertEqual(len(ps.cached_ids(p)), 2)
        j = self.tmp / 'old.json'
        s.save(j)                                       # старый формат
        self.assertEqual(json.loads(j.read_text(encoding='utf-8'))['frame'], s.frame)
        self.assertEqual(Session.from_project(j).load_cached(), 0)


if __name__ == '__main__':
    unittest.main()
