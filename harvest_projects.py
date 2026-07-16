#!/usr/bin/env python3
"""
harvest_projects.py  --  builds projects.json for the Live Projects map.

RUN ENVIRONMENT: GitHub Actions (scheduled), NOT the build sandbox.
The sandbox network is locked to package registries; this script needs open-web
access, so it runs in your repo's Actions runner (like wire_harvest.py).

DELIBERATELY EXCLUDES ConstructConnect / Dodge: those are paywalled commercial
products with no public API. Scraping them violates their ToS. We use OPEN data
instead, which is also better targeted (projects that threaten significant places,
not strip-mall bid leads).

SOURCES (all open):
  - Municipal building-permit open data  (Socrata SODA API)  -> LOCAL projects
  - Global Energy Monitor trackers        (downloadable data) -> energy/fossil infra
  - Land Matrix                           (public API)        -> large land deals
  - EJAtlas                               (data export)       -> documented conflicts
  - EPA EIS database / FERC eLibrary      (gov data)          -> federal review pipeline

Each source fetcher returns a list of normalized dicts:
  {name,type,state,lat,lng,size,status,company,desc,source}
rate_project() then assigns impact 1-5. Records without lat/lng are skipped
(the map needs a point). Output is written to projects.json.
"""
import json, sys, os, re, time, datetime, urllib.request, urllib.parse

UA = "activist-projects-harvester (contact: wheelock.chris@gmail.com)"
TIMEOUT = 30

# ----------------------------------------------------------------------------
# IMPACT RATING  (1 minor .. 5 landscape/nationally significant)
# ----------------------------------------------------------------------------
# Type weight: fossil/extractive/petrochemical infrastructure scores highest
# because it does the most irreversible harm to significant places.
TYPE_WEIGHT = {
    "lng": 5, "petrochemical": 5, "refinery": 5, "coal": 5, "oil": 5, "gas": 4,
    "pipeline": 4, "mine": 5, "mining": 5, "lithium": 4, "power plant": 4,
    "dam": 4, "highway": 3, "landfill": 3, "data center": 3, "warehouse": 2,
    "logging": 4, "timber": 4, "cafo": 3, "feedlot": 3, "subdivision": 2,
    "commercial": 1, "residential": 1, "development": 2,
}

def _type_score(type_str):
    t = (type_str or "").lower()
    best = 1
    for k, w in TYPE_WEIGHT.items():
        if k in t:
            best = max(best, w)
    return best

def _magnitude_score(size_str, value_usd=None, acres=None, mw=None, miles=None):
    """Rough 1-5 from whatever magnitude field is available."""
    if value_usd:
        if value_usd >= 1e9:  return 5
        if value_usd >= 2.5e8: return 4
        if value_usd >= 5e7:  return 3
        if value_usd >= 5e6:  return 2
        return 1
    if acres:
        if acres >= 2000: return 5
        if acres >= 500:  return 4
        if acres >= 100:  return 3
        if acres >= 20:   return 2
        return 1
    if mw:   return 5 if mw >= 500 else 4 if mw >= 100 else 3
    if miles:return 5 if miles >= 100 else 4 if miles >= 25 else 3
    return 0  # unknown magnitude

def rate_project(p, sensitivity=0):
    """Combine type + magnitude + ecological/EJ sensitivity into 1-5."""
    ts = _type_score(p.get("type"))
    ms = _magnitude_score(p.get("size"), p.get("value_usd"), p.get("acres"),
                          p.get("mw"), p.get("miles"))
    # base: lean on type, lifted by magnitude when known
    base = ts if ms == 0 else round((ts * 0.6) + (ms * 0.4))
    base += sensitivity  # +1 if near protected land/water or an EJ community
    return max(1, min(5, base))

# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------
def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _num(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def _first(row, *names):
    for n in names:
        if n in row and row[n] not in (None, ""): return row[n]
    return None

# ----------------------------------------------------------------------------
# SOURCE 1 -- Municipal building permits via Socrata (SODA API)   [LOCAL]
# ----------------------------------------------------------------------------
# Socrata is JSON, no key required for modest volume. Each city exposes a
# dataset; confirm the domain + dataset id + column names per city (they vary),
# then add to SOCRATA_CITIES. The three below are the PATTERN -- verify the
# dataset ids and field names against each portal before trusting them.
SOCRATA_CITIES = [
    # --- VERIFIED (dataset id + field names confirmed against live CSV headers) ---
    # The $-value filter surfaces SIGNIFICANT projects (big developments), not every
    # roof/deck permit. Tune the threshold and date as you like.
    {"city": "Chicago, IL", "domain": "data.cityofchicago.org", "dataset": "ydr8-5enu",
     "lat": "latitude", "lng": "longitude", "name": "work_description", "type": "permit_type",
     "value": "reported_cost", "status": "permit_status",
     "where": "reported_cost > 5000000 AND issue_date > '2025-01-01'"},
    {"city": "Austin, TX", "domain": "data.austintexas.gov", "dataset": "3syk-w9eu",
     "lat": "latitude", "lng": "longitude", "name": "description", "type": "permit_class",
     "value": "total_job_valuation", "status": "status_current",
     "where": "total_job_valuation > 5000000 AND issued_date > '2025-01-01'"},
    {"city": "Seattle, WA", "domain": "data.seattle.gov", "dataset": "76t5-zqzr",
     "lat": "latitude", "lng": "longitude", "name": "description", "type": "permitclassmapped",
     "value": "estprojectcost", "status": "statuscurrent",
     "where": "estprojectcost > 5000000 AND issueddate > '2025-01-01'"},

    # SF: coords live in a `location` POINT column (verified against live CSV).
    {"city": "San Francisco, CA", "domain": "data.sfgov.org", "dataset": "i98e-djp9",
     "point": "location", "name": "description", "type": "permit_type_definition",
     "value": "estimated_cost", "status": "status",
     "where": "estimated_cost > 5000000 AND issued_date > '2025-01-01'"},

    # --- DATASET ID CONFIRMED, FIELD NAMES TO VERIFY before enabling ---
    # LA: coords in a Location column "Latitude/Longitude" -> field `latitude_longitude`
    # (verified against live CSV headers; parsed by _socrata_point's dict branch).
    {"city": "Los Angeles, CA", "domain": "data.lacity.org", "dataset": "pi9x-tg5x",
     "point": "latitude_longitude", "name": "work_description", "type": "permit_type",
     "value": "valuation", "status": "status",
     "where": "valuation > 5000000 AND issue_date > '2025-01-01'"},

    # --- MORE CITIES: same pattern -- confirm dataset id + field names, then add ---
    # NYC: permits are split across DOB NOW + historical datasets and need lat/lng joined
    # from BIN/BBL -- add once you pick the geocoded dataset.
]

def _socrata_point(r, cfg):
    """Return (lat,lng). Some cities (SF, LA) use a point column instead of
    separate lat/lng columns -- either a WKT 'POINT (lng lat)' string or a
    GeoJSON dict. Configure cfg['point'] for those; otherwise use lat/lng cols."""
    pf = cfg.get("point")
    if pf and r.get(pf) is not None:
        v = r.get(pf)
        if isinstance(v, dict):
            c = v.get("coordinates")
            if c and len(c) >= 2: return _num(c[1]), _num(c[0])
            if v.get("latitude") and v.get("longitude"):
                return _num(v["latitude"]), _num(v["longitude"])
        elif isinstance(v, str) and v.upper().startswith("POINT"):
            nums = v.replace("POINT", "").replace("(", "").replace(")", "").split()
            if len(nums) >= 2: return _num(nums[1]), _num(nums[0])
    if cfg.get("lat") and cfg.get("lng"):
        return _num(r.get(cfg["lat"])), _num(r.get(cfg["lng"]))
    return None, None

def fetch_socrata(cfg, limit=500):
    out = []
    base = "https://{d}/resource/{ds}.json".format(d=cfg["domain"], ds=cfg["dataset"])
    params = {"$limit": limit, "$order": ":id"}
    if cfg.get("where"): params["$where"] = cfg["where"]
    url = base + "?" + urllib.parse.urlencode(params)
    try:
        rows = _get_json(url)
    except Exception as e:
        print("  socrata %s failed: %s" % (cfg.get("city"), e)); return out
    _DONE = ("complete", "closed", "expired", "withdrawn", "cancel", "final",
             "void", "revoked", "stop work", "inactive", "issued - closed", "certificate of occupancy")
    for r in rows:
        lat, lng = _socrata_point(r, cfg)
        if lat is None or lng is None: continue
        _st = str(r.get(cfg.get("status")) or "").lower()
        if any(k in _st for k in _DONE):   # drop projects that are no longer active
            continue
        val = _num(r.get(cfg.get("value")))
        p = {"name": r.get(cfg["name"]) or "Permitted project",
             "type": r.get(cfg.get("type")) or "development",
             "state": cfg["city"].split(",")[-1].strip(),
             "lat": lat, "lng": lng, "value_usd": val,
             "status": r.get(cfg.get("status")) or "permitted",
             "company": "", "size": ("$%s" % int(val)) if val else "",
             "desc": "Local permit filing. Verify scope, then check the "
                     "jurisdiction's planning docket for hearings and comment windows.",
             "source": "socrata:" + cfg["domain"]}
        p["impact"] = rate_project(p)
        out.append(p)
    return out

# ----------------------------------------------------------------------------
# SOURCE 2 -- Land Matrix (public API)                           [GLOBAL]
# ----------------------------------------------------------------------------
# Land Matrix exposes deal data via API. Confirm the current endpoint/shape at
# https://landmatrix.org/ (they have a REST/GraphQL interface). Sketch:
def fetch_land_matrix(csv_path="data/land_matrix_deals.csv"):
    """Large-scale land acquisitions WITH coordinates.
    Get the CSV from datahub.io/core/land-matrix (weekly auto-updated) or the
    Land Matrix API export and save it to data/land_matrix_deals.csv. Export
    column names vary, so several aliases are tried."""
    import csv, os
    out = []
    if not os.path.exists(csv_path):
        print("  land matrix: %s not found (skip)" % csv_path); return out
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lat = _num(_first(row, "point_lat", "latitude", "lat", "deal_lat"))
            lng = _num(_first(row, "point_lon", "longitude", "lng", "lon", "deal_lon"))
            if lat is None or lng is None: continue
            ha = _num(_first(row, "deal_size", "size", "intended_size", "contract_size"))
            p = {"name": (_first(row, "deal_name", "name") or "Large land acquisition")[:120],
                 "type": _first(row, "current_intention_of_investment", "intention", "intended_use") or "land deal",
                 "state": _first(row, "target_country", "country") or "",
                 "lat": lat, "lng": lng, "acres": ha * 2.471 if ha else None,
                 "company": _first(row, "operating_company", "investor", "operating_company_name") or "",
                 "status": _first(row, "negotiation_status", "current_negotiation_status") or "",
                 "size": ("%s ha" % int(ha)) if ha else "",
                 "desc": "Large-scale land acquisition tracked by Land Matrix. Verify status "
                         "and investor, then check for community-consent and land-rights issues.",
                 "source": "land_matrix"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
    print("  land matrix: %d deals" % len(out))
    return out

# ----------------------------------------------------------------------------
# SOURCE 3 -- Global Energy Monitor trackers                     [ENERGY INFRA]
# ----------------------------------------------------------------------------
# GEM publishes downloadable trackers (Excel/CSV) under a data-use policy at
# globalenergymonitor.org/projects/. Pipeline: download the relevant tracker(s),
# read with pandas, keep US rows with coords, map columns -> normalized dict.
# only NEW / upcoming energy projects -- never operating or retired infrastructure
_GEM_NEW = ("announced", "pre-construction", "preconstruction", "construction",
            "proposed", "permitted", "in development", "planned")
_GEM_DEAD = ("operating", "retired", "cancelled", "canceled", "mothballed",
             "shelved", "closed", "abandoned", "decommissioned")

def _gem_norm(pr, lat, lng):
    if lat is None or lng is None: return None
    status = str(_first(pr, "Status", "status") or "").strip().lower()
    # keep only projects that are upcoming/under construction (not existing infra)
    if any(k in status for k in _GEM_DEAD): return None
    if not any(k in status for k in _GEM_NEW): return None
    name = _first(pr, "Project Name", "project_name", "Unit Name", "Name",
                  "Pipeline Name", "Mine Name")
    typ = _first(pr, "Type", "Fuel", "Category", "Sector") or "energy project"
    st = _first(pr, "Subnational unit (province/state)", "State/Province", "State", "Region")
    ctry = _first(pr, "Country/Area", "Country")
    mw = _num(_first(pr, "Capacity (MW)", "Capacity", "capacity_mw"))
    p = {"name": (name or "Energy project")[:120], "type": str(typ),
         "state": st or ctry or "", "lat": lat, "lng": lng, "mw": mw, "precise": True,
         "company": _first(pr, "Owner", "Parent", "Operator") or "",
         "status": _first(pr, "Status", "status") or "",
         "size": ("%s MW" % int(mw)) if mw else "",
         "desc": ("Proposed / under-construction energy project tracked by Global Energy "
                  "Monitor (CC BY 4.0). Status: " + (status or "unknown") + "."),
         "source": "gem"}
    p["impact"] = rate_project(p, sensitivity=1)
    return p

def fetch_gem(dir_path="data/gem"):
    """Global Energy Monitor trackers (coal/oil/gas/pipelines/LNG/mines/steel/...),
    all carrying coordinates. Download the tracker(s) from
    globalenergymonitor.org/download-data (CC-BY 4.0) OR generate per-country
    GeoJSON with the open-energy-transition/gem_per_country tool, and drop the
    files (.csv / .xlsx / .geojson) into data/gem/."""
    import os, glob, json as _json, csv
    out = []
    if not os.path.isdir(dir_path):
        print("  gem: %s not found (skip)" % dir_path); return out
    for path in glob.glob(os.path.join(dir_path, "*")):
        low = path.lower()
        try:
            if low.endswith((".geojson", ".json")):
                geo = _json.load(open(path, encoding="utf-8"))
                for ft in geo.get("features", []):
                    g = ft.get("geometry") or {}; pr = ft.get("properties") or {}
                    if g.get("type") != "Point": continue
                    c = g.get("coordinates") or []
                    if len(c) >= 2:
                        p = _gem_norm(pr, _num(c[1]), _num(c[0]))
                        if p: out.append(p)
            elif low.endswith(".csv"):
                for r in csv.DictReader(open(path, newline="", encoding="utf-8")):
                    lat = _num(_first(r, "Latitude", "latitude", "lat"))
                    lng = _num(_first(r, "Longitude", "longitude", "lng", "lon"))
                    p = _gem_norm(r, lat, lng)
                    if p: out.append(p)
            elif low.endswith(".xlsx"):
                import openpyxl
                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                for ws in wb.worksheets:
                    rows = ws.iter_rows(values_only=True); hdr = next(rows, None)
                    if not hdr: continue
                    idx = {str(h).strip(): i for i, h in enumerate(hdr) if h}
                    for r in rows:
                        pr = {k: r[i] for k, i in idx.items() if i < len(r)}
                        lat = _num(_first(pr, "Latitude", "latitude"))
                        lng = _num(_first(pr, "Longitude", "longitude"))
                        p = _gem_norm(pr, lat, lng)
                        if p: out.append(p)
        except Exception as e:
            print("  gem %s failed: %s" % (path, e))
    print("  gem: %d projects" % len(out))
    return out

# EPA EIS (cdxapps EIS database), FERC eLibrary API, EJAtlas export: same shape --
# fetch, keep records with coordinates, normalize, rate. Left as scaffolds so you
# can wire the endpoints you confirm without touching the rating/merge logic.
# state centroids for coarse geocoding of federal notices (approximate)
STATE_CENTROID = {
 "Alabama":(32.8,-86.8),"Alaska":(64.2,-149.5),"Arizona":(34.2,-111.7),"Arkansas":(34.9,-92.4),
 "California":(37.2,-119.3),"Colorado":(39.0,-105.5),"Connecticut":(41.6,-72.7),"Delaware":(39.0,-75.5),
 "District of Columbia":(38.9,-77.0),"Florida":(28.6,-82.4),"Georgia":(32.6,-83.4),"Hawaii":(20.3,-156.4),
 "Idaho":(44.4,-114.6),"Illinois":(40.0,-89.2),"Indiana":(39.9,-86.3),"Iowa":(42.0,-93.5),"Kansas":(38.5,-98.4),
 "Kentucky":(37.5,-85.3),"Louisiana":(31.0,-92.0),"Maine":(45.4,-69.2),"Maryland":(39.0,-76.8),
 "Massachusetts":(42.3,-71.8),"Michigan":(44.3,-85.4),"Minnesota":(46.3,-94.3),"Mississippi":(32.7,-89.7),
 "Missouri":(38.4,-92.5),"Montana":(47.0,-109.6),"Nebraska":(41.5,-99.8),"Nevada":(39.3,-116.6),
 "New Hampshire":(43.7,-71.6),"New Jersey":(40.2,-74.7),"New Mexico":(34.4,-106.1),"New York":(42.9,-75.5),
 "North Carolina":(35.5,-79.4),"North Dakota":(47.5,-100.5),"Ohio":(40.3,-82.8),"Oklahoma":(35.6,-97.5),
 "Oregon":(43.9,-120.6),"Pennsylvania":(40.9,-77.8),"Rhode Island":(41.7,-71.5),"South Carolina":(33.9,-80.9),
 "South Dakota":(44.4,-100.2),"Tennessee":(35.9,-86.4),"Texas":(31.5,-99.3),"Utah":(39.3,-111.7),
 "Vermont":(44.0,-72.7),"Virginia":(37.5,-78.9),"Washington":(47.4,-120.5),"West Virginia":(38.6,-80.6),
 "Wisconsin":(44.6,-89.9),"Wyoming":(43.0,-107.6),
}
import re as _re
def _detect_state(text):
    hits = [s for s in STATE_CENTROID if _re.search(r"\b" + _re.escape(s) + r"\b", text)]
    return hits[0] if len(hits) == 1 else None  # only place if unambiguous

def _infer_type(text):
    t = text.lower()
    for k in ("pipeline","lng","mine","mining","drilling","oil","gas","coal","dam",
              "transmission","highway","timber","logging","port","refinery","reservoir"):
        if k in t: return k
    return "federal project"

_PROJECT_ALLOW = (
    "pipeline","mine","mining","drill","borehole","well pad","lease sale","oil and gas",
    "coal","timber","logging","thinning","vegetation management","fuel reduction","hazardous fuels",
    "dam","reservoir","highway","interstate","roadway","bridge","transmission","substation",
    "power plant","powerplant","lng","terminal","refinery","petrochemical","quarry","aggregate",
    "geothermal","wind farm","wind energy","solar","mineral","uranium","lithium","copper","gold",
    "nickel","cobalt","phosphate","potash","extraction","grazing","allotment","right-of-way",
    "right of way","rights-of-way","land exchange","land disposal","port","harbor","dredg",
    "development","construction","expansion","mill","smelter","export terminal","rail",
    "resource management plan","travel management","forest plan","restoration project","landfill",
    "incinerator","data center","warehouse","subdivision","water project","canal","hydroelectric",
    "hydropower","reclamation","withdrawal","utility corridor","reroute","widening","interchange",
    "airport","runway","fiber","broadband","cell tower","telecom","wastewater","sewer",
    "water treatment","levee","channel","dredging","mining claim","mineral exploration",
    "borrow pit","geophysical","reroute","interconnection","desalination","pumped storage",
)
_RESEARCH_DENY = (
    "marine mammal","incidental take","scientific research","research permit","cetacean","pinniped",
    "stock assessment","fishery observer","enhancement permit","captive","aquarium","recovery plan",
    "status review","proposed for listing","import of","take of marine","permit to conduct research",
    "endangered species permit","scientific purposes","photography permit",
)
def _is_project(text):
    t = (text or "").lower()
    if any(d in t for d in _RESEARCH_DENY):
        return False
    return any(a in t for a in _PROJECT_ALLOW)

def fetch_federal_register(days=45, per_page=100):
    """EIS / NEPA notices from the Federal Register API (free, no key).
    No coordinates in the data, so each is geocoded to its STATE centroid
    (approximate) and only when a single state is unambiguously named."""
    out = []
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    q = {"conditions[term]": "environmental impact statement",
         "conditions[type][]": "NOTICE",
         "conditions[publication_date][gte]": since,
         "per_page": per_page, "order": "newest",
         "fields[]": ["title", "abstract", "agencies", "publication_date", "html_url",
                      "comments_close_on"]}
    # urlencode with repeated keys for the list fields
    parts = []
    for k, v in q.items():
        if isinstance(v, list):
            for item in v: parts.append((k, item))
        else:
            parts.append((k, v))
    url = "https://www.federalregister.gov/api/v1/documents.json?" + urllib.parse.urlencode(parts)
    try:
        data = _get_json(url)
    except Exception as e:
        print("  federal register failed: %s" % e); return out
    jitter = 0.0
    for d in data.get("results", []):
        text = " ".join(filter(None, [d.get("title"), d.get("abstract")]))
        if not _is_project(text): continue
        st = _detect_state(text)
        if not st: continue
        pl = _extract_place(text)
        coords = _geocode_place(pl + ", " + st, "us") if pl else None
        if coords:
            lat, lng = coords; precise = True
            note = "Placed from the notice title (" + pl + ")."
        else:
            lat, lng = STATE_CENTROID[st]; jitter += 0.11
            lat = round(lat + (jitter % 0.8) - 0.4, 4)
            lng = round(lng + ((jitter * 1.7) % 0.8) - 0.4, 4)
            precise = False; note = "Placement is state-level/approximate."
        p = {"name": (d.get("title") or "Federal environmental review")[:140],
             "type": _infer_type(text), "state": st,
             "lat": round(lat, 5), "lng": round(lng, 5), "precise": precise,
             "size": "", "status": "In federal review (comment window may be open)",
             "company": "", "url": d.get("html_url"),
             "date": _iso_date(d.get("publication_date")),
             "deadline": _iso_date(d.get("comments_close_on")),
             "desc": "Federal environmental review notice (" + (d.get("publication_date") or "") +
                     "). " + note + " Open the notice for the exact "
                     "location and the public comment deadline.",
             "source": "federal_register"}
        p["impact"] = rate_project(p, sensitivity=1)
        out.append(p)
    return out

# EPA EIS / FERC / EJAtlas: coordinate-bearing sources -- wire when you confirm
# their export endpoints (EJAtlas + Land Matrix carry real lat/lng; GEM ships
# downloadable trackers with coordinates).
def fetch_epa_eis(): return []
def fetch_ferc(): return []
def fetch_ejatlas(path="data/ejatlas.geojson"):
    """Environmental Justice Atlas conflicts (global). EJAtlas has NO public API,
    and its data is CC BY-NC-SA 3.0 -- free for NON-COMMERCIAL use WITH attribution
    to ejatlas.org. Obtain a GeoJSON export (featured-map export or a data request
    to the EJAtlas team) and drop it at data/ejatlas.geojson. Each point is
    published with a mandatory 'Source: EJAtlas (CC BY-NC-SA)' credit in its desc."""
    out = []
    if not os.path.exists(path):
        print("  ejatlas: %s not found (skip) -- see docstring to add it" % path); return out
    try:
        gj = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print("  ejatlas: bad file: %s" % e); return out
    feats = gj.get("features", gj) if isinstance(gj, dict) else gj
    for f in (feats or []):
        try:
            geom = f.get("geometry") or {}
            props = f.get("properties") or {}
            coords = geom.get("coordinates") or []
            if geom.get("type") == "Point" and len(coords) >= 2:
                lng, lat = float(coords[0]), float(coords[1])
            else:
                continue
            nm = (props.get("name") or props.get("Name") or props.get("title") or "EJ conflict")
            out.append({
                "name": str(nm)[:140],
                "type": props.get("category") or props.get("Category") or "Environmental conflict",
                "state": props.get("country") or props.get("Country") or "",
                "lat": round(lat, 5), "lng": round(lng, 5),
                "size": "", "status": props.get("status") or props.get("intensity") or "",
                "company": props.get("company") or props.get("companies") or "",
                "url": props.get("url") or props.get("link") or "https://ejatlas.org/",
                "desc": (str(props.get("description") or props.get("summary") or "")[:240] +
                         " \u2014 Source: EJAtlas (CC BY-NC-SA)."),
                "source": "ejatlas",
            })
        except Exception:
            continue
    return out

# ----------------------------------------------------------------------------
# MERGE + WRITE
# ----------------------------------------------------------------------------
def dedup(items):
    seen, out = set(), []
    for p in items:
        key = (round(p["lat"], 3), round(p["lng"], 3), (p.get("name") or "").strip().lower()[:40])
        if key in seen: continue
        seen.add(key); out.append(p)
    return out

def _run(name, fn):
    """Run one source in isolation so a single failure can't kill the harvest."""
    try:
        got = fn() or []
        print("  %-18s %d" % (name + ":", len(got)))
        return got
    except Exception as e:
        print("  %-18s FAILED: %s" % (name + ":", e))
        return []


# ---------------------------------------------------------------------------
# PermitStack -- national building/development permits (free tier, needs key).
# Docs: api.permit-stack.com/docs ; auth via X-API-Key ; permits carry lat/lng.
# Set PERMITSTACK_API_KEY as a GitHub Actions secret. Confirmed fields:
# address, permit_number, category, contractor_name, estimated_value,
# date_issued, latitude, longitude, city, state.
# ---------------------------------------------------------------------------
PERMITSTACK_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
]

def _ps_get(o, k):
    return (o.get(k) if isinstance(o, dict) else getattr(o, k, None))

def fetch_permitstack(min_value=1000000, per_state_cap=500):
    key = os.environ.get("PERMITSTACK_API_KEY")
    if not key:
        print("  permitstack: no PERMITSTACK_API_KEY set (skip)"); return []
    try:
        from permitstack import Permitstack
    except Exception:
        print("  permitstack: SDK missing (pip install permitstack) (skip)"); return []
    try:
        client = Permitstack(api_key=key)
    except Exception as e:
        print("  permitstack: init failed: %s" % e); return []
    out = []
    HIGH_VOLUME = {"CA","TX","FL","NY","IL","PA","OH","GA","NC","AZ","WA","CO","VA","NJ",
                   "MA","TN","MD","MI","MN","OR","IN","MO","WI","SC","UT","NV"}
    BUDGET = 99                      # free plan: 100 requests/day -- use almost all of it
    def _val(r):
        try: return float(_ps_get(r, "estimated_value") or 0)
        except Exception: return 0.0
    def _page(st, pg):
        kw = {"state": st, "category": "new_construction", "min_value": min_value}
        if pg > 1: kw["page"] = pg
        res = client.permits.search_permits(**kw)
        return _ps_get(res, "results") or (res if isinstance(res, list) else []) or []
    rows_by_state = {st: [] for st in PERMITSTACK_STATES}
    reqs = 0
    # pass 1: page 1 for every state
    for st in PERMITSTACK_STATES:
        if reqs >= BUDGET: break
        try: rows_by_state[st] += list(_page(st, 1))
        except Exception as e: print("  permitstack %s p1: %s" % (st, e))
        reqs += 1; time.sleep(2.2)
    # pass 2: page 2, high-volume states first, until the daily budget is spent
    for st in sorted(PERMITSTACK_STATES, key=lambda s: 0 if s in HIGH_VOLUME else 1):
        if reqs >= BUDGET: break
        try:
            more = list(_page(st, 2))
            if more: rows_by_state[st] += more
        except Exception:
            pass   # page param unsupported / no more pages -- skip quietly
        reqs += 1; time.sleep(2.2)
    print("  permitstack: used %d/%d daily requests" % (reqs, BUDGET))
    # build items: biggest-value first, capped per state
    for st, rows in rows_by_state.items():
        rows.sort(key=_val, reverse=True)
        n = 0
        for r in rows:
            if n >= per_state_cap: break
            lat = _ps_get(r, "latitude"); lng = _ps_get(r, "longitude")
            if lat is None or lng is None: continue
            n += 1
            val = _ps_get(r, "estimated_value") or 0
            addr = _ps_get(r, "address") or ""
            nm = (addr or _ps_get(r, "category") or "New construction")
            try: size = "$%s" % format(int(val), ",") if val else ""
            except Exception: size = ""
            out.append({
                "name": str(nm)[:140], "type": "New construction",
                "state": _ps_get(r, "state") or "",
                "lat": round(float(lat), 5), "lng": round(float(lng), 5),
                "size": size, "status": "Permit on file",
                "company": _ps_get(r, "contractor_name") or "", "url": "",
                "date": _iso_date(_ps_get(r, "date_issued")),
                "desc": ("Building permit" + (" \u00b7 " + addr if addr else "") +
                         (" \u00b7 issued " + str(_ps_get(r, "date_issued"))
                          if _ps_get(r, "date_issued") else "") + "."),
                "source": "permitstack",
            })
    return out

# ---------------------------------------------------------------------------
# BLM + U.S. Forest Service NEPA actions on PUBLIC LAND, via the Federal
# Register API filtered by agency (free, no key). State-centroid geocode.
# ---------------------------------------------------------------------------

def _best_name(props, keys=()):
    """Pick a real project name from an unknown ArcGIS schema: try known keys,
    then fall back to the longest human-looking string in the record."""
    up = {str(k).upper(): v for k, v in (props or {}).items()}
    for k in keys:
        v = up.get(k)
        if isinstance(v, str) and len(v.strip()) > 3:
            return v.strip()
    cands = []
    for k, v in (props or {}).items():
        if not isinstance(v, str): continue
        s = v.strip()
        if len(s) < 8 or len(s) > 220: continue
        if s.lower().startswith("http"): continue
        if re.match(r"^\d{4}-\d{2}-\d{2}", s): continue
        if re.match(r"^[A-Z0-9_\-]{2,12}$", s): continue     # looks like a code
        if " " not in s: continue                             # single token -> likely a code
        cands.append(s)
    return max(cands, key=len) if cands else None

# ---- structured sector classification (fixes vague titles) ------------------
_WB_BUILD_SECTOR = ("transportation", "transport", "energy", "extractive", "water",
                    "sanitation", "waste", "urban", "mining", "construction",
                    "industry", "irrigation")
_WB_PROG_SECTOR = ("public administration", "education", "health", "financial",
                   "social protection", "information and communication")
# OECD DAC sector codes that mean physical works
_IATI_BUILD_PREFIX = ("140", "21", "23", "322", "323", "43030", "31140")
_IATI_POLICY_CODE = ("14010", "21010", "23110", "23010", "41010")

def _sector_is_build(sector_text):
    s = str(sector_text or "").lower()
    if not s: return None                       # unknown -> caller falls back to title
    if any(w in s for w in _WB_PROG_SECTOR): return False
    if any(w in s for w in _WB_BUILD_SECTOR): return True
    return False

def _dac_is_build(code):
    c = str(code or "").strip()
    if not c.isdigit(): return None             # unknown -> caller falls back to title
    if c in _IATI_POLICY_CODE: return False
    return any(c.startswith(pfx) for pfx in _IATI_BUILD_PREFIX)

def _iati_sector_code(a):
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if "sector" in str(k).lower():
                    if isinstance(v, dict):
                        c = v.get("code") or v.get("@code")
                        if c: return str(c)
                    if isinstance(v, list):
                        for it in v:
                            if isinstance(it, dict):
                                c = it.get("code") or it.get("@code")
                                if c: return str(c)
                    if isinstance(v, (str, int)): return str(v)
            for v in o.values():
                r = walk(v)
                if r: return r
        elif isinstance(o, list):
            for it in o:
                r = walk(it)
                if r: return r
        return None
    return walk(a)

_GEO_CACHE = {}
_GEO_CALLS = [0]
_GEO_MAX = 90   # Nominatim politeness budget per run (1 req/sec)
_PLACE_RE = re.compile(
    r"([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,3}\s+"
    r"(?:County|Parish|Borough|City|Township|District|Province|Governorate|Prefecture|"
    r"Municipality|Reservation|Field Office|Ranger District|Wilderness|"
    r"National\s+(?:Forests?|Grasslands?|Park|Monument|Preserve|Recreation Area)))\b")

def _extract_place(text):
    """Pull a specific place phrase out of a title/name if one is present."""
    m = _PLACE_RE.search(text or "")
    return m.group(1).strip() if m else None
_FOREST_RE = re.compile(
    r"([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,4}\s+"
    r"National\s+(?:Forests?|Grasslands?|Recreation Area|Monument|Preserve))")

def _geocode_place(q, cc="us"):
    """Best-effort geocode of a named place via OpenStreetMap Nominatim (free).
    cc biases to a country (ISO2, or None for worldwide). Honors the 1 req/sec
    policy and a per-run call budget. Returns (lat, lng) or None."""
    if not q:
        return None
    key = (q, cc)
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    if _GEO_CALLS[0] >= _GEO_MAX:
        return None
    res = None
    try:
        params = {"q": q, "format": "json", "limit": 1}
        if cc: params["countrycodes"] = cc
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": "activist-project-map/1.0 (wheelock.chris@gmail.com)"})
        _GEO_CALLS[0] += 1
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            arr = json.loads(r.read().decode("utf-8", "replace"))
        if arr:
            res = (float(arr[0]["lat"]), float(arr[0]["lon"]))
        time.sleep(1.1)
    except Exception:
        res = None
    _GEO_CACHE[key] = res
    return res

def fetch_public_land_nepa(days=60, per_page=100):
    out = []
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    for mode, val, label in [("term", "bureau of land management", "BLM"),
                             ("agency", "forest-service", "USFS")]:
        q = {"conditions[type][]": "NOTICE",
             "conditions[publication_date][gte]": since,
             "per_page": per_page, "order": "newest",
             "fields[]": ["title", "abstract", "agencies", "publication_date", "html_url",
                          "comments_close_on"]}
        if mode == "agency":
            q["conditions[agencies][]"] = val
        else:
            q["conditions[term]"] = val
        parts = []
        for k, v in q.items():
            if isinstance(v, list):
                for it in v: parts.append((k, it))
            else: parts.append((k, v))
        url = ("https://www.federalregister.gov/api/v1/documents.json?" +
               urllib.parse.urlencode(parts))
        try:
            data = _get_json(url)
        except Exception as e:
            print("  public-land %s failed: %s" % (label, e)); continue
        jitter = 0.0
        for d in data.get("results", []):
            text = " ".join(filter(None, [d.get("title"), d.get("abstract")]))
            if not _is_project(text): continue
            st = _detect_state(text)
            # try to place it on the named national forest/grassland (local),
            # else fall back to the state centroid (approximate).
            fm = _FOREST_RE.search(text)
            forest = fm.group(1).strip() if fm else None
            coords = _geocode_place(forest) if forest else None
            if coords:
                lat, lng = coords
                place_note = "Placed on " + forest + "."
            elif st:
                lat, lng = STATE_CENTROID[st]
                jitter += 0.13
                lat = round(lat + (jitter % 0.8) - 0.4, 4)
                lng = round(lng + ((jitter * 1.7) % 0.8) - 0.4, 4)
                place_note = "State-level placement; open the notice for the exact site."
            else:
                continue
            p = {"name": (d.get("title") or (label + " public-land action"))[:140],
                 "type": _infer_type(text), "state": st or "",
                 "lat": round(lat, 5), "lng": round(lng, 5),
                 "size": "", "status": "Public land \u2014 " + label + " NEPA review",
                 "company": "", "url": d.get("html_url"),
                 "date": _iso_date(d.get("publication_date")),
                 "deadline": _iso_date(d.get("comments_close_on")),
                 "desc": (label + " action on public land (" +
                          (d.get("publication_date") or "") + "). " + place_note +
                          " Open the notice for the comment deadline."),
                 "precise": False, "source": "public_land_nepa"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
    return out



# ---------------------------------------------------------------------------
# BLM -- PRECISE project points from BLM's public ArcGIS FeatureServer that
# powers the NEPA Register map (open-comment projects). Real lat/lng, no key.
# gis.blm.gov/arcgis/rest/services/ePlanning/BLM_Natl_Epl_Comment (layer 0).
# ---------------------------------------------------------------------------
def fetch_blm_arcgis():
    base = ("https://gis.blm.gov/arcgis/rest/services/ePlanning/"
            "BLM_Natl_Epl_Comment/FeatureServer/0/query")
    q = urllib.parse.urlencode({"where": "1=1", "outFields": "*", "returnGeometry": "true",
                                "f": "json", "resultRecordCount": "2000"})
    try:
        req = urllib.request.Request(base + "?" + q, headers={
            "User-Agent": "Mozilla/5.0 (compatible; project-map/1.0; +wheelock.chris@gmail.com)",
            "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            gj = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print("  blm arcgis failed: %s" % e); return []
    if isinstance(gj, dict) and gj.get("error"):
        print("  blm arcgis API error: %s" % str(gj.get("error"))[:200]); return []
    _raw = gj.get("features", []) if isinstance(gj, dict) else []
    print("  blm arcgis: %d raw features returned" % len(_raw))
    out = []
    NAME_KEYS = ("PROJECT_NAME", "PROJECT_NA", "PROJECTNAME", "NEPA_PROJECT",
                 "PROJECT", "NAME", "TITLE", "DOC_NAME", "PLAN_NAME")
    for f in gj.get("features", []):
        try:
            geom = f.get("geometry") or {}
            lng = geom.get("x"); lat = geom.get("y")
            if lat is None or lng is None:
                continue
            lng, lat = float(lng), float(lat)
            props = f.get("attributes") or {}
            up = {k.upper(): v for k, v in props.items()}
            nm = next((up[k] for k in NAME_KEYS if up.get(k)), None)
            if not nm:
                strs = [v for v in props.values() if isinstance(v, str) and v.strip()]
                nm = max(strs, key=len) if strs else "BLM NEPA project"
            nepa = next((str(v) for k, v in up.items() if "NEPA" in k and v), None)
            p = {"name": str(nm)[:140], "type": "BLM public-land action", "state": "",
                 "lat": round(lat, 5), "lng": round(lng, 5), "size": "",
                 "status": "Open for comment (BLM NEPA)", "company": "",
                 "url": "https://eplanning.blm.gov/eplanning-ui/home",
                 "desc": ("BLM NEPA project on public land" +
                          ((" \u00b7 " + nepa) if nepa else "") +
                          " \u2014 comment window may be open. Precise location from "
                          "BLM ePlanning."),
                 "source": "blm_arcgis"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    return out



# ---------------------------------------------------------------------------
# World Bank -- ACTIVE financed projects worldwide (free API, no key). GLOBAL.
# Country-level placement via the WB country API centroids (capital coords).
# ---------------------------------------------------------------------------
def _wb_country_centroids():
    cents = {}
    try:
        data = _get_json("https://api.worldbank.org/v2/country?format=json&per_page=400")
        rows = data[1] if isinstance(data, list) and len(data) > 1 else []
        for c in rows:
            try:
                lat = float(c.get("latitude")); lng = float(c.get("longitude"))
            except (TypeError, ValueError):
                continue
            for k in (c.get("iso2Code"), c.get("id"), c.get("name")):
                if k: cents[str(k).upper()] = (lat, lng)
    except Exception as e:
        print("  world bank centroids failed: %s" % e)
    return cents

def fetch_world_bank(rows=1000):
    cents = _wb_country_centroids()
    if not cents:
        print("  world bank: no country centroids (skip)"); return []
    fl = ("id,project_name,countryname,countryshortname,countrycode,totalamt,"
          "totalcommamt,boardapprovaldate,sector1,status,regionname")
    url = ("https://search.worldbank.org/api/v2/projects?format=json"
           "&status_exact=Active&rows=%d&fl=%s" % (rows, urllib.parse.quote(fl)))
    try:
        data = _get_json(url)
    except Exception as e:
        print("  world bank failed: %s" % e); return []
    projs = data.get("projects", data) if isinstance(data, dict) else data
    if isinstance(projs, dict): projs = list(projs.values())
    print("  world bank: %d active projects returned" % (len(projs) if projs else 0))
    out = []; jitter = 0.0
    for pr in (projs or []):
        try:
            if not isinstance(pr, dict): continue
            cc = str(pr.get("countrycode") or "").upper()
            cn = pr.get("countryshortname") or pr.get("countryname") or ""
            ll = cents.get(cc) or cents.get(str(cn).upper())
            if not ll: continue
            lat, lng = ll
            jitter += 0.17
            # try to sharpen from the title (e.g. "Dhaka ... Project" -> geocode Dhaka)
            _secraw = pr.get("sector1")
            if isinstance(_secraw, dict):
                _secraw = _secraw.get("Name") or _secraw.get("name") or ""
            _title = str(pr.get("project_name") or "")
            _sb = _sector_is_build(_secraw)          # True / False / None(unknown)
            if _sb is False:
                continue
            if not _is_hard_build(_title):
                continue
            _pl = _extract_place(pr.get("project_name") or "")
            _cc2 = cc.lower() if len(cc) == 2 else None
            _co = _geocode_place(_pl + ", " + str(cn), _cc2) if _pl else None
            if _co:
                _lat, _lng, _precise = _co[0], _co[1], True
            else:
                _lat = round(lat + (jitter % 1.6) - 0.8, 4)
                _lng = round(lng + ((jitter * 1.7) % 1.6) - 0.8, 4)
                _precise = False
            amt = pr.get("totalamt") or pr.get("totalcommamt") or ""
            try:
                amtf = float(str(amt).replace(",", "")) if amt else 0
                size = ("$%sM" % format(int(amtf), ",")) if amtf else ""
            except Exception:
                size = ""
            sec = pr.get("sector1")
            if isinstance(sec, dict): sec = sec.get("Name") or sec.get("name") or ""
            p = {"name": (pr.get("project_name") or "World Bank project")[:140],
                 "type": (sec or "Development project"),
                 "state": str(cn),
                 "lat": round(_lat, 5), "lng": round(_lng, 5), "precise": _precise,
                 "size": size, "status": str(pr.get("status") or "Active"),
                 "company": "World Bank",
                 "url": ("https://projects.worldbank.org/en/projects-operations/"
                         "project-detail/" + str(pr.get("id") or "")),
                 "date": _iso_date(pr.get("boardapprovaldate")),
                 "desc": ("World Bank-financed project in " + str(cn) +
                          ((" \u00b7 " + size) if size else "") +
                          (". Located from title." if _precise else
                            ". Country-level placement \u2014 open the project page for the exact "
                            "location and status.")),
                 "source": "world_bank"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    return out



# ---------------------------------------------------------------------------
# Physical-build filter for development sources (IATI / World Bank).
# Their portfolios mix PHYSICAL works (roads, dams, plants, pipes) with
# INTANGIBLE programmes (budget support, training, policy loans, GHG targets).
# This keeps the former. Title-based, so it is a heuristic: a project is kept
# only if it names physical works and does not read as a pure programme.
# ---------------------------------------------------------------------------
_BUILD_WORDS = (
    "road", "highway", "expressway", "motorway", "bridge", "tunnel", "corridor",
    "rail", "railway", "metro", "tramway", "port", "harbour", "harbor", "jetty",
    "airport", "runway", "terminal", "dam", "reservoir", "weir", "barrage",
    "irrigation", "canal", "pipeline", "water supply", "waterworks", "borehole",
    "sanitation", "sewer", "sewerage", "wastewater", "drainage", "treatment plant",
    "power plant", "powerplant", "hydropower", "hydroelectric", "geothermal",
    "solar park", "solar plant", "off-grid solar", "wind farm", "wind power",
    "transmission", "substation", "grid", "electrification", "interconnector",
    "refinery", "lng", "gas plant", "mine", "mining", "quarry", "smelter",
    "landfill", "incinerator", "waste facility", "housing", "settlement upgrading",
    "urban development", "urban upgrading", "market construction", "hospital",
    "clinic construction", "school construction", "classroom", "campus",
    "construction", "rehabilitation of", "reconstruction", "upgrading of",
    "rural roads", "feeder road", "bus rapid transit", "brt", "cable car",
    "flood protection", "embankment", "coastal protection", "seawall",
    "storage facility", "warehouse", "silo", "cold chain", "transmission line",
    "water security", "water and sanitation", "sanitation development",
    # broader physical signals
    "solar", "wind", "hydro", "infrastructure", "electricity", "electric power",
    "energy access", "expansion of energy", "power sector", "water supply",
    "water project", "roads", "road project", "transport project", "transport corridor",
    "rural access", "urban mobility", "railway line", "plant", "facility",
    "network expansion", "distribution network", "sewage", "water resources",
    "flood", "drainage", "bridge", "port project", "logistics hub",
)
_PROGRAM_WORDS = (
    "policy financing", "development policy", "dpf", "budget support",
    "cat-ddo", "credit line", "guarantee", "technical assistance",
    "capacity building", "institutional strengthening", "governance",
    "public financial management", "civil service", "statistics", "census",
    "monitoring and evaluation", "jobs and economic", "economic transformation",
    "livelihood", "cash transfer", "social protection", "social safety",
    "income support", "access to finance", "enterprise recovery", "green finance",
    "investment and trade", "trade facilitation", "digital economy",
    "e-government", "carbon abatement", "climate action program",
    "emission reduction", "ghg", "gender", "youth empowerment", "curriculum",
    "equity in learning", "learning outcomes", "health systems", "nutrition",
    "immunization", "devolution support", "service delivery", "resilience program",
    "sector efficiency", "value chain", "financial inclusion", "pension",
)

# ---- STRICT filter for aid/development sources -----------------------------
# Only HARD infrastructure that physically takes land: roads, rail, ports,
# dams, power, pipelines, mines. Deliberately EXCLUDES water-supply/sanitation
# programmes, housing/health/education construction and "rehabilitation" work,
# which are mostly programmatic even when some building happens.
_HARD_BUILD_RE = re.compile(r"\b("
    r"highway|expressway|motorway|ring\s+road|trunk\s+road|rural\s+roads?|feeder\s+roads?|"
    r"road\s+(?:corridor|upgrading|construction|rehabilitation|project|network)|roads?\s+and\s+bridges|"
    r"bridges?|tunnels?|railways?|rail\s+(?:line|corridor|link)|metro\s+rail|light\s+rail|"
    r"bus\s+rapid\s+transit|brt|ports?|harbours?|harbors?|jetty|wharf|quay|"
    r"airports?|runways?|dams?|reservoirs?|barrage|weir|hydro\s?power|hydroelectric|"
    r"irrigation|pipelines?|power\s+plants?|thermal\s+plant|coal\s+plant|gas\s+plant|"
    r"power\s+station|geothermal|solar|photovoltaic|"
    r"wind\s+(?:farm|power\s+plant)|transmission\s+(?:line|network|system)|substations?|"
    r"grid\s+(?:extension|expansion|reinforcement)|interconnector|electrification|"
    r"mines?|mining|quarry|smelter|refinery|lng|coal\s+terminal|oil\s+terminal|"
    r"landfill|incinerator|canals?|hydro"
    r")\b", re.I)
_HARD_DENY_RE = re.compile(r"\b("
    r"water\s+supply|sanitation|sewerage|sewer|wastewater|hygiene|wash|"
    r"rehabilitation|reconstruction|housing|hospitals?|clinics?|schools?|classrooms?|"
    r"education|health|capacity|policy|technical\s+assistance|resilience|livelihoods?|"
    r"institutional|governance|training|scholarship|programme\s+support|program\s+support|"
    r"sector\s+support|budget\s+support|master.s\s+degree|reporting|transparency|"
    r"employment|procurement|single\s+window|feasibility|study|studies|strengthening|management|promotion|promoting|securing|awareness|assessment|monitoring|"r"indicator|transit\s+times|preparation|advisory|planning|design\s+of|consultancy|supervision|trade|exchange|corridors\s+and"
    r")\b", re.I)

def _is_hard_build(text):
    """Only HARD, land-taking infrastructure: roads, rail, ports, dams, power,
    pipelines, mines. Word-boundary matched -- 'Support' must never match 'port'."""
    t = text or ""
    if _HARD_DENY_RE.search(t): return False
    return bool(_HARD_BUILD_RE.search(t))

def _is_build(text):
    """True if a development project title reads as PHYSICAL construction."""
    t = (text or "").lower()
    if not any(b in t for b in _BUILD_WORDS):
        return False
    # a physical word can still sit inside a pure programme title; require that
    # the title is not dominated by programme language
    prog_hits = sum(1 for w in _PROGRAM_WORDS if w in t)
    build_hits = sum(1 for b in _BUILD_WORDS if b in t)
    return build_hits >= prog_hits

# ---------------------------------------------------------------------------
# IATI (Code for IATI mirror) -- global development activities WITH real
# coordinates (free, no key). We keep only activities that carry a location
# so this adds PRECISE global points, complementing World Bank's country dots.
# ---------------------------------------------------------------------------
def _iati_find(obj, want):
    """Recursively find the first value whose key ends with `want`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.split(".")[-1].split("}")[-1].lower() == want:
                if isinstance(v, (str, int, float)): return v
                if isinstance(v, list) and v and isinstance(v[0], (str, int, float)): return v[0]
        for v in obj.values():
            r = _iati_find(v, want)
            if r is not None: return r
    elif isinstance(obj, list):
        for it in obj:
            r = _iati_find(it, want)
            if r is not None: return r
    return None

def _iati_pos(a):
    v = _iati_find(a, "pos")
    if isinstance(v, str):
        parts = v.replace(",", " ").split()
        if len(parts) >= 2:
            try:
                lat, lng = float(parts[0]), float(parts[1])
                if -90 <= lat <= 90 and -180 <= lng <= 180 and (lat or lng):
                    return (lat, lng)
            except Exception:
                pass
    return None

def _iati_activities(data):
    if isinstance(data, list): return data
    if isinstance(data, dict):
        for k in ("iati-activity", "iati_activity", "activities", "activity",
                  "results", "response", "docs", "result"):
            v = data.get(k)
            if isinstance(v, list): return v
            if isinstance(v, dict):
                for kk in ("iati-activity", "activities", "docs"):
                    if isinstance(v.get(kk), list): return v[kk]
        # deep fallback: first list of dicts anywhere
        def firstlist(o):
            if isinstance(o, list) and o and isinstance(o[0], dict): return o
            if isinstance(o, dict):
                for vv in o.values():
                    r = firstlist(vv)
                    if r: return r
            return None
        return firstlist(data) or []
    return []

# recipient countries with lots of geocoded development activity
_IATI_COUNTRIES = [
    # Africa (AfDB region + bilateral donors)
    "DZ","AO","BJ","BW","BF","BI","CM","CV","CF","TD","KM","CG","CD","CI","DJ","EG",
    "GQ","ER","SZ","ET","GA","GM","GH","GN","GW","KE","LS","LR","LY","MG","MW","ML",
    "MR","MU","MA","MZ","NA","NE","NG","RW","ST","SN","SC","SL","SO","ZA","SS","SD",
    "TZ","TG","TN","UG","ZM","ZW",
    # Asia & Pacific (ADB region)
    "AF","AM","AZ","BD","BT","KH","CN","FJ","GE","IN","ID","KZ","KI","KG","LA","MY",
    "MV","MH","FM","MN","MM","NR","NP","PK","PW","PG","PH","WS","SB","LK","TJ","TH",
    "TL","TO","TM","TV","UZ","VU","VN",
    # Latin America & Caribbean (IDB region)
    "AR","BZ","BO","BR","CL","CO","CR","CU","DM","DO","EC","SV","GD","GT","GY","HT",
    "HN","JM","MX","NI","PA","PY","PE","LC","VC","SR","TT","UY","VE",
    # Middle East, Europe & Central Asia (EBRD / EIB neighbourhood)
    "AL","BA","IQ","JO","LB","MD","ME","MK","PS","RS","SY","TR","UA","XK","YE","IR",
]

def fetch_iati(per=1000):
    base = "https://datastore.codeforiati.org/api/1/access/activity.json"
    out = []; scanned = 0; withloc = 0
    for cc in _IATI_COUNTRIES:
        params = {"recipient-country": cc, "activity-status": "2",
                  "limit": per, "offset": 0, "unwrap": "True"}
        try:
            data = _get_json(base + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            print("  iati %s failed: %s" % (cc, e)); continue
        if scanned == 0 and cc == _IATI_COUNTRIES[0]:
            if isinstance(data, dict):
                print("  iati [shape] dict keys: %s" % list(data.keys())[:8])
            else:
                print("  iati [shape] type: %s len: %s" % (type(data).__name__, len(data) if hasattr(data,"__len__") else "?"))
        acts = _iati_activities(data)
        if not acts:
            time.sleep(0.3); continue
        for a in acts:
            scanned += 1
            ll = _iati_pos(a)
            if not ll: continue
            nm = _iati_find(a, "narrative") or _iati_find(a, "title") or "Development activity"
            _db = _dac_is_build(_iati_sector_code(a))    # True / False / None(unknown)
            if _db is False:
                continue                                  # sector says programme
            if not _is_hard_build(str(nm)):
                continue                                  # title must name hard infrastructure
            withloc += 1
            cn = _iati_find(a, "recipient-country") or _iati_find(a, "code") or ""
            org = _iati_find(a, "reporting-org") or _iati_find(a, "narrative") or ""
            p = {"name": str(nm)[:140], "type": "Development / aid project",
                 "state": str(cn), "lat": round(ll[0], 5), "lng": round(ll[1], 5),
                 "precise": True, "size": "", "status": "Active",
                 "company": str(org)[:80],
                 "url": "https://d-portal.org/q.html?aid=" + str(_iati_find(a, "iati-identifier") or ""),
                 "desc": "Development/aid project (IATI). Reported location.",
                 "source": "iati"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        time.sleep(0.4)
    print("  iati: scanned %d active activities across %d countries, %d had coordinates"
          % (scanned, len(_IATI_COUNTRIES), withloc))
    return out


# ---------------------------------------------------------------------------
# ArcGIS Hub -- direct discovery of city/county building-permit datasets
# (free, NO key, NO daily cap). Conservative: only keeps permits from datasets
# that expose a valuation field, filtered to significant value, so it can never
# flood the map with tiny permits. Complements PermitStack's breadth.
# ---------------------------------------------------------------------------
_HUB_VAL_RE = re.compile(r"(valuation|est.?value|job.?value|construction.?cost|"
                         r"total.?value|declared.?value|permit.?value|est.?cost|"
                         r"^value$|^cost$|^amount$|projectcost|jobvalue)", re.I)
_HUB_NAME_RE = re.compile(r"(work.?desc|description|permit.?type|type.?desc|"
                          r"scope|project.?name|proposed.?use|permit.?class)", re.I)

# ArcGIS Hub is a GLOBAL platform: thousands of city/region open-data portals in
# many countries publish permit layers to it. Searching in several languages is
# how we compile SUBNATIONAL data into coverage for countries that have no
# national register (Germany, Spain, Italy, Chile, Japan, Poland...).
_HUB_QUERIES = [
    ("building permits", "permit"), ("construction permits", "permit"),
    ("development applications", "development"), ("planning applications", "planning"),
    ("building approvals", "approval"), ("permis de construire", "permis"),
    ("licencia de construccion", "licencia"), ("licencias urbanisticas", "licencia"),
    ("baugenehmigung", "bau"), ("bouwvergunning", "vergunning"),
    ("permesso di costruire", "permesso"), ("alvara de construcao", "alvar"),
    ("pozwolenie na budowe", "pozwolenie"), ("byggetillatelse", "bygge"),
    ("bygglov", "bygglov"), ("rakennuslupa", "rakennus"),
    ("development permits", "permit"), ("zoning applications", "zoning"),
]

# ---- significance gate for permit feeds -----------------------------------
# A patio, re-roof or kitchen remodel on someone's home has no community or
# environmental impact, and putting it on a public map is an intrusion into
# private life rather than accountability. Keep permits that could plausibly
# affect the surrounding community/environment: large money, or a project type
# that is inherently significant.
_TRIVIAL_RE = re.compile(
    r"(remodel|interior|alteration|renovat|repair|re-?roof|roofing|patio|deck\b|"
    r"fence|shed\b|garage|carport|driveway|swimming\s*pool|spa\b|hot\s*tub|"
    r"water\s*heater|furnace|hvac|air\s*condition|plumbing|electrical\s*(?:permit|only)|"
    r"\bsign\b|awning|window|siding|stucco|sprinkler|irrigation\s*system|"
    r"kitchen|bathroom|basement|deck\s*addition|accessory\s*dwelling|\badu\b|"
    r"mechanical|gas\s*line|water\s*line|fire\s*alarm|sprinkler\s*system|"
    r"single\s*family\s*(?:residence|dwelling|addition)|sfr\b|res\s*addition|"
    r"tenant\s*improvement|fit-?out|handrail|retaining\s*wall|solar\s*panel|"
    r"reroof|demolition\s*of\s*(?:garage|shed|deck))", re.I)
_SIGNIF_RE = re.compile(
    r"(new\s*construction|new\s*building|commercial|industrial|multi-?family|"
    r"apartment|condominium|subdivision|warehouse|distribution\s*cent|data\s*cent|"
    r"hotel|mixed\s*use|tower|high-?rise|manufacturing|factory|plant\b|refinery|"
    r"\bmine\b|mining|quarry|pipeline|substation|transmission|hospital|school|"
    r"university|stadium|arena|shopping\s*cent|mall\b|retail\s*cent|office\s*building|"
    r"parking\s*(?:structure|garage)|bridge|roadway|highway|rail|port\b|terminal|"
    r"landfill|solar\s*(?:farm|field)|wind\s*farm|utility|infrastructure|"
    r"master\s*plan|planned\s*(?:unit|development)|campus|logistics|storage\s*facility)", re.I)

def _permit_is_significant(text, value, big=5000000, floor=1000000):
    """Would this plausibly affect the surrounding community or environment?"""
    t = str(text or "")
    if value is not None and value >= big:
        return True                       # very large spend: significant whatever it's called
    if _TRIVIAL_RE.search(t):
        return False                      # private / cosmetic work
    if value is not None:
        return value >= floor
    return bool(_SIGNIF_RE.search(t))     # no value published: type must be significant

def fetch_arcgis_hub(max_datasets=400, min_value=1000000, per_ds=2000):
    ds = []; seen_ds = set()
    for q, kw in _HUB_QUERIES:
        for pg in range(1, 4):          # paginate the catalogue, not just page 1
            try:
                surl = "https://opendata.arcgis.com/api/v3/datasets?" + urllib.parse.urlencode({
                    "q": q, "page[size]": "100", "page[number]": pg})
                sdata = _get_json(surl)
            except Exception as e:
                if pg == 1: print("  arcgis hub search '%s' failed: %s" % (q, e))
                break
            rows = sdata.get("data", []) if isinstance(sdata, dict) else []
            if not rows: break
            for d in rows:
                nm = str((d.get("attributes") or {}).get("name", "")).lower()
                did = d.get("id")
                if did in seen_ds: continue
                if kw not in nm: continue
                seen_ds.add(did); ds.append(d)
            time.sleep(0.25)
    print("  arcgis hub: %d permit datasets discovered across %d queries"
          % (len(ds), len(_HUB_QUERIES)))
    out = []; used = 0
    for d in ds[:max_datasets]:
        attrs = d.get("attributes") or {}
        url = attrs.get("url")
        if not url or "/FeatureServer" not in url and "/MapServer" not in url:
            continue
        # find a valuation field from the layer metadata so we can ask the server
        # for the BIGGEST projects first instead of an arbitrary slice
        order = None
        try:
            meta = _get_json(url.rstrip("/") + "?f=json")
            for fdef in (meta or {}).get("fields", []) or []:
                fn = str(fdef.get("name") or "")
                ft = str(fdef.get("type") or "")
                if _HUB_VAL_RE.search(fn) and ("Double" in ft or "Integer" in ft or "Single" in ft):
                    order = fn; break
        except Exception:
            pass
        try:
            params = {"where": "1=1", "outFields": "*", "f": "geojson",
                      "outSR": "4326", "resultRecordCount": per_ds}
            if order:
                params["orderByFields"] = order + " DESC"
                params["where"] = "%s > %d" % (order, min_value)
            q = url.rstrip("/") + "/query?" + urllib.parse.urlencode(params)
            gj = _get_json(q)
        except Exception:
            continue
        used += 1
        no_val_kept = 0   # cap value-less records per dataset so they can't flood
        for f in (gj.get("features") or []):
            try:
                geom = f.get("geometry") or {}; c = geom.get("coordinates") or []
                if geom.get("type") != "Point" or len(c) < 2:
                    continue
                lng, lat = float(c[0]), float(c[1])
                props = f.get("properties") or {}
                val = None
                for k, v in props.items():
                    if _HUB_VAL_RE.search(str(k)) and isinstance(v, (int, float)) and v > 0:
                        val = float(v); break
                nm = None
                for k, v in props.items():
                    if _HUB_NAME_RE.search(str(k)) and isinstance(v, str) and v.strip():
                        nm = v; break
                # judge on the permit's own words + value, so patios and re-roofs
                # on private homes never reach the map
                blob = " ".join([str(nm or ""), str(attrs.get("name") or "")])
                if not _permit_is_significant(blob, val, floor=min_value):
                    continue
                if val is None:
                    if no_val_kept >= 400: continue
                    no_val_kept += 1
                size = ("$%s" % format(int(val), ",")) if val else ""
                _dt = None
                for k, v in props.items():
                    if re.search(r"(issue|appl|file|submit|date|created)", str(k), re.I):
                        _dt = _iso_date(v)
                        if _dt: break
                p = {"name": str(nm or attrs.get("name") or "Permitted project")[:140],
                     "type": "New construction", "state": "", "date": _dt,
                     "lat": round(lat, 5), "lng": round(lng, 5), "precise": True,
                     "size": size,
                     "status": "Permit on file", "company": "",
                     "url": "https://hub.arcgis.com/datasets/" + str(d.get("id") or ""),
                     "desc": "Building permit via " + str(attrs.get("name") or "city open data") + ".",
                     "source": "arcgis_hub"}
                p["impact"] = rate_project(p, sensitivity=0)
                out.append(p)
            except Exception:
                continue
        time.sleep(0.4)
    print("  arcgis hub: queried %d datasets, %d significant permits" % (used, len(out)))
    return out


# ---------------------------------------------------------------------------
# UK PlanIt -- national aggregator of UK planning applications (free, NO key).
# GeoJSON API with real coordinates: a UK analogue to PermitStack.
# ---------------------------------------------------------------------------
_UK_TRIVIAL_RE = re.compile(
    r"(signage|shopfront|shop\s*front|fascia|advertisement|extension|conservatory|"
    r"garage|porch|dormer|loft\s*conversion|outbuilding|garden|fence|wall\b|gate\b|"
    r"decking|patio|driveway|hardstanding|summer\s*house|shed\b|"
    r"internal\s*alteration|wc\b|toilet|window|door|roof\s*light|rooflight|"
    r"tree\s*works|fell|prune|pollard|hedge|crown\s*(?:lift|reduc|thin)|sales\s*board|advertisement\s*board|discharge\s*of\s*condition|"
    r"non-?material\s*amendment|certificate\s*of\s*lawful|prior\s*approval\s*for\s*"
    r"(?:larger|single)|change\s*of\s*use\s*of\s*(?:garage|outbuilding)|"
    r"repair\s*works|replacement\s*of\s*(?:windows|doors|nets)|cricket|"
    r"pole\b|cabinet|solar\s*panel|flue|boiler|satellite)", re.I)

def fetch_ukplanit(days=90, pg_sz=200):
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    today = datetime.date.today().isoformat()
    # PlanIt is built for local queries -- a country-sized bbox returns nothing.
    # Tile Great Britain into ~1.5-degree boxes and gather each.
    tiles = []
    for lat0 in (50.0, 51.5, 53.0, 54.5, 56.0, 57.5):
        for lng0 in (-6.0, -4.5, -3.0, -1.5, 0.0):
            tiles.append("%s,%s,%s,%s" % (lng0, lat0, lng0 + 1.5, lat0 + 1.5))
    feats = []; errs = 0
    for bb in tiles:
        params = {"bbox": bb, "start_date": since, "end_date": today,
                  "pg_sz": pg_sz, "limit": pg_sz}
        url = "https://www.planit.org.uk/api/applics/geojson?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; project-map/1.0; +wheelock.chris@gmail.com)",
                "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                gj = json.loads(r.read().decode("utf-8", "replace"))
            feats += gj.get("features", []) if isinstance(gj, dict) else []
        except Exception as e:
            errs += 1
            if errs == 1: print("  uk planit tile error: %s" % e)
        time.sleep(0.4)
    print("  uk planit: %d applications across %d tiles (%d tile errors)" % (len(feats), len(tiles), errs))
    out = []; skipped = 0
    for f in feats:
        try:
            geom = f.get("geometry") or {}; c = geom.get("coordinates") or []
            if geom.get("type") != "Point" or len(c) < 2: continue
            lng, lat = float(c[0]), float(c[1])
            pr = f.get("properties") or {}
            desc = pr.get("description") or "Planning application"
            addr = pr.get("address") or ""
            state = pr.get("app_state") or ""
            # PlanIt returns every application including householder work --
            # extensions, signage, garden walls, tree consents. Keep only those
            # that could plausibly affect the surrounding community/environment.
            sz = str(pr.get("app_size") or "").strip().lower()
            ty = str(pr.get("app_type") or "").strip().lower()
            if ty in ("trees", "conditions", "amendment", "advertising", "heritage",
                      "telecoms", "other"):
                skipped += 1; continue
            if sz == "small":                      # householder / minor works
                skipped += 1; continue
            if not sz and not _permit_is_significant(str(desc), None):
                skipped += 1; continue
            if _UK_TRIVIAL_RE.search(str(desc)):
                skipped += 1; continue
            p = {"name": str(desc)[:140], "type": "Development (UK planning)",
                 "state": pr.get("authority_name") or "United Kingdom",
                 "lat": round(lat, 5), "lng": round(lng, 5), "precise": True,
                 "size": pr.get("app_size") or "", "status": state, "company": "",
                 "url": pr.get("link") or "https://planit.org.uk/",
                 "date": _iso_date(pr.get("start_date") or pr.get("date_received")),
                 "desc": ("UK planning application" + ((" (" + state + ")") if state else "") +
                          ((" \u00b7 " + addr) if addr else "") + "."),
                 "source": "uk_planit"}
            p["impact"] = rate_project(p, sensitivity=0)
            out.append(p)
        except Exception:
            continue
    print("  uk planit: %d significant applications (%d householder/minor skipped)"
          % (len(out), skipped))
    return out


# ---------------------------------------------------------------------------
# Australia -- EPBC Act referrals (national environmental assessments), a
# public ArcGIS feature service (CC BY, weekly). Referrals are areas, so we
# place each at its centroid. Free, no key. fed.dcceew.gov.au
# ---------------------------------------------------------------------------
def _geom_center(geom):
    t = geom.get("type"); c = geom.get("coordinates")
    if not c: return None
    if t == "Point" and len(c) >= 2:
        try: return (float(c[1]), float(c[0]))
        except Exception: return None
    pts = []
    def collect(x):
        if isinstance(x, (list, tuple)):
            if len(x) >= 2 and isinstance(x[0], (int, float)) and isinstance(x[1], (int, float)):
                pts.append((float(x[1]), float(x[0])))
            else:
                for i in x: collect(i)
    collect(c)
    if not pts: return None
    return (sum(a for a, _ in pts) / len(pts), sum(b for _, b in pts) / len(pts))

def _arcgis_query_all(base_url, layer=0, page=2000, max_pages=20, label=""):
    """Query an ArcGIS layer with resultOffset paging -- returns ALL features."""
    feats = []
    for pg in range(max_pages):
        q = base_url.rstrip("/") + "/%d/query?" % layer + urllib.parse.urlencode({
            "where": "1=1", "outFields": "*", "f": "geojson", "outSR": "4326",
            "resultRecordCount": page, "resultOffset": pg * page})
        try:
            gj = _get_json(q)
        except Exception as e:
            if pg == 0: print("  %s query failed: %s" % (label, e))
            break
        if not isinstance(gj, dict) or gj.get("error"):
            if pg == 0 and isinstance(gj, dict):
                print("  %s error: %s" % (label, str(gj.get("error"))[:120]))
            break
        got = gj.get("features") or []
        feats += got
        if len(got) < page: break          # last page
        time.sleep(0.4)
    return feats

def _arcgis_item_url(item_id):
    try:
        meta = _get_json("https://www.arcgis.com/sharing/rest/content/items/%s?f=json" % item_id)
        return (meta or {}).get("url")
    except Exception as e:
        print("  arcgis item lookup failed: %s" % e); return None

# plausible extent per national source -- drops records whose coordinates are
# clearly wrong (bad source data), while KEEPING legitimate external territories.
_SRC_BOX = {
    # (south, north, west, east)
    "iaac_ca": (41.0, 84.0, -141.5, -52.0),          # Canada (no overseas territory)
    "epbc_au": (-90.0, -8.0, 44.0, 170.0),           # Australia + Antarctic/Indian/Pacific territories
}

# Fallback placement when a record's coordinates are clearly wrong: put it at the
# province/national centroid and flag it approximate (dashed ring) rather than
# deleting it -- the project is real, only its coordinates are unusable.
_CA_PROV = {
    "ALBERTA": (55.0, -115.0), "AB": (55.0, -115.0),
    "BRITISH COLUMBIA": (54.0, -125.0), "BC": (54.0, -125.0),
    "MANITOBA": (55.0, -97.0), "MB": (55.0, -97.0),
    "NEW BRUNSWICK": (46.5, -66.0), "NB": (46.5, -66.0),
    "NEWFOUNDLAND AND LABRADOR": (53.0, -60.0), "NEWFOUNDLAND": (53.0, -60.0), "NL": (53.0, -60.0),
    "NOVA SCOTIA": (45.0, -63.0), "NS": (45.0, -63.0),
    "NORTHWEST TERRITORIES": (64.0, -119.0), "NT": (64.0, -119.0),
    "NUNAVUT": (70.0, -90.0), "NU": (70.0, -90.0),
    "ONTARIO": (50.0, -85.0), "ON": (50.0, -85.0),
    "PRINCE EDWARD ISLAND": (46.4, -63.2), "PE": (46.4, -63.2),
    "QUEBEC": (52.0, -72.0), "QUÉBEC": (52.0, -72.0), "QC": (52.0, -72.0),
    "SASKATCHEWAN": (54.0, -106.0), "SK": (54.0, -106.0),
    "YUKON": (63.0, -135.0), "YT": (63.0, -135.0),
}
_NAT_CENTER = {"iaac_ca": (56.13, -106.35), "epbc_au": (-25.27, 133.78)}

def _fallback_center(src, region_text):
    """(lat, lng, label) for approximate placement, or None."""
    rt = str(region_text or "").strip().upper()
    if src == "iaac_ca" and rt:
        # exact 2-letter code match first (never substring: "NT" is inside "ONTARIO")
        if len(rt) == 2 and rt in _CA_PROV:
            v = _CA_PROV[rt]
            return (v[0], v[1], rt)
        # then full names, longest first so "NEWFOUNDLAND AND LABRADOR" wins
        for k in sorted([k for k in _CA_PROV if len(k) > 2], key=len, reverse=True):
            if k in rt:
                v = _CA_PROV[k]
                return (v[0], v[1], k.title())
    c = _NAT_CENTER.get(src)
    if c:
        return (c[0], c[1], "national")
    return None

def _box_ok(src, lat, lng):
    b = _SRC_BOX.get(src)
    if not b: return True
    s, n, w, e = b
    return (lat is not None and lng is not None and s <= lat <= n and w <= lng <= e)

def fetch_epbc_au():
    url = _arcgis_item_url("ee02ed7773d44c6fa799bf558c70f81a")
    if not url:
        print("  epbc au: could not resolve service url"); return []
    feats = _arcgis_query_all(url, label="epbc au")
    out = []; dropped = 0
    for f in feats:
        try:
            ll = _geom_center(f.get("geometry") or {})
            if not ll: continue
            _au_approx = False
            if not _box_ok("epbc_au", ll[0], ll[1]):
                fb = _fallback_center("epbc_au", "")
                if not fb:
                    dropped += 1; continue
                ll = (fb[0], fb[1]); _au_approx = True; dropped += 1
            pr = f.get("properties") or {}
            up = {str(k).upper(): v for k, v in pr.items()}
            nm = _best_name(pr, ("TITLE", "REFERRAL_TITLE", "PROPOSAL_NAME",
                                 "PROPOSAL", "NAME")) or "EPBC referral"
            status = str(up.get("STATUS") or up.get("DECISION") or up.get("ASSESSMENT_STATUS") or "")
            ref = str(up.get("EPBC_NUMBER") or up.get("REFERENCE") or up.get("REFERRAL_NUMBER") or "")
            p = {"name": str(nm)[:140], "type": "Environmental referral (EPBC)",
                 "state": str(up.get("STATE") or "Australia"),
                 "lat": round(ll[0], 5), "lng": round(ll[1], 5), "precise": not _au_approx,
                 "size": "", "status": status, "company": str(up.get("PROPONENT") or "")[:80],
                 "url": "https://epbcpublicportal.environment.gov.au/",
                 "date": _iso_date(up.get("DATE") or up.get("REFERRAL_DATE")
                                   or up.get("DATE_RECEIVED")),
                 "desc": ("Australian EPBC Act referral" + ((" \u00b7 " + ref) if ref else "") +
                          ((" \u00b7 " + status) if status else "") + ". Placed at the referral area centroid."),
                 "source": "epbc_au"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  epbc au: %d referrals (%d re-placed at national level)" % (len(out), dropped))
    return out


# ---------------------------------------------------------------------------
# Canada -- Impact Assessment Registry (Assessment Inventory), the federal
# major-projects registry, as a public geo.ca ArcGIS MapServer. Free, no key.
# ---------------------------------------------------------------------------
_CA_KEYS_SHOWN = []

def fetch_iaac_ca():
    base = ("https://maps-cartes.services.geo.ca/server_serveur/rest/services/"
            "IAAC/assessment_inventory_en/MapServer")
    feats = _arcgis_query_all(base, label="iaac ca")
    out = []; dropped = 0
    for f in feats:
        try:
            pr0 = f.get("properties") or {}
            _up0 = {str(k).upper(): v for k, v in pr0.items()}
            _la = _num(_up0.get("LATITUDE")); _lo = _num(_up0.get("LONGITUDE"))
            ll = (_la, _lo) if (_la is not None and _lo is not None) else _geom_center(f.get("geometry") or {})
            if not ll or ll[0] is None: continue
            _approx = False; _note = ""
            # source contains malformed records (points in Mali, Latvia, Indonesia...).
            # Canada has no overseas territory, so re-place them at the province /
            # national centroid and mark them approximate instead of deleting them.
            if not _box_ok("iaac_ca", ll[0], ll[1]):
                fb = _fallback_center("iaac_ca", _up0.get("PROVINCE_CODES") or _up0.get("PROVINCE") or "")
                if not fb:
                    dropped += 1; continue
                ll = (fb[0], fb[1]); _approx = True; dropped += 1
                _note = (" Source coordinates were unusable \u2014 shown at the "
                         + ("province" if fb[2] != "national" else "national")
                         + " level; open the registry for the exact site.")
            pr = f.get("properties") or {}
            up = {str(k).upper(): v for k, v in pr.items()}
            if not out and not _CA_KEYS_SHOWN:
                print("  iaac ca [fields]: %s" % sorted(list(pr.keys()))[:18])
                _CA_KEYS_SHOWN.append(1)
            nm = (up.get("PROJECT_NAME_EN") or up.get("PROJECT_NAME")
                  or up.get("DESCRIPTION_EN") or "Impact assessment")
            status = str(up.get("PROJECT_STATE_EN") or up.get("STATUS") or "")
            p = {"name": str(nm)[:140],
                 "type": str(up.get("PROJECT_CAT_EN") or "Impact assessment (Canada)"),
                 "state": str(up.get("LOCATION_EN") or up.get("PROVINCE_CODES") or "Canada"),
                 "lat": round(ll[0], 5), "lng": round(ll[1], 5), "precise": not _approx,
                 "size": "", "status": status, "company": str(up.get("PROPONENT_EN") or "")[:80],
                 "url": str(up.get("PROJECT_URL_EN") or "https://iaac-aeic.gc.ca/050/evaluations"),
                 "date": _iso_date(up.get("START_DATE") or up.get("UPDATED_AT")),
                 "desc": ("Canadian federal impact assessment" +
                          ((" \u00b7 " + status) if status else "") +
                          ". From the Impact Assessment Registry." + _note),
                 "source": "iaac_ca"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  iaac ca: %d assessments (%d re-placed at province/national level:"
          " source coords outside Canada)" % (len(out), dropped))
    return out


# ---------------------------------------------------------------------------
# OpenStreetMap (Overpass) -- things PHYSICALLY UNDER CONSTRUCTION worldwide.
# Free, no key, ODbL (attribution required, baked into each desc). Pure builds:
# construction landuse, roads/rail being built, and works-in-progress sites.
# Queried per region bbox with a hard per-bbox cap so it can't flood the map.
# ---------------------------------------------------------------------------
# Continent-sized Overpass queries time out and return partial data, so the
# world is split into small tiles instead. Each run works through a rotating
# slice of the grid and MERGES with what previous runs already found, so
# coverage accumulates instead of being capped.
_OSM_REGIONS = [
    (24.0, -125.0, 49.5, -66.0), (49.0, -141.0, 60.0, -52.0),
    (14.0, -118.0, 33.0, -86.0), (-56.0, -82.0, 13.0, -34.0),
    (36.0, -10.0, 60.0, 30.0), (50.0, 20.0, 70.0, 60.0),
    (-35.0, -18.0, 15.0, 52.0), (15.0, 25.0, 42.0, 63.0),
    (5.0, 60.0, 37.0, 92.0), (18.0, 92.0, 54.0, 135.0),
    (-11.0, 95.0, 22.0, 141.0), (-44.0, 112.0, -10.0, 154.0),
    (-48.0, 166.0, -34.0, 179.0),
]
def _osm_tiles(step=5.0):
    """Split the world's populated regions into ~5-degree tiles (~550km)."""
    out = []
    for (s, w, n, e) in _OSM_REGIONS:
        la = s
        while la < n:
            lo = w
            while lo < e:
                out.append((round(la, 2), round(lo, 2),
                            round(min(la + step, n), 2), round(min(lo + step, e), 2)))
                lo += step
            la += step
    return out

def _osm_existing():
    """Keep what earlier runs already harvested so coverage accumulates."""
    try:
        ex = json.load(open("projects.json", encoding="utf-8"))
        rows = ex.get("projects", []) if isinstance(ex, dict) else (ex if isinstance(ex, list) else [])
        return [q for q in rows if q.get("source") == "osm_construction"]
    except Exception:
        return []


_OVERPASS_EPS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]
def _overpass(q, label=""):
    """POST an Overpass query, trying each mirror. Returns parsed JSON or None."""
    for i, ep in enumerate(_OVERPASS_EPS):
        try:
            req = urllib.request.Request(ep, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as ex:
            if i == len(_OVERPASS_EPS) - 1:
                print("  osm %s failed on all mirrors: %s" % (label, str(ex)[:60]))
            time.sleep(1.5)
    return None

def _quarters(s, w, n, e):
    ms, mw = (s + n) / 2.0, (w + e) / 2.0
    return [(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]

def _osm_query(s, w, n, e, cap):
    bb = "%s,%s,%s,%s" % (s, w, n, e)
    return ('[out:json][timeout:180];('
            'way["landuse"="construction"](%s)(if:length()>400);'
            'way["highway"="construction"](%s)(if:length()>800);'
            'way["railway"="construction"](%s)(if:length()>800);'
            'way["building"="construction"](%s)(if:length()>250);'
            'way["landuse"="quarry"](%s)(if:length()>600);'
            'way["man_made"="pipeline"]["construction"](%s);'
            'way["power"="plant"]["construction"](%s);'
            'way["waterway"="dam"]["construction"](%s);'
            'relation["landuse"="construction"](%s);'
            ');out center %d;' % (bb, bb, bb, bb, bb, bb, bb, bb, bb, cap))

def _osm_collect(data, label, out):
    for el in (data.get("elements") or []):
        try:
            c = el.get("center") or {}
            lat = c.get("lat", el.get("lat")); lng = c.get("lon", el.get("lon"))
            if lat is None or lng is None: continue
            tg = el.get("tags") or {}
            nm = tg.get("name") or tg.get("operator") or ""
            kind = ("Road under construction" if tg.get("highway") else
                    "Railway under construction" if tg.get("railway") else
                    "Building under construction" if tg.get("building") else
                    "Quarry / extraction site" if tg.get("landuse") == "quarry" else
                    "Pipeline under construction" if tg.get("man_made") == "pipeline" else
                    "Power plant under construction" if tg.get("power") == "plant" else
                    "Dam under construction" if tg.get("waterway") == "dam" else
                    "Construction site")
            p = {"name": (nm or kind)[:140], "type": kind, "state": "",
                 "lat": round(float(lat), 5), "lng": round(float(lng), 5),
                 "precise": True, "size": "", "status": "Under construction",
                 "company": tg.get("operator") or "",
                 "url": "https://www.openstreetmap.org/way/" + str(el.get("id") or ""),
                 "desc": kind + " mapped in OpenStreetMap (ODbL).",
                 "source": "osm_construction"}
            p["impact"] = rate_project(p, sensitivity=0)
            out.append(p)
        except Exception:
            continue

def fetch_osm_construction(cap=3000, tiles_per_run=410):
    ep = "https://overpass-api.de/api/interpreter"
    grid = _osm_tiles()
    # STRIDE across the grid, don't take a contiguous slice: the grid is ordered by
    # region, so a contiguous slice = one continent (this made every run US-only and
    # drew visible boxes). Striding spreads each run's tiles across the whole world.
    nslice = max(1, min(tiles_per_run, len(grid)))
    stride = max(1, len(grid) // nslice)
    # advance the offset by RUN INDEX (weeks*2), not calendar day: the OSM job runs
    # twice a week, so a day-based offset would skip parts of the grid entirely.
    run_ix = datetime.date.today().toordinal() // 3
    offset = run_ix % stride
    todo = [grid[i] for i in range(offset, len(grid), stride)][:nslice]
    print("  osm: grid of %d tiles; this run does %d, strided every %d (offset %d) "
          "-> spread worldwide" % (len(grid), len(todo), stride, offset))
    out = _osm_existing()
    print("  osm: carried %d sites forward from previous runs" % len(out))
    ok_boxes = 0; timeouts = 0
    for (s, w, n, e) in todo:
        label = "%.0f,%.0f" % (s, w)
        bb = "%s,%s,%s,%s" % (s, w, n, e)
        # widened tag set: sites, roads, rail, buildings, plus proposed/under-way
        # extraction and energy works -- the land-taking projects this map is about.
        # Overpass can measure features, so instead of an arbitrary cap we keep the
        # BIG ones: length() on a closed way is its perimeter (400m ~ 1 hectare),
        # on a road/rail it's the route length.
        q = ('[out:json][timeout:240];('
             'way["landuse"="construction"](%s)(if:length()>400);'
             'way["highway"="construction"](%s)(if:length()>800);'
             'way["railway"="construction"](%s)(if:length()>800);'
             'way["building"="construction"](%s)(if:length()>250);'
             'way["landuse"="quarry"](%s)(if:length()>600);'
             'way["man_made"="pipeline"]["construction"](%s);'
             'way["power"="plant"]["construction"](%s);'
             'way["waterway"="dam"]["construction"](%s);'
             'relation["landuse"="construction"](%s);'
             ');out center %d;' % (bb, bb, bb, bb, bb, bb, bb, bb, bb, cap))
        data = _overpass(q, label)
        if data is None:
            # A 504 means the tile was too heavy for the server, not that it is
            # empty. Split it into quarters and retry -- otherwise dense areas
            # (Europe, East Asia) would time out forever and stay blank.
            hit = False
            for (qs, qw, qn, qe) in _quarters(s, w, n, e):
                sub = _osm_query(qs, qw, qn, qe, cap)
                d2 = _overpass(sub, label + "/q")
                if d2 is not None:
                    hit = True
                    _osm_collect(d2, label, out)
                time.sleep(1.0)
            if hit: ok_boxes += 1
            else: timeouts += 1
            continue
        ok_boxes += 1
        for el in (data.get("elements") or []):
            try:
                c = el.get("center") or {}
                lat = c.get("lat", el.get("lat")); lng = c.get("lon", el.get("lon"))
                if lat is None or lng is None: continue
                tg = el.get("tags") or {}
                nm = (tg.get("name") or tg.get("construction:name") or
                      tg.get("operator") or "")
                kind = ("Road under construction" if tg.get("highway") else
                        "Railway under construction" if tg.get("railway") else
                        "Building under construction" if tg.get("building") else
                        "Quarry / extraction site" if tg.get("landuse") == "quarry" else
                        "Pipeline under construction" if tg.get("man_made") == "pipeline" else
                        "Power plant under construction" if tg.get("power") == "plant" else
                        "Dam under construction" if tg.get("waterway") == "dam" else
                        "Construction site")
                p = {"name": (nm or kind)[:140], "type": kind, "state": label,
                     "lat": round(float(lat), 5), "lng": round(float(lng), 5),
                     "precise": True, "size": "", "status": "Under construction",
                     "company": tg.get("operator") or "",
                     "url": "https://www.openstreetmap.org/way/" + str(el.get("id") or ""),
                     "desc": (kind + " mapped in OpenStreetMap" +
                              ((" \u00b7 " + nm) if nm else "") +
                              ". Source: OpenStreetMap contributors (ODbL)."),
                     "source": "osm_construction"}
                p["impact"] = rate_project(p, sensitivity=0)
                out.append(p)
            except Exception:
                continue
        time.sleep(1.2)   # Overpass fair-use pacing
    # dedup accumulated + new by rounded position
    seen = set(); merged = []
    for q in out:
        k = (round(q.get("lat", 0), 4), round(q.get("lng", 0), 4))
        if k in seen: continue
        seen.add(k); merged.append(q)
    print("  osm construction: %d sites total (%d/%d tiles ok, %d still timed out)"
          % (len(merged), ok_boxes, len(todo), timeouts))
    return merged


# ---------------------------------------------------------------------------
# Brazil -- IBAMA federal environmental licences (CKAN open data, free, no key).
# Licenca Previa / Instalacao / Operacao = the approvals mines, dams, pipelines
# and highways need. Placed from coordinates when published, else at the state
# centroid (flagged approximate). dadosabertos.ibama.gov.br
# ---------------------------------------------------------------------------
_BR_UF = {
    "AC": (-9.0, -70.0), "AL": (-9.6, -36.8), "AP": (1.4, -51.8), "AM": (-4.1, -63.0),
    "BA": (-12.5, -41.7), "CE": (-5.2, -39.3), "DF": (-15.8, -47.8), "ES": (-19.6, -40.3),
    "GO": (-16.0, -49.6), "MA": (-5.0, -45.3), "MT": (-13.0, -55.9), "MS": (-20.5, -54.6),
    "MG": (-18.6, -44.6), "PA": (-4.0, -53.0), "PB": (-7.2, -36.7), "PR": (-24.6, -51.6),
    "PE": (-8.4, -37.9), "PI": (-7.4, -42.7), "RJ": (-22.3, -42.7), "RN": (-5.8, -36.6),
    "RS": (-30.0, -53.5), "RO": (-10.9, -63.0), "RR": (2.1, -61.4), "SC": (-27.2, -50.5),
    "SP": (-22.2, -48.7), "SE": (-10.6, -37.4), "TO": (-10.2, -48.3),
}
_BR_BUILD_LIC = ("previa", "prévia", "instala", "opera", "supress", "sismic", "sísmic")

def _sniff_col(cols, *pats):
    for pat in pats:
        for c in cols:
            if pat in str(c).lower(): return c
    return None


_BR_CENTER = (-14.24, -51.93)

def _ibama_national(csvs, max_rows=1500):
    """Fallback: IBAMA's licence tables publish no coordinates. Place each licence
    at Brazil's centroid, spread slightly so they don't stack, flagged approximate."""
    import csv as _csv, io as _io
    try:
        req = urllib.request.Request(csvs[0]["url"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode("utf-8-sig", "replace")
    except Exception as e:
        print("  ibama br: national fallback download failed: %s" % e); return []
    delim = ";" if raw[:2000].count(";") > raw[:2000].count(",") else ","
    rdr = _csv.DictReader(_io.StringIO(raw), delimiter=delim)
    cols = [str(c).lstrip("\ufeff") for c in (rdr.fieldnames or [])]
    c_nm = _sniff_col(cols, "empreendimento", "nome", "denomina")
    c_lic = _sniff_col(cols, "tipolicenca", "tipo_licenca", "licenca")
    c_tip = _sniff_col(cols, "tipologia", "atividade")
    c_dat = _sniff_col(cols, "emissao", "data")
    out = []; jit = 0.0
    for row in rdr:
        if len(out) >= max_rows: break
        try:
            row = {str(k).lstrip("\ufeff"): v for k, v in row.items()}
            lic = str(row.get(c_lic) or "").lower() if c_lic else ""
            if lic and not any(k in lic for k in _BR_BUILD_LIC):
                continue
            nm = str(row.get(c_nm) or "").strip()
            if not nm: continue
            jit += 0.37
            tip = str(row.get(c_tip) or "").strip()
            p = {"name": nm[:140],
                 "type": (tip or "Environmental licence (Brazil)")[:60],
                 "state": "Brazil",
                 "lat": round(_BR_CENTER[0] + (jit % 7.0) - 3.5, 4),
                 "lng": round(_BR_CENTER[1] + ((jit * 1.7) % 9.0) - 4.5, 4),
                 "precise": False, "size": "",
                 "status": str(row.get(c_lic) or "")[:60], "company": "",
                 "date": _iso_date(row.get(c_dat)),
                 "url": "https://dadosabertos.ibama.gov.br/dataset/"
                        "licencas-ambientais-de-atividades-e-empreendimentos-licenciados-pelo-ibama",
                 "desc": ("Brazilian federal environmental licence (IBAMA)" +
                          ((" \u00b7 " + str(row.get(c_lic))) if c_lic and row.get(c_lic) else "") +
                          ((" \u00b7 " + str(row.get(c_dat))[:10]) if c_dat and row.get(c_dat) else "") +
                          ". IBAMA publishes no coordinates \u2014 shown at national level; "
                          "open the register for the site."),
                 "source": "ibama_br"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  ibama br: %d licences (national-level placement)" % len(out))
    return out

def fetch_ibama_br(max_rows=4000):
    base = "https://dadosabertos.ibama.gov.br/api/3/action/package_show?id="
    ds = "licencas-ambientais-de-atividades-e-empreendimentos-licenciados-pelo-ibama"
    try:
        meta = _get_json(base + ds)
    except Exception as e:
        print("  ibama br: package lookup failed: %s" % e); return []
    res = ((meta or {}).get("result") or {}).get("resources") or []
    csvs = [r for r in res if str(r.get("format", "")).upper() in ("CSV", "TXT")
            and r.get("url")]
    if not csvs:
        print("  ibama br: no CSV resource found (%d resources)" % len(res)); return []
    import csv as _csv, io as _io
    rdr = None; cols = []
    # the main licence table publishes NO location column -- scan the package's
    # resources for one that actually carries coordinates or a state/municipality.
    for cand in csvs[:6]:
        try:
            req = urllib.request.Request(cand["url"], headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read().decode("utf-8-sig", "replace")
        except Exception as e:
            print("  ibama br: download failed (%s): %s" % (str(cand.get("name"))[:30], e)); continue
        delim = ";" if raw[:2000].count(";") > raw[:2000].count(",") else ","
        rr = _csv.DictReader(_io.StringIO(raw), delimiter=delim)
        cc = [str(c).lstrip("\ufeff") for c in (rr.fieldnames or [])]
        print("  ibama br [fields] %s: %s" % (str(cand.get("name"))[:28], cc[:12]))
        if _sniff_col(cc, "latitude", "lat") or _sniff_col(cc, "uf", "estado", "municipio"):
            rdr = rr; cols = cc; break
    if rdr is None:
        # No resource carries geography. The licences are real, so publish them at
        # the NATIONAL centroid, clearly flagged approximate, rather than dropping.
        print("  ibama br: no geo column in any resource -- placing at national level")
        return _ibama_national(csvs, max_rows)
    c_lat = _sniff_col(cols, "latitude", "lat")
    c_lng = _sniff_col(cols, "longitude", "long", "lng")
    c_nm  = _sniff_col(cols, "empreendimento", "nome", "denomina", "atividade")
    c_uf  = _sniff_col(cols, "uf", "estado", "sigla_uf")
    c_lic = _sniff_col(cols, "tipolicenca", "tipo_licenca", "licenca")
    c_mun = _sniff_col(cols, "municipio", "municipality")
    out = []; n = 0; approx = 0
    for row in rdr:
        if n >= max_rows: break
        try:
            row = {str(k).lstrip("\ufeff"): v for k, v in row.items()}
            lic = str(row.get(c_lic) or "").lower() if c_lic else ""
            if lic and not any(k in lic for k in _BR_BUILD_LIC):
                continue
            lat = _num(row.get(c_lat)) if c_lat else None
            lng = _num(row.get(c_lng)) if c_lng else None
            precise = True
            if lat is None or lng is None or not (-34 < (lat or 99) < 6 and -74 < (lng or 99) < -34):
                uf = str(row.get(c_uf) or "").strip().upper()[:2]
                if uf not in _BR_UF: continue
                lat, lng = _BR_UF[uf]; precise = False; approx += 1
            n += 1
            nm = (str(row.get(c_nm) or "").strip() or "Licenciamento ambiental")[:140]
            mun = str(row.get(c_mun) or "").strip()
            p = {"name": nm, "type": "Environmental licence (Brazil)",
                 "state": (mun + ", " if mun else "") + str(row.get(c_uf) or "Brazil"),
                 "lat": round(float(lat), 5), "lng": round(float(lng), 5),
                 "precise": precise, "size": "",
                 "status": str(row.get(c_lic) or "")[:60], "company": "",
                 "date": _iso_date(row.get(_sniff_col(cols, "emissao", "dat_emissao"))),
                 "url": "https://dadosabertos.ibama.gov.br/dataset/" + ds,
                 "desc": ("Brazilian federal environmental licence (IBAMA)" +
                          ((" \u00b7 " + str(row.get(c_lic))) if c_lic and row.get(c_lic) else "") +
                          ("." if precise else ". State-level placement \u2014 no coordinates published.")),
                 "source": "ibama_br"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  ibama br: %d licences (%d placed at state level)" % (len(out), approx))
    return out


# ---------------------------------------------------------------------------
# France -- Sitadel: the national building/development permit database (SDES,
# Ministry of Ecological Transition). Etalab 2.0 open licence, fully automated:
# the monthly CSV is fetched from data.gouv.fr each run. Sitadel has no
# coordinates, so permits are placed on their COMMUNE centroid (communes are
# small, ~15 km2 on average) via geo.api.gouv.fr -- one request for all ~35k.
# ---------------------------------------------------------------------------
def _fr_communes():
    try:
        rows = _get_json("https://geo.api.gouv.fr/communes?fields=code,nom,centre&format=json")
    except Exception as e:
        print("  france: commune centroids failed: %s" % e); return {}
    out = {}
    for c in (rows or []):
        try:
            ctr = (c.get("centre") or {}).get("coordinates") or []
            if len(ctr) >= 2:
                out[str(c.get("code"))] = (float(ctr[1]), float(ctr[0]), c.get("nom") or "")
        except Exception:
            continue
    return out

def fetch_sitadel_fr(max_rows=3000, months=6):
    com = _fr_communes()
    if not com:
        print("  sitadel fr: no commune centroids (skip)"); return []
    print("  sitadel fr: %d commune centroids loaded" % len(com))
    slug = "liste-des-permis-de-construire-et-autres-autorisations-durbanisme"
    try:
        meta = _get_json("https://www.data.gouv.fr/api/1/datasets/%s/" % slug)
    except Exception as e:
        print("  sitadel fr: dataset lookup failed: %s" % e); return []
    res = (meta or {}).get("resources") or []
    cands = [r for r in res if str(r.get("format", "")).lower() in ("csv", "zip", "txt")
             and r.get("url")]
    # prefer a resource whose title mentions non-residential ("locaux") or permits
    pick = None
    for r in cands:
        t = (str(r.get("title") or "") + " " + str(r.get("url") or "")).lower()
        if "local" in t or "non_resid" in t or "locaux" in t:
            pick = r; break
    pick = pick or (cands[0] if cands else None)
    if not pick:
        print("  sitadel fr: no CSV resource (%d resources)" % len(res)); return []
    print("  sitadel fr: using resource '%s'" % str(pick.get("title"))[:70])
    try:
        req = urllib.request.Request(pick["url"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=240) as r:
            blob = r.read()
    except Exception as e:
        print("  sitadel fr: download failed: %s" % e); return []
    # unzip if needed
    text = None
    if blob[:2] == b"PK":
        try:
            import zipfile, io as _io2
            zf = zipfile.ZipFile(_io2.BytesIO(blob))
            names = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt"))]
            if not names:
                print("  sitadel fr: zip has no csv"); return []
            text = zf.read(names[0]).decode("utf-8", "replace")
        except Exception as e:
            print("  sitadel fr: unzip failed: %s" % e); return []
    else:
        text = blob.decode("utf-8", "replace")
    import csv as _csv, io as _io
    delim = ";" if text[:3000].count(";") > text[:3000].count(",") else ","
    rdr = _csv.DictReader(_io.StringIO(text), delimiter=delim)
    cols = rdr.fieldnames or []
    print("  sitadel fr [fields]: %s" % cols[:14])
    c_com = _sniff_col(cols, "comm", "code_commune", "insee")
    c_dat = _sniff_col(cols, "date_reelle_autorisation", "date_autoris", "date")
    c_nat = _sniff_col(cols, "nature_projet", "nature", "type_dau", "destination")
    c_srf = _sniff_col(cols, "surf_loc_creee", "surface", "surf")
    if not c_com:
        print("  sitadel fr: no commune column found"); return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=months * 31)).isoformat()
    out = []
    for row in rdr:
        if len(out) >= max_rows: break
        try:
            code = str(row.get(c_com) or "").strip().zfill(5)
            hit = com.get(code)
            if not hit: continue
            if c_dat:
                dv = str(row.get(c_dat) or "")[:10]
                if len(dv) == 10 and dv < cutoff: continue
            srf = _num(row.get(c_srf)) if c_srf else None
            if srf is not None and srf < 500:      # keep significant builds only
                continue
            lat, lng, cname = hit
            nat = str(row.get(c_nat) or "").strip()
            nm = ((nat + " \u2014 " if nat else "") + cname)[:140] or "Permis de construire"
            p = {"name": nm, "type": "Development permit (France)",
                 "state": cname, "lat": round(lat, 5), "lng": round(lng, 5),
                 "precise": False,
                 "size": ("%d m\u00b2" % int(srf)) if srf else "",
                 "status": "Permit granted", "company": "",
                 "url": "https://www.data.gouv.fr/datasets/" + slug,
                 "date": _iso_date(row.get(c_dat)) if c_dat else None,
                 "desc": ("French development permit (Sitadel, SDES)" +
                          ((" \u00b7 " + nat) if nat else "") +
                          ". Placed at the commune centroid (" + cname + ")."),
                 "source": "sitadel_fr"}
            p["impact"] = rate_project(p, sensitivity=0)
            out.append(p)
        except Exception:
            continue
    print("  sitadel fr: %d permits" % len(out))
    return out


# ---------------------------------------------------------------------------
# India -- environmental / forest clearances (PARIVESH) via data.gov.in.
# Free API, needs a free key: register at data.gov.in, then add the key as the
# GitHub secret DATA_GOV_IN_KEY. Resource IDs are discovered from the catalog,
# or set INDIA_RESOURCE_IDS (comma-separated) to pin them. Records carry no
# coordinates, so each is placed at its STATE centroid (flagged approximate).
# ---------------------------------------------------------------------------
_IN_STATE = {
    "ANDHRA PRADESH": (15.9, 79.7), "ARUNACHAL PRADESH": (28.2, 94.7), "ASSAM": (26.2, 92.9),
    "BIHAR": (25.1, 85.3), "CHHATTISGARH": (21.3, 81.8), "GOA": (15.3, 74.1),
    "GUJARAT": (22.3, 71.2), "HARYANA": (29.1, 76.1), "HIMACHAL PRADESH": (31.1, 77.2),
    "JHARKHAND": (23.6, 85.3), "KARNATAKA": (15.3, 75.7), "KERALA": (10.9, 76.3),
    "MADHYA PRADESH": (23.5, 78.7), "MAHARASHTRA": (19.7, 75.7), "MANIPUR": (24.7, 93.9),
    "MEGHALAYA": (25.5, 91.4), "MIZORAM": (23.2, 92.9), "NAGALAND": (26.2, 94.6),
    "ODISHA": (20.9, 85.1), "ORISSA": (20.9, 85.1), "PUNJAB": (31.1, 75.3),
    "RAJASTHAN": (27.0, 74.2), "SIKKIM": (27.5, 88.5), "TAMIL NADU": (11.1, 78.7),
    "TELANGANA": (18.1, 79.0), "TRIPURA": (23.9, 91.7), "UTTAR PRADESH": (26.8, 80.9),
    "UTTARAKHAND": (30.1, 79.3), "WEST BENGAL": (22.9, 87.9), "DELHI": (28.6, 77.2),
    "JAMMU AND KASHMIR": (33.8, 76.6), "LADAKH": (34.2, 77.6), "PUDUCHERRY": (11.9, 79.8),
    "CHANDIGARH": (30.7, 76.8), "ANDAMAN AND NICOBAR ISLANDS": (11.7, 92.7),
    "LAKSHADWEEP": (10.6, 72.6), "DADRA AND NAGAR HAVELI": (20.4, 72.8),
}
def _in_state_center(txt):
    t = str(txt or "").strip().upper()
    if not t: return None
    if t in _IN_STATE: return _IN_STATE[t]
    for k in sorted(_IN_STATE, key=len, reverse=True):
        if k in t: return _IN_STATE[k]
    return None

# DORMANT: data.gov.in publishes no API for the PARIVESH clearance resources
# ("The API for this resource does not exist") and only aggregate state counts,
# not individual projects. Kept for the day an API appears; not called.
def fetch_parivesh_in(per=1000, max_rows=3000):
    key = os.environ.get("DATA_GOV_IN_KEY")
    if not key:
        print("  parivesh in: no DATA_GOV_IN_KEY secret set (skip)"); return []
    ids = [s.strip() for s in (os.environ.get("INDIA_RESOURCE_IDS") or "").split(",") if s.strip()]
    if not ids:
        # discover clearance resources from the catalog
        try:
            cat = _get_json("https://api.data.gov.in/catalog?" + urllib.parse.urlencode({
                "api-key": key, "format": "json", "limit": 100,
                "filters[title]": "Environmental Clearance"}))
            recs = (cat or {}).get("records") or (cat or {}).get("data") or []
            for r in recs:
                rid = r.get("index_name") or r.get("resource_id") or r.get("id")
                if rid: ids.append(str(rid))
        except Exception as e:
            print("  parivesh in: catalog discovery failed: %s" % e)
    if not ids:
        print("  parivesh in: no resource ids found -- set INDIA_RESOURCE_IDS secret"); return []
    print("  parivesh in: %d resource(s): %s" % (len(ids), ids[:3]))
    out = []
    for rid in ids[:6]:
        try:
            data = _get_json("https://api.data.gov.in/resource/%s?" % rid + urllib.parse.urlencode({
                "api-key": key, "format": "json", "offset": 0, "limit": per}))
        except Exception as e:
            print("  parivesh in %s: %s" % (rid[:8], e)); continue
        recs = (data or {}).get("records") or []
        if recs and not out:
            print("  parivesh in [fields]: %s" % list(recs[0].keys())[:14])
        for r in recs:
            if len(out) >= max_rows: break
            try:
                low = {str(k).lower(): v for k, v in r.items()}
                st = None
                for kk in ("state", "state_name", "state_ut", "location"):
                    if low.get(kk): st = low[kk]; break
                ctr = _in_state_center(st)
                if not ctr: continue
                nm = None
                for kk in ("project_name", "name_of_project", "proposal_name", "project", "name"):
                    if low.get(kk): nm = str(low[kk]); break
                if not nm: continue
                cat_v = ""
                for kk in ("category", "sector", "project_type", "type"):
                    if low.get(kk): cat_v = str(low[kk]); break
                p = {"name": nm[:140], "type": (cat_v or "Environmental clearance (India)"),
                     "state": str(st), "lat": round(ctr[0], 5), "lng": round(ctr[1], 5),
                     "precise": False, "size": "", "status": "Clearance granted", "company": "",
                     "url": "https://parivesh.nic.in/",
                     "desc": ("Indian environmental/forest clearance (PARIVESH)" +
                              ((" \u00b7 " + cat_v) if cat_v else "") +
                              ". State-level placement \u2014 no coordinates published."),
                     "source": "parivesh_in"}
                p["impact"] = rate_project(p, sensitivity=1)
                out.append(p)
            except Exception:
                continue
        time.sleep(0.5)
    print("  parivesh in: %d clearances" % len(out))
    return out



def _iso_date(v):
    """Normalise a date from any source to YYYY-MM-DD, or None."""
    if v in (None, ""): return None
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m: return m.group(0)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)          # dd/mm/yyyy or mm/dd/yyyy
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        try:
            if int(mo) > 12: d, mo = mo, d
            return "%s-%s-%s" % (y, mo.zfill(2), d.zfill(2))
        except Exception: return None
    if re.fullmatch(r"\d{8}", s):                          # yyyymmdd, exactly 8 digits
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    try:                                                   # epoch seconds/millis (ArcGIS)
        n = float(s)
        if n > 1e11:
            return datetime.datetime.utcfromtimestamp(n / 1000.0).date().isoformat()
        if n > 1e8:
            return datetime.datetime.utcfromtimestamp(n).date().isoformat()
    except Exception:
        pass
    return None

def _slim(p):
    """Trim each project for wire size: drop empty fields, round coords to ~11m,
    cap prose. 20k+ projects make every byte count for map load time."""
    q = {}
    for k, v in p.items():
        if v is None or v == "" or v == []: continue
        if k in ("lat", "lng"):
            try: q[k] = round(float(v), 4)
            except Exception: pass
            continue
        if k in ("date", "deadline"):
            q[k] = str(v)[:10]; continue
        if k == "desc":
            q[k] = str(v)[:95]; continue
        if k == "precise" and v is True:
            continue                       # precise is the default; only mark exceptions
        if k in ("mw",): continue
        q[k] = v
    return q


# ---------------------------------------------------------------------------
# CKAN federation -- national open-data portals worldwide run CKAN, which has a
# standard API. We search each portal for permit / licence / project datasets
# that publish GeoJSON, and map the points. This is the third route to covering
# countries with no national register (Chile, Spain, Italy, Poland, Ireland...).
# Free, no keys.
# ---------------------------------------------------------------------------
_CKAN_PORTALS = [
    ("https://datos.gob.cl", "Chile", "cl"),
    ("https://datos.gob.es/apidata", "Spain", "es"),
    ("https://dados.gov.br", "Brazil", "br"),
    ("https://data.gov.ie", "Ireland", "ie"),
    ("https://catalogue.data.govt.nz", "New Zealand", "nz"),
    ("https://data.overheid.nl/data", "Netherlands", "nl"),
    ("https://opendata.swiss", "Switzerland", "ch"),
    ("https://data.gov.au/data", "Australia", "au"),
    ("https://www.dati.gov.it/opendata", "Italy", "it"),
    ("https://dane.gov.pl", "Poland", "pl"),
    ("https://data.norge.no", "Norway", "no"),
    ("https://www.govdata.de/ckan", "Germany", "de"),
]
_CKAN_TERMS = ["permis construction", "licencia construccion", "building permit",
               "permesso costruire", "pozwolenie budowe", "bouwvergunning",
               "byggetillatelse", "baugenehmigung", "proyectos construccion"]
_CKAN_TITLE_RE = re.compile(
    r"(permit|permis|licenc|licens|vergunning|genehmigung|pozwolen|costruire|"
    r"bygge|bygglov|construc|construction|obra|edifica|planning|urban)", re.I)

def fetch_ckan_federation(per_portal=3, per_ds=1500):
    out = []
    for (base, country, cc) in _CKAN_PORTALS:
        pkgs = []; seen = set()
        for term in _CKAN_TERMS[:4]:
            try:
                u = base.rstrip("/") + "/api/3/action/package_search?" + urllib.parse.urlencode(
                    {"q": term, "rows": 25})
                d = _get_json(u)
            except Exception:
                continue
            for pk in (((d or {}).get("result") or {}).get("results") or []):
                nm = str(pk.get("title") or pk.get("name") or "")
                if pk.get("id") in seen: continue
                if not _CKAN_TITLE_RE.search(nm): continue
                seen.add(pk.get("id")); pkgs.append(pk)
            time.sleep(0.3)
        got = 0
        for pk in pkgs:
            if got >= per_portal: break
            geo = [r for r in (pk.get("resources") or [])
                   if str(r.get("format", "")).lower() in ("geojson", "json") and r.get("url")]
            for r in geo[:1]:
                try:
                    req = urllib.request.Request(r["url"], headers={"User-Agent": UA})
                    with urllib.request.urlopen(req, timeout=90) as resp:
                        gj = json.loads(resp.read().decode("utf-8", "replace"))
                except Exception:
                    continue
                feats = gj.get("features") if isinstance(gj, dict) else None
                if not feats: continue
                got += 1
                n0 = len(out)
                for f in feats[:per_ds]:
                    try:
                        ll = _geom_center(f.get("geometry") or {})
                        if not ll: continue
                        props = f.get("properties") or {}
                        nm = _best_name(props, ("NAME", "TITLE", "DESCRIPCION",
                                                "DESCRIPTION", "OBRA", "PROYECTO"))
                        p = {"name": (nm or str(pk.get("title") or "Permit"))[:140],
                             "type": "Permit / development (%s)" % country,
                             "state": country,
                             "lat": round(ll[0], 5), "lng": round(ll[1], 5),
                             "precise": True, "size": "", "status": "", "company": "",
                             "url": base, "desc": ("From %s open data \u00b7 %s."
                                                   % (country, str(pk.get("title") or "")[:70])),
                             "source": "ckan_%s" % cc}
                        p["impact"] = rate_project(p, sensitivity=0)
                        out.append(p)
                    except Exception:
                        continue
                if len(out) > n0:
                    print("  ckan %s: +%d from '%s'" % (country, len(out) - n0,
                                                         str(pk.get("title"))[:40]))
                time.sleep(0.3)
    print("  ckan federation: %d points from %d portals" % (len(out), len(_CKAN_PORTALS)))
    return out


def _finish(items):
    items = [p for p in items if p.get("lat") is not None and p.get("lng") is not None]
    items = dedup(items)
    items.sort(key=lambda p: -(p.get("impact") or 0))
    # per-source preservation: if a source comes back much thinner than what is
    # already saved (e.g. PermitStack hit its daily rate limit), keep the prior
    # entries for that source instead of clobbering them.
    if os.path.exists("projects.json"):
        try:
            ex = json.load(open("projects.json", encoding="utf-8"))
            exl = ex.get("projects", []) if isinstance(ex, dict) else (ex if isinstance(ex, list) else [])
            from collections import defaultdict
            old_by, new_by = defaultdict(list), defaultdict(list)
            for q in exl: old_by[q.get("source", "")].append(q)
            for q in items: new_by[q.get("source", "")].append(q)
            # Only preserve on a TOTAL failure (zero rows). A source that returns
            # fewer rows may simply have been filtered more strictly -- restoring
            # the old rows there would silently undo intentional filtering.
            for src, oldrows in old_by.items():
                new_n = len(new_by.get(src, []))
                if len(oldrows) >= 10 and new_n == 0:
                    items = [q for q in items if q.get("source") != src] + oldrows
                    print("  [preserve] %s returned nothing (had %d) -- kept prior entries"
                          % (src or "(none)", len(oldrows)))
        except Exception as e:
            print("  [preserve] skipped: %s" % e)

    # anti-wipe: never replace a healthy projects.json with a thin/empty harvest
    if len(items) < 4 and os.path.exists("projects.json"):
        try:
            ex = json.load(open("projects.json", encoding="utf-8"))
            exn = ex.get("projects", []) if isinstance(ex, dict) else (ex if isinstance(ex, list) else [])
            if len(exn) > len(items):
                print("harvest thin (%d) < existing (%d) -- keeping existing projects.json" % (len(items), len(exn)))
                return
        except Exception:
            pass
    items = [_slim(p) for p in items]
    out = {"_meta": {"generated": datetime.datetime.utcnow().isoformat() + "Z",
                     "count": len(items),
                     "sources": "socrata permits, land matrix, global energy monitor, epa eis, ferc, ejatlas",
                     "rating_scale": "1 minor / 2 local / 3 regional / 4 major / 5 landscape"},
           "projects": items}
    with open("projects.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote projects.json with %d projects" % len(items))
    if not items:
        print("NOTE: no sources wired yet -- fill SOCRATA_CITIES and uncomment a "
              "fetcher. The map falls back to its embedded seed set until then.")



def _carry_sources(pred, label):
    """Reuse entries already in projects.json for sources this run isn't refreshing."""
    try:
        ex = json.load(open("projects.json", encoding="utf-8"))
        rows = ex.get("projects", []) if isinstance(ex, dict) else (ex if isinstance(ex, list) else [])
    except Exception:
        rows = []
    keep = [q for q in rows if pred(str(q.get("source") or ""))]
    print("  [%s] carried %d entries forward (not refreshed this run)" % (label, len(keep)))
    return keep

def main():
    osm_only = os.environ.get("HARVEST_OSM") == "1"
    if osm_only:
        # Weekly OSM job: refresh ONLY OpenStreetMap and keep every other source
        # exactly as the daily job last wrote it. Running the full harvest here too
        # would double-spend PermitStack's 100/day budget on OSM days.
        print("MODE: OSM-only refresh (weekly job)")
        items = _run("osm_construction", fetch_osm_construction)
        items += _carry_sources(lambda s: s != "osm_construction", "daily sources")
        _finish(items)
        return
    print("MODE: daily refresh (all sources except OSM)")
    items = []
    items += _run("permitstack", fetch_permitstack)             # national construction permits (key)
    _SOCRATA_OFF = {"data.austintexas.gov", "data.sfgov.org", "data.lacity.org"}  # 400s; PermitStack covers these
    items += _run("ckan_federation", fetch_ckan_federation)     # national CKAN portals worldwide
    items += _run("arcgis_hub", fetch_arcgis_hub)               # US city/county permits (no cap)
    items += _run("socrata_permits", lambda: [p for cfg in SOCRATA_CITIES
                                              if cfg.get("domain") not in _SOCRATA_OFF
                                              for p in fetch_socrata(cfg)])
    items += _run("federal_register", fetch_federal_register)   # US EIS notices
    items += _run("public_land_nepa", fetch_public_land_nepa)   # BLM + USFS via Federal Register
    items += _run("sitadel_fr", fetch_sitadel_fr)               # France national permits (automated)
    items += _run("uk_planit", fetch_ukplanit)                  # UK national planning applications
    items += _run("epbc_au", fetch_epbc_au)                     # Australia national environmental referrals
    items += _run("iaac_ca", fetch_iaac_ca)                     # Canada federal impact assessments
    items += _run("ibama_br", fetch_ibama_br)                   # Brazil federal environmental licences
    items += _run("world_bank", fetch_world_bank)               # GLOBAL: active WB-financed projects
    items += _run("iati", fetch_iati)                           # GLOBAL: aid projects WITH coordinates
    items += _carry_sources(lambda s: s == "osm_construction", "osm_construction")
    _finish(items)

if __name__ == "__main__":
    main()
