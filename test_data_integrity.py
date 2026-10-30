"""Integrity tests for the data files and the page shell. Run with
`python3 -m pytest` or just `python3 test_data_integrity.py`. Needs only
jsonschema (requests too for the live freshness fallback in --gate).

Modes (plain run = every data and page test, offline, against the committed files):
  --catalog         decks.json checks only (CI runs this before scraping)
  --pending-scrape  every data test, but id sets that differ between decks.json
                    and the generated files are warnings: a catalog push lands
                    before the next scrape regenerates prices for it
  --page            page shell checks only: the CSP hash of the inline script,
                    and the files the manifest and the service worker name
  --gate            every data test plus the post scrape gates: coverage
                    floors, a fresh run, fresh upstream feeds, and (with
                    --previous DIR) no sharp drop against the last committed run
  --root DIR        read the files from DIR instead of this folder

Set ALLOW_PRICE_JUMPS=1 (the scrape workflow's manual input) to let a run
through where many decks or boxes moved sharply since their previous day, as
an intentional matcher fix or a sitewide sale does.

Price bands below were measured on 90 days of committed prices.json (97
snapshots, 2026-07-08 to 2026-10-07) and set with roughly 1.5 to 2 times
headroom past the extremes seen, so real market moves do not flake CI while
unit errors, doubled prices and mismatched products still fail.
"""

import ast
import base64
import hashlib
import json
import math
import os
import re
import statistics
import struct
import sys
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import jsonschema

ROOT = Path(__file__).parent
PENDING = False   # set by --pending-scrape

# Post scrape coverage floors, as shares of the catalog in decks.json, so a
# deliberate catalog edit (removing decks or whole sets, or adding preorders)
# moves the floors with it instead of blocking every scheduled run. Crack
# values and the card index need an MTGJSON decklist, which a preorder lacks
# until about its release, so those two floors count released decks only.
# Commander only sets never get booster boxes, so the box floor counts only
# the tracked sets that have had boxes within the history window.
# Steady state on 2026-10-30: TCG and CK priced 121 of 121 decks, 117 crack
# entries fully priced (all 117 released decks), 34 box sets of the 37
# tracked (the catalog's 35 plus 2 box only sets; 3 Commander only sets have
# none), 4854 index cards (41 a deck).
MIN_TCG_SHARE = 0.84
MIN_CK_SHARE = 0.84
MIN_CRACK_SHARE = 0.90    # of released decks: both vendor totals and >= 90% of cards priced
MIN_BOX_SET_SHARE = 0.75  # of the box sets that have had boxes (see boxed_sets)
MIN_INDEX_CARDS_PER_DECK = 25  # per released deck
# Crack entries allowed to break a content rule (not fully priced, no chase
# cards, not 100 cards), as a count or a share, whichever is more. Every
# entry passed all of them in all 97 snapshots.
MAX_CRACK_PARTIAL = 3
# Drop against the previous committed run that fails the gate. Every
# comparison counts only decks and sets still in decks.json, so removing
# them from the catalog is not a drop.
MAX_COVERAGE_DROP = 0.10
MAX_CRACK_MEDIAN_MOVE = 0.30
MAX_HISTORY_DROP = 0.20
# A run that applies a new one off history migration (a name added to
# prices_history.json "migrations") rewrites old entries on purpose; the
# first run of ck_sold_out_2026_10 removed about a quarter of them. A reset
# (series cut to their last entry, or emptied) still fails at this level.
MAX_HISTORY_DROP_ON_MIGRATION = 0.50
# History is compared over this many recent days, which both the previous and
# the current file must keep (the scraper keeps 90), so trimming old days
# does not read as a reset.
HISTORY_COMPARE_DAYS = 60
# Day over day moves against the previous history entry. Over 90 days of
# history no TCG deck price moved more than 1.16 times and no box more than
# 1.33 times. In stock CK asks moved up to 1.56 times (graveyard-overdrive
# and hail-caesar, restocked at a higher ask after selling out), and on
# 2026-08-25 four CK decks moved 1.25 to 1.36 times at once.
BIG_MOVE = 1.5
MAX_BIG_MOVERS = 3       # or 3% of the rows compared, whichever is more
HUGE_MOVE = 2.5          # any single row past this fails
# Freshness. TCGCSV republishes daily around 20:05 UTC, MTGJSON around 06:15 UTC.
MAX_RUN_AGE = timedelta(hours=6)
# prices.json and prices_history.json come from one scraper run (job timeout
# 25 minutes), so their updated_at stamps are minutes apart.
MAX_HISTORY_LAG = timedelta(minutes=30)
MAX_TCGCSV_AGE = timedelta(days=3)
MAX_MTGJSON_AGE = timedelta(days=7)
# The scraper carries crack values and box rows of a failed phase forward
# for at most this long (scrape_prices.CARRY_MAX_DAYS).
MAX_CARRY_AGE = timedelta(days=3, hours=1)
TCGCSV_LAST_UPDATED = "https://tcgcsv.com/last-updated.txt"
MTGJSON_META = "https://mtgjson.com/api/v5/Meta.json"
# (feed, max age, stale blocks the commit). MTGJSON only feeds crack values and
# box EV, which the scraper carries forward, so a stale MTGJSON only warns and
# never holds back the TCG and CK prices.
UPSTREAM_FEEDS = (("tcgcsv", MAX_TCGCSV_AGE, True), ("mtgjson", MAX_MTGJSON_AGE, False))

# The one host each vendor's links may use (the scraper's VENDOR_HOSTS).
VENDOR_HOSTS = {"tcgplayer": "www.tcgplayer.com", "cardkingdom": "www.cardkingdom.com"}
# Plain URL characters (RFC 3986) without "@": no user info, backslash,
# space or control character that could make a browser read another host.
_URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]!$&'()*+,;=%]+")

# Box rows: the booster kinds each type may hold. Kind 'booster' is a box
# whose name names no kind (a plain 'Booster Display', as Mystery Booster
# Commander Edition sells).
BOX_KINDS = {"play": ("play", "draft", "set", "booster"), "collector": ("collector",),
             "jumpstart": ("jumpstart",)}
NAMED_KINDS = ("play", "draft", "set", "collector", "jumpstart")
# Packs per box by kind, wide enough for every format printed so far
# (Commander Masters collector boxes hold 4).
PACKS_RANGE = {"play": (24, 36), "draft": (24, 36), "set": (24, 36), "booster": (24, 36),
               "collector": (4, 12), "jumpstart": (18, 24)}
# Pack counts confirmed against MTGJSON sealed product contents, pinned so a
# regression in the box EV pack count fails (box EV is pack EV times packs).
PINNED_PACKS = {
    ("Commander Masters", "collector"): 4,
    ("Commander Masters", "play"): 24,
    ("Commander Legends: Battle for Baldur's Gate", "play"): 24,
    ("Dominaria United", "jumpstart"): 18,
    ("March of the Machine", "jumpstart"): 18,
    ("The Brothers' War", "jumpstart"): 18,
    ("Phyrexia: All Will Be One", "jumpstart"): 18,
    ("The Lord of the Rings: Tales of Middle-earth", "jumpstart"): 18,
    ("Marvel Super Heroes", "jumpstart"): 24,
    ("Murders at Karlov Manor", "play"): 36,
    ("Outlaws of Thunder Junction", "play"): 36,
    ("Modern Horizons 3", "play"): 36,
    ("Bloomburrow Commander", "play"): 36,
    ("Duskmourn: House of Horror", "play"): 36,
    ("Aetherdrift Commander", "play"): 30,
    ("Tarkir: Dragonstorm", "play"): 30,
    ("Bloomburrow Commander", "collector"): 12,
}

