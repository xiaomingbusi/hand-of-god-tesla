# 构建「上帝之手」绿色版：自带 Python 运行时、ffmpeg/ffprobe、字体与图标
# 用法：右键“使用 PowerShell 运行”，或在 PowerShell 里执行 .\build_green.ps1
# 想减小体积可指定 shared 版 ffmpeg 的 bin 目录（里面含 dll）：
#   .\build_green.ps1 -FfmpegBin "C:\path\to\ffmpeg-shared\bin"
param(
    [string]$FfmpegBin = ''
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$stage = Join-Path $env:TEMP 'hog_build_stage'
$work = Join-Path $env:TEMP 'hog_build_work'
$dist = Join-Path $root '绿色版'

Write-Host "1/4 准备暂存目录..."
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'assets'), (Join-Path $stage 'bin') | Out-Null

Write-Host "2/4 归集资源（图标、车身图、中文字体、ffmpeg）..."
$usedAssets = @('autosteer-icon.png', 'car_side.png', 'pedal_acc.png', 'pedal_brake.png', 'HandOfGod.ico')
foreach ($name in $usedAssets) {
    $src = Join-Path $root "assets\$name"
    if (-not (Test-Path $src)) { throw "缺少素材：$src" }
    Copy-Item $src (Join-Path $stage 'assets')
}
$font = 'C:\Windows\Fonts\simhei.ttf'
if (-not (Test-Path $font)) { throw "缺少中文字体：$font" }
Copy-Item $font (Join-Path $stage 'assets')

if ($FfmpegBin) {
    if (-not (Test-Path $FfmpegBin)) { throw "FfmpegBin 目录不存在：$FfmpegBin" }
    $srcBin = (Resolve-Path $FfmpegBin).Path
    $ffmpeg = Join-Path $srcBin 'ffmpeg.exe'
    $ffprobe = Join-Path $srcBin 'ffprobe.exe'
    if (-not (Test-Path $ffmpeg) -or -not (Test-Path $ffprobe)) { throw "FfmpegBin 里缺少 ffmpeg.exe/ffprobe.exe：$srcBin" }
    Get-ChildItem $srcBin -File | Where-Object { $_.Name -ne 'ffplay.exe' } | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $stage 'bin')
    }
    Write-Host "    使用 shared 版 ffmpeg：$srcBin"
} else {
    $ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
    $ffprobe = (Get-Command ffprobe -ErrorAction SilentlyContinue).Source
    if (-not $ffmpeg -or -not $ffprobe) { throw "未找到 ffmpeg/ffprobe，请先执行 winget install Gyan.FFmpeg" }
    Copy-Item $ffmpeg (Join-Path $stage 'bin\ffmpeg.exe')
    Copy-Item $ffprobe (Join-Path $stage 'bin\ffprobe.exe')
}

Write-Host "3/4 打包（首次约几分钟）..."
$env:HOG_BUILD_ROOT = $stage
python -m PyInstaller --noconfirm --clean --workpath $work --distpath $dist (Join-Path $root 'tesla_overlay.spec')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

# PyInstaller 会把 ffmpeg 的依赖 dll 再复制一份到 _internal 根目录；
# bin 里已经有一份，这里删掉重复副本，避免包体积翻倍
Get-ChildItem (Join-Path $dist '上帝之手\_internal') -File -Filter '*.dll' |
    Where-Object { $_.Name -match '^(av|sw)[a-z]+-' } |
    Remove-Item -Force

Write-Host "4/4 收尾..."
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
Copy-Item (Join-Path $root 'amap_key.txt') (Join-Path $dist '上帝之手\amap_key.txt') -ErrorAction SilentlyContinue

$sizeMB = [math]::Round(((Get-ChildItem (Join-Path $dist '上帝之手') -Recurse | Measure-Object Length -Sum).Sum) / 1MB)
Write-Host "完成：$dist\上帝之手（约 $sizeMB MB）" -ForegroundColor Green
Write-Host "双击 tesla_overlay.exe 选文件，或把视频/事件文件夹直接拖到 exe 上。"
