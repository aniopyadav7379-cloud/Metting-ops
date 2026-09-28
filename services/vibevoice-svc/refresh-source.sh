#!/usr/bin/env bash
# Refresh the bundled VibeVoice source tree into ./VibeVoice-src/.
#
# We download a tarball rather than `git clone` because docker buildkit's
# network on midboy1 stalls reaching github.com over IPv4 inside the build
# sandbox; the tarball pulls cleanly over the host's normal egress path and
# then ends up baked into the image via COPY.
#
# Usage: ./refresh-source.sh [<branch-or-tag>]   (default: main)
set -euo pipefail

REF="${1:-main}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "Fetching vibevoice-community/VibeVoice@${REF} tarball..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -fL -o "$TMP/vv.tar.gz" \
    "https://github.com/vibevoice-community/VibeVoice/archive/refs/heads/${REF}.tar.gz"

rm -rf "$HERE/VibeVoice-src"
mkdir -p "$HERE/VibeVoice-src"
tar -xzf "$TMP/vv.tar.gz" -C "$TMP/"
mv "$TMP/VibeVoice-${REF}"/* "$HERE/VibeVoice-src/"
mv "$TMP/VibeVoice-${REF}"/.[!.]* "$HERE/VibeVoice-src/" 2>/dev/null || true

echo "Refreshed $HERE/VibeVoice-src/ from ${REF}."
ls "$HERE/VibeVoice-src/" | head
