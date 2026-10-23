"""Build dashboard and map outputs from the stats CSV and the compact tracks.

- data/workouts_clean.csv: one row per workout, MapMyWalk's stats corrected with GPS-derived values
- data/kml/all_routes.kml and data/kml/{year}.kml: simplified routes for Google My Maps
- docs/data/: the dashboard's copy of the stats, routes and KML (published with the site)
"""

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import places
import tracks

LOCAL_TZ = ZoneInfo("America/Chicago")
FEET_PER_METER = 3.28084
KML_TOLERANCE_M = 5.0          # Douglas-Peucker tolerance for map lines
MY_MAPS_MAX_BYTES = 5_000_000  # Google My Maps import limit per file

CLEAN_COLUMNS = [
    "workout_id", "date", "year", "month", "weekday", "start_time_local", "start_hour",
    "activity", "distance_mi", "distance_source", "mmf_distance_mi", "duration_s",
    "moving_time_s", "pace_min_per_mi", "moving_pace_min_per_mi", "avg_speed_mph",
    "max_5min_speed_mph", "elevation_gain_ft", "calories", "steps", "notes",
    "place", "place_detail", "place_source", "place_url", "city", "county", "state", "country", "region",
    "has_track", "gps_segments", "gps_glitches_removed", "link",
]

# KML colors are aabbggrr. Ten distinct hues, cycled by year.
YEAR_COLORS = [
    "ffd77800", "ff2fb5e8", "ff3c9f2c", "ff2827d6", "ffbd6794",
    "ff4b568c", "ffc277e3", "ff7f7f7f", "ff22bdbc", "ffcfbe17",
]


def num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def rnd(value, digits):
    return round(value, digits) if value is not None else None


def load_tracks(tracks_dir):
    out = {}
    for path in Path(tracks_dir).glob("*.geojson"):
        with path.open(encoding="utf-8") as f:
            feature = json.load(f)
        out[feature["properties"]["id"]] = feature
    return out


def load_places(data_dir):
    path = Path(data_dir) / "places.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def clean_rows(stats_csv, track_features, places=None):
    places = places or {}
    rows = []
    with open(stats_csv, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            m = tracks.WORKOUT_ID_RE.search(raw.get("Link", ""))
            if not m:
                continue
            workout_id = m.group(1)
            feature = track_features.get(workout_id)
            props = feature["properties"] if feature else {}

            mmf_distance = num(raw["Distance (mi)"])
            duration_s = num(raw["Workout Time (seconds)"])
            if feature:
                start = datetime.fromisoformat(props["start_time_utc"]).astimezone(LOCAL_TZ)
                day = start.date()
                distance, distance_source = props["distance_mi"], "gps"
                moving_s = props["moving_time_s"]
            else:
                start = None
                day = tracks.parse_csv_date(raw["Workout Date"])
                distance, distance_source = mmf_distance, "mapmywalk"
                moving_s = None

            def pace(seconds):
                return rnd(seconds / 60 / distance, 2) if seconds and distance and distance > 0.05 else None

            calories = num(raw["Calories Burned (kCal)"])
            gain_m = props.get("elevation_gain_m")
            rows.append({
                "workout_id": workout_id,
                "date": day.isoformat(),
                "year": day.year,
                "month": day.month,
                "weekday": day.strftime("%a"),
                "start_time_local": start.isoformat(timespec="minutes") if start else None,
                "start_hour": start.hour if start else None,
                "activity": raw["Activity Type"],
                "distance_mi": rnd(distance, 3),
                "distance_source": distance_source,
                "mmf_distance_mi": rnd(mmf_distance, 3),
                "duration_s": int(duration_s) if duration_s else None,
                "moving_time_s": moving_s,
                "pace_min_per_mi": pace(duration_s),
                "moving_pace_min_per_mi": pace(moving_s),
                "avg_speed_mph": rnd(distance / (duration_s / 3600), 2) if duration_s and distance else None,
                "max_5min_speed_mph": props.get("max_5min_speed_mph"),
                "elevation_gain_ft": round(gain_m * FEET_PER_METER) if gain_m is not None else None,
                # MapMyWalk recorded 0 kcal for every 2016-2018 workout; treat as missing.
                "calories": int(calories) if calories else None,
                "steps": int(num(raw["Steps"])) if num(raw["Steps"]) else None,
                "notes": raw["Notes"] or None,
                "has_track": bool(feature),
                "gps_segments": props.get("segments"),
                "gps_glitches_removed": props.get("gps_glitches_removed"),
                "link": raw["Link"],
                **{k: places.get(workout_id, {}).get(k)
                   for k in ("place", "place_detail", "place_source", "place_url", "city", "county", "state", "country", "region")},
            })
    rows.sort(key=lambda r: (r["start_time_local"] or r["date"]), reverse=True)
    return rows


def write_clean_csv(rows, out_path):
    tmp = out_path.with_suffix(".part")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CLEAN_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if v is None else v for k, v in row.items()})
    tmp.replace(out_path)


