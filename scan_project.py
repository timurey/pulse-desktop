#!/usr/bin/env python3
"""
Проект из нескольких статических сканов: попарная стыковка → граф → позы → склейка.

  build  — анализ сканов, автоматическая стыковка пар, отбор надёжных рёбер,
           проверка циклов, оптимизация графа поз, запись project.json
  merge  — склейка сканов в позах проекта в один .e57/.pcd/.ply
           (по умолчанию без зеркальных отражений, см. reflections.py)
  view   — PNG «вид сверху» (для быстрой проверки без GUI)
  openings — найти проёмы во всех сканах проекта, присвоить им номера
  attach — разместить скан по паре проёмов (ручная стыковка, напр. фасад ↔ комната)

Позы в project.json — 4×4, переводят точки скана (в его исходной системе) в
систему опорного скана. Исходные файлы не меняются.

Usage:
    python scan_project.py build static_*.e57 -o project.json [--ref SCAN]
    python scan_project.py merge project.json -o merged.e57 [--voxel 0.02]
    python scan_project.py view project.json -o top.png
    python scan_project.py openings project.json
    python scan_project.py attach project.json FACADE.e57 --pair ROOM.e57:3 7 [--delta auto|0.4]
"""

import sys
import json
import time
import argparse
import itertools
from pathlib import Path

import numpy as np

from planes import analyze_scan, load_points, up_rotation
import plane_register as pr


def rel_path(path, project_file):
    """Путь скана для project.json: относительно папки проекта (переносимо между
    машинами и ОС), иначе абсолютный (другой диск в Windows)."""
    import os
    p = Path(path).resolve()
    base = Path(project_file).resolve().parent
    try:
        return Path(os.path.relpath(p, base)).as_posix()
    except ValueError:
        return str(p)


def abs_path(path, project_file):
    p = Path(path)
    return str(p if p.is_absolute() else (Path(project_file).resolve().parent / p).resolve())


def load_project(project_file):
    """Проект (.pulse — архив, или .json) с путями сканов, приведёнными к абсолютным."""
    import project_store
    proj = project_store.read_project(project_file)
    for e in proj['scans']:
        e['path'] = abs_path(e['path'], project_file)
    return proj


def save_project(proj, project_file):
    import project_store
    out = json.loads(json.dumps(proj))
    for e in out['scans']:
        e['path'] = rel_path(e['path'], project_file)
    project_store.write_project(project_file, out)     # .pulse: кеш анализа сохраняется


# пороги надёжного ребра
EDGE_MIN_SCORE  = 0.05
EDGE_MAX_VIOL   = 0.02
EDGE_MIN_MARGIN = 0.10
LOOP_MAX_T      = 0.10    # м — допустимая невязка цикла из трёх рёбер
LOOP_MAX_DEG    = 1.5


# Сильная однозначная пара допускается и с бо́льшими нарушениями: если между съёмками
# в помещении что-то сдвинули (мебель, дверь, человек), точки изменившихся предметов
# попадают в «пустоту» другого скана. Проверено 2026-10-04 на static_20261001: две серии
# сканов одного помещения с разницей 4 ч — пары между сериями давали 2.4–3 % нарушений при
# совпадении 90 % и отрыве 1–1.9; все 20 циклов сошлись до 0.4 см. На static_20260922 правило
# не добавляет ни одной пары.
EDGE_STRONG_SCORE  = 0.5
EDGE_STRONG_MARGIN = 0.5
EDGE_STRONG_CLOSE  = 0.8
EDGE_STRONG_VIOL   = 0.05


def edge_ok(r):
    if r['score'] < EDGE_MIN_SCORE or r['margin'] < EDGE_MIN_MARGIN:
        return False
    if r['violations'] <= EDGE_MAX_VIOL:
        return True
    return (r['violations'] <= EDGE_STRONG_VIOL and r['score'] >= EDGE_STRONG_SCORE
            and r['margin'] >= EDGE_STRONG_MARGIN and r.get('close', 0) >= EDGE_STRONG_CLOSE)


def information(r, resA, resB):
    """Матрица информации ребра для оптимизации графа (через Open3D)."""
    import open3d as o3d
    A = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(resA['down']))
    B = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(resB['down']))
    return o3d.pipelines.registration.get_information_matrix_from_point_clouds(
        B, A, pr.ICP_MAX_DIST / 2, np.array(r['T_canon']))


