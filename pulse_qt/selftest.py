"""
Самопроверка (в том числе собранного приложения):

    "Pulse Scan" --selftest OUT_DIR [проект.pulse] [--bag BAG]

Открывает проект, ждёт анализа, считает качество совмещения, строит сетку опорного скана
(дочерний процесс), при --bag — реконструирует bag; снимок окна и отчёт — в OUT_DIR.
Код выхода 0 — всё прошло.
"""

import json
import sys
import time
import traceback
from pathlib import Path


def run(args):
    args = [a for a in args if a != '--selftest']
    bag = None
    if '--bag' in args:
        i = args.index('--bag')
        bag = args[i + 1]
        del args[i:i + 2]
    out = Path(args[0]) if args else Path.home() / 'Pulse' / 'selftest'
    out.mkdir(parents=True, exist_ok=True)
    project = args[1] if len(args) > 1 else None
    report = {'ok': False, 'steps': []}

    def step(name, ok, **kw):
        report['steps'].append(dict(name=name, ok=bool(ok), **kw))
        print(('OK  ' if ok else 'FAIL') + f' {name} {kw}', flush=True)
        return ok

    try:
        from .app import make_app, session_from_args
        from .version import __version__
        report['version'] = __version__
        app, theme = make_app()
        from .main_window import MainWindow
        w = MainWindow(session_from_args([project] if project else []), theme)
        w.resize(1440, 900)
        w.show()

        def pump(sec):
            t = time.time()
            while time.time() - t < sec:
                app.processEvents()
                time.sleep(0.02)
        t0 = time.time()
        if project:
            while (w.busy or w.analyzing or not w.view.items) and time.time() - t0 < 600:
                pump(0.2)
            s = w.s
            step('проект открыт и проанализирован', all(sc.analyzed for sc in s.scans),
                 scans=len(s.scans), placed=len(s.placed()), seconds=round(time.time() - t0, 1))
            import quality
            q = quality.compute(s, 'planes')
            step('качество совмещения', len(q) > 0, cells=len(q))
            import surface
            m = surface.build(s, [s.ref], 'poisson', 0.1, 0.1)
            step('сетка (дочерний процесс)', m['info']['triangles'] > 0, triangles=m['info']['triangles'])
            w.view_3d()
            pump(0.5)
            w.view.render_now()
            pump(0.2)
            w.grab().save(str(out / 'window.png'))
            img = w.view.grab_scene()
            step('3D-вид рисует', img is not None and img.width() > 0)
        if bag:
            import bag_reconstruct as br
            P, meta = br.reconstruct(bag)
            step('реконструкция bag', len(P) > 1000, points=len(P), calibration=meta['calibration']['source'])
        report['ok'] = all(x['ok'] for x in report['steps'])
    except Exception as e:                               # noqa: BLE001
        report['error'] = f'{type(e).__name__}: {e}'
        report['trace'] = traceback.format_exc()
        print(report['trace'], flush=True)
    (out / 'selftest.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    print('ИТОГ:', 'всё прошло' if report['ok'] else 'ОШИБКИ', flush=True)
    sys.stdout.flush()
    return 0 if report['ok'] else 1
