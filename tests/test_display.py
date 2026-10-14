"""
Настройки отображения облака (display.py, Session.set_display / effective_display):
наследование от групп, обрезка по дальности и высоте, «в расчётах», раскраска.
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import display as dsp                             # noqa: E402
from scan_session import Session                  # noqa: E402
from test_session import write_scans, POSES       # noqa: E402


class TestDisplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        write_scans(cls.tmp)
        cls.paths = [str(cls.tmp / n) for n in POSES]

    def test_mask_colors(self):
        P = np.array([[1, 0, 0], [5, 0, 0], [0, 0, 2.5], [0, 0, -1.6]], float)
        self.assertEqual(dsp.mask(P, {'rmax': 3}).tolist(), [True, False, True, True])
        self.assertEqual(dsp.mask(P, {'rmin': 2}).tolist(), [False, True, True, False])
        self.assertEqual(dsp.mask(P, {'zmin': -1.5, 'zmax': 2}).tolist(), [True, True, False, False])
        C = dsp.colors(P, P[:, 2], {'color_mode': 'height'})
        self.assertEqual(C.shape, (4, 3))
        self.assertGreater(C[2, 0], C[3, 0])                   # выше — краснее
        self.assertIsNone(dsp.colors(P, P[:, 2], {'color_mode': 'solid'}))
        self.assertEqual(dsp.rgb_hex(dsp.hex_rgb('#ff8000')), '#ff8000')

    def test_session(self):
        s = Session.from_scans(self.paths)
        g = s.tree.add_group('root', 'Комната', 'комната')
        s.move_node('A.ply', g)
        s.move_node('B.ply', g)
        A, B, C = (s.by_id(n) for n in POSES)
        for sc in s.scans:
            sc.analyze()
        nA = len(A.down)
        # группа: свой цвет и дальность только для показа — расчёты не меняются
        s.set_display(g, {'color_mode': 'solid', 'color': '#00ff00', 'rmax': 2.0})
        self.assertEqual(s.effective_display('B.ply')['color'], '#00ff00')
        self.assertEqual(s.effective_display('C.ply'), {})
        self.assertEqual(len(A.down), nA)
        # скан поверх группы
        s.set_display('A.ply', {'color': '#ff0000'})
        self.assertEqual(s.effective_display('A.ply')['color'], '#ff0000')
        self.assertEqual(s.effective_display('A.ply')['rmax'], 2.0)
        # «в расчётах»: down и маска экспорта обрезаются
        s.set_display(g, {'rmax': 2.0, 'apply': True})
        self.assertLess(len(A.down), nA)
        self.assertTrue(np.all(np.linalg.norm(A.down, axis=1) <= 2.0 + 1e-9))
        P = A.res['down']
        self.assertEqual(int(A.keep_mask(P).sum()), int(dsp.mask(P, {'rmax': 2.0}).sum()))
        # сохранение и загрузка
        p = self.tmp / 'disp.pulse'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertEqual(s2.own_display('A.ply'), {'color': '#ff0000'})
        self.assertEqual(s2.by_id('B.ply').calc_filter, {'rmax': 2.0})
        # сброс
        s2.set_display(g, None)
        self.assertIsNone(s2.by_id('B.ply').calc_filter)


if __name__ == '__main__':
    unittest.main()
