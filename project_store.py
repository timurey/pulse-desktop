#!/usr/bin/env python3
"""
Файл проекта `.pulse` — zip-архив директории проекта:

    project.json                 — позы, рёбра, дерево, чистка, горизонт (как раньше)
    cache/<скан>/meta.json       — ключ кеша + плоскости, слои, проёмы, отчёт отражений
    cache/<скан>/arrays.npz      — даунсемпл, индексы инлайеров плоскостей, маска отражений

Сами сканы (E57 и т.п.) в архив не входят — на них ссылаются пути относительно
папки, где лежит архив. Кеш анализа сканов привязан к размеру и дате изменения
файла скана и к версии алгоритмов (CACHE_VERSION): если что-то изменилось, анализ
считается заново. Старые проекты `.json` по-прежнему открываются и сохраняются.
"""

import io
import os
import json
import zipfile
import tempfile
from pathlib import Path

import numpy as np

# увеличивать при изменении planes / openings / reflections, влияющем на результат
CACHE_VERSION = 'analysis-3'
PROJECT_MEMBER = 'project.json'


def is_archive(path):
    p = Path(path)
    return p.suffix.lower() == '.pulse' or (p.exists() and zipfile.is_zipfile(p))


def _safe(sid):
    return ''.join(c if c.isalnum() or c in '._-' else '_' for c in sid)


def quick_hash(scan_path, block=1 << 20):
    """Контрольная сумма первого и последнего мегабайта файла (быстро и для больших сканов)."""
    import hashlib
    h = hashlib.md5()
    size = os.path.getsize(scan_path)
    with open(scan_path, 'rb') as f:
        h.update(f.read(block))
        if size > 2 * block:
            f.seek(size - block)
            h.update(f.read(block))
    return h.hexdigest()


def scan_key(scan_path, up='auto'):
    """
    Ключ кеша: файл скана (размер, дата, контрольная сумма) + версия алгоритмов + вертикаль.
    Дата нужна для старых кешей без суммы; при совпадении суммы дата не важна
    (после распаковки переданного проекта из zip время файлов другое).
    """
    st = os.stat(scan_path)
    return {'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'version': CACHE_VERSION, 'up': up,
            'qhash': quick_hash(scan_path)}


def key_matches(saved, key):
    """Подходит ли кеш с ключом saved к файлу с ключом key."""
    if not saved:
        return False
    same = all(saved.get(k) == key.get(k) for k in ('size', 'version', 'up'))
    if not same:
        return False
    if saved.get('qhash') and key.get('qhash'):
        return saved['qhash'] == key['qhash']
    return saved.get('mtime_ns') == key.get('mtime_ns')


# ── чтение ───────────────────────────────────────────────────────────────
def read_project(path):
    """dict project.json из .pulse или .json."""
    if is_archive(path):
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read(PROJECT_MEMBER).decode('utf-8'))
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_cache(path, scan_id, key):
    """(meta, arrays) из архива, если ключ совпадает; иначе None."""
    if not path or not is_archive(path) or not Path(path).exists():
        return None
    base = f'cache/{_safe(scan_id)}/'
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if base + 'meta.json' not in names or base + 'arrays.npz' not in names:
                return None
            meta = json.loads(z.read(base + 'meta.json').decode('utf-8'))
            if not key_matches(meta.get('key'), key):
                return None
            arrays = dict(np.load(io.BytesIO(z.read(base + 'arrays.npz'))))
            return meta, arrays
    except (zipfile.BadZipFile, KeyError, ValueError, OSError):
        return None


# ── запись ───────────────────────────────────────────────────────────────
def _write_zip(path, project_bytes, caches, keep_from=None, drop_ids=()):
    """
    Атомарная запись архива: project.json, новые кеши (caches: {scan_id: (meta, arrays)})
    и сохранившиеся кеши из прежнего архива keep_from (кроме перезаписанных и drop_ids).
    """
    path = Path(path)
    replaced = {f'cache/{_safe(s)}/' for s in list(caches) + list(drop_ids)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix='.pulse.tmp', dir=str(path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, 'w') as out:
            out.writestr(PROJECT_MEMBER, project_bytes, compress_type=zipfile.ZIP_DEFLATED)
            if keep_from and Path(keep_from).exists() and is_archive(keep_from):
                with zipfile.ZipFile(keep_from) as old:
                    for info in old.infolist():
                        n = info.filename
                        if n == PROJECT_MEMBER or any(n.startswith(r) for r in replaced):
                            continue
                        out.writestr(info, old.read(n))
            for sid, (meta, arrays) in caches.items():
                base = f'cache/{_safe(sid)}/'
                out.writestr(base + 'meta.json', json.dumps(meta, ensure_ascii=False),
                             compress_type=zipfile.ZIP_DEFLATED)
                buf = io.BytesIO()
                np.savez_compressed(buf, **arrays)
                out.writestr(base + 'arrays.npz', buf.getvalue(), compress_type=zipfile.ZIP_STORED)
        mask = os.umask(0)                 # mkstemp создаёт 0600 — вернуть обычные права
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_project(path, proj, caches=None, keep_from=None, drop_ids=()):
    """Сохранить проект: .pulse — архив (с кешем), .json — как раньше (без кеша)."""
    data = json.dumps(proj, indent=1, ensure_ascii=False).encode('utf-8')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if Path(path).suffix.lower() == '.json':
        Path(path).write_bytes(data)
        return
    _write_zip(path, data, caches or {}, keep_from or path, drop_ids)


def update_cache(path, caches):
    """
    Дописать кеш анализа в существующий архив, не трогая project.json
    (несохранённые правки проекта при этом не записываются).
    """
    if not is_archive(path) or not Path(path).exists() or not caches:
        return False
    with zipfile.ZipFile(path) as z:
        project_bytes = z.read(PROJECT_MEMBER)
    _write_zip(path, project_bytes, caches, keep_from=path)
    return True


def cached_ids(path):
    if not path or not is_archive(path) or not Path(path).exists():
        return set()
    with zipfile.ZipFile(path) as z:
        return {n.split('/')[1] for n in z.namelist() if n.startswith('cache/') and n.endswith('meta.json')}
