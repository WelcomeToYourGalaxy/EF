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
import json, sys, os, re, time, datetime, urllib.request, urllib.parse, gzip

UA = "activist-projects-harvester (contact: wheelock.chris@gmail.com)"
TIMEOUT = 30

# ----------------------------------------------------------------------------
# OUTPUT FILE  --  the harvest is committed as gzip (projects.json.gz) because
# the uncompressed JSON exceeds GitHub's 100 MB per-file push limit. No data is
# dropped: the map inflates the .gz client-side. A plain projects.json is read
# as a fallback so an older uncompressed file still carries forward on the first
# run after this change.
# ----------------------------------------------------------------------------
PROJECTS_GZ = "projects.json.gz"
PROJECTS_PLAIN = "projects.json"

def _projects_exists():
    return os.path.exists(PROJECTS_GZ) or os.path.exists(PROJECTS_PLAIN)

def _load_projects():
    """Read the previous harvest (gz preferred, plain fallback). Returns the
    parsed object (dict or list); raises like json.load if neither is present or
    is unreadable, so existing try/except callers behave exactly as before."""
    if os.path.exists(PROJECTS_GZ):
        with gzip.open(PROJECTS_GZ, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(PROJECTS_PLAIN, encoding="utf-8") as f:
        return json.load(f)

def _dump_projects(out):
    """Write the harvest as gzip only. Remove any stale uncompressed file so the
    100 MB-over-limit projects.json can never be what gets committed/pushed."""
    with gzip.open(PROJECTS_GZ, "wt", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    if os.path.exists(PROJECTS_PLAIN):
        try:
            os.remove(PROJECTS_PLAIN)
        except OSError:
            pass

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

def _fmt_usd(v):
    v = float(v)
    if v >= 1e9: return "$%.1fB" % (v / 1e9)
    if v >= 1e6: return "$%.0fM" % (v / 1e6)
    if v >= 1e3: return "$%.0fK" % (v / 1e3)
    return "$%d" % int(v)

def _parse_size(size_str):
    """Extract a real magnitude from a human size string ($ w/ K/M/B suffix,
    acres, hectares, MW, miles/km). Returns whatever it can find; empty if none.
    No guessing: only pulls a number when a recognised unit is actually present."""
    if not size_str:
        return {}
    s = str(size_str); out = {}
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*(b|bn|billion|m|mn|million|k|thousand)?", s, re.I)
    if m:
        v = float(m.group(1).replace(",", "")); suf = (m.group(2) or "").lower()
        if suf in ("b", "bn", "billion"):   v *= 1e9
        elif suf in ("m", "mn", "million"): v *= 1e6
        elif suf in ("k", "thousand"):      v *= 1e3
        out["value_usd"] = v
    a = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:acres?|\bac\b)", s, re.I)
    if a: out["acres"] = float(a.group(1).replace(",", ""))
    hh = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:hectares?|\bha\b)", s, re.I)
    if hh and "acres" not in out: out["acres"] = float(hh.group(1).replace(",", "")) * 2.471
    mw = re.search(r"([\d,]+(?:\.\d+)?)\s*mw\b", s, re.I)
    if mw: out["mw"] = float(mw.group(1).replace(",", ""))
    mi = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:miles?|\bmi\b)", s, re.I)
    if mi: out["miles"] = float(mi.group(1).replace(",", ""))
    km = re.search(r"([\d,]+(?:\.\d+)?)\s*km\b", s, re.I)
    if km and "miles" not in out: out["miles"] = float(km.group(1).replace(",", "")) * 0.621
    return out

def _magnitude_score(size_str, value_usd=None, acres=None, mw=None, miles=None):
    """Rough 1-5 from whatever magnitude field is available."""
    # Fall back to parsing the size string when no explicit magnitude field was set.
    if not any([value_usd, acres, mw, miles]) and size_str:
        ps = _parse_size(size_str)
        value_usd = value_usd or ps.get("value_usd")
        acres = acres or ps.get("acres")
        mw = mw or ps.get("mw")
        miles = miles or ps.get("miles")
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
    """GET JSON with ONE retry on transient failures (timeouts, 5xx, 429). A single
    hiccup used to permanently lose that portal/dataset for the whole run; 4xx
    (other than 429) is a real answer and is not retried."""
    last = None
    for attempt in (0, 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 or e.code >= 500:
                if attempt == 0: time.sleep(2.0); continue
            raise
        except Exception as e:                      # URLError / timeout / reset
            last = e
            if attempt == 0: time.sleep(2.0); continue
            raise
    raise last

def _num(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def _money(x):
    """Parse a currency value that may be numeric or a string like '$5,000,000.00'."""
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    s = str(x).strip().replace("$", "").replace(",", "").replace(" ", "")
    if s in ("", "-", "."): return None
    try: return float(s)
    except ValueError: return None

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
    # Honolulu, HI -- verified dataset id 4vab-c87q on data.honolulu.gov (Socrata).
    # Columns weren't fetchable at build time, so this uses heuristic mode: the
    # harvester reads the live schema and self-maps lat/lng/value/date/status.
    {"city": "Honolulu, HI", "domain": "data.honolulu.gov", "dataset": "4vab-c87q",
     "heuristic": True, "limit": 5000},
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
    # Cincinnati, OH -- verified against live CSV headers (dataset dy5r-w456):
    # LATITUDE/LONGITUDE, ESTPROJECTCOSTDEC, STATUSCURRENT, ISSUEDDATE, DESCRIPTION.
    {"city": "Cincinnati, OH", "domain": "data.cincinnati-oh.gov", "dataset": "dy5r-w456",
     "lat": "latitude", "lng": "longitude", "name": "description", "type": "permittypemapped",
     "value": "estprojectcostdec", "status": "statuscurrent",
     "where": "estprojectcostdec > 5000000 AND issueddate > '2025-01-01'"},

    # New York, NY -- BIS "DOB Job Application Filings" (ic3t-wcy2). Confirmed geo
    # columns gis_latitude/gis_longitude + initial_cost/job_description/job_type.
    # initial_cost is a TEXT "$" field, so the value floor is enforced Python-side
    # (min_value) rather than in $where; the $where filters by recent action date.
    # NB: BIS is being superseded by DOB NOW (which lacks coords); this captures
    # jobs with recent activity. First Actions run will confirm the date $where.
    {"city": "New York, NY", "domain": "data.cityofnewyork.us", "dataset": "ic3t-wcy2",
     "lat": "gis_latitude", "lng": "gis_longitude", "name": "job_description",
     "type": "job_type", "value": "initial_cost", "min_value": 5000000,
     "status": "job_status_descrp", "limit": 5000, "order": "latest_action_date DESC",
     "where": "latest_action_date > '2025-01-01'"},

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

def _socrata_detect(row):
    """Given one sample Socrata record, guess the column API-names for each role
    (lat/lng or point, name, type, value, date, status) by name/value pattern.
    Lets a city be added from a VERIFIED (domain, dataset-id) alone -- no guessed
    column names -- with the harvester reading the real schema at run time."""
    keys = list(row.keys()); low = {k: k.lower() for k in keys}
    det = {}
    def find(pred):
        for k in keys:
            if pred(low[k], row.get(k)): return k
        return None
    for k in keys:                                   # point column
        v = row.get(k); lk = low[k]
        if isinstance(v, dict) and (v.get("coordinates") or (v.get("latitude") and v.get("longitude"))):
            det["point"] = k; break
        if isinstance(v, str) and v.upper().startswith("POINT"):
            det["point"] = k; break
        if lk in ("location", "the_geom", "georeference", "geocoded_column", "point", "location_1"):
            det["point"] = k; break
    det["lat"] = find(lambda lk, v: lk == "latitude" or lk in ("lat", "gis_latitude", "point_latitude", "y_coordinate") or lk.endswith("_latitude"))
    det["lng"] = find(lambda lk, v: lk == "longitude" or lk in ("lng", "lon", "long", "gis_longitude", "point_longitude", "x_coordinate") or lk.endswith("_longitude"))
    det["value"] = find(lambda lk, v: "id" not in lk and "unit" not in lk and ("valuation" in lk or "estprojectcost" in lk or "estimated_cost" in lk or "reported_cost" in lk or "job_cost" in lk or (("cost" in lk or "value" in lk) and "valuation" not in lk)))
    det["date"] = find(lambda lk, v: ("issue" in lk and "date" in lk) or lk in ("issued_date", "issue_date", "issueddate", "permit_issue_date", "date_issued", "issued"))
    det["status"] = find(lambda lk, v: "status" in lk)
    det["name"] = (find(lambda lk, v: lk in ("description", "work_description", "job_description", "proposedworkdescription", "desc_of_work", "descriptionofwork", "scope_of_work"))
                   or find(lambda lk, v: "description" in lk or "scope" in lk))
    det["type"] = (find(lambda lk, v: lk in ("permit_type", "permittype", "type", "permit_class", "permitclass", "job_type", "permittypemapped", "permitclassmapped", "permit_type_definition"))
                   or find(lambda lk, v: "permit" in lk and "type" in lk))
    return {k: v for k, v in det.items() if v}

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

def fetch_socrata(cfg, limit=5000):
    out = []
    base = "https://{d}/resource/{ds}.json".format(d=cfg["domain"], ds=cfg["dataset"])
    if cfg.get("heuristic"):                         # self-detect columns from a live sample
        cfg = dict(cfg)
        try:
            probe = _get_json(base + "?$limit=1")
        except Exception as e:
            print("  socrata %s probe failed: %s" % (cfg.get("city"), e)); return out
        if not probe:
            print("  socrata %s: empty probe" % cfg.get("city")); return out
        det = _socrata_detect(probe[0])
        print("  socrata %s auto-detected columns: %s" % (cfg.get("city"), det))
        for k, v in det.items(): cfg.setdefault(k, v)
        vcol, dcol = cfg.get("value"), cfg.get("date")
        if not cfg.get("where"):                     # bound + significance from detected cols
            clauses = []
            if dcol: clauses.append("%s > '2024-01-01'" % dcol)
            if vcol:
                sv = probe[0].get(vcol)
                if isinstance(sv, (int, float)) or (isinstance(sv, str) and sv.replace(".", "", 1).replace("-", "", 1).isdigit()):
                    clauses.append("%s > 5000000" % vcol)      # only if numeric (text-$ handled below)
            if clauses: cfg["where"] = " AND ".join(clauses)
        if dcol and not cfg.get("order"): cfg["order"] = dcol + " DESC"
        if cfg.get("min_value") is None: cfg["min_value"] = 5000000   # python-side floor (handles text-$)
    params = {"$limit": cfg.get("limit", limit), "$order": cfg.get("order", ":id")}
    if cfg.get("where"): params["$where"] = cfg["where"]
    url = base + "?" + urllib.parse.urlencode(params)
    try:
        rows = _get_json(url)
    except Exception as e:
        print("  socrata %s failed: %s" % (cfg.get("city"), e)); return out
    _DONE = ("complete", "closed", "expired", "withdrawn", "cancel", "final",
             "void", "revoked", "stop work", "inactive", "issued - closed",
             "certificate of occupancy", "signed off", "sign-off")
    for r in rows:
        lat, lng = _socrata_point(r, cfg)
        if lat is None or lng is None: continue
        _st = str(r.get(cfg.get("status")) or "").lower()
        if any(k in _st for k in _DONE):   # drop projects that are no longer active
            continue
        val = _money(r.get(cfg.get("value")))
        mv = cfg.get("min_value")
        if mv is not None and (val is None or val < mv):
            continue        # value floor enforced here for cities whose cost column
                            # is text ($-prefixed) and can't be filtered server-side
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
# ----------------------------------------------------------------------------
# AUTO-DISCOVERY -- Socrata cross-domain Discovery API                [US LOCAL]
# ----------------------------------------------------------------------------
# Instead of hand-adding cities one by one, query Socrata's Discovery API for
# every building/construction-permit dataset across all portals and harvest each
# through the heuristic path. Strong quality gates keep it high-signal: the
# dataset NAME must read like building/construction permits (aggregates/dashboards
# excluded), it must be fresh, and each permit must have coordinates AND clear the
# $5M value floor (enforced inside fetch_socrata). Isolated source group so it can
# be reviewed / toggled independently of the hand-curated cities. CAPPED to bound
# runtime. Runs on Actions (which can reach api.us.socrata.com); sandbox cannot.
_DISCOVERY_CAP = 200
_DISCOVERY_PORTAL_CAP = 400      # max significant permits kept per discovered portal (flood guard)
_DISCOVERY_TOTAL_CAP = 5000      # max significant permits kept per discovery engine
def fetch_socrata_discovered(max_datasets=_DISCOVERY_CAP):
    import re, datetime, urllib.parse
    disc = "http://api.us.socrata.com/api/catalog/v1?" + urllib.parse.urlencode(
        {"q": "building permits", "only": "dataset", "limit": 250})
    try:
        cat = _get_json(disc)
    except Exception as e:
        print("  socrata discovery failed: %s" % e); return []
    results = cat.get("results", []) if isinstance(cat, dict) else []
    configured = {c.get("domain") for c in SOCRATA_CITIES}
    skip_domains = configured | {"data.austintexas.gov", "data.sfgov.org", "data.lacity.org",
                                 "data.cityofchicago.org", "data.seattle.gov", "data.cityofnewyork.us",
                                 "data.cincinnati-oh.gov"}
    cutoff = (datetime.date.today() - datetime.timedelta(days=550)).isoformat()
    NAMEOK = re.compile(r'permit', re.I)
    KIND = re.compile(r'\b(building|construction|development)\b', re.I)
    BAD = re.compile(r'count|summ|metric|monthly|annual|aggregate|dashboard|by year|statistic|'
                     r'trade|electrical|plumbing|mechanical|sign\b|solar|roof|demolition only|fee', re.I)
    picked = []; seen = set()
    for r in results:
        res = r.get("resource", {}) or {}; meta = r.get("metadata", {}) or {}
        did = res.get("id", ""); name = res.get("name", "") or ""; domain = meta.get("domain", "") or ""
        updated = (res.get("updatedAt") or res.get("data_updated_at") or "")[:10]
        if not did or not domain or domain in skip_domains: continue
        if not (NAMEOK.search(name) and KIND.search(name)) or BAD.search(name): continue
        if updated and updated < cutoff: continue                   # stale -> skip
        key = (domain, did)
        if key in seen: continue
        seen.add(key)
        picked.append({"city": domain, "domain": domain, "dataset": did,
                       "heuristic": True, "limit": 3000, "_name": name})
        if len(picked) >= max_datasets: break
    print("  socrata discovery: %d fresh permit datasets to probe" % len(picked))
    out = []
    for cfg in picked:
        try:
            rows = fetch_socrata(dict(cfg))               # heuristic detect + $5M gate inside
        except Exception as e:
            print("  discovery %s failed: %s" % (cfg["domain"], e)); continue
        if len(rows) > _DISCOVERY_PORTAL_CAP:
            print("    ! %s capped %d->%d" % (cfg["domain"], len(rows), _DISCOVERY_PORTAL_CAP)); rows = rows[:_DISCOVERY_PORTAL_CAP]
        for p in rows: p["source"] = "socrata_discovered:" + cfg["domain"]
        if rows: print("    + %-32s %4d permits (%s)" % (cfg["domain"], len(rows), cfg["_name"][:40]))
        out += rows
        if len(out) >= _DISCOVERY_TOTAL_CAP:
            print("  socrata discovery: total cap %d hit, stopping" % _DISCOVERY_TOTAL_CAP); out = out[:_DISCOVERY_TOTAL_CAP]; break
    print("  socrata discovery: %d significant permits from %d portals" % (len(out), len(picked)))
    return out

# ----------------------------------------------------------------------------
# (Land Matrix now lives further down as a LIVE auto-pulling source built from
#  the datasets/land-matrix GitHub mirror -- see fetch_land_matrix().)
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# SOURCE 3 -- Global Energy Monitor trackers                     [ENERGY INFRA]
# ----------------------------------------------------------------------------
# GEM publishes downloadable trackers (Excel/CSV) under a data-use policy at
# globalenergymonitor.org/projects/. Pipeline: download the relevant tracker(s),
# read with pandas, keep US rows with coords, map columns -> normalized dict.
# only NEW / upcoming energy projects -- never operating or retired infrastructure
_GEM_NEW = ("announced", "pre-construction", "preconstruction", "construction",
            "proposed", "permitted", "pre-permit", "in development", "planned")
_GEM_DEAD = ("operating", "retired", "cancelled", "canceled", "mothballed",
             "shelved", "closed", "abandoned", "decommissioned", "shut in",
             "discovered")            # "discovered" (goget) = resource found, not a project yet

_GEM_TRACKERS = [
    # (config slug, category, source suffix, human label, tracker landing page)
    # category drives the map toggle: fossil / renewable / industry
    ("coal-plant",     "fossil",    "coal",       "coal power plant",     "https://globalenergymonitor.org/projects/global-coal-plant-tracker/"),
    ("coal-mine",      "fossil",    "coalmine",   "coal mine",            "https://globalenergymonitor.org/projects/global-coal-mine-tracker/"),
    ("ggit",           "fossil",    "gas",        "gas infrastructure",   "https://globalenergymonitor.org/projects/global-gas-infrastructure-tracker/"),
    ("GOIT",           "fossil",    "oil",        "oil infrastructure",   "https://globalenergymonitor.org/projects/global-oil-infrastructure-tracker/"),
    ("gas-plant",      "fossil",    "gasplant",   "gas power plant",      "https://globalenergymonitor.org/projects/global-gas-plant-tracker/"),
    ("goget",          "fossil",    "extraction", "oil & gas extraction", "https://globalenergymonitor.org/projects/global-oil-gas-extraction-tracker/"),
    ("coal-terminals", "fossil",    "coalterm",   "coal terminal",        "https://globalenergymonitor.org/projects/global-coal-terminals-tracker/"),
    ("nuclear",        "renewable", "nuclear",    "nuclear power plant",  "https://globalenergymonitor.org/projects/global-nuclear-power-tracker/"),
    ("geothermal",     "renewable", "geothermal", "geothermal power plant","https://globalenergymonitor.org/projects/global-geothermal-power-tracker/"),
    ("wind",           "renewable", "wind",       "wind farm",            "https://globalenergymonitor.org/projects/global-wind-power-tracker/"),
    ("solar",          "renewable", "solar",      "solar farm",           "https://globalenergymonitor.org/projects/global-solar-power-tracker/"),
    ("hydro",          "renewable", "hydropower", "hydropower project",   "https://globalenergymonitor.org/projects/global-hydropower-tracker/"),
    ("bioenergy",      "renewable", "bioenergy",  "bioenergy power plant","https://globalenergymonitor.org/projects/global-bioenergy-power-tracker/"),
    ("giomt",          "industry",  "ironore",    "iron ore mine",        "https://globalenergymonitor.org/projects/global-iron-ore-mine-tracker/"),
    ("gist",           "industry",  "steel",      "iron & steel plant",   "https://globalenergymonitor.org/projects/global-iron-and-steel-tracker/"),
    ("gcct",           "industry",  "cement",     "cement plant",         "https://globalenergymonitor.org/projects/global-cement-and-concrete-tracker/"),
]

def _gem_read_config(slug):
    """Read a GEM map tracker's live config.js (production branch) and return its
    current data URL (geojson OR csv) + the field names that tracker uses. GEM
    rewrites config.js on every release, so this auto-follows to the newest data
    with no manual step. Field names come from the config, never guessed."""
    import urllib.request
    url = ("https://raw.githubusercontent.com/GlobalEnergyMonitor/maps/"
           "gitpages-production/trackers/%s/config.js" % slug)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        txt = r.read().decode("utf-8", "replace")
    def g(pat, default=None):
        m = re.search(pat, txt); return m.group(1) if m else default
    return {
        "geojson":  g(r"""geojson\s*:\s*['"]([^'"]+)['"]"""),
        "csv":      g(r"""csv\s*:\s*['"]([^'"]+)['"]"""),
        "name":     g(r"""nameField\s*:\s*['"]([^'"]+)['"]""", "name"),
        "country":  g(r"""countryField\s*:\s*['"]([^'"]+)['"]"""),
        "status":   (g(r"""statusField\s*:\s*['"]([^'"]+)['"]""")
                     or g(r"""field\s*:\s*['"]([^'"]+)['"]""", "status")),  # prefer statusField; else color.field
        "capacity": g(r"""capacityField\s*:\s*['"]([^'"]+)['"]"""),
        "caplabel": (g(r"""capacityLabel\s*:\s*['"]([^'"]*)['"]""", "") or "").strip("() "),
    }

def _gem_status_ok(status):
    """In-process gate, fail-safe: keep ONLY announced/proposed/pre-permit/permitted/
    construction/in-development; drop operating/retired/cancelled/etc.; anything
    unrecognized is dropped (never included). Underscore-normalized so GOGET's
    'in_development' matches, and 'discovered'/'shut_in' fall to _GEM_DEAD."""
    s = str(status or "").strip().lower().replace("_", " ")
    if any(k in s for k in _GEM_DEAD): return False
    return any(k in s for k in _GEM_NEW)

def _gem_feature(pr, lat, lng, fld, label, source, landing):
    """Build one project dict from a mapped record. fld = {name,country,status,
    capacity,caplabel} of the ACTUAL keys for this tracker (config fields for
    geojson, detected columns for csv)."""
    if lat is None or lng is None: return None
    raw_status = pr.get(fld["status"]) if fld.get("status") else ""
    if not _gem_status_ok(raw_status): return None
    status = str(raw_status or "").strip().lower().replace("_", " ")
    name = pr.get(fld["name"]) if fld.get("name") else None
    ctry = pr.get(fld["country"]) if fld.get("country") else ""
    if ctry: ctry = str(ctry).strip().strip(";").strip()
    cap = _num(pr.get(fld["capacity"])) if fld.get("capacity") else None
    unit = fld.get("caplabel") or ""
    size = ("%s %s" % ("{:,}".format(int(cap)), unit)).strip() if cap else ""
    url = ""
    for k, v in pr.items():
        if isinstance(v, str) and v.startswith("http"): url = v.strip(); break
    def _ci(*keys):                        # case-insensitive owner/operator lookup (csv headers vary)
        lk = {str(k).lower(): v for k, v in pr.items()}
        for want in keys:
            if lk.get(want): return lk[want]
        return ""
    p = {"name": (str(name) or label)[:130], "type": label[0].upper() + label[1:],
         "state": str(ctry or "")[:60], "lat": lat, "lng": lng, "precise": True,
         "value_usd": None,
         "company": str(_ci("owner", "parent", "operator") or "")[:80],
         "status": str(raw_status or ""), "size": size,
         "url": url or landing,
         "desc": ("Proposed or under-construction %s tracked by Global Energy Monitor "
                  "(status: %s). Pulled live from GEM's tracker map data." % (label, status or "unknown")),
         "source": source}
    p["impact"] = rate_project(p, sensitivity=1)
    return p

def _gem_read_csv(url):
    """Fetch a GEM tracker CSV and return list-of-dicts. Handles UTF-8, UTF-8-BOM
    and UTF-16 (GOGET is UTF-16) via byte-order-mark sniffing."""
    import urllib.request, csv as _csv, io
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):   enc = "utf-16"
    elif raw[:3] == b"\xef\xbb\xbf":            enc = "utf-8-sig"
    else:                                       enc = "utf-8"
    txt = raw.decode(enc, "replace")
    return list(_csv.DictReader(io.StringIO(txt)))

def _gem_csv_fields(cols):
    """Runtime column detection for a GEM CSV -- headers differ from the geojson
    field names (e.g. 'Project Name'/'Status'/'Capacity (MW)'), so match by pattern,
    case-insensitively, rather than trusting the config's geojson field names."""
    low = {c.lower(): c for c in cols}
    def pick(exacts, contains=None, exclude=()):
        for e in exacts:
            if e in low: return low[e]
        if contains:
            for c in cols:
                cl = c.lower()
                if contains in cl and not any(x in cl for x in exclude): return c
        return None
    capcol = pick({"capacity (mw)", "capacity"}, "capacity")
    unit = ""
    if capcol:
        m = re.search(r"\(([^)]+)\)", capcol)
        if m: unit = m.group(1).strip()
    return {
        "lat":     pick({"lat", "latitude", "y"}),
        "lng":     pick({"lng", "lon", "long", "longitude", "x"}),
        "status":  pick({"status"}, "status"),
        "name":    pick({"project name", "unit_name", "unit name", "name", "wiki-name",
                         "coal-terminal-name"}, "name", exclude=("local language", "phase", "script")),
        "country": pick({"country", "country/area", "areas"}, "countr"),
        "capacity": capcol,
        "caplabel": unit,
    }

def _gem_csv_url(cfg_csv, slug):
    """Resolve a tracker's csv value to a fetchable URL. Full https URLs are used
    as-is (e.g. coal-terminals -> DigitalOcean CDN). A relative filename ('data.csv',
    'GOGET_...csv') is committed to the maps repo *main* branch (not the production
    deploy branch), so resolve it there -- raw-reachable and auto-updating."""
    if not cfg_csv: return None
    if cfg_csv.lower().startswith("http"): return cfg_csv
    return ("https://raw.githubusercontent.com/GlobalEnergyMonitor/maps/"
            "main/trackers/%s/%s" % (slug, cfg_csv.lstrip("./")))

def fetch_gem():
    """Global Energy Monitor -- proposed / under-construction energy & industry
    projects worldwide, grouped for the map into fossil (coal plants+mines, gas, oil,
    gas plants, extraction, coal terminals), renewable (wind, solar, hydro, nuclear,
    geothermal) and industry (iron-ore mines). Fully auto-pulling: each tracker's
    live config.js (GEM maps production branch) gives the current data URL + field
    names, so it follows every GEM release. geojson data sits on GEM's CDN; csv data
    sits on the repo main branch. Only pipeline-stage projects are kept; operating/
    retired/cancelled are excluded (fail-safe gate)."""
    import urllib.request, json as _json
    out = []
    for slug, cat, suf, label, landing in _GEM_TRACKERS:
        source = "gem_%s:%s" % (cat, suf)
        try:
            cfg = _gem_read_config(slug)
        except Exception as e:
            print("  gem %s: config fetch failed: %s" % (slug, e)); continue
        gj = cfg.get("geojson"); csvv = cfg.get("csv")
        n = 0
        if gj and gj.lower().endswith(".geojson"):
            try:
                req = urllib.request.Request(gj, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=120) as r:
                    geo = _json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:
                print("  gem %s: geojson fetch failed: %s" % (slug, e)); continue
            fld = {"name": cfg.get("name"), "country": cfg.get("country"),
                   "status": cfg.get("status"), "capacity": cfg.get("capacity"),
                   "caplabel": cfg.get("caplabel")}
            for ft in (geo.get("features", []) if isinstance(geo, dict) else []):
                try:
                    g = ft.get("geometry") or {}; pr = ft.get("properties") or {}
                    if g.get("type") == "Point":
                        c = g.get("coordinates") or []
                        if len(c) < 2: continue
                        la, lo = _num(c[1]), _num(c[0])
                    else:                                # LineString/Polygon (pipelines) -> centroid
                        ctr = _geom_center(g)
                        if not ctr: continue
                        la, lo = ctr
                    p = _gem_feature(pr, la, lo, fld, label, source, landing)
                    if p: out.append(p); n += 1
                except Exception:
                    continue
            print("  gem %s: %d pipeline features (%s)" % (slug, n, gj.rsplit("/", 1)[-1]))
        elif csvv:
            url = _gem_csv_url(csvv, slug)
            try:
                rows = _gem_read_csv(url)
            except Exception as e:
                print("  gem %s: csv fetch failed: %s" % (slug, e)); continue
            if not rows:
                print("  gem %s: csv empty (skip)" % slug); continue
            fld = _gem_csv_fields(list(rows[0].keys()))
            if not (fld["lat"] and fld["lng"]):
                print("  gem %s: csv coord columns not found (skip)" % slug); continue
            for row in rows:
                try:
                    la, lo = _num(row.get(fld["lat"])), _num(row.get(fld["lng"]))
                    if la is None or lo is None: continue
                    p = _gem_feature(row, la, lo, fld, label, source, landing)
                    if p: out.append(p); n += 1
                except Exception:
                    continue
            print("  gem %s: %d pipeline rows (%s)" % (slug, n, url.rsplit("/", 1)[-1]))
        else:
            print("  gem %s: no data url in config (skip)" % slug); continue
    print("  gem: %d total pipeline projects across %d trackers" % (len(out), len(_GEM_TRACKERS)))
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

def fetch_federal_register(days=180, per_page=100):
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

_SRC_COUNTS = {}      # name -> rows returned this run (populated by _run)
_RUN_FLAGS = []       # collected truncation / field-detection / failure notes

def _flag(msg):
    """Record a first-run review note; also echoed inline so it's greppable."""
    _RUN_FLAGS.append(msg)
    print("  [flag] " + msg)

def _run(name, fn):
    """Run one source in isolation so a single failure can't kill the harvest."""
    try:
        got = fn() or []
        _SRC_COUNTS[name] = _SRC_COUNTS.get(name, 0) + len(got)
        print("  %-18s %d" % (name + ":", len(got)))
        return got
    except Exception as e:
        _SRC_COUNTS.setdefault(name, 0)
        _RUN_FLAGS.append("%s FAILED: %s" % (name, e))
        print("  %-18s FAILED: %s" % (name + ":", e))
        return []


def _print_diagnostics():
    """Consolidated, greppable per-source summary -- the first-run diagnostic log.
    Zero-yield sources and truncation/field flags are what to review after run 1."""
    if not _SRC_COUNTS:
        return
    print("\n=== FIRST-RUN DIAGNOSTIC (per-source yields, pre-dedup) ===")
    for nm, n in sorted(_SRC_COUNTS.items(), key=lambda kv: -kv[1]):
        print("  %-24s %7d" % (nm, n))
    total = sum(_SRC_COUNTS.values())
    active = sum(1 for v in _SRC_COUNTS.values() if v > 0)
    print("  " + "-" * 34)
    print("  %-24s %7d" % ("TOTAL (pre-dedup)", total))
    print("  sources reporting:       %d / %d" % (active, len(_SRC_COUNTS)))
    zero = sorted(k for k, v in _SRC_COUNTS.items() if v == 0)
    if zero:
        print("  ZERO-YIELD (review):     " + ", ".join(zero))
    if _RUN_FLAGS:
        print("  FLAGS (%d):" % len(_RUN_FLAGS))
        for m in _RUN_FLAGS:
            print("    - " + m)
    print("=== END DIAGNOSTIC ===\n")


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
                "size": size, "value_usd": (float(val) if val else None), "status": "Permit on file",
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
_GEO_MAX = 200  # Nominatim politeness budget per run (1 req/sec)
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

def fetch_public_land_nepa(days=270, per_page=100):
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

def fetch_world_bank(rows=1000, max_pages=10):
    cents = _wb_country_centroids()
    if not cents:
        print("  world bank: no country centroids (skip)"); return []
    fl = ("id,project_name,countryname,countryshortname,countrycode,totalamt,"
          "totalcommamt,boardapprovaldate,sector1,status,regionname")
    all_projs = []
    for pg in range(max_pages):
        url = ("https://search.worldbank.org/api/v2/projects?format=json"
               "&status_exact=Active&rows=%d&os=%d&fl=%s" % (rows, pg * rows, urllib.parse.quote(fl)))
        try:
            data = _get_json(url)
        except Exception as e:
            print("  world bank page %d failed: %s" % (pg, e)); break
        projs = data.get("projects", data) if isinstance(data, dict) else data
        if isinstance(projs, dict): projs = list(projs.values())
        n = len(projs) if projs else 0
        if not projs: break
        all_projs.extend(projs)
        if n < rows: break
        time.sleep(0.3)
    else:
        _flag("world_bank hit page cap (%d x %d) -- may be truncated" % (max_pages, rows))
    projs = all_projs
    print("  world bank: %d active projects returned" % len(projs))
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
                size = _fmt_usd(amtf) if amtf else ""   # World Bank totalamt is USD; format cleanly
            except Exception:
                amtf = 0; size = ""
            sec = pr.get("sector1")
            if isinstance(sec, dict): sec = sec.get("Name") or sec.get("name") or ""
            p = {"name": (pr.get("project_name") or "World Bank project")[:140],
                 "type": (sec or "Development project"),
                 "state": str(cn),
                 "lat": round(_lat, 5), "lng": round(_lng, 5), "precise": _precise,
                 "size": size, "value_usd": (amtf or None), "status": str(pr.get("status") or "Active"),
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

def fetch_iati(per=1000, max_pages=40):
    base = "https://datastore.codeforiati.org/api/1/access/activity.json"
    out = []; scanned = 0; withloc = 0; shape_shown = False
    for cc in _IATI_COUNTRIES:
        offset = 0; pages = 0; capped = True
        while pages < max_pages:
            params = {"recipient-country": cc, "activity-status": "2",
                      "limit": per, "offset": offset, "unwrap": "True"}
            try:
                data = _get_json(base + "?" + urllib.parse.urlencode(params))
            except Exception as e:
                print("  iati %s failed: %s" % (cc, e))
                capped = False; break
            if not shape_shown:
                shape_shown = True
                if isinstance(data, dict):
                    print("  iati [shape] dict keys: %s" % list(data.keys())[:8])
                else:
                    print("  iati [shape] type: %s len: %s" % (type(data).__name__, len(data) if hasattr(data, "__len__") else "?"))
            acts = _iati_activities(data)
            if not acts:
                capped = False; break
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
            pages += 1; offset += per
            time.sleep(0.4)
            if len(acts) < per:          # last page for this country
                capped = False; break
        if capped:                        # stopped because we hit max_pages, not the end
            _flag("iati %s hit page cap (%d x %d) -- recipient may be truncated" % (cc, max_pages, per))
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
    # additional development-application types (English)
    ("site plan applications", "site plan"), ("rezoning applications", "rezoning"),
    ("subdivision applications", "subdivision"),
    # additional permit languages (each still runs through the significance gate)
    ("stavebni povoleni", "stavebn"), ("stavebne povolenie", "stavebn"),
    ("yapi ruhsati", "ruhsat"), ("autorizatie de construire", "autoriza"),
    ("epitesi engedely", "epitesi"), ("izin mendirikan bangunan", "izin"),
    ("permiso de construccion", "permiso"), ("oikodomiki adeia", "adeia"),
    # environmental-assessment / consent vocabulary -- broad reach; catches New Zealand
    # council resource consents (abundant + CC-BY on ArcGIS Hub) and English-titled EIA
    # datasets worldwide. All still pass the significance + geometry gates.
    ("environmental impact assessment", "environmental"), ("environmental clearance", "clearance"),
    ("resource consent", "consent"), ("development consent", "consent"),
    # East Asian scripts for the under-covered Pacific-rim gap (native dataset names)
    ("\u5efa\u7bc9\u78ba\u8a8d", "\u5efa\u7bc9"),          # Japanese -- building confirmation
    ("\u958b\u767a\u8a31\u53ef", "\u958b\u767a"),          # Japanese -- development permit
    ("\uac74\ucd95\ud5c8\uac00", "\uac74\ucd95"),          # Korean -- building permit
    ("\uac1c\ubc1c\ud589\uc704\ud5c8\uac00", "\uac1c\ubc1c"),  # Korean -- development-act permit
    # Spanish + Portuguese environmental-assessment / licensing (Latin America has
    # dense gov ArcGIS Hub coverage: Colombia, Mexico, Peru, Chile, Brazil councils).
    ("evaluacion de impacto ambiental", "impacto ambiental"),
    ("estudio de impacto ambiental", "impacto ambiental"),
    ("licencia ambiental", "ambiental"), ("licenciamento ambiental", "ambiental"),
    ("avaliacao de impacto ambiental", "impacto ambiental"),
    # French environmental-assessment (France, Francophone Africa, Quebec)
    ("etude d'impact environnemental", "impact"), ("autorisation environnementale", "environnement"),
    ("evaluation environnementale", "environnement"),
    # more Spanish/Portuguese assessment variants (Chile/Peru "DIA"; Brazilian EIA)
    ("declaracion de impacto ambiental", "impacto ambiental"),
    ("estudo de impacto ambiental", "impacto ambiental"),
    # Arabic (MENA) + Russian (CIS) construction-permit terms -- native dataset names
    ("\u0631\u062e\u0635\u0629 \u0628\u0646\u0627\u0621", "\u0631\u062e\u0635\u0629"),   # Arabic -- building licence
    ("\u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u0435 \u043d\u0430 \u0441\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c\u0441\u0442\u0432\u043e", "\u0441\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c\u0441\u0442\u0432"),  # Russian -- construction permit
    # Western/Central-European languages with dense gov ArcGIS Hub coverage
    ("baugenehmigung", "baugenehmigung"), ("bebauungsplan", "bebauungsplan"),   # German
    ("permesso di costruire", "costruire"),                                     # Italian -- building permit
    ("valutazione di impatto ambientale", "impatto ambientale"),                # Italian -- EIA (VIA)
    ("omgevingsvergunning", "omgevingsvergunning"),                             # Dutch -- environmental/building permit
    ("pozwolenie na budowe", "pozwolenie"),                                     # Polish -- building permit
    # Simplified Chinese (construction permit + construction-project EIA)
    ("\u5efa\u7b51\u65bd\u5de5\u8bb8\u53ef", "\u65bd\u5de5\u8bb8\u53ef"),                    # building construction permit
    ("\u5efa\u8bbe\u9879\u76ee\u73af\u5883\u5f71\u54cd\u8bc4\u4ef7", "\u73af\u5883\u5f71\u54cd"),  # construction-project EIA
    # Nordic building permits (Sweden/Norway/Denmark/Finland -- strong open data)
    ("bygglov", "bygglov"), ("byggetillatelse", "byggetillatelse"),
    ("byggetilladelse", "byggetilladelse"), ("rakennuslupa", "rakennuslupa"),
    # Baltic / Balkan (Latin script) + Mexican EIA term (MIA)
    ("statybos leidimas", "statybos"),                                   # Lithuanian -- building permit
    ("gradjevinska dozvola", "dozvola"),                                 # Serbian/Croatian/Bosnian
    ("manifestacion de impacto ambiental", "impacto ambiental"),         # Mexico -- MIA
    # Ukrainian + Bulgarian (Cyrillic, distinct from Russian) + Hebrew (Israel)
    ("\u0434\u043e\u0437\u0432\u0456\u043b \u043d\u0430 \u0431\u0443\u0434\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e", "\u0431\u0443\u0434\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e"),  # Ukrainian -- construction permit
    ("\u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u0435 \u0437\u0430 \u0441\u0442\u0440\u043e\u0435\u0436", "\u0441\u0442\u0440\u043e\u0435\u0436"),  # Bulgarian -- building permit
    ("\u05d4\u05d9\u05ea\u05e8 \u05d1\u05e0\u05d9\u05d9\u05d4", "\u05d4\u05d9\u05ea\u05e8"),   # Hebrew -- building permit
    # further coverage: SE Asia, Iberia/Catalonia, more of the Nordics/Baltics/Balkans
    ("gi\u1ea5y ph\u00e9p x\u00e2y d\u1ef1ng", "x\u00e2y d\u1ef1ng"),        # Vietnamese -- construction permit
    ("alvara de construcao", "alvar"),                                       # Portuguese -- building permit (alvara)
    ("llicencia d'obres", "obres"),                                          # Catalan -- works licence
    ("gradbeno dovoljenje", "dovoljenje"),                                   # Slovenian -- building permit
    ("ehitusluba", "ehitusluba"),                                            # Estonian -- building permit
    ("byggingarleyfi", "byggingarleyfi"),                                    # Icelandic -- building permit
    ("relatorio de impacto ambiental", "impacto ambiental"),                 # Brazil -- RIMA
    # English environmental-consent variants (South Africa "EA", EU permits)
    ("environmental authorisation", "authorisation"), ("environmental permit", "environmental permit"),
    # SE Asia building permits + native EIA-procedure names (catch assessment datasets
    # that the building-permit terms miss) for Norway/Germany/Sweden/Turkey
    ("\u0e43\u0e1a\u0e2d\u0e19\u0e38\u0e0d\u0e32\u0e15\u0e01\u0e48\u0e2d\u0e2a\u0e23\u0e49\u0e32\u0e07", "\u0e01\u0e48\u0e2d\u0e2a\u0e23\u0e49\u0e32\u0e07"),  # Thai -- construction permit
    ("kebenaran merancang", "merancang"),                                    # Malay -- planning permission
    ("konsekvensutredning", "konsekvensutredning"),                          # Norwegian -- EIA
    ("umweltvertraglichkeitsprufung", "umweltvertr"),                        # German -- UVP (EIA)
    ("miljokonsekvensbeskrivning", "konsekvens"),                            # Swedish -- MKB (EIA)
    ("cevresel etki degerlendirmesi", "etki"),                               # Turkish -- CED (EIA)
    # SE Asia EIA regimes + extraction projects (global)
    ("amdal", "amdal"),                                                      # Indonesia -- AMDAL (EIA)
    ("environmental compliance certificate", "compliance certificate"),      # Philippines -- ECC
    ("mining lease", "mining lease"),                                        # extraction projects worldwide
    # English planning / NEPA-style environmental-review vocabulary (global reach)
    ("environmental impact statement", "impact statement"), ("record of decision", "record of decision"),
    ("notice of intent", "notice of intent"), ("outline planning permission", "outline"),
    ("major development", "major development"), ("infrastructure project", "infrastructure project"),
    # major-infrastructure consent + expropriation regimes (the biggest land-takings)
    ("development consent order", "consent order"),                          # UK -- DCO (nationally significant infra)
    ("declaration d'utilite publique", "utilite publique"),                  # France -- DUP (expropriation)
    ("planfeststellung", "planfeststellung"),                                # Germany -- plan approval (rail/road/airports)
    ("decyzja srodowiskowa", "srodowiskowa"),                                # Poland -- environmental decision
    ("concesion minera", "concesion minera"),                                # Spanish -- mining concession
    ("\u74b0\u5883\u5f71\u97ff\u8a55\u4fa1", "\u74b0\u5883\u5f71\u97ff"),                # Japanese -- EIA
    ("\ud658\uacbd\uc601\ud5a5\ud3c9\uac00", "\ud658\uacbd\uc601\ud5a5"),                # Korean -- EIA
    ("strategic environmental assessment", "strategic environmental"),       # SEA (plans/programmes)
    # more national permit/authorization regimes (all process datasets -> in-process)
    ("permis de construire", "permis de construire"),                        # France -- building permit
    ("permis d'amenager", "amenager"),                                       # France -- development permit
    ("installation classee", "installation classee"),                        # France -- ICPE (industrial)
    ("autorizzazione unica", "autorizzazione unica"),                        # Italy -- single authorization (energy/infra)
    ("bauantrag", "bauantrag"),                                              # Germany -- building application
    ("immissionsschutz", "immissionsschutz"),                                # Germany -- BImSchG (industrial permit)
    ("izin lingkungan", "lingkungan"),                                       # Indonesia -- environmental permit
    ("conditional use permit", "conditional use"), ("special use permit", "special use"),   # US zoning
    ("grading permit", "grading"), ("land disturbance permit", "land disturbance"),          # US site-prep
    ("compulsory purchase order", "compulsory purchase"),                    # UK -- expropriation
    ("\u0111\u00e1nh gi\u00e1 t\u00e1c \u0111\u1ed9ng m\u00f4i tr\u01b0\u1eddng", "t\u00e1c \u0111\u1ed9ng m\u00f4i tr\u01b0\u1eddng"),  # Vietnamese -- EIA (DTM)
    # more EIA regimes + land-taking consents (forestry clearance, marine, water, IPPC)
    ("\u03c0\u03b5\u03c1\u03b9\u03b2\u03b1\u03bb\u03bb\u03bf\u03bd\u03c4\u03b9\u03ba\u03ae \u03b1\u03b4\u03b5\u03b9\u03bf\u03b4\u03cc\u03c4\u03b7\u03c3\u03b7", "\u03c0\u03b5\u03c1\u03b9\u03b2\u03b1\u03bb\u03bb\u03bf\u03bd\u03c4\u03b9\u03ba"),  # Greek -- environmental licensing
    ("evaluarea impactului asupra mediului", "impactului"),                  # Romanian -- EIA
    ("\u043e\u0446\u0456\u043d\u043a\u0430 \u0432\u043f\u043b\u0438\u0432\u0443 \u043d\u0430 \u0434\u043e\u0432\u043a\u0456\u043b\u043b\u044f", "\u0432\u043f\u043b\u0438\u0432\u0443"),  # Ukrainian -- EIA
    ("marine licence", "marine licence"),                                    # marine works (offshore wind, cables, dredging)
    ("felling licence", "felling"),                                          # forestry clearance (deforestation)
    ("aquaculture licence", "aquaculture"),                                  # aquaculture
    ("autorizacion ambiental integrada", "ambiental integrada"),             # Spain -- AAI (IPPC industrial)
    ("defrichement", "defrichement"),                                        # France -- forest clearance
    ("abstraction licence", "abstraction"),                                  # water abstraction (dams/extraction)
    ("reserved matters", "reserved matters"),                                # UK -- planning stage
    # further consent regimes + more national EIA names
    ("prior approval", "prior approval"),                                    # UK -- permitted-development (masts, solar, ag)
    ("hazardous substances consent", "hazardous substances"),                # UK -- major-hazard installations
    ("waste management licence", "waste management"),                        # landfills / incinerators
    ("dredging licence", "dredging"), ("foreshore licence", "foreshore"),    # marine / coastal works
    ("energy consent", "energy consent"),                                    # UK/Scotland -- large energy schemes
    ("ymparistovaikutusten arviointi", "arviointi"),                         # Finnish -- EIA (YVA)
    ("procjena utjecaja na okolis", "utjecaja"),                             # Croatian -- EIA
    ("poveikio aplinkai vertinimas", "poveikio"),                            # Lithuanian -- EIA (PAV)
    # --- extended script coverage: South Asian, SE Asian, Caucasus, E African,
    #     Indonesian/Vietnamese/Chinese-Trad/Ukrainian/Thai (also match new CKAN portals) ---
    ('\u092d\u0935\u0928 \u0928\u093f\u0930\u094d\u092e\u093e\u0923 \u0905\u0928\u0941\u092e\u0924\u093f', 'nirman'),  # Hindi -- building construction permit
    ('\u092a\u0930\u094d\u092f\u093e\u0935\u0930\u0923 \u092a\u094d\u0930\u092d\u093e\u0935 \u0906\u0915\u0932\u0928', 'paryavaran'),  # Hindi -- EIA
    ('\u09aa\u09b0\u09bf\u09ac\u09c7\u09b6\u0997\u09a4 \u09aa\u09cd\u09b0\u09ad\u09be\u09ac \u09ae\u09c2\u09b2\u09cd\u09af\u09be\u09df\u09a8', 'probhab'),  # Bengali -- EIA
    ('\u0b95\u0b9f\u0bcd\u0b9f\u0bbf\u0b9f \u0b85\u0ba9\u0bc1\u0bae\u0ba4\u0bbf', 'kattida'),  # Tamil -- building permit
    ('\u067e\u0631\u0648\u0627\u0646\u0647 \u0633\u0627\u062e\u062a', 'parvane'),  # Persian -- building permit
    ('\u0627\u0631\u0632\u06cc\u0627\u0628\u06cc \u0627\u062b\u0631\u0627\u062a \u0632\u06cc\u0633\u062a \u0645\u062d\u06cc\u0637\u06cc', 'arzyabi'),  # Persian -- EIA
    ('\u062a\u0639\u0645\u06cc\u0631\u0627\u062a\u06cc \u0627\u062c\u0627\u0632\u062a \u0646\u0627\u0645\u06c1', 'taamir'),  # Urdu -- construction permit
    ('\u10db\u10e8\u10d4\u10dc\u10d4\u10d1\u10dc\u10d8\u10e1 \u10dc\u10d4\u10d1\u10d0\u10e0\u10d7\u10d5\u10d0', 'mshenebl'),  # Georgian -- construction permit
    ('\u0577\u056b\u0576\u0561\u0580\u0561\u0580\u0561\u056f\u0561\u0576 \u0569\u0578\u0582\u0575\u056c\u057f\u057e\u0578\u0582\u0569\u0575\u0578\u0582\u0576', 'shinar'),  # Armenian -- construction permit
    ('tikinti icaz\u0259si', 'tikinti'),  # Azerbaijani -- construction permit
    ('\u049b\u04b1\u0440\u044b\u043b\u044b\u0441\u049b\u0430 \u0440\u04b1\u049b\u0441\u0430\u0442', 'qurylys'),  # Kazakh -- construction permit
    ('\u1017\u1010\u17d2\u178f\u17b6\u1005\u17c6\u178e\u1784\u17cb', 'samnang'),  # Khmer -- construction permit
    ('\u1017\u1031\u102c\u1000\u103a\u101c\u102f\u1015\u103a\u101b\u1031\u1038\u1001\u103d\u1004\u1037\u103a\u1015\u103c\u102f\u1001\u103b\u1000\u103a', 'saut'),  # Burmese -- construction permit
    ('\u0d89\u0daf\u0dd2\u0d9a\u0dd2\u0dbb\u0dd3\u0db8\u0dca \u0db6\u0dbd\u0db4\u0dad\u0dca\u200d\u0dbb\u0dba', 'idikirim'),  # Sinhala -- construction permit
    ('\u12e8\u1130\u1295\u1263\u1273 \u134d\u124d\u12f5', 'ginbata'),  # Amharic -- construction permit
    ('kibali cha ujenzi', 'ujenzi'),  # Swahili -- construction permit
    ('tathmini ya athari za mazingira', 'tathmini'),  # Swahili -- EIA
    ('\u0928\u093f\u0930\u094d\u092e\u093e\u0923 \u0905\u0928\u0941\u092e\u0924\u093f \u092a\u0924\u094d\u0930', 'nirmaan'),  # Nepali -- construction permit
    ('izin lingkungan', 'lingkungan'),  # Indonesian -- environmental permit (also CKAN)
    ('izin mendirikan bangunan', 'mendirikan'),  # Indonesian -- building permit / IMB
    ('analisis dampak lingkungan', 'amdal'),  # Indonesian -- EIA / AMDAL
    ('gi\u1ea5y ph\xe9p x\xe2y d\u1ef1ng', 'xaydung'),  # Vietnamese -- construction permit
    ('\u0111\xe1nh gi\xe1 t\xe1c \u0111\u1ed9ng m\xf4i tr\u01b0\u1eddng', 'moitruong'),  # Vietnamese -- EIA (ĐTM)
    ('\u5efa\u7bc9\u57f7\u7167', 'jianzhu'),  # Chinese (Trad) -- building permit
    ('\u74b0\u5883\u5f71\u97ff\u8a55\u4f30', 'huanjing'),  # Chinese (Trad) -- EIA
    ('\u0434\u043e\u0437\u0432\u0456\u043b \u043d\u0430 \u0431\u0443\u0434\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e', 'dozvil'),  # Ukrainian -- construction permit
    ('\u043e\u0446\u0456\u043d\u043a\u0430 \u0432\u043f\u043b\u0438\u0432\u0443 \u043d\u0430 \u0434\u043e\u0432\u043a\u0456\u043b\u043b\u044f', 'ovd'),  # Ukrainian -- EIA (OVD)
    ('\u0e43\u0e1a\u0e2d\u0e19\u0e38\u0e0d\u0e32\u0e15\u0e01\u0e48\u0e2d\u0e2a\u0e23\u0e49\u0e32\u0e07', 'kosang'),  # Thai -- construction permit
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

# ---------------------------------------------------------------------------
# CEQAnet -- California CEQA/NEPA environmental filings (State Clearinghouse).
# VERIFIED: https://ceqanet.lci.ca.gov/Search?OutputFormat=CSV returns CSV with
# columns incl. "Location Coordinates" (DMS), "Location Total Acres",
# "Document Type", "Document Portal URL", "Cities", "Counties". Plain /Search
# returns the latest 100; &DocumentType=<code> narrows it (best-effort -- if the
# param is ignored we simply re-see the latest 100 and dedupe). This catches
# small-city California projects (design-review permits, EIRs, subdivisions,
# specific plans) that NO building-permit feed carries -- the Sierra Madre gap.
# California-only, but California is ~12% of the US and files ~13k CEQA docs/yr.
# ---------------------------------------------------------------------------
CEQANET_CSV = "https://ceqanet.lci.ca.gov/Search?OutputFormat=CSV"
# Substantive environmental-review docs are kept in full; NOE/other are gated
# through the shared significance filter so trivial exemptions (re-roofs, tree
# removals) are dropped but sizeable projects (e.g. a 42-home subdivision) stay.
_CEQ_SUBSTANTIVE = {"EIR","EIS","FIS","SBE","SIR","SIS","SEA","NOP","MND","NEG","FIN"}
_CEQ_TYPE_QUERIES = ["EIR","EIS","NOP","MND","NEG","SBE","SIR","NOD","NOE","FIN"]
_CEQ_DOCLABEL = {
    "EIR":"Draft EIR","EIS":"Draft EIS","FIS":"Final EIS","FIN":"Final document",
    "SBE":"Subsequent EIR","SIR":"Supplemental EIR","SIS":"Revised/Supplemental EIS",
    "SEA":"Supplemental EIR","NOP":"Notice of Preparation","MND":"Mitigated Negative Declaration",
    "NEG":"Negative Declaration","NOD":"Notice of Determination","NOE":"Notice of Exemption",
    "NOC":"Notice of Completion","NOI":"Notice of Intent",
}

def _ceq_latlng(s):
    """Parse CEQAnet 'Location Coordinates'. Handles DMS (34d10'18.5"N 118d3'51.4"W,
    where the degree glyph may arrive mojibaked) and a decimal fallback. Returns
    (lat,lng) inside California's bounding box, else None."""
    if not s:
        return None
    s = str(s)
    dms = re.findall(r"(\d+(?:\.\d+)?)[^\d\n]+?(\d+(?:\.\d+)?)['\u2032]([\d.]+)[\"\u2033]?\s*([NSEW])", s)
    lat = lng = None
    for d, m, sec, hemi in dms:
        try:
            v = float(d) + float(m)/60.0 + float(sec)/3600.0
        except ValueError:
            continue
        if hemi in ("N","S"):
            lat = -v if hemi == "S" else v
        elif hemi in ("E","W"):
            lng = -v if hemi == "W" else v
    if lat is None or lng is None:
        dec = re.findall(r"-?\d+\.\d+", s)
        if len(dec) >= 2:
            lat = float(dec[0]); lng = float(dec[1])
            if lng > 0:
                lng = -lng                       # California is western hemisphere
    if lat is None or lng is None:
        return None
    if 32.3 <= lat <= 42.3 and -124.6 <= lng <= -113.9:
        return (lat, lng)
    return None

def fetch_ceqanet():
    import csv as _csv, io as _io
    seen = {}; seen_sch = set(); out = []
    geo_used = [0]; GEO_CAP = 40                    # cap address-geocoding per run

    def _row_to_project(row, force_keep=False):
        sch = (row.get("SCH Number") or "").strip()
        if not sch: return None
        doc_url = (row.get("Document Portal URL") or "").strip()
        dtype = (row.get("Document Type") or "").strip().upper()
        key = doc_url or (sch + "|" + dtype)
        if key in seen: return None
        ll = _ceq_latlng(row.get("Location Coordinates")); precise = True
        if not ll and geo_used[0] < GEO_CAP:        # no coordinates in record -> geocode the address
            city = (row.get("Cities") or "").split(";")[0].strip()
            cross = (row.get("Location Cross Streets") or "").strip()
            zipc = (row.get("Location Zip Code") or "").strip()
            q = None
            if cross and (city or zipc):
                st = re.split(r"\s+(?:and|&|/|at|,)\s+", cross, maxsplit=1)[0].strip()
                q = "%s, %sCA %s" % (st, (city + ", ") if city else "", zipc)
            elif city:
                q = "%s, CA %s" % (city, zipc)
            if q:
                geo_used[0] += 1
                g = _geocode_place(q, cc="us")
                if g and 32.3 <= g[0] <= 42.3 and -124.6 <= g[1] <= -113.9:
                    ll = g; precise = False
        if not ll: return None                      # still no location -> skip
        title = (row.get("Project Title") or row.get("Document Title") or "CEQA project").strip()
        devtype = (row.get("NOC Development Type") or "").strip()
        gate_text = " ".join([title, row.get("Document Description") or "", devtype])
        acres = _num(row.get("Location Total Acres"))
        if not force_keep:
            if _TRIVIAL_RE.search(gate_text):
                return None                         # cosmetic / private work, whatever the type
            if dtype not in _CEQ_SUBSTANTIVE:
                if not ((acres is not None and acres >= 3) or _permit_is_significant(gate_text, None)):
                    return None                     # NOE/other: keep only sizeable projects
        seen[key] = 1; seen_sch.add(sch)
        agency = (row.get("Lead Agency Name") or row.get("Lead Agency Title") or "").strip()
        city = (row.get("Cities") or "").strip()
        county = (row.get("Counties") or "").strip()
        place = ""
        if city or county:
            place = " (%s%s)" % (city or county, (", " + county) if (city and county) else "")
        label = _CEQ_DOCLABEL.get(dtype, dtype or "CEQA document")
        p = {
            "name": title[:140],
            "type": devtype or "development",
            "state": "California",
            "lat": round(ll[0], 5), "lng": round(ll[1], 5), "precise": precise,
            "value_usd": None, "acres": acres,
            "size": ("%g ac" % acres) if acres else "",
            "status": label,
            "company": "",
            "url": doc_url or ("https://ceqanet.lci.ca.gov/Project/" + sch),
            "desc": ("California CEQA filing (%s) with the State Clearinghouse by %s%s. "
                     "A filing means a decision is in progress -- open the CEQAnet record "
                     "for the documents, then check the lead agency's agenda for the hearing "
                     "and public-comment deadlines.%s" %
                     (label, agency or "a lead agency", place,
                      "" if precise else " Location approximate (geocoded from the listed address).")),
            "source": "ceqanet",
        }
        p["impact"] = rate_project(p)
        return p

    def _pull(url, tag, force_keep=False, one_per_sch=False):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8-sig", "replace")
        except Exception as e:
            print("  ceqanet %s: %s" % (tag, e)); return 0
        if "SCH Number" not in raw[:120]:
            return 0                                # HTML/error, not CSV
        try:
            rdr = _csv.DictReader(_io.StringIO(raw))
        except Exception as e:
            print("  ceqanet %s: parse %s" % (tag, e)); return 0
        n = 0
        for row in rdr:
            if one_per_sch and (row.get("SCH Number") or "").strip() in seen_sch:
                continue                            # already have a pin for this project
            p = _row_to_project(row, force_keep=force_keep)
            if p:
                out.append(p); n += 1
                if one_per_sch:
                    break                           # one pin per watchlisted project
        return n

    # rolling live window: plain latest-100 + per-type latest-100 (auto-catches new
    # filings); records without coordinates are geocoded from their listed address
    for dt in [None] + list(_CEQ_TYPE_QUERIES):
        _pull(CEQANET_CSV + (("&DocumentType=" + dt) if dt else ""), dt or "latest")
    return out

# ---------------------------------------------------------------------------
# Washington State SEPA Register -- state environmental-review filings.
# VERIFIED: Socrata dataset https://data.wa.gov/resource/mmcb-z6jf.json exposes
# sitelatitudedecimal / sitelongitudedecimal (~26k geocoded records), plus
# proposaldescription, documenttypecode (EIS/MDNS/DNS/DS/SCOPING/...),
# leadagencyname, sitecityname and leadagencyissuedate. This is Washington's
# analogue of California's CEQA -- it catches small-city and county projects
# (subdivisions, EIS-level developments) that no building-permit feed carries.
# Reuses the same anonymous Socrata access as the SF/LA permit sources.
# ---------------------------------------------------------------------------
WA_SEPA_RES = "https://data.wa.gov/resource/mmcb-z6jf.json"
# Substantive review documents kept in full; DNS/ODNS ("determination of NON-
# significance") are gated through the shared significance filter.
_WASEPA_SUBSTANTIVE = {"EIS","DEIS","FEIS","SEIS","MDNS","DS","SCOPING","ADDEND"}
_WASEPA_DOCLABEL = {
    "EIS":"Environmental Impact Statement","DEIS":"Draft EIS","FEIS":"Final EIS",
    "SEIS":"Supplemental EIS","MDNS":"Mitigated Determination of Nonsignificance",
    "DNS":"Determination of Nonsignificance","ODNS":"Optional DNS",
    "ODNS/NOA":"Optional DNS / Notice of Application","ODNS-M":"Optional Mitigated DNS",
    "DS":"Determination of Significance","SCOPING":"EIS Scoping","ADDEND":"Addendum",
    "CONSULT":"Agency Consultation",
}
# Routine SEPA filings that flood the register but aren't development threats.
# (Kept separate from the shared _TRIVIAL_RE; also stops "Plant triploid grass
# carp..." from matching the industrial-"plant" keyword in _SIGNIF_RE.)
_WASEPA_TRIVIAL = re.compile(
    r"(grass\s*carp|triploid|aquatic\s*veget|\bdock\b|bulkhead|boat\s*lift|"
    r"\bfloat\b|\bpier\b|\bmooring\b|forest\s*practice|(harvest|thin)\w*\s*timber|"
    r"timber\s*harvest|shoreline\s*exemption|fish\s*enhancement|\bculvert\b|"
    r"single[-\s]*family\s*(residence|home|dwelling))", re.I)

def fetch_wa_sepa(days=365, cap=5000):
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    where = ("sitelatitudedecimal IS NOT NULL AND "
             "leadagencyissuedate > '%sT00:00:00'" % cutoff)
    params = {"$where": where, "$order": "leadagencyissuedate DESC", "$limit": cap}
    url = WA_SEPA_RES + "?" + urllib.parse.urlencode(params)
    try:
        rows = _get_json(url)
    except Exception as e:
        print("  wa_sepa: %s" % e); return []
    out = []
    for r in rows:
        lat = _num(r.get("sitelatitudedecimal")); lng = _num(r.get("sitelongitudedecimal"))
        if lat is None or lng is None:
            continue
        if not (45.4 <= lat <= 49.1 and -125.0 <= lng <= -116.5):
            continue                                   # outside WA -> bad coordinate, drop
        dtype = (r.get("documenttypecode") or "").strip().upper()
        name = (r.get("proposalname") or "").strip()
        desc_raw = (r.get("proposaldescription") or "").strip()
        gate = " ".join([name, desc_raw])
        if _TRIVIAL_RE.search(gate):
            continue                                   # sheds, remodels, interior work, etc.
        if dtype not in _WASEPA_SUBSTANTIVE:
            if _WASEPA_TRIVIAL.search(gate):
                continue                               # WA routine: docks, grass carp, timber, shoreline
            if not _permit_is_significant(gate, None):
                continue                               # DNS/other: keep only significant types
        title = name or (desc_raw[:70] + ("\u2026" if len(desc_raw) > 70 else "")) or "SEPA proposal"
        agency = (r.get("leadagencyname") or "").strip()
        city = (r.get("sitecityname") or "").strip()
        label = _WASEPA_DOCLABEL.get(dtype, dtype or "SEPA filing")
        p = {
            "name": title[:140],
            "type": "development",
            "state": "Washington",
            "lat": round(lat, 5), "lng": round(lng, 5), "precise": True,
            "value_usd": None, "acres": None, "size": "",
            "status": label,
            "company": (r.get("applicantname") or "").strip(),
            "url": "https://apps.ecology.wa.gov/separ/Main/SEPA/Search.aspx",
            "desc": ("Washington SEPA filing (%s) on the Dept. of Ecology register, lead agency %s%s. "
                     "A SEPA filing means a decision is under way -- look the record up on the SEPA "
                     "Register, then check the lead agency for the public-comment deadline." %
                     (label, agency or "(unknown)", (" (%s)" % city) if city else "")),
            "source": "wa_sepa",
        }
        p["impact"] = rate_project(p)
        out.append(p)
    return out

def fetch_arcgis_hub(max_datasets=3000, min_value=1000000, per_ds=4000):
    hub_end = time.time() + int(os.environ.get("HUB_BUDGET_MIN", "110")) * 60
    ds = []; seen_ds = set()
    for q, kw in _HUB_QUERIES:
        for pg in range(1, 6):          # paginate the catalogue, not just page 1
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
        if time.time() > hub_end:
            _flag("arcgis hub hit %d-min budget -- remaining datasets skipped"
                  % int(os.environ.get("HUB_BUDGET_MIN", "110")))
            break
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
                if geom.get("type") == "Point" and len(c) >= 2:
                    lng, lat = float(c[0]), float(c[1]); _hub_precise = True
                else:                                # polygon/line (parcel or boundary) -> centroid
                    ctr = _geom_center(geom)
                    if not ctr: continue
                    lat, lng = ctr; _hub_precise = False
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
                     "lat": round(lat, 5), "lng": round(lng, 5), "precise": _hub_precise,
                     "size": size, "value_usd": (float(val) if val else None),
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

def fetch_ukplanit(days=180, pg_sz=200):
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    today = datetime.date.today().isoformat()
    # PlanIt is built for local queries -- a country-sized bbox returns nothing.
    # Tile Great Britain into ~1.5-degree boxes and gather each.
    tiles = []
    for lat0 in (50.0, 51.5, 53.0, 54.5, 56.0, 57.5):
        for lng0 in (-6.0, -4.5, -3.0, -1.5, 0.0):
            tiles.append("%s,%s,%s,%s" % (lng0, lat0, lng0 + 1.5, lat0 + 1.5))
    feats = []; errs = 0
    max_pages = int(os.environ.get("UK_PLANIT_PAGES", "5"))
    for bb in tiles:
        for pg in range(1, max_pages + 1):       # dense tiles (London...) exceed one page
            params = {"bbox": bb, "start_date": since, "end_date": today,
                      "pg_sz": pg_sz, "limit": pg_sz, "pg": pg}
            url = "https://www.planit.org.uk/api/applics/geojson?" + urllib.parse.urlencode(params)
            # retry each page a few times before giving up -- a single transient timeout
            # used to drop a whole ~1.5deg cell of Britain and leave a persistent hole.
            page_feats = None
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, headers={
                        "User-Agent": "Mozilla/5.0 (compatible; project-map/1.0; +wheelock.chris@gmail.com)",
                        "Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                        gj = json.loads(r.read().decode("utf-8", "replace"))
                    page_feats = gj.get("features", []) if isinstance(gj, dict) else []
                    break
                except Exception as e:
                    if attempt == 2:
                        errs += 1
                        if errs == 1: print("  uk planit tile error (after retries): %s" % e)
                    else:
                        time.sleep(1.0 * (attempt + 1))
            if page_feats is None: break          # page failed after retries -> next tile
            feats += page_feats
            if len(page_feats) < pg_sz: break     # tile exhausted -> no more pages
            time.sleep(0.4)
        time.sleep(0.4)
    print("  uk planit: %d applications across %d tiles (%d tile errors)" % (len(feats), len(tiles), errs))
    out = []; skipped = 0
    for f in feats:
        try:
            geom = f.get("geometry") or {}; c = geom.get("coordinates") or []
            if geom.get("type") == "Point" and len(c) >= 2:
                lng, lat = float(c[0]), float(c[1]); _uk_precise = True
            else:                                  # polygon/line boundary -> centroid
                ctr = _geom_center(geom)
                if not ctr: continue
                lat, lng = ctr; _uk_precise = False
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
                 "lat": round(lat, 5), "lng": round(lng, 5), "precise": _uk_precise,
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

def _arcgis_query_all(base_url, layer=0, page=2000, max_pages=30, label=""):
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
    "anla_co": (-4.5, 13.7, -82.2, -66.7),           # Colombia + San Andrés / Malpelo islands
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
_NAT_CENTER = {"iaac_ca": (56.13, -106.35), "epbc_au": (-25.27, 133.78), "anla_co": (4.57, -74.30)}

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

# ---------------------------------------------------------------------------
# Colombia -- ANLA (Autoridad Nacional de Licencias Ambientales). Projects under
# national environmental licensing, in evaluation & monitoring. VERIFIED public
# ArcGIS MapServer (no key): .../ANLA/ANLA/MapServer, layer 1 = "PROYECTOS EN
# SEGUIMIENTO". Field names are discovered at runtime (printed on first run) and
# mapped heuristically, exactly like the Canada/Australia ArcGIS sources. ANLA
# only licenses major projects (mining, hydrocarbons, power, infrastructure), so
# no significance gate is needed -- every record is a real, licence-scale project.
# ---------------------------------------------------------------------------
_CO_KEYS_SHOWN = []

def fetch_anla_co():
    base = "https://portalsig.anla.gov.co/publico/rest/services/ANLA/ANLA/MapServer"
    feats = _arcgis_query_all(base, layer=1, label="anla co")
    out = []; dropped = 0
    for f in feats:
        try:
            pr = f.get("properties") or {}
            up = {str(k).upper(): v for k, v in pr.items()}
            if not out and not _CO_KEYS_SHOWN:
                print("  anla co [fields]: %s" % sorted(list(pr.keys()))[:20])
                _CO_KEYS_SHOWN.append(1)
            _la = _num(up.get("LATITUD") or up.get("LATITUDE") or up.get("LAT") or up.get("Y"))
            _lo = _num(up.get("LONGITUD") or up.get("LONGITUDE") or up.get("LON")
                       or up.get("LNG") or up.get("X"))
            ll = (_la, _lo) if (_la is not None and _lo is not None) else _geom_center(f.get("geometry") or {})
            if not ll or ll[0] is None: continue
            _approx = False; _note = ""
            if not _box_ok("anla_co", ll[0], ll[1]):
                fb = _fallback_center("anla_co", "")
                if not fb:
                    dropped += 1; continue
                ll = (fb[0], fb[1]); _approx = True; dropped += 1
                _note = (" Source coordinates were unusable \u2014 shown at the national "
                         "level; open the ANLA registry for the exact site.")
            nm = _best_name(pr, ("NOMBRE_PROYECTO", "NOMBREPROYECTO", "NOMBRE_DEL_PROYECTO",
                                 "NOMBRE", "PROYECTO")) or "Proyecto ANLA"
            sector = str(up.get("SECTOR") or up.get("SECTOR_ECONOMICO") or up.get("TIPO") or "")
            status = str(up.get("ESTADO") or up.get("ESTADO_PROYECTO")
                         or up.get("ESTADO_EXPEDIENTE") or "")
            exp = str(up.get("EXPEDIENTE") or up.get("NUMERO_EXPEDIENTE") or up.get("CODIGO") or "")
            company = str(up.get("TITULAR") or up.get("EMPRESA") or up.get("SOLICITANTE")
                          or up.get("BENEFICIARIO") or "")[:80]
            p = {
                "name": str(nm)[:140],
                "type": ("Environmental licence" + ((" \u00b7 " + sector) if sector else "")),
                "state": "Colombia",
                "lat": round(ll[0], 5), "lng": round(ll[1], 5), "precise": not _approx,
                "value_usd": None, "acres": None, "size": "",
                "status": status, "company": company,
                "url": "https://www.anla.gov.co/",
                "desc": ("Project under Colombian environmental licensing (ANLA)"
                         + ((" \u00b7 sector " + sector) if sector else "")
                         + ((" \u00b7 " + status) if status else "")
                         + ((" \u00b7 exp. " + exp) if exp else "") + "." + _note +
                         " ANLA licenses major projects (mining, hydrocarbons, power, "
                         "infrastructure); open the registry for the licensing stage and "
                         "any public-participation window."),
                "source": "anla_co",
            }
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  anla co: %d projects (%d re-placed at national level)" % (len(out), dropped))
    return out


# ---------------------------------------------------------------------------
# Washington, D.C. -- Dept of Buildings construction permits, public ArcGIS
# FeatureServer (no key). WGS84 LATITUDE/LONGITUDE attributes. DC publishes NO
# construction valuation (FEES_PAID is only the permit fee), so significance is
# TYPE-based: we keep NEW CONSTRUCTION and RAZE (demolition) -- the development /
# displacement projects -- and skip supplemental trades and minor alterations.
# Layer 4 = "Building Permits - Last 30 Days" (always current, small).
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tempe, AZ -- City of Tempe Building Safety permits (Accela export), public
# ArcGIS FeatureServer (no key). WGS84 Latitude/Longitude, real EstProjectCost
# valuation -> clean $5M significance gate. We keep only rows the data itself
# marks OriginalCity = TEMPE, so the "Tempe" label is per-record correct even
# though the layer's bbox spills a little into neighbouring Phoenix / Mesa.
# Source confirmed as the City of Tempe Accela export via data.tempe.gov + data.gov.
# ---------------------------------------------------------------------------
_TEMPE_KEYS_SHOWN = [False]
def fetch_tempe_permits():
    base = "https://services.arcgis.com/lQySeXwbBg53XWDi/arcgis/rest/services/building_permits/FeatureServer"
    feats = _arcgis_query_all(base, layer=0, label="tempe permits")
    out = []
    for f in feats:
        try:
            pr = f.get("properties") or {}
            if not _TEMPE_KEYS_SHOWN[0]:
                print("  tempe field keys:", sorted(pr.keys())); _TEMPE_KEYS_SHOWN[0] = True
            up = {str(k).upper(): v for k, v in pr.items()}
            if "TEMPE" not in str(up.get("ORIGINALCITY") or "").upper():
                continue                                         # per-record jurisdiction guard
            val = _money(up.get("ESTPROJECTCOST"))
            if val is None or val < 5000000:
                continue                                         # $5M+ significance (real valuation)
            status = str(up.get("STATUSCURRENT") or "")
            if any(k in status.lower() for k in ("complete", "void", "expired",
                                                 "withdrawn", "cancel", "closed", "final")):
                continue
            la = _num(up.get("LATITUDE")); lo = _num(up.get("LONGITUDE"))
            if la is None or lo is None or (abs(la) < 0.01 and abs(lo) < 0.01):
                c = _geom_center(f.get("geometry"))
                if c: la, lo = c
            if la is None or lo is None:
                continue
            nm = str(up.get("PROJECTNAME") or up.get("DESCRIPTION")
                     or up.get("TYPE") or "Tempe construction permit")[:140]
            p = {
                "name": nm,
                "type": str(up.get("PERMITTYPEDESC") or up.get("TYPE") or "Building permit")[:80],
                "state": "Arizona", "lat": round(la, 5), "lng": round(lo, 5), "precise": True,
                "value_usd": val, "size": "", "status": status,
                "company": str(up.get("CONTRACTORCOMPANYNAME") or "")[:80],
                "url": "https://data.tempe.gov/", "date": _iso_date(up.get("ISSUEDDATE") or up.get("ISSUEDDATEDTM")),
                "desc": ("City of Tempe building permit (est. cost $%s). Local construction filing; "
                         "check the Tempe Community Development / Building Safety docket for review timing."
                         % format(int(val), ",")),
                "source": "tempe_permits",
            }
            p["impact"] = rate_project(p, sensitivity=0)
            out.append(p)
        except Exception:
            continue
    print("  tempe permits: %d permits >= $5M (OriginalCity=TEMPE)" % len(out))
    return out


# ===========================================================================
# GENERIC ArcGIS building-permit harvester (heuristic column detection).
# Twin of the Socrata heuristic path: given a VERIFIED FeatureServer url+layer,
# it reads the live schema, self-maps lat/lng/value/date/status/type, and keeps
# permits >= a value floor. Adding an ArcGIS permit city becomes: verify the
# endpoint once, append one config dict to ARCGIS_PERMIT_CITIES. Value-gated
# only -- a layer with NO cost field is HELD (returns 0) rather than flooded,
# since type-based significance is city-specific and needs a bespoke fetch (cf. DC).
# ===========================================================================
def _arcgis_detect(props):
    keys = list(props.keys()); up = {k: k.upper() for k in keys}
    det = {}
    def find(pred):
        for k in keys:
            if pred(up[k], props.get(k)): return k
        return None
    det["lat"] = find(lambda uk, v: uk == "LATITUDE" or uk in ("LAT", "GIS_LATITUDE", "POINT_Y", "Y") or uk.endswith("LATITUDE"))
    det["lng"] = find(lambda uk, v: uk == "LONGITUDE" or uk in ("LNG", "LON", "LONG", "GIS_LONGITUDE", "POINT_X", "X") or uk.endswith("LONGITUDE"))
    det["value"] = find(lambda uk, v: "ID" not in uk and "UNIT" not in uk and (
        "ESTPROJECTCOST" in uk or "VALUATION" in uk or "ESTIMATEDCOST" in uk or "JOBVALUE" in uk
        or "JOB_VALUE" in uk or "CONSTRUCTIONVALUE" in uk or (("COST" in uk or "VALUE" in uk) and "VALUATION" not in uk)))
    det["date"] = find(lambda uk, v: ("ISSUE" in uk and "DATE" in uk) or uk in ("ISSUEDDATE", "ISSUE_DATE", "ISSUEDATE", "DATEISSUED"))
    det["status"] = find(lambda uk, v: "STATUS" in uk)
    det["name"] = (find(lambda uk, v: uk in ("PROJECTNAME", "DESCRIPTION", "DESC_OF_WORK", "DESCOFWORK", "WORKDESCRIPTION", "PROPOSEDWORKDESCRIPTION", "SCOPEOFWORK"))
                   or find(lambda uk, v: "DESCRIPTION" in uk or ("WORK" in uk and "DESC" in uk)))
    det["type"] = (find(lambda uk, v: uk in ("PERMITTYPEDESC", "PERMITTYPE", "PERMIT_TYPE", "TYPE", "PERMITCLASS", "WORKCLASS", "PERMITTYPEMAPPED"))
                   or find(lambda uk, v: "PERMIT" in uk and "TYPE" in uk))
    det["city"] = find(lambda uk, v: uk in ("ORIGINALCITY", "CITY", "MUNICIPALITY", "JURISDICTION"))
    det["company"] = find(lambda uk, v: uk in ("CONTRACTORCOMPANYNAME", "CONTRACTOR", "CONTRACTORNAME", "OWNERNAME", "OWNER_NAME", "APPLICANT"))
    return {k: v for k, v in det.items() if v}

def _arcgis_project_from(pr, geom, det, cfg, floor):
    """One project dict from an ArcGIS feature via detected field roles. None if
    below the value floor, done/closed, or ungeocodable. Shared by the bespoke
    city path and the discovery path so both behave identically."""
    if not det.get("value"): return None
    val = _money(pr.get(det["value"]))
    if val is None or val < floor: return None
    if det.get("status"):
        st = str(pr.get(det["status"]) or "").lower()
        if any(k in st for k in ("complete", "void", "expired", "withdrawn", "cancel", "closed", "final", "revoked")):
            return None
    la = _num(pr.get(det["lat"])) if det.get("lat") else None
    lo = _num(pr.get(det["lng"])) if det.get("lng") else None
    if la is None or lo is None or (abs(la) < 0.01 and abs(lo) < 0.01):
        c = _geom_center(geom)
        if c: la, lo = c
    if la is None or lo is None: return None
    nm = str((det.get("name") and pr.get(det["name"])) or (det.get("type") and pr.get(det["type"]))
             or (cfg.get("city", "Permitted") + " project"))[:140]
    p = {"name": nm, "type": str((det.get("type") and pr.get(det["type"])) or "Building permit")[:80],
         "state": cfg.get("state", ""), "lat": round(la, 5), "lng": round(lo, 5), "precise": True,
         "value_usd": val, "size": "", "status": str((det.get("status") and pr.get(det["status"])) or ""),
         "company": str((det.get("company") and pr.get(det["company"])) or "")[:80],
         "url": cfg.get("portal", ""), "date": _iso_date(det.get("date") and pr.get(det["date"])),
         "desc": cfg.get("desc", "Local building permit ($%s+). Check the jurisdiction's planning docket for review timing." % format(int(floor), ",")),
         "source": cfg["source"]}
    p["impact"] = rate_project(p, sensitivity=0)
    return p

_ARCGIS_KEYS_SHOWN = {}
def fetch_arcgis_permits(cfg):
    src = cfg["source"]
    feats = _arcgis_query_all(cfg["url"], layer=cfg.get("layer", 0), label=cfg.get("city", src))
    out = []; det = None; floor = cfg.get("min_value", 5000000)
    guard = (cfg.get("city_field_value") or "").upper()
    for f in feats:
        try:
            pr = f.get("properties") or {}
            if det is None:
                det = _arcgis_detect(pr)
                if not _ARCGIS_KEYS_SHOWN.get(src):
                    print("  %s field keys: %s" % (src, sorted(pr.keys())))
                    print("  %s auto-detected: %s" % (src, det)); _ARCGIS_KEYS_SHOWN[src] = True
                if not det.get("value"):
                    print("  %s: no cost/value field -> HELD (needs type-based bespoke fetch)" % src); return []
            if guard:
                cf = det.get("city")
                if not cf or guard not in str(pr.get(cf) or "").upper(): continue
            p = _arcgis_project_from(pr, f.get("geometry"), det, cfg, floor)
            if p: out.append(p)
        except Exception:
            continue
    print("  %s: %d permits >= $%s" % (src, len(out), format(int(floor), ",")))
    return out

# Verified (url, layer) ArcGIS permit endpoints. Each shares the "arcgis_city:" source
# prefix -> one PJ_SRC group on the front end (like "socrata"). Append verified entries here.
ARCGIS_PERMIT_CITIES = [
    # TEMPLATE (verify url+layer carry a cost field + WGS84 coords before adding):
    # {"source": "arcgis_city:denver", "city": "Denver, CO", "state": "Colorado",
    #  "url": "https://.../FeatureServer", "layer": 0, "min_value": 5000000,
    #  "portal": "https://denvergov.org/opendata"},
]

def _arcgis_discover_one(url, title, host, layer=0, floor=5000000):
    """Probe one discovered Feature Service, detect fields, and pull $5M+ permits
    with a SERVER-SIDE value filter + recent ordering so we never drag a full
    permit history. Held (0) if no cost field or no coordinates."""
    import urllib.parse
    base = url.rstrip("/") + "/%d/query?" % layer
    def q(where, order=None, count=1):
        p = {"where": where, "outFields": "*", "outSR": "4326", "f": "geojson",
             "resultRecordCount": count, "returnGeometry": "true"}
        if order: p["orderByFields"] = order
        return _get_json(base + urllib.parse.urlencode(p))
    probe = q("1=1", count=1)
    feats = (probe or {}).get("features", []) if isinstance(probe, dict) else []
    if not feats: return []
    det = _arcgis_detect(feats[0].get("properties") or {})
    if not det.get("value"): return []                       # no cost field -> hold
    vcol, dcol = det["value"], det.get("date")
    sv = (feats[0].get("properties") or {}).get(vcol)
    numeric = isinstance(sv, (int, float)) or (isinstance(sv, str) and sv.replace(".", "", 1).replace("-", "", 1).isdigit())
    where = ("%s > %d" % (vcol, floor)) if numeric else "1=1"
    data = q(where, order=(dcol + " DESC") if dcol else None, count=2000)
    fs = (data or {}).get("features", []) if isinstance(data, dict) else []
    cfg = {"source": "arcgis_discovered:" + host, "city": title, "state": "",
           "portal": "https://" + host, "min_value": floor}
    out = []
    for f in fs:
        p = _arcgis_project_from(f.get("properties") or {}, f.get("geometry"), det, cfg, floor)
        if p: out.append(p)
    return out[:_DISCOVERY_PORTAL_CAP]

_ARCGIS_DISCOVERY_CAP = 120
def fetch_arcgis_discovered(max_services=_ARCGIS_DISCOVERY_CAP):
    """Find fresh building/construction-permit Feature Services via the ArcGIS
    Online item-search API and harvest each ($5M-gated, coords-required). Twin of
    the Socrata discovery source. Runs on Actions (reaches arcgis.com); sandbox cannot."""
    import re, time, urllib.parse
    q = 'title:"building permits" AND type:"Feature Service"'
    search = "https://www.arcgis.com/sharing/rest/search?" + urllib.parse.urlencode(
        {"f": "json", "q": q, "num": 100, "sortField": "modified", "sortOrder": "desc"})
    try:
        cat = _get_json(search)
    except Exception as e:
        print("  arcgis discovery search failed: %s" % e); return []
    results = cat.get("results", []) if isinstance(cat, dict) else []
    NAMEOK = re.compile(r'permit', re.I); KIND = re.compile(r'\b(building|construction|development)\b', re.I)
    BAD = re.compile(r'count|summ|metric|monthly|annual|aggregate|dashboard|statistic|electrical|plumbing|'
                     r'mechanical|solar|roof|\bsign\b|fee|parcel|zoning|address|inspection|violation|contractor', re.I)
    cutoff = int((time.time() - 550 * 86400) * 1000)
    bespoke = {c.get("url") for c in ARCGIS_PERMIT_CITIES}
    picked = []; seen = set()
    for r in results:
        title = r.get("title", "") or ""; url = r.get("url", "") or ""
        typ = r.get("type", ""); mod = r.get("modified", 0) or 0
        if typ != "Feature Service" or not url or url in bespoke: continue
        if "maps2.dcgis.dc.gov" in url or "building_permits/FeatureServer" in url: continue   # dedup DC/Tempe
        if not (NAMEOK.search(title) and KIND.search(title)) or BAD.search(title): continue
        if mod and mod < cutoff: continue
        if url in seen: continue
        seen.add(url)
        host = urllib.parse.urlparse(url).netloc
        picked.append({"title": title, "url": url, "host": host})
        if len(picked) >= max_services: break
    print("  arcgis discovery: %d candidate permit services" % len(picked))
    out = []
    for c in picked:
        try:
            rows = _arcgis_discover_one(c["url"], c["title"], c["host"])
        except Exception as e:
            print("  arcgis discovery %s failed: %s" % (c["host"], e)); continue
        if rows: print("    + %-40s %4d permits (%s)" % (c["host"], len(rows), c["title"][:34]))
        out += rows
        if len(out) >= _DISCOVERY_TOTAL_CAP:
            print("  arcgis discovery: total cap %d hit, stopping" % _DISCOVERY_TOTAL_CAP); out = out[:_DISCOVERY_TOTAL_CAP]; break
    print("  arcgis discovery: %d significant permits from %d services" % (len(out), len(picked)))
    return out

# ---------------------------------------------------------------------------
# Ireland -- National Planning Application Database (Dept of Housing), public
# ArcGIS FeatureServer (no key). Merged planning registers of the 31 local
# authorities. No valuation, so significance is SIZE-based: large residential
# (>=30 units), large sites (>=2 ha) or big floor area (>=5000 m2); single rural
# houses (OneOffKPI=Yes) and withdrawn/invalid apps excluded. Coords from geometry
# (outSR=4326). Source confirmed via data.gov.ie / GeoHive.
# ---------------------------------------------------------------------------
def fetch_ireland_planning():
    import urllib.parse
    base = ("https://services.arcgis.com/NzlPQPKn5QF9v2US/arcgis/rest/services/"
            "IrishPlanningApplications/FeatureServer/0/query?")
    fields = ("PlanningAuthority,ApplicationNumber,DevelopmentDescription,ApplicationStatus,"
              "ApplicationType,NumResidentialUnits,AreaofSite,FloorArea,ReceivedDate,OneOffKPI,LinkAppDetails")
    def q(where):
        p = {"where": where, "outFields": fields, "outSR": "4326", "f": "geojson",
             "resultRecordCount": 2000, "orderByFields": "ReceivedDate DESC", "returnGeometry": "true"}
        return _get_json(base + urllib.parse.urlencode(p))
    feats = []
    for where in ("NumResidentialUnits >= 30 OR AreaofSite >= 2 OR FloorArea >= 5000", "1=1"):
        try:
            data = q(where)
        except Exception as e:
            print("  ireland planning query failed: %s" % e); data = None
        feats = (data or {}).get("features", []) if isinstance(data, dict) else []
        if feats: break
    out = []
    for f in feats:
        try:
            pr = f.get("properties") or {}
            if str(pr.get("OneOffKPI") or "").strip().lower() == "yes":
                continue                                          # single rural house
            units = _num(pr.get("NumResidentialUnits")); area = _num(pr.get("AreaofSite")); floor = _num(pr.get("FloorArea"))
            big = (units is not None and units >= 30) or (area is not None and area >= 2) or (floor is not None and floor >= 5000)
            if not big:
                continue
            status = str(pr.get("ApplicationStatus") or "")
            if any(k in status.lower() for k in ("withdrawn", "invalid", "incomplete")):
                continue
            geom = f.get("geometry")
            if geom and geom.get("type") == "Point":
                co = geom.get("coordinates") or []
                la, lo = (co[1], co[0]) if len(co) >= 2 else (None, None)
            else:
                c = _geom_center(geom); la, lo = c if c else (None, None)
            if la is None or lo is None:
                continue
            sz = ("%d homes" % int(units)) if (units and units >= 30) else \
                 (("%.1f ha" % area) if (area and area >= 2) else (("%d m2" % int(floor)) if floor else ""))
            nm = (str(pr.get("DevelopmentDescription") or "").strip()[:130] or "Irish planning application")
            p = {"name": nm, "type": str(pr.get("ApplicationType") or "Planning application")[:60],
                 "state": "Ireland", "lat": round(la, 5), "lng": round(lo, 5), "precise": True,
                 "value_usd": None, "size": sz, "status": status,
                 "company": str(pr.get("PlanningAuthority") or "")[:80],
                 "url": str(pr.get("LinkAppDetails") or "https://planning.geohive.ie/"),
                 "date": _iso_date(pr.get("ReceivedDate")),
                 "desc": ("Irish planning application (%s). National Planning Application Database; "
                          "check the local authority file and An Coimisi\u00fan Plean\u00e1la for appeals and "
                          "comment windows." % (sz or "large development")),
                 "source": "ireland_planning"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  ireland planning: %d significant developments" % len(out))
    return out


# ---------------------------------------------------------------------------
# Portugal -- national Environmental Impact Assessment processes (APA / SNIAmb),
# public ArcGIS MapServer (no key). Layer 0 "Estudos" = AIA processes, point +
# study-area geometry in EPSG:3763, so we query outSR=4326. No valuation, so (like
# IBAMA/ANLA/IAAC) every national EIA process is kept -- these are major projects
# by definition. Field names are Portuguese and not documented, so they are sniffed
# at runtime over the actually-returned properties (no guessing of data). Service
# verified via the SNIAmb ArcGIS REST directory. FLAG: review first Actions-run log.
# ---------------------------------------------------------------------------
def fetch_portugal_eia():
    import urllib.parse
    url = ("https://sniambgeoogc.apambiente.pt/getogc/rest/services/SNIAmb/"
           "Avaliacao_de_Impacte_Ambiental/MapServer/0/query?" + urllib.parse.urlencode({
               "where": "1=1", "outFields": "*", "outSR": "4326", "f": "geojson",
               "resultRecordCount": 4000, "returnGeometry": "true"}))
    try:
        data = _get_json(url)
    except Exception as e:
        print("  portugal eia query failed: %s" % e); return []
    feats = (data or {}).get("features", []) if isinstance(data, dict) else []
    def pick(props, subs):
        for k in props.keys():
            u = k.upper()
            if any(s in u for s in subs):
                return k
        return None
    out = []
    for f in feats:
        try:
            pr = f.get("properties") or {}
            if not pr:
                continue
            geom = f.get("geometry") or {}
            c = _geom_center(geom)
            if not c:
                continue
            la, lo = c
            k_name = pick(pr, ["DESIG", "NOME", "PROJET", "TITUL", "ASSUNTO", "DESCR"])
            k_stat = pick(pr, ["ESTADO", "FASE", "SITUAC", "DECIS"])
            k_type = pick(pr, ["TIPOLOG", "NATUREZA", "CATEG", "SETOR", "SECTOR", "TIPO"])
            k_proc = pick(pr, ["NUP", "PROC", "NUMERO", "NUM_"])
            k_prom = pick(pr, ["PROMOTOR", "PROPON", "REQUER", "ENTIDAD"])
            k_link = pick(pr, ["URL", "LINK", "LIGA"])
            k_date = pick(pr, ["DATA", "DATE"])
            nm = (str(pr.get(k_name)).strip() if k_name and pr.get(k_name) else "") \
                 or (("Processo AIA " + str(pr.get(k_proc))) if k_proc and pr.get(k_proc) else "Processo de Avalia\u00e7\u00e3o de Impacte Ambiental")
            link = str(pr.get(k_link)).strip() if k_link and pr.get(k_link) else ""
            if not link.lower().startswith("http"):
                link = "https://siaia.apambiente.pt/"
            p = {"name": nm[:140],
                 "type": (str(pr.get(k_type)).strip()[:60] if k_type and pr.get(k_type) else "Avalia\u00e7\u00e3o de Impacte Ambiental"),
                 "state": "Portugal", "lat": round(la, 5), "lng": round(lo, 5),
                 "precise": (geom.get("type") == "Point"),
                 "value_usd": None, "size": "",
                 "status": (str(pr.get(k_stat)).strip()[:60] if k_stat and pr.get(k_stat) else ""),
                 "company": (str(pr.get(k_prom)).strip()[:80] if k_prom and pr.get(k_prom) else ""),
                 "url": link, "date": _iso_date(pr.get(k_date)) if k_date else None,
                 "desc": ("Portuguese Environmental Impact Assessment process (national AIA register, "
                          "Ag\u00eancia Portuguesa do Ambiente / SIAIA). Check the SIAIA file for the public "
                          "consultation window."),
                 "source": "portugal_eia"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  portugal eia: %d AIA processes" % len(out))
    return out


# ---------------------------------------------------------------------------
# Chile -- SEIA (Sistema de Evaluacion de Impacto Ambiental). The Environmental
# Assessment Service (SEA) publishes a georeferenced public layer of every project
# entering environmental assessment. VERIFIED live (ArcGIS MapServer):
#   https://arcgisv11.sea.gob.cl/server/rest/services/WEBServices/ProyectosSEIA/MapServer
# Layer 1 = EIA (Estudios de Impacto Ambiental -- the MAJOR projects: mines, dams,
# power, ports) which is what communities organise around; layer 2 = DIA (smaller
# declarations). Point geometry, SR 3857 (query outSR=4326). Fields (verified):
# NOMBRE_PROYECTO, ESTADO_EVALUACION, REGION, TITULAR, INVERSION_US (US$ millions),
# NOMBRE_TIPOLOGIA, FECHA_PRESENTACION, FECHA_CALIFICACION, URL_EXPEDIENTE.
# Scope gate: keep only in-process -- "En Calificacion" (under evaluation, any date)
# and recently "Aprobado" (approved within ~36 months, i.e. the build pipeline);
# drop Rechazado/Desistido/No Admitido/Caducado/Revocado/etc. Fail-safe: an
# unrecognised state is dropped, never included.
# ---------------------------------------------------------------------------
CHILE_SEIA_EIA = ("https://arcgisv11.sea.gob.cl/server/rest/services/"
                  "WEBServices/ProyectosSEIA/MapServer/1/query")

def fetch_chile_seia(pages=10, per=2000, approved_months=36):
    import json as _json
    out = []
    cutoff = (time.time() - approved_months * 2629800) * 1000.0     # ms epoch, ~36 mo
    for pg in range(pages):
        params = {"where": "1=1", "outFields": "*", "f": "geojson",
                  "outSR": "4326", "orderByFields": "FECHA_PRESENTACION DESC",
                  "resultRecordCount": per, "resultOffset": pg * per}
        try:
            gj = _get_json(CHILE_SEIA_EIA + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            print("  chile seia: page %d failed: %s" % (pg, e)); break
        feats = (gj.get("features") or []) if isinstance(gj, dict) else []
        if not feats:
            break
        for f in feats:
            try:
                geom = f.get("geometry") or {}; c = geom.get("coordinates") or []
                if geom.get("type") != "Point" or len(c) < 2:
                    continue
                lng, lat = float(c[0]), float(c[1])
                pr = f.get("properties") or {}
                estado = str(pr.get("ESTADO_EVALUACION") or "").strip().lower()
                # in-process gate (accent-insensitive substring; fail-safe)
                keep = False
                if "calific" in estado and "no calific" not in estado and "descalific" not in estado:
                    keep = True                                    # En Calificacion (under evaluation)
                elif "aprobad" in estado:
                    fc = _num(pr.get("FECHA_CALIFICACION"))
                    keep = (fc is not None and fc >= cutoff)       # approved, recently only
                if not keep:
                    continue
                inv = _num(pr.get("INVERSION_US"))                 # US$ millions
                val = inv * 1e6 if inv else None
                name = str(pr.get("NOMBRE_PROYECTO") or "Proyecto SEIA").strip()[:140]
                region = str(pr.get("REGION") or "").strip()
                comunas = str(pr.get("COMUNAS") or "").strip()
                place = comunas or region
                url = str(pr.get("URL_EXPEDIENTE") or "").strip()
                p = {"name": name,
                     "type": str(pr.get("NOMBRE_TIPOLOGIA") or "Proyecto (EIA)").strip()[:60],
                     "state": ("Chile" + ((" \u2014 " + region) if region else "")),
                     "lat": round(lat, 5), "lng": round(lng, 5), "precise": True,
                     "value_usd": val, "size": "",
                     "status": (pr.get("ESTADO_EVALUACION") or "").strip(),
                     "company": str(pr.get("TITULAR") or "").strip()[:80],
                     "url": url or "https://seia.sea.gob.cl/",
                     "date": _iso_date(pr.get("FECHA_PRESENTACION")),
                     "desc": ("Chilean project under national environmental assessment (SEIA) "
                              "%s%s. An EIA (Estudio de Impacto Ambiental) is filed for the largest "
                              "projects -- mines, dams, power, ports. Open the expediente for the "
                              "documents, the public-comment period and the evaluating authority." %
                              (("in " + place) if place else "",
                               (" \u2014 investment US$%s" % format(int(val), ",")) if val else "")),
                     "source": "chile_seia"}
                p["impact"] = rate_project(p, sensitivity=1)
                out.append(p)
            except Exception:
                continue
        if len(feats) < per:
            break
    print("  chile seia: %d in-process EIA projects" % len(out))
    return out


# ---------------------------------------------------------------------------
# Peru -- SENACE (Servicio Nacional de Certificacion Ambiental). SENACE evaluates
# the environmental impact studies of the largest investment projects (mining,
# hydrocarbons, energy, transport). Its GeoSENACE ArcGIS server publishes a public
# point layer of the environmental-management instruments SUBMITTED FOR EVALUATION
# and certification. VERIFIED the service exists (ArcGIS MapServer, SR 4326, point
# layer): https://geosenace.senace.gob.pe/arcgis/rest/services/DGE/IGA_SENACE/MapServer/0
# The server blocks automated readers (robots), so the exact field NAMES could not
# be confirmed from here -- like Portugal, fields are therefore detected at RUNTIME
# by a Spanish-language sniffer rather than hardcoded/guessed. Scope: this layer is
# projects under evaluation / certification (an active national EIA registry, like
# IBAMA/ANLA), so keep-all minus records whose status is clearly finished-dead
# (desaprobado / no conforme / archivado / desistido / denegado). FIRST-RUN REVIEW.
# ---------------------------------------------------------------------------
PERU_SENACE = ("https://geosenace.senace.gob.pe/arcgis/rest/services/"
               "DGE/IGA_SENACE/MapServer/0/query")
_PE_DEAD = ("desaprob", "no conforme", "archivad", "desist", "denegad", "rechaz",
            "abandonad", "caducad", "no admit")

def fetch_peru_senace(pages=10, per=1000):
    out = []
    def pick(keys, *subs):
        for k in keys:
            kl = k.lower()
            if any(s in kl for s in subs): return k
        return None
    for pg in range(pages):
        params = {"where": "1=1", "outFields": "*", "f": "geojson",
                  "outSR": "4326", "resultRecordCount": per, "resultOffset": pg * per}
        try:
            gj = _get_json(PERU_SENACE + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            print("  peru senace: page %d failed: %s" % (pg, e)); break
        feats = (gj.get("features") or []) if isinstance(gj, dict) else []
        if not feats:
            break
        # detect the useful columns once, from the first feature's keys
        keys = list((feats[0].get("properties") or {}).keys())
        k_name = pick(keys, "nombre", "proyecto")
        k_stat = pick(keys, "estado", "situacion", "situaci\u00f3n", "evaluaci")
        k_type = pick(keys, "tipo", "instrumento", "iga")
        k_titl = pick(keys, "titular", "empresa", "proponente")
        k_inv  = pick(keys, "inversion", "inversi\u00f3n", "monto")
        k_url  = pick(keys, "url", "expediente", "enlace", "link")
        k_place = pick(keys, "region", "departamento", "distrito", "provincia", "ubica")
        for f in feats:
            try:
                geom = f.get("geometry") or {}; c = geom.get("coordinates") or []
                if geom.get("type") == "Point" and len(c) >= 2:
                    lng, lat = float(c[0]), float(c[1]); precise = True
                else:
                    ctr = _geom_center(geom)
                    if not ctr: continue
                    lat, lng = ctr; precise = False
                pr = f.get("properties") or {}
                status = str(pr.get(k_stat) or "").strip() if k_stat else ""
                if status and any(d in status.lower() for d in _PE_DEAD):
                    continue                                   # clearly finished/dead -> drop
                name = (str(pr.get(k_name)).strip()[:140] if k_name and pr.get(k_name)
                        else "Proyecto SENACE")
                inv = _num(pr.get(k_inv)) if k_inv else None
                val = inv * 1e6 if (inv and inv < 1e6) else inv  # SENACE reports US$ millions
                place = str(pr.get(k_place)).strip() if k_place and pr.get(k_place) else ""
                url = str(pr.get(k_url)).strip() if k_url and pr.get(k_url) else ""
                p = {"name": name,
                     "type": (str(pr.get(k_type)).strip()[:60] if k_type and pr.get(k_type)
                              else "Instrumento de gesti\u00f3n ambiental"),
                     "state": ("Peru" + ((" \u2014 " + place) if place else "")),
                     "lat": round(lat, 5), "lng": round(lng, 5), "precise": precise,
                     "value_usd": val, "size": "",
                     "status": status,
                     "company": (str(pr.get(k_titl)).strip()[:80] if k_titl and pr.get(k_titl) else ""),
                     "url": url or "https://www.gob.pe/senace",
                     "desc": ("Peruvian investment project under national environmental "
                              "certification (SENACE)%s. SENACE evaluates the environmental "
                              "impact studies of the country's largest mining, hydrocarbons, "
                              "energy and transport projects. Open the record for the study, "
                              "the public-participation process and the evaluating authority." %
                              ((" in " + place) if place else ""))}
                p["source"] = "peru_senace"
                p["impact"] = rate_project(p, sensitivity=1)
                out.append(p)
            except Exception:
                continue
        if len(feats) < per:
            break
    print("  peru senace: %d SENACE projects" % len(out))
    return out


# ---------------------------------------------------------------------------
# Australia (New South Wales) -- NSW "Major Projects" register: State Significant
# Development / Infrastructure assessed by the NSW Dept of Planning (the mines,
# quarries, data centres, warehouses, energy and big subdivisions that bypass
# local councils). VERIFIED live ArcGIS MapServer, point geometry, SR 4283 ->
# queried as 4326: REI/Major_Projects/MapServer/0, Display Field "Name", status
# field "Case_status" with values Open*/Pending*/Recommendation/Resolved-*. Scope
# gate keeps only PRE-DECISION cases (Open, Pending, Recommendation = proposed /
# under assessment); Resolved-* (decided) and any unrecognized status are dropped
# (fail-safe). Attribute names beyond Name/Case_status aren't confirmable from
# here, so they're detected at RUNTIME rather than guessed. FIRST-RUN REVIEW.
# ---------------------------------------------------------------------------
NSW_MAJOR = ("https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/"
             "REI/Major_Projects/MapServer/0/query")

def fetch_nsw_major(pages=12, per=1000):
    out = []; fields = None
    for pg in range(pages):
        params = {"where": "1=1", "outFields": "*", "f": "geojson",
                  "outSR": "4326", "resultRecordCount": per, "resultOffset": pg * per}
        try:
            gj = _get_json(NSW_MAJOR + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            print("  nsw major: page %d failed: %s" % (pg, e)); break
        feats = (gj.get("features") or []) if isinstance(gj, dict) else []
        if not feats:
            break
        if fields is None:                                    # runtime field detection
            cols = list((feats[0].get("properties") or {}).keys())
            _rows = [(f.get("properties") or {}) for f in feats[:60]]
            fields = {
                "name":   _sniff_name_col(cols, _rows, "name", "project", "title") or "Name",
                "status": _sniff_col(cols, "case_status", "status", "stage") or "Case_status",
                "type":   _sniff_col(cols, "developmenttype", "type", "category", "class", "purpose"),
                "who":    _sniff_col(cols, "applicant", "proponent", "developer", "company"),
                "date":   _sniff_col(cols, "lodge", "submitted", "received", "date"),
                "url":    _sniff_col(cols, "url", "link", "majorproject", "planningportal"),
                "lga":    _sniff_col(cols, "lga", "council", "localgov", "region"),
            }
        for f in feats:
            try:
                geom = f.get("geometry") or {}; c = geom.get("coordinates") or []
                if geom.get("type") != "Point" or len(c) < 2:
                    continue
                lng, lat = float(c[0]), float(c[1])
                pr = f.get("properties") or {}
                status = str(pr.get(fields["status"]) or "").strip()
                sl = status.lower()
                # in-process gate (fail-safe whitelist): only PRE-DECISION stages kept
                if not (sl.startswith("open") or sl.startswith("pending")
                        or sl.startswith("recommend")):
                    continue
                name = str(pr.get(fields["name"]) or "Major project").strip()[:140]
                typ = (str(pr.get(fields["type"]) or "").strip()[:60] if fields["type"] else "")
                lga = (str(pr.get(fields["lga"]) or "").strip() if fields["lga"] else "")
                who = (str(pr.get(fields["who"]) or "").strip()[:80] if fields["who"] else "")
                url = (str(pr.get(fields["url"]) or "").strip() if fields["url"] else "")
                if not url.startswith("http"):
                    url = "https://www.planningportal.nsw.gov.au/major-projects"
                p = {"name": name,
                     "type": typ or "State significant project",
                     "state": "Australia \u2014 New South Wales" + ((" \u2014 " + lga) if lga else ""),
                     "lat": round(lat, 5), "lng": round(lng, 5), "precise": True,
                     "value_usd": None, "size": "",
                     "status": status,
                     "company": who,
                     "url": url,
                     "date": (_iso_date(pr.get(fields["date"])) if fields["date"] else ""),
                     "desc": ("NSW State Significant project under the Major Projects "
                              "assessment pathway -- the mines, quarries, energy, data "
                              "centres, warehouses and large subdivisions decided by the "
                              "state rather than local councils. Open the case on the NSW "
                              "Planning Portal for the EIS, the exhibition period and how "
                              "to make a submission."),
                     "source": "nsw_major"}
                p["impact"] = rate_project(p, sensitivity=1)
                out.append(p)
            except Exception:
                continue
        if len(feats) < per:
            break
    print("  nsw major: %d in-process state-significant projects" % len(out))
    return out


# ---------------------------------------------------------------------------
# Australia (Queensland) -- the Coordinator-General's "Coordinated Projects":
# large infrastructure (mines, dams, ports, pipelines, industrial precincts)
# declared under the State Development and Public Works Organisation Act and put
# through a state-run impact assessment. VERIFIED live ArcGIS MapServer (layer 10),
# POLYGON geometry, SR 4283 -> queried as 4326, Display Field "name":
# PlanningCadastre/CoordinatedProjects/MapServer/10. Polygons are reduced to a
# centroid (precise:false). A status/stage field is detected at RUNTIME and gated
# fail-safe; if none is present the layer is, by its own description, "current
# proposed infrastructure", so rows are kept. FIRST-RUN REVIEW.
# ---------------------------------------------------------------------------
QLD_COORD = ("https://spatial-gis.information.qld.gov.au/arcgis/rest/services/"
             "PlanningCadastre/CoordinatedProjects/MapServer/10/query")
_AU_DEAD = ("complet", "finish", "lapsed", "withdraw", "cancel", "closed",
            "not proceed", "decommission", "operational", "operating")
_AU_LIVE = ("declar", "eis", "assess", "current", "propos", "under", "progress",
            "review", "evaluat", "application", "pending", "active", "notif")

def fetch_qld_coordinated(pages=8, per=2000):
    out = []; fields = None
    for pg in range(pages):
        params = {"where": "1=1", "outFields": "*", "f": "geojson", "outSR": "4326",
                  "resultRecordCount": per, "resultOffset": pg * per}
        try:
            gj = _get_json(QLD_COORD + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            print("  qld coordinated: page %d failed: %s" % (pg, e)); break
        feats = (gj.get("features") or []) if isinstance(gj, dict) else []
        if not feats:
            break
        if fields is None:
            cols = list((feats[0].get("properties") or {}).keys())
            _rows = [(f.get("properties") or {}) for f in feats[:60]]
            fields = {
                "name":   _sniff_name_col(cols, _rows, "name", "project", "title") or "name",
                "status": _sniff_col(cols, "status", "stage", "phase", "progress"),
                "type":   _sniff_col(cols, "type", "category", "sector", "class"),
                "who":    _sniff_col(cols, "proponent", "applicant", "developer", "company"),
                "url":    _sniff_col(cols, "url", "link", "web"),
                "date":   _sniff_col(cols, "declared", "lodged", "date"),
            }
        for f in feats:
            try:
                geom = f.get("geometry") or {}
                if geom.get("type") == "Point":
                    c = geom.get("coordinates") or []
                    if len(c) < 2: continue
                    lat, lng, precise = float(c[1]), float(c[0]), True
                else:
                    ctr = _geom_center(geom)
                    if not ctr: continue
                    lat, lng, precise = ctr[0], ctr[1], False
                pr = f.get("properties") or {}
                status = str(pr.get(fields["status"]) or "").strip() if fields["status"] else ""
                sl = status.lower()
                if sl:                                        # gate only when status present (fail-safe)
                    if any(d in sl for d in _AU_DEAD): continue
                    if not any(k in sl for k in _AU_LIVE): continue
                name = str(pr.get(fields["name"]) or "Coordinated project").strip()[:140]
                typ = (str(pr.get(fields["type"]) or "").strip()[:60] if fields["type"] else "")
                who = (str(pr.get(fields["who"]) or "").strip()[:80] if fields["who"] else "")
                url = (str(pr.get(fields["url"]) or "").strip() if fields["url"] else "")
                if not url.startswith("http"):
                    url = "https://www.statedevelopment.qld.gov.au/coordinator-general/assessments-and-approvals/coordinated-projects"
                p = {"name": name,
                     "type": typ or "Coordinated project (infrastructure)",
                     "state": "Australia \u2014 Queensland",
                     "lat": round(lat, 5), "lng": round(lng, 5), "precise": precise,
                     "value_usd": None, "size": "",
                     "status": status or "Coordinated project",
                     "company": who,
                     "url": url,
                     "date": (_iso_date(pr.get(fields["date"])) if fields["date"] else ""),
                     "desc": ("Queensland Coordinated Project -- major infrastructure "
                              "(mines, dams, ports, pipelines, industrial precincts) declared "
                              "for a state-run impact assessment by the Coordinator-General. "
                              "Open the project page for the EIS, the submission period and the "
                              "assessment documents."),
                     "source": "qld_coordinated"}
                p["impact"] = rate_project(p, sensitivity=1)
                out.append(p)
            except Exception:
                continue
        if len(feats) < per:
            break
    print("  qld coordinated: %d coordinated projects" % len(out))
    return out


# ---------------------------------------------------------------------------
# Canada (British Columbia) -- EAO Project Information Centre (EPIC): the major
# projects (mines, LNG, pipelines, dams, resorts) going through BC's provincial
# environmental assessment. VERIFIED public layer EPIC_PROJECT_POINTS_SVW in the
# BC Geographic Warehouse, updated daily, served as GeoJSON over the province's
# WFS (openmaps.gov.bc.ca). POINT geometry. Field names + status vocabulary aren't
# confirmable from here, so both are handled at RUNTIME: fields sniffed, and the
# in-process gate keeps only pre-decision phases (pre-application / application /
# assessment / under review / in progress) while dropping certified, withdrawn,
# terminated and completed (fail-safe -- unrecognized statuses are dropped).
# Project pages: projects.eao.gov.bc.ca. FIRST-RUN REVIEW.
# ---------------------------------------------------------------------------
BC_EAO = ("https://openmaps.gov.bc.ca/geo/pub/"
          "WHSE_ENVIRONMENT_ASSESSMENT.EPIC_PROJECT_POINTS_SVW/ows")
_BC_LIVE = ("pre-application", "pre application", "application", "assess", "under review",
            "in progress", "review", "screening", "propos", "pending", "readiness",
            "effects", "recommend")
_BC_DEAD = ("certif", "certificate", "complet", "withdraw", "terminat", "decommission",
            "not certified", "exempt", "closed", "abandon", "operating", "operational",
            "post-certificate", "post certificate")

def fetch_bc_eao(pages=10, per=1000):
    out = []; fields = None
    for pg in range(pages):
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                  "typeNames": "pub:WHSE_ENVIRONMENT_ASSESSMENT.EPIC_PROJECT_POINTS_SVW",
                  "outputFormat": "application/json", "srsName": "EPSG:4326",
                  "count": per, "startIndex": pg * per}
        try:
            gj = _get_json(BC_EAO + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            print("  bc eao: page %d failed: %s" % (pg, e)); break
        feats = (gj.get("features") or []) if isinstance(gj, dict) else []
        if not feats:
            break
        if fields is None:
            cols = list((feats[0].get("properties") or {}).keys())
            _rows = [(f.get("properties") or {}) for f in feats[:60]]
            fields = {
                "name":   _sniff_name_col(cols, _rows, "project_name", "name", "proj", "title"),
                "status": _sniff_col(cols, "status", "phase", "stage", "current_phase", "disposition"),
                "type":   _sniff_col(cols, "type", "category", "sector", "purpose"),
                "who":    _sniff_col(cols, "proponent", "applicant", "developer", "company"),
                "url":    _sniff_col(cols, "url", "link", "web", "project_url"),
            }
        for f in feats:
            try:
                geom = f.get("geometry") or {}; c = geom.get("coordinates") or []
                if geom.get("type") != "Point" or len(c) < 2:
                    continue
                lng, lat = float(c[0]), float(c[1])
                pr = f.get("properties") or {}
                status = str(pr.get(fields["status"]) or "").strip() if fields["status"] else ""
                sl = status.lower()
                # in-process gate: keep only pre-decision phases; drop decided/unknown (fail-safe)
                if any(d in sl for d in _BC_DEAD): continue
                if not any(k in sl for k in _BC_LIVE): continue
                name = (str(pr.get(fields["name"]) or "").strip()[:140] if fields["name"] else "")
                if not name: name = "EAO project"
                typ = (str(pr.get(fields["type"]) or "").strip()[:60] if fields["type"] else "")
                who = (str(pr.get(fields["who"]) or "").strip()[:80] if fields["who"] else "")
                url = (str(pr.get(fields["url"]) or "").strip() if fields["url"] else "")
                if not url.startswith("http"):
                    url = "https://projects.eao.gov.bc.ca/"
                p = {"name": name,
                     "type": typ or "Environmental assessment (BC)",
                     "state": "Canada \u2014 British Columbia",
                     "lat": round(lat, 5), "lng": round(lng, 5), "precise": True,
                     "value_usd": None, "size": "",
                     "status": status,
                     "company": who,
                     "url": url,
                     "date": "",
                     "desc": ("Major project under British Columbia's provincial "
                              "environmental assessment (EAO) -- mines, LNG, pipelines, dams "
                              "and resorts. Open the project on EPIC for the assessment "
                              "documents, the comment periods and how to participate."),
                     "source": "bc_eao"}
                p["impact"] = rate_project(p, sensitivity=1)
                out.append(p)
            except Exception:
                continue
        if len(feats) < per:
            break
    print("  bc eao: %d in-process EA projects" % len(out))
    return out


# ---------------------------------------------------------------------------
# Canada (Saskatchewan) -- Ministry of Environment, Environmental Assessment
# Branch. The public EASProjects ArcGIS service exposes the projects moving
# through the province's EA process. VERIFIED live (production host, no token):
# gis.saskatchewan.ca/arcgis/rest/services/EASProjects/MapServer -- point layers
# 0 "Active Applications" and 1 "Active EIA Projects" are the in-process ones
# (layers 2/3 are Historical and are skipped). SR 2957 -> queried as 4326,
# geoJSON supported. The layers are the province's OWN "active" classification, so
# they're kept as-is; a status field, if present, is gated fail-safe to drop any
# stray decided/withdrawn rows. Attribution: Environmental Assessment Branch,
# Saskatchewan Ministry of Environment. Field names detected at RUNTIME.
# FIRST-RUN REVIEW.
# ---------------------------------------------------------------------------
SASK_EAS = ("https://gis.saskatchewan.ca/arcgis/rest/services/"
            "EASProjects/MapServer/%d/query")

def fetch_sask_eia(per=2000):
    out = []
    for layer in (0, 1):                      # 0 Active Applications, 1 Active EIA Projects
        fields = None
        for pg in range(4):
            params = {"where": "1=1", "outFields": "*", "f": "geojson", "outSR": "4326",
                      "resultRecordCount": per, "resultOffset": pg * per}
            try:
                gj = _get_json((SASK_EAS % layer) + "?" + urllib.parse.urlencode(params))
            except Exception as e:
                print("  sask eia L%d: page %d failed: %s" % (layer, pg, e)); break
            feats = (gj.get("features") or []) if isinstance(gj, dict) else []
            if not feats:
                break
            if fields is None:
                cols = list((feats[0].get("properties") or {}).keys())
                _rows = [(f.get("properties") or {}) for f in feats[:60]]
                fields = {
                    "name":   _sniff_name_col(cols, _rows, "project", "name", "title", "proposal"),
                    "status": _sniff_col(cols, "status", "stage", "phase", "decision"),
                    "type":   _sniff_col(cols, "type", "category", "sector", "class", "nature"),
                    "who":    _sniff_col(cols, "proponent", "applicant", "developer", "company", "owner"),
                    "date":   _sniff_col(cols, "received", "submitted", "date", "lodged"),
                    "url":    _sniff_col(cols, "url", "link", "web"),
                }
            for f in feats:
                try:
                    geom = f.get("geometry") or {}; c = geom.get("coordinates") or []
                    if geom.get("type") != "Point" or len(c) < 2:
                        continue
                    lng, lat = float(c[0]), float(c[1])
                    pr = f.get("properties") or {}
                    status = str(pr.get(fields["status"]) or "").strip() if fields["status"] else ""
                    sl = status.lower()
                    if sl and any(d in sl for d in _AU_DEAD):     # fail-safe: drop stray decided/withdrawn
                        continue
                    name = (str(pr.get(fields["name"]) or "").strip()[:140] if fields["name"] else "")
                    if not name: name = "Environmental assessment project"
                    typ = (str(pr.get(fields["type"]) or "").strip()[:60] if fields["type"] else "")
                    who = (str(pr.get(fields["who"]) or "").strip()[:80] if fields["who"] else "")
                    url = (str(pr.get(fields["url"]) or "").strip() if fields["url"] else "")
                    if not url.startswith("http"):
                        url = ("https://www.saskatchewan.ca/business/environmental-protection-and-"
                               "sustainability/environmental-assessment/environmental-assessment-projects")
                    p = {"name": name,
                         "type": typ or ("Application (EA)" if layer == 0 else "Environmental assessment"),
                         "state": "Canada \u2014 Saskatchewan",
                         "lat": round(lat, 5), "lng": round(lng, 5), "precise": True,
                         "value_usd": None, "size": "",
                         "status": status or ("Active application" if layer == 0 else "Active EIA project"),
                         "company": who,
                         "url": url,
                         "date": (_iso_date(pr.get(fields["date"])) if fields["date"] else ""),
                         "desc": ("Project under Saskatchewan's environmental assessment process "
                                  "(mines, dams, power, industry, infrastructure). Open the province's "
                                  "EA projects list for the technical proposal, the public-comment "
                                  "window and the Minister's decision. Source: Environmental "
                                  "Assessment Branch, Saskatchewan Ministry of Environment."),
                         "source": "sask_eia"}
                    p["impact"] = rate_project(p, sensitivity=1)
                    out.append(p)
                except Exception:
                    continue
            if len(feats) < per:
                break
    print("  sask eia: %d active EA projects/applications" % len(out))
    return out


# ---------------------------------------------------------------------------
# Ireland -- national EIA Location Point layer (the "Environmental Impact
# Assessment Open Data Project", implementing EU Directive 2014/52/EU). VERIFIED
# public ArcGIS FeatureServer, CC-BY 4.0, point geometry:
# services.arcgis.com/NzlPQPKn5QF9v2US/.../EIA_Location_Point/FeatureServer/0
# This complements ireland_planning (the National Planning Application Database) by
# pinning the projects that actually triggered a full EIA -- the biggest builds.
# Overlap with ireland_planning is removed by the harvester's coordinate dedup.
# Field names / decision vocabulary aren't confirmable from here, so both are
# handled at RUNTIME; the gate drops clearly-decided rows (granted / refused /
# withdrawn / complete) fail-safe and keeps pending / new / under-consideration.
# FIRST-RUN REVIEW.
# ---------------------------------------------------------------------------
IRELAND_EIA = ("https://services.arcgis.com/NzlPQPKn5QF9v2US/arcgis/rest/services/"
               "EIA_Location_Point/FeatureServer/0/query")
_IE_DEAD = ("grant", "refus", "withdraw", "invalid", "complet", "closed",
            "decided", "permission", "final grant", "lapsed", "expired", "operational")

def fetch_ireland_eia(pages=10, per=2000):
    out = []; fields = None
    for pg in range(pages):
        params = {"where": "1=1", "outFields": "*", "f": "geojson", "outSR": "4326",
                  "resultRecordCount": per, "resultOffset": pg * per}
        try:
            gj = _get_json(IRELAND_EIA + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            print("  ireland eia: page %d failed: %s" % (pg, e)); break
        feats = (gj.get("features") or []) if isinstance(gj, dict) else []
        if not feats:
            break
        if fields is None:
            cols = list((feats[0].get("properties") or {}).keys())
            _rows = [(f.get("properties") or {}) for f in feats[:60]]
            fields = {
                "name":   _sniff_name_col(cols, _rows, "project", "development", "name", "title", "proposal"),
                "status": _sniff_col(cols, "decision", "status", "stage", "outcome", "determination"),
                "type":   _sniff_col(cols, "type", "class", "category", "nature", "annex"),
                "who":    _sniff_col(cols, "applicant", "developer", "proponent", "company"),
                "auth":   _sniff_col(cols, "authority", "council", "planning"),
                "url":    _sniff_col(cols, "url", "link", "web", "file"),
                "date":   _sniff_col(cols, "received", "lodged", "date", "decision_date"),
            }
        for f in feats:
            try:
                geom = f.get("geometry") or {}; c = geom.get("coordinates") or []
                if geom.get("type") != "Point" or len(c) < 2:
                    continue
                lng, lat = float(c[0]), float(c[1])
                pr = f.get("properties") or {}
                status = str(pr.get(fields["status"]) or "").strip() if fields["status"] else ""
                sl = status.lower()
                if sl and any(d in sl for d in _IE_DEAD):     # fail-safe: drop decided rows
                    continue
                name = (str(pr.get(fields["name"]) or "").strip()[:140] if fields["name"] else "")
                typ = (str(pr.get(fields["type"]) or "").strip()[:60] if fields["type"] else "")
                who = (str(pr.get(fields["who"]) or "").strip()[:80] if fields["who"] else "")
                name = _clean_proj_name(name, typ, who)      # never publish "Yes."/"No."
                if not name: name = "EIA development"
                auth = (str(pr.get(fields["auth"]) or "").strip() if fields["auth"] else "")
                url = (str(pr.get(fields["url"]) or "").strip() if fields["url"] else "")
                if not url.startswith("http"):
                    url = "https://data.gov.ie/dataset/eia-location-point1"
                p = {"name": name,
                     "type": typ or "EIA development",
                     "state": "Ireland" + ((" \u2014 " + auth) if auth else ""),
                     "lat": round(lat, 5), "lng": round(lng, 5), "precise": True,
                     "value_usd": None, "size": "",
                     "status": status,
                     "company": who,
                     "url": url,
                     "date": (_iso_date(pr.get(fields["date"])) if fields["date"] else ""),
                     "desc": ("Large enough to require a full Environmental Impact Assessment "
                              "Report (EIAR) under EU Directive 2014/52/EU -- the threshold that "
                              "catches major roads, quarries, energy, waste and large housing "
                              "schemes. The EIAR, the submissions window and the decision are on "
                              "the planning authority's file; observations from any member of the "
                              "public must be considered before permission issues."),
                     "source": "ireland_eia"}
                p["impact"] = rate_project(p, sensitivity=1)
                out.append(p)
            except Exception:
                continue
        if len(feats) < per:
            break
    print("  ireland eia: %d in-process EIA developments" % len(out))
    return out


# ---------------------------------------------------------------------------
# Land Matrix -- global large-scale land acquisitions ("land grabs"), ~2,300
# concluded transnational deals >=200 ha across ~97 countries (agriculture,
# forestry, mining, renewable-energy land, industry, speculation, tourism).
# Pulled LIVE + auto-updating weekly from the datasets/land-matrix GitHub mirror
# (built from the Land Matrix API; CC-BY-NC). The mirror is country-level (no
# per-deal coordinates), so each deal is placed at its country centroid
# (precise:false) using the google/dspl country-centroid CSV (also GitHub-hosted).
# Both sources are auto-pullable -- no manual downloads. Deal pages: landmatrix.org/deal/<n>/
# ---------------------------------------------------------------------------
_LM_DEALS_URL = "https://raw.githubusercontent.com/datasets/land-matrix/main/data/database.csv"
_LM_CENT_URL  = "https://raw.githubusercontent.com/google/dspl/master/samples/google/canonical/countries.csv"
# WB-style Land Matrix country name -> ISO2 (for the 13 names that differ from the
# centroid CSV's labels; coordinates themselves always come from the dspl CSV).
_LM_ALIAS = {
    "Congo, Dem. Rep.": "CD", "Congo, Rep.": "CG", "Egypt, Arab Rep.": "EG",
    "Gambia, The": "GM", "Kyrgyz Republic": "KG", "Lao PDR": "LA", "Myanmar": "MM",
    "North Macedonia": "MK", "Russian Federation": "RU", "South Sudan": "SS",
    "S\u00e3o Tom\u00e9 and Principe": "ST", "T\u00fcrkiye": "TR", "Venezuela, RB": "VE",
}
def _lm_read_csv(url, delim):
    import urllib.request, csv, io
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))
def fetch_land_matrix():
    try:
        cent_rows = _lm_read_csv(_LM_CENT_URL, ",")
    except Exception as e:
        print("  land matrix: centroid fetch failed: %s" % e); return []
    name2ll, iso2ll = {}, {}
    for c in cent_rows:
        try:
            ll = (float(c["latitude"]), float(c["longitude"]))
        except (TypeError, ValueError, KeyError):
            continue
        if c.get("name"): name2ll[c["name"]] = ll
        if c.get("country"): iso2ll[c["country"].upper()] = ll
    try:
        deals = _lm_read_csv(_LM_DEALS_URL, ";")
    except Exception as e:
        print("  land matrix: deals fetch failed: %s" % e); return []
    out, unplaced = [], set()
    for d in deals:
        try:
            country = (d.get("Target Country") or "").strip()
            ll = name2ll.get(country) or iso2ll.get(_LM_ALIAS.get(country, "").upper())
            if not ll:
                unplaced.add(country); continue
            ha = _num(d.get("Hectares"))
            inv = (d.get("Investor 1") or "").strip()
            sector = (d.get("Inv. Sector 1") or "").strip()
            crop = (d.get("Crop 1") or "").strip()
            invc = (d.get("Investor Country 1") or "").strip()
            yr = (d.get("Year") or "").strip()
            num = (d.get("Deal Number") or "").strip()
            use = crop or sector or "land acquisition"
            nm = ("%s \u2014 %s" % (inv, use)) if inv else ("Large-scale land deal \u2014 %s" % use)
            size = ("%s ha" % ("{:,}".format(int(ha)) if ha else "")) if ha else ""
            desc = ("Large-scale land acquisition tracked by the Land Matrix"
                    + (" (deal #%s)" % num if num else "") + ". "
                    + (("Investor: %s" % inv) + (" (%s)" % invc if invc else "") + ". " if inv else "")
                    + (("Intended use: %s. " % use) if use != "land acquisition" else "")
                    + "Concluded transnational deal \u2265200 ha; see the Land Matrix deal page for sources and status.")
            p = {"name": nm[:140], "type": (sector or "Land acquisition")[:60],
                 "state": country, "lat": round(ll[0], 4), "lng": round(ll[1], 4),
                 "precise": False, "value_usd": None, "size": size,
                 "status": "Concluded", "company": inv[:80],
                 "url": ("https://landmatrix.org/deal/%s/" % num) if num else "https://landmatrix.org/",
                 "date": (yr if (yr.isdigit() and len(yr) == 4) else None),
                 "acres": (round(ha * 2.47105) if ha else None),
                 "desc": desc, "source": "land_matrix"}
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  land matrix: %d land deals (%d countries)%s" % (
        len(out), len(set(p["state"] for p in out)),
        (" | unplaced: %s" % ", ".join(sorted(unplaced))) if unplaced else ""))
    return out


def fetch_dc_permits():
    base = "https://maps2.dcgis.dc.gov/dcgis/rest/services/FEEDS/DCRA/FeatureServer"
    feats = _arcgis_query_all(base, layer=4, label="dc permits")
    out = []
    for f in feats:
        try:
            pr = f.get("properties") or {}
            up = {str(k).upper(): v for k, v in pr.items()}
            if str(up.get("PERMIT_TYPE_NAME") or "").upper() != "CONSTRUCTION":
                continue                                   # skip supplemental (electrical/plumbing/etc.)
            sub = str(up.get("PERMIT_SUBTYPE_NAME") or "").upper()
            if not ("NEW CONSTRUCTION" in sub or "RAZE" in sub):
                continue                                   # keep only new builds + demolitions
            status = str(up.get("APPLICATION_STATUS_NAME") or "")
            if any(k in status.lower() for k in ("completed", "cancel", "withdrawn",
                                                 "expired", "revoked", "disapprov")):
                continue
            la = _num(up.get("LATITUDE")); lo = _num(up.get("LONGITUDE"))
            if la is None or lo is None or (abs(la) < 0.01 and abs(lo) < 0.01):
                continue                                   # ungeocoded 0,0 rows
            nm = str(up.get("DESC_OF_WORK") or up.get("FULL_ADDRESS") or "D.C. construction permit")[:140]
            kind = "Demolition" if "RAZE" in sub else "New construction"
            p = {
                "name": nm, "type": kind, "state": "District of Columbia",
                "lat": round(la, 5), "lng": round(lo, 5), "precise": True,
                "value_usd": None, "size": "", "status": status,
                "company": str(up.get("OWNER_NAME") or up.get("PERMIT_APPLICANT") or "")[:80],
                "url": "https://opendata.dc.gov/", "date": _iso_date(up.get("ISSUE_DATE")),
                "desc": ("D.C. building permit \u2014 " + kind.lower() + ". Local construction "
                         "filing; check the ANC agenda and Dept of Buildings docket for any "
                         "review or comment window."),
                "source": "dc_permits",
            }
            p["impact"] = rate_project(p, sensitivity=1)
            out.append(p)
        except Exception:
            continue
    print("  dc permits: %d new-construction / demolition permits (last 30 days)" % len(out))
    return out


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
# The world, gridded. Earlier this was a list of hand-drawn region boxes, which is
# how Sochi, Reykjavik, Alaska and the whole of Siberia ended up unqueried: every
# box edge is a chance to miss somewhere. Covering the globe outright removes the
# guesswork -- ocean tiles cost almost nothing (Overpass answers them instantly)
# and no inhabited place can fall through a seam again.
_OSM_LAT_MIN, _OSM_LAT_MAX = -60.0, 84.0     # Antarctic ice / high Arctic have no construction
_OSM_LNG_MIN, _OSM_LNG_MAX = -180.0, 180.0

def _osm_tiles(step=5.0):
    """Every 5-degree tile on Earth between 60S and 84N. ~2,088 tiles."""
    out = []
    la = _OSM_LAT_MIN
    while la < _OSM_LAT_MAX:
        lo = _OSM_LNG_MIN
        while lo < _OSM_LNG_MAX:
            out.append((round(la, 2), round(lo, 2),
                        round(min(la + step, _OSM_LAT_MAX), 2),
                        round(min(lo + step, _OSM_LNG_MAX), 2)))
            lo += step
        la += step
    return out

def _osm_existing():
    """Keep what earlier runs already harvested so coverage accumulates."""
    try:
        ex = _load_projects()
        rows = ex.get("projects", []) if isinstance(ex, dict) else (ex if isinstance(ex, list) else [])
        return [q for q in rows if q.get("source") == "osm_construction"]
    except Exception:
        return []


_OVERPASS_EPS = [
    # WORLDWIDE instances only. overpass.osm.ch was removed: it is the Switzerland-
    # only REGIONAL server, so for any non-Swiss tile it answered HTTP 200 with an
    # empty result -- a fake "success" that stopped the quad-split from ever running.
    # That is exactly what left the persistent grid holes over the densest regions
    # (northern US, western Europe, east Asia): overpass-api.de timed out there,
    # kumi failed, osm.ch "succeeded" with 0 elements, and the tile was never split.
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
def _overpass(q, label="", deadline=None, client_timeout=120):
    """POST an Overpass query, trying each mirror. Returns parsed JSON or None.
    client_timeout must exceed the query's own [timeout:] so a slow-but-succeeding
    dense query isn't killed mid-download by the client."""
    for i, ep in enumerate(_OVERPASS_EPS):
        if deadline and time.time() > deadline:
            return None                      # out of time; don't start another call
        try:
            req = urllib.request.Request(ep, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=client_timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            # Overpass answers HTTP 200 with an EMPTY elements list and a "remark"
            # when the query times out or errors server-side. Treating that as a
            # success is what left box-shaped holes (Montana, Nevada, Portugal,
            # Spain...) -- the tile was recorded "ok, 0 sites" and never split.
            rm = str((data or {}).get("remark") or "")
            if rm and ("timed out" in rm.lower() or "error" in rm.lower()
                       or "out of memory" in rm.lower()):
                if i == len(_OVERPASS_EPS) - 1:
                    print("  osm %s server-side timeout -> will split: %s" % (label, rm[:60]))
                time.sleep(1.0)
                continue                      # next mirror; then the caller splits
            return data
        except Exception as ex:
            if i == len(_OVERPASS_EPS) - 1:
                print("  osm %s failed on all mirrors: %s" % (label, str(ex)[:50]))
            time.sleep(1.0)
    return None

def _quarters(s, w, n, e):
    ms, mw = (s + n) / 2.0, (w + e) / 2.0
    return [(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]

def _osm_fetch_box(s, w, n, e, cap, label, out, deadline, depth=0, max_depth=5):
    """Fetch one tile; on a server-side timeout, recursively split into quarters
    (up to max_depth extra levels: 5deg -> 2.5 -> 1.25 -> 0.625 -> 0.3125deg) so the
    DENSEST regions (Randstad, Ruhr, Tokyo, Seoul, coastal China) actually fill in
    instead of leaving empty boxes. Earlier this stopped at 1.25deg, which still
    timed out over the biggest metros and left permanent grid holes there.
    Returns True if any data landed for this box."""
    if deadline and time.time() > deadline:
        return False
    qt = 90 if depth < 2 else 150            # deep (dense) sub-tiles get more server time
    data = _overpass(_osm_query(s, w, n, e, cap, qt=qt), label, deadline=deadline,
                     client_timeout=qt + 45)
    if data is not None:
        _osm_collect(data, label, out)
        return True
    # server timeout: subdivide if we still have depth budget and wall-clock time
    if depth >= max_depth or (deadline and time.time() > deadline - 60):
        return False
    got = False
    for (qs, qw, qn, qe) in _quarters(s, w, n, e):
        if _osm_fetch_box(qs, qw, qn, qe, cap, label + "/q", out, deadline, depth + 1, max_depth):
            got = True
        time.sleep(0.6)
    return got

def _osm_query(s, w, n, e, cap, qt=90):
    bb = "%s,%s,%s,%s" % (s, w, n, e)
    body = ('way["landuse"="construction"](%s)(if:length()>400);'
            'way["highway"="construction"](%s)(if:length()>800);'
            'way["railway"="construction"](%s)(if:length()>800);'
            'way["building"="construction"](%s)(if:length()>250);'
            'way["landuse"="quarry"]["construction"](%s);'
            'way["proposed:landuse"="quarry"](%s);'
            'way["man_made"="pipeline"]["construction"](%s);'
            'way["power"="plant"]["construction"](%s);'
            'way["waterway"="dam"]["construction"](%s);'
            'relation["landuse"="construction"](%s);'
            % (bb, bb, bb, bb, bb, bb, bb, bb, bb, bb))
    return '[out:json][timeout:%d];(%s);out geom %d;' % (qt, body, cap)

def _osm_measure(el, linear):
    """From out-geom geometry: (acres, miles). Area for closed features, length
    for linear ones. Equirectangular projection about mean latitude -- accurate
    enough for ranking. (None, None) if geometry is unusable."""
    g = el.get("geometry") or []
    pts = [(pt.get("lat"), pt.get("lon")) for pt in g
           if isinstance(pt, dict) and pt.get("lat") is not None and pt.get("lon") is not None]
    if len(pts) < 2:
        return (None, None)
    import math as _m
    mlat = sum(a for a, _ in pts) / len(pts)
    kx = 111320.0 * _m.cos(_m.radians(mlat)); ky = 110540.0
    if linear:
        dist = 0.0
        for i in range(1, len(pts)):
            dx = (pts[i][1] - pts[i-1][1]) * kx; dy = (pts[i][0] - pts[i-1][0]) * ky
            dist += _m.hypot(dx, dy)
        return (None, dist / 1609.344)
    a2 = 0.0
    for i in range(len(pts)):
        x1 = pts[i][1] * kx; y1 = pts[i][0] * ky
        j = (i + 1) % len(pts)
        x2 = pts[j][1] * kx; y2 = pts[j][0] * ky
        a2 += x1 * y2 - x2 * y1
    return (abs(a2) / 2.0 / 4046.8564224, None)

def _osm_center(el):
    b = el.get("bounds") or {}
    if b.get("minlat") is not None:
        return ((b["minlat"] + b["maxlat"]) / 2.0, (b["minlon"] + b["maxlon"]) / 2.0)
    g = el.get("geometry") or []
    if g and isinstance(g[0], dict) and g[0].get("lat") is not None:
        return (g[0]["lat"], g[0]["lon"])
    c = el.get("center") or {}
    return (c.get("lat", el.get("lat")), c.get("lon", el.get("lon")))

def _osm_collect(data, label, out):
    for el in (data.get("elements") or []):
        try:
            lat, lng = _osm_center(el)
            if lat is None or lng is None: continue
            tg = el.get("tags") or {}
            nm = tg.get("name") or tg.get("construction:name") or tg.get("operator") or ""
            linear = bool(tg.get("highway") or tg.get("railway") or tg.get("man_made") == "pipeline")
            kind = ("Road under construction" if tg.get("highway") else
                    "Railway under construction" if tg.get("railway") else
                    "Building under construction" if tg.get("building") else
                    "New quarry / extraction site" if (tg.get("landuse") == "quarry"
                        or tg.get("proposed:landuse") == "quarry") else
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
            acres, miles = _osm_measure(el, linear)
            if miles is not None and miles > 0:
                p["miles"] = round(miles, 2); p["size"] = "%.1f mi" % miles
            elif acres is not None and acres > 0:
                p["acres"] = round(acres, 1)
                p["size"] = ("%d ac" % round(acres)) if acres >= 1 else ("%.2f ac" % acres)
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
    # NB: no [:nslice] truncation -- that dropped the tail of each strided slice, so
    # the highest-index tiles in each residue class were never queried on any run.
    todo = [grid[i] for i in range(offset, len(grid), stride)]
    print("  osm: grid of %d tiles; this run does %d, strided every %d (offset %d) "
          "-> spread worldwide" % (len(grid), len(todo), stride, offset))
    out = _osm_existing()
    print("  osm: carried %d sites forward from previous runs" % len(out))
    ok_boxes = 0; timeouts = 0; skipped_time = 0
    budget_min = int(os.environ.get("OSM_BUDGET_MIN", "150"))
    t_end = time.time() + budget_min * 60
    print("  osm: wall-clock budget %d min -- will stop early and still save" % budget_min)
    for (s, w, n, e) in todo:
        if time.time() > t_end:
            skipped_time += 1
            continue
        label = "%.0f,%.0f" % (s, w)
        bb = "%s,%s,%s,%s" % (s, w, n, e)
        # widened tag set: sites, roads, rail, buildings, plus proposed/under-way
        # extraction and energy works -- the land-taking projects this map is about.
        # Overpass can measure features, so instead of an arbitrary cap we keep the
        # BIG ones: length() on a closed way is its perimeter (400m ~ 1 hectare),
        # on a road/rail it's the route length.
        # recursive subdivide-on-timeout (5deg -> 2.5deg -> 1.25deg) so dense
        # regions (Western Europe, East Asia) fill in instead of leaving boxes.
        if _osm_fetch_box(s, w, n, e, cap, label, out, t_end):
            ok_boxes += 1
        else:
            timeouts += 1
        time.sleep(1.2)   # Overpass fair-use pacing
    # dedup accumulated + new by rounded position
    seen = set(); merged = []
    for q in out:
        k = (round(q.get("lat", 0), 4), round(q.get("lng", 0), 4))
        if k in seen: continue
        seen.add(k); merged.append(q)
    print("  osm construction: %d sites total (%d/%d tiles ok, %d timed out, "
          "%d skipped for time -- next run rotates to them)"
          % (len(merged), ok_boxes, len(todo), timeouts, skipped_time))
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


# --- name-column picking that actually looks at the VALUES ---------------
# Matching only on column NAME lets a boolean flag column (e.g. an "EIA_Project"
# Y/N field) win the "project" pattern and become every row's title, which is how
# "Yes."/"No." project names got published. These helpers validate the contents.
_JUNK_NAME_VALS = {"", "yes", "no", "y", "n", "true", "false", "none", "n/a", "na",
                   "null", "nil", "unknown", "not applicable", "tbd", "-", "--",
                   "0", "1", "other", "misc", "various"}


_ADMIN_WORDS = {
    "file", "files", "reg", "regs", "register", "registration", "ref", "refs",
    "reference", "doc", "docs", "document", "documents", "form", "forms", "case",
    "cases", "folder", "sheet", "table", "column", "field", "fields", "row", "rows",
    "index", "key", "code", "codes", "serial", "batch", "lot", "seq", "sequence",
    "version", "revision", "draft", "copy", "page", "part", "section", "appendix",
    "attachment", "record", "records", "entry", "item", "number", "no", "num", "id",
    "detail", "details", "description", "status", "stage", "phase", "decision",
    "outcome", "result", "pending", "active", "current", "new", "old", "test",
    "sample", "default", "temp", "temporary", "placeholder", "blank", "empty",
    "project", "projects", "development", "site", "type", "other", "misc", "various",
}


def _looks_like_name(v):
    """True only if the value reads like a real title. Rejects flags ("Yes"/"No"),
    bare numbers, and admin/metadata scraps ("File Reg", "no. 2", "Doc 12")."""
    s = str(v if v is not None else "").strip().strip(".")
    if not s:
        return False
    if s.lower() in _JUNK_NAME_VALS:
        return False
    if len(s) < 4:
        return False
    if s.replace(".", "").replace("-", "").replace(" ", "").isdigit():
        return False
    if not re.search(r"[A-Za-z]{3,}", s):
        return False
    # needs at least one word that is not admin boilerplate and not a bare number
    for tok in re.split(r"[^A-Za-z0-9]+", s.lower()):
        if not tok or tok.isdigit():
            continue
        if tok not in _ADMIN_WORDS and len(tok) >= 3:
            return True
    return False


def _name_col_score(rows, c):
    vals = [r.get(c) for r in rows if isinstance(r, dict)]
    vals = [v for v in vals if str(v if v is not None else "").strip() != ""]
    if not vals:
        return 0.0, 0.0
    good = sum(1 for v in vals if _looks_like_name(v))
    avg = sum(len(str(v).strip()) for v in vals) / float(len(vals))
    return good / float(len(vals)), avg


def _sniff_name_col(cols, rows, *pats):
    """Pick a title column by pattern priority, but only if its values look like
    names. Falls back to the most title-like column present. Returns None rather
    than hand back a Y/N flag column."""
    rows = [r for r in (rows or []) if isinstance(r, dict)][:60]
    if not rows:
        return _sniff_col(cols, *pats)
    seen = []
    for pat in pats:
        for c in cols:
            if pat in str(c).lower() and c not in seen:
                seen.append(c)
    for c in seen:                      # honour pattern priority first
        sc, avg = _name_col_score(rows, c)
        if sc >= 0.6 and avg >= 5:
            return c
    best, best_key = None, None         # otherwise: best title-like column anywhere
    for c in cols:
        sc, avg = _name_col_score(rows, c)
        if sc >= 0.75 and avg >= 8:
            key = (sc, min(avg, 80))
            if best_key is None or key > best_key:
                best, best_key = c, key
    return best


def _clean_proj_name(nm, *fallbacks):
    """Never publish a junk value as a project title; build a readable label."""
    s = str(nm if nm is not None else "").strip()
    if _looks_like_name(s):
        return s[:140]
    bits = []
    for b in fallbacks:
        b = str(b if b is not None else "").strip()
        if b and b.lower() not in _JUNK_NAME_VALS and b not in bits:
            bits.append(b)
    return (" \u2014 ".join(bits[:2]))[:140] if bits else ""


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

def fetch_ibama_br(max_rows=12000):
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

def fetch_sitadel_fr(max_rows=40000, months=24):
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


def fetch_emodnet_wind():
    """EU/EEA seas: offshore wind farms IN DEVELOPMENT, via the EMODnet Human
    Activities WFS (ows.emodnet-humanactivities.eu, GeoServer; verified live
    2026-07: GetFeature + outputFormat=application/json is the documented
    pattern, EMODnet/Web-Service-Documentation on GitHub).
    Status vocabulary (per EMODnet metadata record 8201070b): Approved, Planned,
    Dismantled, Construction, Production, Test site (older releases: Authorised,
    Operational, Under Construction).
    IN-PROCESS GATE (fail-safe): keep ONLY planned/approved/authorised/
    construction; anything else -- production, operational, dismantled, test
    site, or unrecognized -- is dropped.
    The exact WFS typeName is not documented on a stable page, so candidates are
    probed; a miss is logged and the source returns 0 (per-source preservation
    then protects the previous harvest)."""
    base = ("https://ows.emodnet-humanactivities.eu/wfs?service=WFS&version=1.1.0"
            "&request=GetFeature&outputFormat=application/json&srsName=EPSG:4326"
            "&typeName=%s")
    ALLOW = ("planned", "approved", "authori", "construction")
    BLOCK = ("production", "operational", "dismantl", "decommission", "test")
    out = []
    for layer in ("windfarms", "windfarmspoly", "emodnet:windfarms", "emodnet:windfarmspoly"):
        try:
            data = _get_json(base % urllib.parse.quote(layer))
        except Exception as e:
            print("  emodnet layer %s failed: %s" % (layer, e)); continue
        feats = (data or {}).get("features") or []
        if not feats:
            print("  emodnet layer %s: no features" % layer); continue
        kept = 0
        for f in feats:
            try:
                pr = f.get("properties") or {}
                st = str(pr.get("status") or pr.get("STATUS") or "").strip().lower()
                if not st or any(b in st for b in BLOCK): continue
                if not any(a in st for a in ALLOW): continue     # unrecognized -> excluded
                g = f.get("geometry") or {}
                lat = lng = None
                if g.get("type") == "Point":
                    lng, lat = g["coordinates"][0], g["coordinates"][1]
                else:
                    ring = g.get("coordinates")
                    while ring and isinstance(ring[0], (list, tuple)) and isinstance(ring[0][0], (list, tuple)):
                        ring = ring[0]
                    if ring:
                        xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
                        lng = sum(xs) / len(xs); lat = sum(ys) / len(ys)
                if lat is None or lng is None: continue
                mwf = None
                try: mwf = float(pr.get("power_mw") or pr.get("POWER_MW"))
                except Exception: pass
                imp = 5 if (mwf and mwf >= 500) else 4 if (mwf and mwf >= 250) else 3 if (mwf and mwf >= 100) else 2
                nt = pr.get("n_turbines")
                out.append({
                    "name": str(pr.get("name") or "Offshore wind farm").strip()[:120],
                    "type": "Offshore wind farm", "status": st,
                    "country": str(pr.get("country") or "").strip(),
                    "state": (" \u00b7 " + str(pr.get("country")).strip()) if pr.get("country") else "",
                    "size": ("%d MW" % int(mwf)) if mwf else ((str(nt) + " turbines") if nt else ""),
                    "date": (str(pr.get("year") or pr.get("start") or "")[:10] or None),
                    "lat": lat, "lng": lng, "impact": imp,
                    "precise": g.get("type") == "Point",
                    "desc": "Offshore wind farm in development \u2014 EMODnet Human Activities (EU seas marine-spatial data).",
                    "url": "https://emodnet.ec.europa.eu/en/human-activities",
                    "source": "emodnet_wind"})
                kept += 1
            except Exception:
                continue
        print("  emodnet %s: %d features, %d in development kept" % (layer, len(feats), kept))
        if kept:
            break   # first working layer wins; the other geometry layer would duplicate it
    return out


# HELD (at-sea, documented 2026-07): ISA deep-sea mining exploration-contract
# areas -- the International Seabed Authority publishes contract-area geodata
# (31 exploration contracts: 19 polymetallic-nodule incl. CCZ, 7 sulphide-ridge,
# 5 cobalt-crust) through its DeepData GIS viewer (data.isa.org.jm/isa/map/),
# but no machine-readable endpoint could be live-verified; the viewer is a
# closed JS app and the shapefile catalogue sits behind it. Add the polygons
# if/when an open WFS/ArcGIS/download endpoint is confirmed. The ISA registry
# itself is linked on the map under International bodies.


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
    # additional national CKAN instances (all expose /api/3/action/package_search)
    ("https://data.gov.uk", "United Kingdom", "gb"),
    ("https://catalog.data.gov", "United States", "us"),
    ("https://open.canada.ca/data/en", "Canada", "ca"),
    ("https://datos.gob.ar", "Argentina", "ar"),
    ("https://datos.gob.mx/busca", "Mexico", "mx"),
    ("https://www.data.gv.at/katalog", "Austria", "at"),
    ("https://data.gov.ro", "Romania", "ro"),
    ("https://www.avoindata.fi/data/en", "Finland", "fi"),
    # Latin America + Eastern Europe national CKAN instances
    ("https://catalogodatos.gub.uy", "Uruguay", "uy"),
    ("https://www.datosabiertos.gob.pe", "Peru", "pe"),
    ("https://datosabiertos.gob.ec", "Ecuador", "ec"),
    ("https://www.datos.gov.py", "Paraguay", "py"),
    ("https://data.gov.hr", "Croatia", "hr"),
    ("https://data.gov.ua", "Ukraine", "ua"),
    ("https://date.gov.md", "Moldova", "md"),
    # SE Asia + more of the Balkans/Baltics
    ("https://data.go.th", "Thailand", "th"),
    ("https://data.gov.rs", "Serbia", "rs"),
    ("https://data.gov.lv", "Latvia", "lv"),
    ("https://podatki.gov.si", "Slovenia", "si"),
    ("https://data.gov.tn", "Tunisia", "tn"),
    ("https://data.gov.cy", "Cyprus", "cy"),
    ("https://data.gov.mt", "Malta", "mt"),
    ("https://data.public.lu", "Luxembourg", "lu"),
    # more verified-CKAN portals (national + a subnational + a pan-African aggregator)
    ("https://www.data.go.jp/data", "Japan", "jp"),
    ("https://data.sa.gov.au/data", "South Australia", "au"),
    ("https://africaopendata.org", "openAFRICA", "af"),
    ("https://data.gov.il", "Israel", "il"),
    ("https://datos.gob.do", "Dominican Republic", "do"),
    ("https://portal.opendata.dk", "Denmark", "dk"),
    # registry-verified (commondataio) national aggregators -- new countries/territories
    ("https://datos.gob.bo", "Bolivia", "bo"),
    ("https://opendata.government.bg", "Bulgaria", "bg"),
    ("https://data.gov.et", "Ethiopia", "et"),
    ("https://datosabiertos.gob.hn", "Honduras", "hn"),
    ("https://data.gov.kg", "Kyrgyzstan", "kg"),
    ("https://opendata.gov.mn", "Mongolia", "mn"),
    ("https://www.data.gov.ma", "Morocco", "ma"),
    ("https://datosabiertos.gob.pa", "Panama", "pa"),
    ("https://data.gov.mk", "North Macedonia", "mk"),
    ("https://data.gov.sk", "Slovakia", "sk"),
    ("https://www.data.gov.so", "Somalia", "so"),
    ("https://data.gov.tt", "Trinidad and Tobago", "tt"),
    ("https://opendata.gov.je", "Jersey", "je"),
    # thematic ministry portals most likely to hold geo project/EIA data
    ("https://datos.ambiente.gob.ar", "Argentina \u2014 environment", "ar"),
    ("https://datos.energia.gob.ar", "Argentina \u2014 energy", "ar"),
    ("https://dados.ana.gov.br", "Brazil \u2014 water/ANA", "br"),
    ("https://dados.infraestrutura.gov.br", "Brazil \u2014 infrastructure", "br"),
    ("https://data.forest.go.th", "Thailand \u2014 forestry", "th"),
    ("https://energydata.info", "Global energy (ESMAP/WB)", "xx"),
    # more registry-verified government thematic portals (project/infra/forestry)
    ("https://datos.minem.gob.ar", "Argentina \u2014 energy/mining", "ar"),
    ("https://datos.transporte.gob.ar", "Argentina \u2014 transport", "ar"),
    ("https://dados.florestal.gov.br", "Brazil \u2014 forest service", "br"),
    ("https://dados.transportes.gov.br", "Brazil \u2014 transport", "br"),
    ("https://www.juntadeandalucia.es/datosabiertos/portal", "Spain \u2014 Andalusia", "es"),
    ("https://gdcatalog.energy.go.th", "Thailand \u2014 energy", "th"),
    ("https://dataportal.drr.go.th", "Thailand \u2014 rural roads", "th"),
    ("https://dataportal.gov.tc", "Turks and Caicos", "tc"),
    ("https://dados.mma.gov.br", "Brazil \u2014 environment ministry", "br"),
    ("https://datosretc.mma.gob.cl", "Chile \u2014 environment/RETC", "cl"),
    ("https://opendata.housing.gov.ie", "Ireland \u2014 housing", "ie"),
    # --- international subnational (state/province/regional) government CKAN portals,
    # mined from commondataio/dataportals-registry (owner.type=Regional government, non-US,
    # audit/health/legislative/stats hosts excluded). Budget-protected; FIRST-RUN REVIEW. ---
    ("http://datos.neuquen.gob.ar", "Argentina", "ar"),
    ("https://catalogo.datos.gba.gob.ar", "Argentina", "ar"),
    ("https://datos.acumar.gov.ar", "Argentina", "ar"),
    ("https://datos.entrerios.gov.ar", "Argentina", "ar"),
    ("https://datos.santafe.gob.ar", "Argentina", "ar"),
    ("https://datosabiertos.mendoza.gov.ar", "Argentina", "ar"),
    ("https://sep.tucuman.gob.ar", "Argentina", "ar"),
    ("https://www.datos.misiones.gob.ar", "Argentina", "ar"),
    ("https://catalogue.data.wa.gov.au", "Australia", "au"),
    ("https://data.cese.nsw.gov.au", "Australia", "au"),
    ("https://data.nsw.gov.au/data", "Australia", "au"),
    ("https://data.nt.gov.au", "Australia", "au"),
    ("https://data.qld.gov.au", "Australia", "au"),
    ("https://data.vic.gov.au", "Australia", "au"),
    ("https://datasets.seed.nsw.gov.au", "Australia", "au"),
    ("https://discover.data.vic.gov.au", "Australia", "au"),
    ("https://geoscience.data.qld.gov.au", "Australia", "au"),
    ("https://opendata.transport.nsw.gov.au", "Australia", "au"),
    ("http://catalogo.governoaberto.sp.gov.br", "Brazil", "br"),
    ("https://dados.ac.gov.br", "Brazil", "br"),
    ("https://dados.al.gov.br/catalogo", "Brazil", "br"),
    ("https://dados.ba.gov.br", "Brazil", "br"),
    ("https://dados.es.gov.br", "Brazil", "br"),
    ("https://dados.mg.gov.br", "Brazil", "br"),
    ("https://dados.pe.gov.br", "Brazil", "br"),
    ("https://dados.rs.gov.br", "Brazil", "br"),
    ("https://dados.sc.gov.br", "Brazil", "br"),
    ("https://dadosabertos.ba.gov.br", "Brazil", "br"),
    ("https://dadosabertos.go.gov.br", "Brazil", "br"),
    ("https://dadosabertos.sp.gov.br", "Brazil", "br"),
    ("https://data.se.df.gov.br", "Brazil", "br"),
    ("https://portal.dados.al.gov.br", "Brazil", "br"),
    ("https://ppn.sc.gov.br", "Brazil", "br"),
    ("https://catalogue.data.gov.bc.ca", "Canada", "ca"),
    ("https://opendata.gov.nt.ca", "Canada", "ca"),
    ("https://datosabiertos.bogota.gov.co", "Colombia", "co"),
    ("https://datosabiertos.valledelcauca.gov.co", "Colombia", "co"),
    ("https://datosabiertos.carchi.gob.ec", "Ecuador", "ec"),
    ("https://sil.eloro.gob.ec", "Ecuador", "ec"),
    ("https://admin.opendatani.gov.uk", "United Kingdom", "gb"),
    ("https://ckan.diskominfo.sultengprov.go.id", "Indonesia", "id"),
    ("https://ckan.jombangkab.go.id", "Indonesia", "id"),
    ("https://data.acehbaratkab.go.id", "Indonesia", "id"),
    ("https://data.acehselatankab.go.id", "Indonesia", "id"),
    ("https://data.bengkuluutarakab.go.id", "Indonesia", "id"),
    ("https://data.halmaherautarakab.go.id", "Indonesia", "id"),
    ("https://data.inhilkab.go.id", "Indonesia", "id"),
    ("https://data.jatengprov.go.id", "Indonesia", "id"),
    ("https://data.kalbarprov.go.id", "Indonesia", "id"),
    ("https://data.kaltaraprov.go.id", "Indonesia", "id"),
    ("https://data.kaltimprov.go.id", "Indonesia", "id"),
    ("https://data.kutaitimurkab.go.id", "Indonesia", "id"),
    ("https://data.papuabaratprov.go.id", "Indonesia", "id"),
    ("https://data.pekalongankab.go.id", "Indonesia", "id"),
    ("https://data.purbalinggakab.go.id", "Indonesia", "id"),
    ("https://data.sumbarprov.go.id", "Indonesia", "id"),
    ("https://opendata.bogorkab.go.id", "Indonesia", "id"),
    ("https://opendata.kebumenkab.go.id", "Indonesia", "id"),
    ("https://satudataindonesia.malukuprov.go.id", "Indonesia", "id"),
    ("http://datos.mpiochih.gob.mx", "Mexico", "mx"),
    ("https://datos.qroo.gob.mx", "Mexico", "mx"),
    ("https://datos.slp.gob.mx", "Mexico", "mx"),
    ("https://datos15-21.slp.gob.mx", "Mexico", "mx"),
    ("https://data.sarawak.gov.my", "Malaysia", "my"),
    ("http://sbsopendata.ekitistate.gov.ng", "Nigeria", "ng"),
    ("https://opendata.kp.gov.pk", "Pakistan", "pk"),
    ("https://opendata.azores.gov.pt", "Portugal", "pt"),
    ("https://data.bangkok.go.th", "Thailand", "th"),
    ("http://ckan.tycg.gov.tw", "Taiwan", "tw"),
    ("https://data.kcg.gov.tw", "Taiwan", "tw"),
    ("https://data.nantou.gov.tw", "Taiwan", "tw"),
    ("https://data.tainan.gov.tw", "Taiwan", "tw"),
    ("https://opendata.e-land.gov.tw", "Taiwan", "tw"),
    ("https://opendata.penghu.gov.tw", "Taiwan", "tw"),
    ("https://opendata.taichung.gov.tw", "Taiwan", "tw"),
    ("https://data.carpathia.gov.ua", "Ukraine", "ua"),
    ("https://data.loda.gov.ua", "Ukraine", "ua"),
    ("https://opendata.vin.gov.ua", "Ukraine", "ua"),
    ("http://csdlcntmgialai.gov.vn", "Vietnam", "vn"),
    ("https://data.haugiang.gov.vn", "Vietnam", "vn"),
    ("https://data.longan.gov.vn", "Vietnam", "vn"),
    ("https://data.ninhbinh.gov.vn", "Vietnam", "vn"),
    ("https://opendata.quangngai.gov.vn", "Vietnam", "vn"),
    ("https://opendata.thanhhoa.gov.vn", "Vietnam", "vn"),
    ("https://opendata.vinhlong.gov.vn", "Vietnam", "vn"),
    # --- major-city municipal government CKAN portals (non-US, big/capital cities;
    #     lowest priority so budget cuts these first). FIRST-RUN REVIEW. ---
    ("http://concejoabierto.cdcordoba.gob.ar", "Argentina", "ar"),
    ("http://datos.concejorosario.gov.ar", "Argentina", "ar"),
    ("https://data.buenosaires.gob.ar", "Argentina", "ar"),
    ("https://dados.fortaleza.ce.gov.br", "Brazil", "br"),
    ("https://dados.portoalegre.rs.gov.br", "Brazil", "br"),
    ("https://dados.recife.pe.gov.br", "Brazil", "br"),
    ("https://datos.cali.gov.co", "Colombia", "co"),
    ("https://gobiernoabierto.quito.gob.ec/catalogo-datos-abiertos", "Ecuador", "ec"),
    ("https://data.glasgow.gov.uk", "United Kingdom", "gb"),
    ("https://data.london.gov.uk", "United Kingdom", "gb"),
    ("https://www.cityobservatory.birmingham.gov.uk", "United Kingdom", "gb"),
    ("http://opendata.semarangkota.go.id", "Indonesia", "id"),
    ("https://danta-admin.bekasikota.go.id", "Indonesia", "id"),
    ("https://data.bandung.go.id", "Indonesia", "id"),
    ("https://data.pemalangkab.go.id", "Indonesia", "id"),
    ("https://data.tangerangselatankota.go.id", "Indonesia", "id"),
    ("https://opendata.surabaya.go.id", "Indonesia", "id"),
    ("https://satudata.palembang.go.id", "Indonesia", "id"),
    ("https://datos.cdmx.gob.mx", "Mexico", "mx"),
    ("https://datos.monterrey.gob.mx", "Mexico", "mx"),
    # -- widest-net sweep: remaining international gov CKAN (all tiers) --
    ("https://opendata.fcsc.gov.ae", "United Arab Emirates", "ae"),
    ("https://opendata.moei.gov.ae/ckan", "United Arab Emirates", "ae"),
    ("http://datos.crespo.gob.ar", "Argentina", "ar"),
    ("http://datos.legislatura.gob.ar", "Argentina", "ar"),
    ("http://datos.mindef.gov.ar", "Argentina", "ar"),
    ("http://datos.quilmes.gov.ar", "Argentina", "ar"),
    ("http://datos.salud.gob.ar", "Argentina", "ar"),
    ("http://datos.tandil.gov.ar", "Argentina", "ar"),
    ("http://datos.yerbabuena.gob.ar", "Argentina", "ar"),
    ("http://datosabiertos.desarrollosocial.gob.ar", "Argentina", "ar"),
    ("http://datosabiertos.hcdmza.gob.ar", "Argentina", "ar"),
    ("http://datosabiertos.mercedes.gob.ar", "Argentina", "ar"),
    ("http://datosabiertos.pergamino.gob.ar", "Argentina", "ar"),
    ("http://datosabiertos.rafaela.gob.ar", "Argentina", "ar"),
    ("http://goy-cte-datos.paisdigital.modernizacion.gob.ar", "Argentina", "ar"),
    ("http://lin-bue-datos.paisdigital.modernizacion.gob.ar", "Argentina", "ar"),
    ("http://luj-bue-datos.paisdigital.innovacion.gob.ar", "Argentina", "ar"),
    ("http://mc.consejomagistratura.gob.ar", "Argentina", "ar"),
    ("http://per-bue-datos.paisdigital.modernizacion.gob.ar", "Argentina", "ar"),
    ("https://ckan.ciudaddemendoza.gob.ar", "Argentina", "ar"),
    ("https://datasets.datos.mincyt.gob.ar", "Argentina", "ar"),
    ("https://datos.acumar.gob.ar", "Argentina", "ar"),
    ("https://datos.bahia.gob.ar", "Argentina", "ar"),
    ("https://datos.ciudaddecorrientes.gov.ar", "Argentina", "ar"),
    ("https://datos.ciudaddemendoza.gob.ar", "Argentina", "ar"),
    ("https://datos.ciudaddemendoza.gov.ar", "Argentina", "ar"),
    ("https://datos.csjn.gov.ar", "Argentina", "ar"),
    ("https://datos.cultura.gob.ar", "Argentina", "ar"),
    ("https://datos.estadistica.ec.gba.gov.ar", "Argentina", "ar"),
    ("https://datos.jesusmaria.gov.ar", "Argentina", "ar"),
    ("https://datos.jus.gob.ar", "Argentina", "ar"),
    ("https://datos.magyp.gob.ar", "Argentina", "ar"),
    ("https://datos.mininterior.gob.ar", "Argentina", "ar"),
    ("https://datos.tsjbaires.gov.ar", "Argentina", "ar"),
    ("https://datos.villamaria.gob.ar", "Argentina", "ar"),
    ("https://datos.vivamoscomodoro.gob.ar", "Argentina", "ar"),
    ("https://datos.yvera.gob.ar", "Argentina", "ar"),
    ("https://datosabiertos.gualeguaychu.gov.ar", "Argentina", "ar"),
    ("https://datosabiertos.municipiosanjuan.gob.ar", "Argentina", "ar"),
    ("https://datosabiertos.sanjuan.gob.ar", "Argentina", "ar"),
    ("https://datosestadistica.cba.gov.ar", "Argentina", "ar"),
    ("https://datosgestionabierta.cba.gov.ar", "Argentina", "ar"),
    ("https://transparencia.enargas.gob.ar", "Argentina", "ar"),
    ("https://data.brisbane.qld.gov.au", "Australia", "au"),
    ("https://data.datahub.freightaustralia.gov.au", "Australia", "au"),
    ("https://flooddata.ses.nsw.gov.au", "Australia", "au"),
    ("https://publications.qld.gov.au", "Australia", "au"),
    ("http://app.podaci.gov.ba", "Bosnia and Herzegovina", "ba"),
    ("https://catalogodedados.serpro.gov.br", "Brazil", "br"),
    ("https://ckan.jbrj.gov.br", "Brazil", "br"),
    ("https://ckan.pbh.gov.br", "Brazil", "br"),
    ("https://dados.agricultura.gov.br", "Brazil", "br"),
    ("https://dados.antt.gov.br", "Brazil", "br"),
    ("https://dados.ciga.sc.gov.br", "Brazil", "br"),
    ("https://dados.cultura.gov.br", "Brazil", "br"),
    ("https://dados.cvm.gov.br", "Brazil", "br"),
    ("https://dados.mda.gov.br", "Brazil", "br"),
    ("https://dados.mj.gov.br", "Brazil", "br"),
    ("https://dados.mogidascruzes.sp.gov.br", "Brazil", "br"),
    ("https://dados.ouropreto.mg.gov.br", "Brazil", "br"),
    ("https://dados.pbh.gov.br", "Brazil", "br"),
    ("https://dados.prefeitura.sp.gov.br", "Brazil", "br"),
    ("https://dados.tce.rs.gov.br", "Brazil", "br"),
    ("https://dadosabertos.aneel.gov.br", "Brazil", "br"),
    ("https://dadosabertos.bcb.gov.br", "Brazil", "br"),
    ("https://dadosabertos.bndes.gov.br", "Brazil", "br"),
    ("https://dadosabertos.capes.gov.br", "Brazil", "br"),
    ("https://dadosabertos.inss.gov.br", "Brazil", "br"),
    ("https://dadosabertos.presidencia.gov.br", "Brazil", "br"),
    ("https://dadosabertos.senado.gov.br", "Brazil", "br"),
    ("https://dadosabertos.tce.go.gov.br", "Brazil", "br"),
    ("https://inforepositorio.se.df.gov.br", "Brazil", "br"),
    ("https://opendata.bcb.gov.br", "Brazil", "br"),
    ("https://opendatasus.saude.gov.br", "Brazil", "br"),
    ("https://orcamentoaberto.prefeitura.sp.gov.br", "Brazil", "br"),
    ("https://reds.ses.pb.gov.br", "Brazil", "br"),
    ("https://web.transparencia.pe.gov.br", "Brazil", "br"),
    ("https://www.tesourotransparente.gov.br", "Brazil", "br"),
    ("https://www.transparencia.mg.gov.br", "Brazil", "br"),
    ("https://datos.odepa.gob.cl", "Chile", "cl"),
    ("https://ider-catalogo.sdp.gov.co", "Colombia", "co"),
    ("https://medata.gov.co", "Colombia", "co"),
    ("https://www.postdata.gov.co", "Colombia", "co"),
    ("https://datosabiertos.muniguarco.go.cr", "Costa Rica", "cr"),
    ("https://datosabiertos.santaana.go.cr", "Costa Rica", "cr"),
    ("https://ckan.issi.gov.cz", "Czech Republic", "cz"),
    ("https://datasets.catalogue.data.gov.dk", "Denmark", "dk"),
    ("http://sil.loja.gob.ec", "Ecuador", "ec"),
    ("https://catalogo.datosabiertos.gob.ec", "Ecuador", "ec"),
    ("https://cuencaendatos.cuenca.gob.ec", "Ecuador", "ec"),
    ("http://data.ata.gov.et", "Ethiopia", "et"),
    ("https://data.moa.gov.et", "Ethiopia", "et"),
    ("https://data.aberdeencity.gov.uk", "United Kingdom", "gb"),
    ("https://data.barrowbc.gov.uk", "United Kingdom", "gb"),
    ("https://data.dundeecity.gov.uk", "United Kingdom", "gb"),
    ("https://data.hounslow.gov.uk", "United Kingdom", "gb"),
    ("https://data.pkc.gov.uk", "United Kingdom", "gb"),
    ("https://dataworks.calderdale.gov.uk", "United Kingdom", "gb"),
    ("https://opendata.angus.gov.uk", "United Kingdom", "gb"),
    ("https://opendata.hullcc.gov.uk", "United Kingdom", "gb"),
    ("https://publications.aberdeenshire.gov.uk", "United Kingdom", "gb"),
    ("https://www.opendatani.gov.uk", "United Kingdom", "gb"),
    ("http://data.nap.gov.gr", "Greece", "gr"),
    ("https://catalog.data.gov.gr", "Greece", "gr"),
    ("https://data.kavala.gov.gr", "Greece", "gr"),
    ("https://diavgeia.gov.gr", "Greece", "gr"),
    ("https://geodata.gov.gr", "Greece", "gr"),
    ("https://opendata.agrinio.gov.gr", "Greece", "gr"),
    ("https://repository.data.gov.gr", "Greece", "gr"),
    ("http://catalogo.datos.gob.gt", "Guatemala", "gt"),
    ("http://datos.conred.gob.gt", "Guatemala", "gt"),
    ("https://catalogo.senacyt.gob.gt:80", "Guatemala", "gt"),
    ("https://datos.minfin.gob.gt", "Guatemala", "gt"),
    ("https://datos.segeplan.gob.gt", "Guatemala", "gt"),
    ("https://datosabiertos.mineduc.gob.gt", "Guatemala", "gt"),
    ("https://datosabiertos.mspas.gob.gt", "Guatemala", "gt"),
    ("https://data.gov.hk/en-data", "Hong Kong", "hk"),
    ("https://ckan.perpusnas.go.id", "Indonesia", "id"),
    ("https://data.acehjayakab.go.id", "Indonesia", "id"),
    ("https://data.balikpapan.go.id", "Indonesia", "id"),
    ("https://data.bandaacehkota.go.id", "Indonesia", "id"),
    ("https://data.bangkalankab.go.id", "Indonesia", "id"),
    ("https://data.bantulkab.go.id", "Indonesia", "id"),
    ("https://data.baritotimurkab.go.id", "Indonesia", "id"),
    ("https://data.batangkab.go.id", "Indonesia", "id"),
    ("https://data.batubarakab.go.id", "Indonesia", "id"),
    ("https://data.belitung.go.id", "Indonesia", "id"),
    ("https://data.beltim.go.id", "Indonesia", "id"),
    ("https://data.bnpb.go.id", "Indonesia", "id"),
    ("https://data.bontangkota.go.id", "Indonesia", "id"),
    ("https://data.cilacapkab.go.id", "Indonesia", "id"),
    ("https://data.dairikab.go.id", "Indonesia", "id"),
    ("https://data.deliserdangkab.go.id", "Indonesia", "id"),
    ("https://data.demakkab.go.id", "Indonesia", "id"),
    ("https://data.gayolueskab.go.id", "Indonesia", "id"),
    ("https://data.gresikkab.go.id", "Indonesia", "id"),
    ("https://data.grobogan.go.id", "Indonesia", "id"),
    ("https://data.kaboki.go.id", "Indonesia", "id"),
    ("https://data.kamparkab.go.id", "Indonesia", "id"),
    ("https://data.kendalkab.go.id", "Indonesia", "id"),
    ("https://data.lamongankab.go.id", "Indonesia", "id"),
    ("https://data.linggakab.go.id", "Indonesia", "id"),
    ("https://data.magelangkota.go.id", "Indonesia", "id"),
    ("https://data.mahakamulukab.go.id", "Indonesia", "id"),
    ("https://data.mempawahkab.go.id", "Indonesia", "id"),
    ("https://data.pasamanbaratkab.go.id", "Indonesia", "id"),
    ("https://data.pasamankab.go.id", "Indonesia", "id"),
    ("https://data.pekalongankota.go.id", "Indonesia", "id"),
    ("https://data.pesisirselatankab.go.id", "Indonesia", "id"),
    ("https://data.pidiejayakab.go.id", "Indonesia", "id"),
    ("https://data.pidiekab.go.id", "Indonesia", "id"),
    ("https://data.pontianakkota.go.id", "Indonesia", "id"),
    ("https://data.pu.go.id", "Indonesia", "id"),
    ("https://data.purworejokab.go.id", "Indonesia", "id"),
    ("https://data.rsud.tulungagung.go.id", "Indonesia", "id"),
    ("https://data.sintang.go.id", "Indonesia", "id"),
    ("https://data.sumbawabaratkab.go.id", "Indonesia", "id"),
    ("https://data.tanjabbarkab.go.id", "Indonesia", "id"),
    ("https://data.tegalkab.go.id", "Indonesia", "id"),
    ("https://data.wonogirikab.go.id", "Indonesia", "id"),
    ("https://datasets.kaurkab.go.id", "Indonesia", "id"),
    ("https://datasets.palukota.go.id", "Indonesia", "id"),
    ("https://disada.lebakkab.go.id", "Indonesia", "id"),
    ("https://katalog.data.go.id", "Indonesia", "id"),
    ("https://katalog.data.gorontalokota.go.id", "Indonesia", "id"),
    ("https://katalogdata.cilegon.go.id", "Indonesia", "id"),
    ("https://mydata.sijunjung.go.id", "Indonesia", "id"),
    ("https://opendata.blitarkab.go.id", "Indonesia", "id"),
    ("https://opendata.bovendigoelkab.go.id", "Indonesia", "id"),
    ("https://opendata.brebeskab.go.id", "Indonesia", "id"),
    ("https://opendata.bulukumbakab.go.id", "Indonesia", "id"),
    ("https://opendata.malinau.go.id", "Indonesia", "id"),
    ("https://opendata.pacitankab.go.id", "Indonesia", "id"),
    ("https://opendata.pandeglangkab.go.id", "Indonesia", "id"),
    ("https://opendata.rsadhyatma.jatengprov.go.id", "Indonesia", "id"),
    ("https://opendata.samarindakota.go.id", "Indonesia", "id"),
    ("https://opendata.sidoarjokab.go.id", "Indonesia", "id"),
    ("https://opendata.solselkab.go.id", "Indonesia", "id"),
    ("https://portaldata.batukota.go.id", "Indonesia", "id"),
    ("https://portalsatudata.simalungunkab.go.id", "Indonesia", "id"),
    ("https://saritamura.murungrayakab.go.id", "Indonesia", "id"),
    ("https://satudata.dharmasrayakab.go.id", "Indonesia", "id"),
    ("https://satudata.dpd.go.id", "Indonesia", "id"),
    ("https://satudata.kapuaskab.go.id", "Indonesia", "id"),
    ("https://satudata.kayongutarakab.go.id", "Indonesia", "id"),
    ("https://satudata.landakkab.go.id", "Indonesia", "id"),
    ("https://satudata.langkatkab.go.id", "Indonesia", "id"),
    ("https://satudata.mempawahkab.go.id", "Indonesia", "id"),
    ("https://satudata.padang.go.id", "Indonesia", "id"),
    ("https://satudata.palikab.go.id", "Indonesia", "id"),
    ("https://satudata.probolinggokab.go.id", "Indonesia", "id"),
    ("https://satudata.solokkab.go.id", "Indonesia", "id"),
    ("https://satudata.sumbawakab.go.id", "Indonesia", "id"),
    ("https://satudata.tojounauna.go.id", "Indonesia", "id"),
    ("https://satudatapalapa.mojokertokab.go.id", "Indonesia", "id"),
    ("https://sdi.katingankab.go.id", "Indonesia", "id"),
    ("https://sdi.niasutarakab.go.id", "Indonesia", "id"),
    ("https://sdi.palukota.go.id", "Indonesia", "id"),
    ("https://sdi.selumakab.go.id", "Indonesia", "id"),
    ("https://sisada.pematangsiantar.go.id", "Indonesia", "id"),
    ("https://statistik.ponorogo.go.id", "Indonesia", "id"),
    ("https://data.nbco.gov.ie", "Ireland", "ie"),
    ("https://datacatalogue.gov.ie", "Ireland", "ie"),
    ("https://opendata.agriculture.gov.ie", "Ireland", "ie"),
    ("https://catalog.data.gov.ir", "Iran", "ir"),
    ("https://dati.mit.gov.it/catalog", "Italy", "it"),
    ("https://indicepa.gov.it/ipa-dati", "Italy", "it"),
    ("https://opendata-ercolano.cultura.gov.it", "Italy", "it"),
    ("https://data.e-gov.go.jp/data", "Japan", "jp"),
    ("https://data.env.go.jp", "Japan", "jp"),
    ("http://data.nsdi.go.kr", "South Korea", "kr"),
    ("https://pilot.data.gov.la", "Laos", "la"),
    ("https://dataset.gov.md", "Moldova", "md"),
    ("https://edu.mrpam.gov.mn", "Mongolia", "mn"),
    ("https://dms.hiv.health.gov.mw", "Malawi", "mw"),
    ("https://datos.congresogto.gob.mx", "Mexico", "mx"),
    ("https://datos.veracruzmunicipio.gob.mx", "Mexico", "mx"),
    ("https://www2.imss.gob.mx", "Mexico", "mx"),
    ("https://archive.data.gov.my", "Malaysia", "my"),
    ("https://data.birgunjmun.gov.np", "Nepal", "np"),
    ("https://data.lekbeshimun.gov.np", "Nepal", "np"),
    ("https://data.nsonepal.gov.np", "Nepal", "np"),
    ("https://data.tulsipurmun.gov.np", "Nepal", "np"),
    ("https://geodata.nzpam.govt.nz", "New Zealand", "nz"),
    ("https://datos.ins.gob.pe", "Peru", "pe"),
    ("https://datosabiertos.mef.gob.pe", "Peru", "pe"),
    ("https://dados.justica.gov.pt", "Portugal", "pt"),
    ("http://data.sepa.gov.rs", "Serbia", "rs"),
    ("http://opendata.city.tambov.gov.ru", "Russian Federation", "ru"),
    ("https://od.data.gov.sa", "Saudi Arabia", "sa"),
    ("http://alienfivejulyfile.doe.go.th", "Thailand", "th"),
    ("http://ckan.dwr.go.th", "Thailand", "th"),
    ("http://ckan.vec.go.th", "Thailand", "th"),
    ("http://data.ieat.go.th", "Thailand", "th"),
    ("http://opendata.alro.go.th", "Thailand", "th"),
    ("https://cadckan.cad.go.th", "Thailand", "th"),
    ("https://catalog-acfs.data.go.th", "Thailand", "th"),
    ("https://catalog-cpd.data.go.th", "Thailand", "th"),
    ("https://catalog-dga.data.go.th", "Thailand", "th"),
    ("https://catalog.customs.go.th", "Thailand", "th"),
    ("https://catalog.dip.go.th", "Thailand", "th"),
    ("https://catalog.dmf.go.th", "Thailand", "th"),
    ("https://catalog.dmh.go.th", "Thailand", "th"),
    ("https://catalog.dnp.go.th", "Thailand", "th"),
    ("https://catalog.doe.go.th", "Thailand", "th"),
    ("https://catalog.dopa.go.th", "Thailand", "th"),
    ("https://catalog.dpim.go.th", "Thailand", "th"),
    ("https://catalog.excise.go.th", "Thailand", "th"),
    ("https://catalog.fisheries.go.th", "Thailand", "th"),
    ("https://catalog.fpo.go.th", "Thailand", "th"),
    ("https://catalog.ipthailand.go.th", "Thailand", "th"),
    ("https://catalog.moe.go.th", "Thailand", "th"),
    ("https://catalog.mof.go.th", "Thailand", "th"),
    ("https://catalog.nso.go.th", "Thailand", "th"),
    ("https://catalog.ocsb.go.th", "Thailand", "th"),
    ("https://catalog.ocsc.go.th", "Thailand", "th"),
    ("https://catalog.qsds.go.th", "Thailand", "th"),
    ("https://catalog.rdpb.go.th", "Thailand", "th"),
    ("https://catalog.royalrain.go.th", "Thailand", "th"),
    ("https://catalog.sbpac.go.th", "Thailand", "th"),
    ("https://catalog.sepo.go.th", "Thailand", "th"),
    ("https://catalog.sso.go.th", "Thailand", "th"),
    ("https://catalog.tmd.go.th", "Thailand", "th"),
    ("https://catalog.travellink.go.th", "Thailand", "th"),
    ("https://ckan.dsi.go.th", "Thailand", "th"),
    ("https://ckan.mots.go.th", "Thailand", "th"),
    ("https://ckan.pdmo.go.th", "Thailand", "th"),
    ("https://data.dmr.go.th", "Thailand", "th"),
    ("https://data.dss.go.th", "Thailand", "th"),
    ("https://data.mhesi.go.th", "Thailand", "th"),
    ("https://data.onec.go.th", "Thailand", "th"),
    ("https://datacatalog.bde.go.th", "Thailand", "th"),
    ("https://datacatalog.dit.go.th", "Thailand", "th"),
    ("https://datacatalog.doa.go.th", "Thailand", "th"),
    ("https://datacatalog.moc.go.th", "Thailand", "th"),
    ("https://datacatalog.nbtc.go.th", "Thailand", "th"),
    ("https://datacatalog.onde.go.th", "Thailand", "th"),
    ("https://datacatalog.senate.go.th", "Thailand", "th"),
    ("https://datagov.mot.go.th", "Thailand", "th"),
    ("https://dataportal.opdc.go.th", "Thailand", "th"),
    ("https://demo.gdcatalog.go.th", "Thailand", "th"),
    ("https://gdcatalog.airports.go.th", "Thailand", "th"),
    ("https://gdcatalog.dlt.go.th", "Thailand", "th"),
    ("https://gdcatalog.go.th", "Thailand", "th"),
    ("https://gdcatalog.m-culture.go.th", "Thailand", "th"),
    ("https://hss.gdcatalog.go.th", "Thailand", "th"),
    ("https://itd.gdcatalog.go.th", "Thailand", "th"),
    ("https://lddcatalog.ldd.go.th", "Thailand", "th"),
    ("https://nabc-catalog.oae.go.th", "Thailand", "th"),
    ("https://ocpb.gdcatalog.go.th", "Thailand", "th"),
    ("https://onab.gdcatalog.go.th", "Thailand", "th"),
    ("https://onep.gdcatalog.go.th", "Thailand", "th"),
    ("https://opendata.cifs.go.th", "Thailand", "th"),
    ("https://opendata.dbd.go.th", "Thailand", "th"),
    ("https://opendata.dla.go.th", "Thailand", "th"),
    ("https://opendata.dmcr.go.th", "Thailand", "th"),
    ("https://opendata.dpe.go.th", "Thailand", "th"),
    ("https://opendata.dpo.go.th", "Thailand", "th"),
    ("https://opendata.dsd.go.th", "Thailand", "th"),
    ("https://opendata.led.go.th", "Thailand", "th"),
    ("https://opendata.nesdc.go.th", "Thailand", "th"),
    ("https://opendata.nrct.go.th", "Thailand", "th"),
    ("https://opendata.oae.go.th", "Thailand", "th"),
    ("https://opendata.obec.go.th", "Thailand", "th"),
    ("https://opendata.ocsb.go.th", "Thailand", "th"),
    ("https://opendata.onde.go.th", "Thailand", "th"),
    ("https://opendata.onwr.go.th", "Thailand", "th"),
    ("https://opendata.tisi.go.th", "Thailand", "th"),
    ("https://otp.gdcatalog.go.th", "Thailand", "th"),
    ("https://pcd.gdcatalog.go.th", "Thailand", "th"),
    ("https://pei.dede.go.th", "Thailand", "th"),
    ("https://phetchaburi.gdcatalog.go.th", "Thailand", "th"),
    ("https://prd.gdcatalog.go.th", "Thailand", "th"),
    ("https://roiet.gdcatalog.go.th", "Thailand", "th"),
    ("https://catalog.industrie.gov.tn", "Tunisia", "tn"),
    ("https://www.openculture.gov.tn", "Tunisia", "tn"),
    ("https://data.ibb.gov.tr", "Turkey", "tr"),
    ("https://ulasav.csb.gov.tr", "Turkey", "tr"),
    ("https://bmckan.cpami.gov.tw", "Taiwan", "tw"),
    ("https://data.cdc.gov.tw", "Taiwan", "tw"),
    ("https://dani.kolrada.gov.ua", "Ukraine", "ua"),
    ("https://data.dniprorada.gov.ua", "Ukraine", "ua"),
    ("https://data.imr.gov.ua", "Ukraine", "ua"),
    ("https://data.kr-rada.gov.ua", "Ukraine", "ua"),
    ("https://data.lutskrada.gov.ua", "Ukraine", "ua"),
    ("https://data.menarada.gov.ua", "Ukraine", "ua"),
    ("https://opendata.drohobych-rada.gov.ua", "Ukraine", "ua"),
    ("https://opendata.gov.ua", "Ukraine", "ua"),
    ("https://opendata.mlt.gov.ua", "Ukraine", "ua"),
    ("https://opendata.slavrada.gov.ua", "Ukraine", "ua"),
    ("https://opendata.slavuta-mvk.gov.ua", "Ukraine", "ua"),
    ("https://opendata.ternopilcity.gov.ua", "Ukraine", "ua"),
    ("https://open.data.gov.vn", "Vietnam", "vn"),
    ("https://opendata.monre.gov.vn", "Vietnam", "vn"),
    ("https://data.ocean.gov.za", "South Africa", "za"),
    ("https://data.vulekamali.gov.za", "South Africa", "za"),
    # -- widest-net sweep: remaining US gov CKAN (over-covered elsewhere; last) --
    ("http://opendata.fortsmithar.gov", "United States", "us"),
    ("https://catalog.data.faa.gov", "United States", "us"),
    ("https://data.birminghamal.gov", "United States", "us"),
    ("https://data.boston.gov", "United States", "us"),
    ("https://data.ca.gov", "United States", "us"),
    ("https://data.capitol.texas.gov", "United States", "us"),
    ("https://data.chhs.ca.gov", "United States", "us"),
    ("https://data.cnra.ca.gov", "United States", "us"),
    ("https://data.doi.gov", "United States", "us"),
    ("https://data.ed.gov", "United States", "us"),
    ("https://data.houstontx.gov", "United States", "us"),
    ("https://data.illinois.gov", "United States", "us"),
    ("https://data.milwaukee.gov", "United States", "us"),
    ("https://data.noaa.gov", "United States", "us"),
    ("https://data.ok.gov", "United States", "us"),
    ("https://data.pompanobeachfl.gov", "United States", "us"),
    ("https://data.sanantonio.gov", "United States", "us"),
    ("https://data.sanjoseca.gov/home", "United States", "us"),
    ("https://data.santamonica.gov", "United States", "us"),
    ("https://data.sba.gov", "United States", "us"),
    ("https://data.sugarlandtx.gov", "United States", "us"),
    ("https://data.tn.gov", "United States", "us"),
    ("https://data.treasury.ri.gov", "United States", "us"),
    ("https://datahub.cmap.illinois.gov", "United States", "us"),
    ("https://edx.netl.doe.gov", "United States", "us"),
    ("https://gisdata.mn.gov", "United States", "us"),
    ("https://hub.mph.in.gov", "United States", "us"),
    ("https://inventory.data.gov", "United States", "us"),
    ("https://ndotdata.nebraska.gov", "United States", "us"),
    ("https://open.jacksonms.gov", "United States", "us"),
    ("https://opendata.hawaii.gov", "United States", "us"),
    ("https://opendata.sbcountyatc.gov", "United States", "us"),
    ("https://opendata.tampa.gov", "United States", "us"),
    ("https://opendata.winchesterva.gov", "United States", "us"),
    ("https://opendata.worcesterma.gov", "United States", "us"),
    ("https://www.wvcheckbook.gov", "United States", "us"),
]
_CKAN_TERMS = ["permis construction", "licencia construccion", "building permit",
               "permesso costruire", "pozwolenie budowe", "bouwvergunning",
               "byggetillatelse", "baugenehmigung", "proyectos construccion",
               "development application", "planning application", "resource consent",
               "environmental impact assessment", "licenciamento ambiental", "impacto ambiental",
               "mining lease", "wind farm", "infrastructure project",
               "izin lingkungan", "amdal", "izin mendirikan bangunan",
               "\u0111\u00e1nh gi\u00e1 t\u00e1c \u0111\u1ed9ng m\u00f4i tr\u01b0\u1eddng",
               "gi\u1ea5y ph\u00e9p x\u00e2y d\u1ef1ng", "\u5efa\u7bc9\u57f7\u7167",
               "\u043e\u0446\u0456\u043d\u043a\u0430 \u0432\u043f\u043b\u0438\u0432\u0443",
               "empreendimento", "obra publica", "concesion", "uvp", "amenagement"]
_CKAN_TITLE_RE = re.compile(
    r"(permit|permis|licenc|licens|vergunning|genehmigung|pozwolen|costruire|"
    r"bygge|bygglov|construc|construction|obra|edifica|planning|urban|"
    # EIA / environment / development (matches the search vocabulary)
    r"ambient|environment|impacto|impact|amenagement|am\u00e9nagement|desarrollo|"
    r"empreendimento|proyecto|projeto|projet|infraestru|infrastru|mineria|miner\u00eda|"
    r"mining|concesi|concess|chantier|lotissement|cantiere|edilizia|"
    # Indonesian / Malay
    r"izin|amdal|pembangunan|bangunan|tambang|lingkungan|"
    # Vietnamese (accent-bearing substrings)
    r"x\u00e2y d\u1ef1ng|m\u00f4i tr\u01b0\u1eddng|gi\u1ea5y ph\u00e9p|"
    # Chinese (simplified + traditional)
    r"\u5efa\u7b51|\u5efa\u7bc9|\u65bd\u5de5|\u74b0\u5883|\u73af\u5883|\u5f00\u53d1|\u958b\u767c|"
    # Ukrainian / Russian
    r"\u0431\u0443\u0434\u0456\u0432|\u0434\u043e\u0437\u0432\u0456\u043b|\u0434\u043e\u0432\u043a\u0456\u043b|"
    r"\u0441\u0442\u0440\u043e\u0438\u0442|\u0440\u0430\u0437\u0440\u0435\u0448|"
    # Thai
    r"\u0e01\u0e48\u0e2d\u0e2a\u0e23\u0e49\u0e32\u0e07|\u0e2a\u0e34\u0e48\u0e07\u0e41\u0e27\u0e14\u0e25\u0e49\u0e2d\u0e21)", re.I)

# ============================ OpenDataSoft federation ============================
# OpenDataSoft Explore API v2.1 (verified live against help.opendatasoft.com):
#   catalog search : GET https://{host}/api/explore/v2.1/catalog/datasets?where=<odsql>&limit=
#                    -> {"results":[{"dataset_id":..,"metas":{..}}]}  (some deployments: "datasets")
#   records        : GET .../catalog/datasets/{id}/records?limit=  -> {"results":[{..fields..}]}
# Public portals allow anonymous access (quota-limited). Heavy FR/EU/AU-council adoption.
# Term-scoped like the CKAN federation; additionally drops clear terminal permit states.
_ODS_PORTALS = [
    ("data.ajman.ae", "United Arab Emirates", "ae"),
    ("connectgh.com.au", "Australia", "au"),
    ("data.ballarat.vic.gov.au", "Australia", "au"),
    ("data.bmcc.nsw.gov.au", "Australia", "au"),
    ("data.camden.nsw.gov.au", "Australia", "au"),
    ("data.campbelltown.nsw.gov.au", "Australia", "au"),
    ("data.casey.vic.gov.au", "Australia", "au"),
    ("data.corangamite.vic.gov.au", "Australia", "au"),
    ("data.cumberland.nsw.gov.au", "Australia", "au"),
    ("data.fairfieldcity.nsw.gov.au", "Australia", "au"),
    ("data.frankston.vic.gov.au", "Australia", "au"),
    ("data.hawkesbury.nsw.gov.au", "Australia", "au"),
    ("data.lakemac.com.au", "Australia", "au"),
    ("data.liverpool.nsw.gov.au", "Australia", "au"),
    ("data.maitland.nsw.gov.au", "Australia", "au"),
    ("data.melbourne.vic.gov.au", "Australia", "au"),
    ("data.penrith.city", "Australia", "au"),
    ("data.randwick.nsw.gov.au", "Australia", "au"),
    ("data.theparks.nsw.gov.au", "Australia", "au"),
    ("data.wollondilly.nsw.gov.au", "Australia", "au"),
    ("data.wpcouncils.nsw.gov.au", "Australia", "au"),
    ("geelongdataexchange.com.au", "Australia", "au"),
    ("mav-technology-geelongvic.opendatasoft.com", "Australia", "au"),
    ("opendata-newcastlenswiar.opendatasoft.com", "Australia", "au"),
    ("smart.darwin.nt.gov.au", "Australia", "au"),
    ("ares-digitalwallonia.opendatasoft.com", "Belgium", "be"),
    ("bruxellesdata.opendatasoft.com", "Belgium", "be"),
    ("d4w-digitalwallonia.opendatasoft.com", "Belgium", "be"),
    ("data.bep.be", "Belgium", "be"),
    ("data.brugge.be", "Belgium", "be"),
    ("data.namur.be", "Belgium", "be"),
    ("data.stad.gent", "Belgium", "be"),
    ("e-zybw.be", "Belgium", "be"),
    ("odwb.be", "Belgium", "be"),
    ("opendata.brussel.be", "Belgium", "be"),
    ("opendata.brussels.be", "Belgium", "be"),
    ("opendata.liege.be", "Belgium", "be"),
    ("opendata.mons.be", "Belgium", "be"),
    ("opendata.wse.vlaanderen.be", "Belgium", "be"),
    ("prc-digitalwallonia.opendatasoft.com", "Belgium", "be"),
    ("spi-digitalwallonia.opendatasoft.com", "Belgium", "be"),
    ("tournai.opendatasoft.com", "Belgium", "be"),
    ("data.gov.bh", "Bahrain", "bh"),
    ("do101mtl.opendatasoft.com", "Canada", "ca"),
    ("opendata.vancouver.ca", "Canada", "ca"),
    ("opendatakingston.cityofkingston.ca", "Canada", "ca"),
    ("opendatakingston.opendatasoft.com", "Canada", "ca"),
    ("data.bl.ch", "Switzerland", "ch"),
    ("data.bs.ch", "Switzerland", "ch"),
    ("data.gr.ch", "Switzerland", "ch"),
    ("data.sz.ch", "Switzerland", "ch"),
    ("data.tg.ch", "Switzerland", "ch"),
    ("daten.sg.ch", "Switzerland", "ch"),
    ("daten.stadt.sg.ch", "Switzerland", "ch"),
    ("opendata.fr.ch", "Switzerland", "ch"),
    ("opendata.tpg.ch", "Switzerland", "ch"),
    ("swisspost.opendatasoft.com", "Switzerland", "ch"),
    ("bogota-laburbano.opendatasoft.com", "Colombia", "co"),
    ("transport.opendatasoft.com", "Colombia", "co"),
    ("mannheim.opendatasoft.com", "Germany", "de"),
    ("open-data.dortmund.de", "Germany", "de"),
    ("opendata.dormagen.de", "Germany", "de"),
    ("opendata.potsdam.de", "Germany", "de"),
    ("opendata.rhein-kreis-neuss.de", "Germany", "de"),
    ("opendata.wuerzburg.de", "Germany", "de"),
    ("analisis.datosabiertos.jcyl.es", "Spain", "es"),
    ("angeles-navarro.opendatasoft.com", "Spain", "es"),
    ("datosabiertos.dipcas.es", "Spain", "es"),
    ("observa.gijon.es", "Spain", "es"),
    ("opendata.clermontmetropole.eu", "Spain", "es"),
    ("valencia.opendatasoft.com", "Spain", "es"),
    ("achat-public.data.bretagne.bzh", "France", "fr"),
    ("aix-en-provence.opendatasoft.com", "France", "fr"),
    ("anglet-opendatapaysbasque.opendatasoft.com", "France", "fr"),
    ("anruopendata.opendatasoft.com", "France", "fr"),
    ("api-lannuaire.service-public.fr", "France", "fr"),
    ("app.datajoule.fr", "France", "fr"),
    ("ardennemetropole.opendatasoft.com", "France", "fr"),
    ("auvergne-rhone-alpes-dataeducation.opendatasoft.com", "France", "fr"),
    ("bayonne-opendatapaysbasque.opendatasoft.com", "France", "fr"),
    ("boamp-datadila.opendatasoft.com", "France", "fr"),
    ("boamp.fr", "France", "fr"),
    ("bodacc.fr", "France", "fr"),
    ("bondoufle-grandparissud.opendatasoft.com", "France", "fr"),
    ("boulognebillancourt-seineouest.opendatasoft.com", "France", "fr"),
    ("bpce.opendatasoft.com", "France", "fr"),
    ("bretagne-dataeducation.opendatasoft.com", "France", "fr"),
    ("cachan.opendatasoft.com", "France", "fr"),
    ("cesson-grandparissud.opendatasoft.com", "France", "fr"),
    ("chaville-seineouest.opendatasoft.com", "France", "fr"),
    ("combslaville-grandparissud.opendatasoft.com", "France", "fr"),
    ("corbeil-essonnes-grandparissud.opendatasoft.com", "France", "fr"),
    ("dashboard.paris", "France", "fr"),
    ("data.82amenagement.fr", "France", "fr"),
    ("data.82numerique.fr", "France", "fr"),
    ("data.ademe.fr", "France", "fr"),
    ("data.agen.fr", "France", "fr"),
    ("data.agglo-carene.fr", "France", "fr"),
    ("data.agglo-montargoise.fr", "France", "fr"),
    ("data.aide-developpement.gouv.fr", "France", "fr"),
    ("data.ameli.fr", "France", "fr"),
    ("data.ampmetropole.fr", "France", "fr"),
    ("data.anfr.fr", "France", "fr"),
    ("data.angers.fr", "France", "fr"),
    ("data.blois.agglopolys.fr", "France", "fr"),
    ("data.bourgesplus.fr", "France", "fr"),
    ("data.bretagne.bzh", "France", "fr"),
    ("data.cannes.com", "France", "fr"),
    ("data.cannes.fr", "France", "fr"),
    ("data.capatlantique.fr", "France", "fr"),
    ("data.centrevaldeloire.fr", "France", "fr"),
    ("data.chateauroux-metropole.fr", "France", "fr"),
    ("data.cnav.fr", "France", "fr"),
    ("data.combs-la-ville.fr", "France", "fr"),
    ("data.corsica", "France", "fr"),
    ("data.coudray-montceaux.fr", "France", "fr"),
    ("data.culture.gouv.fr", "France", "fr"),
    ("data.culturecommunication.gouv.fr", "France", "fr"),
    ("data.departement41.fr", "France", "fr"),
    ("data.drees.solidarites-sante.gouv.fr", "France", "fr"),
    ("data.dunkerque-agglo.fr", "France", "fr"),
    ("data.economie.gouv.fr", "France", "fr"),
    ("data.education.gouv.fr", "France", "fr"),
    ("data.enseignementsup-recherche.gouv.fr", "France", "fr"),
    ("data.etiolles.fr", "France", "fr"),
    ("data.eurelien.fr", "France", "fr"),
    ("data.evrycourcouronnes.fr", "France", "fr"),
    ("data.fleurysurorne.fr", "France", "fr"),
    ("data.gers.fr", "France", "fr"),
    ("data.gouv.nc", "France", "fr"),
    ("data.grandchambord.fr", "France", "fr"),
    ("data.grandparisgrandest.fr", "France", "fr"),
    ("data.grandparissud.fr", "France", "fr"),
    ("data.grandpoitiers.fr", "France", "fr"),
    ("data.grandsoissons.com", "France", "fr"),
    ("data.grigny91.fr", "France", "fr"),
    ("data.haute-garonne.fr", "France", "fr"),
    ("data.hauts-de-france.education.gouv.fr", "France", "fr"),
    ("data.idelis.fr", "France", "fr"),
    ("data.iledefrance-mobilites.fr", "France", "fr"),
    ("data.iledefrance.fr", "France", "fr"),
    ("data.issy.com", "France", "fr"),
    ("data.lafibre64.fr", "France", "fr"),
    ("data.lamayenne.fr", "France", "fr"),
    ("data.laregion.fr", "France", "fr"),
    ("data.larochesuryon.fr", "France", "fr"),
    ("data.le64.fr", "France", "fr"),
    ("data.loire-atlantique.fr", "France", "fr"),
    ("data.maine-et-loire.fr", "France", "fr"),
    ("data.mairie-ris-orangis.fr", "France", "fr"),
    ("data.maugescommunaute.fr", "France", "fr"),
    ("data.metropole-rouen-normandie.fr", "France", "fr"),
    ("data.metropoletpm.fr", "France", "fr"),
    ("data.meudon.fr", "France", "fr"),
    ("data.moissy-cramayel.fr", "France", "fr"),
    ("data.montreuil.fr", "France", "fr"),
    ("data.mulhouse-alsace.fr", "France", "fr"),
    ("data.nantesmetropole.fr", "France", "fr"),
    ("data.nimes-metropole.fr", "France", "fr"),
    ("data.normandie.education.gouv.fr", "France", "fr"),
    ("data.occitanie.education.gouv.fr", "France", "fr"),
    ("data.ofgl.fr", "France", "fr"),
    ("data.orleans-metropole.fr", "France", "fr"),
    ("data.osmontrouge.fr", "France", "fr"),
    ("data.paysdelaloire.fr", "France", "fr"),
    ("data.ratp.fr", "France", "fr"),
    ("data.regionreunion.com", "France", "fr"),
    ("data.rennesmetropole.fr", "France", "fr"),
    ("data.saint-maur.com", "France", "fr"),
    ("data.saint-pierre-du-perray.fr", "France", "fr"),
    ("data.saintnazaireagglo.fr", "France", "fr"),
    ("data.saintry-sur-seine.fr", "France", "fr"),
    ("data.sarthe.fr", "France", "fr"),
    ("data.savigny-le-temple.fr", "France", "fr"),
    ("data.seineouest.fr", "France", "fr"),
    ("data.sevres.fr", "France", "fr"),
    ("data.sicoval.fr", "France", "fr"),
    ("data.smartidf.services", "France", "fr"),
    ("data.stmalo-agglomeration.fr", "France", "fr"),
    ("data.tco.re", "France", "fr"),
    ("data.teo-paysdelaloire.fr", "France", "fr"),
    ("data.toulouse-metropole.fr", "France", "fr"),
    ("data.tours-metropole.fr", "France", "fr"),
    ("data.twisto.fr", "France", "fr"),
    ("data.unedic.org", "France", "fr"),
    ("data.val2c.fr", "France", "fr"),
    ("data.valdeloirenumerique.fr", "France", "fr"),
    ("data.vendee.fr", "France", "fr"),
    ("data.vert-saint-denis.fr", "France", "fr"),
    ("data.ville-bondoufle.fr", "France", "fr"),
    ("data.ville-cesson.fr", "France", "fr"),
    ("data.ville-lieusaint.fr", "France", "fr"),
    ("data.ville-soissons.fr", "France", "fr"),
    ("data.villedavray.fr", "France", "fr"),
    ("data.vincennes.fr", "France", "fr"),
    ("dataratp.opendatasoft.com", "France", "fr"),
    ("dataratp2.opendatasoft.com", "France", "fr"),
    ("datavaccin-covid.ameli.fr", "France", "fr"),
    ("dataviz-haute-garonne.opendatasoft.com", "France", "fr"),
    ("dgal.opendatasoft.com", "France", "fr"),
    ("dgefp.opendatasoft.com", "France", "fr"),
    ("donnees.grandchambery.fr", "France", "fr"),
    ("e-agre.opendatasoft.com", "France", "fr"),
    ("enseignement-agricole.opendatasoft.com", "France", "fr"),
    ("epn-agglo.opendatasoft.com", "France", "fr"),
    ("equipements.sports.gouv.fr", "France", "fr"),
    ("etiolles-grandparissud.opendatasoft.com", "France", "fr"),
    ("evrycourcouronnes-grandparissud.opendatasoft.com", "France", "fr"),
    ("fos-sur-mer.opendatasoft.com", "France", "fr"),
    ("future4care.opendatasoft.com", "France", "fr"),
    ("geocatalogue.lorient-agglo.bzh", "France", "fr"),
    ("gpseo.opendatasoft.com", "France", "fr"),
    ("grand-est-dataeducation.opendatasoft.com", "France", "fr"),
    ("hasparren-opendatapaysbasque.opendatasoft.com", "France", "fr"),
    ("hautespyrenees.opendatasoft.com", "France", "fr"),
    ("herault-data.eu", "France", "fr"),
    ("herault-data.fr", "France", "fr"),
    ("info-financiere.fr", "France", "fr"),
    ("journal-officiel.gouv.fr", "France", "fr"),
    ("karudata.com", "France", "fr"),
    ("lafibre64-data64.opendatasoft.com", "France", "fr"),
    ("lieusaint-grandparissud.opendatasoft.com", "France", "fr"),
    ("lisses-grandparissud.opendatasoft.com", "France", "fr"),
    ("mairie-bastia-datacorsica.opendatasoft.com", "France", "fr"),
    ("meudon-seineouest.opendatasoft.com", "France", "fr"),
    ("nandy-grandparissud.opendatasoft.com", "France", "fr"),
    ("observatoire-climat.toulouse-metropole.fr", "France", "fr"),
    ("observatoire.odds93.fr", "France", "fr"),
    ("oddc-datacorsica.opendatasoft.com", "France", "fr"),
    ("odisse.santepubliquefrance.fr", "France", "fr"),
    ("open.urssaf.fr", "France", "fr"),
    ("opendata-paysbasque.fr", "France", "fr"),
    ("opendata.afd.fr", "France", "fr"),
    ("opendata.aude.fr", "France", "fr"),
    ("opendata.aveyron.fr", "France", "fr"),
    ("opendata.bordeaux-metropole.fr", "France", "fr"),
    ("opendata.brest-metropole.fr", "France", "fr"),
    ("opendata.caissedesdepots.fr", "France", "fr"),
    ("opendata.cangt.fr", "France", "fr"),
    ("opendata.cc-lacqorthez.fr", "France", "fr"),
    ("opendata.clermont-ferrand.fr", "France", "fr"),
    ("opendata.doubs.fr", "France", "fr"),
    ("opendata.finistere.fr", "France", "fr"),
    ("opendata.ha-py.fr", "France", "fr"),
    ("opendata.hauts-de-seine.fr", "France", "fr"),
    ("opendata.iledefrance.fr", "France", "fr"),
    ("opendata.isere.fr", "France", "fr"),
    ("opendata.lillemetropole.fr", "France", "fr"),
    ("opendata.paris.fr", "France", "fr"),
    ("opendata.paris.fr.opendatasoft.com", "France", "fr"),
    ("opendata.pau.fr", "France", "fr"),
    ("opendata.plus.transformation.gouv.fr", "France", "fr"),
    ("opendata.roubaix.fr", "France", "fr"),
    ("opendata.roumoiseine.fr", "France", "fr"),
    ("opendata.sqy.fr", "France", "fr"),
    ("opendata.stif.info", "France", "fr"),
    ("opendata.strasbourg.eu", "France", "fr"),
    ("opendata.tourcoing.fr", "France", "fr"),
    ("opendata.vert-saint-denis.fr", "France", "fr"),
    ("opendata56.fr", "France", "fr"),
    ("parisdata.opendatasoft.com", "France", "fr"),
    ("porto-vecchio.opendatasoft.com", "France", "fr"),
    ("projets-environnement.gouv.fr", "France", "fr"),
    ("rec-stif.opendatasoft.com", "France", "fr"),
    ("regionguadeloupe.opendatasoft.com", "France", "fr"),
    ("risorangis-grandparissud.opendatasoft.com", "France", "fr"),
    ("saint-jean-de-luz-opendatapaysbasque.opendatasoft.com", "France", "fr"),
    ("saint-louis-agglo.opendatasoft.com", "France", "fr"),
    ("saintgermainlescorbeil-grandparissud.opendatasoft.com", "France", "fr"),
    ("saintmande.opendatasoft.com", "France", "fr"),
    ("savignyletemple-grandparissud.opendatasoft.com", "France", "fr"),
    ("sevres-seineouest.opendatasoft.com", "France", "fr"),
    ("sports-sgsocialgouv.opendatasoft.com", "France", "fr"),
    ("stpdp-grandparissud.opendatasoft.com", "France", "fr"),
    ("tourisme62.opendatasoft.com", "France", "fr"),
    ("transparence.sante.gouv.fr", "France", "fr"),
    ("twisto.opendatasoft.com", "France", "fr"),
    ("valenciennesmetro.opendatasoft.com", "France", "fr"),
    ("vanves-seineouest.opendatasoft.com", "France", "fr"),
    ("villedavray-seineouest.opendatasoft.com", "France", "fr"),
    ("visualisation.dila.fr", "France", "fr"),
    ("zabal-agriculture.opendata-paysbasque.fr", "France", "fr"),
    ("data.leicester.gov.uk", "United Kingdom", "gb"),
    ("nihr.opendatasoft.com", "United Kingdom", "gb"),
    ("ukpowernetworks.opendatasoft.com", "United Kingdom", "gb"),
    ("opendata.comune.bologna.it", "Italy", "it"),
    ("cdmx.opendatasoft.com", "Mexico", "mx"),
    ("inai.opendatasoft.com", "Mexico", "mx"),
    ("nuevoleon.opendatasoft.com", "Mexico", "mx"),
    ("maps.opendata.opt.nc", "New Caledonia", "nc"),
    ("data.eindhoven.nl", "Netherlands", "nl"),
    ("transparencia.sns.gov.pt", "Portugal", "pt"),
    ("data.gov.qa", "Qatar", "qa"),
    ("zastrugis.my.opendatasoft.com", "Serbia", "rs"),
    ("helsingborg.opendatasoft.com", "Sweden", "se"),
    ("opendata.umea.se", "Sweden", "se"),
    ("cityofsalinas.opendatasoft.com", "United States", "us"),
    ("codeforraleigh.opendatasoft.com", "United States", "us"),
    ("data.carync.gov", "United States", "us"),
    ("data.jerseycitynj.gov", "United States", "us"),
    ("data.longbeach.gov", "United States", "us"),
    ("data.nccourts.gov", "United States", "us"),
    ("data.townofcary.org", "United States", "us"),
    ("demography.osbm.nc.gov", "United States", "us"),
    ("linc.osbm.nc.gov", "United States", "us"),
    ("opendata.morrisvillenc.gov", "United States", "us"),
    ("opendata.townofmorrisville.org", "United States", "us"),
    ("geocatalogue.smavd.org", "World", "world"),
]
_ODS_TERMS = ["permis de construire", "autorisation d'urbanisme", "am\u00e9nagement",
              "installations class\u00e9es", "urbanisme", "chantier",
              "building permit", "development application", "planning application",
              "licencia urbanistica", "licenciamento", "construction",
              "bouwvergunning", "omgevingsvergunning",
              "baugenehmigung", "bauprojekt",
              "permesso di costruire", "cantiere",
              "enqu\u00eate publique", "lotissement"]
# terminal states to exclude (multilingual, substring, accent/underscore-normalised)
_ODS_DEAD = ("refus", "reject", "rechaz", "annul", "cancel", "retir", "withdraw",
             "expir", "caduc", "perim", "abandon", "desist", "archiv", "indefer",
             "lapsed", "void", "vencido",
             # built / no-longer-in-process states (in-process gate):
             "operat", "production", "dismantl", "decommission", "completed",
             "terminad", "concluid", "en service", "in betrieb")
_ODS_NAMEK = ("nom", "name", "title", "titre", "libelle", "libell\u00e9", "intitule",
              "intitul\u00e9", "denomination", "d\u00e9nomination", "projet", "project",
              "operation", "op\u00e9ration", "objet", "adresse", "address", "designation",
              "nombre", "nome", "titulo", "t\u00edtulo", "denominacion", "denominaci\u00f3n",
              "proyecto", "projeto", "nama", "naam", "bezeichnung", "titel", "ten", "t\u00ean")
_ODS_STATUSK = ("statut", "status", "etat", "\u00e9tat", "phase", "etape", "\u00e9tape",
                "avancement", "estado", "situacao", "situa\u00e7\u00e3o", "state",
                "situacion", "situaci\u00f3n", "estatus", "zustand", "fase",
                "trang thai", "tr\u1ea1ng th\u00e1i", "keterangan")
_ODS_GEOK = ("geo_point_2d", "geopoint", "geo_point", "point_geo", "coordonnees",
             "coordonn\u00e9es", "latlng", "lat_lng", "location", "geolocalisation",
             "geo_shape", "geometry", "wkb_geometry", "the_geom",
             "coordenadas", "ubicacion", "ubicaci\u00f3n", "localizacao",
             "localiza\u00e7\u00e3o", "koordinat", "geolocation", "geom")

def _ods_latlng(rec):
    # 1) explicit geo fields
    for k, v in rec.items():
        if k.lower() not in _ODS_GEOK or v in (None, "", []):
            continue
        if isinstance(v, dict):
            if "coordinates" in v:                       # geo_shape / geometry
                c = _geom_center(v)
                if c: return c
            la = v.get("lat") or v.get("latitude") or v.get("y")
            lo = v.get("lon") or v.get("lng") or v.get("longitude") or v.get("x")
            la, lo = _num(la), _num(lo)
            if la is not None and lo is not None: return (la, lo)
        elif isinstance(v, (list, tuple)) and len(v) == 2:
            a, b = _num(v[0]), _num(v[1])                # ODS geo_point_2d list = [lat, lon]
            if a is not None and b is not None:
                return (a, b) if abs(a) <= 90 else (b, a)
        elif isinstance(v, str) and "," in v:
            parts = v.split(",")
            if len(parts) == 2:
                a, b = _num(parts[0]), _num(parts[1])
                if a is not None and b is not None:
                    return (a, b) if abs(a) <= 90 else (b, a)
    # 2) separate latitude / longitude columns
    la = lo = None
    for k, v in rec.items():
        kl = k.lower()
        if la is None and kl in ("lat", "latitude", "y_lat", "ycoord", "y",
                                 "latitud", "latitude_dd", "lat_dd", "lintang"): la = _num(v)
        if lo is None and kl in ("lon", "lng", "longitude", "x_lon", "xcoord", "x",
                                 "longitud", "longitude_dd", "lon_dd", "bujur"): lo = _num(v)
    if la is not None and lo is not None: return (la, lo)
    return None

def _ods_pick(rec, keys):
    for k, v in rec.items():
        if k.lower() in keys and isinstance(v, (str, int, float)) and str(v).strip():
            return str(v)
    return ""

def fetch_ods_federation(per_portal=None, per_ds=800):
    per_portal = per_portal or (10 if os.environ.get("HARVEST_FEDERATIONS") == "1" else 6)
    out = []
    budget_min = _fed_budget("ODS_BUDGET_MIN", 35, 45)
    t_end = time.time() + budget_min * 60
    _ods_portals = _shard_list(_ODS_PORTALS)
    for (host, country, cc) in _ods_portals:
        if time.time() > t_end:
            _flag("ods federation hit %d-min budget -- %d portals not reached" %
                  (budget_min, len(_ods_portals) - _ods_portals.index((host, country, cc))))
            break
        base = "https://%s/api/explore/v2.1/catalog/datasets" % host
        dsids = []; seen = set()
        for term in _ODS_TERMS:
            try:
                u = base + "?" + urllib.parse.urlencode(
                    {"where": 'search("%s")' % term.replace('"', ""), "limit": 20})
                d = _get_json(u)
            except Exception:
                continue
            for it in ((d or {}).get("results") or (d or {}).get("datasets") or []):
                did = it.get("dataset_id") or (it.get("dataset") or {}).get("dataset_id")
                if did and did not in seen:
                    seen.add(did); dsids.append(did)
            time.sleep(0.25)
            if len(dsids) >= per_portal: break
        got = 0
        for did in dsids[:per_portal]:
            recs = []
            for off in range(0, per_ds, 100):        # ODS caps limit at 100 -> offset-page
                try:
                    ru = ("https://%s/api/explore/v2.1/catalog/datasets/%s/records?limit=100&offset=%d"
                          % (host, did, off))
                    rd = _get_json(ru)
                except Exception:
                    break
                batch = (rd or {}).get("results") or []
                recs.extend(batch)
                if len(batch) < 100: break
                time.sleep(0.15)
            n0 = len(out)
            for r in recs[:per_ds]:
                try:
                    rec = r.get("fields") if isinstance(r.get("fields"), dict) else r
                    ll = _ods_latlng(rec)
                    if not ll: continue
                    st = _ods_pick(rec, _ODS_STATUSK)
                    sn = str(st or "").lower().replace("_", " ")
                    if any(k in sn for k in _ODS_DEAD): continue      # drop terminal states
                    nm = _ods_pick(rec, _ODS_NAMEK) or did.replace("-", " ")
                    p = {"name": nm[:140], "type": "Permit / development (%s)" % country,
                         "state": country, "lat": round(ll[0], 5), "lng": round(ll[1], 5),
                         "precise": True, "size": "", "status": st[:40], "company": "",
                         "url": "https://%s" % host,
                         "desc": "From %s open data (OpenDataSoft) \u00b7 %s." % (country, did[:60]),
                         "source": "ods_%s" % cc}
                    p["impact"] = rate_project(p, sensitivity=0)
                    out.append(p)
                except Exception:
                    continue
            if len(out) > n0:
                got += 1
                print("  ods %s: +%d from '%s'" % (country, len(out) - n0, did[:40]))
            time.sleep(0.25)
    print("  ods federation: %d points from %d portals" % (len(out), len(_ODS_PORTALS)))
    return out


# ============================== GeoNode federation ==============================
# GeoNode REST API v2 (verified against docs.geonode.org):
#   resource search : GET https://{host}/api/v2/resources/?filter{title.icontains}=<term>&page_size=
#                     -> {"resources":[{"alternate":"ws:layer","subtype":"vector","links":[...]}]}
#   each vector layer's features via its OGC:WFS link (resource.links[] link_type OGC:WFS),
#   fallback https://{host}/geoserver/wfs -> WFS GetFeature outputFormat=application/json (GeoJSON).
# Title-scoped to development/land-use vocabulary; terminal states dropped (shared _ODS_DEAD).
# Coarser scope than permit feeds (some layers are full inventories) -> FIRST-RUN REVIEW.
_GEONODE_PORTALS = [
    ("lrimsfaoaf.ait.ac.th", "Afghanistan", "af"),
    ("nafcoast.org", "Africa", "africa"),
    ("climateriskmap.environment.gov.ag", "Antigua and Barbuda", "ag"),
    ("nri.environment.gov.ag", "Antigua and Barbuda", "ag"),
    ("coronelsuarezgis.gob.ar", "Argentina", "ar"),
    ("datos.inidep.edu.ar", "Argentina", "ar"),
    ("dipec.jujuy.gob.ar", "Argentina", "ar"),
    ("estadisticasig.rionegro.gov.ar", "Argentina", "ar"),
    ("geo.gualeguaychu.gov.ar", "Argentina", "ar"),
    ("geonode.minfra.gba.gob.ar", "Argentina", "ar"),
    ("geonode.pergamino.gob.ar", "Argentina", "ar"),
    ("geonode.senasa.gob.ar", "Argentina", "ar"),
    ("geoportal.cfi.org.ar", "Argentina", "ar"),
    ("geoportal.idesa.gob.ar", "Argentina", "ar"),
    ("geoportal.lujandecuyo.gob.ar", "Argentina", "ar"),
    ("geoportal.obraspublicas.gob.ar", "Argentina", "ar"),
    ("geoportal.salta.gob.ar", "Argentina", "ar"),
    ("geoportal.tresdefebrero.gob.ar", "Argentina", "ar"),
    ("geoportalqa.lujandecuyo.gob.ar", "Argentina", "ar"),
    ("ide-enacom.arsat.com.ar", "Argentina", "ar"),
    ("ide.correoargentino.com.ar", "Argentina", "ar"),
    ("ide.godoycruz.gob.ar", "Argentina", "ar"),
    ("ide.santarosamendoza.gob.ar", "Argentina", "ar"),
    ("idecapital.cc.gob.ar", "Argentina", "ar"),
    ("manejodelfuego.conae.gov.ar", "Argentina", "ar"),
    ("mapas.geomatica.idr.org.ar", "Argentina", "ar"),
    ("mapas.sancarlos.gob.ar", "Argentina", "ar"),
    ("municipiosig.rionegro.gov.ar", "Argentina", "ar"),
    ("nodo.cfi.org.ar", "Argentina", "ar"),
    ("oat.ambiente.gob.ar", "Argentina", "ar"),
    ("poblacion.idear.gov.ar", "Argentina", "ar"),
    ("saludsig.rionegro.gov.ar", "Argentina", "ar"),
    ("sigvial.dpvmisiones.gob.ar", "Argentina", "ar"),
    ("geonode.lebensraumvernetzung.at", "Austria", "at"),
    ("maps.oraotca.org", "Australia", "au"),
    ("geodash.gov.bd", "Bangladesh", "bd"),
    ("geoportal.bforest.gov.bd", "Bangladesh", "bd"),
    ("gis.nesco.gov.bd", "Bangladesh", "bd"),
    ("nsdi.gov.bd", "Bangladesh", "bd"),
    ("geoportail.bumigeb.bf", "Burkina Faso", "bf"),
    ("46.165.252.159", "Bulgaria", "bg"),
    ("84.16.227.175", "Bulgaria", "bg"),
    ("georisk.gouv.bj", "Benin", "bj"),
    ("sig.sineb.bj", "Benin", "bj"),
    ("aetn.geo.gob.bo", "Bolivia", "bo"),
    ("dgf.geo.gob.bo", "Bolivia", "bo"),
    ("geo03.siarh.gob.bo", "Bolivia", "bo"),
    ("geoconcurso.geo.gob.bo", "Bolivia", "bo"),
    ("geonode.fonadin.gob.bo", "Bolivia", "bo"),
    ("geoportal.mhe.gob.bo", "Bolivia", "bo"),
    ("ide.lapaz.bo", "Bolivia", "bo"),
    ("ipdsa.geo.gob.bo", "Bolivia", "bo"),
    ("sigvmeea.hidrocarburos.gob.bo", "Bolivia", "bo"),
    ("arvorezinha.geomunicipios.com", "Brazil", "br"),
    ("catalogo.ipe.df.gov.br", "Brazil", "br"),
    ("dados.it-amazonia.dev", "Brazil", "br"),
    ("geodados.daee.sp.gov.br", "Brazil", "br"),
    ("geonode.paranagua.pr.gov.br", "Brazil", "br"),
    ("geoportal-spunet.gestao.gov.br", "Brazil", "br"),
    ("imde.portoalegre.rs.gov.br", "Brazil", "br"),
    ("inderh.snirh.gov.br", "Brazil", "br"),
    ("sig.amvali.org.br", "Brazil", "br"),
    ("siga.meioambiente.go.gov.br", "Brazil", "br"),
    ("sigahomol.meioambiente.go.gov.br", "Brazil", "br"),
    ("cgi-gn.nlcs.gov.bt", "Bhutan", "bt"),
    ("sdss.dofps.gov.bt", "Bhutan", "bt"),
    ("geo.lachute.ca", "Canada", "ca"),
    ("geonode.doigriverfn.com", "Canada", "ca"),
    ("anidlimarichoapa.ciren.cl", "Chile", "cl"),
    ("apibotanico.ciren.cl", "Chile", "cl"),
    ("geonode.meteochile.gob.cl", "Chile", "cl"),
    ("ide.emprendequillota.cl", "Chile", "cl"),
    ("geo.snh.cm", "Cameroon", "cm"),
    ("catastro.munisc.go.cr", "Costa Rica", "cr"),
    ("geonode.bgeoinorca.com", "Costa Rica", "cr"),
    ("geoportal-saracc.cne.go.cr", "Costa Rica", "cr"),
    ("gestorgeo.inec.go.cr", "Costa Rica", "cr"),
    ("ide.santaana.go.cr", "Costa Rica", "cr"),
    ("ideonion.go.cr", "Costa Rica", "cr"),
    ("idesca.munisc.go.cr", "Costa Rica", "cr"),
    ("raster.munisc.go.cr", "Costa Rica", "cr"),
    ("sae.inec.go.cr", "Costa Rica", "cr"),
    ("idema.geotech.cu", "Cuba", "cu"),
    ("185.17.146.157", "Germany", "de"),
    ("bish-staging.aconium.eu", "Germany", "de"),
    ("breitband-in-sh.de", "Germany", "de"),
    ("geo-katalog.julius-kuehn.de", "Germany", "de"),
    ("geoportal.giz.de", "Germany", "de"),
    ("giz.sta.hz.kartoza.com", "Germany", "de"),
    ("milton-geo.thw-fts.org", "Germany", "de"),
    ("waldgeoportal.de", "Germany", "de"),
    ("dominode.dm", "Dominica", "dm"),
    ("cartografia.ayuntamientosantiago.gob.do", "Republica Dominicana", "do"),
    ("geoportal.catastro.gob.do", "Republica Dominicana", "do"),
    ("geoportal.iderd.gob.do", "Republica Dominicana", "do"),
    ("geoportal.pedepe.org", "Republica Dominicana", "do"),
    ("nodosini.ministeriodeeducacion.gob.do", "Republica Dominicana", "do"),
    ("plataforma.sini.gob.do", "Republica Dominicana", "do"),
    ("geonode.inec.gob.ec", "Ecuador", "ec"),
    ("geoportal.ame.gob.ec", "Ecuador", "ec"),
    ("geoservicios.inamhi.gob.ec", "Ecuador", "ec"),
    ("geovisor.manabi.gob.ec", "Ecuador", "ec"),
    ("movimientosenmasadmq.geoenergia.gob.ec", "Ecuador", "ec"),
    ("data.transportforcairo.com", "Egypt", "eg"),
    ("datosmarinos.cedex.es", "Spain", "es"),
    ("geonode.arxiuhistoricpoblenou.cat", "Spain", "es"),
    ("geoportal-lavalldegallinera.geoinnova.es", "Spain", "es"),
    ("pmpc-arandadeduero.geoinnova.es", "Spain", "es"),
    ("ethionsdi.gov.et", "Ethiopia", "et"),
    ("geo.portal.ebi.gov.et", "Ethiopia", "et"),
    ("geonode.portal.ebi.gov.et", "Ethiopia", "et"),
    ("lbdcdirectory.gov.et", "Ethiopia", "et"),
    ("lsc-hub.eiar.gov.et", "Ethiopia", "et"),
    ("nsis.moa.gov.et", "Ethiopia", "et"),
    ("qfield.cloud.ebi.gov.et", "Ethiopia", "et"),
    ("iws.seastorms.eu", "European Union", "eu"),
    ("portodimare.eu", "European Union", "eu"),
    ("jyvaskyla.infraweb.fi", "Finland", "fi"),
    ("data.sigea.educagri.fr", "France", "fr"),
    ("geonode.recette.oieau.fr", "France", "fr"),
    ("qualif.data.sigea.educagri.fr", "France", "fr"),
    ("datamap.gov.wales", "United Kingdom", "gb"),
    ("ghanageoportal.com", "Ghana", "gh"),
    ("maps.ghanaein.net", "Ghana", "gh"),
    ("3.23.95.209", "Gambia", "gm"),
    ("beta.gis.cityofathens.gr", "Greece", "gr"),
    ("geoportal.ermis-f.eu", "Greece", "gr"),
    ("gis.cityofathens.gr", "Greece", "gr"),
    ("maps.msp-greece.eu", "Greece", "gr"),
    ("mapsportal.ypen.gr", "Greece", "gr"),
    ("riskdata.thessaloniki.gr", "Greece", "gr"),
    ("thalchor-2.ypen.gov.gr", "Greece", "gr"),
    ("ypendev2.cfserver3.net", "Greece", "gr"),
    ("geonode4.ine.gob.gt", "Guatemala", "gt"),
    ("geoportal.gov.gy", "Guyana", "gy"),
    ("mapas.simet.amdc.hn", "Honduras", "hn"),
    ("mapassimpro.copeco.gob.hn", "Honduras", "hn"),
    ("haitidata.org", "Haiti", "ht"),
    ("atlas.atrbpn.go.id", "Indonesia", "id"),
    ("fp2.menlhk.go.id", "Indonesia", "id"),
    ("geodata.bandung.go.id", "Indonesia", "id"),
    ("geonode.folunc-id.org", "Indonesia", "id"),
    ("geonode.nodc.id", "Indonesia", "id"),
    ("geoportal.asahankab.go.id", "Indonesia", "id"),
    ("geoportal.bantulkab.go.id", "Indonesia", "id"),
    ("geoportal.beraukab.go.id", "Indonesia", "id"),
    ("geoportal.bukittinggikota.go.id", "Indonesia", "id"),
    ("geoportal.bulukumbakab.go.id", "Indonesia", "id"),
    ("geoportal.deliserdangkab.go.id", "Indonesia", "id"),
    ("geoportal.jogjaprov.go.id", "Indonesia", "id"),
    ("geoportal.kolakakab.go.id", "Indonesia", "id"),
    ("geoportal.kolakatimurkab.go.id", "Indonesia", "id"),
    ("geoportal.kominfo.go.id", "Indonesia", "id"),
    ("geoportal.kuburayakab.go.id", "Indonesia", "id"),
    ("geoportal.kulonprogokab.go.id", "Indonesia", "id"),
    ("geoportal.langkatkab.go.id", "Indonesia", "id"),
    ("geoportal.magelangkab.go.id", "Indonesia", "id"),
    ("geoportal.magelangkota.go.id", "Indonesia", "id"),
    ("geoportal.manadokota.go.id", "Indonesia", "id"),
    ("geoportal.sanggau.go.id", "Indonesia", "id"),
    ("geoportal.slemankab.go.id", "Indonesia", "id"),
    ("geoportal.sultengprov.go.id", "Indonesia", "id"),
    ("geoportal.sumbarprov.go.id", "Indonesia", "id"),
    ("jigd.kaltimprov.go.id", "Indonesia", "id"),
    ("pisda.sukoharjokab.go.id", "Indonesia", "id"),
    ("portal.sitarung.win", "Indonesia", "id"),
    ("simtaru.papua.go.id", "Indonesia", "id"),
    ("smartgis.dishut.kaltaraprov.go.id", "Indonesia", "id"),
    ("opensdi.kerala.gov.in", "India", "in"),
    ("sdi.kmporg.ir", "Iran", "ir"),
    ("atlad.geoportale.it", "Italy", "it"),
    ("atlantedellalaguna.it", "Italy", "it"),
    ("catalog.geourba.it", "Italy", "it"),
    ("cigno.atlantedellalaguna.it", "Italy", "it"),
    ("decimetro.cittametropolitana.mi.it", "Italy", "it"),
    ("geomap.arpa.veneto.it", "Italy", "it"),
    ("geonode.nnb.isprambiente.it", "Italy", "it"),
    ("geonode.provincia.treviso.it", "Italy", "it"),
    ("geonode.supportopcveneto.it", "Italy", "it"),
    ("geoportale.regione.lazio.it", "Italy", "it"),
    ("geosdi.geodatalab.cloud", "Italy", "it"),
    ("mappe.provincia.teramo.it", "Italy", "it"),
    ("reportdu.nnb.isprambiente.it", "Italy", "it"),
    ("portal.msp.go.ke", "Kenya", "ke"),
    ("geonode.water.gov.kg", "Kyrgyzstan", "kg"),
    ("cmhl.peoplecenter.gov.kh", "Cambodia", "kh"),
    ("map.gov.kz", "Kazakhstan", "kz"),
    ("virgo.mpwt.gov.la", "Laos", "la"),
    ("mebin.nara.ac.lk", "Sri Lanka", "lk"),
    ("riverbasins.irrigation.gov.lk", "Sri Lanka", "lk"),
    ("onlinegis.cedis.me", "Montenegro", "me"),
    ("razvojgis.cedis.me", "Montenegro", "me"),
    ("resiliencemada.gov.mg", "Madagascar", "mg"),
    ("eic.mn", "Mongolia", "mn"),
    ("pfni-ce.mr", "Mauritania", "mr"),
    ("geoportal.govmu.org", "Mauritius", "mu"),
    ("masdap.mw", "Malawi", "mw"),
    ("congreso2021.iplaneg.net", "Mexico", "mx"),
    ("geoinfo.iplaneg.net", "Mexico", "mx"),
    ("geomexicali.info", "Mexico", "mx"),
    ("geonode.conabio.gob.mx", "Mexico", "mx"),
    ("geonode.idegeo.centrogeo.org.mx", "Mexico", "mx"),
    ("geonode.implancmty.geoint.mx", "Mexico", "mx"),
    ("geonode.matiko.centrogeo.org.mx", "Mexico", "mx"),
    ("geonode.milpaalta.geoint.mx", "Mexico", "mx"),
    ("geonode.sgg.geoint.mx", "Mexico", "mx"),
    ("geonode.spotmet.geoint.mx", "Mexico", "mx"),
    ("geonode.tlalpan.geoint.mx", "Mexico", "mx"),
    ("ide.sedatu.gob.mx", "Mexico", "mx"),
    ("idefor.cnf.gob.mx", "Mexico", "mx"),
    ("leonatlasriesgo.gob.mx", "Mexico", "mx"),
    ("sieg.cdmx.gob.mx", "Mexico", "mx"),
    ("sisplade.geoint.mx", "Mexico", "mx"),
    ("sqnodo.com", "Mexico", "mx"),
    ("ismp.water.gov.my", "Malaysia", "my"),
    ("mymaps.mygeoportal.gov.my", "Malaysia", "my"),
    ("madico.terrafirma.co.mz", "Mozambique", "mz"),
    ("data.nigeriase4all.gov.ng", "Nigeria", "ng"),
    ("gis.ogunstate.gov.ng", "Nigeria", "ng"),
    ("geonodo.ineter.gob.ni", "Nicaragua", "ni"),
    ("admin.nationalgeoportal.gov.np", "Nepal", "np"),
    ("database.ntb.gov.np", "Nepal", "np"),
    ("geoportal.ntnc.org.np", "Nepal", "np"),
    ("nationalgeoportal.gov.np", "Nepal", "np"),
    ("data.codc.govt.nz", "New Zealand", "nz"),
    ("data.otodc.govt.nz", "New Zealand", "nz"),
    ("data.wairoadc.govt.nz", "New Zealand", "nz"),
    ("geo-01.innovacion.gob.pa", "Panama", "pa"),
    ("luims.dlpp.gov.pg", "Papua New Guinea", "pg"),
    ("png-geoportal.org", "Papua New Guinea", "pg"),
    ("carmonagis.org", "Philippines", "ph"),
    ("crisp.r10.denr.gov.ph", "Philippines", "ph"),
    ("geonode.tagabukid.net", "Philippines", "ph"),
    ("gisportal.bukidnon.gov.ph", "Philippines", "ph"),
    ("muntinlupacity.webgis1.com", "Philippines", "ph"),
    ("rgin.rdc1.gov.ph", "Philippines", "ph"),
    ("rgin.rdc9.gov.ph", "Philippines", "ph"),
    ("geoportal.nsdi.ps", "Palestine", "ps"),
    ("sig-altotamega.pt", "Portugal", "pt"),
    ("geohidroinformatica.itaipu.gov.py", "Paraguay", "py"),
    ("geonode.ine.gov.py", "Paraguay", "py"),
    ("geoportal.paraguay.gov.py", "Paraguay", "py"),
    ("fis.upravazasume.gov.rs", "Serbia", "rs"),
    ("geohazards.rtda.gov.rw", "Rwanda", "rw"),
    ("geoportal.rwb.rw", "Rwanda", "rw"),
    ("gdzhao.gmes.cse.sn", "Senegal", "sn"),
    ("geoportalofhargeisa.org", "Somalia", "so"),
    ("geodatarisk.tg", "Togo", "tg"),
    ("sig-anpc.switch-maker.net", "Togo", "tg"),
    ("geonode.envilink.go.th", "Thailand", "th"),
    ("portal.dol.go.th", "Thailand", "th"),
    ("portal.gfms.gistda.or.th", "Thailand", "th"),
    ("portal.marineportal.gistda.or.th", "Thailand", "th"),
    ("portal2.marineportal.gistda.or.th", "Thailand", "th"),
    ("maps.wis.tj", "Tajikistan", "tj"),
    ("geonode.resilienceacademy.ac.tz", "Tanzania", "tz"),
    ("geonode.tarurapcugeodata.or.tz", "Tanzania", "tz"),
    ("geonode.nema.go.ug", "Uganda", "ug"),
    ("chocofair.dev", "United States", "us"),
    ("geonode.ggcity.org", "United States", "us"),
    ("geonode.imperialbeachca.gov", "United States", "us"),
    ("geonode.state.gov", "United States", "us"),
    ("geoplatform.spacesur.com", "United States", "us"),
    ("landscapeportal.org", "United States", "us"),
    ("mapas.alcaldiademaracaibo.org", "Venezuela (Bolivarian Repu", "ve"),
    ("congbo.dulieuvientham.gov.vn", "Vietnam", "vn"),
    ("opendata.hcmgis.vn", "Vietnam", "vn"),
    ("portal.hcmgis.vn", "Vietnam", "vn"),
    ("geonode.gov.vu", "Vanuatu", "vu"),
    ("181.171.117.68", "World", "world"),
    ("190.112.43.34", "World", "world"),
    ("196.45.37.197", "World", "world"),
    ("seagrass.observing.earth", "World", "world"),
    ("sirei.pariis.net", "World", "world"),
]
_GEONODE_TERMS = ["construction", "development", "permit", "mining", "mine", "quarry",
                  "pipeline", "concession", "infrastructure", "land use", "environmental",
                  "obra", "proyecto", "mineria", "licencia", "concesion",
                  "licenciamento", "empreendimento", "amenagement", "permis",
                  "izin", "tambang", "pembangunan", "amdal",
                  "cava", "edilizia", "bergbau", "bauleitplan",
                  "\u03ac\u03b4\u03b5\u03b9\u03b1", "\u03bc\u03b5\u03bb\u03ad\u03c4\u03b7"]

def fetch_geonode_federation(per_portal=None, per_ds=800):
    per_portal = per_portal or (10 if os.environ.get("HARVEST_FEDERATIONS") == "1" else 6)
    out = []
    budget_min = _fed_budget("GEONODE_BUDGET_MIN", 30, 40)
    t_end = time.time() + budget_min * 60
    _gn_portals = _shard_list(_GEONODE_PORTALS)
    for (host, country, cc) in _gn_portals:
        if time.time() > t_end:
            _flag("geonode federation hit %d-min budget -- %d portals not reached" %
                  (budget_min, len(_gn_portals) - _gn_portals.index((host, country, cc))))
            break
        layers = []; seen = set()
        for term in _GEONODE_TERMS:
            try:
                u = ("https://%s/api/v2/resources/?page_size=%d&filter{title.icontains}=%s"
                     % (host, 20, urllib.parse.quote(term)))
                d = _get_json(u)
            except Exception:
                continue
            items = (d or {}).get("resources") or (d or {}).get("datasets")                     or (d or {}).get("data") or []
            for it in items:
                if str(it.get("subtype", "")).lower() not in ("vector", "tabular"):
                    continue
                alt = it.get("alternate")
                if not alt or alt in seen:
                    continue
                wfs = None
                for ln in (it.get("links") or []):
                    if str(ln.get("link_type", "")).upper() == "OGC:WFS" and ln.get("url"):
                        wfs = ln["url"]; break
                wfs = wfs or ("https://%s/geoserver/wfs" % host)
                seen.add(alt); layers.append((alt, wfs, str(it.get("title") or alt)))
            time.sleep(0.25)
            if len(layers) >= per_portal:
                break
        for alt, wfs, title in layers[:per_portal]:
            try:
                sep = "&" if "?" in wfs else "?"
                gu = wfs + sep + urllib.parse.urlencode(
                    {"service": "WFS", "version": "1.1.0", "request": "GetFeature",
                     "typeName": alt, "outputFormat": "application/json", "maxFeatures": per_ds})
                gj = _get_json(gu)
            except Exception:
                continue
            feats = gj.get("features") if isinstance(gj, dict) else None
            if not feats:
                continue
            n0 = len(out)
            for f in feats[:per_ds]:
                try:
                    ll = _geom_center(f.get("geometry") or {})
                    if not ll:
                        continue
                    props = f.get("properties") or {}
                    st = _ods_pick(props, _ODS_STATUSK)
                    sn = str(st or "").lower().replace("_", " ")
                    if any(k in sn for k in _ODS_DEAD):
                        continue
                    nm = _ods_pick(props, _ODS_NAMEK) or title
                    p = {"name": nm[:140], "type": "Geospatial dataset (%s)" % country,
                         "state": country, "lat": round(ll[0], 5), "lng": round(ll[1], 5),
                         "precise": True, "size": "", "status": st[:40], "company": "",
                         "url": "https://%s" % host,
                         "desc": "From %s spatial data (GeoNode) \u00b7 %s." % (country, title[:60]),
                         "source": "geonode_%s" % cc}
                    p["impact"] = rate_project(p, sensitivity=0)
                    out.append(p)
                except Exception:
                    continue
            if len(out) > n0:
                print("  geonode %s: +%d from '%s'" % (country, len(out) - n0, title[:40]))
            time.sleep(0.25)
    print("  geonode federation: %d points from %d portals" % (len(out), len(_GEONODE_PORTALS)))
    return out


# =============================== DKAN federation ===============================
# DKAN ships two API generations (verified against dkan.readthedocs.io):
#   * DKAN 7.x  -> CKAN-compatible: GET /api/3/action/package_search?q=&rows=
#   * DKAN 2.x  -> GET /api/1/metastore/schemas/dataset/items  (dataset list)
#                 POST /api/1/datastore/query/{id}/0  {"limit":N}  (tabular rows)
# One fetcher tries both per host (read-only, anonymous). Geo sniffed from geojson
# resources (7.x) or lat/lng columns (2.x). Terminal states dropped (_ODS_DEAD).
_DKAN_PORTALS = [
    ("data.abudhabi", "United Arab Emirates", "ae"),
    ("datosabiertos.rosario.gob.ar", "Argentina", "ar"),
    ("americansamoa-data.sprep.org", "Samoa", "as"),
    ("data.gov.bd", "Bangladesh", "bd"),
    ("fair.healthdata.be", "Belgium", "be"),
    ("dados.educacao.sp.gov.br", "Brazil", "br"),
    ("open.yukon.ca", "Canada", "ca"),
    ("cookislands-data.sprep.org", "Cook Islands", "ck"),
    ("datosabiertos.inec.cr", "Costa Rica", "cr"),
    ("opendata.heredia.go.cr", "Costa Rica", "cr"),
    ("data.army.cz", "Czech Republic", "cz"),
    ("data.ctu.cz", "Czech Republic", "cz"),
    ("kod.opava-city.cz", "Czech Republic", "cz"),
    ("daten.diepholz.de", "Germany", "de"),
    ("geo.muelheim-ruhr.de", "Germany", "de"),
    ("offene-daten-mse.de", "Germany", "de"),
    ("offenedaten-koeln.de", "Germany", "de"),
    ("offenedaten-konstanz.de", "Germany", "de"),
    ("offenedaten-owl.de", "Germany", "de"),
    ("offenedaten-wuppertal.de", "Germany", "de"),
    ("offenedaten.duesseldorf.de", "Germany", "de"),
    ("offenedaten.kdvz-frechen.de", "Germany", "de"),
    ("offenedaten.kdvz.nrw", "Germany", "de"),
    ("open-data.bielefeld.de", "Germany", "de"),
    ("opendata-duisburg.de", "Germany", "de"),
    ("opendata.bonn.de", "Germany", "de"),
    ("opendata.braunschweig.de", "Germany", "de"),
    ("opendata.duesseldorf.de", "Germany", "de"),
    ("opendata.essen.de", "Germany", "de"),
    ("opendata.gelsenkirchen.de", "Germany", "de"),
    ("opendata.heilbronn.de", "Germany", "de"),
    ("opendata.oldenburg.de", "Germany", "de"),
    ("opendata.stadt-muenster.de", "Germany", "de"),
    ("dadesobertes.diba.cat", "Spain", "es"),
    ("datos.cadiz.es", "Spain", "es"),
    ("datosabiertos.alicante.es", "Spain", "es"),
    ("datosabiertos.castillalamancha.es", "Spain", "es"),
    ("datosabiertos.ctpdandalucia.es", "Spain", "es"),
    ("opendata.turismoconil.es", "Spain", "es"),
    ("opendatahubs.eu", "European Union", "eu"),
    ("fsm-data.sprep.org", "Micronesia", "fm"),
    ("data.montpellier3m.fr", "France", "fr"),
    ("data.cambridgeshireinsight.org.uk", "United Kingdom", "gb"),
    ("data.marine.gov.scot", "United Kingdom", "gb"),
    ("data.gov.gh", "Ghana", "gh"),
    ("catalog.hcapdata.gr", "Greece", "gr"),
    ("data.apdkritis.gov.gr", "Greece", "gr"),
    ("data.ktimatologio.gr", "Greece", "gr"),
    ("data.trikalacity.gr", "Greece", "gr"),
    ("opencrete.gov.gr", "Greece", "gr"),
    ("opendata.thessaloniki.gr", "Greece", "gr"),
    ("data.grad-krk.hr", "Croatia", "hr"),
    ("data.lahatkab.go.id", "Indonesia", "id"),
    ("data.lomboktimurkab.go.id", "Indonesia", "id"),
    ("data.manggaraibaratkab.go.id", "Indonesia", "id"),
    ("data.ntbprov.go.id", "Indonesia", "id"),
    ("opendata.lampungprov.go.id", "Indonesia", "id"),
    ("opendata.tangerangkab.go.id", "Indonesia", "id"),
    ("pusdataru.jatengprov.go.id", "Indonesia", "id"),
    ("satudata.dinkes.riau.go.id", "Indonesia", "id"),
    ("data.telangana.gov.in", "India", "in"),
    ("geoportal.natmo.gov.in", "India", "in"),
    ("ssdi.jk.gov.in", "India", "in"),
    ("dati.genovametropoli.it", "Italy", "it"),
    ("opendata.cittametropolitanaroma.it", "Italy", "it"),
    ("opendata.comune.parma.it", "Italy", "it"),
    ("opengolfo.it", "Italy", "it"),
    ("data.gov.jm", "Jamaica", "jm"),
    ("data.city.kyoto.lg.jp", "Japan", "jp"),
    ("kiribati-data.sprep.org", "Kiribati", "ki"),
    ("data.gov.lk", "Sri Lanka", "lk"),
    ("rmi-data.sprep.org", "Republic of Marshall Islan", "mh"),
    ("data.govmu.org", "Mauritius", "mu"),
    ("datos.imss.gob.mx", "Mexico", "mx"),
    ("datos.pueblacapital.gob.mx", "Mexico", "mx"),
    ("datos.zapopan.gob.mx", "Mexico", "mx"),
    ("datosabiertos.cholula.gob.mx", "Mexico", "mx"),
    ("newcaledonia-data.sprep.org", "New Caledonia", "nc"),
    ("nauru-data.sprep.org", "Nauru", "nr"),
    ("niue-data.sprep.org", "Niue", "nu"),
    ("pacific-data.sprep.org", "Oceania", "oceania"),
    ("datosabiertos.regioncajamarca.gob.pe", "Peru", "pe"),
    ("png-data.sprep.org", "Papua New Guinea", "pg"),
    ("opendata.sarai.ph", "Philippines", "ph"),
    ("e-uslugi.mazowieckie.pl", "Poland", "pl"),
    ("epibaza.pzh.gov.pl", "Poland", "pl"),
    ("omat.cm-loule.pt", "Portugal", "pt"),
    ("palau-data.sprep.org", "Palau", "pw"),
    ("opendata.yarcloud.ru", "Russian Federation", "ru"),
    ("solomonislands-data.sprep.org", "Solomon Islands", "sb"),
    ("tonga-data.sprep.org", "Tonga", "to"),
    ("tuvalu-data.sprep.org", "Tuvalu", "tv"),
    ("opendata.kalushcity.gov.ua", "Ukraine", "ua"),
    ("data.georgia.gov", "United States", "us"),
    ("data.medicaid.gov", "United States", "us"),
    ("data.nal.usda.gov", "United States", "us"),
    ("open.obamawhitehouse.archives.gov", "United States", "us"),
    ("rdx.stldata.org", "United States", "us"),
    ("rtams.org", "United States", "us"),
    ("datos.gob.ve", "Venezuela (Bolivarian Repu", "ve"),
    ("opendata.hochiminhcity.gov.vn", "Vietnam", "vn"),
    ("vanuatu-data.sprep.org", "Vanuatu", "vu"),
    ("rio-samoa.mnre.gov.ws", "Samoa", "ws"),
    ("samoa-data.sprep.org", "Samoa", "ws"),
]

def fetch_dkan_federation(per_portal=None, per_ds=800):
    per_portal = per_portal or (10 if os.environ.get("HARVEST_FEDERATIONS") == "1" else 6)
    out = []
    budget_min = _fed_budget("DKAN_BUDGET_MIN", 25, 25)
    t_end = time.time() + budget_min * 60
    _dk_portals = _shard_list(_DKAN_PORTALS)
    for (host, country, cc) in _dk_portals:
        if time.time() > t_end:
            _flag("dkan federation hit %d-min budget -- %d portals not reached" %
                  (budget_min, len(_dk_portals) - _dk_portals.index((host, country, cc))))
            break
        base = "https://%s" % host
        n_before = len(out)
        # -- path A: CKAN-compatible (DKAN 7.x) --
        pkgs = []; seen = set()
        for term in _CKAN_TERMS[:20]:
            try:
                u = base + "/api/3/action/package_search?" + urllib.parse.urlencode({"q": term, "rows": 100})
                d = _get_json(u)
            except Exception:
                continue
            for pk in (((d or {}).get("result") or {}).get("results") or []):
                nm = str(pk.get("title") or pk.get("name") or "")
                if pk.get("id") in seen or not _CKAN_TITLE_RE.search(nm): continue
                seen.add(pk.get("id")); pkgs.append(pk)
            time.sleep(0.2)
            if len(pkgs) >= per_portal: break
        got = 0
        for pk in pkgs[:per_portal]:
            geo = [r for r in (pk.get("resources") or [])
                   if str(r.get("format", "")).lower() in ("geojson", "json") and r.get("url")]
            if not geo:                                   # CSV-only portal -> sniff lat/lng columns
                csvs = [r for r in (pk.get("resources") or [])
                        if str(r.get("format", "")).lower() == "csv" and r.get("url")]
                for r in csvs[:1]:
                    rows_csv = _fed_csv_points(r["url"], country, cc, "dkan_",
                                               str(pk.get("title") or "Permit"), base, per_ds)
                    if rows_csv:
                        got += 1; out.extend(rows_csv)
                        print("  dkan %s: +%d from CSV '%s'" % (country, len(rows_csv),
                                                                str(pk.get("title"))[:40]))
                    time.sleep(0.3)
            for r in geo[:1]:
                try:
                    gj = _get_json(r["url"])
                except Exception:
                    continue
                for f in (gj.get("features") if isinstance(gj, dict) else []) or []:
                    try:
                        ll = _geom_center(f.get("geometry") or {})
                        if not ll: continue
                        props = f.get("properties") or {}
                        st = _ods_pick(props, _ODS_STATUSK); sn = str(st or "").lower().replace("_", " ")
                        if any(k in sn for k in _ODS_DEAD): continue
                        nm = _ods_pick(props, _ODS_NAMEK) or str(pk.get("title") or "Permit")
                        p = {"name": nm[:140], "type": "Permit / development (%s)" % country,
                             "state": country, "lat": round(ll[0], 5), "lng": round(ll[1], 5),
                             "precise": True, "size": "", "status": st[:40], "company": "",
                             "url": base, "desc": "From %s open data (DKAN) \u00b7 %s." %
                             (country, str(pk.get("title") or "")[:60]), "source": "dkan_%s" % cc}
                        p["impact"] = rate_project(p, sensitivity=0)
                        out.append(p)
                    except Exception:
                        continue
                time.sleep(0.2)
        # -- path B: DKAN 2.x metastore + datastore (only if A found nothing) --
        if len(out) == n_before:
            try:
                items = _get_json(base + "/api/1/metastore/schemas/dataset/items?show-reference-ids")
            except Exception:
                items = None
            hits = []
            for it in (items or []):
                if not isinstance(it, dict): continue
                nm = str(it.get("title") or "")
                if _CKAN_TITLE_RE.search(nm) and it.get("identifier"):
                    hits.append((it.get("identifier"), nm))
                if len(hits) >= per_portal: break
            for did, title in hits:
                try:
                    body = json.dumps({"limit": min(per_ds, 500)}).encode("utf-8")
                    req = urllib.request.Request(base + "/api/1/datastore/query/%s/0" % did,
                                                 data=body, method="POST",
                                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        qd = json.loads(resp.read().decode("utf-8", "replace"))
                except Exception:
                    continue
                for row in ((qd or {}).get("results") or []):
                    try:
                        if not isinstance(row, dict): continue
                        ll = _ods_latlng(row)
                        if not ll: continue
                        st = _ods_pick(row, _ODS_STATUSK); sn = str(st or "").lower().replace("_", " ")
                        if any(k in sn for k in _ODS_DEAD): continue
                        nm = _ods_pick(row, _ODS_NAMEK) or title
                        p = {"name": nm[:140], "type": "Permit / development (%s)" % country,
                             "state": country, "lat": round(ll[0], 5), "lng": round(ll[1], 5),
                             "precise": True, "size": "", "status": st[:40], "company": "",
                             "url": base, "desc": "From %s open data (DKAN) \u00b7 %s." %
                             (country, title[:60]), "source": "dkan_%s" % cc}
                        p["impact"] = rate_project(p, sensitivity=0)
                        out.append(p)
                    except Exception:
                        continue
                time.sleep(0.2)
        if len(out) > n_before:
            print("  dkan %s (%s): +%d" % (country, host, len(out) - n_before))
    print("  dkan federation: %d points from %d portals" % (len(out), len(_DKAN_PORTALS)))
    return out


# =============================== uData federation ===============================
# uData API v1 (verified against guides.data.gouv.fr): GET /api/1/datasets/?q=&page_size=
#   -> {"data":[{"resources":[{"format":"geojson","url":..}], "title":..}]}  (open, no key).
# data.gouv.fr is the flagship (Sitadel pulls only ONE dataset from it; this searches broadly).
_UDATA_PORTALS = [
    ("www.data.gouv.fr", "France", "fr"),
    ("dados.gov.pt", "Portugal", "pt"),
]

def _fed_csv_points(url, country, cc, src_prefix, ds_title, base_url, per_ds=800):
    """Fallback for CSV-only open-data portals: download one CSV (size-capped),
    BOM-detect encoding (UTF-16 goget lesson), sniff the delimiter, then reuse the
    shared lat/lng + name/status sniffers row by row."""
    import csv as _csv, io as _io
    out = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read(15000000)
    except Exception:
        return out
    enc = "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"): enc = "utf-16"
    try:
        text = raw.decode(enc, "replace")
    except Exception:
        return out
    head = text[:4000]
    delim = ";" if head.count(";") > head.count(",") else ","
    if head.count("\t") > max(head.count(","), head.count(";")): delim = "\t"
    try:
        rdr = _csv.DictReader(_io.StringIO(text), delimiter=delim)
        for i, row in enumerate(rdr):
            if i >= per_ds: break
            if not isinstance(row, dict): continue
            row = {str(k or ""): v for k, v in row.items()}
            ll = _ods_latlng(row)
            if not ll: continue
            la, lo = ll
            if not (-90 <= la <= 90 and -180 <= lo <= 180): continue
            st = _ods_pick(row, _ODS_STATUSK); sn = str(st or "").lower().replace("_", " ")
            if any(k in sn for k in _ODS_DEAD): continue
            nm = _ods_pick(row, _ODS_NAMEK) or ds_title
            p = {"name": nm[:140], "type": "Permit / development (%s)" % country,
                 "state": country, "lat": round(la, 5), "lng": round(lo, 5),
                 "precise": True, "size": "", "status": str(st or "")[:40], "company": "",
                 "url": base_url, "desc": "From %s open data (CSV) \u00b7 %s." % (country, ds_title[:60]),
                 "source": "%s%s" % (src_prefix, cc)}
            p["impact"] = rate_project(p, sensitivity=0)
            out.append(p)
    except Exception:
        pass
    return out

def fetch_udata_federation(per_portal=8, per_ds=800):
    out = []
    for (host, country, cc) in _UDATA_PORTALS:
        seen = set(); dss = []
        for term in _CKAN_TERMS[:16]:
            try:
                u = "https://%s/api/1/datasets/?%s" % (
                    host, urllib.parse.urlencode({"q": term, "page_size": 20}))
                d = _get_json(u)
            except Exception:
                continue
            for ds in ((d or {}).get("data") or []):
                did = ds.get("id") or ds.get("slug")
                if not did or did in seen: continue
                if not _CKAN_TITLE_RE.search(str(ds.get("title") or "")): continue
                seen.add(did); dss.append(ds)
            time.sleep(0.25)
            if len(dss) >= per_portal: break
        for ds in dss[:per_portal]:
            geo = [r for r in (ds.get("resources") or [])
                   if str(r.get("format", "")).lower() in ("geojson", "json") and r.get("url")]
            if not geo:                                   # CSV-only dataset -> sniff lat/lng columns
                csvs = [r for r in (ds.get("resources") or [])
                        if str(r.get("format", "")).lower() == "csv" and r.get("url")]
                for r in csvs[:1]:
                    rows_csv = _fed_csv_points(r["url"], country, cc, "udata_",
                                               str(ds.get("title") or "Permit"),
                                               "https://%s" % host, per_ds)
                    if rows_csv:
                        out.extend(rows_csv)
                        print("  udata %s: +%d from CSV '%s'" % (country, len(rows_csv),
                                                                 str(ds.get("title"))[:40]))
                    time.sleep(0.25)
            for r in geo[:1]:
                try:
                    gj = _get_json(r["url"])
                except Exception:
                    continue
                n0 = len(out)
                for f in (gj.get("features") if isinstance(gj, dict) else []) or []:
                    try:
                        ll = _geom_center(f.get("geometry") or {})
                        if not ll: continue
                        props = f.get("properties") or {}
                        st = _ods_pick(props, _ODS_STATUSK); sn = str(st or "").lower().replace("_", " ")
                        if any(k in sn for k in _ODS_DEAD): continue
                        nm = _ods_pick(props, _ODS_NAMEK) or str(ds.get("title") or "Permit")
                        p = {"name": nm[:140], "type": "Permit / development (%s)" % country,
                             "state": country, "lat": round(ll[0], 5), "lng": round(ll[1], 5),
                             "precise": True, "size": "", "status": st[:40], "company": "",
                             "url": "https://%s" % host,
                             "desc": "From %s open data (uData) \u00b7 %s." % (country, str(ds.get("title") or "")[:60]),
                             "source": "udata_%s" % cc}
                        p["impact"] = rate_project(p, sensitivity=0)
                        out.append(p)
                    except Exception:
                        continue
                if len(out) > n0:
                    print("  udata %s: +%d from '%s'" % (country, len(out) - n0, str(ds.get("title"))[:40]))
                time.sleep(0.25)
    print("  udata federation: %d points from %d portals" % (len(out), len(_UDATA_PORTALS)))
    return out


# ============================ WFS / GeoServer federation ============================
# Generic OGC WFS federation over government GeoServer / WFS endpoints mined from the
# dataportals registry (software=geoserver OR endpoint type wfs*). Per endpoint:
#   GetCapabilities (XML) -> FeatureType Name/Title -> keep dev/permit/mining/EIA layers
#   GetFeature outputFormat=application/json (GeoJSON) -> features -> sniff -> emit.
# Namespace-agnostic XML parse; size/parse/timeout-guarded; terminal states dropped.
# Layer scope is coarse (some layers are inventories/basemaps) -> FIRST-RUN REVIEW.
import xml.etree.ElementTree as _ET
_WFS_ENDPOINTS = [
    # EU-wide aggregator (verified live): offshore wind farms (status incl Planned/Approved/
    # Construction), oil & gas licences, pipelines across 18 European countries
    ("https://ows.emodnet-humanactivities.eu/wfs", "EU waters", "eu"),
    ("https://www.ideandorra.ad/Serveis/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Andorra", "ad"),
    ("https://nafcoast.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Africa", "africa"),
    ("https://climateriskmap.environment.gov.ag/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Antigua and Barbuda", "ag"),
    ("https://nri.environment.gov.ag/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Antigua and Barbuda", "ag"),
    ("http://aicsig.neuquen.gov.ar:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://atmgis.mendoza.gov.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://g.geosplan.tucuman.gob.ar:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://g.geosplan.tucuman.gov.ar:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://geo2.ambiente.gob.ar/geoserver/ows", "Argentina", "ar"),
    ("http://geoeducacion.neuquen.gov.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://geointa.inta.gov.ar/geoserver/ows", "Argentina", "ar"),
    ("http://geoportal.idesa.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://geosalud.neuquen.gov.ar:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://gisestadisticanqn.neuquen.gov.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://ide.sedronar.gov.ar/geoserver/ows", "Argentina", "ar"),
    ("http://ideneu.neuquen.gov.ar:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://idet.tucuman.gob.ar:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://municipiosig.rionegro.gov.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://rechidr.neuquen.gov.ar:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://redvial.neuquen.gov.ar:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://sigdefensacivil.neuquen.gov.ar:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://www.coronelsuarezgis.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://www.siat.mendoza.gov.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://alerta.ina.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://datos.inidep.edu.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://estadisticasig.rionegro.gov.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geo.ambiente.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geo.gualeguaychu.gov.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geo.test.arba.gov.ar/geoserver/ows", "Argentina", "ar"),
    ("https://geoadmin.magyp.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geonode.pergamino.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geonode.senasa.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoportal.cfi.org.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoportal.lujandecuyo.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoportal.obraspublicas.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoportal.salta.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoportal.tresdefebrero.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoportalqa.lujandecuyo.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoserver-nodo2.ideba.gba.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoserver.agroindustria.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoserver.sanbenito.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoservicios.conae.gov.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://geoservicios.indec.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://hidrocarburos.energianeuquen.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://ide-enacom.arsat.com.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://ide.correoargentino.com.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://ide.pergamino.gob.ar:8443/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://ide.santarosamendoza.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://ide.transporte.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://idern.rionegro.gov.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://imagenes.ign.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://manejodelfuego.conae.gov.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://mapa.educacion.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://mapas.geomatica.idr.org.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://mapas.sancarlos.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://nodo.cfi.org.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://oat.ambiente.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://poblacion.idear.gov.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://riesgo.ign.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://saludsig.rionegro.gov.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://sigam.segemar.gov.ar/geoserver/ows", "Argentina", "ar"),
    ("https://sigvial.dpvmisiones.gob.ar/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://wms.ign.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("https://www.ide.posadas.gob.ar/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Argentina", "ar"),
    ("http://geo.noe.gv.at/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Austria", "at"),
    ("https://geo.kaerntennetz.at/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Austria", "at"),
    ("https://geogis.ages.at:443/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Austria", "at"),
    ("https://geonode.lebensraumvernetzung.at/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Austria", "at"),
    ("https://gis.geologie.ac.at/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Austria", "at"),
    ("https://sdigeo-free.austrocontrol.at/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Austria", "at"),
    ("http://data.daff.gov.au:8080/geoserver/ows", "Australia", "au"),
    ("http://gaservices.ga.gov.au:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("http://geoserver.dea.ga.gov.au/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("http://nhirs.ga.gov.au/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("http://services.aad.gov.au/geoserver/ows", "Australia", "au"),
    ("http://services.ga.gov.au/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("https://auth2.dbca.wa.gov.au/geoserver/ows", "Australia", "au"),
    ("https://geology.data.nt.gov.au/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("https://geology.information.qld.gov.au/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("https://geoserver.tern.org.au/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("https://gs-mv-dev.geoscience.nsw.gov.au/geoserver/ows", "Australia", "au"),
    ("https://gs-mv.geoscience.nsw.gov.au/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("https://gs-seamless.geoscience.nsw.gov.au/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("https://gs.geoscience.nsw.gov.au/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("https://kmi.dpaw.wa.gov.au/geoserver/ows", "Australia", "au"),
    ("https://opendata.maps.vic.gov.au/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("https://programs.communications.gov.au/geoserver/ows", "Australia", "au"),
    ("https://sarigdata.pir.sa.gov.au/nvcl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Australia", "au"),
    ("http://202.4.179.152/geoserver/wfs", "Bangladesh", "bd"),
    ("http://geoportal.bforest.gov.bd/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bangladesh", "bd"),
    ("https://gis.nesco.gov.bd/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bangladesh", "bd"),
    ("https://nsdi.gov.bd/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bangladesh", "bd"),
    ("http://data-mobility.irisnet.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("http://geoserver.gis.irisnet.be/geoserver/ows", "Belgium", "be"),
    ("http://geoservices-inspire.irisnet.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("http://geoservices-urbis.irisnet.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("http://www.dov.vlaanderen.be:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://bathgrid.vlaanderen.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://data.mobility.brussels/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://geo.api.vlaanderen.be/GRB/wfs?service=WFS&version=2.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://geo.onroerenderfgoed.be:443/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://geodata.toerismevlaanderen.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://geoserver.gis.cloud.mow.vlaanderen.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://geoserver.vmm.be/geoserver/wfs?service=WFS&version=2.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://geoservices-others.irisnet.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://geoservices.valid.wallonie.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://geoservices.wallonie.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://gis.urban.brussels/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://klimaat.vmm.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://mister.vlaanderen.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("https://mybrugis.irisnet.be/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belgium", "be"),
    ("http://www.bumigeb.bf/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Burkina Faso", "bf"),
    ("https://geoportail.bumigeb.bf/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Burkina Faso", "bf"),
    ("http://46.165.252.159/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bulgaria", "bg"),
    ("http://84.16.227.175/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bulgaria", "bg"),
    ("http://inspire.mzh.government.bg:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Bulgaria", "bg"),
    ("https://georisk.gouv.bj/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Benin", "bj"),
    ("https://sig.sineb.bj/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Benin", "bj"),
    ("http://geo.siarh.gob.bo/geoserver/ows", "Bolivia", "bo"),
    ("http://geo03.siarh.gob.bo/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bolivia", "bo"),
    ("http://geodata.oopp.gob.bo/geoserver/ows", "Bolivia", "bo"),
    ("http://geosinager.defensacivil.gob.bo/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Bolivia", "bo"),
    ("http://geosirh.riegobolivia.org:80/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bolivia", "bo"),
    ("http://geosunit.vicetierras.gob.bo/geoserver/ows", "Bolivia", "bo"),
    ("http://maps.abe.bo:8081/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Bolivia", "bo"),
    ("http://siged.ine.gob.bo/geoserver/ows", "Bolivia", "bo"),
    ("http://sigvmeea.minenergias.gob.bo/geoserver/ows", "Bolivia", "bo"),
    ("http://siip.produccion.gob.bo:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Bolivia", "bo"),
    ("http://sitservicios.lapaz.bo/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Bolivia", "bo"),
    ("https://aetn.geo.gob.bo/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bolivia", "bo"),
    ("https://dgf.geo.gob.bo/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bolivia", "bo"),
    ("https://geo.inra.gob.bo/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Bolivia", "bo"),
    ("https://geonode.fonadin.gob.bo/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bolivia", "bo"),
    ("https://geoportal.mhe.gob.bo/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Bolivia", "bo"),
    ("https://ipdsa.geo.gob.bo/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bolivia", "bo"),
    ("http://geoinfo.cnps.embrapa.br/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("http://geoserver.sobral.ce.gov.br/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("http://raster.geosampa.prefeitura.sp.gov.br:80/geoserver/ows", "Brazil", "br"),
    ("http://sig.amvali.org.br/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Brazil", "br"),
    ("http://siscom.ibama.gov.br/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("http://wfs.geosampa.prefeitura.sp.gov.br:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("http://wms.geosampa.prefeitura.sp.gov.br:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("http://wms.geosampa.prodam/geoserver/ows", "Brazil", "br"),
    ("http://www.geoservicos.ibge.gov.br/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("https://arvorezinha.geomunicipios.com/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Brazil", "br"),
    ("https://catalogo.ipe.df.gov.br/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Brazil", "br"),
    ("https://dados.it-amazonia.dev/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Brazil", "br"),
    ("https://geoaisweb.decea.mil.br/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("https://geodados.daee.sp.gov.br/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Brazil", "br"),
    ("https://geoserver.car.gov.br/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("https://geoserver.funai.gov.br/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("https://geoserver.meioambiente.mg.gov.br/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("https://geoserver.praiagrande.sp.gov.br/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("https://ibram.df.gov.br/geoserver/ows", "Brazil", "br"),
    ("https://ide.geobases.es.gov.br/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("https://imde.portoalegre.rs.gov.br/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Brazil", "br"),
    ("https://siga.meioambiente.go.gov.br/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Brazil", "br"),
    ("https://sigahomol.meioambiente.go.gov.br/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Brazil", "br"),
    ("https://sistemas.florestal.gov.br/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Brazil", "br"),
    ("https://www.brasiliaambiental.df.gov.br/geoserver/ows", "Brazil", "br"),
    ("https://cgi-gn.nlcs.gov.bt/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Bhutan", "bt"),
    ("https://meta.geo.by/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Belarus", "by"),
    ("http://geogratis.gc.ca:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Canada", "ca"),
    ("https://canada3d.geosciences.ca/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Canada", "ca"),
    ("https://data.chs-shc.ca/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Canada", "ca"),
    ("https://geo.lachute.ca/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Canada", "ca"),
    ("https://geonode.doigriverfn.com/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Canada", "ca"),
    ("https://gis.crgl.ca/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Canada", "ca"),
    ("https://nonna-geoserver.data.chs-shc.ca/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Canada", "ca"),
    ("https://servicesvecto3.mern.gouv.qc.ca/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Canada", "ca"),
    ("https://www.marinfo.gc.ca/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Canada", "ca"),
    ("https://sdi.georhena.eu/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Switzerland", "ch"),
    ("http://anidlimarichoapa.ciren.cl/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Chile", "cl"),
    ("http://apibotanico.ciren.cl/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Chile", "cl"),
    ("http://idemagallanes.cl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Chile", "cl"),
    ("http://inventarioerosion.ciren.cl/sitemap.xml/wfs", "Chile", "cl"),
    ("http://www.geoportal.cl/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Chile", "cl"),
    ("https://geonode.meteochile.gob.cl/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Chile", "cl"),
    ("https://geoserver.exploradorenergia.cl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Chile", "cl"),
    ("https://geoserver.infor.cl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Chile", "cl"),
    ("https://ide.emprendequillota.cl/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Chile", "cl"),
    ("https://geo.snh.cm/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Cameroon", "cm"),
    ("http://ws-idesc.cali.gov.co:8081/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Colombia", "co"),
    ("https://sig.cormacarena.gov.co/geoserver/ows", "Colombia", "co"),
    ("http://catastro.munisc.go.cr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Costa Rica", "cr"),
    ("https://geonode.bgeoinorca.com/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Costa Rica", "cr"),
    ("https://geoportal-saracc.cne.go.cr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Costa Rica", "cr"),
    ("https://gestorgeo.inec.go.cr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Costa Rica", "cr"),
    ("https://ide.santaana.go.cr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Costa Rica", "cr"),
    ("https://ideonion.go.cr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Costa Rica", "cr"),
    ("https://idesca.munisc.go.cr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Costa Rica", "cr"),
    ("https://metadatos.ideonion.go.cr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Costa Rica", "cr"),
    ("https://raster.munisc.go.cr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Costa Rica", "cr"),
    ("https://sae.inec.go.cr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Costa Rica", "cr"),
    ("https://idema.geotech.cu/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Cuba", "cu"),
    ("https://gis.cenia.cz/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Czech Republic", "cz"),
    ("https://jsdi01.secar.cz/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Czech Republic", "cz"),
    ("https://opgis.slavicin.unart.cz/geoserver/ows", "Czech Republic", "cz"),
    ("http://185.17.146.157/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Germany", "de"),
    ("http://WFS-Kataster.fuerstenwalde-spree.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("http://geodaten.metropoleruhr.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("http://geoportal.birkenwerder.de:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("http://geoportal.eberswalde.de:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("http://geoportal.kreis-lup.de:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("http://mdi.niedersachsen.de:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("http://weinlagen.lwk-rlp.de:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("http://wms.fis-wasser-mv.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("http://www.brandenburg-forst.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://api.viz.berlin.de/geoserver/ows", "Germany", "de"),
    ("https://breitband-in-sh.de/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Germany", "de"),
    ("https://bsis.aachen.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://cdc.dwd.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://elsterwerda.gajamatrix.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://geo-katalog.julius-kuehn.de/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Germany", "de"),
    ("https://geodaten.herne.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://geoportal.giz.de/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Germany", "de"),
    ("https://geoportal.muenchen.de:443/geoserver/ows", "Germany", "de"),
    ("https://geoportal.stadt.wolfsburg.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://geoserver.digitale-mrn.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://geoserver.geonet-mrn.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://geoserver.stuttgart.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://gis.amberg-sulzbach.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://gis.kultus-bw.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://gis.planungsregion-abw.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://giz.sta.hz.kartoza.com/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Germany", "de"),
    ("https://maps.dwd.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://mdi-de-dienste.org/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://milton-geo.thw-fts.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Germany", "de"),
    ("https://nng.riwagis.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://plis-bb.de/plisproject/inspire/index.php/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://rudolstadt.gajamatrix.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://sla.niedersachsen.de/ml-geoportal/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://stadtplan.weimar.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://webgis.regionalverband-braunschweig.de/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Germany", "de"),
    ("https://www.pegelmobil.de/geoserver/ows", "Germany", "de"),
    ("https://www.waldgeoportal.de/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Germany", "de"),
    ("http://fiskeriservice.fiskeristyrelsen.dk/geoserver/ows", "Denmark", "dk"),
    ("http://geodata.fvm.dk/geoserver/ows", "Denmark", "dk"),
    ("http://geoserver.surfacewater.miljoeportal.dk/geoserver/ows", "Denmark", "dk"),
    ("http://jordbrugsanalyser.dk/geoserver/ows", "Denmark", "dk"),
    ("http://vmgeoserver.vd.dk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Denmark", "dk"),
    ("http://webkort.silkeborg.dk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Denmark", "dk"),
    ("http://wfs.plansystem.dk/geoserver/ows", "Denmark", "dk"),
    ("https://geoserver.plandata.dk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Denmark", "dk"),
    ("https://havplan.dk/geoserver/ows", "Denmark", "dk"),
    ("https://kortdata.fvm.dk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Denmark", "dk"),
    ("https://dominode.dm/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Dominica", "dm"),
    ("http://ozf.economia.gob.do/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Republica Dominicana", "do"),
    ("https://cartografia.ayuntamientosantiago.gob.do/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Republica Dominicana", "do"),
    ("https://geoportal.catastro.gob.do/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Republica Dominicana", "do"),
    ("https://geoportal.iderd.gob.do/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Republica Dominicana", "do"),
    ("https://geoportal.pedepe.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Republica Dominicana", "do"),
    ("https://inventariovial.mopc.gob.do/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Republica Dominicana", "do"),
    ("https://plataforma.sini.gob.do/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Republica Dominicana", "do"),
    ("http://geoportal.sigtierras.gob.ec:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Ecuador", "ec"),
    ("https://geonode.inec.gob.ec/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ecuador", "ec"),
    ("https://geoservicios.inamhi.gob.ec/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ecuador", "ec"),
    ("https://geovisor.manabi.gob.ec/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ecuador", "ec"),
    ("https://www.geoportal.ame.gob.ec/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ecuador", "ec"),
    ("https://gsavalik.envir.ee/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Estonia", "ee"),
    ("https://inspire.geoportaal.ee/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Estonia", "ee"),
    ("https://kls.pria.ee/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Estonia", "ee"),
    ("https://data.transportforcairo.com/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Egypt", "eg"),
    ("http://geoserver.costadelsolmalaga.org/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("http://geoserver.icgc.cat:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("http://ide.ticmallorca.net/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Spain", "es"),
    ("http://idechg.chguadalquivir.es:80/geoserver/ows", "Spain", "es"),
    ("http://oden.diputaciolleida.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("http://siurana.icgc.cat/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("http://visorrpgur.asturias.es:8090/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("http://www.geoportalagriculturaypesca.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://alzira.gvsigonline.com/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://bomberos.gvsigonline.com/geoserver/wms/wfs", "Spain", "es"),
    ("https://geonode.arxiuhistoricpoblenou.cat/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Spain", "es"),
    ("https://geoportal-lavalldegallinera.geoinnova.es/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Spain", "es"),
    ("https://geoserver.puertos.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://geoserver.villanuevadelaserena.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://gestion4-idearagon.aragon.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://ide.caceres.es/geoserver/wms/wfs", "Spain", "es"),
    ("https://ide.cime.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://idearagon.aragon.es:443/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://idecyl.jcyl.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://mapas-gis-inter.carm.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://mapas.idepa.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://meteogalicia.gal/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://opengeo-gis.carm.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://ortofotos-gis.carm.es:443/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("https://pmpc-arandadeduero.geoinnova.es/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Spain", "es"),
    ("https://www.pescacastillayleon.es/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Spain", "es"),
    ("http://geo.portal.ebi.gov.et/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ethiopia", "et"),
    ("http://lbdcdirectory.gov.et/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ethiopia", "et"),
    ("http://qfield.cloud.ebi.gov.et/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ethiopia", "et"),
    ("http://www.ethionsdi.gov.et/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ethiopia", "et"),
    ("https://lsc-hub.eiar.gov.et/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ethiopia", "et"),
    ("https://nsis.moa.gov.et/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ethiopia", "et"),
    ("http://climate-adapt.eea.europa.eu/geoserver/ows", "European Union", "eu"),
    ("http://geospatial2.jrc.ec.europa.eu/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "European Union", "eu"),
    ("https://geospatial.jrc.ec.europa.eu/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "European Union", "eu"),
    ("https://iws.seastorms.eu/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "European Union", "eu"),
    ("https://www.portodimare.eu/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "European Union", "eu"),
    ("http://avoinkara.mmm.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("http://geo.stat.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("http://geoserver.lounaistieto.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("http://gtkimage.gtk.fi:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("http://kartta.suomi.fi/geoserver/wfs", "Finland", "fi"),
    ("http://maps.luke.fi:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("http://openwms.fmi.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("http://paikkatieto.jamsa.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://avoindata.kotka.fi:8443/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://data.nsdc.fmi.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://enontekio.ubihub.io/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://geodata.tampere.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://georaster.tampere.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://geoserv.stat.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://geoserver.hel.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://geoserver.ymparisto.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://geosrv.sipoo.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://gis.paimio.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://gis.vantaa.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://inspire.ruokavirasto-awsa.com/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://ixgsmap2.ymparisto.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://kartta.hel.fi/ws/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://kartta.hsy.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://opendata.ymparistonyt.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://paikkatiedot.ymparisto.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://rajapinnat.metsaan.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("https://tieto.pirkanmaa.fi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Finland", "fi"),
    ("http://CLC.developpement-durable.gouv.fr/geoserver/ows", "France", "fr"),
    ("http://alsace.websol.fr:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("http://geo.compiegnois.fr/geoserver/ows", "France", "fr"),
    ("http://geo.valdille-aubigne.fr.fr/geoserver/ows", "France", "fr"),
    ("http://geoserver.lannion-tregor.com/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("http://ids.pigma.org/geoserver/ows", "France", "fr"),
    ("http://services.vuduciel.loire-atlantique.fr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("http://valsdesaintonge-sig.org:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("http://www.sig.cg971.fr:8080/geoserver/ows", "France", "fr"),
    ("https://artois-picardie.eaufrance.fr:443/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("https://atmo-bfc.iad-informatique.com/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("https://catalogue.guyane-sig.fr/geoserver/ows", "France", "fr"),
    ("https://data.sigea.educagri.fr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "France", "fr"),
    ("https://dev.pigma.org/geoserver/ows", "France", "fr"),
    ("https://geo.valdille-aubigne.fr/geoserver/ows", "France", "fr"),
    ("https://geonode.recette.oieau.fr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "France", "fr"),
    ("https://georchestra.cbnbl.org/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("https://opendata.agglo-lepuyenvelay.fr/geoserver/ows", "France", "fr"),
    ("https://ows.region-bretagne.fr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("https://pigma.org/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("https://ppige-npdc.fr/geoserver/ows", "France", "fr"),
    ("https://qualif.data.sigea.educagri.fr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "France", "fr"),
    ("https://scot.datasud.fr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("https://sig.atmo-auvergnerhonealpes.fr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("https://sigcapa.fr:8443/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "France", "fr"),
    ("https://www.geoplateforme17.fr/geoserver/ows", "France", "fr"),
    ("https://www.ppige-npdc.fr:443/geoserver/ows", "France", "fr"),
    ("http://apps.hinckley-bosworth.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://arcgisweb.fife.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://data.nottinghamshire.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://geonet.allerdale.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://geoserver.rushmoor.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://gistch1.copelandbc.org.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://inspire.dundeecity.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://inspire.halton.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://inspire.nationalparks.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://inspire.northdevon.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://inspire.redcar-cleveland.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://inspire.ribblevalley.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://inspire.sthelens.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://inspire.worcester.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://inspire.wychavon.gov.uk:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://kgeo.knowsley.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://mapping.broxbourne.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://maps.communities.gov.uk:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://maps.darlington.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://maps.northlincs.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://maps.scarborough.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://maps.staffordbc.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://w3.blaby.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://w3.fylde.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://webmap.stockton.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://www.gis.northlincs.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://www.map.hackney.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://www.newcastle-staffs.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("http://www.rbkc.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://data.angus.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://datamap.gov.wales/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://geo.powys.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://geo.spatialhub.scot/geoserver/ows", "United Kingdom", "gb"),
    ("https://geodata.rbwm.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://geoserver.rcdo.co.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://gis.beacons-npa.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://gis.cumbria.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://gis.herefordshire.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://hbcmaps.harrogate.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://inspire.nationalparks.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://inspire.northyorkmoors.org.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://inspire.pembrokeshire.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://map.salford.gov.uk:443/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://maps.cheshireeast.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://maps.dartmoor.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://maps.middevon.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://maps.runnymede.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://ogc.nature.scot/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://spatial.stockport.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://w3.blackpool.gov.uk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://wms.derbyshire.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://www.southampton.gov.uk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United Kingdom", "gb"),
    ("https://gpv0.napr.gov.ge/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Georgia", "ge"),
    ("https://nv.napr.gov.ge/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Georgia", "ge"),
    ("https://ghanageoportal.com/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ghana", "gh"),
    ("http://download.geoportal.gov.gi/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Gibraltar", "gi"),
    ("https://gis.govmin.gl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Greenland", "gl"),
    ("http://3.23.95.209/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Gambia", "gm"),
    ("http://geoportal.ypen.gr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Greece", "gr"),
    ("http://gis.cityofathens.gr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Greece", "gr"),
    ("http://gis.thessaloniki.gr/geoserver/ows", "Greece", "gr"),
    ("http://maps.msp-greece.eu/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Greece", "gr"),
    ("http://mapsportal.ypen.gr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Greece", "gr"),
    ("http://services.halandri.gr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Greece", "gr"),
    ("http://services.opendatacorfu.gr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Greece", "gr"),
    ("http://services.opendataepirus.gr/geoserver/ows", "Greece", "gr"),
    ("http://ypendev2.cfserver3.net/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Greece", "gr"),
    ("https://beta.gis.cityofathens.gr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Greece", "gr"),
    ("https://geoportal.ermis-f.eu/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Greece", "gr"),
    ("https://geoserver.ims.forth.gr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Greece", "gr"),
    ("https://services.chalandri.gr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Greece", "gr"),
    ("https://services.heraklion.gr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Greece", "gr"),
    ("https://thalchor-2.ypen.gov.gr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Greece", "gr"),
    ("http://ideg.segeplan.gob.gt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Guatemala", "gt"),
    ("https://portal.ric.gob.gt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Guatemala", "gt"),
    ("https://www.geoportal.marn.gob.gt:8080/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Guatemala", "gt"),
    ("https://maps.nre.gov.gy/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Guyana", "gy"),
    ("http://geonode.copeco.gob.hn/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Honduras", "hn"),
    ("http://geoserver.icf.gob.hn/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Honduras", "hn"),
    ("https://geonode.ine.gob.hn/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Honduras", "hn"),
    ("https://mapassimpro.copeco.gob.hn/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Honduras", "hn"),
    ("http://rgi.dgu.hr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Croatia", "hr"),
    ("https://jisms.gospodarstvo.gov.hr/nipp/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Croatia", "hr"),
    ("https://nipp.hzinfra.hr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Croatia", "hr"),
    ("https://stgo.dgu.hr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Croatia", "hr"),
    ("https://haitidata.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Haiti", "ht"),
    ("http://geonetwork.mfgi.hu:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Hungary", "hu"),
    ("http://geoserver.inspire.fomi.hu/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Hungary", "hu"),
    ("http://agamkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://anambaskab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://balangankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://baliprov.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://bandungkota.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://banggaikab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://banggaikep.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://banggailautkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://banjarkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://banjarkota.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://banjarmasinkota.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://banyuwangikab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://baritokualakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://baritoselatankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://bekasikota.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://bengkaliskab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://bengkayangkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://bengkuluprov.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://bimakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://bondowosokab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://ciamiskab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://cianjurkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://cimahikota.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://cirebonkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://cirebonkota.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://demakkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://dharmasrayakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://dumaikota.ina-sdi.or.id/geoserver/wfs", "Indonesia", "id"),
    ("http://empatlawangkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://enrekangkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://fakfakkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://fp2.menlhk.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://garutkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.babelprov.go.id:8080/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.banjarbarukota.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.bantulkab.go.id:8080/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.banyumaskab.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.batangkab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.bekasikab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.boyolali.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.jambikota.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.jatengprov.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.jogjaprov.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.kaltaraprov.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.kalteng.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.kebumenkab.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.kemenperin.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.kolakakab.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.kolakatimurkab.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.kuburayakab.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.kuduskab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.lebongkab.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.manadokota.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.mojokertokota.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.natunakab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.ntbprov.go.id:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.okutimurkab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.palembang.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.papua.go.id:8080/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.pemkomedan.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.penajamkab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.purworejokab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.riau.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.sumbawakab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.sumselprov.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.tabalongkab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.temanggungkab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geoportal.tulungagung.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://geospasial.kalbarprov.go.id:8080/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://gresikkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://grobogankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://halmaherautarakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://hulusungaitengahkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://indragirihilirkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://indragirihulukab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://indramayukab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://jambiprov.ina-sdi.or.id:8080/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://jayapurakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://jemberkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://jigd.pangandarankab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://jombangkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://kaimanakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://kamparkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://kapuashulukab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://karawangkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://kayongutarakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://kepriprov.ina-sdi.or.id:8080/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://kepulauansulakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://ketapangkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://kotabaru.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://kotatanjungpinang.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://kotawaringinbaratkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://kuningankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://labuhanbatukab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://lahatkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://lamongankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://lampungbaratkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://landakkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://lebakkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://limapuluhkotakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://lubuklinggaukota.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://lumajangkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://luwutimurkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://madiunkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://magetankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://majalengkakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://malangkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://maluku.ina-sdi.or.id:8080/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://malukuutara.ina-sdi.or.id:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://mamujutengahkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://manokwarikab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://manselkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://melawikab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://mempawahkab.ina-sdi.or.id:8080/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://metrokota.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://musirawaskab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://nagekeokab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://nganjukkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://ngawikab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://nttprov.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://oganilirkab.ina-sdi.or.id:8080/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://ogankomeringulukab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://okuselatankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://padanglawasutarakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://padangpariamankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://palangkarayakota.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://palukota.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://pamekasankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://pariamankota.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://paserkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://pasuruankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://pesisirbaratkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://pgis.blitarkab.go.id:8080/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://pidiekab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://polewalimandarkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://ponorogokab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://pontianakkota.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://purwakartakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://rajaampatkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sabangkota.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sambaskab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sanggaukab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sawahluntokota.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sekadaukab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://serambigeoportal.padangpanjang.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://siakkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sidrapkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sigikab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sintangkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://situbondokab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://solokkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sorongkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://subangkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sukabumikab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://sulbarprov.ina-sdi.or.id:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://tanahbumbukab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://tanahlautkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://tapselkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://tarakankota.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://tasikmalayakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://tojounaunakab.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://trenggalekkab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://tubankab.ina-sdi.or.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://waykanan.ina-sdi.or.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://atlas.atrbpn.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geodata.bandung.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geonode.bantulkab.go.id/geoserver/ows", "Indonesia", "id"),
    ("https://geonode.folunc-id.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geonode.nodc.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.asahankab.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.beraukab.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.bukittinggikota.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.bulukumbakab.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.deliserdangkab.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.kominfo.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.kotimkab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.kulonprogokab.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.langkatkab.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.magelangkab.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.magelangkota.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.mubakab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.pareparekota.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.purbalinggakab.go.id/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.slemankab.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://geoportal.sultengprov.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://gis.blitarkab.go.id/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://jigd.kaltimprov.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://pisda.sukoharjokab.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://portal.sitarung.win/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Indonesia", "id"),
    ("https://simtaru.papua.go.id/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Indonesia", "id"),
    ("http://eutgn.marine.ie/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ireland", "ie"),
    ("https://gis-int.epa.ie/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Ireland", "ie"),
    ("https://gis-stg.epa.ie/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Ireland", "ie"),
    ("https://gis-test.epa.ie/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Ireland", "ie"),
    ("https://www.floodinfo.ie/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Ireland", "ie"),
    ("https://bhuvan-vec3.nrsc.gov.in/bhuvan/ows?service=WFS&version=1.0.0&request=GetCapabilities", "India", "in"),
    ("https://geosadak-pmgsy.nic.in:8080/ows?service=WFS&version=1.0.0&request=GetCapabilities", "India", "in"),
    ("https://geoserver.dx.geospatial.org.in/stac/wfs", "India", "in"),
    ("https://geoserver.ts.adex.org.in/stac/wfs", "India", "in"),
    ("https://tngis.tn.gov.in/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "India", "in"),
    ("https://iransdi.ncc.gov.ir/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Iran", "ir"),
    ("http://gagnaveita.vegagerdin.is/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Iceland", "is"),
    ("http://vefsja.skjalasafn.is/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Iceland", "is"),
    ("https://geo.vedur.is/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Iceland", "is"),
    ("https://geoserver.mast.is/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Iceland", "is"),
    ("https://gis.fasteignaskra.is/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Iceland", "is"),
    ("https://gis.hafogvatn.is/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Iceland", "is"),
    ("https://gis.is/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Iceland", "is"),
    ("https://gis.lmi.is/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Iceland", "is"),
    ("https://thjonustukort.is/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Iceland", "is"),
    ("http://geonode.supportopcveneto.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("http://geoserver.comune.fano.pu.it:8090/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("http://geoserver.comune.prato.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("http://geoserver.protezionecivile.fvg.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("http://geoservices.retecivica.bz.it/geoserver/ows", "Italy", "it"),
    ("http://mappe.provincia.teramo.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("http://microzonazione.regione.basilicata.it:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("http://ows.provinciatreviso.it:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("http://pubblicazioni.cittametropolitana.fi.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("http://sdi.isprambiente.it:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("http://sit.cittametropolitana.na.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("http://wgmatera.paesit.it/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Italy", "it"),
    ("http://www.silvenezia.it:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://app.geonue.com/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://atlad.geoportale.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("https://cigno.atlantedellalaguna.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("https://decimetro.cittametropolitana.mi.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("https://demo-geoservices8.civis.bz.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://gaia.arpa.veneto.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://geomap.arpa.veneto.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("https://geonode.provincia.treviso.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("https://geoportale.comune.roma.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://geoportale.comunedisanremo.it/geoserver-next/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://geoportale.lamma.rete.toscana.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://geoportale.regione.lazio.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("https://geosdi.geodatalab.cloud/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("https://geoserver.comune.modena.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://geoserver.comune.re.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://geoservices.buergernetz.bz.it/geoserver/ows", "Italy", "it"),
    ("https://geoservices1.civis.bz.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://geoservices6.civis.bz.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://geoservizi.regione.vda.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://gisserver.territorio.csi.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://idrogeo.isprambiente.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://idt2-geoserver.regione.veneto.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://reportdu.nnb.isprambiente.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("https://serviziogc.regione.fvg.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://sgi2.isprambiente.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://sit2.regione.campania.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://staging.webgis.adbpo.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://webgis.regione.sardegna.it/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Italy", "it"),
    ("https://www.atlantedellalaguna.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("https://www.geonode.nnb.isprambiente.it/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Italy", "it"),
    ("http://gissv03.pref.nagasaki.jp/geoserver/ows", "Japan", "jp"),
    ("https://portal.msp.go.ke/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Kenya", "ke"),
    ("https://geonode.water.gov.kg/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Kyrgyzstan", "kg"),
    ("http://lxgis.jeonju.go.kr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "South Korea", "kr"),
    ("http://service.kosha.or.kr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "South Korea", "kr"),
    ("https://bigdata.dongjak.go.kr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "South Korea", "kr"),
    ("https://floodmap.go.kr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "South Korea", "kr"),
    ("https://geo.safemap.go.kr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "South Korea", "kr"),
    ("https://gis.bdna.or.kr:4443/ows?service=WFS&version=1.0.0&request=GetCapabilities", "South Korea", "kr"),
    ("https://weather.go.kr/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "South Korea", "kr"),
    ("http://geo.eatyrau.kz/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Kazakhstan", "kz"),
    ("https://geopavlodar.kz/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Kazakhstan", "kz"),
    ("https://geoportal.akt.kz/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Kazakhstan", "kz"),
    ("https://map.gov.kz/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Kazakhstan", "kz"),
    ("https://dmhlao.la/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Laos", "la"),
    ("https://virgo.mpwt.gov.la/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Laos", "la"),
    ("https://spims.moe.gov.lb/geoserver/web/wfs", "Lebanon", "lb"),
    ("https://geoservices.govt.lc/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Saint Lucia", "lc"),
    ("http://www.mebin.nara.ac.lk/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Sri Lanka", "lk"),
    ("https://geoserver.kaunas.lt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Lithuania", "lt"),
    ("https://www.inspire-geoportal.lt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Lithuania", "lt"),
    ("https://wms.inspire.geoportail.lu/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Luxembourg", "lu"),
    ("https://wms.staging.inspire.geoportail.lu/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Luxembourg", "lu"),
    ("https://geoserver.lvgmc.lv/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Latvia", "lv"),
    ("https://ims-web.vvd.gov.lv/geoserver/web/wfs", "Latvia", "lv"),
    ("https://is.mantojums.lv/geoserver/web/wfs", "Latvia", "lv"),
    ("https://tapis.gov.lv/izpl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Latvia", "lv"),
    ("http://geodata.gov.md/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Moldova", "md"),
    ("http://oikumena.md:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Moldova", "md"),
    ("https://map.cadastru.md/geoserver/ows", "Moldova", "md"),
    ("https://geoportal.bar.me:8081/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Montenegro", "me"),
    ("https://onlinegis.cedis.me/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Montenegro", "me"),
    ("https://razvojgis.cedis.me/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Montenegro", "me"),
    ("https://protectedareas.mg/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Madagascar", "mg"),
    ("https://www.resiliencemada.gov.mg/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Madagascar", "mg"),
    ("http://map.cuk.gov.mk:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Republic of North Macedo", "mk"),
    ("http://eic.mn:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Mongolia", "mn"),
    ("https://geo.nsdi.gov.mn/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Mongolia", "mn"),
    ("http://pfni-ce.mr/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mauritania", "mr"),
    ("https://msdi.data.gov.mt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Malta", "mt"),
    ("https://geoportal.govmu.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mauritius", "mu"),
    ("https://www.masdap.mw/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Malawi", "mw"),
    ("http://geo.datos.jalisco.gob.mx/geoserver/wfs", "Mexico", "mx"),
    ("http://geoinfo.iplaneg.net/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("http://geonode.conabio.gob.mx/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://congreso2021.iplaneg.net/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://geonode.idegeo.centrogeo.org.mx/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://geonode.implancmty.geoint.mx/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://geonode.matiko.centrogeo.org.mx/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://geonode.sgg.geoint.mx/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://geonode.spotmet.geoint.mx/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://geonode.tlalpan.geoint.mx/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://idefor.cnf.gob.mx/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://sieg.cdmx.gob.mx/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://sisplade.geoint.mx/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://www.geomexicali.info/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("https://www.sqnodo.com/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mexico", "mx"),
    ("http://ismp.water.gov.my/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Malaysia", "my"),
    ("http://skips.jupem.gov.my:82/geoserver/ows", "Malaysia", "my"),
    ("https://mymaps.mygeoportal.gov.my/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Malaysia", "my"),
    ("https://madico.terrafirma.co.mz/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Mozambique", "mz"),
    ("https://data.nigeriase4all.gov.ng/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Nigeria", "ng"),
    ("https://geoserver.grid-nigeria.org/geoserver/wfs", "Nigeria", "ng"),
    ("https://mapserverprivado.ineter.gob.ni/geoserver/wfs", "Nicaragua", "ni"),
    ("http://geo.iszf.nl/geoserver/wfs", "Netherlands", "nl"),
    ("http://geo.sudwestfryslan.nl/geoserver/wfs", "Netherlands", "nl"),
    ("http://geodata.rivm.nl/geoserver/wfs", "Netherlands", "nl"),
    ("http://geoserver.nieuwegein.nl/geoserver/wfs", "Netherlands", "nl"),
    ("http://inspire.rivm.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("http://services.geodataoverijssel.nl:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://data-wior-amsterdam.webgis.nl/ows", "Netherlands", "nl"),
    ("https://data.haarlem.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://datalab.alkmaar.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://geo.drenthe.nl/geoserver/wfs", "Netherlands", "nl"),
    ("https://geo.ede.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://geo.koggenland.nl/geoserver/wfs", "Netherlands", "nl"),
    ("https://geo.rijkswaterstaat.nl/services/ogc/gdr/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://geo.vggm.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://geo2.flevoland.nl/geoserver/wfs", "Netherlands", "nl"),
    ("https://geodata.utrecht.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://geodata.zuid-holland.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://geoserver-almereinkaart.webgispublisher.nl/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://geoserver-productie.webgispublisher.nl/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://geoserver.gelderland.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://geoserver.waalwijk.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://geoservices.portaalnatuurenlandschap.nl/geoserver/gwc/service/tms/1.0.0/wfs", "Netherlands", "nl"),
    ("https://geoweb.amstelveen.nl/geoserver/wfs", "Netherlands", "nl"),
    ("https://gnlufosrv02.kaartviewer.nl:8444/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://ihm-pub.geopublisher.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://inspire.caris.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://maps-intern.zaanstad.gem.local/geoserver/ows", "Netherlands", "nl"),
    ("https://maps.groningen.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://maps.vlaardingen.nl/geoserver/ows", "Netherlands", "nl"),
    ("https://maps.zaanstad.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://maps1.klimaatatlas.net/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://ogcgeo.zwemwater.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://opendata.hunzeenaas.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://opengeodata.zeeland.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://plattegronden.gooisemeren.nl/geoserver/wfs", "Netherlands", "nl"),
    ("https://projectgeodata.zeeland.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://rvo.b3p.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://services.geodata-utrecht.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://services.rce.geovoorziening.nl/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://waddinxveen.kaartviewer.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://waterveiligheidsportaal.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://wmsonly-services.geodataoverijssel.nl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Netherlands", "nl"),
    ("https://www.wibon-inspire.nl/geoserver/ows", "Netherlands", "nl"),
    ("https://www.wion-inspire.nl/geoserver/wfs", "Netherlands", "nl"),
    ("http://geo.ngu.no/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Norway", "no"),
    ("http://wms.dirnat.no/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Norway", "no"),
    ("https://geoserver.barentswatch.no/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Norway", "no"),
    ("https://kart.hi.no/data/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Norway", "no"),
    ("https://kart.miljodirektoratet.no/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Norway", "no"),
    ("http://geoportal.ntnc.org.np/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Nepal", "np"),
    ("https://admin.nationalgeoportal.gov.np/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Nepal", "np"),
    ("https://database.ntb.gov.np/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Nepal", "np"),
    ("https://nationalgeoportal.gov.np/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Nepal", "np"),
    ("https://data.codc.govt.nz/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "New Zealand", "nz"),
    ("https://data.otodc.govt.nz/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "New Zealand", "nz"),
    ("https://data.wairoadc.govt.nz/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "New Zealand", "nz"),
    ("https://gs.niwa.co.nz/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "New Zealand", "nz"),
    ("https://geo-01.innovacion.gob.pa/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Panama", "pa"),
    ("https://geonode.mupa.gob.pa/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Panama", "pa"),
    ("http://geo.ceplan.gob.pe:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Peru", "pe"),
    ("http://geo.munisanisidro.gob.pe:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Peru", "pe"),
    ("http://geo.sernanp.gob.pe/geoserver/ows", "Peru", "pe"),
    ("http://ider.regionhuanuco.gob.pe/geoserver/ows", "Peru", "pe"),
    ("http://mtcgeo2.mtc.gob.pe:8080/geoserver/ows", "Peru", "pe"),
    ("https://estadoconservacion.sernanp.gob.pe/geoserver/ows", "Peru", "pe"),
    ("https://geoserver.miraflores.gob.pe:8443/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Peru", "pe"),
    ("https://geoservicios.cultura.gob.pe/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Peru", "pe"),
    ("https://sdmr.inei.gob.pe/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Peru", "pe"),
    ("https://luims.dlpp.gov.pg/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Papua New Guinea", "pg"),
    ("https://png-geoportal.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Papua New Guinea", "pg"),
    ("http://crisp.r10.denr.gov.ph/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Philippines", "ph"),
    ("https://geonode.tagabukid.net/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Philippines", "ph"),
    ("https://geoserver.bukidnon.gov.ph/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Philippines", "ph"),
    ("https://geoserver.geoportal.gov.ph/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "Philippines", "ph"),
    ("https://rgin.rdc1.gov.ph/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Philippines", "ph"),
    ("https://rgin.rdc9.gov.ph/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Philippines", "ph"),
    ("https://www.carmonagis.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Philippines", "ph"),
    ("http://sit-mapa.tarnowskiegory.pl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Poland", "pl"),
    ("http://usip-kielce.e-swietokrzyskie.pl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Poland", "pl"),
    ("http://usip.e-swietokrzyskie.pl/geoserver/wms/wfs", "Poland", "pl"),
    ("http://wms2.geopoz.poznan.pl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Poland", "pl"),
    ("https://iip.ekoportal.gov.pl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Poland", "pl"),
    ("https://sip.um.swidnica.pl/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Poland", "pl"),
    ("https://geoportal.nsdi.ps/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Palestine", "ps"),
    ("http://geo.sigamcb.pt:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("http://geos.ccdrc.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("http://geoservices.dgadr.pt:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("http://igdrem.madeira.gov.pt/geoserver/wfs", "Portugal", "pt"),
    ("http://mapas.hidrografico.pt:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("http://prototipo-catalogo.ipma.pt:80/geoserver/wfs", "Portugal", "pt"),
    ("http://si.icnf.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("http://sig.cm-terrasdebouro.pt/geoserver21/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("http://sigmealhada.cm-mealhada.pt/geoMealhada/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("http://wssiglrec.azores.gov.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://geo2.dgterritorio.gov.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://geoserver.sig.cm-agueda.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://geoservices.madeira.gov.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://inspire.ine.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://sig-altotamega.pt/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://sigweb.cmnordeste.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://webgeo1.hidrografico.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://webgeo2.hidrografico.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://webgeo5.hidrografico.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://wssiga.azores.gov.pt/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Portugal", "pt"),
    ("https://geonode.ine.gov.py/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Paraguay", "py"),
    ("https://geoportal.paraguay.gov.py/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Paraguay", "py"),
    ("http://imdroflood.meteoromania.ro:8080/geoserver/ows", "Romania", "ro"),
    ("https://geo.salt.gov.ro/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Romania", "ro"),
    ("https://inspire.igr.ro/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Romania", "ro"),
    ("https://sitgorjnv.ro:8443/geoserver/ows", "Romania", "ro"),
    ("https://fis.upravazasume.gov.rs/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Serbia", "rs"),
    ("http://geo.ferhri.ru:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Russian Federation", "ru"),
    ("http://geoportal.rgis.rk.gov.ru/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Russian Federation", "ru"),
    ("http://gisa.aari.ru:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Russian Federation", "ru"),
    ("https://fires.dvinaland.ru/geoserver/ows", "Russian Federation", "ru"),
    ("https://fpd.lenobl.ru/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Russian Federation", "ru"),
    ("https://geoportal.gov39.ru/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Russian Federation", "ru"),
    ("https://geoserver.geo.gov35.ru/geoserver/geo/wfs", "Russian Federation", "ru"),
    ("https://gs2.rgis.spb.ru/geoserver/gwc/service/tms/1.0.0/wfs", "Russian Federation", "ru"),
    ("https://investmapapi.economy.gov.ru/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Russian Federation", "ru"),
    ("https://map.vbglenobl.ru/GISWebServiceSE/service.php?SERVICE=WFS&REQUEST=GetCapabilities", "Russian Federation", "ru"),
    ("https://mnp.economy.gov.ru/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Russian Federation", "ru"),
    ("https://nspd.rosreestr.gov.ru/api/wfs/v2?SERVICE=WFS&REQUEST=GetCapabilities", "Russian Federation", "ru"),
    ("https://pub.fgislk.gov.ru/plk/geoservermaster/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Russian Federation", "ru"),
    ("https://rgis71.tularegion.ru/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Russian Federation", "ru"),
    ("https://transport.mos.ru/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Russian Federation", "ru"),
    ("https://geohazards.rtda.gov.rw/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Rwanda", "rw"),
    ("https://www.geoportal.rwb.rw/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Rwanda", "rw"),
    ("https://geoserver-apia.sprep.org/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Solomon Islands", "sb"),
    ("http://epub.sjv.se/inspire/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("http://gi.karlstad.se:8080/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://arcticsdi.lm.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://daim.lfv.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://geodata.scb.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://geonode.folkhalsomyndigheten.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://karta.enkoping.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://karta.hallstahammar.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://karta.miljoforvaltningen.goteborg.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://karta.sigtuna.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://nvgis.naturvardsverket.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://stationsregister.miljodatasamverkan.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://stationsregistertest.miljodatasamverkan.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://www.malardalskartan.se/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Sweden", "se"),
    ("https://geoserver.geo-zs.si/GeoZS_Superficial_Geology/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Slovenia", "si"),
    ("https://gis.arso.gov.si/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Slovenia", "si"),
    ("https://prostor.zgs.gov.si/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Slovenia", "si"),
    ("http://inspire.biomonitoring.sk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Slovakia", "sk"),
    ("http://maps.geop.sazp.sk:80/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Slovakia", "sk"),
    ("https://geo.shmu.sk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Slovakia", "sk"),
    ("https://geopresovregion.sk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Slovakia", "sk"),
    ("https://geos.sazp.sk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Slovakia", "sk"),
    ("https://gisgeo.zsr.sk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Slovakia", "sk"),
    ("https://www.geoportalksk.sk/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Slovakia", "sk"),
    ("https://georisques.sec.gouv.sn/geoserver-prod/web/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Senegal", "sn"),
    ("http://geoportalofhargeisa.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Somalia", "so"),
    ("https://geodatarisk.tg/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Togo", "tg"),
    ("https://sig-anpc.switch-maker.net/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Togo", "tg"),
    ("http://bpt.dol.go.th:8088/geoserver/ows", "Thailand", "th"),
    ("http://gis.rid.go.th/geoserver/ows", "Thailand", "th"),
    ("http://portal.dol.go.th/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Thailand", "th"),
    ("http://simahosot.onep.go.th/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Thailand", "th"),
    ("http://tile.gistda.or.th/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Thailand", "th"),
    ("http://wms.nso.go.th/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Thailand", "th"),
    ("https://change2.gistda.or.th/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Thailand", "th"),
    ("https://geo.dla.go.th/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Thailand", "th"),
    ("https://geonode.envilink.go.th/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Thailand", "th"),
    ("https://gis.labour.go.th/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Thailand", "th"),
    ("https://portal.gfms.gistda.or.th/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Thailand", "th"),
    ("https://portal.marineportal.gistda.or.th/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Thailand", "th"),
    ("https://portal2.marineportal.gistda.or.th/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Thailand", "th"),
    ("https://tcs.dmcr.go.th/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Thailand", "th"),
    ("http://maps.wis.tj:555/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Tajikistan", "tj"),
    ("http://www.onagri.tn/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Tunisia", "tn"),
    ("http://cbs.yalova.bel.tr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Turkey", "tr"),
    ("http://veri.tarimorman.gov.tr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Turkey", "tr"),
    ("https://acikyesil.bursa.bel.tr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Turkey", "tr"),
    ("https://cbsservis.uab.gov.tr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Turkey", "tr"),
    ("https://geoserver.trabzon.bel.tr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Turkey", "tr"),
    ("https://ivmegeoserver.afad.gov.tr/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Turkey", "tr"),
    ("http://tchgis.tainan.gov.tw:8080/geoserver/web/wfs", "Taiwan", "tw"),
    ("https://eland.cpami.gov.tw/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Taiwan", "tw"),
    ("https://geonode.resilienceacademy.ac.tz/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Tanzania", "tz"),
    ("https://geonode.tarurapcugeodata.or.tz/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Tanzania", "tz"),
    ("https://geonode.nema.go.ug/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Uganda", "ug"),
    ("http://geoserver2.pr.gov/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United States", "us"),
    ("http://landscapeportal.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United States", "us"),
    ("http://www.vdotdatasharing.org/sitemap.xml/wfs", "United States", "us"),
    ("https://chocofair.dev/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United States", "us"),
    ("https://data.howardcountymd.gov/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United States", "us"),
    ("https://geonode.ggcity.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United States", "us"),
    ("https://geonode.imperialbeachca.gov/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United States", "us"),
    ("https://geonode.state.gov/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United States", "us"),
    ("https://geoplatform.spacesur.com/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "United States", "us"),
    ("https://geoserver.geoplatform.gov/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United States", "us"),
    ("https://gis.fema.gov/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United States", "us"),
    ("https://opengeo.ncep.noaa.gov/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United States", "us"),
    ("https://www.mrlc.gov/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United States", "us"),
    ("https://www.sciencebase.gov/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "United States", "us"),
    ("http://geoserver.montevideo.gub.uy/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Uruguay", "uy"),
    ("https://durazno.gvsigonline.com/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Uruguay", "uy"),
    ("https://geoserver.miem.gub.uy/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Uruguay", "uy"),
    ("https://geoserver.opp.gub.uy/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Uruguay", "uy"),
    ("https://geoserver.snia.gub.uy/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Uruguay", "uy"),
    ("https://geoservicios.mtop.gub.uy/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Uruguay", "uy"),
    ("https://gs.igm.gub.uy/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Uruguay", "uy"),
    ("https://mapas.mides.gub.uy/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Uruguay", "uy"),
    ("https://geoserver.mppp.gob.ve/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Venezuela (Bolivarian Re", "ve"),
    ("https://mapas.alcaldiademaracaibo.org/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Venezuela (Bolivarian Re", "ve"),
    ("http://portal.hcmgis.vn/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Vietnam", "vn"),
    ("https://congbo.dulieuvientham.gov.vn/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Vietnam", "vn"),
    ("https://geodata-stnmt.tphcm.gov.vn/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Vietnam", "vn"),
    ("https://map.tnmtgialai.gov.vn/ows?service=WFS&version=1.0.0&request=GetCapabilities", "Vietnam", "vn"),
    ("https://opendata.hcmgis.vn/geoserver/web/wfs", "Vietnam", "vn"),
    ("https://geonode.gov.vu/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "Vanuatu", "vu"),
    ("http://146.118.96.76/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "World", "world"),
    ("http://190.112.43.34/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "World", "world"),
    ("http://192.168.210.58/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "World", "world"),
    ("http://196.45.37.197/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "World", "world"),
    ("http://202.154.182.164:8080/geoserver/wfs", "World", "world"),
    ("http://213.165.151.135/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities", "World", "world"),
    ("http://geo-spatial.org/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "World", "world"),
    ("http://geo4.vic-metria.nu:80/geoserver/wfs", "World", "world"),
    ("http://geoserver.prosmap.org/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "World", "world"),
    ("http://sirei.pariis.net:8000/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "World", "world"),
    ("https://rsistest.ramsar.org/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "World", "world"),
    ("https://seagrass.observing.earth/geoserver/ows?service=WFS&version=1.1.0&request=GetCapabilities", "World", "world"),
    ("https://sigpobla.gvsigonline.com/geoserver/ows?service=WFS&version=1.0.0&request=GetCapabilities", "World", "world"),
]
_WFS_KEEP = _re.compile(
    r"permit|planning|develop|construct|mining|\bmine\b|quarr|pipeline|concession|"
    r"infrastructur|\bproject|licen[cs]e|environ|impact|land ?use|zoning|cadastr|"
    r"obra|proyecto|miner|licencia|concesi|urbanism|catastro|ambient|ordenamiento|"
    r"licenciamento|empreendimento|minera|permis|amenagement|chantier|carri\u00e8re|"
    r"exploitation|baugen|genehmig|planung|bergbau|umwelt|vorhaben|"
    r"wind ?farm|offshore|hydrocarbon|dredg|aggregate extraction|"
    r"izin|tambang|pembangunan|amdal|tata ?ruang|lingkungan|"
    r"cava|miniera|ambientale|urbanistica|edilizia|vergunning|bouw|ontwikkel|mijnbouw",
    _re.I)

def _wfs_typenames(xmltext):
    try:
        root = _ET.fromstring(xmltext)
    except Exception:
        return []
    out = []
    for ft in root.iter():
        if ft.tag.rsplit("}", 1)[-1] != "FeatureType":
            continue
        nm = ti = None
        for ch in ft:
            ln = ch.tag.rsplit("}", 1)[-1]
            if ln == "Name":
                nm = (ch.text or "").strip()
            elif ln == "Title":
                ti = (ch.text or "").strip()
        if nm:
            out.append((nm, ti or nm))
    return out

def _wfs_get(url, limit_bytes=3000000, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit_bytes).decode("utf-8", "replace")

def fetch_wfs_federation(per_endpoint=None, per_ds=900):
    per_endpoint = per_endpoint or (10 if os.environ.get("HARVEST_FEDERATIONS") == "1" else 6)
    out = []
    budget_min = _fed_budget("WFS_BUDGET_MIN", 40, 80)
    t_end = time.time() + budget_min * 60
    _wfs_eps = _shard_list(_WFS_ENDPOINTS)
    for (url, country, cc) in _wfs_eps:
        if time.time() > t_end:
            _flag("wfs federation hit %d-min budget -- %d endpoints not reached" %
                  (budget_min, len(_wfs_eps) - _wfs_eps.index((url, country, cc))))
            break
        base = url.split("?")[0]
        sep = "&" if "?" in base else "?"
        try:
            cap = _wfs_get(base + sep + "service=WFS&version=1.1.0&request=GetCapabilities")
        except Exception:
            continue
        layers = [(n, t) for (n, t) in _wfs_typenames(cap) if _WFS_KEEP.search(t) or _WFS_KEEP.search(n)]
        got = 0
        for nm, ti in layers:
            if got >= per_endpoint:
                break
            gj = None
            for ofmt in ("application/json", "geojson"):   # GeoServer vs MapServer/deegree
                try:
                    gu = base + sep + urllib.parse.urlencode(
                        {"service": "WFS", "version": "1.1.0", "request": "GetFeature",
                         "typeName": nm, "outputFormat": ofmt, "maxFeatures": per_ds})
                    gj = json.loads(_wfs_get(gu, limit_bytes=8000000))
                    break
                except Exception:
                    continue
            if gj is None:
                continue
            feats = gj.get("features") if isinstance(gj, dict) else None
            if not feats:
                continue
            got += 1
            n0 = len(out)
            for f in feats[:per_ds]:
                try:
                    ll = _geom_center(f.get("geometry") or {})
                    if not ll:
                        continue
                    props = f.get("properties") or {}
                    st = _ods_pick(props, _ODS_STATUSK)
                    sn = str(st or "").lower().replace("_", " ")
                    if any(k in sn for k in _ODS_DEAD):
                        continue
                    nmv = _ods_pick(props, _ODS_NAMEK) or ti
                    # a bare /ows endpoint returns an OWS ExceptionReport in a browser
                    # ("No service: ( ows )"), so publish a real GetFeature request
                    src = base + sep + urllib.parse.urlencode(
                        {"service": "WFS", "version": "1.1.0", "request": "GetFeature",
                         "typeName": nm, "outputFormat": "application/json",
                         "maxFeatures": 50})
                    # carry the feature's own attributes through instead of dropping them
                    det = []
                    for k, v in list(props.items()):
                        if v in (None, "", []):
                            continue
                        ks = str(k)
                        if ks.lower() in ("geometry", "the_geom", "bbox", "shape"):
                            continue
                        vs = str(v).strip()
                        if len(vs) > 90 or not vs:
                            continue
                        det.append("%s: %s" % (ks, vs))
                        if len(det) >= 10:
                            break
                    dtxt = "From %s spatial data (WFS) \u00b7 layer '%s'." % (country, ti[:60])
                    if det:
                        dtxt += " " + " \u00b7 ".join(det)
                    p = {"name": nmv[:140], "type": ("%s \u2014 %s" % (ti[:48], country)) if ti else "Geospatial dataset (%s)" % country,
                         "state": country, "lat": round(ll[0], 5), "lng": round(ll[1], 5),
                         "precise": True, "size": "", "status": st[:40], "company": "",
                         "url": src, "desc": dtxt[:600],
                         "source": "wfs_%s" % cc}
                    p["impact"] = rate_project(p, sensitivity=0)
                    out.append(p)
                except Exception:
                    continue
            if len(out) > n0:
                print("  wfs %s: +%d from '%s'" % (country, len(out) - n0, ti[:40]))
            time.sleep(0.2)
    print("  wfs federation: %d points from %d endpoints" % (len(out), len(_WFS_ENDPOINTS)))
    return out


# ========================= OGC API - Features federation =========================
# OGC API - Features (verified against ogcapi-workshop.ogc.org & pygeoapi docs):
#   GET {base}/collections?f=json -> {"collections":[{"id":..,"title":..}]}
#   GET {base}/collections/{id}/items?limit=N&f=json -> GeoJSON FeatureCollection (CRS84).
# JSON-native successor to WFS; catches pygeoapi servers that expose no WFS. Gov supply
# is thin and weather/EO-heavy -- the dev/permit/mining keyword filter drops those to 0.
_OGCAPI_ENDPOINTS = [
    ("https://api.weather.gc.ca", "Canada", "ca"),
    ("https://betageo.woudc.org/oapi", "Canada", "ca"),
    ("https://wis2-gdc.weather.gc.ca", "Canada", "ca"),
    ("https://api.geo.bs.ch/stac/v1", "Switzerland", "ch"),
    ("https://opendataapi.dmi.dk/v1/forecastdata/api", "Denmark", "dk"),
    ("https://api-coverages.idee.es", "Spain", "es"),
    ("https://api-features.idee.es", "Spain", "es"),
    ("https://api-features.ign.es", "Spain", "es"),
    ("https://api-maps.idee.es", "Spain", "es"),
    ("https://api.geosas.fr/rpg", "France", "fr"),
    ("https://api.gis.cityofathens.gr/pygeoapi", "Greece", "gr"),
    ("https://sealevelrise.kartverket.no", "Norway", "no"),
    ("https://ogcapi.dgterritorio.gov.pt", "Portugal", "pt"),
    ("https://astrogeology.usgs.gov/pygeoapi", "United States", "us"),
    ("https://geoapi.geoplatform.gov", "United States", "us"),
    ("https://labs.waterdata.usgs.gov/api/nldi/pygeoapi", "United States", "us"),
    ("https://wis2node.globaldata.nws.noaa.gov", "United States", "us"),
    ("https://wis2.dwd.de/gdc", "World", "world"),
]

def fetch_ogcapi_federation(per_endpoint=6, per_ds=600):
    out = []
    budget_min = _fed_budget("OGCAPI_BUDGET_MIN", 10, 10)
    t_end = time.time() + budget_min * 60
    _oapi_eps = _shard_list(_OGCAPI_ENDPOINTS)
    for (base, country, cc) in _oapi_eps:
        if time.time() > t_end:
            _flag("ogcapi federation hit %d-min budget -- %d endpoints not reached" %
                  (budget_min, len(_oapi_eps) - _oapi_eps.index((base, country, cc))))
            break
        root = base.rstrip("/")
        try:
            cj = _get_json(root + "/collections?f=json")
        except Exception:
            continue
        colls = (cj or {}).get("collections") or []
        matched = []
        for c in colls:
            cid = c.get("id") or c.get("name")
            title = str(c.get("title") or cid or "")
            if cid and (_WFS_KEEP.search(title) or _WFS_KEEP.search(str(cid))):
                matched.append((cid, title))
        got = 0
        for cid, title in matched[:per_endpoint]:
            try:
                iu = root + "/collections/%s/items?%s" % (
                    urllib.parse.quote(str(cid)),
                    urllib.parse.urlencode({"limit": per_ds, "f": "json"}))
                gj = _get_json(iu)
            except Exception:
                continue
            feats = gj.get("features") if isinstance(gj, dict) else None
            if not feats:
                continue
            got += 1
            n0 = len(out)
            for f in feats[:per_ds]:
                try:
                    ll = _geom_center(f.get("geometry") or {})
                    if not ll:
                        continue
                    props = f.get("properties") or {}
                    st = _ods_pick(props, _ODS_STATUSK)
                    sn = str(st or "").lower().replace("_", " ")
                    if any(k in sn for k in _ODS_DEAD):
                        continue
                    nm = _ods_pick(props, _ODS_NAMEK) or title
                    p = {"name": nm[:140], "type": "Geospatial dataset (%s)" % country,
                         "state": country, "lat": round(ll[0], 5), "lng": round(ll[1], 5),
                         "precise": True, "size": "", "status": st[:40], "company": "",
                         "url": root, "desc": "From %s spatial data (OGC API) \u00b7 %s." % (country, title[:60]),
                         "source": "ogcapi_%s" % cc}
                    p["impact"] = rate_project(p, sensitivity=0)
                    out.append(p)
                except Exception:
                    continue
            if len(out) > n0:
                print("  ogcapi %s: +%d from '%s'" % (country, len(out) - n0, title[:40]))
            time.sleep(0.2)
    print("  ogcapi federation: %d points from %d endpoints" % (len(out), len(_OGCAPI_ENDPOINTS)))
    return out


def fetch_ckan_federation(per_portal=None, per_ds=1500):
    per_portal = per_portal or (10 if os.environ.get("HARVEST_FEDERATIONS") == "1" else 6)
    out = []
    budget_min = _fed_budget("CKAN_BUDGET_MIN", 75, 110)
    t_end = time.time() + budget_min * 60
    _ckan_portals = _shard_list(_CKAN_PORTALS)
    for (base, country, cc) in _ckan_portals:
        if time.time() > t_end:
            _flag("ckan federation hit %d-min budget -- %d portals not reached" %
                  (budget_min, len(_ckan_portals) - _ckan_portals.index((base, country, cc))))
            break
        pkgs = []; seen = set()
        for term in _CKAN_TERMS[:30]:
            try:
                u = base.rstrip("/") + "/api/3/action/package_search?" + urllib.parse.urlencode(
                    {"q": term, "rows": 100})
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
            if not geo:                                   # CSV-only portal -> sniff lat/lng columns
                csvs = [r for r in (pk.get("resources") or [])
                        if str(r.get("format", "")).lower() == "csv" and r.get("url")]
                for r in csvs[:1]:
                    rows_csv = _fed_csv_points(r["url"], country, cc, "ckan_",
                                               str(pk.get("title") or "Permit"), base, per_ds)
                    if rows_csv:
                        got += 1; out.extend(rows_csv)
                        print("  ckan %s: +%d from CSV '%s'" % (country, len(rows_csv),
                                                                str(pk.get("title"))[:40]))
                    time.sleep(0.3)
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
    _print_diagnostics()
    items = [p for p in items if p.get("lat") is not None and p.get("lng") is not None]
    items = dedup(items)
    items.sort(key=lambda p: -(p.get("impact") or 0))
    # per-source preservation: if a source comes back much thinner than what is
    # already saved (e.g. PermitStack hit its daily rate limit), keep the prior
    # entries for that source instead of clobbering them.
    if _projects_exists():
        try:
            ex = _load_projects()
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
    if len(items) < 4 and _projects_exists():
        try:
            ex = _load_projects()
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
    _dump_projects(out)
    print("wrote projects.json.gz with %d projects" % len(items))
    if not items:
        print("NOTE: no sources wired yet -- fill SOCRATA_CITIES and uncomment a "
              "fetcher. The map falls back to its embedded seed set until then.")




# ---------------------------------------------------------------------------
# SHARDED OSM: the grid is far too big for one job, so N parallel jobs each take
# every Nth tile (shard k of n). Each writes osm_part_k.json; a final merge job
# folds them into projects.json. Full 817-tile global sweep on EVERY run.
# ---------------------------------------------------------------------------
def fetch_osm_shard(k, n, cap=3000):
    grid = _osm_tiles()
    todo = [grid[i] for i in range(len(grid)) if i % n == k]
    budget_min = int(os.environ.get("OSM_BUDGET_MIN", "150"))
    t_end = time.time() + budget_min * 60
    print("  osm shard %d/%d: %d tiles (of %d), budget %d min"
          % (k, n, len(todo), len(grid), budget_min))
    out = []; ok = 0; to = 0; skipped = 0
    for (s, w, n_, e) in todo:
        if time.time() > t_end:
            skipped += 1; continue
        label = "%.0f,%.0f" % (s, w)
        if _osm_fetch_box(s, w, n_, e, cap, label, out, t_end):
            ok += 1
        else:
            to += 1
        time.sleep(0.8)
    print("  osm shard %d/%d: %d sites (%d tiles ok, %d timed out, %d skipped for time)"
          % (k, n, len(out), ok, to, skipped))
    return out

def _osm_merge_parts():
    """Merge every osm_part_*.json produced by the shard jobs into projects.json.

    Prior OSM entries are kept ONLY for tiles no shard covered this run. Keeping
    prior data for tiles that WERE refreshed would resurrect exactly what the
    current filters exclude (that is how 73k stale quarries survived a run that
    no longer collects them)."""
    import glob as _glob
    parts = sorted(_glob.glob("osm_part_*.json"))
    nsh = int(os.environ.get("OSM_SHARDS", "16"))
    grid = _osm_tiles()
    fresh = []; done_shards = set()
    for f in parts:
        m = re.search(r"osm_part_(\d+)\.json$", f)
        if m: done_shards.add(int(m.group(1)))
        try:
            rows = json.load(open(f, encoding="utf-8"))
            fresh += rows if isinstance(rows, list) else []
            print("  merge: %s -> %d sites" % (f, len(rows)))
        except Exception as ex:
            print("  merge: %s unreadable: %s" % (f, ex))
    if not parts:
        print("  merge: no osm_part_*.json found -- nothing to merge"); return

    keep = _carry_sources(lambda s: s != "osm_construction", "daily sources")
    prior = _carry_sources(lambda s: s == "osm_construction", "prior osm")

    # tiles that a shard actually covered this run -> their data is authoritative
    covered = [grid[i] for i in range(len(grid)) if (i % nsh) in done_shards]
    print("  merge: %d/%d shards reported -> %d/%d tiles refreshed"
          % (len(done_shards), nsh, len(covered), len(grid)))

    def _in_covered(q):
        la, lo = q.get("lat"), q.get("lng")
        if la is None or lo is None: return False
        for (s, w, n, e) in covered:
            if s <= la < n and w <= lo < e: return True
        return False

    # Preserve-on-zero PER TILE: keep prior OSM data ONLY for 5-degree tiles that
    # produced NO fresh data this run (a dense tile that timed out even after
    # subdividing, or a genuinely empty one). Tiles that DID return fresh data are
    # authoritative and replace their prior. This is what stops a timed-out tile
    # (e.g. dense Western Europe) from wiping to an empty box.
    import math as _math
    def _tk(la, lo):
        return (int(_math.floor(la / 5.0)) * 5, int(_math.floor(lo / 5.0)) * 5)
    fresh_tiles = set()
    for q in fresh:
        la, lo = q.get("lat"), q.get("lng")
        if la is not None and lo is not None:
            fresh_tiles.add(_tk(la, lo))
    if prior:
        before = len(prior)
        prior = [q for q in prior
                 if q.get("lat") is not None and q.get("lng") is not None
                 and _tk(q["lat"], q["lng"]) not in fresh_tiles]
        print("  merge: kept %d of %d prior OSM entries (tiles with no fresh data "
              "this run -> prevents empty boxes); %d tiles had fresh data"
              % (len(prior), before, len(fresh_tiles)))

    if not fresh and not prior:
        print("  merge: nothing fresh and nothing to keep -- leaving OSM empty")
        _finish(keep); return
    print("  merge: %d fresh + %d retained OSM sites" % (len(fresh), len(prior)))
    _finish(keep + fresh + prior)

def _shard_list(lst):
    """In a federation shard job (FED_SHARD=k, FED_SHARDS=n) each of the parallel
    jobs takes every n-th entry of every portal list, so together the shards cover
    100% of the portals in one scheduled run (mirrors the OSM tile shards)."""
    sh = os.environ.get("FED_SHARD")
    if sh is None or sh == "":
        return lst
    k = int(sh); n = int(os.environ.get("FED_SHARDS", "4"))
    return [p for i, p in enumerate(lst) if i % n == k]

_FED_PREFIXES = ("ckan_", "ods_", "geonode_", "dkan_", "udata_", "wfs_", "ogcapi_")
def _is_fed(s):
    return any(str(s).startswith(p) for p in _FED_PREFIXES)

def _fed_budget(name, daily_default, fed_default):
    """Budget minutes for a federation. Explicit env var always wins. In the
    dedicated federations job the budget is the fed_default; but when the job is
    SHARDED (FED_SHARDS>1) each shard only handles 1/n of every portal list, so
    its time budget is divided by the shard count (with a small floor) to keep
    every shard well under the GitHub Actions 6-hour ceiling. In the daily job
    federations run only as a fallback, on the smaller daily_default."""
    if os.environ.get(name):
        return int(os.environ[name])
    if os.environ.get("HARVEST_FEDERATIONS") == "1":
        try:
            n = int(os.environ.get("FED_SHARDS", "1"))
        except Exception:
            n = 1
        if n > 1:
            return max(12, -(-fed_default // n))   # ceil-divide, floor 12 min
        return fed_default
    return daily_default

def _carry_sources(pred, label):
    """Reuse entries already in projects.json for sources this run isn't refreshing."""
    try:
        ex = _load_projects()
        rows = ex.get("projects", []) if isinstance(ex, dict) else (ex if isinstance(ex, list) else [])
    except Exception:
        rows = []
    keep = [q for q in rows if pred(str(q.get("source") or ""))]
    print("  [%s] carried %d entries forward (not refreshed this run)" % (label, len(keep)))
    return keep

def main():
    # merge job: fold the shard artifacts into projects.json
    if os.environ.get("OSM_MERGE") == "1":
        print("MODE: merge OSM shard parts")
        _osm_merge_parts(); return
    # shard job: harvest one slice of the tile grid, write it as an artifact
    sh = os.environ.get("OSM_SHARD")
    if sh is not None and sh != "":
        k = int(sh); n = int(os.environ.get("OSM_SHARDS", "8"))
        print("MODE: OSM shard %d of %d" % (k, n))
        rows = fetch_osm_shard(k, n)
        with open("osm_part_%d.json" % k, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))
        print("wrote osm_part_%d.json with %d sites" % (k, len(rows)))
        return
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
    if os.environ.get("FED_MERGE") == "1":
        print("MODE: merge federation shard parts")
        import glob as _glob
        items = []
        for f in sorted(_glob.glob("fed_part_*.json")):
            try:
                items += json.load(open(f, encoding="utf-8"))
                print("  merge: read %s" % f)
            except Exception as e:
                print("  merge: %s unreadable: %s" % (f, e))
        if not items:
            print("  merge: no shard data -- keeping prior federation entries")
            items = _carry_sources(_is_fed, "prior federation sources")
        items += _carry_sources(lambda s: not _is_fed(s), "non-federation sources")
        _finish(items)
        return
    fsh = os.environ.get("FED_SHARD")
    if fsh is not None and fsh != "":
        k = int(fsh); n = int(os.environ.get("FED_SHARDS", "4"))
        print("MODE: federation shard %d of %d" % (k, n))
        os.environ["HARVEST_FEDERATIONS"] = "1"   # shard gets fed-mode budgets + depth
        # Global wall-clock guard: stay safely under the Actions 6h job ceiling no
        # matter how the per-federation budgets add up. Default 315 min; each
        # federation is skipped once the guard is passed, and whatever has been
        # gathered so far is still written -- so a slow shard degrades instead of
        # being cancelled with no artifact (which would fail the merge).
        _shard_end = time.time() + int(os.environ.get("FED_SHARD_MAX_MIN", "315")) * 60
        def _fed_step(name, fn):
            if time.time() > _shard_end:
                _flag("shard %d: wall-clock guard passed -- skipping %s" % (k, name))
                return []
            return _run(name, fn)
        items = []
        # Order matters: run uData FIRST on shard 0 so its extra load can't be the
        # thing that gets starved, then the rest. Incrementally persist after each
        # federation so a hard kill still leaves a usable partial artifact.
        def _persist():
            try:
                with open("fed_part_%d.json" % k, "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, separators=(",", ":"))
            except Exception as _e:
                print("  shard %d: partial persist failed: %s" % (k, _e))
        if k == 0:
            items += _fed_step("udata_federation", fetch_udata_federation); _persist()
        items += _fed_step("ckan_federation", fetch_ckan_federation); _persist()
        items += _fed_step("ods_federation", fetch_ods_federation); _persist()
        items += _fed_step("geonode_federation", fetch_geonode_federation); _persist()
        items += _fed_step("dkan_federation", fetch_dkan_federation); _persist()
        items += _fed_step("wfs_federation", fetch_wfs_federation); _persist()
        items += _fed_step("ogcapi_federation", fetch_ogcapi_federation); _persist()
        _persist()
        print("wrote fed_part_%d.json with %d entries" % (k, len(items)))
        return
    if os.environ.get("HARVEST_FEDERATIONS") == "1":
        # Dedicated federations job: refresh ONLY the 7 portal federations with big
        # budgets (defaults sum to ~5.2h -- inside the 6h Actions job limit); keep
        # every other source exactly as the daily job last wrote it.
        print("MODE: federations refresh (dedicated job)")
        items = []
        items += _run("ckan_federation", fetch_ckan_federation)
        items += _run("ods_federation", fetch_ods_federation)
        items += _run("geonode_federation", fetch_geonode_federation)
        items += _run("dkan_federation", fetch_dkan_federation)
        items += _run("udata_federation", fetch_udata_federation)
        items += _run("wfs_federation", fetch_wfs_federation)
        items += _run("ogcapi_federation", fetch_ogcapi_federation)
        items += _carry_sources(lambda s: not _is_fed(s), "non-federation sources")
        _finish(items)
        return
    print("MODE: daily refresh (all sources except OSM + federations)")
    items = []
    items += _run("permitstack", fetch_permitstack)             # national construction permits (key)
    _SOCRATA_OFF = {"data.austintexas.gov", "data.sfgov.org", "data.lacity.org"}  # 400s; PermitStack covers these
    items += _run("arcgis_hub", fetch_arcgis_hub)               # US city/county permits (no cap)
    items += _run("socrata_permits", lambda: [p for cfg in SOCRATA_CITIES
                                              if cfg.get("domain") not in _SOCRATA_OFF
                                              for p in fetch_socrata(cfg)])
    items += _run("dc_permits", fetch_dc_permits)                     # Washington DC new-construction + demolition permits
    items += _run("tempe_permits", fetch_tempe_permits)                 # Tempe AZ major construction ($5M+)
    for _acfg in ARCGIS_PERMIT_CITIES:
        items += _run(_acfg["source"], (lambda c: (lambda: fetch_arcgis_permits(c)))(_acfg))   # generic ArcGIS permit cities
    items += _run("socrata_discovery", fetch_socrata_discovered)          # auto-discovered Socrata permit portals (capped, $5M-gated)
    items += _run("arcgis_discovery", fetch_arcgis_discovered)            # auto-discovered ArcGIS permit services (capped, $5M-gated)
    items += _run("federal_register", fetch_federal_register)   # US EIS notices
    items += _run("public_land_nepa", fetch_public_land_nepa)   # BLM + USFS via Federal Register
    items += _run("blm_arcgis", fetch_blm_arcgis)               # BLM ePlanning ArcGIS -- PRECISE open-comment NEPA points
    items += _run("ceqanet", fetch_ceqanet)                     # California CEQA/NEPA environmental filings (state clearinghouse)
    items += _run("wa_sepa", fetch_wa_sepa)                     # Washington State SEPA environmental-review filings
    items += _run("sitadel_fr", fetch_sitadel_fr)               # France national permits (automated)
    items += _run("uk_planit", fetch_ukplanit)                  # UK national planning applications
    items += _run("epbc_au", fetch_epbc_au)                     # Australia national environmental referrals
    items += _run("iaac_ca", fetch_iaac_ca)                     # Canada federal impact assessments
    items += _run("anla_co", fetch_anla_co)                     # Colombia ANLA environmental-licensing projects
    items += _run("ibama_br", fetch_ibama_br)
    items += _run("ireland_planning", fetch_ireland_planning)          # Ireland national planning DB (size-gated)
    items += _run("portugal_eia", fetch_portugal_eia)              # Portugal national EIA processes (APA/SNIAmb)                   # Brazil federal environmental licences
    items += _run("chile_seia", fetch_chile_seia)                  # Chile SEIA -- major EIA projects under evaluation (SEA)
    items += _run("peru_senace", fetch_peru_senace)                # Peru SENACE -- major projects under environmental certification
    items += _run("nsw_major", fetch_nsw_major)                    # Australia (NSW) State Significant / Major Projects register
    items += _run("qld_coordinated", fetch_qld_coordinated)        # Australia (QLD) Coordinator-General coordinated projects
    items += _run("bc_eao", fetch_bc_eao)                          # Canada (BC) Environmental Assessment Office EPIC projects
    items += _run("sask_eia", fetch_sask_eia)                      # Canada (Saskatchewan) active EA projects + applications
    items += _run("ireland_eia", fetch_ireland_eia)                # Ireland national EIA Location Point layer (CC-BY)
    items += _run("world_bank", fetch_world_bank)               # GLOBAL: active WB-financed projects
    items += _run("iati", fetch_iati)                           # GLOBAL: aid projects WITH coordinates
    items += _run("land_matrix", fetch_land_matrix)               # GLOBAL: large-scale land acquisitions (Land Matrix, country-level)
    items += _run("gem", fetch_gem)
    items += _run("emodnet_wind", fetch_emodnet_wind)          # EU SEAS: offshore wind farms in development (EMODnet Human Activities)                               # GLOBAL: proposed fossil infra -- coal plants+mines, gas, oil (Global Energy Monitor, live)
    # -- portal federations run in their OWN twice-weekly job (HARVEST_FEDERATIONS=1,
    #    projects_federations.yml) with ~3x budgets + deeper per-portal caps; the daily
    #    job just carries their last results forward, like OSM --
    items += _carry_sources(_is_fed, "federation sources")
    items += _carry_sources(lambda s: s == "osm_construction", "osm_construction")
    _finish(items)

if __name__ == "__main__":
    main()
