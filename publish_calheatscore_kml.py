#!/usr/bin/env python3
"""
Publish daily CalHeatScore KML files to a web directory, modeled on the NWS
HeatRisk data page: stable "latest" URLs plus a dated archive.

Each run pulls the live layer from the public CalHeatScore API and writes:

  <web-root>/CalHeatScore_Entire_Fcst.kml   small wrapper of auto-updating links to the 7 Day files
                                            (same approach as HeatRisk_Entire_Fcst.kml)
  <web-root>/CalHeatScore_Day1_Fcst.kmz     Day 1 = today (CHS_Day_0), colored by score
  ...
  <web-root>/CalHeatScore_Day7_Fcst.kmz     Day 7 (CHS_Day_6), colored by score
  <web-root>/CalHeatScore_Day1_Fcst.png ... Day7   ready-made 1920x1080 map image per day
  <web-root>/CalHeatScore_7Day_Fcst.geojson        all 7 days' scores as attributes, one feature per ZIP
  <web-root>/CalHeatScore_7Day_Fcst_shp.zip        the same as a zipped shapefile
  <web-root>/CalHeatScore_latest.json       issue/valid dates, so the web page can show them
  <web-root>/index.html                     landing page with links, dates and the score scale
  <web-root>/README.txt                     file and field descriptions
  <web-root>/archive/YYYYMMDD/...           same files, kept per issue date (skip with --no-archive)
  <web-root>/archive/index.html             listing of archived dates

Usage:
  python publish_calheatscore_kml.py \
      --web-root /var/www/html/sites/default/files/calheatscore_kml \
      --base-url https://calheatscore.calepa.ca.gov/sites/default/files/calheatscore_kml/
  python publish_calheatscore_kml.py --web-root ./site/data --format kml --keep-days 365
  python publish_calheatscore_kml.py --web-root ./preview --page-only   # landing page only, for review

Dependencies (Python 3.9+): requests; matplotlib for the PNG maps; pyshp for the
shapefile. If matplotlib or pyshp is missing, that product is skipped with a
warning and everything else is still published.
"""

import argparse
import json
import math
import logging
import os
import shutil
import sys
import time
import zipfile
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import requests

QUERY_URL = (
    "https://services1.arcgis.com/PCHfdHz4GlDNAhBb/arcgis/rest/services/"
    "CalHeatScore_Live_Data_for_API_Use/FeatureServer/0/query"
)
PAGE_SIZE = 1000
NUM_DAYS = 7
MIN_EXPECTED_FEATURES = 1500   # statewide layer is ~1,700 ZIPs; blocks partial pulls
MAX_MISSING_FRACTION = 0.05    # fail if >5% of ZIPs have no Day 1 score
TZ = ZoneInfo("America/Los_Angeles")
PREFIX = "CalHeatScore"
ENTIRE_NAME = f"{PREFIX}_Entire_Fcst.kml"  # always plain KML: it only holds links

# Official CalHeatScore scale (score -> label, hex color)
SCALE = {
    0: ("Low", "#DEDEDE"),
    1: ("Mild", "#FAE0C8"),
    2: ("Moderate", "#EA8753"),
    3: ("High", "#C23A00"),
    4: ("Severe", "#700006"),
}
NO_DATA_COLOR = "#FFFFFF"


# ---------------------------------------------------------------- fetching

def fetch_page(session, params, retries=4, url=QUERY_URL):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=180)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            if attempt == retries - 1:
                raise
            wait = 5 * 2 ** attempt
            logging.warning("Request failed (%s); retrying in %ss", exc, wait)
            time.sleep(wait)


def object_id_field(session):
    """The GeoPortal export is in object-ID order; look up that field's name."""
    try:
        info = fetch_page(session, {"f": "json"}, url=QUERY_URL.rsplit("/", 1)[0])
        return info.get("objectIdField") or "OBJECTID"
    except Exception as exc:  # fall back rather than fail the daily run
        logging.warning("Could not read layer info (%s); ordering by OBJECTID", exc)
        return "OBJECTID"


def fetch_features(simplify=0.0):
    session = requests.Session()
    order_field = object_id_field(session)
    features, offset = [], 0
    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": 4326,
            "orderByFields": order_field,  # matches GeoPortal order; stable for paging
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "f": "geojson",
        }
        if simplify:
            params["maxAllowableOffset"] = simplify  # degrees; ~0.0002 is ~20 m
        data = fetch_page(session, params)
        page = data.get("features", [])
        features.extend(page)
        more = data.get("exceededTransferLimit") or data.get("properties", {}).get(
            "exceededTransferLimit"
        )
        if not page or not more:
            return features
        offset += len(page)


def parse_score(value):
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return None
    return score if score in SCALE else None


def parse_date(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):  # esri date field: epoch milliseconds
        return datetime.fromtimestamp(value / 1000, TZ).date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def issue_date_of(features):
    dates = Counter(parse_date((f.get("properties") or {}).get("DATE")) for f in features)
    dates.pop(None, None)
    return dates.most_common(1)[0][0] if dates else None


# ---------------------------------------------------------------- KML
#
# Placemark markup mirrors the official GeoPortal (ArcGIS Hub) KMZ export:
# one Document/Folder named after the layer, one Placemark per ZIP with
# id ID_00000..., the ZIP as <name>, the full attribute table as HTML in
# <description>, and clampToGround polygons.

NL = "\r\n"
LAYER_NAME = "California_Zip_Codes_7_Day_CHS_Data"
OFFICIAL_FIELDS = ["ZIP_CODE", "DATE", *[f"CHS_Day_{d}" for d in range(NUM_DAYS)],
                   "GlobalID", "Shape__Area", "Shape__Length"]
