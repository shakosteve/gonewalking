"""Walk soundtracks: the Spotify songs that played during each walk, from the MusicDashboard project.

MusicDashboard (a separate project) keeps the listening history in its data folder: the Spotify privacy
export (data/export/Streaming_History_Audio_*.json, each play with the moment it ended and how long it
played) and, for plays since the export, the live sync (recently_played.jsonl, each play with the moment
it ended). This module only reads those files; it never changes them and never calls Spotify.

A song belongs to a walk when at least 30 seconds of it played during the walk (Spotify counts a play
from 30 seconds), and a walk gets a soundtrack when at least 3 songs did. Its mile marker is where the walk was, by GPS, when the song started (or when the walk
started, for a song already playing). Podcasts are left out.

Writes docs/data/music.js (window.GW_MUSIC). Each song is listed once and walks point at it by number:
    {"songs": [[name, artist, cover id, Spotify track id], ...],
     "walks": {walk_id: {"share": fraction of the walk with music, "list": [[song number, mile], ...]}}}
Cover id is the end of an i.scdn.co image address; the dashboard links every song and cover to Spotify.

The data folder is MUSIC_DATA in .env, or by default the MusicDashboard folder next to this one. If it
isn't there, music.js is left as it is and the dashboard simply shows no soundtracks.
"""

import bisect
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

MIN_PLAY_S = 30
MIN_SONGS = 3  # fewer is a song left playing at the trailhead, not a soundtrack
COVER_PREFIX = "https://i.scdn.co/image/"


def music_dir(root):
    return Path(os.environ.get("MUSIC_DATA") or Path(root).parent / "MusicDashboard" / "data")


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def load_plays(folder):
    """Every song play as (start, end, track uri, name, artist), oldest first."""
    plays = []
    for f in sorted(folder.glob("export/Streaming_History_Audio_*.json")):
        for s in json.loads(f.read_text(encoding="utf-8")):
            if s.get("spotify_track_uri") and s.get("master_metadata_track_name"):
                end = parse_time(s["ts"])
                plays.append((end - timedelta(milliseconds=s["ms_played"]), end, s["spotify_track_uri"],
                              s["master_metadata_track_name"], s.get("master_metadata_album_artist_name") or ""))
    exported_to = max((p[1] for p in plays), default=datetime.min.replace(tzinfo=timezone.utc))
    live = folder / "recently_played.jsonl"
    if live.exists():
        for line in live.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            p = json.loads(line)
            end = parse_time(p["played_at"])  # the live sync's time is when the song ended
            if end > exported_to:
                plays.append((end - timedelta(milliseconds=p.get("duration_ms") or 0), end, p["uri"], p["name"],
                              ", ".join(p.get("artists") or [])))
    plays.sort()
    return plays


def load_covers(folder):
    """Each track's cover image id, from MusicDashboard's history (album of each track) and art cache."""
    art_file = folder / "art.json"
    art = json.loads(art_file.read_text(encoding="utf-8")) if art_file.exists() else {}
    covers = {}
    for name in ("history.jsonl", "recently_played.jsonl"):
        path = folder / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                p = json.loads(line)
                image = (art.get(p.get("album_uri")) or {}).get("image") or ""
                if image.startswith(COVER_PREFIX):
                    covers[p["uri"]] = image[len(COVER_PREFIX):]
    return covers


def miles_along(feature):
    """The walk's GPS points as (seconds from start, miles so far), scaled to the walk's stated distance."""
    out, total, prev = [], 0.0, None
    for line, times in zip(feature["geometry"]["coordinates"], feature["properties"]["times"]):
        prev = None  # no distance across a gap between segments
        for (lon, lat, *_), t in zip(line, times):
            if prev:
                dlat, dlon = math.radians(lat - prev[1]), math.radians(lon - prev[0])
                a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat)) * math.cos(math.radians(prev[1])) * math.sin(dlon / 2) ** 2
                total += 3958.8 * 2 * math.asin(math.sqrt(a))
            out.append((t, total))
            prev = (lon, lat)
    stated = feature["properties"].get("distance_mi") or total
    scale = stated / total if total else 1
    return [(t, m * scale) for t, m in out]


def mile_at(points, seconds):
    if not points:
        return None
    for (t0, m0), (t1, m1) in zip(points, points[1:]):
        if t1 >= seconds:
            return m0 + (m1 - m0) * max(0.0, min(1.0, (seconds - t0) / ((t1 - t0) or 1)))
    return points[-1][1]


def update_music(root, tracks_dir, site_data_dir):
    folder = music_dir(root)
    if not (folder / "export").exists() and not (folder / "recently_played.jsonl").exists():
        print(f"Music: no listening history at {folder} (set MUSIC_DATA in .env), soundtracks skipped")
        return
    plays = load_plays(folder)
    covers = load_covers(folder)
    starts = [p[0] for p in plays]
    songs, song_index, walks = [], {}, {}
    features = sorted((json.loads(f.read_text(encoding="utf-8")) for f in Path(tracks_dir).glob("*.geojson")),
                      key=lambda f: f["properties"]["start_time_utc"])
    for feature in features:
        props = feature["properties"]
        begin = parse_time(props["start_time_utc"])
        finish = begin + timedelta(seconds=props["duration_s"])
        i = bisect.bisect_left(starts, begin - timedelta(hours=2))
        found, heard = [], 0.0
        while i < len(plays) and plays[i][0] < finish:
            start, end, uri, name, artist = plays[i]
            overlap = (min(end, finish) - max(start, begin)).total_seconds()
            if overlap >= MIN_PLAY_S:
                found.append(plays[i])
                heard += overlap
            i += 1
        if len(found) < MIN_SONGS:
            continue
        points = miles_along(feature)
        entry = []
        for start, end, uri, name, artist in found:
            if uri not in song_index:
                song_index[uri] = len(songs)
                songs.append([name, artist, covers.get(uri, ""), uri.split(":")[-1]])
            mile = mile_at(points, max(0.0, (start - begin).total_seconds()))
            entry.append([song_index[uri], round(mile, 1) if mile is not None else None])
        walks[props["id"]] = {"share": round(min(1.0, heard / props["duration_s"]), 2), "list": entry}
    out = Path(site_data_dir) / "music.js"
    tmp = out.with_suffix(".part")
    tmp.write_text("window.GW_MUSIC = " + json.dumps({"songs": songs, "walks": walks}, separators=(",", ":"),
                                                      ensure_ascii=False) + ";\n", encoding="utf-8")
    tmp.replace(out)
    print(f"Music: soundtracks for {len(walks)} walks, {len(songs)} different songs -> {out} ({out.stat().st_size // 1024} KB)")
