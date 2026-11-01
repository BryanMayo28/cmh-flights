#!/usr/bin/env python3
"""Find cheap flights out of CMH (explore mode) from several sources and push them
to your phone via ntfy, with domestic and international kept separate.

Sources (each is optional, enabled by its key):
  - Aviasales / Travelpayouts Data API  (TRAVELPAYOUTS_TOKEN)  cached fares
  - Google Flights Explore via SerpApi  (SERPAPI_KEY)           Google's data
  - Deal blogs (error fares / fire sales) via RSS: Secret Flying, The Flight Deal
    No key needed. Posts are matched on DEAL_KEYWORDS (default: Columbus, CMH).

To add a source: write a function that returns a list of deals (see below)
and add it to PROVIDERS.
"""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ORIGIN = os.environ.get("ORIGIN", "CMH")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
# Optional separate topics so you can mute one category on your phone.
TOPICS = {
    "domestic": os.environ.get("NTFY_TOPIC_DOMESTIC", NTFY_TOPIC),
    "international": os.environ.get("NTFY_TOPIC_INTERNATIONAL", NTFY_TOPIC),
}
MAX_PRICE = {
    "domestic": float(os.environ.get("MAX_PRICE_DOMESTIC", "150")),
    "international": float(os.environ.get("MAX_PRICE_INTERNATIONAL", "500")),
}
# Nearby airports you'd drive to. Fares are compared as fare + estimated drive cost.
ORIGINS = [o.strip().upper() for o in os.environ.get("ORIGINS", "CMH,CVG,CLE,DAY,DTW,PIT,IND").split(",") if o.strip()]
if ORIGIN not in ORIGINS:
    ORIGINS.insert(0, ORIGIN)
DRIVE_HOURS = {"CMH": 0, "CVG": 1.75, "CLE": 2.25, "DAY": 1.2, "DTW": 3.25, "PIT": 3.0, "IND": 3.0}  # one way, from Columbus
DRIVE_RATE = float(os.environ.get("DRIVE_COST_PER_HOUR", "20"))  # dollars per hour, round trip (gas + parking + time)
NOT_DEST = set(ORIGINS) | {"DTT", "CMH"}  # never suggest flying to the airports you'd drive to
WATCH_FILE = Path(__file__).with_name("watchlist.txt")  # optional: lines like "Lisbon 400"
_repo = os.environ.get("GITHUB_REPOSITORY", "")
SITE_URL = os.environ.get("SITE_URL") or (
    f"https://{_repo.split('/')[0].lower()}.github.io/{_repo.split('/')[1]}/" if "/" in _repo else "")
DROP_PCT = float(os.environ.get("DROP_PCT", "15"))
ONE_WAY = os.environ.get("ONE_WAY", "false").lower() == "true"
DIRECT_ONLY = os.environ.get("DIRECT_ONLY", "false").lower() == "true"
MAX_STOPS = int(os.environ.get("MAX_STOPS", "1"))
MAX_ALERTS = int(os.environ.get("MAX_ALERTS", "5"))  # per category, per run
EXCLUDE = {c.strip().lower() for c in os.environ.get("EXCLUDE", "").split(",") if c.strip()}

TOPICS["deals"] = os.environ.get("NTFY_TOPIC_DEALS", NTFY_TOPIC)
DEAL_FEEDS = [u.strip() for u in os.environ.get(
    "DEAL_FEEDS", "https://www.secretflying.com/feed/,https://www.theflightdeal.com/feed/,https://www.fly4free.com/feed/,https://www.reddit.com/r/traveldeals/new/.rss").split(",") if u.strip()]
# Posts mentioning any of these count. Add airports you'd drive to, e.g. "Cleveland,Cincinnati,Detroit".
DEAL_KEYWORDS = [k.strip() for k in os.environ.get("DEAL_KEYWORDS", "Columbus,CMH,Cleveland,Cincinnati,Dayton,Detroit,Pittsburgh,Indianapolis,Ohio").split(",") if k.strip()]
# Separate these with | (they contain commas)
DEAL_EXCLUDE = [k.strip().lower() for k in os.environ.get(
    "DEAL_EXCLUDE", "Columbus, GA|Columbus, Georgia|Columbus, MS|Columbus, IN").split("|") if k.strip()]
