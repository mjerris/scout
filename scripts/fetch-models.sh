#!/bin/sh
# Download the model files into models/. Whisper downloads itself on first run.
#   Kokoro TTS (~350 MB), Silero VAD v5 (2 MB, MIT), Pipecat smart-turn v3.2 CPU (8 MB, BSD-2).
set -eu
cd "$(dirname "$0")/.."
mkdir -p models

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# fetch NAME URL [SHA256]: download into models/NAME unless present; when a
# sha256 is given, check it (also for a file already present).
fetch() {
  name=$1 url=$2 want=${3:-}
  if [ ! -s "models/$name" ]; then
    echo "fetching $name"
    curl -fL --progress-bar -o "models/$name.part" "$url"
    mv "models/$name.part" "models/$name"
  fi
  if [ -n "$want" ]; then
    got=$(sha256 "models/$name")
    if [ "$got" != "$want" ]; then
      echo "checksum mismatch for models/$name: got $got, want $want" >&2
      mv "models/$name" "models/$name.bad"
      exit 1
    fi
  fi
}

KOKORO=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
fetch kokoro-v1.0.onnx "$KOKORO/kokoro-v1.0.onnx"
fetch voices-v1.0.bin "$KOKORO/voices-v1.0.bin"

# Silero VAD v5.1.2 (snakers4/silero-vad tag v5.1.2).
fetch silero_vad.onnx \
  https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx \
  2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f

# smart-turn v3.2, int8 CPU build (pipecat-ai/smart-turn-v3 at a pinned revision).
fetch smart-turn-v3.2-cpu.onnx \
  https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/f766f81d3cfdf7737ac64aad813d91bbfd56bf93/smart-turn-v3.2-cpu.onnx \
  2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f

echo "models ready"