KML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>' + NL
    + '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' + NL
    + ' xsi:schemaLocation="http://www.opengis.net/kml/2.2 http://schemas.opengis.net/kml/2.2.0/ogckml22.xsd '
    'http://www.google.com/kml/ext/2.2 http://code.google.com/apis/kml/schema/kml22gx.xsd">' + NL
)


# Machine-readable fields on every Day-file placemark (KML ExtendedData), so GIS
# and broadcast graphics software can style and query by score. CHS is always
# "this file's day", so one template works for every Day file.
SCHEMA_ID = "CalHeatScoreSchema"
SCHEMA_FIELDS = [("ZIP_CODE", "string"), ("ISSUE_DATE", "string"), ("VALID_DATE", "string"),
                 ("FCST_DAY", "int"), ("CHS", "int"), ("CHS_LABEL", "string"),
                 *[(f"CHS_Day_{d}", "int") for d in range(NUM_DAYS)]]


def schema_kml():
    fields = "".join(f'    <SimpleField name="{n}" type="{t}"></SimpleField>{NL}' for n, t in SCHEMA_FIELDS)
    return f'  <Schema name="CalHeatScore" id="{SCHEMA_ID}">{NL}{fields}  </Schema>{NL}'


def extended_data_kml(props, day_idx, issue):
    score = parse_score(props.get(f"CHS_Day_{day_idx}"))
    values = {
        "ZIP_CODE": props.get("ZIP_CODE"),
        "ISSUE_DATE": issue.isoformat(),
        "VALID_DATE": (issue + timedelta(days=day_idx)).isoformat(),
        "FCST_DAY": day_idx + 1,
        "CHS": score,
        "CHS_LABEL": SCALE[score][0] if score is not None else "No data",
        **{f"CHS_Day_{d}": parse_score(props.get(f"CHS_Day_{d}")) for d in range(NUM_DAYS)},
    }
    data = "".join(f'<SimpleData name="{n}">{escape(str(values[n]))}</SimpleData>'
                   for n, _ in SCHEMA_FIELDS if values[n] is not None)
    return (f'      <ExtendedData><SchemaData schemaUrl="#{SCHEMA_ID}">{data}'
            f"</SchemaData></ExtendedData>{NL}")


def kml_color(hex_rgb, alpha="ff"):
    """#RRGGBB -> KML aabbggrr."""
    h = hex_rgb.lstrip("#")
    return f"{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".lower()


def fmt_value(field, value):
    if value is None:
        return ""
    if field in ("Shape__Area", "Shape__Length"):
        return f"{float(value):.6f}".rstrip("0").rstrip(".")
    return str(value)


def description_html(props):
    rows = "".join(f"<tr><td>{f}</td><td>{fmt_value(f, props.get(f))}</td></tr>" for f in OFFICIAL_FIELDS)
    return ('<html><body><table border="1"><tr><th>Field Name</th><th>Field Value</th></tr>'
            f"{rows}</table></body></html>")


def coords_kml(ring):
    return " " + " ".join(f"{float(pt[0])!r},{float(pt[1])!r},0" for pt in ring)


def polygon_kml(rings):
    outer, *inners = rings
    ind = " " * 8
    out = [f"{ind}<Polygon>{NL}",
           f"{ind}  <extrude>0</extrude><altitudeMode>clampToGround</altitudeMode>{NL}",
           f"{ind}  <outerBoundaryIs><LinearRing><coordinates>{coords_kml(outer)}"
           f"</coordinates></LinearRing></outerBoundaryIs>{NL}"]
    for inner in inners:
        out.append(f"{ind}  <innerBoundaryIs><LinearRing><coordinates>{coords_kml(inner)}"
                   f"</coordinates></LinearRing></innerBoundaryIs>{NL}")
    out.append(f"{ind}</Polygon>{NL}")
    return "".join(out)


def geometry_to_kml(geom):
    if not geom:
        return ""
    if geom["type"] == "Polygon":
        return polygon_kml(geom["coordinates"])
    if geom["type"] == "MultiPolygon":
        if len(geom["coordinates"]) == 1:
            return polygon_kml(geom["coordinates"][0])
        polys = "".join(polygon_kml(p) for p in geom["coordinates"])
        return f"      <MultiGeometry>{NL}{polys}      </MultiGeometry>{NL}"
    raise ValueError(f"Unsupported geometry type: {geom['type']}")


def style_kml(style_id, fill):
    return (
        f'  <Style id="{style_id}">{NL}'
        f"    <LabelStyle>{NL}      <color>00000000</color>{NL}      <scale>0.000000</scale>{NL}    </LabelStyle>{NL}"
        f"    <LineStyle>{NL}      <color>ff6e6e6e</color>{NL}      <width>0.700000</width>{NL}    </LineStyle>{NL}"
        f"    <PolyStyle>{NL}      <color>{fill}</color>{NL}      <outline>1</outline>{NL}    </PolyStyle>{NL}"
        f"  </Style>{NL}"
    )


def score_styles():
    out = [style_kml(f"CHS{k}", kml_color(rgb)) for k, (_, rgb) in SCALE.items()]
    out.append(style_kml("CHSNA", kml_color(NO_DATA_COLOR)))
    return "".join(out)


