#!/usr/bin/env python3
"""
Сеанс стыковки статических сканов - логика интерактивного инструмента (scan_gui.py)
без зависимости от окна: её можно тестировать и вызывать из скриптов.

Системы координат:
  - исходная система скана (как в файле; у экспорта HMI «вверх» = -Z);
  - каноническая система скана (planes.py): +Z вверх, начало - сканер;
  - ОБЩАЯ система сеанса = каноническая система опорного скана, повёрнутая на
    поправку горизонта проекта L (`level_project`): Tc = L * R_ref * pose * R_scan^T.
    В ней рисуется сцена, выбираются точки и задаются позы `Tc` (канон. скана -> общая).
  - в project.json, как и раньше, `pose` - исходная скана -> исходная опорного.

Рёбра графа: автоматические пары (plane_register.register_pair) и ручные
(ручная стыковка, принятый кандидат). Пользователь может принудительно
принять/отклонить любое ребро; позы пересчитываются по графу.
"""

import json
import time
import threading
import itertools
from pathlib import Path

import numpy as np

import planes
import plane_register as pr
import scan_project as sp
import openings as op_mod
import reflections
import manual_clean
import project_store
from scan_tree import Tree


PALETTE = np.array([[31, 119, 180], [255, 127, 14], [44, 160, 44], [214, 39, 40],
                    [148, 103, 189], [140, 86, 75], [227, 119, 194], [127, 127, 127],
                    [188, 189, 34], [23, 190, 207]]) / 255.0
DISPLAY_VOXEL = 0.04
DROP_VOXEL = 0.05          # удалённые точки (движущиеся объекты) — воксели этого размера
MANUAL_INFO = 1e3          # «жёсткость» ручных рёбер в оптимизации графа


def homog(R):
    return pr.make_T(np.asarray(R), np.zeros(3))


# ── скан ───────────────────────────────────────────────────────────────────
class Scan:
    def __init__(self, path, up='auto', pose=None, sid=None):
        self.path = str(Path(path).resolve())
        self.id = sid or Path(path).name
        self.up = up
        self.pose = None if pose is None else np.asarray(pose, float)   # исходная -> исходная опорного
        self.visible = True
        self.clean = True            # убирать отражения
        self.erase = []              # ручная чистка: области (4 плоскости) в канон. системе
        self.drop = np.zeros(0, np.int64)   # удалённые воксели DROP_VOXEL (канон. система)
        self.dyn = None              # оценка «движущийся объект» по точкам res['down'] (кеш)
        self._down_cache = None
        self.color = PALETTE[0]
        self._res = None
        self._ghost = None
        self._reflect_report = None
        self._openings = None
        self._quick_R = None         # вертикаль до полного анализа (быстрый показ)
        self.source = None           # откуда скан: метаданные реконструкции из bag
        self.from_cache = False      # анализ взят из кеша проекта
        self.cache_dirty = False     # анализ посчитан и ещё не записан в кеш
        self._lock = threading.RLock()

    @property
    def analyzed(self):
        return self._res is not None and self._ghost is not None and self._openings is not None

    # анализ - лениво, один раз, потокобезопасно (фоновый анализ и действия пользователя)
    @property
    def res(self):
        with self._lock:
            if self._res is None:
                up = self.up if self.up != 'auto' or self._quick_R is None else self._quick_up
                self._res = planes.analyze_scan(self.path, up, cache=False)
                self.up = self._res['up']
                self.cache_dirty = True
            return self._res

    @property
    def R_up(self):
        if self._res is not None:
            return np.asarray(self._res['R_up'])
        if self._quick_R is not None:
            return self._quick_R
        return np.asarray(self.res['R_up'])

    def quick_points(self, voxel):
        """
        Облако для показа до полного анализа: чтение, вертикаль, прореживание.
        Отражения и ручная чистка здесь не учитываются (заменится после анализа).
        """
        import open3d as o3d
        pts = planes.load_points(self.path)
        with self._lock:
            if self._quick_R is None and self._res is None:
                label = self.up if self.up != 'auto' else planes.detect_up(pts)[0]
                self._quick_up = label
                self._quick_R = planes.up_rotation(label)
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts @ self.R_up.T))
        P = np.asarray(pc.voxel_down_sample(voxel).points)
        if self.erase or len(self.drop):
            P = P[self.keep_mask(P)]
        return P

    @property
    def down(self):
        """Даунсемпл в канонической системе с учётом чистки отражений и ручной чистки."""
        key = (self.clean, len(self.erase), len(self.drop))
        if self._down_cache is None or self._down_cache[0] != key:
            d = self.res['down']
            drop = self.ghost_mask() if self.clean else np.zeros(len(d), bool)
            if self.erase:
                drop = drop | manual_clean.inside_regions(d, self.erase)
            if len(self.drop):
                drop = drop | np.isin(voxel_keys(d), self.drop)
            self._down_cache = (key, d[~drop])
        return self._down_cache[1]

    def keep_mask(self, P_canon, clean=None):
        """Маска сохраняемых точек произвольного облака в канон. системе (ручная чистка)."""
        keep = np.ones(len(P_canon), bool)
        if self.erase:
            keep &= ~manual_clean.inside_regions(P_canon, self.erase)
        if len(self.drop):
            keep &= ~np.isin(voxel_keys(P_canon), self.drop)
        return keep

    def ghost_mask(self):
        with self._lock:
            if self._ghost is None:
                self._ghost, self._reflect_report = reflections.find_reflections(self.res['down'], self.res)
                self.cache_dirty = True
            return self._ghost

    @property
    def reflect_report(self):
        self.ghost_mask()
        return self._reflect_report

    @property
    def openings(self):
        with self._lock:
            if self._openings is None:
                self._openings = op_mod.find_openings(self.res)
                self.cache_dirty = True
            return self._openings

    @property
    def planes(self):
        return self.res['planes']

    def analyze(self):
        """Полный анализ (плоскости, отражения, проёмы)."""
        self.res
        self.ghost_mask()
        self.openings
        return self

    # ── кеш анализа (project_store) ─────────────────────────────────────
    def cache_key(self):
        return project_store.scan_key(self.path)

    def to_cache(self):
        r = self.analyze().res
        pl = r['planes']
        idx = [np.asarray(p.inliers, np.int64) for p in pl]
        off = np.cumsum([0] + [len(i) for i in idx])
        meta = {'key': self.cache_key(), 'up': r['up'], 'up_info': _jsonable(r.get('up_info', {})),
                'R_up': np.asarray(r['R_up']).tolist(), 'voxel': r['voxel'],
                'n_points': r['n_points'], 'layers': _jsonable(r['layers']),
                'planes': [p.to_json() for p in pl],
                'openings': [_jsonable(o.to_json()) for o in self._openings],
                'reflect_report': _jsonable(self._reflect_report)}
        arrays = {'down': np.asarray(r['down'], np.float32),
                  'inliers': np.concatenate(idx).astype(np.int32) if idx else np.zeros(0, np.int32),
                  'inl_off': off.astype(np.int64),
                  'ghost': np.asarray(self._ghost, bool)}
        return meta, arrays

    def load_cache(self, meta, arrays):
        """Взять анализ из кеша; False — несовместим (другая вертикаль)."""
        if self.up not in ('auto', meta['up']):
            return False
        down = np.asarray(arrays['down'], np.float64)
        idx, off = arrays['inliers'], arrays['inl_off']
        pls = []
        for k, pj in enumerate(meta['planes']):
            pls.append(planes.Plane(**pj, inliers=np.asarray(idx[off[k]:off[k + 1]], np.int64)))
        with self._lock:
            self._res = {'scan': Path(self.path).name, 'up': meta['up'], 'up_info': meta.get('up_info', {}),
                         'R_up': meta['R_up'], 'voxel': meta['voxel'], 'n_points': meta['n_points'],
                         'layers': meta['layers'], 'planes': pls, 'down': down}
            self._ghost = np.asarray(arrays['ghost'], bool)
            self._reflect_report = meta.get('reflect_report', [])
            self._openings = [op_mod.Opening(**o) for o in meta.get('openings', [])]
            self.up = meta['up']
            self._down_cache = None
            self.from_cache, self.cache_dirty = True, False
        return True

    def to_json(self):
        return {'id': self.id, 'path': self.path, 'up': self.up,
                'pose': None if self.pose is None else self.pose.tolist(),
                'clean': self.clean,
                'erase': [np.asarray(r).tolist() for r in self.erase],
                'drop': np.asarray(self.drop).tolist(),
                'source': self.source}


