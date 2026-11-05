@echo off
rem Local Dots - Cloudflare QUICK tunnel autostart (ticket-less / credential-free).
rem Startup entry. Runs the small PowerShell driver hidden: it kills any stale
rem cloudflared, wipes cf_quick.log, starts a fresh hidden tunnel to
rem 127.0.0.1:8000, polls for the fresh URL (QUIC first, HTTP/2 fallback) and
rem writes it to cf_quick_url.txt. Progress: cf_autostart.log. ASCII-only.
setlocal
set "REPO=D:\Python Project\local_dots"
if not exist "%REPO%\cloudflared.exe" exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%REPO%\scripts\cloudflare_quick_autostart.ps1" > "%REPO%\cf_autostart.log" 2>&1
endlocal
