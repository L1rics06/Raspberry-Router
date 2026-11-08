param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$bundlePath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $bundlePath
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'

# 初始化独立虚拟环境，Python参数可传解释器的完整路径
# 不安装系统服务，不启动舵机，复制到另一台电脑后需重新执行
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $Python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ required' }
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
Write-Host 'Ready: .\scripts\run.ps1 demo'
