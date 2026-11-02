#!/usr/bin/env python3
"""Creates a Dossier application from a job posting, end to end, with Dossier's own AI (local Ollama model):
reads the posting, matches it to the profile, writes the tailored resume + cover letter, creates the application
(a Draft: nothing is sent), and downloads both PDFs.

Usage:
  python3 dossier_apply.py posting.txt [--tone DIRECT|WARM|FORMAL] [--url https://...] [--note "..."]
  pbpaste | python3 dossier_apply.py -          # posting text from the clipboard
  python3 dossier_apply.py --link https://...   # let Dossier fetch the posting from its link

Settings (URL, tone, standing note, password) come from the toolkit .env.
Uses only Dossier's own endpoints (sign-in form, its server actions, /api/jobs, /api/pdf); no browser automation.
"""
from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import toolkit_env as env  # noqa: E402

BASE = env.get("DOSSIER_URL", "http://localhost:3000").rstrip("/")
OUT = os.path.join(env.get("DOSSIER_DIR", "~/dossier"), "applications")
ACTIONS = ("startReadingPosting", "startWritingDocuments", "createApplication", "fetchPostingText")
# Local 7B models tend to undersell ("I lack experience with X" when the profile shows X) and to write the summary
# in the third person. This note goes with every application.
STANDING_NOTE = env.get("DOSSIER_NOTE")   # from .env: sent with every application

# The note isn't always followed (e.g. calling a core skill "an area I am eager to explore"), so the written
# documents are checked before the application is made, and rewritten once if a check fails.
UNDERSELL = re.compile(r"\b(lack\w*|eager to (learn|explore)|new to|limited (experience|exposure)|no (prior )?experience|unfamiliar)\b", re.I)
_TERMS = ["RAG", "LLM", "vector", "LangChain", "LangGraph", "FastAPI", "Docker", "Kubernetes", "Python", "agentic", "OCR", "voice",
          "microservices", "guardrails", "tool calling", "retrieval", "prompt"] + [t.strip() for t in env.get("SKILLS").split(",") if t.strip()]
PROFILE_TERMS = re.compile(r"\b(" + "|".join(re.escape(t) for t in _TERMS) + r")\b", re.I)   # your skills (from .env) + common ones
THIRD_PERSON = re.compile(r"\b(he|his|him|she|her)\b|\b" + re.escape(env.get("FIRST_NAME", "_")) + r" (is|has|brings|was)\b", re.I)
EXPERIENCE_RANGE = re.compile(r"\b\d+\s*(-|–|to)\s*\d+\+?\s*years|\b[2-9]\+\s*years", re.I)

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPRedirectHandler())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


raw_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())


