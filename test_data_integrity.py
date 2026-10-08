"""Integrity tests for the data files. Run with `python3 -m pytest` or just
`python3 test_data_integrity.py`. Needs only jsonschema (requests too for the
live freshness fallback in --gate).

Modes (plain run = every data test, offline, against the committed files):
  --catalog         decks.json checks only (CI runs this before scraping)
  --pending-scrape  every data test, but id sets that differ between decks.json
                    and the generated files are warnings: a catalog push lands
                    before the next scrape regenerates prices for it
  --gate            every data test plus the post scrape gates: coverage
                    floors, a fresh run, fresh upstream feeds, and (with
                    --previous DIR) no sharp drop against the last committed run
  --root DIR        read the data files from DIR instead of this folder

Set ALLOW_PRICE_JUMPS=1 (the scrape workflow's manual input) to let a run
through where many decks or boxes moved sharply since their previous day, as
an intentional matcher fix does.

Price bands below were measured on 90 days of committed prices.json (97
snapshots, 2026-07-08 to 2026-10-07) and set with roughly 1.5 to 2 times
headroom past the extremes seen, so real market moves do not flake CI while
unit errors, doubled prices and mismatched products still fail.
"""

import json
import math
import os
import re
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import jsonschema

ROOT = Path(__file__).parent
PENDING = False   # set by --pending-scrape

# Post scrape coverage floors, about 90% of steady state on 2026-10-07
# (109 TCG, 110 CK, 117 crack, 32 box sets, 4832 index cards).
MIN_TCG = 98
MIN_CK = 99
MIN_CRACK = 105          # entries with both vendor totals and >= 90% of cards priced
# Crack entries allowed to be less than fully priced (every entry was fully
# priced in all 97 snapshots), as a count or a share, whichever is more.
MAX_CRACK_PARTIAL = 3
MIN_BOX_SETS = 29
MIN_INDEX_CARDS = 4300
# Drop against the previous committed run that fails the gate.
MAX_COVERAGE_DROP = 0.10
MAX_CRACK_MEDIAN_MOVE = 0.30
MAX_HISTORY_DROP = 0.20
# History is compared over this many recent days, which both the previous and
# the current file must keep (the scraper keeps 90), so trimming old days or a
# one off history migration does not read as a reset.
HISTORY_COMPARE_DAYS = 60
# Day over day moves against the previous history entry. Over 90 days no deck
# moved more than 1.16 times and no box more than 1.33 times in one day.
BIG_MOVE = 1.5
MAX_BIG_MOVERS = 3       # or 3% of the rows compared, whichever is more
HUGE_MOVE = 2.5          # any single row past this fails
# Freshness. TCGCSV republishes daily around 20:05 UTC, MTGJSON around 06:15 UTC.
MAX_RUN_AGE = timedelta(hours=6)
MAX_TCGCSV_AGE = timedelta(days=3)
MAX_MTGJSON_AGE = timedelta(days=7)
TCGCSV_LAST_UPDATED = "https://tcgcsv.com/last-updated.txt"
MTGJSON_META = "https://mtgjson.com/api/v5/Meta.json"
# (feed, max age, stale blocks the commit). MTGJSON only feeds crack values and
# box EV, which the scraper carries forward, so a stale MTGJSON only warns and
# never holds back the TCG and CK prices.
UPSTREAM_FEEDS = (("tcgcsv", MAX_TCGCSV_AGE, True), ("mtgjson", MAX_MTGJSON_AGE, False))

VENDOR_HOSTS = {"tcgplayer": "tcgplayer.com", "cardkingdom": "cardkingdom.com"}


def _reject_constant(name):
    raise ValueError(f"non standard JSON number {name}")


def _finite_float(text):
    v = float(text)
    if not math.isfinite(v):
        raise ValueError(f"number out of range: {text}")
    return v


@lru_cache(maxsize=None)
def _load(path: Path):
    # Strict parse: Python's json accepts NaN, Infinity and 1e999 by default,
    # but browsers reject the file outright, so they must fail here.
    return json.loads(path.read_text(), parse_constant=_reject_constant,
                      parse_float=_finite_float)


def load(name: str):
    return _load(ROOT / name)


def on_host(url, host: str) -> bool:
    """An https URL on `host` or one of its subdomains."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    h = urlparse(url).hostname or ""
    return h == host or h.endswith("." + host)


def jumps_allowed() -> bool:
    return os.environ.get("ALLOW_PRICE_JUMPS", "").strip().lower() in ("1", "true", "yes")


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def decks_by_id() -> dict:
    return {d["id"]: d for d in load("decks.json")}


def former_id_map() -> dict:
    """{former id: current id} from the decks.json formerIds field."""
    return {old: d["id"] for d in load("decks.json") for old in d.get("formerIds", [])}


def ids_must_match(what: str, missing=(), extra=()):
    """Deck id sets that disagree. Fatal, except under --pending-scrape where a
    decks.json change is waiting for the next scrape to regenerate the data."""
    missing, extra = sorted(missing), sorted(extra)
    if not (missing or extra):
        return
    msg = f"{what}: missing {missing[:5]} stale {extra[:5]}"
    if PENDING:
        print(f"  note {msg} (allowed until the next scrape)")
        return
    raise AssertionError(msg)


def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def priced(vendor: str) -> dict:
    """{deck id: price} for one vendor's priced entries."""
    return {did: e["price"] for did, e in load("prices.json")["vendors"].get(vendor, {}).items()
            if num((e or {}).get("price"))}


def crack_best(c: dict):
    vals = [v for v in (c.get("tcg"), c.get("cardkingdom")) if num(v) and v > 0]
    return max(vals) if vals else None


def crack_full(c: dict) -> bool:
    """Both vendor totals present and at least 90% of the list priced."""
    p, m = c.get("priced"), c.get("missing")
    return (num(c.get("tcg")) and c["tcg"] > 0 and num(c.get("cardkingdom")) and c["cardkingdom"] > 0
            and isinstance(p, int) and isinstance(m, int) and p > 0 and p >= 0.9 * (p + m))


