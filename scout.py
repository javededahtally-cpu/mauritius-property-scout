#!/usr/bin/env python3
"""Mauritius Property Scout - standalone scanner.

Runs without Claude. Reads docs/data/db.json (matches, searches, comparables,
settings), visits the property sites listed in config.yaml, adds new matching
adverts, records price cuts, refreshes the exchange rate, sends alerts
(ntfy push to your iPhone, optional email) and writes db.json back.

Run it:  python scout.py          (normal run)
         python scout.py --dry    (no alerts, no saving - for testing)
"""
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import smtplib
import sys
import time
import urllib.robotparser
from email.mime.text import MIMEText
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "docs", "data", "db.json")
CFG_PATH = os.path.join(ROOT, "config.yaml")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36 PropertyScout/1.0 (personal use)"
PERCH = 42.21
ARPENT = 4220.87
DRY = "--dry" in sys.argv
NOW = dt.datetime.now(dt.timezone.utc)
ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

# Words that rule an advert out for a Mauritian (non-foreign) resale buyer.
EXCLUDE_ALWAYS = [
    r"\bfor rent\b", r"\bto rent\b", r"\bà louer\b", r"\ba louer\b", r"\blocation saisonni", r"\bto let\b", r"\bwanted\b",
    r"\bPDS\b", r"\bIRS\b", r"\bRES scheme\b", r"\bsmart city\b", r"\bG\+2\b", r"accessible to foreigners only",
    r"\boff[- ]plan\b", r"\bVEFA\b", r"\bsur plan\b",
]
TYPE_WORDS = [
    ("penthouse", "penthouse"), ("duplex", "townhouse"), ("townhouse", "townhouse"), ("triplex", "townhouse"),
    ("bungalow", "bungalow"), ("apartment", "apartment"), ("appartement", "apartment"), ("flat", "apartment"),
    ("studio", "apartment"), ("villa", "villa"), ("house", "villa"), ("maison", "villa"),
    ("agricultural", "land_agri"), ("agricole", "land_agri"), ("commercial land", "land_com"),
    ("terrain", "land_res"), ("land", "land_res"), ("plot", "land_res"),
]
GROUP = {"villa": "house", "bungalow": "house", "townhouse": "house", "apartment": "apartment",
         "penthouse": "apartment", "land_res": "land_res", "land_agri": "land_agri", "land_com": "land_com"}


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def slug(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def num(s):
    s = (s or "").replace("\u202f", "").replace("\xa0", "").replace(" ", "").replace(",", "").replace("'", "")
    try:
        return float(s)
    except ValueError:
        return None


class Fetcher:
    def __init__(self, delay):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9,fr;q=0.8"})
        self.delay = delay
        self.robots = {}
        self.last = {}
        self.status = {}  # host -> {"ok":n,"fail":n,"blocked":n,"last":msg}

    def _note(self, host, key, msg=""):
        st = self.status.setdefault(host, {"ok": 0, "fail": 0, "blocked": 0, "last": ""})
        st[key] += 1
        if msg:
            st["last"] = msg

    def allowed(self, url):
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=15)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except Exception:
                rp.parse([])
            self.robots[base] = rp
        try:
            return self.robots[base].can_fetch(UA, url)
        except Exception:
            return True

    def get(self, url):
        host = urlparse(url).netloc
        if not self.allowed(url):
            self._note(host, "blocked", "robots.txt asks us not to read this page")
            return None
        wait = self.delay - (time.time() - self.last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self.last[host] = time.time()
        try:
            r = self.s.get(url, timeout=25)
        except Exception as e:
            self._note(host, "fail", f"couldn't connect ({type(e).__name__})")
            return None
        if r.status_code in (401, 403, 429, 503):
            self._note(host, "blocked", f"site refused the visit (HTTP {r.status_code})")
            return None
        if r.status_code != 200:
            self._note(host, "fail", f"HTTP {r.status_code}")
            return None
        self._note(host, "ok", "readable")
        r.encoding = r.encoding or "utf-8"
        return r.text


# ---------------------------------------------------------------- parsing
def jsonld(soup):
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                out.append(d)
                for v in d.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(d, list):
                stack.extend(d)
    return out


def meta(soup, *names):
    for n in names:
        t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if t and t.get("content"):
            return unescape(t["content"]).strip()
    return None


PRICE_RE = re.compile(r"(?:Rs\.?|MUR|₨)\s*([0-9][0-9\s,.'\u202f\xa0]{4,})|([0-9][0-9\s,.'\u202f\xa0]{4,})\s*(?:Rs|MUR|roupies)", re.I)
EUR_RE = re.compile(r"(?:€|EUR)\s*([0-9][0-9\s,.'\u202f\xa0]{4,})|([0-9][0-9\s,.'\u202f\xa0]{4,})\s*(?:€|EUR|euros?)", re.I)
M2_RE = re.compile(r"([0-9][0-9\s,.]*)\s*(?:m²|m2|sqm|sq\.? ?m|mètres? carrés)", re.I)
PERCH_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*(?:perches?|toises?)", re.I)
ARPENT_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*arpents?", re.I)
BED_RE = re.compile(r"([0-9]{1,2})\s*(?:-|\s)?(?:bed(?:room)?s?|chambres?|ch\b)", re.I)
PHONE_RE = re.compile(r"(?:\+?230[\s-]?)?\b(5\d{3}[\s-]?\d{4}|[2-4,6]\d{2}[\s-]?\d{4})\b")
WA_RE = re.compile(r"wa\.me/(\+?\d{7,15})|api\.whatsapp\.com/send\?phone=(\+?\d{7,15})")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
DATE_RE = re.compile(r"(?:posted|published|publié|mis en ligne|date)[^0-9]{0,25}(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})", re.I)


def first_number(rx, text, lo, hi):
    for m in rx.finditer(text):
        v = num(next(g for g in m.groups() if g))
        if v and lo <= v <= hi:
            return v
    return None


def parse_advert(html, url, towns, fx):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        if t.get("type") != "application/ld+json":
            t.decompose()
    lds = jsonld(soup)
    title = meta(soup, "og:title", "twitter:title") or (soup.h1.get_text(" ", strip=True) if soup.h1 else "") or (soup.title.string if soup.title else "")
    desc = meta(soup, "og:description", "description") or ""
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))[:40000]
    full = f"{title} {desc} {text}"

    price, currency = None, "MUR"
    for d in lds:
        off = d.get("offers") if isinstance(d.get("offers"), dict) else d
        p, c = off.get("price"), (off.get("priceCurrency") or "").upper()
        if p and c in ("MUR", "EUR", "USD"):
            price, currency = num(str(p)), c
            break
    if not price:
        price = first_number(PRICE_RE, f"{title} {desc} {text[:6000]}", 200_000, 2_000_000_000)
    if not price:
        e = first_number(EUR_RE, f"{title} {desc} {text[:6000]}", 5_000, 50_000_000)
        if e:
            price, currency = e, "EUR"
    if price and currency == "EUR":
        price = round(price * fx.get("eurMur", 52.9))
    elif price and currency == "USD":
        price = round(price * fx.get("usdMur", 47.2))

    low = full.lower()
    typ = None
    for w, t in TYPE_WORDS:
        if w in (title or "").lower():
            typ = t
            break
    if not typ:
        for w, t in TYPE_WORDS:
            if w in low[:3000]:
                typ = t
                break

    land = None
    m = ARPENT_RE.search(full)
    if m:
        land = num(m.group(1).replace(",", ".")) * ARPENT
    m = PERCH_RE.search(full)
    if m and not land:
        v = num(m.group(1).replace(",", "."))
        if v and 2 <= v <= 2000:
            land = v * PERCH
    sizes = [v for v in (num(x) for x in M2_RE.findall(full[:8000])) if v and 15 <= v <= 200000]
    living = None
    if typ and typ.startswith("land"):
        land = land or (max(sizes) if sizes else None)
    else:
        small = [v for v in sizes if v <= 1500]
        living = small[0] if small else None
        big = [v for v in sizes if living and v > living * 1.3]
        land = land or (big[0] if big else None)
    beds = None
    m = BED_RE.search(f"{title} {desc} {text[:3000]}")
    if m:
        beds = int(m.group(1))

    loc = None
    for t in sorted(towns, key=len, reverse=True):
        if re.search(r"\b" + re.escape(t.lower()) + r"\b", (title + " " + url.replace("-", " ")).lower()):
            loc = t
            break
    if not loc:
        for t in sorted(towns, key=len, reverse=True):
            if re.search(r"\b" + re.escape(t.lower()) + r"\b", low[:4000]):
                loc = t
                break

    photos = []
    og = meta(soup, "og:image")
    if og:
        photos.append(urljoin(url, og))
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if re.search(r"\.(jpe?g|webp)(\?|$)", src, re.I) and not re.search(r"logo|icon|avatar|flag", src, re.I):
            photos.append(urljoin(url, src))
    photos = list(dict.fromkeys(photos))[:15]

    raw = str(soup)
    wa = WA_RE.search(raw)
    phones = list(dict.fromkeys("+230" + re.sub(r"\D", "", p) for p in PHONE_RE.findall(text[:20000])))[:3]
    emails = [e for e in EMAIL_RE.findall(text[:20000]) if not e.lower().endswith((".png", ".jpg"))][:1]
    date = None
    for d in lds:
        for k in ("datePosted", "datePublished", "dateCreated"):
            if d.get(k):
                date = str(d[k])[:10]
                break
    if not date:
        m = DATE_RE.search(text)
        if m:
            date = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    pool = bool(re.search(r"\b(pool|piscine)\b", low))
    sea = bool(re.search(r"sea view|vue mer|vue sur (la )?mer|ocean view|seaview", low))
    newb = bool(re.search(r"\bnew (build|development|project)\b|\bnouveau projet\b|\bbrand new\b|\bnewly built\b", low))
    return {
        "title": (title or "").strip()[:160], "description": desc[:600], "details": text[:1500],
        "price": int(price) if price else None, "currency": "MUR",
        "priceText": f"Rs {int(price):,}" if price else None,
        "type": typ, "livingM2": round(living) if living else None, "landM2": round(land) if land else None,
        "beds": beds, "baths": None, "location": loc, "photoUrls": photos,
        "phone": phones[0] if phones else None, "whatsapp": ("+" + (wa.group(1) or wa.group(2)).lstrip("+")) if wa else None,
        "email": emails[0] if emails else None, "datePosted": date, "pool": pool or None, "seaView": sea or None,
        "isNewBuild": newb, "_text": low,
    }