DEAL_MAX_AGE_DAYS = int(os.environ.get("DEAL_MAX_AGE_DAYS", "3"))  # fire sales expire fast
MAX_DEAL_ALERTS = int(os.environ.get("MAX_DEAL_ALERTS", "5"))
# Portal value of your points, in cents per point (change if the programs change them).
# Sapphire Preferred = 1.25 (Freedom points pooled into it redeem the same). Amex MR on flights = 1.0.
CPP_CHASE = float(os.environ.get("CPP_CHASE", "1.25"))
CPP_AMEX = float(os.environ.get("CPP_AMEX", "1.0"))
# Card-offer news from points blogs: must mention a card AND a flight word.
CARD_FEEDS = [u.strip() for u in os.environ.get("CARD_FEEDS",
    "https://upgradedpoints.com/feed/,https://www.doctorofcredit.com/feed/,https://thepointsguy.com/feed/,https://frequentmiler.com/feed/,https://onemileatatime.com/feed/").split(",") if u.strip()]
CARD_KEYWORDS = [k.strip() for k in os.environ.get("CARD_KEYWORDS",
    "Amex Travel,Amex Platinum,Platinum Card,Amex Gold,Gold Card,Chase Travel,Points Boost,Sapphire,Freedom,Ultimate Rewards,Membership Rewards").split(",") if k.strip()]
CARD_FLIGHT_WORDS = ["flight", "flights", "airfare", "airfares", "airline", "airlines", "fare", "fares", "Delta", "United", "Southwest", "JetBlue", "Points Boost"]
STATUS = {"ok": 0, "fail": 0}
PUSH_FAILS = []

US_NAMES = {"united states", "united states of america", "usa", "us"}
STATE = Path(__file__).with_name("seen.json")
OUT = Path(__file__).with_name("docs") / "deals.json"  # read by the web app


def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (cmh-flight-alerts)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def get_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


# A deal is a dict: name, price, stops, airline, dates, link, source,
# domestic (True / False / None if unknown; unknown is treated as international).

def extra_cost(origin):
    return round(DRIVE_HOURS.get(origin, 0) * 2 * DRIVE_RATE)


def aviasales():
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        return []
    cities = {c.get("code"): c for c in get_json("https://api.travelpayouts.com/data/en/cities.json")}
    deals, errs = [], []
    for origin in ORIGINS:
        params = {
            "origin": origin, "one_way": str(ONE_WAY).lower(), "direct": str(DIRECT_ONLY).lower(),
            "unique": "true", "sorting": "price", "currency": "usd", "limit": 100, "token": token,
        }
        try:
            body = get_json("https://api.travelpayouts.com/aviasales/v3/prices_for_dates?" + urllib.parse.urlencode(params))
            if not body.get("success"):
                raise RuntimeError(str(body)[:200])
        except Exception as e:
            errs.append(f"{origin}: {e}")
            print(f"aviasales {origin} failed: {e}", file=sys.stderr)
            continue
        extra = extra_cost(origin)
        for d in body.get("data", []):
            code = d.get("destination")
            if code in NOT_DEST or d.get("price") is None:
                continue
            city = cities.get(code, {})
            cc = city.get("country_code")
            dates = (d.get("departure_at") or "")[:10]
            if d.get("return_at"):
                dates += f" to {d['return_at'][:10]}"
            deals.append({
                "name": city.get("name") or code, "price": d["price"] + extra, "fare": d["price"], "extra": extra,
                "origin": origin, "stops": d.get("transfers", 0), "dur": d.get("duration_to") or d.get("duration"),
                "airline": d.get("airline", ""), "dates": dates, "source": "Aviasales",
                "link": ("https://www.aviasales.com" + d["link"]) if d.get("link") else None,
                "domestic": (cc == "US") if cc else None,
                "code": code, "dep": (d.get("departure_at") or "")[:10], "ret": (d.get("return_at") or "")[:10],
            })
        time.sleep(0.3)
    if not deals and errs:
        raise RuntimeError("Aviasales error: " + "; ".join(errs))
    return deals


def google_explore():
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        return []
    params = {"engine": "google_travel_explore", "departure_id": ORIGIN, "currency": "USD",
              "gl": "us", "hl": "en", "api_key": key}
    body = get_json("https://serpapi.com/search.json?" + urllib.parse.urlencode(params))
    if body.get("error"):
        raise RuntimeError(f"SerpApi error: {body['error']}")
    link = body.get("search_metadata", {}).get("google_travel_explore_url")
    deals = []
    for d in body.get("destinations", []):
        country = (d.get("country") or "").strip().lower()
        deals.append({
            "name": d.get("name"), "price": d.get("flight_price", d.get("price")),
            "stops": d.get("number_of_stops", 0), "airline": d.get("airline", ""),
            "dates": " to ".join(x for x in (d.get("start_date"), d.get("end_date")) if x),
            "source": "Google Flights", "link": link,
            "domestic": (country in US_NAMES) if country else None,
        })
    return deals


