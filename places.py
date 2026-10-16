"""Work out where each walk took place (park or trail, city, county, state) from its GPS track.

- Parks: park boundaries are downloaded from OpenStreetMap (Overpass API) once per map tile the
  routes pass through, then matched to the route locally. The park containing the most points
  along a route wins. OpenStreetMap's website tag gives the park's own page.
- City, county, state and trail names: Nominatim reverse geocoding at a few points per walk.
- Everything is cached under data/places_cache/, so only new areas and points are ever fetched.

Your own names in place_overrides.csv win over OpenStreetMap: each row is a name plus a
location and radius (any walk centred within the radius gets that name), or a workout_id.

Each place also gets a web link where one can be found: place_links.csv (name,url) first,
then the park's OpenStreetMap website tag, its Wikidata official website, or Wikipedia.
"""

import csv
import json
import math
import re
import time
from collections import Counter
from pathlib import Path

import requests

import tracks

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "GoneWalking/1.0 (personal walking dashboard)"
TILE_DEG = 0.1                # park boundaries are fetched per 0.1° x 0.1° tile
PARK_SAMPLES = 25             # points along each route checked against park boundaries
MIN_PARK_SHARE = 0.25         # a park must contain at least this share of those points
NOMINATIM_SAMPLES = 3         # points per route sent to Nominatim (city, county, trail)
NOMINATIM_DELAY_S = 1.1       # Nominatim's usage policy: at most one request per second
KEY_DECIMALS = 3              # Nominatim cache grid, ~100 m
TRAIL_TYPES = {"footway", "path", "cycleway", "track", "bridleway", "pedestrian", "steps"}
OVERRIDE_RADIUS_M = 500
CLUSTER_RADIUS_M = 500

# Regions, from nearest to farthest. The metro is the Metropolitan Council's seven counties.
HOME_COUNTRY = "United States"
HOME_STATE = "Minnesota"
METRO_NAME = "Twin Cities metro"
METRO_COUNTIES = {"Anoka County", "Carver County", "Dakota County", "Hennepin County",
                  "Ramsey County", "Scott County", "Washington County"}
REGIONS = [METRO_NAME, f"Greater {HOME_STATE}", "Other U.S. states", "Outside the U.S."]


def region_of(place):
    if not place.get("country"):
        return None
    if place["country"] != HOME_COUNTRY:
        return REGIONS[3]
    if place.get("state") != HOME_STATE:
        return REGIONS[2]
    return REGIONS[0] if place.get("county") in METRO_COUNTIES else REGIONS[1]

PARK_QUERY = """[out:json][timeout:90][bbox:{s},{w},{n},{e}];
(way[leisure~"^(park|nature_reserve)$"][name]; relation[leisure~"^(park|nature_reserve)$"][name];
 way[boundary~"^(protected_area|national_park)$"][name]; relation[boundary~"^(protected_area|national_park)$"][name];);
out tags geom;"""


def distance_m(a, b):
    return tracks.haversine_m(a[0], a[1], b[0], b[1])


def route_points(feature):
    return [(c[1], c[0]) for line in feature["geometry"]["coordinates"] for c in line]


def spaced(points, n):
    if len(points) <= n:
        return list(points)
    return [points[round(i * (len(points) - 1) / (n - 1))] for i in range(n)]


