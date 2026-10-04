#!/usr/bin/env python3
"""
Поверхность (треугольная сетка) по сканам — экспериментально.

Точки скана — полного разрешения, без зеркальных отражений и удалённого вручную
(как при экспорте склейки), прореженные до точность/2. Нормали оцениваются по
соседям и направляются **на свой сканер** (в канонической системе он в начале
координат) — надёжнее, чем согласование касательных плоскостей.

Методы:
  poisson — гладкая замкнутая поверхность (Open3D, Пуассон). Глубина октодерева —
            из точности. Пуассон «додумывает» поверхность в дырах и окнах; такие
            «пузыри» срезаются: вершины с низкой плотностью (квантиль `trim`) и дальше
            1,5·точности от ближайшей исходной точки удаляются.
            ВАЖНО: PoissonRecon внутри Open3D 0.19 при ошибке «Failed to close loop»
            вызывает exit() — процесс завершается целиком (случайно, в многопоточном
            режиме). Поэтому сетка всегда считается в отдельном процессе (`_run_child`),
            Пуассон — в один поток, с повтором при неудаче; процесс можно остановить.
  bpa     — ball pivoting: треугольники строго по точкам, без додумывания
            (дыры остаются там, где точек мало). Радиусы 1, 2, 4 × точность.
            Однопоточный и медленный: больше BPA_MAX_POINTS точек — точность загрубляется.

Результат — вершины и треугольники в общей системе сеанса (Z вверх).
"""

import os
import time
import tempfile

import numpy as np

MAX_POINTS = 6_000_000      # больше — точность автоматически загрубляется
BPA_MAX_POINTS = 800_000    # ball pivoting однопоточный и медленный (~35 с на 340 тыс. точек)


def scan_points(session, sc, acc, progress=None):
    """Точки и нормали скана для сетки (общая система). → (P N×3, Nrm N×3)."""
    import open3d as o3d
    import planes
    import reflections
    if sc.clean:
        pts, _ = reflections.clean_scan(sc.path, sc.up)
    else:
        pts = planes.load_points(sc.path)
    P = np.asarray(pts, float) @ np.asarray(sc.R_up).T        # канон. система: сканер в начале
    if sc.erase or len(getattr(sc, 'drop', ())):
        P = P[sc.keep_mask(P)]
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
    pc = pc.voxel_down_sample(acc / 2)
    if progress:
        progress(f'нормали {len(pc.points):,} точек'.replace(',', ' '))
    pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=3 * acc, max_nn=30))
    pc.orient_normals_towards_camera_location(np.zeros(3))
    T = session.Tc(sc)
    R, t = T[:3, :3], T[:3, 3]
    return np.asarray(pc.points) @ R.T + t, np.asarray(pc.normals) @ R.T


class Cancelled(RuntimeError):
    pass


def _coarsen(P, N, acc, max_points):
    """Прорежение до max_points (точность загрубляется). → (P, N, точность)."""
    import open3d as o3d
    if len(P) <= max_points:
        return P, N, acc
    acc2 = acc * float(np.sqrt(len(P) / max_points))
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
    pc.normals = o3d.utility.Vector3dVector(N)
    pc = pc.voxel_down_sample(acc2 / 2)
    return np.asarray(pc.points), np.asarray(pc.normals), acc2


