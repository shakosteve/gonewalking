"""Unattended update, for Windows Task Scheduler (via sync.bat): sync, then photos, then a report.

Runs `gonewalking.py sync` and, when PHOTOS_DIR is set, `gonewalking.py photos`, writes everything
they print to data/logs/sync-YYYYMMDD-HHMM.log (the newest 30 are kept), and reports how it went
with a Windows notification (click it to open the log). Only shows when you're signed in, so set
the task to "Run only when user is logged on". Optional settings in .env:

    WINDOWS_NOTIFY=off               no Windows notification
    REPORT_ON=always                 "always" (default), or "failure" for failures only
    REPORT_EMAIL=you@example.com     also e-mail the report (needs an SMTP password):
    SMTP_PASSWORD=xxxx xxxx xxxx xxxx  for Gmail, an app password (Google Account > Security >
                                     2-Step Verification > App passwords), not your usual password
    SMTP_USER=you@gmail.com          optional: the account it's sent from (default REPORT_EMAIL)
    SMTP_HOST=smtp.gmail.com         optional (default smtp.gmail.com)
    SMTP_PORT=587                    optional (default 587, STARTTLS)

The report says whether it worked, lists new walks with their stats and share links, gives the git
commands to publish when there are uncommitted changes, and says when the MapMyFitness token expires
(from MMF_TOKEN_EXPIRES=YYYY-MM-DD, or "expires YYYY-MM-DD" in .env), with the steps to renew it once
it's within 14 days of expiring or has been rejected.

    python scheduled_sync.py            run it
    python scheduled_sync.py --dry-run  run it, but print the report instead of sending it
    python scheduled_sync.py --test     just show a test notification
    python scheduled_sync.py --sample   send the report as if the latest walk were new (no sync)
"""

import json
import os
import re
import smtplib
import subprocess
import sys
import time
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import exports

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
LOG_DIR = ROOT / "data" / "logs"
PLACES = ROOT / "data" / "places.json"
WORKOUTS_JS = ROOT / "docs" / "data" / "workouts.js"
INDEX_HTML = ROOT / "docs" / "index.html"
KEEP_LOGS = 30
STEP_TIMEOUT_S = 3 * 3600
TOKEN_WARN_DAYS = 14


def load_env():
    if not ENV.exists():
        return
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def token_expiry():
    text = os.environ.get("MMF_TOKEN_EXPIRES") or ""
    if not text and ENV.exists():
        m = re.search(r"expires\s+(\d{4}-\d{2}-\d{2})", ENV.read_text(encoding="utf-8"), re.I)
        text = m.group(1) if m else ""
    try:
        return date.fromisoformat(text.strip()) if text.strip() else None
    except ValueError:
        return None


