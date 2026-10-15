# GoneWalking

Downloads my MapMyWalk / MapMyFitness workout history and GPS tracks so they can feed a
dashboard and a custom Google Map.

## Setup

```
pip install -r requirements.txt
```

Create `.env` in this folder (it is git-ignored):

```
MMF_AUTH_TOKEN=US.xxxxxxxx
```

To get the token: log in at https://www.mapmyfitness.com, open DevTools (F12) →
**Application** → **Cookies** → `https://www.mapmyfitness.com`, and copy the value of
`auth-token`. The `auth-token-expiry` cookie shows when it expires (about 60 days); when it
does, the tool stops with a message telling you to refresh it.

## Usage

```
python gonewalking.py sync       # refresh stats, download new GPS tracks, build compact tracks
python gonewalking.py stats      # refresh stats only
python gonewalking.py process    # rebuild compact tracks from TCX files (--force to redo all)
```

`sync` is safe to run any time: it only downloads and processes tracks it doesn't already have.

Options for `sync`: `--limit N` (download at most N tracks), `--delay SECONDS` (pause between
downloads, default 2), `--retry-no-gps` (re-check workouts that previously had no GPS data).

## Output

```
data/
  workouts.csv        # full workout history export (one row per workout, newest first)
  tcx/{workout_id}.tcx  # GPS track per workout: lat/lon, altitude, distance, timestamp each second
  manifest.json       # per-workout download status: ok / no_gps / error
  tracks/{workout_id}.geojson  # compact, cleaned track per workout (see below)
```

### Compact tracks (`data/tracks/`)

Built by [tracks.py](tracks.py) from the TCX files. Each file is one GeoJSON `Feature`:

- `geometry`: a `MultiLineString` of `[lon, lat, elevation_m]`, one point every ~10 m. A track
  has more than one line only where the recording jumped to a new place (paused and resumed
  elsewhere); the jump isn't drawn or counted in distance.
- `properties.times`: the same shape as the coordinates, holding seconds since the workout
  started, so pace can be computed for any stretch.
- Summary properties: `date`, `activity`, `start_time_utc`, `distance_mi`, `duration_s`,
  `moving_time_s`, `avg_pace_min_per_mi` (over moving time), `max_5min_speed_mph`,
  `elevation_gain_m`, `segments`, `gps_glitches_removed`, `mmf_distance_mi` (MapMyWalk's own
  figure, for comparison), `link`.

Cleaning: GPS points implying an impossible speed (over 10 mph walking, 40 mph cycling) are
dropped; elevation is averaged over a minute before counting climbs, because phone altimeters
are noisy.

Workout IDs match the `Link` column in `workouts.csv`
(`http://www.mapmywalk.com/workout/{workout_id}`).

## Where the data comes from

- Stats: `https://www.mapmyfitness.com/workout/export/csv`
- Tracks: `https://www.mapmyfitness.com/workout/export/{workout_id}/tcx` (the "TCX" button on
  a workout page)

Both are the same downloads the website offers, fetched with your logged-in session.