# ---------------------------------------------------------------- value (same idea as the app)
def measure(group, lv, ld):
    if group == "house":
        if lv and ld:
            return 0.6 * lv + 0.4 * ld
        return lv or ld
    if group == "apartment":
        return lv
    return ld


def value_z(listing, comps_docs, region_of, centroids):
    g = GROUP.get(listing.get("type"))
    reg = (listing.get("region") or "").lower()
    if not g or not reg:
        return None, 0
    m0 = measure(g, listing.get("livingM2"), listing.get("landM2"))
    if not m0 or not listing.get("price"):
        return None, 0
    entries = (comps_docs.get(f"{g}__{reg}") or {}).get("entries", [])
    pool = []
    for e in entries:
        if e.get("y") not in ("d", "e"):
            continue
        m = measure(g, e.get("lv"), e.get("ld"))
        if not m or not e.get("p") or e.get("u") == listing.get("url"):
            continue
        if not (m0 / 3 <= m <= m0 * 3):
            continue
        pool.append((e, m))
    tk = slug(listing.get("location"))
    same = [x for x in pool if x[0].get("tk") == tk]
    if len(same) >= 22:
        sample = sorted(same, key=lambda x: abs(math.log(x[1] / m0)))[:50]
    else:
        c0 = centroids.get(tk)
        def dist(x):
            c = centroids.get(x[0].get("tk"))
            if not c0 or not c:
                return 99
            return math.hypot((c[0] - c0[0]) * 111, (c[1] - c0[1]) * 104)
        others = sorted([x for x in pool if x[0].get("tk") != tk], key=dist)
        sample = same + others[: max(0, 50 - len(same))]
    if len(sample) < 22:
        return None, len(sample)
    logs = sorted(math.log(e["p"] / m) for e, m in sample)[1:-1]
    mu = sum(logs) / len(logs)
    sd = math.sqrt(sum((x - mu) ** 2 for x in logs) / (len(logs) - 1)) or 1
    return (math.log(listing["price"] / m0) - mu) / sd, len(logs)


