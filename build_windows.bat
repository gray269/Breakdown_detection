@echo off
setlocal
cd /d "%~dp0"

echo Installation des dependances et de PyInstaller...
py -m pip install --upgrade pip
if errorlevel 1 goto error
py -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto error

echo Creation de SuiviClaquage.exe...
py -m PyInstaller --clean --noconfirm --onefile --windowed --name SuiviClaquage app.py
if errorlevel 1 goto error

echo.
echo Application creee : dist\SuiviClaquage.exe
pause
exit /b 0

:error
echo.
echo La creation a echoue. Verifiez Python et la connexion internet, puis relancez ce fichier.
pause
exit /b 1
