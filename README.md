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
  workouts_clean.csv  # corrected stats, one row per workout (see below)
  kml/all_routes.kml  # every route, for Google My Maps
  kml/{year}.kml      # the same, one file per year
```

`process` (and therefore `sync`) rebuilds `tracks/`, `workouts_clean.csv` and `kml/`.

## Places and regions

`python gonewalking.py places` (also run by `sync`) works out where each walk was, using
OpenStreetMap: the park (from park boundaries) or trail, plus city, county, state and country
(from Nominatim). Each walk also gets a **region**: Twin Cities metro (the seven Metropolitan
Council counties), Greater Minnesota, Other U.S. states, or Outside the U.S. These are set in
[places.py](places.py) (`METRO_COUNTIES`, `HOME_STATE`). Lookups are cached in
`data/places_cache/`, so later runs only fetch new areas. If the OpenStreetMap servers are busy,
some park tiles are skipped; run `places` again later to fill them in.

Two hand-edited files refine the results:

- `place_overrides.csv` (`name,kind,lat,lon,radius_m,workout_id`): your names for spots, applied
  to walks centred within `radius_m` of the point (or to one walk, by `workout_id`). `kind` is
  `park` or `trail`: a walk's place is its park, and a trail becomes a detail
  ("Cleary Lake Regional Park · Ironwood Trail"). `places` lists unnamed spots with Google
  Maps links to help.
- `place_aliases.csv` (`osm_name,name`): renames an OpenStreetMap park or trail name everywhere
  it appears.
- `place_links.csv` (`name,url`): a web page for a place, overriding the one from OpenStreetMap.

## Dashboard (`docs/`)

A static site, ready for GitHub Pages (Settings → Pages → Deploy from a branch → `main`,
`/docs`). `process` refreshes its data in `docs/data/` (stats, simplified routes and KML);
everything else in `docs/` is hand-written:

- `index.html`, `style.css`, `app.js`: the page. Charts are plain SVG; the map uses Leaflet
  with OpenStreetMap tiles. No build step.
- `config.js`: your name for the header (`ownerName`), your birth date for the age trends
  (`birthDate`, YYYY-MM-DD; leave empty to hide age), where you've lived (`homes`, each with
  the date it started; drives the "Living in" filter, the "Two chapters" comparison and the
  per-home map views), other milestones to mark on the time charts (`events`), and `myMapsId`
  to embed your Google My Map on the page.
- `images/portrait.jpg`: header photo (a 320 px crop of the original).
- `images/openmoji/`: illustrations from [OpenMoji](https://openmoji.org/), licensed
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/); the footer credits them, as
  the licence requires.

To view it locally, open `docs/index.html` in a browser, or serve it:

```
python -m http.server 8000 -d docs     # then visit http://localhost:8000
```

A walk can be linked directly with `#walk={workout_id}` on the end of the URL.

### Clean stats (`data/workouts_clean.csv`)

Built by [exports.py](exports.py). Newest first. Distances, pace, speed and elevation come from
the GPS tracks rather than MapMyWalk's figures (`distance_source` = `gps`; `mmf_distance_mi`
keeps MapMyWalk's number). Dates and times are local (America/Chicago). `pace_min_per_mi` uses
total workout time; `moving_pace_min_per_mi` leaves out stops. `calories` is blank where
MapMyWalk recorded 0 (all of 2016–2018); `steps` is blank for bike rides.

### Google My Maps (`data/kml/`)

Routes are simplified to within 5 m of the GPS track. Each route's pop-up shows date,
distance, time, pace, elevation gain and a link to the workout; tracks that were split by a
jump appear as "part 1 of 2" etc. Lines are coloured by year.

To import: open https://mymaps.google.com → **Create a new map** → **Import** under the
untitled layer → upload `all_routes.kml` (or a year file). To restyle, click the layer's
style (paint roller) icon → **Style by data column** → `year` or `activity`.

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
