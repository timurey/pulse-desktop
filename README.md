# StaticScan

https://github.com/user-attachments/assets/401fc6c8-332f-4903-893c-5cb5063d45b4

Offline point cloud reconstruction pipeline for a rotating-platform LiDAR (Velodyne VLP-16) using ROS 2 MCAP bag files. No ROS installation required.

The pipeline has three stages:

| Script | Purpose | Output |
|---|---|---|
| `offline_deskew.py` | Motion-deskew each scan frame using platform encoder angles | `.npz` |
| `icp_merge.py` | ICP-align angular slices into a single registered map | `.pcd` |
| `flythrough.py` | First-person OpenGL viewer for the resulting point cloud | — |

---



## Pulse Scan — от bag до готового облака в одной программе (macOS / Windows / Linux)

**Запуск:** `run_gui.command` (macOS, двойной щелчок), `run_gui.bat` (Windows) или `./run_gui.sh` (Linux). При первом запуске создаётся окружение и ставятся зависимости; нужен Python 3.10–3.12.

**Порядок работы:**
1. «Проект» → **«Новый проект»**, затем один из вариантов:
   - **«Импорт со сканера...»** — прямо со сканера по сети. Адрес по умолчанию `pulse.local` (mDNS, веб-HMI на порту 80); если имя не разрешается, укажите IP. Выберите день съёмки, отметьте записи, они скачаются (с докачкой; Esc — остановить) в `bags/` рядом с проектом или в `~/Pulse/bags`;
   - **«Импорт bag...»** — папка bag'а или папка с bag'ами на диске (например, `bags/` после `make sync-bags`).
2. В диалоге: поправка наклона оси (по умолчанию «по полу и стенам»), воксель, максимальная дальность, папка для сканов (`scans/` рядом с проектом), группа в дереве. Bag'и реконструируются в фоне (все кадры, deskew по энкодеру, ~5 с на bag), каждый скан сразу появляется в дереве.
3. Сканы анализируются в фоне (плоскости, проёмы, отражения), затем запускается **автостыковка**.
4. То, что не встало автоматически (фасады): вкладка «Ручная».
5. Выравнивание («Проект»), чистка («Чистка»), **«Сохранить как...»** → `.pulse`.
6. **Экспорт** склейки или ветки (E57 / PLY / PCD).

То же из командной строки: `python scanner_client.py list` / `get NAME --dest bags/`, `python bag_reconstruct.py bags/ --out-dir scans/` → `python scan_project.py build scans/*.ply -o X.pulse`.

### Новое окно (Qt + VTK, в разработке — ветка `feature/qt-ui`)

**Запуск:** `run_qt.command` (macOS), `run_qt.bat` (Windows), `./run_qt.sh` (Linux) или `python scan_qt.py [X.pulse]`. Окружение то же (`.venv312`), при первом запуске доставляются PySide6, VTK и qtawesome (`requirements-qt.txt`). Старое окно (`run_gui.*`, `scan_gui.py`) работает как прежде.

Те же функции, что в старом окне, в раскладке макета: панель инструментов («Со сканера», «Bag», «Автостыковка», «Ручная», вид сверху / 3D / полёт, «Чистка», «Экспорт»); слева дерево проекта (глаз — видимость, перетаскивание — в группу, правая кнопка — действия) и слои (поверхности, проёмы, отражения, сетка); в центре 3D-вид с кубом навигации и нижняя панель (пары, циклы, кандидаты, журнал); справа инспектор выбранного скана или группы, панель ручной стыковки или чистки. Тёмная и светлая тема — кнопка справа вверху.

