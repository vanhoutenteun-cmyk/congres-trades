#!/usr/bin/env python3
"""Bouwt data.json voor het Congres Trades-dashboard.

Bronnen:
  - Financial Modeling Prep (FMP): meldingen van Senaat en Huis, bedrijfsprofielen (sector), slotkoersen.
  - unitedstates/congress-legislators: partij, staat, kamer en commissies.

Draait zonder extra pakketten (alleen de standaardbibliotheek van Python 3.9+).
Instellingen via omgevingsvariabelen:
  FMP_API_KEY    verplicht
  LOOKBACK_DAYS  hoeveel dagen terug trades bewaard worden (standaard 400)
  MAX_PAGES      max. pagina's per kamer per run (standaard 40)
  PAGE_LIMIT     meldingen per pagina (standaard 25, het maximum van het gratis FMP-abonnement)
  MAX_TICKERS    max. aantal tickers waarvoor koersen worden opgehaald (standaard 150)
"""
import json, os, re, sys, time, unicodedata, urllib.request, urllib.error, urllib.parse
from datetime import date, datetime, timedelta, timezone

FMP = os.environ.get("FMP_BASE", "https://financialmodelingprep.com/stable")
LEG = os.environ.get("LEG_BASE", "https://unitedstates.github.io/congress-legislators")
KEY = os.environ.get("FMP_API_KEY", "").strip()
LOOKBACK = int(os.environ.get("LOOKBACK_DAYS", "400"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "40"))
PAGE_LIMIT = int(os.environ.get("PAGE_LIMIT", "25"))
MAX_TICKERS = int(os.environ.get("MAX_TICKERS", "150"))
OUT = os.environ.get("OUT_FILE", "data.json")
PROFILE_CACHE = os.environ.get("PROFILE_CACHE", "profielen.json")
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


