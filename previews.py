"""Share pages for single walks, so a link to one walk shows that walk in social media previews.

Link previews only read a page's og: tags, never run its JavaScript, and ignore everything after
"#", so docs/#walk=<id> always previews as the home page. Instead each walk gets docs/w/<id>.html:
its own title, description and a 1200x630 picture of the route on an OpenStreetMap map (beside a
grid of the walk's photos, when it has any), and a script that sends people on to the walk on the
dashboard.

Map tiles are cached under data/tile_cache/, so each is downloaded once. A walk's picture is only
redrawn when its route, text or photos change (data/previews.json remembers what each was drawn
from). Everything is read from the published site data in docs/data/, so both `process` and
`photos` can bring the pages up to date.
"""

import hashlib
import html
import json
import math
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

import exports
import places

STYLE_VERSION = 1   # bump to redraw every picture after changing how they look
WIDTH, HEIGHT = 1200, 630
PANEL_H = 150       # text panel along the bottom; the route is fitted into the map above it
PAD = 60
PHOTOS_W = 480      # photo grid at the right of walks with photos; the map gets the rest
GAP = 4
MAX_ZOOM = 16       # short walks aren't zoomed in further than this
LINE_SCALE = 3      # the route is drawn this many times larger, then shrunk, for smooth edges
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
ROUTE_COLOR = "#eb6834"
TITLE_COLOR = "#2a78d6"
INK, MUTED = "#1f2328", "#57606a"
FONT_DIR = Path("C:/Windows/Fonts")
FONTS = {"bold": ["segoeuib.ttf", "DejaVuSans-Bold.ttf"], "semi": ["seguisb.ttf", "DejaVuSans-Bold.ttf"],
         "regular": ["segoeui.ttf", "DejaVuSans.ttf"]}


def font(weight, size):
    for name in FONTS[weight]:
        for path in (FONT_DIR / name, Path(name)):
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                pass
    return ImageFont.load_default(size)


def world_xy(lat, lon):
    """Web Mercator position at zoom 0, in pixels of a 256-pixel world."""
    s = math.sin(math.radians(lat))
    return (lon + 180) / 360 * 256, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * 256


class Tiles:
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = places.USER_AGENT
        self.fetched = 0

    def get(self, z, x, y):
        path = self.cache_dir / str(z) / str(x) / f"{y}.png"
        if not path.exists():
            for attempt in range(3):
                try:
                    resp = self.session.get(TILE_URL.format(z=z, x=x, y=y), timeout=30)
                    resp.raise_for_status()
                    break
                except requests.RequestException:
                    if attempt == 2:
                        raise
                    time.sleep(2 * (attempt + 1))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
            self.fetched += 1
        return Image.open(path).convert("RGB")


