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
import baked as bk
import display as dsp
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
MANUAL_GAIN = 10.0         # ручная связь весомее автоматических: информация по точкам × MANUAL_GAIN
CONFLICT_T = 0.10          # м — связь противоречит принятой вручную позе, если расходится больше
CONFLICT_DEG = 1.5         # (или на столько градусов); такие связи «Принять позу» отключает


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
        self.display = None          # настройки отображения (display.py)
        self.calc_filter = None      # обрезка для расчётов (display.calc_filter, ставит сеанс)
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
        if self.erase or len(self.drop) or self.calc_filter:
            P = P[self.keep_mask(P)]
        return P

    @property
    def down(self):
        """Даунсемпл в канонической системе с учётом чистки отражений и ручной чистки."""
        key = (self.clean, len(self.erase), len(self.drop), dsp.signature(self.calc_filter))
        if self._down_cache is None or self._down_cache[0] != key:
            d = self.res['down']
            drop = self.ghost_mask() if self.clean else np.zeros(len(d), bool)
            if self.erase:
                drop = drop | manual_clean.inside_regions(d, self.erase)
            if len(self.drop):
                drop = drop | np.isin(voxel_keys(d), self.drop)
            if self.calc_filter:
                drop = drop | ~dsp.mask(d, self.calc_filter)
            self._down_cache = (key, d[~drop])
        return self._down_cache[1]

    def keep_mask(self, P_canon, clean=None):
        """Маска сохраняемых точек произвольного облака в канон. системе (ручная чистка)."""
        keep = np.ones(len(P_canon), bool)
        if self.erase:
            keep &= ~manual_clean.inside_regions(P_canon, self.erase)
        if len(self.drop):
            keep &= ~np.isin(voxel_keys(P_canon), self.drop)
        if self.calc_filter:
            keep &= dsp.mask(P_canon, self.calc_filter)
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
                'source': self.source, **({'display': self.display} if self.display else {})}


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
        self._removed_ids = set()    # удалённые из проекта сканы — их кеш не переносится при сохранении
        self.zero = None             # нулевой уровень {'z', 'source'} (общая система); None — пол опорного
        self.measures = []           # замеры (measure.py), точки в общей системе
        self.section = None          # сечение (section.py), общая система
        self._units = {}             # запечённые группы: gid → BakedGroup (по полю baked в дереве)
        self.last_conflicts = []     # связи, отключённые последним «Принять позу» (reject_conflicts)
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
            sc.display = e.get('display')
            s.scans.append(sc)
        s.frame = proj.get('frame') or s.scans[0].id
        s.level = np.asarray(proj.get('level', np.eye(3).tolist()), float)
        s.edges = proj.get('pairs', []) + proj.get('manual_edges', [])
        s.tree = Tree.from_json(proj.get('tree'), [x.id for x in s.scans])
        s.zero = proj.get('zero')
        s.measures = proj.get('measures', [])
        s.section = proj.get('section')
        s.apply_visibility()
        s.sync_filters()
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
        caches.update(self._unit_caches())
        if caches and project_store.update_cache(self.project_path, caches):
            for sc in list(self.scans) + list(self._units.values()):
                if sc.id in caches:
                    sc.cache_dirty = False
        return len(caches)

    def _project_dict(self, path, scan_paths=None):
        """Содержимое project.json; пути сканов — относительно файла проекта path."""
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
            src = (scan_paths or {}).get(e['id'], e['path'])
            e['path'] = sp.rel_path(src, path)
        return proj

    def import_project(self, path, bake=False, progress=None):
        """
        Импорт другого проекта (.pulse / .json) в этот: его дерево — новой группой (имя —
        имя проекта) с подгруппами; сканы с чисткой, настройками отображения и кешем анализа;
        связи между его сканами (автоматические и ручные); запечённые группы. Позы внутри
        импортированной части — по её связям; относительно этого проекта она не размещена,
        если нет общих сканов. Скан с тем же файлом, что уже есть, не дублируется (его связи
        с импортированными сохраняются); совпадающее имя другого файла получает суффикс.
        Опорный скан, горизонт, нулевой уровень, замеры и сечение импортируемого проекта не
        переносятся. bake — сразу запечь импортированную группу.
        → {'group', 'added', 'shared', 'renamed', 'missing', 'edges', 'cached', 'baked', 'bake_error'}
        """
        path = Path(path)
        if not path.exists():
            raise ValueError(f'файл проекта не найден: {path}')
        if self.project_path and path.resolve() == Path(self.project_path).resolve():
            raise ValueError('это тот же проект')
        proj = sp.load_project(path)
        say = progress or (lambda f, m: None)
        by_path = {str(Path(sc.path).resolve()): sc.id for sc in self.scans}
        used = {sc.id for sc in self.scans}
        idmap, added, shared, renamed, missing = {}, [], [], {}, []
        for k, e in enumerate(proj['scans']):
            say(0.6 * k / max(1, len(proj['scans'])), f"импорт проекта: {e['id']}")
            src = str(Path(e['path']).resolve())
            if src in by_path:                             # тот же файл уже в проекте
                idmap[e['id']] = by_path[src]
                shared.append(by_path[src])
                continue
            if not Path(src).exists():
                missing.append(e['id'])
                continue
            sid = e['id']
            if sid in used:
                stem, suf = Path(sid).stem, Path(sid).suffix
                n = 2
                while f'{stem}_{n}{suf}' in used:
                    n += 1
                sid = f'{stem}_{n}{suf}'
                renamed[e['id']] = sid
            used.add(sid)
            sc = Scan(src, e.get('up', 'auto'), None, sid)
            sc.clean = e.get('clean', True)
            sc.erase = [np.asarray(r, float) for r in e.get('erase', [])]
            sc.drop = np.asarray(e.get('drop', []), np.int64)
            sc.source = e.get('source')
            sc.display = e.get('display')
            idmap[e['id']] = sid
            added.append(sc)
        # кеш анализа из импортируемого архива (ключ — по самому файлу скана)
        cached = 0
        for k, sc in enumerate(added):
            say(0.6 + 0.3 * k / max(1, len(added)), f'кеш анализа: {sc.id}')
            old = next((o for o, n in idmap.items() if n == sc.id), sc.id)
            try:
                c = project_store.read_cache(str(path), old, sc.cache_key())
            except OSError:
                c = None
            if c is not None and sc.load_cache(*c):
                sc.cache_dirty = True                      # записать в кеш этого проекта
                cached += 1
        if not added and not shared:
            raise ValueError('в проекте нет сканов, которые можно импортировать (файлы не найдены)')
        self.scans += added
        if self.frame is None and added:
            self.frame = added[0].id
            added[0].pose = np.eye(4)
        # дерево: новая группа с подгруппами импортируемого проекта
        other = Tree.from_json(proj.get('tree'), [e['id'] for e in proj['scans']])
        gmap = {}
        top = self.tree.add_group('root', path.stem, 'прочее')
        gmap[other.root.id] = top
        self.tree.sync([sc.id for sc in self.scans])       # новые сканы — в корень, затем переносим

        def copy(src_g, dst_gid):
            for ch in src_g.children:
                if hasattr(ch, 'children'):
                    gid = self.tree.add_group(dst_gid, ch.name, ch.kind)
                    g = self.tree.group(gid)
                    g.visible, g.display = ch.visible, ch.display
                    gmap[ch.id] = gid
                    copy(ch, gid)
                elif ch.scan in idmap and idmap[ch.scan] in {x.id for x in added}:
                    self.tree.move(idmap[ch.scan], dst_gid)
                    lf = self.tree.leaf(idmap[ch.scan])
                    if lf is not None:
                        lf.visible = ch.visible
        copy(other.root, top)
        # запечённые группы импортируемого проекта (позы — по новым id)
        for og in other.groups():
            if og.baked and og.id in gmap:
                poses = {idmap[k]: v for k, v in og.baked['poses'].items() if k in idmap}
                root = idmap.get(og.baked['root'])
                if root is not None and len(poses) == len(og.baked['poses']):
                    self.tree.group(gmap[og.id]).baked = {'root': root, 'poses': poses}
        # связи: id сканов и запечённых групп — новые; между двумя уже бывшими сканами — не дублируем
        def rid(x):
            g = bk.gid_of(x)
            if g is not None:
                return bk.unit_id(gmap[g]) if g in gmap else None
            return idmap.get(x)
        new_ids = {sc.id for sc in added}
        n_edges = 0
        for e in proj.get('pairs', []) + proj.get('manual_edges', []):
            a, b = rid(e['A']), rid(e['B'])
            if a is None or b is None or a == b:
                continue
            if not ({a, b} & (new_ids | {bk.unit_id(g) for g in gmap.values()})):
                continue
            e2 = dict(e, A=a, B=b)
            if 'groups' in e2:
                e2['groups'] = [gmap.get(g, g) for g in e2['groups']]
            self.edges.append(e2)
            n_edges += 1
        self._recolor()
        self.apply_visibility()
        self.sync_filters()
        say(0.95, 'позы по графу')
        self.recompute_poses()
        baked_ok, err = False, None
        if bake:
            todo = [sc for sc in added if not sc.analyzed]
            for k, sc in enumerate(todo):                  # запекание — по проанализированным сканам
                say(0.95, f'анализ {sc.id} ({k + 1}/{len(todo)})')
                sc.analyze()
            try:
                self.bake(top)
                baked_ok = True
            except ValueError as ex:
                err = str(ex)
        return {'group': top, 'added': [sc.id for sc in added], 'shared': shared, 'renamed': renamed,
                'missing': missing, 'edges': n_edges, 'cached': cached, 'baked': baked_ok, 'bake_error': err}

    def pack(self, dest, as_zip=False, progress=None):
        """
        Проект для передачи другому человеку: папка (или один zip) с проектом .pulse,
        всеми сканами в scans/ (с их .json и .dyn.npy), полным кешем анализа и ПРОЧТИ.txt.
        Пути в проекте — относительные, поэтому пакет открывается на любой машине и ОС.
        Открытый сеанс не меняется. → dict(path, scans, missing, size).
        """
        import shutil
        import tempfile
        import zipfile
        dest = Path(dest)
        name = dest.stem if as_zip else dest.name
        tmp = Path(tempfile.mkdtemp(prefix='pulse_pack_')) if as_zip else None
        root = (tmp / name) if as_zip else dest
        if not as_zip and root.exists() and any(root.iterdir()):
            raise FileExistsError(f'папка не пуста: {root}')
        sdir = root / 'scans'
        sdir.mkdir(parents=True, exist_ok=True)
        say = progress or (lambda f, m: None)
        mapping, missing, used = {}, [], set()
        try:
            for k, sc in enumerate(self.scans):
                src = Path(sc.path)
                say(0.8 * k / max(1, len(self.scans)), f'копирование {src.name}')
                if not src.exists():
                    missing.append(sc.id)
                    continue
                stem, n = src.stem, 2
                while stem.lower() in used:            # одинаковые имена из разных папок
                    stem, n = f'{src.stem}_{n}', n + 1
                used.add(stem.lower())
                dst = sdir / (stem + src.suffix)
                shutil.copy2(src, dst)
                for side, new in ((src.with_suffix('.json'), dst.with_suffix('.json')),
                                  (src.with_name(src.stem + '.dyn.npy'), dst.with_name(stem + '.dyn.npy'))):
                    if side.exists():
                        shutil.copy2(side, new)
                mapping[sc.id] = str(dst)
            say(0.85, 'проект и кеш анализа')
            pfile = root / f'{name}.pulse'
            proj = self._project_dict(pfile, mapping)
            proj['scans'] = [e for e in proj['scans'] if e['id'] in mapping]
            caches = {sc.id: sc.to_cache() for sc in self.scans if sc.analyzed and sc.id in mapping}
            keep = self.project_path if (self.project_path and project_store.is_archive(self.project_path)
                                         and Path(self.project_path).exists()) else None
            project_store.write_project(pfile, proj, caches, keep_from=keep,
                                        drop_ids=self._removed_ids | {sc.id for sc in self.scans if sc.id not in mapping})
            (root / 'ПРОЧТИ.txt').write_text(
                f'Проект Pulse Scan «{name}»\n\n'
                f'Откройте {name}.pulse в Pulse Scan (scan_qt.py / run_qt.*): «Проект» → «Открыть».\n'
                f'Сканы лежат в папке scans/ рядом с проектом — не переносите их отдельно от файла проекта.\n'
                f'Внутри проекта сохранены позы и связи сканов, дерево, ручная чистка, нулевой уровень,\n'
                f'сечение, замеры и кеш анализа (проект открывается без пересчёта).\n\n'
                f'Сканов: {len(mapping)}. Упаковано {time.strftime("%Y-%m-%d %H:%M")}.\n', encoding='utf-8')
            files = [f for f in root.rglob('*') if f.is_file()]
            size = sum(f.stat().st_size for f in files)
            out = pfile
            if as_zip:
                dest.parent.mkdir(parents=True, exist_ok=True)
                part = dest.with_name(dest.name + '.part')
                with zipfile.ZipFile(part, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                    for k, f in enumerate(files):
                        say(0.88 + 0.12 * k / max(1, len(files)), f'архив: {f.name}')
                        z.write(f, f'{name}/{f.relative_to(root).as_posix()}')
                part.replace(dest)
                out, size = dest, dest.stat().st_size
            say(1.0, 'готово')
            return {'path': str(out), 'project': pfile.name, 'scans': len(mapping), 'missing': missing,
                    'size': size}
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    def save(self, path=None):
        path = path or self.project_path
        proj = self._project_dict(path)
        caches = {}
        if project_store.is_archive(path) or str(path).lower().endswith('.pulse'):
            same = self.project_path and Path(self.project_path).resolve() == Path(path).resolve()
            caches = {sc.id: sc.to_cache() for sc in self.scans
                      if sc.analyzed and (sc.cache_dirty or not same)}
            caches.update(self._unit_caches(all_=not same))
        keep = self.project_path if (self.project_path and project_store.is_archive(self.project_path)) else None
        project_store.write_project(path, proj, caches, keep_from=keep, drop_ids=self._removed_ids)
        for sc in list(self.scans) + list(self._units.values()):
            if sc.id in caches:
                sc.cache_dirty = False
        self.project_path = str(path)
        return path

    def _unit_caches(self, all_=False):
        """Кеш анализа запечённых групп (посчитанный, новый или все при сохранении в другой файл)."""
        out = {}
        for u in self._units.values():
            if u._res is not None and u._openings is not None and (u.cache_dirty or all_):
                out[u.id] = u.to_cache()
        return out

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
        moving_out = [k for k in keys if self.tree.parent(k) is None or self.tree.parent(k).id != target_gid]
        self._check_baked(moving_out, target_gid if moving_out else None)
        ok = self.tree.move_many(keys, target_gid, index)
        self.apply_visibility()
        return ok

    def group_nodes(self, keys, name, kind='прочее'):
        """Создать группу из выбранных узлов. → id группы или None."""
        self._check_baked(keys)
        gid = self.tree.group_from(keys, name, kind)
        self.apply_visibility()
        return gid

    def delete_group(self, gid):
        for g in [x for x in self.tree.groups(self.tree.group(gid)) if x.baked] if self.tree.group(gid) else []:
            self.unbake(g.id)
        ok = self.tree.delete_group(gid)
        self.apply_visibility()
        return ok

    def remove_scans(self, ids):
        """
        Удалить сканы из проекта (файлы на диске не трогаются): связи, место в дереве,
        сетки и кеш анализа (при следующем сохранении). Если удалён опорный — опорным
        становится размещённый скан с наибольшим числом активных связей; позы — по графу.
        → список удалённых id.
        """
        ids = {i for i in ids if self.by_id(i) is not None and bk.gid_of(i) is None}
        if not ids:
            return []
        for i in ids:                                      # скан из запечённой группы — группа распекается
            g = self.tree.baked_owner(i)
            while g is not None:
                self.unbake(g.id)
                g = self.tree.baked_owner(i)
        self.scans = [sc for sc in self.scans if sc.id not in ids]
        self.edges = [e for e in self.edges if e['A'] not in ids and e['B'] not in ids]
        self.tree.sync([sc.id for sc in self.scans])
        for k in [k for k in self.meshes if k in ids]:
            self.meshes.pop(k)
        self.erase_undo = [[x for x in r if (x[1] if x[0] == 'drop' else x[0]) not in ids] for r in self.erase_undo]
        self.erase_undo = [r for r in self.erase_undo if r]
        self._removed_ids |= ids
        if self.frame in ids:
            placed = [sc for sc in self.scans if sc.pose is not None]
            act = self.active_edges()
            deg = lambda sc: sum(1 for e in act if sc.id in (e['A'], e['B']))
            new = max(placed, key=deg) if placed else (self.scans[0] if self.scans else None)
            self.frame = new.id if new is not None else None
            if new is not None:
                new.pose = np.eye(4)
        if self.scans:
            self.recompute_poses()
        self._recolor()
        self.apply_visibility()
        return sorted(ids)

    def show_all(self):
        self.tree.show_all()
        self.apply_visibility()

    def by_id(self, sid):
        gid = bk.gid_of(sid)
        if gid is not None:
            return self.baked_unit(gid)
        return next((s for s in self.scans if s.id == sid), None)

    # ── отображение (display.py) ─────────────────────────────────────────
    def own_display(self, key):
        """Свои настройки отображения скана или группы (без наследования)."""
        g = self.tree.group(key)
        if g is not None:
            return g.display or {}
        sc = self.by_id(key)
        return (sc.display or {}) if sc is not None and not isinstance(sc, bk.BakedGroup) else {}

    def effective_display(self, scan_or_key):
        """Настройки скана поверх настроек его групп (от корня к скану)."""
        key = scan_or_key if isinstance(scan_or_key, str) else scan_or_key.id
        chain = [g.display for g in reversed(self.tree.ancestors(key))]
        g = self.tree.group(key)
        if g is not None:
            chain.append(g.display)
        else:
            chain.append(self.own_display(key))
        return dsp.merge(*chain)

    def set_display(self, key, d):
        """Задать свои настройки скана или группы (None — сбросить). → id затронутых сканов."""
        d = dsp.clean(d)
        g = self.tree.group(key)
        if g is not None:
            g.display = d
            ids = self.tree.scans_in(key)
        else:
            sc = self.by_id(key)
            if sc is None:
                return []
            sc.display = d
            ids = [key]
        self.sync_filters()
        return ids

    def sync_filters(self):
        """Обрезка для расчётов у каждого скана — по действующим настройкам."""
        for sc in self.scans:
            sc.calc_filter = dsp.calc_filter(self.effective_display(sc.id))

    # ── запечённые группы (baked.py) ─────────────────────────────────────
    def baked_unit(self, gid):
        """BakedGroup запечённой группы gid (или None)."""
        g = self.tree.group(gid)
        if g is None or not g.baked:
            self._units.pop(gid, None)
            return None
        u = self._units.get(gid)
        if u is None or u._spec is not g.baked:
            u = bk.BakedGroup(self, gid, g.baked['root'], g.baked['poses'], g.name)
            u._spec = g.baked
            self._units[gid] = u
        u.name = g.name
        return u

    def units(self):
        """Узлы графа стыковки: сканы вне запечённых групп и внешние запечённые группы (по порядку сканов)."""
        out, seen = [], set()
        for sc in self.scans:
            g = self.tree.baked_owner(sc.id)
            if g is None:
                out.append(sc)
            elif g.id not in seen:
                seen.add(g.id)
                u = self.baked_unit(g.id)
                if u is not None:
                    out.append(u)
        return out

    def unit_of(self, sid):
        """Узел графа, в который входит скан (запечённая группа или сам скан)."""
        if bk.gid_of(sid) is not None:
            return self.by_id(sid)
        g = self.tree.baked_owner(sid)
        return self.baked_unit(g.id) if g is not None else self.by_id(sid)

    @staticmethod
    def members(obj):
        """{id скана: P} — сканы объекта (скан или запечённая группа) и их позы в его системе."""
        return dict(obj.members) if isinstance(obj, bk.BakedGroup) else {obj.id: np.eye(4)}

    def _unit_edges(self, edges):
        """Рёбра на уровне узлов: внутренние для запечённой группы — пропуск, остальные — пересчёт."""
        out = []
        for e in edges:
            ua, ub = self.unit_of(e['A']), self.unit_of(e['B'])
            if ua is None or ub is None or ua is ub:
                continue
            if ua.id == e['A'] and ub.id == e['B']:
                out.append(e)
                continue
            PA = self.members(ua).get(e['A'], np.eye(4))
            PB = self.members(ub).get(e['B'], np.eye(4))
            out.append(dict(e, A=ua.id, B=ub.id, T_canon=(PA @ np.asarray(e['T_canon']) @ np.linalg.inv(PB)).tolist()))
        return out

    def _check_baked(self, keys, target_gid=None):
        """Перенос узлов в запечённую группу и из неё запрещён (ValueError с понятным текстом)."""
        for k in keys:
            g = self.tree.baked_owner(k)
            if g is not None and g.id != k:
                raise ValueError(f'«{short_name(k)}» в запечённой группе «{g.name}»: сначала распеките её')
        if target_gid is not None:
            g = self.tree.baked_owner(target_gid)
            if g is not None:
                raise ValueError(f'группа «{g.name}» запечена: сначала распеките её')

    def bake(self, gid):
        """
        Запечь группу: внутренние позы — по графу её внутренних активных связей (запечённые
        подгруппы — одно целое), главный скан — самый крупный. Все сканы ветки должны быть
        связаны между собой. → BakedGroup.
        """
        g = self.tree.group(gid)
        if g is None or g is self.tree.root:
            raise ValueError('выберите группу')
        if g.baked:
            return self.baked_unit(gid)
        ids = [i for i in self.tree.scans_in(gid) if self.by_id(i) is not None]
        if len(ids) < 2:
            raise ValueError('в группе меньше двух сканов')
        not_ready = [i for i in ids if not self.by_id(i).analyzed]
        if not_ready:
            raise ValueError(f'дождитесь анализа сканов: {", ".join(short_name(i) for i in not_ready[:4])}')
        inner = {}                                         # узлы внутри: запечённые подгруппы и сканы
        for i in ids:
            o = self.tree.baked_owner(i)
            u = self.baked_unit(o.id) if o is not None and o is not g else self.by_id(i)
            inner[u.id] = u
        names = list(inner)
        inside = set(ids) | set(names)
        edges = self._unit_edges([e for e in self.active_edges() if e['A'] in inside and e['B'] in inside])
        edges = [e for e in edges if e['A'] in inner and e['B'] in inner]
        root_u = max(inner.values(), key=lambda u: len(u.down))
        poses = sp.spanning_poses(names, edges, root_u.id)
        missing = [n for n in names if n not in poses]
        if missing:
            raise ValueError('не связаны с остальными сканами группы: ' +
                             ', '.join(short_name(n) for n in missing[:6]) +
                             (' …' if len(missing) > 6 else '') + ' — состыкуйте их внутри группы')
        if len(edges) > len(poses) - 1 and len(poses) > 1:
            infos = [sp.information(e, _clean_res(inner[e['A']]), _clean_res(inner[e['B']])) *
                     float(e.get('weight', 1.0)) + (MANUAL_INFO * np.eye(6) if e.get('method') == 'manual' else 0)
                     for e in edges]
            poses = sp.optimize(names, edges, poses, infos, root_u.id)
        members = {}
        for n, T in poses.items():
            for sid, P in self.members(inner[n]).items():
                members[sid] = np.asarray(T) @ P
        root = root_u.root if isinstance(root_u, bk.BakedGroup) else root_u.id
        Pr = members[root]                                 # система группы = система её главного скана
        members = {sid: np.linalg.inv(Pr) @ P for sid, P in members.items()}
        g.baked = {'root': root, 'poses': {sid: np.round(P, 9).tolist() for sid, P in members.items()}}
        self.recompute_poses()
        return self.baked_unit(gid)

    def unbake(self, gid):
        """Распечь группу: позы сканов остаются, связи группы переходят на её главный скан."""
        g = self.tree.group(gid)
        if g is None or not g.baked:
            return False
        uid, root = bk.unit_id(gid), g.baked['root']
        for e in self.edges:
            for k in ('A', 'B'):
                if e[k] == uid:
                    e[k] = root
        g.baked = None
        self._units.pop(gid, None)
        self.recompute_poses()
        return True

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
                      None if sc.pose is None else np.round(sc.pose, 6).tobytes(),
                      json.dumps(sc.display, sort_keys=True) if sc.display else None]
        for e in self.edges:
            parts += [e['A'], e['B'], e.get('user'), e.get('method')]
        parts += [None if self.zero is None else round(self.zero['z'], 6), len(self.measures)]
        parts.append(json.dumps(self.tree.to_json(), sort_keys=True, ensure_ascii=False))
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
    def run_auto(self, progress=None, yaw='manhattan', reuse=True, by_tree=None, combos=None, cancel=None):
        """
        Автоматическая стыковка, затем позы по графу.
        by_tree=True — только пары по дереву (внутри групп + представители соседних
        веток), иначе все пары. По умолчанию — по дереву, если в нём есть группы.
        combos — явный список пар (например, только внутри групп — within_group_pairs).
        cancel() → True — остановить: посчитанные пары сохраняются, позы — по ним
        (повторный запуск с reuse продолжит с места).
        → {'pairs': всего пар, 'done': посчитано, 'cancelled': bool}
        """
        names = [u.id for u in self.units()]
        known = set(names) | {s.id for s in self.scans}
        old = {(e['A'], e['B']): e for e in self.edges if e.get('method') not in ('manual', 'group')}
        manual = [e for e in self.edges if e.get('method') in ('manual', 'group')]
        if by_tree is None:
            by_tree = not self.tree.is_flat()
        if combos is None:
            combos = self.auto_pairs('tree' if by_tree else 'all')
        order = {n: i for i, n in enumerate(names)}
        combos = self._unit_pairs(combos)
        combos = [tuple(sorted(c, key=order.get)) for c in combos if c[0] in order and c[1] in order]
        chosen = set(combos)
        edges = []
        n = len(combos)
        def cached(c):
            if not (reuse and c in old and 'T_canon' in old[c]):
                return False
            o = old[c]
            if sp.edge_ok(o) or o.get('hint_tried') or 'user' in o:
                return True
            ua, ub = self.by_id(c[0]), self.by_id(c[1])         # неудачная пара размещённых сканов —
            return not (ua is not None and ub is not None and    # один раз пересчитать от текущей позы
                        ua.pose is not None and ub.pose is not None)
        todo = sum(1 for c in combos if not cached(c))
        cancelled = False
        t0, k = time.time(), 0                        # k — посчитано заново (для оценки времени)
        for i, (a, b) in enumerate(combos):
            if cancel is not None and cancel():
                cancelled = True
                chosen = {(e['A'], e['B']) for e in edges}
                break
            if cached((a, b)):
                e = dict(old[(a, b)])
            else:
                if progress:
                    eta = f", осталось {fmt_eta((time.time() - t0) / k * (todo - k))}" if k else ''
                    progress(i / max(1, n), f"стыковка: пара {i + 1} из {n}{eta} ({a} <- {b})")
                k += 1
                sa, sb = self.by_id(a), self.by_id(b)
                hints = []
                if sa.pose is not None and sb.pose is not None:     # уже размещены — уточнить от текущей позы
                    hints.append(np.linalg.inv(self.Tc(sa)) @ self.Tc(sb))
                e = pr.register_pair(_clean_res(sa), _clean_res(sb), yaw, verbose=False, hints=hints)
                e['A'], e['B'] = a, b
                if hints:
                    e['hint_tried'] = True
                if not sp.edge_ok(e) and e['violations'] <= sp.EDGE_THIN_VIOL:
                    e['thick'], e['thick_cells'] = self.pair_thick(sa, sb, np.asarray(e['T_canon']))
                o = old.get((a, b))
                if o is not None and 'user' in o and 'T_canon' in o:
                    # решение пользователя относится к прежнему результату: переносим, только если
                    # пара пересчиталась в ту же позу, иначе её снова решает автоматика
                    dt, da = pr.pose_delta(np.asarray(e['T_canon']), np.asarray(o['T_canon']))
                    if dt <= CONFLICT_T and da <= CONFLICT_DEG:
                        e['user'] = o['user']
            e['auto_ok'] = sp.edge_ok(e)
            edges.append(e)
        # ранее посчитанные пары вне выбранных — тоже данные, не выбрасываем
        edges += [dict(e, auto_ok=sp.edge_ok(e)) for k, e in old.items()
                  if k not in chosen and 'T_canon' in e and k[0] in known and k[1] in known]
        for e in edges:
            e.pop('loop_rejected', None)
        with self.lock:
            self.edges = edges + manual
        # слабые пары, из-за которых не сходятся циклы, — отключить (по узлам графа)
        ue = {id(e): u for e in self.edges for u in self._unit_edges([e])}
        view = [dict(u, _src=e) for e in self.edges for u in [ue.get(id(e))] if u is not None]
        for v in sp.prune_loops(view, [u.id for u in self.units()], lambda v: self.edge_active(v)):
            v['_src']['auto_ok'] = False
            v['_src']['loop_rejected'] = True
        if progress:
            progress(1.0, "оптимизация графа поз")
        self.recompute_poses()
        if progress:
            progress(1.0, "готово")
        return {'pairs': n, 'done': len([e for e in edges if (e['A'], e['B']) in chosen]), 'cancelled': cancelled,
                'computed': k, 'seconds': time.time() - t0}

    def auto_pairs(self, method='tree', gid=None):
        """
        Пары для автостыковки: method 'all' — все пары, 'tree' — по дереву (как run_auto
        by_tree; без групп — все пары), 'within' — внутри групп (within_group_pairs).
        gid — только сканы этой ветки.
        """
        if method == 'within':
            return self._unit_pairs(self.within_group_pairs(gid))
        ids = [s.id for s in self.scans]
        if isinstance(gid, (list, tuple, set)):            # выделенные узлы дерева: все пары среди них
            inside = {i for k in gid for i in self.tree.scans_in(k)}
            return self._unit_pairs(list(itertools.combinations([i for i in ids if i in inside], 2)))
        if gid is not None and gid != 'root' and self.tree.group(gid) is not None:
            inside = set(self.tree.scans_in(gid))
            ids = [i for i in ids if i in inside]
        if method == 'tree' and not self.tree.is_flat():
            weight = {sc.id: sc.res.get('n_points', 0) if sc.analyzed else 0 for sc in self.scans}
            return self._unit_pairs(self.tree.registration_pairs(ids, weight))
        return self._unit_pairs(list(itertools.combinations(ids, 2)))

    def _unit_pairs(self, combos):
        """Пары сканов → пары узлов графа (запечённая группа — один узел), без повторов и пар внутри узла."""
        out, seen = [], set()
        for a, b in combos:
            ua, ub = self.unit_of(a), self.unit_of(b)
            if ua is None or ub is None or ua is ub:
                continue
            k = frozenset((ua.id, ub.id))
            if k not in seen:
                seen.add(k)
                out.append((ua.id, ub.id))
        return out

    def pending_pairs(self, combos, reuse=True):
        """Сколько пар из combos придётся считать (остальные уже посчитаны)."""
        if not reuse:
            return len(combos)
        done = {(e['A'], e['B']) for e in self.edges if 'T_canon' in e and e.get('method') not in ('manual', 'group')}
        return sum(1 for a, b in combos if (a, b) not in done and (b, a) not in done)

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
        g = self.tree.group(gid)
        if g is not None and g.baked:
            u = self.baked_unit(gid)
            return u.root, dict(u.members)
        ids = [i for i in self.tree.scans_in(gid) if self.by_id(i) is not None and self.by_id(i).analyzed]
        if not ids:
            return None, {}
        inside = [e for e in self.active_edges() if e['A'] in ids and e['B'] in ids and 'T_canon' in e]
        root = max(ids, key=lambda i: len(self.by_id(i).down))
        return root, sp.spanning_poses(ids, inside, root)

    def group_res(self, gid):
        """Облако группы в системе её главного скана + плоскости и слои (как у одного скана)."""
        u = self.baked_unit(gid)
        if u is not None:                                  # запечённая: анализ уже есть (и в кеше проекта)
            r = dict(u.res)
            r.update(down=u.down, members=sorted(u.members), group=gid)
            return r
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
        """
        Позы всех сканов, связанных с опорным активными рёбрами. Граф строится по узлам
        (units): запечённая группа — один узел, её внутренние связи не участвуют.
        """
        units = self.units()
        names = [u.id for u in units]
        edges = self._unit_edges(self.active_edges())
        fu = self.unit_of(self.frame)
        poses = sp.spanning_poses(names, edges, fu.id)
        if len(edges) > len(poses) - 1 and len(poses) > 1:
            infos = []
            for e in edges:
                A, B = self.by_id(e['A']), self.by_id(e['B'])
                info = sp.information(e, _clean_res(A), _clean_res(B))
                if e.get('method') == 'manual':
                    info = info * MANUAL_GAIN + MANUAL_INFO * np.eye(6)
                infos.append(info * float(e.get('weight', 1.0)))
            poses = sp.optimize(names, edges, poses, infos, fu.id)
        # граф — в канон. системе опорного узла; опорный скан остаётся в единичной позе
        G = homog(self.level) @ np.linalg.inv(self.members(fu).get(self.frame, np.eye(4)))
        with self.lock:
            for u in units:
                if u.id in poses:
                    self.set_Tc(u, G @ poses[u.id])
                elif u is not fu:
                    u.pose = None
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

    def pair_thick(self, a, b, T_canon):
        """
        Толщина общих плоскостей пары (узлы a, b; T_canon — канон. b → канон. a), не трогая
        позы сеанса: (медиана, м | None, число ячеек 20 см).
        """
        import quality
        mA, mB = self.members(a), self.members(b)
        poses = dict(mA)
        poses.update({i: np.asarray(T_canon) @ P for i, P in mB.items()})
        q = quality.plane_cells(self, scan_ids=set(poses), poses=poses)
        cells = [k for k in range(len(q)) if set(mA) & set(q.scans[k]) and set(mB) & set(q.scans[k])]
        if not cells:
            return None, 0
        return float(np.median(q.thick[cells])), len(cells)

    def pair_thickness(self, fixed_list, moving, T):
        """
        Насколько расходятся одни и те же поверхности подвижного (в позе T) и неподвижных:
        медиана толщины по плоскостям, м (None — нет общих поверхностей). → (медиана, ячеек).
        """
        import quality
        mem = self.members(moving)
        fixed_ids = {i for s in fixed_list for i in self.members(s)} - set(mem)
        q = quality.plane_cells(self, scan_ids=set(mem) | fixed_ids,
                                poses={i: np.asarray(T) @ P for i, P in mem.items()})
        pairs = [k for k in range(len(q)) if set(mem) & set(q.scans[k]) and fixed_ids & set(q.scans[k])]
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
        self.last_conflicts = self.reject_conflicts(moving, Tc)
        e = {'A': anchor.id, 'B': moving.id, 'T_canon': T_canon.tolist(), 'method': 'manual',
             'source': method, 'score': 1.0, 'violations': 0.0, 'margin': 1.0}
        if info:
            e['info'] = _jsonable(info)
        if weight != 1.0:
            e['weight'] = float(weight)
        self.edges.append(e)
        self.recompute_poses()
        return e

    def manual_conflict(self, idx):
        """Насколько связь idx расходится с ручной связью той же пары: (м, °) или None."""
        e = self.edges[idx]
        for m in self.edges:
            if m is e or m.get('method') != 'manual' or {m['A'], m['B']} != {e['A'], e['B']} or 'T_canon' not in e:
                continue
            T = np.asarray(e['T_canon'])
            Tm = np.asarray(m['T_canon']) if m['A'] == e['A'] else np.linalg.inv(np.asarray(m['T_canon']))
            dt, da = pr.pose_delta(T, Tm)
            if dt > CONFLICT_T or da > CONFLICT_DEG:
                return float(dt), float(da)
        return None

    def edge_residuals(self):
        """
        Активные связи, которые итоговые позы не выполняют (расхождение больше CONFLICT_T /
        CONFLICT_DEG): граф свёл противоречащие связи — обычно грубую ручную расстановку и
        уточнившую её автоматическую пару. → [(ребро, м, °)].
        """
        out = []
        for e in self.active_edges():
            ue = self._unit_edges([e])
            if not ue:
                continue
            a, b = self.by_id(ue[0]['A']), self.by_id(ue[0]['B'])
            if a is None or b is None or a.pose is None or b.pose is None:
                continue
            dt, da = pr.pose_delta(np.linalg.inv(self.Tc(a)) @ self.Tc(b), np.asarray(ue[0]['T_canon']))
            if dt > CONFLICT_T or da > CONFLICT_DEG:
                out.append((e, float(dt), float(da)))
        return out

    def reject_conflicts(self, moving, Tc):
        """
        Отключить (user='reject') активные связи узла moving, которые противоречат его позе
        Tc больше CONFLICT_T / CONFLICT_DEG при текущих позах соседей: вручную поставленная
        поза — решение пользователя, иначе граф сведёт её с ошибочными парами и скан уедет.
        → [(ребро, сдвиг м, угол °)].
        """
        Tc = np.asarray(Tc)
        out = []
        for e in self.active_edges():
            ue = self._unit_edges([e])
            if not ue or moving.id not in (ue[0]['A'], ue[0]['B']):
                continue
            u = ue[0]
            other = self.by_id(u['B'] if u['A'] == moving.id else u['A'])
            if other is None or other.pose is None:
                continue
            T = np.asarray(u['T_canon'])                   # канон. B -> канон. A
            implied = self.Tc(other) @ (T if u['B'] == moving.id else np.linalg.inv(T))
            dt, da = pr.pose_delta(implied, Tc)
            if dt > CONFLICT_T or da > CONFLICT_DEG:
                e['user'] = 'reject'
                e['rejected_by'] = 'ручная поза'
                out.append((e, float(dt), float(da)))
        return out

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
            if s.erase or len(s.drop) or s.calc_filter:        # ручная чистка, обрезка (канон. система)
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
        import fileio
        fileio.write_cloud(path, pts)
        if progress:
            progress(1.0, f"{path.name}: {len(pts):,} точек")
        return len(pts)