def simplify(coords, tolerance_m):
    """Douglas-Peucker on [lon, lat, ...] points, using a local flat projection in meters."""
    return [coords[i] for i in simplify_indices(coords, tolerance_m)]


def simplify_indices(coords, tolerance_m):
    if len(coords) < 3:
        return list(range(len(coords)))
    lat0 = math.radians(coords[0][1])
    xy = [(c[0] * 111320 * math.cos(lat0), c[1] * 110540) for c in coords]
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        best, best_k = 0.0, None
        for k in range(i + 1, j):
            x, y = xy[k]
            if length == 0:
                d = math.hypot(x - x1, y - y1)
            else:
                d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / length
            if d > best:
                best, best_k = d, k
        if best_k is not None and best > tolerance_m:
            keep[best_k] = True
            stack += [(i, best_k), (best_k, j)]
    return [i for i, k in enumerate(keep) if k]


def format_duration(seconds):
    if not seconds:
        return ""
    h, rem = divmod(int(seconds), 3600)
    return f"{h}:{rem // 60:02d}" if h else f"{rem // 60} min"


def format_pace(minutes):
    if not minutes:
        return ""
    whole = int(minutes)
    return f"{whole}:{round((minutes - whole) * 60):02d} /mi"


def placemarks(row, feature):
    day = datetime.fromisoformat(row["date"])
    title = f"{day:%b} {day.day}, {day.year} · {row['distance_mi']:.2f} mi {row['activity'].lower()}"
    details = [
        ("Date", f"{day:%A, %B} {day.day}, {day.year}"),
        ("Distance", f"{row['distance_mi']:.2f} mi"),
        ("Time", format_duration(row["duration_s"])),
        ("Pace", format_pace(row["pace_min_per_mi"])),
        ("Elevation gain", f"{row['elevation_gain_ft']} ft" if row["elevation_gain_ft"] is not None else ""),
        ("Notes", row["notes"] or ""),
    ]
    description = "<br>".join(f"<b>{k}:</b> {escape(v)}" for k, v in details if v)
    description += f'<br><a href="{escape(row["link"])}">View on MapMyWalk</a>'
    data = {
        "date": row["date"], "year": row["year"], "activity": row["activity"],
        "distance_mi": row["distance_mi"], "pace_min_per_mi": row["pace_min_per_mi"] or "",
        "workout_id": row["workout_id"],
    }
    extended = "".join(f'<Data name="{k}"><value>{escape(str(v))}</value></Data>' for k, v in data.items())

    lines = feature["geometry"]["coordinates"]
    out = []
    for n, line in enumerate(lines, 1):
        name = title if len(lines) == 1 else f"{title} (part {n} of {len(lines)})"
        points = " ".join(f"{c[0]},{c[1]}" for c in simplify(line, KML_TOLERANCE_M))
        out.append(
            f"<Placemark><name>{escape(name)}</name>"
            f"<description><![CDATA[{description}]]></description>"
            f"<styleUrl>#y{row['year']}</styleUrl>"
            f"<ExtendedData>{extended}</ExtendedData>"
            f"<LineString><tessellate>1</tessellate><coordinates>{points}</coordinates></LineString>"
            f"</Placemark>"
        )
    return out


def write_kml(name, rows, track_features, out_path):
    years = sorted({r["year"] for r in rows})
    styles = "".join(
        f'<Style id="y{y}"><LineStyle><color>{YEAR_COLORS[y % len(YEAR_COLORS)]}</color>'
        f"<width>3</width></LineStyle></Style>"
        for y in years
    )
    body = "".join(p for r in rows for p in placemarks(r, track_features[r["workout_id"]]))
    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{escape(name)}</name>{styles}{body}</Document></kml>\n"
    )
    out_path.write_text(kml, encoding="utf-8")
    return len(kml.encode("utf-8"))


