# NEXUS VIDEO AI - Windows setup. Idempotent: safe to run again at any time. Never deletes project data.
# Everything installed here is free software; nothing needs an account or a payment method.
#   NEXUS_SETUP.bat               full install + model download (about 7 GB)
#   NEXUS_SETUP.bat -SkipModels   install only; models are fetched on first use
param([switch]$SkipModels)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\..")
function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "`nSETUP STOPPED: $m" -ForegroundColor Red; exit 1 }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function RefreshPath { $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") }
function Winget($id, $name) {
  if (-not (Have winget)) { return $false }
  Write-Host "   installing $name with winget (free, official package)..."
  winget install -e --id $id --accept-source-agreements --accept-package-agreements --scope user | Out-Host
  RefreshPath
  return $true
}

Step "Disk space"
$drive = (Get-Item .).PSDrive
$freeGB = [math]::Round($drive.Free / 1GB, 1)
Write-Host "   $freeGB GB free on $($drive.Name):"
if ($freeGB -lt 4) { Fail "at least 4 GB of free disk space is needed (15 GB with all models)." }
if ($freeGB -lt 15 -and -not $SkipModels) { Write-Host "   less than 15 GB free: models will be downloaded on first use instead of now." -ForegroundColor Yellow; $SkipModels = $true }

Step "Python 3.10 - 3.13"
$py = $null
foreach ($cand in @(@("py", "-3.12"), @("py", "-3.13"), @("py", "-3.11"), @("py", "-3.10"), @("python"))) {
  try {
    $exe = $cand[0]; $rest = @($cand | Select-Object -Skip 1)
    $v = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $v -match '^3\.(10|11|12|13)$') { $py = $cand; break }
  } catch {}
}
if (-not $py) {
  if (Winget "Python.Python.3.12" "Python 3.12") { $py = @("py", "-3.12") }
  else { Fail "Python was not found. Install Python 3.12 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run NEXUS_SETUP.bat again." }
}
Write-Host "   using: $($py -join ' ')"

Step "FFmpeg (video engine)"
if (-not (Have ffmpeg)) { [void](Winget "Gyan.FFmpeg" "FFmpeg") }
if (-not (Have ffmpeg)) { Fail "FFmpeg is not on PATH. Install it (winget install Gyan.FFmpeg), reopen this window and run NEXUS_SETUP.bat again." }
Write-Host "   $((ffmpeg -version | Select-Object -First 1))"

Step "Optional tools (NEXUS works without them; the related features stay disabled)"
if (-not (Have git)) { Write-Host "   Git not found - NEXUS_UPDATE and NEXUS_CHECKPOINT need it (winget install Git.Git)" -ForegroundColor Yellow }
if (-not (Have blender)) { Write-Host "   Blender not found - 3D animation disabled (winget install BlenderFoundation.Blender, then add it to PATH)" -ForegroundColor Yellow }
if (-not (Have espeak-ng)) { Write-Host "   eSpeak NG not found - fallback voices disabled (winget install eSpeak-NG.eSpeak-NG)" -ForegroundColor Yellow }
if (-not (Have rclone)) { Write-Host "   rclone not found - only local/external-drive backups available (winget install Rclone.Rclone)" -ForegroundColor Yellow }

Step "Private Python environment (.venv)"
$exe = $py[0]; $rest = @($py | Select-Object -Skip 1)
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $exe @rest -m venv .venv; if ($LASTEXITCODE -ne 0) { Fail "could not create .venv" } }
$vpy = ".venv\Scripts\python.exe"
& $vpy -m pip install -q -U pip
Write-Host "   installing packages (first run takes several minutes)..."
# Prebuilt CPU wheels for llama-cpp-python, so no C++ compiler is needed.
& $vpy -m pip install -q --prefer-binary --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "a Python package failed to install - the error is printed above." }

if (-not $SkipModels) {
  Step "Models (speech recognition, language model, translation, voices) - resumable download"
  & $vpy -m nexus models fetch
  if ($LASTEXITCODE -ne 0) { Write-Host "   model download did not finish; it resumes automatically the next time a model is needed." -ForegroundColor Yellow }
}

Step "Doctor"
& $vpy -m nexus doctor
Write-Host "`n(Lines marked BAD above name what is missing. Optional tools may be BAD without blocking NEXUS_START.)"
exit 0