# TCGplayer statuses that say a deck has no TCGplayer price today (the
# scraper's TCG_NO_PRICE_STATUSES), as opposed to a fetch that failed.
TCG_NO_PRICE_STATUSES = ("tcgcsv-no-price", "tcgcsv-no-product-match",
                         "tcgcsv-no-group-match", "tcgcsv-likely-single")


def _reject_constant(name):
    raise ValueError(f"non standard JSON number {name}")


def _finite_float(text):
    v = float(text)
    if not math.isfinite(v):
        raise ValueError(f"number out of range: {text}")
    return v


def _no_duplicate_keys(pairs):
    out = dict(pairs)
    if len(out) != len(pairs):
        seen, dup = set(), set()
        for k, _ in pairs:
            (dup if k in seen else seen).add(k)
        raise ValueError(f"duplicate keys {sorted(dup)[:3]}")
    return out


def strict_loads(text: str):
    """Python's json accepts NaN, Infinity, 1e999 and repeated keys, but a
    browser either rejects the file outright or silently keeps the last key,
    so all of them fail here."""
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float,
                      object_pairs_hook=_no_duplicate_keys)


@lru_cache(maxsize=None)
def _load(path: Path):
    return strict_loads(path.read_text(encoding="utf-8"))


def load(name: str):
    return _load(ROOT / name)


def on_host(url, host: str) -> bool:
    """An https link whose host is exactly `host`: no other host, subdomain,
    port or user info, and nothing a browser would parse differently."""
    if not isinstance(url, str) or not _URL_CHARS.fullmatch(url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.netloc == host


def jumps_allowed() -> bool:
    return os.environ.get("ALLOW_PRICE_JUMPS", "").strip().lower() in ("1", "true", "yes")


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def decks_by_id() -> dict:
    return {d["id"]: d for d in load("decks.json")}


def catalog_sets() -> set:
    return {d["set"] for d in load("decks.json")}


@lru_cache(maxsize=None)
def _box_only_sets(path: Path) -> frozenset:
    """BOX_ONLY_SETS, read from the scraper's source without importing it
    (an import would need requests and run its module setup)."""
    if not path.is_file():
        return frozenset()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "BOX_ONLY_SETS" for t in node.targets):
            return frozenset(ast.literal_eval(node.value))
    return frozenset()


def box_sets() -> set:
    """Every set the box phase prices (scrape_prices.box_sets): the catalog's
    sets plus BOX_ONLY_SETS, whose boxes are priced with no deck of ours."""
    src = ROOT / "scrape_prices.py"
    return catalog_sets() | _box_only_sets(src if src.is_file() else Path(__file__).parent / "scrape_prices.py")


def boxed_sets() -> set:
    """Tracked sets (box_sets) that have had box rows: a box series in
    prices_history.json, which keeps each live series' last entry for as long
    as the set is tracked, or a row in this run. Commander only sets never
    have boxes, so adding or removing them moves neither side of the box
    floor. With no box history yet (a first run) every tracked set counts."""
    wanted = box_sets()
    hist = load("prices_history.json").get("boxes") if (ROOT / "prices_history.json").exists() else None
    if not hist:
        return wanted
    seen = {box_key_parts(k)[0] for k, s in hist.items() if s} | set(load("prices.json").get("boxes", {}))
    return wanted & seen


def released_count() -> int:
    """Decks out by the day prices.json was written. A preorder has no
    MTGJSON decklist yet, so no crack value and no index cards."""
    run = parse_utc(load("prices.json")["updated_at"]).date()
    return sum(1 for d in load("decks.json") if date.fromisoformat(d["releaseDate"]) <= run)


def history_day() -> str:
    """The day this run's history entries carry: the UTC date of the history
    file's updated_at (update_history dates entries by the UTC day it runs),
    else of prices.json's. Not the newest entry date: entries are written only
    when a price changes, so on a day with no change the newest entry is an
    older day."""
    h = load("prices_history.json") if (ROOT / "prices_history.json").exists() else {}
    return parse_utc(h.get("updated_at") or load("prices.json")["updated_at"]).date().isoformat()


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


_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})")


def feed_time(key: str, raw) -> datetime:
    """A feed's publish time: TCGCSV gives an ISO timestamp with an offset
    ('2026-10-07T20:06:09+0000' or '+00:00'), MTGJSON a bare date. Anything
    else (an HTML error page, a new format) raises ValueError."""
    s = str(raw if raw is not None else "").strip()
    if key == "mtgjson":
        if not _ISO_DAY.fullmatch(s):
            raise ValueError(f"mtgjson date {s[:40]!r} is not YYYY-MM-DD")
        return datetime.combine(date.fromisoformat(s), datetime.min.time(), timezone.utc)
    if not _ISO_STAMP.fullmatch(s):
        raise ValueError(f"{key} time {s[:40]!r} is not an ISO timestamp with an offset")
    s = s.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", s):
        s = f"{s[:-2]}:{s[-2:]}"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


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


def allowed_misses(n: int) -> int:
    return max(MAX_CRACK_PARTIAL, int(0.03 * n))


def box_key_parts(key: str) -> tuple:
    """(set, type, archived kind or None) of a box history key: the live key
    '<set>::<type>' or the archive key '<set>::<type>@<kind>'."""
    set_name, _, rest = key.partition("::")
    box_type, at, kind = rest.partition("@")
    return set_name, box_type, (kind if at else None)


def names_kind(text: str, kind: str) -> bool:
    """A product name or CK slug names this booster kind. Kind 'booster'
    means a plain booster box: 'Booster Display' or 'Booster Box' with no
    kind word before it."""
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    if kind != "booster":
        return f"{kind} booster" in t
    return bool(re.search(r"\bbooster (display|box)\b", t)) and \
        not any(f"{k} booster" in t for k in NAMED_KINDS)


def check_carried(where: str, value, updated_at: str):
    """carried_from (a value carried from an earlier run because this run's
    phase failed) is a real time, not after this run, and not older than the
    scraper's carry limit."""
    if value is None:
        return
    assert isinstance(value, str), f"{where}: carried_from {value!r} is not a timestamp"
    when, run = parse_utc(value), parse_utc(updated_at)
    assert when <= run + timedelta(minutes=5), f"{where}: carried_from {value} is after the run {updated_at}"
    assert run - when <= MAX_CARRY_AGE, f"{where}: carried_from {value} is older than the carry limit"


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


def test_commanders():
    """commander is the face commander; partner is the second card in the
    command zone; altCommanders are the other cards that can lead the deck.
    No card holds two of these roles."""
    for d in load("decks.json"):
        cmd, partner, alts = d.get("commander"), d.get("partner"), d.get("altCommanders") or []
        if partner is not None:
            assert cmd, f"{d['id']}: partner {partner!r} without a commander"
            assert partner != cmd, f"{d['id']}: partner is the commander {cmd!r}"
            assert partner not in alts, f"{d['id']}: partner {partner!r} is also in altCommanders"
        assert cmd not in alts, f"{d['id']}: commander {cmd!r} is also in altCommanders"