# ---------------------------------------------------------------------------
# Catalog (decks.json)
# ---------------------------------------------------------------------------

def test_decks_schema():
    jsonschema.validate(load("decks.json"), load("decks.schema.json"),
                        format_checker=jsonschema.FormatChecker())


def test_unique_deck_ids():
    decks = load("decks.json")
    ids = [d["id"] for d in decks]
    assert len(ids) == len(set(ids)), "duplicate deck ids in decks.json"


def test_id_slug_format():
    decks = load("decks.json")
    for d in decks:
        assert re.fullmatch(r"[a-z0-9-]+", d["id"]), f"bad id slug: {d['id']!r}"


def test_decks_dates():
    """Real calendar dates (the schema regex alone takes 2026-02-30), and no
    release far in the future (preorders are fine, a typo'd year is not)."""
    latest = date.today() + timedelta(days=366)
    for d in load("decks.json"):
        rel = date.fromisoformat(d["releaseDate"])
        assert date(1993, 8, 5) <= rel <= latest, f"{d['id']}: releaseDate {rel} out of range"
        if d.get("priceDate"):
            date.fromisoformat(d["priceDate"])
        assert num(d.get("msrp")) and d["msrp"] > 0, f"{d['id']}: msrp must be positive"


def test_former_ids():
    """formerIds (deck renames): never a live id, never claimed twice."""
    current = {d["id"] for d in load("decks.json")}
    seen = {}
    for d in load("decks.json"):
        for old in d.get("formerIds", []):
            assert old != d["id"], f"{d['id']} lists itself in formerIds"
            assert old not in current, f"{d['id']}: former id {old} is still a live deck id"
            assert old not in seen, f"former id {old} claimed by {seen[old]} and {d['id']}"
            seen[old] = d["id"]


CATALOG_TESTS = (test_decks_schema, test_unique_deck_ids, test_id_slug_format,
                 test_decks_dates, test_former_ids)


# ---------------------------------------------------------------------------
# Generated data: shape
# ---------------------------------------------------------------------------

def test_prices_schema():
    """prices.json validates against prices.schema.json (the machine generated
    file most likely to drift)."""
    jsonschema.validate(load("prices.json"), load("prices.schema.json"),
                        format_checker=jsonschema.FormatChecker())


def test_prices_header():
    prices = load("prices.json")
    parse_utc(prices["updated_at"])
    if PENDING:
        return
    assert prices.get("deck_count") == len(load("decks.json")), \
        f"deck_count {prices.get('deck_count')} != {len(load('decks.json'))} decks"


def test_prices_match_decks():
    """Every deck in decks.json has a corresponding entry in each vendor map."""
    deck_ids = set(decks_by_id())
    for vendor, entries in load("prices.json")["vendors"].items():
        ids_must_match(f"vendors.{vendor}", deck_ids - set(entries), set(entries) - deck_ids)


def test_prices_shape():
    """Each vendor entry has the expected keys."""
    prices = load("prices.json")
    for vendor, entries in prices["vendors"].items():
        for did, entry in entries.items():
            assert "price" in entry, f"{vendor}/{did} missing price key"
            assert "url" in entry, f"{vendor}/{did} missing url key"
            assert "status" in entry, f"{vendor}/{did} missing status key"
            if entry["price"] is not None:
                assert num(entry["price"]), f"{vendor}/{did} non numeric price"
                assert entry["price"] > 0, f"{vendor}/{did} non-positive price {entry['price']}"


def test_vendor_listing_is_the_deck():
    """A priced listing lives on its vendor's site and names the deck.

    The TCG matcher requires the deck name inside the product name, and every
    priced TCG and CK listing over the last 90 days passed this. It catches a
    vendor map rotated onto the wrong decks, swapped vendor maps, and swapped
    deck names."""
    decks = decks_by_id()
    renamed = set()
    for vendor, entries in load("prices.json")["vendors"].items():
        host = VENDOR_HOSTS.get(vendor)
        for did, e in entries.items():
            if e.get("url") and host:
                assert on_host(e["url"], host), f"{vendor}/{did} url is not on {host}: {e['url']}"
            if not num(e.get("price")) or did not in decks:
                continue
            name = norm(decks[did]["name"])
            text = norm(f"{e.get('snippet') or ''} {e.get('url') or ''}")
            if name in text:
                continue
            # CK's matcher also accepts a CK deck name contained in ours.
            slug = (urlparse(e.get("url") or "").path.rstrip("/").rsplit("/", 1)[-1])
            tail = norm(re.split(r"commander-decks?-", slug)[-1])
            if vendor == "cardkingdom" and tail and tail in name:
                continue
            assert PENDING, \
                f"{vendor}/{did} listing does not name '{decks[did]['name']}': {e.get('snippet') or e.get('url')}"
            renamed.add(did)
    if renamed:
        print(f"  note listings still carry old names for {sorted(renamed)[:5]} (allowed until the next scrape)")


def test_crack_shape():
    """crack map: keys are real deck ids; buy/sell numbers non-negative;
    sell never exceeds buy for the same vendor; counts add up; an entry with
    priced cards has a vendor total (the page drops a zero total silently)."""
    prices = load("prices.json")
    deck_ids = set(decks_by_id())
    crack = prices.get("crack", {})
    ids_must_match("crack", extra=set(crack) - deck_ids)
    for did, c in crack.items():
        for k in ("tcg", "cardkingdom", "sell_tcg", "sell_cardkingdom"):
            v = c.get(k)
            if v is not None:
                assert num(v) and v >= 0, f"crack {did}.{k} bad: {v}"
        # Sell must not exceed buy for the same vendor (economically impossible).
        if c.get("sell_cardkingdom") is not None and c.get("cardkingdom") is not None:
            assert c["sell_cardkingdom"] <= c["cardkingdom"] + 1e-6, f"crack {did}: CK sell > buy"
        if c.get("sell_tcg") is not None and c.get("tcg") is not None:
            assert c["sell_tcg"] <= c["tcg"] + 1e-6, f"crack {did}: TCG sell > buy"
        n, p, m = c.get("card_count"), c.get("priced"), c.get("missing")
        if all(isinstance(x, int) for x in (n, p, m)):
            assert n > 0 and p + m <= n, f"crack {did}: priced {p} + missing {m} > card_count {n}"
        if isinstance(p, int) and p > 0:
            assert crack_best(c) is not None, \
                f"crack {did}: {p} cards priced but no positive TCG or CK total"
        best = crack_best(c)
        for card in c.get("top_cards") or []:
            assert best is not None and card.get("price", 0) <= best + 1e-6, \
                f"crack {did}: chase card {card.get('name')} ${card.get('price')} exceeds the deck total"


