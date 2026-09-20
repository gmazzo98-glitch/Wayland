@echo off
title Uninstall Vienna Crawlers
set "VDIR=%~dp0"
copy /y "%~dp0uninstall.ps1" "%TEMP%\vienna-uninstall.ps1" >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\vienna-uninstall.ps1"