Дополнительно:
- **слой «Качество совмещения»** — подсветка мест, где одна поверхность из разных сканов толще порога (ползунок; способы «по плоскостям» и «локально»), список мест на вкладке «Качество»;
- **«Чистка» → «Движущиеся объекты»** — найти точки людей, машин, открытых дверей (между сканами и по полуоборотам платформы; второе — для сканов, импортированных из bag) и удалить их (Ctrl+Z);
- **«Поверхность β»** — экспериментальная сетка (Пуассон или ball pivoting, точность 1–20 см) для скана или всех видимых сканов, экспорт OBJ / PLY / STL;
- **проекция:** перспектива или ортогональная (O); размер точек — ползунком.
- **Контроль → сечения:** «Срез» (видна полоса облака у плоскости, например план на отметке +1.200) и «Отсечение» (скрыть всё выше 2,5 м); основа — горизонтально, по X, по Y или по стене / полу, доворот транспортирами и полями углов;
- **Контроль → замеры и нулевой уровень:** расстояние, отметка, до плоскости, ломаная / угол / площадь; нулевой уровень — пол опорного скана, кликом или числом; замеры и сечение сохраняются в проекте;
- **уточнение стыковки:** «Автоподгонка (ICP)» в ручной стыковке после грубого совмещения (показывает расхождение поверхностей до и после) или «Уточнить стыковку» в меню скана; **опорная точка** — совместить одну характерную точку, закрепить и поворачивать скан вокруг неё транспортирами (тянуть кольца мышью), полями углов или «Подогнать поворот».

Мышь в 3D-виде: левая — вращение вокруг вертикали, правая / средняя / Shift + левая — сдвиг, колесо — масштаб к курсору, двойной клик — центр вращения. Клавиши: T — вид сверху, F — полёт (WASD), R — выделение, Delete — удалить выделенное, Ctrl+Z — отменить, Esc — отменить текущее, F12 — скриншот.

## Интерактивная стыковка статических сканов (macOS / Windows / Linux)

`scan_gui.py` — окно для стыковки статических сканов: автоматическая стыковка пар, ручная правка (выбор плоскостей, проёмов, точек мышью; сдвиг и поворот перетаскиванием), кандидаты для трудных сканов (фасады), удаление зеркальных отражений стёкол, экспорт склейки. Подробности — `docs/plan_plane_registration.md`.

### Установка

Нужен **Python 3.10–3.12**: для 3.13+ нет колёс Open3D 0.19.0. Open3D закреплён на **0.19.0**, потому что в 0.20 нативное окно чёрное на macOS 15.

