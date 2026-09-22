@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [错误] 没有找到 Python。
    echo   请先安装 Python 3.9 或更高版本，安装时勾选 "Add Python to PATH"。
    echo   下载地址: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

python -c "import mutagen" >nul 2>nul
if errorlevel 1 (
    echo 正在安装依赖 mutagen ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   [错误] 依赖安装失败，请检查网络。
        pause
        exit /b 1
    )
)

set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
python -m amc --gui
if errorlevel 1 pause
