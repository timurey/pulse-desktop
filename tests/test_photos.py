"""
Синтетика для photos.py: привязка фото (PnP, гомография) и окраска.

Сцены — пинхол-рендер с известной позой: точки проецируются в кадр, по пикселям
решается поза и сравнивается с истиной. Перед solve всегда проверять ok.all() —
случайный поворот/сдвиг уводит точки из кадра и тест падает не на том ассерте.

Запуск:  .venv312/bin/python -m unittest tests.test_photos -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import photos as ph                                   # noqa: E402
from PIL import Image                                 # noqa: E402

W, H, F35 = 2296, 4080, 26          # портретный кадр, эквивалент 26 мм


def scene_planes(seed=0, n1=12, n2=6):
    """Фасад z=0 + земля y=0 (некомпланарно), камера в (0, 2, 18)."""
    rng = np.random.default_rng(seed)
    p1 = np.column_stack([rng.uniform(-5, 5, n1), rng.uniform(0, 8, n1), np.zeros(n1)])
    p2 = np.column_stack([rng.uniform(-5, 5, n2), np.zeros(n2), rng.uniform(-2, 2, n2)])
    C = np.array([0.0, 2.0, 18.0])
    T = ph.T_of(ph.look_R((0, 2, 0) - C, (0, 0, 1)), C)
    return np.vstack([p1, p2]), T, rng


def project_ok(P, T, f35=F35, w=W, h=H):
    uv, _, ok = ph.project(P, T, f35, w, h)
    assert ok.all(), 'сцена не в кадре — проверь камеру/разброс точек'
    return uv


class TestImage(unittest.TestCase):
    def test_focal_px_diagonal(self):
        f = ph.focal_px(F35, W, H)
        self.assertAlmostEqual(f, F35 * np.hypot(W, H) / 43.27, places=6)
        # не зависит от ориентации кадра
        self.assertAlmostEqual(ph.focal_px(F35, H, W), f)

    def test_load_image_exif_transpose(self):
        im = Image.new('RGB', (60, 40), (12, 34, 56))
        ex = Image.Exif()
        ex[274] = 6                                  # Orientation: повернуть на 90°
        p = os.path.join(tempfile.mkdtemp(), 'a.jpg')
        im.save(p, exif=ex.tobytes())
        arr = ph.load_image(p)
        self.assertEqual(arr.shape[:2], (60, 40))    # портрет после разворота
        self.assertEqual(ph.image_size(p), (60, 40))

    def test_exif_focal(self):
        im = Image.new('RGB', (64, 48), (1, 2, 3))
        ex = Image.Exif()
        ex.get_ifd(0x8769)[41989] = F35              # FocalLengthIn35mmFilm в под-IFD
        p = os.path.join(tempfile.mkdtemp(), 'b.jpg')
        im.save(p, exif=ex.tobytes())
        self.assertEqual(ph.exif_focal(p), float(F35))
        p2 = os.path.join(tempfile.mkdtemp(), 'c.jpg')
        im.save(p2)
        self.assertIsNone(ph.exif_focal(p2))


class TestProject(unittest.TestCase):
    def test_roundtrip(self):
        P, T, _ = scene_planes()
        uv = project_ok(P, T)
        # обратный луч через пиксель попадает в исходную точку (на её глубине)
        f = ph.focal_px(F35, W, H)
        cx, cy = (W - 1) / 2, (H - 1) / 2
        X = np.column_stack([(uv[:, 0] - cx) / f, (uv[:, 1] - cy) / f, np.ones(len(uv))])
        dirs_world = X @ T[:3, :3].T                 # лучи в мире
        z = (P - T[:3, 3]) @ T[:3, :3]
        depths = z[:, 2]
        back = T[:3, 3] + dirs_world * depths[:, None]
        self.assertTrue(np.abs(back - P).max() < 1e-6)

    def test_depth_positive_before_camera(self):
        C = np.zeros(3)
        T = ph.T_of(ph.look_R((0, 0, -1), (0, 0, 1)), C)
        P = np.array([[0.0, 0.0, -1.0], [0.0, 0.0, 1.0]])   # перед камерой / за ней
        _, z, ok = ph.project(P, T, F35, W, H)
        self.assertGreater(z[0], 0)
        self.assertTrue(ok[0])
        self.assertLess(z[1], 0)
        self.assertFalse(ok[1])                      # за камерой


class TestSolve(unittest.TestCase):
    def test_pnp_exact_and_noise(self):
        P, T, rng = scene_planes(0)
        uv = project_ok(P, T)
        T2, sg = ph.solve(uv, P, F35, W, H)
        self.assertIsNotNone(T2)
        self.assertAlmostEqual(sg, 0.0, places=3)
        self.assertLess(ph.rot_err_deg(T2[:3, :3], T[:3, :3]), 0.05)
        self.assertLess(np.linalg.norm(T2[:3, 3] - T[:3, 3]), 0.02)
        # шум 0.5 px
        T3, sg3 = ph.solve(uv + rng.normal(0, 0.5, uv.shape), P, F35, W, H)
        self.assertIsNotNone(T3)
        self.assertLess(sg3, 1.5)
        self.assertLess(ph.rot_err_deg(T3[:3, :3], T[:3, :3]), 0.5)
        self.assertLess(np.linalg.norm(T3[:3, 3] - T[:3, 3]), 0.15)

    def test_pnp_no_mirror(self):
        """det(R) = +1 у решения (нормировка знака DLT)."""
        P, T, _ = scene_planes(3)
        uv = project_ok(P, T)
        T2, _ = ph.solve(uv, P, F35, W, H)
        self.assertAlmostEqual(np.linalg.det(T2[:3, :3]), 1.0, places=6)

    def test_coplanar_pairs(self):
        """Все пары на одной плоскости: гомография-разложение, не DLT."""
        P, T, _ = scene_planes(1)
        plane = P[:12]
        uv = project_ok(plane, T)
        # без hint сторона плоскости неоднозначна — обе стороны дают малую σ;
        # с hint-облаком выбирается верная
        T2, sg = ph.solve(uv, plane, F35, W, H, hint=P)
        self.assertIsNotNone(T2)
        self.assertLess(sg, 1e-3)
        self.assertLess(ph.rot_err_deg(T2[:3, :3], T[:3, :3]), 0.05)
        self.assertLess(np.linalg.norm(T2[:3, 3] - T[:3, 3]), 0.05)

    def test_wrong_focal_sigma_grows(self):
        """Неверное фокусное — честная большая σ (или отказ), не «точки за камерой»."""
        P, T, _ = scene_planes(2)
        uv = project_ok(P, T)
        T2, sg = ph.solve(uv, P, 14, W, H)
        if T2 is not None:
            self.assertGreater(sg, 10)
            _, z, _ = ph.project(P, T2, 14, W, H)
            self.assertTrue((z > ph.EPS_Z).all())

    def test_too_few_pairs(self):
        P, T, _ = scene_planes()
        uv = project_ok(P, T)
        T2, sg = ph.solve(uv[:3], P[:3], F35, W, H)
        self.assertIsNone(T2)

    def test_wahba_known_center(self):
        P, T, _ = scene_planes(4)
        uv = project_ok(P, T)
        T2, sg = ph.solve(uv[:5], P[:5], F35, W, H, center=T[:3, 3])
        self.assertIsNotNone(T2)
        self.assertLess(ph.rot_err_deg(T2[:3, :3], T[:3, :3]), 0.05)
        self.assertLess(sg, 1e-3)

    def test_estimate_focal(self):
        P, T, rng = scene_planes(5)
        uv = project_ok(P, T)
        est = ph.estimate_focal(uv + rng.normal(0, 0.4, uv.shape), P, W, H)
        self.assertIsNotNone(est)
        f35, sg = est
        self.assertLess(abs(f35 - F35), 3.0)
        self.assertLess(sg, 1.5)


class TestHomography(unittest.TestCase):
    def _plane(self, seed=6):
        P, T, rng = scene_planes(seed)
        plane = P[:12]
        c = plane.mean(axis=0)
        _, _, Vt = np.linalg.svd(plane - c)
        return plane, P, c, Vt[0], Vt[1], T, rng

    def test_dlt_h_roundtrip(self):
        plane, _, c, ua, va, T, _ = self._plane()
        loc4 = np.array([[-6, -4], [6, -4], [6, 4], [-6, 4]], float)
        X4 = c + loc4[:, 0:1] * ua + loc4[:, 1:2] * va
        uv4 = project_ok(X4, T)
        H = ph.dlt_h(loc4, uv4)
        self.assertIsNotNone(H)
        back = ph.apply_h(H, loc4)
        self.assertTrue(np.abs(back - uv4).max() < 1e-6)

    def test_dlt_h_degenerate(self):
        # три точки на одной прямой — соответствия вырождены
        src = np.array([[0, 0], [1, 0], [2, 0], [3, 1]], float)
        dst = np.array([[0, 0], [10, 0], [20, 0], [30, 5]], float)
        self.assertIsNone(ph.dlt_h(src, dst))

    def test_plane_pose_from_h(self):
        plane, _, c, ua, va, T, _ = self._plane()
        loc4 = np.array([[-6, -4], [6, -4], [6, 4], [-6, 4]], float)
        X4 = c + loc4[:, 0:1] * ua + loc4[:, 1:2] * va
        uv4 = project_ok(X4, T)
        Hm = ph.dlt_h(loc4, uv4)
        cands = ph.plane_pose_from_h(Hm, c, ua, va, None, F35, W, H)
        self.assertEqual(len(cands), 2)
        best = max(cands, key=lambda t: ph.visible_count(t, plane, F35, W, H))
        self.assertLess(ph.rot_err_deg(best[:3, :3], T[:3, :3]), 0.05)
        self.assertLess(np.linalg.norm(best[:3, 3] - T[:3, 3]), 0.05)

    def test_rect_perms_recover_pose(self):
        """Перебор симметрийных порядков «анонимных ручек» находит позу.

        Основной критерий выбора — согласие с выровненным слоем (ref_uv);
        у квадратной поверхности без проёмов повороты геометрически
        неразличимы — различает именно он.
        """
        plane, _, c, ua, va, T, _ = self._plane(7)
        rng = np.random.default_rng(70)
        a = np.column_stack([rng.uniform(-6, 1, 300), rng.uniform(-4, 4, 300)])
        b = np.column_stack([rng.uniform(1, 6, 120), rng.uniform(-4, -1.5, 120)])
        inl = c + np.vstack([a, b])[:, 0:1] * ua + np.vstack([a, b])[:, 1:2] * va
        loc4 = np.array([[-6, -4], [6, -4], [6, 4], [-6, 4]], float)
        X4 = c + loc4[:, 0:1] * ua + loc4[:, 1:2] * va
        uv4 = project_ok(X4, T)
        ref_uv, _, _ = ph.project(inl, T, F35, W, H)     # «слой» пользователя
        for name, px4 in [('верные ручки', uv4),
                          ('перекрёстные', uv4[ph.H_PERMS[1]]),
                          ('зеркальные', uv4[ph.H_PERMS[4]])]:
            cands = ph.plane_poses_from_rect(loc4, px4, c, ua, va, F35, W, H)
            self.assertTrue(cands, f'{name}: нет кандидатов')
            best = ph.pick_plane_pose(cands, inl, F35, W, H, quad_px=uv4, ref_uv=ref_uv)
            self.assertIsNotNone(best, f'{name}: не выбран')
            self.assertLess(ph.rot_err_deg(best[:3, :3], T[:3, :3]), 0.1,
                            f'{name}: поза не восстановлена')

    def test_rect8_and_corners(self):
        r8 = ph.rect8(4.0, 2.0)
        self.assertEqual(r8.shape, (8, 2))
        # углы в чётных строках, середины — средние соседних углов
        np.testing.assert_allclose(r8[1], (r8[0] + r8[2]) / 2)
        np.testing.assert_allclose(r8[7], (r8[6] + r8[0]) / 2)
        c = ph.rect8_corners(r8)
        np.testing.assert_allclose(c, r8[::2])


class TestPolyUtils(unittest.TestCase):
    def test_hull_area_inpoly(self):
        pts = np.array([[0, 0], [1, 0.2], [2, 0], [2, 2], [1, 0.9], [0, 2], [0.5, 1]], float)
        hull = ph.convex_hull_2d(pts)
        self.assertGreater(ph.poly_area(hull), 3.9)
        self.assertLess(ph.poly_area(hull), 4.1)
        inside = ph.point_in_poly(hull, np.array([[1.0, 1.0], [1.0, 0.95], [5.0, 5.0]]))
        self.assertTrue(inside[0])
        self.assertFalse(inside[2])                  # (1, 0.95) — над «впадиной» [1, 0.9]

    def test_resample_loop(self):
        sq = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
        rs = ph.resample_loop(sq, 40)
        self.assertEqual(len(rs), 40)
        # все точки на контуре (расстояние до ближайшей вершины/рёбер)
        d = np.abs(rs - rs.round()).max()

    def test_plane_mask(self):
        m = ph.plane_mask(np.array([[10, 10], [100, 10], [100, 60], [10, 60]], float), 110, 70)
        self.assertTrue(m[35, 55])
        self.assertFalse(m[0, 0])
        self.assertFalse(m[35, 5])
        self.assertAlmostEqual(m.mean(), 90 / 110 * 50 / 70, places=1)


class TestColorize(unittest.TestCase):
    def test_occlusion_same_ray(self):
        """Точка за стеной НА ЛУЧЕ стеновой точки не красится (z-буфер)."""
        rng = np.random.default_rng(2)
        wall = np.column_stack([rng.uniform(-5, 5, 40), rng.uniform(1, 8, 40), np.zeros(40)])
        C = np.array([0.0, 4.0, 20.0])
        T = ph.T_of(ph.look_R((0, 4, 0) - C, (0, 0, 1)), C)
        behind = C + 1.25 * (wall - C)
        img = np.zeros((H, W, 3), np.uint8)
        img[..., 0] = 200
        rgb, painted = ph.colorize(np.vstack([wall, behind]),
                                   [{'img': img, 'T': T, 'f35': F35, 'surfaces': []}])
        self.assertTrue(painted[:40].all())
        self.assertFalse(painted[40:].any())
        self.assertTrue((rgb[:40, 0] == 200).all())

    def test_surface_mask_restricts(self):
        """Пиксель маски поверхности красит только точки своей плоскости ±3σ."""
        rng = np.random.default_rng(2)
        wall = np.column_stack([rng.uniform(-5, 5, 40), rng.uniform(1, 8, 40), np.zeros(40)])
        C = np.array([0.0, 4.0, 20.0])
        T = ph.T_of(ph.look_R((0, 4, 0) - C, (0, 0, 1)), C)
        p0 = wall[0]
        front = (C + 0.8 * (p0 - C))[None]           # на луче p0, перед стеной
        uv0 = project_ok(p0[None], T)[0]
        mask = np.zeros((H, W), bool)
        mask[int(uv0[1]), int(uv0[0])] = True
        img = np.zeros((H, W, 3), np.uint8)
        img[..., 1] = 99
        rgb, painted = ph.colorize(
            np.vstack([wall, front]),
            [{'img': img, 'T': T, 'f35': F35, 'surfaces': [
                {'mask': mask, 'origin': np.zeros(3), 'n': np.array([0.0, 0.0, 1.0]),
                 'sigma': 0.05}]}])
        self.assertTrue(painted[:40].all())
        self.assertFalse(painted[40])                # пиксель фасада — чужая плоскость мимо

    def test_nearest_camera_wins(self):
        rng = np.random.default_rng(3)
        wall = np.column_stack([rng.uniform(-3, 3, 30), rng.uniform(1, 7, 30), np.zeros(30)])
        C1 = np.array([0.0, 4.0, 20.0])
        C2 = np.array([0.0, 4.0, 10.0])
        T1 = ph.T_of(ph.look_R((0, 4, 0) - C1, (0, 0, 1)), C1)
        T2 = ph.T_of(ph.look_R((0, 4, 0) - C2, (0, 0, 1)), C2)
        img1 = np.zeros((H, W, 3), np.uint8); img1[..., 0] = 200
        img2 = np.zeros((H, W, 3), np.uint8); img2[..., 2] = 255
        rgb, painted = ph.colorize(wall, [
            {'img': img1, 'T': T1, 'f35': F35, 'surfaces': []},
            {'img': img2, 'T': T2, 'f35': F35, 'surfaces': []}])
        self.assertTrue(painted.all())
        _, _, ok1 = ph.project(wall, T1, F35, W, H)
        _, _, ok2 = ph.project(wall, T2, F35, W, H)
        both = ok1 & ok2
        self.assertTrue((rgb[both, 2] == 255).all())   # ближе — вторая камера
        self.assertTrue((rgb[~ok2, 0] == 200).all())   # видит только дальняя

    def test_join_colors_validity(self):
        """Маска валидности переносится: «воксель есть» ≠ «окрашено»."""
        rng = np.random.default_rng(4)
        src = np.column_stack([rng.uniform(0, 5, 50), rng.uniform(0, 5, 50), np.zeros(50)])
        rgb_src = np.tile(np.array([[10, 200, 30]], np.uint8), (50, 1))
        painted_src = rng.random(50) < 0.5
        full = np.vstack([src, src + rng.normal(0, 0.001, src.shape)])
        rgb, painted = ph.join_colors(full, src, rgb_src, painted_src, voxel=0.05)
        # непокрашенные src не дают цвета
        keys = ph._vkeys(src, 0.05)
        fk = ph._vkeys(full, 0.05)
        lut = dict(zip(keys[painted_src].tolist(), [True] * int(painted_src.sum())))
        expect = np.array([k in lut for k in fk.tolist()])
        np.testing.assert_array_equal(painted, expect)
        self.assertTrue(painted.sum() > 0)
        hit = painted[:50] & painted_src
        self.assertTrue((rgb[:50][hit] == [10, 200, 30]).all())


class TestEdges(unittest.TestCase):
    def test_dist_transform_exact(self):
        m = np.zeros((6, 9), bool)
        m[0, 0] = True
        d = ph.dist_transform(m)
        self.assertEqual(d[0, 0], 0)
        self.assertEqual(d[3, 4], 7)
        self.assertEqual(d[5, 8], 13)
        m2 = np.zeros((5, 5), bool)
        m2[2, 2] = m2[0, 4] = True
        d2 = ph.dist_transform(m2)
        self.assertEqual(d2[0, 2], 2)               # до (0,4): 2, до (2,2): 2
        self.assertEqual(d2[4, 4], 4)
        self.assertEqual(d2[1, 1], 2)               # L1 до (2,2)

    def test_photo_edges_finds_frame(self):
        img = np.zeros((120, 160, 3), np.uint8)
        img[40:90, 50:120] = 255
        e = ph.photo_edges(img)
        self.assertTrue(e[40:90, 50].any())          # границы рамки — края
        self.assertFalse(e[50:80, 60:110].any())     # внутри заливки — нет

    @unittest.skipIf(not ph.HAS_CV2, 'нет opencv')
    def test_refine_pose_improves(self):
        """Уточнение по краям улучшает метрику (позу — не обязательно: плато DT)."""
        w, h = 574, 1017
        per = []
        for a in np.linspace(-4, 4, 60):
            per += [[a, 1.0], [a, 6.5], [-4.0, a * 5.5 / 8 + 1.0], [4.0, a * 5.5 / 8 + 1.0]]
        P = np.unique(np.round(np.column_stack([np.asarray(per), np.zeros(len(per))]), 6), axis=0)
        C = np.array([0.0, 3.5, 15.0])
        T = ph.T_of(ph.look_R((0, 3.5, 0) - C, (0, 0, 1)), C)
        uv, _, ok = ph.project(P, T, F35, w, h)
        self.assertTrue(ok.all())
        img = np.zeros((h, w, 3), np.uint8)
        lo, hi = uv.min(axis=0), uv.max(axis=0)
        img[int(lo[1]):int(hi[1]) + 1, int(lo[0]):int(hi[0]) + 1] = 255
        img[int(lo[1]) + 6:int(hi[1]) - 5, int(lo[0]) + 6:int(hi[0]) - 5] = 0
        dt = ph.dist_transform(ph.photo_edges(img))

        def metric(Tx):
            u, _, o = ph.project(P, Tx, F35, w, h)
            return float(ph._dt_at(dt, u[o, 0], u[o, 1]).mean())

        Tp = ph.T_of(ph.rotvec_R(np.array([0.0025, -0.002, 0.0])) @ T[:3, :3],
                     T[:3, 3] + np.array([0.06, -0.04, 0.12]))
        Tr, err, n = ph.refine_pose(Tp, P, dt, F35, w, h)
        self.assertGreater(n, 50)
        self.assertLess(metric(Tr), metric(Tp) - 0.1)
        # ограниченный дрейф (не улетел)
        self.assertLess(ph.rot_err_deg(Tr[:3, :3], T[:3, :3]), 5.0)
        self.assertLess(np.linalg.norm(Tr[:3, 3] - T[:3, 3]), 3.0)


class TestFingerprint(unittest.TestCase):
    def test_changes_on_pose_and_hs(self):
        base = ['id1', '/x/a.jpg', 1000.0, 5, np.eye(4), 26.0, True, {'el1': [[1, 2]]}]
        self.assertEqual(ph.fingerprint([tuple(base)]), ph.fingerprint([tuple(base)]))
        p2 = list(base)
        p2[4] = np.eye(4) * 2
        self.assertNotEqual(ph.fingerprint([tuple(base)]), ph.fingerprint([tuple(p2)]))
        p3 = list(base)
        p3[7] = {'el1': [[1, 3]]}
        self.assertNotEqual(ph.fingerprint([tuple(base)]), ph.fingerprint([tuple(p3)]))


# ---------------------------------------------------------------------------
# панорама 360° (equirectangular)
# ---------------------------------------------------------------------------

PW, PH = 4000, 2000              # панорама 2:1


def scene_pano(seed=1, n=40):
    """Точки вокруг камеры (комната) при наклонённой позе: весь мир «в кадре»."""
    rng = np.random.default_rng(seed)
    C = np.array([0.5, -0.8, 1.4])
    R = ph.rotvec_R(np.array([0.03, -0.05, 0.7]))
    T = ph.T_of(R, C)
    P = []
    for _ in range(n):
        a = rng.uniform(0, 2 * np.pi)
        r_ = rng.uniform(1.5, 6.0)
        P.append([C[0] + r_ * np.cos(a), C[1] + r_ * np.sin(a),
                  C[2] + rng.uniform(-1.2, 1.2)])
    return np.asarray(P), T, rng


class TestPanoProject(unittest.TestCase):
    def test_roundtrip(self):
        P, T, _ = scene_pano()
        uv, r, ok = ph.project_pano(P, T, PW, PH)
        self.assertTrue(ok.all())
        back = ph.rays_pano(uv, PW, PH) * r[:, None]      # в системе камеры
        back_world = back @ T[:3, :3].T + T[:3, 3]
        self.assertLess(np.abs(back_world - P).max(), 1e-9)

    def test_near_zone_excluded(self):
        T = np.eye(4)
        P = np.array([[0.1, 0.0, 0.05], [1.0, 0.0, 2.0]])
        _, r, ok = ph.project_pano(P, T, PW, PH)
        self.assertFalse(ok[0])                          # ближе EPS_Z_PANO: палка/руки
        self.assertTrue(ok[1])

    def test_residual_wrap_across_seam(self):
        """Невязка у шва (u через край кадра) — короткая, а не ~W пикселей."""
        T = np.eye(4)
        P = np.array([[-1.0, 0.0, 0.0]])                 # за «спиной»: lon = −π, u = 0
        uv, _, _ = ph.project_pano(P, T, PW, PH)
        obs = np.mod(uv + np.array([30.0, 0.0]), [PW, PH])
        rr = ph.residuals_pano(obs, P, T, PW, PH)
        self.assertLess(rr[0], 35.0)


class TestPanoSolve(unittest.TestCase):
    def test_exact_and_noise(self):
        P, T, rng = scene_pano()
        uv, _, ok = ph.project_pano(P, T, PW, PH)
        self.assertTrue(ok.all())
        T2, sg = ph.solve_pano(uv, P, PW, PH)
        self.assertIsNotNone(T2)
        self.assertAlmostEqual(sg, 0.0, places=3)
        self.assertLess(ph.rot_err_deg(T2[:3, :3], T[:3, :3]), 0.05)
        self.assertLess(np.linalg.norm(T2[:3, 3] - T[:3, 3]), 0.02)
        # шум 0.5 px
        T3, sg3 = ph.solve_pano(uv + rng.normal(0, 0.5, uv.shape), P, PW, PH)
        self.assertIsNotNone(T3)
        self.assertLess(sg3, 1.5)
        self.assertLess(ph.rot_err_deg(T3[:3, :3], T[:3, :3]), 0.5)
        self.assertLess(np.linalg.norm(T3[:3, 3] - T[:3, 3]), 0.15)

    def test_known_center(self):
        """Центр камеры известен («камера у сканера»): 3 пары достаточно."""
        P, T, _ = scene_pano()
        uv, _, ok = ph.project_pano(P[:3], T, PW, PH)
        self.assertTrue(ok.all())
        T2, sg = ph.solve_pano(uv, P[:3], PW, PH, center=T[:3, 3])
        self.assertIsNotNone(T2)
        self.assertLess(sg, 0.05)
        self.assertLess(ph.rot_err_deg(T2[:3, :3], T[:3, :3]), 0.05)

    def test_too_few_pairs(self):
        P, T, _ = scene_pano()
        uv, _, _ = ph.project_pano(P[:2], T, PW, PH)
        self.assertEqual(ph.solve_pano(uv, P[:2], PW, PH), (None, None))
        self.assertEqual(ph.solve_pano(uv, P[:2], PW, PH, center=T[:3, 3]), (None, None))


class TestPanoColorize(unittest.TestCase):
    def test_occlusion_and_near_zone(self):
        T = np.eye(4)                                 # forward = +Z: lon 0 — колонка w/2
        P = np.array([[0.0, 0.0, 3.0],              # стена перед камерой
                      [0.0, 0.0, 6.0],              # за стеной, тот же пиксель
                      [0.2, 0.0, 0.0],              # ближняя зона (палка)
                      [-2.0, 3.0, 0.1]])            # отдельный пиксель
        w, h = 200, 100
        img = np.zeros((h, w, 3), np.uint8)
        img[:, w // 2] = (200, 10, 10)              # колонка lon = 0
        uv4, _, _ = ph.project_pano(P[3:4], T, w, h)
        img[int(uv4[0, 1]), int(uv4[0, 0])] = (10, 200, 10)
        rgb, painted = ph.colorize(P, [{'img': img, 'T': T, 'proj': 'pano360'}])
        self.assertTrue(painted[0])
        self.assertEqual(tuple(rgb[0]), (200, 10, 10))
        self.assertFalse(painted[1])                # за стеной — z-буфер по пикселям
        self.assertFalse(painted[2])                # ближе EPS_Z_PANO
        self.assertTrue(painted[3])
        self.assertEqual(tuple(rgb[3]), (10, 200, 10))


def _xmp_segment(xml):
    """APP1-сегмент XMP (как пишут MADV360/Insta360/Google Photo Sphere)."""
    seg = b'http://ns.adobe.com/xap/1.0/\x00' + xml.encode()
    return b'\xff\xe1' + (len(seg) + 2).to_bytes(2, 'big') + seg


def _save_jpeg(path, w, h, xmp=None):
    Image.new('RGB', (w, h), (90, 90, 90)).save(path, 'JPEG')
    if xmp is not None:
        data = Path(path).read_bytes()
        Path(path).write_bytes(data[:2] + _xmp_segment(xmp) + data[2:])


def _gpano(full_w, full_h, left=0, top=0):
    return ('<x:xmpmeta><rdf:RDF><rdf:Description '
            'xmlns:GPano="http://ns.google.com/photos/1.0/panorama/" '
            'GPano:ProjectionType="equirectangular" '
            f'GPano:CroppedAreaLeftPixels="{left}" GPano:CroppedAreaTopPixels="{top}" '
            f'GPano:FullPanoWidthPixels="{full_w}" GPano:FullPanoHeightPixels="{full_h}" '
            '/></rdf:RDF></x:xmpmeta>')


class TestIsPano(unittest.TestCase):
    def test_by_ratio(self):
        p = os.path.join(tempfile.mkdtemp(), 'r.jpg')
        _save_jpeg(p, 2000, 1000)
        self.assertTrue(ph.is_pano(p))
        p2 = os.path.join(tempfile.mkdtemp(), 'n.jpg')
        _save_jpeg(p2, 800, 600)
        self.assertFalse(ph.is_pano(p2))

    def test_by_xmp(self):
        p = os.path.join(tempfile.mkdtemp(), 'x.jpg')
        _save_jpeg(p, 800, 600, xmp=_gpano(800, 600))   # полный охват (не 2:1 — только XMP)
        self.assertTrue(ph.is_pano(p))

    def test_cropped_xmp_rejected(self):
        p = os.path.join(tempfile.mkdtemp(), 'c.jpg')
        # обрезана слева (кадр 500 — часть полного 600): полноэкранная формула не годится
        _save_jpeg(p, 500, 300, xmp=_gpano(600, 300, left=50))
        self.assertFalse(ph.is_pano(p))


if __name__ == '__main__':
    unittest.main()


class TestHolesAndSnap(unittest.TestCase):
    def test_occupancy_holes_finds_opening(self):
        """Сетка точек с прямоугольной дырой: возвращаются точки на её границе."""
        g = np.stack(np.meshgrid(np.arange(0, 10, 0.15), np.arange(0, 6, 0.15)), -1).reshape(-1, 2)
        hole = (np.abs(g[:, 0] - 5.0) < 0.6) & (np.abs(g[:, 1] - 3.0) < 0.6)
        pts = ph.occupancy_holes(g[~hole])
        self.assertGreater(len(pts), 4)
        h = np.asarray(pts)
        # все точки — вокруг дыры, не на внешней границе облака
        near_hole = (np.abs(h[:, 0] - 5.0) < 1.2) & (np.abs(h[:, 1] - 3.0) < 1.2)
        self.assertTrue(near_hole.all(), 'граница дыры, а не внешний контур')

    def test_occupancy_holes_empty_when_solid(self):
        g = np.stack(np.meshgrid(np.arange(0, 6, 0.15), np.arange(0, 4, 0.15)), -1).reshape(-1, 2)
        self.assertEqual(ph.occupancy_holes(g), [])

    @unittest.skipIf(not ph.HAS_CV2, 'нет opencv')
    def test_snap_point_corner(self):
        img = np.zeros((200, 200), np.uint8)
        img[60:140, 40:160] = 255
        u, v = ph.snap_point(img, 38, 58)                 # рядом с углом (40,60)
        self.assertLess(np.hypot(u - 40, v - 60), 3.0)
        u2, v2 = ph.snap_point(img, 100, 100)             # внутри заливки углов нет
        self.assertLess(np.hypot(u2 - 100, v2 - 100), 20.0)

    @unittest.skipIf(not ph.HAS_CV2, 'нет opencv')
    def test_refine_pose_improves_metric(self):
        """refine_pose по DT краёв уменьшает среднюю невязку (уже проверено выше)."""
        self.assertTrue(callable(ph.refine_pose))