def draw_map(segments, tiles, map_w=WIDTH):
    """The route on map tiles, fitted into the left map_w pixels of the area above the text panel."""
    pts = [[world_xy(seg[i], seg[i + 1]) for i in range(0, len(seg), 3)] for seg in segments]
    xs = [p[0] for line in pts for p in line]
    ys = [p[1] for line in pts for p in line]
    span_x, span_y = max(max(xs) - min(xs), 1e-9), max(max(ys) - min(ys), 1e-9)
    map_h = HEIGHT - PANEL_H
    zf = min(math.log2(min((map_w - 2 * PAD) / span_x, (map_h - 2 * PAD) / span_y)), MAX_ZOOM)
    z = math.ceil(zf)
    k = 2 ** (z - zf)  # tiles at zoom z are drawn k times too big, then shrunk to fit
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    ox, oy = cx * 2 ** z - map_w / 2 * k, cy * 2 ** z - map_h / 2 * k
    big_w, big_h = math.ceil(WIDTH * k), math.ceil(HEIGHT * k)
    big = Image.new("RGB", (big_w, big_h), "#e8e4dc")
    n = 2 ** z
    for ty in range(math.floor(oy / 256), math.floor((oy + big_h) / 256) + 1):
        if not 0 <= ty < n:
            continue
        for tx in range(math.floor(ox / 256), math.floor((ox + big_w) / 256) + 1):
            big.paste(tiles.get(z, tx % n, ty), (round(tx * 256 - ox), round(ty * 256 - oy)))
    img = big.resize((WIDTH, HEIGHT), Image.LANCZOS)

    # The route, drawn large on its own layer and shrunk, so its edges are smooth.
    scale = 2 ** zf * LINE_SCALE
    to_px = lambda p: ((p[0] - cx) * scale + map_w / 2 * LINE_SCALE, (p[1] - cy) * scale + map_h / 2 * LINE_SCALE)
    layer = Image.new("RGBA", (WIDTH * LINE_SCALE, HEIGHT * LINE_SCALE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    lines = [[to_px(p) for p in line] for line in pts]
    for color, width in (("white", 11), (ROUTE_COLOR, 6)):
        for line in lines:
            if len(line) > 1:
                d.line(line, fill=color, width=width * LINE_SCALE, joint="curve")
    for (x, y), fill in ((lines[-1][-1], "#24292f"), (lines[0][0], "#2f9e44")):
        r = 9 * LINE_SCALE
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill, outline="white", width=3 * LINE_SCALE)
    layer = layer.resize((WIDTH, HEIGHT), Image.LANCZOS)
    img.paste(layer, (0, 0), layer)
    return img


# Photo grid layouts by photo count: (columns, rows) and each photo's (column, row, columns wide, rows tall).
GRIDS = {
    1: ((1, 1), [(0, 0, 1, 1)]),
    2: ((1, 2), [(0, 0, 1, 1), (0, 1, 1, 1)]),
    3: ((2, 2), [(0, 0, 2, 1), (0, 1, 1, 1), (1, 1, 1, 1)]),
    4: ((2, 2), [(0, 0, 1, 1), (1, 0, 1, 1), (0, 1, 1, 1), (1, 1, 1, 1)]),
    6: ((2, 3), [(c, r, 1, 1) for r in range(3) for c in range(2)]),
    9: ((3, 3), [(c, r, 1, 1) for r in range(3) for c in range(3)]),
}


def pick_photos(photos):
    """As many photos as the largest layout that they fill, spread evenly along the walk."""
    size = max(n for n in GRIDS if n <= len(photos))
    step = len(photos) / size
    return [photos[int(i * step)] for i in range(size)]


def draw_photos(img, paths, more):
    """The photos as a contact sheet at the right of the map, the last one marked +N if some aren't shown."""
    left, map_h = WIDTH - PHOTOS_W, HEIGHT - PANEL_H
    ImageDraw.Draw(img).rectangle((left - GAP, 0, WIDTH, map_h), fill="white")
    (cols, rows), cells = GRIDS[len(paths)]
    cw, ch = (PHOTOS_W - GAP * (cols - 1)) / cols, (map_h - GAP * (rows - 1)) / rows
    for path, (c, r, wide, tall) in zip(paths, cells):
        x, y = round(left + c * (cw + GAP)), round(r * (ch + GAP))
        w, h = round(cw * wide + GAP * (wide - 1)), round(ch * tall + GAP * (tall - 1))
        with Image.open(path) as photo:
            img.paste(ImageOps.fit(photo.convert("RGB"), (w, h), Image.LANCZOS), (x, y))
    if more:
        shade = Image.new("RGBA", (w, h), (0, 0, 0, 120))
        img.paste(shade, (x, y), shade)
        ImageDraw.Draw(img).text((x + w / 2, y + h / 2), f"+{more}", font=font("bold", 44), fill="white", anchor="mm")


def fit_text(draw, text, weight, size, max_w, min_size=24):
    """The largest font (down to min_size) that fits text into max_w, shortening the text if even that doesn't."""
    while size > min_size and draw.textlength(text, font=font(weight, size)) > max_w:
        size -= 2
    f = font(weight, size)
    while draw.textlength(text, font=f) > max_w and len(text) > 1:
        text = text[:-2].rstrip() + "…"
    return text, f


def draw_panel(img, title, subtitle, site_title):
    panel = Image.new("RGBA", (WIDTH, PANEL_H), (255, 255, 255, 240))
    img.paste(panel, (0, HEIGHT - PANEL_H), panel)
    d = ImageDraw.Draw(img)
    top = HEIGHT - PANEL_H
    site_f, credit_f = font("semi", 24), font("regular", 16)
    side_w = d.textlength(site_title, font=site_f)
    d.text((WIDTH - PAD // 2, top + 34), site_title, font=site_f, fill=TITLE_COLOR, anchor="rt")
    d.text((WIDTH - PAD // 2, HEIGHT - 20), "© OpenStreetMap contributors", font=credit_f, fill=MUTED, anchor="rb")
    text_w = WIDTH - PAD // 2 - side_w - PAD
    title, title_f = fit_text(d, title, "bold", 46, text_w)
    d.text((PAD // 2 + 4, top + 26), title, font=title_f, fill=INK)
    subtitle, sub_f = fit_text(d, subtitle, "regular", 28, WIDTH - PAD - 220, min_size=20)
    d.text((PAD // 2 + 4, top + 92), subtitle, font=sub_f, fill=MUTED)


def read_js(path):
    """The value a docs/data/*.js file assigns (window.NAME = {...};)."""
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(text[text.index("=") + 1:].strip().rstrip(";"))


def walk_text(w):
    place = w["place"] or w["city"] or "Unknown"
    title = f"{place} · {w['detail']}" if w["detail"] else place
    day = date.fromisoformat(w["date"])
    kind = "walk" if w["activity"] == "Walk" else "ride"
    stats = [f"{w['mi']:.2f} mi {kind}", exports.format_duration(w["dur"]), exports.format_pace(w["pace"]),
             f"{w['ft']:,} ft climbing" if w["ft"] else ""]
    return {
        "place": place, "title": title,
        "long_date": f"{day:%A, %B} {day.day}, {day.year}", "short_date": f"{day:%b} {day.day}, {day.year}",
        "stats": " · ".join(s for s in stats if s),
    }


def site_info(index_html):
    """The site's address and name, from the home page's own og: tags."""
    text = Path(index_html).read_text(encoding="utf-8")
    url = re.search(r'<meta property="og:url" content="([^"]+)"', text).group(1)
    name = re.search(r'<meta property="og:title" content="([^"]+)"', text).group(1)
    return url.rstrip("/") + "/", html.unescape(name)


def share_page(w, t, base_url, site_title, has_image, photo_count):
    target = f"../#place={quote(t['place'], safe='')}&walk={w['id']}"
    page_url = f"{base_url}w/{w['id']}.html"
    image = f"{base_url}w/{w['id']}.jpg" if has_image else f"{base_url}images/share.jpg"
    title = f"{t['title']} · {t['short_date']}"
    photos = f" · {photo_count} photo{'s' if photo_count != 1 else ''}" if photo_count else ""
    description = f"{t['stats']}{photos}. {t['long_date']}, on {site_title}."
    alt = f"Map of the route at {t['title']}{' and photos from the walk' if photo_count else ''}" if has_image else site_title
    e = lambda s: html.escape(s, quote=True)
    # No <meta http-equiv="refresh">: Facebook follows those and would preview the home page instead.
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)} · {e(site_title)}</title>
  <meta name="description" content="{e(description)}">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="{e(site_title)}">
  <meta property="og:url" content="{e(page_url)}">
  <meta property="og:title" content="{e(title)}">
  <meta property="og:description" content="{e(description)}">
  <meta property="og:image" content="{e(image)}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:image:alt" content="{e(alt)}">
  <meta name="twitter:card" content="summary_large_image">
  <script>location.replace({json.dumps(target)});</script>
</head>
<body>
  <p><a href="{e(target)}">{e(title)}</a></p>
</body>
</html>
"""


def update_previews(site_dir, data_dir):
    site_dir = Path(site_dir)
    workouts = read_js(site_dir / "data" / "workouts.js")["workouts"]
    routes = read_js(site_dir / "data" / "routes.js")
    albums = read_js(site_dir / "data" / "photos.js") if (site_dir / "data" / "photos.js").exists() else {}
    out_dir = site_dir / "w"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(data_dir) / "previews.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    base_url, site_title = site_info(Path(site_dir) / "index.html")
    tiles = Tiles(Path(data_dir) / "tile_cache")

    texts = {w["id"]: walk_text(w) for w in workouts}
    shown = {w["id"]: pick_photos(albums[w["id"]]) for w in workouts if albums.get(w["id"])}
    prints = {w["id"]: fingerprint(texts[w["id"]], routes[w["id"]], site_title, [p["f"] for p in shown.get(w["id"], [])])
              for w in workouts if w["id"] in routes}
    stale = [w for w in workouts if w["id"] in prints and (
        manifest.get(w["id"]) != prints[w["id"]] or not (out_dir / f"{w['id']}.jpg").exists())]
    drawn = failed = 0
    started = time.time()
    for n, w in enumerate(stale, 1):
        t = texts[w["id"]]
        try:
            img = draw_map(routes[w["id"]], tiles, WIDTH - PHOTOS_W - GAP if w["id"] in shown else WIDTH)
        except requests.RequestException as exc:
            failed += 1
            print(f"  preview for {w['id']} skipped: map tiles unavailable ({exc})")
            continue
        if w["id"] in shown:
            draw_photos(img, [site_dir / "photos" / w["id"] / f"{p['f']}.jpg" for p in shown[w["id"]]],
                        len(albums[w["id"]]) - len(shown[w["id"]]))
        draw_panel(img, t["title"], f"{t['long_date']} · {t['stats']}", site_title)
        tmp = out_dir / f"{w['id']}.part"
        img.save(tmp, "JPEG", quality=82, optimize=True, progressive=True)
        tmp.replace(out_dir / f"{w['id']}.jpg")
        manifest[w["id"]] = prints[w["id"]]
        drawn += 1
        if n % 25 == 0:
            manifest_path.write_text(json.dumps(manifest, indent=0), encoding="utf-8")
            print(f"  previews: {n}/{len(stale)} drawn, {tiles.fetched} map tiles downloaded, {time.time() - started:.0f}s",
                  flush=True)
    manifest_path.write_text(json.dumps(manifest, indent=0), encoding="utf-8")

    keep = set()
    for w in workouts:
        has_image = (out_dir / f"{w['id']}.jpg").exists()
        page = share_page(w, texts[w["id"]], base_url, site_title, has_image, len(albums.get(w["id"], [])))
        path = out_dir / f"{w['id']}.html"
        if not path.exists() or path.read_text(encoding="utf-8") != page:
            path.write_text(page, encoding="utf-8")
        keep |= {path.name, f"{w['id']}.jpg"}
    removed = 0
    for path in out_dir.iterdir():
        if path.name not in keep:
            path.unlink()
            removed += 1
    size = sum(p.stat().st_size for p in out_dir.iterdir())
    extra = f", {failed} skipped (no map tiles)" if failed else ""
    print(f"Share pages: {len(workouts)} walks, {drawn} pictures drawn{extra}, {tiles.fetched} map tiles downloaded, "
          f"{removed} old files removed, {size / 1e6:.1f} MB -> {out_dir}")


def fingerprint(text, segments, site_title, photo_names):
    value = [STYLE_VERSION, text, segments, site_title] + ([photo_names] if photo_names else [])
    value = json.dumps(value, separators=(",", ":"))
    return hashlib.sha1(value.encode("utf-8")).hexdigest()