def loop_checks(edges, names):
    """Невязки всех треугольников графа (в канонических системах)."""
    E = {}
    for e in edges:
        T = np.array(e['T_canon'])
        E[(e['A'], e['B'])] = T
        E[(e['B'], e['A'])] = np.linalg.inv(T)
    out = []
    for a, b, c in itertools.combinations(names, 3):
        if (a, b) in E and (b, c) in E and (a, c) in E:
            # x_a = T_ab x_b = T_ab T_bc x_c  vs  T_ac x_c
            dt, dang = pr.pose_delta(E[(a, b)] @ E[(b, c)], E[(a, c)])
            out.append({'loop': [a, b, c], 'dt': dt, 'deg': dang,
                        'ok': dt <= LOOP_MAX_T and dang <= LOOP_MAX_DEG})
    return out


def spanning_poses(names, edges, ref):
    """Начальные позы (канон. система скана → канон. система ref) по дереву max score."""
    adj = {n: [] for n in names}
    for e in edges:
        T = np.array(e['T_canon'])
        adj[e['A']].append((e['score'], e['B'], T))            # x_A = T x_B
        adj[e['B']].append((e['score'], e['A'], np.linalg.inv(T)))
    poses = {ref: np.eye(4)}
    frontier = [(s, ref, nb, T) for s, nb, T in adj[ref]]
    while frontier:
        frontier.sort(key=lambda x: -x[0])
        s, par, n, T = frontier.pop(0)
        if n in poses:
            continue
        poses[n] = poses[par] @ T
        frontier += [(s2, n, nb, T2) for s2, nb, T2 in adj[n] if nb not in poses]
    return poses


def optimize(names, edges, poses, infos, ref):
    """Оптимизация графа поз (Open3D), только для связной с ref компоненты."""
    import open3d as o3d
    reg = o3d.pipelines.registration
    nodes = [n for n in names if n in poses]
    idx = {n: i for i, n in enumerate(nodes)}
    g = reg.PoseGraph()
    for n in nodes:
        g.nodes.append(reg.PoseGraphNode(poses[n]))
    for e, info in zip(edges, infos):
        if e['A'] not in idx or e['B'] not in idx:
            continue
        # Open3D: ребро source→target с T: x_target = T x_source → source=B, target=A
        g.edges.append(reg.PoseGraphEdge(idx[e['B']], idx[e['A']],
                                         np.array(e['T_canon']), info, uncertain=False))
    opt = reg.GlobalOptimizationOption(max_correspondence_distance=pr.ICP_MAX_DIST / 2,
                                       edge_prune_threshold=0.25,
                                       reference_node=idx[ref])
    o3d.utility.set_verbosity_level(o3d.utility.VerbosityLevel.Error)
    reg.global_optimization(g, reg.GlobalOptimizationLevenbergMarquardt(),
                            reg.GlobalOptimizationConvergenceCriteria(), opt)
    return {n: np.asarray(g.nodes[idx[n]].pose) for n in nodes}


