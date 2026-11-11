@echo off
title NEXUS VIDEO AI - Stop
call "%~dp0scripts\windows\_env.bat"
if errorlevel 1 (pause & exit /b 1)
"%NEXUS_PY%" -m nexus stop %*
if errorlevel 1 (
  echo.
  echo  The command reported a problem. Read the messages above.
  pause
  exit /b 1
)
