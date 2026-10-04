"""
Передача проекта (Session.pack): папка и zip; после переноса и распаковки в другое место
проект открывается с теми же позами, замерами и кешем анализа (без пересчёта).
"""
import os
import sys
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_session import Session                  # noqa: E402
from test_session import write_scans, POSES       # noqa: E402


class TestPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        src = cls.tmp / 'где-то' / 'сканы'
        src.mkdir(parents=True)
        write_scans(src)
        (src / 'A.json').write_text('{"name": "A"}', encoding='utf-8')
        cls.s = Session.from_scans([str(src / n) for n in POSES])
        cls.s.run_auto()
        for sc in cls.s.scans:
            sc.analyze()
        cls.s.measures.append({'type': 'height', 'pts': [[0, 0, 1.0]]})
        cls.s.zero = {'z': -1.4, 'source': 'manual'}
        cls.s.save(cls.tmp / 'проект' / 'объект.pulse')

    def _check(self, pulse):
        s2 = Session.from_project(pulse)
        root = Path(pulse).parent
        for a, b in zip(self.s.scans, s2.scans):
            self.assertTrue(Path(b.path).is_relative_to(root.resolve()), b.path)
            if a.pose is None:
                self.assertIsNone(b.pose)
            else:
                np.testing.assert_allclose(a.pose, b.pose, atol=1e-9)
        self.assertEqual(s2.load_cached(), len(self.s.scans))          # кеш цел — без пересчёта
        self.assertEqual(len(s2.measures), 1)
        self.assertEqual(s2.zero, self.s.zero)
        return s2

    def test_folder_moved(self):
        out = self.tmp / 'передача' / 'Объект'
        r = self.s.pack(out)
        self.assertEqual(r['scans'], len(self.s.scans))
        self.assertFalse(r['missing'])
        self.assertTrue((out / 'scans' / 'A.json').exists())
        self.assertTrue((out / 'ПРОЧТИ.txt').exists())
        moved = self.tmp / 'у_получателя' / 'Объект'
        moved.parent.mkdir()
        shutil.move(str(out), str(moved))                           # получатель положил в другое место
        self._check(moved / 'Объект.pulse')
        self.assertEqual(Path(self.s.project_path).name, 'объект.pulse')   # открытый сеанс не изменился
        with self.assertRaises(FileExistsError):
            self.s.pack(moved)                                       # не пустая папка

    def test_zip_extracted(self):
        z = self.tmp / 'Объект.zip'
        r = self.s.pack(z, as_zip=True)
        self.assertTrue(z.exists() and r['size'] == z.stat().st_size)
        time.sleep(0.01)
        dst = self.tmp / 'распаковано'
        with zipfile.ZipFile(z) as f:
            f.extractall(dst)                                        # время файлов — уже другое
        p = dst / 'Объект' / 'Объект.pulse'
        self.assertNotEqual(os.stat(p.parent / 'scans' / 'A.ply').st_mtime_ns,
                            os.stat(self.s.scans[0].path).st_mtime_ns)
        self._check(p)


if __name__ == '__main__':
    unittest.main()
