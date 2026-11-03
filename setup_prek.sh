#!/usr/bin/env bash
# Install prek git hooks (hygiene + Conventional Commits on commit-msg).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "Smarter Compact — prek setup"

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: uv is required (https://docs.astral.sh/uv/)"
  exit 1
fi
echo "✓ uv: $(uv --version)"

echo ""
echo "📥 Installing prek (uv tool)..."
uv tool install --force "prek>=0.5,<0.6"

if ! command -v prek >/dev/null 2>&1; then
  echo "Error: prek not on PATH — add: export PATH=\"$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin"):\$PATH\""
  exit 1
fi
echo "✓ prek: $(prek --version)"

echo ""
echo "📦 npm ci..."
npm ci

echo ""
echo "🔧 Installing git hooks (pre-commit + commit-msg for Conventional Commits)..."
prek install --prepare-hooks --hook-type pre-commit --hook-type commit-msg -f

echo ""
echo "🚀 Running prek on all files (may auto-fix whitespace)..."
if prek run --all-files; then
  echo "✅ prek checks passed."
else
  echo "⚠️  Some checks failed or auto-fixed files — review and commit."
  exit 1
fi

echo ""
echo "Commit messages: feat:, fix:, docs:, chore:, etc. (see cliff.toml / prepare-release workflow)"
echo "Useful: prek run | prek run --all-files | prek update --freeze"
