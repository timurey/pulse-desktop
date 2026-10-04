"""
Пути с кириллицей (fileio.native_path): на Windows Open3D и pye57 их не открывают.
Обход включается принудительно (PULSE_FORCE_ASCII_PATHS=1) — та же логика проверяется на любой ОС.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fileio                                      # noqa: E402
import planes                                      # noqa: E402


class TestFileIO(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get('PULSE_FORCE_ASCII_PATHS')
        os.environ['PULSE_FORCE_ASCII_PATHS'] = '1'
        self.dir = Path(tempfile.mkdtemp()) / 'Рабочий стол' / 'Объект №1'

    def tearDown(self):
        if self.old is None:
            os.environ.pop('PULSE_FORCE_ASCII_PATHS', None)
        else:
            os.environ['PULSE_FORCE_ASCII_PATHS'] = self.old

    def test_clouds_roundtrip(self):
        P = np.random.default_rng(0).uniform(-5, 5, size=(2000, 3))
        for ext in ('.ply', '.pcd', '.e57'):
            f = self.dir / f'скан{ext}'
            fileio.write_cloud(f, P)
            self.assertTrue(f.exists(), ext)
            Q = planes.load_points(f)
            self.assertEqual(len(Q), len(P), ext)
            np.testing.assert_allclose(np.sort(Q[:, 0]), np.sort(P[:, 0]), atol=1e-4, err_msg=ext)
        self.assertFalse(list(fileio.ascii_tmpdir().glob('*.*')))      # временные файлы убраны

    def test_mesh_export(self):
        import surface
        V = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32)
        F = np.array([[0, 1, 2]], np.int32)
        for ext in ('.obj', '.ply', '.stl'):
            f = self.dir / f'сетка{ext}'
            surface.export({'V': V, 'F': F}, f)
            self.assertGreater(f.stat().st_size, 0, ext)


if __name__ == '__main__':
    unittest.main()
