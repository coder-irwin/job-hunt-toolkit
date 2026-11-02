import time
import re
import sys
import random
from urllib.parse import quote
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import toolkit_env as env   # everything personal comes from ../.env
# Where the sent-emails list, logs, LinkedIn safety counters and the logged-in browser profile live.
DATA_DIR = env.get("DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
import json
import smtplib
from email.message import EmailMessage
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


# --- 0. LOGGING: every line goes to the terminal AND logs/run_<date>_<time>.log,
#        each stamped with the clock time, so a run can be read back later. ---
LOG_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, time.strftime("run_%Y-%m-%d_%H-%M-%S.log"))

class _Tee:
    """Write to several streams, prefixing each new line with [HH:MM:SS]."""
    def __init__(self, *streams):
        self.streams = streams
        self._at_line_start = True
    def write(self, data):
        if not data:
            return
        out = []
        for chunk in data.splitlines(True):
            if self._at_line_start and chunk.strip():
                out.append(time.strftime("[%H:%M:%S] ") + chunk)
            else:
                out.append(chunk)
            self._at_line_start = chunk.endswith("\n")
        text = "".join(out)
        for s in self.streams:
            try:
                s.write(text)
                s.flush()
            except Exception:
                pass
    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass
    def isatty(self):
        return False

_log_fh = open(LOG_FILE, "a", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _log_fh)
sys.stderr = _Tee(sys.__stderr__, _log_fh)

