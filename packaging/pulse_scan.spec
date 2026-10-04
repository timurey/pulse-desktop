# -*- mode: python ; coding: utf-8 -*-
"""
Сборка Pulse Scan (PyInstaller, onedir) для macOS и Windows.

    .venv312/bin/pyinstaller packaging/pulse_scan.spec --noconfirm      # из папки desktop/

Результат: dist/Pulse Scan.app (macOS) или dist/Pulse Scan/Pulse Scan.exe (Windows).
Собирать на той ОС, для которой сборка (кросс-сборки у PyInstaller нет); для обеих ОС —
.github/workflows/release.yml.
"""
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from pulse_qt.version import __version__        # noqa: E402

IS_MAC = sys.platform == 'darwin'

datas = [
    (str(ROOT / 'pulse_qt' / 'fonts'), 'pulse_qt/fonts'),
    (str(ROOT / 'docs' / 'manual'), 'docs/manual'),
    (str(ROOT / 'packaging' / 'icon.png'), 'packaging'),
]
datas += collect_data_files('qtawesome')
datas += collect_data_files('open3d', excludes=['**/*.pdb'])
binaries = collect_dynamic_libs('open3d')

hiddenimports = (
    collect_submodules('vtkmodules', filter=lambda m: not m.startswith('vtkmodules.web')
                       and 'Web' not in m and 'test' not in m.lower())
    + collect_submodules('mcap_ros2') + collect_submodules('mcap')
    + ['scipy.spatial', 'scipy.spatial.transform', 'pye57', 'laspy', 'zstandard']
    # модули логики подгружаются лениво (import внутри функций)
    + ['planes', 'plane_register', 'openings', 'reflections', 'manual_clean', 'scan_tree', 'project_store',
       'scan_project', 'scan_session', 'bag_reconstruct', 'offline_deskew', 'scanner_client', 'quality',
       'dynamic', 'surface', 'measure', 'section', 'calibration', 'view_cube', 'world_map']
)

# неиспользуемые части Qt, VTK и прочего — меньше размер
excludes = [
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick', 'PySide6.QtWebView',
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DExtras', 'PySide6.Qt3DInput', 'PySide6.Qt3DLogic',
    'PySide6.Qt3DAnimation', 'PySide6.QtQuick3D', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
    'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
    'PySide6.QtGraphs', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtBluetooth', 'PySide6.QtNfc',
    'PySide6.QtPositioning', 'PySide6.QtLocation', 'PySide6.QtSensors', 'PySide6.QtSerialPort',
    'PySide6.QtRemoteObjects', 'PySide6.QtScxml', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtDesigner',
    'PySide6.QtHelp', 'PySide6.QtSpatialAudio', 'PySide6.QtTextToSpeech', 'PySide6.QtHttpServer',
    'tkinter', 'matplotlib', 'IPython', 'jupyter', 'notebook', 'pandas', 'sklearn', 'torch', 'tensorflow',
    'open3d.ml', 'pytest',
]

a = Analysis(
    [str(ROOT / 'scan_qt.py')],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='Pulse Scan',
    console=False,
    icon=str(ROOT / 'packaging' / ('PulseScan.icns' if IS_MAC else 'PulseScan.ico')),
    target_arch=None,
    codesign_identity=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Pulse Scan')
if IS_MAC:
    app = BUNDLE(
        coll,
        name='Pulse Scan.app',
        icon=str(ROOT / 'packaging' / 'PulseScan.icns'),
        bundle_identifier='ru.pulse.scan',
        version=__version__,
        info_plist={
            'CFBundleShortVersionString': __version__.split('-')[0],
            'CFBundleVersion': __version__,
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '12.0',
            'NSHumanReadableCopyright': 'Pulse',
            'CFBundleDocumentTypes': [{'CFBundleTypeName': 'Проект Pulse Scan', 'CFBundleTypeExtensions': ['pulse'],
                                       'CFBundleTypeRole': 'Editor'}],
        },
    )