PROVIDERS = [aviasales, google_explore]


def collect():
    """Run every enabled provider; keep the cheapest fare per destination."""
    best, alts = {}, {}
    for p in PROVIDERS:
        try:
            deals = p()
        except Exception as e:  # one broken source shouldn't stop the others
            print(f"{p.__name__} failed: {e}", file=sys.stderr)
            STATUS["fail"] += 1
            continue
        STATUS["ok"] += 1
        print(f"{p.__name__}: {len(deals)} fares")
        for d in deals:
            name, price = d.get("name"), d.get("price")
            if not name or price is None or name.lower() in EXCLUDE:
                continue
            if (d.get("stops") or 0) > MAX_STOPS:
                continue
            d.setdefault("origin", ORIGIN)
            d.setdefault("fare", price)
            d.setdefault("extra", 0)
            bucket = "domestic" if d["domestic"] is True else "international"
            k = f"{bucket}|{name.lower()}"
            a = alts.setdefault(k, {})
            if d["origin"] not in a or price < a[d["origin"]]["price"]:
                a[d["origin"]] = {"o": d["origin"], "price": price, "fare": d["fare"], "dep": d.get("dep", ""),
                                  "ret": d.get("ret", ""), "link": d.get("link"), "stops": d.get("stops") or 0,
                                  "dur": d.get("dur"), "airline": d.get("airline", "")}
            if k not in best or price < best[k]["price"]:
                best[k] = {**d, "bucket": bucket}
    for k, d in best.items():
        d["alts"] = sorted(alts.get(k, {}).values(), key=lambda x: x["price"])
    return best


CAL_TOP = int(os.environ.get("CAL_TOP", "8"))        # destinations per tab that get a price calendar
CAL_MONTHS = int(os.environ.get("CAL_MONTHS", "3"))  # how many months ahead the calendar covers


def build_calendars(best):
    """For the cheapest destinations, fetch the cheapest fare for every departure day (Aviasales only)."""
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        return
    today = datetime.now(timezone.utc).date().replace(day=1)
    months = []
    for i in range(CAL_MONTHS):
        y, m = divmod(today.month - 1 + i, 12)
        months.append(f"{today.year + y}-{m + 1:02d}")
    for bucket in ("domestic", "international"):
        top = sorted((d for d in best.values() if d["bucket"] == bucket and d.get("code")), key=lambda d: d["price"])[:CAL_TOP]
        for d in top:
            days = {}
            for month in months:
                params = {
                    "origin": d.get("origin", ORIGIN), "destination": d["code"], "departure_at": month,
                    "one_way": str(ONE_WAY).lower(), "direct": str(DIRECT_ONLY).lower(), "unique": "false",
                    "sorting": "price", "currency": "usd", "limit": 1000, "token": token,
                }
                try:
                    body = get_json("https://api.travelpayouts.com/aviasales/v3/prices_for_dates?" + urllib.parse.urlencode(params))
                except Exception as e:
                    print(f"calendar {d['code']} {month} failed: {e}", file=sys.stderr)
                    continue
                for t in body.get("data", []) if body.get("success") else []:
                    dep = (t.get("departure_at") or "")[:10]
                    if not dep or (t.get("transfers") or 0) > MAX_STOPS:
                        continue
                    total = t["price"] + d.get("extra", 0)
                    if dep not in days or total < days[dep][2]:
                        days[dep] = [dep, (t.get("return_at") or "")[:10], total, t.get("transfers") or 0,
                                     t.get("link") or ""]
                time.sleep(0.2)
            d["cal"] = sorted(days.values())
            print(f"calendar {d['name']}: {len(days)} days")


def points(price):
    return round(price / (CPP_CHASE / 100)), round(price / (CPP_AMEX / 100))


def card_match(text):
    t = text.lower()
    return any(k.lower() in t for k in CARD_KEYWORDS) and any(
        re.search(r"\b" + re.escape(w) + r"\b", text, re.I) for w in CARD_FLIGHT_WORDS)


def matches(text):
    t = text.lower()
    if any(x in t for x in DEAL_EXCLUDE):
        return False
    return any(re.search(r"\b" + re.escape(k) + r"\b", text, re.I) for k in DEAL_KEYWORDS)


ATOM = "{http://www.w3.org/2005/Atom}"


