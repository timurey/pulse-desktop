#!/usr/bin/env python3
"""
Экспорт замеров (Ф6/Ф7, механика «Оттиска»):

- CSV — строка на каждую вершину каждого замера, ';' + utf-8-sig (Excel);
- DXF — 3D-замеры в метрах ($INSUNITS=6), слои IZM_* с ACI-цветами Оттиска
  (совместимость: их файлы открываются у нас и наоборот).

Координаты — общая система (мировые метры); СК появится в Ф5 и добавит базис.
"""

import csv

import numpy as np

import measure

# слои DXF (как у Оттиска) и их ACI-цвета
DXF_LAYERS = {'IZM_POINT': 2, 'IZM_DIST': 1, 'IZM_LINE': 3, 'IZM_AREA': 5, 'IZM_TEXT': 7}


def _value_units(m, z0):
    """(число, единицы) замера — для CSV и подписи в DXF."""
    v = measure.evaluate(m, z0)
    t = m['type']
    if not v:
        return None, ''
    if t == 'dist':
        return v['length'], 'м'
    if t == 'height':
        return v['h'], 'м'
    if t == 'plane':
        return v['distance'], 'м'
    if t == 'poly':
        if m.get('closed') and 'area' in v:
            return v['area'], 'м²'
        return v['length'], 'м'
    return None, ''


def _title(m, i):
    """Название замера для экспорта: имя пользователя или тип + номер."""
    if m.get('name'):
        return m['name']
    t = m['type']
    if t == 'poly' and m.get('closed'):
        return f'Площадь {i}'
    return f"{measure.TITLES.get(t, t)} {i}"


def _plan(m, session):
    """Базовая точка подписи (центр масс вершин)."""
    P = np.asarray(m['pts'], float)
    return P.mean(axis=0)


def csv_rows(session, z0=None):
    """Строки CSV по вершинам замеров: ID;Название;Тип;Вершина;X;Y;Z;Значение;Единицы;Примечание."""
    if z0 is None:
        z0 = session.zero_z() if session.scans else 0.0
    rows = []
    for i, m in enumerate(session.measures, 1):
        title = _title(m, i)
        t = m['type']
        if t == 'poly' and m.get('closed'):
            t = 'Площадь'
        else:
            t = __import__('measure').TITLES.get(m['type'], m['type'])
        value, units = _value_units(m, z0)
        single = len(m['pts']) == 1
        for k, p in enumerate(m['pts'], 1):
            rows.append([str(i), title, t, '' if single else f'P{k}',
                         f'{p[0]:.3f}', f'{p[1]:.3f}', f'{p[2]:.3f}',
                         '' if (not single and k > 1) else
                         ('' if value is None else f'{value:.3f}'),
                         units if (single or k == 1) else '',
                         m.get('note', '')])
    return rows


def write_csv(session, path):
    """Замеры → CSV (';', utf-8-sig — Excel открывает без настроек)."""
    from fileio import native_path
    rows = csv_rows(session)
    head = ['ID', 'Название', 'Тип', 'Вершина', 'X', 'Y', 'Z', 'Значение', 'Единицы', 'Примечание']
    with native_path(path, 'w') as p:
        with open(p, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f, delimiter=';', lineterminator='\r\n')
            w.writerow(head)
            w.writerows(rows)
    return path


def write_dxf(session, path, z0=None):
    """Замеры → 3D-DXF (метры): слои IZM_* как у Оттиска, подписи на IZM_TEXT."""
    import ezdxf
    from fileio import native_path
    if z0 is None:
        z0 = session.zero_z() if session.scans else 0.0
    doc = ezdxf.new('R2010', setup=False)
    doc.header['$INSUNITS'] = 6                          # метры
    doc.header['$MEASUREMENT'] = 1
    for name, aci in DXF_LAYERS.items():
        doc.layers.add(name, color=aci)
    msp = doc.modelspace()
    for i, m in enumerate(session.measures, 1):
        title = _title(m, i)
        value, units = _value_units(m, z0)
        P = [np.asarray(p, float) for p in m['pts']]
        t = m['type']
        if t == 'height':
            msp.add_point(P[0], dxfattribs={'layer': 'IZM_POINT'})
        elif t == 'dist' and len(P) == 2:
            msp.add_line(P[0], P[1], dxfattribs={'layer': 'IZM_DIST'})
        elif t == 'plane' and len(P) == 1 and 'plane' in m:
            import measure
            foot = measure.point_plane(P[0], m['plane']['n'], m['plane']['c'])['foot']
            msp.add_line(P[0], np.asarray(foot, float), dxfattribs={'layer': 'IZM_DIST'})
        elif t == 'poly' and len(P) >= 2:
            layer = 'IZM_AREA' if m.get('closed') else 'IZM_LINE'
            msp.add_lwpolyline([(float(p[0]), float(p[1]), float(p[2])) for p in P],
                               dxfattribs={'layer': layer, 'closed': bool(m.get('closed'))})
        else:
            continue
        if value is not None:
            at = _plan(m, session)
            text = f"{title} = {value:.3f} {units}" if m.get('name') else f"{value:.3f} {units}"
            msp.add_text(text, height=0.2,
                         dxfattribs={'layer': 'IZM_TEXT'}).set_placement((float(at[0]), float(at[1]), float(at[2])))
    with native_path(path, 'w') as p:
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            doc.write(f)
    return path
