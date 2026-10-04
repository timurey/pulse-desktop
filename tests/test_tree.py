"""Иерархия сканов: правка, видимость, сохранение, пары для стыковки по дереву."""
import sys
import json
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scan_tree import Tree, Group, Leaf      # noqa: E402

SCANS = ['r1a', 'r1b', 'r1c', 'cor1', 'cor2', 'r2a', 'r2b', 'fac1', 'fac2']


def building():
    t = Tree.from_json(None, SCANS)
    b = t.add_group('root', 'Здание', 'здание')
    f1 = t.add_group(b, '1 этаж', 'этаж')
    r1 = t.add_group(f1, 'Комната 1', 'комната')
    cor = t.add_group(f1, 'Коридор', 'комната')
    r2 = t.add_group(f1, 'Комната 2', 'комната')
    out = t.add_group(b, 'Фасады', 'снаружи')
    for s in ('r1a', 'r1b', 'r1c'):
        t.move(s, r1)
    for s in ('cor1', 'cor2'):
        t.move(s, cor)
    for s in ('r2a', 'r2b'):
        t.move(s, r2)
    for s in ('fac1', 'fac2'):
        t.move(s, out)
    return t, dict(b=b, f1=f1, r1=r1, cor=cor, r2=r2, out=out)


class TestTree(unittest.TestCase):
    def test_legacy_and_sync(self):
        t = Tree.from_json(None, ['a', 'b'])
        self.assertTrue(t.is_flat())
        self.assertEqual([l.scan for l in t.leaves()], ['a', 'b'])
        t.sync(['b', 'c'])
        self.assertEqual(sorted(l.scan for l in t.leaves()), ['b', 'c'])

    def test_move_cycle_delete(self):
        t, g = building()
        self.assertEqual(sorted(t.scans_in(g['f1'])), sorted(SCANS[:7]))
        self.assertFalse(t.move(g['b'], g['r1']))          # группу внутрь своей ветки нельзя
        self.assertFalse(t.move(g['f1'], g['f1']))
        self.assertTrue(t.move(g['r2'], 'root'))
        self.assertEqual(t.parent(g['r2']).id, 'root')
        self.assertTrue(t.delete_group(g['cor']))          # сканы коридора — к родителю
        self.assertEqual(t.parent('cor1').id, g['f1'])
        self.assertIsNone(t.group(g['cor']))
        self.assertEqual(t.path('r1a'), 'Здание / 1 этаж / Комната 1')

    def test_reorder_and_move_many(self):
        t, g = building()
        kids = lambda gid: [c.scan if isinstance(c, Leaf) else c.id for c in t.group(gid).children]
        # перестановка внутри группы: r1c — в начало, r1a — в конец
        self.assertTrue(t.move('r1c', g['r1'], 0))
        self.assertEqual(kids(g['r1']), ['r1c', 'r1a', 'r1b'])
        self.assertTrue(t.move('r1a', g['r1'], 3))
        self.assertEqual(kids(g['r1']), ['r1c', 'r1b', 'r1a'])
        # несколько сразу, порядок дерева сохраняется независимо от порядка выбора
        self.assertTrue(t.move_many(['r2b', 'cor1', 'r2a'], g['out'], 1))
        self.assertEqual(kids(g['out']), ['fac1', 'cor1', 'r2a', 'r2b', 'fac2'])
        self.assertEqual(kids(g['r2']), [])
        # группа вместе со своим сканом: скан едет внутри группы
        self.assertTrue(t.move_many([g['cor'], 'cor2'], g['b'], 0))
        self.assertEqual(kids(g['b'])[0], g['cor'])
        self.assertEqual(kids(g['cor']), ['cor2'])
        # нельзя вложить группу в её же потомка — ничего не меняется
        self.assertFalse(t.move_many(['fac1', g['f1']], g['r1']))
        self.assertIn('fac1', kids(g['out']))

    def test_group_from_selection(self):
        t, g = building()
        gid = t.group_from(['r1b', 'r1c'], 'Угол', 'комната')
        kids = [c.scan if isinstance(c, Leaf) else c.id for c in t.group(g['r1']).children]
        self.assertEqual(kids, ['r1a', gid])                    # на месте первого выбранного
        self.assertEqual([c.scan for c in t.group(gid).children], ['r1b', 'r1c'])
        gid2 = t.group_from(['fac2', 'cor1'], 'Смешанная')      # из разных групп — в родителе первого
        self.assertIn(gid2, [c.id for c in t.group(g['cor']).children if isinstance(c, Group)])
        self.assertEqual([c.scan for c in t.group(gid2).children], ['cor1', 'fac2'])

    def test_visibility(self):
        t, g = building()
        t.set_visible(g['f1'], False)
        self.assertFalse(t.effective_visible('r1a'))
        self.assertTrue(t.effective_visible('fac1'))
        t.set_visible('r1a', False)
        t.set_visible(g['f1'], True)                       # флаг скана внутри ветки сохраняется
        self.assertFalse(t.effective_visible('r1a'))
        self.assertTrue(t.effective_visible('r1b'))
        t.only([g['out']])
        self.assertEqual([s for s in SCANS if t.effective_visible(s)], ['fac1', 'fac2'])

    def test_json_roundtrip(self):
        t, g = building()
        t.set_visible(g['out'], False)
        d = json.loads(json.dumps(t.to_json(), ensure_ascii=False))
        t2 = Tree.from_json(d, SCANS)
        self.assertEqual(t2.to_json(), t.to_json())
        self.assertNotIn(t2.add_group('root', 'x'), [x.id for x in t.groups()])   # id не повторяется

    def test_registration_pairs(self):
        t, g = building()
        pairs = t.registration_pairs(SCANS, weight={s: i for i, s in enumerate(SCANS)})
        flat = len(SCANS) * (len(SCANS) - 1) // 2
        self.assertLess(len(pairs), flat)
        for a, b in (('r1a', 'r1b'), ('cor1', 'cor2'), ('fac1', 'fac2')):
            self.assertIn((a, b), pairs)                   # все пары внутри комнаты
        # между комнатами 1 этажа — есть хотя бы одна пара
        self.assertTrue(any(a.startswith('r1') and b.startswith('cor') for a, b in pairs))
        self.assertTrue(any(a.startswith('cor') and b.startswith('r2') for a, b in pairs))
        # плоское дерево = все пары
        self.assertEqual(len(Tree.from_json(None, SCANS).registration_pairs(SCANS)), flat)


if __name__ == '__main__':
    unittest.main()


class TestTreeDirty(unittest.TestCase):
    def test_tree_changes_mark_project_dirty(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from scan_session import Session
        s = Session()
        s.tree = Tree.from_json(None, SCANS)
        sig = s.state_signature()
        s.tree.group_from(['r1a', 'r1b'], 'Комната')
        self.assertNotEqual(sig, s.state_signature())