print("=" * 70)
print(f"🚀 RUN START  {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"📝 Log file   {LOG_FILE}")
print(f"🐍 Python     {sys.version.split()[0]}   Script: {os.path.basename(__file__)}")
print("=" * 70)

 
# --- 1. CONFIGURATION ---
LINKEDIN_EMAIL = env.get("LINKEDIN_EMAIL")
LINKEDIN_PASS = env.secret("LINKEDIN_PASS")   # environment, .env, or macOS Keychain
SENDER_EMAIL = env.get("SENDER_EMAIL")
SENDER_PASS = env.secret("SENDER_PASS")       # Gmail App Password (Google Account -> Security -> App passwords)
RESUME_PATH = env.get("RESUME_PDF")
env.require("LINKEDIN_EMAIL", "SENDER_EMAIL", "RESUME_PDF")
BLACKLIST_FILE = os.path.join(DATA_DIR, "sent_emails.txt")
SEND_DAILY_CAP = env.number("SEND_DAILY_CAP", 200)              # max applications per calendar day (Gmail safety)
SEND_GAP = (25, 45)               # random seconds between emails (spam-filter safety)

# Personal / free-mail providers: never mailed, only official company addresses.
PERSONAL_DOMAINS = {
    "gmail", "googlemail", "yahoo", "ymail", "rocketmail", "outlook", "hotmail",
    "live", "msn", "icloud", "me", "mac", "aol", "rediffmail", "rediff", "proton",
    "protonmail", "pm", "zoho", "zohomail", "gmx", "mail", "yandex", "tutanota",
    "fastmail", "inbox", "hey",
}

def is_personal_email(email):
    return email.split("@")[-1].split(".")[0].lower() in PERSONAL_DOMAINS

def company_from_email(email):
    """hr@acme-labs.co.in -> 'Acme Labs'."""
    name = email.split("@")[-1].split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()

# Shared inboxes get the application; a named person's work address
# (nancy@acme.com) gets a referral request quoting their post.
ROLE_INBOX_WORDS = (
    "hr", "career", "job", "recruit", "talent", "hiring", "hire", "resume", "cv",
    "people", "apply", "application", "opening", "vacanc", "placement", "staffing",
    "info", "contact", "admin", "team", "support", "hello", "work", "join",
)

def person_first_name(email):
    """'' for shared inboxes, else a best-guess first name ('' if unclear)."""
    local = email.split("@")[0].lower()
    if any(w in local for w in ROLE_INBOX_WORDS):
        return ""
    first = re.split(r"[._\-+0-9]", local)[0]
    return first.title() if first.isalpha() and len(first) >= 3 else "there"

RETRY_FILE = os.path.join(DATA_DIR, "logs", "failed_sends.jsonl")

_SENT_TODAY_FILE = os.path.join(DATA_DIR, "logs",
                                time.strftime("sent_%Y-%m-%d.txt"))

def sent_today_count():
    if not os.path.exists(_SENT_TODAY_FILE):
        return 0
    with open(_SENT_TODAY_FILE) as f:
        return sum(1 for line in f if line.strip())

# --- 2. FILE HANDLING LOGIC ---
def get_already_sent_emails():
    """Reads the text file and returns a set of previously contacted emails."""
    if not os.path.exists(BLACKLIST_FILE):        
        return set()
    with open(BLACKLIST_FILE, "r") as f:
        # Using a set for O(1) lookup speed
        return set(line.strip().lower() for line in f if line.strip())
 
def log_successful_send(email):
    """Appends a single email to the sent_emails.txt file."""
    print(f"Writing email to file: {email}")
    with open(BLACKLIST_FILE, "a") as f:
        f.write(email + "\n")
    with open(_SENT_TODAY_FILE, "a") as f:
        f.write(email + "\n")
 
# --- 3. EMAIL DISPATCHER ---
# Referral email: one fixed template, only the first name changes.
REFERRAL_SUBJECT = env.get("EMAIL_REFERRAL_SUBJECT")
REFERRAL_BODY = env.get("EMAIL_REFERRAL_BODY") + "\n"   # {name} = first name

# Application email (shared inboxes: careers@, hr@, jobs@ ...): one fixed template.
APPLICATION_SUBJECT = env.get("EMAIL_APPLICATION_SUBJECT")
APPLICATION_BODY = env.get("EMAIL_APPLICATION_BODY") + "\n"
BLOCK_DOMAINS = [d.lower() for d in env.lines("BLOCK_DOMAINS")]

def send_application(recipient, ctx=None):
    # Final safety check: official company addresses only.
    recipient_lower = recipient.lower()
    if is_personal_email(recipient_lower) or any(d in recipient_lower for d in BLOCK_DOMAINS):
        print(f"⏩ Skipping personal/blocked address: {recipient}")
        return False

    ctx = ctx or {}
    first_name = person_first_name(recipient)

    msg = EmailMessage()
    msg['From'] = SENDER_EMAIL
    msg['To'] = recipient

    if first_name:
        msg['Subject'] = REFERRAL_SUBJECT
        body = REFERRAL_BODY.format(name=first_name)
    else:
        msg['Subject'] = APPLICATION_SUBJECT
        body = APPLICATION_BODY
    msg.set_content(body)
   
    try:
        with open(RESUME_PATH, 'rb') as f:
            msg.add_attachment(f.read(), maintype='application', subtype='pdf', filename=env.get("RESUME_ATTACHMENT_NAME", "Resume.pdf"))
    except Exception as e:
        print(f"❌ Resume not readable at {RESUME_PATH}: {e}")
        return False

    # Retry on network errors (a first DNS lookup sometimes fails). Auth errors are NOT retried - a bad app password won't fix itself.
    last_err = None
    waits = [5, 15, 30, 60]          # ~2 min total: rides out a short Wi-Fi/DNS drop
    for attempt in range(1, len(waits) + 2):
        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as smtp:
                smtp.login(SENDER_EMAIL, SENDER_PASS)
                smtp.send_message(msg)
            # CRITICAL: Only add to file if sending actually worked
            log_successful_send(recipient)
            print(f"✅ Successfully sent and logged: {recipient}")
            return True
        except smtplib.SMTPAuthenticationError as e:
            print(f"❌ Gmail rejected the login (check SENDER_PASS app password): {e}")
            return False
        except Exception as e:
            last_err = e
            if attempt <= len(waits):
                w = waits[attempt - 1]
                print(f"   ↻ send to {recipient} failed ({str(e)[:80]}), retry {attempt + 1}/{len(waits) + 1} in {w}s...")
                time.sleep(w)
    print(f"❌ Failed to send to {recipient} after {len(waits) + 1} tries: {last_err}")
    # Keep it (with its post context) so the next run retries it.
    with open(RETRY_FILE, "a") as f:
        f.write(json.dumps({"email": recipient, "ctx": ctx}) + "\n")
    return False
 
# --- 4. MAIN AUTOMATION ---
# Load history before starting
already_sent = get_already_sent_emails()
print(f"⚙️  Resume      : {RESUME_PATH}  (exists: {os.path.exists(RESUME_PATH)})")
print(f"⚙️  Sender      : {SENDER_EMAIL}")
print(f"⚙️  LinkedIn    : {LINKEDIN_EMAIL}")
print(f"⚙️  Blacklist   : {len(already_sent)} addresses already contacted")
found_this_session = set()
rejected_this_session = set()   # leads dropped by skip_reason during the scrape
email_query = {}                # email -> the search query that found it
reject_reason = {}              # email -> why it was dropped
MAIL_LIMIT = env.number("MAIL_LIMIT", 150)                  # target: stop searching once this many new emails are found
RATE_LIMIT_SIGNS = [
    "unusual search traffic",
    "search activity on your account has been paused",
]
RATE_LIMIT_FLAG = os.path.join(DATA_DIR, ".linkedin_rate_limited")
# --- SAFE PACING (research: LinkedIn judges each account against its own
# baseline; spikes, repeated logins and back-to-back sessions trigger pauses.
# Steady, small, slowly-growing daily volume does not). Ledger of every
# LinkedIn session: logs/linkedin_usage.json
USAGE_FILE = os.path.join(LOG_DIR, "linkedin_usage.json")
FORCE = "--force" in sys.argv
try:
    usage = json.load(open(USAGE_FILE))
except Exception:
    usage = {"safe_start": time.strftime("%Y-%m-%d"), "days": {}, "pauses": []}
_today = time.strftime("%Y-%m-%d")

# Cooldown grows with repeat pauses in the last 14 days: 1 -> 3 days, 2+ -> 7 days.
_recent_pauses = [p for p in usage["pauses"] if time.time() - time.mktime(time.strptime(p, "%Y-%m-%d %H:%M:%S")) < 14 * 86400]
_COOLDOWN_HOURS = 168 if len(_recent_pauses) >= 2 else 72
if usage["pauses"] and not FORCE:
    _age_h = (time.time() - time.mktime(time.strptime(usage["pauses"][-1], "%Y-%m-%d %H:%M:%S"))) / 3600
    if _age_h < _COOLDOWN_HOURS:
        print(f"🛑 LinkedIn paused search {_age_h:.0f}h ago ({len(_recent_pauses)} pause(s) in 14 days). "
              f"Safe to run again in {_COOLDOWN_HOURS - _age_h:.0f}h (or use --force, at your own risk).")
        sys.exit(1)

# One LinkedIn session per day: back-to-back sessions/logins were a pause trigger.
if usage["days"].get(_today, 0) > 0 and not FORCE and "--explore" not in sys.argv:
    print(f"🛑 Already ran a LinkedIn session today ({usage['days'][_today]} page loads). "
          f"One session per day keeps the account safe. Run again tomorrow, or use --force.")
    sys.exit(1)

# Daily page-load allowance: start at 15 (a safe starting volume), +15% per week, cap 40.
_weeks = max(0, (time.time() - time.mktime(time.strptime(usage["safe_start"], "%Y-%m-%d"))) // (7 * 86400))
SAFE_DAILY_LOADS = int(min(40, round(15 * 1.15 ** _weeks)))

def record_usage(loads=0, paused=False):
    usage["days"][_today] = usage["days"].get(_today, 0) + loads
    if paused:
        usage["pauses"].append(time.strftime("%Y-%m-%d %H:%M:%S"))
    json.dump(usage, open(USAGE_FILE, "w"), indent=1)
 
# Prefer a local chromedriver (no network needed); fall back to the downloader.
_LOCAL_DRIVER = os.path.join(DATA_DIR, "chromedriver")
# 'eager' = return from navigation as soon as the DOM is ready, instead of
# waiting for LinkedIn's never-ending background requests. Without this, the
# first driver.current_url after login can block for minutes.
_opts = webdriver.ChromeOptions()
_opts.page_load_strategy = "eager"
_opts.add_argument("--disable-notifications")
# Remember the LinkedIn session between runs (fewer logins = fewer red flags).
_opts.add_argument("--user-data-dir=" + os.path.join(DATA_DIR, "chrome_profile_bot"))
_opts.add_argument("--start-maximized")
_opts.add_argument("--window-size=1440,1000")
if os.path.exists(_LOCAL_DRIVER):
    print(f"🚗 Using local chromedriver: {_LOCAL_DRIVER}")
    driver = webdriver.Chrome(service=Service(_LOCAL_DRIVER), options=_opts)
else:
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=_opts)
driver.set_page_load_timeout(60)

 
# Robust Login Logic
def fill_login_field(field, value, field_name):
    """Fill an input reliably for React-style forms (LinkedIn)."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
        WebDriverWait(driver, 5).until(lambda d: field.is_displayed() and field.is_enabled())

        # Use native value setter so frameworks detect the update.
        driver.execute_script(
            """
            const element = arguments[0];
            const val = arguments[1];
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(element, val);
            element.dispatchEvent(new Event('input', { bubbles: true }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            field,
            value,
        )

        # Validate; fallback to clear + type if JS path did not stick.
        current_val = field.get_attribute("value") or ""
        if current_val.strip() != value:
            field.click()
            field.clear()
            field.send_keys(value)

        print(f"✅ Filled {field_name}")
        return True
    except Exception as e:
        print(f"⚠️ Could not fill {field_name}: {e}")
        return False


def attempt_login():
    print(f"Attempting Auto-Login at: {driver.current_url}")
    
    for attempt in range(3): # Try 3 times
        try:
            print(f"Login attempt {attempt + 1}/3...")
            # Wait for login form inputs to appear.
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password'], #password, input[name='session_password']"))
            )
            
            # Clear any overlays (cookie banners, etc.)
            driver.execute_script("""
                const overlays = document.querySelectorAll('.artdeco-modal-overlay, #artdeco-modal-outlet, .cookie-consent-banner');
                overlays.forEach(el => el.remove());
                document.body.style.overflow = 'auto';
            """)
            time.sleep(1)

            # 1. Find User Field
            user_field = None
            user_selectors = [
                "input[autocomplete='username']",
                "input[aria-label='Email or phone']",
                "#username",
                "input[name='session_key']",
                "input[type='email']"
            ]
            for sel in user_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, sel)
                    for el in elements:
                        if el.is_displayed() and el.is_enabled():
                            user_field = el
                            break
                    if user_field: break
                except: continue
            
            if not user_field:
                time.sleep(2)
                continue

            # 2. Find Password Field
            pass_field = None
            pass_selectors = [
                "input[type='password']",
                "input[autocomplete='current-password']",
                "#password",
                "input[name='session_password']"
            ]
            for sel in pass_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, sel)
                    for el in elements:
                        if el.is_displayed() and el.is_enabled():
                            pass_field = el
                            break
                    if pass_field: break
                except: continue
            
            if not pass_field:
                time.sleep(2)
                continue

            # 3. Fill credentials using robust helper.
            print("💉 Filling credentials...")
            user_ok = fill_login_field(user_field, LINKEDIN_EMAIL, "username")
            pass_ok = fill_login_field(pass_field, LINKEDIN_PASS, "password")

            if not (user_ok and pass_ok):
                time.sleep(2)
                continue
            
            time.sleep(1)

            # 4. Submit the form. Several strategies, and VERIFY each one
            #    actually left the login page before declaring success.
            def _left_login_page(wait_s=8):
                end = time.time() + wait_s
                while time.time() < end:
                    try:
                        u = driver.current_url
                        if "/login" not in u and "uas/login" not in u:
                            return True
                    except Exception:
                        pass
                    time.sleep(1)
                return False

            # 4a. Enter in the password box: the path LinkedIn itself wires up.
            try:
                pass_field.send_keys(Keys.RETURN)
                print("⌨️ Pressed Enter in password field.")
                if _left_login_page():
                    print("✅ Login submitted (Enter).")
                    return True
            except Exception as e:
                print(f"   Enter key failed: {e}")

            # 4b. Click the first VISIBLE sign-in button, not just the first match.
            btn_selectors = [
                "button[data-litms-control-urn='login-submit']",
                ".login__form_action_container button",
                "button.btn__primary--large",
                "button[aria-label='Sign in']",
                "form button[type='submit']",
                "button[type='submit']",
            ]
            clicked = False
            for sel in btn_selectors:
                try:
                    for btn in driver.find_elements(By.CSS_SELECTOR, sel):
                        if not (btn.is_displayed() and btn.is_enabled()):
                            continue
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                        try:
                            btn.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", btn)
                        print(f"🖱️ Clicked sign-in button ({sel}).")
                        clicked = True
                        break
                except Exception:
                    continue
                if clicked:
                    break
            if clicked and _left_login_page():
                print("✅ Login submitted (button click).")
                return True

            # 4c. Last resort: submit the form directly.
            print("⌨️ Trying form submission fallback...")
            try:
                driver.execute_script(
                    "const f = arguments[0].form; if (f) { f.requestSubmit ? f.requestSubmit() : f.submit(); }",
                    pass_field)
            except Exception as e:
                print(f"   form submit failed: {e}")
            if _left_login_page():
                print("✅ Login submitted (form submit).")
                return True

            # Still on the login page. Surface LinkedIn's own error text (wrong
            # password, etc.) and let the outer loop retry.
            try:
                errs = driver.find_elements(By.CSS_SELECTOR,
                    "#error-for-password, #error-for-username, .form__label--error, [role='alert']")
                msgs = [x.text.strip() for x in errs if x.text.strip()]
                if msgs:
                    print(f"⚠️ LinkedIn says: {' | '.join(msgs)[:200]}")
            except Exception:
                pass
            print(f"⚠️ Still on login page after attempt {attempt + 1}: {driver.current_url}")
            time.sleep(2)

        except Exception as e:
            if "no such window" in str(e).lower():
                print("⚠️ Window closed. Stopping.")
                return False
            print(f"Attempt {attempt + 1} failed: {e}")
            time.sleep(2)
            
    return False

