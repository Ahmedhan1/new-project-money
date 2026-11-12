#!/usr/bin/env bash
# One-command setup for Ubuntu/Debian. Everything installed here is free software; nothing needs a card or an account.
#   ./scripts/setup.sh            full install + model download (~7 GB)
#   NEXUS_SKIP_MODELS=1 ./scripts/setup.sh   install only; models are then fetched on first use
set -euo pipefail
cd "$(dirname "$0")/.."
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
echo "== system packages (FFmpeg, Blender, eSpeak NG, fonts, Cairo/Pango for Manim, build tools for llama.cpp)"
$SUDO apt-get update -qq
DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq ffmpeg blender espeak-ng fonts-noto-core fonts-noto-cjk fonts-dejavu-core tesseract-ocr tesseract-ocr-ara tesseract-ocr-chi-sim tesseract-ocr-hin tesseract-ocr-rus tesseract-ocr-deu \
  libcairo2-dev libpango1.0-dev pkg-config cmake build-essential python3-venv curl
echo "== python environment"
python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt      # exact tested versions: requirements.lock
if [ -z "${NEXUS_SKIP_MODELS:-}" ]; then
  echo "== models (Whisper small, Qwen3-4B, MADLAD-400 3B, Piper voices) - checks free disk space first"
  .venv/bin/python -m nexus models fetch
fi
.venv/bin/python -m nexus doctor
echo "Start the app:  .venv/bin/python -m nexus serve   then open http://127.0.0.1:8765"