def feed_items(text):
    """Return (title, link, date) for RSS or Atom text; falls back to a regex pass if the XML is malformed."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    out = []
    try:
        root = ET.fromstring(text)
        for it in root.iter("item"):
            out.append(((it.findtext("title") or "").strip(), (it.findtext("link") or "").strip(), it.findtext("pubDate")))
        for it in root.iter(ATOM + "entry"):
            ln = it.find(ATOM + "link")
            out.append(((it.findtext(ATOM + "title") or "").strip(), (ln.get("href") if ln is not None else "") or "",
                        it.findtext(ATOM + "updated")))
        return out
    except ET.ParseError:
        pass
    for m in re.finditer(r"<item\b.*?</item>", text, re.S):
        blk = m.group(0)

        def g(tag):
            x = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", blk, re.S)
            return html.unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", x.group(1), flags=re.S)).strip() if x else ""
        out.append((g("title"), g("link"), g("pubDate")))
    return out


def parse_when(s):
    try:
        return parsedate_to_datetime(s)
    except Exception:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_deal_posts(feeds=None, match=None, kind="deal"):
    """Error fares / fire sales from deal-blog RSS feeds, filtered to your airports."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEAL_MAX_AGE_DAYS)
    posts = {}
    for url in (feeds or DEAL_FEEDS):
        try:
            for title, link, pub in feed_items(get_text(url)):
                if not title or not link:
                    continue
                try:
                    when = parse_when(pub)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    if when < cutoff:
                        continue
                except Exception:
                    pass  # no usable date: keep it, dedupe still prevents repeats
                if (match or matches)(title):
                    posts[f"deal|{link}"] = {"title": title, "link": link, "source": urllib.parse.urlparse(url).netloc, "kind": kind}
            STATUS["ok"] += 1
        except Exception as e:
            print(f"deal feed {url} failed: {e}", file=sys.stderr)
            STATUS["fail"] += 1
    print(f"{kind} feeds: {len(posts)} matching posts")
    return posts


def push(bucket, title, message, click=None):
    """Send one phone alert. If ntfy rejects the tap-link, retry without it; log why on failure."""
    tags = {"domestic": "us", "international": "earth_americas", "deals": "fire"}
    topic = TOPICS[bucket].strip()
    for use_click in ([True, False] if click else [False]):
        headers = {"Title": title, "Tags": tags[bucket]}
        if use_click:
            headers["Click"] = click
        req = urllib.request.Request(f"{NTFY_SERVER}/{topic}", data=message.encode("utf-8"), headers=headers)
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return True
        except urllib.error.HTTPError as e:
            print(f"ntfy rejected the alert (HTTP {e.code}): {e.read().decode('utf-8', 'replace')[:300]}", file=sys.stderr)
        except Exception as e:
            print(f"ntfy push failed: {e}", file=sys.stderr)
            break
    PUSH_FAILS.append(title)
    return False


def load_watch():
    """watchlist.txt: one 'City price' per line, e.g. 'Lisbon 400'. Optional."""
    watch = {}
    try:
        for line in WATCH_FILE.read_text().splitlines():
            line = line.split("#")[0].strip()
            m = re.match(r"(.+?)[\s,=:]+\$?(\d+)$", line)
            if m:
                watch[m.group(1).strip().lower()] = float(m.group(2))
    except FileNotFoundError:
        pass
    return watch


def site_link(d, k):
    """Tapping a phone alert opens the app on that destination's calendar."""
    return (SITE_URL + "#" + urllib.parse.quote(k, safe="")) if SITE_URL else d.get("link")


def write_site(best, posts):
    """Save everything current to docs/deals.json for the web app (keeps a short price history)."""
    OUT.parent.mkdir(exist_ok=True)
    try:
        old = json.loads(OUT.read_text())
    except Exception:
        old = {}
    hist = {d["key"]: d.get("hist", []) for d in old.get("deals", [])}
    now = datetime.now(timezone.utc)
    deals = [{
        "key": k, "bucket": d["bucket"], "name": d["name"], "price": d["price"], "prev": d.get("prev"),
        "stops": d.get("stops") or 0, "airline": d.get("airline", ""), "dates": d.get("dates", ""),
        "source": d["source"], "link": d.get("link"), "code": d.get("code"), "dep": d.get("dep", ""), "ret": d.get("ret", ""),
        "origin": d.get("origin", ORIGIN), "extra": d.get("extra", 0), "fare": d.get("fare", d["price"]), "dur": d.get("dur"),
        "alts": d.get("alts", []), "watch": d.get("watch"),
        "cal": d.get("cal", []), "ur": points(d.get("fare", d["price"]))[0], "mr": points(d.get("fare", d["price"]))[1], "hist": (hist.get(k, []) + [d["price"]])[-30:],
    } for k, d in best.items()]
    deals.sort(key=lambda x: x["price"])
    keep = now - timedelta(days=14)
    kept = {p["link"]: p for p in old.get("posts", []) if datetime.fromisoformat(p["seen_at"]) > keep}
    for p in posts.values():
        kept.setdefault(p["link"], {**p, "seen_at": now.isoformat()})
    OUT.write_text(json.dumps({
        "updated": now.isoformat(), "origin": ORIGIN, "deals": deals,
        "posts": sorted(kept.values(), key=lambda p: p["seen_at"], reverse=True),
    }))


