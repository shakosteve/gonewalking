"""Unattended update, for Windows Task Scheduler (via sync.bat): sync, then photos, publish, report.

Runs `gonewalking.py sync` and, when PHOTOS_DIR is set and there are new walks or photos waiting
there, `gonewalking.py photos`. With AUTO_PUBLISH=on, new walks or photos are then committed (docs/
only) and pushed to GitHub. Everything is logged to data/logs/sync-YYYYMMDD-HHMM.log (the newest 30
are kept). It reports only when something happened (new walks or photos, a failure, or the token
within 14 days of expiring), with a Windows notification (click it to open the log; only shown when
you're signed in) and/or an e-mail. Optional settings in .env:

    AUTO_PUBLISH=on                  commit and push new walks and photos (default off: the report
                                     gives the git commands instead)
    WINDOWS_NOTIFY=off               no Windows notification
    REPORT_ON=always                 "always" (default): report new walks and photos too, or "failure"
                                     for failures and the token warning only
    REPORT_EMAIL=you@example.com     also e-mail the report (needs an SMTP password):
    SMTP_PASSWORD=xxxx xxxx xxxx xxxx  for Gmail, an app password (Google Account > Security >
                                     2-Step Verification > App passwords), not your usual password
    SMTP_USER=you@gmail.com          optional: the account it's sent from (default REPORT_EMAIL)
    SMTP_HOST=smtp.gmail.com         optional (default smtp.gmail.com)
    SMTP_PORT=587                    optional (default 587, STARTTLS)

The report says whether it worked, lists new walks (and older walks with new photos) with their
stats and share links, says whether they were published (or gives the git commands to publish them),
and says when the MapMyFitness token expires
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
from email.utils import make_msgid
from html import escape as h
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import exports
import photos
import previews

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
LOG_DIR = ROOT / "data" / "logs"
PLACES = ROOT / "data" / "places.json"
SITE_DATA = ROOT / "docs" / "data"
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


def site_data(name, default):
    """A docs/data/*.js file's value, or default if it can't be read."""
    try:
        return previews.read_js(SITE_DATA / name)
    except (OSError, ValueError):
        return default


def is_on(name, default="off"):
    return os.environ.get(name, default).strip().lower() in ("on", "yes", "1", "true")


def drop_folders():
    return [ROOT / f.strip() for f in os.environ.get("PHOTOS_DIR", "").split(";") if f.strip()]


def photos_waiting():
    """Whether the photo drop folders hold any photos (published ones are cleared out of them)."""
    return any(p.suffix.lower() in photos.PHOTO_EXTS for folder in drop_folders() if folder.exists()
               for p in folder.rglob("*"))


def photo_counts():
    """Published photos per walk."""
    return {wid: len(album) for wid, album in site_data("photos.js", {}).items()}


def git(args, log):
    """Run one git command, logged. Never waits for a sign-in prompt: with nobody there, it fails instead."""
    log.write(f"\n=== git {' '.join(args)}\n")
    try:
        proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never"))
        out, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        out, code = f"{type(exc).__name__}: {exc}", -1
    log.write(out + f"=== exit code {code}\n")
    log.flush()
    return code == 0, [l for l in out.splitlines() if l.strip()]


MANUAL_PUBLISH = ["cd C:\\code\\GoneWalking", "git add .", 'git commit -m "{message}"', "git push"]


def publish(message, log):
    """Commit docs/ (only generated site files, so half-done code edits stay out) and push it.
    Returns (ok, what went wrong, the commands left to run by hand)."""
    finish = ["cd C:\\code\\GoneWalking", "git pull --rebase --autostash", "git push"]
    ok, lines = git(["add", "docs"], log)
    if ok:
        staged, _ = git(["diff", "--cached", "--quiet"], log)   # exit 0: nothing staged
        if staged:
            return True, "", []
        ok, lines = git(["commit", "-m", message], log)
    if not ok:
        return False, "git couldn't commit the changes", [c.format(message=message) for c in MANUAL_PUBLISH]
    ok, lines = git(["pull", "--rebase", "--autostash"], log)
    if not ok:
        git(["rebase", "--abort"], log)
        return False, "the changes were committed, but bringing in GitHub's newer commits failed", finish
    ok, lines = git(["push"], log)
    if not ok:
        return False, "the changes were committed, but pushing to GitHub failed", finish[:1] + finish[2:]
    return True, "", []


def commit_message(new, photo_walks):
    where = lambda p: f"{p.get('place') or p.get('city') or 'Unknown place'}, {p.get('date')}"
    if len(new) == 1:
        return f"New walk: {where(new[0])}"
    if new:
        return f"{len(new)} new walks through {new[-1].get('date')}"
    if len(photo_walks) == 1:
        return f"New photos: {where(photo_walks[0][0])}"
    return f"New photos on {len(photo_walks)} walks"


TOKEN_STEPS = [
    "Log in at https://www.mapmyfitness.com and open DevTools (F12).",
    "Application > Cookies > https://www.mapmyfitness.com: copy the value of auth-token.",
    "In C:\\code\\GoneWalking\\.env, paste it after MMF_AUTH_TOKEN=",
    "Copy the date from the auth-token-expiry cookie into .env's \"This token expires YYYY-MM-DD\" line, "
    "so these reports know the new date.",
    "Check it works: python gonewalking.py stats",
]
MAX_PICTURES = 10   # walks beyond this many in one report get no embedded picture


def walk_card(p, w, base_url, site_title, photo_count):
    """What the report shows for one new walk; w is its entry in the site data (None if it has none)."""
    where = " · ".join(x for x in (p.get("place") or p.get("city"), p.get("place_detail")) if x) or "Unknown place"
    card = {"title": f"{where} · {p.get('date')}", "description": "", "tiles": [], "extra": "", "share": None,
            "mapmywalk": None, "image": None}
    if not w:
        return card
    card["title"], card["description"] = previews.share_text(previews.walk_text(w), site_title, photo_count)
    hrs, mins = divmod(round(w["dur"] / 60), 60) if w.get("dur") else (0, 0)
    card["tiles"] = [("Distance", f"{w['mi']:.2f}", "mi"), ("Time", f"{hrs}h {mins:02d}m" if hrs else f"{mins}m", ""),
                     ("Pace", exports.format_pace(w["pace"]).replace(" /mi", ""), "/mi"),
                     ("Climbing", f"{w['ft']:,}" if w.get("ft") is not None else "–", "ft")]
    started = datetime.fromisoformat(w["start"]).strftime("%I:%M %p").lstrip("0") if w.get("start") else ""
    card["extra"] = " · ".join(x for x in (f"Started {started}" if started else "",
                                            f"{w['steps']:,} steps" if w.get("steps") else "") if x)
    card["share"] = f"{base_url}w/{w['id']}.html" if base_url else None
    card["mapmywalk"] = w["link"].replace("http://", "https://") if w.get("link") else None
    image = ROOT / "docs" / "w" / f"{w['id']}.jpg"
    card["image"] = image if image.exists() else None
    return card


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


def send_report(subject, body, dry_run, html=None, images=()):
    """E-mail the report: plain text, plus an HTML version whose pictures (cid, path) are embedded."""
    to = os.environ.get("REPORT_EMAIL", "").strip()
    if not to:
        return None
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    user = os.environ.get("SMTP_USER", "").strip() or to
    msg["From"] = f"GoneWalking <{user}>"
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
        for cid, path in images:
            msg.get_payload()[1].add_related(Path(path).read_bytes(), "image", "jpeg", cid=cid,
                                             filename=Path(path).name)
    if dry_run:
        print("----- e-mail (not sent: --dry-run) -----")
        print(f"To: {to}\nSubject: {subject}\n\n{body}")
        if html:
            # The HTML version, with its pictures pointing at the local files, to open in a browser.
            for cid, path in images:
                html = html.replace(f"cid:{cid[1:-1]}", Path(path).as_uri())
            preview = LOG_DIR / "report-preview.html"
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            preview.write_text(html, encoding="utf-8")
            print(f"----- HTML version: {preview}")
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


def notify(subject, body, failed, log_path, dry_run, html=None, images=()):
    """Send the report every configured way; returns what happened, for the log."""
    results = [r for r in (send_toast(subject, body, failed, log_path, dry_run),
                           send_report(subject, body, dry_run, html, images)) if r]
    return "; ".join(results) or "WINDOWS_NOTIFY=off and no REPORT_EMAIL, so no report sent"


def build_report(new, steps, failures, log_path, photo_walks=(), published=None, sample=False):
    """The report: subject, plain-text body, HTML body, embedded pictures and the days left on the token
    (None if its expiry isn't known). photo_walks are (walk, photos added) for older walks that got
    photos; published is what publish() returned, or None when it didn't run (then the report gives
    the commands to publish by hand). A sample shows the token-renewal steps even when they aren't
    needed yet."""
    expiry = token_expiry()
    days_left = (expiry - date.today()).days if expiry else None
    r = {"failures": [], "cards": [], "photo_cards": [], "notes": [], "publish": None, "log_path": log_path,
         "token_soon": days_left is not None and days_left <= TOKEN_WARN_DAYS, "show_token_steps": sample}
    for name, lines in failures:
        auth = any("auth-token" in l or "expired" in l.lower() or "401" in l for l in lines)
        r["show_token_steps"] |= auth
        reason = ("the MapMyFitness login token has expired or was rejected (renewal steps below)." if auth
                  else "it stopped with an error.")
        r["failures"].append((name, reason, lines[-15:] or ["(no output)"]))
    r["show_token_steps"] |= r["token_soon"]

    base_url, site_title = previews.site_info(INDEX_HTML)
    r["site_title"], r["site_url"] = site_title, base_url
    if new or photo_walks:
        walks, albums = {w["id"]: w for w in site_data("workouts.js", {}).get("workouts", [])}, site_data("photos.js", {})
        card = lambda p: walk_card(p, walks.get(p["id"]), base_url, site_title, len(albums.get(p["id"], [])))
        r["cards"] = [card(p) for p in new]
        r["photo_cards"] = [dict(card(p), badge=f"+{n} new photo{'s' if n != 1 else ''}") for p, n in photo_walks]
    for name, ok, lines in steps:
        summary = next((l for l in reversed(lines) if l.startswith(("Photos:", "Places:", "Site data:"))), None)
        if ok and name == "photos" and summary:
            r["notes"].append(summary.strip())
    if new or photo_walks:
        message = commit_message(new, photo_walks)
        if published is None:
            r["publish"] = ("manual", "Run these in a terminal; GitHub Pages republishes within a minute or two, "
                                      "and the links above work from then on.",
                            [c.format(message=message) for c in MANUAL_PUBLISH])
        elif published[0]:
            r["publish"] = ("done", f'Committed ("{message}") and pushed to GitHub. The site updates within a '
                                    "minute or two; the links above work from then on.", [])
        else:
            r["publish"] = ("failed", f"Not published: {published[1]}. To finish, run these in a terminal "
                                      "(the full log says what git reported):", published[2])

    if expiry is None:
        r["token"] = "Expiry date unknown: add \"This token expires YYYY-MM-DD\" to .env."
    elif days_left < 0:
        r["token"] = f"Expired on {expiry:%B} {expiry.day}, {expiry.year} ({-days_left} days ago)."
    else:
        r["token"] = (f"Expires {expiry:%B} {expiry.day}, {expiry.year}, in {days_left} day{'s' if days_left != 1 else ''}"
                      + (". Renew it soon." if r["token_soon"] else "."))
    r["token_bad"] = expiry is not None and days_left < 0

    if failures:
        subject = f"GoneWalking update FAILED ({', '.join(n for n, _ in failures)})"
    elif len(new) == 1:
        subject = f"GoneWalking: {r['cards'][0]['title']}"
    elif new:
        subject = f"GoneWalking: {len(new)} new walks"
    elif len(photo_walks) == 1:
        subject = f"GoneWalking: new photos · {r['photo_cards'][0]['title']}"
    elif photo_walks:
        subject = f"GoneWalking: new photos on {len(photo_walks)} walks"
    else:
        subject = "GoneWalking: no new walks"
    if r["publish"] and r["publish"][0] == "failed":
        subject += " · not published"
    if r["token_soon"] and not failures:
        subject += " · token expires soon"

    pictured = [c for c in r["cards"] + r["photo_cards"] if c["image"]][:MAX_PICTURES]
    images = [(make_msgid(domain="gonewalking"), c["image"]) for c in pictured]
    for (cid, _), card in zip(images, pictured):
        card["cid"] = cid[1:-1]
    return {"subject": subject, "text": report_text(r), "html": report_html(r), "images": images, "days_left": days_left}


def report_text(r):
    out = []
    if r["failures"]:
        out.append("The update FAILED.")
        for name, reason, lines in r["failures"]:
            out.append(f"\n{name}: {reason}")
            out.append("Last lines:\n  " + "\n  ".join(lines))
    else:
        out.append("The update worked.")
    for heading, cards in ((f"{len(r['cards'])} new {'walk' if len(r['cards']) == 1 else 'walks'}:", r["cards"]),
                           ("New photos:", r["photo_cards"])):
        if not cards:
            continue
        out.append(f"\n{heading}")
        for c in cards:
            out.append(f"  {c['title']}" + (f"  ({c['badge']})" if c.get("badge") else ""))
            if c["tiles"]:
                out.append("    " + " · ".join(f"{v} {u}".strip() for _, v, u in c["tiles"]))
            if c["extra"]:
                out.append(f"    {c['extra']}")
            if c["share"]:
                out.append(f"    Share: {c['share']}")
    if not (r["cards"] or r["photo_cards"] or r["failures"]):
        out.append("\nNo new walks since the last run.")
    out.extend(f"\n{n}" for n in r["notes"])
    if r["publish"]:
        _, text, commands = r["publish"]
        out.append(f"\n{text}")
        out.extend(f"  {c}" for c in commands)
    out.append(f"\nMapMyFitness token: {r['token']}")
    if r["show_token_steps"]:
        out.append("To renew it:")
        out.extend(f"  {i}. {s}" for i, s in enumerate(TOKEN_STEPS, 1))
    if r["log_path"]:
        out.append(f"\nFull log: {r['log_path']}")
    return "\n".join(out)


# Colors match the site (previews.py and the dashboard). Styles are inline and the layout is tables,
# which is what Gmail, Outlook and phone mail apps all render the same way.
INK, MUTED, BLUE, LINE, PAGE = "#1f2328", "#57606a", "#2a78d6", "#d0d7de", "#f3f4f6"
FONT = "font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;"
STATUS = {"ok": ("#1a7f37", "#dafbe1"), "warn": ("#9a6700", "#fff8c5"), "bad": ("#cf222e", "#ffebe9")}


def html_section(title, inner):
    return (f'<tr><td style="padding:24px 28px 0;"><div style="{FONT}font-size:13px;font-weight:600;color:{MUTED};'
            f'text-transform:uppercase;letter-spacing:.06em;margin:0 0 10px;">{h(title)}</div>{inner}</td></tr>')


def html_button(href, label, primary):
    style = (f"background:{BLUE};color:#ffffff;border:1px solid {BLUE};" if primary
             else f"background:#ffffff;color:{BLUE};border:1px solid {LINE};")
    return (f'<a href="{h(href)}" style="{FONT}{style}display:inline-block;padding:9px 16px;border-radius:6px;'
            f'font-size:14px;font-weight:600;text-decoration:none;margin:0 8px 8px 0;">{h(label)}</a>')


def html_card(c, site_url):
    """A walk as a link preview card (picture, site, title, description), then its numbers and links."""
    # The site's address as previews show it, but linked: left as plain text, Gmail links the bare
    # domain, which isn't the site (it's under /gonewalking/).
    site = re.sub(r"^https?://", "", site_url or "").rstrip("/")
    site = (f'<a href="{h(site_url)}" style="color:{MUTED};text-decoration:none;">{h(site)}</a>' if site_url else "")
    picture = ""
    if c.get("cid"):
        img = (f'<img src="cid:{c["cid"]}" width="542" alt="{h("Map of the route: " + c["title"])}" '
               f'style="display:block;width:100%;max-width:542px;height:auto;border:0;">')
        picture = f'<a href="{h(c["share"])}">{img}</a>' if c["share"] else img
    preview = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {LINE};'
               f'border-radius:10px;border-collapse:separate;overflow:hidden;"><tr><td>{picture}</td></tr>'
               f'<tr><td style="background:#f6f8fa;padding:12px 16px;{FONT}">'
               f'<div style="font-size:12px;color:{MUTED};text-transform:uppercase;letter-spacing:.04em;">{site}</div>'
               f'<div style="font-size:17px;font-weight:700;color:{INK};margin-top:3px;line-height:1.3;">{h(c["title"])}</div>'
               + (f'<div style="font-size:14px;color:{MUTED};margin-top:4px;line-height:1.4;">{h(c["description"])}</div>'
                  if c["description"] else "")
               + "</td></tr></table>")
    tiles = ""
    if c["tiles"]:
        cells = "".join(
            f'<td class="tile" width="25%" style="padding:12px 6px;text-align:center;{FONT}'
            f'{"border-left:1px solid " + LINE + ";" if i else ""}">'
            f'<div style="font-size:12px;color:{MUTED};">{h(label)}</div>'
            f'<div class="tile-value" style="font-size:20px;font-weight:700;color:{INK};margin-top:2px;white-space:nowrap;">{h(value)}'
            + (f'<span style="font-size:13px;font-weight:400;color:{MUTED};"> {h(unit)}</span>' if unit else "")
            + "</div></td>" for i, (label, value, unit) in enumerate(c["tiles"]))
        tiles = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px;'
                 f'border:1px solid {LINE};border-radius:10px;border-collapse:separate;"><tr>{cells}</tr></table>')
    extra = " · ".join(x for x in (c.get("badge"), c["extra"]) if x)
    extra = f'<div style="{FONT}font-size:14px;color:{MUTED};margin-top:10px;">{h(extra)}</div>' if extra else ""
    buttons = "".join(b for b in (html_button(c["share"], "Open this walk", True) if c["share"] else "",
                                  html_button(c["mapmywalk"], "View on MapMyWalk", False) if c["mapmywalk"] else "") if b)
    share = (f'<div style="{FONT}font-size:13px;color:{MUTED};margin-top:4px;word-break:break-all;">Share link: '
             f'<a href="{h(c["share"])}" style="color:{BLUE};">{h(c["share"])}</a></div>') if c["share"] else ""
    return f'<div style="margin-bottom:24px;">{preview}{tiles}{extra}<div style="margin-top:14px;">{buttons}</div>{share}</div>'


def report_html(r):
    if r["failures"]:
        status, headline = "bad", "The update failed"
    elif r["token_soon"]:
        status, headline = "warn", "Updated · MapMyFitness token expires soon"
    else:
        status, headline = "ok", "Update complete"
    color, tint = STATUS[status]
    n, m = len(r["cards"]), len(r["photo_cards"])
    if r["failures"]:
        sub = ", ".join(name for name, _, _ in r["failures"]) + " stopped with an error"
    else:
        news = [f"{n} new {'walk' if n == 1 else 'walks'}" if n else "",
                f"new photos on {m} {'walk' if m == 1 else 'walks'}" if m else ""]
        sub = " and ".join(x for x in news if x) or "No new walks since the last run"
        sub = sub[0].upper() + sub[1:]
        if r["publish"]:
            sub += {"done": " · published to the site", "manual": " · ready to publish",
                    "failed": " · not published yet"}[r["publish"][0]]
        if r["publish"] and r["publish"][0] == "failed" and status == "ok":
            status, headline = "warn", "Updated, but not published"
            color, tint = STATUS[status]
    rows = [f'<tr><td style="padding:22px 28px 0;{FONT}"><div style="font-size:20px;font-weight:700;color:{BLUE};">'
            f'{h(r["site_title"])}</div><div style="font-size:13px;color:{MUTED};margin-top:2px;">Scheduled update · '
            f'{datetime.now():%A, %B} {datetime.now().day}, {datetime.now():%Y}</div></td></tr>',
            f'<tr><td style="padding:18px 28px 0;"><div style="{FONT}background:{tint};border-left:4px solid {color};'
            f'border-radius:6px;padding:12px 16px;"><div style="font-size:16px;font-weight:700;color:{color};">'
            f'{h(headline)}</div><div style="font-size:14px;color:{INK};margin-top:2px;">{h(sub)}</div></div></td></tr>']
    for name, reason, lines in r["failures"]:
        rows.append(html_section(f"{name} failed", f'<p style="{FONT}font-size:15px;color:{INK};margin:0 0 10px;">'
                                 f'{h(reason[0].upper() + reason[1:])}</p>' + html_code(lines)))
    if r["cards"]:
        rows.append(html_section("New walk" if n == 1 else f"{n} new walks",
                                 "".join(html_card(c, r["site_url"]) for c in r["cards"])))
    if r["photo_cards"]:
        rows.append(html_section("New photos", "".join(html_card(c, r["site_url"]) for c in r["photo_cards"])))
    for note in r["notes"]:
        rows.append(f'<tr><td style="padding:12px 28px 0;{FONT}font-size:14px;color:{MUTED};">{h(note)}</td></tr>')
    if r["publish"]:
        kind, text, commands = r["publish"]
        mark = {"done": STATUS["ok"][0], "failed": STATUS["bad"][0]}.get(kind)
        mark = f'<span style="color:{mark};">&#9679;</span>&nbsp; ' if mark else ""
        rows.append(html_section("Published" if kind == "done" else "Publish to the site",
                                 f'<p style="{FONT}font-size:15px;color:{INK};margin:0 0 10px;">{mark}{h(text)}</p>'
                                 + (html_code(commands) if commands else "")))
    token_color = STATUS["bad" if r["token_bad"] else "warn" if r["token_soon"] else "ok"][0]
    steps = ""
    if r["show_token_steps"]:
        items = "".join(f'<li style="margin:0 0 6px;">{h(s)}</li>' for s in TOKEN_STEPS)
        steps = (f'<div style="{FONT}font-size:14px;font-weight:600;color:{INK};margin:12px 0 6px;">To renew it</div>'
                 f'<ol style="{FONT}font-size:14px;color:{INK};line-height:1.45;margin:0;padding-left:20px;">{items}</ol>')
    rows.append(html_section("MapMyFitness token", f'<div style="{FONT}font-size:15px;color:{INK};">'
                             f'<span style="color:{token_color};">&#9679;</span>&nbsp; {h(r["token"])}</div>{steps}'))
    footer = f"Full log: {h(str(r['log_path']))}<br>" if r["log_path"] else ""
    rows.append(f'<tr><td style="padding:28px 28px 24px;{FONT}font-size:12px;color:{MUTED};line-height:1.5;'
                f'border-top:1px solid {LINE};">{footer}Sent by scheduled_sync.py · '
                + (f'<a href="{h(r["site_url"])}" style="color:{MUTED};">{h(r["site_url"])}</a>' if r["site_url"] else "")
                + "</td></tr>")
    first = (r["cards"] + r["photo_cards"])[:1]
    preheader = first[0]["description"] if first and first[0]["description"] else sub
    # Every row is a <td> with 28px side padding; phones get less, and smaller stat tiles, so four fit across.
    body = "".join(rows).replace('<tr><td style="padding:', '<tr><td class="px" style="padding:')
    phone = ("@media (max-width:520px){.outer{padding:12px 6px!important}.px{padding-left:16px!important;"
             "padding-right:16px!important}.tile{padding:10px 2px!important}.tile-value{font-size:16px!important}}")
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">'
            f'<style>{phone}</style></head>'
            f'<body style="margin:0;padding:0;background:{PAGE};">'
            f'<div style="display:none;max-height:0;overflow:hidden;">{h(preheader)}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE};">'
            f'<tr><td class="outer" align="center" style="padding:24px 12px;">'
            f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;'
            f'background:#ffffff;border:1px solid {LINE};border-radius:12px;border-collapse:separate;">'
            + body + "</table></td></tr></table></body></html>")


def html_code(lines):
    return (f'<div style="background:#1f2328;border-radius:8px;padding:12px 16px;font-family:Consolas,Menlo,monospace;'
            f'font-size:13px;line-height:1.6;color:#e6edf3;white-space:pre-wrap;word-break:break-word;">'
            + "<br>".join(h(l) for l in lines) + "</div>")


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
        # Shows the publish section as the scheduled run would: done (AUTO_PUBLISH) or the commands to run.
        r = build_report(new, [], [], None, published=(True, "", []) if is_on("AUTO_PUBLISH") else None, sample=True)
        print(notify(f"[Sample] {r['subject']}", r["text"], False, None, dry_run, r["html"], r["images"]))
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now()
    log_path = LOG_DIR / f"sync-{stamp:%Y%m%d-%H%M}.log"
    before, photos_before = known_walks(), photo_counts()

    steps, failures = [], []
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"GoneWalking scheduled update, {stamp:%Y-%m-%d %H:%M}\n")
        ok, lines = run_step(["sync"], log)
        steps.append(("sync", ok, lines))
        if not ok:
            failures.append(("sync", lines))
        elif drop_folders():
            # Photos for new walks, or ones dropped in later for older walks; otherwise there's nothing to match.
            if any(k not in before for k in known_walks()) or photos_waiting():
                ok, lines = run_step(["photos"], log)
                steps.append(("photos", ok, lines))
                if not ok:
                    failures.append(("photos", lines))
            else:
                log.write("\n=== photos skipped: no new walks and no photos waiting\n")

    after, photos_after = known_walks(), photo_counts()
    new = sorted((dict(p, id=k) for k, p in after.items() if k not in before), key=lambda p: p.get("date", ""))
    photo_walks = sorted(((dict(after[k], id=k), n - photos_before.get(k, 0)) for k, n in photos_after.items()
                          if k in after and k in before and n > photos_before.get(k, 0)),
                         key=lambda x: x[0].get("date", ""))
    news = bool(new or photo_walks)

    published = None
    if news and not failures and is_on("AUTO_PUBLISH") and not dry_run:
        with open(log_path, "a", encoding="utf-8") as log:
            published = publish(commit_message(new, photo_walks), log)
    r = build_report(new, steps, failures, log_path, photo_walks, published)
    subject, days_left = r["subject"], r["days_left"]

    # Quiet unless something happened: new walks or photos, a failure, or the token running out.
    want = os.environ.get("REPORT_ON", "always").strip().lower()
    token_soon = days_left is not None and days_left <= TOKEN_WARN_DAYS
    if failures or token_soon or (published and not published[0]) or (news and want != "failure"):
        outcome = notify(subject, r["text"], bool(failures), log_path, dry_run, r["html"], r["images"])
    elif not news:
        outcome = "nothing new, so no report sent"
    else:
        outcome = "worked; REPORT_ON=failure, so no report sent"
    if published:
        outcome = ("published to GitHub; " if published[0] else f"not published ({published[1]}); ") + outcome
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n{subject}\n{outcome}\n")
    print(f"{subject} ({outcome}). Log: {log_path}")

    for old in sorted(LOG_DIR.glob("sync-*.log"))[:-KEEP_LOGS]:
        old.unlink()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
