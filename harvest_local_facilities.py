#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
harvest_local_facilities.py
===========================
Bulk-extract record-holding local facilities from OpenStreetMap and write them
in the same JSON shape the map's facility layer already loads
(execmap_local_*.json): a flat list of

    [lat, lng, name, website, "", address, "", phone]

Only lat/lng/name are required; the rest are filled when OSM has them.

Facility sets produced (one JSON file each):
    courthouse -> execmap_local_courthouse.json   (amenity=courthouse)
    library    -> execmap_local_library.json      (amenity=library)
    archive    -> execmap_local_archive.json       (amenity=archive  --
                  the closest OSM tag for public records / archive offices;
                  dedicated "records office" tagging is sparse in OSM)

Why this looks like the OSM code in harvest_projects.py:
It reuses the SAME hardening that fixed the box-shaped gaps -- it detects
Overpass's silent server-side timeout (HTTP 200 + a "remark" field) and
RECURSIVELY quarter-splits any tile that times out, down to a small floor, so
heavy regions never come back empty.

Run:
    # all three sets, whole world
    python harvest_local_facilities.py

    # one set only
    FAC_TYPE=courthouse python harvest_local_facilities.py

    # shard the world across N parallel jobs (for a GitHub Actions matrix)
    FAC_TYPE=library FAC_SHARD=3 FAC_SHARDS=20 python harvest_local_facilities.py
    # -> writes execmap_local_library_part3.json ; merge the parts yourself
    #    (concatenate the lists) or run unsharded for a single file.

Tunables (env):
    FAC_TYPE      courthouse | library | archive | all      (default all)
    FAC_SHARD     shard index k (0..FAC_SHARDS-1)           (default: unsharded)
    FAC_SHARDS    number of shards                          (default 20)
    OSM_BUDGET_MIN wall-clock minutes before it stops early (default 150)
    OSM_MIN_DEG   recursion floor in degrees (~0.625 = 70km)(default 0.625)
    CONTACT       contact string for the User-Agent         (default below)
