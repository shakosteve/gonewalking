"""Match your photos to walks, by when and where each was taken, and publish the likely ones.

Each photo taken during a walk (or up to MARGIN before or after) near the walk's GPS track gets a
certainty score, from how close it was to the track at the time, whether it was during the walk,
how its time and location were recorded, and (once checked) what the photo shows:

  PUBLISH_AT and up     published on the walk's album
  REVIEW_AT and up      held for your review, not published
  below                 left off

Time and location come from the photo's EXIF, or from the .json file Google Takeout puts beside
each photo. What a photo shows (outdoors, indoors, a vehicle, a screenshot) comes from
photo_content.csv; `photos --sheets` makes contact sheets of the photos not yet checked.

Your decisions in photo_overrides.csv (file,workout_id,keep) win over everything: a workout_id
publishes the photo on that walk, a blank workout_id keeps it off the site, and keep=yes keeps a
photo that would otherwise be dropped as a near-duplicate. The review page writes this file for you.

Near-duplicates: photos on the same walk taken within DUP_SECONDS of each other, framed the same
way (both portrait or both landscape) and nearly identical (DUP_DISTANCE or less, out of 256, on a
difference hash) are grouped, and only the sharpest of each group is published.

Albums build up over time: photos published by earlier runs stay published (unless you reject
them) even when their folder isn't part of this run, so you can drop new photos in a folder of
their own. data/photo_published.json records what is published.

Outputs:
  docs/photos/{workout_id}/{name}.jpg and {name}_t.jpg   web copy and thumbnail, EXIF removed
  docs/data/photos.js          the published albums (window.GW_PHOTOS)
  data/photo_matches.csv       every photo looked at: score, status and why
  data/photo_review.html       with --review: flagged and published photos, with Approve/Reject (not published)
  data/photo_thumbs/           small copies of candidate photos, for spotting near-duplicates
  data/photo_cache.json        what each photo file says, so later runs don't read it again
"""

import csv
import hashlib
import json
import math
import re
import shutil
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import ExifTags, Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

from exports import LOCAL_TZ, write_js

# Some phones write slightly broken EXIF; Pillow warns, and the Takeout .json fills any gap.
warnings.filterwarnings("ignore", "Corrupt EXIF", UserWarning)

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MARGIN = timedelta(minutes=10)
NEAR_S = 300            # compare the photo with the track within 5 minutes of when it was taken
MAX_DISTANCE_M = 300    # farther than this from the track: not a candidate
PUBLISH_AT = 0.90
REVIEW_AT = 0.50
WEB_PX, THUMB_PX, REVIEW_PX = 1024, 400, 360
DUP_SECONDS = 60
DUP_DISTANCE = 80
SITE_URL = "https://shakosteve.github.io/gonewalking/"
# What the photo shows, from photo_content.csv. Walk photos are outdoors.
CONTENT_FACTOR = {"outdoor": 1.0, "unclear": 0.85, "indoor": 0.3, "vehicle": 0.3, "screen": 0.1}
CONTENT_WORDS = {"outdoor": "outdoors", "unclear": "can't tell what it shows", "indoor": "indoors",
                 "vehicle": "in a vehicle", "screen": "a screenshot or document"}


def km(a, b):
    p = math.radians
    h = math.sin(p(b[0] - a[0]) / 2) ** 2 + math.cos(p(a[0])) * math.cos(p(b[0])) * math.sin(p(b[1] - a[1]) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


# ---------- reading photos ----------

def _dms(values, ref):
    deg = float(values[0]) + float(values[1]) / 60 + float(values[2]) / 3600
    return -deg if ref in ("S", "W") else deg


def read_exif(path):
    """(taken, latlon, zone_known) from the photo's own metadata; taken is an aware datetime or None."""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
    except Exception:
        return None, None, False
    info = exif.get_ifd(ExifTags.IFD.Exif)
    taken, zone_known = None, False
    stamp = info.get(ExifTags.Base.DateTimeOriginal) or exif.get(ExifTags.Base.DateTime)
    if stamp:
        try:
            taken = datetime.strptime(str(stamp).strip()[:19], "%Y:%m:%d %H:%M:%S")
            offset = info.get(ExifTags.Base.OffsetTimeOriginal) or info.get(ExifTags.Base.OffsetTime)
            if offset and re.fullmatch(r"[+-]\d\d:\d\d", str(offset)):
                sign = -1 if str(offset)[0] == "-" else 1
                hh, mm = map(int, str(offset)[1:].split(":"))
                taken = taken.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm)))
                zone_known = True
            else:
                taken = taken.replace(tzinfo=LOCAL_TZ)  # older phones record local time without a zone
        except ValueError:
            taken = None
    latlon = None
    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        if gps.get(2) and gps.get(4):
            latlon = (_dms(gps[2], gps.get(1)), _dms(gps[4], gps.get(3)))
            if latlon == (0.0, 0.0):
                latlon = None
    except (TypeError, ValueError, ZeroDivisionError, IndexError, KeyError):
        latlon = None
    return taken, latlon, zone_known


