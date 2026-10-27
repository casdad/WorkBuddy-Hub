$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = @('.\.venv\Scripts\python.exe','..\.venv\Scripts\python.exe') | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) { throw '请先建立 Python 环境并安装 requirements-dev.txt。' }
Push-Location web
npm.cmd ci
if ($LASTEXITCODE) { throw 'npm ci 失败' }
npm.cmd run build
if ($LASTEXITCODE) { throw '前端构建失败' }
Pop-Location
$bundleArgs = @('--noconfirm','--clean','--onefile','--name','WorkBuddyHub','--add-data','web/dist;web/dist','--add-data','hub/vendor/LICENSE-wb2api;licenses','--add-data','hub/vendor/LICENSE-switch;licenses','--collect-all','pystray')
# Some Python distributions add a certificate policy file beside ssl.py.
# Preserve that policy in the frozen runtime instead of disabling TLS checks.
$sslPolicy = & $python -c "import pathlib, ssl; print(pathlib.Path(ssl.__file__).with_name('codex-rejected-cas.pem'))"
if (Test-Path -LiteralPath $sslPolicy) { $bundleArgs += @('--add-data', "$sslPolicy;.") }
& $python -m PyInstaller @bundleArgs main.py
if ($LASTEXITCODE) { throw 'EXE 打包失败' }
Get-FileHash -LiteralPath '.\dist\WorkBuddyHub.exe' -Algorithm SHA256 | Format-List
