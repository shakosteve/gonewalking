"""Download workout history and GPS tracks from MapMyFitness / MapMyWalk.

Usage:
    python gonewalking.py sync              # refresh stats CSV, download any missing TCX files
    python gonewalking.py sync --limit 5    # only download up to 5 new TCX files
    python gonewalking.py stats             # refresh the stats CSV only
    python gonewalking.py process           # rebuild tracks, clean stats CSV and KML from downloads
    python gonewalking.py places            # look up park/city for new walks, list unnamed spots

Authentication comes from MMF_AUTH_TOKEN in .env (the "auth-token" cookie from
a logged-in browser session on mapmyfitness.com).
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import exports
import places
import tracks

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TCX_DIR = DATA_DIR / "tcx"
TRACKS_DIR = DATA_DIR / "tracks"
SITE_DATA_DIR = ROOT / "docs" / "data"
PLACE_OVERRIDES = ROOT / "place_overrides.csv"
PLACE_LINKS = ROOT / "place_links.csv"
STATS_CSV = DATA_DIR / "workouts.csv"
MANIFEST = DATA_DIR / "manifest.json"

BASE_URL = "https://www.mapmyfitness.com"
CSV_URL = f"{BASE_URL}/workout/export/csv"
TCX_URL = BASE_URL + "/workout/export/{workout_id}/tcx"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
WORKOUT_ID_RE = re.compile(r"/workout/(\d+)")


class AuthError(Exception):
    pass


def load_env(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def make_session():
    token = os.environ.get("MMF_AUTH_TOKEN")
    if not token:
        sys.exit("MMF_AUTH_TOKEN is not set. Add it to .env (see README).")
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.cookies.set("auth-token", token, domain=".mapmyfitness.com")
    return session


def fetch(session, url, retries=4):
    """GET a URL, retrying transient failures. Raises AuthError if bounced to login."""
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=120)
        except requests.RequestException as exc:
            error = exc
        else:
            if "/auth/login" in resp.url:
                raise AuthError(
                    "MapMyFitness redirected to the login page: the auth token in .env "
                    "has expired or is wrong. Copy a fresh auth-token cookie from your browser."
                )
            if resp.status_code == 200:
                return resp
            if resp.status_code not in (429, 500, 502, 503, 504):
                resp.raise_for_status()
            error = requests.HTTPError(f"HTTP {resp.status_code} for {url}")
        wait = 5 * 2**attempt
        print(f"  {error}; retrying in {wait}s", file=sys.stderr)
        time.sleep(wait)
    raise error


def write_atomic(path, content):
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(content)
    tmp.replace(path)


def load_manifest():
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {"workouts": {}}


def save_manifest(manifest):
    manifest["updated_at"] = now_iso()
    write_atomic(MANIFEST, json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8"))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def refresh_stats(session):
    print(f"Downloading workout history CSV -> {STATS_CSV.relative_to(ROOT)}")
    resp = fetch(session, CSV_URL)
    if not resp.content.startswith(b"Date Submitted"):
        raise RuntimeError("Unexpected response for workout CSV (not a MapMyFitness export).")
    write_atomic(STATS_CSV, resp.content)
    workouts = read_workouts()
    print(f"  {len(workouts)} workouts in history")
    return workouts


def read_workouts():
    with STATS_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    workouts = []
    for row in rows:
        match = WORKOUT_ID_RE.search(row.get("Link", ""))
        if match:
            workouts.append({
                "id": match.group(1),
                "date": row["Workout Date"],
                "activity": row["Activity Type"],
                "distance_mi": row["Distance (mi)"],
            })
    return workouts


def download_tracks(session, workouts, manifest, limit=None, delay=2.0):
    pending = [
        w for w in workouts
        if w["id"] not in manifest["workouts"] or not (TCX_DIR / f"{w['id']}.tcx").exists()
    ]
    # Skip workouts we already know have no GPS track, unless retried explicitly.
    pending = [w for w in pending if manifest["workouts"].get(w["id"], {}).get("status") != "no_gps"]
    if limit is not None:
        pending = pending[:limit]
    print(f"{len(pending)} track(s) to download")

    for i, w in enumerate(pending, 1):
        path = TCX_DIR / f"{w['id']}.tcx"
        print(f"[{i}/{len(pending)}] {w['date']:>15}  {w['activity']:<10} {w['id']}", end="", flush=True)
        entry = {**w, "downloaded_at": now_iso()}
        try:
            content = fetch(session, TCX_URL.format(workout_id=w["id"])).content
            if b"<TrainingCenterDatabase" not in content[:1000]:
                raise RuntimeError("response is not a TCX file")
        except (requests.RequestException, RuntimeError) as exc:
            entry.update(status="error", error=str(exc))
            print(f"  FAILED: {exc}")
            manifest["workouts"][w["id"]] = entry
            save_manifest(manifest)
            continue
        points = content.count(b"<Position>")
        entry["gps_points"] = points
        if points:
            write_atomic(path, content)
            entry.update(status="ok", file=f"tcx/{path.name}")
            print(f"  {points} GPS points, {len(content) / 1e6:.1f} MB")
        else:
            entry["status"] = "no_gps"
            print("  no GPS points (skipped)")
        manifest["workouts"][w["id"]] = entry
        save_manifest(manifest)
        if i < len(pending):
            time.sleep(delay)


def process(force=False, fetch_places=False):
    tracks.process_all(STATS_CSV, TCX_DIR, TRACKS_DIR, force=force)
    found = places.update_places(TRACKS_DIR, DATA_DIR, PLACE_OVERRIDES, fetch_missing=fetch_places, links_path=PLACE_LINKS)
    exports.export_all(STATS_CSV, TRACKS_DIR, DATA_DIR, SITE_DATA_DIR)
    return found


def print_unknown_places(found):
    clusters = places.unknown_clusters(found)
    if not clusters:
        print("Every workout has a place name.")
        return
    print(f"\n{sum(len(c['walks']) for c in clusters)} workouts in {len(clusters)} spots have no park name.")
    print(f"Add names to {PLACE_OVERRIDES.name} (name,lat,lon,radius_m) and run process again.\n")
    for c in clusters:
        lat, lon = c["center"]
        p = c["walks"][0][1]
        where = ", ".join(x for x in (p["city"], p["county"], p["state"] or p["country"]) if x)
        dates = ", ".join(w[1]["date"] for w in c["walks"][:4]) + (" ..." if len(c["walks"]) > 4 else "")
        print(f"{len(c['walks']):>3}  {lat:.5f},{lon:.5f}  {where}")
        print(f"     https://www.google.com/maps/search/?api=1&query={lat:.5f},{lon:.5f}   ({dates})")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="refresh stats and download missing GPS tracks")
    sync.add_argument("--limit", type=int, help="download at most N new tracks")
    sync.add_argument("--delay", type=float, default=2.0, help="seconds between downloads (default 2)")
    sync.add_argument("--retry-no-gps", action="store_true", help="re-check workouts previously found to have no GPS")
    sub.add_parser("stats", help="refresh the workout stats CSV only")
    process_cmd = sub.add_parser("process", help="rebuild tracks, clean stats CSV and KML from downloads")
    process_cmd.add_argument("--force", action="store_true", help="reprocess every track, even if up to date")
    sub.add_parser("places", help="look up park/city names for new walks and list walks without one")
    args = parser.parse_args()

    if args.command == "process":
        process(force=args.force)
        return
    if args.command == "places":
        print_unknown_places(process(fetch_places=True))
        return

    load_env()
    session = make_session()
    TCX_DIR.mkdir(parents=True, exist_ok=True)

    try:
        workouts = refresh_stats(session)
        if args.command == "sync":
            manifest = load_manifest()
            if args.retry_no_gps:
                for entry in manifest["workouts"].values():
                    if entry.get("status") == "no_gps":
                        entry["status"] = "retry"
            download_tracks(session, workouts, manifest, args.limit, args.delay)
            process(fetch_places=True)
    except AuthError as exc:
        sys.exit(str(exc))
    print("Done.")


if __name__ == "__main__":
    main()
