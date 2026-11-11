@echo off
rem Shared by every NEXUS_*.bat launcher: go to the project folder and locate its private Python environment.
cd /d "%~dp0..\.."
set "NEXUS_PY=%CD%\.venv\Scripts\python.exe"
set PYTHONUTF8=1
if not exist "%NEXUS_PY%" (
  echo.
  echo  NEXUS is not installed on this computer yet.
  echo  Double-click NEXUS_SETUP.bat first ^(one time, needs internet^).
  echo.
  exit /b 1
)
exit /b 0
