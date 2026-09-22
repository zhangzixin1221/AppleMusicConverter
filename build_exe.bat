@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   AMC - 构建单文件 exe
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 Python，请先安装 Python 3.9+ 并加入 PATH。
    pause
    exit /b 1
)

echo [1/3] 安装依赖...
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail

echo.
echo [2/3] 构建 AMC.exe（图形界面，无控制台）...
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build AMC.spec
if errorlevel 1 goto fail

echo.
echo [3/3] 构建 AMC-CLI.exe（命令行，带控制台）...
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build AMC-CLI.spec
if errorlevel 1 goto fail

echo.
echo ============================================
echo   构建完成，产物在 dist\ 目录：
echo ============================================
dir /b dist
echo.
echo 注意：exe 不含 ffmpeg。请确保 ffmpeg.exe / ffprobe.exe
echo       在 PATH 中，或直接放在 exe 同一目录下。
echo.
pause
exit /b 0

:fail
echo.
echo [错误] 构建失败，请检查上方输出。
pause
exit /b 1
