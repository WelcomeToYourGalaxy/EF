#!/usr/bin/env python3
"""
wire_harvest.py  --  builds wire.json for the Global Wire on the activist map.

RUN ENVIRONMENT: GitHub Actions (scheduled), NOT the build sandbox.
OUTPUT: wire.json -- a TOP-LEVEL JSON ARRAY of {name,title,link,date,sig,snippet}.
The map checks Array.isArray(...), so the output MUST be an array, not an object.
Dependency: feedparser  (pip install feedparser)
"""
import json, time, datetime, html, re, os, calendar
import feedparser

MOVEMENT = [
    ("It's Going Down", "https://itsgoingdown.org/feed/", 5, False),
    ("Unicorn Riot",    "https://unicornriot.ninja/feed/", 5, False),
    ("Earth First! Journal", "https://earthfirstjournal.news/feed/", 5, False),
]
INVESTIGATIVE = [
    ("Grist",    "https://grist.org/feed/",          2, True),
    ("DeSmog",   "https://www.desmog.com/feed/",      2, True),
    ("Mongabay", "https://news.mongabay.com/feed/",   2, True),
    ("Inside Climate News", "https://insideclimatenews.org/feed/", 2, True),
    ("The Narwhal", "https://thenarwhal.ca/feed/",    2, True),
]
FRONTS = [
    "Mountain Valley Pipeline", "Line 5 pipeline", "Thacker Pass lithium",
    "Cop City forest Atlanta", "CP2 LNG", "Formosa Plastics Louisiana",
    "Willow project Alaska drilling", "Resolution Copper Oak Flat", "Pebble Mine Bristol Bay",
    "pipeline blockade", "old growth logging protest", "lithium mine protest",
    "data center water fight", "LNG terminal Gulf Coast", "coal ash landfill fight",
]
def google_news(q):
    from urllib.parse import quote
    return ("Front: " + q,
            "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en" % quote(q),
            3, True)

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
            if not title or not link:
                continue
            blob = title + " " + summary
            if strict and not matches(blob):
                continue
            key = re.sub(r"[^a-z0-9]", "", title.lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            ts = None
            for f in ("published_parsed", "updated_parsed"):
                if e.get(f):
                    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", e.get(f)); break
            hits = sum(1 for k in ALLOW if k in blob.lower())
            recent = ts and ts[:10] >= (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
            score = weight * 10 + hits + (5 if recent else 0)
            date_ms = 0
            if ts:
                try: date_ms = int(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) * 1000)
                except Exception: date_ms = 0
            if not date_ms:
                date_ms = int(time.time() * 1000)
            items.append({"name": name, "title": title[:200], "link": link,
                          "date": date_ms, "sig": weight, "snippet": summary[:280],
                          "score": score})
    items.sort(key=lambda x: -x["score"])
    return items

def main():
    items = collect()[:60]
    for it in items:
        it.pop("score", None)
    # Safety: if too few items came back, keep the existing wire.json rather than wiping it.
    if len(items) < 4 and os.path.exists("wire.json"):
        try:
            existing = json.load(open("wire.json", encoding="utf-8"))
            if isinstance(existing, list) and len(existing) >= len(items):
                print("harvest thin (%d) -- keeping existing wire.json (%d)" % (len(items), len(existing)))
                return
        except Exception:
            pass
    # TOP-LEVEL ARRAY -- required by the map's Array.isArray() check
    with open("wire.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    print("wrote wire.json with %d items" % len(items))

if __name__ == "__main__":
    main()
