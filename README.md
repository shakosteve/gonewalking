# GoneWalking

Downloads my MapMyWalk / MapMyFitness workout history and GPS tracks so they can feed a
dashboard and a custom Google Map.

**Live site: [Steve's Gone Walking](https://shakosteve.github.io/gonewalking/)**, published
with GitHub Pages from the `docs/` folder on `main`.

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
python gonewalking.py photos     # match your photos to walks and publish them as albums
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

## Scheduled updates

`sync.bat` runs `sync` and then `photos` (when `PHOTOS_DIR` is set), logs everything to
`data/logs/` (the newest 30 runs are kept) and shows a Windows notification saying how it went:
any new walks, a failure and why (an expired MapMyFitness token is spelled out), or a warning when
the token is within 14 days of expiring. Click the notification to open the log. It exits with an
error code when a step fails, so Task Scheduler shows the run as failed too. The site files are
updated; commit and push `docs/` to publish them.

To schedule it: Task Scheduler → **Create Basic Task** → daily → **Start a program** →
`C:\code\GoneWalking\sync.bat`. Keep **Run only when user is logged on** (the default): Windows only
shows notifications in your own signed-in session, so pick a time you're usually at the PC. If the
PC was off or asleep then, tick **Run task as soon as possible after a scheduled start is missed**
(task properties → Settings).

Optional settings in `.env`:

```
MMF_TOKEN_EXPIRES=2026-12-13        # the token's expiry date, for the warning
REPORT_ON=always                    # or "failure" to hear only about problems (and expiry)
WINDOWS_NOTIFY=off                  # no notification
# REPORT_EMAIL=you@example.com      # also e-mail the report (needs SMTP_PASSWORD; see scheduled_sync.py)
```

`python scheduled_sync.py --test` shows a test notification; `--dry-run` runs everything but prints
the report instead; `--sample` sends the report as if your latest walk had just been downloaded,
without syncing.

Each report lists new walks with their stats and share links, gives the git commands to publish
when there are uncommitted changes (`git add .`; `.gitignore` keeps out `.env`, `data/` and
`photo_drop/`), and says when the MapMyFitness token expires, adding the
steps to renew it once it's within 14 days of expiring or has been rejected.

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

## Photos

`python gonewalking.py photos` matches your photos to walks and publishes the likely ones as an
album on each walk. Photos not taken during a walk (or within 10 minutes of one), more than
300 m from its track, or with no location at all, are skipped. Each remaining photo gets a certainty score from:

- how close it was to the track at the time (within 30 m counts fully, up to 300 m much less);
- whether it was taken during the walk or just before or after;
- how its time and location were recorded (the camera's own, or Google's, which may be estimated);
- what it shows, once checked (see below): walk photos are outdoors, so indoor, in-vehicle and
  screenshot photos count against it.

90% and up is published; 50–90% is held for your review and not published; below that is left off.

**Near-duplicates.** Photos on the same walk taken within 60 seconds of each other, framed the
same way (both portrait or both landscape) and nearly identical are grouped, and only the
sharpest of each group is published. The others appear under "Near-duplicates" on the review
page, where **Keep it too** publishes one anyway (`keep` = `yes` in `photo_overrides.csv`).

Time and location come from the photo itself, or from the `.json` file Google Takeout saves beside
each photo. Duplicates count once, and an edited copy ("-edited") is used instead of its original.
What each photo file says is kept in `data/photo_cache.json`, so later runs only read new photos.

Where the photos are: new photos go in `photo_drop/` in this folder (set by `PHOTOS_DIR=photo_drop`
in `.env`; git ignores the folder). Several folders can be listed, separated by `;`, or named on
the command line. Albums build up over time: photos published by earlier runs stay published
(recorded in `data/photo_published.json`) even after they leave `photo_drop/`, unless you reject
them. A new photo nearly identical to one already published is set aside as a near-duplicate.
Run `photos` after `sync` to pick up photos from new walks. Once a photo from `photo_drop/` is published, it's moved
to the Recycle Bin (with any Takeout `.json` beside it); photos to review, near-duplicates and photos
that don't match a walk stay there. Folders named on the command line are never emptied.

**Reviewing.** `python gonewalking.py photos --review` also writes `data/photo_review.html`; open it
in a browser (it is never published, and the next run without `--review` removes it). It shows the
photos to review, and the published ones, with their score, the reasons, and links to the map, the
walk and the original file. Approve or reject each, then **Download photo_overrides.csv**, save it
in this folder (replacing the old one) and run `photos` again. `photo_overrides.csv`
(`file,workout_id,keep`) can also be edited by hand: a `workout_id` publishes the photo on that walk, a
blank one keeps it off the site. These decisions win over the score.

**What a photo shows.** `photo_content.csv` (`file,content`) records whether each candidate photo
is `outdoor`, `indoor`, `vehicle`, `screen` (a screenshot or document) or `unclear`. PNGs and
screenshots are marked `screen` automatically. `photos --sheets` makes numbered contact sheets of
the candidates not yet checked (`data/photo_sheets/`, with `index.json` saying which photo
is which), for filling this in, by eye or by asking Claude to look at them.

It writes:

- `docs/photos/{workout_id}/`: a 1024 px web copy and a 400 px thumbnail of each published photo.
  Their EXIF is removed, so the published files carry no location, camera or time details.
- `docs/data/photos.js`: which photos each walk has, when and where on the route.
- `data/photo_matches.csv`: every photo looked at, with its status, score and why.
- `data/photo_review.html` (with `--review`) and `data/photo_thumbs/`: the review page, and small
  copies of candidate photos used for it and for spotting near-duplicates.

Published photos count toward GitHub Pages' 1 GB site limit: each one takes about 250 KB.

## Dashboard (`docs/`)

A static site, published at **https://shakosteve.github.io/gonewalking/** by GitHub Pages
(Settings → Pages → Deploy from a branch → `main`, `/docs`; `docs/.nojekyll` makes Pages serve
the files as-is). `process` refreshes its data in `docs/data/` (stats, simplified routes and
KML) and the walks' share pages in `docs/w/`; everything else in `docs/` is hand-written.

To update the live site: run `python gonewalking.py sync`, then commit and push the changes in
`docs/` (and any edited `place_*.csv` files). Pages republishes within a minute or two.

The files in `docs/`:

- `index.html`, `style.css`, `app.js`: the page. Charts are plain SVG; the map uses Leaflet
  with OpenStreetMap tiles. No build step.
- `config.js`: your name for the header (`ownerName`), your birth date for the age trends
  (`birthDate`, YYYY-MM-DD; leave empty to hide age), where you've lived (`homes`, each with
  the date it started; drives the "Living in" filter, the "Two chapters" comparison and the
  per-home map views), other milestones to mark on the time charts (`events`), and `myMapsId`
  to embed your Google My Map on the page.