def test_crack_content():
    """Crack entries carry real totals, not just a count: all but a few
    (MAX_CRACK_PARTIAL) have both vendor totals and 90% or more of their cards
    priced. Every snapshot in the last 90 days had all of them."""
    crack = load("prices.json").get("crack", {})
    if not crack:
        return
    partial = sorted(did for did, c in crack.items() if not crack_full(c))
    allowed = max(MAX_CRACK_PARTIAL, int(0.03 * len(crack)))
    assert len(partial) <= allowed, \
        f"{len(partial)} of {len(crack)} crack entries are not fully priced (TCG and CK totals, 90% of cards), e.g. {partial[:5]}"


def buyable(vendors: dict, did: str) -> dict:
    """{vendor: price} a buyer can act on for a deck: TCG, and CK while it has
    stock (an unknown count counts as in stock, as on the page)."""
    out = {}
    t = (vendors.get("tcgplayer", {}).get(did) or {})
    if num(t.get("price")):
        out["tcgplayer"] = t["price"]
    k = (vendors.get("cardkingdom", {}).get(did) or {})
    if num(k.get("price")) and (k.get("qty") is None or k["qty"] > 0):
        out["cardkingdom"] = k["price"]
    return out


def check_bundle_components(bid: str, b: dict, vendors: dict):
    """`components` names the vendor and price behind each part of
    individual_total. Each must be that vendor's live, buyable price for the
    deck (the cheapest one under individual_basis cheapest_buyable), and the
    total must be their sum."""
    comps = b["components"]
    assert isinstance(comps, list) and [c.get("deck_id") for c in comps] == list(b["deck_ids"]), \
        f"bundle {bid}: components do not list its decks {b['deck_ids']}"
    total = b.get("individual_total")
    for c in comps:
        did, v, p = c["deck_id"], c.get("vendor"), c.get("price")
        if p is None:
            assert total is None, f"bundle {bid}: {did} has no price but individual_total is {total}"
            continue
        assert v in VENDOR_HOSTS, f"bundle {bid}: {did} priced from unknown vendor {v!r}"
        live = (vendors.get(v, {}).get(did) or {})
        assert num(live.get("price")) and abs(live["price"] - p) <= 0.011, \
            f"bundle {bid}: {did} {v} ${p} is not that vendor's live price {live.get('price')}"
        opts = buyable(vendors, did)
        assert v in opts, f"bundle {bid}: {did} counted at {v} ${p}, which has no stock"
        if b.get("individual_basis") == "cheapest_buyable":
            assert p <= min(opts.values()) + 0.011, \
                f"bundle {bid}: {did} counted at {v} ${p}, but {min(opts, key=opts.get)} has it for ${min(opts.values())}"
    if total is not None:
        parts = sum(c["price"] for c in comps)
        assert abs(parts - total) <= 0.05, \
            f"bundle {bid} individual_total {total} != its components' sum {parts:.2f}"


def test_bundles_shape():
    """bundles: deck_ids reference real decks; price positive; savings>0 or null;
    the individual total is the sum of its components (or, for data written
    before components existed, of the member decks' TCG prices)."""
    prices = load("prices.json")
    deck_ids = set(decks_by_id())
    tcg = priced("tcgplayer")
    for bid, b in prices.get("bundles", {}).items():
        assert num(b.get("price")) and b["price"] > 0, f"bundle {bid} bad price"
        if b.get("url") is not None:
            assert on_host(b["url"], "tcgplayer.com"), f"bundle {bid} url is not on tcgplayer.com: {b['url']}"
        ids_must_match(f"bundle {bid}", extra=set(b.get("deck_ids", [])) - deck_ids)
        if b.get("savings") is not None:
            assert b["savings"] > 0, f"bundle {bid} non-positive savings stored: {b['savings']}"
        # Savings arithmetic must be self-consistent when a total is recorded.
        if b.get("savings") is not None and b.get("individual_total") is not None:
            expected = round(b["individual_total"] - b["price"], 2)
            assert abs(expected - b["savings"]) <= 0.02, \
                f"bundle {bid} savings {b['savings']} != total-price {expected}"
        total = b.get("individual_total")
        if b.get("components") is not None:
            check_bundle_components(bid, b, prices["vendors"])
        elif total is not None and all(d in tcg for d in b["deck_ids"]):
            parts = sum(tcg[d] for d in b["deck_ids"])
            assert abs(parts - total) <= 0.05, \
                f"bundle {bid} individual_total {total} != member TCG sum {parts:.2f}"
        if total is not None:
            assert num(total) and total > 0, f"bundle {bid} bad individual_total {total}"
            # Observed 0.72 to 1.70 over 90 days.
            ratio = b["price"] / total
            assert 0.3 <= ratio <= 3.0, f"bundle {bid} price is {ratio:.2f} times its decks"