def write_js(path, variable, value):
    tmp = path.with_suffix(".part")
    tmp.write_text(f"window.{variable} = {json.dumps(value, separators=(',', ':'))};\n", encoding="utf-8")
    tmp.replace(path)
    return path.stat().st_size


def write_site_data(rows, track_features, kml_dir, site_data_dir):
    """Write the dashboard's data as .js files (so the page also works opened from disk)."""
    site_data_dir.mkdir(parents=True, exist_ok=True)
    workouts, routes = [], {}
    for r in rows:
        feature = track_features.get(r["workout_id"])
        start = None
        if feature:
            segments = []
            for line, times in zip(feature["geometry"]["coordinates"], feature["properties"]["times"]):
                flat = []
                for i in simplify_indices(line, KML_TOLERANCE_M):
                    flat += [round(line[i][1], 5), round(line[i][0], 5), times[i]]
                segments.append(flat)
            routes[r["workout_id"]] = segments
            start = segments[0][:2]
        workouts.append({
            "id": r["workout_id"], "date": r["date"], "start": r["start_time_local"],
            "hour": r["start_hour"], "activity": r["activity"], "mi": r["distance_mi"],
            "dur": r["duration_s"], "moving": r["moving_time_s"], "pace": r["pace_min_per_mi"],
            "mpace": r["moving_pace_min_per_mi"], "ft": r["elevation_gain_ft"],
            "steps": r["steps"], "notes": r["notes"], "link": r["link"], "latlon": start,
            "place": r["place"], "detail": r["place_detail"], "url": r["place_url"], "city": r["city"], "county": r["county"],
            "state": r["state"], "country": r["country"], "region": r["region"],
        })
    # Names of Three Rivers Park District places and trails (their link is on its website), so the
    # dashboard can find walks whose route passes through one even when it isn't the walk's place.
    links = Path(site_data_dir).parent.parent / "place_links.csv"
    three_rivers = set()
    if links.exists():
        with open(links, newline="", encoding="utf-8") as f:
            three_rivers = {r["name"] for r in csv.DictReader(f) if "threeriversparks.org/" in (r.get("url") or "")}
    three_rivers |= {r["place"] for r in rows if "threeriversparks.org/" in (r["place_url"] or "")}
    meta = {"updated": datetime.now(LOCAL_TZ).isoformat(timespec="minutes"),
            "three_rivers": sorted(n for n in three_rivers if n),
            "kml_years": sorted({r["year"] for r in rows if r["has_track"]}, reverse=True),
            "regions": places.REGIONS}
    write_js(site_data_dir / "workouts.js", "GW_DATA", {"meta": meta, "workouts": workouts})
    size = write_js(site_data_dir / "routes.js", "GW_ROUTES", routes)
    site_kml = site_data_dir / "kml"
    site_kml.mkdir(exist_ok=True)
    for kml in Path(kml_dir).glob("*.kml"):
        (site_kml / kml.name).write_bytes(kml.read_bytes())
    print(f"Site data: {len(workouts)} workouts, routes {size / 1e6:.1f} MB -> {site_data_dir}")


def export_all(stats_csv, tracks_dir, data_dir, site_data_dir=None):
    track_features = load_tracks(tracks_dir)
    rows = clean_rows(stats_csv, track_features, load_places(data_dir))
    clean_csv = Path(data_dir) / "workouts_clean.csv"
    write_clean_csv(rows, clean_csv)
    print(f"Clean stats: {len(rows)} workouts -> {clean_csv}")

    kml_dir = Path(data_dir) / "kml"
    kml_dir.mkdir(exist_ok=True)
    mapped = [r for r in rows if r["has_track"]]
    size = write_kml("GoneWalking: all routes", mapped, track_features, kml_dir / "all_routes.kml")
    warn = "  (over Google My Maps' 5 MB import limit: use the per-year files)" if size > MY_MAPS_MAX_BYTES else ""
    print(f"KML: all_routes.kml, {len(mapped)} routes, {size / 1e6:.1f} MB{warn}")
    for year in sorted({r["year"] for r in mapped}):
        year_rows = [r for r in mapped if r["year"] == year]
        write_kml(f"GoneWalking: {year}", year_rows, track_features, kml_dir / f"{year}.kml")
    print(f"KML: {len({r['year'] for r in mapped})} per-year files -> {kml_dir}")

    if site_data_dir:
        write_site_data(rows, track_features, kml_dir, Path(site_data_dir))