def build(session, scans, method='poisson', acc=0.05, trim=0.1, progress=None, cancel=None):
    """
    Сетка по сканам. → dict(V float32 K×3, F int32 M×3, info).
    progress(доля, текст); cancel() → True — прервать (Cancelled). Сам расчёт — в
    отдельном процессе: его можно остановить, а сбой Open3D не роняет окно.
    """
    import open3d as o3d
    t0 = time.time()
    say = (lambda f, m: progress(f, m)) if progress else (lambda f, m: None)
    check = (lambda: cancel and cancel())
    Ps, Ns = [], []
    for k, sc in enumerate(scans):
        if check():
            raise Cancelled('остановлено')
        say(0.4 * k / max(1, len(scans)), f'точки {sc.id}')
        P, N = scan_points(session, sc, acc, lambda m: say(0.4 * (k + 0.5) / max(1, len(scans)), m))
        Ps.append(P)
        Ns.append(N)
    P, N = np.vstack(Ps), np.vstack(Ns)
    P, N, acc_used = _coarsen(P, N, acc, MAX_POINTS if method == 'poisson' else BPA_MAX_POINTS)
    info = {'method': method, 'acc': acc, 'acc_used': round(acc_used, 4), 'points': int(len(P)),
            'scans': [sc.id for sc in scans]}
    name = 'Пуассон' if method == 'poisson' else 'ball pivoting'
    if method == 'poisson':
        ext = float(np.max(np.ptp(P, axis=0)))
        depth = int(np.clip(np.ceil(np.log2(max(ext, 1e-3) / acc_used)), 6, 12))
        attempts = [dict(depth=depth, scale=1.1), dict(depth=depth, scale=1.3),
                    dict(depth=depth - 1, scale=1.1), dict(depth=depth - 2, scale=1.2)]
        attempts = [a for a in attempts if a['depth'] >= 6]
    else:
        attempts = [dict(radii=[acc_used, 2 * acc_used, 4 * acc_used])]
    z = None
    for k, a in enumerate(attempts):
        label = f"{name}{', глубина ' + str(a['depth']) if 'depth' in a else ''}" + (' (повтор)' if k else '')
        say(0.45, f'{label}…')
        z = _run_child(method, P, N, a, cancel,
                       lambda sec, label=label: say(0.5, f'{label}: {sec:.0f} c (Esc — остановить)'))
        if z is not None:
            info.update(a)
            break
    if z is None:
        raise RuntimeError(f'{name}: сетка не построена (сбой Open3D) — попробуйте другую точность или метод')
    mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(z['V']), o3d.utility.Vector3iVector(z['F']))
    if method == 'poisson':
        say(0.9, 'обрезка «пузырей»')
        dens = z['D']
        drop = dens < np.quantile(dens, trim) if trim > 0 else np.zeros(len(dens), bool)
        # вершины дальше 1.5·точности от исходных точек — додуманная поверхность
        V = np.asarray(mesh.vertices)
        nns = o3d.core.nns.NearestNeighborSearch(o3d.core.Tensor(np.ascontiguousarray(P, np.float32)))
        r = 1.5 * acc_used
        nns.hybrid_index(r)
        _, _, cnt = nns.hybrid_search(o3d.core.Tensor(np.ascontiguousarray(V, np.float32)), r, 1)
        far = cnt.numpy() == 0
        mesh.remove_vertices_by_mask(drop | far)
        info['trimmed'] = int((drop | far).sum())
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_unreferenced_vertices()
    V = np.asarray(mesh.vertices, np.float32)
    F = np.asarray(mesh.triangles, np.int32)
    info.pop('radii', None)
    info.update(triangles=int(len(F)), vertices=int(len(V)), seconds=round(time.time() - t0, 1))
    say(1.0, f'сетка: {len(F):,} треугольников'.replace(',', ' '))
    return {'V': V, 'F': F, 'info': info}


def _child(inp, out):
    """Выполняется в отдельном процессе: сетка по точкам из inp → out (npz)."""
    import open3d as o3d
    z = np.load(inp, allow_pickle=False)
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(z['P']))
    pcd.normals = o3d.utility.Vector3dVector(z['N'])
    if str(z['method']) == 'poisson':
        mesh, dens = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
            pcd, depth=int(z['depth']), scale=float(z['scale']), n_threads=1)
        D = np.asarray(dens)
    else:
        mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_ball_pivoting(
            pcd, o3d.utility.DoubleVector([float(r) for r in z['radii']]))
        D = np.zeros(0)
    np.savez(out + '.tmp.npz', V=np.asarray(mesh.vertices), F=np.asarray(mesh.triangles), D=D)
    os.replace(out + '.tmp.npz', out)


def _run_child(method, P, N, params, cancel=None, tick=None, timeout=3600):
    """
    Расчёт сетки в отдельном процессе (см. заголовок модуля). → npz-результат или None
    (процесс завершился без результата — сбой PoissonRecon). cancel() → True — остановить.
    """
    import multiprocessing as mp
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, 'in.npz'), os.path.join(d, 'out.npz')
        np.savez(inp, P=P, N=N, method=method, **params)
        # spawn — новый интерпретатор (и в собранном приложении: freeze_support в scan_qt.py);
        # PoissonRecon может вызвать exit() — завершится только дочерний процесс
        proc = mp.get_context('spawn').Process(target=_child, args=(inp, out), daemon=True)
        proc.start()
        t0 = time.time()
        while proc.is_alive():
            if cancel and cancel():
                proc.kill()
                proc.join()
                raise Cancelled('остановлено')
            if time.time() - t0 > timeout:
                proc.kill()
                proc.join()
                raise RuntimeError(f'расчёт сетки дольше {timeout // 60} мин — остановлен')
            if tick:
                tick(time.time() - t0)
            proc.join(0.5)
        if not os.path.exists(out):
            return None
        z = np.load(out)
        return {k: z[k] for k in z.files}


def export(mesh, path):
    """Сетка → OBJ / PLY / STL (по расширению)."""
    import open3d as o3d
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(np.asarray(mesh['V'], float)),
                                  o3d.utility.Vector3iVector(np.asarray(mesh['F'], np.int32)))
    m.compute_vertex_normals()
    m.compute_triangle_normals()
    if not o3d.io.write_triangle_mesh(str(path), m):
        raise OSError(f'не удалось записать {path}')
    return path
