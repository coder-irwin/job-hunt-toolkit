#!/usr/bin/env python3
"""Loads your profile (DOSSIER_PROFILE_JSON, Dossier's export format) into Dossier. Only the profile is replaced:
the current data is exported first, the profile swapped in, and everything else (applications, themes, settings)
imported back unchanged. Dossier also keeps its own backup before every import.

  python3 dossier/import_profile.py            # import
  python3 dossier/import_profile.py --check    # only validate the file and show what it contains
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dossier_apply as d  # noqa: E402  (sign-in and HTTP helpers)
from dossier_apply import env  # noqa: E402

path = Path(env.get("DOSSIER_PROFILE_JSON", "./dossier/profile.json"))
if not path.is_absolute():
    path = Path(__file__).resolve().parent.parent / path
if not path.exists():
    sys.exit(f"No profile at {path}. Copy dossier/profile.example.json to that path and fill it in.")
data = json.loads(path.read_text(encoding="utf-8"))
profile = data.get("profile", data)
summary = {k: len(v) for k, v in profile.items() if isinstance(v, list)}
print(f"Profile {path.name}: {summary}")
if "--check" in sys.argv:
    sys.exit(0)

d.sign_in()
status, _, raw = d.req("GET", "/api/export")
if status != 200:
    sys.exit(f"Export failed (HTTP {status}): is Dossier running at {d.BASE}?")
current = json.loads(raw)
current["profile"] = profile
status, _, raw = d.req("POST", "/api/import", json.dumps(current).encode(), {"Content-Type": "application/json"}, timeout=300)
print(f"Import: HTTP {status} {raw.decode()[:300]}")
if status != 200:
    sys.exit(1)
_, _, raw = d.req("GET", "/api/export")
after = json.loads(raw)
print("Now in Dossier:", {k: len(v) for k, v in after["profile"].items() if isinstance(v, list)}, "| applications kept:", len(after.get("applications", [])))
