# Extending the toolkit

Where things live, and how to add your own. Most changes need only `.env`; this page is for changing behaviour.

## The rule that keeps it shareable

Nothing personal goes in code. If a change needs a value that differs per person (a city, a number, a phrase, a list),
add a key to **both** `.env.example` (with a neutral example and a comment) and your `.env`, and read it with
`toolkit_env`:

```python
import toolkit_env as env
env.get("KEY", "default")     # a string; ${OTHER_KEY} references and a leading ~ are filled in
env.lines("KEY")              # a list: one item per line, or a|b|c
env.flag("KEY", False)        # true / yes / 1 / on
env.number("KEY", 10)         # int or float; empty or "none" -> the default
env.secret("KEY")             # environment -> .env -> macOS Keychain (service KEYCHAIN_SERVICE, account KEY)
env.require("A", "B")         # stop with a clear message if any is empty
```

## 1. Cold-email script (`email-script/script.py`)

The script runs top to bottom: config → log in to LinkedIn → search each query → collect emails and the post each came
from → filter → send.

| You want to… | Change |
|---|---|
| Search different posts | `SEARCH_QUERIES` / `CITY_QUERIES` in `.env`. Keep them short: `hiring <role> <place>`. |
| Skip another kind of post | `skip_reason(email, ctx)`: return a short reason string to skip, `""` to send. `ctx["text"]` is the post text, `ctx["min_exp"]` the years it asks for. Rules that apply even with `STRICT_FIT=false` go in `hard_block()`. |
| Block more agencies / IT-services firms | add domain words to `AGENCY_DOMAIN_WORDS` or names to `SERVICES_FIRMS` (these are shared knowledge, so they stay in code) |
| Change who gets which email | `person_first_name()`: named people get the referral template, shared inboxes (`hr@`, `careers@`…) the application template |
| Add a field to the emails | write `${KEY}` in `EMAIL_*_BODY` in `.env`; any key works, no code change |
| A different date window or post type | `content_search_url()` builds the LinkedIn search URL |
| Send through another provider than Gmail | `send_application()`: replace the `smtplib.SMTP_SSL('smtp.gmail.com', 465)` block |
| Pace LinkedIn differently | the SAFE PACING block (`SAFE_DAILY_LOADS`, cooldowns) and `MAX_LOADS_PER_QUERY`. Raising them risks the account. |

Test a filter change without LinkedIn: put test queries in `DATA_DIR/logs/explore_queries.txt`
(`past-week|hiring AI engineer Pune` per line) and run `python3 script.py --explore`. It reports what each query found
and why each lead was dropped, and sends nothing.

## 2. Auto-apply agent (`autoapply-agent/`)

The extension itself lives in its own repository (`AGENT_REPO`); the toolkit only downloads it and prefills its
profile. Change the extension in its own repository, following its author's license.

| You want to… | Change |
|---|---|
| Prefill another profile field from `.env` | add it to the `profile` dict in `build_profile.py` (keys must match the extension's profile fields), add the key to `.env.example`, run `./setup.sh agent` |
| Use an extension with different field names | rename the keys in the `profile` dict in `build_profile.py` |
| Give the extension more prepared answers | add lines to `ANSWER_BANK` / `FACT_NOTES` in `.env`; `build_profile.py` puts them in the profile's `notes` |
| Use another local model | `OLLAMA_MODEL` in `.env`, then `./setup.sh agent` (it pulls the model) |

## 3. Dossier (`dossier/`)

Dossier itself runs unmodified from its Docker image; the toolkit talks to it through its own endpoints.

| You want to… | Change |
|---|---|
| Change what every letter must (not) do | `DOSSIER_NOTE` in `.env` |
| Catch another kind of bad sentence | `problems()` in `dossier_apply.py` and the regexes above it (`UNDERSELL`, `THIRD_PERSON`, `EXPERIENCE_RANGE`). A failing check triggers one automatic rewrite. |
| Use a hosted model instead of Ollama | set `DOSSIER_AI_PROVIDER` / `DOSSIER_AI_URL` / `DOSSIER_AI_MODEL` / `DOSSIER_AI_KEY` in `DOSSIER_DIR/.env` (see Dossier's documentation), then `docker compose up -d` |
| Bulk-load or back up your profile | `import_profile.py` (only the profile is replaced); `GET /api/export` gives the full backup |

## 4. Adding a fourth tool

Make a folder, read settings with `toolkit_env` (add `sys.path.insert(0, <repo root>)`), add its keys to
`.env.example` under a new numbered section, add a `setup_<tool>.sh` if it needs one, and call it from `setup.sh`.
Document it in the README's tool table and "Daily use".

## Before you push

```bash
git diff --cached | grep -inE "<your name>|<your phone>|<your email>|/Users/"   # should print nothing
```
`.env`, `dossier/profile.json`, résumés (`*.pdf`, `*.docx`) and runtime data are git-ignored. Keep it that way.
