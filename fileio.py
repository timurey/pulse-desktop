#!/usr/bin/env python3
"""
Пути к файлам для библиотек, которые на Windows не открывают пути не в ASCII.

Open3D (PLY, PCD, сетки) и pye57 (E57) передают путь в C++ как узкую строку; на Windows
файл по пути с кириллицей («Рабочий стол», «Документы», имя пользователя) не открывается:
облако читается пустым, запись не создаёт файл. Python и numpy такие пути понимают.

native_path(path, mode) — путь, который можно отдать такой библиотеке:
  - не Windows или путь в ASCII — как есть;
  - чтение — короткое имя 8.3 (если оно в ASCII), иначе временная копия в папке с ASCII-путём;
  - запись — временный файл в такой папке, после выхода из блока он переносится на место.

    with native_path(p) as q:
        pc = o3d.io.read_point_cloud(q)
    with native_path(out, 'w') as q:
        o3d.io.write_point_cloud(q, pc)

Переменная окружения PULSE_FORCE_ASCII_PATHS=1 включает обход и на других ОС (для тестов).
"""

import os
import sys
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


def _active(p):
    forced = os.environ.get('PULSE_FORCE_ASCII_PATHS') == '1'
    return (sys.platform == 'win32' or forced) and not str(p).isascii()


def _short_name(p):
    """Короткое имя 8.3 существующего файла (Windows) или None."""
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        f = ctypes.windll.kernel32.GetShortPathNameW
        f.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        buf = ctypes.create_unicode_buffer(32768)
        n = f(str(p), buf, len(buf))
        return buf.value if n else None
    except Exception:                                    # noqa: BLE001
        return None


_TMP = None


def ascii_tmpdir():
    """Папка для временных файлов с путём в ASCII (создаётся при первом обращении)."""
    global _TMP
    if _TMP is not None:
        return _TMP
    cands = [Path(tempfile.gettempdir()) / 'pulse_io']
    if sys.platform == 'win32':
        drive = os.environ.get('SystemDrive', 'C:') + '\\'
        cands += [Path(os.environ.get('PUBLIC', drive + 'Users\\Public')) / 'PulseScanTmp',
                  Path(os.environ.get('ProgramData', drive + 'ProgramData')) / 'PulseScanTmp',
                  Path(drive) / 'PulseScanTmp']
    for d in cands:
        if not str(d).isascii():
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / f'.probe_{uuid.uuid4().hex}'
            probe.write_bytes(b'')
            probe.unlink()
            _TMP = d
            return d
        except OSError:
            continue
    raise OSError('нет папки для временных файлов с путём латиницей: задайте TEMP с путём без кириллицы')


@contextmanager
def native_path(path, mode='r'):
    """Путь для C++-библиотек (см. заголовок модуля). mode: 'r' — чтение, 'w' — запись."""
    p = Path(path)
    if not _active(p):
        yield str(p)
        return
    if mode == 'r':
        short = _short_name(p)
        if short and short.isascii():
            yield short
            return
    tmp = ascii_tmpdir() / f'{uuid.uuid4().hex}{p.suffix.lower() if p.suffix.isascii() else ""}'
    try:
        if mode == 'r':
            shutil.copyfile(p, tmp)
        yield str(tmp)
        if mode == 'w' and tmp.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), str(p))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def write_cloud(path, pts):
    """Облако N×3 → .e57 / .ply / .pcd (по расширению), любой путь на любой ОС."""
    import numpy as np
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    P = np.ascontiguousarray(np.asarray(pts, np.float64)[:, :3])
    with native_path(path, 'w') as q:
        if path.suffix.lower() == '.e57':
            import pye57
            e57 = pye57.E57(q, mode='w')
            e57.write_scan_raw({'cartesianX': P[:, 0], 'cartesianY': P[:, 1], 'cartesianZ': P[:, 2]})
            e57.close()
        else:
            import open3d as o3d
            if not o3d.io.write_point_cloud(q, o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))):
                raise OSError(f'не удалось записать {path}')
    return path