def placemark(pid, props, geom_kml, style_id, extended=""):
    return (
        f'    <Placemark id="{pid}">{NL}'
        f"      <name>{escape(str(props.get('ZIP_CODE', '')))}</name>{NL}"
        f"      <snippet></snippet>{NL}"
        f"      <description><![CDATA[{description_html(props)}]]></description>{NL}"
        f"      <styleUrl>#{style_id}</styleUrl>{NL}"
        f"{extended}{geom_kml}    </Placemark>{NL}"
    )


def day_document(doc_name, placemarks, expires):
    """A Day file: same structure as the GeoPortal export, plus score styles and
    a Schema for the placemarks' ExtendedData fields.

    The NetworkLinkControl <expires> tells Google Earth (when the file is loaded
    through the Entire file's links) not to re-download it until the next update.
    """
    return (
        f"{KML_HEADER}<NetworkLinkControl><expires>{expires.isoformat()}</expires></NetworkLinkControl>{NL}"
        f'<Document id="{LAYER_NAME}">{NL}'
        f"  <name>{escape(doc_name)}</name>{NL}  <snippet></snippet>{NL}{schema_kml()}"
        f'  <Folder id="FeatureLayer0">{NL}    <name>{LAYER_NAME}</name>{NL}    <snippet></snippet>{NL}'
        f"{placemarks}  </Folder>{NL}{score_styles()}</Document>{NL}</kml>"
    )


def parse_refresh_times(text):
    """'05:45,08:45' -> sorted list of datetime.time (Pacific)."""
    times = []
    for part in text.split(","):
        hh, mm = part.strip().split(":")
        times.append(dtime(int(hh), int(mm)))
    return sorted(times)


def next_refresh(now, times):
    """First scheduled refresh time strictly after `now` (both in Pacific time)."""
    for days_ahead in (0, 1):
        day = now.date() + timedelta(days=days_ahead)
        for t in times:
            candidate = datetime.combine(day, t, tzinfo=TZ)
            if candidate > now:
                return candidate
    raise ValueError("no refresh times given")


def refresh_text(times):
    fmt = lambda t: datetime.combine(date.today(), t).strftime("%-I:%M %p")
    when = " and ".join(fmt(t) for t in times)
    return f"Auto-Updates {'Twice Daily' if len(times) == 2 else 'Daily'} ({when} Pacific)" \
        if len(times) <= 2 else f"Auto-Updates Daily ({', '.join(fmt(t) for t in times)} Pacific)"


def entire_document(hrefs, title, day_labels, refresh_times=None):
    """Entire file modeled on HeatRisk_Entire_Fcst.kml: a small wrapper of NetworkLinks,
    one per Day file, with Day 1 switched on. With refresh_times, links use onExpire, so
    Google Earth re-pulls each Day file when its <expires> time passes; without it
    (archive copies) the links are static."""
    T = "\t"
    links = []
    for d, (href, label) in enumerate(zip(hrefs, day_labels)):
        refresh = f"{T*4}<refreshMode>onExpire</refreshMode>{NL}" if refresh_times else ""
        desc = refresh_text(refresh_times) if refresh_times else label
        vis = f"{T*3}<open>1</open>{NL}" if d == 0 else f"{T*3}<open>0</open>{NL}{T*3}<visibility>0</visibility>{NL}"
        links.append(
            f"{T*2}<NetworkLink>{NL}{T*3}<name>CalHeatScore {d + 1} Day Forecast</name>{NL}{vis}"
            f"{T*3}<description>{escape(desc)}</description>{NL}"
            f"{T*3}<Link>{NL}{T*4}<href>{escape(href)}</href>{NL}{refresh}{T*3}</Link>{NL}"
            f"{T*2}</NetworkLink>{NL}"
        )
    return (f'<?xml version="1.0" encoding="UTF-8"?>{NL}<kml xmlns="http://www.opengis.net/kml/2.2">{NL}'
            f"{T}<Folder>{NL}{T*2}<name>{escape(title)}</name>{NL}{T*2}<open>1</open>{NL}"
            f"{''.join(links)}{T}</Folder>{NL}</kml>{NL}")


@contextmanager
def open_product(path, fmt):
    """Binary stream for a .kml file, or for doc.kml inside a .kmz."""
    if fmt == "kmz":
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Standard (non-ZIP64) entry: Google Earth can't read ZIP64 headers.
            with zf.open("doc.kml", "w") as fh:
                yield fh
    else:
        with open(path, "wb") as fh:
            yield fh


# ---------------------------------------------------------------- GeoJSON / shapefile / PNG

GEOJSON_NAME = f"{PREFIX}_7Day_Fcst.geojson"
SHAPEFILE_NAME = f"{PREFIX}_7Day_Fcst_shp.zip"
WGS84_PRJ = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
             'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')


def gis_properties(props):
    return {
        "ZIP_CODE": None if props.get("ZIP_CODE") is None else str(props.get("ZIP_CODE")),
        "DATE": None if props.get("DATE") is None else str(props.get("DATE")),
        **{f"CHS_Day_{d}": parse_score(props.get(f"CHS_Day_{d}")) for d in range(NUM_DAYS)},
        "GlobalID": props.get("GlobalID"),
    }


def round_geom(geom, nd=6):
    """6 decimal places (~10 cm), as RFC 7946 recommends; keeps files smaller."""
    def rr(c):
        return [rr(x) for x in c] if isinstance(c[0], (list, tuple)) else [round(c[0], nd), round(c[1], nd)]
    return {"type": geom["type"], "coordinates": rr(geom["coordinates"])} if geom else None


def polygons_of(geom):
    if not geom:
        return []
    return [geom["coordinates"]] if geom["type"] == "Polygon" else list(geom["coordinates"])


