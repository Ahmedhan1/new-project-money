@echo off
title NEXUS VIDEO AI - Start
call "%~dp0scripts\windows\_env.bat"
if errorlevel 1 (pause & exit /b 1)
"%NEXUS_PY%" -m nexus start %*
if errorlevel 1 (
  echo.
  echo  NEXUS did not start. The reason and the fix are printed above.
  pause
  exit /b 1
)
