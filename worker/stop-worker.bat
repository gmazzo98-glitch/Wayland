@echo off
set "VDIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process node -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($env:VDIR, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { & taskkill /F /T /PID $_.Id | Out-Null }"
rem The site evidence reader runs in its own process and would otherwise keep going. Ask it to stop cleanly;
rem the run is still wanted, so it resumes by itself the next time the worker starts.
if exist "%USERPROFILE%\SiteEvidenceOutput\control.json" echo stopped together with the worker> "%USERPROFILE%\SiteEvidenceOutput\STOP"
echo The Vienna Crawler Worker has been stopped. It starts again when you next log in,
echo or when you use "Start Vienna Crawler Worker".
timeout /t 5 >nul
