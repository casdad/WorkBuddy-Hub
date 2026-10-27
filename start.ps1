param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$port = 8792
$url = "http://127.0.0.1:$port"

function Test-HubRunning {
    try { $null = Invoke-RestMethod "$url/healthz" -TimeoutSec 1; return $true } catch { return $false }
}

if (Test-HubRunning) {
    Write-Host "WorkBuddy Hub 已在运行：$url"
    Write-Host "已为你打开管理台；如需重启，请先结束该进程。"
    if (-not $NoBrowser) { Start-Process $url }
    exit 0
}

$dataDir = Join-Path $PSScriptRoot 'data'
$extra = @($args)
if ($NoBrowser) { $extra += '--no-open' }

$python = @('..\.venv\Scripts\python.exe', '.\.venv\Scripts\python.exe') |
    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if ($python) {
    Write-Host "启动方式：源码（$python）"
    Write-Host "关闭此窗口或按 Ctrl+C 即可停止服务。"
    & $python main.py --data-dir $dataDir @extra
    exit $LASTEXITCODE
}

if (Test-Path -LiteralPath '.\dist\WorkBuddyHub.exe') {
    Write-Host '启动方式：打包版本 dist\WorkBuddyHub.exe'
    # Keep the data location explicit and quoted.  Running the EXE directly
    # falls back to %LOCALAPPDATA%/WorkBuddyHub, which can be a different
    # account pool from this project and leaves clients with stale credentials.
    $arguments = @('--data-dir="{0}"' -f $dataDir) + $extra
    $process = Start-Process -FilePath '.\dist\WorkBuddyHub.exe' `
        -ArgumentList $arguments -WindowStyle Hidden -PassThru
    # Start-Process returns before the server has bound its port.  Wait for the
    # health endpoint so a failed EXE cannot look like a successful launch to
    # the caller (DSH would otherwise only report a generic connection error).
    for ($i = 0; $i -lt 40; $i++) {
        if (Test-HubRunning) {
            Write-Host "WorkBuddy Hub 已启动：$url"
            exit 0
        }
        if ($process.HasExited) {
            throw "WorkBuddyHub.exe 启动失败（退出码 $($process.ExitCode)）。请在终端直接运行 dist\WorkBuddyHub.exe 查看错误。"
        }
        Start-Sleep -Milliseconds 250
    }
    throw "WorkBuddy Hub 启动超时：$url 未响应。请检查 dist\WorkBuddyHub.exe 的输出和端口占用。"
}

throw '未找到可运行的环境：请先运行 setup.ps1 安装源码环境，或使用 release 包中的 EXE。'