# Start Navigation: open the feed first; only show the login page if the saved session expired.
driver.get("https://www.linkedin.com/feed/")
time.sleep(random.uniform(4, 7))
if "/login" not in driver.current_url and "authwall" not in driver.current_url and "checkpoint" not in driver.current_url:
    print("✅ Already logged in (saved session).")
else:
    driver.get("https://www.linkedin.com/login")
    time.sleep(2)

# Check if already logged in (cookies/session)
if "feed" in driver.current_url:
    print("✅ Already logged in!")
else:
    if not attempt_login():
        print("Auto-login failed. Please enter credentials manually.")
        print("The script will wait until it detects you are logged in...")



# Wait until login is successful
while True:
    try:
        current_url = driver.current_url
        if "checkpoint" in current_url:
            print("Security Check/CAPTCHA detected. Please solve it manually.")
            time.sleep(5)
            continue
        if "feed" in current_url or "search" in current_url:
            print("✅ Login successful! Proceeding...")
            break
        time.sleep(2)
    except Exception:
        print("❌ Browser closed. Exiting.")
        driver.quit()
        exit()


 
# --- 4. SEARCH TARGETS: content type = Posts, date posted = last 24 hours ---
# Your core queries (SEARCH_QUERIES in .env) run first, every run.
# Short queries only: LinkedIn post search needs (almost) every word to match,
# so extra words ("startup", "opening", "send CV") return nothing. Location,
# experience, role and company type are checked on the post text instead.
BASE_QUERIES = env.lines("SEARCH_QUERIES")   # from .env: short queries work best

# Then a rotating slice of a much wider pool, so back-to-back runs keep
# searching NEW ground instead of re-reading the same 24h of posts that are
# already in sent_emails.txt. Each run takes the next EXTRA_PER_RUN queries;
# the position is remembered in logs/.query_rotation between runs.
EXTRA_ROLES = env.lines("TARGET_ROLES")
EXTRA_LOCS = [""]   # location lives in EXTRA_MODS now
EXTRA_MODS = ["remote India", "work from home India", "remote"]   # the rotating pool searches remote roles
EXTRA_POOL = [" ".join(f"hiring {r} {m}".split()) for r in EXTRA_ROLES for m in EXTRA_MODS]
EXTRA_POOL = [q for q in EXTRA_POOL if q not in BASE_QUERIES]   # core queries already run every time
# Fixed-seed shuffle so each run's slice mixes roles AND cities, instead of
# spending a whole run on one (possibly niche) role across all 7 cities.
random.Random(42).shuffle(EXTRA_POOL)
# Walk the shuffled pool from where the last run stopped, for as many queries
# as it takes to reach MAIL_LIMIT (or the time / page-load caps). The position
# is saved after the scrape, so the next run continues with unsearched queries.
_ROT_FILE = os.path.join(LOG_DIR, ".query_rotation_pos_v4_remote")   # new pool: start fresh
try:
    _start = int(open(_ROT_FILE).read().strip() or 0) % len(EXTRA_POOL)
except Exception:
    _start = 0
EXTRA_QUERIES = [EXTRA_POOL[(_start + i) % len(EXTRA_POOL)] for i in range(len(EXTRA_POOL))]
if not env.flag("ROTATING_QUERIES", False):   # off by default: only SEARCH_QUERIES run
    EXTRA_QUERIES = []

# Core queries: last 24 hours. Rotating queries: past week. The blacklist
# guarantees nobody is mailed twice, so a wider window only adds new people.
QUERY_WINDOW = {q: "past-24h" for q in BASE_QUERIES}
QUERY_WINDOW.update({q: "past-week" for q in BASE_QUERIES if "startup" in q})
QUERY_WINDOW.update({q: "past-week" for q in EXTRA_QUERIES})
SEARCH_QUERIES = BASE_QUERIES + EXTRA_QUERIES
# --week: catch-up run after missed days. Every query searches the past week (the blacklist still
# prevents repeat mails). Page-load budget and other safety limits are unchanged.
CATCH_UP_WEEK = "--week" in sys.argv
if CATCH_UP_WEEK:
    QUERY_WINDOW = {q: "past-week" for q in SEARCH_QUERIES}
