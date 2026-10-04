# Сборка и выпуск Pulse Scan

Как получить готовое приложение для **macOS** и **Windows** из исходников, проверить его и выпустить релиз.

Pulse Scan собирается [PyInstaller](https://pyinstaller.org) в папку с программой (режим onedir): интерпретатор Python и все библиотеки лежат внутри, пользователю ничего ставить не нужно.

**Кросс-сборки нет:** macOS-версия собирается на macOS, Windows-версия — на Windows.

| Что | Где |
|---|---|
| Версия | `pulse_qt/version.py` (`__version__`, `RELEASE_DATE`) |
| Описание сборки PyInstaller | `packaging/pulse_scan.spec` |
| Значки | `packaging/PulseScan.icns` (macOS), `PulseScan.ico` (Windows), `icon.png` (окно) |
| DMG для macOS | `packaging/make_dmg.sh` |
| Сборка на Windows одной командой | `packaging/build_windows.bat` |
| Сборка обеих ОС на GitHub | `.github/workflows/release.yml` |
| Самопроверка собранного приложения | `pulse_qt/selftest.py` (`--selftest`) |
| Руководство пользователя | `docs/manual/README.md` + `docs/manual/img/` |
| Снимки для руководства | `tests/manual_shots.py` |
| Примечания к выпуску | `docs/RELEASE_NOTES.md` |

---

## 1. Требования

- **Python 3.10–3.12, x64** (на Mac — arm64). Open3D 0.19 не имеет колёс для 3.13+ и для Windows ARM.
- Зависимости: `requirements-qt.txt` (подключает `requirements.txt`) + `pyinstaller`; для тестов ещё `flask`.
- Место на диске: ~3 ГБ под окружение и сборку. Готовое приложение — ~0,9 ГБ, DMG / zip — ~0,3 ГБ.

| Платформа | Что получается | Ограничения |
|---|---|---|
| macOS (Apple Silicon) | `Pulse Scan.app` → DMG | только arm64; подпись ad-hoc (без сертификата Apple) |
| Windows 10 / 11 x64 | папка `Pulse Scan\` с `Pulse Scan.exe` → zip | без подписи; нужен OpenGL 3.2 |

---

## 2. Версия

Перед выпуском обновите:
1. `pulse_qt/version.py` — `__version__ = 'X.Y.Z'` или `'X.Y.Z-rcN'`, `RELEASE_DATE`;
2. `docs/RELEASE_NOTES.md` — что нового, ограничения, что проверить;
3. при изменениях интерфейса — руководство и снимки (раздел 7).

Версия видна в заголовке окна, в «О программе», в `"Pulse Scan" --version`, в имени файла сборки и в `Info.plist` на macOS. Тег git — `vX.Y.Z[-rcN]`, совпадающий с `__version__`.

---

## 3. macOS

Из папки `desktop/`, в окружении `.venv312`:

```bash
.venv312/bin/pip install pyinstaller
.venv312/bin/pyinstaller packaging/pulse_scan.spec --noconfirm        # → dist/Pulse Scan.app (~80 с)
"dist/Pulse Scan.app/Contents/MacOS/Pulse Scan" --version
"dist/Pulse Scan.app/Contents/MacOS/Pulse Scan" --selftest /tmp/st static_20260922.pulse --bag ../bags/<запись>
bash packaging/make_dmg.sh "dist/Pulse Scan.app" PulseScan-vX.Y.Z-macOS-arm64.dmg
```

В DMG лежат `Pulse Scan.app`, ярлык «Программы» и папка «Руководство».

**Собирайте из чистой копии тега**, а не из рабочей папки с незакоммиченными правками: PyInstaller берёт файлы как они лежат на диске.

```bash
git worktree add --detach /tmp/rc vX.Y.Z
cd /tmp/rc && /path/to/desktop/.venv312/bin/pyinstaller packaging/pulse_scan.spec --noconfirm \
    --workpath /tmp/rc_build --distpath /tmp/rc_dist
cd - && git worktree remove --force /tmp/rc
```

Подписи сертификатом Apple и нотаризации пока нет. Пользователь при первом запуске открывает программу через правую кнопку → «Открыть» (руководство, раздел 1). Для подписи нужен Apple Developer ID: `codesign_identity` в spec и `xcrun notarytool`.

---

## 4. Windows

### На Windows-компьютере или в ВМ — одной командой

```bat
packaging\build_windows.bat [путь\к\проекту.pulse] [/notests] [/noselftest] [/clean]
```

Скрипт:
1. находит Python 3.12 / 3.11 / 3.10 x64 через `py`;
2. создаёт отдельное окружение `build\venv`, ставит `requirements-qt.txt`, `pyinstaller`, `flask`;
3. прогоняет тесты (`/notests` — пропустить);
4. собирает приложение в `build\dist\Pulse Scan\`;
5. запускает **собранный** `Pulse Scan.exe --selftest` (окно появится на время проверки) и печатает шаги из `build\selftest\selftest.json`. С проектом проверка полная, без него — запуск и 3D;
6. добавляет `Руководство\` и `RELEASE_NOTES.md` и упаковывает **`build\release\PulseScan-vX.Y.Z-Windows-x64.zip`**.

Всё временное лежит в `build\` (в `.gitignore`). `/clean` пересоздаёт окружение и сборку с нуля.

**ВМ на Mac** (Parallels, UTM, VMware Fusion с Windows 11 ARM): ставьте **x64**-установщик Python, не ARM64. Windows 11 ARM выполняет x64 через эмуляцию, а колёс Open3D под Windows ARM нет.

**Docker** для Windows-сборки не подходит: на Mac он запускает только Linux-контейнеры. Сборка через Wine с PySide6 + VTK + Open3D на Apple Silicon (x86-эмуляция) ненадёжна, и проверить окно с 3D в контейнере нельзя.

---

## 5. GitHub Actions — обе ОС сразу

`.github/workflows/release.yml` запускается по тегу `v*` или вручную (Actions → release → Run workflow).

Для каждой ОС (`macos-14` arm64, `windows-latest` x64):
1. ставит зависимости;
2. прогоняет тесты;
3. собирает;
4. запускает самопроверку собранного приложения. На Windows она идёт с программной графикой (Mesa) и не прерывает сборку при неудаче: на виртуальных машинах GitHub нет видеокарты;
5. упаковывает DMG / zip.

Затем создаётся **черновик релиза** с обоими файлами и текстом из `docs/RELEASE_NOTES.md`. Версии с `rc` в теге помечаются как prerelease. Черновик публикуется вручную на GitHub.

```bash
git tag -a vX.Y.Z -m "Pulse Scan X.Y.Z"
git push origin <ветка> vX.Y.Z
```

---

## 6. Самопроверка (`--selftest`)

```
"Pulse Scan" --selftest OUT_DIR [проект.pulse] [--bag BAG]
```

Шаги:
- проект открыт и проанализирован;
- качество совмещения;
- сетка — в дочернем процессе; это проверяет `multiprocessing` в сборке;
- окно и 3D-вид рисуют;
- реконструкция bag (с `--bag`).

Результат пишется в `OUT_DIR/selftest.json` и `window.png`. Код выхода 0 — всё прошло. Работает и из исходников: `python scan_qt.py --selftest …`.

---

## 7. Руководство и снимки

Руководство — `docs/manual/README.md`. В программе открывается клавишей F1 (`MainWindow.show_manual`), в пакет попадает папкой «Руководство».

Снимки экрана пересоздаются на реальном проекте:

```bash
.venv312/bin/python tests/manual_shots.py static_20260922.pulse        # → docs/manual/img/*.png (светлая тема)
```

Скрипт подменяет сканер заглушкой, проходит все вкладки и состояния (импорт, стыковка, опорная точка, качество, сечения, замеры, чистка, поверхность, экспорт, передача) и возвращает настройки пользователя (тема, проекция и т. п.) как были.

В `.gitignore` стоит `*.png`. Снимки руководства и `packaging/icon.png` добавляются принудительно (`git add -f`) или исключением в `.gitignore`: `!docs/manual/img/*.png`, `!packaging/icon.png`.

---

## 8. Порядок выпуска (чек-лист)

1. [ ] `pulse_qt/version.py`, `docs/RELEASE_NOTES.md` обновлены.
2. [ ] Тесты: `python -m unittest discover -s tests`.
3. [ ] Проверки окна: `python tests/qt_interact.py X.pulse`, `python tests/qt_smoke.py X.pulse OUT`.
4. [ ] Руководство актуально; снимки пересняты при изменениях интерфейса.
5. [ ] Коммит, тег `vX.Y.Z[-rcN]`.
6. [ ] macOS: сборка из чистой копии тега, `--selftest` с проектом и bag, DMG.
7. [ ] Windows: `build_windows.bat проект.pulse` на Windows-машине или GitHub Actions.
8. [ ] Ручная проверка на «чистых» машинах: установка из DMG / zip, первый запуск, импорт, стыковка, экспорт E57.
9. [ ] Публикация: черновик релиза на GitHub (или раздача файлов напрямую).

---

## 9. Подводные камни сборки (проверено)

- **Нет интерпретатора в сборке.** Дочерние процессы нельзя запускать как `subprocess` + `sys.executable -c …`: в собранном приложении `sys.executable` — это сама программа. Расчёт сетки идёт через `multiprocessing` (контекст `spawn`), а в `scan_qt.py` вызывается `multiprocessing.freeze_support()`.
- **Open3D тянет `plotly` и `dash`** при импорте (`open3d.visualization`). Исключать их в spec нельзя: приложение не запустится.
- **Ленивые импорты.** Модули логики импортируются внутри функций, поэтому PyInstaller их не видит. Они перечислены в `hiddenimports` spec-файла. Новый модуль логики, который грузится лениво, добавьте туда.
- **Писать рядом с программой нельзя:** на macOS `.app` только для чтения. Скриншоты идут в `~/Pulse/Скриншоты` (`main_window.user_dir`), данные — в `~/Pulse` или рядом с проектом.
- **Файлы данных внутри сборки** ищутся через `sys._MEIPASS` (руководство, значок окна; `main_window.manual_path`, `app.make_app`). Шрифты и qtawesome находятся сами через `__file__`.
- **Оконный `.exe` на Windows без консоли:** `sys.stdout` и `sys.stderr` равны `None`. `flush` и `reconfigure` вызываются только с проверкой, результат самопроверки берётся из `selftest.json`.
- **Двойной щелчок по `.pulse` на macOS** передаёт файл событием `QEvent.FileOpen`, а не аргументом (`app._FileOpen`). Тип документа объявлен в `Info.plist` (`CFBundleDocumentTypes` в spec).
- **Open3D импортируется в главном потоке при запуске** (`app.make_app`). Иначе первый импорт мог случиться из фонового потока загрузки сканов.
- **Калибровка лидара** в сборке не зависит от констант `offline_deskew.py` и `calib.json`: реконструкция берёт её из записи или из таблицы `calibration.HISTORY`. При замене кронштейна обновите таблицу или пишите калибровку в bag на сканере.
- **Размер:** основное — PySide6 (~1,2 ГБ в окружении), VTK (~0,5 ГБ), Open3D (~0,3 ГБ). Неиспользуемые модули Qt (WebEngine, Qt3D, Multimedia, QML и др.) исключены в spec. Сборка ~0,9 ГБ, DMG ~0,3 ГБ.
