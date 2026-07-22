#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAYER_DIR="${ROOT_DIR}/layers/ffmpeg"
BUILD_DIR="${LAYER_DIR}/build"
ZIP_PATH="${LAYER_DIR}/ffmpeg-layer.zip"
DOWNLOAD_URL="${FFMPEG_STATIC_URL:-https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz}"

mkdir -p "${BUILD_DIR}" "${LAYER_DIR}/bin"
rm -rf "${BUILD_DIR:?}"/* "${LAYER_DIR}/bin/ffmpeg" "${LAYER_DIR}/bin/ffprobe" "${ZIP_PATH}"

echo "Downloading Linux static ffmpeg build..."
curl -L "${DOWNLOAD_URL}" -o "${BUILD_DIR}/ffmpeg-static.tar.xz"

echo "Extracting..."
tar -xJf "${BUILD_DIR}/ffmpeg-static.tar.xz" -C "${BUILD_DIR}"

EXTRACTED_DIR="$(find "${BUILD_DIR}" -maxdepth 1 -type d -name 'ffmpeg-*static' | head -n 1)"
if [[ -z "${EXTRACTED_DIR}" ]]; then
  echo "Could not find extracted ffmpeg static directory." >&2
  exit 1
fi

cp "${EXTRACTED_DIR}/ffmpeg" "${LAYER_DIR}/bin/ffmpeg"
cp "${EXTRACTED_DIR}/ffprobe" "${LAYER_DIR}/bin/ffprobe"
chmod +x "${LAYER_DIR}/bin/ffmpeg" "${LAYER_DIR}/bin/ffprobe"

echo "Creating Lambda layer zip..."
(
  cd "${LAYER_DIR}"
  zip -qr "${ZIP_PATH}" bin
)

echo "Created ${ZIP_PATH}"
echo "Layer contents:"
unzip -l "${ZIP_PATH}" | sed -n '1,12p'