def write_geojson(features, issue, path):
    out = {
        "type": "FeatureCollection",
        "name": f"{PREFIX}_7Day_Fcst",
        "issue_date": issue.isoformat(),
        "features": [
            {"type": "Feature", "properties": gis_properties(f.get("properties") or {}),
             "geometry": round_geom(f.get("geometry"))}
            for f in features
        ],
    }
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")


def ring_area(ring):
    return sum(x0 * y1 - x1 * y0 for (x0, y0, *_), (x1, y1, *_) in zip(ring, ring[1:])) / 2


def write_shapefile(features, path):
    """Zipped shapefile (WGS84). Outer rings clockwise, holes counterclockwise, per the spec."""
    import shapefile  # pyshp

    tmpdir = path.with_name(path.stem + "_parts")
    tmpdir.mkdir()
    base = tmpdir / f"{PREFIX}_7Day_Fcst"
    with shapefile.Writer(str(base), shapeType=shapefile.POLYGON, encoding="utf-8") as w:
        w.field("ZIP_CODE", "C", size=10)
        w.field("DATE", "C", size=25)
        for d in range(NUM_DAYS):
            w.field(f"CHS_Day_{d}", "N", size=1, decimal=0)
        w.field("GlobalID", "C", size=40)
        for f in features:
            parts = []
            for poly in polygons_of(f.get("geometry")):
                for k, ring in enumerate(poly):
                    ring = [list(pt[:2]) for pt in ring]
                    clockwise = ring_area(ring) < 0
                    if (k == 0) != clockwise:  # outer must be clockwise, holes counterclockwise
                        ring.reverse()
                    parts.append(ring)
            if parts:
                w.poly(parts)
            else:
                w.null()
            pr = gis_properties(f.get("properties") or {})
            w.record(pr["ZIP_CODE"], pr["DATE"], *[pr[f"CHS_Day_{d}"] for d in range(NUM_DAYS)], pr["GlobalID"])
    base.with_suffix(".prj").write_text(WGS84_PRJ)
    base.with_suffix(".cpg").write_text("UTF-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for part in sorted(tmpdir.iterdir()):
            zf.write(part, part.name)
    shutil.rmtree(tmpdir)


def write_png_maps(features, issue, staging, credit, days=range(NUM_DAYS)):
    """One 1920x1080 map per day, for TV, web stories and social posts."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PathCollection
    from matplotlib.patches import Rectangle
    from matplotlib.path import Path as MplPath

    # One compound path per ZIP (outer rings plus holes), built once for all 7 days.
    paths, xs, ys = [], [], []
    for f in features:
        verts, codes = [], []
        for poly in polygons_of(f.get("geometry")):
            for ring in poly:
                pts = [pt[:2] for pt in ring]
                verts += pts
                codes += [MplPath.MOVETO] + [MplPath.LINETO] * (len(pts) - 2) + [MplPath.CLOSEPOLY]
                xs += [p[0] for p in pts]
                ys += [p[1] for p in pts]
        paths.append(MplPath(verts, codes) if verts else None)
    props = [f.get("properties") or {} for f in features]
    pad = 0.3
    xlim, ylim = (min(xs) - pad, max(xs) + pad), (min(ys) - pad, max(ys) + pad)
    aspect = 1 / math.cos(math.radians(sum(ylim) / 2))

    names = []
    for d in days:
        valid = issue + timedelta(days=d)
        fig = plt.figure(figsize=(19.2, 10.8), dpi=100, facecolor="white")
        ax = fig.add_axes([0.02, 0.03, 0.55, 0.94])
        ax.set_xlim(*xlim)
        ax.set_ylim(*ylim)
        ax.set_aspect(aspect)
        ax.axis("off")
        groups = {}
        for path, p in zip(paths, props):
            if path is not None:
                groups.setdefault(parse_score(p.get(f"CHS_Day_{d}")), []).append(path)
        missing = None in groups
        for score in [None, *SCALE]:  # draw higher scores last so they stay visible
            if score in groups:
                color = SCALE[score][1] if score is not None else NO_DATA_COLOR
                ax.add_collection(PathCollection(groups[score], facecolor=color,
                                                 edgecolor="#8c8c8c", linewidth=0.15))

        fig.text(0.60, 0.86, "CalHeatScore", fontsize=54, fontweight="bold", color="#222222")
        fig.text(0.60, 0.79, f"Day {d + 1}: {valid:%A, %B} {valid.day}, {valid.year}",
                 fontsize=32, color="#222222")
        fig.text(0.60, 0.745, "Heat-related health risk by ZIP code", fontsize=22, color="#555555")
        entries = [(SCALE[k][1], f"{k}  {SCALE[k][0]}") for k in sorted(SCALE, reverse=True)]
        if missing:
            entries.append((NO_DATA_COLOR, "No data"))
        for i, (color, label) in enumerate(entries):
            y = 0.61 - i * 0.085
            fig.add_artist(Rectangle((0.60, y), 0.045, 0.06, transform=fig.transFigure,
                                     facecolor=color, edgecolor="#8c8c8c", linewidth=1))
            fig.text(0.66, y + 0.03, label, fontsize=30, va="center", color="#222222")
        fig.text(0.60, 0.09, f"Issued {issue:%B} {issue.day}, {issue.year}", fontsize=18, color="#555555")
        fig.text(0.60, 0.05, credit, fontsize=18, color="#555555")
        name = f"{PREFIX}_Day{d + 1}_Fcst.png"
        fig.savefig(staging / name, dpi=100, facecolor="white")
        plt.close(fig)
        names.append(name)
    return names


def build_products(features, issue, staging, fmt, expires, png=True, gis=True, credit=""):
    """Write the 7 Day files, each colored by its own day's scores.

    Placemarks use the GeoPortal export's markup (ZIP name, HTML attribute popup,
    clampToGround polygons) with the official CalHeatScore palette.
    """
    props = [f.get("properties") or {} for f in features]
    geoms = [geometry_to_kml(f.get("geometry")) for f in features]

    def style_for(p, d):
        s = parse_score(p.get(f"CHS_Day_{d}"))
        return f"CHS{s}" if s is not None else "CHSNA"

    days = []
    for d in range(NUM_DAYS):
        valid = issue + timedelta(days=d)
        body = "".join(placemark(f"ID_{i:05d}", p, g, style_for(p, d), extended_data_kml(p, d, issue))
                       for i, (p, g) in enumerate(zip(props, geoms)))
        name = f"{PREFIX}_Day{d + 1}_Fcst.{fmt}"
        title = f"CalHeatScore Day {d + 1}: {valid:%a %b %d, %Y} (issued {issue:%b %d, %Y})"
        with open_product(staging / name, fmt) as fh:
            fh.write(day_document(title, body, expires).encode())
        days.append({"day": d + 1, "valid_date": valid.isoformat(), "score_field": f"CHS_Day_{d}", "file": name})

    # Optional products: a missing library skips that product, not the whole run.
    geojson = shapefile_zip = None
    pngs = []
    if gis:
        write_geojson(features, issue, staging / GEOJSON_NAME)
        geojson = GEOJSON_NAME
        try:
            write_shapefile(features, staging / SHAPEFILE_NAME)
            shapefile_zip = SHAPEFILE_NAME
        except ImportError:
            logging.warning("pyshp not installed (pip install pyshp); skipping the shapefile")
    if png:
        try:
            pngs = write_png_maps(features, issue, staging, credit)
            for entry, name in zip(days, pngs):
                entry["png"] = name
        except ImportError:
            logging.warning("matplotlib not installed (pip install matplotlib); skipping PNG maps")

    return {
        "product": "CalHeatScore",
        "issue_date": issue.isoformat(),
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "next_update": expires.isoformat(),
        "zip_count": len(features),
        "entire_forecast": ENTIRE_NAME,
        "geojson": geojson,
        "shapefile": shapefile_zip,
        "days": days,
    }


# ---------------------------------------------------------------- publishing

def install(src, dest):
    """Copy into place via temp file + rename so the web server never serves a partial file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)


