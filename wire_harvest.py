#!/usr/bin/env python3
"""
wire_harvest.py  --  builds wire.json for the Global Wire on the activist map.

RUN ENVIRONMENT: GitHub Actions (scheduled), NOT the build sandbox.

The wire is a HYBRID, per the design we settled on:
  (1) RESISTANCE VOICE  -- movement outlets that cover a fight when it blows up
      (It's Going Down, Unicorn Riot, Earth First! Journal). Weighted to the top.
  (2) NAMED FRONTS      -- tight Google-News RSS queries for specific fights, so
      local-paper coverage the movement outlets miss still surfaces.
  (3) INVESTIGATIVE      -- a thin, HARD-FILTERED layer (Grist/DeSmog/Mongabay) kept
      low-weight because it skews to narrative/pattern pieces, not episodic ones.

Everything passes through a keyword ALLOW-LIST so general climate/policy noise is
dropped and only development/company/ecosystem items remain. Output: wire.json,
which the map's Global Wire reads (point the wire fetch at this file).

Dependency: feedparser  (pip install feedparser)
"""
import json, time, datetime, html, re
import feedparser

# ---------------------------------------------------------------------------
# SOURCES
# ---------------------------------------------------------------------------
# weight: how strongly to float an item to the top (movement voice dominates)
# strict: if True, item must hit the allow-list; movement sources are already
#         on-theme so they use a looser gate.
MOVEMENT = [
    ("It's Going Down", "https://itsgoingdown.org/feed/", 5, False),
    ("Unicorn Riot",    "https://unicornriot.ninja/feed/", 5, False),
    ("Earth First! Journal", "https://earthfirstjournal.news/feed/", 5, False),
]
INVESTIGATIVE = [
    ("Grist",    "https://grist.org/feed/",          2, True),
    ("DeSmog",   "https://www.desmog.com/feed/",      2, True),
    ("Mongabay", "https://news.mongabay.com/feed/",   2, True),
]
# named fronts -> Google News RSS (episodic local coverage of specific fights)
FRONTS = [
    "Mountain Valley Pipeline", "Line 5 pipeline", "Thacker Pass lithium",
    "Cop City forest Atlanta", "CP2 LNG", "Formosa Plastics Louisiana",
    "Willow project Alaska drilling", "Resolution Copper Oak Flat", "Pebble Mine Bristol Bay",
    "pipeline blockade", "old growth logging protest", "lithium mine protest",
    "data center water fight", "LNG terminal Gulf Coast", "coal ash landfill fight",
]
def google_news(q):
    from urllib.parse import quote
    return ("Google News: " + q,
            "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en" % quote(q),
            3, True)

# ---------------------------------------------------------------------------
# KEYWORD ALLOW-LIST  (item text must contain at least one)
# ---------------------------------------------------------------------------
ALLOW = [
    "pipeline","lng","refinery","petrochemical","cracker plant","gas plant","power plant",
    "coal","oil","drilling","fracking","frack","well pad","compressor",
    "mine","mining","lithium","copper","quarry","tailings","strip mine","mountaintop",
    "clearcut","clear-cut","old growth","old-growth","logging","timber","deforestation",
    "wetland","waterway","aquifer","watershed","estuary","floodplain",
    "landfill","incinerator","cafo","factory farm","feedlot","hog farm",
    "data center","warehouse","distribution center","rezoning","zoning","subdivision","sprawl",
    "dam","reservoir","transmission line","substation","highway","interchange","port expansion",
    "blockade","tree sit","tree-sit","encampment","land defense","water protector","frontline",
    "eminent domain","easement","permit","comment period","draft eis","environmental review",
    "nepa","army corps","ferc","zoning board","planning commission","conservation easement",
]
def matches(text):
    t = text.lower()
    return any(k in t for k in ALLOW)

# ---------------------------------------------------------------------------
# HARVEST
# ---------------------------------------------------------------------------
def clean(s):
    return html.unescape(re.sub("<[^>]+>", "", s or "")).strip()

def collect():
    feeds = list(MOVEMENT) + list(INVESTIGATIVE) + [google_news(q) for q in FRONTS]
    seen, items = set(), []
    for name, url, weight, strict in feeds:
        try:
            fp = feedparser.parse(url)
        except Exception as e:
            print("  feed %s failed: %s" % (name, e)); continue
        for e in fp.entries[:25]:
            title = clean(e.get("title"))
            summary = clean(e.get("summary", ""))
            link = e.get("link", "")
            if not title or not link: continue
            blob = title + " " + summary
            if strict and not matches(blob):      # gate investigative + google news
                continue
            key = re.sub(r"[^a-z0-9]", "", title.lower())[:60]
            if key in seen: continue
            seen.add(key)
            # recency
            ts = None
            for f in ("published_parsed", "updated_parsed"):
                if e.get(f):
                    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", e.get(f)); break
            hits = sum(1 for k in ALLOW if k in blob.lower())
            score = weight * 10 + hits + (5 if ts and ts[:10] >= (
                datetime.date.today() - datetime.timedelta(days=7)).isoformat() else 0)
            items.append({"title": title[:200], "source": name, "url": link,
                          "published": ts or "", "snippet": summary[:280],
                          "weight": weight, "score": score})
    items.sort(key=lambda x: (-x["score"], x["published"]), reverse=False)
    items.sort(key=lambda x: -x["score"])
    return items

def main():
    items = collect()[:60]
    out = {"_meta": {"generated": datetime.datetime.utcnow().isoformat() + "Z",
                     "count": len(items),
                     "note": "Environmental-resistance wire: movement outlets + named-front "
                             "Google-News queries + hard-filtered investigative, keyword-gated "
                             "and movement-weighted."},
           "items": items}
    with open("wire.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("wrote wire.json with %d items" % len(items))

if __name__ == "__main__":
    main()