def deal_score(z, listing, cut_pct):
    s = 40
    if z is not None:
        s += max(-30, min(40, -z * 20))
    if cut_pct:
        s += min(20, cut_pct * 2)
    t = listing.get("_text", "")
    if re.search(r"\b(urgent|must sell|quick sale|vente rapide|price reduced|negotiable|à saisir|a saisir|départ|leaving the country)\b", t):
        s += 8
    if re.search(r"\b(private seller|particulier|no agent|owner selling|sans agence)\b", t):
        s += 6
    return int(max(0, min(100, s)))


# ---------------------------------------------------------------- matching
def matches(listing, search):
    if search.get("active") is False:
        return False
    if listing["type"] not in (search.get("types") or [listing["type"]]):
        return False
    p = listing.get("price")
    if not p:
        return False
    if search.get("priceMax") and p > search["priceMax"]:
        return False
    if search.get("priceMin") and p < search["priceMin"]:
        return False
    if search.get("regions") and (listing.get("region") or "") not in search["regions"]:
        return False
    if search.get("towns") and (listing.get("location") or "") not in search["towns"]:
        return False
    if search.get("bedsMin") and (listing.get("beds") or 0) < search["bedsMin"]:
        return False
    if search.get("livingMin") and (listing.get("livingM2") or 0) < search["livingMin"]:
        return False
    if search.get("landMinM2") and (listing.get("landM2") or 0) < search["landMinM2"]:
        return False
    if search.get("landMaxM2") and listing.get("landM2") and listing["landM2"] > search["landMaxM2"]:
        return False
    if not search.get("includeNew") and listing.get("isNewBuild"):
        return False
    feats = search.get("features") or []
    if "pool" in feats and not listing.get("pool"):
        return False
    if "seaView" in feats and not listing.get("seaView"):
        return False
    t = listing.get("_text", "")
    for kw in [k.strip().lower() for k in (search.get("excludeKeywords") or "").split(",") if k.strip()]:
        if kw in t:
            return False
    kws = [k.strip().lower() for k in (search.get("keywords") or "").split(",") if k.strip()]
    if kws and not any(k in t for k in kws):
        return False
    return True


