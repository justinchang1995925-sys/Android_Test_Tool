@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Checking Python syntax...
python -m py_compile app.py
if errorlevel 1 goto failed

echo [2/3] Installing PyInstaller if needed...
python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  python -m pip install pyinstaller
  if errorlevel 1 goto failed
)

echo [3/3] Building AndroidTestTool.exe...
python -m PyInstaller ^
  --clean ^
  --onefile ^
  --name AndroidTestTool ^
  --add-data "index.html;." ^
  --add-data "stats.html;." ^
  --add-data "theme.css;." ^
  --add-data "theme.js;." ^
  --add-data "chart-export.js;." ^
  app.py
if errorlevel 1 goto failed

echo.
echo Build complete: %~dp0dist\AndroidTestTool.exe
echo Copy this exe to another Windows machine with adb available in PATH, then double-click it.
pause
exit /b 0

:failed
echo.
echo Build failed. Please review the error above.
pause
exit /b 1
