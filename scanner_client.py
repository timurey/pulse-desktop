#!/usr/bin/env python3
"""
Клиент веб-HMI сканера Pulse: список записей и скачивание bag прямо со сканера.

Сканер объявляет себя по mDNS как `pulse.local`, веб-HMI — порт 80.
  GET /api/status                          — состояние (идёт ли запись)
  GET /api/bags                            — список записей
  GET /api/bags/<name>/files               — файлы записи (orangepi/hmi/bag_files.py)
  GET /api/bags/<name>/files/<path>        — файл; Range — докачка

Только стандартная библиотека (urllib) — работает на macOS / Windows / Linux.
Разрешение имён `.local`: macOS — встроено; Windows 10+ — обычно встроено;
Linux — нужен avahi (nss-mdns). Иначе укажите IP сканера.

Usage:
    python scanner_client.py list [--host pulse.local]
    python scanner_client.py get NAME [NAME ...] --dest bags/
"""

import os
import sys
import json
import time
import argparse
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_HOST = 'pulse.local'
CHUNK = 1 << 20


class ScannerError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class ScannerClient:
    def __init__(self, host=DEFAULT_HOST, timeout=10.0):
        host = host.strip() or DEFAULT_HOST
        if '://' not in host:
            host = 'http://' + host
        self.base = host.rstrip('/')
        self.timeout = timeout

    # ── HTTP ─────────────────────────────────────────────────────────────
    def _url(self, *parts):
        return self.base + '/' + '/'.join(urllib.parse.quote(p, safe='/') for p in parts)

    def _json(self, *parts):
        try:
            with urllib.request.urlopen(self._url(*parts), timeout=self.timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code in (404, 405) and parts[-1] == 'files':     # старый веб-HMI
                raise ScannerError('сканер не отдаёт файлы записей - обновите веб-HMI '
                                   '(нужен orangepi/hmi/bag_files.py)') from e
            raise ScannerError(f'{self.base}: HTTP {e.code}') from e
        except (urllib.error.URLError, OSError) as e:
            raise ScannerError(f'нет связи со сканером {self.base}: {getattr(e, "reason", e)}') from e

    # ── API ──────────────────────────────────────────────────────────────
    def status(self):
        return self._json('api', 'status')

    def bags(self):
        """Записи сканера; у идущей сейчас записи — recording=True (её не скачивать)."""
        data = self._json('api', 'bags')
        try:
            st = self.status()
            busy = {st.get('bag_name'), st.get('pending_bag')} if st.get('recording') else set()
        except ScannerError:
            busy = set()
        out = []
        for b in data.get('bags', []):
            out.append(dict(b, recording=b['name'] in busy))
        return out

    def files(self, name):
        return self._json('api', 'bags', name, 'files')['files']

    def download(self, name, dest_root, progress=None, cancel=None):
        """
        Скачать запись в dest_root/<name>/ с докачкой. Уже скачанные файлы (тот же размер)
        пропускаются. progress(доля, текст); cancel() → True — прервать. → путь записи.
        """
        files = self.files(name)
        total = sum(f['size'] for f in files) or 1
        dest = Path(dest_root) / name
        done = 0
        t0 = time.time()
        for f in files:
            target = dest / f['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size == f['size']:
                done += f['size']
                continue
            part = target.with_name(target.name + '.part')
            have = part.stat().st_size if part.exists() else 0
            if have > f['size']:
                have = 0
            req = urllib.request.Request(self._url('api', 'bags', name, 'files', f['path']))
            if have:
                req.add_header('Range', f'bytes={have}-')
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    if have and r.status != 206:          # сервер не поддержал Range — сначала
                        have = 0
                    with open(part, 'ab' if have else 'wb') as out:
                        got = have
                        while True:
                            if cancel and cancel():
                                raise Cancelled('загрузка прервана')
                            chunk = r.read(CHUNK)
                            if not chunk:
                                break
                            out.write(chunk)
                            got += len(chunk)
                            if progress:
                                cur = done + got
                                speed = (cur - 0) / max(time.time() - t0, 1e-3)
                                progress(cur / total, f'{name}: {cur / 1e6:.0f}/{total / 1e6:.0f} МБ '
                                                      f'({speed / 1e6:.1f} МБ/с)')
            except (urllib.error.URLError, OSError) as e:
                if isinstance(e, Cancelled):
                    raise
                raise ScannerError(f'{name}/{f["path"]}: {getattr(e, "reason", e)} '
                                   f'(повторите - загрузка продолжится)') from e
            if part.stat().st_size != f['size']:
                raise ScannerError(f'{name}/{f["path"]}: получено {part.stat().st_size} из '
                                   f'{f["size"]} байт (повторите - продолжится)')
            os.replace(part, target)
            done += f['size']
        if progress:
            progress(1.0, f'{name}: скачано')
        return dest


def local_state(name, dest_root, size_b=None):
    """'скачан' / 'частично' / '' — есть ли запись в локальной папке."""
    d = Path(dest_root) / name
    if not d.exists():
        return ''
    parts = list(d.rglob('*.part'))
    have = sum(p.stat().st_size for p in d.rglob('*') if p.is_file() and p.suffix != '.part')
    if parts or (size_b and have < size_b):
        return 'частично'
    return 'скачан'


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                                # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description='Записи сканера Pulse по сети')
    ap.add_argument('cmd', choices=['list', 'get'])
    ap.add_argument('names', nargs='*')
    ap.add_argument('--host', default=DEFAULT_HOST)
    ap.add_argument('--dest', default='bags')
    a = ap.parse_args()
    c = ScannerClient(a.host)
    if a.cmd == 'list':
        for b in c.bags():
            st = local_state(b['name'], a.dest, b.get('size_b'))
            print(f"{b['name']:32s} {b['mtime']}  {b['size']:>10s}"
                  f"{'  [идёт запись]' if b['recording'] else ''}{'  [' + st + ']' if st else ''}")
    else:
        for n in a.names:
            p = c.download(n, a.dest, progress=lambda f, m: print(f'\r{m}   ', end='', flush=True))
            print(f'\n-> {p}')


if __name__ == '__main__':
    main()