def build(scans, out, ref=None, up='auto', yaw='manhattan', reuse=None):
    t0 = time.time()
    res = {}
    for s in scans:
        r = analyze_scan(s, up)
        res[Path(s).name] = (s, r)
        print(f"  анализ {Path(s).name}: up={r['up']}, плоскостей {len(r['planes'])}")
    names = list(res)
    # пары из прошлого проекта (те же сканы) — без повторной стыковки
    cached = {}
    if reuse:
        for p in json.loads(Path(reuse).read_text(encoding='utf-8'))['pairs']:
            if 'T_canon' in p:
                cached[(p['A'], p['B'])] = p
    pairs = []
    for a, b in itertools.combinations(names, 2):
        if (a, b) in cached:
            r = dict(cached[(a, b)])
        else:
            r = pr.register_pair(res[a][1], res[b][1], yaw, verbose=False)
            r['A'], r['B'] = a, b
        r['accepted'] = edge_ok(r)
        pairs.append(r)
        print(f"  {a} ← {b}: оценка {r['score']:+.3f}, нарушений {r['violations']:.3f}, "
              f"отрыв {r['margin']:.3f} {'✓' if r['accepted'] else ''}", flush=True)
    edges = [p for p in pairs if p['accepted']]
    loops = loop_checks(edges, names)
    bad = [l for l in loops if not l['ok']]
    for l in loops:
        print(f"  цикл {' → '.join(l['loop'])}: {l['dt'] * 100:.1f} см, {l['deg']:.2f}° "
              f"{'OK' if l['ok'] else 'НЕСОГЛАСОВАН'}")
    # опорный скан — с наибольшей суммой оценок рёбер
    deg = {n: sum(e['score'] for e in edges if n in (e['A'], e['B'])) for n in names}
    ref = ref or max(names, key=lambda n: deg[n])
    poses_c = spanning_poses(names, edges, ref)
    if len(edges) > len(poses_c) - 1:
        infos = [information(e, res[e['A']][1], res[e['B']][1]) for e in edges]
        poses_c = optimize(names, edges, poses_c, infos, ref)
    R_ref = up_rotation(res[ref][1]['up'])
    proj = {'frame': ref, 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
            'thresholds': {'min_score': EDGE_MIN_SCORE, 'max_viol': EDGE_MAX_VIOL,
                           'min_margin': EDGE_MIN_MARGIN},
            'scans': [], 'pairs': [], 'loops': loops}
    for n in names:
        path, r = res[n]
        entry = {'id': n, 'path': str(Path(path).resolve()), 'up': r['up']}
        if n in poses_c:
            R_n = np.asarray(r['R_up'])
            # исходная система скана → исходная система ref
            T = pr.make_T(R_ref.T, np.zeros(3)) @ poses_c[n] @ pr.make_T(R_n, np.zeros(3))
            entry['pose'] = T.tolist()
        else:
            entry['pose'] = None                  # не связан с ref — нужна ручная стыковка
        proj['scans'].append(entry)
    proj['pairs'] = pairs
    save_project(proj, out)
    placed = sum(1 for s in proj['scans'] if s['pose'] is not None)
    print(f"\nОпорный скан: {ref}. Размещено {placed}/{len(names)}, рёбер {len(edges)}, "
          f"несогласованных циклов {len(bad)}. → {out}  ({time.time() - t0:.0f} c)")
    return proj


def placed_clouds(proj, voxel, clean=True):
    import open3d as o3d
    for s in proj['scans']:
        if s['pose'] is None:
            continue
        if clean:
            from reflections import clean_scan
            pts, _ = clean_scan(s['path'], s.get('up', 'auto'))
        else:
            pts = load_points(s['path'])
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
        if voxel > 0:
            pc = pc.voxel_down_sample(voxel)
        yield s, pr.transform(np.asarray(pc.points), np.array(s['pose']))


def merge(project, out, voxel=0.02, clean=True):
    import open3d as o3d
    proj = load_project(project)
    parts = [p for _, p in placed_clouds(proj, voxel, clean)]
    pts = np.vstack(parts)
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
    if voxel > 0:
        pc = pc.voxel_down_sample(voxel)
    pts = np.asarray(pc.points)
    out = Path(out)
    if out.suffix.lower() == '.e57':
        import pye57
        e57 = pye57.E57(str(out), mode='w')
        e57.write_scan_raw({'cartesianX': pts[:, 0], 'cartesianY': pts[:, 1],
                            'cartesianZ': pts[:, 2]})
        e57.close()
    else:
        o3d.io.write_point_cloud(str(out), pc)
    print(f"{out}: {len(pts):,} точек из {len(parts)} сканов")


