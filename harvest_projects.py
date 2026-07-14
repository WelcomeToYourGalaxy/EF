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
         "fields[]": ["title", "abstract", "agencies", "publication_date", "html_url"]}
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
             "fields[]": ["title", "abstract", "agencies", "publication_date", "html_url"]}
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
_IATI_COUNTRIES = ["KE","ET","TZ","UG","NG","GH","CD","MZ","ML","NE","SN","RW","ZM",
                   "MW","BD","NP","PK","IN","ID","PH","MM","KH","VN","LK","AF","YE",
                   "HT","BO","PE","CO","GT","HN","NI","EG","MA","JO","LB","SS","SO"]

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
            withloc += 1
            nm = _iati_find(a, "narrative") or _iati_find(a, "title") or "Development activity"
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

def fetch_arcgis_hub(max_datasets=25, min_value=1000000, per_ds=300):
    try:
        surl = "https://opendata.arcgis.com/api/v3/datasets?" + urllib.parse.urlencode({
            "q": "building permits", "page[size]": "50"})
        sdata = _get_json(surl)
    except Exception as e:
        print("  arcgis hub search failed: %s" % e); return []
    ds = sdata.get("data", []) if isinstance(sdata, dict) else []
    ds = [d for d in ds
          if "permit" in str((d.get("attributes") or {}).get("name", "")).lower()]
    print("  arcgis hub: %d permit datasets discovered" % len(ds))
    out = []; used = 0
    for d in ds[:max_datasets]:
        attrs = d.get("attributes") or {}
        url = attrs.get("url")
        if not url or "/FeatureServer" not in url and "/MapServer" not in url:
            continue
        try:
            q = url.rstrip("/") + "/query?" + urllib.parse.urlencode({
                "where": "1=1", "outFields": "*", "f": "geojson",
                "outSR": "4326", "resultRecordCount": per_ds})
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
                if val is not None:
                    if val < min_value:
                        continue
                    size = "$%s" % format(int(val), ",")
                else:
                    # dataset exposes no cost field: still keep a capped sample
                    if no_val_kept >= 40:
                        continue
                    no_val_kept += 1
                    size = ""
                nm = None
                for k, v in props.items():
                    if _HUB_NAME_RE.search(str(k)) and isinstance(v, str) and v.strip():
                        nm = v; break
                p = {"name": str(nm or attrs.get("name") or "Permitted project")[:140],
                     "type": "New construction", "state": "",
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
    out = []
    for f in feats:
        try:
            geom = f.get("geometry") or {}; c = geom.get("coordinates") or []
            if geom.get("type") != "Point" or len(c) < 2: continue
            lng, lat = float(c[0]), float(c[1])
            pr = f.get("properties") or {}
            desc = pr.get("description") or "Planning application"
            addr = pr.get("address") or ""
            state = pr.get("app_state") or ""
            p = {"name": str(desc)[:140], "type": "Development (UK planning)",
                 "state": pr.get("authority_name") or "United Kingdom",
                 "lat": round(lat, 5), "lng": round(lng, 5), "precise": True,
                 "size": pr.get("app_size") or "", "status": state, "company": "",
                 "url": pr.get("link") or "https://planit.org.uk/",
                 "desc": ("UK planning application" + ((" (" + state + ")") if state else "") +
                          ((" \u00b7 " + addr) if addr else "") + "."),
                 "source": "uk_planit"}
            p["impact"] = rate_project(p, sensitivity=0)
            out.append(p)
        except Exception:
            continue
    print("  uk planit: %d large applications" % len(out))
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

def _arcgis_item_query(item_id, layer=0, rec=2000):
    meta = _get_json("https://www.arcgis.com/sharing/rest/content/items/%s?f=json" % item_id)
    url = (meta or {}).get("url")
    if not url: return None
    q = url.rstrip("/") + "/%d/query?" % layer + urllib.parse.urlencode({
        "where": "1=1", "outFields": "*", "f": "geojson", "outSR": "4326",
        "resultRecordCount": rec})
    return _get_json(q)

def fetch_epbc_au():
    try:
        gj = _arcgis_item_query("ee02ed7773d44c6fa799bf558c70f81a")
    except Exception as e:
        print("  epbc au failed: %s" % e); return []
    if not gj or not isinstance(gj, dict):
        print("  epbc au: no service response"); return []
    feats = gj.get("features", [])
    out = []
    for f in feats:
        try:
            ll = _geom_center(f.get("geometry") or {})
            if not ll: continue
            pr = f.get("properties") or {}
            up = {str(k).upper(): v for k, v in pr.items()}
            nm = (up.get("TITLE") or up.get("REFERRAL_TITLE") or up.get("PROPOSAL_NAME")
                  or up.get("PROPOSAL") or up.get("NAME") or "EPBC referral")
            status = str(up.get("STATUS") or up.get("DECISION") or up.get("ASSESSMENT_STATUS") or "")
            ref = str(up.get("EPBC_NUMBER") or up.get("REFERENCE") or up.get("REFERRAL_NUMBER") or "")
            p = {"name": str(nm)[:140], "type": "Environmental referral (EPBC)",
                 "state": str(up.get("STATE") or "Australia"),
                 "lat": round(ll[0], 5), "lng": round(ll[1], 5), "precise": True,
                 "size": "", "status": status, "company": str(up.get("PROPONENT") or "")[:80],
                 "url": "https://epbcpublicportal.environment.gov.au/",
                 "desc": ("Australian EPBC Act referral" + ((" \u00b7 " + ref) if ref else "") +
                          ((" \u00b7 " + status) if status else "") + ". Placed at the referral area centroid."),
                 "source": "epbc_au"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  epbc au: %d referrals" % len(out))
    return out


# ---------------------------------------------------------------------------
# Canada -- Impact Assessment Registry (Assessment Inventory), the federal
# major-projects registry, as a public geo.ca ArcGIS MapServer. Free, no key.
# ---------------------------------------------------------------------------
def fetch_iaac_ca():
    base = ("https://maps-cartes.services.geo.ca/server_serveur/rest/services/"
            "IAAC/assessment_inventory_en/MapServer/0/query")
    q = urllib.parse.urlencode({"where": "1=1", "outFields": "*", "f": "geojson",
                                "outSR": "4326", "resultRecordCount": "2000"})
    try:
        gj = _get_json(base + "?" + q)
    except Exception as e:
        print("  iaac ca failed: %s" % e); return []
    if not isinstance(gj, dict):
        print("  iaac ca: no response"); return []
    if gj.get("error"):
        print("  iaac ca error: %s" % str(gj.get("error"))[:150]); return []
    out = []
    for f in gj.get("features", []):
        try:
            ll = _geom_center(f.get("geometry") or {})
            if not ll: continue
            pr = f.get("properties") or {}
            up = {str(k).upper(): v for k, v in pr.items()}
            nm = (up.get("NAME") or up.get("PROJECT_NAME") or up.get("TITLE")
                  or up.get("PROJECT") or "Impact assessment")
            status = str(up.get("STATUS") or up.get("PHASE") or up.get("STAGE") or "")
            p = {"name": str(nm)[:140], "type": "Impact assessment (Canada)",
                 "state": str(up.get("PROVINCE") or up.get("REGION") or "Canada"),
                 "lat": round(ll[0], 5), "lng": round(ll[1], 5), "precise": True,
                 "size": "", "status": status, "company": str(up.get("PROPONENT") or "")[:80],
                 "url": "https://iaac-aeic.gc.ca/050/evaluations",
                 "desc": ("Canadian federal impact assessment" +
                          ((" \u00b7 " + status) if status else "") +
                          ". From the Impact Assessment Registry."),
                 "source": "iaac_ca"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  iaac ca: %d assessments" % len(out))
    return out

def main():
    items = []
    items += _run("permitstack", fetch_permitstack)             # national construction permits (key)
    _SOCRATA_OFF = {"data.austintexas.gov", "data.sfgov.org", "data.lacity.org"}  # 400s; PermitStack covers these
    items += _run("arcgis_hub", fetch_arcgis_hub)               # US city/county permits (no cap)
    items += _run("socrata_permits", lambda: [p for cfg in SOCRATA_CITIES
                                              if cfg.get("domain") not in _SOCRATA_OFF
                                              for p in fetch_socrata(cfg)])
    items += _run("federal_register", fetch_federal_register)   # US EIS notices
    items += _run("public_land_nepa", fetch_public_land_nepa)   # BLM + USFS via Federal Register
    items += _run("uk_planit", fetch_ukplanit)                  # UK national planning applications
    items += _run("epbc_au", fetch_epbc_au)                     # Australia national environmental referrals
    items += _run("iaac_ca", fetch_iaac_ca)                     # Canada federal impact assessments
    items += _run("world_bank", fetch_world_bank)               # GLOBAL: active WB-financed projects
    items += _run("iati", fetch_iati)                           # GLOBAL: aid projects WITH coordinates
    items += _run("gem", fetch_gem)                             # GLOBAL: new energy projects (local files)
    items += _run("ejatlas", fetch_ejatlas)                     # global EJ conflicts (local export)
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
            for src, oldrows in old_by.items():
                new_n = len(new_by.get(src, []))
                if len(oldrows) >= 10 and new_n < len(oldrows) * 0.5:
                    items = [q for q in items if q.get("source") != src] + oldrows
                    print("  [preserve] %s came back thin (%d < %d) -- kept prior entries"
                          % (src or "(none)", new_n, len(oldrows)))
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
    out = {"_meta": {"generated": datetime.datetime.utcnow().isoformat() + "Z",
                     "count": len(items),
                     "sources": "socrata permits, land matrix, global energy monitor, epa eis, ferc, ejatlas",
                     "rating_scale": "1 minor / 2 local / 3 regional / 4 major / 5 landscape"},
           "projects": items}
    with open("projects.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("wrote projects.json with %d projects" % len(items))
    if not items:
        print("NOTE: no sources wired yet -- fill SOCRATA_CITIES and uncomment a "
              "fetcher. The map falls back to its embedded seed set until then.")

if __name__ == "__main__":
    main()
