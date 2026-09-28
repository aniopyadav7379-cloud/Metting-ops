#!/usr/bin/env bash
# Single source of truth for the release version is the VERSION file at the repo root.
# Everything else (README badge, README status heading, frontend/package.json) is derived.
#
#   scripts/sync-version.sh          rewrite derived files from VERSION
#   scripts/sync-version.sh --check  exit 1 if anything has drifted (for CI / pre-release)
#
# Added 2026-07-30 after the README badge (v3.57.3) drifted from the real release (v3.58.0).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[ -f VERSION ] || { echo "error: no VERSION file at repo root" >&2; exit 2; }
VERSION="$(tr -d '[:space:]' < VERSION)"
[ -n "$VERSION" ] || { echo "error: VERSION is empty" >&2; exit 2; }

MODE="${1:-write}"

python3 - "$VERSION" "$MODE" <<'PY'
import json, re, sys, pathlib

version, mode = sys.argv[1], sys.argv[2]
check = (mode == "--check")
minor = ".".join(version.split(".")[:2])          # 3.58.0 -> 3.58
drift = []

def edit(path, pattern, replacement, label):
    p = pathlib.Path(path)
    if not p.exists():
        return
    old = p.read_text()
    new, n = re.subn(pattern, replacement, old, count=1)
    if n == 0:
        drift.append(f"{label}: pattern not found in {path} (manual check needed)")
        return
    if new != old:
        drift.append(f"{label}: {path} is out of date")
        if not check:
            p.write_text(new)

# README release badge:  ...badge/release-v3.58.0-8b5cf6...
edit("README.md",
     r"(badge/release-v)[0-9]+\.[0-9]+\.[0-9]+(-)",
     rf"\g<1>{version}\g<2>",
     "README badge")

# README status heading:  "... — v3.58.x:"
edit("README.md",
     r"(— v)[0-9]+\.[0-9]+(\.x:)",
     rf"\g<1>{minor}\g<2>",
     "README status heading")

# frontend/package.json
pkg = pathlib.Path("frontend/package.json")
if pkg.exists():
    data = json.loads(pkg.read_text())
    if data.get("version") != version:
        drift.append(f"frontend/package.json: {data.get('version')} != {version}")
        if not check:
            data["version"] = version
            pkg.write_text(json.dumps(data, indent=2) + "\n")

if check:
    if drift:
        print("version drift detected (VERSION = %s):" % version)
        for d in drift:
            print("  -", d)
        sys.exit(1)
    print(f"version consistent: {version}")
else:
    if drift:
        print(f"synced to {version}:")
        for d in drift:
            print("  -", d)
    else:
        print(f"already consistent: {version}")
PY
