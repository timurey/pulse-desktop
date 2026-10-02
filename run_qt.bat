@echo off
rem Pulse Scan (новое окно, Qt + VTK) - Windows.
rem Окружение .venv312 общее со старым окном (run_gui.bat); недостающее ставится при первом запуске.
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set VENV=.venv312
if not exist "%VENV%\Scripts\python.exe" (
  set PY=
  for %%v in (3.12 3.11 3.10) do (
    if not defined PY (
      py -%%v -c "import sys" >nul 2>&1 && set PY=py -%%v
    )
  )
  if not defined PY (
    echo Нужен Python 3.10-3.12 ^(для Open3D 0.19^): https://www.python.org/downloads/
    pause
    exit /b 1
  )
  echo Создаю окружение - один раз, несколько минут...
  call !PY! -m venv "%VENV%"
  "%VENV%\Scripts\python.exe" -m pip install --upgrade pip
)
"%VENV%\Scripts\python.exe" -c "import PySide6, vtk, qtawesome, open3d" >nul 2>&1
if errorlevel 1 (
  echo Ставлю зависимости окна ^(PySide6, VTK^) - один раз...
  "%VENV%\Scripts\python.exe" -m pip install -r requirements-qt.txt
)
"%VENV%\Scripts\python.exe" scan_qt.py %*
