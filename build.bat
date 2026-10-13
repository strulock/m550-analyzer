@echo off
rem One-shot Windows build: dist\n63read.exe (GUI) and dist\n63read-cli.exe.
setlocal
cd /d "%~dp0"

set DATA=src\n63read\data
set ADD=--add-data "%DATA%\N63TU2_DME_readable_measurements.csv;." ^
 --add-data "%DATA%\BMW_fault_code_texts.csv;." ^
 --add-data "%DATA%\BMW_dtc_status_byte.csv;." ^
 --add-data "%DATA%\BMW_enum_tables.csv;." ^
 --add-data "%DATA%\BMW_freeze_frame_fields.csv;." ^
 --add-data "%DATA%\G30_module_sgbd_map.csv;."

python -m PyInstaller --noconfirm --onefile --windowed --name n63read --paths src %ADD% src\n63read\gui.py || exit /b 1

rem The CLI is built without --windowed, or it has no stdout.
python -m PyInstaller --noconfirm --onefile --console --name n63read-cli --paths src %ADD% src\n63read\__main__.py || exit /b 1

if not exist dist\n63read.exe exit /b 1
if not exist dist\n63read-cli.exe exit /b 1
echo Built dist\n63read.exe and dist\n63read-cli.exe