def test_boxes_shape():
    """boxes: each entry has type/price; price positive; no duplicate type per
    set; the TCG link is on tcgplayer.com."""
    prices = load("prices.json")
    valid_types = {"play", "collector", "jumpstart"}
    for set_name, rows in prices.get("boxes", {}).items():
        seen = set()
        for r in rows:
            assert r.get("type") in valid_types, f"box {set_name} bad type {r.get('type')}"
            assert r["type"] not in seen, f"box {set_name} duplicate type {r['type']}"
            seen.add(r["type"])
            assert num(r.get("price")) and r["price"] > 0, f"box {set_name} bad price"
            if r.get("url") is not None:
                assert on_host(r["url"], "tcgplayer.com"), \
                    f"box {set_name}/{r['type']} url is not on tcgplayer.com: {r['url']}"


def test_history_shape():
    """If prices_history.json exists, it has the expected shape."""
    p = ROOT / "prices_history.json"
    if not p.exists():
        return  # built up over time by the scraper
    history = load("prices_history.json")
    assert "decks" in history
    deck_ids = set(decks_by_id())
    ids_must_match("history", extra=set(history["decks"]) - deck_ids)
    for did, series in history["decks"].items():
        assert isinstance(series, list)
        prev_date = None
        for entry in series:
            assert "date" in entry and re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["date"])
            date.fromisoformat(entry["date"])
            assert prev_date is None or entry["date"] > prev_date, \
                f"history {did}: dates not strictly increasing ({prev_date} -> {entry['date']})"
            prev_date = entry["date"]
            for k, v in entry.items():
                if k in ("tcg", "ck", "best"):
                    assert num(v) and v > 0, f"history {did} {entry['date']}: bad {k} {v!r}"
                elif k != "date":
                    # Room for optional per day extras (stock counts and the
                    # like); vendor prices must use the keys above.
                    assert v is None or isinstance(v, bool) or num(v), \
                        f"history {did} {entry['date']}: unexpected value {k}={v!r}"
    # Box history (optional key): keyed "<set>::<type>", entries {date, price}.
    for key, series in (history.get("boxes") or {}).items():
        assert "::" in key, f"bad box history key: {key}"
        assert isinstance(series, list)
        prev_date = None
        for entry in series:
            assert "date" in entry and re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["date"])
            date.fromisoformat(entry["date"])
            assert prev_date is None or entry["date"] > prev_date, \
                f"box history {key}: dates not strictly increasing"
            prev_date = entry["date"]
            assert num(entry.get("price")) and entry["price"] > 0


def test_history_matches_prices():
    """The newest history day is the run that wrote prices.json: it is no later
    than updated_at, at most a day earlier, and its TCG values are the live ones
    (the chart's last point must equal the headline price)."""
    if not (ROOT / "prices_history.json").exists():
        return
    prices, history = load("prices.json"), load("prices_history.json")
    series = {d: s for d, s in history.get("decks", {}).items() if s}
    if not series:
        return
    newest = max(s[-1]["date"] for s in series.values())
    run_day = parse_utc(prices["updated_at"]).date()
    assert run_day - timedelta(days=1) <= date.fromisoformat(newest) <= run_day, \
        f"newest history day {newest} does not match prices.json updated_at {prices['updated_at']}"
    for did, price in priced("tcgplayer").items():
        last = (series.get(did) or [{}])[-1]
        if last.get("date") == newest and num(last.get("tcg")):
            assert abs(last["tcg"] - price) < 0.011, \
                f"history {did} {newest}: tcg {last['tcg']} != live {price}"


def test_crack_top_cards():
    """top_cards (chase card breakdown, F1): list of {name, price}, prices
    positive and sorted descending, capped at a small N."""
    prices = load("prices.json")
    for did, c in prices.get("crack", {}).items():
        tc = c.get("top_cards")
        if tc is None:
            continue
        assert isinstance(tc, list), f"crack {did}.top_cards not a list"
        assert len(tc) <= 4, f"crack {did}.top_cards too long: {len(tc)}"
        prev = None
        for card in tc:
            assert isinstance(card.get("name"), str) and card["name"], f"crack {did} top card missing name"
            p = card.get("price")
            assert num(p) and p > 0, f"crack {did} top card bad price {p}"
            if prev is not None:
                assert p <= prev + 1e-6, f"crack {did} top_cards not sorted desc"
            prev = p


def test_boxes_ev():
    """box EV (F3): when present, a positive number labeled as an estimate."""
    prices = load("prices.json")
    for set_name, rows in prices.get("boxes", {}).items():
        for r in rows:
            if "ev" in r and r["ev"] is not None:
                assert num(r["ev"]) and r["ev"] > 0, \
                    f"box {set_name}/{r.get('type')} bad ev {r['ev']}"


def test_cards_index():
    """cards_index.json (F2 reverse staple finder): if present, maps card name
    keys to {name, decks:[real deck ids]}, and agrees with the crack chase
    cards it was built from."""
    p = ROOT / "cards_index.json"
    if not p.exists():
        return  # produced alongside prices.json by the scraper
    idx = load("cards_index.json")
    cards = idx.get("cards")
    assert isinstance(cards, dict) and cards, "cards_index has no cards map"
    deck_ids = set(decks_by_id())
    unknown = set()
    by_name = {}
    for key, entry in cards.items():
        assert isinstance(entry.get("name"), str) and entry["name"], f"card {key!r} missing name"
        decks = entry.get("decks")
        assert isinstance(decks, list) and decks, f"card {key!r} has no decks"
        unknown |= set(decks) - deck_ids
        assert len(decks) == len(set(decks)), f"card {key!r} has duplicate deck ids"
        by_name.setdefault(entry["name"], set()).update(decks)
    ids_must_match("cards_index", extra=unknown)
    # Every chase card is a non basic card of its deck, so the index must list
    # that deck for it. A shifted index fails this.
    chase = [(did, t["name"]) for did, c in load("prices.json").get("crack", {}).items()
             for t in c.get("top_cards") or []]
    wrong = [(did, n) for did, n in chase if did not in by_name.get(n, ())]
    assert len(wrong) <= 0.05 * max(1, len(chase)), \
        f"{len(wrong)} of {len(chase)} chase cards are not indexed under their deck, e.g. {wrong[:3]}"


