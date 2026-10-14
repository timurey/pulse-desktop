"""
Импорт проекта в проект (Session.import_project): дерево — новой группой, связи, чистка,
кеш анализа, общие сканы, совпадающие имена, запекание.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_session import Session                  # noqa: E402
from test_session import write_scans, POSES       # noqa: E402


class TestImportProject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        write_scans(cls.tmp)
        # проект «второй день»: A и B в группе, состыкованы, у A — ручная чистка
        d2 = cls.tmp / 'день2'
        d2.mkdir()
        for n in ('A.ply', 'B.ply'):
            shutil.copy(cls.tmp / n, d2 / n)
        s = Session.from_scans([str(d2 / 'A.ply'), str(d2 / 'B.ply')])
        g = s.tree.add_group('root', 'Комната', 'комната')
        s.move_node('A.ply', g)
        s.move_node('B.ply', g)
        for sc in s.scans:
            sc.analyze()
        s.run_auto()
        s.by_id('A.ply').drop = np.array([1, 2, 3], np.int64)
        s.set_display(g, {'color_mode': 'solid', 'color': '#00ff00'})
        cls.rel = np.linalg.inv(s.Tc(s.by_id('A.ply'))) @ s.Tc(s.by_id('B.ply'))
        cls.p2 = d2 / 'день2.pulse'
        s.save(cls.p2)

    def test_import(self):
        # текущий проект: C и (тот же по имени, другой файл) A
        s = Session.from_scans([str(self.tmp / 'C.ply'), str(self.tmp / 'A.ply')])
        r = s.import_project(self.p2)
        self.assertEqual(r['renamed'], {'A.ply': 'A_2.ply'})
        self.assertEqual(sorted(r['added']), ['A_2.ply', 'B.ply'])
        self.assertEqual(r['cached'], 2)                         # анализ — из кеша импортируемого проекта
        self.assertGreaterEqual(r['edges'], 1)
        top = s.tree.group(r['group'])
        self.assertEqual(top.name, 'день2')
        sub = [c for c in top.children if hasattr(c, 'children')]
        self.assertEqual([g.name for g in sub], ['Комната'])
        self.assertEqual(sorted(s.tree.scans_in(sub[0].id)), ['A_2.ply', 'B.ply'])
        self.assertEqual(s.effective_display('B.ply').get('color'), '#00ff00')
        self.assertEqual(s.by_id('A_2.ply').drop.tolist(), [1, 2, 3])
        self.assertTrue(all(e['A'] != 'A.ply' or e['B'] in ('C.ply',) for e in s.edges))
        # запекание импортированной группы сохраняет взаимную позу
        u = s.bake(r['group'])
        P = u.members
        rel = np.linalg.inv(P['A_2.ply']) @ P['B.ply']
        self.assertLess(np.linalg.norm(rel[:3, 3] - self.rel[:3, 3]), 0.02)
        # сохранение: кеш импортированных сканов переходит в этот проект
        out = self.tmp / 'общий.pulse'
        s.save(out)
        s2 = Session.from_project(out)
        s2.load_cached()
        self.assertTrue(s2.by_id('A_2.ply').analyzed and s2.by_id('B.ply').analyzed)

    def test_shared_and_bake(self):
        # тот же файл B уже в проекте — не дублируется
        s = Session.from_scans([str(self.p2.parent / 'B.ply'), str(self.tmp / 'C.ply')])
        r = s.import_project(self.p2, bake=True)
        self.assertEqual(r['shared'], ['B.ply'])
        self.assertEqual(r['added'], ['A.ply'])
        self.assertEqual(len([x for x in s.scans if x.id == 'B.ply']), 1)
        self.assertFalse(r['baked'])                             # в группе один новый скан — запечь нечего
        with self.assertRaises(ValueError):
            Session.from_project(self.p2).import_project(self.p2.parent / 'нет.pulse')


if __name__ == '__main__':
    unittest.main()
