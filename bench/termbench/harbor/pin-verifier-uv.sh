#!/bin/bash
# Copy a Terminal-Bench 2.x dataset with each verifier using a mounted uv 0.9.5 + Python 3.13, not a GitHub download.
# Usage: pin-verifier-uv.sh <dataset dir> <output parent dir> [uv dir, default /tmp/opencode/sc-uv]
# Then mount <uv dir> at /opt/sc-uv in the job (bind) and point the dataset path at the copy.
set -euo pipefail
src=$1 out=$2 uvdir=${3:-/tmp/opencode/sc-uv}
image=${SC_UV_IMAGE:-ubuntu:24.04}
if [ ! -x "$uvdir/bin/uv" ]; then
  mkdir -p "$uvdir/bin"
  curl -LsSf https://github.com/astral-sh/uv/releases/download/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz \
    | tar xz --strip-components=1 -C "$uvdir/bin"
fi
# Installed in a container so Python's paths are the /opt/sc-uv ones the verifier sees.
ls -d "$uvdir"/python/cpython-3.13* >/dev/null 2>&1 \
  || docker run --rm -v "$uvdir:/opt/sc-uv" -e UV_PYTHON_INSTALL_DIR=/opt/sc-uv/python "$image" /opt/sc-uv/bin/uv python install 3.13
mkdir -p "$out" && cp -a "$src" "$out/"
n=0
for f in "$out/$(basename "$src")"/*/tests/test.sh; do
  grep -q '^curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh$' "$f" || continue
  sed -i 's#^curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh$#export PATH=/opt/sc-uv/bin:$PATH UV_PYTHON_INSTALL_DIR=/opt/sc-uv/python UV_PYTHON_DOWNLOADS=never  \# smarter-compact bench: pinned uv 0.9.5 + Python mounted, no GitHub download#; s#^source \$HOME/.local/bin/env$#true#' "$f"
  n=$((n + 1))
done
echo "patched $n verifiers in $out/$(basename "$src")"