# ---------------------------------------------------------------- alerts
def send_alerts(items, cfg):
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("alerts", {}).get("ntfy_topic")
    app_url = os.environ.get("APP_URL") or cfg.get("alerts", {}).get("app_url") or ""
    if DRY or not items:
        return
    if topic:
        for it in items[:10]:
            l = it["listing"]
            title = ("Price cut: " if it["kind"] == "cut" else "New match: ") + (l.get("title") or "")[:80]
            body = f"{l.get('priceText')} · {l.get('location') or ''} · score {l.get('dealScore', '–')}"
            if it["kind"] == "cut":
                body += f" (was Rs {it['old']:,})"
            try:
                requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), timeout=15, headers={
                    "Title": title.encode("utf-8"), "Click": l.get("url", app_url),
                    "Priority": "high" if (l.get("dealScore", 0) >= 70 or it["kind"] == "cut") else "default",
                    "Tags": "house"})
            except Exception as e:
                log("ntfy failed:", e)
    to, user, pw = os.environ.get("EMAIL_TO"), os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if to and user and pw:
        lines = []
        for it in items[:30]:
            l = it["listing"]
            tag = "PRICE CUT" if it["kind"] == "cut" else "NEW"
            lines.append(f"[{tag}] {l.get('title')}\n  {l.get('priceText')} · {l.get('location')} · deal score {l.get('dealScore','–')}\n  {l.get('url')}\n")
        msg = MIMEText(f"{len(items)} update(s) from your Property Scout.\n\n" + "\n".join(lines) + (f"\nOpen the app: {app_url}\n" if app_url else ""), "plain", "utf-8")
        msg["Subject"] = f"Property Scout: {len(items)} update(s)"
        msg["From"], msg["To"] = user, to
        try:
            with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587")), timeout=30) as s:
                s.starttls()
                s.login(user, pw)
                s.sendmail(user, [to], msg.as_string())
        except Exception as e:
            log("email failed:", e)


