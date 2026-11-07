<#
.SYNOPSIS
    把黑盒历史的图形界面打包成单文件 EXE：dist\heibox-history.exe

.EXAMPLE
    .\build.ps1            # 增量构建
    .\build.ps1 -Clean     # 先清掉 build/ 和 dist/ 再打
#>
param([switch]$Clean)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "找不到 .venv\Scripts\python.exe，请先在项目根目录创建虚拟环境并 pip install -e ."
}

if ($Clean) {
    Remove-Item -Recurse -Force (Join-Path $root 'build'), (Join-Path $root 'dist') -ErrorAction SilentlyContinue
}

$icon = Join-Path $root 'packaging\app.ico'
if (-not (Test-Path $icon)) {
    & $python (Join-Path $root 'packaging\make_icon.py')
}

# 原生命令往 stderr 写一行（比如 pip 的升级提示）时，EAP=Stop 会把它当成异常终止脚本，
# 所以这几步单独用 Continue，改由 $LASTEXITCODE 判断成败。
$ErrorActionPreference = 'Continue'

Write-Host '安装 PyInstaller…'
& $python -m pip install --quiet --upgrade 'pyinstaller>=6.11' 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "pip 安装 PyInstaller 失败，退出码 $LASTEXITCODE" }

Write-Host '开始打包…'
& $python -m PyInstaller --noconfirm --clean (Join-Path $root 'heibox-history.spec')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，退出码 $LASTEXITCODE" }

$ErrorActionPreference = 'Stop'

$exe = Join-Path $root 'dist\heibox-history.exe'
if (-not (Test-Path $exe)) { throw "没有生成 dist\heibox-history.exe" }
$mb = (Get-Item $exe).Length / 1MB
'{0}   {1:N1} MB' -f $exe, $mb
if ($mb -gt 16) { Write-Warning '超过 16 MB 的目标，检查是不是把新依赖带进来了' }