```bash
# macOS / Linux
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Windows (PowerShell)
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

**Linux**: нужны OpenGL-библиотеки и шрифт с кириллицей. Debian/Ubuntu: `sudo apt install libgl1 libgomp1 fonts-dejavu-core`.

### Запуск

```bash
.venv/bin/python scan_gui.py static_20260922.pulse           # Windows: .venv\Scripts\python ...
.venv/bin/python scan_gui.py scan1.e57 scan2.e57 scan3.e57      # новый проект из сканов
```

- **Шрифт.** Шрифт с кириллицей ищется автоматически: Arial на macOS, Segoe UI или Arial на Windows, DejaVu или Noto на Linux. Свой шрифт задаётся так: `PULSE_GUI_FONT=/путь/к/шрифту.ttf`.
- **Чёрное окно.** Если окно всё равно чёрное, есть вариант `--web`: окно открывается в браузере по адресу http://localhost:8888.
- **Скриншот.** F12 сохраняет в `screenshots/` рендер сцены, текстовое состояние окна и снимок окна. Снимок окна работает на macOS, если у терминала есть разрешение «Запись экрана»; на Linux, если установлен ImageMagick; на Windows, если установлен Pillow.
- **Файл проекта `.pulse`** — zip-архив: внутри `project.json` (позы, рёбра, дерево, чистка, горизонт) и кеш анализа сканов (плоскости, проёмы, маска отражений). Сами сканы в архив не входят. Кеш привязан к размеру и дате файла скана и к версии алгоритмов: если скан изменился, анализ пересчитывается. Старые `.json` открываются, но кеша в них нет; «Сохранить как...» `.pulse` переводит проект в новый формат.
- **Открытие проекта.** Сканы появляются сразу по сохранённым позам: из кеша — очищенными, без кеша — сырыми, пока анализ идёт в фоне. По окончании анализ дописывается в архив. На `static_20260922`: с кешем 0.2 с, без кеша сканы видны через 5 с, а фоновый анализ занимает около 40 с.
- **Пути в проекте.** Пути к сканам хранятся относительно файла проекта, поэтому папку со сканами и проектом можно переносить между машинами и ОС.

| Действие | Как |
|---|---|
| выбрать признак (плоскость / проём / точку) | Ctrl + клик (на macOS также Cmd + клик): сначала в неподвижном скане, затем в подвижном |
| сдвинуть подвижный скан | Shift + перетаскивание |
| повернуть вокруг вертикали | Alt (Option) + перетаскивание или Shift + правая кнопка |
| повернуть подвижный скан по всем осям | вкладка «Ручная»: yaw±, крен± (вокруг X), тангаж± (вокруг Y) с заданным шагом; вращение вокруг точки сканера |
| убрать наклон подвижного скана | «Выровнять подвижный по горизонту»: пол и земля становятся горизонтальными, стены вертикальными |
| убрать наклон всего проекта | вкладка «Проект»: «Выровнять проект по горизонту» (сохраняется в проекте, «Сбросить» отменяет) |
| решение с наклоном | галочка «Полный поворот при решении»: пары, задающие наклон (пол + две стены), дают поворот по всем осям |
| куб навигации (правый верхний угол сцены) | клик по грани, ребру или углу — плавный поворот вида с этой стороны; перетаскивание куба — вращение вида; оси X (красная), Y (зелёная), Z (синяя) |
| ручная чистка (артефакты, остатки отражений) | вкладка «Чистка» → «Выделение прямоугольником» (R): левая кнопка — прямоугольник, Shift — добавить к выделению; Delete — удалить, Ctrl+Z — отменить. Удаляется всё в прямоугольнике на всю глубину взгляда, поэтому удобно чистить в виде сверху или сбоку (куб навигации). Чистка хранится в проекте как области у скана и применяется к экспорту и стыковке |
| выровнять план в виде сверху | вкладка «Проект»: «Выровнять план по стенам» — стены вдоль осей X/Y; «Поворот плана < >» — тонкая подгонка с заданным шагом |
| точный угол подвижного скана | «Ручная»: «Довернуть подвижный по стенам» — автоматически; шаг поворота 0.1 / 1 / 5 град |
| Esc | выйти из полёта, снять выделение, выйти из режима выделения, отменить незаконченную пару (окно не закрывает) |
| закрытие окна | с подтверждением; при несохранённых изменениях — «Сохранить и выйти» / «Выйти без сохранения» / «Отмена» |
| дерево проекта (здание / этаж / комната ...) | вкладка «Проект»: галочка у группы или скана — видимость (у группы — всей ветки); кнопки «Новая группа», «Переименовать», «В группу...», «Удалить группу», «Сделать опорным», «Только эта ветка», «Показать всё», «Экспорт ветки...» |
| автостыковка по дереву | вкладка «Пары»: «По дереву» — пары внутри групп и между соседними ветками вместо всех со всеми (на 10 сканах: 24 пары вместо 45) |
| показать найденные объекты | вкладка «Проект»: «Поверхности» — контуры плоскостей (пол — зелёный, потолок — синий, стены — оранжевый), «Проёмы» — рамки окон (пурпурный) и дверей (голубой). Объекты из отражений и вручную удалённых областей не показываются |
| режим полёта (как в 3D-шутере) | F — вкл/выкл, Esc — выход; кнопка «Встать в точку скана» ставит камеру на место сканера |
| полёт: ходьба | W/S — вперёд/назад, A/D — вбок, Space/E — вверх, C/Q — вниз, Shift — быстрее ×4 |
| полёт: обзор и скорость | левая кнопка + мышь — обзор, колесо — скорость (0.1–50 м/с) |

## Requirements

**Python 3.11 recommended** — Open3D does not support Python 3.13+.

### Install dependencies

```bash
pip install -r requirements.txt
```

Or manually:

```bash
pip install numpy mcap mcap-ros2-support zstandard open3d glfw PyOpenGL PyOpenGL_accelerate
```

| Package | Used by | Purpose |
|---|---|---|
| `numpy` | all | Array maths |
| `mcap` | `offline_deskew`, `icp_merge` | MCAP bag reading |
| `mcap-ros2-support` | `offline_deskew`, `icp_merge` | ROS 2 message decoding |
| `zstandard` | `offline_deskew`, `icp_merge` | Decompress `.mcap.zstd` bags |
| `open3d` | `icp_merge`, `flythrough` | ICP registration, PCD I/O |
| `glfw` | `flythrough` | OpenGL window and input |
| `PyOpenGL` | `flythrough` | OpenGL bindings |
| `PyOpenGL_accelerate` | `flythrough` | Optional C accelerators for PyOpenGL |

> **Note:** `PyOpenGL_accelerate` is optional but recommended for performance.

### Virtual environment (recommended)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux / macOS
pip install -r requirements.txt
```