# ── сеанс ──────────────────────────────────────────────────────────────────
class Session:
    def __init__(self):
        self.scans = []              # [Scan]
        self.frame = None            # id опорного скана
        self.edges = []              # пары/рёбра (dict, как в scan_project)
        self.project_path = None
        self.level = np.eye(3)       # поправка горизонта проекта (поворот общей системы)
        self.erase_undo = []         # [(scan_id, число добавленных областей)]
        self.tree = Tree()           # иерархия сканов (организация, видимость веток)
        self.meshes = {}             # построенные поверхности (surface.py): только в памяти, не сохраняются
        self.zero = None             # нулевой уровень {'z', 'source'} (общая система); None — пол опорного
        self.measures = []           # замеры (measure.py), точки в общей системе
        self.section = None          # сечение (section.py), общая система
        self.lock = threading.RLock()

    # ── загрузка / сохранение ─────────────────────────────────────────────
    @classmethod
    def from_project(cls, path):
        s = cls()
        proj = sp.load_project(path)
        for e in proj['scans']:
            sc = Scan(e['path'], e.get('up', 'auto'), e.get('pose'), e['id'])
            sc.clean = e.get('clean', True)
            sc.erase = [np.asarray(r, float) for r in e.get('erase', [])]
            sc.drop = np.asarray(e.get('drop', []), np.int64)
            sc.source = e.get('source')
            s.scans.append(sc)
        s.frame = proj.get('frame') or s.scans[0].id
        s.level = np.asarray(proj.get('level', np.eye(3).tolist()), float)
        s.edges = proj.get('pairs', []) + proj.get('manual_edges', [])
        s.tree = Tree.from_json(proj.get('tree'), [x.id for x in s.scans])
        s.zero = proj.get('zero')
        s.measures = proj.get('measures', [])
        s.section = proj.get('section')
        s.apply_visibility()
        s.project_path = str(path)
        s._recolor()
        return s

    @classmethod
    def from_scans(cls, paths):
        s = cls()
        for p in paths:
            s.add_scan(p)
        return s

    def add_scan(self, path, source=None, group=None):
        sc = Scan(path)
        if self.by_id(sc.id) is not None:
            return self.by_id(sc.id)
        sc.source = source
        self.scans.append(sc)
        self.tree.sync([x.id for x in self.scans])
        if group and self.tree.group(group) is not None:
            self.tree.move(sc.id, group)
        if self.frame is None:
            self.frame = sc.id
            sc.pose = np.eye(4)
        self._recolor()
        return sc

    def load_cached(self):
        """Взять анализ сканов из кеша проекта (.pulse), где ключ совпадает. → число сканов."""
        n = 0
        for sc in self.scans:
            if sc.analyzed:
                continue
            try:
                c = project_store.read_cache(self.project_path, sc.id, sc.cache_key())
            except OSError:
                c = None
            if c is not None and sc.load_cache(*c):
                n += 1
        return n

    def flush_cache(self):
        """
        Дописать свежий анализ в архив проекта, не трогая project.json
        (несохранённые правки проекта не записываются). → число сканов.
        """
        if not self.project_path or not project_store.is_archive(self.project_path) \
                or not Path(self.project_path).exists():
            return 0
        caches = {sc.id: sc.to_cache() for sc in self.scans if sc.analyzed and sc.cache_dirty}
        if caches and project_store.update_cache(self.project_path, caches):
            for sc in self.scans:
                if sc.id in caches:
                    sc.cache_dirty = False
        return len(caches)

    def save(self, path=None):
        path = path or self.project_path
        auto = [e for e in self.edges if e.get('method') != 'manual']
        manual = [e for e in self.edges if e.get('method') == 'manual']
        proj = {'frame': self.frame, 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
                'level': self.level.tolist(),
                'scans': [s.to_json() for s in self.scans],
                'tree': self.tree.to_json(),
                'zero': self.zero, 'measures': _jsonable(self.measures), 'section': _jsonable(self.section),
                'pairs': [_jsonable(e) for e in auto],
                'manual_edges': [_jsonable(e) for e in manual]}
        for e in proj['scans']:
            e['path'] = sp.rel_path(e['path'], path)
        caches = {}
        if project_store.is_archive(path) or str(path).lower().endswith('.pulse'):
            same = self.project_path and Path(self.project_path).resolve() == Path(path).resolve()
            caches = {sc.id: sc.to_cache() for sc in self.scans
                      if sc.analyzed and (sc.cache_dirty or not same)}
        keep = self.project_path if (self.project_path and project_store.is_archive(self.project_path)) else None
        project_store.write_project(path, proj, caches, keep_from=keep)
        for sc in self.scans:
            if sc.id in caches:
                sc.cache_dirty = False
        self.project_path = str(path)
        return path

    def _recolor(self):
        for i, s in enumerate(self.scans):
            s.color = PALETTE[i % len(PALETTE)]

    # ── иерархия ─────────────────────────────────────────────────────────
    def apply_visibility(self):
        """Видимость сканов = флаг скана в дереве и всех его групп-предков."""
        for sc in self.scans:
            sc.visible = self.tree.effective_visible(sc.id)

    def set_visible(self, key, visible):
        self.tree.set_visible(key, visible)
        self.apply_visibility()

    def only_show(self, keys):
        self.tree.only(keys)
        self.apply_visibility()

    def move_node(self, key, target_gid, index=None):
        """Переместить скан/группу в группу; видимость пересчитывается (скрытая группа скрывает)."""
        return self.move_nodes([key], target_gid, index)

    def move_nodes(self, keys, target_gid, index=None):
        """Переместить несколько узлов (порядок дерева сохраняется) в группу на позицию index."""
        ok = self.tree.move_many(keys, target_gid, index)
        self.apply_visibility()
        return ok

    def group_nodes(self, keys, name, kind='прочее'):
        """Создать группу из выбранных узлов. → id группы или None."""
        gid = self.tree.group_from(keys, name, kind)
        self.apply_visibility()
        return gid

    def delete_group(self, gid):
        ok = self.tree.delete_group(gid)
        self.apply_visibility()
        return ok

    def show_all(self):
        self.tree.show_all()
        self.apply_visibility()

    def by_id(self, sid):
        return next((s for s in self.scans if s.id == sid), None)

    @property
    def ref(self):
        return self.by_id(self.frame)

    # ── системы координат ────────────────────────────────────────────────
    def Tc(self, scan):
        """Канон. система скана -> общая. None, если скан не размещён."""
        if scan.pose is None:
            return None
        return homog(self.level) @ homog(self.ref.R_up) @ scan.pose @ homog(scan.R_up).T

    def set_Tc(self, scan, Tc):
        scan.pose = homog(self.ref.R_up).T @ homog(self.level).T @ np.asarray(Tc) @ homog(scan.R_up)

    # ── горизонт ─────────────────────────────────────────────────────────
    @staticmethod
    def _plane_normals(scan, R, min_area=1.0):
        ns, ws = [], []
        for p in scan.planes:
            if p.area >= min_area:
                ns.append(R @ np.asarray(p.normal))
                ws.append(p.area)
        return ns, ws

    def level_pose(self, scan, T):
        """
        Поза скана T (канон. -> общая) с исправленным наклоном: пол/земля горизонтальны,
        стены вертикальны. Поворот вокруг точки сканера. -> (T', info).
        """
        T = np.asarray(T, float)
        ns, ws = self._plane_normals(scan, T[:3, :3])
        Rc, info = pr.level_correction(ns, ws)
        T2 = T.copy()
        T2[:3, :3] = Rc @ T[:3, :3]
        return T2, info

    def level_project(self):
        """
        Поправка горизонта всего проекта по полам и стенам всех размещённых сканов:
        поворачивает общую систему (позы сканов друг относительно друга не меняются).
        """
        ns, ws = [], []
        for sc in self.placed():
            n, w = self._plane_normals(sc, self.Tc(sc)[:3, :3])
            ns += n
            ws += w
        Rc, info = pr.level_correction(ns, ws)
        self.level = Rc @ self.level
        return info

    def reset_level(self):
        self.level = np.eye(3)

    @staticmethod
    def _wall_direction(scans_T, min_area=1.0):
        """Манхэттенское направление стен (mod 90°, рад) группы сканов в общей системе."""
        a, w = [], []
        for sc, T in scans_T:
            for p in sc.planes:
                if p.kind == 'wall' and p.area >= min_area:
                    n = T[:3, :3] @ np.asarray(p.normal)
                    if abs(n[2]) < 0.3:
                        a.append(np.arctan2(n[1], n[0]))
                        w.append(p.area)
        if not a:
            return None
        z = np.sum(np.asarray(w) * np.exp(4j * np.asarray(a)))
        return float(np.angle(z) / 4)

    def align_plan_to_walls(self):
        """
        Повернуть общую систему вокруг вертикали так, чтобы стены размещённых сканов
        легли вдоль осей X/Y (в виде сверху — по краям экрана). → поворот, градусы.
        """
        th = self._wall_direction([(sc, self.Tc(sc)) for sc in self.placed()])
        if th is None:
            return None
        self.level = pr.rot_z(-th) @ self.level
        return float(np.degrees(-th))

    def rotate_plan(self, deg):
        """Тонкий поворот всего плана вокруг вертикали (общая система)."""
        self.level = pr.rot_z(np.radians(deg)) @ self.level

    def snap_yaw_to_walls(self, moving, T, others=None, max_deg=45.0):
        """
        Довернуть подвижный скан вокруг вертикали так, чтобы его стены стали
        параллельны стенам размещённых сканов (ближайший угол mod 90°). → (T', поворот°).
        """
        others = [s for s in (others or self.placed()) if s is not moving]
        th_ref = self._wall_direction([(sc, self.Tc(sc)) for sc in others])
        th_mov = self._wall_direction([(moving, np.asarray(T))])
        if th_ref is None or th_mov is None:
            return np.asarray(T), None
        d = (th_ref - th_mov + np.pi / 4) % (np.pi / 2) - np.pi / 4      # в [-45°, 45°)
        if abs(np.degrees(d)) > max_deg:
            return np.asarray(T), None
        return self.nudge(T, dyaw_deg=float(np.degrees(d))), float(np.degrees(d))

    def state_signature(self):
        """Сводка состояния для проверки «есть несохранённые изменения»."""
        parts = [self.frame, np.round(self.level, 6).tobytes()]
        for sc in self.scans:
            parts += [sc.id, sc.clean, len(sc.erase), len(sc.drop),
                      None if sc.pose is None else np.round(sc.pose, 6).tobytes()]
        for e in self.edges:
            parts += [e['A'], e['B'], e.get('user'), e.get('method')]
        parts += [None if self.zero is None else round(self.zero['z'], 6), len(self.measures)]
        st = self.section or {}
        parts += [st.get('mode'), st.get('base'), round(st.get('c', 0), 4), st.get('a'), st.get('b'),
                  st.get('thick'), st.get('flip')]
        return hash(tuple(parts))

    def zero_z(self):
        """Высота нулевого уровня в общей системе: заданная или пол опорного скана (иначе 0)."""
        if self.zero is not None:
            return float(self.zero['z'])
        import measure
        z = measure.default_zero(self)
        return 0.0 if z is None else z

    def placed(self):
        return [s for s in self.scans if s.pose is not None]

    def display_points(self, scan, voxel=DISPLAY_VOXEL):
        """Точки скана для показа, в его канонической системе (поза - отдельно)."""
        import open3d as o3d
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(scan.down))
        if voxel > (scan.res['voxel'] or 0):
            pc = pc.voxel_down_sample(voxel)
        return np.asarray(pc.points)

    # ── автостыковка ─────────────────────────────────────────────────────
    def run_auto(self, progress=None, yaw='manhattan', reuse=True, by_tree=None, combos=None):
        """
        Автоматическая стыковка, затем позы по графу.
        by_tree=True — только пары по дереву (внутри групп + представители соседних
        веток), иначе все пары. По умолчанию — по дереву, если в нём есть группы.
        combos — явный список пар (например, только внутри групп — within_group_pairs).
        """
        names = [s.id for s in self.scans]
        old = {(e['A'], e['B']): e for e in self.edges if e.get('method') not in ('manual', 'group')}
        manual = [e for e in self.edges if e.get('method') in ('manual', 'group')]
        if by_tree is None:
            by_tree = not self.tree.is_flat()
        if combos is not None:
            order = {n: i for i, n in enumerate(names)}
            combos = [tuple(sorted(c, key=order.get)) for c in combos if c[0] in order and c[1] in order]
        elif by_tree:
            weight = {sc.id: sc.res.get('n_points', 0) for sc in self.scans}
            combos = self.tree.registration_pairs(names, weight)
        else:
            combos = list(itertools.combinations(names, 2))
        chosen = set(combos)
        edges = []
        for i, (a, b) in enumerate(combos):
            if progress:
                progress(i / max(1, len(combos)), f"стыковка {a} <- {b}")
            if reuse and (a, b) in old and 'T_canon' in old[(a, b)]:
                e = dict(old[(a, b)])
            else:
                sa, sb = self.by_id(a), self.by_id(b)
                e = pr.register_pair(_clean_res(sa), _clean_res(sb), yaw, verbose=False)
                e['A'], e['B'] = a, b
                if (a, b) in old and 'user' in old[(a, b)]:
                    e['user'] = old[(a, b)]['user']
            e['auto_ok'] = sp.edge_ok(e)
            edges.append(e)
        # ранее посчитанные пары вне выбранных — тоже данные, не выбрасываем
        edges += [dict(e, auto_ok=sp.edge_ok(e)) for k, e in old.items()
                  if k not in chosen and 'T_canon' in e and k[0] in names and k[1] in names]
        with self.lock:
            self.edges = edges + manual
        if progress:
            progress(1.0, "оптимизация графа поз")
        self.recompute_poses()
        if progress:
            progress(1.0, "готово")

    # ── стыковка групп ───────────────────────────────────────────────────
    def within_group_pairs(self, gid=None):
        """
        Пары для «стыковки внутри групп»: выбранная группа — все пары её сканов (с
        подгруппами); без выбора — в каждой группе пары её собственных сканов.
        """
        if gid is not None and gid != 'root' and self.tree.group(gid) is not None:
            return list(itertools.combinations(self.tree.scans_in(gid), 2))
        out = []
        for g in self.tree.groups():
            if g is self.tree.root:
                continue
            own = [c.scan for c in g.children if not hasattr(c, 'children')]
            out += list(itertools.combinations(own, 2))
        return out

    def group_frame(self, gid):
        """
        Группа как жёсткое целое по её внутренним активным связям (независимо от связи
        с опорным сканом). → (главный скан, {id: поза канон. скана → канон. главного}).
        """
        ids = [i for i in self.tree.scans_in(gid) if self.by_id(i) is not None and self.by_id(i).analyzed]
        if not ids:
            return None, {}
        inside = [e for e in self.active_edges() if e['A'] in ids and e['B'] in ids and 'T_canon' in e]
        root = max(ids, key=lambda i: len(self.by_id(i).down))
        return root, sp.spanning_poses(ids, inside, root)

    def group_res(self, gid):
        """Облако группы в системе её главного скана + плоскости и слои (как у одного скана)."""
        root, poses = self.group_frame(gid)
        if root is None:
            return None
        P = np.vstack([pr.transform(self.by_id(i).down, T) for i, T in poses.items()])
        down, pl = planes.extract_planes(P)
        return {'scan': root, 'up': '+z', 'R_up': np.eye(3).tolist(), 'voxel': planes.VOXEL,
                'n_points': int(len(P)), 'layers': planes.horizontal_layers(P), 'planes': pl, 'down': down,
                'members': sorted(poses), 'group': gid}

    def register_groups(self, gid=None, progress=None, yaw='manhattan', reps=2):
        """
        Стыковка групп между собой: каждая группа — жёсткое целое (облако по внутренним
        связям); пары групп — дочерние группы выбранной (или корня).
        Гипотезы позы группы B относительно A: по плоскостям объединённых облаков, по парам
        reps крупнейших сканов групп (как обычная автостыковка) и текущая взаимная поза
        (если обе группы уже размещены). Каждая уточняется ICP по объединённым облакам и
        оценивается одинаково (ConsistencyScorer); лучшая — ребро 'group' между главными
        сканами групп; затем позы по графу. → [(группа A, группа B, ребро)].
        """
        parent = self.tree.group(gid) if gid else None
        parent = parent or self.tree.root
        kids = [c for c in parent.children if hasattr(c, 'children') and self.tree.scans_in(c.id)]
        if len(kids) < 2:
            raise ValueError('нужно хотя бы две группы (дочерние группы выбранной ветки или корня)')
        res = {}
        for k, g in enumerate(kids):
            if progress:
                progress(0.15 * k / len(kids), f'облако группы «{g.name}»')
            r = self.group_res(g.id)
            if r is not None:
                r['poses'] = self.group_frame(g.id)[1]
                res[g.id] = r
        out = []
        combos = list(itertools.combinations([g for g in kids if g.id in res], 2))
        for k, (ga, gb) in enumerate(combos):
            say = (lambda m, k=k: progress(0.15 + 0.8 * k / max(1, len(combos)), m)) if progress else (lambda m: None)
            say(f'группы «{ga.name}» ← «{gb.name}»: гипотезы')
            e = self._register_group_pair(res[ga.id], res[gb.id], yaw, reps, say)
            e.update(A=res[ga.id]['scan'], B=res[gb.id]['scan'], method='group',
                     source=f'группы «{ga.name}» ← «{gb.name}»', groups=[ga.id, gb.id])
            e['auto_ok'] = sp.edge_ok(e)
            self.edges = [x for x in self.edges if not (x.get('method') == 'group' and
                                                         {x['A'], x['B']} == {e['A'], e['B']})]
            self.edges.append(e)
            out.append((ga.name, gb.name, e))
        if progress:
            progress(0.97, 'оптимизация графа поз')
        self.recompute_poses()
        return out

    def _register_group_pair(self, ra, rb, yaw, reps, say):
        """Поза группы rb в системе главного скана группы ra (см. register_groups)."""
        hyps = []
        try:
            e0 = pr.register_pair(ra, rb, yaw, verbose=False)
            hyps.append(('плоскости групп', np.asarray(e0['T_canon'])))
        except Exception:                                # noqa: BLE001
            pass
        A0, B0 = self.by_id(ra['scan']), self.by_id(rb['scan'])
        if A0.pose is not None and B0.pose is not None:
            hyps.append(('текущая', np.linalg.inv(self.Tc(A0)) @ self.Tc(B0)))
        big = lambda r: sorted(r['poses'], key=lambda i: -len(self.by_id(i).down))[:reps]
        for a in big(ra):
            for b in big(rb):
                say(f'пара сканов {a} ← {b}')
                try:
                    e = pr.register_pair(_clean_res(self.by_id(a)), _clean_res(self.by_id(b)), yaw, verbose=False)
                except Exception:                        # noqa: BLE001
                    continue
                T = ra['poses'][a] @ np.asarray(e['T_canon']) @ np.linalg.inv(rb['poses'][b])
                hyps.append((f'{a} ← {b}', T))
        scorer = pr.ConsistencyScorer(ra['down'], rb['down'])
        scored = []
        for name, T in hyps:
            T2, fit, rmse = pr.refine_icp(ra['down'], rb['down'], T)
            sc, close, viol = scorer.score(T2)
            scored.append({'T': T2, 'score': sc, 'close': close, 'viol': viol, 'fitness': fit, 'rmse': rmse,
                           'from': name})
        scored.sort(key=lambda h: -h['score'])
        best = scored[0]
        second = next((h['score'] for h in scored[1:] if pr.pose_delta(h['T'], best['T'])[0] > 0.1
                       or pr.pose_delta(h['T'], best['T'])[1] > 1), 0.0)
        return {'T_canon': best['T'].tolist(), 'score': float(best['score']), 'close': float(best['close']),
                'violations': float(best['viol']), 'second_score': float(second),
                'margin': float(best['score'] - second), 'fitness': float(best['fitness']),
                'rmse': float(best['rmse']), 'hypothesis': best['from'], 'n_hypotheses': len(scored),
                'yaw_deg': float(np.degrees(np.arctan2(best['T'][1, 0], best['T'][0, 0])))}

    @staticmethod
    def edge_active(e):
        if e.get('user') == 'accept':
            return True
        if e.get('user') == 'reject':
            return False
        if e.get('method') == 'manual':
            return True
        return bool(e.get('auto_ok', sp.edge_ok(e)))

    def set_edge_user(self, idx, state):
        """state: 'accept' | 'reject' | None (как решит автоматика)."""
        e = self.edges[idx]
        if state is None:
            e.pop('user', None)
        else:
            e['user'] = state
        self.recompute_poses()

    def active_edges(self):
        return [e for e in self.edges if self.edge_active(e)]

    def loops(self):
        return sp.loop_checks(self.active_edges(), [s.id for s in self.scans])

    def recompute_poses(self):
        """Позы всех сканов, связанных с опорным активными рёбрами."""
        edges = self.active_edges()
        names = [s.id for s in self.scans]
        poses = sp.spanning_poses(names, edges, self.frame)
        if len(edges) > len(poses) - 1 and len(poses) > 1:
            infos = []
            for e in edges:
                A, B = self.by_id(e['A']), self.by_id(e['B'])
                info = sp.information(e, _clean_res(A), _clean_res(B))
                if e.get('method') == 'manual':
                    info = info + MANUAL_INFO * np.eye(6)
                infos.append(info * float(e.get('weight', 1.0)))
            poses = sp.optimize(names, edges, poses, infos, self.frame)
        with self.lock:
            for s in self.scans:
                if s.id in poses:
                    self.set_Tc(s, homog(self.level) @ poses[s.id])   # граф — в канон. системе опорного
                elif s.id != self.frame:
                    s.pose = None
            self.ref.pose = np.eye(4)

    def set_frame(self, sid):
        """Сменить опорный скан (позы пересчитываются по графу)."""
        self.frame = sid
        self.recompute_poses()

    # ── ручная стыковка ──────────────────────────────────────────────────
    def pick_feature(self, scan, p_canon, kind='auto', max_plane_dist=0.05):
        """
        Признак скана под точкой p (канон. система скана):
          проём (если точка внутри прямоугольника проёма ±10 см),
          иначе плоскость (точка ближе max_plane_dist и рядом с её инлайерами),
          иначе - сама точка.
        """
        p = np.asarray(p_canon, float)
        if kind in ('auto', 'opening'):
            for k, o in enumerate(scan.openings):
                C = np.asarray(o.corners)
                n = np.asarray(o.normal)
                if abs((p - C[0]) @ n) > 0.3:
                    continue
                u, v = C[1] - C[0], C[3] - C[0]
                a = (p - C[0]) @ u / (u @ u)
                b = (p - C[0]) @ v / (v @ v)
                pad_u, pad_v = 0.1 / np.linalg.norm(u), 0.1 / np.linalg.norm(v)
                if -pad_u <= a <= 1 + pad_u and -pad_v <= b <= 1 + pad_v:
                    return {'type': 'opening', 'scan': scan.id, 'index': k,
                            'center': list(o.center), 'normal': list(o.normal),
                            'size': [o.width, o.height], 'p': p.tolist()}
            if kind == 'opening':
                return None
        if kind in ('auto', 'plane'):
            down = scan.res['down']
            best = None
            for pl in scan.planes:
                d = abs(np.asarray(pl.normal) @ p - pl.offset)
                if d > max_plane_dist:
                    continue
                q = down[pl.inliers]
                near = np.min(np.linalg.norm(q - p, axis=1))
                if near < 0.15 and (best is None or pl.area > best.area):
                    best = pl
            if best is not None:
                return {'type': 'plane', 'scan': scan.id, 'index': best.id,
                        'kind': best.kind, 'normal': list(best.normal), 'offset': best.offset,
                        'area': best.area, 'p': p.tolist()}
            if kind == 'plane':
                return None
        return {'type': 'point', 'scan': scan.id, 'p': p.tolist()}

    def manual_constraints(self, fixed, moving, pairs, T_fixed=None):
        """Пары признаков -> ограничения в общей системе (A = fixed в общей, B = moving канон.)."""
        Tf = self.Tc(fixed) if T_fixed is None else T_fixed
        Rf = Tf[:3, :3]
        cons_planes, cons_points, walls = [], [], []
        for fa, fb in pairs:
            if fa['type'] == 'plane' and fb['type'] == 'plane':
                nA = Rf @ np.asarray(fa['normal'])
                cA = fa['offset'] + nA @ Tf[:3, 3]
                A = planes.Plane(-1, fa.get('kind', 'wall'), nA.tolist(), float(cA), [0, 0, 0],
                                 fa.get('area', 1.0), [1, 1], [1, 0, 0], [0, 1, 0], 0, 0.0)
                B = planes.Plane(-1, fb.get('kind', 'wall'), list(fb['normal']), float(fb['offset']),
                                 [0, 0, 0], fb.get('area', 1.0), [1, 1], [1, 0, 0], [0, 1, 0], 0, 0.0)
                cons_planes.append((A, B))
            elif fa['type'] == 'opening' and fb['type'] == 'opening':
                walls.append((fa, fb))
            else:
                pA = pr.transform(np.asarray(fa['p'])[None], Tf)[0]
                cons_points.append((pA, np.asarray(fb['p'])))
        return Tf, cons_planes, cons_points, walls

    def dof_status(self, fixed, moving, pairs):
        """Какие степени свободы (z, yaw, x, y) определены выбранными парами."""
        if not pairs:
            return {'z': False, 'yaw': False, 'xy_rank': 0, 'text': 'пар нет'}
        Tf, cp, cpt, walls = self.manual_constraints(fixed, moving, pairs)
        yaw = any(a.kind == 'wall' for a, _ in cp) or len(cpt) >= 2 or bool(walls)
        rows = [np.asarray(a.normal) for a, _ in cp] + [np.eye(3)[i] for _ in cpt for i in range(3)]
        for fa, _ in walls:                     # проём: вдоль стены и по высоте
            n = Tf[:3, :3] @ np.asarray(fa['normal'])
            u = np.cross([0, 0, 1.0], n)
            rows += [u / max(np.linalg.norm(u), 1e-9), np.array([0, 0, 1.0])]
        M = np.array(rows) if rows else np.zeros((0, 3))
        z = bool(len(M) and np.abs(M[:, 2]).max() > 0.9)
        Mxy = M[:, :2] if len(M) else np.zeros((0, 2))
        rank = int(np.linalg.matrix_rank(Mxy, tol=0.2)) if len(Mxy) else 0
        missing = []
        if not yaw:
            missing.append('поворот (нужна стена или 2 точки)')
        if not z:
            missing.append('высота (пол/потолок или точка)')
        if rank < 2:
            missing.append('сдвиг в плане (ещё стена, непараллельная первой, или точка)'
                           if rank == 1 else 'сдвиг в плане (две непараллельные стены или точки)')
        txt = 'всё определено' if not missing else 'не хватает: ' + '; '.join(missing)
        if walls and not missing:
            txt += ' (толщина стены для проёмов подбирается автоматически)'
        return {'z': z, 'yaw': yaw, 'xy_rank': rank, 'text': txt}

    def solve_manual(self, fixed, moving, pairs, T_init=None, full=True):
        """Поза moving (канон. -> общая) по выбранным парам. -> (Tc, info)."""
        Tf, cp, cpt, walls = self.manual_constraints(fixed, moving, pairs)
        if walls and not cp and not cpt:
            # только проёмы: первый задаёт позу, толщина стены - перебором
            # только проёмы: первый задаёт позу. Видят ли сканы проём с одной стороны
            # стены (две комнаты) или с разных (комната <-> фасад) - пробуем оба варианта,
            # толщину стены - перебором; лучший по согласованности.
            fa, fb = walls[0]
            cA = pr.transform(np.asarray(fa['center'])[None], Tf)[0]
            nA = Tf[:3, :3] @ np.asarray(fa['normal'])
            scorer = pr.MultiScanScorer(
                [(s.down, self.Tc(s)) for s in self.placed() if s is not moving], moving.down)
            res = []
            for same in (False, True):
                for r in pr.search_opening_pair(scorer, cA, nA, fb['center'], fb['normal'],
                                                same_side=same):
                    res.append((*r, same))
            res.sort(key=lambda r: -r[0])
            s, nc, v, d, T, same = res[0]
            return T, {'method': 'opening', 'same_side': same, 'delta': None if same else d,
                       'score': s, 'violations': v}
        pts = list(cpt)
        for fa, fb in walls:                   # проём как пара центров (без толщины стены)
            n = Tf[:3, :3] @ np.asarray(fa['normal'])
            u = np.cross([0, 0, 1.0], n)
            P = np.array([u / np.linalg.norm(u), [0, 0, 1.0], [0, 0, 0]])
            pts.append((pr.transform(np.asarray(fa['center'])[None], Tf)[0],
                        np.asarray(fb['center']), P))
        T, info = pr.solve_pose(cp, pts, T_init=T_init, full=full)
        info['method'] = 'manual'
        return T, info

    def refine_icp(self, fixed_list, moving, T, wide=False, center=None):
        """
        ICP moving -> объединение fixed_list (все в общей системе).
        wide — после грубой ручной стыковки: захват до 60 см, затем 25 и 8 см;
        center — только поворот вокруг этой точки (опорная точка закреплена).
        """
        A = np.vstack([pr.transform(s.down, self.Tc(s)) for s in fixed_list])
        if center is not None:
            T2, fit, rmse = pr.refine_rotation(A, moving.down, np.asarray(T), center)
        elif wide:
            T2, fit, rmse = pr.refine_icp(A, moving.down, np.asarray(T), voxels=(0.15, 0.08, 0.04),
                                          dists=(0.6, 0.25, 0.08))
        else:
            T2, fit, rmse = pr.refine_icp(A, moving.down, np.asarray(T))
        return T2, {'fitness': fit, 'rmse': rmse, 'shift': pr.pose_delta(T, T2)}

    def pair_thickness(self, fixed_list, moving, T):
        """
        Насколько расходятся одни и те же поверхности подвижного (в позе T) и неподвижных:
        медиана толщины по плоскостям, м (None — нет общих поверхностей). → (медиана, ячеек).
        """
        import quality
        q = quality.plane_cells(self, scan_ids={moving.id} | {s.id for s in fixed_list},
                                poses={moving.id: np.asarray(T)})
        pairs = [k for k in range(len(q)) if moving.id in q.scans[k]]
        if not pairs:
            return None, 0
        return float(np.median(q.thick[pairs])), len(pairs)

    @staticmethod
    def rotate_about(T, center, droll_deg=0.0, dpitch_deg=0.0, dyaw_deg=0.0):
        """Поворот позы вокруг точки (общая система): yaw — Z, roll — X, pitch — Y."""
        return pr.rotate_about(T, center, _rot_rpy(droll_deg, dpitch_deg, dyaw_deg))

    @staticmethod
    def nudge(T, dx=0.0, dy=0.0, dz=0.0, dyaw_deg=0.0, droll_deg=0.0, dpitch_deg=0.0):
        """
        Сдвиг и повороты в общей системе вокруг точки сканера:
        yaw - вокруг вертикали Z, roll - вокруг X, pitch - вокруг Y.
        """
        T = np.asarray(T, float).copy()
        c = T[:3, 3].copy()
        r, p = np.radians(droll_deg), np.radians(dpitch_deg)
        Rx = np.array([[1, 0, 0], [0, np.cos(r), -np.sin(r)], [0, np.sin(r), np.cos(r)]])
        Ry = np.array([[np.cos(p), 0, np.sin(p)], [0, 1, 0], [-np.sin(p), 0, np.cos(p)]])
        R = pr.rot_z(np.radians(dyaw_deg)) @ Ry @ Rx
        T[:3, :3] = R @ T[:3, :3]
        T[:3, 3] = c + np.array([dx, dy, dz])
        return T

    @staticmethod
    def tilt_deg(T):
        """Наклон оси Z скана от вертикали, градусы."""
        return float(np.degrees(np.arccos(np.clip(np.asarray(T)[2, 2], -1, 1))))

    def score_pose(self, moving, Tc):
        others = [s for s in self.placed() if s is not moving]
        if not others:
            return None
        sc = pr.MultiScanScorer([(s.down, self.Tc(s)) for s in others], moving.down)
        s, nc, v = sc.score(np.asarray(Tc))
        return {'score': s, 'n_close': nc, 'violations': v}

    def accept_pose(self, moving, Tc, anchor=None, method='manual', info=None, weight=1.0):
        """
        Принять позу moving: добавляется ручное ребро к anchor (размещённому скану)
        и позы пересчитываются по графу. weight > 1 — связь точнее прочих ручных
        (уточнена ICP): при несогласованности графа ошибка уходит в грубые связи.
        """
        anchor = anchor or self.ref
        TA = self.Tc(anchor)
        T_canon = np.linalg.inv(TA) @ np.asarray(Tc)          # канон. moving -> канон. anchor
        self.edges = [e for e in self.edges if not (e.get('method') == 'manual' and
                                                     {e['A'], e['B']} == {anchor.id, moving.id})]
        e = {'A': anchor.id, 'B': moving.id, 'T_canon': T_canon.tolist(), 'method': 'manual',
             'source': method, 'score': 1.0, 'violations': 0.0, 'margin': 1.0}
        if info:
            e['info'] = _jsonable(info)
        if weight != 1.0:
            e['weight'] = float(weight)
        self.edges.append(e)
        self.recompute_poses()
        return e

    # ── кандидаты ────────────────────────────────────────────────────────
    def candidates(self, moving, progress=None, top=8):
        """
        Гипотезы позы для moving относительно уже размещённых сканов:
          1) автоматическая стыковка с каждым размещённым сканом (по плоскостям);
          2) по «уличным» точкам - что размещённые сканы видели сквозь проёмы
             (для фасадов).
        Все гипотезы оцениваются одинаково (MultiScanScorer) и сортируются.
        """
        placed = [s for s in self.placed() if s is not moving]
        if not placed:
            return []
        out = []
        for i, s in enumerate(placed):
            if progress:
                progress(i / (len(placed) + 1), f"плоскости: {moving.id} -> {s.id}")
            r = pr.register_pair(_clean_res(s), _clean_res(moving), 'manhattan', top=3,
                                 verbose=False)
            out.append({'Tc': self.Tc(s) @ np.asarray(r['T_canon']), 'method': f'плоскости <-> {s.id}',
                        'pair_score': r['score']})
        if progress:
            progress(len(placed) / (len(placed) + 1), "уличные точки сквозь проёмы")
        out += self._outdoor_candidates(moving, placed)
        sc = pr.MultiScanScorer([(s.down, self.Tc(s)) for s in placed], moving.down)
        for c in out:
            c['score'], c['n_close'], c['violations'] = sc.score(c['Tc'])
        out.sort(key=lambda c: -c['score'])
        uniq = []
        for c in out:
            if all(pr.pose_delta(c['Tc'], u['Tc'])[0] > 0.3 or
                   pr.pose_delta(c['Tc'], u['Tc'])[1] > 3 for u in uniq):
                uniq.append(c)
        if progress:
            progress(1.0, "готово")
        return uniq[:top]

    def _outdoor_points(self, placed):
        pts = []
        for s in placed:
            down = s.res['down']
            ghost = s.ghost_mask()
            mirrors = [r for r in s.reflect_report if r.get('mirror') and r['kind'] == 'window']
            for o in s.openings:
                if any(np.allclose(o.center, r['center'], atol=0.05) for r in mirrors):
                    continue
                n = np.asarray(o.normal)
                c = float(n @ np.asarray(o.corners)[0])
                m = reflections._through_aperture(down, n, c, o.corners) & ~ghost
                # сквозь проём, но далеко от остальных размещённых сканов - «улица»
                if m.sum() > 200:
                    pts.append(pr.transform(down[m], self.Tc(s)))
        return np.vstack(pts) if pts else np.zeros((0, 3))

    def _outdoor_candidates(self, moving, placed, n_keep=4):
        import open3d as o3d
        A = self._outdoor_points(placed)
        if len(A) < 500:
            return []
        _, PA = planes.extract_planes(A, min_area=0.5)
        resB = _clean_res(moving)
        pcB = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(resB['down']))
        def close(T):
            P = pr.transform(A, np.linalg.inv(T))
            d = np.asarray(o3d.geometry.PointCloud(
                o3d.utility.Vector3dVector(P)).compute_point_cloud_distance(pcB))
            return float((d < 0.10).mean())
        floorA = self.ref.res['layers'].get('floor_z') or 0.0
        floorB = resB['layers'].get('floor_z') or 0.0
        empty = {'floor_z': None, 'ceiling_z': None}
        hyps = []
        dirA = pr.dominant_direction(PA)[0] or 0.0
        for yaw in pr.yaw_candidates(PA, resB['planes'], 'full', top=8):
            R = pr.rot_z(yaw)
            cands, _ = pr.translation_candidates(PA, resB['planes'], empty, empty, R, dirA)
            for t in cands:
                for plinth in np.arange(-0.6, 1.21, 0.3):
                    t2 = t.copy()
                    t2[2] = floorA - plinth - floorB
                    T = pr.make_T(R, t2)
                    hyps.append((close(T), T))
        hyps.sort(key=lambda h: -h[0])
        out = []
        for s0, T in hyps:
            if all(pr.pose_delta(T, c['Tc'])[0] > 0.5 for c in out):
                out.append({'Tc': T, 'method': 'уличные точки', 'outdoor_close': s0})
            if len(out) >= n_keep:
                break
        return out

    # ── ручная чистка ────────────────────────────────────────────────────
    def erase_region(self, scans_T, planes_common):
        """
        Удалить из сканов всё, что внутри областей (плоскости в общей системе).
        planes_common: одна область (4×4) или список областей — одно действие для отмены.
        scans_T: [(scan, T)] — T = текущая поза показа скана (канон. -> общая).
        """
        regions = [planes_common] if np.ndim(planes_common) == 2 else list(planes_common)
        rec = []
        for sc, T in scans_T:
            for reg in regions:
                sc.erase.append(manual_clean.planes_to_local(reg, T))
            rec.append((sc.id, len(regions)))
        if rec:
            self.erase_undo.append(rec)
        return rec

    def drop_points(self, items):
        """
        Удалить точки из сканов: items — [(скан, точки в его канон. системе)]; удаляются
        воксели DROP_VOXEL вокруг точек (движущиеся объекты). Одно действие для отмены.
        → число новых вокселей.
        """
        rec = []
        for scan, P_canon in items:
            keys = np.setdiff1d(np.unique(voxel_keys(np.asarray(P_canon, float))), scan.drop)
            if len(keys):
                scan.drop = np.union1d(scan.drop, keys)
                rec.append(('drop', scan.id, keys))
        if rec:
            self.erase_undo.append(rec)
        return sum(len(r[2]) for r in rec)

    def undo_erase(self):
        """Отменить последнее удаление. → [(id скана, …)] или None."""
        if not self.erase_undo:
            return None
        rec = self.erase_undo.pop()
        out = []
        for item in rec:
            if item[0] == 'drop':
                sc = self.by_id(item[1])
                if sc is not None:
                    sc.drop = np.setdiff1d(sc.drop, item[2])
                out.append((item[1], len(item[2])))
            else:
                sid, k = item
                sc = self.by_id(sid)
                if sc is not None:
                    del sc.erase[len(sc.erase) - k:]
                out.append((sid, k))
        return out

    def clear_erase(self, scan):
        """Сбросить ручную чистку скана: области и удалённые воксели."""
        scan.erase = []
        scan.drop = np.zeros(0, np.int64)
        sid_of = lambda x: x[1] if x[0] == 'drop' else x[0]
        self.erase_undo = [[x for x in r if sid_of(x) != scan.id] for r in self.erase_undo]
        self.erase_undo = [r for r in self.erase_undo if r]

    # ── экспорт ──────────────────────────────────────────────────────────
    def export(self, path, voxel=0.02, progress=None, frame='ref', scan_ids=None):
        """
        frame='ref'    - исходная система опорного скана (как в файле опорного скана);
        frame='common' - общая система: Z вверх, с поправкой горизонта проекта.
        """
        import open3d as o3d
        parts = []
        placed = [s for s in self.placed() if scan_ids is None or s.id in scan_ids]
        if not placed:
            raise ValueError('нет размещённых сканов для экспорта')
        for i, s in enumerate(placed):
            if progress:
                progress(i / len(placed), f"склейка {s.id}")
            if s.clean:
                pts, _ = reflections.clean_scan(s.path, s.up)
            else:
                pts = planes.load_points(s.path)
            if s.erase or len(s.drop):                         # ручная чистка (канон. система)
                pts = pts[s.keep_mask(pts @ s.R_up.T)]
            pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
            if voxel > 0:
                pc = pc.voxel_down_sample(voxel)
            T = s.pose if frame == 'ref' else self.Tc(s) @ homog(s.R_up)
            parts.append(pr.transform(np.asarray(pc.points), T))
        pts = np.vstack(parts)
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
        if voxel > 0:
            pc = pc.voxel_down_sample(voxel)
        pts = np.asarray(pc.points)
        path = Path(path)
        if path.suffix.lower() == '.e57':
            import pye57
            e57 = pye57.E57(str(path), mode='w')
            e57.write_scan_raw({'cartesianX': pts[:, 0], 'cartesianY': pts[:, 1],
                                'cartesianZ': pts[:, 2]})
            e57.close()
        else:
            o3d.io.write_point_cloud(str(path), pc)
        if progress:
            progress(1.0, f"{path.name}: {len(pts):,} точек")
        return len(pts)


