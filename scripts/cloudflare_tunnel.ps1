<#
.SYNOPSIS
  Local Dots V1.0 Cloudflare 隧道管理（票 09）—— setup / run / quick / status / register / remove。

.DESCRIPTION
  拓扑（docs/cloudflare-blueprint.md §2.1）：
    手机 → Cloudflare Access（MFA，仅指定身份）→ Cloudflare Tunnel → 127.0.0.1:8000（FastAPI）
  纪律（AGENTS.md 安全基线 / 蓝图 §2.2）：
    - 本机不新增监听、不开放入站端口；隧道配置只映射 127.0.0.1:8000；
    - 8788（mimo2codex）、Codex 凭据、API 密钥一律不经隧道；
    - Access 是入口层不是权限层：后端 API Token 校验保持不变；
    - quick 子命令（一次性 trycloudflare.com 链接）无 MFA/Access，
      仅限开发/冒烟；生产必须走"命名隧道 + Access 策略 + MFA"。

.PARAMETER Action
  setup   : 打印命名隧道三步指引（login/create/route dns）与 Access 策略建法；检查凭据状态
  run     : 前台运行命名隧道（cloudflared tunnel run localdots，生产路径）
  quick   : 一次性 quick tunnel（--url 127.0.0.1:8000，仅开发/冒烟，无 MFA）
  status  : 只读核查：cloudflared 进程 / 8000 回环 / 8788 仅回环 / 配置守卫 / 凭据 / 自启
  register: 注册命名隧道的登录自启（Startup 目录 bat，票 08 同模式；默认不注册，由用户决定）
  remove  : 清理隧道自启入口

.EXAMPLE
  powershell -File scripts\cloudflare_tunnel.ps1 setup
  powershell -File scripts\cloudflare_tunnel.ps1 status
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)][ValidateSet("setup", "run", "quick", "status", "register", "remove", "help")]
  [string]$Action = "help"
)

$ErrorActionPreference = "Stop"
$RepoRoot      = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Cf            = Join-Path $RepoRoot "cloudflared.exe"
$Cfg           = Join-Path $RepoRoot "scripts\cloudflare_tunnel.json"
$TunnelName    = "localdots"
$Backend       = "http://127.0.0.1:8000"
$CredDir       = Join-Path $env:USERPROFILE ".cloudflared"
$LauncherDir   = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" -Name Startup).Startup
$LauncherPath  = [System.IO.Path]::Combine($LauncherDir, "localdots_tunnel_autostart.bat")

function Get-CredentialsStatus {
  $c = Get-ChildItem -Path $CredDir -Filter "*.json" -File -ErrorAction SilentlyContinue
  if ($c) { return $true }
  return $false
}

function Test-Binary {
  if (-not (Test-Path $Cf)) {
    Write-Host "[!] 缺少 cloudflared.exe（仓库根，gitignore，由本机提供）。" -ForegroundColor Red
    exit 1
  }
}

function Show-Setup {
  Test-Binary
  Write-Host "=== 命名隧道三步（一次性）===" -ForegroundColor Cyan
  Write-Host "  1) 登录（浏览器授权；凭据写入 $CredDir\）"
  Write-Host "       .\cloudflared.exe tunnel login"
  Write-Host "  2) 创建命名隧道 $TunnelName"
  Write-Host "       .\cloudflared.exe tunnel create $TunnelName"
  Write-Host "  3) 绑定域名（<host>.<你的 cloudflare zone>；DNS 由 route dns 管理，配置里不写死）"
  Write-Host "       .\cloudflared.exe tunnel route dns $TunnelName <host>.<your-zone>"
  Write-Host ""
  Write-Host "=== Access 策略（Cloudflare Zero Trust 控制台）===" -ForegroundColor Cyan
  Write-Host "  1) 应用安全与网络 -> Access -> 应用程序 -> Add an application -> Self-hosted"
  Write-Host "     -> 选择 Cloudflare Tunnel $TunnelName -> Service http://127.0.0.1:8000 (port 8000)"
  Write-Host "  2) 策略：Allow 指定身份（公司账号清单），勾选 MFA 校验（如 TOTP/Passkey）；"
  Write-Host "     未放行身份到不了本机。Access 是入口层，不是权限层——后端 API Token 校验不变。"
  Write-Host "  3) 先 quick 冒烟连通性，再 run 命名隧道上生产。"
  Write-Host ""
  if (Get-CredentialsStatus) {
    Write-Host "[OK] 已发现凭据文件（$CredDir），可直接 run。"
  } else {
    Write-Host "[!] 未发现凭据（$CredDir\*.json）：请先执行第 1 步 tunnel login，否则 run 会失败。" -ForegroundColor Yellow
  }
}

function Run-NamedTunnel {
  Test-Binary
  if (-not (Get-CredentialsStatus)) {
    Write-Host "[!] 无凭据，命名隧道无法启动。先执行：powershell -File scripts\cloudflare_tunnel.ps1 setup" -ForegroundColor Red
    exit 1
  }
  Write-Host "启动命名隧道 $TunnelName -> $Backend（Ctrl+C 停止；本机不新增入站端口）"
  & $Cf --no-autoupdate --config $Cfg "tunnel" "run" $TunnelName
}

