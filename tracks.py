"""Convert downloaded TCX files into compact, cleaned GeoJSON tracks.

Each workout becomes data/tracks/{workout_id}.geojson: a single GeoJSON Feature whose
MultiLineString holds [lon, lat, elevation_m] points spaced at least RESAMPLE_M apart. A
track has more than one line only where the recording jumped to a new location (paused and
resumed elsewhere). properties.times mirrors the coordinates with seconds since the start
of the workout, so pace can be computed for any stretch.
"""

import csv
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

# Bump when the output format or cleaning rules change; older files get reprocessed.
PROCESS_VERSION = 1

TCX_NS = "{http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2}"
METERS_PER_MILE = 1609.344
RESAMPLE_M = 10.0          # keep a point once we've moved this far from the last kept one
GLITCH_SLACK_M = 25.0      # GPS jitter allowance on top of the speed limit
MAX_GLITCH_RUN = 15        # after this many rejected points in a row, trust the new position
ELEVATION_SMOOTH_S = 60
ELEVATION_HYSTERESIS_M = 3.0
STOPPED_MPH = 1.0          # segments slower than this don't count toward moving time
MAX_SPEED_WINDOW_S = 300
MAX_PLAUSIBLE_MPH = {"Walk": 10.0, "Bike Ride": 40.0}
DEFAULT_MAX_MPH = 15.0

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
CSV_DATE_RE = re.compile(r"([A-Za-z]+)\.? (\d+), (\d{4})")
WORKOUT_ID_RE = re.compile(r"/workout/(\d+)")


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(a))


def parse_csv_date(text):
    """'Sept. 28, 2026' -> date(2026, 9, 28)"""
    month, day, year = CSV_DATE_RE.match(text.strip()).groups()
    return date(int(year), MONTHS[month[:3].lower()], int(day))


def read_tcx_points(path):
    """Return [(epoch_seconds, lat, lon, elevation_or_None)] for trackpoints with a position."""
    points = []
    for tp in ET.parse(path).getroot().iter(f"{TCX_NS}Trackpoint"):
        pos = tp.find(f"{TCX_NS}Position")
        if pos is None:
            continue
        t = datetime.fromisoformat(tp.find(f"{TCX_NS}Time").text).timestamp()
        lat = float(pos.find(f"{TCX_NS}LatitudeDegrees").text)
        lon = float(pos.find(f"{TCX_NS}LongitudeDegrees").text)
        alt = tp.find(f"{TCX_NS}AltitudeMeters")
        points.append((t, lat, lon, float(alt.text) if alt is not None else None))
    points.sort(key=lambda p: p[0])
    return points


def split_and_clean(points, max_mph):
    """Drop GPS glitches and split the track wherever it teleports.

    A point that implies an impossible speed from the last accepted point is held back.
    If the track keeps going from the new location for MAX_GLITCH_RUN points, it was a
    real relocation (app paused, then resumed somewhere else): the held-back points start
    a new segment. Otherwise they were glitches and are dropped.
    Returns (segments, glitches_removed).
    """
    max_mps = max_mph * METERS_PER_MILE / 3600

    def plausible(a, b):
        dist = haversine_m(a[1], a[2], b[1], b[2])
        return dist <= max_mps * max(b[0] - a[0], 0) + GLITCH_SLACK_M

    segments, held, glitches = [[points[0]]], [], 0
    for p in points[1:]:
        if plausible(segments[-1][-1], p):
            glitches += len(held)
            held = []
            segments[-1].append(p)
        elif held and plausible(held[-1], p):
            held.append(p)
            if len(held) >= MAX_GLITCH_RUN:
                segments.append(held)
                held = []
        else:
            glitches += len(held)
            held = [p]
    glitches += len(held)
    return [s for s in segments if len(s) >= 2], glitches


def resample(points):
    """Keep the first and last point, and each point at least RESAMPLE_M from the last kept."""
    kept = [points[0]]
    for p in points[1:-1]:
        if haversine_m(kept[-1][1], kept[-1][2], p[1], p[2]) >= RESAMPLE_M:
            kept.append(p)
    if len(points) > 1:
        kept.append(points[-1])
    return kept


def smooth_elevations(points):
    """Average each point's elevation over +/- half of ELEVATION_SMOOTH_S (phone altimeters are noisy)."""
    pts = [p for p in points if p[3] is not None]
    half, out, lo, hi, total = ELEVATION_SMOOTH_S / 2, [], 0, 0, 0.0
    for p in pts:
        while hi < len(pts) and pts[hi][0] <= p[0] + half:
            total += pts[hi][3]
            hi += 1
        while pts[lo][0] < p[0] - half:
            total -= pts[lo][3]
            lo += 1
        out.append(total / (hi - lo))
    return out


def elevation_gain(elevations):
    gain, ref = 0.0, None
    for e in elevations:
        if ref is None or e < ref:
            ref = e
        elif e - ref >= ELEVATION_HYSTERESIS_M:
            gain += e - ref
            ref = e
    return gain


