#!/usr/bin/env python3
"""
Ручная чистка облака выделением прямоугольником на экране.

Прямоугольник в окне задаёт «пирамиду взгляда» (у перспективной камеры) или
призму (у ортографической) — четыре боковые плоскости через лучи из углов
прямоугольника, бесконечную по глубине. Удалённая область хранится как набор
таких плоскостей в канонической системе скана: она применяется к облаку любой
плотности (показ, стыковка, экспорт полного разрешения) и не зависит от того,
где скан стоит в проекте.
"""

import numpy as np


def project(P, view, proj, W, H):
    """Точки (N×3, мир) → экранные координаты (N×2) и признак «перед камерой»."""
    Ph = np.column_stack([P, np.ones(len(P))])
    clip = Ph @ (proj @ view).T
    w = clip[:, 3]
    front = w > 1e-9
    ndc = clip[:, :2] / np.where(front, w, 1.0)[:, None]
    sx = (ndc[:, 0] + 1) * 0.5 * W
    sy = (1 - ndc[:, 1]) * 0.5 * H
    return np.column_stack([sx, sy]), front


def in_rect(P, view, proj, W, H, rect):
    """Маска точек, попавших в прямоугольник rect = (x0, y0, x1, y1) в пикселях виджета."""
    s, front = project(P, view, proj, W, H)
    x0, x1 = sorted((rect[0], rect[2]))
    y0, y1 = sorted((rect[1], rect[3]))
    return front & (s[:, 0] >= x0) & (s[:, 0] <= x1) & (s[:, 1] >= y0) & (s[:, 1] <= y1)


def frustum_planes(near, far):
    """
    Боковые плоскости пирамиды по 4 углам прямоугольника на ближней (near) и
    дальней (far) глубине (оба 4×3, углы по кругу). → (4×4) [nx, ny, nz, d],
    внутри: n·p ≤ d.
    """
    near, far = np.asarray(near, float), np.asarray(far, float)
    inside = 0.5 * (near.mean(0) + far.mean(0))
    planes = []
    for i in range(4):
        a, b = near[i], near[(i + 1) % 4]
        c = far[i]
        n = np.cross(b - a, c - a)
        n /= max(np.linalg.norm(n), 1e-12)
        d = float(n @ a)
        if n @ inside > d:                      # нормаль наружу
            n, d = -n, -d
        planes.append([*n, d])
    return np.array(planes)


def planes_to_local(planes, T):
    """Плоскости из общей системы в систему скана, где p_common = T · p_local."""
    R, t = np.asarray(T)[:3, :3], np.asarray(T)[:3, 3]
    out = []
    for nx, ny, nz, d in planes:
        n = np.array([nx, ny, nz])
        out.append([*(R.T @ n), d - n @ t])
    return np.array(out)


def inside_regions(P, regions):
    """Маска точек P (канон. система скана), попавших хотя бы в одну область."""
    m = np.zeros(len(P), bool)
    for reg in regions:
        reg = np.asarray(reg, float)
        inside = np.ones(len(P), bool)
        for nx, ny, nz, d in reg:
            inside &= (P @ np.array([nx, ny, nz])) <= d
        m |= inside
    return m
