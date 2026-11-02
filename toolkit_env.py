"""Reads the toolkit's .env (one file for all three tools). Standard library only.

Format: KEY=value per line; # starts a comment line. Multi-line values go in double quotes and may span lines:
    EMAIL_APPLICATION_BODY="Dear Hiring Team,

    ..."
Secrets (passwords) are read from the environment first, then .env, then the macOS Keychain
(service = KEYCHAIN_SERVICE, account = the key name), so they never have to be written in .env.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("TOOLKIT_ENV", ROOT / ".env"))


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if val.startswith('"') and not (len(val) > 1 and val.endswith('"')):
            buf = [val[1:]]
            while i < len(lines) and not lines[i].rstrip().endswith('"'):
                buf.append(lines[i]); i += 1
            if i < len(lines):
                buf.append(lines[i].rstrip()[:-1]); i += 1
            val = "\n".join(buf)
        elif len(val) > 1 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


_VALUES = _parse(ENV_FILE.read_text(encoding="utf-8")) if ENV_FILE.exists() else {}


def get(key: str, default: str = "", _depth: int = 0) -> str:
    """A value, with ${OTHER_KEY} references filled in and a leading ~ expanded."""
    v = os.environ.get(key, _VALUES.get(key, default))
    if _depth < 3:
        v = re.sub(r"\$\{(\w+)\}", lambda m: get(m.group(1), "", _depth + 1), v)
    return os.path.expanduser(v) if v.startswith("~") else v


def lines(key: str, default: str = "") -> list[str]:
    """A list value: one item per line (or separated by |)."""
    raw = get(key, default)
    parts = raw.split("\n") if "\n" in raw else raw.split("|")
    return [p.strip() for p in parts if p.strip()]


def flag(key: str, default: bool = False) -> bool:
    v = get(key, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


def number(key: str, default=None):
    v = get(key, "").strip()
    if not v or v.lower() == "none":
        return default
    return float(v) if "." in v else int(v)


def secret(key: str) -> str:
    v = get(key)
    if v:
        return v
    service = get("KEYCHAIN_SERVICE", "job-hunt-toolkit")
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-a", key, "-w"], capture_output=True, text=True)
        return r.stdout.strip()
    except FileNotFoundError:   # not macOS
        return ""


def require(*keys: str) -> None:
    missing = [k for k in keys if not get(k)]
    if missing:
        raise SystemExit(f"Missing in {ENV_FILE}: {', '.join(missing)}  (copy .env.example to .env and fill them in)")