def _rot_rpy(droll_deg=0.0, dpitch_deg=0.0, dyaw_deg=0.0):
    """R = Rz(yaw) · Ry(pitch) · Rx(roll), градусы."""
    r, p = np.radians(droll_deg), np.radians(dpitch_deg)
    Rx = np.array([[1, 0, 0], [0, np.cos(r), -np.sin(r)], [0, np.sin(r), np.cos(r)]])
    Ry = np.array([[np.cos(p), 0, np.sin(p)], [0, 1, 0], [-np.sin(p), 0, np.cos(p)]])
    return pr.rot_z(np.radians(dyaw_deg)) @ Ry @ Rx


def voxel_keys(P, size=DROP_VOXEL):
    """Ключи вокселей (int64) точек P — для хранения удалённых областей скана."""
    q = np.floor(np.asarray(P, float) / size).astype(np.int64) + (1 << 20)
    return (q[:, 0] << 42) | (q[:, 1] << 21) | q[:, 2]


def _clean_res(scan):
    """res скана с даунсемплом без отражений (для стыковки)."""
    r = dict(scan.res)
    r['down'] = scan.down
    return r


def _jsonable(x):
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, (np.floating, np.integer)):
        return x.item()
    if isinstance(x, np.bool_):
        return bool(x)
    return x