def prune_archive(archive_root, keep_days, today):
    if not keep_days or not archive_root.exists():
        return
    cutoff = today - timedelta(days=keep_days)
    for d in archive_root.iterdir():
        try:
            day = datetime.strptime(d.name, "%Y%m%d").date()
        except ValueError:
            continue
        if d.is_dir() and day < cutoff:
            shutil.rmtree(d)
            logging.info("Pruned archive %s", d.name)


def write_archive_index(archive_root):
    dirs = sorted((d for d in archive_root.iterdir()
                   if d.is_dir() and d.name.isdigit() and len(d.name) == 8), reverse=True)
    rows = []
    for d in dirs:
        files = sorted(f.name for f in d.iterdir() if f.is_file() and not f.name.startswith("."))
        def label(f):
            stem, ext = f.rsplit(".", 1)
            stem = stem.replace(PREFIX + "_", "").replace("_Fcst", "").replace("_shp", "")
            kind = "shapefile" if f.endswith("_shp.zip") else ext.upper()
            return f"{stem} ({kind})" if stem != "metadata" else "metadata"

        links = " &middot; ".join(f'<a href="{d.name}/{escape(f)}">{escape(label(f))}</a>' for f in files)
        rows.append(f"<tr><td>{d.name[:4]}-{d.name[4:6]}-{d.name[6:]}</td><td>{links}</td></tr>")
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>CalHeatScore KML Archive</title>"
        "<style>body{font-family:sans-serif;margin:2rem}td{padding:4px 12px;border-bottom:1px solid #ddd}</style>"
        "</head><body><h1>CalHeatScore KML Archive</h1><p>Files by forecast issue date.</p>"
        f"<table><tr><th>Issued</th><th>Files</th></tr>{''.join(rows)}</table></body></html>\n"
    )
    tmp = archive_root / ".index.html.tmp"
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, archive_root / "index.html")


SITE_HOME = "https://calheatscore.calepa.ca.gov/"
PAGE_CSS = (
    "body{font-family:Arial,Helvetica,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;"
    "color:#222;line-height:1.5}"
    "h1{font-size:1.6rem}h2{font-size:1.2rem;margin-top:2rem}"
    "table{border-collapse:collapse;margin:1rem 0}"
    "th,td{border:1px solid #ccc;padding:6px 12px;text-align:left;vertical-align:top}"
    "th{background:#f3f3f3}.sw{display:inline-block;width:1.2em;height:1em;"
    "border:1px solid #888;vertical-align:middle;margin-right:.4em}"
    ".note{color:#555;font-size:.95em}"
    ".preview{background:#fff6dd;border:1px solid #e0c36a;padding:.8rem 1rem;margin-bottom:1.5rem}"
    ".sample img{max-width:100%;border:1px solid #ccc}"
)


def write_text_atomic(path, text):
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def fmt_pt(dt):
    return dt.astimezone(TZ).strftime("%m/%d/%Y %-I:%M %p %Z")