CATALOG_TESTS = (test_decks_schema, test_unique_deck_ids, test_id_slug_format,
                 test_decks_dates, test_former_ids, test_commanders)


# ---------------------------------------------------------------------------
# Generated data: shape
# ---------------------------------------------------------------------------

def test_strict_json():
    """Every data file the page loads is standard JSON a browser accepts: no
    NaN, Infinity, out of range numbers or repeated keys."""
    for name in ("decks.json", "prices.json", "prices_history.json", "cards_index.json"):
        if (ROOT / name).exists():
            load(name)


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
                assert on_host(e["url"], host), f"{vendor}/{did} url is not on {host}: {e['url']!r}"
            if not num(e.get("price")) or did not in decks:
                continue
            name = norm(decks[did]["name"])
            text = norm(f"{e.get('snippet') or ''} {e.get('url') or ''}")
            if name in text:
                continue
            # CK's matcher also accepts a CK deck name contained in ours.
            slug = (urlsplit(e.get("url") or "").path.rstrip("/").rsplit("/", 1)[-1])
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
    priced cards has a vendor total (the page drops a zero total silently);
    a carried entry is recent."""
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
        check_carried(f"crack {did}", c.get("carried_from"), prices["updated_at"])


def test_crack_content():
    """Crack entries carry real totals and their chase cards, not just a
    count. All but a few (MAX_CRACK_PARTIAL, or 3%) have both vendor totals
    and 90% or more of their cards priced, list 1 to 4 chase cards, and
    count 100 cards (card copies, a Commander deck); 95% have no card
    missing. Every entry met all of these on 2026-10-08."""
    crack = load("prices.json").get("crack", {})
    if not crack:
        return
    allowed = allowed_misses(len(crack))
    rules = (
        ("are not fully priced (TCG and CK totals, 90% of cards)", lambda c: not crack_full(c)),
        ("have no chase cards (top_cards)", lambda c: crack_full(c) and not (c.get("top_cards") or [])),
        ("do not count 100 cards", lambda c: c.get("card_count") != 100),
    )
    for what, broken in rules:
        bad = sorted(did for did, c in crack.items() if broken(c))
        assert len(bad) <= allowed, f"{len(bad)} of {len(crack)} crack entries {what}, e.g. {bad[:5]}"
    missing = sorted(did for did, c in crack.items() if c.get("missing") != 0)
    assert len(missing) <= 0.05 * len(crack), \
        f"{len(missing)} of {len(crack)} crack entries have unpriced cards, e.g. {missing[:5]}"


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


def tcg_pay(rec: dict) -> float | None:
    """The TCGplayer price a buyer can pay for a record with price and
    price_low: the lowest listing when Market sits below every listing, else
    the recorded price. scrape_prices.tcg_pay_price and the page's
    tcgBuyPrice apply the same rule."""
    p = rec.get("price")
    if not num(p):
        return None
    low = rec.get("price_low")
    return low if num(low) and low > p else p


def payable(vendors: dict, did: str) -> dict:
    """Like buyable, but TCG at tcg_pay: the prices behind individual_basis
    cheapest_payable."""
    out = buyable(vendors, did)
    if "tcgplayer" in out:
        out["tcgplayer"] = tcg_pay(vendors["tcgplayer"][did])
    return out


# Kinds a bundle component may name (scrape_prices.cheapest_payable).
COMPONENT_KINDS = {"Market", "lowest listing", "Mid", "in stock"}


def check_bundle_components(bid: str, b: dict, vendors: dict):
    """`components` names the vendor and price behind each part of
    individual_total. Each must be that vendor's live price for the deck
    while it can be bought; under individual_basis cheapest_payable a TCG
    part is the price you can pay (tcg_pay) and each part is the deck's
    cheapest payable price, under the older cheapest_buyable the cheapest
    buyable one. The total must be their sum."""
    comps = b["components"]
    assert isinstance(comps, list) and [c.get("deck_id") for c in comps] == list(b["deck_ids"]), \
        f"bundle {bid}: components do not list its decks {b['deck_ids']}"
    total = b.get("individual_total")
    basis = b.get("individual_basis")
    for c in comps:
        did, v, p = c["deck_id"], c.get("vendor"), c.get("price")
        if p is None:
            assert total is None, f"bundle {bid}: {did} has no price but individual_total is {total}"
            continue
        assert v in VENDOR_HOSTS, f"bundle {bid}: {did} priced from unknown vendor {v!r}"
        live = (vendors.get(v, {}).get(did) or {})
        want = tcg_pay(live) if (basis == "cheapest_payable" and v == "tcgplayer") else live.get("price")
        assert num(want) and abs(want - p) <= 0.011, \
            f"bundle {bid}: {did} {v} ${p} is not that vendor's live price {want}"
        opts = payable(vendors, did) if basis == "cheapest_payable" else buyable(vendors, did)
        assert v in opts, f"bundle {bid}: {did} counted at {v} ${p}, which has no stock"
        if basis in ("cheapest_payable", "cheapest_buyable"):
            assert p <= min(opts.values()) + 0.011, \
                f"bundle {bid}: {did} counted at {v} ${p}, but {min(opts, key=opts.get)} has it for ${min(opts.values())}"
        if c.get("kind") is not None:
            assert c["kind"] in COMPONENT_KINDS, f"bundle {bid}: {did} has unknown price kind {c['kind']!r}"
    if total is not None:
        parts = sum(c["price"] for c in comps)
        assert abs(parts - total) <= 0.05, \
            f"bundle {bid} individual_total {total} != its components' sum {parts:.2f}"


def test_bundles_shape():
    """bundles: deck_ids reference real decks of the bundle's set and match
    deck_count; the product is a set of Commander decks on tcgplayer.com;
    price positive; savings>0 or null; the individual total is the sum of its
    components (or, for data written before components existed, of the
    member decks' TCG prices)."""
    prices = load("prices.json")
    decks = decks_by_id()
    tcg = priced("tcgplayer")
    for bid, b in prices.get("bundles", {}).items():
        assert num(b.get("price")) and b["price"] > 0, f"bundle {bid} bad price"
        if b.get("url") is not None:
            assert on_host(b["url"], VENDOR_HOSTS["tcgplayer"]), \
                f"bundle {bid} url is not on {VENDOR_HOSTS['tcgplayer']}: {b['url']!r}"
        ids = list(b.get("deck_ids", []))
        ids_must_match(f"bundle {bid}", extra=set(ids) - set(decks))
        assert len(ids) == len(set(ids)), f"bundle {bid} lists a deck twice"
        if b.get("deck_count") is not None:
            assert b["deck_count"] == len(ids), f"bundle {bid} deck_count {b['deck_count']} != {len(ids)} deck_ids"
        if b.get("set"):
            other = sorted(d for d in ids if d in decks and decks[d]["set"] != b["set"])
            assert not other, f"bundle {bid} of {b['set']!r} lists decks of another set: {other}"
        if b.get("name"):
            assert "commander" in norm(b["name"]), f"bundle {bid} {b['name']!r} is not a Commander deck product"
        if b.get("savings") is not None:
            assert b["savings"] > 0, f"bundle {bid} non-positive savings stored: {b['savings']}"
        # Savings arithmetic must be self consistent when a total is recorded.
        # Under cheapest_payable the bundle side is the price you can pay too.
        paid = tcg_pay(b) if b.get("individual_basis") == "cheapest_payable" else b["price"]
        if b.get("savings") is not None and b.get("individual_total") is not None:
            expected = round(b["individual_total"] - paid, 2)
            assert abs(expected - b["savings"]) <= 0.02, \
                f"bundle {bid} savings {b['savings']} != total minus price paid {expected}"
        total = b.get("individual_total")
        if b.get("components") is not None:
            check_bundle_components(bid, b, prices["vendors"])
        elif total is not None and all(d in tcg for d in ids):
            parts = sum(tcg[d] for d in ids)
            assert abs(parts - total) <= 0.05, \
                f"bundle {bid} individual_total {total} != member TCG sum {parts:.2f}"
        if total is not None:
            assert num(total) and total > 0, f"bundle {bid} bad individual_total {total}"
            # Observed 0.72 to 1.70 over 90 days.
            ratio = paid / total
            assert 0.3 <= ratio <= 3.0, f"bundle {bid} price is {ratio:.2f} times its decks"


def test_boxes_shape():
    """boxes: rows of catalog sets or BOX_ONLY_SETS; each has type/price, no
    duplicate type per set; kind fits the type and the product name names
    it; the TCG link is on tcgplayer.com; set_release (the page orders box
    sets by it) is a real date shared by the set's rows; a carried row is
    recent."""
    prices = load("prices.json")
    ids_must_match("boxes (sets)", extra=set(prices.get("boxes", {})) - box_sets())
    latest = parse_utc(prices["updated_at"]).date() + timedelta(days=366)
    for set_name, rows in prices.get("boxes", {}).items():
        seen = set()
        releases = set()
        for r in rows:
            where = f"box {set_name}/{r.get('type')}"
            assert r.get("type") in BOX_KINDS, f"box {set_name} bad type {r.get('type')}"
            assert r["type"] not in seen, f"box {set_name} duplicate type {r['type']}"
            seen.add(r["type"])
            assert num(r.get("price")) and r["price"] > 0, f"{where} bad price"
            if r.get("url") is not None:
                assert on_host(r["url"], VENDOR_HOSTS["tcgplayer"]), \
                    f"{where} url is not on {VENDOR_HOSTS['tcgplayer']}: {r['url']!r}"
            kind = r.get("kind")
            if kind is not None:
                assert kind in BOX_KINDS[r["type"]], f"{where}: kind {kind!r} does not fit the type"
                if r.get("name"):
                    assert names_kind(r["name"], kind), \
                        f"{where}: product {r['name']!r} is not a {kind} booster box"
            assert "set_release" in r, f"{where}: no set_release"
            rel = r["set_release"]
            if rel is not None:
                assert isinstance(rel, str) and _ISO_DAY.fullmatch(rel), f"{where}: set_release {rel!r}"
                assert date(1993, 8, 5) <= date.fromisoformat(rel) <= latest, f"{where}: set_release {rel} out of range"
            releases.add(rel)
            check_carried(where, r.get("carried_from"), prices["updated_at"])
        assert len(releases) <= 1, f"box {set_name}: rows disagree on set_release {sorted(map(str, releases))}"


def test_history_shape():
    """prices_history.json: dated series in date order, prices positive, best
    on every priced deck entry ({date} alone marks a day with no buyable
    price), box keys '<set>::<type>' or the archive '<set>::<type>@<kind>'
    holding one booster kind each, and only the window plus one carry in
    entry per live series."""
    p = ROOT / "prices_history.json"
    if not p.exists():
        return  # built up over time by the scraper
    history = load("prices_history.json")
    assert isinstance(history.get("decks"), dict), "history has no decks map"
    migrations = history.get("migrations", [])
    assert isinstance(migrations, list) and all(isinstance(m, str) and m for m in migrations) \
        and len(migrations) == len(set(migrations)), f"bad migrations list {migrations!r}"
    window = history.get("window_days")
    cutoff = None
    if window is not None:
        assert isinstance(window, int) and not isinstance(window, bool) and window > 0, f"bad window_days {window!r}"
        last = [s[-1]["date"] for s in history["decks"].values() if s]
        ref = (parse_utc(history["updated_at"]).date() if history.get("updated_at")
               else date.fromisoformat(max(last)) if last else None)
        # One day of slack for a run that crosses midnight UTC.
        cutoff = (ref - timedelta(days=window + 1)).isoformat() if ref else None

    def dated(where, series):
        assert isinstance(series, list), f"{where}: not a list"
        prev_date = None
        for entry in series:
            assert isinstance(entry, dict) and isinstance(entry.get("date"), str) \
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["date"]), f"{where}: bad entry {entry!r}"
            date.fromisoformat(entry["date"])
            assert prev_date is None or entry["date"] > prev_date, \
                f"{where}: dates not strictly increasing ({prev_date} -> {entry['date']})"
            prev_date = entry["date"]

    def window_ok(where, series, carry_in):
        if cutoff is None:
            return
        old = sum(1 for e in series if e["date"] < cutoff)
        assert old <= (1 if carry_in else 0), \
            f"{where}: {old} entries older than the {window} day window"

    deck_ids = set(decks_by_id())
    ids_must_match("history", extra=set(history["decks"]) - deck_ids)
    for did, series in history["decks"].items():
        dated(f"history {did}", series)
        window_ok(f"history {did}", series, carry_in=True)
        for entry in series:
            for k, v in entry.items():
                if k in ("tcg", "ck", "best"):
                    assert num(v) and v > 0, f"history {did} {entry['date']}: bad {k} {v!r}"
                elif k != "date":
                    # Room for optional per day extras (stock counts and the
                    # like); vendor prices must use the keys above.
                    assert v is None or isinstance(v, bool) or num(v), \
                        f"history {did} {entry['date']}: unexpected value {k}={v!r}"
            if "tcg" in entry or "ck" in entry:
                assert "best" in entry, f"history {did} {entry['date']}: priced entry without best"
    boxes = history.get("boxes") or {}
    stale = set()
    for key, series in boxes.items():
        set_name, box_type, archived = box_key_parts(key)
        assert set_name and box_type in BOX_KINDS, f"bad box history key: {key}"
        if set_name not in box_sets():
            stale.add(set_name)
        dated(f"box history {key}", series)
        window_ok(f"box history {key}", series, carry_in=archived is None)
        kinds = set()
        for entry in series:
            assert num(entry.get("price")) and entry["price"] > 0, f"box history {key} {entry['date']}: bad price"
            if "kind" in entry:
                assert entry["kind"] in BOX_KINDS[box_type], f"box history {key}: kind {entry['kind']!r}"
                kinds.add(entry["kind"])
        # The scraper keeps one product per series, so a chart never joins
        # two booster kinds; other kinds wait under '@<kind>'.
        assert len(kinds) <= 1, f"box history {key} mixes kinds {sorted(kinds)}"
        if archived is not None:
            assert archived in BOX_KINDS[box_type] and kinds <= {archived}, \
                f"box history {key} holds kinds {sorted(kinds)}"
            live = boxes.get(f"{set_name}::{box_type}") or []
            assert not (live and live[-1].get("kind") == archived), \
                f"box history {key} archives the kind its live series shows"
    ids_must_match("box history (sets)", extra=stale)


