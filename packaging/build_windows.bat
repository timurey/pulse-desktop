@echo off
rem ============================================================================
rem  Сборка Pulse Scan для Windows (x64): окружение, зависимости, тесты, PyInstaller,
rem  самопроверка собранного приложения, zip для раздачи.
rem
rem    packaging\build_windows.bat [проект.pulse] [/notests] [/noselftest] [/clean]
rem
rem  Нужен Python 3.12 x64 (python.org, галочка «py launcher»); 3.10 / 3.11 тоже подойдут.
rem  Всё временное — в build\ (в .gitignore): окружение build\venv, сборка build\dist.
rem  Результат: build\release\PulseScan-v<версия>-Windows-x64.zip
rem  Проект для самопроверки (необязательно) — любой .pulse со сканами рядом.
rem ============================================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set "ROOT=%CD%"

set "PROJECT="
set "NOTESTS="
set "NOSELFTEST="
set "CLEAN="
:args
if "%~1"=="" goto args_done
if /i "%~1"=="/notests" (set "NOTESTS=1") else if /i "%~1"=="/noselftest" (set "NOSELFTEST=1") else if /i "%~1"=="/clean" (set "CLEAN=1") else set "PROJECT=%~f1"
shift
goto args
:args_done

echo.
echo === Pulse Scan: сборка для Windows ===
echo папка: %ROOT%

rem --- Python ----------------------------------------------------------------
set "PY="
for %%v in (3.12 3.11 3.10) do (
  if not defined PY (
    py -%%v-64 -c "import sys" >nul 2>&1 && set "PY=py -%%v-64"
  )
)
if not defined PY (
  echo [ошибка] Не найден Python 3.10-3.12 x64. Установите с https://www.python.org/downloads/windows/
  echo          ^(отметьте «Add python.exe to PATH» и «py launcher»^).
  exit /b 1
)
echo Python: %PY%

rem --- окружение сборки ------------------------------------------------------
set "VENV=%ROOT%\build\venv"
if defined CLEAN (
  echo очистка: build\ ^(окружение, сборка, zip^)
  if exist "%ROOT%\build" rmdir /s /q "%ROOT%\build"
)
if not exist "%VENV%\Scripts\python.exe" (
  echo создаю окружение build\venv ...
  %PY% -m venv "%VENV%" || (echo [ошибка] не удалось создать окружение & exit /b 1)
)
set "VPY=%VENV%\Scripts\python.exe"
echo зависимости ^(первый раз — несколько минут^) ...
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install -r requirements-qt.txt pyinstaller flask || (echo [ошибка] установка зависимостей & exit /b 1)

for /f "usebackq delims=" %%v in (`"%VPY%" -c "import sys; sys.path.insert(0, '.'); from pulse_qt.version import __version__; print(__version__)"`) do set "VER=%%v"
echo версия: %VER%

rem --- тесты логики ----------------------------------------------------------
if not defined NOTESTS (
  echo.
  echo === тесты ===
  set "PYTHONIOENCODING=utf-8"
  "%VPY%" -m unittest discover -s tests || (echo [ошибка] тесты не прошли ^(/notests — пропустить^) & exit /b 1)
)

rem --- сборка ----------------------------------------------------------------
echo.
echo === PyInstaller ===
"%VPY%" -m PyInstaller packaging\pulse_scan.spec --noconfirm --distpath "%ROOT%\build\dist" --workpath "%ROOT%\build\pyi" || (echo [ошибка] сборка & exit /b 1)
set "APPDIR=%ROOT%\build\dist\Pulse Scan"
set "EXE=%APPDIR%\Pulse Scan.exe"
if not exist "%EXE%" (echo [ошибка] нет "%EXE%" & exit /b 1)

rem --- самопроверка собранного приложения ------------------------------------
if not defined NOSELFTEST (
  echo.
  echo === самопроверка собранного приложения ^(окно появится на время проверки^) ===
  set "ST=%ROOT%\build\selftest"
  if exist "!ST!" rmdir /s /q "!ST!"
  if defined PROJECT (
    start "" /wait "%EXE%" --selftest "!ST!" "%PROJECT%"
  ) else (
    start "" /wait "%EXE%" --selftest "!ST!"
  )
  if not exist "!ST!\selftest.json" (
    echo [ошибка] самопроверка не создала отчёт — приложение не запустилось
    echo          ^(частая причина — нет OpenGL 3.2: обновите драйвер видеокарты^)
    exit /b 1
  )
  powershell -NoProfile -Command "$r = Get-Content -Raw -Encoding UTF8 '!ST!\selftest.json' | ConvertFrom-Json; foreach ($s in $r.steps) { '{0} {1}' -f ($(if ($s.ok) {'OK  '} else {'FAIL'}), $s.name) }; if ($r.error) { 'ошибка: ' + $r.error }; if (-not $r.ok) { exit 1 }"
  if errorlevel 1 (echo [ошибка] самопроверка не прошла, отчёт: !ST!\selftest.json & exit /b 1)
  if not defined PROJECT echo ^(проверены запуск и 3D; для полной проверки укажите проект: build_windows.bat путь\к\проекту.pulse^)
)

rem --- упаковка --------------------------------------------------------------
echo.
echo === zip ===
if exist "%APPDIR%\Руководство" rmdir /s /q "%APPDIR%\Руководство"
xcopy /e /i /q /y "%ROOT%\docs\manual" "%APPDIR%\Руководство" >nul
copy /y "%ROOT%\docs\RELEASE_NOTES.md" "%APPDIR%\RELEASE_NOTES.md" >nul
if not exist "%ROOT%\build\release" mkdir "%ROOT%\build\release"
set "ZIP=%ROOT%\build\release\PulseScan-v%VER%-Windows-x64.zip"
if exist "%ZIP%" del /q "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Path '%APPDIR%' -DestinationPath '%ZIP%' -CompressionLevel Optimal" || (echo [ошибка] упаковка & exit /b 1)

for %%f in ("%ZIP%") do set "SIZE=%%~zf"
set /a SIZEMB=%SIZE:~0,-6% 2>nul
echo.
echo === готово ===
echo %ZIP%  ^(~%SIZEMB% МБ^)
echo Раздавать этот zip: распаковать и запустить «Pulse Scan.exe».
endlocal
exit /b 0
