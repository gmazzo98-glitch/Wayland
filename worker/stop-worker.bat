@echo off
set "VDIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process node -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($env:VDIR, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { & taskkill /F /T /PID $_.Id | Out-Null }"
echo The Vienna Crawler Worker has been stopped. It starts again when you next log in,
echo or when you use "Start Vienna Crawler Worker".
timeout /t 5 >nul
