#!/usr/bin/env python3
"""Bouwt de website-map voor het Congres Trades-dashboard (versie 2, voor FMP Starter).

Uitvoer (map OUT_DIR, standaard 'site'):
  index.html            kopie van het dashboard
  data.json             politici + trades (met koersen op transactie-, meldings- en huidige datum)
  koersen/<TICKER>.json slotkoersen per aandeel, alleen geladen als je een grafiek opent
  profielen.json        sector/naam per ticker (cache voor de volgende run)

Bronnen:
  - Financial Modeling Prep (FMP): meldingen Senaat en Huis, bedrijfsprofielen, slotkoersen.
  - unitedstates/congress-legislators: partij, staat, kamer en commissies.

Elke run bouwt alles opnieuw op (geen opgeslagen tussenstand nodig). Alleen de profielen
worden hergebruikt van de vorige, gepubliceerde versie (PREV_URL), om aanroepen te sparen.

Standaardbibliotheek van Python 3.9+, geen extra pakketten.
Instellingen via omgevingsvariabelen:
  FMP_API_KEY    verplicht
  LOOKBACK_DAYS  hoeveel dagen terug (transactiedatum), standaard 1095 (3 jaar; Starter geeft max. 5 jaar koersen)
  MAX_PAGES      max. pagina's per kamer, standaard 400
  PAGE_LIMIT     meldingen per pagina, standaard 100 (wordt automatisch verlaagd als FMP dat eist)
  MAX_TICKERS    max. aantal aandelen met koersen, standaard 3000
  PREV_URL       adres van de gepubliceerde site (voor de profielcache), optioneel
"""
import json, os, re, shutil, sys, time, unicodedata, urllib.request, urllib.error, urllib.parse
from datetime import date, datetime, timedelta, timezone

FMP = os.environ.get("FMP_BASE", "https://financialmodelingprep.com/stable")
LEG = os.environ.get("LEG_BASE", "https://unitedstates.github.io/congress-legislators")
KEY = os.environ.get("FMP_API_KEY", "").strip()
LOOKBACK = int(os.environ.get("LOOKBACK_DAYS", "1095"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "400"))
PAGE_LIMIT = int(os.environ.get("PAGE_LIMIT", "100"))
MAX_TICKERS = int(os.environ.get("MAX_TICKERS", "3000"))
OUT_DIR = os.environ.get("OUT_DIR", "site")
PREV_URL = os.environ.get("PREV_URL", "").rstrip("/")
DELAY = float(os.environ.get("FMP_DELAY", "0.22"))  # ~270 aanroepen/minuut, onder de 300 van Starter
TODAY = date.today()
SINCE = (TODAY - timedelta(days=LOOKBACK)).isoformat()
CALLS = 0

SECTOR_NL = {
    "Technology": "Technologie", "Healthcare": "Gezondheidszorg", "Financial Services": "Financiën",
    "Consumer Cyclical": "Consument (cyclisch)", "Consumer Defensive": "Consument (defensief)",
    "Industrials": "Industrie", "Energy": "Energie", "Utilities": "Nutsbedrijven",
    "Basic Materials": "Grondstoffen", "Real Estate": "Vastgoed", "Communication Services": "Communicatie",
}


def log(*a):
    print(*a, flush=True)


def safe(url):
    return re.sub(r"apikey=[^&]+", "apikey=***", url)


class HttpError(RuntimeError):
    def __init__(self, code, body, url):
        super().__init__(f"HTTP {code}: {body} ({safe(url)})")
        self.code, self.body = code, body