def known_walks():
    try:
        return json.loads(PLACES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def site_walks():
    """The published walks by ID, for stats in the report."""
    try:
        text = WORKOUTS_JS.read_text(encoding="utf-8")
        return {w["id"]: w for w in json.loads(text[text.index("=") + 1:].strip().rstrip(";"))["workouts"]}
    except (OSError, ValueError, KeyError):
        return {}


def site_url():
    """The dashboard's address, from the home page's og:url tag."""
    try:
        m = re.search(r'<meta property="og:url" content="([^"]+)"', INDEX_HTML.read_text(encoding="utf-8"))
        return m.group(1).rstrip("/") + "/" if m else None
    except OSError:
        return None


def unpublished_changes():
    """Changed files that haven't been committed (git status lines), or None if git isn't usable.
    .gitignore keeps out .env, data/ and photo_drop/, so these are the files that belong on GitHub."""
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        return [l for l in proc.stdout.splitlines() if l.strip()] if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


TOKEN_STEPS = """To renew the MapMyFitness token:
  1. Log in at https://www.mapmyfitness.com and open DevTools (F12).
  2. Application > Cookies > https://www.mapmyfitness.com: copy the value of auth-token.
  3. In C:\\code\\GoneWalking\\.env, paste it after MMF_AUTH_TOKEN=
  4. Copy the date from the auth-token-expiry cookie into .env's "This token expires YYYY-MM-DD" line,
     so these reports know the new date.
  5. Check it works: python gonewalking.py stats"""


def publish_steps(changed, latest_day):
    message = f"Walks through {latest_day}" if latest_day else "Update site data"
    count = f" ({len(changed)} changed file{'s' if len(changed) != 1 else ''})" if changed else ""
    return (f"To publish to the site{count}, in a terminal:\n"
            "  cd C:\\code\\GoneWalking\n"
            "  git add .\n"
            f'  git commit -m "{message}"\n'
            "  git push\n"
            "GitHub Pages republishes within a minute or two.")


def walk_lines(p, w, base_url):
    """One new walk for the report: where, stats and its share link."""
    where = " · ".join(x for x in (p.get("place") or p.get("city"), p.get("place_detail")) if x) or "Unknown place"
    lines = [f"  {p.get('date')}  {p.get('activity', 'Walk')} at {where}"]
    if w:
        kind = "walk" if w["activity"] == "Walk" else "ride"
        stats = [f"{w['mi']:.2f} mi {kind}", exports.format_duration(w["dur"]), exports.format_pace(w["pace"]),
                 f"{w['ft']:,} ft climbing" if w.get("ft") else "", f"{w['steps']:,} steps" if w.get("steps") else ""]
        lines.append("    " + " · ".join(s for s in stats if s))
        if base_url:
            lines.append(f"    Share: {base_url}w/{w['id']}.html")
    return lines


def run_step(args, log):
    """Run one gonewalking.py command; its output goes to the log. Returns (ok, last lines)."""
    log.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S}  python gonewalking.py {' '.join(args)}\n")
    log.flush()
    started = time.time()
    try:
        proc = subprocess.run([sys.executable, "-u", str(ROOT / "gonewalking.py"), *args], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=STEP_TIMEOUT_S, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        out, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired as exc:
        out, code = (exc.stdout or "") + f"\nStopped after {STEP_TIMEOUT_S // 60} minutes.", -1
    log.write(out)
    log.write(f"=== exit code {code} after {time.time() - started:.0f}s\n")
    log.flush()
    lines = [l for l in out.splitlines() if l.strip()]
    return code == 0, lines


# Shows a notification as Windows PowerShell (an app every Windows has registered), so nothing needs
# installing. The notification's XML arrives in an environment variable to avoid quoting trouble.
TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:GW_TOAST_XML)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def send_toast(subject, body, failed, log_path, dry_run):
    if os.environ.get("WINDOWS_NOTIFY", "on").strip().lower() in ("off", "no", "0", "false"):
        return None
    # A notification has room for a few short lines: the start of the report, without the log path.
    lines = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("Full log:")][:4]
    texts = [f"<text>{escape(subject)}</text>"] + [f"<text>{escape(l)}</text>" for l in lines[:2]]
    if lines[2:]:
        texts.append(f'<text placement="attribution">{escape(" · ".join(lines[2:]))}</text>')
    opening, actions = "<toast", ""
    if log_path:
        uri = quoteattr(Path(log_path).as_uri())
        opening += f' activationType="protocol" launch={uri}'
        actions = f'<actions><action content="Open log" activationType="protocol" arguments={uri}/></actions>'
    if failed:
        opening += ' duration="long"'
    xml = f'{opening}><visual><binding template="ToastGeneric">{"".join(texts)}</binding></visual>{actions}</toast>'
    if dry_run:
        print("----- Windows notification (not shown: --dry-run) -----")
        print("\n".join([subject] + lines))
        return "notification printed (dry run)"
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", TOAST_PS],
                              env=dict(os.environ, GW_TOAST_XML=xml), capture_output=True, text=True, timeout=60)
        if proc.returncode:
            return f"couldn't show the notification ({(proc.stderr or proc.stdout).strip()[:200]})"
        return "Windows notification shown"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"couldn't show the notification ({type(exc).__name__})"


def send_report(subject, body, dry_run):
    to = os.environ.get("REPORT_EMAIL", "").strip()
    if not to:
        return None
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    user = os.environ.get("SMTP_USER", "").strip() or to
    msg["From"] = f"GoneWalking <{user}>"
    msg.set_content(body)
    if dry_run:
        print("----- e-mail (not sent: --dry-run) -----")
        print(f"To: {to}\nSubject: {subject}\n\n{body}")
        return "e-mail printed (dry run)"
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not password:
        return "REPORT_EMAIL is set but SMTP_PASSWORD isn't, so no e-mail"
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=60) as smtp:
            smtp.starttls()
            smtp.login(user, password.replace(" ", ""))
            smtp.send_message(msg)
        return f"report e-mailed to {to}"
    except (smtplib.SMTPException, OSError) as exc:
        return f"couldn't send the e-mail ({type(exc).__name__}: {exc})"


def notify(subject, body, failed, log_path, dry_run):
    """Send the report every configured way; returns what happened, for the log."""
    results = [r for r in (send_toast(subject, body, failed, log_path, dry_run), send_report(subject, body, dry_run)) if r]
    return "; ".join(results) or "WINDOWS_NOTIFY=off and no REPORT_EMAIL, so no report sent"