def max_speed_mph(times, cum_dist):
    """Fastest average speed over any window of at least MAX_SPEED_WINDOW_S seconds."""
    best, j = 0.0, 0
    for i in range(len(times)):
        while j < len(times) and times[j] - times[i] < MAX_SPEED_WINDOW_S:
            j += 1
        if j == len(times):
            break
        best = max(best, (cum_dist[j] - cum_dist[i]) / (times[j] - times[i]))
    return best * 3600 / METERS_PER_MILE


def build_feature(workout, points):
    max_mph = MAX_PLAUSIBLE_MPH.get(workout["activity"], DEFAULT_MAX_MPH)
    segments, glitches = split_and_clean(points, max_mph)
    if not segments:
        raise ValueError("no usable GPS segments")
    t0 = segments[0][0][0]

    coords, times, distance_m, moving_s, top_mph, gain_m = [], [], 0.0, 0.0, 0.0, 0.0
    for seg in segments:
        track = resample(seg)
        cum_dist = [0.0]
        for a, b in zip(track, track[1:]):
            d = haversine_m(a[1], a[2], b[1], b[2])
            cum_dist.append(cum_dist[-1] + d)
            dt = b[0] - a[0]
            if dt > 0 and d / dt * 3600 / METERS_PER_MILE >= STOPPED_MPH:
                moving_s += dt
        distance_m += cum_dist[-1]
        top_mph = max(top_mph, max_speed_mph([p[0] for p in track], cum_dist))
        gain_m += elevation_gain(smooth_elevations(seg))
        coords.append([
            [round(p[2], 6), round(p[1], 6)] + ([round(p[3], 1)] if p[3] is not None else [])
            for p in track
        ])
        times.append([round(p[0] - t0) for p in track])

    distance_mi = distance_m / METERS_PER_MILE
    duration_s = segments[-1][-1][0] - t0
    return {
        "type": "Feature",
        "geometry": {"type": "MultiLineString", "coordinates": coords},
        "properties": {
            "id": workout["id"],
            "date": workout["date"].isoformat(),
            "activity": workout["activity"],
            "start_time_utc": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(timespec="seconds"),
            "distance_mi": round(distance_mi, 3),
            "duration_s": round(duration_s),
            "moving_time_s": round(moving_s),
            "avg_pace_min_per_mi": round(moving_s / 60 / distance_mi, 2) if distance_mi > 0.05 else None,
            "max_5min_speed_mph": round(top_mph, 2),
            "elevation_gain_m": round(gain_m),
            "segments": len(segments),
            "gps_points_raw": len(points),
            "gps_glitches_removed": glitches,
            "mmf_distance_mi": round(float(workout["mmf_distance_mi"]), 3),
            "link": workout["link"],
            "times": times,
            "process_version": PROCESS_VERSION,
        },
    }


def read_workouts(stats_csv):
    workouts = {}
    with open(stats_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            m = WORKOUT_ID_RE.search(row.get("Link", ""))
            if m:
                workouts[m.group(1)] = {
                    "id": m.group(1),
                    "date": parse_csv_date(row["Workout Date"]),
                    "activity": row["Activity Type"],
                    "mmf_distance_mi": row["Distance (mi)"] or 0,
                    "link": row["Link"],
                }
    return workouts


def is_current(out_path):
    if not out_path.exists():
        return False
    try:
        with out_path.open(encoding="utf-8") as f:
            return json.load(f)["properties"].get("process_version") == PROCESS_VERSION
    except (ValueError, KeyError):
        return False


def process_all(stats_csv, tcx_dir, out_dir, force=False):
    out_dir.mkdir(parents=True, exist_ok=True)
    workouts = read_workouts(stats_csv)
    done = skipped = failed = 0
    for tcx in sorted(Path(tcx_dir).glob("*.tcx")):
        workout_id = tcx.stem
        out = out_dir / f"{workout_id}.geojson"
        if not force and is_current(out) and out.stat().st_mtime >= tcx.stat().st_mtime:
            skipped += 1
            continue
        workout = workouts.get(workout_id)
        if workout is None:
            print(f"  {workout_id}: not in workouts.csv, skipped")
            failed += 1
            continue
        try:
            points = read_tcx_points(tcx)
            if len(points) < 2:
                raise ValueError("fewer than 2 GPS points")
            feature = build_feature(workout, points)
        except (ET.ParseError, ValueError) as exc:
            print(f"  {workout_id}: FAILED: {exc}")
            failed += 1
            continue
        tmp = out.with_suffix(".part")
        tmp.write_text(json.dumps(feature, separators=(",", ":")), encoding="utf-8")
        tmp.replace(out)
        done += 1
    print(f"Tracks: {done} processed, {skipped} up to date, {failed} failed -> {out_dir}")
