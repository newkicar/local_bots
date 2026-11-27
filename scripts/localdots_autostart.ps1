<#
.SYNOPSIS
  Local Dots V1.0 登录自启常驻（票 08）—— 每用户 Startup 目录自启入口 注册/注销/查询。

.DESCRIPTION
  为什么不用计划任务（本机实测记录，2026-11-04）：
    本机为公司标准用户（无管理员），创建任务对象被 IT 策略拒绝：
      - Register-ScheduledTask / schtasks /create 均 "拒绝访问" (HRESULT 0x80070005)；
      - 根库与子库均验证失败。
    每用户 Startup 目录是 Windows 标准用户对登录自启的受支持机制
    （无管理员、无密码、无 UAC），故作为 V1.0 自启入口。
    若日后任务调度，可平移为计划任务（验收记录与云网蓝图已注明）。

  纪律（AGENTS.md / 方案文档第八节）：
    - 服务仅监听 127.0.0.1:8000，永不直接暴露公网；
    - 不打包、非 Docker；登录自启只是"启动 uvicorn"这一步；
    - 自启以当前用户运行，不落密码、不动系统级配置。  健康门禁（防僵尸服务，2026-11-26 修订）：
    根因：开机瞬时竞态可挂死 uvicorn lifespan（端口已绑定、HTTP 无响应，即"僵尸服务"）；
    旧版守护只检查 8000 是否 LISTENING 就跳过，会把僵尸放大成永久故障。
    现启动器改为三段门禁：端口空闲→启动；HTTP 探活健康→跳过；
    端口被占但 HTTP 无响应（僵尸）→先杀旧 python/uvicorn 进程再启动。

.PARAMETER Python
  可选：指定启动服务用的 Python 可执行路径；缺省读 $env:LD_PYTHON，再缺省用 PATH 上的 python。

.PARAMETER Action
  register : 写入每用户 Startup 目录自启入口（幂等覆盖；健康门禁：服务健康则跳过，端口被僵尸占用则先杀旧进程再启动）
  remove   : 清理自启入口
  status   : 查看自启入口与 8000 端口监听情况

.EXAMPLE
  powershell -File scripts\localdots_autostart.ps1 register
  powershell -File scripts\localdots_autostart.ps1 status
  powershell -File scripts\localdots_autostart.ps1 remove
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)][ValidateSet("register", "remove", "status")]
  [string]$Action = "status"
  [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$Python = if (-not $Python) {
  if ($env:LD_PYTHON) {
    $env:LD_PYTHON
  }
  else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $cmd.Source } else { "python" }
  }
}
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Port    = 8000
$LauncherDir = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" -Name Startup).Startup
$LauncherPath = [System.IO.Path]::Combine($LauncherDir, "localdots_autostart.bat")

function Get-LauncherText {
  $bat = @'
@echo off
rem Local Dots V1.0 autostart resident entry (ticket 08): loopback-only FastAPI service.
rem The service listens on 127.0.0.1:8000 only and is never exposed to the public network.
rem ASCII-only on purpose: cmd.exe reads this file in the system code page.
rem Health gate (2026-11-26): port free -> start; HTTP healthy -> skip;
rem zombie listener (port bound, no HTTP) -> kill old process, then start.
set "REPO=__REPO__"
set "PY=__PY__"
set "PORT=__PORT__"
:gate
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', __PORT__); $c.Close() } catch { exit 1 }; try { $r = Invoke-WebRequest 'http://127.0.0.1:__PORT__/' -TimeoutSec 4 -UseBasicParsing; exit 0 } catch { exit 2 }"
if %errorlevel%==0 (
  echo [localdots] 127.0.0.1:__PORT__ healthy; skip start.
  goto :end
)
if %errorlevel%==2 (
  echo [localdots] zombie listener on 127.0.0.1:__PORT__ (port bound, no HTTP); killing it.
  for /f "tokens=5" %%a in ('netstat -ano ^| findstr "127.0.0.1:__PORT__" ^| findstr "LISTENING" ') do (
    for /f "delims=" %%i in ('tasklist /FI "PID eq %%a" /FO CSV 2^>nul ^| findstr /i "python.exe uvicorn" ') do taskkill /F /PID %%a >nul 2>&1
  )
  timeout /t 3
)
cd /d "%REPO%"
start "" /min "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% 1>"%REPO%\uvicorn_boot.log" 2>&1
echo [localdots] uvicorn launched (minimized); polling health up to 3 rounds.
timeout /t 15
set "TRY=0"
:poll
set /a TRY+=1
powershell -NoProfile -Command "try { $r = Invoke-WebRequest 'http://127.0.0.1:__PORT__/' -TimeoutSec 4 -UseBasicParsing; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  echo [localdots] service healthy on 127.0.0.1:%PORT%
  goto :end
)
if %TRY% GEQ 3 (
  echo [localdots] service not healthy after %TRY% rounds; check %REPO%\uvicorn_boot.log
  exit /b 1
)
timeout /t 90
goto :poll
:end
exit /b 0
'@
  $bat = $bat.Replace("`r`n", "`n")
  $bat = $bat.Replace("__REPO__", $RepoRoot)
  $bat = $bat.Replace("__PY__", $Python)
  $bat = $bat.Replace("__PORT__", [string]$Port)
  $bat = $bat.Replace("`n", "`r`n")
  return $bat
}

function Register-LDLauncher {
  [System.IO.File]::WriteAllText($LauncherPath, (Get-LauncherText), [System.Text.Encoding]::ASCII)
  Write-Host "[+] 自启入口已注册: $LauncherPath"
  Write-Host "    登录时健康门禁: 服务健康则跳过; 端口被僵尸占用则先杀旧进程再启动"`n  Write-Host "    服务日志写入 $RepoRoot\uvicorn_boot.log"
  Write-Host "    Known limitation: minimized cmd window during health polling (recorded in acceptance record; a hidden VBS wrapper is a V1.1 candidate)."
}

function Remove-LDLauncher {
  if (-not (Test-Path $LauncherPath)) {
    Write-Host "[i] 自启入口不存在（可能已清理）: $LauncherPath"
    return
  }
  Remove-Item $LauncherPath -Force
  Write-Host "[-] 自启入口已移除: $LauncherPath"
}

function Show-LDStatus {
  if (Test-Path $LauncherPath) {
    Write-Host "[=] 自启入口存在: $LauncherPath"
  } else {
    Write-Host "[.] 自启入口不存在: $LauncherPath（未注册）"
  }
  $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  if ($conn) {
    Write-Host "    [i] 端口 $Port 当前 LISTEN 中 (pid = $($conn.OwningProcess -join ','))."
    try {
      $resp = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/health")
      $resp.Method = "GET"
      $resp.Headers.Add("Authorization", "Bearer local-dots-dev-token")
      $resp.Timeout = 3000
      $r2 = $resp.GetResponse()
      Write-Host "    [i] /api/health -> $([int]$r2.StatusCode)"
      $r2.Close()
    } catch {
      Write-Host "    [i] /api/health 不可达: $($_.Exception.Message)"
    }
  } else {
    Write-Host "    [i] 端口 $Port 未 LISTEN（服务当前未运行）。"
  }
}

switch ($Action) {
  "register" { Register-LDLauncher }
  "remove"   { Remove-LDLauncher }
  "status"   { Show-LDStatus }
}