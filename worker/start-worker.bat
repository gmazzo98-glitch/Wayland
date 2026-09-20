@echo off
wscript.exe "%~dp0worker\start-worker.vbs"
echo The Vienna Crawler Worker is running in the background.
echo You can close this window.
timeout /t 4 >nul