def view(project, out, px=0.05, clean=True):
    """Вид сверху (в канонической системе опорного скана): цвет = скан, яркость = высота."""
    import open3d as o3d
    proj = load_project(project)
    ref = next(s for s in proj['scans'] if s['id'] == proj['frame'])
    R = up_rotation(ref['up'])
    clouds = [(s, p @ R.T) for s, p in placed_clouds(proj, 0.05, clean)]
    allp = np.vstack([p for _, p in clouds])
    lo, hi = np.percentile(allp[:, :2], 0.5, axis=0), np.percentile(allp[:, :2], 99.5, axis=0)
    W, H = (np.ceil((hi - lo) / px).astype(int) + 1)
    img = np.full((H, W, 3), 255, np.uint8)
    pal = np.array([[31, 119, 180], [255, 127, 14], [44, 160, 44], [214, 39, 40],
                    [148, 103, 189], [140, 86, 75], [227, 119, 194], [127, 127, 127],
                    [188, 189, 34], [23, 190, 207]], np.uint8)
    for k, (s, p) in enumerate(clouds):
        z = p[:, 2]
        m = (z > -1.2) & (z < 1.0)                 # срез стен, без пола и потолка
        ij = np.floor((p[m, :2] - lo) / px).astype(int)
        ok = (ij[:, 0] >= 0) & (ij[:, 0] < W) & (ij[:, 1] >= 0) & (ij[:, 1] < H)
        ij = ij[ok]
        img[H - 1 - ij[:, 1], ij[:, 0]] = pal[k % len(pal)]
        # позиция сканера — крестик
        c = np.floor((np.array(s['pose'])[:3, 3] @ R.T)[:2] - lo) / px
        cx, cy = int(c[0]), H - 1 - int(c[1])
        img[max(0, cy - 6):cy + 7, max(0, cx - 1):cx + 2] = 0
        img[max(0, cy - 1):cy + 2, max(0, cx - 6):cx + 7] = 0
    o3d.io.write_image(str(out), o3d.geometry.Image(img))
    legend = ', '.join(f"{k}:{s['id']}" for k, (s, _) in enumerate(clouds))
    print(f"{out}: {W}×{H} px, {px * 100:.0f} см/px; цвета: {legend}")


def _scan_res(entry):
    return analyze_scan(entry['path'], entry.get('up', 'auto'), cache=False)


def _T_common(proj, entry, res):
    """Канон. система скана → канон. система опорного скана."""
    ref = next(s for s in proj['scans'] if s['id'] == proj['frame'])
    R_ref = up_rotation(ref['up'])
    return pr.make_T(R_ref, np.zeros(3)) @ np.array(entry['pose']) @ \
        pr.make_T(np.asarray(res['R_up']).T, np.zeros(3))


def openings_cmd(project):
    """Находит проёмы во всех сканах проекта и сохраняет их с номерами."""
    from openings import find_openings
    proj = load_project(project)
    for e in proj['scans']:
        res = _scan_res(e)
        ops = find_openings(res)
        e['openings'] = [o.to_json() for o in ops]       # канон. система самого скана
        state = 'размещён' if e['pose'] is not None else 'НЕ размещён'
        print(f"{e['id']} ({state}): проёмов {len(ops)}")
        for k, o in enumerate(ops):
            c = np.array(o.center)
            if e['pose'] is not None:
                c = pr.transform(c[None], _T_common(proj, e, res))[0]
            sill = f"{o.sill:.2f}" if o.sill is not None else '—'
            print(f"   [{k:2d}] {o.kind:6s} {o.width:.2f}×{o.height:.2f} м  низ {sill} м  "
                  f"центр {np.round(c, 2)}{' (в системе проекта)' if e['pose'] is not None else ''}")
    save_project(proj, project)