LAYER_URL = QUERY_URL.rsplit("/", 1)[0]


def write_site_index(web_root, meta, refresh_times, archive_enabled, credit, contact, preview=None):
    """Landing page for the KML directory (index.html), regenerated on every run."""
    issue = date.fromisoformat(meta["issue_date"])
    generated = datetime.fromisoformat(meta["generated_utc"])
    next_update = datetime.fromisoformat(meta["next_update"])
    times = " and ".join(datetime.combine(date.today(), t).strftime("%-I:%M %p") for t in refresh_times)

    day_cells = "".join(
        f'<td><a href="{escape(d["file"])}">Day {d["day"]}</a><br>'
        f'{date.fromisoformat(d["valid_date"]):%a %m/%d}</td>'
        for d in meta["days"]
    )
    legend = "".join(
        f'<tr><td><span class="sw" style="background:{rgb}"></span>{k}</td><td>{label}</td></tr>'
        for k, (label, rgb) in SCALE.items()
    )
    png_row = ""
    if any(d.get("png") for d in meta["days"]):
        png_cells = "".join(f'<td><a href="{escape(d["png"])}">Day {d["day"]}</a></td>' if d.get("png")
                            else "<td></td>" for d in meta["days"])
        png_row = f"<tr><th>Map images (PNG, 1920&times;1080)</th>{png_cells}</tr>"
    span = len(meta["days"])
    gis_rows = ""
    if meta.get("geojson"):
        gis_rows += (f'<tr><th>GeoJSON (all 7 days)</th><td colspan="{span}"><a href="{escape(meta["geojson"])}">'
                     f'{escape(meta["geojson"])}</a></td></tr>')
    if meta.get("shapefile"):
        gis_rows += (f'<tr><th>Shapefile (all 7 days, zipped)</th><td colspan="{span}">'
                     f'<a href="{escape(meta["shapefile"])}">{escape(meta["shapefile"])}</a></td></tr>')
    gis_rows += (f'<tr><th>Web service (ArcGIS REST)</th><td colspan="{span}"><a href="{LAYER_URL}">'
                 f'CalHeatScore live data layer</a> <span class="note">(query, GeoJSON and other '
                 f'formats; no key required)</span></td></tr>')
    contact_p = f" Questions about the data: <a href=\"mailto:{escape(contact)}\">{escape(contact)}</a>." if contact else ""
    archive_p = (
        '<h2>Archive</h2><p>Past forecasts are kept in the <a href="archive/">archive</a>, '
        "organized by issue date.</p>"
    ) if archive_enabled else ""

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CalHeatScore KML Data</title><style>{PAGE_CSS}</style></head>
<body>
{preview["banner"] if preview else ""}
<h1>CalHeatScore KML Data</h1>
<p>This page hosts the current CalHeatScore forecast as KML files for Google Earth, broadcast
graphics and GIS software, along with GeoJSON, shapefile and ready-made map images. Each file covers
every California ZIP code, colored by its CalHeatScore for that day. The files are refreshed twice daily, at about {times} Pacific Time.</p>
<p><strong>Forecast issued:</strong> {issue:%m/%d/%Y}<br>
<strong>Files last updated:</strong> {fmt_pt(generated)}<br>
<strong>Next scheduled update:</strong> {fmt_pt(next_update)}</p>

