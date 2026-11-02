#!/usr/bin/env bash
# One-command setup for the job-hunt toolkit. Re-run any time (after editing .env too); it only does what's missing.
#
#   ./setup.sh                    everything: email script, auto-apply agent, Dossier
#   ./setup.sh email|agent|dossier   just one tool
#   ./setup.sh --store-secrets    save Gmail / LinkedIn passwords in the macOS Keychain (instead of .env)
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

if [ ! -f .env ]; then
  cp .env.example .env && chmod 600 .env
  echo "Created .env from .env.example. Fill in your details (sections 1-5 at least), then run ./setup.sh again."
  exit 0
fi
val() { python3 -c "import sys; sys.path.insert(0, '$ROOT'); import toolkit_env as e; print(e.get('$1', '${2:-}'))"; }

if [ "${1:-}" = "--store-secrets" ]; then
  SERVICE="$(val KEYCHAIN_SERVICE job-hunt-toolkit)"
  for key in SENDER_PASS LINKEDIN_PASS; do
    read -r -s -p "$key (leave empty to skip): " v; echo
    [ -n "$v" ] && security add-generic-password -U -s "$SERVICE" -a "$key" -w "$v" && echo "✓ $key saved in Keychain ($SERVICE)"
  done
  exit 0
fi

need() { command -v "$1" >/dev/null || { echo "! $1 is required: $2" >&2; exit 1; }; }
need python3 "brew install python"
need git "xcode-select --install"
WHAT="${1:-all}"

if [ "$WHAT" = all ] || [ "$WHAT" = email ]; then
  echo "== 1/3 Cold-email script"
  python3 -m pip install --quiet --user -r email-script/requirements.txt 2>/dev/null || python3 -m pip install --quiet --break-system-packages --user -r email-script/requirements.txt
  [ -d "/Applications/Google Chrome.app" ] || echo "! Google Chrome is needed (the script drives it to read LinkedIn posts)"
  python3 - <<PY
import sys, os; sys.path.insert(0, "$ROOT"); import toolkit_env as e
problems = [k for k in ("LINKEDIN_EMAIL", "SENDER_EMAIL", "RESUME_PDF", "EMAIL_APPLICATION_BODY") if not e.get(k)]
if e.get("RESUME_PDF") and not os.path.exists(e.get("RESUME_PDF")): problems.append("RESUME_PDF file not found")
for k in ("SENDER_PASS", "LINKEDIN_PASS"):
    if not e.secret(k): problems.append(k + " (run ./setup.sh --store-secrets)")
print("✓ email script ready" if not problems else "! fill in: " + ", ".join(problems))
PY
fi
if [ "$WHAT" = all ] || [ "$WHAT" = agent ]; then
  echo "== 2/3 Auto-apply agent"
  ./autoapply-agent/setup_agent.sh
fi
if [ "$WHAT" = all ] || [ "$WHAT" = dossier ]; then
  echo "== 3/3 Dossier"
  ./dossier/setup_dossier.sh
fi
echo; echo "Done. How to use each tool: README.md → Daily use."
