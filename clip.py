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


# ребро → локальная ось, вдоль которой оно бежит (индексы вершин из box_wire:
# idx = 4*i_sx + 2*i_sy + i_sz; рёбра 0..3 — низ, 4..7 — верх, 8..11 — стойки)
EDGE_AXIS = {0: 2, 1: 1, 2: 2, 3: 1, 4: 2, 5: 1, 6: 2, 7: 1,
             8: 0, 9: 0, 10: 0, 11: 0}


def edge_axis_world(box, edge_idx):
    """Мировое направление ребра edge_idx (единичное)."""
    a = np.zeros(3)
    a[EDGE_AXIS[edge_idx]] = 1.0
    yaw = np.radians(float(box.get('yaw_deg', 0.0)))
    R = np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1]])
    return R @ a


def box_handles(box, seg=8):
    """
    Органы управления на рёбрах (стрелки-ручки): на середине каждого ребра —
    двухсторонняя стрелка вдоль оси растяжения (размер по этой оси меняется
    при тяге). → (точки, сегменты) для set_lines; направление ребра — мир.
    """
    c = np.asarray(box['center'], float)
    d = np.abs(np.asarray(box['size'], float)) / 2.0
    yaw = np.radians(float(box.get('yaw_deg', 0.0)))
    R = np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1]])
    V, E = box_wire(c, np.abs(np.asarray(box['size'], float)), float(box.get('yaw_deg', 0.0)))
    pts, segs = [], []
    for k, (i0, i1) in enumerate(E):
        axis = EDGE_AXIS[k]
        m = 0.5 * (V[i0] + V[i1])
        a = np.zeros(3); a[axis] = 1.0
        a = R @ a
        hs = 0.3 * (2 * d[axis])                       # полудлина стрелки
        head = 0.25 * hs
        pts.extend([m - hs * a, m + hs * a])
        segs.append([len(pts) - 2, len(pts) - 1])
        for s_side in (-1, 1):                         # наконечники-«ласточки»
            tip = m + s_side * hs * a
            for sign in (-1, 1):
                perp = np.roll(a, 1) * sign            # любая перпендикулярная
                q = tip - head * (s_side * a) + head * 0.5 * perp
                pts.extend([tip, q])
                segs.append([len(pts) - 2, len(pts) - 1])
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
