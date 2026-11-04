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
    若日后 IT 授权任务调度，可平移为计划任务（验收记录与云网蓝图已注明）。

  纪律（AGENTS.md / 方案文档第八节）：
    - 服务仅监听 127.0.0.1:8000，永不直接暴露公网；
    - 不打包、非 Docker；登录自启只是"启动 uvicorn"这一步；
    - 自启以当前用户运行，不落密码、不动系统级配置。

.PARAMETER Python
  可选：指定启动服务用的 Python 可执行路径；缺省读 $env:LD_PYTHON，再缺省用 PATH 上的 python。

.PARAMETER Action
  register : 写入每用户 Startup 目录自启入口（幂等覆盖；含 8000 已占用则跳过的守护）
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
  @"
@echo off
rem Local Dots V1.0 autostart resident entry (ticket 08): loopback-only FastAPI service.
rem The service listens on 127.0.0.1:8000 only and is never exposed to the public network.
rem ASCII-only on purpose: cmd.exe reads this file in the system code page.
rem If 8000 is already LISTENING (service survived logoff), skip a duplicate start.
netstat -ano | findstr "127.0.0.1:8000" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo [localdots] 127.0.0.1:8000 already LISTENING; skip duplicate start.
  goto :end
)
cd /d "$RepoRoot"
"$Python" -m uvicorn app.main:app --host 127.0.0.1 --port $Port
:end
"@
}

function Register-LDLauncher {
  [System.IO.File]::WriteAllText($LauncherPath, (Get-LauncherText), [System.Text.Encoding]::ASCII)
  Write-Host "[+] 自启入口已注册: $LauncherPath"
  Write-Host "    登录时由 cmd 执行: cd /d $RepoRoot && $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port"
  Write-Host "    Known limitation: logon start opens a visible cmd window (recorded in acceptance record; a hidden VBS wrapper is a V1.1 candidate)."
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