def get_json(url, tries=5):
    global CALLS
    last = None
    for i in range(tries):
        try:
            CALLS += 1
            req = urllib.request.Request(url, headers={"User-Agent": "congres-trades/2.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            last = HttpError(e.code, body, url)
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(3 * (i + 1) + (20 if e.code == 429 else 0))
                continue
            raise last
        except Exception as e:  # netwerkfout
            last = RuntimeError(f"{e} ({safe(url)})")
            time.sleep(3 * (i + 1))
    raise last


def fmp(path, **params):
    params["apikey"] = KEY
    data = get_json(f"{FMP}/{path}?{urllib.parse.urlencode(params)}")
    if isinstance(data, dict) and ("Error Message" in data or "error" in data):
        raise RuntimeError(f"FMP weigert {path}: {data.get('Error Message') or data.get('error')}")
    time.sleep(DELAY)
    return data


def pick(d, *names):
    for n in names:
        v = d.get(n)
        if v not in (None, ""):
            return v
    return None


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(hon|mr|mrs|ms|dr|jr|sr|ii|iii|iv)\b\.?", " ", s)
    return re.sub(r"[^a-z ]", " ", s).split()


def to_date(v):
    if not v:
        return None
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def trade_type(v):
    s = str(v or "").lower()
    if "purchase" in s or s.startswith("buy"):
        return "koop"
    if "sale" in s or s.startswith("sell"):
        return "verkoop"
    return None  # bv. Exchange: overslaan


def lower_bound(amount):
    m = re.search(r"\d[\d,\.]*", str(amount or ""))
    return int(re.sub(r"[,\.]", "", m.group(0))) if m else None


def ticker_file(tk):
    return re.sub(r"[^A-Z0-9.\-]", "_", tk.upper())


# ---------- 1. Congresleden en commissies ----------
def load_legislators():
    try:
        leg = get_json(f"{LEG}/legislators-current.json")
        coms = get_json(f"{LEG}/committees-current.json")
        mem = get_json(f"{LEG}/committee-membership-current.json")
    except Exception as e:
        log("Waarschuwing: congress-legislators niet bereikbaar, partij/commissies ontbreken:", e)
        return []
    names = {c.get("thomas_id"): c.get("name") for c in coms if c.get("thomas_id")}
    by_bio = {}
    for code, members in mem.items():
        if code not in names:  # subcommissies overslaan
            continue
        for m in members:
            by_bio.setdefault(m.get("bioguide"), []).append(names[code])
    people = []
    for p in leg:
        t = (p.get("terms") or [{}])[-1]
        n = p.get("name", {})
        bio = p.get("id", {}).get("bioguide")
        people.append({
            "id": bio, "naam": n.get("official_full") or f"{n.get('first', '')} {n.get('last', '')}".strip(),
            "partij": {"Democrat": "D", "Republican": "R", "Independent": "I"}.get(t.get("party"), t.get("party") or "–"),
            "kamer": "Senate" if t.get("type") == "sen" else "House", "staat": t.get("state", ""),
            "commissies": sorted(set(by_bio.get(bio, []))),
            "_first": norm(n.get("first")) + norm(n.get("nickname")), "_last": norm(n.get("last")),
        })
    log(f"{len(people)} huidige Congresleden geladen")
    return people


def match_person(people, first, last, office, kamer, state):
    f, l = norm(first), norm(last)
    if not l and office:
        parts = norm(office)
        f, l = parts[:1], parts[-1:]
    cands = [p for p in people if p["kamer"] == kamer and l and p["_last"] and p["_last"][-1] == l[-1]]
    if state:
        s = [p for p in cands if p["staat"] == state]
        cands = s or cands
    if len(cands) > 1 and f:
        s = [p for p in cands if any(x == f[0] or x.startswith(f[0][:3]) for x in p["_first"])]
        cands = s or cands
    return cands[0] if len(cands) == 1 else None


# ---------- 2. Meldingen ----------
def fetch_disclosures(path, kamer):
    global PAGE_LIMIT
    rows, logged = [], False
    page = 0
    while page < MAX_PAGES:
        try:
            data = fmp(path, page=page, limit=PAGE_LIMIT)
        except HttpError as e:
            m = re.search(r"'limit' must be between 0 and (\d+)", e.body)
            if m and int(m.group(1)) < PAGE_LIMIT:
                PAGE_LIMIT = max(1, int(m.group(1)))
                log(f"FMP staat max. {PAGE_LIMIT} per pagina toe; opnieuw")
                continue
            if page == 0:
                raise
            log(f"{path}: pagina {page} niet beschikbaar, gestopt ({str(e)[:200]})")
            break
        if not isinstance(data, list) or not data:
            break
        if not logged:
            log(f"{path}: velden in antwoord: {sorted(data[0].keys())}")
            logged = True
        rows += [(kamer, r) for r in data]
        dates = [d for d in (to_date(pick(r, "disclosureDate", "dateRecieved", "dateReceived")) for r in data) if d]
        if dates and max(dates) < SINCE:
            break
        page += 1
    log(f"{path}: {len(rows)} meldingen op {page + 1} pagina's")
    return rows


def prices_fmp(tk, frm):
    d = fmp("historical-price-eod/light", symbol=tk, **{"from": frm, "to": TODAY.isoformat()})
    if isinstance(d, dict):
        d = d.get("historical", [])
    pts = {}
    for x in d or []:
        c = pick(x, "price", "close", "adjClose")
        dt = to_date(pick(x, "date"))
        if c and dt:
            pts[dt] = round(float(c), 4)
    return [[k, pts[k]] for k in sorted(pts)]


def load_prev_profiles():
    if not PREV_URL:
        return {}
    try:
        d = get_json(f"{PREV_URL}/profielen.json?t={int(time.time())}", tries=2)
        if isinstance(d, dict) and "Error Message" not in d:
            log(f"Profielcache van vorige run: {len(d)} tickers")
            return d
    except Exception as e:
        log("Geen profielcache van vorige run (eerste run is normaal):", str(e)[:120])
    return {}


INSIDER_DAYS = int(os.environ.get("INSIDER_DAYS", "365"))
INSIDER_SINCE = (TODAY - timedelta(days=INSIDER_DAYS)).isoformat()
INSIDER_WINDOW = int(os.environ.get("INSIDER_WINDOW", "30"))  # dagen rond de transactiedatum

# Commissie -> sectoren / branches. Bewust grof; zie uitleg in het dashboard.
COMMITTEE_RULES = [
    (["armed services", "defense"], [], ["aerospace", "defense"]),
    (["intelligence", "homeland security"], [], ["aerospace", "defense", "software", "communication equipment", "information technology", "computer"]),
    (["financial services", "banking", "finance"], ["Financiën", "Vastgoed"], []),
    (["energy", "natural resources", "environment and public works"], ["Energie", "Nutsbedrijven", "Grondstoffen"], []),
    (["energy and commerce"], ["Gezondheidszorg", "Communicatie"], []),
    (["health", "veterans", "aging"], ["Gezondheidszorg"], []),
    (["agriculture"], ["Consument (defensief)", "Grondstoffen"], ["agricultur", "farm", "food", "tobacco"]),
    (["science, space", "commerce, science"], ["Technologie", "Communicatie"], []),
    (["transportation", "infrastructure"], [], ["airline", "railroad", "trucking", "auto", "aerospace", "engineering & construction", "marine shipping"]),
]


def committee_match(commissies, sector, industrie):
    ind = (industrie or "").lower()
    for c in commissies:
        cl = c.lower()
        for keys, sectors, inds in COMMITTEE_RULES:
            if any(k in cl for k in keys) and (sector in sectors or any(x in ind for x in inds)):
                return c
    return None


def fetch_insiders(trades):
    recent = sorted({t["ticker"] for t in trades if (t["meldingsdatum"] or "") >= INSIDER_SINCE})
    out, fails, logged = {}, 0, False
    for i, tk in enumerate(recent):
        try:
            d = fmp("insider-trading/search", symbol=tk, page=0, limit=100)
        except Exception as e:
            fails += 1
            if fails == 1:
                log("Insider-data mislukt:", str(e)[:200])
            if fails == 3 and i == 2:
                log("Insider-module uitgeschakeld voor deze run (endpoint niet beschikbaar).")
                return {}
            continue
        if isinstance(d, list) and d and not logged:
            log(f"insider-trading: velden in antwoord: {sorted(d[0].keys())}")
            logged = True
        rows = []
        for x in d if isinstance(d, list) else []:
            code = str(pick(x, "transactionType") or "").upper()
            if code.startswith("P"):
                typ = "koop"
            elif code.startswith("S"):
                typ = "verkoop"
            else:
                continue  # opties, schenkingen, toekenningen: geen marktaankoop/-verkoop
            dt = to_date(pick(x, "transactionDate", "filingDate"))
            if not dt:
                continue
            rows.append({"naam": pick(x, "reportingName") or "?", "rol": pick(x, "typeOfOwner") or "",
                         "type": typ, "datum": dt, "gemeld": to_date(pick(x, "filingDate")),
                         "aantal": pick(x, "securitiesTransacted"), "prijs": pick(x, "price"), "link": pick(x, "url", "link")})
        rows.sort(key=lambda r: r["datum"], reverse=True)
        out[tk] = rows
        if (i + 1) % 250 == 0:
            log(f"  insiders: {i + 1}/{len(recent)}")
    log(f"Insider-trades opgehaald voor {len(out)} van {len(recent)} tickers ({fails} mislukt)")
    return out


def insider_match(t, rows):
    txd = date.fromisoformat(t["transactiedatum"])
    hits = [r for r in rows if r["type"] == t["type"] and abs((date.fromisoformat(r["datum"]) - txd).days) <= INSIDER_WINDOW]
    if not hits:
        return None
    return {"aantal": len(hits), "namen": sorted({r["naam"] for r in hits})[:5]}


def build():
    if not KEY:
        sys.exit("FMP_API_KEY ontbreekt. Zet hem als repository secret.")
    log(f"Trades vanaf {SINCE} ({LOOKBACK} dagen)")
    people = load_legislators()

    raw = fetch_disclosures("senate-latest", "Senate") + fetch_disclosures("house-latest", "House")
    politici, trades, skipped = {}, {}, 0
    for kamer, r in raw:
        ticker = str(pick(r, "symbol", "ticker") or "").strip().upper()
        typ = trade_type(pick(r, "type", "transactionType"))
        tx = to_date(pick(r, "transactionDate"))
        mel = to_date(pick(r, "disclosureDate", "dateRecieved", "dateReceived"))
        if not ticker or ticker in ("--", "N/A") or not typ or not tx or tx < SINCE or tx > TODAY.isoformat():
            skipped += 1
            continue
        first, last = pick(r, "firstName"), pick(r, "lastName")
        office = pick(r, "office", "representative", "senator")
        district = str(pick(r, "district") or "")
        state = district[:2].upper() if re.match(r"[A-Za-z]{2}", district) else ""
        p = match_person(people, first, last, office, kamer, state)
        if p:
            pid = p["id"]
            politici[pid] = {k: v for k, v in p.items() if not k.startswith("_")}
        else:
            name = " ".join(x for x in [first, last] if x) or office or "Onbekend"
            pid = "x-" + "-".join(norm(name)) + "-" + kamer.lower()
            politici.setdefault(pid, {"id": pid, "naam": name, "partij": "–", "kamer": kamer,
                                      "staat": state, "commissies": []})
        amount = pick(r, "amount") or "–"
        key = "|".join([pid, ticker, tx, typ, str(amount), str(pick(r, "owner") or "")])
        trades[key] = {
            "politicus_id": pid, "type": typ, "ticker": ticker,
            "bedrijf": pick(r, "assetDescription", "companyName") or ticker, "sector": None,
            "bedrag_range": amount, "transactiedatum": tx, "meldingsdatum": mel,
            "eigenaar": pick(r, "owner"), "link": pick(r, "link"), "signalen": [],
        }
    trades = list(trades.values())
    log(f"{len(trades)} trades, {skipped} meldingen overgeslagen (geen ticker/type/datum, of buiten de periode)")

    # ---------- 3. Sector ----------
    cache = load_prev_profiles()
    count = {}
    for t in trades:
        count[t["ticker"]] = count.get(t["ticker"], 0) + 1
    tickers = sorted(count, key=lambda s: -count[s])[:MAX_TICKERS]
    new_prof = 0
    for tk in tickers:
        if tk in cache and cache[tk].get("sector") is not None and "industrie" in cache[tk]:
            continue
        try:
            d = fmp("profile", symbol=tk)
            d = d[0] if isinstance(d, list) and d else {}
            cache[tk] = {"sector": SECTOR_NL.get(d.get("sector"), d.get("sector") or ""), "naam": d.get("companyName"),
                         "industrie": d.get("industry") or ""}
            new_prof += 1
        except Exception as e:
            cache[tk] = {"sector": "", "naam": None, "industrie": ""}
    log(f"Profielen: {new_prof} nieuw opgehaald")

    # ---------- 4. Koersen ----------
    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(os.path.join(OUT_DIR, "koersen"))
    first_needed = {}
    for t in trades:
        first_needed[t["ticker"]] = min(first_needed.get(t["ticker"], "9999"), t["transactiedatum"])
    hist, fails, ok = {}, [], 0
    for i, tk in enumerate(tickers):
        frm = (date.fromisoformat(first_needed[tk]) - timedelta(days=45)).isoformat()
        try:
            pts = prices_fmp(tk, frm)
        except Exception as e:
            pts = []
            fails.append(f"{tk}: {str(e)[:140]}")
        if pts:
            hist[tk] = pts
            ok += 1
            with open(os.path.join(OUT_DIR, "koersen", ticker_file(tk) + ".json"), "w", encoding="utf-8") as f:
                json.dump(pts, f, separators=(",", ":"))
        if (i + 1) % 250 == 0:
            log(f"  koersen: {i + 1}/{len(tickers)}")
    log(f"Koersen voor {ok} van {len(tickers)} tickers ({len(fails)} zonder data)")
    for f in fails[:5]:
        log("  zonder koers:", f)

    def close_on(tk, d):
        best = None
        for dt, c in hist.get(tk, []):
            if dt <= d:
                best = c
            else:
                break
        return best

    for t in trades:
        tk = t["ticker"]
        c = cache.get(tk, {})
        t["sector"] = c.get("sector") or "Onbekend"
        if c.get("naam"):
            t["bedrijf"] = c["naam"]
        t["koers_transactie"] = close_on(tk, t["transactiedatum"])
        t["koers_melding"] = close_on(tk, t["meldingsdatum"]) if t["meldingsdatum"] else None
        t["koers_nu"] = hist[tk][-1][1] if hist.get(tk) else None
        # Track record: koers 1/3/6/12 maanden na de melding (alleen als die datum al voorbij is)
        last = hist[tk][-1][0] if hist.get(tk) else None
        for label, days in (("1m", 30), ("3m", 91), ("6m", 182), ("12m", 365)):
            target = (date.fromisoformat(t["meldingsdatum"]) + timedelta(days=days)).isoformat() if t["meldingsdatum"] else None
            t["koers_" + label] = close_on(tk, target) if target and last and target <= last else None

    # ---------- 5. Insider-trades (Form 4) ----------
    insiders = fetch_insiders(trades)

    # ---------- 6. Signalen ----------
    by_tk = {}
    for t in trades:
        by_tk.setdefault(t["ticker"], []).append(t)
    for t in trades:
        s = []
        lb = lower_bound(t["bedrag_range"])
        if lb is not None and lb >= 250001:
            s.append("groot bedrag")
        if t["meldingsdatum"]:
            delay = (date.fromisoformat(t["meldingsdatum"]) - date.fromisoformat(t["transactiedatum"])).days
            if delay > 45:
                s.append("late melding")
        txd = date.fromisoformat(t["transactiedatum"])
        if any(o["politicus_id"] != t["politicus_id"] and abs((date.fromisoformat(o["transactiedatum"]) - txd).days) <= 14
               for o in by_tk[t["ticker"]]):
            s.append("cluster")
        if t["meldingsdatum"] and t["meldingsdatum"] >= INSIDER_SINCE:
            ins = insider_match(t, insiders.get(t["ticker"], []))
            if ins:
                s.append("insider zelfde richting")
                t["insider_match"] = ins
        p = politici.get(t["politicus_id"]) or {}
        cm = committee_match(p.get("commissies") or [], t["sector"], cache.get(t["ticker"], {}).get("industrie", ""))
        if cm:
            s.append("commissie-match")
            t["commissie_match"] = cm
        t["signalen"] = s

    # ---------- 7. Wegschrijven ----------
    used = {t["politicus_id"] for t in trades}
    out = {
        "laatst_bijgewerkt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "politici": sorted([p for k, p in politici.items() if k in used], key=lambda p: p["naam"]),
        "trades": sorted(trades, key=lambda t: t["meldingsdatum"] or "", reverse=True),
        "koershistorie_map": "koersen/",
        "insiders": {tk: v[:15] for tk, v in insiders.items() if v},
    }
    with open(os.path.join(OUT_DIR, "data.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "profielen.json"), "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
    if os.path.exists("index.html"):
        shutil.copy("index.html", os.path.join(OUT_DIR, "index.html"))
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").close()
    size = os.path.getsize(os.path.join(OUT_DIR, "data.json")) / 1e6
    log(f"Klaar: {len(trades)} trades, {len(out['politici'])} politici, data.json {size:.1f} MB, {CALLS} aanroepen.")


if __name__ == "__main__":
    try:
        build()
    except RuntimeError as e:
        sys.exit(f"FOUT: {e}")
