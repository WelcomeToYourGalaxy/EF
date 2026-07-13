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
    for r in rows:
        lat, lng = _socrata_point(r, cfg)
        if lat is None or lng is None: continue
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
def _gem_norm(pr, lat, lng):
    if lat is None or lng is None: return None
    name = _first(pr, "Project Name", "project_name", "Unit Name", "Name",
                  "Pipeline Name", "Mine Name")
    typ = _first(pr, "Type", "Fuel", "Category", "Sector") or "energy infrastructure"
    st = _first(pr, "Subnational unit (province/state)", "State/Province", "State", "Region")
    ctry = _first(pr, "Country/Area", "Country")
    mw = _num(_first(pr, "Capacity (MW)", "Capacity", "capacity_mw"))
    p = {"name": (name or "Energy project")[:120], "type": str(typ),
         "state": st or ctry or "", "lat": lat, "lng": lng, "mw": mw,
         "company": _first(pr, "Owner", "Parent", "Operator") or "",
         "status": _first(pr, "Status", "status") or "",
         "size": ("%s MW" % int(mw)) if mw else "",
         "desc": "Energy/extraction infrastructure tracked by Global Energy Monitor.",
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
        lat, lng = STATE_CENTROID[st]
        jitter += 0.11  # fan notices out around the centroid so they don't stack
        p = {"name": (d.get("title") or "Federal environmental review")[:140],
             "type": _infer_type(text), "state": st,
             "lat": round(lat + (jitter % 0.8) - 0.4, 4),
             "lng": round(lng + ((jitter * 1.7) % 0.8) - 0.4, 4),
             "size": "", "status": "In federal review (comment window may be open)",
             "company": "", "url": d.get("html_url"),
             "desc": "Federal environmental review notice (" + (d.get("publication_date") or "") +
                     "). Placement is state-level/approximate. Open the notice for the exact "
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

def fetch_permitstack(min_value=1000000, per_state_cap=120):
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
    for st in PERMITSTACK_STATES:
        try:
            res = client.permits.search_permits(state=st, category="new_construction",
                                                 min_value=min_value)
            rows = _ps_get(res, "results") or (res if isinstance(res, list) else []) or []
            n = 0
            for r in rows:
                if n >= per_state_cap:
                    break
                n += 1
                lat = _ps_get(r, "latitude"); lng = _ps_get(r, "longitude")
                if lat is None or lng is None:
                    continue
                val = _ps_get(r, "estimated_value") or 0
                addr = _ps_get(r, "address") or ""
                nm = (addr or _ps_get(r, "category") or "New construction")
                try: size = "$%s" % format(int(val), ",") if val else ""
                except Exception: size = ""
                out.append({
                    "name": str(nm)[:140],
                    "type": "New construction",
                    "state": _ps_get(r, "state") or "",
                    "lat": round(float(lat), 5), "lng": round(float(lng), 5),
                    "size": size,
                    "status": "Permit on file",
                    "company": _ps_get(r, "contractor_name") or "",
                    "url": "",
                    "desc": ("Building permit" + (" \u00b7 " + addr if addr else "") +
                             (" \u00b7 issued " + str(_ps_get(r, "date_issued"))
                              if _ps_get(r, "date_issued") else "") + "."),
                    "source": "permitstack",
                })
        except Exception as e:
            print("  permitstack %s: %s" % (st, e))
        time.sleep(2.2)  # free plan = 30 req/min; ~2.2s keeps us safely under
    return out

# ---------------------------------------------------------------------------
# BLM + U.S. Forest Service NEPA actions on PUBLIC LAND, via the Federal
# Register API filtered by agency (free, no key). State-centroid geocode.
# ---------------------------------------------------------------------------
_GEO_CACHE = {}
_FOREST_RE = re.compile(
    r"([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,4}\s+"
    r"National\s+(?:Forests?|Grasslands?|Recreation Area|Monument|Preserve))")

def _geocode_place(q):
    """Best-effort geocode of a named public land (e.g. a national forest) via
    OpenStreetMap Nominatim. Free; we honor the 1 req/sec usage policy and fall
    back to state placement if it is unavailable. Returns (lat, lng) or None."""
    if not q:
        return None
    if q in _GEO_CACHE:
        return _GEO_CACHE[q]
    res = None
    try:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": q, "format": "json", "limit": 1, "countrycodes": "us"})
        req = urllib.request.Request(url, headers={
            "User-Agent": "activist-project-map/1.0 (wheelock.chris@gmail.com)"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            arr = json.loads(r.read().decode("utf-8", "replace"))
        if arr:
            res = (float(arr[0]["lat"]), float(arr[0]["lon"]))
        time.sleep(1.1)
    except Exception:
        res = None
    _GEO_CACHE[q] = res
    return res

def fetch_public_land_nepa(days=60, per_page=100):
    out = []
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    for slug, label in [("bureau-of-land-management", "BLM"),
                        ("forest-service", "USFS")]:
        q = {"conditions[agencies][]": slug,
             "conditions[type][]": "NOTICE",
             "conditions[publication_date][gte]": since,
             "per_page": per_page, "order": "newest",
             "fields[]": ["title", "abstract", "agencies", "publication_date", "html_url"]}
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
                 "source": "public_land_nepa"}
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


def main():
    items = []
    items += _run("permitstack", fetch_permitstack)             # national construction permits (key)
    items += _run("socrata_permits", lambda: [p for cfg in SOCRATA_CITIES for p in fetch_socrata(cfg)])
    items += _run("federal_register", fetch_federal_register)   # US EIS notices
    items += _run("public_land_nepa", fetch_public_land_nepa)   # BLM + USFS via Federal Register
    items += _run("ejatlas", fetch_ejatlas)                     # global EJ conflicts (local export)
    items = [p for p in items if p.get("lat") is not None and p.get("lng") is not None]
    items = dedup(items)
    items.sort(key=lambda p: -(p.get("impact") or 0))
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
