"""
Логика интерактивного инструмента (scan_session.py) на синтетических сканах.

Запуск:  .venv/bin/python -m unittest discover -s tests -v
"""
import sys
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import open3d as o3d                              # noqa: E402
import plane_register as pr                       # noqa: E402
import fileio                                     # noqa: E402
from scan_session import Session                  # noqa: E402
from test_registration import scan, gt_pose, FLIP # noqa: E402


POSES = {'A.ply': ((2.0, 1.8), 0.3), 'B.ply': ((6.8, 3.5), -1.1), 'C.ply': ((4.5, 4.0), 2.0)}


def write_scans(tmp):
    origins = {}
    for i, (name, (xy, yaw)) in enumerate(POSES.items()):
        pts, o = scan(xy, yaw, seed=i + 1)
        fileio.write_cloud(Path(tmp) / name, pts)   # пути с кириллицей — и на Windows
        origins[name] = (o, yaw)
    return origins


def pose_err(T, name, origins, canon=False):
    """T — поза в исходных системах (scan.pose) или канонических (canon=True, Tc)."""
    oA, yA = origins['A.ply']
    o, y = origins[name]
    gt = gt_pose(oA, yA, o, y)
    if canon:                                   # синтетика: исходная = FLIP · каноническая
        F = pr.make_T(FLIP, np.zeros(3))
        gt = F @ gt @ F
    return pr.pose_delta(np.asarray(T), gt)