def test_vendors_cardkingdom():
    """CK vendor map (re-enabled via CK's public pricelist API): keys are real
    deck ids; price positive-or-null; url present; status from a known set."""
    prices = load("prices.json")
    deck_ids = set(decks_by_id())
    ck = prices["vendors"].get("cardkingdom", {})
    assert ck, "vendors.cardkingdom missing"
    ids_must_match("vendors.cardkingdom", extra=set(ck) - deck_ids)
    for did, e in ck.items():
        assert "url" in e, f"CK {did} missing url"
        # Status is informational — assert it's a non-empty string rather than a
        # brittle whitelist that drifts from the scraper's actual values.
        if e.get("status") is not None:
            assert isinstance(e["status"], str) and e["status"], f"CK {did} bad status {e['status']!r}"
        p = e.get("price")
        if p is not None:
            assert num(p) and p > 0, f"CK {did} bad price {p}"
        if e.get("buy") is not None:
            assert num(e["buy"]) and e["buy"] >= 0, f"CK {did} bad buy"
        if e.get("qty") is not None:
            assert isinstance(e["qty"], int) and e["qty"] >= 0, f"CK {did} bad qty"


def test_boxes_ck():
    """CK box prices (when present): positive number; qty int>=0; url on
    cardkingdom.com; in the same ballpark as TCG per box and across boxes."""
    prices = load("prices.json")
    ratios = []
    for set_name, rows in prices.get("boxes", {}).items():
        for r in rows:
            if r.get("ck_price") is not None:
                assert num(r["ck_price"]) and r["ck_price"] > 0, \
                    f"box {set_name}/{r.get('type')} bad ck_price {r['ck_price']}"
            if r.get("ck_qty") is not None:
                assert isinstance(r["ck_qty"], int) and r["ck_qty"] >= 0, \
                    f"box {set_name}/{r.get('type')} bad ck_qty"
            if r.get("ck_url") is not None:
                assert on_host(r["ck_url"], "cardkingdom.com"), \
                    f"box {set_name}/{r.get('type')} ck_url is not on cardkingdom.com: {r['ck_url']}"
            if r.get("ck_buy") is not None:
                assert num(r["ck_buy"]) and r["ck_buy"] >= 0, \
                    f"box {set_name}/{r.get('type')} bad ck_buy {r['ck_buy']}"
            # Sanity: CK retail and TCG market should be in the same ballpark
            # (a mismatched product would be wildly off). Observed 0.78 to 2.05.
            if r.get("ck_price") is not None and r.get("price"):
                ratio = r["ck_price"] / r["price"]
                ratios.append(ratio)
                assert 0.2 <= ratio <= 5.0, \
                    f"box {set_name}/{r.get('type')} CK/TCG ratio {ratio:.2f} out of range"
    # The median ran 1.14 to 1.20 over 90 days; one vendor's box prices
    # doubled or halved moves it far outside.
    _check_median("box CK/TCG", ratios, 0.75, 1.75)


def test_history_best_is_min():
    """`best` must equal the cheapest buyable vendor recorded that day: it
    drives the all time low badge, so a wrong value misleads directly. A CK
    value flagged out of stock (ck_qty 0) is not buyable."""
    p = ROOT / "prices_history.json"
    if not p.exists():
        return
    history = load("prices_history.json")
    for did, series in history.get("decks", {}).items():
        for e in series:
            keys = ("tcg",) if e.get("ck_qty") == 0 else ("tcg", "ck")
            vals = [e[k] for k in keys if num(e.get(k))]
            if not vals or "best" not in e:
                continue
            assert abs(e["best"] - min(vals)) < 0.011, (
                f"history {did} {e.get('date')}: best={e['best']} != min{vals}")


def test_no_wrong_product_matches():
    """A vendor listing must be the same KIND of product as the deck.

    Regression guard: name-only matching once priced Urza's Iron Alliance off a
    *prerelease pack* (-74%) and Lorehold Legacies off a "(Deck Only)" SKU
    (-61%). Because those are cheaper, they won the best-price headline and
    pointed buyers at the wrong product.
    """
    prices = load("prices.json")
    decks = decks_by_id()
    markers = ("prerelease", "brawl", "booster", "bundle", "display", "case",
               "deck only", "commander kit", "collector", "jumpstart",
               "starter kit", "sample", "art series")
    for vendor, entries in prices["vendors"].items():
        for did, e in entries.items():
            if not e.get("price"):
                continue
            deck_name = (decks.get(did, {}).get("name") or "").lower()
            # Both the human snippet and the product URL leak the product kind.
            text = f"{e.get('snippet') or ''} {e.get('url') or ''}".lower()
            for m in markers:
                assert not (m in text and m not in deck_name), (
                    f"{vendor}/{did} looks like a different product ('{m}'): "
                    f"{e.get('snippet') or e.get('url')}")


# ---------------------------------------------------------------------------
# Generated data: price sanity (both directions)
# ---------------------------------------------------------------------------

def _check_median(label, ratios, lo, hi, min_n=20):
    if len(ratios) < min_n:
        return
    m = statistics.median(ratios)
    assert lo <= m <= hi, f"median {label} is {m:.2f}, outside {lo} to {hi}: a unit or matching error across many decks"


def test_vendor_price_sanity():
    """Card Kingdom and TCGplayer must be quoting the same product for a deck.

    The two legitimately drift apart: Card Kingdom keeps fixed retail asks
    (often for sold out stock) while TCGplayer Market moves. Over 90 days the
    TCG/CK ratio per deck ran from 0.52 to 1.78, so each deck gets a wide band
    (0.3 to 3.3). The median across decks ran 0.89 to 0.92, so it gets a tight
    one (0.6 to 1.4): one vendor doubled, tripled or cut to a third moves it far
    outside while no real market does.
    """
    tcg, ck = priced("tcgplayer"), priced("cardkingdom")
    ratios = []
    for did in tcg.keys() & ck.keys():
        t, c = tcg[did], ck[did]
        ratios.append(t / c)
        for name, p, other in (("tcgplayer", t, c), ("cardkingdom", c, t)):
            assert p >= other * 0.3, (
                f"{name}/{did} = ${p:.2f} is under 0.3 times the other vendor "
                f"(${other:.2f}), likely a wrong product")
    _check_median("TCG/CK", ratios, 0.6, 1.4)