# ---------------------------------------------------------------- main
def main():
    cfg = yaml.safe_load(open(CFG_PATH, encoding="utf-8"))
    if not os.path.exists(DB_PATH):
        log("STOPPED: the data file docs/data/db.json is missing from the repository.")
        log("Upload the 'docs' folder from the zip (it contains data/db.json), then run again.")
        sys.exit(1)
    db = load_json(DB_PATH, {"collections": {}})
    C = db.setdefault("collections", {})
    for k in ("listings", "searches", "config", "comps", "runs", "market", "directory"):
        C.setdefault(k, {})
    listings, comps = C["listings"], C["comps"]
    state = C["config"].setdefault("scout", {"seen": [], "recheck": {}})
    seen = set(state.get("seen", []))
    lim = cfg.get("limits", {})
    fetch = Fetcher(lim.get("delay_seconds", 2))

    # exchange rate (free, no key)
    fx = C["config"].get("fx", {})
    try:
        r = requests.get("https://open.er-api.com/v6/latest/MUR", timeout=20).json()
        rt = r["rates"]
        fx = {"gbpMur": round(1 / rt["GBP"], 2), "eurMur": round(1 / rt["EUR"], 2), "usdMur": round(1 / rt["USD"], 2),
              "at": ISO, "source": "open.er-api.com mid-market"}
        C["config"]["fx"] = fx
    except Exception as e:
        log("FX refresh failed, keeping last rate:", e)

    # towns, regions, map centroids - learned from your existing data
    region_of, centroids, cnt = {}, {}, {}
    for key, doc in comps.items():
        reg = key.split("__")[-1]
        for e in doc.get("entries", []):
            if e.get("t"):
                region_of[e["t"]] = reg.capitalize()
    for l in listings.values():
        if l.get("location") and l.get("region"):
            region_of.setdefault(l["location"], l["region"])
        if l.get("lat") and l.get("lng") and l.get("location"):
            k = slug(l["location"])
            a = centroids.get(k, (0, 0))
            cnt[k] = cnt.get(k, 0) + 1
            centroids[k] = (a[0] + l["lat"], a[1] + l["lng"])
    centroids = {k: (v[0] / cnt[k], v[1] / cnt[k]) for k, v in centroids.items()}
    towns = sorted(region_of)
    # scanner-settings.json (downloaded from the app's "My searches" tab) overrides searches + settings
    sp = os.path.join(ROOT, "scanner-settings.json")
    if os.path.exists(sp):
        try:
            up = json.load(open(sp, encoding="utf-8"))
            if up.get("searches"):
                C["searches"] = up["searches"]
            if up.get("settings"):
                C["config"]["settings"] = up["settings"]
        except Exception as e:
            log("scanner-settings.json could not be read, keeping the previous settings:", e)
    src_on = (C["config"].get("settings") or {}).get("sources") or {}
    searches = {k: v for k, v in C["searches"].items() if v.get("active", True)}
    known_urls = {l.get("url") for l in listings.values()} | {u for l in listings.values() for u in (l.get("altUrls") or [])}

    # 1) collect advert links from each source's list pages
    candidates = []
    src_cfg = cfg.get("sources", [])
    agencies = [a for a in C["directory"].get("agencies", {}).get("list", []) if a.get("active") and a.get("forSaleUrl")]
    rot = state.get("agencyCursor", 0)
    per_run = lim.get("agencies_per_run", 12)
    picked = agencies[rot:rot + per_run] or agencies[:per_run]
    state["agencyCursor"] = (rot + per_run) % max(1, len(agencies))
    for a in picked:
        host = urlparse(a["forSaleUrl"]).netloc
        src_cfg.append({"name": a.get("name", host), "key": "agencies", "lists": [a["forSaleUrl"]], "pages": 1,
                        "link": r"https?://(www\.)?" + re.escape(host.replace("www.", "")) + r"/.*(propert|annonce|detail|offer|listing|for-sale|vente|a-vendre|/p/).*\d"})
    per_source = {}
    for s in src_cfg:
        if s.get("enabled") is False or src_on.get(s.get("key")) is False:
            continue
        got = 0
        for lst in s.get("lists", []):
            for page in range(1, s.get("pages", 1) + 1):
                url = lst.format(page=page)
                html = fetch.get(url)
                if not html:
                    break
                links = set()
                for m in re.finditer(r'href=["\']([^"\'#]+)', html):
                    u = urljoin(url, unescape(m.group(1)))
                    if re.search(s["link"], u) and u != url:
                        links.add(u.split("?")[0] if s.get("strip_query", True) else u)
                new = [u for u in links if u not in known_urls and hashlib.md5(u.encode()).hexdigest()[:12] not in seen]
                for u in new:
                    candidates.append((s, u))
                got += len(links)
                if not links or "{page}" not in lst:
                    break
        per_source[s["name"]] = got
    random.shuffle(candidates)
    candidates = candidates[: lim.get("new_adverts_per_run", 220)]
    log(f"{len(candidates)} new advert links to read")

    # 2) read new adverts
    alerts, added, comps_added = [], 0, 0
    first_run = not state.get("ranBefore")
    for s, u in candidates:
        html = fetch.get(u)
        seen.add(hashlib.md5(u.encode()).hexdigest()[:12])
        if not html:
            continue
        a = parse_advert(html, u, towns, fx)
        a["url"], a["source"] = u, s.get("key", s["name"])
        a["region"] = region_of.get(a["location"]) if a["location"] else None
        if any(re.search(rx, a["_text"][:5000], re.I) for rx in EXCLUDE_ALWAYS):
            continue
        if not a["type"] or not a["price"]:
            continue
        # add to comparables (value curves) when it has a usable size
        g = GROUP.get(a["type"])
        if g and a["region"] and (a["livingM2"] or a["landM2"]) and not a["isNewBuild"]:
            key = f"{g}__{a['region'].lower()}"
            doc = comps.setdefault(key, {"entries": [], "group": g, "region": a["region"].lower(), "schema": 2, "segment": key})
            if not any(e.get("u") == u for e in doc["entries"]):
                doc["entries"].append({"t": a["location"], "tk": slug(a["location"]), "p": a["price"], "lv": a["livingM2"], "ld": a["landM2"],
                                       "b": a["beds"], "u": u, "s": urlparse(u).netloc.replace("www.", ""), "pid": None,
                                       "d": a["datePosted"], "dk": "posted" if a["datePosted"] else "none",
                                       "y": "d" if (a["datePosted"] or "") >= "2026" else "e", "at": ISO[:10]})
                doc["entries"] = doc["entries"][-600:]
                doc["updatedAt"] = ISO
                comps_added += 1
        hit = next((sid for sid, sv in searches.items() if matches(a, sv)), None)
        if not hit:
            continue
        lid = slug(urlparse(u).netloc.replace("www.", "").split(".")[0]) + "-" + hashlib.md5(u.encode()).hexdigest()[:10]
        z, n = value_z(a, comps, region_of, centroids)
        a.update({"searchId": hit, "searchName": searches[hit].get("name"), "status": "new", "firstSeen": ISO,
                  "agent": None, "ref": None, "altUrls": [], "valueZ": round(z, 2) if z is not None else None,
                  "valueN": n, "priceHistory": [{"at": ISO[:10], "price": a["price"]}]})
        a["dealScore"] = deal_score(z, a, 0)
        a.pop("_text", None)
        listings[lid] = a
        known_urls.add(u)
        added += 1
        alerts.append({"kind": "new", "listing": a})

    # 3) re-check a few existing matches for price cuts
    rc = state.setdefault("recheck", {})
    today = ISO[:10]
    if rc.get("day") != today:
        rc.clear()
        rc["day"], rc["n"] = today, 0
    pool = sorted([(k, l) for k, l in listings.items() if l.get("status") not in ("rejected", "gone")],
                  key=lambda kv: kv[1].get("checkedAt", ""))
    cuts = 0
    for k, l in pool[: max(0, lim.get("rechecks_per_day", 30) - rc["n"])][:5]:
        rc["n"] += 1
        html = fetch.get(l["url"])
        l["checkedAt"] = ISO
        if not html:
            continue
        a = parse_advert(html, l["url"], towns, fx)
        if a["price"] and l.get("price") and a["price"] < l["price"] * 0.99:
            old = l["price"]
            l.setdefault("priceHistory", [{"at": l.get("firstSeen", ISO)[:10], "price": old}]).append({"at": today, "price": a["price"]})
            l["price"], l["priceText"] = a["price"], f"Rs {a['price']:,}"
            l["dealScore"] = min(100, (l.get("dealScore") or 40) + 10)
            alerts.append({"kind": "cut", "listing": l, "old": old})
            cuts += 1

    # 4) alerts (first run: only the 10 best, so you aren't flooded)
    alerts.sort(key=lambda it: -(it["listing"].get("dealScore") or 0))
    if first_run:
        alerts = alerts[:10]
    send_alerts(alerts, cfg)

    # 5) save
    state["seen"] = list(seen)[-20000:]
    state["ranBefore"] = True
    status = {h: v for h, v in fetch.status.items()}
    rid = ISO.replace(":", "").replace("-", "")
    C["runs"][rid] = {"at": ISO, "sourcesChecked": len(per_source), "found": len(candidates), "newCount": added,
                      "notes": f"{cuts} price cut(s); {comps_added} comparable adverts added; runner: {os.environ.get('RUNNER', 'PC')}",
                      "sources": status}
    for old in sorted(C["runs"])[:-200]:
        C["runs"].pop(old)
    C["config"]["sourceStatus"] = {"at": ISO, "hosts": status, "perSource": per_source}
    db["exportedAt"] = ISO
    if DRY:
        log("dry run - nothing saved")
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        tmp = DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, sort_keys=True, indent=1)
        os.replace(tmp, DB_PATH)
    log(f"done: {added} new match(es), {cuts} price cut(s), {comps_added} comparables added")
    for h, v in sorted(status.items()):
        log(f"  {h}: ok {v['ok']}, refused {v['blocked']}, failed {v['fail']} - {v['last']}")


if __name__ == "__main__":
    main()
