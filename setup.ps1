$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
py -3 -m venv .venv
if ($LASTEXITCODE) { throw 'Python environment creation failed' }
& .\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
if ($LASTEXITCODE) { throw 'Python dependency installation failed' }
Push-Location web
npm.cmd ci
if ($LASTEXITCODE) { throw 'npm ci failed' }
npm.cmd run build
if ($LASTEXITCODE) { throw 'Frontend build failed' }
Pop-Location