function Run-QuickTunnel {
  Test-Binary
  Write-Host "!!! 警告：quick tunnel 无 Access/MFA，公网匿名可达（仅开发/冒烟，用完即停）！" -ForegroundColor Yellow
  Write-Host "!!! 生产请使用命名隧道 + Access 策略（见 setup）。" -ForegroundColor Yellow
  & $Cf --no-autoupdate "tunnel" "--url" $Backend
}

function Show-Status {
  Write-Host "=== Local Dots Cloudflare 状态（只读）===" -ForegroundColor Cyan
  $p = Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue
  if ($p) { Write-Host "[OK] cloudflared 进程在运行 (pid: $($p.Id -join ','))." }
  else     { Write-Host "[i ] cloudflared 进程未运行。" }

  $c8000 = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
  if ($c8000) {
    $bad = $c8000 | Where-Object { $_.LocalAddress -notin @("127.0.0.1", "::1") }
    if ($bad) { Write-Host "[!!] 8000 存在非回环绑定：$($bad.LocalAddress -join ',')（违反蓝图 §2.2）" -ForegroundColor Red }
    else      { Write-Host "[OK] 8000 仅 127.0.0.1 回环 LISTEN。" }
  } else {
    Write-Host "[i ] 8000 未 LISTEN（FastAPI 服务当前未运行）。"
  }

  $c8788 = Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue
  if ($c8788) {
    $bad = $c8788 | Where-Object { $_.LocalAddress -notin @("127.0.0.1", "::1") }
    if ($bad) { Write-Host "[!!] 8788 (mimo2codex) 存在非回环绑定：$($bad.LocalAddress -join ',')（违反蓝图 §2.2 第3条）" -ForegroundColor Red }
    else      { Write-Host "[OK] 8788 (mimo2codex) 仅 127.0.0.1 回环，对公网不可达。" }
  } else {
    Write-Host "[i ] 8788 未监听（mimo2codex 当前未运行；守卫通过）。"
  }

  if (Test-Path $Cfg) {
    $raw = [System.IO.File]::ReadAllText($Cfg)
    $violations = @()
    if ($raw -match "8788") { $violations += "8788" }
    foreach ($m in @("api_key", "secret", "token", "password", "bearer", "credential")) {
      if ($raw.ToLower() -match [regex]::Escape($m)) { $violations += $m }
    }
    if ($violations) {
      Write-Host "[!!] 隧道配置出现守卫违禁项: $($violations -join ',')（蓝图 §2.2）" -ForegroundColor Red
    } else {
      Write-Host "[OK] 隧道配置 $Cfg 无 8788/密钥类字段。"
    }
  } else {
    Write-Host "[i ] 隧道配置 $Cfg 不存在。"
  }

  if (Get-CredentialsStatus) { Write-Host "[OK] 凭据文件存在（$CredDir）。" }
  else { Write-Host "[i ] 无凭据文件（命名隧道未 setup；quick 无需凭据）。" }

  if (Test-Path $LauncherPath) { Write-Host "[OK] 隧道自启入口存在: $LauncherPath" }
  else { Write-Host "[i ] 隧道自启入口不存在（默认不注册；需要时执行 register）。" }
}

function Get-TunnelLauncherText {
  @"
@echo off
rem Local Dots V1.0 Cloudflare named tunnel autostart entry (ticket 09).
rem Backend stays loopback-only (127.0.0.1:8000); no public ingress port is opened on this host.
rem ASCII-only on purpose: cmd.exe reads this file in the system code page.
rem Prereqs (see setup): cloudflared credentials + Access policy with MFA.
netstat -ano | findstr "127.0.0.1:8000" | findstr "LISTENING" >nul 2>&1
if %errorlevel% neq 0 (
  echo [localdots-tunnel] 127.0.0.1:8000 not LISTENING; tunnel has no service for now.
)
cd /d "$RepoRoot"
"$Cf" --no-autoupdate --config "$Cfg" tunnel run $TunnelName
"@
}

function Register-TunnelLauncher {
  [System.IO.File]::WriteAllText($LauncherPath, (Get-TunnelLauncherText), [System.Text.Encoding]::ASCII)
  Write-Host "[+] 隧道自启入口已注册: $LauncherPath"
  Write-Host "    登录时后台运行命名隧道（8000 未启动则仅打印提醒，服务起来后链路自愈）。"
  Write-Host "    提醒：请确认 Access 策略（指定身份 + MFA）已生效后再保留自启。"
}

function Remove-TunnelLauncher {
  if (Test-Path $LauncherPath) {
    Remove-Item $LauncherPath -Force
    Write-Host "[-] 隧道自启入口已移除: $LauncherPath"
  } else {
    Write-Host "[i] 隧道自启入口不存在（可能未注册）。"
  }
}

function Show-Help {
  Write-Host "子命令: setup | run | quick | status | register | remove"
  Write-Host "  生产路径: setup（一次性）-> Access 策略配置 -> run（可 register 自启）"
  Write-Host "  冒烟路径: quick（临时公网 URL，无 MFA，用完即停）"
}

switch ($Action) {
  "setup"    { Show-Setup }
  "run"      { Run-NamedTunnel }
  "quick"    { Run-QuickTunnel }
  "status"   { Show-Status }
  "register" { Register-TunnelLauncher }
  "remove"   { Remove-TunnelLauncher }
  "help"     { Show-Help }
}