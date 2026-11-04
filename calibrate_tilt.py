#!/usr/bin/env python3
"""
Автоматична калібровка кута нахилу осі обертання.

Запускати один раз після заміни кронштейна лідара.
Скрипт будує хмару з bag-файлу, знаходить горизонтальні/вертикальні
площини і зберігає матрицю корекції в calib.json поряд із собою.
Всі наступні запуски world_map.py та bag_reconstruct.py
підхоплять її автоматично.

Usage:
    python calibrate_tilt.py <bag_dir_or_mcap>
"""

import sys
import json
import datetime
import numpy as np
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: python calibrate_tilt.py <bag_dir_or_mcap>")
        sys.exit(1)

    bag = sys.argv[1]
    out_path = Path(__file__).parent / 'calib.json'

    print(f"Читаем bag: {bag}")
    from offline_deskew import read_bag, parse_pointcloud2
    from bag_reconstruct import process_frames, level_by_geometry

    angle_times, angle_values, clouds = read_bag(bag)
    if len(angle_times) < 2:
        print("ERROR: нет данных угла платформы в bag")
        sys.exit(1)

    frames = [(stamp, parse_pointcloud2(msg)) for stamp, msg in clouds]
    print(f"Фреймов: {len(frames)}. Строим облако без коррекции наклона...")

    P, stats = process_frames(
        frames, angle_times, angle_values,
        R_tilt=None, voxel=0.05,
    )
    print(f"Точек: {len(P)}")

    print("Ищем горизонтальные/вертикальные плоскости...")
    R, info = level_by_geometry(P)

    if not info.get('applied', False):
        print(f"\nНЕ УДАЛОСЬ определить наклон автоматически.")
        print(f"  Найдено горизонт.: {info.get('horizontal',0)}, стен: {info.get('walls',0)}")
        print(f"  Угол: {info.get('tilt_deg',0):.2f}°")
        print("  Нужно хотя бы 3 детектированных плоскости (пол + потолок + стена).")
        print("  Попробуйте отсканировать комнату с хорошо видимым полом и стенами.")
        sys.exit(1)

    tilt_deg = info['tilt_deg']
    print(f"\nНаклон оси: {tilt_deg:.3f}°")
    print(f"  Горизонтальных плоскостей: {info.get('horizontal', '?')}")
    print(f"  Вертикальных (стены):      {info.get('walls', '?')}")

    calib = {
        'tilt_deg': round(tilt_deg, 4),
        'R': R.tolist(),
        'source': 'geometry',
        'bag': str(Path(bag).name),
        'date': str(datetime.date.today()),
        'info': {k: v for k, v in info.items() if k != 'applied'},
    }
    out_path.write_text(json.dumps(calib, indent=2, ensure_ascii=False))
    print(f"\nСохранено: {out_path}")
    print("Теперь world_map.py и bag_reconstruct.py будут использовать эту калибровку.")

    # Quick sanity: apply R to cloud and measure residual flatness of floor
    try:
        import planes
        R_up = planes.up_rotation('+x')
        canon_raw  = np.asarray(P, np.float64) @ R_up.T
        canon_corr = np.asarray(P, np.float64) @ (R.T) @ R_up.T
        z_raw  = canon_raw[:, 2]
        z_corr = canon_corr[:, 2]
        floor_raw  = np.percentile(z_raw,  2)
        floor_corr = np.percentile(z_corr, 2)
        ceil_raw   = np.percentile(z_raw,  98)
        ceil_corr  = np.percentile(z_corr, 98)
        spread_raw  = np.std(z_raw[(z_raw  < floor_raw  + 0.15)])
        spread_corr = np.std(z_corr[(z_corr < floor_corr + 0.15)])
        print(f"\nПроверка плоскости пола:")
        print(f"  До  калибровки  σ={spread_raw*1000:.1f} мм")
        print(f"  После калибровки σ={spread_corr*1000:.1f} мм")
    except Exception:
        pass


if __name__ == '__main__':
    main()
