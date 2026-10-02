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
            режиме). Поэтому Пуассон считается в отдельном процессе, в один поток, с
            повтором при неудаче (`_poisson_safe`).
  bpa     — ball pivoting: треугольники строго по точкам, без додумывания
            (дыры остаются там, где точек мало). Радиусы 1, 2, 4 × точность.

Результат — вершины и треугольники в общей системе сеанса (Z вверх).
"""

import os
import sys
import time
import tempfile
import subprocess
from pathlib import Path

import numpy as np

MAX_POINTS = 6_000_000      # больше — точность автоматически загрубляется


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


def build(session, scans, method='poisson', acc=0.05, trim=0.1, progress=None):
    """
    Сетка по сканам. → dict(V float32 K×3, F int32 M×3, info).
    progress(доля, текст) — по этапам (сами операции Open3D не прерываются).
    """
    import open3d as o3d
    t0 = time.time()
    say = (lambda f, m: progress(f, m)) if progress else (lambda f, m: None)
    Ps, Ns = [], []
    for k, sc in enumerate(scans):
        say(0.4 * k / max(1, len(scans)), f'точки {sc.id}')
        P, N = scan_points(session, sc, acc, lambda m: say(0.4 * (k + 0.5) / max(1, len(scans)), m))
        Ps.append(P)
        Ns.append(N)
    P, N = np.vstack(Ps), np.vstack(Ns)
    acc_used = acc
    if len(P) > MAX_POINTS:                              # загрубить, чтобы хватило памяти и времени
        acc_used = acc * float(np.sqrt(len(P) / MAX_POINTS))
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
        pc.normals = o3d.utility.Vector3dVector(N)
        pc = pc.voxel_down_sample(acc_used / 2)
        P, N = np.asarray(pc.points), np.asarray(pc.normals)
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
    pcd.normals = o3d.utility.Vector3dVector(N)
    info = {'method': method, 'acc': acc, 'acc_used': round(acc_used, 4), 'points': int(len(P)),
            'scans': [sc.id for sc in scans]}
    if method == 'poisson':
        ext = float(np.max(np.ptp(P, axis=0)))
        depth = int(np.clip(np.ceil(np.log2(max(ext, 1e-3) / acc_used)), 6, 12))
        info['depth'] = depth
        say(0.45, f'Пуассон, глубина {depth}…')
        V0, F0, dens, used = _poisson_safe(P, N, depth, lambda m: say(0.5, m))
        info['depth'] = used
        mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(V0), o3d.utility.Vector3iVector(F0))
        say(0.85, 'обрезка «пузырей»')
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
    else:
        radii = [acc_used, 2 * acc_used, 4 * acc_used]
        say(0.45, 'ball pivoting…')
        mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_ball_pivoting(
            pcd, o3d.utility.DoubleVector(radii))
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_unreferenced_vertices()
    V = np.asarray(mesh.vertices, np.float32)
    F = np.asarray(mesh.triangles, np.int32)
    info.update(triangles=int(len(F)), vertices=int(len(V)), seconds=round(time.time() - t0, 1))
    say(1.0, f'сетка: {len(F):,} треугольников'.replace(',', ' '))
    return {'V': V, 'F': F, 'info': info}


def _poisson_child(inp, out):
    """Выполняется в отдельном процессе: Пуассон по точкам из inp → out (npz)."""
    import open3d as o3d
    z = np.load(inp)
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(z['P']))
    pcd.normals = o3d.utility.Vector3dVector(z['N'])
    mesh, dens = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
        pcd, depth=int(z['depth']), scale=float(z['scale']), n_threads=int(z['threads']))
    np.savez(out, V=np.asarray(mesh.vertices), F=np.asarray(mesh.triangles), D=np.asarray(dens))


def _poisson_safe(P, N, depth, say=None, timeout=1800):
    """
    Пуассон в отдельном процессе (см. заголовок модуля): один поток; при неудаче —
    другой масштаб, затем глубина на единицу меньше. → (V, F, плотности, глубина).
    """
    attempts = [(depth, 1.1), (depth, 1.3), (depth - 1, 1.1), (depth - 2, 1.2)]
    here = str(Path(__file__).resolve().parent)
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, 'in.npz'), os.path.join(d, 'out.npz')
        for k, (dep, scale) in enumerate(attempts):
            if dep < 6:
                break
            np.savez(inp, P=P, N=N, depth=dep, scale=scale, threads=1)
            if k and say:
                say(f'Пуассон: повтор (глубина {dep}, масштаб {scale})…')
            code = f'import sys; sys.path.insert(0, {here!r}); import surface; surface._poisson_child({inp!r}, {out!r})'
            subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=timeout)
            if os.path.exists(out):
                z = np.load(out)
                return z['V'], z['F'], z['D'], dep
    raise RuntimeError('Пуассон не построил поверхность (сбой PoissonRecon) — '
                       'попробуйте другую точность или ball pivoting')


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
