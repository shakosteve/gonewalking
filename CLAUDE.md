# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

GoneWalking downloads MapMyWalk/MapMyFitness workout history and GPS tracks, works out where each
walk was, matches phone photos to walks, and publishes a static dashboard from `docs/` to GitHub
Pages (https://shakosteve.github.io/gonewalking/). README.md is the detailed user manual; this
file covers what you need to change the code.

## Commands

There is no build step, test suite or linter. Python 3.13 on Windows; deps in `requirements.txt`
(requests, tzdata, Pillow).

```
python gonewalking.py sync       # download new stats + TCX tracks (needs MMF_AUTH_TOKEN in .env), then process
python gonewalking.py process    # rebuild everything derived from local downloads; offline (--force: redo all tracks)
python gonewalking.py places     # process + fetch missing OpenStreetMap lookups, list walks with no place name
python gonewalking.py photos     # match photo_drop/ photos to walks and publish (--review, --sheets)
python scheduled_sync.py --dry-run   # the unattended Task Scheduler run (sync.bat), report printed not sent
python -m http.server 8000 -d docs   # view the dashboard locally
```

To check a change, run `process` (fast, no network) and look at the dashboard. Avoid running
`photos` without folder arguments casually: once it publishes a photo from `photo_drop/` it moves the
original to the Recycle Bin.

## Pipeline

`gonewalking.py` is the CLI and orchestrator; `process()` runs the stages in order:

1. `tracks.py`: `data/tcx/*.tcx` → `data/tracks/{id}.geojson` (glitch removal, ~10 m resampling,
   segments split at jumps, smoothed elevation). Skips up-to-date files; bump `PROCESS_VERSION` to
   force a rebuild when the processing changes.
2. `places.py`: each walk's park/trail, city, county, state and region from OSM (Overpass park
   boundaries + Nominatim), cached in `data/places_cache/`, → `data/places.json`. Only fetches when
   called with `fetch_missing=True` (the `places` and `sync` commands). Applies the hand-edited
   `place_overrides.csv`, `place_aliases.csv` and `place_links.csv` from the repo root.
3. `exports.py`: joins `data/workouts.csv` with tracks and places → `data/workouts_clean.csv`, KML for
   Google My Maps, and the site data `docs/data/workouts.js` (`window.GW_DATA`) and `routes.js`
   (`window.GW_ROUTES`).
4. `previews.py`: per-walk share pages `docs/w/{id}.html` + 1200×630 `.jpg` route pictures, redrawn
   only when a fingerprint changes (`data/previews.json`); bump `STYLE_VERSION` to redraw all.

`photos.py` is separate (scores photos by distance to track, timing, metadata source and
`photo_content.csv`; dedupes; writes `docs/photos/{id}/` and `docs/data/photos.js`); `photo_overrides.csv`
decisions win over the score. `scheduled_sync.py` wraps `sync` + `photos`, optionally commits and
pushes `docs/` only (`AUTO_PUBLISH`), and sends e-mail/Windows reports.

## Key conventions

- **Generated vs hand-written in `docs/`:** `docs/data/*`, `docs/w/*` and `docs/photos/*` are
  regenerated; never edit them by hand. `index.html`, `app.js`, `style.css`, `config.js` and
  `images/` are hand-written. Site data is emitted as `.js` assignments (not JSON) so the page also
  works opened from `file://`.
- **The dashboard** (`docs/app.js`) is a single vanilla-JS IIFE: plain SVG charts, Leaflet map, no
  framework or bundler. State lives in the URL hash (`#place=...&walk={id}`). Site settings (homes,
  birth date, season theme) come from `config.js` (`window.GW_CONFIG`).
- **`data/` and `photo_drop/` are junctions into OneDrive** and git-ignored. `data/` holds
  irreplaceable state (`tcx/`, `photo_published.json`); don't delete or bulk-rewrite it.
- **Place CSVs:** loaders read only the documented columns (`place_overrides.csv`:
  `name,kind,lat,lon,radius_m,workout_id`; `place_links.csv`: `name,url`; `place_aliases.csv`:
  `osm_name,name`). Extra columns (`walks`, `dates`, `city`, `google_maps`, `where`, `search`) are
  reference notes for the person editing. A place's display name is its park; a trail becomes the
  detail ("Park · Trail"). Link precedence: `place_links.csv` > OSM website > Wikidata > Wikipedia.
- Places with a `threeriversparks.org` link are gathered into `meta.three_rivers` in `workouts.js`,
  which the dashboard uses to find walks passing through Three Rivers parks.
- Local time is America/Chicago (`exports.LOCAL_TZ`). Home region constants (`METRO_COUNTIES`,
  `HOME_STATE`) are in `places.py`.
- Code style: small modules of plain functions, `pathlib`, atomic writes (`.part` then replace),
  short docstrings in plain English. User-facing messages and README prose are written for a
  non-programmer; keep that tone.