---

## Expected bag topics

| ROS 2 topic | Message type | Description |
|---|---|---|
| `/velodyne_points` | `sensor_msgs/PointCloud2` | VLP-16 scan frames |
| `/rotating_platform/angle` | `std_msgs/Float64` | Platform encoder angle in degrees |

Compressed `.mcap.zstd` bags are automatically decompressed in-place on first use.

---

## Usage

### Quick start

```bash
# Reconstruct with ICP
python icp_merge.py /data/my_scan/

# View the result
python flythrough.py icp_merged.pcd
```

---

### 1. Deskew — `offline_deskew.py`

Corrects motion blur in each scan frame by interpolating the platform rotation angle at each point's acquisition time. Can be used standalone or is called automatically by `icp_merge.py`.

```
python offline_deskew.py <bag_path> [output.npz]
```

| Argument | Description |
|---|---|
| `bag_path` | Path to a ROS 2 bag directory or a `.mcap` / `.mcap.zstd` file |
| `output.npz` | Output file (default: `deskewed.npz`) |

**Examples**

```bash
# From a bag directory
python offline_deskew.py /data/my_scan/

# From a compressed MCAP file with a custom output name
python offline_deskew.py /data/my_scan/scan.mcap.zstd corrected.npz
```

The output `.npz` contains:

| Key | Description |
|---|---|
| `points` | Array of deskewed XYZ point arrays (one per scan frame) |
| `timestamps` | Nanosecond timestamps per frame |
| `angle_times` | Encoder sample timestamps |
| `angle_values` | Encoder angles in radians |

**Calibration parameters** — edit at the top of `offline_deskew.py`:

| Parameter | Default | Description |
|---|---|---|
| `ROTATION_AXIS` | `'x'` | Axis the platform rotates around (`'x'`, `'y'`, or `'z'`) |
| `ANGLE_OFFSET_DEG` | `184.0` | Zero-angle calibration offset in degrees |
| `ROTATION_CENTER` | `[0,0,0]` | Offset of LiDAR optical centre from rotation axis |
| `INVERT_ROTATION` | `False` | Flip rotation direction |
| `ENCODER_TIME_OFFSET_MS` | `0.0` | Time offset between encoder and LiDAR clocks |
| `MOUNT_RPY_DEG` | `[0,0,0]` | LiDAR mount roll / pitch / yaw in degrees |
| `MOUNT_AXES` | `['+x','+y','+z']` | Axis remapping for the LiDAR mount |

---

### 2. ICP Merge — `icp_merge.py`

Splits the full scan into overlapping angular slices, deskews each frame, then uses ICP to align all slices into a single registered point cloud. Uses a two-pass strategy (chain → refine → re-chain) to minimise drift.

ICP alignment is applied on top of the deskewing step to compensate for mechanical imperfections — shaft runout, bearing wobble, or other hardware tolerances that cause opposite 180° scan halves to not align perfectly even after motion correction. The angular-slice approach gives ICP overlapping geometry between adjacent slices, making registration robust where pure deskewing falls short.

```
python icp_merge.py <bag_path> [options]
```

| Argument / Flag | Default | Description |
|---|---|---|
| `bag_path` | — | Path to bag directory or `.mcap` file |
| `--slices N` | `12` | Number of angular slices |
| `--overlap F` | `2.0` | Slice window width as a multiple of the step width |
| `--fine` | off | Use finer voxel size (0.05 m) for ICP |
| `--min-fitness F` | `0.3` | Minimum ICP fitness score to accept a slice |
| `--no-chain` | off | Align every slice directly to the reference (no chaining) |
| `--map-voxel F` | `0.01` | Final output voxel downsample size in metres; `0` to disable |
| `--trim-end N` | `0` | Trim the last N seconds from the bag |
| `-o FILE` | `icp_merged.pcd` | Output PCD file path |

**Examples**

