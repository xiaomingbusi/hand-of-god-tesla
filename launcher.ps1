# 上帝之手 - 拖拽启动器（PowerShell 版，原生支持中文）
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::GetEncoding(936) } catch {}
Set-Location $PSScriptRoot

Write-Host "============================================"
Write-Host "   上帝之手 Hand of God"
Write-Host "   特斯拉行车视频还原工具"
Write-Host "============================================"
Write-Host ""

# 环境检查
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[错误] 没有检测到 Python！" -ForegroundColor Red
    Write-Host "请先按 README 说明安装 Python，安装时勾选 Add Python to PATH。"
    Write-Host "下载地址：https://www.python.org/downloads/"
    exit 1
}
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Host "[错误] 没有检测到 ffmpeg！" -ForegroundColor Red
    Write-Host "请在 PowerShell 中执行：winget install Gyan.FFmpeg，装完重开窗口。"
    exit 1
}

$target = $args[0]
if (-not $target) {
    Write-Host "请用下面任意一种方式选择要处理的视频："
    Write-Host "  方式一：把【事件文件夹或视频文件】拖到本窗口里，然后按回车"
    Write-Host "  方式二：关掉本窗口，把它直接拖到本 bat 文件的图标上"
    Write-Host ""
    $target = Read-Host "拖入后按回车（直接按回车退出）"
}
if ([string]::IsNullOrWhiteSpace($target)) { exit 0 }

$target = $target.Trim('"').Trim("'")

while (-not (Test-Path -LiteralPath $target)) {
    Write-Host ""
    Write-Host "[错误] 找不到这个文件或文件夹：" -ForegroundColor Red
    Write-Host $target
    $target = Read-Host "请重新拖入（直接按回车退出）"
    if ([string]::IsNullOrWhiteSpace($target)) { exit 0 }
    $target = $target.Trim('"').Trim("'")
}

Write-Host ""
Write-Host "正在处理：$target"
Write-Host "视频越长耗时越久，处理期间请勿关闭窗口……"
Write-Host ""

& python (Join-Path $PSScriptRoot "tesla_dashcam_processor.py") $target

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "   处理结束！成片在原视频文件夹中，文件名带 _with_overlay"
    Write-Host "============================================" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "处理过程中出现错误（退出码 $LASTEXITCODE），请把上方红色文字截图反馈。" -ForegroundColor Red
}