def _retail_cents(p) -> bool:
    """Card Kingdom sets retail asks ending in .99 or .49."""
    return round(p * 100) % 100 in (99, 49)


def test_vendor_signature():
    """Each vendor's prices look like that vendor's. Over 90 days every CK deck
    and box ask ended in .99 or .49, against at most 6% of TCG Market deck
    prices and 8% of box prices. Swapped vendor values, or one vendor's prices
    copied into the other, fail this even when every ratio still looks sane."""
    boxes = [r for rows in load("prices.json").get("boxes", {}).values() for r in rows]
    groups = (
        ("cardkingdom deck", list(priced("cardkingdom").values()), True),
        ("tcgplayer deck", list(priced("tcgplayer").values()), False),
        ("cardkingdom box", [r["ck_price"] for r in boxes if num(r.get("ck_price")) and r["ck_price"] > 0], True),
        ("tcgplayer box", [r["price"] for r in boxes if num(r.get("price"))], False),
    )
    for label, prices, retail in groups:
        if len(prices) < 20:
            continue
        share = sum(map(_retail_cents, prices)) / len(prices)
        if retail:
            assert share >= 0.8, f"only {share:.0%} of {label} prices end in .99 or .49: not Card Kingdom asks"
        else:
            assert share <= 0.3, f"{share:.0%} of {label} prices end in .99 or .49: these look like Card Kingdom asks"


def test_price_vs_msrp():
    """Every quote stays within 0.2 to 15 times MSRP per deck (observed 0.47 to
    8.9, Warhammer and Baldur's Gate decks sit near the top), and the median per
    vendor stays in a band around the observed 1.7 to 2.1. This is the only
    check for a deck quoted by a single vendor."""
    decks = decks_by_id()
    for vendor, (lo, hi) in (("tcgplayer", (0.9, 3.5)), ("cardkingdom", (1.0, 4.0))):
        ratios = []
        for did, p in priced(vendor).items():
            msrp = (decks.get(did) or {}).get("msrp")
            if not (num(msrp) and msrp > 0):
                continue
            r = p / msrp
            ratios.append(r)
            assert 0.2 <= r <= 15, f"{vendor}/{did} ${p:.2f} is {r:.2f} times MSRP ${msrp:.2f}"
        _check_median(f"{vendor}/MSRP", ratios, lo, hi)


def test_crack_vs_price():
    """Singles value against the sealed TCG price, for fully priced lists:
    0.2 to 10 per deck (observed 0.54 to 4.2) and a median of 1.0 to 3.5
    (observed 1.77 to 1.93). Catches crack totals scaled by a unit error or
    wiped to cents, and a TCG side scaled against them.

    Card Kingdom singles also cost more than TCG Market: the median CK/TCG
    singles total ran 1.39 to 1.46, so it gets 1.0 to 2.2, which swapped
    vendor totals (about 0.7) fail."""
    tcg = priced("tcgplayer")
    ratios, vendor_ratios = [], []
    for did, c in load("prices.json").get("crack", {}).items():
        if not crack_full(c):
            continue
        vendor_ratios.append(c["cardkingdom"] / c["tcg"])
        if did not in tcg:
            continue
        r = crack_best(c) / tcg[did]
        ratios.append(r)
        assert 0.2 <= r <= 10, f"crack {did}: singles ${crack_best(c):.2f} is {r:.2f} times the deck price ${tcg[did]:.2f}"
    _check_median("singles/TCG", ratios, 1.0, 3.5)
    _check_median("singles CK/TCG", vendor_ratios, 1.0, 2.2)


def test_box_ev_vs_price():
    """Box EV within 0.05 to 10 times the box price per box (observed 0.32 to
    2.7). The median across boxes ran 0.92 to 1.02, so it gets 0.6 to 1.6:
    EV or box prices doubled or halved move it outside."""
    ratios = []
    for set_name, rows in load("prices.json").get("boxes", {}).items():
        for r in rows:
            if num(r.get("ev")) and num(r.get("price")) and r["price"] > 0:
                x = r["ev"] / r["price"]
                ratios.append(x)
                assert 0.05 <= x <= 10, f"box {set_name}/{r['type']}: EV ${r['ev']:.2f} is {x:.2f} times price ${r['price']:.2f}"
    _check_median("box EV/price", ratios, 0.6, 1.6)


def _previous_value(series: list, newest: str, key: str):
    """The value of `key` in the last history entry before the newest day."""
    prev = series[:-1] if series and series[-1]["date"] == newest else series
    prev = [e for e in prev if num(e.get(key)) and e[key] > 0]
    return prev[-1][key] if prev else None


def _check_moves(label: str, moves: dict):
    """{row: today / previous}. Over 90 days the median never left 1.00 to
    1.01 and no deck or box moved more than BIG_MOVE in a day. A median outside
    0.8 to 1.25 is a scaling bug. More than a few rows past BIG_MOVE, or any
    row past HUGE_MOVE, is prices landing on the wrong products. An
    intentional matcher fix does that too, so ALLOW_PRICE_JUMPS lets it pass;
    so does --pending-scrape, since committed data already passed the gate."""
    if len(moves) >= 20:
        _check_median(f"{label} today/previous", list(moves.values()), 0.8, 1.25)
    size = {k: max(r, 1 / r) for k, r in moves.items()}
    big = sorted((k for k in size if size[k] > BIG_MOVE), key=lambda k: -size[k])
    huge = [k for k in big if size[k] > HUGE_MOVE]
    cap = max(MAX_BIG_MOVERS, int(0.03 * len(moves)))
    show = lambda ks: ", ".join(f"{k} {moves[k]:.2f}x" for k in ks[:5])
    problems = []
    if huge:
        problems.append(f"{len(huge)} {label} moved more than {HUGE_MOVE} times since their previous day ({show(huge)})")
    if len(big) > cap:
        problems.append(f"{len(big)} of {len(moves)} {label} moved more than {BIG_MOVE} times since their previous day ({show(big)})")
    if not problems:
        return
    msg = "; ".join(problems)
    if PENDING or jumps_allowed():
        print(f"  note {msg} (allowed: {'ALLOW_PRICE_JUMPS' if jumps_allowed() else '--pending-scrape'})")
        return
    raise AssertionError(msg + ". If a matcher fix moved them on purpose, run the scrape workflow by hand"
                         " with allow_price_jumps (ALLOW_PRICE_JUMPS=1 locally)")


