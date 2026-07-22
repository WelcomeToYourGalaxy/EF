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

# ---------------------------------------------------------------------------
# GEO-TAGGING: resolve each wire item to a country (ISO2) and, where possible,
# a subnational region, by scanning title+snippet against a worldwide gazetteer.
# The map's region filter reads item["iso"] and item["region"].
# Purely additive: an item that resolves to nothing is tagged iso=None.
# ---------------------------------------------------------------------------
# Country aliases -> ISO2. Kept deliberately broad but disambiguated (word-boundary
# matched at runtime). Demonyms included because headlines use them ("Brazilian dam").
_COUNTRY = {
 "US": ["united states","u.s.","u.s.a","usa","america","american"],
 "CA": ["canada","canadian"], "MX": ["mexico","mexican"],
 "BR": ["brazil","brazilian"], "AR": ["argentina","argentine","argentinian"],
 "CL": ["chile","chilean"], "PE": ["peru","peruvian"], "CO": ["colombia","colombian"],
 "EC": ["ecuador","ecuadorian"], "BO": ["bolivia","bolivian"], "VE": ["venezuela","venezuelan"],
 "PY": ["paraguay"], "UY": ["uruguay"], "GT": ["guatemala"], "HN": ["honduras"],
 "PA": ["panama"], "CR": ["costa rica"], "NI": ["nicaragua"], "DO": ["dominican republic"],
 "GB": ["united kingdom","britain","british","england","scotland","wales","northern ireland"," uk "],
 "IE": ["ireland","irish"], "FR": ["france","french"], "DE": ["germany","german"],
 "ES": ["spain","spanish"], "PT": ["portugal","portuguese"], "IT": ["italy","italian"],
 "NL": ["netherlands","dutch"], "BE": ["belgium","belgian"], "SE": ["sweden","swedish"],
 "NO": ["norway","norwegian"], "FI": ["finland","finnish"], "DK": ["denmark","danish"],
 "PL": ["poland","polish"], "CZ": ["czech"], "AT": ["austria","austrian"], "CH": ["switzerland","swiss"],
 "GR": ["greece","greek"], "RO": ["romania"], "HU": ["hungary"], "UA": ["ukraine","ukrainian"],
 "RU": ["russia","russian"], "TR": ["turkey","turkish","turkiye"], "RS": ["serbia"], "BG": ["bulgaria"],
 "HR": ["croatia"], "BA": ["bosnia"], "SK": ["slovakia"], "SI": ["slovenia"],
 "CN": ["china","chinese"], "IN": ["india","indian"], "PK": ["pakistan"], "BD": ["bangladesh"],
 "JP": ["japan","japanese"], "KR": ["south korea","korean","korea"], "ID": ["indonesia","indonesian"],
 "PH": ["philippines","philippine","filipino"], "VN": ["vietnam","vietnamese"], "TH": ["thailand","thai"],
 "MY": ["malaysia","malaysian"], "MM": ["myanmar","burma"], "KH": ["cambodia"], "LA": ["laos"],
 "NP": ["nepal"], "LK": ["sri lanka"], "KZ": ["kazakhstan"], "MN": ["mongolia"], "TW": ["taiwan","taiwanese"],
 "AU": ["australia","australian"], "NZ": ["new zealand"," nz "],
 "PG": ["papua new guinea"], "FJ": ["fiji"], "SB": ["solomon islands"],
 "ZA": ["south africa","south african"], "NG": ["nigeria","nigerian"], "KE": ["kenya","kenyan"],
 "GH": ["ghana"], "TZ": ["tanzania"], "UG": ["uganda"], "ET": ["ethiopia"], "CD": ["congo","drc"],
 "CG": ["republic of congo"], "CM": ["cameroon"], "CI": ["ivory coast","cote d'ivoire"],
 "SN": ["senegal"], "ML": ["mali"], "ZM": ["zambia"], "ZW": ["zimbabwe"], "MZ": ["mozambique"],
 "AO": ["angola"], "NA": ["namibia"], "BW": ["botswana"], "MG": ["madagascar"], "RW": ["rwanda"],
 "MA": ["morocco","moroccan"], "DZ": ["algeria"], "TN": ["tunisia"], "EG": ["egypt","egyptian"],
 "LY": ["libya"], "SD": ["sudan"], "SA": ["saudi arabia","saudi"], "AE": ["united arab emirates","uae"],
 "IL": ["israel","israeli"], "PS": ["palestine","palestinian","gaza","west bank"], "IQ": ["iraq","iraqi"],
 "IR": ["iran","iranian"], "SY": ["syria","syrian"], "JO": ["jordan"], "LB": ["lebanon"],
 "YE": ["yemen"], "OM": ["oman"], "QA": ["qatar"], "KW": ["kuwait"], "AZ": ["azerbaijan"],
 "GE": ["georgia"], "AM": ["armenia"], "UZ": ["uzbekistan"], "AF": ["afghanistan"],
}
# Subnational regions -> (ISO2, canonical region name). Federations + hotspots where
# fights are commonly datelined by state/province. Matched before country so a state
# name also resolves the country.
_REGION = {
 # US states (subset most datelined; extendable)
 "california":("US","California"),"texas":("US","Texas"),"florida":("US","Florida"),
 "new york":("US","New York"),"pennsylvania":("US","Pennsylvania"),"ohio":("US","Ohio"),
 "west virginia":("US","West Virginia"),"virginia":("US","Virginia"),"louisiana":("US","Louisiana"),
 "north dakota":("US","North Dakota"),"south dakota":("US","South Dakota"),"montana":("US","Montana"),
 "wyoming":("US","Wyoming"),"colorado":("US","Colorado"),"arizona":("US","Arizona"),
 "new mexico":("US","New Mexico"),"nevada":("US","Nevada"),"utah":("US","Utah"),
 "oregon":("US","Oregon"),"washington state":("US","Washington"),"alaska":("US","Alaska"),
 "minnesota":("US","Minnesota"),"wisconsin":("US","Wisconsin"),"michigan":("US","Michigan"),
 "illinois":("US","Illinois"),"georgia state":("US","Georgia"),"north carolina":("US","North Carolina"),
 "south carolina":("US","South Carolina"),"tennessee":("US","Tennessee"),"kentucky":("US","Kentucky"),
 "alabama":("US","Alabama"),"mississippi":("US","Mississippi"),"appalachia":("US","Appalachia"),
 # Canada
 "alberta":("CA","Alberta"),"british columbia":("CA","British Columbia"),"ontario":("CA","Ontario"),
 "quebec":("CA","Quebec"),"saskatchewan":("CA","Saskatchewan"),"manitoba":("CA","Manitoba"),
 "nova scotia":("CA","Nova Scotia"),"newfoundland":("CA","Newfoundland and Labrador"),
 # Australia
 "queensland":("AU","Queensland"),"new south wales":("AU","New South Wales"),"victoria":("AU","Victoria"),
 "western australia":("AU","Western Australia"),"south australia":("AU","South Australia"),
 "tasmania":("AU","Tasmania"),"northern territory":("AU","Northern Territory"),
 # Brazil
 "amazonas":("BR","Amazonas"),"para":("BR","Para"),"mato grosso":("BR","Mato Grosso"),
 "minas gerais":("BR","Minas Gerais"),"bahia":("BR","Bahia"),"sao paulo":("BR","Sao Paulo"),
 "rondonia":("BR","Rondonia"),"maranhao":("BR","Maranhao"),
 # Argentina
 "mendoza":("AR","Mendoza"),"chubut":("AR","Chubut"),"catamarca":("AR","Catamarca"),
 "jujuy":("AR","Jujuy"),"neuquen":("AR","Neuquen"),"la rioja":("AR","La Rioja"),
 # India
 "odisha":("IN","Odisha"),"jharkhand":("IN","Jharkhand"),"chhattisgarh":("IN","Chhattisgarh"),
 "maharashtra":("IN","Maharashtra"),"goa":("IN","Goa"),"karnataka":("IN","Karnataka"),
 "tamil nadu":("IN","Tamil Nadu"),"gujarat":("IN","Gujarat"),"assam":("IN","Assam"),
 # Indonesia / Philippines / others
 "sumatra":("ID","Sumatra"),"kalimantan":("ID","Kalimantan"),"papua":("ID","Papua"),
 "sulawesi":("ID","Sulawesi"),"java":("ID","Java"),
 "mindanao":("PH","Mindanao"),"luzon":("PH","Luzon"),"palawan":("PH","Palawan"),
 # UK nations
 "scotland":("GB","Scotland"),"wales":("GB","Wales"),"northern ireland":("GB","Northern Ireland"),
 # Mexico / Chile / Peru hotspots
 "oaxaca":("MX","Oaxaca"),"chiapas":("MX","Chiapas"),"sonora":("MX","Sonora"),"yucatan":("MX","Yucatan"),
 "atacama":("CL","Atacama"),"antofagasta":("CL","Antofagasta"),"patagonia":("CL","Patagonia"),
 "cajamarca":("PE","Cajamarca"),"cusco":("PE","Cusco"),"puno":("PE","Puno"),
 # South Africa
 "mpumalanga":("ZA","Mpumalanga"),"limpopo":("ZA","Limpopo"),"kwazulu":("ZA","KwaZulu-Natal"),
 "eastern cape":("ZA","Eastern Cape"),
}
_GLOBAL_HINT = [" eu ","european union","european commission","united nations"," un ","u.n.","international",
 "worldwide","global","cross-border","transnational","treaty","cop28","cop29","cop30"]