def test_history_matches_prices():
    """History was written by the run that wrote prices.json, and the
    history's price in force is the live one: a deck's last entry holds the
    live TCG Market price, and the live CK price while in stock (no CK when
    sold out); a box's live series ends at the live price and kind. A value
    written only into history would otherwise be committed, and then block
    every later run through the day over day check.

    Freshness compares the two files' updated_at (the scraper writes both in
    one run, minutes apart), not the newest entry date: an entry is added
    only when a price changes, so a run where no deck price moved (TCGCSV
    serving the same snapshot again) leaves the newest entry days old."""
    if not (ROOT / "prices_history.json").exists():
        return
    prices, history = load("prices.json"), load("prices_history.json")
    series = {d: s for d, s in history.get("decks", {}).items() if s}
    run_at = parse_utc(prices["updated_at"])
    if history.get("updated_at"):
        h_at = parse_utc(history["updated_at"])
        assert abs(h_at - run_at) <= MAX_HISTORY_LAG, \
            f"prices_history.json updated_at {history['updated_at']} is not from the run that wrote " \
            f"prices.json ({prices['updated_at']})"
    newest = max((s[-1]["date"] for s in series.values()), default=None)
    assert newest is None or date.fromisoformat(newest) <= (run_at + MAX_HISTORY_LAG).date(), \
        f"history has entries dated {newest}, after prices.json updated_at {prices['updated_at']}"
    tcg = prices["vendors"].get("tcgplayer", {})
    ck = prices["vendors"].get("cardkingdom", {})
    wrong = []
    for did, s in series.items():
        last = s[-1]
        t = tcg.get(did) or {}
        if num(t.get("price")) and (t.get("price_source") or "Market") == "Market":
            if not (num(last.get("tcg")) and abs(last["tcg"] - t["price"]) < 0.011):
                wrong.append(f"{did} tcg {last.get('tcg')} != live {t['price']}")
        elif t.get("price") is None and t.get("status") in TCG_NO_PRICE_STATUSES and "tcg" in last:
            wrong.append(f"{did} tcg {last['tcg']} still in force with no TCG price")
        k = ck.get(did) or {}
        if num(k.get("price")) and (k.get("qty") is None or k["qty"] > 0):
            if not (num(last.get("ck")) and abs(last["ck"] - k["price"]) < 0.011):
                wrong.append(f"{did} ck {last.get('ck')} != live {k['price']}")
        elif (num(k.get("price")) or k.get("status") in ("no-match", "out-of-stock")) and "ck" in last:
            wrong.append(f"{did} ck {last['ck']} in force while Card Kingdom has none in stock")
    boxes = history.get("boxes") or {}
    for set_name, rows in prices.get("boxes", {}).items():
        for r in rows:
            # A carried row was not observed this run, and a Low or Mid
            # fallback is not recorded: the box series is TCGplayer Market.
            if r.get("carried_from") or (r.get("price_source") or "Market") != "Market" or not num(r.get("price")):
                continue
            key = f"{set_name}::{r.get('type')}"
            last = (boxes.get(key) or [{}])[-1]
            if not (num(last.get("price")) and abs(last["price"] - r["price"]) < 0.011):
                wrong.append(f"box {key} {last.get('price')} != live {r['price']}")
            elif r.get("kind") and last.get("kind") not in (None, r["kind"]):
                wrong.append(f"box {key} series is {last.get('kind')}, the row is {r['kind']}")
    assert not wrong, f"{len(wrong)} history values differ from prices.json: " + "; ".join(wrong[:5])


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
    """box EV (F3): when present, a positive estimate under the published
    box_ev_basis rules, backed by real card prices for at least min_coverage
    of its value, over a real pack count (pinned where MTGJSON confirmed it)."""
    prices = load("prices.json")
    rows = [(s, r) for s, rs in prices.get("boxes", {}).items() for r in rs]
    with_ev = [(s, r) for s, r in rows if r.get("ev") is not None]
    if not with_ev:
        return
    basis = prices.get("box_ev_basis")
    assert isinstance(basis, dict), "box EV values without box_ev_basis (the rule the page states)"
    min_cov = basis.get("min_coverage")
    assert num(min_cov) and 0 < min_cov <= 1, f"box_ev_basis min_coverage {min_cov!r}"
    assert num(basis.get("bulk_threshold")) and basis["bulk_threshold"] >= 0, \
        f"box_ev_basis bulk_threshold {basis.get('bulk_threshold')!r}"
    for set_name, r in with_ev:
        where = f"box {set_name}/{r.get('type')}"
        assert num(r["ev"]) and r["ev"] > 0, f"{where} bad ev {r['ev']}"
        cov = r.get("ev_coverage")
        if "ev_coverage" in r or "packs" in r or "kind" in r:
            # Rows written by the box EV rules of 2026-10 carry all three.
            assert num(cov) and min_cov - 1e-9 <= cov <= 1, \
                f"{where}: EV published at coverage {cov!r}, under the {min_cov} minimum"
            packs = r.get("packs")
            assert isinstance(packs, int) and not isinstance(packs, bool), f"{where}: EV without a pack count"
            kind = r.get("kind") or r["type"]
            lo, hi = PACKS_RANGE.get(kind, (1, 36))
            assert lo <= packs <= hi, f"{where}: {packs} packs is outside {lo} to {hi} for a {kind} box"
    for (set_name, box_type), packs in PINNED_PACKS.items():
        for r in prices.get("boxes", {}).get(set_name, []):
            if r.get("type") == box_type and r.get("packs") is not None:
                assert r["packs"] == packs, \
                    f"box {set_name}/{box_type}: {r['packs']} packs, MTGJSON says {packs}"


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
        # Status is informational: a non empty string rather than a brittle
        # whitelist that drifts from the scraper's actual values.
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
    www.cardkingdom.com and, for a product page, the row's booster kind; in
    the same ballpark as TCG per box and across boxes."""
    prices = load("prices.json")
    ratios = []
    for set_name, rows in prices.get("boxes", {}).items():
        for r in rows:
            where = f"box {set_name}/{r.get('type')}"
            if r.get("ck_price") is not None:
                assert num(r["ck_price"]) and r["ck_price"] > 0, f"{where} bad ck_price {r['ck_price']}"
            if r.get("ck_qty") is not None:
                assert isinstance(r["ck_qty"], int) and r["ck_qty"] >= 0, f"{where} bad ck_qty"
            if r.get("ck_url") is not None:
                assert on_host(r["ck_url"], VENDOR_HOSTS["cardkingdom"]), \
                    f"{where} ck_url is not on {VENDOR_HOSTS['cardkingdom']}: {r['ck_url']!r}"
                path = urlsplit(r["ck_url"]).path
                if r.get("kind") and path.startswith("/mtg-sealed/"):
                    assert names_kind(path.rsplit("/", 1)[-1], r["kind"]), \
                        f"{where}: CK product {path} is not the row's {r['kind']} booster box"
            if r.get("ck_buy") is not None:
                assert num(r["ck_buy"]) and r["ck_buy"] >= 0, f"{where} bad ck_buy {r['ck_buy']}"
            # Sanity: CK retail and TCG market should be in the same ballpark
            # (a mismatched product would be wildly off). Observed 0.78 to 2.05.
            if r.get("ck_price") is not None and r.get("price"):
                ratio = r["ck_price"] / r["price"]
                ratios.append(ratio)
                assert 0.2 <= ratio <= 5.0, f"{where} CK/TCG ratio {ratio:.2f} out of range"
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


# Markup and invisible characters no product, card or set name contains:
# angle brackets, C0 and C1 controls (tab and newline included), line and
# paragraph separators, and the bidi overrides that can reorder shown text.
_UNSAFE_TEXT = re.compile(r"[<>\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")


def test_no_markup_in_generated_text():
    """Every string (and key) in the generated files is plain text. The page
    escapes them all, but this gate is the one place a compromised or broken
    upstream response (a TCGplayer snippet, a box name, a top card or index
    name) can be refused before it is published. None of 549 committed
    snapshots held one."""
    def walk(x, where):
        if isinstance(x, dict):
            for k, v in x.items():
                yield from walk(k, f"{where} key")
                yield from walk(v, f"{where}.{k}")
        elif isinstance(x, list):
            for i, v in enumerate(x):
                yield from walk(v, f"{where}[{i}]")
        elif isinstance(x, str) and _UNSAFE_TEXT.search(x):
            yield f"{where}: {x[:60]!r}"
    for name in ("prices.json", "cards_index.json", "prices_history.json"):
        if (ROOT / name).exists():
            bad = list(walk(load(name), name))
            assert not bad, f"{len(bad)} strings with markup or control characters, e.g. {bad[:3]}"


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
    """Box EV within 0.05 to 10 times the box price per box. Since 2026-10 EV
    counts only cards worth $1 or more over the real pack count, so it sits
    below the price for most boxes: 0.29 to 1.6 on 2026-10-08, median 0.75.
    The median band (0.3 to 1.6) still fails EV or box prices doubled or
    cut to a third."""
    ratios = []
    for set_name, rows in load("prices.json").get("boxes", {}).items():
        for r in rows:
            if num(r.get("ev")) and num(r.get("price")) and r["price"] > 0:
                x = r["ev"] / r["price"]
                ratios.append(x)
                assert 0.05 <= x <= 10, f"box {set_name}/{r['type']}: EV ${r['ev']:.2f} is {x:.2f} times price ${r['price']:.2f}"
    _check_median("box EV/price", ratios, 0.3, 1.6)


def _previous_entry(series: list, today: str, key: str):
    """The last history entry before `today` (this run's day) with a price
    under `key`, or None."""
    prev = [e for e in series if e["date"] < today and num(e.get(key)) and e[key] > 0]
    return prev[-1] if prev else None


def _check_moves(label: str, moves: dict, presale: dict | None = None):
    """{row: today / previous}. Over 90 days the median never left 1.00 to
    1.01 (see BIG_MOVE for single rows). A median outside 0.8 to 1.25 is a
    scaling bug. More than a few rows past BIG_MOVE, or any row past
    HUGE_MOVE, is prices landing on the wrong products. An intentional
    matcher fix or a sitewide sale does that too, so ALLOW_PRICE_JUMPS lets
    it pass; so does --pending-scrape, since committed data already passed
    the gate.

    `presale` ({row: release date}) holds rows whose previous entry predates
    their release: a preorder price. A whole Commander set (4 or 5 decks)
    can reprice on release day, so those rows count toward the median and
    HUGE_MOVE but not the BIG_MOVE cap, and a big move is only noted.
    Otherwise one release would fail every later run until a manual
    allow_price_jumps dispatch, since a failed run commits nothing."""
    presale = presale or {}
    problems = []
    if len(moves) >= 20:
        m = statistics.median(moves.values())
        if not 0.8 <= m <= 1.25:
            problems.append(f"median {label} today/previous is {m:.2f}, outside 0.8 to 1.25 (a unit or matching error)")
    size = {k: max(r, 1 / r) for k, r in moves.items()}
    over = sorted((k for k in size if size[k] > BIG_MOVE), key=lambda k: -size[k])
    huge = [k for k in over if size[k] > HUGE_MOVE]
    big = [k for k in over if k not in presale]
    released = [k for k in over if k in presale]
    cap = max(MAX_BIG_MOVERS, int(0.03 * len(moves)))
    show = lambda ks: ", ".join(f"{k} {moves[k]:.2f}x" for k in ks[:5])
    if released:
        print(f"  note {len(released)} {label} moved more than {BIG_MOVE} times from a preorder price at "
              f"release ({show(released)}); not counted toward the cap of {cap}")
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
    raise AssertionError(msg + ". If a matcher fix or a sitewide sale moved them, run the scrape workflow by hand"
                         " with allow_price_jumps (ALLOW_PRICE_JUMPS=1 locally)")


def test_price_moves_vs_previous_day():
    """Deck prices (TCG, and CK while in stock) against each deck's last
    history entry before this run's day. See _check_moves for the limits; a
    previous entry dated before the deck's releaseDate is a preorder price."""
    if not (ROOT / "prices_history.json").exists():
        return
    series = {d: s for d, s in load("prices_history.json").get("decks", {}).items() if s}
    if not series:
        return
    today = history_day()
    decks = decks_by_id()
    ck = load("prices.json")["vendors"].get("cardkingdom", {})
    in_stock = {d: p for d, p in priced("cardkingdom").items()
                if ck[d].get("qty") is None or ck[d]["qty"] > 0}
    for vendor, key, live in (("tcgplayer", "tcg", priced("tcgplayer")), ("cardkingdom", "ck", in_stock)):
        moves, presale = {}, {}
        for did, price in live.items():
            before = _previous_entry(series.get(did) or [], today, key)
            if not before:
                continue
            moves[did] = price / before[key]
            rel = (decks.get(did) or {}).get("releaseDate")
            if rel and before["date"] < rel <= today:
                presale[did] = rel
        _check_moves(f"{vendor} decks", moves, presale)


def test_box_moves_vs_previous_day():
    """TCG box prices against each box's last history entry before this
    run's day (boxes in prices_history.json, keyed "<set>::<type>"). See
    _check_moves."""
    if not (ROOT / "prices_history.json").exists():
        return
    series = {k: s for k, s in (load("prices_history.json").get("boxes") or {}).items() if s}
    if not series:
        return
    today = history_day()
    moves = {}
    for set_name, rows in load("prices.json").get("boxes", {}).items():
        for r in rows:
            key = f"{set_name}::{r.get('type')}"
            # Only the same booster kind is the same product.
            same = [e for e in series.get(key) or []
                    if not (r.get("kind") and e.get("kind")) or e["kind"] == r["kind"]]
            before = _previous_entry(same, today, "price")
            if num(r.get("price")) and before:
                moves[key] = r["price"] / before["price"]
    _check_moves("boxes", moves)


def test_upstream_dates():
    """Upstream snapshot dates (the `upstream` block the scraper writes): a
    real TCGCSV timestamp and MTGJSON date, not after the run, and not stale
    relative to it (a stale MTGJSON only warns, see UPSTREAM_FEEDS)."""
    prices = load("prices.json")
    up = prices.get("upstream")
    if not up:
        return
    run = parse_utc(prices["updated_at"])
    for key, max_age, blocking in UPSTREAM_FEEDS:
        if not up.get(key):
            continue
        when = feed_time(key, up[key])
        assert when <= run + timedelta(hours=1), f"upstream {key} {up[key]} is after the run {prices['updated_at']}"
        if run - when > max_age:
            msg = f"upstream {key} {up[key]} was already stale at the run {prices['updated_at']}"
            assert not blocking, msg
            print(f"::warning::{msg}")


# ---------------------------------------------------------------------------
# Page shell (--page): index.html, manifest.webmanifest, sw.js
# ---------------------------------------------------------------------------

class _PageParser(HTMLParser):
    """Inline scripts (raw text, as the browser hashes it), meta tags and
    link tags of a page."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts, self.metas, self.links, self._cur = [], [], [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self._cur = (a, [])
        elif tag == "meta":
            self.metas.append(a)
        elif tag == "link":
            self.links.append(a)

    def handle_endtag(self, tag):
        if tag == "script" and self._cur is not None:
            self.scripts.append((self._cur[0], "".join(self._cur[1])))
            self._cur = None

    def handle_data(self, data):
        if self._cur is not None:
            self._cur[1].append(data)


def _page() -> _PageParser:
    p = _PageParser()
    p.feed((ROOT / "index.html").read_text(encoding="utf-8"))
    p.close()
    return p


def _local_file(ref: str):
    """The repo file a same origin relative reference names, or None for a
    reference to another origin."""
    if re.match(r"^[a-z][a-z0-9+.-]*:|^//", ref, re.I):
        return None
    path = urlsplit(ref).path
    return ROOT / (path[2:] if path.startswith("./") else path.lstrip("/"))


def test_page_csp_script_hash():
    """index.html's Content-Security-Policy allows its inline script by hash
    only, so any edit to the script without a new hash blanks the page in
    every browser. Each executable inline script's sha256 (base64, over the
    text between the tags with newlines normalized as the HTML parser does)
    must be in script-src, and script-src must list no other hash."""
    page = _page()
    csp = [m.get("content") or "" for m in page.metas
           if (m.get("http-equiv") or "").lower() == "content-security-policy"]
    assert len(csp) == 1, f"index.html has {len(csp)} Content-Security-Policy meta tags, expected 1"
    directives = {}
    for part in csp[0].split(";"):
        words = part.split()
        if words:
            directives.setdefault(words[0].lower(), words[1:])
    sources = directives.get("script-src", directives.get("default-src"))
    assert sources is not None, "the CSP has no script-src"
    inline = [body for attrs, body in page.scripts if "src" not in attrs
              and (attrs.get("type") or "text/javascript").lower()
              in ("text/javascript", "application/javascript", "module")]
    assert inline, "index.html has no inline script"
    want = []
    for body in inline:
        text = body.replace("\r\n", "\n").replace("\r", "\n")
        want.append("'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'")
    missing = [w for w in want if w not in sources]
    assert not missing, f"script-src lacks {missing}: update the CSP hash in index.html after editing the script"
    stale = [s for s in sources if s.lower().startswith("'sha") and s not in want]
    assert not stale, f"script-src lists hashes of no inline script: {stale}"


def test_page_offline_assets():
    """Every file the page shell names exists: manifest icons (PNG sizes as
    declared), the service worker's precache list (shell, ASSETS, startup
    JSON), and the page's own icon, manifest and preload links. A missing
    file fails the worker's install (the shell) or quietly drops out of the
    offline cache (the rest)."""
    missing = []
    manifest = strict_loads((ROOT / "manifest.webmanifest").read_text(encoding="utf-8"))
    icons = manifest.get("icons") or []
    assert icons, "manifest.webmanifest lists no icons"
    for icon in icons:
        f = _local_file(icon.get("src") or "")
        if f is None or not f.is_file():
            missing.append(f"manifest icon {icon.get('src')}")
            continue
        size = re.fullmatch(r"(\d+)x(\d+)", icon.get("sizes") or "")
        if f.suffix.lower() == ".png" and size:
            head = f.read_bytes()[:24]
            assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{icon['src']} is not a PNG"
            w, h = struct.unpack(">II", head[16:24])
            assert (w, h) == (int(size[1]), int(size[2])), f"{icon['src']} is {w}x{h}, the manifest says {icon['sizes']}"
    sw = (ROOT / "sw.js").read_text(encoding="utf-8")
    listed = []
    for name in ("SHELL_KEY",):
        m = re.search(rf"\bconst\s+{name}\s*=\s*([\"'])(.*?)\1", sw)
        assert m, f"sw.js has no {name}"
        listed.append(m[2])
    for name in ("ASSETS", "DATA_PRECACHE"):
        m = re.search(rf"\bconst\s+{name}\s*=\s*\[(.*?)\]", sw, re.S)
        assert m, f"sw.js has no {name} list"
        listed += [s for _, s in re.findall(r"([\"'])(.*?)\1", m[1])]
    for ref in listed:
        f = _local_file(ref)
        if f is None or not f.is_file():
            missing.append(f"sw.js precache {ref}")
    for link in _page().links:
        rel = (link.get("rel") or "").lower().split()
        if {"icon", "apple-touch-icon", "manifest", "preload"} & set(rel) and link.get("href"):
            f = _local_file(link["href"])
            if f is not None and not f.is_file():
                missing.append(f"index.html link {link['href']}")
    assert not missing, f"missing files: {missing}"


# ---------------------------------------------------------------------------
# Post scrape gates (--gate): CI only, right after scrape_prices.py
# ---------------------------------------------------------------------------

def gate_coverage():
    """Floors as shares of the catalog, so a partial loss fails, not just an
    outage, while a catalog edit moves them along with it. Crack and index
    floors count released decks, since a preorder has no decklist yet; the
    box floor counts sets that have had boxes (boxed_sets)."""
    prices = load("prices.json")
    n, released = len(load("decks.json")), released_count()
    wanted_sets = boxed_sets()
    tcg, ck = len(priced("tcgplayer")), len(priced("cardkingdom"))
    crack = prices.get("crack", {})
    full = sum(1 for c in crack.values() if crack_full(c))
    boxes = set(prices.get("boxes", {})) & box_sets()
    assert (ROOT / "cards_index.json").exists(), "cards_index.json missing"
    index = len(load("cards_index.json").get("cards", {}))
    print(f"  coverage: {tcg} TCG, {ck} CK, {full}/{len(crack)} crack fully priced, "
          f"{len(boxes)}/{len(wanted_sets)} box sets ({len(box_sets())} tracked), {index} index cards "
          f"({n} decks, {released} released, in {len(catalog_sets())} sets)")
    floors = (
        (tcg, math.floor(MIN_TCG_SHARE * n), "TCG prices", f"{n} decks", "TCGCSV matching likely broke"),
        (ck, math.floor(MIN_CK_SHARE * n), "Card Kingdom prices", f"{n} decks", "CK pricing likely broke"),
        (full, math.floor(MIN_CRACK_SHARE * released), "fully priced crack entries",
         f"{released} released decks", "singles pricing likely broke"),
        (len(boxes), math.floor(MIN_BOX_SET_SHARE * len(wanted_sets)), "box sets",
         f"{len(wanted_sets)} sets that have had boxes", "box pricing likely broke"),
        (index, MIN_INDEX_CARDS_PER_DECK * released, "cards in index",
         f"{released} released decks", "reverse index likely broke"),
    )
    for got, floor, what, base, cause in floors:
        assert got >= floor, f"only {got} {what} (floor {floor} for {base}): {cause}"


def gate_fresh_run():
    """prices.json and history were written by this run, not left over."""
    now = datetime.now(timezone.utc)
    for name in ("prices.json", "prices_history.json"):
        if (ROOT / name).exists() and load(name).get("updated_at"):
            at = parse_utc(load(name)["updated_at"])
            assert now - at <= MAX_RUN_AGE and at <= now + timedelta(minutes=5), \
                f"{name} updated_at {at.isoformat()} is not from this run"


def _fetch_upstream() -> dict:
    """{feed: raw publish date} read live. A feed that cannot be reached, or
    answers with something that is not a date (an error page, a new format),
    is left out with a warning, never an error."""
    import requests
    out = {}
    headers = {"User-Agent": "precon-tracker-ci (freshness check)"}
    reads = (("tcgcsv", TCGCSV_LAST_UPDATED, lambda r: r.text.strip()),
             ("mtgjson", MTGJSON_META, lambda r: r.json()["meta"]["date"]))
    for key, url, read in reads:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            raw = read(r)
            feed_time(key, raw)
            out[key] = raw
        except Exception as e:
            print(f"::warning::could not read a publish date from {url}: {type(e).__name__}: {str(e)[:120]}")
    return out


def gate_upstream_fresh():
    """A frozen feed still answers 200, so check its own publish date. Uses the
    scraper's recorded `upstream` block, and asks a feed directly for any date
    the block lacks, holds as null or holds in a form that does not parse
    (test_upstream_dates fails that last case on its own). A stale TCGCSV
    blocks the commit; a stale MTGJSON only warns (see UPSTREAM_FEEDS)."""
    up = {}
    for key, value in (load("prices.json").get("upstream") or {}).items():
        try:
            feed_time(key, value)
            up[key] = value
        except ValueError:
            pass
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
        age = now - feed_time(key, raw)
        print(f"  upstream {key}: {raw} ({age.total_seconds() / 3600:.0f} h old)")
        if age > max_age:
            msg = f"{key} has not published since {raw}"
            assert not blocking, f"{msg}: refusing to commit frozen prices"
            print(f"::warning::{msg}: crack values and box EV may be stale")
        elif age > timedelta(days=2):
            print(f"::warning::{key} last published {raw}")


def gate_vs_previous(prev_dir: Path):
    """Compare with the last committed run. Previous ids are mapped through
    formerIds, and every count covers only decks and sets that are in
    decks.json now, so a deliberate catalog edit never reads as a drop."""
    prev_path = prev_dir / "prices.json"
    if not prev_path.exists():
        print("  no previous prices.json, skipping the drift check")
        return
    prev = _load(prev_path)
    cur = load("prices.json")
    fmap = former_id_map()
    current, sets = set(decks_by_id()), box_sets()
    remap = lambda m: {fmap.get(k, k): v for k, v in m.items() if fmap.get(k, k) in current}
    problems = []

    def no_drop(what, was, now, limit=MAX_COVERAGE_DROP, cause="matching likely broke"):
        if now < was * (1 - limit):
            problems.append(f"{what} fell from {was} to {now}: {cause}")

    for vendor in ("tcgplayer", "cardkingdom"):
        p_map = remap(prev["vendors"].get(vendor, {}))
        c_map = cur["vendors"].get(vendor, {})
        shared = p_map.keys() & c_map.keys()
        no_drop(f"{vendor} priced decks",
                sum(1 for d in shared if num((p_map[d] or {}).get("price"))),
                sum(1 for d in shared if num((c_map[d] or {}).get("price"))))
    p_crack, c_crack = remap(prev.get("crack", {})), cur.get("crack", {})
    no_drop("fully priced crack entries",
            sum(1 for c in p_crack.values() if crack_full(c)),
            sum(1 for d in p_crack if crack_full(c_crack.get(d) or {})), cause="singles pricing likely broke")
    shared = [d for d in p_crack.keys() & c_crack.keys() if crack_best(p_crack[d]) and crack_best(c_crack[d])]
    if len(shared) >= 20:
        m_was = statistics.median(crack_best(p_crack[d]) for d in shared)
        m_now = statistics.median(crack_best(c_crack[d]) for d in shared)
        if abs(m_now / m_was - 1) > MAX_CRACK_MEDIAN_MOVE:
            problems.append(f"median singles value moved from ${m_was:.2f} to ${m_now:.2f} in one run")
    rows = lambda p: sum(len(r) for s, r in p.get("boxes", {}).items() if s in sets)
    no_drop("box rows of tracked sets", rows(prev), rows(cur), cause="box pricing likely broke")
    i_prev = prev_dir / "cards_index.json"
    if i_prev.exists() and (ROOT / "cards_index.json").exists():
        listed = lambda idx, ids: sum(1 for e in (idx.get("cards") or {}).values()
                                      if any(fmap.get(d, d) in ids for d in e.get("decks") or []))
        old_ids = {fmap.get(d, d) for e in (_load(i_prev).get("cards") or {}).values()
                   for d in e.get("decks") or []} & current
        no_drop("index cards of catalog decks", listed(_load(i_prev), old_ids),
                listed(load("cards_index.json"), old_ids), cause="reverse index likely broke")
    h_prev = prev_dir / "prices_history.json"
    if h_prev.exists() and (ROOT / "prices_history.json").exists():
        # Count only recent entries, which both files keep: trimming days past
        # the window then barely moves the count, while a reset (series cut to
        # their last entry, or emptied) still fails.
        h_was, h_now = _load(h_prev), load("prices_history.json")
        last = [s[-1]["date"] for h in (h_now, h_was) for s in (h.get("decks") or {}).values() if s]
        ref = date.fromisoformat(max(last)) if last else datetime.now(timezone.utc).date()
        cutoff = (ref - timedelta(days=HISTORY_COMPARE_DAYS)).isoformat()
        new_migrations = sorted(set(h_now.get("migrations") or []) - set(h_was.get("migrations") or []))
        limit = MAX_HISTORY_DROP_ON_MIGRATION if new_migrations else MAX_HISTORY_DROP
        keep = {"decks": lambda k: fmap.get(k, k) in current,
                "boxes": lambda k: box_key_parts(k)[0] in sets}
        for part in ("decks", "boxes"):
            count = lambda h: sum(1 for k, s in (h.get(part) or {}).items() if keep[part](k)
                                  for e in s if e.get("date", "") >= cutoff)
            no_drop(f"{part} history entries since {cutoff}", count(h_was), count(h_now), limit=limit,
                    cause="history was likely reset" + (f" (this run applied {new_migrations})" if new_migrations else ""))
        if new_migrations:
            print(f"  history migrations applied this run: {new_migrations}")
    assert not problems, "; ".join(problems)
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
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    page = [t for t in tests if t.__name__.startswith("test_page_")]
    data = [t for t in tests if t not in page]
    if "--catalog" in args:
        checks = list(CATALOG_TESTS)
    elif "--page" in args:
        checks = page
    elif "--gate" in args or PENDING:
        # The scrape gate and the pending run check data only; the page
        # shell has its own step in validate.yml.
        checks = data
        if "--gate" in args:
            checks += list(GATES)
            if prev_dir:
                checks.append(lambda: gate_vs_previous(prev_dir))
                checks[-1].__name__ = "gate_vs_previous"
    else:
        checks = data + page
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
