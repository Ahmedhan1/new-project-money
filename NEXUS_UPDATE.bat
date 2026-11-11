@echo off
title NEXUS VIDEO AI - Update
call "%~dp0scripts\windows\_env.bat"
if errorlevel 1 (pause & exit /b 1)
echo [1/5] Stopping NEXUS (running jobs resume from their checkpoint afterwards)...
"%NEXUS_PY%" -m nexus stop
echo [2/5] Safety backup of the database and project metadata...
"%NEXUS_PY%" -m nexus backup --tiers state,metadata
if errorlevel 1 (
  echo  The safety backup failed, so the update was NOT started. Nothing was changed.
  pause
  exit /b 1
)
echo [3/5] Downloading the latest version from GitHub (fast-forward only, never overwrites local changes)...
git pull --ff-only
if errorlevel 1 (
  echo  Could not update automatically: you have local changes or no network. Nothing was changed.
  pause
  exit /b 1
)
echo [4/5] Updating Python packages...
"%NEXUS_PY%" -m pip install -q --prefer-binary --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu -r requirements.txt
echo [5/5] Checking the installation...
"%NEXUS_PY%" -m nexus doctor
echo.
echo  Update finished. Your projects, models and settings were not touched. Start with NEXUS_START.bat.
pause
