@echo off
title NEXUS VIDEO AI - Resume
call "%~dp0scripts\windows\_env.bat"
if errorlevel 1 (pause & exit /b 1)
"%NEXUS_PY%" -m nexus resume %*
if errorlevel 1 (
  echo.
  echo  The command reported a problem. Read the messages above.
  pause
  exit /b 1
)
pause
