# Builds NEXUS_VIDEO_AI_Setup.exe on a Windows machine (GitHub windows-latest or the owner's PC). Needs: Python 3.12 (to
# install wheels into the bundle), Inno Setup 6 (iscc.exe). Every download is checked against installer/windows/pins.json.
# Output: dist\NEXUS_VIDEO_AI_Setup.exe + dist\NEXUS_VIDEO_AI_Setup.exe.sha256 + dist\build_manifest.json
# Never bundles secrets, the vault, NEXUS_HOME, model weights or the .git folder.
param([string]$Python = "python", [string]$Version = "")
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $Root
$pins = Get-Content "$PSScriptRoot\pins.json" -Raw | ConvertFrom-Json
if (-not $Version) { $Version = (& $Python -c "import re;print(re.search(r'VERSION = \""(.+?)\""', open('nexus/config.py').read()).group(1))").Trim() }
$B = Join-Path $Root "build\win"; $D = Join-Path $Root "dist"
if (Test-Path $B) { Remove-Item -Recurse -Force $B }
New-Item -ItemType Directory -Force "$B\app", "$B\dl", $D | Out-Null

function Get-Pinned($pin, $out) {
  Invoke-WebRequest -Uri $pin.url -OutFile $out -UseBasicParsing
  $h = (Get-FileHash $out -Algorithm SHA256).Hash.ToLower()
  if ($h -ne $pin.sha256) { throw "SHA-256 mismatch for $($pin.url): $h != $($pin.sha256)" }
  Write-Host "verified $($pin.url)"
}

# 1. embedded CPython runtime
Get-Pinned $pins.python "$B\dl\python.zip"
Expand-Archive "$B\dl\python.zip" "$B\app\runtime"
$pth = Get-ChildItem "$B\app\runtime\python*._pth" | Select-Object -First 1
Set-Content $pth.FullName -Encoding ascii -Value @("python312.zip", ".", "Lib\site-packages", "..", "import site")

# 2. Python packages (binary wheels only, into the bundle; pytest is a dev tool and is left out). Exception: `srt`
#    (a manim dependency) is pure Python and publishes no wheel at all, so it is built from its sdist (no compiler needed);
#    without this the resolver fails (first real Windows run, 2026-11-13).
$req = Get-Content requirements.txt | Where-Object { $_ -notmatch '^\s*pytest' }
$req | Set-Content "$B\requirements-app.txt"
& $Python -m pip install --only-binary=:all: --no-binary=srt --prefer-binary --no-warn-script-location --target "$B\app\runtime\Lib\site-packages" `
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu -r "$B\requirements-app.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install into the bundle failed" }

# 3. FFmpeg (ffmpeg.exe + ffprobe.exe + licence)
Get-Pinned $pins.ffmpeg "$B\dl\ffmpeg.zip"
Expand-Archive "$B\dl\ffmpeg.zip" "$B\dl\ffmpeg"
$fb = Get-ChildItem "$B\dl\ffmpeg" -Directory | Select-Object -First 1
New-Item -ItemType Directory -Force "$B\app\ffmpeg" | Out-Null
Copy-Item "$($fb.FullName)\bin\ffmpeg.exe", "$($fb.FullName)\bin\ffprobe.exe", "$($fb.FullName)\LICENSE" "$B\app\ffmpeg\"

# 4. application code (tracked files only: no NEXUS_HOME, no secrets, no models)
git archive --format=zip -o "$B\dl\src.zip" HEAD nexus README.md requirements.txt
if ($LASTEXITCODE -ne 0) { throw "git archive failed" }
Expand-Archive "$B\dl\src.zip" "$B\app"
Copy-Item "$PSScriptRoot\NEXUS.pyw" "$B\app\NEXUS.pyw"
$commit = (git rev-parse HEAD).Trim()
@{version = $Version; commit = $commit; data_dir = '%LOCALAPPDATA%\NEXUS\data'} | ConvertTo-Json | Set-Content "$B\app\install.json" -Encoding utf8
foreach ($bad in @(".nexus", "secrets", "vault.key", ".env")) {
  if (Get-ChildItem "$B\app" -Recurse -Force -Filter $bad -ErrorAction SilentlyContinue) { throw "refusing to package '$bad'" }
}

# 5. bundle smoke test before packaging: imports, FFmpeg, CLI
$env:PATH = "$B\app\ffmpeg;$env:PATH"
& "$B\app\runtime\python.exe" -c "import nexus, fastapi, uvicorn, cryptography, numpy, cv2, onnxruntime; from nexus import config; print('bundle ok', config.VERSION, config.installed())"
if ($LASTEXITCODE -ne 0) { throw "bundle import check failed" }

# 6. installer
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
& $iscc "/DAppVersion=$Version" "/DSourceDir=$B\app" "/DOutDir=$D" "$PSScriptRoot\NEXUS.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
$exe = "$D\NEXUS_VIDEO_AI_Setup.exe"
$h = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
"$h  NEXUS_VIDEO_AI_Setup.exe" | Set-Content "$exe.sha256" -Encoding ascii
@{version = $Version; commit = $commit; sha256 = $h; bytes = (Get-Item $exe).Length; python = $pins.python.version; ffmpeg = $pins.ffmpeg.version;
  built_at = (Get-Date).ToUniversalTime().ToString("o"); os = [System.Environment]::OSVersion.VersionString} | ConvertTo-Json | Set-Content "$D\build_manifest.json" -Encoding utf8
Write-Host "built $exe ($h)"