- `images/portrait.jpg`: header photo (a 320 px crop of the original).
- `images/share.jpg`: the 1200×630 preview card shown when the site's link is shared on social
  media (set by the `og:` tags in `index.html`). Remake it if the title or portrait changes.
- `images/openmoji/`: illustrations from [OpenMoji](https://openmoji.org/), licensed
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/); the footer credits them, as
  the licence requires.

To view it locally, open `docs/index.html` in a browser, or serve it:

```
python -m http.server 8000 -d docs     # then visit http://localhost:8000
```

The address keeps track of what you're looking at, so views can be linked and bookmarked:
`#place=Murphy-Hanrehan%20Park%20Reserve` opens a place with its walk list beside the map, and
`#walk={workout_id}` (optionally after a place, joined with `&`) opens one walk. The browser's
Back and Forward move between a place's walk list and its walks.

Social media link previews ignore everything after `#`, so those links all preview as the home
page. To share one walk, use its **Copy link to this walk** button (**Share this walk** on a
phone), which gives `w/{workout_id}.html`: a page made by `process` (`previews.py`) with that
walk's own title, stats and a 1200×630 picture of its route on an OpenStreetMap map, which sends
visitors on to the walk on the dashboard. Walks with photos split the picture: the map on the left
and a contact sheet of up to 9 photos (spread along the walk, the last marked +N if there are
more) on the right. Both `process` and `photos` bring the pages up to date. The map tiles are
cached in `data/tile_cache/`, and a picture is only redrawn when its walk's route, text or photos
change (`data/previews.json`); bump
`STYLE_VERSION` in `previews.py` to redraw them all after changing how they look.

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