def takeout_sidecars(folder):
    """Google Takeout's per-photo .json files in a folder, by the photo's original file name."""
    out = {}
    for j in folder.glob("*.json"):
        try:
            meta = json.loads(j.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(meta, dict) and meta.get("title") and "photoTakenTime" in meta:
            out.setdefault(meta["title"], meta)
    return out


def read_sidecar(meta):
    taken, latlon = None, None
    try:
        taken = datetime.fromtimestamp(int(meta["photoTakenTime"]["timestamp"]), timezone.utc)
    except (KeyError, TypeError, ValueError):
        pass
    for key in ("geoDataExif", "geoData"):
        geo = meta.get(key) or {}
        if geo.get("latitude") or geo.get("longitude"):
            latlon = (geo["latitude"], geo["longitude"])
            break
    return taken, latlon


def read_photo(p, sidecars):
    """What one photo file says: when and where it was taken, and how we know."""
    taken, latlon, zone_known = read_exif(p)
    time_src = "exif" if zone_known else ("exif-local" if taken else None)
    loc_src = "exif" if latlon else None
    if taken is None or latlon is None or not zone_known:
        if p.parent not in sidecars:
            sidecars[p.parent] = takeout_sidecars(p.parent)
        meta = sidecars[p.parent].get(p.name.replace("-edited", ""))
        if meta:
            s_taken, s_latlon = read_sidecar(meta)
            if s_taken and not zone_known:  # Takeout's time is exact UTC
                taken, time_src = s_taken, "takeout"
            if latlon is None and s_latlon:
                latlon, loc_src = s_latlon, "takeout"
    with p.open("rb") as f:
        key = hashlib.sha1(f.read(65536)).hexdigest()[:16]
    return {"taken": taken.isoformat() if taken else None, "latlon": list(latlon) if latlon else None,
            "time_src": time_src, "loc_src": loc_src, "key": f"{p.stat().st_size}-{key}"}


def scan(sources, cache, save=None):
    """Every photo under the source folders (or files), once each. Uses and updates the cache;
    save() is called now and then so an interrupted run keeps what it has read."""
    files = []
    for src in map(Path, sources):
        if src.is_file():
            files.append(src)
        elif src.is_dir():
            files += [p for p in src.rglob("*") if p.suffix.lower() in PHOTO_EXTS]
    # Takeout keeps an edited copy beside the original ("X-edited.jpg"): use the edited one.
    edited = {p.with_name(p.name.replace("-edited", "")) for p in files if "-edited" in p.stem}
    files = [p for p in files if p not in edited and p.suffix.lower() in PHOTO_EXTS]

    sidecars, seen, photos, read = {}, set(), [], 0
    started = time.time()
    print(f"Reading {len(files)} photos...", flush=True)
    for n, p in enumerate(sorted(files), 1):
        if n % 500 == 0:
            if save:
                save()
            rate = n / max(time.time() - started, 0.1)
            print(f"  {n} of {len(files)} ({n * 100 // len(files)}%), about {(len(files) - n) / rate / 60:.0f} min to go", flush=True)
        st = p.stat()
        entry = cache.get(str(p))
        if not entry or entry.get("size") != st.st_size or entry.get("mtime") != int(st.st_mtime):
            entry = dict(read_photo(p, sidecars), size=st.st_size, mtime=int(st.st_mtime))
            cache[str(p)] = entry
            read += 1
        if entry["key"] in seen:  # the same photo in a year folder and an album folder
            continue
        seen.add(entry["key"])
        photos.append({"path": p, "name": p.name, "key": entry["key"],
                       "taken": datetime.fromisoformat(entry["taken"]) if entry["taken"] else None,
                       "latlon": tuple(entry["latlon"]) if entry["latlon"] else None,
                       "time_src": entry["time_src"], "loc_src": entry["loc_src"]})
    return photos, read


# ---------- matching ----------

def load_walks(tracks_dir):
    walks = []
    for f in Path(tracks_dir).glob("*.geojson"):
        feature = json.loads(f.read_text(encoding="utf-8"))
        props = feature["properties"]
        if not props.get("start_time_utc"):
            continue
        start = datetime.fromisoformat(props["start_time_utc"].replace("Z", "+00:00"))
        points = [((lat, lon), t) for line, times in zip(feature["geometry"]["coordinates"], props["times"])
                  for (lon, lat, *_), t in zip(line, times)]
        if not points:
            continue
        pad = MAX_DISTANCE_M / 111000  # degrees, roughly; a quick box around the track
        lats, lons = [ll[0] for ll, _ in points], [ll[1] for ll, _ in points]
        walks.append({"id": f.stem, "start": start, "end": start + timedelta(seconds=props["duration_s"]),
                      "points": points, "date": props.get("date"),
                      "box": (min(lats) - pad, max(lats) + pad, min(lons) - pad * 1.5, max(lons) + pad * 1.5)})
    walks.sort(key=lambda w: w["start"])
    return walks


def match(photo, walks):
    """(walk, seconds into the walk, metres from the track or None, reason)."""
    if photo["taken"] is None:
        return None, None, None, "no date"
    during = [w for w in walks if w["start"] - MARGIN <= photo["taken"] <= w["end"] + MARGIN]
    if not during:
        return None, None, None, "not during a walk"
    if photo["latlon"] is None:
        # Nothing ties it to the walk: often images saved into the library, dated when saved.
        return None, None, None, "no location"
    lat, lon = photo["latlon"]
    best = None
    for w in during:
        at = (photo["taken"] - w["start"]).total_seconds()
        b = w["box"]
        if not (b[0] <= lat <= b[1] and b[2] <= lon <= b[3]):
            continue  # nowhere near this walk's track
        near = [(km(photo["latlon"], ll) * 1000, t) for ll, t in w["points"] if abs(t - at) <= NEAR_S]
        if not near:  # just before the start or after the end: compare with the nearest end of the track
            near = [(km(photo["latlon"], ll) * 1000, t) for ll, t in (w["points"][0], w["points"][-1])]
        dist = min(near)[0]
        if best is None or dist < best[2]:
            best = (w, round(at), dist)
    if best is None or best[2] > MAX_DISTANCE_M:
        return None, None, best[2] if best else None, "far from the track"
    return best[0], best[1], best[2], "near the track"


def certainty(photo, walk, at, dist, content):
    """A 0-1 score that the photo was taken on this walk, and the reasons behind it."""
    score = 1.0 if dist <= 30 else 0.95 if dist <= 75 else 0.8 if dist <= 150 else 0.5
    reasons = [f"{dist:.0f} m from the track"]
    duration = (walk["end"] - walk["start"]).total_seconds()
    if not 0 <= at <= duration:
        score *= 0.85
        reasons.append(f"{abs(at if at < 0 else at - duration) / 60:.0f} min {'before' if at < 0 else 'after'} the walk")
    if photo["time_src"] == "exif-local":
        score *= 0.95
        reasons.append("time zone assumed")
    if photo["loc_src"] == "takeout":
        score *= 0.9
        reasons.append("location from Google, not the camera")
    if content:
        score *= CONTENT_FACTOR.get(content, 1.0)
        reasons.append(CONTENT_WORDS.get(content, content))
    return round(score, 2), reasons


def image_print(path):
    """(difference hash, sharpness, is_portrait) of a small copy, for spotting near-duplicates."""
    with Image.open(path) as im:
        portrait = im.height > im.width
        gray = im.convert("L")
        small = gray.resize((17, 16), Image.LANCZOS)
        px = small.load()
        bits = 0
        for y in range(16):
            for x in range(16):
                bits = bits << 1 | (px[x, y] > px[x + 1, y])
        edges = gray.filter(ImageFilter.FIND_EDGES)
        sharp = ImageStat.Stat(edges).var[0]
    return bits, sharp, portrait


def near_duplicates(entries, keeps):
    """Group a walk's published photos into near-duplicates; returns {file: file it duplicates}.
    Photos in keeps (kept by you, or published by an earlier run) are never set aside, and win
    over new photos."""
    entries = sorted(entries, key=lambda e: e["taken"])
    groups = []
    for e in entries:
        for g in groups:
            if any(abs((e["taken"] - o["taken"]).total_seconds()) <= DUP_SECONDS and e["portrait"] == o["portrait"]
                   and bin(e["hash"] ^ o["hash"]).count("1") <= DUP_DISTANCE for o in g):
                g.append(e)
                break
        else:
            groups.append([e])
    dropped = {}
    for g in groups:
        if len(g) < 2:
            continue
        protected = [e for e in g if e["file"] in keeps]
        best = max(protected or g, key=lambda e: e["sharp"])
        for e in g:
            if e is not best and e["file"] not in keeps:
                dropped[e["file"]] = best["file"]
    return dropped


def load_overrides(path):
    """{file: workout_id} and the set of files marked keep=yes, from photo_overrides.csv."""
    if not Path(path).exists():
        return {}, set()
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("file")]
    return ({r["file"].strip(): (r.get("workout_id") or "").strip() for r in rows},
            {r["file"].strip() for r in rows if (r.get("keep") or "").strip().lower() in ("yes", "y", "1", "true")})