def attach(project, scan, pair, delta='auto', same_side=False):
    """
    Размещает скан по паре проёмов: pair = ('A.e57', k, m) — проём k размещённого
    скана A ↔ проём m скана scan. Толщина стены — перебором (delta='auto') или
    заданная. Результат проверяется по свободному пространству всех размещённых сканов.
    """
    proj = load_project(project)
    byid = {e['id']: e for e in proj['scans']}
    name = Path(scan).name
    if name not in byid:
        proj['scans'].append({'id': name, 'path': str(Path(scan).resolve()), 'up': 'auto',
                              'pose': None})
        byid = {e['id']: e for e in proj['scans']}
    eB = byid[name]
    a_id, k, m = pair
    eA = byid[a_id]
    if eA['pose'] is None:
        raise SystemExit(f"{a_id} не размещён")
    if 'openings' not in eA or 'openings' not in eB:
        raise SystemExit("Сначала: scan_project.py openings project.json "
                         "(нумерация проёмов)")
    resB = _scan_res(eB)
    eB['up'] = resB['up']
    placed = []
    resA = None
    for e in proj['scans']:
        if e['pose'] is None or e['id'] == name:
            continue
        r = _scan_res(e)
        placed.append((r['down'], _T_common(proj, e, r)))
        if e['id'] == a_id:
            resA, TA = r, placed[-1][1]
    oA, oB = eA['openings'][k], eB['openings'][m]
    cA = pr.transform(np.array([oA['center']]), TA)[0]
    nA = TA[:3, :3] @ np.array(oA['normal'])
    scorer = pr.MultiScanScorer(placed, resB['down'])
    if delta == 'auto':
        res = pr.search_opening_pair(scorer, cA, nA, oB['center'], oB['normal'],
                                     same_side=same_side)
    else:
        T = pr.pose_from_opening_pair(cA, nA, oB['center'], oB['normal'], float(delta), same_side)
        res = [(*scorer.score(T), float(delta), T)]
    s, nclose, viol, d, T_c = res[0]
    print(f"{name} ← проём {a_id}[{k}] ({oA['width']:.2f}×{oA['height']:.2f}) ↔ [{m}] "
          f"({oB['width']:.2f}×{oB['height']:.2f})")
    for r in res[:5]:
        print(f"   δ={r[3]:.2f} м  оценка {r[0]:+.3f}  совпавших точек {r[1]}  нарушений {r[2]:.4f}")
    ref = byid[proj['frame']]
    R_ref = up_rotation(ref['up'])
    T = pr.make_T(R_ref.T, np.zeros(3)) @ T_c @ pr.make_T(np.asarray(resB['R_up']), np.zeros(3))
    eB['pose'] = T.tolist()
    eB['method'] = {'opening_pair': [a_id, k, m], 'delta': d, 'score': s,
                    'n_close': nclose, 'violations': viol}
    save_project(proj, project)
    print(f"   принято δ={d:.2f} м → {project}")
    if viol > EDGE_MAX_VIOL:
        print(f"   ВНИМАНИЕ: нарушений {viol:.3f} > {EDGE_MAX_VIOL} — пара проёмов, вероятно, неверна")


def main():
    ap = argparse.ArgumentParser(description="Проект из статических сканов")
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('scans', nargs='+')
    b.add_argument('-o', '--output', default='project.json')
    b.add_argument('--ref')
    b.add_argument('--up', default='auto')
    b.add_argument('--yaw', default='manhattan', choices=['manhattan', 'full'])
    b.add_argument('--reuse', help='project.json с уже посчитанными парами')
    m = sub.add_parser('merge')
    m.add_argument('project')
    m.add_argument('-o', '--output', default='merged.e57')
    m.add_argument('--voxel', type=float, default=0.02)
    m.add_argument('--keep-reflections', action='store_true',
                   help='не удалять зеркальные отражения (стёкла, глянцевый пол)')
    v = sub.add_parser('view')
    v.add_argument('project')
    v.add_argument('-o', '--output', default='top.png')
    v.add_argument('--px', type=float, default=0.05)
    v.add_argument('--keep-reflections', action='store_true')
    o = sub.add_parser('openings')
    o.add_argument('project')
    t = sub.add_parser('attach')
    t.add_argument('project')
    t.add_argument('scan')
    t.add_argument('--pair', nargs=2, required=True, metavar=('A.e57:K', 'M'),
                   help='проём K размещённого скана A и проём M присоединяемого скана')
    t.add_argument('--delta', default='auto', help="толщина стены, м, или auto")
    t.add_argument('--same-side', action='store_true',
                   help='оба скана видят проём с одной стороны стены')
    a = ap.parse_args()
    if a.cmd == 'build':
        build(a.scans, a.output, a.ref, a.up, a.yaw, a.reuse)
    elif a.cmd == 'merge':
        merge(a.project, a.output, a.voxel, not a.keep_reflections)
    elif a.cmd == 'view':
        view(a.project, a.output, a.px, not a.keep_reflections)
    elif a.cmd == 'openings':
        openings_cmd(a.project)
    else:
        a_id, k = a.pair[0].rsplit(':', 1)
        attach(a.project, a.scan, (a_id, int(k), int(a.pair[1])), a.delta, a.same_side)


if __name__ == '__main__':
    main()