def _geo_tag(text):
    """Return (iso, region) for a wire item. region may be '' if only country resolves.
    Multi-country or explicitly global items get iso='GL' (Global) so the filter can
    surface them under every region view as context."""
    s = " " + re.sub(r"[^a-z ]", " ", (text or "").lower()) + " "
    # region first (also fixes country)
    region = ""; iso = None
    for name, (cc, canon) in _REGION.items():
        if (" " + name + " ") in s:
            iso, region = cc, canon; break
    # countries: collect distinct hits
    hits = []
    for cc, aliases in _COUNTRY.items():
        for a in aliases:
            a2 = a if a.startswith(" ") or len(a) > 4 else " " + a + " "
            if a2 in s or (" " + a + " ") in s:
                hits.append(cc); break
    hits = list(dict.fromkeys(hits))
    if iso is None:
        if len(hits) == 1:
            iso = hits[0]
        elif len(hits) >= 2:
            iso = "GL"                      # multiple countries -> global/cross-border
    if any(h in s for h in _GLOBAL_HINT) and (iso is None or (region == "" and len(hits) >= 1 and iso != "GL")):
        # explicit global/bloc language present and no single clear local dateline -> global
        if region == "":
            iso = "GL"
    return iso, region


MOVEMENT = [
    # strict=True now: only items that name a concrete project/land-use fight get
    # through, and the weight is lower so scene round-ups stop dominating the wire.
    ("It's Going Down", "https://itsgoingdown.org/feed/", 3, True),
    ("Unicorn Riot",    "https://unicornriot.ninja/feed/", 3, True),
    ("Earth First! Journal", "https://earthfirstjournal.news/feed/", 3, True),
]
INVESTIGATIVE = [
    ("Grist",    "https://grist.org/feed/",          2, True),
    ("DeSmog",   "https://www.desmog.com/feed/",      2, True),
    ("Mongabay", "https://news.mongabay.com/feed/",   2, True),
    ("Inside Climate News", "https://insideclimatenews.org/feed/", 2, True),
    ("The Narwhal", "https://thenarwhal.ca/feed/",    2, True),
    ("Climate Home News", "https://www.climatechangenews.com/feed/", 2, True),
    ("Guardian Environment", "https://www.theguardian.com/environment/rss", 2, True),
    ("Mongabay Latam", "https://es.mongabay.com/feed/", 2, True),
    ("Mongabay India", "https://india.mongabay.com/feed/", 2, True),
    ("Mongabay Africa", "https://africa.mongabay.com/feed/", 2, True),
    ("The Third Pole", "https://www.thethirdpole.net/en/feed/", 2, True),
    ("Down To Earth (India)", "https://www.downtoearth.org.in/rss", 2, True),
]
FRONTS = [
    # A few high-profile US fights...
    "Mountain Valley Pipeline", "Line 5 pipeline", "Willow project Alaska drilling",
    "CP2 LNG terminal", "Resolution Copper Oak Flat",
    # ...balanced against major fights on every other continent, so the wire reads
    # as global coverage of the biggest, most irreversible projects.
    "EACOP East African Crude Oil Pipeline", "Uganda Tilenga oil drilling",
    "Adani Carmichael coal mine Australia", "Cerrejon coal mine Colombia",
    "Cobre Panama mine", "Rio Tinto Jadar lithium Serbia",
    "Grand Inga dam Congo", "ReconAfrica Okavango drilling",
    "Indonesia nickel mining Sulawesi deforestation", "Papua palm oil deforestation",
    "Amazon deforestation highway BR-319", "Trans Mountain pipeline Canada",
    "deep sea mining Pacific", "Balkans hydropower dam protest",
    "Hasdeo coal mine India", "Andes lithium mining protest",
    # --- Broadened country/region coverage: real, high-profile land & environmental
    # fights across every continent, so many more countries surface in the wire's
    # region filter. Each is a named fight that geo-resolves to its country.
    # Africa
    "Sengwer eviction Kenya forest", "Lamu coal plant Kenya", "Ogoni Shell cleanup Nigeria",
    "Niger Delta oil spill", "TotalEnergies Mozambique LNG Cabo Delgado", "Congo peatland oil auction",
    "Tanzania Uganda EACOP pipeline", "South Africa Wild Coast Shell seismic", "Xolobeni titanium mine",
    "Ghana bauxite Atewa forest", "Zambia copper mine pollution", "Zimbabwe Hwange coal",
    "Botswana Okavango oil drilling", "Madagascar mine Base Toliara", "Morocco Western Sahara phosphate",
    "DRC cobalt mining", "Ethiopia Gibe dam", "Senegal Bargny coal plant",
    # Asia
    "Philippines Kaliwa dam", "Philippines nickel mining Palawan", "Indonesia Rempang eco city eviction",
    "Indonesia Wadas quarry", "India Hasdeo Aranya coal", "India Great Nicobar project",
    "India Mumbai coastal road Aarey", "Bangladesh Rampal power plant Sundarbans", "Nepal Nijgadh airport forest",
    "Cambodia Koh Kong Cardamom", "Myanmar Myitsone dam", "Thailand Mekong dam protest",
    "Vietnam coal power Mekong delta", "Japan Henoko base landfill Okinawa", "South Korea Jeju naval base",
    "Malaysia Baram dam Sarawak", "Pakistan Reko Diq mine", "Sri Lanka Adani wind Mannar",
    "Mongolia Oyu Tolgoi mine water", "Kazakhstan uranium mining",
    # Latin America
    "Peru Conga mine", "Peru Tia Maria copper", "Bolivia lithium Salar de Uyuni",
    "Chile Dominga mine", "Chile Escondida water", "Argentina lithium Salinas Grandes",
    "Argentina Mendoza mining law protest", "Ecuador Yasuni oil drilling", "Ecuador Intag mining",
    "Colombia Hidroituango dam", "Brazil Belo Monte dam", "Brazil Ferrogrão railway Amazon",
    "Mexico Maya Train Tren Maya", "Mexico Dos Bocas refinery", "Panama Donoso copper mine",
    "Guatemala Escobal silver mine", "Honduras Guapinol river", "Venezuela Arco Minero Orinoco",
    # Europe
    "Serbia Rio Tinto Jadar lithium", "Portugal Barroso lithium mine", "Spain Aguas Tenidas mine",
    "Norway Fosen wind Sami", "Sweden Kallak iron mine", "Finland Terrafame mine",
    "Germany Lützerath coal mine", "France A69 motorway protest", "Italy TAP pipeline",
    "Greece Skouries gold mine", "Romania Rosia Montana", "Poland Turów coal mine",
    "UK Cumbria coal mine", "Ireland LNG Shannon",
    # Middle East / Caucasus / Pacific
    "Turkey Mount Ida gold mine Kaz", "Turkey Akbelen forest coal", "Armenia Amulsar gold mine",
    "Georgia Namakhvani dam", "Iran Karun dam", "Papua New Guinea Wafi Golpu mine",
    "Fiji seabed mining", "Australia Beetaloo fracking", "New Zealand seabed mining Taranaki",
    # generic global fronts (region-neutral phrasing)
    "Indigenous land defenders mine pipeline", "old growth logging protest",
    "LNG terminal opposition", "pipeline blockade protest",
]
def google_news(q):
    from urllib.parse import quote
    return ("Front: " + q,
            "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en" % quote(q),
            2, True)

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
    "hydropower","hydroelectric","palm oil","nickel","cobalt","bauxite","gold mine","copper mine",
    "crude oil pipeline","offshore drilling","seabed mining","deep-sea mining","megadam","reservoir dam",
    "land defender","land grab","evict","displacement","rainforest","peatland","mangrove","biodiversity",
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
        per_feed = 0
        for e in fp.entries[:25]:
            if per_feed >= 6:
                break
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
            per_feed += 1
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
            _iso, _region = _geo_tag(title + " " + summary)
            items.append({"name": name, "title": title[:200], "link": link,
                          "date": date_ms, "sig": weight, "snippet": summary[:280],
                          "iso": _iso, "region": _region, "score": score})
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