# --city: only the on-site / hybrid searches in CITY_QUERIES (.env), past week. Office/hybrid jobs
# are allowed for this run, but only in TARGET_CITIES (or remote); other cities are dropped.
CITY_MODE = "--city" in sys.argv
if CITY_MODE:
    SEARCH_QUERIES = env.lines("CITY_QUERIES")
    QUERY_WINDOW = {q: "past-week" for q in SEARCH_QUERIES}
    CATCH_UP_WEEK = True

# EXPLORE MODE (python3 script.py --explore): search the test
# queries in logs/explore_queries.txt ("past-week|query" per line), report the
# quality of what each one finds, and exit WITHOUT sending anything or touching
# sent_emails.txt / the rotation position.
EXPLORE = "--explore" in sys.argv
if EXPLORE:
    SEARCH_QUERIES, QUERY_WINDOW = [], {}
    _explore_file = os.path.join(LOG_DIR, "explore_queries.txt")
    if not os.path.exists(_explore_file):
        sys.exit(f"Explore mode reads test queries from {_explore_file} (one 'past-week|hiring AI engineer Pune' per line). Create it first.")
    for _line in open(_explore_file):
        if "|" in _line and not _line.lstrip().startswith("#"):
            _w, _q = _line.split("|", 1)
            SEARCH_QUERIES.append(_q.strip())
            QUERY_WINDOW[_q.strip()] = _w.strip()
    MAIL_LIMIT = 1000
    print(f"🧪 EXPLORE MODE: {len(SEARCH_QUERIES)} test queries. Nothing will be sent.")
print(f"🗂️  Query plan: {len(BASE_QUERIES)} core (last 24h), then rotating (past week) from position "
      f"{_start}/{len(EXTRA_POOL)} until {MAIL_LIMIT} emails are found. First few:")
for _q in SEARCH_QUERIES[:len(BASE_QUERIES) + 10]:
    print(f"      • [{QUERY_WINDOW[_q]:>9}] {_q}")

def content_search_url(keywords, date_posted="past-24h"):
    """LinkedIn post search (Posts tab), newest first.
    date_posted: "past-24h" for the core queries, "past-week" for the rotating ones."""
    return ("https://www.linkedin.com/search/results/content/?"
            f"keywords={quote(keywords)}"
            f"&datePosted=%5B%22{date_posted}%22%5D"
            "&sortBy=%5B%22date_posted%22%5D"
            "&origin=FACETED_SEARCH")

trash_keywords = [
    '.png', '.jpg', '.jpeg', '.gif', '1x', '2x', 'licdn', 'static', 'media',
    '@gmail.com', '@yahoo.com', '@outlook.com',
    '@hotmail.com', '@icloud.com'
]

# The ending must be all-lowercase or all-caps, so "rsystems.comKnow more" stops at ".com".
EMAIL_RE = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?:[a-z]{2,}|[A-Z]{2,})'
KNOWN_TLDS = ("com", "in", "io", "ai", "co", "net", "org", "tech", "dev", "app", "info", "biz", "us", "uk",
              "online", "cloud", "global", "solutions", "services", "digital", "pro", "xyz", "me", "so", "gg")

def clean_email(email):
    """Trim words glued onto the ending ("x@acme.comknow" -> "x@acme.com")."""
    local, _, domain = email.lower().rpartition("@")
    # Labels glued onto the front: "Email-abhilasha@x.in", "Mail:hr@x.com", "ID-hr@x.com"
    local = re.sub(r"^(?:e-?mail|mail|mailto|email ?id|id|contact|cv|resume|at)[-:]+(?=[a-z0-9])", "", local)
    parts = domain.split(".")
    tld = parts[-1]
    if tld not in KNOWN_TLDS and len(tld) > 3:
        for k in sorted(KNOWN_TLDS, key=len, reverse=True):
            if tld.startswith(k):
                parts[-1] = k
                break
    return f"{local}@{'.'.join(parts)}"

# Finds the tallest scrollable container (LinkedIn: <main id="workspace">),
# nudges it down a screen, then jumps it to the bottom. Returns the container's
# scrollHeight so the caller can tell whether more results were loaded.
SCROLL_JS = """
let el = null, best = 0;
const main = document.querySelector('main#workspace') || document.querySelector('main');
const cands = main ? [main, ...document.querySelectorAll('div, section')] : [...document.querySelectorAll('div, section, main')];
for (const c of cands) {
  const cs = getComputedStyle(c);
  if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && c.scrollHeight > c.clientHeight + 100 && c.scrollHeight > best) {
    best = c.scrollHeight; el = c;
  }
}
if (el) {
  el.scrollTop = el.scrollTop + Math.max(600, el.clientHeight * 0.9);
  el.dispatchEvent(new Event('scroll', {bubbles: true}));
  el.scrollTop = el.scrollHeight;
  el.dispatchEvent(new Event('scroll', {bubbles: true}));
  return el.scrollHeight;
}
window.scrollBy(0, 900);
window.scrollTo(0, document.documentElement.scrollHeight);
return document.documentElement.scrollHeight;
"""

# For each email, find the LinkedIn post it appears in and return that post's
# link (from its activity URN) and opening text, so the email can quote it.
POST_CONTEXT_JS = """
const emails = arguments[0], out = {};
const EM = /[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}/g;
const emSet = t => new Set(((t || '').toLowerCase().match(EM) || []));
// Vendor / distribution lists: many short items (lines, links) that each hold
// an address, side by side in one container, or a single block packed with
// addresses. A normal feed is long post cards, each with at most one address.
function listCount(el, post) {
  let n = emSet(post.innerText).size;
  for (let a = el, k = 0; a && a !== document.body && k < 14; a = a.parentElement, k++) {
    const s = emSet(a.innerText);
    if (s.size < 6) continue;
    const txt = a.innerText || '';
    if (a === el) {
      if (txt.length / s.size < 150) n = Math.max(n, s.size);
    } else {
      const shortItems = [...a.children].filter(c => {
        const t = c.innerText || '';
        return t.length < 150 && emSet(t).size >= 1;
      }).length;
      if (shortItems >= 6) n = Math.max(n, s.size);
    }
    break;
  }
  return n;
}
const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
let node;
while ((node = walker.nextNode())) {
  const t = (node.nodeValue || '').toLowerCase();
  for (const e of emails) {
    if (out[e] || !t.includes(e)) continue;
    let el = node.parentElement, post = null;
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      const attrs = (p.getAttribute('data-urn') || '') + (p.getAttribute('data-id') || '');
      if (/urn:li:(activity|ugcPost|share):/.test(attrs) || p.getAttribute('role') === 'article') { post = p; break; }
    }
    if (!post) {
      // Grow outward to ~300 chars, but never into a container that also holds
      // another post's email addresses (that would merge two posts).
      const ems = t => new Set(((t || '').toLowerCase().match(/[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}/g) || []));
      post = el;
      while (post.parentElement && post.parentElement !== document.body && (post.innerText || '').length < 300) {
        // Stop once this block has real text of its own (not just addresses) and the
        // parent would add another post's addresses. A bare "hr@ / careers@" line keeps
        // climbing to its post; a real post stops at its own boundary.
        const own = (post.innerText || '').replace(EM, '').replace(/\\s+/g, ' ').trim().length;
        if (own >= 25 && ems(post.parentElement.innerText).size > ems(post.innerText).size) break;
        post = post.parentElement;
      }
    }
    let url = '';
    for (let a = post, k = 0; a && a !== document.body && k < 8 && !url; a = a.parentElement, k++) {
      const h = a.outerHTML.length < 400000 ? a.outerHTML : '';
      const m = h.match(/urn(?::|%3A)li(?::|%3A)(activity|ugcPost|share)(?::|%3A)(\\d{15,})/i);
      if (m) { url = `https://www.linkedin.com/feed/update/urn:li:${m[1]}:${m[2]}/`; break; }
      const pl = h.match(/https:\\/\\/www\\.linkedin\\.com\\/posts\\/[^"'?\\s]+/);
      if (pl) url = pl[0];
    }
    out[e] = {
      url: url,
      n: listCount(el, post),
      text: (post.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 1500),
    };
  }
}
return out;
"""
post_context = {}   # email -> {"url": ..., "snippet": ...}