def build_report(new, steps, failures, log_path, sample=False):
    """The report's subject and body, and the days left on the token (None if its expiry isn't known).
    A sample shows the publish and token-renewal steps even when they aren't needed yet."""
    expiry = token_expiry()
    days_left = (expiry - date.today()).days if expiry else None
    token_soon = days_left is not None and days_left <= TOKEN_WARN_DAYS
    auth_failed = False

    report = []
    if failures:
        report.append("The update FAILED.")
        for name, lines in failures:
            auth = any("auth-token" in l or "expired" in l.lower() or "401" in l for l in lines)
            auth_failed |= auth
            report.append(f"\n{name}: " + ("the MapMyFitness login token has expired or was rejected (renewal "
                                           "steps below)." if auth else "it stopped with an error."))
            report.append("Last lines:\n  " + "\n  ".join(lines[-15:] or ["(no output)"]))
    else:
        report.append("The update worked.")
    if new:
        walks, base_url = site_walks(), site_url()
        report.append(f"\n{len(new)} new {'walk' if len(new) == 1 else 'walks'}:")
        for p in new:
            report.extend(walk_lines(p, walks.get(p["id"]), base_url))
    elif not failures:
        report.append("\nNo new walks since the last run.")
    for name, ok, lines in steps:
        summary = next((l for l in reversed(lines) if l.startswith(("Photos:", "Places:", "Site data:"))), None)
        if ok and name == "photos" and summary:
            report.append(f"\n{summary.strip()}")
    changed = unpublished_changes()
    if changed or sample:
        report.append("\n" + publish_steps(changed, new[-1].get("date") if new else None))

    if expiry is None:
        report.append("\nMapMyFitness token: expiry date unknown (add \"This token expires YYYY-MM-DD\" to .env).")
    elif days_left < 0:
        report.append(f"\nMapMyFitness token: EXPIRED on {expiry} ({-days_left} days ago).")
    else:
        report.append(f"\nMapMyFitness token: expires {expiry} (in {days_left} day{'s' if days_left != 1 else ''})"
                      + (" - renew it soon." if token_soon else "."))
    if token_soon or auth_failed or sample:
        report.append(TOKEN_STEPS)
    if log_path:
        report.append(f"\nFull log: {log_path}")
    body = "\n".join(report)

    if new and not failures:
        subject = f"GoneWalking: {len(new)} new {'walk' if len(new) == 1 else 'walks'}"
    elif failures:
        subject = f"GoneWalking update FAILED ({', '.join(n for n, _ in failures)})"
    else:
        subject = "GoneWalking: no new walks"
    if token_soon and not failures:
        subject += " · token expires soon"
    return subject, body, days_left


def main():
    dry_run = "--dry-run" in sys.argv
    load_env()
    if "--test" in sys.argv:
        print(notify("GoneWalking: test", "If you can read this, scheduled reports will reach you.", False, None, dry_run))
        return
    if "--sample" in sys.argv:
        # The report as it would read if the latest walk had just been downloaded; nothing is synced.
        walks = known_walks()
        latest = max(walks, key=lambda k: walks[k].get("date", ""), default=None)
        new = [dict(walks[latest], id=latest)] if latest else []
        subject, body, _ = build_report(new, [], [], None, sample=True)
        print(notify(f"[Sample] {subject}", body, False, None, dry_run))
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now()
    log_path = LOG_DIR / f"sync-{stamp:%Y%m%d-%H%M}.log"
    before = known_walks()

    steps, failures = [], []
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"GoneWalking scheduled update, {stamp:%Y-%m-%d %H:%M}\n")
        commands = [["sync"]] + ([["photos"]] if os.environ.get("PHOTOS_DIR", "").strip() else [])
        for args in commands:
            ok, lines = run_step(args, log)
            steps.append((args[0], ok, lines))
            if not ok:
                failures.append((args[0], lines))
                if args[0] == "sync":
                    break  # nothing new to do photos for

    after = known_walks()
    new = sorted((dict(p, id=k) for k, p in after.items() if k not in before), key=lambda p: p.get("date", ""))
    subject, body, days_left = build_report(new, steps, failures, log_path)

    want = os.environ.get("REPORT_ON", "always").strip().lower()
    if failures or want != "failure" or (days_left is not None and days_left <= TOKEN_WARN_DAYS):
        outcome = notify(subject, body, bool(failures), log_path, dry_run)
    else:
        outcome = "worked; REPORT_ON=failure, so no report sent"
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n{subject}\n{outcome}\n")
    print(f"{subject} ({outcome}). Log: {log_path}")

    for old in sorted(LOG_DIR.glob("sync-*.log"))[:-KEEP_LOGS]:
        old.unlink()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
