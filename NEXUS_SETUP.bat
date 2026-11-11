@echo off
title NEXUS VIDEO AI - Setup
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\setup.ps1" %*
if errorlevel 1 (
  echo.
  echo  Setup did not finish. Fix the problem printed above and run NEXUS_SETUP.bat again - it is safe to re-run.
  pause
  exit /b 1
)
echo.
echo  Setup finished. Double-click NEXUS_START.bat to open NEXUS.
pause
