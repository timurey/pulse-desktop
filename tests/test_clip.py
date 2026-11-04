"""
Боксы отсечения (clip.py, Ф15): маски, композиция с сечением, каркас, roundtrip.

Запуск:  .venv312/bin/python -m unittest tests.test_clip -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import clip                                       # noqa: E402
import section as sec                             # noqa: E402
from scan_session import Session                  # noqa: E402
from test_session import write_scans, POSES       # noqa: E402

BOX = {'id': 'box1', 'name': 'Комната', 'on': True, 'visible': True,
       'center': [5.0, 5.0, 1.5], 'size': [4.0, 2.0, 3.0], 'yaw_deg': 0.0}


class TestMask(unittest.TestCase):
    P = np.array([[5.0, 5.0, 1.5],      # центр — внутри
                  [7.2, 5.0, 1.5],      # за гранью +x (полуразмер 2.0)
                  [5.0, 4.0, 3.2],      # выше потолка (полуразмер z 1.5)
                  [2.0, 2.0, 1.0]])     # далеко снаружи

    def test_inside(self):
        m = clip.inside_mask(self.P, BOX)
        np.testing.assert_array_equal(m, [True, False, False, False])

    def test_yaw(self):
        """Поворот на 90°: бокс 4×2 вдоль x становится 2×4; точка (5, 6.9) внутри."""
        b = dict(BOX, yaw_deg=90.0)
        q = np.array([[5.0, 6.9, 1.5], [6.9, 5.0, 1.5]])
        np.testing.assert_array_equal(clip.inside_mask(q, b), [True, False])

    def test_combined_or(self):
        """Два включённых бокса — объединение (OR)."""
        s = Session()
        s.clip_boxes = [BOX, dict(BOX, id='box2', center=[10.0, 10.0, 1.5], size=[2.0, 2.0, 2.0])]
        m = clip.combined_mask(self.P, s)
        np.testing.assert_array_equal(m, [True, False, False, False])
        m2 = clip.combined_mask(np.array([[10.0, 10.0, 1.5]]), s)
        self.assertTrue(m2[0])

    def test_combined_and_section(self):
        """Сечение AND-ится поверх бокса: полоса z∈[1.0,1.2] внутри бокса."""
        s = Session()
        s.clip_boxes = [BOX]
        st = sec.default('z', z0=0.0)
        st.update(mode='slice', c=1.1, thick=0.2)
        Q = np.array([[5.0, 5.0, 1.1],      # в боксе и в полосе
                      [5.0, 5.0, 1.5],      # в боксе, выше полосы
                      [7.2, 5.0, 1.1],      # в полосе, вне бокса
                      [2.0, 2.0, 1.1]])     # в полосе, далеко
        m = clip.combined_mask(Q, s, section_keep=sec.keep_mask(Q, st))
        np.testing.assert_array_equal(m, [True, False, False, False])
        # та же точка вне полосы, но внутри бокса — отсечена сечением
        q = np.array([[5.0, 5.0, 2.0]])
        self.assertFalse(clip.combined_mask(q, s, section_keep=sec.keep_mask(q, st))[0])

    def test_no_boxes(self):
        s = Session()
        keep = np.array([True, False, True, True])
        np.testing.assert_array_equal(clip.combined_mask(self.P, s, section_keep=keep), keep)


class TestPlanesAndWire(unittest.TestCase):
    def test_six_planes(self):
        ps = clip.box_planes(BOX)
        self.assertEqual(len(ps), 6)
        for (o, n) in ps:
            self.assertAlmostEqual(np.linalg.norm(n), 1.0, places=9)
        # центр внутри всех полупространств: n·c − c ≥ 0 − c … проще: n·center ≥ n·o
        c = np.asarray(BOX['center'])
        for o, n in ps:
            self.assertGreaterEqual(float(n @ c), float(n @ o) - 1e-9)

    def test_wire(self):
        V, E = clip.box_wire(BOX['center'], BOX['size'])
        self.assertEqual(len(V), 8)
        self.assertEqual(len(E), 12)
        # все вершины в пределах размера
        d = np.abs(V - np.asarray(BOX['center'])) <= np.asarray(BOX['size']) / 2 + 1e-9
        self.assertTrue(d.all())

    def test_wire_yaw(self):
        V, _ = clip.box_wire([0, 0, 0], [2.0, 1.0, 1.0], yaw_deg=90.0)
        xs, ys = V[:, 0], V[:, 1]
        self.assertAlmostEqual(np.ptp(xs), 1.0, places=9)     # размер повернулся
        self.assertAlmostEqual(np.ptp(ys), 2.0, places=9)


class TestSessionBoxes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.origins = write_scans(cls.tmp)
        cls.paths = [str(Path(cls.tmp) / n) for n in POSES]

    def test_add_roundtrip(self):
        s = Session.from_scans(self.paths[:1])
        b1 = s.add_box([1.0, 2.0, 0.5], size=[3.0, 3.0, 2.0])
        b2 = s.add_box([4.0, 4.0, 0.5])                     # размер по умолчанию
        self.assertEqual(b1['id'], 'box1')
        self.assertEqual(b2['id'], 'box2')
        self.assertEqual(b2['size'], list(clip.DEFAULT_SIZE))
        p = Path(self.tmp) / 'cb.pulse'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertEqual(len(s2.clip_boxes), 2)
        self.assertEqual(s2.box_by_id('box1')['center'], [1.0, 2.0, 0.5])
        self.assertTrue(s2.remove_box('box1'))
        self.assertIsNone(s2.box_by_id('box1'))
        self.assertFalse(s2.remove_box('box1'))             # второй раз — нет

    def test_state_signature_changes(self):
        s = Session.from_scans(self.paths[:1])
        h0 = s.state_signature()
        s.add_box([0, 0, 0])
        self.assertNotEqual(s.state_signature(), h0)


if __name__ == '__main__':
    unittest.main()