def test_price_moves_vs_previous_day():
    """Deck prices (TCG, and CK while in stock) against each deck's previous
    history entry. See _check_moves for the limits."""
    if not (ROOT / "prices_history.json").exists():
        return
    series = {d: s for d, s in load("prices_history.json").get("decks", {}).items() if s}
    if not series:
        return
    newest = max(s[-1]["date"] for s in series.values())
    ck = load("prices.json")["vendors"].get("cardkingdom", {})
    in_stock = {d: p for d, p in priced("cardkingdom").items()
                if ck[d].get("qty") is None or ck[d]["qty"] > 0}
    for vendor, key, live in (("tcgplayer", "tcg", priced("tcgplayer")), ("cardkingdom", "ck", in_stock)):
        moves = {}
        for did, price in live.items():
            before = _previous_value(series.get(did) or [], newest, key)
            if before:
                moves[did] = price / before
        _check_moves(f"{vendor} decks", moves)


def test_box_moves_vs_previous_day():
    """TCG box prices against each box's previous history entry (boxes in
    prices_history.json, keyed "<set>::<type>"). See _check_moves."""
    if not (ROOT / "prices_history.json").exists():
        return
    series = {k: s for k, s in (load("prices_history.json").get("boxes") or {}).items() if s}
    if not series:
        return
    newest = max(s[-1]["date"] for s in series.values())
    moves = {}
    for set_name, rows in load("prices.json").get("boxes", {}).items():
        for r in rows:
            key = f"{set_name}::{r.get('type')}"
            # Only the same booster kind is the same product.
            same = [e for e in series.get(key) or []
                    if not (r.get("kind") and e.get("kind")) or e["kind"] == r["kind"]]
            before = _previous_value(same, newest, "price")
            if num(r.get("price")) and before:
                moves[key] = r["price"] / before
    _check_moves("boxes", moves)


def _upstream_time(raw: str) -> datetime:
    """A feed's publish time: an ISO timestamp, or a bare date (MTGJSON)."""
    if "T" in raw:
        return parse_utc(raw)
    return datetime.combine(date.fromisoformat(raw), datetime.min.time(), timezone.utc)


def test_upstream_dates():
    """Upstream snapshot dates (optional `upstream` block written by the
    scraper): real timestamps, not after the run, and not stale relative to it
    (a stale MTGJSON only warns, see UPSTREAM_FEEDS)."""
    prices = load("prices.json")
    up = prices.get("upstream")
    if not up:
        return
    run = parse_utc(prices["updated_at"])
    for key, max_age, blocking in UPSTREAM_FEEDS:
        if not up.get(key):
            continue
        when = _upstream_time(up[key])
        assert when <= run + timedelta(hours=1), f"upstream {key} {up[key]} is after the run {prices['updated_at']}"
        if run - when > max_age:
            msg = f"upstream {key} {up[key]} was already stale at the run {prices['updated_at']}"
            assert not blocking, msg
            print(f"::warning::{msg}")


# ---------------------------------------------------------------------------
# Post scrape gates (--gate): CI only, right after scrape_prices.py
# ---------------------------------------------------------------------------

def gate_coverage():
    """Floors near steady state, so a partial loss fails, not just an outage."""
    prices = load("prices.json")
    tcg, ck = len(priced("tcgplayer")), len(priced("cardkingdom"))
    crack = prices.get("crack", {})
    full = sum(1 for c in crack.values() if crack_full(c))
    boxes = prices.get("boxes", {})
    assert (ROOT / "cards_index.json").exists(), "cards_index.json missing"
    index = len(load("cards_index.json").get("cards", {}))
    print(f"  coverage: {tcg} TCG, {ck} CK, {full}/{len(crack)} crack fully priced, "
          f"{len(boxes)} box sets, {index} index cards")
    assert tcg >= MIN_TCG, f"only {tcg} TCG prices (floor {MIN_TCG}): TCGCSV matching likely broke"
    assert ck >= MIN_CK, f"only {ck} Card Kingdom prices (floor {MIN_CK}): CK pricing likely broke"
    assert full >= MIN_CRACK, f"only {full} fully priced crack entries (floor {MIN_CRACK}): singles pricing likely broke"
    assert len(boxes) >= MIN_BOX_SETS, f"only {len(boxes)} box sets (floor {MIN_BOX_SETS}): box pricing likely broke"
    assert index >= MIN_INDEX_CARDS, f"only {index} cards in index (floor {MIN_INDEX_CARDS}): reverse index likely broke"


def gate_fresh_run():
    """prices.json and history were written by this run, not left over."""
    now = datetime.now(timezone.utc)
    for name in ("prices.json", "prices_history.json"):
        if (ROOT / name).exists() and load(name).get("updated_at"):
            at = parse_utc(load(name)["updated_at"])
            assert now - at <= MAX_RUN_AGE and at <= now + timedelta(minutes=5), \
                f"{name} updated_at {at.isoformat()} is not from this run"


