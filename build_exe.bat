@echo off
rem ============================================================
rem   AMC - build the single-file Windows executables
rem   Output: dist\AMC.exe (GUI) and dist\AMC-CLI.exe (console)
rem ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   AMC - building single-file exe
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.9+ and add it to PATH.
    pause
    exit /b 1
)

echo [1/3] Installing dependencies...
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail

echo.
echo [2/3] Building AMC.exe (GUI, no console)...
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build AMC.spec
if errorlevel 1 goto fail

echo.
echo [3/3] Building AMC-CLI.exe (console)...
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build AMC-CLI.spec
if errorlevel 1 goto fail

echo.
echo ============================================
echo   Build finished. Artifacts in dist\
echo ============================================
dir /b dist
echo.
echo NOTE: the exe does NOT bundle ffmpeg. Make sure ffmpeg.exe / ffprobe.exe
echo       are on PATH, or put them next to the exe.
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Build failed. Check the output above.
pause
exit /b 1