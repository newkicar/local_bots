# Local Dots - Cloudflare QUICK tunnel autostart (ticket-less / credential-free).
#
# Pattern follows the proven scan-puzzle launcher (kill stale -> wipe log ->
# fresh hidden start -> poll for URL, with a single HTTP/2 fallback):
#   D:\uida0712\Downloads\MES二期\扫描拼图游戏\一键启动隧道.ps1
#
# It does NOT touch the Local Dots service or its code. The service is
# started by localdots_autostart.bat (separate Startup entry).
#
# Outputs (repo root):
#   cf_quick.log      cloudflared log (fresh each run)
#   cf_quick_url.txt  the fresh URL, written when the tunnel is up
#
# NOTE: quick-tunnel URL rotates on EVERY start; the URL is valid only until
# the next boot / manual start. No Cloudflare account needed.

$ErrorActionPreference = "Continue"

$Repo    = "D:\Python Project\local_dots"
$Cf      = Join-Path $Repo "cloudflared.exe"
$Log     = Join-Path $Repo "cf_quick.log"
$UrlFile = Join-Path $Repo "cf_quick_url.txt"

function Stop-Cloudflared {
    $p = Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue
    if ($p) {
        $p | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}

# All argument values below are space-free on purpose (relative log name,
# loopback URL): Start-Process ArgumentList does not auto-quote spaces.
function Start-Cloudflared([string[]]$ExtraArgs) {
    $argList = @("tunnel", "--url", "http://127.0.0.1:8000", "--no-autoupdate", "--logfile", "cf_quick.log") + $ExtraArgs
    Start-Process -FilePath $Cf -ArgumentList $argList -WorkingDirectory $Repo -WindowStyle Hidden
}

function Get-TunnelUrl([int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Path $Log) {
            $lines = Get-Content $Log -ErrorAction SilentlyContinue
            if ($null -ne $lines) {
                $hit = $lines | Where-Object { $_ -match "https://[a-z0-9\-]+\.trycloudflare\.com" } | Select-Object -First 1
                if ($hit) {
                    $m = [regex]::Match($hit, "https://[a-z0-9\-]+\.trycloudflare\.com")
                    if ($m.Success) { return $m.Value }
                }
            }
        }
    }
    return $null
}

Write-Host "[LocalDots] quick-tunnel autostart"

if (-not (Test-Path $Cf)) {
    Write-Host "[X] cloudflared.exe not found at $Cf"
    exit 1
}

# 1) Soft-wait (max 30s) for the local service so the URL is usable sooner.
#    The tunnel is created anyway if the service is still down.
$svcUp = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:8000/" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($r.StatusCode) { $svcUp = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $svcUp) { Write-Host "[..] service 127.0.0.1:8000 not up yet - starting tunnel anyway" }

# 2) Kill stale cloudflared + wipe log, then a fresh start (QUIC/UDP first).
Stop-Cloudflared
if (Test-Path $Log) { Remove-Item $Log -Force -ErrorAction SilentlyContinue }
if (Test-Path $UrlFile) { Remove-Item $UrlFile -Force -ErrorAction SilentlyContinue }

Write-Host "[..] starting cloudflared (QUIC) ..."
Start-Cloudflared @()
$Url = Get-TunnelUrl 30

# 3) Still no URL -> likely UDP blocked; retry once over HTTP/2 (TCP/443).
if (-not $Url) {
    Write-Host "[..] no URL in 30s, retrying with --protocol http2 (TCP/443) ..."
    Stop-Cloudflared
    if (Test-Path $Log) { Remove-Item $Log -Force -ErrorAction SilentlyContinue }
    Start-Cloudflared @("--protocol", "http2")
    $Url = Get-TunnelUrl 30
}

if ($Url) {
    Set-Content -Path $UrlFile -Value $Url -Encoding ASCII
    Write-Host "[OK] quick tunnel URL: $Url"
    Write-Host "[OK] saved to $UrlFile"
} else {
    # Rescue: network may be down at boot (no Wi-Fi / airplane mode). The last
    # cloudflared (http2) is still running and auto-reconnects; keep watching
    # up to 600s more and save the URL file as soon as it re-registers.
    Write-Host "[..] no URL yet (network down?) - cloudflared keeps retrying; watching 600s ..."
    for ($i = 0; $i -lt 100; $i++) {
        $Url = Get-TunnelUrl 1
        if ($Url) { break }
        Start-Sleep -Seconds 5
    }
    if ($Url) {
        Set-Content -Path $UrlFile -Value $Url -Encoding ASCII
        Write-Host "[OK] quick tunnel URL: $Url (recovered after network came back)"
        Write-Host "[OK] saved to $UrlFile"
        exit 0
    }
    Write-Host "[X] still no URL after 600s watching. Network likely still down - re-run the startup bat when the network is back. Last log lines:"
    if (Test-Path $Log) { Get-Content $Log -Tail 12 }
    exit 1
}