<h2>Current forecast</h2>
<table>
<tr><th>KML for entire forecast period</th>
<td colspan="{len(meta["days"])}"><a href="{escape(meta["entire_forecast"])}">7-Day Forecast</a>
<span class="note">(opens all 7 days in Google Earth and updates automatically)</span></td></tr>
<tr><th>KML for individual days</th>{day_cells}</tr>
{png_row}
{gis_rows}
</table>
<p class="note">The links above always point to the latest forecast. See the
<a href="README.txt">readme</a> for file and field descriptions.</p>
{preview["sample"] if preview else ""}
<h2>Using the data</h2>
<p>Each Day KML includes the fields <code>CHS</code> (that day's score, 0&ndash;4),
<code>CHS_LABEL</code> and <code>VALID_DATE</code>, so graphics and GIS software can color or label ZIP
codes with a single template that works for every day. The GeoJSON and shapefile carry all 7 days
as <code>CHS_Day_0</code> (Day 1) through <code>CHS_Day_6</code> (Day 7). The map images can be used
as-is in broadcasts, web stories and social posts.</p>
<p>Please credit: <strong>{escape(credit)}</strong>.{contact_p}</p>

<h2>CalHeatScore scale</h2>
<table><tr><th>Score</th><th>Level</th></tr>{legend}</table>
{archive_p}
<p><a href="{SITE_HOME}">Navigate back to the CalHeatScore website.</a></p>
</body></html>
"""
    write_text_atomic(web_root / "index.html", html.replace("\n\n\n", "\n\n"))


def planned_meta(features, issue, fmt, expires, png, gis):
    """The metadata a full run would produce, without writing any data files."""
    from importlib.util import find_spec
    png = png and find_spec("matplotlib") is not None
    days = [{"day": d + 1, "valid_date": (issue + timedelta(days=d)).isoformat(),
             "score_field": f"CHS_Day_{d}", "file": f"{PREFIX}_Day{d + 1}_Fcst.{fmt}",
             **({"png": f"{PREFIX}_Day{d + 1}_Fcst.png"} if png else {})} for d in range(NUM_DAYS)]
    return {
        "product": "CalHeatScore",
        "issue_date": issue.isoformat(),
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "next_update": expires.isoformat(),
        "zip_count": len(features),
        "entire_forecast": ENTIRE_NAME,
        "geojson": GEOJSON_NAME if gis else None,
        "shapefile": SHAPEFILE_NAME if gis and find_spec("shapefile") is not None else None,
        "days": days,
    }


def preview_parts(features, issue, credit, png, workdir):
    """Banner and embedded Day 1 map for a shareable, self-contained preview page."""
    banner = (
        '<div class="preview"><strong>Preview for review.</strong> This page is not live yet, so the '
        f"file download links don't work. Dates and scores are from the forecast issued {issue:%m/%d/%Y}."
        "</div>"
    )
    sample = ""
    if png:
        try:
            import base64
            name = write_png_maps(features, issue, workdir, credit, days=[0])[0]
            data = base64.b64encode((workdir / name).read_bytes()).decode()
            sample = ('<div class="preview sample"><p><strong>Preview only:</strong> the Day 1 map image '
                      f'as it will be published.</p><img alt="CalHeatScore Day 1 map" '
                      f'src="data:image/png;base64,{data}"></div>')
        except ImportError:
            logging.warning("matplotlib not installed; preview will not include a sample map")
    return {"banner": banner, "sample": sample}


def write_readme(web_root, meta, refresh_times, archive_enabled, credit):
    times = " and ".join(datetime.combine(date.today(), t).strftime("%-I:%M %p") for t in refresh_times)
    scale = "\n".join(f"  {k}  {label:<9} {rgb}" for k, (label, rgb) in SCALE.items())
    archive = (
        "\narchive/YYYYMMDD/\n"
        "  The same files for each past forecast, by issue date. The archived\n"
        "  Entire file links to that date's archived Day files and does not update.\n"
    ) if archive_enabled else ""
    extra = ""
    if any(d.get("png") for d in meta["days"]):
        extra += ("\nCalHeatScore_Day1_Fcst.png ... CalHeatScore_Day7_Fcst.png\n"
                  "  Ready-made 1920x1080 map of each day's scores, with legend and credit\n"
                  "  line, for broadcasts, web stories and social posts.\n")
    if meta.get("geojson"):
        extra += (f"\n{meta['geojson']}\n"
                  "  All ZIP codes with all 7 days' scores as attributes (WGS84).\n")
    if meta.get("shapefile"):
        extra += (f"\n{meta['shapefile']}\n"
                  "  The same data as a zipped ESRI shapefile (WGS84).\n")
    text = f"""CalHeatScore KML Data - README

CalHeatScore rates heat-related health risk for every California ZIP code on a
0-4 scale, for today and the following 6 days. These files are regenerated from
the live CalHeatScore data twice daily, at about {times} Pacific Time.

FILES

CalHeatScore_Entire_Fcst.kml
  A small file of network links to the 7 Day files. Open it in Google Earth to
  get every day of the forecast; Day 1 is shown first and the others can be
  switched on in the Places panel. Google Earth re-downloads the Day files
  automatically after each scheduled update.

CalHeatScore_Day1_Fcst.kmz ... CalHeatScore_Day7_Fcst.kmz
  One file per forecast day. Day 1 is the issue date (today), Day 2 is
  tomorrow, and so on. ZIP code polygons are colored by that day's score.

  Each placemark's popup lists the source fields below. Each placemark also
  carries machine-readable data fields (KML ExtendedData) for software that
  styles or queries by attribute:

    ZIP_CODE      ZIP code
    ISSUE_DATE    Forecast issue date (YYYY-MM-DD)
    VALID_DATE    Date this file's scores apply to (YYYY-MM-DD)
    FCST_DAY      Forecast day, 1-7
    CHS           CalHeatScore for VALID_DATE (0-4); same field name in every
                  Day file, so one style template works for all of them
    CHS_LABEL     Low, Mild, Moderate, High or Severe
    CHS_Day_0-6   Scores for all 7 days
{extra}
CalHeatScore_latest.json
  Issue date, file update time, next scheduled update, and the valid date of
  each Day file.
{archive}
SOURCE FIELDS (popups, GeoJSON and shapefile)

  ZIP_CODE        ZIP code
  DATE            Forecast issue date
  CHS_Day_0       CalHeatScore for the issue date (Day 1)
  CHS_Day_1-6     CalHeatScore for the following 6 days (Days 2-7)
  GlobalID        Feature identifier
  Shape__Area     ZIP code polygon area (KML popups only)
  Shape__Length   ZIP code polygon perimeter (KML popups only)

SCALE

{scale}

CREDIT

  {credit}

Live data web service (ArcGIS REST, no key required): {LAYER_URL}
More information: {SITE_HOME}
"""
    write_text_atomic(web_root / "README.txt", text)


def main():
    p = argparse.ArgumentParser(description="Publish daily CalHeatScore KML files to a web directory.")
    p.add_argument("--web-root", required=True, type=Path, help="Directory served at your data URL")
    p.add_argument("--base-url", help="Public URL of --web-root, e.g. https://calheatscore.calepa.ca.gov/data/ "
                   "(the Entire file links to the Day files at this URL)")
    p.add_argument("--refresh-times", default="05:45,08:45",
                   help="Pacific times when Google Earth should re-pull the Day files, comma-separated. "
                        "Set these a little after the cron runs that publish new files.")
    p.add_argument("--format", choices=["kml", "kmz"], default="kmz", help="Format of the Day files")
    p.add_argument("--simplify", type=float, default=0.0,
                   help="Geometry simplification tolerance in degrees (e.g. 0.0002); 0 = off")
    p.add_argument("--keep-days", type=int, default=0, help="Prune archives older than N days; 0 = keep all")
    p.add_argument("--no-archive", action="store_true", help="Only publish the current files; keep no dated archive")
    p.add_argument("--no-png", action="store_true", help="Skip the PNG map images")
    p.add_argument("--no-gis", action="store_true", help="Skip the GeoJSON and shapefile downloads")
    p.add_argument("--banner", help="Notice shown at the top of the landing page, e.g. for a staging or "
                   "preview site; plain text")
    p.add_argument("--page-only", action="store_true",
                   help="Write only a preview of the landing page (index.html) and README.txt to --web-root, "
                        "using the current data for dates; publishes no data files")
    p.add_argument("--credit", default="Source: CalHeatScore (calheatscore.calepa.ca.gov)",
                   help="Credit line shown on the page, in the readme and on the map images")
    p.add_argument("--contact", help="Email address for data questions, shown on the page")
    p.add_argument("--require-fresh", action="store_true",
                   help="Exit without publishing if the layer's DATE is older than today")
    args = p.parse_args()
    try:
        refresh_times = parse_refresh_times(args.refresh_times)
    except ValueError:
        p.error("--refresh-times must look like 05:45,08:45")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    features = fetch_features(args.simplify)
    n = len(features)
    logging.info("Fetched %d ZIP codes", n)
    if n < MIN_EXPECTED_FEATURES:
        logging.error("Only %d features returned; not publishing", n)
        return 1
    missing = sum(parse_score((f.get("properties") or {}).get("CHS_Day_0")) is None for f in features)
    if missing / n > MAX_MISSING_FRACTION:
        logging.error("%d of %d ZIPs have no Day 1 score; not publishing", missing, n)
        return 1

    today = datetime.now(TZ).date()
    issue = issue_date_of(features)
    if issue is None:
        logging.warning("Could not read DATE field; using today (%s) as issue date", today)
        issue = today
    elif issue < today:
        logging.warning("Layer DATE is %s but today is %s; layer may not have refreshed yet", issue, today)
        if args.require_fresh:
            return 2

    if args.page_only:
        web_root = args.web_root
        web_root.mkdir(parents=True, exist_ok=True)
        expires = next_refresh(datetime.now(TZ).replace(microsecond=0), refresh_times)
        meta = planned_meta(features, issue, args.format, expires, not args.no_png, not args.no_gis)
        workdir = web_root / f".preview-{os.getpid()}"
        workdir.mkdir()
        try:
            preview = preview_parts(features, issue, args.credit, not args.no_png, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        write_readme(web_root, meta, refresh_times, not args.no_archive, args.credit)
        write_site_index(web_root, meta, refresh_times, not args.no_archive, args.credit, args.contact, preview)
        logging.info("Wrote preview page to %s", web_root / "index.html")
        return 0

    base = args.base_url or ""
    if base and not base.endswith("/"):
        base += "/"
    if not base:
        logging.warning("No --base-url given; the Entire file will use relative links, which work "
                        "when opened from the website but not after it's downloaded")

    web_root = args.web_root
    staging = web_root / f".staging-{os.getpid()}"
    try:
        staging.mkdir(parents=True)
        expires = next_refresh(datetime.now(TZ).replace(microsecond=0), refresh_times)
        meta = build_products(features, issue, staging, args.format, expires,
                              png=not args.no_png, gis=not args.no_gis, credit=args.credit)
        meta_name = f"{PREFIX}_latest.json"
        (staging / meta_name).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        day_files = [d["file"] for d in meta["days"]]
        extra_files = [d["png"] for d in meta["days"] if d.get("png")] + \
            [f for f in (meta.get("geojson"), meta.get("shapefile")) if f]
        stamp = issue.strftime("%Y%m%d")
        labels = [f"Valid {date.fromisoformat(d['valid_date']):%a %b %d, %Y}" for d in meta["days"]]

        # Latest Entire file: auto-refreshing links to the stable Day file URLs.
        (staging / ENTIRE_NAME).write_text(entire_document(
            [base + f for f in day_files], "CalHeatScore", labels, refresh_times), encoding="utf-8")
        archive_dir = web_root / "archive" / stamp
        if not args.no_archive:
            # Archived Entire file: static links to that day's archived Day files.
            archive_entire = staging / f"archive_{ENTIRE_NAME}"
            archive_entire.write_text(entire_document(
                [f"{base}archive/{stamp}/{f}" for f in day_files],
                f"CalHeatScore (issued {issue:%b %d, %Y})", labels), encoding="utf-8")
            for name in day_files + extra_files:
                install(staging / name, archive_dir / name)
            install(archive_entire, archive_dir / ENTIRE_NAME)
            install(staging / meta_name, archive_dir / "metadata.json")
        for name in day_files + extra_files:
            install(staging / name, web_root / name)
        install(staging / ENTIRE_NAME, web_root / ENTIRE_NAME)
        install(staging / meta_name, web_root / meta_name)  # last, so it only describes published files
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if not args.no_archive:
        prune_archive(web_root / "archive", args.keep_days, today)
        write_archive_index(web_root / "archive")
    write_readme(web_root, meta, refresh_times, not args.no_archive, args.credit)
    banner = ({"banner": f'<div class="preview">{escape(args.banner)}</div>', "sample": ""}
              if args.banner else None)
    write_site_index(web_root, meta, refresh_times, not args.no_archive, args.credit, args.contact, banner)
    logging.info("Published CalHeatScore issued %s to %s", issue, web_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
