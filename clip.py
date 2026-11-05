#!/usr/bin/env python3
"""
Боксы отсечения (Ф15, механика «Оттиска», улучшенная): активная область =
сечение (section.keep_mask) ∩ объединение включённых боксов. Неразрушающий
вид — точки не удаляются, в отличие от erase/drop.

Бокс осе-упорядоченный (yaw вокруг вертикали), в общей системе (как сечение).
Показываются точки внутри любого включённого бокса (объединение — работа с
несколькими комнатами сразу); включённое сечение AND-ится поверх (перекрытие
внутри комнаты).

Рендер: один включённый бокс — VTK clipping planes (мгновенно, 6+1–2 плоскости);
несколько — CPU-маска при перерисовке (объединение плоскостями не выразить).
"""

import numpy as np

DEFAULT_SIZE = (3.0, 3.0, 3.0)      # м — новый бокс по клику


def box_wire(center, size, yaw_deg=0.0):
    """12 рёбер бокса (8 вершин) — для каркаса в 3D-виде. → (точки, сегменты)."""
    c = np.asarray(center, float)
    d = np.asarray(size, float) / 2.0
    a = np.radians(yaw_deg)
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    V = [(sx * d[0], sy * d[1], sz * d[2])
         for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    V = np.array([c + R @ np.asarray(v, float) for v in V])
    # индексы: 0..3 — нижняя грань (sz=-1), 4..7 — верхняя
    edges = [[0, 1], [1, 3], [3, 2], [2, 0], [4, 5], [5, 7], [7, 6], [6, 4],
             [0, 4], [1, 5], [3, 7], [2, 6]]
    return V, np.asarray(edges, np.int64)


def box_faces(box):
    """Грани бокса: [(центр_грани, нормаль_наружу_в_мире, ось, знак)] × 6.
    Порядок как у box_planes: ось 0,1,2, внутри оси знак −1, +1."""
    c = np.asarray(box['center'], float)
    d = np.abs(np.asarray(box['size'], float)) / 2.0
    a = np.radians(float(box.get('yaw_deg', 0.0)))
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    out = []
    for k in range(3):
        for s in (-1, 1):
            n = R[:, k] * s                        # нормаль наружу (мир)
            out.append((c + n * d[k], n, k, s))
    return out


def face_handles(box, shaft=0.25, head=0.06):
    """
    Стрелки-ручки граней: по одной на грань, из центра грани наружу (тяга
    двигает только эту грань). Длина стержня — доля shaft от полугабарита
    грани (большей стороны), наконечник head — фиксированный (м).
    → (точки K×3, сегменты M×2) для set_lines. Порядок точек и сегментов —
    порядок граней box_faces: по 4 точки и 3 сегмента на грань (стержень +
    2 линии наконечника). Наконечник строится из стабильного перпендикуляра
    в плоскости грани — выглядит стрелкой с любой стороны.
    """
    size = np.abs(np.asarray(box['size'], float))
    yaw = np.radians(float(box.get('yaw_deg', 0.0)))
    R = np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1]])
    pts, segs = [], []
    for fc, n, k, _s in box_faces(box):
        L = shaft * 0.5 * max(size[i] for i in range(3) if i != k)
        tip = fc + n * L
        perp = np.cross(n, R[:, (k + 1) % 3])      # единичный, в плоскости грани
        b1 = tip - n * head + perp * (0.6 * head)  # барбы симметричны
        b2 = tip - n * head - perp * (0.6 * head)
        pts.extend([fc, tip, b1, b2])
        i = len(pts) - 4
        segs.extend([[i, i + 1], [i + 1, i + 2], [i + 1, i + 3]])
    return np.asarray(pts, float), np.asarray(segs, np.int64)


def box_planes(box):
    """Бокс → 6 плоскостей внутрь: [(n, c)] с n·x ≥ c внутри (для VTK clipping
    плоскость оставляет сторону, куда смотрит нормаль)."""
    c = np.asarray(box['center'], float)
    d = np.abs(np.asarray(box['size'], float)) / 2.0
    a = np.radians(float(box.get('yaw_deg', 0.0)))
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    out = []
    for k in range(3):
        for s in (-1, 1):
            out_dir = R[:, k] * s                 # наружу от центра
            out.append((c + out_dir * d[k], -out_dir))   # нормаль — внутрь
    return out


def _local(pts, box):
    """Точки в системе бокса (центр в нуле, оси вдоль размера)."""
    c = np.asarray(box['center'], float)
    a = np.radians(float(box.get('yaw_deg', 0.0)))
    R = np.array([[np.cos(a), np.sin(a), 0], [-np.sin(a), np.cos(a), 0], [0, 0, 1]])
    return (np.asarray(pts, float) - c) @ R.T


def inside_mask(pts, box):
    """Маска точек внутри бокса: |локальные координаты| ≤ размер/2 по всем осям."""
    q = _local(pts, box)
    d = np.abs(np.asarray(box['size'], float)) / 2.0
    return np.all(np.abs(q) <= d[None, :] + 1e-9, axis=1)


def active_boxes(session):
    """Включённые боксы (on=True), в порядке списка."""
    return [b for b in getattr(session, 'clip_boxes', []) if b.get('on')]


def combined_mask(pts, session, section_keep=None):
    """
    Итоговая маска видимости точек (общая система): сечение AND (объединение
    включённых боксов). section_keep — готовая маска сечения (можно передать,
    чтобы не считать дважды); боксов нет — маска сечения как есть.
    """
    pts = np.asarray(pts, float)
    keep = np.ones(len(pts), bool) if section_keep is None else np.asarray(section_keep, bool)
    boxes = active_boxes(session)
    if not boxes:
        return keep
    in_any = np.zeros(len(pts), bool)
    for b in boxes:
        in_any |= inside_mask(pts, b)
        if in_any.all():
            break
    return keep & in_any


def face_arrow(box, k, s, shaft=0.28, r=None):
    """3D-стрелка-ручка грани (ось k, знак s): (base, tip, radius).
    Длина — доля от полугабарита перпендикулярных осей; радиус — доля от длины
    (крупные ручки на крупных боксах; clamp 3..25 см от габарита сцены задаёт
    вызывающий через r)."""
    fc, n, kk, ss = next(f for f in box_faces(box) if f[2] == k and f[3] == s)
    size = np.abs(np.asarray(box['size'], float))
    L = shaft * max(size[i] for i in range(3) if i != kk)
    L = float(np.clip(L, 0.25, 1.5))
    radius = float(np.clip(0.40 * L, 0.10, 0.30)) if r is None else float(r)
    return fc, fc + n * L, radius
