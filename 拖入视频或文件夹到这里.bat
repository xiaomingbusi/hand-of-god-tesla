@echo off
chcp 65001 >nul
title 上帝之手 - 特斯拉行车视频还原
cd /d "%~dp0"

echo ============================================
echo    上帝之手 Hand of God
echo    特斯拉行车视频还原工具
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有检测到 Python！
    echo 请先按 README 说明安装 Python，安装第一步务必勾选 "Add Python to PATH"。
    echo 下载地址：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有检测到 ffmpeg！
    echo 请按 README 说明，在 PowerShell 中执行：winget install Gyan.FFmpeg
    echo 安装后重新打开本窗口。
    echo.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo 使用方法：
    echo   把 U 盘里的【事件文件夹】（里面有 event.json 的那个）
    echo   或【单个视频文件】，直接拖到这个 bat 文件的图标上。
    echo.
    echo 处理完成后，成片 *_with_overlay.mp4 会生成在原视频旁边。
    echo.
    pause
    exit /b 1
)

echo 正在处理：%~1
echo 视频越长耗时越久，处理期间请勿关闭窗口……
echo.
python tesla_dashcam_processor.py "%~1"

echo.
echo ============================================
echo    处理结束。如果上方没有红色 Error 字样，就是成功了。
echo    成片在原视频文件夹中，文件名带 _with_overlay
echo ============================================
pause
