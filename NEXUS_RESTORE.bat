@echo off
title NEXUS VIDEO AI - Restore
call "%~dp0scripts\windows\_env.bat"
if errorlevel 1 (pause & exit /b 1)
echo  This restores the latest verified backup into a NEW folder.
echo  Your current data folder is never overwritten.
echo.
set "TARGET=%CD%\.nexus-restored-%RANDOM%"
set /p TARGET_IN= Folder to restore into [%TARGET%]: 
if not "%TARGET_IN%"=="" set "TARGET=%TARGET_IN%"
"%NEXUS_PY%" -m nexus restore "%TARGET%" %*
if errorlevel 1 (
  echo.
  echo  RESTORE FAILED or is incomplete - see above. Your current data was not changed.
  pause
  exit /b 1
)
echo.
echo  Restored and verified. To use it:
echo    1. Run NEXUS_STOP.bat
echo    2. Rename the folder ".nexus" to ".nexus-old" and rename "%TARGET%" to ".nexus"
echo    3. Run NEXUS_START.bat
pause