def req(method: str, path: str, body: bytes | None = None, headers: dict | None = None, follow=True, timeout=120):
    r = urllib.request.Request(BASE + path, data=body, method=method, headers={"Origin": BASE, **(headers or {})})
    try:
        with (opener if follow else raw_opener).open(r, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def keychain_password() -> str:
    """DOSSIER_PASSWORD from the environment, .env or the Keychain (service KEYCHAIN_SERVICE); setup_dossier.sh stores it."""
    pw = env.secret("DOSSIER_PASSWORD")
    if not pw:
        sys.exit("No Dossier password: run ./dossier/setup_dossier.sh (it stores one), or set DOSSIER_PASSWORD.")
    return pw


def sign_in() -> None:
    _, _, page = req("GET", "/login")
    form = re.search(r'<form[^>]*method="POST"[^>]*>(.*?)</form>', page.decode(), re.S)
    if not form:
        sys.exit("Couldn't read Dossier's sign-in form (is Dossier running?)")
    fields = [(m.group(1), html.unescape(m.group(2) or "")) for m in
              re.finditer(r'<input type="hidden" name="([^"]+)"(?: value="([^"]*)")?', form.group(1))]
    fields += [("password", keychain_password()), ("remember", "on")]
    boundary = "----dossier" + uuid.uuid4().hex
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
    body += f"--{boundary}--\r\n".encode()
    status, _, _ = req("POST", "/login", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, follow=False)
    if status not in (302, 303) or not any(c.name == "dossier_session" for c in jar):
        sys.exit(f"Sign-in failed (HTTP {status}). Check the Keychain password.")


def action_ids() -> dict[str, str]:
    """Server action IDs change with every Dossier build, so they're read from the New application page each run."""
    _, _, page = req("GET", "/new")
    ids: dict[str, str] = {}
    for chunk in sorted(set(re.findall(r'/_next/static/[^"]+\.js', page.decode()))):
        _, _, js = req("GET", chunk)
        for m in re.finditer(r'createServerReference\)?\("([0-9a-f]{40,})"[^;]{0,200}?"(\w+)"\)', js.decode("utf8", "replace")):
            if m.group(2) in ACTIONS:
                ids[m.group(2)] = m.group(1)
    missing = [a for a in ACTIONS if a not in ids]
    if missing:
        sys.exit(f"Couldn't find Dossier actions {missing} (Dossier version changed?)")
    return ids


def call(ids: dict, name: str, args: dict):
    """Calls a server action like the page does; returns its data or exits with its error."""
    status, _, raw = req("POST", "/new", json.dumps([args]).encode(),
                         {"Next-Action": ids[name], "Content-Type": "text/plain;charset=UTF-8", "Accept": "text/x-component"})
    text = raw.decode("utf8", "replace")
    for line in text.splitlines():
        m = re.match(r'^[0-9a-f]+:(\{.*"ok":.*\})$', line)
        if m:
            res = json.loads(m.group(1))
            if res.get("ok"):
                return res.get("data")
            sys.exit(f"{name}: {res.get('error')}")
    sys.exit(f"{name}: unexpected reply (HTTP {status}): {text[:300]}")


def wait_job(job_id: str, label: str, limit_s=1200) -> dict:
    t0 = time.time()
    last = None
    while time.time() - t0 < limit_s:
        _, _, raw = req("GET", f"/api/jobs/{job_id}")
        job = json.loads(raw)
        st = job.get("status")
        if st != last:
            print(f"   {label}: {st} ({int(time.time() - t0)}s)", flush=True)
            last = st
        if st == "DONE":
            return job
        if st in ("FAILED", "CANCELLED"):
            sys.exit(f"{label} failed: {job.get('error') or job}")
        time.sleep(3)
    sys.exit(f"{label}: still not done after {limit_s // 60} min")


def texts(node) -> list[str]:
    """Every piece of written text in a documents result (labels and ids left out)."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [t for k, v in node.items() if k not in ("id", "label", "kind", "type") for t in texts(v)]
    if isinstance(node, list):
        return [t for v in node for t in texts(v)]
    return []


def problems(documents, profile_text: str) -> list[str]:
    """Sentences that undersell the profile, talk in the third person, copy the posting's experience range, or carry a
    number that isn't in the profile."""
    today = time.strftime("%d %B %Y").lstrip("0")
    found = []
    for text in texts(documents):
        for s in re.split(r"(?<=[.!?])\s+|\n+", text.replace(today, "")):
            reasons = []
            if UNDERSELL.search(s) and PROFILE_TERMS.search(s):
                reasons.append("undersells")
            if THIRD_PERSON.search(s):
                reasons.append("third person")
            if EXPERIENCE_RANGE.search(s):
                reasons.append("experience range")
            new = [n for n in re.findall(r"\d[\d.,]*%?", s) if n.rstrip(".,") not in profile_text]
            if new:
                reasons.append("new number " + " ".join(new))
            if reasons:
                found.append(f"[{', '.join(reasons)}] {s.strip()}")
    return found


def download(app_id: str, doc: str, folder: str) -> str | None:
    status, _, raw = req("POST", f"/api/pdf?doc={doc}&application={app_id}", timeout=300)
    if status != 200:
        print(f"   {doc} PDF: HTTP {status} {raw[:200]!r}")
        return None
    url = json.loads(raw)["url"]
    status, headers, pdf = req("GET", url, timeout=300)
    name = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', headers.get("Content-Disposition", "") or "")
    fn = os.path.join(folder, urllib.request.unquote(name.group(1)) if name else f"{doc}.pdf")
    open(fn, "wb").write(pdf)
    return fn


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("posting", nargs="?", help="file with the posting text, or - for stdin")
    ap.add_argument("--link", default="", help="posting URL: Dossier fetches the text itself")
    ap.add_argument("--url", default="", help="posting URL to store with the application")
    ap.add_argument("--tone", default=env.get("DOSSIER_TONE", "DIRECT"), choices=["DIRECT", "WARM", "FORMAL"])
    ap.add_argument("--note", default="", help="extra instruction for the documents, added to the standing one")
    a = ap.parse_args()

    sign_in()
    ids = action_ids()
    if a.link:
        fetched = call(ids, "fetchPostingText", {"link": a.link})
        posting, a.url = fetched["text"], a.url or fetched["url"]
    elif a.posting:
        posting = sys.stdin.read() if a.posting == "-" else open(a.posting).read()
    else:
        ap.error("give a posting file, - for stdin, or --link")
    if len(posting.strip()) < 100:
        sys.exit("The posting text is too short.")

    t0 = time.time()
    print("1/3 Reading the posting and matching it to your profile…", flush=True)
    read_id = call(ids, "startReadingPosting", {"posting": posting})
    read = wait_job(read_id, "read")["result"]
    ex = read.get("extracted", {})
    details = {k: (ex.get(k) or "") for k in ("title", "company", "role", "location", "mode", "salary")}
    details["deadline"] = ex.get("deadline") or ""
    print(f"   → {details['role'] or details['title']} at {details['company']} ({details['location']}, {details['mode']})")

    print("2/3 Writing the tailored resume and cover letter…", flush=True)
    _, _, raw = req("GET", "/api/export")
    profile_text = json.dumps(json.loads(raw)["profile"], ensure_ascii=False)
    note = (STANDING_NOTE + (" " + a.note.strip() if a.note.strip() else ""))[:2000]
    write_id = call(ids, "startWritingDocuments", {"readJobId": read_id, "details": details, "match": read["match"], "tone": a.tone, "note": note})
    found = problems(wait_job(write_id, "write")["result"], profile_text)
    if found:
        print("   The documents need fixing, writing them once more:\n     " + "\n     ".join(found), flush=True)
        retry_note = (note + " Do not write sentences like these again: " + " | ".join(f[f.index("]") + 2:] for f in found))[:2000]
        retry_id = call(ids, "startWritingDocuments", {"readJobId": read_id, "details": details, "match": read["match"], "tone": a.tone, "note": retry_note})
        retry_found = problems(wait_job(retry_id, "rewrite")["result"], profile_text)
        if len(retry_found) <= len(found):
            write_id, found = retry_id, retry_found

    print("3/3 Creating the application (Draft) and downloading the PDFs…", flush=True)
    app_id = call(ids, "createApplication", {"writeJobId": write_id, "details": details, "postingUrl": a.url, "postingId": None, "startupId": None})
    safe = re.sub(r"[^\w.-]+", "_", f"{details['company']}_{details['role'] or details['title']}")[:80] or app_id
    folder = os.path.join(OUT, safe)
    os.makedirs(folder, exist_ok=True)
    files = [f for f in (download(app_id, "resume", folder), download(app_id, "letter", folder)) if f]
    print(f"\nDone in {int(time.time() - t0)}s. Application: {BASE}/applications/{app_id}")
    for f in files:
        print("   " + f)
    if found:
        print("\nCHECK BEFORE SENDING, fix these in Dossier:\n   " + "\n   ".join(found))
    else:
        print("Checks passed: no underselling, first person, no copied experience range, no new numbers.")


if __name__ == "__main__":
    main()
