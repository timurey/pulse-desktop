#!/usr/bin/env python3
"""
Pulse Scan — новое окно стыковки сканов (Qt + VTK). Логика та же, что у
scan_gui.py (scan_session.py); старое окно на Open3D остаётся рабочим.

    python scan_qt.py [X.pulse | X.project.json | scan1.e57 scan2.e57 ...]
"""

import multiprocessing

if __name__ == '__main__':
    multiprocessing.freeze_support()          # дочерние процессы (расчёт сетки) в собранном приложении
    from pulse_qt.app import main
    main()
