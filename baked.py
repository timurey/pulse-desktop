#!/usr/bin/env python3
"""
Запечённая группа сканов: группа дерева с зафиксированными внутренними позами, которая
для стыковки выглядит и работает как один скан (виртуально и обратимо: исходные файлы
и сканы проекта не меняются).

Каноническая система группы совпадает с канонической системой её главного скана (root,
самый крупный); members[id] = P — канон. система скана → канон. система группы.
BakedGroup повторяет то, что стыковка ожидает от Scan: id, pose (+ R_up → Session.Tc /
set_Tc), down, res (плоскости, слои), planes, openings, analyzed / analyze().

Поза: чтение — поза root; запись — root и всем сканам группы Tc(m) = Tc(группы) @ P[m].
Облако — down сканов (уже без отражений и удалённого) в системе группы, вокселизация
planes.VOXEL убирает двойную плотность в перекрытиях. Анализ ленивый; кешируется в .pulse
(project_store, id 'group:<gid>'), ключ — отпечаток сканов, поз и чистки.
"""

import hashlib
import threading

import numpy as np

import planes
import plane_register as pr
import openings as op_mod
import project_store

PREFIX = 'group:'
CACHE_TAG = '-bake1'


def unit_id(gid):
    return PREFIX + gid


def gid_of(uid):
    return uid[len(PREFIX):] if isinstance(uid, str) and uid.startswith(PREFIX) else None


def voxel_merge(clouds, voxel=planes.VOXEL):
    """Облака N×3 → одно, по точке на воксель (центроид)."""
    P = np.vstack([c for c in clouds if len(c)]) if clouds else np.zeros((0, 3))
    if not len(P):
        return P
    k = np.floor(P / voxel).astype(np.int64)
    _, inv, cnt = np.unique(k, axis=0, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    out = np.zeros((len(cnt), 3))
    np.add.at(out, inv, P)
    return out / cnt[:, None]


class BakedGroup:
    def __init__(self, session, gid, root, members, name=''):
        self.s = session
        self.gid = gid
        self.id = unit_id(gid)
        self.name = name
        self.root = root                                   # id главного скана
        self.members = {k: np.asarray(v, float) for k, v in members.items()}
        self._res = None
        self._openings = None
        self._down = None
        self._down_key = None
        self.cache_dirty = False
        self._lock = threading.RLock()
        self.visible = True
        self.clean = True

    # ── как у скана ──────────────────────────────────────────────────────
    def scans(self):
        return [self.s.by_id(i) for i in self.members if self.s.by_id(i) is not None]

    @property
    def R_up(self):
        return self.s.by_id(self.root).R_up

    @property
    def pose(self):
        return self.s.by_id(self.root).pose

    @pose.setter
    def pose(self, value):
        root = self.s.by_id(self.root)
        if value is None:
            for sc in self.scans():
                sc.pose = None
            return
        root.pose = np.asarray(value, float)
        T = self.s.Tc(root)
        for sid, P in self.members.items():
            sc = self.s.by_id(sid)
            if sc is not None and sid != self.root:
                self.s.set_Tc(sc, T @ P)

    @property
    def analyzed(self):
        return all(sc.analyzed for sc in self.scans())

    def _key(self):
        return tuple((sid, sc.clean, len(sc.erase), len(sc.drop)) for sid, sc in
                     ((i, self.s.by_id(i)) for i in sorted(self.members)) if sc is not None)

    @property
    def down(self):
        """Облако группы в её канонической системе (по сканам без отражений и удалённого)."""
        with self._lock:
            key = self._key()
            if self._down is None or self._down_key != key:
                self._down = voxel_merge([pr.transform(self.s.by_id(i).down, P)
                                          for i, P in self.members.items() if self.s.by_id(i) is not None])
                self._down_key = key
                if self._res is not None and self._res.get('_key') != key:
                    self._res, self._openings = None, None
            return self._down

    def fingerprint(self):
        h = hashlib.md5()
        for sid in sorted(self.members):
            sc = self.s.by_id(sid)
            if sc is None:
                continue
            try:
                q = project_store.quick_hash(sc.path)
            except OSError:
                q = sid
            h.update(f'{sid}|{q}|{sc.clean}|{len(sc.erase)}|{len(sc.drop)}|'.encode('utf-8'))
            h.update(np.round(self.members[sid], 4).tobytes())
        return h.hexdigest()

    def cache_key(self):
        return {'size': len(self.members), 'version': project_store.CACHE_VERSION + CACHE_TAG, 'up': '+z',
                'qhash': self.fingerprint()}

    @property
    def res(self):
        with self._lock:
            down = self.down
            if self._res is None and not self._load_cache():
                d, pl = planes.extract_planes(down)
                self._res = {'scan': self.root, 'up': '+z', 'R_up': np.eye(3).tolist(), 'voxel': planes.VOXEL,
                             'n_points': int(len(down)), 'layers': planes.horizontal_layers(down),
                             'planes': pl, 'down': d}
                self.cache_dirty = True
            self._res['_key'] = self._down_key
            return self._res

    @property
    def planes(self):
        return self.res['planes']

    @property
    def openings(self):
        with self._lock:
            if self._openings is None:
                self._openings = op_mod.find_openings(self.res)
                self.cache_dirty = True
            return self._openings

    def analyze(self):
        self.res
        self.openings
        return self

    # ── кеш (project_store) ──────────────────────────────────────────────
    def to_cache(self):
        r = self.analyze().res
        pl = r['planes']
        idx = [np.asarray(p.inliers, np.int64) for p in pl]
        off = np.cumsum([0] + [len(i) for i in idx])
        meta = {'key': self.cache_key(), 'up': '+z', 'R_up': np.eye(3).tolist(), 'voxel': r['voxel'],
                'n_points': r['n_points'], 'layers': _jsonable(r['layers']),
                'planes': [p.to_json() for p in pl],
                'openings': [_jsonable(o.to_json()) for o in self._openings]}
        arrays = {'down': np.asarray(r['down'], np.float32),
                  'inliers': np.concatenate(idx).astype(np.int32) if idx else np.zeros(0, np.int32),
                  'inl_off': off.astype(np.int64)}
        return meta, arrays

    def _load_cache(self):
        path = self.s.project_path
        try:
            c = project_store.read_cache(path, self.id, self.cache_key()) if path else None
        except OSError:
            c = None
        if c is None:
            return False
        meta, arrays = c
        idx, off = arrays['inliers'], arrays['inl_off']
        pls = [planes.Plane(**pj, inliers=np.asarray(idx[off[k]:off[k + 1]], np.int64))
               for k, pj in enumerate(meta['planes'])]
        self._res = {'scan': self.root, 'up': '+z', 'R_up': meta['R_up'], 'voxel': meta['voxel'],
                     'n_points': meta['n_points'], 'layers': meta['layers'], 'planes': pls,
                     'down': np.asarray(arrays['down'], np.float64)}
        self._openings = [op_mod.Opening(**o) for o in meta.get('openings', [])]
        self.cache_dirty = False
        return True


def _jsonable(x):
    from scan_session import _jsonable as j
    return j(x)