```bash
# Basic run with defaults
python icp_merge.py /data/my_scan/

# 16 slices, fine ICP, custom output
python icp_merge.py /data/my_scan/ --slices 16 --fine -o result.pcd

# Trim last 3 seconds (e.g. platform spin-down)
python icp_merge.py /data/my_scan/ --trim-end 3

# Disable final downsample to keep all points
python icp_merge.py /data/my_scan/ --map-voxel 0

# More overlap for difficult geometry
python icp_merge.py /data/my_scan/ --slices 12 --overlap 3.0
```

The pipeline prints four stages of progress:

1. Read bag — decoder encoder and point cloud messages
2. Deskew all scan frames
3. Build overlapping slices and ICP-align (chain → refine → re-chain)
4. Merge core points with aligned transforms and write PCD

---

### 3. Viewer — `flythrough.py`

First-person OpenGL viewer for `.pcd` or `.npz` point cloud files.

```
python flythrough.py <file1.pcd> [file2.pcd ...]
```

Pass a single file for normal viewing, or multiple files to enter **compare mode** — each cloud is rendered in a distinct colour (blue, orange, green, magenta) and the full stitching / alignment toolset becomes available.

**Example — compare and stitch two scans**

```bash
python flythrough.py scan_a.pcd scan_b.pcd
```

---

**Navigation controls**

| Key / Mouse | Action |
|---|---|
| W / S | Move forward / backward |
| A / D | Strafe left / right |
| Space / LCtrl | Move up / down |
| Left-click drag | Look around |
| Mid / Right-click drag | Pan |
| Scroll | Adjust move speed |
| + / - | Point size |
| C | Cycle color mode |
| E | Toggle Eye-Dome Lighting (EDL) |
| L / Shift+L | EDL strength up / down |
| R | Reset camera to centre |
| P | Print camera position |
| 1–5 | Speed presets |
| X | Statistical outlier filter |
| F12 | Save / overwrite original PCD |
| F5 / F6 | Rotate scene ±90° around X |
| F7 / F8 | Rotate scene ±90° around Y |
| F9 / F10 | Rotate scene ±90° around Z |
| F1 | Reset scene rotation |
| Esc | Quit |

---

**Compare mode — cloud visibility** *(2+ files)*

| Key | Action |
|---|---|
| F2 | Toggle cloud 1 visibility |
| F3 | Toggle cloud 2 visibility |
| F4 | Toggle cloud 3 visibility |

---

**Grab mode — interactive alignment** *(2+ files, Tab to enter/exit)*

Grab mode lets you manually position cloud 2 against cloud 1 before running ICP.

| Key / Mouse | Action |
|---|---|
| Tab | Toggle grab mode on / off (Esc to cancel and revert) |
| Left-click drag | Translate cloud 2 in the view plane |
| Shift + left-click drag | Rotate cloud 2 around its centroid |
| Scroll | Push / pull cloud 2 along camera forward axis |
| I | Run ICP to refine alignment (then exits grab mode) |

---

**Pick mode — correspondence-based alignment** *(2+ files)*

Pick at least 3 matching point pairs from each cloud. A Kabsch SVD initial transform is computed from the picks, then refined with ICP.

| Key / Mouse | Action |
|---|---|
| T | Toggle pick mode on / off |
| Left-click | Pick nearest point (alternates between cloud 1 and cloud 2) |
| U | Undo last pick |
| G | Compute alignment from current pairs (needs 3+) |
| M | Merge all clouds and save (prompts for filename) |

---

## Typical workflow

```bash
# 1. Reconstruct with ICP (deskewing is done automatically)
python icp_merge.py /data/my_scan/ --slices 12 -o map.pcd

# 2. View the result
python flythrough.py map.pcd

# 3. Optionally run deskew standalone for inspection
python offline_deskew.py /data/my_scan/ deskewed.npz
python flythrough.py deskewed.npz
```

---

## Platform notes

- Tested on **Windows 11** with Python 3.11.
- Should work on Linux and macOS. On Linux ensure system OpenGL and GLFW libraries are available (`libglfw3-dev`, `libgl1-mesa-dev`).
- Compressed `.mcap.zstd` bags are decompressed in-place beside the original file on first use.