# DM / APPLY LEADS: posts with no email ("apply on LinkedIn / DM me") can't be mailed,
# so remote-India posts for your roles without an address are saved to logs/dm_leads.csv for manual follow-up.
# Read from the page already loaded: no extra LinkedIn requests.
NO_EMAIL_POSTS_JS = """
const EM = /[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}/i, out = [], seen = new Set();
for (const p of document.querySelectorAll('[data-urn], [data-id], [role="article"]')) {
  const attrs = (p.getAttribute('data-urn') || '') + ' ' + (p.getAttribute('data-id') || '');
  const m = attrs.match(/urn:li:(activity|ugcPost|share):(\\d{15,})/);
  if (!m || seen.has(m[2])) continue;
  const text = (p.innerText || '').replace(/\\s+/g, ' ').trim();
  if (text.length < 80 || EM.test(text)) continue;
  seen.add(m[2]);
  out.push({url: `https://www.linkedin.com/feed/update/urn:li:${m[1]}:${m[2]}/`, text: text.slice(0, 1500)});
}
return out;
"""
DM_LEADS_FILE = os.path.join(LOG_DIR, "dm_leads.csv")
FOREIGN_STARTUP_RE = re.compile(r"\b(sf|san francisco|bay area|silicon valley|us[- ]based|u\.s\.[- ]based|y ?combinator|yc|"
                                r"ex-?(apple|meta|google|amazon|microsoft|openai|stripe|uber)|esops?|seed[- ]funded|series [ab])\b", re.I)
HIRING_RE = re.compile(r"\b(hiring|we['’]?re hiring|looking for|open role|opening)\b", re.I)
_dm_seen = set()
try:
    import csv as _csv
    if os.path.exists(DM_LEADS_FILE):
        _dm_seen = {row[2] for row in _csv.reader(open(DM_LEADS_FILE, encoding="utf-8")) if len(row) > 2}
except Exception:
    _dm_seen = set()
dm_leads_new = 0

def dm_lead_ok(text):
    """Remote job in India, AI role, actually hiring, not abroad-only. Returns (ok, priority)."""
    if not (HIRING_RE.search(text) and AI_ROLE_RE.search(text) and REMOTE_RE.search(text) and INDIA_RE.search(text)):
        return False, False
    if BENCH_SALES_RE.search(text):
        return False, False
    if ONSITE_RE.search(text) and not FULLY_REMOTE_RE.search(text) and not re.search(r"remote\s*\(?\s*india", text, re.I):
        return False, False
    return True, bool(FOREIGN_STARTUP_RE.search(text))