class TestSession(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.origins = write_scans(cls.tmp)
        cls.paths = [str(Path(cls.tmp) / n) for n in POSES]

    def test_auto_save_load(self):
        s = Session.from_scans(self.paths)
        s.run_auto()
        for sc in s.scans[1:]:
            dt, da = pose_err(sc.pose, sc.id, self.origins)
            self.assertLess(dt, 0.03, sc.id)
            self.assertLess(da, 0.5, sc.id)
        # отклонить ребро → скан может остаться связанным через другие
        idx = next(i for i, e in enumerate(s.edges) if {e['A'], e['B']} == {'A.ply', 'B.ply'})
        s.set_edge_user(idx, 'reject')
        self.assertFalse(s.edge_active(s.edges[idx]))
        p = Path(self.tmp) / 'p.json'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertEqual(s2.edges[idx].get('user'), 'reject')
        for a, b in zip(s.scans, s2.scans):
            np.testing.assert_allclose(a.pose, b.pose, atol=1e-9)

    def _canon_point(self, s, sc, world_xyz, name=None):
        """Мировая точка комнаты → канон. система скана (как будто клик по ней)."""
        o, yaw = self.origins[name or sc.id]
        local = (np.asarray(world_xyz) - o) @ pr.rot_z(-yaw).T      # Z вверх, начало — сканер
        return local                                               # канон. = локальная Z-вверх

    def test_elements(self):
        """Элементы (fits.py): подгонка по «клику», остатки, каркас, сохранение."""
        import fits
        s = Session.from_scans(self.paths[:1])
        A = s.scans[0]
        el = s.add_element(A, self._canon_point(s, A, (1.0, 1.0, 0.0)), kind='auto')
        self.assertIsNotNone(el)
        self.assertEqual(el['kind'], 'plane')
        self.assertEqual(el['name'], 'Плоскость 1')
        self.assertNotIn('used', el)                       # индексы не хранятся
        # пол: нормаль вверх (на сканер), отметка ≈ −1.4 (штатив), инлайнеры есть
        n = np.asarray(el['params']['n'])
        self.assertLess(abs(n[2]), 1.0, msg='нормаль не вертикальна')
        self.assertAlmostEqual(el['params']['c'], -1.4, delta=0.03)
        self.assertGreater(len(s.element_inliers(A, el)), 5000)
        pts, segs = fits.element_wire(el)
        self.assertEqual(len(segs), 4)                     # прямоугольник
        # сохранение/загрузка
        p = Path(self.tmp) / 'el.json'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertEqual(len(s2.elements), 1)
        self.assertEqual(s2.elements[0]['id'], el['id'])
        self.assertEqual(s2.elements[0]['params'], el['params'])
        # переименование, id не переиспользуются, удаление
        s2.rename_element(el['id'], 'Пол')
        self.assertEqual(s2.elements[0]['name'], 'Пол')
        el2 = s2.add_element(A, self._canon_point(s2, A, (9.0, 3.0, 1.5)), kind='auto')
        self.assertIsNotNone(el2)
        self.assertNotEqual(el2['id'], el['id'])
        self.assertEqual(el2['name'], 'Плоскость 2')
        # видимость: по умолчанию True, флаг переживает сохранение
        self.assertTrue(s2.element_visible(el2))
        s2.set_element_visible(el2['id'], False)
        self.assertFalse(s2.element_visible(s2.element_by_id(el2['id'])))
        p2 = Path(self.tmp) / 'el2.json'
        s2.save(p2)
        s3 = Session.from_project(p2)
        self.assertFalse(s3.element_visible(s3.element_by_id(el2['id'])))
        self.assertTrue(s3.element_visible(s3.element_by_id(el['id'])))
        self.assertTrue(s2.remove_element(el['id']))
        self.assertEqual([e['id'] for e in s2.elements], [el2['id']])

    def test_level_by_plane(self):
        """Горизонт кликом: наклонённый пол → project выровнен, ноль по плоскости."""
        s = Session.from_scans(self.paths[:1])
        A = s.scans[0]
        el = s.add_element(A, self._canon_point(s, A, (1.0, 1.0, 0.0)), kind='plane')
        self.assertIsNotNone(el)
        T = s.Tc(A)
        n_w = T[:3, :3] @ np.asarray(el['params']['n'])
        c_w = float(n_w @ (T[:3, :3] @ np.asarray(el['center']) + T[:3, 3]))
        info = s.level_by_plane(n_w, c_w, set_zero=True)
        # плоскость теперь горизонтальна: нормаль в новой общей системе вертикальна
        n2 = s.Tc(A)[:3, :3] @ np.asarray(el['params']['n'])
        self.assertLess(np.linalg.norm(n2 - np.array([0, 0, 1.0])), 1e-9)
        self.assertLess(info['tilt_deg'], 3.0)             # синтетика почти ровная
        self.assertEqual(s.zero['source'], 'plane')
        self.assertAlmostEqual(s.zero_z(), c_w, places=6)
        # повторное выравнивание ничего не меняет (уже вертикально)
        L1 = s.level.copy()
        info2 = s.level_by_plane(n2, c_w)
        self.assertLess(info2['tilt_deg'], 1e-6)
        np.testing.assert_allclose(s.level, L1, atol=1e-7)

    def test_measure_undo(self):
        """Undo замеров: снимок перед мутацией, стек до 10, независим от проекта."""
        s = Session.from_scans(self.paths[:1])
        for k in range(3):
            s.push_measure_undo()
            s.measures.append({'type': 'dist', 'pts': [[k, 0, 0], [k, 1, 0]]})
        self.assertTrue(s.undo_measure())
        self.assertEqual(len(s.measures), 2)
        self.assertTrue(s.undo_measure())
        self.assertEqual(len(s.measures), 1)
        self.assertTrue(s.undo_measure())
        self.assertEqual(s.measures, [])
        self.assertFalse(s.undo_measure())
        # снимок — глубокая копия: правка текущего замера после push не портит стек
        s.measures.append({'type': 'height', 'pts': [[9, 9, 9]]})
        s.push_measure_undo()
        s.measures[0]['pts'][0][0] = 777
        self.assertTrue(s.undo_measure())
        self.assertEqual(len(s.measures), 1)
        self.assertEqual(s.measures[0]['pts'][0][0], 9)       # состояние на момент push
        # deque(maxlen=10) — не сериализуется: в проекте замеры, стека нет
        p = Path(self.tmp) / 'mu.json'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertEqual(len(s2.measures), 1)
        self.assertFalse(s2.undo_measure())

    def test_manual_planes(self):
        s = Session.from_scans(self.paths[:2])
        A, B = s.scans
        # «клики»: пол, стена x=0, стена y=0 — в обоих сканах
        clicks = [(1.0, 1.0, 0.0), (0.0, 2.0, 2.0), (3.0, 0.0, 2.0)]
        pairs = []
        for w in clicks:
            fa = s.pick_feature(A, self._canon_point(s, A, w))
            fb = s.pick_feature(B, self._canon_point(s, B, w))
            self.assertEqual(fa['type'], 'plane', fa)
            self.assertEqual(fb['type'], 'plane', fb)
            pairs.append((fa, fb))
            st = s.dof_status(A, B, pairs)
        self.assertEqual(st['text'], 'всё определено')
        self.assertIn('не хватает', s.dof_status(A, B, pairs[:2])['text'])
        Tc, info = s.solve_manual(A, B, pairs)
        dt, da = pose_err(Tc, 'B.ply', self.origins, canon=True)
        self.assertLess(dt, 0.02)
        self.assertLess(da, 0.5)
        T2, _ = s.refine_icp([A], B, Tc)
        s.accept_pose(B, T2, anchor=A)
        dt, da = pose_err(B.pose, 'B.ply', self.origins)
        self.assertLess(dt, 0.02)

    def test_manual_points_and_nudge(self):
        s = Session.from_scans(self.paths[:2])
        A, B = s.scans
        # точки на углах колонны (точные координаты; kind='point')
        corners = [(6.0, 1.0, 2.0), (6.6, 1.6, 0.5), (2.5, 4.5, 1.0)]
        pairs = [({'type': 'point', 'scan': 'A.ply', 'p': self._canon_point(s, A, w).tolist()},
                  {'type': 'point', 'scan': 'B.ply', 'p': self._canon_point(s, B, w).tolist()})
                 for w in corners]
        self.assertEqual(s.dof_status(A, B, pairs)['text'], 'всё определено')
        Tc, _ = s.solve_manual(A, B, pairs)
        dt, da = pose_err(Tc, 'B.ply', self.origins, canon=True)
        self.assertLess(dt, 1e-6)
        T2 = Session.nudge(Tc, dx=0.1, dyaw_deg=5)
        dt, da = pr.pose_delta(Tc, T2)
        self.assertAlmostEqual(da, 5, places=6)

    def test_tilt_level_and_6dof(self):
        """Скан B с наклоном 3° (ошибка IMU): выравнивание и решение с полным поворотом."""
        tilt = pr.make_T(np.eye(3), np.zeros(3))
        a = np.radians(3.0)
        Rt = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]]) @ \
            np.array([[np.cos(a / 2), 0, np.sin(a / 2)], [0, 1, 0], [-np.sin(a / 2), 0, np.cos(a / 2)]])
        tilt[:3, :3] = Rt
        pts, _ = scan(POSES['B.ply'][0], POSES['B.ply'][1], seed=2)
        F = pr.make_T(FLIP, np.zeros(3))
        # наклон в канонической системе (Z вверх): канон = FLIP · исходная
        tilted = pr.transform(pts, F @ tilt @ F)
        p = Path(self.tmp) / 'Bt.ply'
        o3d.io.write_point_cloud(str(p), o3d.geometry.PointCloud(o3d.utility.Vector3dVector(tilted)))
        s = Session.from_scans([self.paths[0], str(p)])
        A, B = s.scans
        oA, yA = self.origins['A.ply']
        oB, yB = self.origins['B.ply']
        Tc_true = F @ gt_pose(oA, yA, oB, yB) @ F @ np.linalg.inv(tilt)
        # 1) «наивная» поза (скан считается ровным) + сдвиг → выравнивание даёт истинный поворот
        T_naive = F @ gt_pose(oA, yA, oB, yB) @ F
        T_naive[:3, 3] += [0.3, 0.0, 0.0]
        lev, info = s.level_pose(B, T_naive)
        R_err = lev[:3, :3] @ Tc_true[:3, :3].T
        err_deg = np.degrees(np.arccos(np.clip((np.trace(R_err) - 1) / 2, -1, 1)))
        self.assertLess(err_deg, 0.3, info)
        self.assertGreater(info['tilt_deg'], 3.0)
        np.testing.assert_allclose(lev[:3, 3], T_naive[:3, 3])     # вращение вокруг сканера
        # 2) пары «пол + две стены» + полный поворот → поза с наклоном
        clicks = [(1.0, 1.0, 0.0), (0.0, 2.0, 2.0), (3.0, 0.0, 2.0)]
        pairs = []
        for w in clicks:
            fa = s.pick_feature(A, self._canon_point(s, A, w))
            pb = pr.transform(self._canon_point(s, B, w, 'B.ply')[None], tilt)[0]
            fb = s.pick_feature(B, pb)
            pairs.append((fa, fb))
        Tc, info = s.solve_manual(A, B, pairs, T_init=np.eye(4), full=True)
        self.assertEqual(info['rotation'], '6dof')
        dt, da = pr.pose_delta(Tc, Tc_true)
        self.assertLess(dt, 0.03, info)
        self.assertLess(da, 0.3, info)

    def test_plan_align_and_yaw_snap(self):
        s = Session.from_scans(self.paths[:2])
        A, B = s.scans
        d = s.align_plan_to_walls()                     # скан A повёрнут на 0.3 рад
        self.assertIsNotNone(d)
        th = s._wall_direction([(A, s.Tc(A))])
        self.assertLess(abs(np.degrees(th)), 0.5)       # стены вдоль осей
        # подвижный B в истинной позе, но с ошибкой поворота 7° → доворот её убирает
        F = pr.make_T(FLIP, np.zeros(3))
        oA, yA = self.origins['A.ply']
        oB, yB = self.origins['B.ply']
        T_true = pr.make_T(s.level, np.zeros(3)) @ F @ gt_pose(oA, yA, oB, yB) @ F
        T_bad = Session.nudge(T_true, dyaw_deg=7.0)
        T_fix, dd = s.snap_yaw_to_walls(B, T_bad, others=[A])
        self.assertAlmostEqual(dd, -7.0, delta=0.5)
        self.assertLess(pr.pose_delta(T_fix, T_true)[1], 0.5)

    def test_tree_session(self):
        s = Session.from_scans(self.paths)
        g = s.tree.add_group('root', 'Комната', 'комната')
        s.tree.move('A.ply', g)
        s.tree.move('B.ply', g)
        s.run_auto(by_tree=True)
        for sc in s.scans[1:]:
            dt, da = pose_err(sc.pose, sc.id, self.origins)
            self.assertLess(dt, 0.03, sc.id)
        s.set_visible(g, False)
        self.assertEqual([x.id for x in s.scans if x.visible], ['C.ply'])
        # перемещение видимого скана в скрытую группу — скрывает его сразу
        s.move_node('C.ply', g)
        self.assertEqual([x.id for x in s.scans if x.visible], [])
        s.move_node('C.ply', 'root')
        self.assertEqual([x.id for x in s.scans if x.visible], ['C.ply'])
        p = Path(self.tmp) / 'tree.json'
        s.save(p)
        s2 = Session.from_project(p)
        self.assertEqual(s2.tree.to_json(), s.tree.to_json())
        self.assertEqual([x.id for x in s2.scans if x.visible], ['C.ply'])
        n_all = s2.export(Path(self.tmp) / 'all.ply', voxel=0.1)
        n_g = s2.export(Path(self.tmp) / 'g.ply', voxel=0.1, scan_ids=s2.tree.scans_in(g))
        self.assertLess(n_g, n_all)

    def test_project_portable(self):
        """Пути сканов в проекте относительные: папку можно перенести (другая машина/ОС)."""
        import shutil
        src = Path(tempfile.mkdtemp()) / 'proj'
        shutil.copytree(self.tmp, src)
        s = Session.from_scans([str(src / n) for n in POSES])
        s.save(src / 'p.json')
        raw = json.loads((src / 'p.json').read_text(encoding='utf-8'))
        self.assertEqual(raw['scans'][0]['path'], 'A.ply')
        dst = Path(tempfile.mkdtemp()) / 'moved'
        shutil.move(str(src), dst)
        s2 = Session.from_project(dst / 'p.json')
        self.assertTrue(all(Path(sc.path).exists() for sc in s2.scans))

    def test_export(self):
        s = Session.from_scans(self.paths)
        s.run_auto()
        out = Path(self.tmp) / 'm.ply'
        n = s.export(out, voxel=0.05)
        self.assertGreater(n, 1000)
        self.assertTrue(out.exists())


    def test_views_roundtrip(self):
        """Ф14: сохранённые виды — roundtrip, удаление."""
        s = Session.from_scans(self.paths[:1])
        v = s.add_view('Вход', [1, 2, 3], [4, 5, 6], [0, 0, 1])
        p = Path(self.tmp) / 'vw.pulse'
        s.save(p)
        s2 = Session.from_project(p)
        got = s2.view_by_id(v['id'])
        self.assertEqual(got['name'], 'Вход')
        self.assertEqual(got['eye'], [1, 2, 3])
        self.assertTrue(s2.remove_view(v['id']))
        self.assertIsNone(s2.view_by_id(v['id']))


if __name__ == '__main__':
    unittest.main()
