#!/usr/bin/env python3
"""Prefills the auto-apply extension's profile from the toolkit .env.

The extension reads optional defaults from its own profile-default.js (window.AA_DEFAULT_PROFILE); they only fill
EMPTY fields of its first-run setup screen. This script writes that file from .env, so you don't type your details
by hand. Run it after editing .env, then reload the extension."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import toolkit_env as env  # noqa: E402

agent = Path(env.get("AGENT_DIR", "~/job-hunt-agent")).expanduser()
if not (agent / "manifest.json").exists():
    sys.exit(f"No extension at {agent}. Run ./autoapply-agent/setup_agent.sh first.")

notes = "\n".join(x for x in (env.get("FACT_NOTES"), env.get("ANSWER_BANK")) if x)
profile = {   # only fields the extension's profile has
    "firstName": env.get("FIRST_NAME"), "lastName": env.get("LAST_NAME"), "email": env.get("EMAIL"), "phone": env.get("PHONE"),
    "city": env.get("CITY"), "country": env.get("COUNTRY", "India"), "currentTitle": env.get("CURRENT_TITLE"),
    "currentCompany": env.get("CURRENT_COMPANY"), "totalYears": env.get("TOTAL_YEARS"), "skills": env.get("SKILLS"),
    "currentCtc": env.get("CURRENT_CTC_INR"), "expectedCtc": env.get("EXPECTED_CTC_INR"), "salaryCurrency": "INR",
    "noticeDays": env.get("NOTICE_DAYS"), "workAuthorized": env.get("WORK_AUTHORIZED", "Yes"),
    "needSponsorship": env.get("NEEDS_SPONSORSHIP", "No"), "willingRelocate": env.get("WILLING_TO_RELOCATE", "Yes"),
    "onsiteOk": env.get("ONSITE_OK", "Yes"), "linkedinUrl": env.get("LINKEDIN_URL"), "githubUrl": env.get("GITHUB_URL"),
    "portfolioUrl": env.get("PORTFOLIO_URL"), "education": env.get("EDUCATION"), "notes": notes,
    "minSkillYears": env.get("SKILL_YEARS"),
}
profile = {k: v for k, v in profile.items() if v != ""}

(agent / "profile-default.js").write_text(
    "// Written by job-hunt-toolkit/autoapply-agent/build_profile.py from your .env (prefills empty profile fields).\n"
    f"window.AA_DEFAULT_PROFILE = {json.dumps(profile, indent=1, ensure_ascii=False)};\n", encoding="utf-8")
print(f"✓ {agent / 'profile-default.js'}: {len(profile)} profile fields prefilled from .env")