def save_dm_leads(posts, query):
    """Append new qualifying no-email posts to logs/dm_leads.csv."""
    global dm_leads_new
    import csv
    new_rows = []
    for p in posts or []:
        url, text = p.get("url", ""), p.get("text", "")
        if not url or url in _dm_seen:
            continue
        ok, priority = dm_lead_ok(text)
        if not ok:
            continue
        _dm_seen.add(url)
        new_rows.append([time.strftime("%Y-%m-%d %H:%M"), "PRIORITY" if priority else "", url, query, make_snippet(text, "")])
    if new_rows:
        first = not os.path.exists(DM_LEADS_FILE)
        with open(DM_LEADS_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if first:
                w.writerow(["found_at", "priority", "post_url", "query", "snippet"])
            w.writerows(new_rows)
        dm_leads_new += len(new_rows)
        for r in new_rows:
            print(f"📌 DM lead{' ⭐' if r[1] else ''}: {r[2]} | {r[4][:90]}")

def make_snippet(text, email):
    """A short, readable quote from the post, starting at the hiring sentence."""
    text = re.sub(r"\s*…?\s*(see more|\.\.\.more)\s*$", "", text, flags=re.I)
    m = re.search(r"(?<!#)\b(we['’]?re hiring|we are hiring|hiring|opening|looking for)\b", text, flags=re.I)
    i = m.start() if m else 0
    if i:
        b = max((x.end() for x in re.finditer(r"[.!?]\s|\n", text[:i])), default=0)
        i = b if i - b < 120 else i
    chunk = text[i:i + 180].strip()
    return chunk + ("..." if len(text) > i + 180 else "")

MAX_MIN_EXPERIENCE = env.number("MAX_YEARS_ASKED")  # None (empty in .env) = apply regardless of the years a post asks for

# Product companies only: skip staffing agencies, "for our client" posts and
# large IT-services firms. Judged from the post text and the email domain.
AGENCY_TEXT_RE = re.compile(
    r"\bour clients?\b|\bfor (?:one of )?(?:our|a) (?:leading |reputed |esteemed )?clients?\b|\bon behalf of\b"
    r"|\bc2h\b|contract[- ]to[- ]hire|third[- ]party payroll|\bpayroll of\b|\bstaffing\b"
    r"|\bmanpower\b|\bplacement (?:agency|consultant)|\brecruitment (?:agency|firm|partner)\b"
    r"|\bc2c\b|corp[- ]to[- ]corp|\bw2\b|\bh-?1b\b|\bbench\b|hot ?list|vendor list|distribution list"
    r"|\bgc holders?\b|green card|\busc\b|\bead\b|\bvisa\b", re.I)
MAX_EMAILS_PER_POST = 5   # a post listing more addresses than this is a vendor / distribution list
MAX_NEW_PER_PAGE = 15     # more new emails than this from one page read = an undetected list
AGENCY_DOMAIN_WORDS = ("staffing", "consult", "recruit", "recruis", "placement", "manpower", "liveconnections",
                       "codersbrain", "hirenest", "talentpro", "headhunt", "executivesearch", "brillminds",
                       "hiring", "jobs", "career", "workforce", "outsourc", "hrsolution", "hrservice",
                       # well-known agency brands whose domains have no agency word
                       "michaelpage", "pagegroup", "randstad", "adecco", "teamlease", "quess", "abcconsultants", "hays",
                       "kellyservices", "manpowergroup", "cielhr", "xpheno", "naukri-rms", "spectrumtalent", "taggd",
                       # more staffing firms
                       "lancesoft", "resourcetree", "infohr", "talentgrid", "globaltalent", "hrsol", "staffingsol")
SERVICES_FIRMS = {"tcs", "infosys", "wipro", "hcl", "hcltech", "techmahindra", "cognizant", "accenture",
                  "capgemini", "ltimindtree", "lntinfotech", "mphasis", "genpact", "deloitte", "ey", "kpmg",
                  "pwc", "birlasoft", "hexaware", "coforge", "persistent", "zensar", "mindtree", "nagarro",
                  "ibm", "dxc", "atos", "ntt", "virtusa", "sopra", "sasken", "cyient",
                  "itcinfotech", "lntts", "ltts", "mastek", "kpit", "happiestminds", "sonata-software",
                  "sonatasoftware", "zensartech", "unisys", "conduent", "niit-tech", "iris", "irissoftware",
                  "ust", "globallogic", "quesscorp", "teamlease", "randstad", "adecco", "manpowergroup",
                  "ciel", "cielhr", "xoriant", "infogain", "R systems".replace(" ", "").lower(), "rsystems",
                  "3i-infotech", "3iinfotech", "hexaware", "sutherland", "firstsource", "wns", "exl", "exlservice"}
# Allowed locations: TARGET_CITIES from .env, or remote. A post naming only another city is skipped.
LOCATION_OK_RE = re.compile(r"\b(" + "|".join(env.lines("TARGET_CITIES")) + r"|remote|work from home|wfh)\b", re.I)
# Other Indian cities: a post naming only these (and none of TARGET_CITIES / remote) is skipped.
OTHER_CITY_RE = re.compile(r"\b(delhi|new delhi|faridabad|ghaziabad|bangalore|bengaluru|hyderabad|pune|mumbai|navi mumbai|chennai|kolkata|ahmedabad|"
                           r"coimbatore|kochi|trivandrum|thiruvananthapuram|indore|jaipur|chandigarh|mohali|"
                           r"vizag|visakhapatnam|bhubaneswar|nagpur|surat|vadodara|lucknow|mysore|mysuru|madurai)\b", re.I)
# The role itself must match ROLE_WORDS (.env): checked on the hiring line, not the whole post.
AI_ROLE_RE = re.compile(r"\b(" + env.get("ROLE_WORDS") + r")\b", re.I)
INTERN_RE = re.compile(r"\binternship\b|\binterns?\b|\bfreshers? only\b", re.I)
# Remote searches also surface jobs abroad: skip posts that name a foreign
# location and never mention India or an Indian city.
FOREIGN_RE = re.compile(r"\b(nigeria|lagos|kenya|usa|united states|u\.s\.|canada|uk|united kingdom|london|"
                        r"europe|germany|netherlands|dubai|uae|abu dhabi|saudi|riyadh|qatar|doha|singapore|"
                        r"malaysia|australia|philippines|pakistan|bangladesh|egypt|south africa|lahore|karachi|islamabad|"
                        r"nyc|new york|charlotte|texas|california|chicago|seattle|new jersey|atlanta|dallas|boston|"
                        r"rawalpindi|faisalabad|dhaka|kathmandu|colombo|sharjah|muscat|kuwait|bahrain|manama)\b", re.I)
# Country-code domains outside India (e.g. pitb.gov.pk) are never Indian jobs.
FOREIGN_TLDS = (".pk", ".bd", ".np", ".lk", ".ng", ".ke", ".za", ".eg", ".ae", ".sa", ".qa", ".om", ".kw",
                ".bh", ".uk", ".us", ".ca", ".au", ".sg", ".my", ".ph", ".de", ".nl", ".fr")
INDIA_RE = re.compile(r"\b(india|indian|noida|gurgaon|gurugram|delhi|ncr|bangalore|bengaluru|hyderabad|pune|"
                      r"mumbai|chennai|kolkata|ahmedabad|jaipur|chandigarh|indore|kochi)\b|\bIST\b", re.I)

# Hard blocks (always): personal mail (checked elsewhere), jobs outside India,
# US vendor / bench-sales lists. Soft checks (role, experience, city, agency,
# IT-services, internship) only apply when STRICT_FIT is on. STRICT_FIT=false reaches
# more companies, including agencies and IT-services firms.
STRICT_FIT = env.flag("STRICT_FIT", True)   # product companies in your roles and cities only
# REMOTE_ONLY: only mail posts for remote / work-from-home jobs.
REMOTE_ONLY = env.flag("REMOTE_ONLY", False)  # false: remote anywhere OR on-site/hybrid in TARGET_CITIES
if CITY_MODE:
    REMOTE_ONLY = False
REMOTE_RE = re.compile(r"\b(remote|work from home|work-from-home|wfh|work from anywhere|anywhere in india)\b", re.I)
ONSITE_RE = re.compile(r"\b(hybrid|on-?site|in-?office|work from office|wfo|\d+ days? (a week )?(in|from) (the )?office)\b", re.I)
FULLY_REMOTE_RE = re.compile(r"\b(fully remote|100% remote|remote[- ]first|remote only|permanent(ly)? remote|completely remote|full[- ]time remote)\b", re.I)
BENCH_SALES_RE = re.compile(r"\bc2c\b|corp[- ]to[- ]corp|\bw2\b|\bh-?1b\b|\bbench\b|hot ?list|vendor list|"
                            r"distribution list|\bgc holders?\b|green card|\busc\b|\bead\b", re.I)

def hard_block(email, ctx):
    """Leads never worth mailing: outside India or US vendor/bench-sales lists."""
    text = ctx.get("text", "")
    if email.lower().endswith(FOREIGN_TLDS):
        return "foreign country domain"
    if ctx.get("n_emails", 0) > MAX_EMAILS_PER_POST:
        return f"post lists {ctx['n_emails']} email addresses (vendor / distribution list)"
    if BENCH_SALES_RE.search(text):
        return "US bench-sales / C2C post"
    if FOREIGN_RE.search(text) and not INDIA_RE.search(text):
        return "job located outside India"
    if CITY_MODE and len(re.sub(r"[\w.%+-]+@[\w.-]+\.[a-z]{2,}", "", text, flags=re.I).strip()) > 20:
        if not LOCATION_OK_RE.search(text) and (OTHER_CITY_RE.search(text) or FOREIGN_RE.search(text)):
            return "not in TARGET_CITIES or remote"
    if REMOTE_ONLY and len(re.sub(r"[\w.%+-]+@[\w.-]+\.[a-z]{2,}", "", text, flags=re.I).strip()) > 20:   # real post text captured
        if not REMOTE_RE.search(text):
            return "not a remote job"
        if ONSITE_RE.search(text) and not FULLY_REMOTE_RE.search(text):
            return "hybrid / onsite, not fully remote"
    return ""

def skip_reason(email, ctx):
    """Why this lead shouldn't be mailed, or '' to send."""
    why = hard_block(email, ctx)
    if why or not STRICT_FIT:
        return why
    domain = email.split("@")[-1].split(".")[0].lower()
    if domain in SERVICES_FIRMS:
        return "IT-services firm, not a product company"
    if any(w in domain.replace("-", "") for w in AGENCY_DOMAIN_WORDS):   # hyphens can hide a word ("x-hr-solutions")
        return "staffing/recruitment agency domain"
    text = ctx.get("text", "")
    if AGENCY_TEXT_RE.search(text):
        return "agency / 'for our client' post"
    if len(re.sub(r"[\w.%+-]+@[\w.-]+\.[a-z]{2,}", "", text, flags=re.I).strip()) > 20:   # real post text captured
        if not LOCATION_OK_RE.search(text) and (OTHER_CITY_RE.search(text) or FOREIGN_RE.search(text)):
            return "not in TARGET_CITIES or remote"
        head = make_snippet(text.replace("#", ""), email)[:110]
        if not AI_ROLE_RE.search(head):
            return f"not a target role ({head[:45].strip()}...)"
    if INTERN_RE.search(text):
        return "internship / fresher post"
    if MAX_MIN_EXPERIENCE is not None and ctx.get("min_exp") is not None and ctx["min_exp"] > MAX_MIN_EXPERIENCE:
        return f"post asks for {ctx['min_exp']}+ years (target 1-{MAX_MIN_EXPERIENCE})"
    return ""

def min_experience(text):
    """Minimum years the post asks for, or None.
    Reads the labelled field first ("Experience: 8+ Years", "Exp: 5.5 to 10"),
    because posts often mention smaller numbers elsewhere ("2 years of Python")."""
    t = text.lower().replace("*", "")
    m = re.search(r"\b(?:experience|exp|yoe|work ex)\b\s*(?:required|needed)?\s*[:\-–|]\s*(\d{1,2})(?:\.\d+)?", t)
    if m and int(m.group(1)) <= 30:
        return int(m.group(1))
    mins = [int(m.group(1)) for m in re.finditer(
        r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|—|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years|yrs|yr|year)", t)]
    mins = [m for m in mins if m <= 30]
    return min(mins) if mins else None

# Per-query exit rules (the ONLY two ways we leave a city and move to the next):
#   1. LinkedIn has no more postings to load  (page stops growing / "No more results")
#   2. No new email for IDLE_LIMIT seconds
IDLE_LIMIT = 60
END_OF_RESULTS_TEXT = ["No more results", "You've reached the end", "you\u2019ve reached the end"]
NO_GROWTH_ROUNDS = 3          # page height unchanged this many scrolls in a row = end
MAX_DURATION = 100 * 60       # overall safety cap across ALL queries
PAGE_LOAD_BUDGET = SAFE_DAILY_LOADS   # 15/day to start, +15%/week, cap 40 (see SAFE PACING above)
MAX_LOADS_PER_QUERY = 3       # 62% of all emails ever found came in the first 3 scrolls
# --max-loads=N: a lighter run (e.g. right after a LinkedIn pause)
for _a in sys.argv:
    if _a.startswith("--max-loads="):
        PAGE_LOAD_BUDGET = int(_a.split("=", 1)[1])
if EXPLORE:
    PAGE_LOAD_BUDGET = 60     # keep test runs light on LinkedIn
    MAX_LOADS_PER_QUERY = 5   # equal, small sample per test query
page_loads = 0
_last_q = 0
start_time = time.time()
stop_all = False
budget_hit = False

print(f"Searching {len(SEARCH_QUERIES)} queries (Posts; {'ALL past week (catch-up)' if CATCH_UP_WEEK else 'core last 24h, rotating past week'}). Each runs until LinkedIn "
      f"runs out of postings or {IDLE_LIMIT // 60} min pass with no new email. "
      f"Cap {MAIL_LIMIT} emails / {MAX_DURATION // 60} min total.")

for qi, query in enumerate(SEARCH_QUERIES, 1):
    if stop_all or len(found_this_session) >= MAIL_LIMIT:
        break
    if (time.time() - start_time) >= MAX_DURATION:
        print("⏰ Overall time cap reached.")
        break

    _last_q = qi
    if qi > 1:
        time.sleep(random.uniform(30, 90))   # a person reads the results before searching again
    print(f"\n🔍 [{qi}/{len(SEARCH_QUERIES)}] {query}  ({QUERY_WINDOW.get(query, 'past-24h')})")
    try:
        driver.get(content_search_url(query, QUERY_WINDOW.get(query, "past-24h")))
    except Exception as e:
        if "no such window" in str(e).lower() or "invalid session id" in str(e).lower():
            print("❌ Browser window was closed or Chrome crashed. Ending the scrape; sending what was found.")
            stop_all = True
            break
        print(f"   ⚠️ Could not load search: {str(e).splitlines()[0][:120]}")
        continue
    time.sleep(random.uniform(8, 14))

    q_start = time.time()
    q_found = 0
    q_loads = 0
    last_new_email_at = time.time()
    prev_height = 0
    same_height_rounds = 0

    while len(found_this_session) < MAIL_LIMIT and (time.time() - start_time) < MAX_DURATION:
        # 1. Scroll THE RIGHT THING. LinkedIn's results live in a scrollable
        #    <main> container; the page body itself never scrolls, so Page Down
        #    on the body does nothing. Scroll the container to its bottom (this
        #    is what triggers LinkedIn to load the next batch), then pause.
        try:
            height = driver.execute_script(SCROLL_JS) or 0
            page_loads += 1
            q_loads += 1
            time.sleep(random.uniform(14, 28))   # read the page like a person, unevenly
        except Exception as e:
            if "no such window" in str(e).lower():
                print("❌ Browser window was closed. Stopping.")
                stop_all = True
                break
            height = prev_height

        # Per-run page-load budget: end the scrape by our own rule, cleanly.
        if page_loads >= PAGE_LOAD_BUDGET:
            print(f"   🧮 Page-load budget reached ({PAGE_LOAD_BUDGET}). Ending scrape for this run.")
            budget_hit = True
            stop_all = True
            break

        # 2. Read the page.
        try:
            page_source = driver.page_source or ""
        except Exception:
            page_source = ""

        # LinkedIn rate limit: keep scrolling here and the block only gets longer.
        if any(k in page_source for k in RATE_LIMIT_SIGNS):
            print("\n🛑 LinkedIn has PAUSED search on this account (unusual search traffic).")
            print("   Stopping the scrape now so the block does not get extended.")
            print("   Wait 24 hours before running again. Do NOT search manually in the meantime.")
            with open(RATE_LIMIT_FLAG, "w") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            record_usage(paused=True)
            stop_all = True
            break
        try:
            _u = driver.current_url
        except Exception:
            _u = ""
        if any(k in _u for k in ("/login", "authwall", "checkpoint", "/uas/")):
            print("\n🛑 LinkedIn asked to re-verify the session mid-run (early warning sign). Stopping now.")
            record_usage(paused=True)
            stop_all = True
            break

        # 2b. Remote-India posts for your roles without an email -> DM / apply leads (no extra page load).
        try:
            save_dm_leads(driver.execute_script(NO_EMAIL_POSTS_JS), query)
        except Exception as ex:
            print(f"   (dm-lead scan skipped: {type(ex).__name__})")

        # 3. Extract emails.
        new_here = 0
        page_new = []
        for email in re.findall(EMAIL_RE, page_source):
            email = clean_email(email)
            if any(word in email for word in trash_keywords) or is_personal_email(email):
                continue
            if email in already_sent or email in found_this_session or email in rejected_this_session:
                continue
            if len(found_this_session) >= MAIL_LIMIT:
                break
            found_this_session.add(email)
            page_new.append(email)
            email_query[email] = query
            new_here += 1
            q_found += 1
            print(f"➕ Found: {email} ({len(found_this_session)}/{MAIL_LIMIT}) "
                  f"| {int(time.time() - q_start)}s into this query")
        if new_here:
            last_new_email_at = time.time()
            missing = [e for e in found_this_session if e not in post_context]
            try:
                for e, c in (driver.execute_script(POST_CONTEXT_JS, missing) or {}).items():
                    post_context[e] = {"url": c.get("url", ""), "snippet": make_snippet(c.get("text", ""), e),
                                       "min_exp": min_experience(c.get("text", "")),
                                       "text": c.get("text", ""), "n_emails": c.get("n", 0)}
            except Exception as ex:
                print(f"   ⚠️ Could not read post context: {str(ex).splitlines()[0][:100]}")
            for e in missing:
                why = skip_reason(e, post_context.get(e) or {})
                if why:
                    found_this_session.discard(e)
                    rejected_this_session.add(e)
                    reject_reason[e] = why
                    print(f"   ✖ Dropped {e}: {why}")
            # Safety net: a normal results page yields ~5-10 new emails. If one
            # read still leaves more than this, it is a vendor / distribution
            # list the post detector missed, so drop the whole batch.
            kept = [e for e in page_new if e in found_this_session]
            if len(kept) > MAX_NEW_PER_PAGE:
                for e in kept:
                    found_this_session.discard(e)
                    rejected_this_session.add(e)
                    reject_reason[e] = "page burst (undetected vendor list)"
                print(f"   ✖ Dropped {len(kept)} emails from one page read (more than {MAX_NEW_PER_PAGE}: "
                      f"looks like a vendor / distribution list)")

        # 4a. Exit rule 1: no more postings to load.
        if height <= prev_height:
            same_height_rounds += 1
            try:
                driver.find_element(By.TAG_NAME, 'h1').click()   # refocus nudge
            except Exception:
                pass
        else:
            same_height_rounds = 0
        prev_height = height
        end_text = any(t in page_source for t in END_OF_RESULTS_TEXT)
        if end_text or same_height_rounds >= NO_GROWTH_ROUNDS:
            print(f"   🏁 No more postings for this query ({q_found} emails, "
                  f"{int(time.time() - q_start)}s). Next city.")
            break

        if q_loads >= MAX_LOADS_PER_QUERY:
            print(f"   🧪 Sampled {q_loads} loads ({q_found} emails). Next query.")
            break

        # 4b. Exit rule 2: nothing new for IDLE_LIMIT seconds.
        idle = time.time() - last_new_email_at
        if idle >= IDLE_LIMIT:
            print(f"   ⏭️ No new email for {int(idle)}s ({q_found} emails, "
                  f"{int(time.time() - q_start)}s). Next city.")
            break

_rotating_done = max(0, _last_q - len(BASE_QUERIES))
try:
    if EXPLORE:
        raise RuntimeError("explore mode: rotation untouched")
    open(_ROT_FILE, "w").write(str((_start + _rotating_done) % len(EXTRA_POOL)))
except Exception:
    pass
record_usage(loads=page_loads)
print(f"\n📋 Scrape finished: {len(found_this_session)} new emails, {page_loads} page loads"
      f"{' (budget reached)' if budget_hit else ''}, across {_last_q} queries "
      f"in {int((time.time() - start_time) // 60)}m {int((time.time() - start_time) % 60)}s.")
driver.quit()

try:
    with open(os.path.join(LOG_DIR, time.strftime("posts_%Y-%m-%d.jsonl")), "a") as _f:
        for _e, _q in email_query.items():
            _c = post_context.get(_e) or {}
            _f.write(json.dumps({"email": _e, "query": _q, "kept": _e in found_this_session,
                                 "reason": reject_reason.get(_e, ""), "min_exp": _c.get("min_exp"),
                                 "text": _c.get("text", "")[:1500]}) + "\n")
except Exception as _ex:
    print(f"⚠️ Could not save post log: {_ex}")

if EXPLORE:
    import collections
    AI_RE = AI_ROLE_RE
    rep_lines = [f"EXPLORE REPORT {time.strftime('%Y-%m-%d %H:%M')}  ({page_loads} page loads)", ""]
    leads = []
    for q in SEARCH_QUERIES[:_last_q]:
        mine = [e for e, qq in email_query.items() if qq == q]
        kept = [e for e in mine if e in found_this_session]
        why = collections.Counter(reject_reason[e].split(" (")[0] if "email addresses" not in reject_reason[e]
                                  else "vendor / distribution list" for e in mine if e in reject_reason)
        rel = sum(1 for e in kept if AI_RE.search((post_context.get(e) or {}).get("text", "")))
        rep_lines.append(f"[{QUERY_WINDOW.get(q,'?')}] {q}")
        rep_lines.append(f"   found {len(mine)} | kept {len(kept)} (role match {rel}) | dropped: "
                         + (", ".join(f"{v} {k}" for k, v in why.most_common()) or "none"))
        for e in kept:
            ctx = post_context.get(e) or {}
            rep_lines.append(f"     ✓ {e:40} {ctx.get('snippet','(no post text)')[:110]}")
            leads.append({"email": e, "query": q, "ctx": ctx})
        rep_lines.append("")
    tot_kept = len(leads)
    rep_lines.append(f"TOTAL: found {len(email_query)} | kept {tot_kept} | dropped {len(reject_reason)}")
    report = "\n".join(rep_lines)
    print("\n" + report)
    stamp = time.strftime("%Y-%m-%d_%H-%M")
    open(os.path.join(LOG_DIR, f"explore_report_{stamp}.txt"), "w").write(report)
    json.dump(leads, open(os.path.join(LOG_DIR, f"explore_leads_{stamp}.json"), "w"), indent=1)
    print(f"\n🧪 Explore finished. Nothing was sent. Report + leads saved in logs/.")
    sys.exit(0)

# --- 5. EXECUTION ---
# Re-queue sends that failed on a previous run (network drops etc.).
if os.path.exists(RETRY_FILE):
    with open(RETRY_FILE) as f:
        for line in f:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            e = item.get("email", "").lower()
            if e and e not in already_sent and not is_personal_email(e):
                found_this_session.add(e)
                if item.get("ctx"):
                    post_context.setdefault(e, item["ctx"])
                print(f"↩️  Retrying earlier failed send: {e}")
    os.remove(RETRY_FILE)

print(f"\nStarting batch send for {len(found_this_session)} recipients...")
sent_count = 0
ok_sends = 0
skipped_fit = 0
total_to_send = len(found_this_session)

# One email per company domain per run (several addresses at one company look like spam)
_one_per_domain, _domains_seen = [], set()
for _t in sorted(found_this_session):
    _d = _t.split("@")[-1].lower()
    if _d in _domains_seen:
        print(f"⏩ Skipping {_t}: already emailing someone at {_d} this run")
        continue
    _domains_seen.add(_d)
    _one_per_domain.append(_t)
total_to_send = len(_one_per_domain)
for target in _one_per_domain:
    if sent_today_count() >= SEND_DAILY_CAP:
        print(f"🛑 Daily cap of {SEND_DAILY_CAP} applications reached. "
              f"{total_to_send - sent_count} found emails left unsent (they will be found again next run).")
        break
    ctx = post_context.get(target) or {}
    why = skip_reason(target, ctx)
    if why:
        print(f"⏩ Skipping {target}: {why}")
        sent_count += 1
        skipped_fit += 1
        continue
    success = send_application(target, ctx)
    sent_count += 1
    if success:
        ok_sends += 1
        remaining_emails = total_to_send - sent_count
        eta_seconds = remaining_emails * sum(SEND_GAP) // 2
        print(f"📈 Progress: {sent_count}/{total_to_send} sent | today {sent_today_count()}/{SEND_DAILY_CAP} "
              f"| ETA: {eta_seconds // 60}m {eta_seconds % 60}s remaining")

    # Don't sleep after the very last email
    if sent_count < total_to_send:
        time.sleep(random.uniform(*SEND_GAP))  # random gap to stay clear of spam filters

 
print("\n" + "=" * 70)
print(f"🏁 RUN END    {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"   Found {len(found_this_session)} | Sent {ok_sends} | Skipped (not a fit) {skipped_fit} "
      f"| Failed {sent_count - ok_sends - skipped_fit} "
      f"| Blacklist now {len(get_already_sent_emails())} unique")
print(f"   Full log: {LOG_FILE}")
print("=" * 70)