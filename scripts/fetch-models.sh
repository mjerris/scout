#!/bin/sh
# Download the Kokoro TTS model files into models/. Whisper downloads itself on first run.
set -eu
cd "$(dirname "$0")/.."
BASE=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
mkdir -p models
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
  if [ ! -s "models/$f" ]; then
    echo "fetching $f"
    curl -fL --progress-bar -o "models/$f.part" "$BASE/$f"
    mv "models/$f.part" "models/$f"
  fi
done
echo "models ready"
