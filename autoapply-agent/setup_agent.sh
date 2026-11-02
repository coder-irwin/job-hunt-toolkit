#!/usr/bin/env bash
# Sets up the auto-apply browser extension: downloads it from its original repository (AGENT_REPO in .env), prefills
# its profile from your .env, and gets the local Ollama model it uses. Safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
val() { python3 -c "import sys; sys.path.insert(0, '$ROOT'); import toolkit_env as e; print(e.get('$1', '${2:-}'))"; }
AGENT_DIR="$(val AGENT_DIR ~/job-hunt-agent)"
REPO="$(val AGENT_REPO)"
MODEL="$(val OLLAMA_MODEL qwen2.5:7b-instruct)"

echo "== Auto-apply extension → $AGENT_DIR"
if [ ! -f "$AGENT_DIR/manifest.json" ]; then
  [ -n "$REPO" ] || { echo "! Set AGENT_REPO in .env to the extension's git URL (see README → Auto-apply agent)." >&2; exit 1; }
  git clone --quiet "$REPO" "$AGENT_DIR"
  echo "✓ downloaded the extension"
else
  echo "✓ extension already there (update it with: git -C \"$AGENT_DIR\" pull)"
fi
git -C "$AGENT_DIR" checkout --quiet -- profile-default.js 2>/dev/null || true
python3 "$HERE/build_profile.py"
git -C "$AGENT_DIR" update-index --skip-worktree profile-default.js 2>/dev/null || true   # keep `git pull` working

echo "== Ollama (the extension's local AI)"
command -v ollama >/dev/null || { echo "! Install Ollama: brew install ollama (or https://ollama.com/download), then re-run."; exit 1; }
# The extension calls Ollama from chrome-extension:// pages, which Ollama blocks unless allowed.
if [ "$(uname)" = "Darwin" ] && [ "$(launchctl getenv OLLAMA_ORIGINS || true)" != "chrome-extension://*" ]; then
  launchctl setenv OLLAMA_ORIGINS "chrome-extension://*"
  echo "✓ allowed Chrome extensions to reach Ollama (restart the Ollama app once for it to apply)"
fi
pgrep -xq ollama || pgrep -fq "ollama serve" || (open -a Ollama 2>/dev/null || (ollama serve >/dev/null 2>&1 &)); sleep 2
ollama show "$MODEL" >/dev/null 2>&1 || ollama pull "$MODEL"
echo "✓ model $MODEL ready"

cat <<MSG

Next (once): load the extension in Chrome
  1. Open chrome://extensions and switch on "Developer mode" (top right)
  2. Click "Load unpacked" and choose: $AGENT_DIR
  3. Open its side panel. Your profile is prefilled from .env: upload your resume, check the fields, press Get started.
  4. Set job titles, cities, sites and daily limits in the panel (README → Auto-apply agent has suggested values).
After editing .env: run ./setup.sh again, then click reload ↻ on the extension card.
MSG