"""

import os, sys, time, json, urllib.request, urllib.parse

CONTACT     = os.environ.get("CONTACT", "wheelock.chris@gmail.com")
UA          = "local-map-facilities/1.0 (+%s)" % CONTACT
OSM_MIN_DEG = float(os.environ.get("OSM_MIN_DEG", "0.625"))
BUDGET_MIN  = int(os.environ.get("OSM_BUDGET_MIN", "150"))

_LAT_MIN, _LAT_MAX = -60.0, 84.0
_LNG_MIN, _LNG_MAX = -180.0, 180.0

_EPS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

# facility type -> Overpass selector (node + way + relation)
SETS = {
    "courthouse": ('node["amenity"="courthouse"](%s);'
                   'way["amenity"="courthouse"](%s);'
                   'relation["amenity"="courthouse"](%s);'),
    "library":    ('node["amenity"="library"](%s);'
                   'way["amenity"="library"](%s);'
                   'relation["amenity"="library"](%s);'),
    "archive":    ('node["amenity"="archive"](%s);'
                   'way["amenity"="archive"](%s);'
                   'relation["amenity"="archive"](%s);'),
}


def _tiles(step=5.0):
    out = []
    la = _LAT_MIN
    while la < _LAT_MAX:
        lo = _LNG_MIN
        while lo < _LNG_MAX:
            out.append((round(la, 2), round(lo, 2),
                        round(min(la + step, _LAT_MAX), 2),
                        round(min(lo + step, _LNG_MAX), 2)))
            lo += step
        la += step
    return out


def _quarters(s, w, n, e):
    ms, mw = (s + n) / 2.0, (w + e) / 2.0
    return [(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]


def _query(sel, s, w, n, e):
    bb = "%s,%s,%s,%s" % (s, w, n, e)
    body = sel % (bb, bb, bb)
    return "[out:json][timeout:70];(" + body + ");out center tags;"


def _overpass(q, label=""):
    """POST to each mirror; return parsed JSON, or None on a real/silent timeout."""
    for i, ep in enumerate(_EPS):
        try:
            req = urllib.request.Request(
                ep, data=urllib.parse.urlencode({"data": q}).encode(),
                headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=75) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            # HTTP 200 + a "remark" == server-side timeout. Treat as failure so
            # the caller splits, instead of recording an empty tile (the bug that
            # left box-shaped holes on the projects layer).
            rm = str((data or {}).get("remark") or "")
            if rm and ("timed out" in rm.lower() or "error" in rm.lower()
                       or "out of memory" in rm.lower()):
                if i == len(_EPS) - 1:
                    print("  %s server-side timeout -> will split: %s"
                          % (label, rm[:60]))
                time.sleep(1.0)
                continue
            return data
        except Exception as ex:
            if i == len(_EPS) - 1:
                print("  %s failed on all mirrors: %s" % (label, str(ex)[:60]))
            time.sleep(1.0)
    return None


def _collect(data, out):
    for el in (data.get("elements") or []):
        try:
            c = el.get("center") or {}
            lat = c.get("lat", el.get("lat"))
            lng = c.get("lon", el.get("lon"))
            if lat is None or lng is None:
                continue
            tg = el.get("tags") or {}
            name = tg.get("name") or tg.get("official_name") or tg.get("operator") or ""
            website = tg.get("website") or tg.get("contact:website") or ""
            phone = tg.get("phone") or tg.get("contact:phone") or ""
            hn = tg.get("addr:housenumber", ""); st = tg.get("addr:street", "")
            city = tg.get("addr:city", "")
            address = " ".join(x for x in [(hn + " " + st).strip(), city] if x).strip()
            out.append([round(float(lat), 5), round(float(lng), 5),
                        name[:140], website, "", address, "", phone])
        except Exception:
            continue


def _fetch_recursive(sel, s, w, n, e, deadline, out, label):
    """Query a box; on timeout, quarter-split recursively to OSM_MIN_DEG.
    Returns (ok_leaves, gaveup_leaves, splits)."""
    if deadline and time.time() > deadline:
        return (0, 1, 0)
    data = _overpass(_query(sel, s, w, n, e), label)
    if data is not None:
        _collect(data, out)
        return (1, 0, 0)
    if (n - s) <= OSM_MIN_DEG * 1.5:
        return (0, 1, 0)               # smallest box, still failing -> give up
    ok = gu = 0; sp = 1
    for (qs, qw, qn, qe) in _quarters(s, w, n, e):
        a, g, s2 = _fetch_recursive(sel, qs, qw, qn, qe, deadline, out,
                                    label + "/q")
        ok += a; gu += g; sp += s2
        time.sleep(0.5)
    return (ok, gu, sp)


def harvest(ftype):
    sel = SETS[ftype]
    grid = _tiles()
    shard = os.environ.get("FAC_SHARD")
    if shard is not None and shard != "":
        k = int(shard); nsh = int(os.environ.get("FAC_SHARDS", "20"))
        grid = [grid[i] for i in range(len(grid)) if i % nsh == k]
        tag = "_part%d" % k
        print("== %s: shard %d/%d -> %d tiles ==" % (ftype, k, nsh, len(grid)))
    else:
        tag = ""
        print("== %s: whole world -> %d tiles ==" % (ftype, len(grid)))

    t_end = time.time() + BUDGET_MIN * 60
    out = []; ok = split = gaveup = skipped = 0
    for (s, w, n, e) in grid:
        if time.time() > t_end:
            skipped += 1; continue
        label = "%s %.0f,%.0f" % (ftype, s, w)
        a, g, sp = _fetch_recursive(sel, s, w, n, e, t_end, out, label)
        if a > 0: ok += 1
        if sp > 0: split += 1
        gaveup += g
        time.sleep(0.4)

    # dedup by rounded position
    seen = set(); merged = []
    for row in out:
        key = (round(row[0], 4), round(row[1], 4))
        if key in seen:
            continue
        seen.add(key); merged.append(row)

    fn = "execmap_local_%s%s.json" % (ftype, tag)
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, separators=(",", ":"))
    print("  %s: %d facilities (%d tiles ok, %d needed splitting, %d leaves gave "
          "up, %d skipped) -> %s" % (ftype, len(merged), ok, split, gaveup,
                                     skipped, fn))
    if gaveup:
        print("  NOTE: %d leaves gave up at the %.3f-degree floor -- lower "
              "OSM_MIN_DEG if a gap remains." % (gaveup, OSM_MIN_DEG))
    return merged


def main():
    want = os.environ.get("FAC_TYPE", "all").lower()
    types = list(SETS.keys()) if want in ("", "all") else [want]
    for t in types:
        if t not in SETS:
            print("unknown FAC_TYPE %r; choose from %s" % (t, ", ".join(SETS)))
            sys.exit(2)
    for t in types:
        harvest(t)


if __name__ == "__main__":
    main()