def route_center(points):
    mean = (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
    return min(points, key=lambda p: distance_m(p, mean))


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, value):
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(value, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# ---------- parks: boundaries per tile, matched locally ----------

def tile_of(lat, lon):
    return f"{math.floor(lat / TILE_DEG)}_{math.floor(lon / TILE_DEG)}"


def fetch_tile(tile):
    i, j = (int(x) for x in tile.split("_"))
    query = PARK_QUERY.format(s=i * TILE_DEG, w=j * TILE_DEG, n=(i + 1) * TILE_DEG, e=(j + 1) * TILE_DEG)
    last_error = None
    for attempt in range(8):
        url = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        try:
            resp = requests.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=120)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
                return [park_from_element(e) for e in resp.json()["elements"]]
            last_error = f"HTTP {resp.status_code} from {url}"
        except (requests.RequestException, ValueError) as exc:
            last_error = f"{type(exc).__name__} from {url}"
        wait = 5 * (attempt + 1)
        print(f"    {last_error}; retrying in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"Park boundary download failed for tile {tile}: {last_error}")


def join_rings(ways):
    """Join way geometries (lists of [lat, lon]) end to end into closed rings."""
    ways = [w for w in ways if len(w) >= 2]
    rings = []
    while ways:
        ring = list(ways.pop(0))
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for k, w in enumerate(ways):
                if w[0] == ring[-1]:
                    ring += w[1:]
                elif w[-1] == ring[-1]:
                    ring += w[-2::-1]
                elif w[-1] == ring[0]:
                    ring = w[:-1] + ring
                elif w[0] == ring[0]:
                    ring = w[:0:-1] + ring
                else:
                    continue
                ways.pop(k)
                changed = True
                break
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def park_from_element(e):
    to_pts = lambda geom: [[round(g["lat"], 6), round(g["lon"], 6)] for g in geom if g]
    if e["type"] == "way":
        rings = join_rings([to_pts(e.get("geometry", []))])
    else:
        rings = join_rings([to_pts(m["geometry"]) for m in e.get("members", [])
                            if m.get("type") == "way" and m.get("geometry")])
    tags = e.get("tags", {})
    return {
        "osm": f"{e['type']}/{e['id']}",
        "name": tags.get("name"),
        "kind": tags.get("leisure") or tags.get("boundary"),
        "operator": tags.get("operator"),
        "website": tags.get("website") or tags.get("contact:website") or tags.get("url"),
        "wikidata": tags.get("wikidata"),
        "wikipedia": tags.get("wikipedia"),
        "rings": rings,
    }


def ring_area_km2(ring):
    lat0 = math.radians(ring[0][0])
    pts = [(p[1] * 111.32 * math.cos(lat0), p[0] * 110.57) for p in ring]
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2


def contains(rings, lat, lon):
    """Even-odd rule across all rings, so holes (inner rings) are excluded."""
    inside = False
    for ring in rings:
        for (y1, x1), (y2, x2) in zip(ring, ring[1:]):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


class ParkIndex:
    def __init__(self, parks_by_tile):
        self.parks = {}
        for parks in parks_by_tile.values():
            for p in parks:
                if p["rings"] and p["osm"] not in self.parks:
                    lats = [pt[0] for r in p["rings"] for pt in r]
                    lons = [pt[1] for r in p["rings"] for pt in r]
                    p["bbox"] = (min(lats), min(lons), max(lats), max(lons))
                    p["area_km2"] = sum(ring_area_km2(r) for r in p["rings"])
                    self.parks[p["osm"]] = p

    def smallest_at(self, lat, lon):
        best = None
        for p in self.parks.values():
            s, w, n, e = p["bbox"]
            if s <= lat <= n and w <= lon <= e and contains(p["rings"], lat, lon):
                if best is None or p["area_km2"] < best["area_km2"]:
                    best = p
        return best


def load_parks(walk_points, cache_dir, fetch_missing):
    tiles = sorted({tile_of(lat, lon) for pts in walk_points.values() for lat, lon in pts})
    parks_by_tile, missing = {}, []
    for t in tiles:
        path = cache_dir / f"parks_{t}.json"
        if path.exists():
            parks_by_tile[t] = load_json(path, [])
        else:
            missing.append(t)
    if fetch_missing and missing:
        print(f"Places: downloading park boundaries for {len(missing)} map tiles")
        failed = 0
        for k, t in enumerate(missing, 1):
            try:
                parks_by_tile[t] = fetch_tile(t)
            except RuntimeError as exc:
                # Leave it uncached so the next run tries again; carry on with the rest.
                failed += 1
                print(f"  [{k}/{len(missing)}] tile {t}: skipped ({exc})")
                continue
            save_json(cache_dir / f"parks_{t}.json", parks_by_tile[t])
            print(f"  [{k}/{len(missing)}] tile {t}: {len(parks_by_tile[t])} parks")
            time.sleep(1)
        if failed:
            print(f"Places: {failed} tiles couldn't be downloaded (servers busy); run places again later to fill them in")
    return ParkIndex(parks_by_tile)


# ---------- Nominatim: city, county, state, trail ----------

def point_key(lat, lon):
    return f"{lat:.{KEY_DECIMALS}f},{lon:.{KEY_DECIMALS}f}"


def nominatim_lookup(keys, cache_path, fetch_missing):
    cache = load_json(cache_path, {})
    missing = sorted(set(keys) - cache.keys())
    if fetch_missing and missing:
        minutes = len(missing) * NOMINATIM_DELAY_S / 60
        print(f"Places: looking up {len(missing)} points with Nominatim (about {minutes:.0f} min)")
        for k, key in enumerate(missing, 1):
            lat, lon = key.split(",")
            for attempt in range(4):
                try:
                    resp = requests.get(NOMINATIM_URL, params={
                        "format": "jsonv2", "lat": lat, "lon": lon, "zoom": 17, "addressdetails": 1, "extratags": 1,
                    }, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=30)
                    resp.raise_for_status()
                    d = resp.json()
                    break
                except (requests.RequestException, ValueError) as exc:
                    print(f"    {type(exc).__name__}; retrying")
                    time.sleep(10 * (attempt + 1))
            else:
                raise RuntimeError("Nominatim lookups keep failing; try again later")
            a = d.get("address", {})
            cache[key] = {
                "city": a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("municipality"),
                "county": a.get("county"),
                "state": a.get("state"),
                "country": a.get("country"),
                "trail": d.get("name") if d.get("category") == "highway" and d.get("type") in TRAIL_TYPES else None,
                "trail_website": (d.get("extratags") or {}).get("website"),
            }
            if k % 25 == 0 or k == len(missing):
                save_json(cache_path, cache)
                print(f"  {k}/{len(missing)}")
            time.sleep(NOMINATIM_DELAY_S)
    return cache


# ---------- overrides and links ----------

def load_overrides(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("name") or "").strip()]
    for r in rows:
        r["name"] = r["name"].strip()
        r["kind"] = (r.get("kind") or "").strip().lower() or guess_kind(r["name"])
        r["radius_m"] = float(r.get("radius_m") or OVERRIDE_RADIUS_M)
        r["point"] = (float(r["lat"]), float(r["lon"])) if r.get("lat") and r.get("lon") else None
    return rows


