@echo off
title NEXUS VIDEO AI - Backup
call "%~dp0scripts\windows\_env.bat"
if errorlevel 1 (pause & exit /b 1)
"%NEXUS_PY%" -m nexus backup --drill %*
if errorlevel 1 (
  echo.
  echo  BACKUP FAILED or could not be verified. Your local files are untouched.
  pause
  exit /b 1
)
pause
