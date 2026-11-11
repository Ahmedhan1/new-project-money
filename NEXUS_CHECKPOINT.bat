@echo off
title NEXUS VIDEO AI - Checkpoint
call "%~dp0scripts\windows\_env.bat"
if errorlevel 1 (pause & exit /b 1)
"%NEXUS_PY%" -m nexus checkpoint %*
if errorlevel 1 (
  echo.
  echo  Checkpoint was blocked or could not be pushed - see above. Nothing was lost.
  pause
  exit /b 1
)
pause
