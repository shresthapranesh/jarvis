#!/usr/bin/env bash
# Build a single-folder Jarvis bundle for macOS Apple Silicon.
# Output: dist/jarvis-macos-arm64.tar.gz

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: build_macos.sh must run on macOS" >&2
    exit 1
fi

echo "==> cleaning previous build artifacts"
rm -rf build dist/jarvis dist/jarvis-macos-arm64.tar.gz

echo "==> installing Python deps (with dev group for pyinstaller)"
uv sync --group dev

echo "==> building frontend"
pushd frontend >/dev/null
pnpm install --frozen-lockfile
pnpm build
popd >/dev/null

if [[ ! -f static/dist/index.html ]]; then
    echo "error: frontend build did not produce static/dist/index.html" >&2
    exit 1
fi

echo "==> running PyInstaller"
uv run pyinstaller --noconfirm jarvis.spec

if [[ ! -x dist/jarvis/jarvis ]]; then
    echo "error: PyInstaller did not produce dist/jarvis/jarvis" >&2
    exit 1
fi

echo "==> packaging tarball"
tar -czf dist/jarvis-macos-arm64.tar.gz -C dist jarvis

echo
echo "Done. Bundle: $(du -sh dist/jarvis | awk '{print $1}')"
echo "Tarball:     $(du -h dist/jarvis-macos-arm64.tar.gz | awk '{print $1}')  → dist/jarvis-macos-arm64.tar.gz"
echo
echo "Try it:      ./dist/jarvis/jarvis start --port 8765"
