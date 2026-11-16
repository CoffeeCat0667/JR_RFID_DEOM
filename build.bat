@echo off
setlocal
cd /d "%~dp0"

python -m pip install -r requirements.txt || goto :error

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name RFID_EPC_Uploader --paths . app\main.py || goto :error

echo.
echo Build OK: dist\RFID_EPC_Uploader.exe
exit /b 0

:error
echo.
echo Build FAILED.
exit /b 1