def load_csv_map(path, key, value):
    if not Path(path).exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r[key].strip(): (r.get(value) or "").strip() for r in csv.DictReader(f) if r.get(key)}


def auto_content(photo):
    """What we can tell without looking: screenshots and other non-camera images."""
    name = photo["name"].lower()
    if "screenshot" in name or name.startswith("screen_") or photo["path"].suffix.lower() == ".png":
        return "screen"
    return None


# ---------- output ----------

def web_copy(src, dest, size):
    """A resized JPEG with no EXIF (so no location or camera details), turned the right way up."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((size, size), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, "JPEG", quality=80, optimize=True, progressive=True)
        return im.size


def fresh(dest, src):
    return dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime


def make_sheets(rows, data_dir):
    """Contact sheets of candidate photos whose content hasn't been checked, 20 to a sheet."""
    sheet_dir = data_dir / "photo_sheets"
    if sheet_dir.exists():
        shutil.rmtree(sheet_dir)
    todo = [r for r in rows if r["thumb"] and not r["content"] and r["status"] in ("review", "published")]
    if not todo:
        return 0
    sheet_dir.mkdir(parents=True)
    tile, cols, per = 300, 5, 20
    try:
        font = ImageFont.truetype("arialbd.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    index = []
    for s in range(0, len(todo), per):
        batch = todo[s:s + per]
        rows_n = math.ceil(len(batch) / cols)
        sheet = Image.new("RGB", (cols * tile, rows_n * tile), "white")
        draw = ImageDraw.Draw(sheet)
        for i, r in enumerate(batch):
            with Image.open(r["thumb"]) as im:
                im = im.copy()
            im.thumbnail((tile - 8, tile - 8))
            x, y = (i % cols) * tile, (i // cols) * tile
            sheet.paste(im, (x + (tile - im.width) // 2, y + (tile - im.height) // 2))
            draw.rectangle((x + 4, y + 4, x + 52, y + 40), fill="black")
            draw.text((x + 10, y + 6), str(i + 1), fill="yellow", font=font)
            index.append({"sheet": f"sheet_{s // per + 1:02}.jpg", "n": i + 1, "file": r["file"]})
        sheet.save(sheet_dir / f"sheet_{s // per + 1:02}.jpg", quality=85)
    (sheet_dir / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return len(todo)


def write_review_page(rows, overrides, keeps, out_path):
    """A local page (not published) to approve or reject photos; it writes photo_overrides.csv."""
    items = [{k: r[k] for k in ("file", "workout_id", "walk_date", "taken", "distance_m", "score", "status", "why",
                                "lat", "lon", "thumb_rel", "original", "decided")}
             for r in rows if r["status"] in ("review", "published", "duplicate")]
    data = json.dumps({"items": items, "overrides": overrides, "keeps": sorted(keeps), "site": SITE_URL}).replace("</", "<\\/")
    page = REVIEW_PAGE.replace("/*DATA*/", data)
    out_path.write_text(page, encoding="utf-8")


def update_photos(sources, tracks_dir, data_dir, site_dir, overrides_path, content_path, sheets=False, review=False):
    data_dir, site_dir = Path(data_dir), Path(site_dir)
    walks = load_walks(tracks_dir)
    by_id = {w["id"]: w for w in walks}
    overrides, keeps = load_overrides(overrides_path)
    contents = load_csv_map(content_path, "file", "content")
    cache_path = data_dir / "photo_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    def save_cache():
        tmp = cache_path.with_suffix(".part")
        tmp.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
        tmp.replace(cache_path)
    photos, read = scan(sources, cache, save_cache)
    save_cache()
    print("Matching to walks and making copies...", flush=True)

    photo_dir, thumb_dir = site_dir / "photos", data_dir / "photo_thumbs"
    published_path = data_dir / "photo_published.json"
    earlier = json.loads(published_path.read_text(encoding="utf-8")) if published_path.exists() else {}
    albums, rows, keep, keep_review, candidates, chosen = {}, [], set(), set(), 0, []
    for photo in photos:
        name = photo["name"]
        walk, at, dist, reason = match(photo, walks)
        content = contents.get(name) or auto_content(photo)
        score, why, status = None, [reason], "skipped"
        if walk:
            score, why = certainty(photo, walk, at, dist, content)
            status = "published" if score >= PUBLISH_AT else "review" if score >= REVIEW_AT else "left off"
        decided = ""
        if name in overrides:
            wid = overrides[name]
            if not wid:
                status, decided = "left off", "rejected"
                why = why + ["you rejected it"]
            elif wid in by_id:
                walk, status, decided = by_id[wid], "published", "approved"
                at = max(0, round((photo["taken"] - walk["start"]).total_seconds())) if photo["taken"] else None
                why = why + ["you approved it"]
            else:
                why = why + [f"unknown workout_id {wid} in photo_overrides.csv"]
        taken_local = photo["taken"].astimezone(LOCAL_TZ) if photo["taken"] else None
        row = {"file": name, "status": status, "score": f"{score:.2f}" if score is not None else "",
               "why": "; ".join(why), "workout_id": walk["id"] if walk and status != "skipped" else "",
               "walk_date": walk["date"] if walk and status != "skipped" else "",
               "taken": taken_local.isoformat(timespec="seconds") if taken_local else "",
               "lat": f"{photo['latlon'][0]:.5f}" if photo["latlon"] else "",
               "lon": f"{photo['latlon'][1]:.5f}" if photo["latlon"] else "",
               "distance_m": f"{dist:.0f}" if dist is not None else "", "content": content or "",
               "decided": decided, "original": photo["path"].resolve().as_uri(), "source": str(photo["path"]),
               "thumb": None, "thumb_rel": ""}
        rows.append(row)
        if status != "skipped":
            candidates += 1
            if candidates % 50 == 0:
                print(f"  {candidates} candidates so far", flush=True)

        # A small copy for the review page and contact sheets, for every candidate.
        if walk and status != "skipped":
            thumb = thumb_dir / f"{photo['key']}.jpg"
            if not fresh(thumb, photo["path"]):
                web_copy(photo["path"], thumb, REVIEW_PX)
            keep_review.add(thumb)
            row["thumb"], row["thumb_rel"] = thumb, f"photo_thumbs/{thumb.name}"

        if status == "published":
            chosen.append({"row": row, "photo": photo, "walk": walk, "at": at, "taken_local": taken_local})

    # Photos published by earlier runs whose folders aren't part of this run stay published,
    # unless you've since rejected them.
    seen = {r["file"] for r in rows}
    carried = 0
    for name, e in earlier.items():
        if name in seen or e["walk"] not in by_id or (name in overrides and not overrides[name]):
            continue
        big, small = photo_dir / e["walk"] / f"{e['f']}.jpg", photo_dir / e["walk"] / f"{e['f']}_t.jpg"
        if not (big.exists() and small.exists()):
            continue
        walk = by_id[e["walk"]]
        taken = datetime.fromisoformat(e["time"]) if e.get("time") else None
        row = {"file": name, "status": "published", "score": "", "why": "published by an earlier run",
               "workout_id": walk["id"], "walk_date": walk["date"], "taken": e.get("time") or "",
               "lat": f"{e['ll'][0]:.5f}" if e.get("ll") else "", "lon": f"{e['ll'][1]:.5f}" if e.get("ll") else "",
               "distance_m": "", "content": contents.get(name, ""), "decided": "", "original": "", "source": "",
               "thumb": small, "thumb_rel": "../docs/photos/" + walk["id"] + "/" + small.name}
        rows.append(row)
        chosen.append({"row": row, "photo": {"taken": taken}, "walk": walk, "at": e.get("t"),
                       "taken_local": taken, "earlier": e})
        carried += 1

    # Near-duplicates: keep the sharpest photo of each burst.
    dupes = 0
    by_walk = {}
    for c in chosen:
        if c["photo"]["taken"]:
            h, sharp, portrait = image_print(c["row"]["thumb"])
            by_walk.setdefault(c["walk"]["id"], []).append(
                {"file": c["row"]["file"], "taken": c["photo"]["taken"], "hash": h, "sharp": sharp, "portrait": portrait})
    dropped = {}
    for entries in by_walk.values():
        dropped.update(near_duplicates(entries, keeps | {c["row"]["file"] for c in chosen if "earlier" in c}))
    for c in chosen:
        name = c["row"]["file"]
        if name in dropped:
            c["row"]["status"] = "duplicate"
            c["row"]["why"] += f"; nearly identical to {dropped[name]}, which is sharper"
            dupes += 1
        elif name in keeps:
            c["row"]["why"] += "; you kept it although it has a near-duplicate"

    published_now = {}
    for c in sorted(chosen, key=lambda c: "earlier" not in c):  # earlier photos keep their file names
        row, photo, walk, at, taken_local = c["row"], c["photo"], c["walk"], c["at"], c["taken_local"]
        if row["status"] != "published":
            continue
        name = row["file"]
        if "earlier" in c:
            e = c["earlier"]
            album = albums.setdefault(walk["id"], [])
            album.append({k: e.get(k) for k in ("f", "w", "h", "t", "time", "ll")})
            keep |= {photo_dir / walk["id"] / f"{e['f']}.jpg", photo_dir / walk["id"] / f"{e['f']}_t.jpg"}
            published_now[name] = e
            continue
        stem = taken_local.strftime("%Y%m%d-%H%M%S") if taken_local else Path(name).stem
        album = albums.setdefault(walk["id"], [])
        while any(p["f"] == stem for p in album):
            stem += "b"
        big, small = photo_dir / walk["id"] / f"{stem}.jpg", photo_dir / walk["id"] / f"{stem}_t.jpg"
        if fresh(big, photo["path"]) and small.exists():
            with Image.open(big) as im:
                w, h = im.size
        else:
            w, h = web_copy(photo["path"], big, WEB_PX)
            web_copy(photo["path"], small, THUMB_PX)
        keep |= {big, small}
        album.append({"f": stem, "w": w, "h": h, "t": at,
                      "time": taken_local.isoformat(timespec="seconds") if taken_local else None,
                      "ll": [round(photo["latlon"][0], 5), round(photo["latlon"][1], 5)] if photo["latlon"] else None})
        published_now[name] = dict(album[-1], walk=walk["id"])

    for album in albums.values():
        album.sort(key=lambda p: (p["t"] is None, p["t"] or 0, p["f"]))
    # Remove published copies (and review thumbnails) of photos that no longer qualify.
    for folder, wanted in ((photo_dir, keep), (thumb_dir, keep_review)):
        if folder.exists():
            for f in folder.rglob("*.jpg"):
                if f not in wanted:
                    f.unlink()
            for d in [d for d in folder.rglob("*") if d.is_dir()]:
                if not any(d.iterdir()):
                    d.rmdir()

    write_js(site_dir / "data" / "photos.js", "GW_PHOTOS", albums)
    tmp = published_path.with_suffix(".part")
    tmp.write_text(json.dumps(published_now, indent=0), encoding="utf-8")
    tmp.replace(published_path)
    rows.sort(key=lambda r: ({"review": 0, "duplicate": 1, "published": 2, "left off": 3}.get(r["status"], 4), r["taken"]))
    fields = ["file", "status", "score", "why", "workout_id", "walk_date", "taken", "lat", "lon", "distance_m",
              "content", "decided", "source"]
    with open(data_dir / "photo_matches.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    review_page = data_dir / "photo_review.html"
    if review:
        write_review_page(rows, overrides, keeps, review_page)
    elif review_page.exists():
        review_page.unlink()
    unchecked = make_sheets(rows, data_dir) if sheets else 0

    count = lambda s: sum(r["status"] == s for r in rows)
    published = sum(len(a) for a in albums.values())
    print(f"Photos: {len(photos)} looked at ({read} read for the first time), {carried} kept from earlier runs, "
          f"{published} published on {len(albums)} walks, {count('review')} to review, {dupes} near-duplicates set aside, "
          f"{count('left off')} left off")
    skipped = {}
    for r in rows:
        if r["status"] == "skipped":
            skipped[r["why"]] = skipped.get(r["why"], 0) + 1
    for why, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>6}  skipped: {why}")
    if count("review") or dupes:
        print(f"Review them: {review_page}" if review else "Review them: python gonewalking.py photos --review")
    if sheets:
        print(f"Contact sheets: {unchecked} photos not yet checked -> {data_dir / 'photo_sheets'}")
    return albums


REVIEW_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo review</title>
<style>
  :root { --page: #f9f9f7; --surface: #fff; --text: #0b0b0b; --muted: #5f5e5a; --border: rgba(0,0,0,.12);
          --ok: #1a7f4b; --no: #b3261e; --accent: #2a78d6; }
  @media (prefers-color-scheme: dark) {
    :root { --page: #0d0d0d; --surface: #1a1a19; --text: #f0efec; --muted: #b5b4ad; --border: rgba(255,255,255,.12);
            --ok: #3fbf7f; --no: #f2766b; --accent: #5aa0f0; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--page); color: var(--text); font: 14px/1.45 system-ui, "Segoe UI", sans-serif; }
  header { position: sticky; top: 0; z-index: 1; background: var(--page); border-bottom: 1px solid var(--border);
           padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; }
  h1 { font-size: 18px; margin: 0 auto 0 0; }
  .tabs button, .bar button { font: inherit; border: 1px solid var(--border); background: var(--surface); color: var(--text);
           border-radius: 8px; padding: 6px 12px; cursor: pointer; }
  .tabs button[aria-pressed="true"] { font-weight: 600; box-shadow: inset 0 -2px 0 var(--accent); }
  .bar .primary { background: var(--accent); color: #fff; border-color: transparent; }
  main { padding: 16px; display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; display: flex; flex-direction: column; }
  .card.approved { outline: 3px solid var(--ok); } .card.rejected { outline: 3px solid var(--no); opacity: .6; }
  .card img { width: 100%; aspect-ratio: 1; object-fit: cover; display: block; background: #888; }
  .info { padding: 8px 10px; flex: 1; display: flex; flex-direction: column; gap: 3px; }
  .score { font-weight: 600; } .why { color: var(--muted); font-size: 12px; }
  .links { display: flex; flex-wrap: wrap; gap: 4px 10px; font-size: 12px; } .links a { color: var(--accent); }
  .acts { display: grid; grid-template-columns: 1fr 1fr; border-top: 1px solid var(--border); }
  .acts button { font: inherit; padding: 9px; border: 0; background: none; color: var(--text); cursor: pointer; }
  .acts button + button { border-left: 1px solid var(--border); }
  .acts .yes:hover, .card.approved .yes { background: var(--ok); color: #fff; }
  .acts .no:hover, .card.rejected .no { background: var(--no); color: #fff; }
  .empty { grid-column: 1 / -1; color: var(--muted); }
  .help { padding: 0 16px; color: var(--muted); max-width: 80ch; }
</style>
</head>
<body>
<header>
  <h1>Photo review</h1>
  <div class="tabs"><button id="tab-review" aria-pressed="true"></button> <button id="tab-duplicate" aria-pressed="false"></button> <button id="tab-published" aria-pressed="false"></button></div>
  <div class="bar"><span id="count"></span> <button class="primary" id="download">Download photo_overrides.csv</button></div>
</header>
<p class="help">Approve the photos that were taken on the walk shown, reject the rest. Your choices are kept in this
browser until you download <b>photo_overrides.csv</b>: save it in the GoneWalking folder (replacing the old one) and run
<code>python gonewalking.py photos</code> again. Approved photos are published; rejected ones never come back.</p>
<main id="grid"></main>
<script>
const DATA = /*DATA*/;
const KEY = "gw-photo-review";
let decisions = {};
try { decisions = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(decisions)); } catch (e) {} };
let tab = "review";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
function state(it) { return decisions[it.file] || it.decided || ""; }
function render() {
  const list = DATA.items.filter((it) => it.status === tab);
  const n = (s) => DATA.items.filter((i) => i.status === s).length;
  $("tab-review").textContent = `To review (${n("review")})`;
  $("tab-duplicate").textContent = `Near-duplicates (${n("duplicate")})`;
  $("tab-published").textContent = `Published (${n("published")})`;
  for (const t of ["review", "duplicate", "published"]) $("tab-" + t).setAttribute("aria-pressed", tab === t);
  const nd = Object.keys(decisions).length;
  $("count").textContent = nd ? `${nd} new decision${nd === 1 ? "" : "s"}` : "";
  $("grid").innerHTML = list.length ? list.map((it, i) => {
    const t = it.taken ? new Date(it.taken) : null;
    const when = t ? t.toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) : "No date";
    const map = it.lat ? `<a href="https://www.google.com/maps/search/?api=1&query=${it.lat},${it.lon}" target="_blank" rel="noopener">Map</a>` : "";
    return `<div class="card ${state(it)}" data-i="${i}">
      <a href="${esc(it.original)}" target="_blank" title="Open the original"><img src="${esc(it.thumb_rel)}" alt="" loading="lazy"></a>
      <div class="info"><div class="score">${Math.round(it.score * 100)}% sure · ${esc(when)}</div>
        <div>Walk of ${esc(it.walk_date)}</div><div class="why">${esc(it.why)}</div>
        <div class="links">${map}<a href="${DATA.site}#walk=${it.workout_id}" target="_blank" rel="noopener">The walk</a><a href="${esc(it.original)}" target="_blank">Original</a><span>${esc(it.file)}</span></div></div>
      <div class="acts"><button class="yes">${it.status === "duplicate" ? "Keep it too" : "Approve"}</button><button class="no">Reject</button></div></div>`;
  }).join("") : `<p class="empty">Nothing here.</p>`;
  $("grid").querySelectorAll(".card").forEach((card) => {
    const it = list[card.dataset.i];
    card.querySelector(".yes").onclick = () => { decisions[it.file] = state(it) === "approved" ? undefined : "approved"; tidy(); };
    card.querySelector(".no").onclick = () => { decisions[it.file] = state(it) === "rejected" ? undefined : "rejected"; tidy(); };
  });
}
function tidy() { for (const k in decisions) if (!decisions[k]) delete decisions[k]; save(); render(); }
$("tab-review").onclick = () => { tab = "review"; render(); };
$("tab-published").onclick = () => { tab = "published"; render(); };
$("tab-duplicate").onclick = () => { tab = "duplicate"; render(); };
$("download").onclick = () => {
  const out = Object.assign({}, DATA.overrides);
  const keeps = new Set(DATA.keeps);
  for (const it of DATA.items) {
    const d = decisions[it.file];
    if (d === "approved") { out[it.file] = it.workout_id; if (it.status === "duplicate") keeps.add(it.file); }
    else if (d === "rejected") { out[it.file] = ""; keeps.delete(it.file); }
  }
  const q = (s) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  const csv = "file,workout_id,keep\n" + Object.keys(out).sort().map((f) => `${q(f)},${out[f]},${keeps.has(f) ? "yes" : ""}`).join("\n") + "\n";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = "photo_overrides.csv";
  a.click();
};
render();
</script>
</body>
</html>
"""