def main():
    seen = json.loads(STATE.read_text()) if STATE.exists() else {}
    alerts = {"domestic": [], "international": []}

    best = collect()
    watch = load_watch()
    for k, d in best.items():
        d["key"] = k
        d["watch"] = watch.get(d["name"].lower()) or watch.get((d.get("code") or "").lower())
        price, bucket = d["price"], d["bucket"]
        prev = seen.get(k)
        cheap_enough = price <= MAX_PRICE[bucket] or bool(d["watch"] and price <= d["watch"])
        big_drop = prev is not None and price <= prev * (1 - DROP_PCT / 100)
        is_new_low = prev is None or price < prev
        if (cheap_enough and is_new_low) or big_drop:
            alerts[bucket].append((price, prev, d))
        d["prev"] = prev
        seen[k] = price

    sent = 0
    for bucket, items in alerts.items():
        items.sort(key=lambda a: a[0])
        for price, prev, d in items[:MAX_ALERTS]:
            stops = d.get("stops") or 0
            note = f" (was ${prev:.0f})" if prev else ""
            label = "Domestic" if bucket == "domestic" else "Intl"
            if d.get("watch") and price <= d["watch"]:
                label = "WATCHLIST"
            org = d.get("origin", ORIGIN)
            drive = (f"Fly from {org}: about {DRIVE_HOURS.get(org, 0):g}h drive each way, ${d.get('extra', 0)} drive cost included in ${price:.0f}\n"
                     if org != ORIGIN else "")
            fare = d.get("fare", price)
            push(
                bucket,
                f"{label}: {org} to {d['name']} ${price:.0f}{note}",
                f"{d['dates']} | {'nonstop' if stops == 0 else f'{stops} stop'} | {d['airline']} | via {d['source']}\n"
                + drive +
                f"About {points(fare)[0]:,} Chase UR or {points(fare)[1]:,} Amex MR in the portals\n"
                "Fares can be cached, so confirm the price before booking.",
                site_link(d, d["key"]),
            )
            sent += 1

    # Error fares / fire sales from deal blogs
    posts = fetch_deal_posts()
    posts.update(fetch_deal_posts(CARD_FEEDS, card_match, "card"))
    new_posts = [(k, p) for k, p in posts.items() if k not in seen]
    for k, p in new_posts[:MAX_DEAL_ALERTS]:
        push("deals", f"{'Card offer' if p['kind'] == 'card' else 'Deal'}: {p['title']}"[:200],
             f"Via {p['source']}. " + ("Offers may be targeted, so check your account." if p["kind"] == "card" else "Deals like this can expire fast."), p["link"])
        sent += 1
    for k, _ in new_posts[MAX_DEAL_ALERTS:]:
        pass  # beyond the cap: still marked seen below so they don't pile up
    seen = {k: v for k, v in seen.items() if not k.startswith("deal|")}
    seen.update({k: 1 for k in posts})

    try:
        build_calendars(best)
    except Exception as e:  # the calendar is a bonus; never let it break alerts
        print(f"calendars failed: {e}", file=sys.stderr)
    write_site(best, posts)
    if not PUSH_FAILS:  # if a push failed, don't mark fares as seen, so they alert again next run
        STATE.write_text(json.dumps(seen, indent=2, sort_keys=True))
    print(f"{sent} pushed (domestic {len(alerts['domestic'])}, international {len(alerts['international'])} qualified)")
    if PUSH_FAILS:
        sys.exit(f"{len(PUSH_FAILS)} alert(s) could not be sent. See the ntfy messages above.")
    if STATUS["fail"] and not STATUS["ok"]:
        sys.exit("All sources failed.")


if __name__ == "__main__":
    main()