def _rot_rpy(droll_deg=0.0, dpitch_deg=0.0, dyaw_deg=0.0):
    """R = Rz(yaw) · Ry(pitch) · Rx(roll), градусы."""
    r, p = np.radians(droll_deg), np.radians(dpitch_deg)
    Rx = np.array([[1, 0, 0], [0, np.cos(r), -np.sin(r)], [0, np.sin(r), np.cos(r)]])
    Ry = np.array([[np.cos(p), 0, np.sin(p)], [0, 1, 0], [-np.sin(p), 0, np.cos(p)]])
    return pr.rot_z(np.radians(dyaw_deg)) @ Ry @ Rx


def short_name(sid):
    """Короткое имя скана для сообщений: без даты и расширения."""
    import re
    n = str(sid)
    for ext in ('.ply', '.e57', '.pcd', '.las'):
        if n.endswith(ext):
            n = n[:-len(ext)]
    m = re.search(r'(\d{6})(?:_\d+)?$', n)
    return n if m is None else n[m.start():]


def fmt_eta(sec):
    """Оставшееся время для строки состояния: «~40 с», «~12 мин», «~1 ч 20 мин»."""
    if sec < 60:
        return f'~{max(5, int(round(sec / 5)) * 5)} с'
    m = int(round(sec / 60))
    return f'~{m} мин' if m < 60 else f'~{m // 60} ч {m % 60:02d} мин'


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
