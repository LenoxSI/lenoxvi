#!/usr/bin/env bash
# fetch_kokoro_models.sh — download the Kokoro TTS model files once, so the
# bot never downloads anything at runtime (it refuses to: a missing file is
# a clear error naming this script).
#
# Run once after `pip install -r requirements.txt`, as the user the
# bot runs as:
#   bash scripts/fetch_kokoro_models.sh
#
# Files land in ${XDG_CACHE_HOME:-~/.cache}/lenoxvi/kokoro, about 354 MB.
# Each one is checked against a pinned sha256; a mismatch is refused and
# deleted. Safe to re-run: files that already match are left alone.
set -euo pipefail

DIR="${XDG_CACHE_HOME:-$HOME/.cache}/lenoxvi/kokoro"
URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES=(
  "kokoro-v1.0.onnx 7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
  "voices-v1.0.bin bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
)

command -v sha256sum >/dev/null || { echo "sha256sum not found" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl not found" >&2; exit 1; }
mkdir -p "$DIR"

for entry in "${FILES[@]}"; do
  read -r name sum <<<"$entry"
  dest="$DIR/$name"
  if [ -f "$dest" ] && [ "$(sha256sum "$dest" | cut -d' ' -f1)" = "$sum" ]; then
    echo "ok  $name (already there)"
    continue
  fi
  echo "..  downloading $name"
  tmp="$dest.part"
  rm -f "$tmp"
  curl -fL --retry 3 -o "$tmp" "$URL/$name"
  got="$(sha256sum "$tmp" | cut -d' ' -f1)"
  if [ "$got" != "$sum" ]; then
    rm -f "$tmp"
    echo "FAILED $name: sha256 $got, expected $sum (file deleted)" >&2
    exit 1
  fi
  mv -f "$tmp" "$dest"
  echo "ok  $name"
done
echo "ok  Kokoro model files in $DIR"
