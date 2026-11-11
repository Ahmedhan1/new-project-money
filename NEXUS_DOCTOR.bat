@echo off
title NEXUS VIDEO AI - Doctor
call "%~dp0scripts\windows\_env.bat"
if errorlevel 1 (pause & exit /b 1)
"%NEXUS_PY%" -m nexus doctor %*
if errorlevel 1 (
  echo.
  echo  Doctor found problems - see the lines marked BAD above.
  pause
  exit /b 1
)
pause
