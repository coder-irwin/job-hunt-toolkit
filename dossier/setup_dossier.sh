#!/usr/bin/env bash
# Sets up Dossier (self-hosted resume + cover-letter tailoring and application tracker) in Docker, using your local
# Ollama model, and loads your profile. Safe to re-run: existing secrets and data are kept.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
val() { python3 -c "import sys; sys.path.insert(0, '$ROOT'); import toolkit_env as e; print(e.get('$1', '${2:-}'))"; }
DIR="$(val DOSSIER_DIR ~/dossier)"
PORT="$(val DOSSIER_PORT 3000)"
MODEL="$(val DOSSIER_AI_MODEL qwen2.5:7b-instruct)"
SERVICE="$(val KEYCHAIN_SERVICE job-hunt-toolkit)"
IMAGE=ghcr.io/aientech/dossier:latest

echo "== Docker"
if ! docker info >/dev/null 2>&1; then
  if command -v colima >/dev/null; then
    echo "starting Colima (Apple Silicon: Rosetta lets the amd64 image print PDFs)…"
    colima start --vm-type vz --vz-rosetta --memory 6 --cpu 4 >/dev/null
  else
    echo "! Docker isn't running. Install Docker Desktop, or: brew install colima docker docker-compose && re-run." >&2; exit 1
  fi
fi
echo "✓ Docker ready"

mkdir -p "$DIR/applications"
for f in compose.yml compose.override.yml; do [ -f "$DIR/$f" ] || cp "$HERE/$f" "$DIR/$f"; done

if [ ! -f "$DIR/.env" ]; then
  echo "== First run: creating Dossier's secrets and your login password"
  PW="$(openssl rand -hex 12)"
  HASH="$(printf '%s\n%s\n' "$PW" "$PW" | docker run --rm -i --platform linux/amd64 "$IMAGE" hash-password 2>/dev/null | grep -o 'argon2id[^[:space:]]*' | tail -1)"
  [ -n "$HASH" ] || { echo "! couldn't hash the password" >&2; exit 1; }
  if command -v security >/dev/null; then
    security add-generic-password -U -s "$SERVICE" -a DOSSIER_PASSWORD -w "$PW"
    echo "✓ login password saved in the macOS Keychain (service $SERVICE, account DOSSIER_PASSWORD)"
  else
    echo "Your Dossier login password (shown once, keep it; or put it in .env as DOSSIER_PASSWORD): $PW"
  fi
  umask 077
  cat > "$DIR/.env" <<ENV
POSTGRES_PASSWORD=$(openssl rand -hex 16)
DOSSIER_SECRET=$(openssl rand -hex 24)
DOSSIER_PASSWORD='$HASH'
DOSSIER_PORT=$PORT
TZ=Asia/Kolkata
DOSSIER_TELEMETRY=off
DOSSIER_AI_PROVIDER=local
DOSSIER_AI_URL=http://host.docker.internal:11434
DOSSIER_AI_MODEL=$MODEL
DOSSIER_AI_TIMEOUT_SECONDS=600
ENV
else
  echo "✓ keeping existing $DIR/.env (secrets and login)"
fi

echo "== Starting Dossier"
(cd "$DIR" && docker compose up -d >/dev/null)
for i in $(seq 1 60); do curl -sf -o /dev/null "http://localhost:$PORT/api/health" && break; sleep 3; done
curl -sf -o /dev/null "http://localhost:$PORT/api/health" && echo "✓ Dossier running: http://localhost:$PORT" || { echo "! Dossier didn't become healthy: cd $DIR && docker compose logs app" >&2; exit 1; }

command -v ollama >/dev/null && (ollama show "$MODEL" >/dev/null 2>&1 || ollama pull "$MODEL") || echo "! install Ollama and pull $MODEL for Dossier's AI"

PROFILE="$(val DOSSIER_PROFILE_JSON ./dossier/profile.json)"
case "$PROFILE" in /*) ;; *) PROFILE="$ROOT/${PROFILE#./}";; esac
if [ -f "$PROFILE" ]; then
  python3 "$HERE/import_profile.py"
else
  echo "! No profile yet: cp dossier/profile.example.json dossier/profile.json, fill it in, re-run (or fill the Profile page in Dossier)."
fi