def get_json(url, tries=4):
    global CALLS
    last = None
    for i in range(tries):
        try:
            CALLS += 1
            req = urllib.request.Request(url, headers={"User-Agent": "congres-trades/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {e.code}: {body}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1) + (10 if e.code == 429 else 0))
                continue
            break
        except Exception as e:  # netwerkfout
            last = str(e)
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Ophalen mislukt ({safe(url)}): {last}")


def safe(url):
    return re.sub(r"apikey=[^&]+", "apikey=***", url)


def fmp(path, **params):
    params["apikey"] = KEY
    data = get_json(f"{FMP}/{path}?{urllib.parse.urlencode(params)}")
    if isinstance(data, dict) and ("Error Message" in data or "error" in data):
        raise RuntimeError(f"FMP weigert {path}: {data.get('Error Message') or data.get('error')}")
    time.sleep(0.2)
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
    s = str(v)[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(v)[:len(datetime.now().strftime(fmt))], fmt).date().isoformat()
        except ValueError:
            pass
    return s if re.match(r"\d{4}-\d\d-\d\d$", s) else None


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


# ---------- Koersbronnen ----------
YAHOO = os.environ.get("YAHOO_BASE", "https://query1.finance.yahoo.com/v8/finance/chart")
STOOQ = os.environ.get("STOOQ_BASE", "https://stooq.com/q/d/l/")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def prices_fmp(tk, frm):
    d = fmp("historical-price-eod/light", symbol=tk, **{"from": frm, "to": TODAY.isoformat()})
    if isinstance(d, dict):
        d = d.get("historical", [])
    pts = []
    for x in d or []:
        c = pick(x, "price", "close", "adjClose")
        dt = to_date(pick(x, "date"))
        if c and dt:
            pts.append({"datum": dt, "slotkoers": round(float(c), 4)})
    return pts


def http_text(url):
    global CALLS
    CALLS += 1
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def prices_yahoo(tk, frm):
    """Onofficiële Yahoo Finance-grafiekdata. Alleen voor persoonlijk gebruik."""
    p1 = int(datetime.combine(date.fromisoformat(frm), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp()) + 86400
    sym = tk.replace(".", "-")
    url = f"{YAHOO}/{urllib.parse.quote(sym)}?period1={p1}&period2={p2}&interval=1d&events=history"
    for i in range(3):
        try:
            data = json.loads(http_text(url))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < 2:
                time.sleep(5 * (i + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}")
    res = ((data.get("chart") or {}).get("result") or [None])[0]
    if not res:
        raise RuntimeError(str((data.get("chart") or {}).get("error") or "geen resultaat")[:120])
    off = (res.get("meta") or {}).get("gmtoffset") or 0
    closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    pts = []
    for ts, c in zip(res.get("timestamp") or [], closes):
        if c:
            dt = datetime.fromtimestamp(ts + off, timezone.utc).date().isoformat()
            pts.append({"datum": dt, "slotkoers": round(float(c), 4)})
    time.sleep(0.4)
    return pts


def prices_stooq(tk, frm):
    sym = tk.lower().replace(".", "-") + ".us"
    url = f"{STOOQ}?s={urllib.parse.quote(sym)}&d1={frm.replace('-', '')}&d2={TODAY.strftime('%Y%m%d')}&i=d"
    txt = http_text(url).strip()
    lines = txt.splitlines()
    if not lines or not lines[0].lower().startswith("date"):
        raise RuntimeError("geen CSV: " + (lines[0][:80] if lines else "leeg"))
    head = [h.strip().lower() for h in lines[0].split(",")]
    di, ci = head.index("date"), head.index("close")
    pts = []
    for ln in lines[1:]:
        f = ln.split(",")
        try:
            pts.append({"datum": f[di][:10], "slotkoers": round(float(f[ci]), 4)})
        except (ValueError, IndexError):
            pass
    time.sleep(0.4)
    return pts


# ---------- 1. Congresleden en commissies ----------
def load_legislators():
    try:
        leg = get_json(f"{LEG}/legislators-current.json")
        coms = get_json(f"{LEG}/committees-current.json")
        mem = get_json(f"{LEG}/committee-membership-current.json")
    except Exception as e:
        log("Waarschuwing: congress-legislators niet bereikbaar, partij/commissies ontbreken:", e)
        return [], {}
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
    return people, {p["id"]: p for p in people}


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
def fetch_disclosures(path, kamer, stop_at):
    rows, first_keys = [], None
    for page in range(MAX_PAGES):
        try:
            data = fmp(path, page=page, limit=PAGE_LIMIT)
        except RuntimeError as e:
            if page == 0:
                raise
            log(f"{path}: pagina {page} niet beschikbaar, gestopt ({e})")
            break
        if not isinstance(data, list) or not data:
            break
        if first_keys is None:
            first_keys = sorted(data[0].keys())
            log(f"{path}: velden in antwoord: {first_keys}")
        rows += [(kamer, r) for r in data]
        dates = [to_date(pick(r, "disclosureDate", "dateRecieved", "dateReceived")) for r in data]
        dates = [d for d in dates if d]
        if dates and min(dates) < stop_at:
            break
    log(f"{path}: {len(rows)} meldingen opgehaald")
    return rows


def build():
    if not KEY:
        sys.exit("FMP_API_KEY ontbreekt. Zet hem als repository secret.")
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            prev = {}
    people, by_id = load_legislators()

    # Alleen terugbladeren tot kort voor de vorige run (bespaart API-aanroepen)
    prev_mels = [t.get("meldingsdatum") for t in prev.get("trades", []) if t.get("meldingsdatum")]
    stop_at = SINCE
    if prev_mels:
        stop_at = max(SINCE, (date.fromisoformat(max(prev_mels)) - timedelta(days=10)).isoformat())
    log(f"Meldingen ophalen tot {stop_at}")
    raw = fetch_disclosures("senate-latest", "Senate", stop_at) + fetch_disclosures("house-latest", "House", stop_at)
    politici = {p["id"]: p for p in prev.get("politici", [])}
    trades = {}
    for t in prev.get("trades", []):  # eerdere runs behouden
        if (t.get("transactiedatum") or "") >= SINCE:
            trades[t.get("_key") or json.dumps(t, sort_keys=True)] = t
    skipped = 0
    for kamer, r in raw:
        ticker = str(pick(r, "symbol", "ticker") or "").strip().upper()
        typ = trade_type(pick(r, "type", "transactionType"))
        tx = to_date(pick(r, "transactionDate"))
        mel = to_date(pick(r, "disclosureDate", "dateRecieved", "dateReceived"))
        if not ticker or ticker in ("--", "N/A") or not typ or not tx or tx < SINCE:
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
            "_key": key, "politicus_id": pid, "type": typ, "ticker": ticker,
            "bedrijf": pick(r, "assetDescription", "companyName") or ticker, "sector": None,
            "bedrag_range": amount, "transactiedatum": tx, "meldingsdatum": mel,
            "eigenaar": pick(r, "owner"), "link": pick(r, "link"), "signalen": [],
        }
    trades = list(trades.values())
    log(f"{len(trades)} trades bewaard, {skipped} meldingen overgeslagen (geen ticker/type/datum of te oud)")

    # ---------- 3. Sector ----------
    cache = {}
    if os.path.exists(PROFILE_CACHE):
        try:
            cache = json.load(open(PROFILE_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}
    tickers = sorted({t["ticker"] for t in trades}, key=lambda s: -sum(1 for t in trades if t["ticker"] == s))
    tickers = tickers[:MAX_TICKERS]
    for tk in tickers:
        if tk in cache:
            continue
        try:
            d = fmp("profile", symbol=tk)
            d = d[0] if isinstance(d, list) and d else {}
            cache[tk] = {"sector": SECTOR_NL.get(d.get("sector"), d.get("sector") or None), "naam": d.get("companyName")}
        except Exception as e:
            log("Profiel mislukt", tk, e)
            cache[tk] = {"sector": None, "naam": None}
    json.dump(cache, open(PROFILE_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)

    # ---------- 4. Koersen ----------
    # Vorige koersen behouden; nieuwe aanvullen. Mislukt een ticker, dan blijft de oude reeks staan.
    hist = {k: v for k, v in (prev.get("koershistorie") or {}).items() if any(t["ticker"] == k for t in trades)}
    first_needed = {}
    for t in trades:
        first_needed[t["ticker"]] = min(first_needed.get(t["ticker"], "9999"), t["transactiedatum"])
    src_count = {}
    price_fail = 0
    for tk in tickers:
        frm = (date.fromisoformat(first_needed[tk]) - timedelta(days=40)).isoformat()
        old = hist.get(tk) or []
        if old and old[0]["datum"] <= frm:
            frm = (date.fromisoformat(old[-1]["datum"]) - timedelta(days=5)).isoformat()
        pts, errors = [], []
        sources = [("FMP", prices_fmp), ("Yahoo", prices_yahoo), ("Stooq", prices_stooq)]
        if cache.get(tk, {}).get("fmp_koers") is False:
            sources = sources[1:]  # FMP gaf eerder 'niet in je abonnement' voor deze ticker
        for name, fn in sources:
            try:
                pts = fn(tk, frm)
            except Exception as e:
                errors.append(f"{name}: {e}")
                if name == "FMP" and "not available under your current subscription" in str(e):
                    cache.setdefault(tk, {})["fmp_koers"] = False
                pts = []
            if pts:
                src_count[name] = src_count.get(name, 0) + 1
                break
        merged = {p["datum"]: p for p in old}
        merged.update({p["datum"]: p for p in pts})
        if merged:
            hist[tk] = [merged[k] for k in sorted(merged)]
        if not pts:
            price_fail += 1
            if price_fail <= 5:
                log("Koersen mislukt", tk, " | ".join(e[:160] for e in errors) or "geen data")
    json.dump(cache, open(PROFILE_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    log(f"Koersen voor {len(hist)} van {len(tickers)} tickers; bronnen deze run: {src_count or 'geen'}; {price_fail} zonder nieuwe koersen")

    def close_on(tk, d):
        best = None
        for p in hist.get(tk, []):
            if p["datum"] <= d:
                best = p["slotkoers"]
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
        t["koers_nu"] = hist[tk][-1]["slotkoers"] if hist.get(tk) else None

    # ---------- 5. Signalen ----------
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
        t["signalen"] = s

    used = {t["politicus_id"] for t in trades}
    out = {
        "laatst_bijgewerkt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "politici": sorted([p for k, p in politici.items() if k in used], key=lambda p: p["naam"]),
        "trades": sorted(trades, key=lambda t: t["meldingsdatum"] or "", reverse=True),
        "koershistorie": hist,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    log(f"Klaar: {len(out['trades'])} trades, {len(out['politici'])} politici, {CALLS} API-aanroepen.")


if __name__ == "__main__":
    try:
        build()
    except RuntimeError as e:
        sys.exit(f"FOUT: {e}")
