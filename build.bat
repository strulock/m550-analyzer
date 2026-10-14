@echo off
rem One-shot Windows build: dist\BMW Analyzer\ with n63read.exe (GUI),
rem n63read-cli.exe and _internal\. See BMW-Analyzer.spec.
setlocal
cd /d "%~dp0"

python -m PyInstaller --noconfirm BMW-Analyzer.spec || exit /b 1

if not exist "dist\BMW Analyzer\n63read.exe" exit /b 1
if not exist "dist\BMW Analyzer\n63read-cli.exe" exit /b 1
echo Built "dist\BMW Analyzer\"