def guess_kind(name):
    """Your names are parks unless they sound like a route."""
    return "trail" if re.search(r"\b(Trail|Trl|Path|Bridge|Bikeway|Singletrack)\b", name, re.I) else "park"


def match_overrides(workout_id, center, overrides):
    """Your park name and trail name for a walk: a workout_id row wins, else the nearest row in range."""
    found = {}
    for kind in ("park", "trail"):
        rows = [r for r in overrides if r["kind"] == kind]
        exact = next((r["name"] for r in rows if (r.get("workout_id") or "").strip() == workout_id), None)
        if exact:
            found[kind] = exact
            continue
        best = None
        for r in rows:
            if r["point"] and not (r.get("workout_id") or "").strip():
                d = distance_m(center, r["point"])
                if d <= r["radius_m"] and (best is None or d < best[0]):
                    best = (d, r["name"])
        if best:
            found[kind] = best[1]
    return found


def load_aliases(path):
    """OpenStreetMap name -> the name you prefer, applied wherever that name turns up."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {r["osm_name"].strip(): r["name"].strip() for r in csv.DictReader(f)
                if (r.get("osm_name") or "").strip() and (r.get("name") or "").strip()}


def load_links(path):
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {r["name"].strip(): r["url"].strip() for r in csv.DictReader(f)
                if (r.get("name") or "").strip() and (r.get("url") or "").strip()}


def wikidata_websites(qids, cache_path, fetch_missing):
    """Official website (P856) for each Wikidata ID, cached."""
    cache = load_json(cache_path, {})
    missing = sorted(set(qids) - cache.keys())
    if fetch_missing and missing:
        for i in range(0, len(missing), 50):
            batch = missing[i:i + 50]
            resp = requests.get("https://www.wikidata.org/w/api.php", params={
                "action": "wbgetentities", "ids": "|".join(batch), "props": "claims", "format": "json",
            }, headers={"User-Agent": USER_AGENT}, timeout=60)
            resp.raise_for_status()
            entities = resp.json().get("entities", {})
            for qid in batch:
                claims = entities.get(qid, {}).get("claims", {}).get("P856", [])
                values = [c["mainsnak"].get("datavalue", {}).get("value") for c in claims]
                cache[qid] = next((v for v in values if v), None)
        save_json(cache_path, cache)
    return {q: cache.get(q) for q in qids}


def wikipedia_url(tag):
    if not tag or ":" not in tag:
        return None
    lang, title = tag.split(":", 1)
    return f"https://{lang}.wikipedia.org/wiki/{title.strip().replace(' ', '_')}"


def add_links(places, cache_dir, links_path, fetch_missing):
    """Set place_url: your link > OSM website > Wikidata official site > Wikipedia article."""
    manual = load_links(Path(links_path))
    qids = {p["park_wikidata"] for p in places.values() if p.get("park_wikidata") and not p.get("park_website")}
    sites = wikidata_websites(qids, cache_dir / "wikidata.json", fetch_missing)
    for p in places.values():
        own = p["place"] is not None and p["place"] == p.get("park")
        trail = p["place"] is not None and p["place"] == p.get("trail")
        candidates = [
            (manual.get(p["place"]) if p["place"] else None, "manual"),
            (p.get("park_website") if own else None, "osm"),
            (sites.get(p.get("park_wikidata")) if own else None, "wikidata"),
            (wikipedia_url(p.get("park_wikipedia")) if own else None, "wikipedia"),
            (p.get("trail_website") if trail else None, "osm"),
        ]
        url, source = next(((u, s) for u, s in candidates if u), (None, None))
        if url and not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        p["place_url"], p["place_url_source"] = url, source


# ---------- putting it together ----------

def majority(values):
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0] if counts else (None, 0)


def update_places(tracks_dir, data_dir, overrides_path, fetch_missing=True, links_path=None):
    """Resolve every walk's place; with fetch_missing, download whatever isn't cached yet."""
    data_dir = Path(data_dir)
    cache_dir = data_dir / "places_cache"
    cache_dir.mkdir(exist_ok=True)

    walks = {}
    for path in sorted(Path(tracks_dir).glob("*.geojson")):
        with path.open(encoding="utf-8") as f:
            feature = json.load(f)
        pts = route_points(feature)
        walks[feature["properties"]["id"]] = (feature["properties"], pts)

    park_index = load_parks({k: spaced(p, PARK_SAMPLES) for k, (_, p) in walks.items()}, cache_dir, fetch_missing)
    nominatim_keys = {k: [point_key(*pt) for pt in spaced(p, NOMINATIM_SAMPLES + 2)[1:-1]] for k, (_, p) in walks.items()}
    geo = nominatim_lookup({key for keys in nominatim_keys.values() for key in keys},
                           cache_dir / "nominatim.json", fetch_missing)

    overrides = load_overrides(Path(overrides_path))
    aliases = load_aliases(Path(overrides_path).with_name("place_aliases.csv"))
    places = {}
    for workout_id, (props, pts) in walks.items():
        samples = spaced(pts, PARK_SAMPLES)
        hits = [park_index.smallest_at(lat, lon) for lat, lon in samples]
        (park_osm, count) = majority(h["osm"] for h in hits if h)
        park = park_index.parks[park_osm] if park_osm and count >= MIN_PARK_SHARE * len(samples) else None

        found = [geo[k] for k in nominatim_keys[workout_id] if k in geo]
        place = {field: majority(f[field] for f in found)[0] for field in ("city", "county", "state", "country")}
        # OpenStreetMap separates multiple names with ";".
        trail, trail_count = majority((f["trail"] or "").replace(";", " / ") or None for f in found)
        place["trail"] = trail if trail_count >= 2 else None
        place["trail_website"] = next((f["trail_website"] for f in found if f["trail"] == place["trail"] and f["trail_website"]), None)
        place.update({
            "park": park["name"] if park else None,
            "park_kind": park["kind"] if park else None,
            "park_operator": park["operator"] if park else None,
            "park_osm": park["osm"] if park else None,
            "park_website": park["website"] if park else None,
            "park_wikidata": park["wikidata"] if park else None,
            "park_wikipedia": park["wikipedia"] if park else None,
            "park_share": round(count / len(samples), 2) if park else None,
        })

        center = route_center(pts)
        # The place is the park (yours, else OpenStreetMap's); the trail within it is a detail.
        manual = match_overrides(workout_id, center, overrides)
        park_name = manual.get("park") or aliases.get(place["park"], place["park"])
        trail_name = manual.get("trail") or aliases.get(place["trail"], place["trail"])
        place["place"] = park_name or trail_name
        place["place_detail"] = trail_name if trail_name and trail_name != place["place"] else None
        from_manual = manual.get("park") if park_name else manual.get("trail")
        place["place_source"] = "manual" if from_manual else "park" if place["park"] and park_name else "trail" if place["place"] else None
        place["region"] = region_of(place)
        place["center"] = [round(center[0], 5), round(center[1], 5)]
        place["date"] = props["date"]
        place["activity"] = props["activity"]
        places[workout_id] = place

    if links_path:
        add_links(places, cache_dir, links_path, fetch_missing)

    out = data_dir / "places.json"
    tmp = out.with_suffix(".part")
    tmp.write_text(json.dumps(places, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(out)
    named = sum(1 for p in places.values() if p["place"])
    linked = sum(1 for p in places.values() if p.get("place_url"))
    print(f"Places: {named} of {len(places)} workouts named, {linked} with a web link -> {out}")
    return places


def unknown_clusters(places):
    """Group walks without a place name by location, so each spot is named once."""
    clusters = []
    for workout_id, p in sorted(places.items(), key=lambda kv: kv[1]["date"]):
        if p["place"]:
            continue
        center = tuple(p["center"])
        for c in clusters:
            if distance_m(center, c["center"]) <= CLUSTER_RADIUS_M:
                c["walks"].append((workout_id, p))
                break
        else:
            clusters.append({"center": center, "walks": [(workout_id, p)]})
    clusters.sort(key=lambda c: -len(c["walks"]))
    return clusters
