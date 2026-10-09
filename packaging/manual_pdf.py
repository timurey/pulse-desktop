#!/usr/bin/env python3
"""
Руководство пользователя в PDF: docs/manual/README.md → docs/manual/PulseScan-manual.pdf.

Markdown → HTML с вёрсткой для печати (A4, шрифты IBM Plex как в программе, титульная
страница, разделы с новой страницы, номера страниц, рабочие ссылки оглавления) → PDF через
headless Chrome / Chromium.

    .venv312/bin/pip install markdown pypdf
    .venv312/bin/python packaging/manual_pdf.py [--out файл.pdf] [--chrome путь]
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / 'docs' / 'manual' / 'README.md'
FONTS = ROOT / 'pulse_qt' / 'fonts'

CHROME_CANDIDATES = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'google-chrome', 'chromium', 'chromium-browser',
]

CSS = """
@font-face { font-family: 'Plex'; src: url('{fonts}/IBMPlexSans.ttf'); font-weight: 100 900; }
@font-face { font-family: 'PlexMono'; src: url('{fonts}/IBMPlexMono-Regular.ttf'); font-weight: 400; }
@font-face { font-family: 'PlexMono'; src: url('{fonts}/IBMPlexMono-Medium.ttf'); font-weight: 500; }
@page {
  size: A4; margin: 18mm 17mm 18mm 17mm;
  @bottom-right { content: counter(page); font: 9pt 'Plex'; color: #8a93a3; }
  @bottom-left { content: 'Pulse Scan {version} · руководство пользователя'; font: 9pt 'Plex'; color: #8a93a3; }
}
@page :first { @bottom-right { content: none; } @bottom-left { content: none; } }
* { box-sizing: border-box; }
html { font-family: 'Plex', sans-serif; font-size: 10.5pt; line-height: 1.5; color: #1d2330; }
body { margin: 0; }
a { color: #2457b8; text-decoration: none; }
h1, h2, h3 { line-height: 1.25; color: #121722; break-after: avoid; }
h2 { font-size: 19pt; font-weight: 600; margin: 0 0 10pt; padding-bottom: 6pt; border-bottom: 2px solid #2b6cc4;
     break-before: page; }
h3 { font-size: 13pt; font-weight: 600; margin: 18pt 0 6pt; }
p, li { orphans: 3; widows: 3; }
ul, ol { padding-left: 18pt; }
li { margin: 2pt 0; }
code { font-family: 'PlexMono', monospace; font-size: 9pt; background: #eef1f6; padding: 0.5pt 3pt; border-radius: 3pt; }
pre { background: #eef1f6; padding: 8pt 10pt; border-radius: 5pt; break-inside: avoid; white-space: pre-wrap; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0 12pt; font-size: 9.5pt; break-inside: auto; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th { text-align: left; font-weight: 600; background: #eef1f6; color: #3b4456; }
th, td { border-bottom: 1px solid #d9dee8; padding: 4pt 6pt; vertical-align: top; }
img { max-width: 100%; display: block; margin: 8pt auto 2pt; border: 1px solid #d9dee8; border-radius: 4pt;
      break-inside: avoid; }
p:has(> img:only-child) { break-inside: avoid; text-align: center; margin: 6pt 0 10pt; }
img.ribbon { border-radius: 2pt; }
hr { display: none; }
blockquote { margin: 8pt 0; padding: 4pt 10pt; border-left: 3px solid #2b6cc4; background: #f3f6fb; }
.cover { height: 255mm; display: flex; flex-direction: column; justify-content: center; align-items: flex-start;
         padding: 0 6mm; }
.cover img { border: 0; width: 34mm; margin: 0 0 14mm; }
.cover .t { font-size: 34pt; font-weight: 600; color: #121722; letter-spacing: -0.5pt; }
.cover .s { font-size: 16pt; color: #3b4456; margin-top: 4pt; }
.cover .lead { font-size: 11.5pt; color: #3b4456; margin-top: 16mm; max-width: 140mm; }
.cover .v { margin-top: 22mm; font-size: 10pt; color: #8a93a3; }
.toc h2 { break-before: page; }
.toc ol { list-style: none; padding-left: 0; columns: 1; }
.toc li { display: flex; border-bottom: 1px dotted #c6ccd8; padding: 4pt 0; font-size: 11pt; }
"""


def find_chrome(explicit=None):
    for c in ([explicit] if explicit else []) + CHROME_CANDIDATES:
        if c and (Path(c).exists() or shutil.which(c)):
            return shutil.which(c) or c
    raise SystemExit('не найден Chrome / Chromium / Edge: укажите --chrome путь')


def build_html(version):
    import markdown
    from markdown.extensions.toc import slugify_unicode
    src = MANUAL.read_text(encoding='utf-8')
    # заголовок и строку версии — на титульную страницу; «Содержание» — отдельной страницей
    src = re.sub(r'\A# .*?\n', '', src, count=1)
    src = re.sub(r'\AВерсия программы:.*?\n', '', src.lstrip(), count=1)
    lead, _, rest = src.partition('\n---\n')
    toc_part, _, body = rest.partition('\n---\n')
    md = lambda text: markdown.markdown(text, extensions=['tables', 'fenced_code', 'attr_list', 'sane_lists',
                                                          'toc'],
                                        extension_configs={'toc': {'slugify': slugify_unicode}})
    body_html = md(body)
    body_html = re.sub(r'<img alt="([^"]*)" src="img/ribbon_', r'<img class="ribbon" alt="\1" src="img/ribbon_', body_html)
    toc_html = md(toc_part)
    # якоря латиницей: кириллица в percent-encoding длиннее 127 байт, на что жалуются просмотрщики PDF
    ids = {}
    for i, k in enumerate(re.findall(r' id="([^"]+)"', body_html)):
        ids.setdefault(k, f's{i}')
    body_html = re.sub(r' id="([^"]+)"', lambda m: f' id="{ids[m.group(1)]}"', body_html)
    fix = lambda h: re.sub(r'href="#([^"]+)"',
                           lambda m: f'href="#{ids.get(urllib.parse.unquote(m.group(1)), m.group(1))}"', h)
    body_html, toc_html = fix(body_html), fix(toc_html)
    icon = (ROOT / 'packaging' / 'icon.png').as_uri()
    css = CSS.replace('{fonts}', FONTS.as_uri()).replace('{version}', version)
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<base href="{(MANUAL.parent.as_uri())}/"><title>Pulse Scan — руководство пользователя</title>
<style>{css}</style></head><body>
<section class="cover">
  <img src="{icon}" alt="">
  <div class="t">Pulse Scan</div>
  <div class="s">Руководство пользователя</div>
  <div class="lead">{md(lead)}</div>
  <div class="v">Версия {version} · {time.strftime('%Y-%m-%d')}</div>
</section>
<section class="toc">{toc_html}</section>
{body_html}
</body></html>"""


def print_pdf(html, out, chrome):
    tmp = Path(tempfile.mkdtemp(prefix='pulse_manual_'))
    page = tmp / 'manual.html'
    page.write_text(html, encoding='utf-8')
    prof = tmp / 'profile'
    pdf = tmp / 'out.pdf'
    cmd = [chrome, '--headless=new', '--disable-gpu', f'--user-data-dir={prof}', '--no-pdf-header-footer',
           '--allow-file-access-from-files', '--virtual-time-budget=15000', f'--print-to-pdf={pdf}', page.as_uri()]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    last = -1
    while time.time() - t0 < 180:                      # Chrome иногда не выходит сам — ждём файл
        if pdf.exists() and pdf.stat().st_size > 0 and pdf.stat().st_size == last:
            break
        last = pdf.stat().st_size if pdf.exists() else -1
        if proc.poll() is not None and pdf.exists():
            break
        time.sleep(1.0)
    proc.kill()
    if not pdf.exists():
        raise SystemExit('Chrome не создал PDF')
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pdf), str(out))
    shutil.rmtree(tmp, ignore_errors=True)


def set_meta(out, version):
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return None
    r = PdfReader(str(out))
    w = PdfWriter(clone_from=r)
    w.add_metadata({'/Title': f'Pulse Scan {version} — руководство пользователя', '/Author': 'Pulse',
                    '/Subject': 'Обработка сканов лидара Pulse: импорт, стыковка, контроль, чистка, экспорт'})
    with open(out, 'wb') as f:
        w.write(f)
    return len(r.pages)


def main():
    ap = argparse.ArgumentParser(description='Руководство Pulse Scan в PDF')
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'manual' / 'PulseScan-manual.pdf'))
    ap.add_argument('--chrome', default=None)
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    from pulse_qt.version import __version__
    out = Path(a.out)
    html = build_html(__version__)
    if os.environ.get('PULSE_MANUAL_HTML'):
        Path(os.environ['PULSE_MANUAL_HTML']).write_text(html, encoding='utf-8')
    print_pdf(html, out, find_chrome(a.chrome))
    n = set_meta(out, __version__)
    print(f'{out}: {out.stat().st_size / 1e6:.1f} МБ' + (f', страниц {n}' if n else ''))


if __name__ == '__main__':
    main()