def _fetch_upstream() -> dict:
    import requests
    out = {}
    headers = {"User-Agent": "precon-tracker-ci (freshness check)"}
    try:
        r = requests.get(TCGCSV_LAST_UPDATED, headers=headers, timeout=20)
        r.raise_for_status()
        out["tcgcsv"] = r.text.strip().replace("+0000", "+00:00")
    except Exception as e:
        print(f"::warning::could not read {TCGCSV_LAST_UPDATED}: {e}")
    try:
        r = requests.get(MTGJSON_META, headers=headers, timeout=20)
        r.raise_for_status()
        out["mtgjson"] = r.json()["meta"]["date"]
    except Exception as e:
        print(f"::warning::could not read {MTGJSON_META}: {e}")
    return out


def gate_upstream_fresh():
    """A frozen feed still answers 200, so check its own publish date. Uses the
    scraper's recorded `upstream` block, and asks a feed directly for any date
    the block lacks or holds as null. A stale TCGCSV blocks the commit; a
    stale MTGJSON only warns (see UPSTREAM_FEEDS)."""
    up = dict(load("prices.json").get("upstream") or {})
    if any(not up.get(key) for key, _, _ in UPSTREAM_FEEDS):
        live = _fetch_upstream()
        for key, _, _ in UPSTREAM_FEEDS:
            up[key] = up.get(key) or live.get(key)
    now = datetime.now(timezone.utc)
    for key, max_age, blocking in UPSTREAM_FEEDS:
        raw = up.get(key)
        if not raw:
            print(f"::warning::no publish date for {key}, its freshness is unchecked")
            continue
        age = now - _upstream_time(raw)
        print(f"  upstream {key}: {raw} ({age.total_seconds() / 3600:.0f} h old)")
        if age > max_age:
            msg = f"{key} has not published since {raw}"
            assert not blocking, f"{msg}: refusing to commit frozen prices"
            print(f"::warning::{msg}: crack values and box EV may be stale")
        elif age > timedelta(days=2):
            print(f"::warning::{key} last published {raw}")


def gate_vs_previous(prev_dir: Path):
    """Compare with the last committed run (ids mapped through formerIds)."""
    prev_path = prev_dir / "prices.json"
    if not prev_path.exists():
        print("  no previous prices.json, skipping the drift check")
        return
    prev = _load(prev_path)
    cur = load("prices.json")
    fmap = former_id_map()
    remap = lambda m: {fmap.get(k, k): v for k, v in m.items()}
    for vendor in ("tcgplayer", "cardkingdom"):
        p_map = remap(prev["vendors"].get(vendor, {}))
        c_map = cur["vendors"].get(vendor, {})
        shared = p_map.keys() & c_map.keys()
        was = sum(1 for d in shared if num((p_map[d] or {}).get("price")))
        now = sum(1 for d in shared if num((c_map[d] or {}).get("price")))
        assert now >= was * (1 - MAX_COVERAGE_DROP), \
            f"{vendor} priced {now} decks, down from {was} last run: matching likely broke"
    p_crack, c_crack = remap(prev.get("crack", {})), cur.get("crack", {})
    was = sum(1 for c in p_crack.values() if crack_full(c))
    now = sum(1 for c in c_crack.values() if crack_full(c))
    assert now >= was * (1 - MAX_COVERAGE_DROP), f"fully priced crack entries fell from {was} to {now}"
    shared = [d for d in p_crack.keys() & c_crack.keys() if crack_best(p_crack[d]) and crack_best(c_crack[d])]
    if len(shared) >= 20:
        m_was = statistics.median(crack_best(p_crack[d]) for d in shared)
        m_now = statistics.median(crack_best(c_crack[d]) for d in shared)
        assert abs(m_now / m_was - 1) <= MAX_CRACK_MEDIAN_MOVE, \
            f"median singles value moved from ${m_was:.2f} to ${m_now:.2f} in one run"
    rows = lambda p: sum(len(r) for r in p.get("boxes", {}).values())
    assert rows(cur) >= rows(prev) * (1 - MAX_COVERAGE_DROP), \
        f"box rows fell from {rows(prev)} to {rows(cur)}"
    h_prev = prev_dir / "prices_history.json"
    if h_prev.exists() and (ROOT / "prices_history.json").exists():
        # Count only recent entries, which both files keep: trimming days past
        # the window or a one off migration then barely moves the count, while
        # a reset (series cut to their last entry, or emptied) still fails.
        h_was, h_now = _load(h_prev), load("prices_history.json")
        last = [s[-1]["date"] for h in (h_now, h_was) for s in (h.get("decks") or {}).values() if s]
        ref = date.fromisoformat(max(last)) if last else datetime.now(timezone.utc).date()
        cutoff = (ref - timedelta(days=HISTORY_COMPARE_DAYS)).isoformat()
        for part in ("decks", "boxes"):
            count = lambda h: sum(1 for s in (h.get(part) or {}).values() for e in s
                                  if e.get("date", "") >= cutoff)
            was, now = count(h_was), count(h_now)
            assert now >= was * (1 - MAX_HISTORY_DROP), \
                f"{part} history entries since {cutoff} fell from {was} to {now}: history was likely reset"
    print("  drift vs previous run: ok")


GATES = (gate_coverage, gate_fresh_run, gate_upstream_fresh)


def main(argv) -> int:
    global ROOT, PENDING
    args = list(argv)
    prev_dir = None
    if "--root" in args:
        i = args.index("--root")
        ROOT = Path(args[i + 1]).resolve()
        del args[i:i + 2]
    if "--previous" in args:
        i = args.index("--previous")
        prev_dir = Path(args[i + 1]).resolve()
        del args[i:i + 2]
    PENDING = "--pending-scrape" in args
    if "--catalog" in args:
        checks = list(CATALOG_TESTS)
    else:
        checks = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
        if "--gate" in args:
            checks += list(GATES)
            if prev_dir:
                checks.append(lambda: gate_vs_previous(prev_dir))
                checks[-1].__name__ = "gate_vs_previous"
    failures = 0
    for t in checks:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(checks) - failures}/{len(checks)} checks passed")
    return failures


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
