"""
Scrape precon prices.

v1.5 changes:
  - Smarter group matching: token-set instead of substring. "Bloomburrow
    Commander" now matches a TCGCSV group named "Commander: Bloomburrow".
  - Distinctive-token fallback: if full token-set match fails (e.g.
    "Strixhaven: School of Mages (Commander 2021)" has too many tokens
    for any group to satisfy), retry on the longest single distinctive
    token.
  - Group-type preference: prefer groups whose name contains "deck" /
    "decks" over those containing "kit". Tarkir Dragonstorm precons land
    on the regular Commander Deck group instead of the Commander Kit
    bundle group (which has no market price in TCGCSV).
  - $5 price floor: reject any product whose Market Price is under $5
    as almost certainly a single card, not a precon. Fixes false matches
    like Limit Break -> "Cloud's Limit Break" and Riders of Rohan ->
    the single card of the same name.
  - Product preference: prefer products whose name contains "deck",
    "kit", or "commander" over bare names — further reduces single-card
    matches even when the price floor doesn't catch them.
"""

import gzip
import io
import json
import math
import random
import re
import statistics
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TCGCSV_BASE = "https://tcgcsv.com/tcgplayer"
MAGIC_CATEGORY = 1
TIMEOUT = (10, 30)  # (connect, read) seconds per attempt
TCGCSV_UA = "precon-tracker/1.5 (+https://github.com/CooknRice/precon-tracker)"
PRICE_FLOOR = 5.0  # USD; any "deck" priced below this is rejected as a single
HISTORY_DAYS = 90  # calendar days of price history kept per deck and box
# Below this share of the catalog with a TCGPlayer price, a run is treated
# as degraded: the scraper refuses to overwrite an existing good prices.json
# or pollute history. A share, not a count, so removing decks from
# decks.json never makes every later run look degraded (see min_tcg_hits).
MIN_TCG_HIT_SHARE = 0.4


def min_tcg_hits(n_decks: int) -> int:
    """TCGPlayer hits a run needs to count as healthy, for a catalog of
    n_decks: 47 of 117 decks today, about half the usual 109."""
    return max(1, math.ceil(MIN_TCG_HIT_SHARE * n_decks))

# Run time budget. The workflow kills the job at 25 minutes and then commits
# nothing, so the fetch phases share one deadline: once it passes, no phase
# starts another request. One request can still overrun it by its own worst
# case (4 attempts at TIMEOUT, backoff, capped Retry-After, capped body read),
# about 4 minutes, which leaves room for setup, tests and the commit.
RUN_BUDGET_SECONDS = 15 * 60
REQUEST_MAX_SECONDS = 120   # wall clock cap on reading one response body
RETRY_AFTER_CAP = 10        # seconds; a server's Retry-After is honoured up to this
BREAKER_FAILS = 5           # consecutive failures that end a phase early
# A phase cut short (or failed) keeps the previous run's crack and box values
# for what it did not reach, marked with carried_from, for at most this long.
CARRY_MAX_DAYS = 3

# Tokens to ignore when comparing set names. Common English fillers plus
# Magic-specific noise that appears in different positions across naming
# conventions (decks.json vs TCGCSV groups).
STOPWORDS = {
    "the", "of", "a", "and", "an", "in", "to", "with", "for", "on", "at",
}
# Treated as semi-noise: not required for match, but tracked for scoring.
# "universes"/"beyond" let a deck set ("Assassin's Creed") match its TCGCSV
# group ("Universes Beyond: Assassin's Creed"), which otherwise fails the
# bidirectional token check and silently drops that set's box pricing.
SEMI_NOISE = {"commander", "kit", "deck", "decks", "precon", "starter",
              "universes", "beyond"}


# -------------------------------------------------------------------------
# TCGCSV helpers
# -------------------------------------------------------------------------

def norm(s: str) -> str:
    """Lowercase + strip all non-alphanumeric. For fuzzy product-name match."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def tokenize(s: str) -> set:
    """Lowercase, split on non-alphanumeric, drop pure stopwords."""
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in STOPWORDS}


class _CappedRetry(Retry):
    """Retry that honours Retry-After only up to RETRY_AFTER_CAP seconds.
    urllib3 would otherwise sleep for whatever the server asks (up to 6 h)."""

    def get_retry_after(self, response):
        v = super().get_retry_after(response)
        return None if v is None else min(v, RETRY_AFTER_CAP)


def make_session(user_agent: str) -> requests.Session:
    """Session with exponential-backoff retry on transient failures.

    Retries on 429 (rate limit) and 5xx, with backoff 1s, 2s, 4s. urllib3
    honours Retry-After headers, capped at RETRY_AFTER_CAP. Connection errors
    get the same treatment via `connect=` and `read=`.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    retry = _CappedRetry(
        total=4,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---- Run clock and phase circuit breaker ----

_RUN_DEADLINE: float | None = None


def start_run_clock(budget: float = RUN_BUDGET_SECONDS) -> None:
    global _RUN_DEADLINE
    _RUN_DEADLINE = time.monotonic() + budget


def budget_left() -> float:
    """Seconds left in the run budget (infinite when no clock was started)."""
    return math.inf if _RUN_DEADLINE is None else _RUN_DEADLINE - time.monotonic()


class PhaseGuard:
    """Stops a fetch loop early when the run budget is spent or after
    BREAKER_FAILS failures in a row, so one slow or broken source cannot hold
    the whole job past its timeout. `cut_short` says why it stopped (None
    while the phase runs normally); `trip()` marks a phase that failed.
    A failure passed a `key` (a group or a set) counts once per key, so
    several decks hitting the same broken group or set count as one failure,
    and only failures across distinct keys stop the phase."""

    def __init__(self, name: str, max_fails: int = BREAKER_FAILS):
        self.name = name
        self.max_fails = max_fails
        self.fails = 0
        self.failed_keys: set = set()
        self.cut_short: str | None = None

    def ok(self) -> bool:
        if self.cut_short is None and budget_left() <= 0:
            self.trip("run time budget spent")
        return self.cut_short is None

    def success(self) -> None:
        self.fails = 0

    def failure(self, key=None) -> None:
        if key is not None:
            if key in self.failed_keys:
                return
            self.failed_keys.add(key)
        self.fails += 1
        if self.cut_short is None and self.fails >= self.max_fails:
            self.trip(f"{self.fails} failures in a row")

    def trip(self, reason: str) -> None:
        if self.cut_short is None:
            self.cut_short = reason
            print(f"  {self.name}: stopping early ({reason})", flush=True)


def _finite(v) -> float | None:
    """v as a float when it is a finite number, else None. Upstream JSON can
    carry 1e999 or NaN, which Python reads as inf/nan and a browser cannot."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: an int too large for a float (a 330 digit literal).
        return None
    return f if math.isfinite(f) else None


def _parse_finite_float(s: str) -> float | None:
    return _finite(float(s))


def _parse_finite_int(s: str) -> int | None:
    """JSON integer as an int, or None when no float can hold it (or it is
    past Python's int string length limit), like an out of range float."""
    try:
        n = int(s)
    except ValueError:
        return None
    return n if _finite(n) is not None else None


# Shared by every upstream JSON parse: NaN, Infinity and numbers out of the
# float range (1e999, a 400 digit integer) all read as None.
_FINITE_JSON = {"parse_float": _parse_finite_float, "parse_int": _parse_finite_int,
                "parse_constant": lambda _name: None}


def loads_finite(text) -> object:
    """json.loads that reads NaN, Infinity and out of range numbers as None."""
    return json.loads(text, **_FINITE_JSON)


def _get_body(url: str, session: requests.Session) -> bytes:
    """GET url and return the body, reading it under a wall clock cap.
    TIMEOUT bounds each socket read, not the whole body, so a server that
    drips bytes could otherwise hold a request open indefinitely."""
    started = time.monotonic()
    with session.get(url, timeout=TIMEOUT, stream=True) as r:
        r.raise_for_status()
        # read1 returns whatever one socket read brings, so the clock is
        # checked at least every TIMEOUT seconds; iter_content would block
        # until a whole chunk arrived. urllib3 1.x has no read1.
        read1 = getattr(r.raw, "read1", None)
        chunks = iter(lambda: read1(1 << 16, decode_content=True) or b"", b"") if read1 \
            else r.iter_content(chunk_size=1 << 16)
        body = []
        for chunk in chunks:
            body.append(chunk)
            if time.monotonic() - started > REQUEST_MAX_SECONDS:
                raise TimeoutError(f"{url}: body not read within {REQUEST_MAX_SECONDS}s")
        return b"".join(body)


def fetch_tcgcsv_json(path: str, session: requests.Session) -> dict:
    url = f"{TCGCSV_BASE}/{path}"
    data = loads_finite(_get_body(url, session))
    # Guard against an HTTP-200 non-JSON / wrong-shape body (e.g. a Cloudflare
    # interstitial) sneaking past raise_for_status as valid-but-useless data.
    if not isinstance(data, dict):
        raise ValueError(f"TCGCSV {path}: expected a JSON object, got {type(data).__name__}")
    return data


def load_magic_groups(session: requests.Session) -> list:
    data = fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/groups", session)
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError("TCGCSV groups: 'results' missing or not a list")
    return results


def score_group(group: dict) -> tuple:
    """Sortable preference score for a group. Higher = more preferred.

    Order of priorities:
      1. Has 'commander' in name (it's a precon group, not a singles set).
      2. Has 'deck' or 'decks' (regular precon, not a Kit/Bundle).
      3. Does NOT have 'kit' (Kits often have no market price in TCGCSV).
      4. Shorter name (more canonical / less specific variant).
    """
    name = (group.get("name") or "").lower()
    has_cmdr = "commander" in name
    has_deck = "deck" in name  # matches both "deck" and "decks"
    has_kit = "kit" in name
    return (has_cmdr, has_deck, not has_kit, -len(name))


# Deck set name -> TCGCSV groupId, for sets the token rules can't resolve.
# Checked before any matching, and ignored if the group ever disappears.
GROUP_OVERRIDES = {
    # The 2021 Strixhaven precons live in group 'Commander 2021' (C21), whose
    # name shares no word with ours but the year. Without this the token
    # fallback once picked the 2026 'Commander: Secrets of Strixhaven' group.
    "Strixhaven: School of Mages (Commander 2021)": 2801,
}
# A group first published this many days AFTER the decks came out is a later
# release, however well the names match (Strixhaven 2021 vs 2026). Only that
# direction is checked: an older group can gain products later (the Foundations
# Commander decks of 2026 sit in the 2024 'Commander: Foundations' group).
GROUP_MAX_DAYS_AFTER_RELEASE = 90
_YEAR_RE = re.compile(r"^(19|20)\d\d$")


def _days_after(later: str | None, earlier: str | None) -> int | None:
    """Days from `earlier` to `later` (ISO dates or datetimes); None if unusable."""
    try:
        dl = datetime.fromisoformat((later or "")[:10])
        de = datetime.fromisoformat((earlier or "")[:10])
    except ValueError:
        return None
    return (dl - de).days


def _group_matches_pins(group: dict, pins: set) -> bool:
    """A pin (a year like '2021', or a set code written in parentheses) must
    appear in the group's name or be its set code."""
    if not pins:
        return True
    name_tokens = tokenize(group.get("name") or "")
    abbr = (group.get("abbreviation") or "").lower()
    return all(p in name_tokens or p == abbr for p in pins)


def pick_group_for_set(groups: list, deck_set: str,
                       release_date: str | None = None) -> dict | None:
    """Token set group matching with release guards and fallbacks.

    Order: explicit override, then strict token containment, then the
    parenthetical alone ('(Commander 2021)'), then the longest distinctive
    token. Every pass honours two release guards so a newer set that reuses a
    word cannot win: pinned tokens (a year, or a set code in parentheses) must
    appear in the group, and when `release_date` is known the group must not
    have been published more than GROUP_MAX_DAYS_AFTER_RELEASE days after it.
    """
    override = GROUP_OVERRIDES.get(deck_set)
    if override is not None:
        for g in groups:
            if g.get("groupId") == override:
                return g

    deck_tokens = tokenize(deck_set)
    if not deck_tokens:
        return None

    paren = " ".join(re.findall(r"\(([^)]*)\)", deck_set or ""))
    pins = {t for t in deck_tokens if _YEAR_RE.match(t)}
    pins |= tokenize(paren) - SEMI_NOISE

    def in_release(g: dict) -> bool:
        if not _group_matches_pins(g, pins):
            return False
        gap = _days_after(g.get("publishedOn"), release_date)
        return gap is None or gap <= GROUP_MAX_DAYS_AFTER_RELEASE

    def containing(required: set) -> list:
        return [(score_group(g), g) for g in groups
                if required.issubset(tokenize(g.get("name") or "")) and in_release(g)]

    # Required tokens are everything that isn't pure noise. We'll require
    # all of these to appear in the group's name token set.
    required = deck_tokens - SEMI_NOISE
    if not required:
        # Set name is nothing but noise words (e.g. "Starter Commander
        # Decks" -> {starter, decks, commander}, all SEMI_NOISE). Don't give
        # up — fall back to the full token set so a same-named group can
        # still match instead of silently dropping the whole set's pricing.
        required = deck_tokens

    # First pass: full token-set containment.
    candidates = containing(required)

    # Second pass: the parenthetical names the precon product on its own
    # ("Strixhaven: School of Mages (Commander 2021)" -> "Commander 2021").
    if not candidates and paren:
        paren_tokens = tokenize(paren)
        candidates = containing((paren_tokens - SEMI_NOISE) or paren_tokens)

    # Fallback: strict containment failed (often because decks.json set
    # name has more tokens than the canonical TCGCSV name). Try the single
    # longest distinctive token (>=5 chars), still inside the release guards.
    if not candidates:
        long_tokens = sorted([t for t in required if len(t) >= 5 and t not in pins],
                             key=len, reverse=True)
        for token in long_tokens:
            for g in groups:
                if token in (g.get("name") or "").lower() and in_release(g):
                    candidates.append((score_group(g), g))
            if candidates:
                break

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# Words that make a listing a DIFFERENT product from the plain precon deck.
# A marker only disqualifies when the product has it and the deck's own name
# does not, so a deck really called "... Jumpstart ..." still finds itself.
PRODUCT_KIND_MARKERS = (
    "collector", "game edition", "minimal packaging", "deluxe", "deck only",
    "prerelease", "sample", "display commander", "thick stock", "art series",
    "gift", "brawl", "jumpstart", "booster", "surge foil", "extended art",
)


def is_same_product_kind(deck_name: str, product_name: str) -> bool:
    """True when a listing looks like the same kind of product as the deck."""
    p = (product_name or "").lower()
    d = (deck_name or "").lower()
    return not any(m in p and m not in d for m in PRODUCT_KIND_MARKERS)


def pick_product_for_deck(
    products: list, deck_name: str, prices: dict | None = None
) -> dict | None:
    """Find the product that's most likely a precon deck, not a single card.

    When `prices` is supplied, products with a real listed price beat
    products with no price — and full "Commander Deck" SKUs beat the
    smaller "Commander Kit" SKUs that often have no market price.
    """
    deck_norm = norm(deck_name)
    if not deck_norm:
        return None

    candidates = []
    for p in products:
        pname = p.get("name") or ""
        pnorm = norm(pname)
        if deck_norm not in pnorm:
            continue
        # A Collector's Edition, Game Edition, Minimal Packaging or Deluxe Kit
        # SKU is a different product at a different price. Never let one stand
        # in for the regular deck, even on a day the regular SKU has no price
        # row: an unpriced regular deck must read 'no price', not 3x the price.
        if not is_same_product_kind(deck_name, pname):
            continue

        is_exact = (deck_norm == pnorm)
        pname_lower = pname.lower()
        # Hint that this is a precon, not a bare card with the same name.
        is_precon_named = any(
            kw in pname_lower for kw in ("deck", "kit", "precon", "commander", "bundle")
        )

        # Bundles (Case/Bundle/Set-of) are tracked separately; never pick
        # them as the single-deck match.
        is_bundle = any(
            kw in pname_lower for kw in (" case", "bundle", "set of", "5-pack", "5 pack", "all decks", "deck set")
        )
        if is_bundle:
            continue

        # Score the product type: prefer "Deck" over "Kit" because
        # Commander Decks are the canonical precon and Kits often have
        # no market price (and are a different cheaper product).
        is_deck = "deck" in pname_lower
        is_kit_only = "kit" in pname_lower and not is_deck

        # Price signals — needs a row in `prices` to evaluate.
        has_any_price = False
        passes_floor = True
        if prices is not None:
            row = prices.get(p.get("productId"))
            if row:
                market = row.get("marketPrice")
                low = row.get("lowPrice")
                mid = row.get("midPrice")
                # Any one of these counts; we'll fall back at read time too.
                has_any_price = any(v is not None for v in (market, low, mid))
                effective = market if market is not None else (low if low is not None else mid)
                if effective is not None and effective < PRICE_FLOOR:
                    passes_floor = False

        # Sortable score (higher first):
        #   - is precon named (deck/kit/commander/bundle), ranked above price
        #     so a priced single card with the deck's name ("Wibbly-wobbly,
        #     Timey-wimey (Surge Foil)") never beats an unpriced deck SKU
        #   - not a kit-only (prefer real Deck SKU)
        #   - has any price (don't pick a price-less SKU when one is priced)
        #   - passes floor (single-card sanity check)
        #   - is exact name match
        #   - shorter name
        score = (
            is_precon_named,
            not is_kit_only,
            has_any_price,
            passes_floor,
            is_exact,
            -len(pname),
        )
        candidates.append((score, p))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# Words a bundle name may add beyond the set's own name without pointing at a
# different release ("Commander Deck Case", "[Set of 5]", "Display").
BUNDLE_NAME_WORDS = {
    "case", "display", "set", "of", "bundle", "deck", "decks", "commander",
    "collection", "complete", "all", "pack", "two", "three", "four", "five",
    "universes", "beyond",
}
# A bundle whose contents we cannot verify must cost about what one of each
# deck costs; a case holding two of each lands near twice the members' total.
BUNDLE_UNVERIFIED_PRICE_BAND = (0.5, 1.3)


def bundle_is_same_release(bundle_name: str, set_name: str, group_name: str = "",
                           deck_names: list | None = None) -> bool:
    """Every distinctive word of a bundle's name must come from our set, its
    TCGCSV group or our deck names. 'Secrets of Strixhaven Commander Deck
    Case' fails for the 2021 set because 'secrets' appears in none of them."""
    allowed = tokenize(set_name) | tokenize(group_name)
    for n in deck_names or []:
        allowed |= tokenize(n)
    extra = tokenize(bundle_name) - BUNDLE_NAME_WORDS - SEMI_NOISE
    extra = {t for t in extra if not re.fullmatch(r"\d", t)}  # 'Set of 5'
    return extra.issubset(allowed)


def find_bundle_in_products(products: list, set_name: str, prices: dict | None = None,
                            deck_count: int | None = None, member_total: float | None = None,
                            contents: dict | None = None, group_name: str = "",
                            deck_names: list | None = None) -> tuple | None:
    """Pick the product that holds exactly one of each deck in the set.

    Candidates are multi deck SKUs (Set of N, Case, Display, Bundle) from the
    same release, never a premium edition. A candidate is attached only when
    its deck count equals `deck_count`, checked in this order:
      1. MTGJSON sealed contents (`contents`: TCGplayer id -> {deck: count}),
         which must list each deck exactly once and name most of our decks;
      2. a count in the name ('Set of 5', 'All 4');
      3. otherwise its price must sit near `member_total` (one of each).
    Among valid candidates a verified count beats the price check, a Market
    price beats a Low/Mid fallback, a real 'Set of N' beats a case, then the
    cheaper product wins. Returns (product, contents_source) or None.
    """
    BUNDLE_KEYWORDS = ("commander deck case", "commander kit case", "deck case",
                       "decks bundle", "deck bundle", "deck set", "set of",
                       "all decks", "5-pack", "5 pack", "all 5", "all 4",
                       "complete set", "commander collection", "deck display")
    EXCLUDE_KEYWORDS = ("token", "sleeve", "playmat", "binder", "deck box", "card box",
                        "display commander", "thick stock")
    PREMIUM_KEYWORDS = ("collector", "game edition", "minimal packaging", "deluxe", "gift",
                        "starter kit")
    prices = prices or {}
    contents = contents or {}

    candidates = []
    for p in products:
        name = (p.get("name") or "")
        nl = name.lower()
        pid = p.get("productId")
        if any(x in nl for x in EXCLUDE_KEYWORDS) or any(x in nl for x in PREMIUM_KEYWORDS):
            continue
        # Must look like a multi deck bundle, not a single deck.
        if not (pid in contents or any(k in nl for k in BUNDLE_KEYWORDS)):
            continue
        if not bundle_is_same_release(name, set_name, group_name, deck_names):
            continue
        row = prices.get(pid) or {}
        market, low, mid = row.get("marketPrice"), row.get("lowPrice"), row.get("midPrice")
        eff = market if market is not None else (low if low is not None else mid)
        if eff is None:
            continue
        held = contents.get(pid)
        claimed = _bundle_claimed_count(name)
        if held is not None:
            source = "mtgjson"
            ok = deck_count is not None and sum(held.values()) == deck_count and max(held.values()) == 1
            # The decks inside must be ours: at least half of our deck names
            # must appear (tolerates a misspelt catalog name, rejects the
            # Assassin's Creed Starter Kit, whose two decks are not ours).
            if ok and deck_names:
                found = sum(1 for dn in deck_names if any(norm(dn) in norm(h) for h in held))
                ok = found * 2 >= len(deck_names)
        elif claimed is not None:
            source = "name"
            ok = claimed == deck_count
        else:
            source = "price"
            lo, hi = BUNDLE_UNVERIFIED_PRICE_BAND
            ok = bool(member_total) and lo <= eff / member_total <= hi
        if not ok:
            continue
        score = (source != "price", market is not None, "set of" in nl, -float(eff), -len(name))
        candidates.append((score, p, source))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][2]


def _bundle_claimed_count(name: str) -> int | None:
    """If a bundle name advertises how many decks it holds ('Set of 5',
    'All 4', '5-pack', 'Set of Two'), return that count so the bundle pass can
    check it matches the decks we actually have. None when not stated."""
    low = (name or "").lower()
    words = {"two": 2, "three": 3, "four": 4, "five": 5}
    m = re.search(r"set of (two|three|four|five)\b", low)
    if m:
        return words[m.group(1)]
    m = re.search(r"set of (\d+)|all (\d+)|(\d+)\s*[- ]?pack", low)
    if m:
        for g in m.groups():
            if g:
                return int(g)
    return None


def sealed_deck_contents(setlist: list) -> dict:
    """TCGplayer product id -> {deck product name: count} for every MTGJSON
    sealed product made only of precon decks (a Set of N, a Display, a Case).
    Nested products are expanded ('Display' = 2 x 'Set of 2'). Products that
    also hold boosters or other sealed goods are left out."""
    by_uuid = {}
    for st in setlist or []:
        for sp in st.get("sealedProduct") or []:
            if sp.get("uuid"):
                by_uuid[sp["uuid"]] = sp

    def decks_in(sp: dict, depth: int = 0) -> dict | None:
        if sp.get("category") == "deck":
            return {sp.get("name") or sp.get("uuid"): 1}
        if depth > 3:
            return None
        c = sp.get("contents") or {}
        if any(c.get(k) for k in ("pack", "card", "variable")):
            return None
        out: dict = {}
        for d in c.get("deck") or []:
            out[d.get("name")] = out.get(d.get("name"), 0) + 1
        for ch in c.get("sealed") or []:
            child = by_uuid.get(ch.get("uuid"))
            sub = decks_in(child, depth + 1) if child else None
            if not sub:
                return None
            for k, v in sub.items():
                out[k] = out.get(k, 0) + v * (ch.get("count") or 1)
        return out or None

    result = {}
    for sp in by_uuid.values():
        pid = (sp.get("identifiers") or {}).get("tcgplayerProductId")
        if not pid or sp.get("category") == "deck":
            continue
        held = decks_in(sp)
        if held and sum(held.values()) >= 2:
            try:
                result[int(pid)] = held
            except (TypeError, ValueError):
                continue
    return result


def flatten_prices(price_results: list) -> dict:
    """Reduce TCGCSV price rows to one record per productId. Prefer 'Normal'.
    Prices that are not finite numbers (a string, an overflow) become None."""
    by_pid: dict[int, dict] = {}
    for row in price_results:
        pid = row.get("productId") if isinstance(row, dict) else None
        if pid is None:
            continue
        row = dict(row, **{k: _finite(row.get(k)) for k in ("marketPrice", "lowPrice", "midPrice")})
        existing = by_pid.get(pid)
        if existing is None:
            by_pid[pid] = row
            continue
        if row.get("subTypeName") == "Normal" and existing.get("subTypeName") != "Normal":
            by_pid[pid] = row
    return by_pid


def tcg_product_url(product_id: int, product_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (product_name or "").lower()).strip("-")
    return f"https://www.tcgplayer.com/product/{product_id}/magic-{slug}" if slug else f"https://www.tcgplayer.com/product/{product_id}"


def tcg_search_url(name: str) -> str:
    return f"https://www.tcgplayer.com/search/magic/product?q={urllib.parse.quote(name or '')}"


def ck_search_url(name: str) -> str:
    return (f"https://www.cardkingdom.com/catalog/search?filter%5Bname%5D="
            f"{urllib.parse.quote(name or '')}&filter%5Btab%5D=product")


# The one host each vendor's links may use. A link anywhere else (a Card
# Kingdom API path that resolves off site, a scheme other than https, user
# info before the host) is replaced by that vendor's search link, so
# prices.json never sends a buyer to a third party. The page checks again.
VENDOR_HOSTS = {"tcgplayer": "www.tcgplayer.com", "cardkingdom": "www.cardkingdom.com"}
_URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


def vendor_url_ok(url, vendor: str) -> bool:
    """Is `url` an https link on `vendor`'s own host (VENDOR_HOSTS)? Only
    plain URL characters are allowed, so no space, backslash or control
    character can make a browser read a different host than Python does."""
    if not isinstance(url, str) or not _URL_CHARS.fullmatch(url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == VENDOR_HOSTS[vendor]
            and parts.username is None and parts.password is None and port is None)


def enforce_vendor_hosts(output: dict, decks: list) -> int:
    """Replace every vendor link in a prices.json `output` that fails
    vendor_url_ok with the vendor's search link for the same item, in place
    (carried values included). Returns how many links it replaced."""
    names = {d["id"]: d.get("name") or "" for d in decks}
    fixed = 0

    def check(rec: dict, key: str, vendor: str, name: str) -> None:
        nonlocal fixed
        if not isinstance(rec, dict) or rec.get(key) is None or vendor_url_ok(rec[key], vendor):
            return
        print(f"  links: {vendor} link for {name!r} is off site, using search: {rec[key]!r}", flush=True)
        rec[key] = tcg_search_url(name) if vendor == "tcgplayer" else ck_search_url(name)
        fixed += 1

    vendors = output.get("vendors") or {}
    for did, rec in (vendors.get("tcgplayer") or {}).items():
        check(rec, "url", "tcgplayer", names.get(did, did))
    for did, rec in (vendors.get("cardkingdom") or {}).items():
        check(rec, "url", "cardkingdom", names.get(did, did))
    for b in (output.get("bundles") or {}).values():
        check(b, "url", "tcgplayer", (b or {}).get("name") or "")
    for set_name, rows in (output.get("boxes") or {}).items():
        for r in rows or []:
            label = (r or {}).get("name") or set_name
            check(r, "url", "tcgplayer", label)
            check(r, "ck_url", "cardkingdom", label)
    return fixed


def fetch_all_tcgcsv(decks: list, shared: dict | None = None) -> tuple[dict, dict]:
    """Returns (per_deck_results, bundles_by_set).

    bundles_by_set maps set name → bundle info (price/url/deck_ids/etc.)
    A bundle is a "Commander Deck Case" / "Set of N" SKU that contains
    every precon from the same set.
    When `shared` is a dict, it receives the groups list ("groups") and the
    fetched groups by groupId ("group_cache"), so the box phase reuses them
    instead of downloading them again.
    """
    session = make_session(TCGCSV_UA)

    print("Loading TCGCSV Magic groups list...", flush=True)
    try:
        groups = load_magic_groups(session)
    except Exception as e:
        print(f"  FATAL: couldn't load groups list: {e}", flush=True)
        groups = []
    # Zero groups => the source is down/blocked; abort before we produce a file
    # full of no-match decks (the coverage gate would refuse it anyway, but
    # failing here is louder and avoids wasted downstream work).
    if not groups:
        raise SystemExit("Aborting: TCGCSV returned 0 Magic groups (source down or blocked).")
    print(f"  {len(groups)} Magic groups loaded\n", flush=True)

    group_cache: dict[int, dict] = {}
    if shared is not None:
        shared["groups"] = groups
        shared["group_cache"] = group_cache
    guard = PhaseGuard("TCGCSV")

    def ensure_group(group: dict) -> dict:
        gid = group["groupId"]
        cached = group_cache.get(gid)
        # Reuse only SUCCESSFUL results. A cached error is NOT reused, so a
        # transient failure on one deck's pass doesn't permanently zero the
        # group for every later deck/bundle that needs it — they retry.
        if cached is not None and not cached.get("error"):
            return cached
        if not guard.ok():
            return {"name": group.get("name"), "products": [], "prices": {},
                    "error": f"skipped: {guard.cut_short}"}
        # Fetch products and prices independently so a price-only failure
        # still yields product data (and vice versa).
        result = {"name": group.get("name"), "products": [], "prices": {}, "error": None}
        errs = []
        try:
            result["products"] = fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/{gid}/products", session).get("results", [])
        except Exception as e:
            errs.append(f"products: {e}")
        try:
            result["prices"] = flatten_prices(fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/{gid}/prices", session).get("results", []))
        except Exception as e:
            errs.append(f"prices: {e}")
        if errs:
            result["error"] = "; ".join(errs)
            guard.failure(gid)
        else:
            guard.success()
        time.sleep(0.3 + random.uniform(0, 0.2))  # jittered politeness
        group_cache[gid] = result
        return result

    # Earliest release date per set: the release guard in pick_group_for_set.
    release_by_set: dict[str, str] = {}
    for d in decks:
        rd = d.get("releaseDate")
        if rd and (d.get("set", "") not in release_by_set or rd < release_by_set[d.get("set", "")]):
            release_by_set[d.get("set", "")] = rd

    results: dict[str, dict] = {}
    for i, deck in enumerate(decks, 1):
        deck_id = deck["id"]
        deck_name = deck["name"]
        deck_set = deck.get("set", "")

        fallback_url = tcg_search_url(deck_name)
        base = {
            "price": None, "price_low": None,
            "url": fallback_url,
            "status": "unknown",
            "snippet": None,
        }

        group = pick_group_for_set(groups, deck_set, release_by_set.get(deck_set))
        if not group:
            base["status"] = "tcgcsv-no-group-match"
            base["snippet"] = f"set={deck_set}"
            results[deck_id] = base
            print(f"[{i:3}/{len(decks)}] {deck_name:<36} TCG ------  (no group for '{deck_set}')", flush=True)
            continue

        gc = ensure_group(group)
        if gc.get("error"):
            base["status"] = "tcgcsv-group-error"
            base["snippet"] = (gc["error"] or "")[:100]
            results[deck_id] = base
            print(f"[{i:3}/{len(decks)}] {deck_name:<36} TCG ------  (group fetch error)", flush=True)
            continue

        product = pick_product_for_deck(gc["products"], deck_name, gc["prices"])
        if not product:
            base["status"] = "tcgcsv-no-product-match"
            base["snippet"] = f"group={gc['name']}"
            results[deck_id] = base
            print(f"[{i:3}/{len(decks)}] {deck_name:<36} TCG ------  (no product in '{gc['name']}')", flush=True)
            continue

        pid = product["productId"]
        price_row = gc["prices"].get(pid) or {}
        market = price_row.get("marketPrice")
        low = price_row.get("lowPrice")
        mid = price_row.get("midPrice")
        url = tcg_product_url(pid, product.get("name", ""))

        # Effective price: prefer marketPrice; fall back to lowPrice (the
        # cheapest currently-listed copy), then midPrice. Some newer or
        # niche products (e.g. Tarkir Dragonstorm Commander Kits) have no
        # marketPrice yet but plenty of low/mid pricing.
        effective_price = market if market is not None else (low if low is not None else mid)
        price_source = "Market" if market is not None else ("Low" if low is not None else ("Mid" if mid is not None else None))

        # Price-floor sanity check: precons are never < $5. If the matched
        # product is, we almost certainly hit a single card with the same
        # name (e.g. "Cloud's Limit Break" instead of the Limit Break deck).
        if effective_price is not None and effective_price < PRICE_FLOOR:
            results[deck_id] = {
                "price": None, "price_low": None,
                "url": fallback_url,
                "status": "tcgcsv-likely-single",
                "snippet": f"rejected: {product.get('name')} ({price_source} ${float(effective_price):.2f})",
            }
            print(f"[{i:3}/{len(decks)}] {deck_name:<36} TCG ------  (rejected ${float(effective_price):.2f}: likely single card)", flush=True)
            continue

        if effective_price is not None:
            results[deck_id] = {
                "price": float(effective_price),
                "price_low": float(low) if low is not None else None,
                "price_source": price_source,  # "Market" | "Low" | "Mid"
                "url": url,
                "status": "ok" if price_source == "Market" else f"ok-{price_source.lower()}-fallback",
                "snippet": product.get("name"),
            }
            low_str = f" / low ${float(low):.2f}" if low and price_source != "Low" else ""
            print(f"[{i:3}/{len(decks)}] {deck_name:<36} TCG ${float(effective_price):>6.2f}  ({price_source}{low_str})", flush=True)
        else:
            results[deck_id] = {
                "price": None, "price_low": None,
                "url": url,
                "status": "tcgcsv-no-price",
                "snippet": product.get("name"),
            }
            print(f"[{i:3}/{len(decks)}] {deck_name:<36} TCG ------  (no price data)", flush=True)

    # ----------------------------------------------------------------
    # Bundle pass: for each set with 2+ of our decks, find the SKU that
    # holds exactly one of each deck (see find_bundle_in_products) and
    # price it. Savings are filled in later by price_bundles(), once the
    # Card Kingdom prices are known.
    # ----------------------------------------------------------------
    print("\n=== Phase 1b: TCGPlayer set bundles ===", flush=True)
    bundles: dict[str, dict] = {}
    deck_ids_by_set: dict[str, list] = {}
    deck_by_id = {d["id"]: d for d in decks}
    for d in decks:
        deck_ids_by_set.setdefault(d.get("set", ""), []).append(d["id"])

    # MTGJSON's sealed product contents say exactly how many of which deck a
    # bundle SKU holds. Optional: without it the name and price checks apply.
    contents: dict = {}
    try:
        contents = sealed_deck_contents(_get_setlist(make_session(TCGCSV_UA)))
        print(f"  {len(contents)} multi deck products with MTGJSON contents", flush=True)
    except Exception as e:
        print(f"  bundle contents unavailable (not fatal): {e}", flush=True)

    for set_name, deck_ids in deck_ids_by_set.items():
        if len(deck_ids) < 2:
            continue  # bundles only make sense for sets with multiple precons
        group = pick_group_for_set(groups, set_name, release_by_set.get(set_name))
        if not group:
            continue
        gc = group_cache.get(group["groupId"]) or ensure_group(group)
        if gc.get("error"):
            continue
        member_prices = [results.get(did, {}).get("price") for did in deck_ids]
        member_total = sum(member_prices) if all(p is not None for p in member_prices) else None
        picked = find_bundle_in_products(
            gc["products"], set_name, gc["prices"], len(deck_ids), member_total,
            contents, gc.get("name") or "", [deck_by_id[did]["name"] for did in deck_ids])
        if not picked:
            print(f"  [{set_name}] no bundle holding one of each of our {len(deck_ids)} decks", flush=True)
            continue
        bundle_product, contents_source = picked
        bpid = bundle_product["productId"]
        prow = gc["prices"].get(bpid) or {}
        market = prow.get("marketPrice")
        low = prow.get("lowPrice")
        mid = prow.get("midPrice")
        eff = market if market is not None else (low if low is not None else mid)
        src = "Market" if market is not None else ("Low" if low is not None else "Mid")
        bundle_id = re.sub(r"[^a-z0-9]+", "-", set_name.lower()).strip("-") + "-bundle"
        bundles[bundle_id] = {
            "name": bundle_product.get("name"),
            "set": set_name,
            "deck_ids": deck_ids,
            "deck_count": len(deck_ids),
            "price": float(eff),
            "price_source": src,
            "contents_source": contents_source,  # "mtgjson" | "name" | "price"
            "individual_total": None,
            "savings": None,
            "url": tcg_product_url(bpid, bundle_product.get("name", "")),
            "snippet": bundle_product.get("name"),
        }
        print(f"  [{set_name}] {bundle_product.get('name')} → ${eff:.2f} ({contents_source})", flush=True)

    print(f"\nFound {len(bundles)} set bundle{'s' if len(bundles) != 1 else ''}", flush=True)
    return results, bundles


# -------------------------------------------------------------------------
# Crack value (sum of singles) via MTGJSON decklists + AllPricesToday
# -------------------------------------------------------------------------
# We price every card in each deck at Near Mint retail, from BOTH TCGPlayer
# and Card Kingdom. One source (MTGJSON AllPricesToday), one condition basis
# (retail = NM), so the two vendor totals are directly comparable.

MTGJSON_BASE = "https://mtgjson.com/api/v5"
ALLPRICES_URL = f"{MTGJSON_BASE}/AllPricesToday.json.gz"
# TCGPlayer has no buylist in MTGJSON, so we estimate the "crack to sell"
# value at this fraction of TCG retail (typical net after marketplace/direct
# fees and the buyer discount). Card Kingdom uses its REAL buylist instead.
TCG_SELL_RATE = 0.70

# Manual deck_id -> MTGJSON fileName overrides for decks whose names don't
# match by normalization. MTGJSON lists the Final Fantasy commander decks
# only under variant names like "Limit Break (FINAL FANTASY VII)" plus a
# separate "...Collector's Edition" SKU; we pin the non-collector file.
# (Verified: each resolves to a real decklist that prices ~100/100 cards.)
# Every other deck in decks.json matches its MTGJSON decklist by name with
# the same release date (checked 2026-10-08). A deck that matches nothing is
# usually misnamed in decks.json; the products once listed as Animus
# Antiquity, Master of Keys, Faerie Schemes and Take Flight never existed
# under those names and were removed or renamed in the catalog.
DECK_FILE_MAX_DAYS = 30  # a decklist must be released this close to the deck
DECK_FILE_OVERRIDES = {
    "revival-trance": "RevivalTranceFinalFantasyVi_FIC",
    "limit-break": "LimitBreakFinalFantasyVii_FIC",
    "counter-blitz": "CounterBlitzFinalFantasyX_FIC",
    "scions-and-spellcraft": "ScionsSpellcraftFinalFantasyXiv_FIC",
}


def build_deck_mtgjson_map(decks: list, session: requests.Session) -> dict:
    """Map each of our deck ids → MTGJSON deck fileName.

    First by normalized name (Commander type decks only, so theme and
    sample decks are never picked) among decklists released within
    DECK_FILE_MAX_DAYS of the deck, nearest first, then apply
    DECK_FILE_OVERRIDES for decks MTGJSON names differently. Deck names repeat
    across releases (Draconic Domination, Peace Offering), so a decklist from
    another year is skipped rather than priced as this deck.
    """
    # Raises when DeckList.json cannot be read: pricing only the override
    # decks would drop every other deck, while a failed phase keeps the
    # previous run's values (see main).
    data = fetch_json(f"{MTGJSON_BASE}/DeckList.json", session)
    mtg_decks = data.get("data", [])
    by_norm: dict = {}
    valid_files = set()
    for m in mtg_decks:
        valid_files.add(m.get("fileName"))
        if "commander" not in (m.get("type") or "").lower():
            continue
        by_norm.setdefault(norm(m.get("name") or ""), []).append(m)
    mapping = {}
    for d in decks:
        near = []
        for m in by_norm.get(norm(d["name"]), []):
            # A deck without a release date keeps the old name only match.
            gap = _days_after(m.get("releaseDate"), d.get("releaseDate")) \
                if d.get("releaseDate") else 0
            if gap is not None and abs(gap) <= DECK_FILE_MAX_DAYS:
                near.append((abs(gap), m.get("fileName") or ""))
        if near:
            mapping[d["id"]] = min(near)[1]
        elif d["id"] not in DECK_FILE_OVERRIDES and by_norm.get(norm(d["name"])):
            other = ", ".join(f"{m.get('fileName')} ({m.get('releaseDate')})"
                              for m in by_norm[norm(d["name"])])
            print(f"  crack: [{d['id']}] skipped, the MTGJSON decklists named "
                  f"{d['name']!r} are from another release: {other}", flush=True)
    # Apply overrides, but only if the target file actually exists in the
    # current DeckList (guards against a renamed/removed MTGJSON file).
    for did, fn in DECK_FILE_OVERRIDES.items():
        if fn in valid_files:
            mapping[did] = fn
        else:
            print(f"  crack: override file missing in MTGJSON: {fn}", flush=True)
    return mapping


def fetch_json(url: str, session: requests.Session) -> dict:
    return loads_finite(_get_body(url, session))


def fetch_gz_json(url: str, session: requests.Session) -> dict:
    """GET a gzipped JSON file and return the parsed object."""
    with gzip.open(io.BytesIO(_get_body(url, session)), "rt", encoding="utf-8") as f:
        return json.load(f, **_FINITE_JSON)


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _latest_price(byday) -> float | None:
    """Most-recent dated price from an MTGJSON {date: price} map, or None.

    MTGJSON keys are zero-padded ISO dates, so lexicographic max == latest.
    We still filter to well-formed ISO keys so a stray malformed key can't be
    chosen as the 'latest'.
    """
    if not byday:
        return None
    keys = [k for k in byday.keys() if _ISO_DATE.match(str(k))]
    if not keys:
        return None
    return _finite(byday[max(keys)])


def finish_order(is_foil: bool = False, is_etched: bool = False) -> tuple:
    """MTGJSON price finishes to try for one printing, best match first.

    A foil entry or booster sheet reads the foil price, an etched one the
    etched price. Some printings exist in one finish only (the foil only face
    commanders of 2021 to 2024 precons, the etched sheets of Commander
    Masters or Assassin's Creed collector boosters), and MTGJSON then has no
    price under the asked finish, so the other finishes follow as fallbacks:
    a printing with a single finish can only be that finish."""
    if is_etched:
        return ("etched", "foil", "normal")
    if is_foil:
        return ("foil", "etched", "normal")
    return ("normal", "foil", "etched")


def price_in_finish(by_finish, order: tuple) -> tuple:
    """(price, finish) for the first finish in `order` with a price in an
    MTGJSON {finish: {date: price}} map, or (None, None)."""
    by_finish = by_finish or {}
    for fin in order:
        p = _latest_price(by_finish.get(fin))
        if p is not None:
            return p, fin
    return None, None


def _get_allprices(session: requests.Session) -> dict:
    """Download + parse MTGJSON AllPricesToday ONCE per run (about 5.5 MB gz,
    a few hundred MB parsed). Cached on the function so the crack phase and
    the box-EV phase share a single download instead of fetching it twice."""
    cached = getattr(_get_allprices, "_data", None)
    if cached is None:
        cached = fetch_gz_json(ALLPRICES_URL, session).get("data", {})
        _get_allprices._data = cached
    return cached


def fetch_crack_values(decks: list, session: requests.Session,
                       guard: PhaseGuard | None = None) -> dict:
    """Per-deck 'crack value' = sum of every card's Near Mint retail price,
    computed separately for TCGPlayer and Card Kingdom.

    Pipeline: MTGJSON decklist (uuid + count per card) → MTGJSON
    AllPricesToday (NM retail price per uuid, both vendors).
    Resilient: unmatched decks and unpriced cards are skipped, not fatal.
    `guard` stops the decklist loop early and is tripped when the phase
    cannot price anything, so main() knows the result is incomplete.
    Each card is priced in the finish the deck holds (MTGJSON isFoil and
    isEtched), so a foil only face commander counts at its foil price.
    card_count, priced and missing count cards (copies), not decklist rows:
    a deck with 32 Plains in 2 rows counts 32.
    Returns { deck_id: {tcg, cardkingdom, card_count, priced, missing} }.
    """
    guard = guard or PhaseGuard("crack")
    # Decks whose decklist could not be fetched this run; main() carries
    # their previous values forward instead of dropping them.
    failed: list = []
    fetch_crack_values.failed_ids = failed
    deck_map = build_deck_mtgjson_map(decks, session)
    print(f"  crack: matched {len(deck_map)}/{len(decks)} decks to MTGJSON", flush=True)
    if not deck_map:
        guard.trip("no decks matched to MTGJSON")
        return {}

    # 1. Pull each deck's card list (uuid + count). Collect unique uuids.
    deck_cards: dict[str, list] = {}
    need_uuids: set = set()
    for d in decks:
        fn = deck_map.get(d["id"])
        if not fn:
            continue
        if not guard.ok():
            break
        try:
            dj = fetch_json(f"{MTGJSON_BASE}/decks/{fn}.json", session)
        except Exception as e:
            print(f"  crack: [{d['id']}] deck fetch failed: {e}", flush=True)
            failed.append(d["id"])
            guard.failure(d.get("set") or d["id"])
            continue
        guard.success()
        body = dj.get("data", {})
        cards = []
        for board in ("commander", "mainBoard"):
            for c in body.get(board) or []:
                u = c.get("uuid")
                cnt = c.get("count") or 1
                nm = c.get("name")
                # Skip basic lands from the reverse index / chase list — they're
                # noise ("which precon has Forest?" is not a useful query).
                is_basic = (c.get("type") or "").startswith("Basic")
                fins = finish_order(bool(c.get("isFoil")), bool(c.get("isEtched")))
                if u:
                    cards.append((u, cnt, nm, is_basic, fins))
                    need_uuids.add((u, fins))
        deck_cards[d["id"]] = cards
        time.sleep(0.05)

    # 2. Download today's prices once; keep only the printings we need (both
    #    vendors, retail = Near Mint, in the finish the deck holds).
    if budget_left() <= 0:
        guard.trip("run time budget spent")
        return {}
    n_unique = len({u for u, _ in need_uuids})
    print(f"  crack: downloading MTGJSON prices ({n_unique} unique cards)...", flush=True)
    try:
        pdata = _get_allprices(session)
    except Exception as e:
        print(f"  crack: price file download failed: {e}", flush=True)
        guard.trip("price file download failed")
        return {}
    price_by_uuid: dict = {}
    for u, fins in need_uuids:
        paper = (pdata.get(u) or {}).get("paper", {})
        ckp = paper.get("cardkingdom") or {}
        tcgp = paper.get("tcgplayer") or {}
        tcg_retail, _ = price_in_finish(tcgp.get("retail"), fins)
        ck_retail, ck_fin = price_in_finish(ckp.get("retail"), fins)
        # The buylist price of the same finish CK sells, so a card never
        # sells for more than it costs because the two sides read different
        # finishes; with no CK retail price, the first finish CK buys.
        ck_buylist, _ = price_in_finish(ckp.get("buylist"), (ck_fin,) if ck_fin else fins)
        price_by_uuid[(u, fins)] = (tcg_retail, ck_retail, ck_buylist)
    # Keep the AllPrices cache (shared with the box-EV phase); don't free it.

    # 3. Sum per deck. Two sides:
    #    BUY  = retail (what the cards cost to acquire) — tcg + cardkingdom.
    #    SELL = what you'd get cracking to sell:
    #             - cardkingdom: REAL Card Kingdom buylist (only ~60% of cards
    #               are buyable; bulk commons/basics are genuinely $0 sell).
    #             - tcgplayer: estimated at TCG_SELL_RATE of TCG retail, since
    #               MTGJSON carries no TCGPlayer buylist. Clearly flagged.
    results: dict = {}
    card_index: dict = {}   # normalized card name -> {"name": display, "decks": [ids]}
    for did, cards in deck_cards.items():
        tcg_buy = ck_buy = 0.0
        tcg_sell_est = ck_sell = 0.0
        tcg_n = ck_n = missing = 0
        contributors = []   # (unit_tcg_price, name) for top-cards ranking
        seen_names = set()  # dedupe for the reverse index (per deck)
        for u, cnt, nm, is_basic, fins in cards:
            tcg_r, ck_r, ck_bl = price_by_uuid.get((u, fins), (None, None, None))
            if tcg_r is not None:
                tcg_buy += tcg_r * cnt
                tcg_sell_est += tcg_r * TCG_SELL_RATE * cnt
                tcg_n += cnt
            if ck_r is not None:
                ck_buy += ck_r * cnt
                ck_n += cnt
            if ck_bl is not None:
                ck_sell += ck_bl * cnt  # cards CK won't buy simply add $0
            if tcg_r is None and ck_r is None:
                missing += cnt
            # Chase-card tracking: TCG retail is the yardstick, but fall back to
            # CK retail for cards TCGPlayer doesn't price so CK-only chases show.
            if nm and not is_basic:
                val = tcg_r if tcg_r is not None else ck_r
                if val is not None:
                    contributors.append((val, nm))
            # Reverse index: which decks contain this (non-basic) card.
            if nm and not is_basic:
                key = norm(nm)
                if key and key not in seen_names:
                    seen_names.add(key)
                    entry = card_index.setdefault(key, {"name": nm, "decks": []})
                    entry["decks"].append(did)
        if tcg_n or ck_n:
            # Top 4 chase cards by unit price.
            contributors.sort(key=lambda x: -x[0])
            top_cards = [{"name": nm, "price": round(p, 2)} for p, nm in contributors[:4]]
            results[did] = {
                # BUY side (retail) — unchanged keys for back-compat.
                "tcg": round(tcg_buy, 2) if tcg_n else None,
                "cardkingdom": round(ck_buy, 2) if ck_n else None,
                # SELL side.
                "sell_tcg": round(tcg_sell_est, 2) if tcg_n else None,
                "sell_cardkingdom": round(ck_sell, 2) if ck_sell > 0 else None,
                "sell_tcg_estimated": True,  # TCG sell is a 70%-of-retail estimate
                "card_count": sum(cnt for _, cnt, _, _, _ in cards),
                "priced": max(tcg_n, ck_n),
                "missing": missing,
                "top_cards": top_cards,
            }
    print(f"  crack: priced {len(results)} decks", flush=True)
    # Stash the reverse index on the function so main() can write it out.
    fetch_crack_values.card_index = card_index
    return results


# -------------------------------------------------------------------------
# Sealed box prices (Play/Draft, Collector, Jumpstart) per set
# -------------------------------------------------------------------------

# Draftable "Commander" sets that have their own booster boxes (unlike the
# precon-only Commander products attached to a main set).
DRAFTABLE_COMMANDER = {
    "commandermasters",
    "commanderlegends",
    "commanderlegendsbattleforbaldursgate",
}


def _clean_set_name(s: str) -> str:
    """Strip 'Commander' qualifier and parenthetical years so a deck's set
    ('Bloomburrow Commander') matches the main box group ('Bloomburrow')."""
    s = re.sub(r"\(.*?\)", "", s or "")
    s = re.sub(r"\bcommander\b", "", s, flags=re.I)
    return s.strip(" :-")


def find_main_set_group(groups: list, set_name: str) -> dict | None:
    """Find the canonical main-set group for box products (NOT the
    'Commander:' / 'Art Series' / 'Promo' groups).

    Matching is two-stage to avoid a short cleaned set name (e.g.
    'Commander Masters' -> {masters}) subset-matching an unrelated superset
    group ('Masters 25'):
      1. Prefer an exact normalized-name match.
      2. Otherwise require a *bidirectional* token match — set tokens are a
         subset of group tokens AND the group's tokens (minus generic noise)
         are a subset of the set tokens — so the group can't carry extra
         distinguishing words like '25' that the set doesn't have.
    """
    # A few "Commander X" sets ARE draftable and carry their own booster boxes;
    # their group name legitimately contains 'commander', so match them by exact
    # (uncleaned) name before the generic 'commander'-excluding logic below.
    raw_norm = norm(set_name)
    if raw_norm in DRAFTABLE_COMMANDER:
        for g in groups:
            if norm(g.get("name") or "") == raw_norm:
                return g

    cleaned = _clean_set_name(set_name)
    toks = tokenize(cleaned)
    if not toks:
        return None
    set_norm = norm(cleaned)

    def eligible(g):
        gl = (g.get("name") or "").lower()
        return not any(x in gl for x in ("commander", "art series", "promo pack", "minimal packaging"))

    # Stage 1: exact normalized-name match wins outright.
    for g in groups:
        if eligible(g) and norm(_clean_set_name(g.get("name") or "")) == set_norm:
            return g

    # Stage 2: bidirectional token containment.
    best = None
    for g in groups:
        if not eligible(g):
            continue
        gtoks = tokenize(g.get("name") or "")
        # set ⊆ group  AND  group-extra-tokens ⊆ set (no extra distinguishing
        # words on the group side, e.g. a year/number the set lacks).
        if toks.issubset(gtoks) and (gtoks - SEMI_NOISE).issubset(toks):
            gn = g.get("name") or ""
            if best is None or len(gn) < len(best.get("name") or ""):
                best = g
    return best


def _classify_box(name: str) -> str | None:
    """Map a product name to one of our 3 tracked box types, or None.
    Excludes cases/master cases (bulk) and singles packs."""
    l = (name or "").lower()
    # Exclude bulk SKUs. Use the compound "master case" (not a bare "master",
    # which is a substring of set names like Double Masters / Commander Masters
    # and would wrongly drop their real boxes). "case" also drops "Booster Box
    # Case" before the box check below.
    if "case" in l or "master case" in l or "sample" in l or "omega" in l:
        return None
    if "booster pack" in l:
        return None  # single pack, not a box
    # A box/display unit. TCGPlayer labels these "Display" for recent sets but
    # "Booster Box" for some older ones (e.g. Commander Masters) — accept both.
    if "display" not in l and "booster box" not in l:
        return None
    if "collector booster" in l:
        return "collector"
    if "jumpstart" in l:
        return "jumpstart"
    # Play / Draft / Set boosters all map to our generic "play" box type
    # (Draft/Set predate Play Boosters, introduced 2024).
    if "play booster" in l or "draft booster" in l or "set booster" in l:
        return "play"
    return None

# Human labels for the 3 box types.
BOX_TYPE_LABELS = {"play": "Booster Box", "collector": "Collector Booster Box", "jumpstart": "Jumpstart Box"}
# The 'play' type covers three different products: Play Booster boxes (2024
# on) and, before them, Draft and Set Booster boxes, which differ in price,
# pack count and contents. Each box row records the kind it priced, and the
# label names it, so nothing compares or charts one kind against another.
BOX_KIND_LABELS = {"play": "Play Booster Box", "draft": "Draft Booster Box",
                   "set": "Set Booster Box", "collector": "Collector Booster Box",
                   "jumpstart": "Jumpstart Box"}
# When a set sells several kinds, the box row shows the first one priced, in
# this order: the Play Booster box, else the Draft box (the standard box of
# its era), else the Set Booster box.
PLAY_KIND_ORDER = ("play", "draft", "set")


def box_kind(name: str) -> str | None:
    """The booster kind printed on a box SKU's name (TCGplayer or Card
    Kingdom): play, draft, set, collector or jumpstart. None when the name
    does not say (an old plain 'Booster Box')."""
    n = (name or "").lower()
    for kind in ("collector", "jumpstart", "draft", "set", "play"):
        if f"{kind} booster" in n:
            return kind
    if "jumpstart" in n:
        return "jumpstart"
    return None

# Typical packs per display box, by the booster KIND printed on the SKU. Only
# a fallback: the real count comes from the MTGJSON sealed product of the SKU
# we priced (box_pack_count), because many boxes break the pattern. Commander
# Masters and Baldur's Gate Draft boxes hold 24 packs, the Commander Masters
# Collector box 4, most set Jumpstart boxes 18, and the 2024 Play boxes of
# MKM, OTJ, MH3, BLB and DSK 36.
PACKS_BY_KIND = {"draft": 36, "set": 30, "play": 30, "collector": 12, "jumpstart": 24}


def packs_for_sku(sku_name: str, box_type: str) -> int:
    """Typical pack count for a box, read off the product name (fallback)."""
    n = (sku_name or "").lower()
    for kind in ("collector", "jumpstart", "draft", "set", "play"):
        if f"{kind} booster" in n:
            return PACKS_BY_KIND[kind]
    return PACKS_BY_KIND.get(box_type, 1)


def _packs_in_contents(product: dict, kind: str | None) -> int | None:
    """Booster packs in one MTGJSON sealed box: the count of the pack entry in
    its contents. Box topper packs are left out (EV prices regular packs
    only). None when the contents do not name one pack product."""
    packs = [e for e in ((product.get("contents") or {}).get("sealed") or [])
             if isinstance(e, dict) and "topper" not in (e.get("name") or "").lower()]
    if kind and len(packs) > 1:
        packs = [e for e in packs if kind in (e.get("name") or "").lower()]
    if len(packs) != 1:
        return None
    n = packs[0].get("count")
    return n if isinstance(n, int) and n > 0 else None


def box_pack_count(sealed_products: list, product_id, kind: str | None,
                   sku_name: str, box_type: str) -> tuple[int, str]:
    """(packs, source) for the box SKU we priced.

    1. The MTGJSON sealed product with the same TCGplayer product id
       (source 'mtgjson').
    2. Else every MTGJSON booster box of the same kind in the set, when they
       all agree on one count (source 'mtgjson').
    3. Else the typical count for the kind (source 'typical')."""
    boxes = [sp for sp in sealed_products or []
             if isinstance(sp, dict) and sp.get("category") == "booster_box"]
    if product_id is not None:
        for sp in boxes:
            if str((sp.get("identifiers") or {}).get("tcgplayerProductId")) == str(product_id):
                n = _packs_in_contents(sp, kind)
                if n:
                    return n, "mtgjson"
    if kind:
        counts = {_packs_in_contents(sp, kind) for sp in boxes if sp.get("subtype") == kind}
        if len(counts) == 1 and None not in counts:
            return counts.pop(), "mtgjson"
    return packs_for_sku(sku_name, box_type), "typical"


BOX_BOOSTER_KEY = {"play": "play", "collector": "collector", "jumpstart": "jumpstart"}

# Box EV counts only cards worth at least this much (TCGplayer retail, USD).
# Cheaper cards sell only as bulk, for cents or less, so they add nothing; the
# UI note says the estimate leaves out cards you cannot easily sell.
BOX_EV_BULK_THRESHOLD = 1.00
# Share of a box's estimated value that must come from priced cards before
# its EV is published (see _pack_ev_from_booster for how it is measured).
BOX_EV_MIN_COVERAGE = 0.90
# Sellable value guessed for one unpriced card when no card of its rarity in
# the same booster model has a price (a new set before MTGJSON prices its
# rares). Below the bulk threshold, commons and uncommons count as nothing.
RARITY_VALUE_PRIOR = {"common": 0.0, "uncommon": 0.0, "rare": 2.0,
                      "mythic": 8.0, "special": 8.0, "bonus": 8.0}
UNKNOWN_RARITY_PRIOR = 2.0


def _get_setlist(session: requests.Session) -> list:
    """MTGJSON SetList.json 'data', downloaded once per run (the bundle pass
    and the box phase both use it). Raises on failure; a failure is not cached."""
    cached = getattr(_get_setlist, "_cache", None)
    if cached is not None:
        return cached
    data = fetch_json(f"{MTGJSON_BASE}/SetList.json", session).get("data", [])
    if not isinstance(data, list):
        raise ValueError("SetList: 'data' is not a list")
    _get_setlist._cache = data
    return data


def _build_setcode_map(session: requests.Session) -> dict:
    """normalized set name -> MTGJSON set code, from SetList.json."""
    try:
        data = _get_setlist(session)
    except Exception as e:
        print(f"  ev: couldn't load SetList: {e}", flush=True)
        return {}
    out = {}
    for s in data:
        code = s.get("code")
        nm = s.get("name")
        if code and nm:
            out[norm(nm)] = code
    return out


def _pack_ev_from_booster(booster_cfg: dict, price_of_uuid, bulk_threshold: float = 0.0,
                          rarity_of=None):
    """Expected value of ONE pack using MTGJSON's published booster model.

    booster_cfg has:
      - 'boosters': list of {weight, contents:{sheetName: count}}
      - 'boostersTotalWeight': sum of weights
      - 'sheets': {sheetName: {'totalWeight':N, 'foil':bool, 'cards':{uuid:weight}}}
    EV = sum over booster configs (weighted) of sum over sheet slots of
         (count * expected card value on that sheet).
    price_of_uuid(uuid, is_foil) returns a float price or None (no price).
    A sheet's `foil` flag selects the foil finish for the SAME card uuids.
    A priced card under `bulk_threshold` is worth 0 (it sells only as bulk).
    Returns (ev, coverage). Coverage is the share of the pack's estimated
    value that comes from priced cards, so an unpriced mythic weighs far more
    than an unpriced common. An unpriced card of known rarity (rarity_of) is
    guessed at the mean value of the priced cards of its rarity on its own
    sheet, else of its rarity and finish elsewhere in the model, else at
    RARITY_VALUE_PRIOR: a sheet whose mythics are all unpriced is not valued
    at its rares' prices. A card of unknown rarity is guessed at its sheet's
    priced mean (on a sheet at least half priced), else at the mean of the
    model's priced cards of unknown rarity, else at UNKNOWN_RARITY_PRIOR.
    The guesses only measure coverage; the EV
    counts priced cards alone, so a low coverage EV runs low.
    """
    sheets = booster_cfg.get("sheets") or {}
    boosters = booster_cfg.get("boosters") or []
    total_w = booster_cfg.get("boostersTotalWeight") or sum(b.get("weight", 0) for b in boosters)
    if not boosters or not total_w:
        return None, 0.0

    def rarity(uuid):
        return rarity_of(uuid) if rarity_of else None

    # Per sheet: expected value of the priced cards, the priced share of the
    # sheet's weight, and the unpriced cards. `pool` collects the value of
    # priced cards by (rarity, foil) and (rarity, any finish).
    sheet_ev = {}
    sheet_info = {}
    pool: dict = {}
    for name, sh in sheets.items():
        cards = sh.get("cards") or {}
        sw = sh.get("totalWeight") or sum(cards.values())
        is_foil = bool(sh.get("foil"))
        acc = 0.0
        priced_w = 0.0
        unpriced = []
        own: dict = {}  # rarity -> [value sum, count] of this sheet's priced cards
        if sw:
            for uuid, w in cards.items():
                p = price_of_uuid(uuid, is_foil)
                if p is None or p <= 0:
                    unpriced.append((uuid, w))
                    continue
                v = p if p >= bulk_threshold else 0.0
                acc += (w / sw) * v
                priced_w += w
                r = rarity(uuid)
                for s in (pool.setdefault((r, is_foil), [0.0, 0]),
                          pool.setdefault((r, None), [0.0, 0]),
                          own.setdefault(r, [0.0, 0])):
                    s[0] += v
                    s[1] += 1
        sheet_ev[name] = acc
        sheet_info[name] = (acc, (priced_w / sw) if sw else 0.0, unpriced, sw, is_foil, own)

    def mean(s):
        return s[0] / s[1] if s and s[1] else None

    def guess(uuid, is_foil, own, sheet_mean):
        r = rarity(uuid)
        if r is None:
            means = (sheet_mean, mean(pool.get((None, is_foil))), mean(pool.get((None, None))))
        else:
            means = (mean(own.get(r)), mean(pool.get((r, is_foil))), mean(pool.get((r, None))))
        for m in means:
            if m is not None:
                return m
        return RARITY_VALUE_PRIOR.get(r, UNKNOWN_RARITY_PRIOR)

    # Estimated value of each sheet's unpriced cards (per slot).
    sheet_gap = {}
    for name, (acc, share, unpriced, sw, is_foil, own) in sheet_info.items():
        sheet_mean = acc / share if share >= 0.5 else None
        sheet_gap[name] = sum((w / sw) * guess(u, is_foil, own, sheet_mean)
                              for u, w in unpriced)

    ev = 0.0
    gap = 0.0
    for b in boosters:
        bw = b.get("weight", 0)
        if not bw:
            continue
        pack = 0.0
        pack_gap = 0.0
        for sheet_name, count in (b.get("contents") or {}).items():
            pack += sheet_ev.get(sheet_name, 0.0) * count
            # A slot whose sheet the model does not describe is unpriced.
            pack_gap += sheet_gap.get(sheet_name, UNKNOWN_RARITY_PRIOR) * count
        ev += (bw / total_w) * pack
        gap += (bw / total_w) * pack_gap
    coverage = ev / (ev + gap) if ev + gap > 0 else 0.0
    return ev, coverage


def compute_box_ev(set_name: str, setcode_map: dict, session: requests.Session,
                   want_types: set, sku_names: dict | None = None,
                   sku_ids: dict | None = None) -> dict:
    """Return {box_type: {ev, ev_coverage, packs}} for the given set, using
    MTGJSON's set file (booster model, sealed product contents) and
    AllPricesToday (TCGplayer retail per card and finish). An estimate, see
    the UI note: cards under BOX_EV_BULK_THRESHOLD count as 0, box toppers are
    left out. sku_names and sku_ids give the priced SKU's name and TCGplayer
    product id per box type. Returns {} when the set, booster or price data
    isn't available, and leaves out a type whose coverage is too low."""
    code = setcode_map.get(norm(_clean_set_name(set_name))) or setcode_map.get(norm(set_name))
    if not code:
        return {}
    try:
        sdata = fetch_json(f"{MTGJSON_BASE}/{code}.json", session).get("data", {})
    except Exception as e:
        print(f"  ev: [{set_name}] set file {code}.json fetch failed: {e}", flush=True)
        return {}
    booster = sdata.get("booster") or {}
    if not booster:
        return {}
    # Shared AllPricesToday (one download per run, reused from the crack phase).
    try:
        allp = _get_allprices(session)
    except Exception as e:
        print(f"  ev: AllPrices download failed: {e}", flush=True)
        return {}

    def price_of(uuid, is_foil=False):
        # Foil sheets reference the same uuids; price the sheet's finish,
        # falling back to the others only when it has no price (etched
        # sheets are marked foil but MTGJSON prices them as 'etched').
        paper = (allp.get(uuid) or {}).get("paper", {})
        tcg = (paper.get("tcgplayer") or {}).get("retail") or {}
        return price_in_finish(tcg, finish_order(is_foil))[0]

    rarities = {c.get("uuid"): c.get("rarity") for c in sdata.get("cards") or []
                if isinstance(c, dict)}
    sealed = sdata.get("sealedProduct") or []

    out = {}
    for t in want_types:
        sku = (sku_names or {}).get(t, "")
        kind = box_kind(sku)
        if kind:
            # Use the booster model of the kind we priced: a Set Booster box
            # must not be valued with Draft Booster packs, or the reverse.
            cfg = booster.get(BOX_BOOSTER_KEY.get(kind, kind))
            if not cfg and kind == "draft":
                cfg = booster.get("default")
        else:
            bkey = BOX_BOOSTER_KEY.get(t)
            cfg = booster.get(bkey) if bkey else None
            # SKU name unknown: pre-2024 sets predate Play Boosters.
            if not cfg and t == "play":
                cfg = booster.get("draft") or booster.get("set")
        if not cfg:
            if kind:
                print(f"  ev: [{set_name}] no MTGJSON '{kind}' booster model for {sku}", flush=True)
            continue
        pack_ev, coverage = _pack_ev_from_booster(cfg, price_of, BOX_EV_BULK_THRESHOLD,
                                                  rarities.get)
        # Skip publishing a misleadingly low EV when much of the value is unpriced.
        if pack_ev is None or pack_ev <= 0 or coverage < BOX_EV_MIN_COVERAGE:
            if pack_ev is not None:
                print(f"  ev: [{set_name}] {t} skipped, coverage {coverage:.2f}", flush=True)
            continue
        # Multiply by the pack count of the SKU we actually priced.
        packs, src = box_pack_count(sealed, (sku_ids or {}).get(t), kind, sku, t)
        if src != "mtgjson":
            print(f"  ev: [{set_name}] {t}: no MTGJSON contents for {sku!r}, "
                  f"assuming {packs} packs", flush=True)
        out[t] = {"ev": round(pack_ev * packs, 2), "ev_coverage": round(coverage, 3),
                  "packs": packs}
    return out


# ---------- Card Kingdom sealed pricelist (free public API) ----------
# CK's HTML storefront is Cloudflare-challenged (403 to bots), but they serve a
# free, no-auth JSON pricelist of sealed product — the intended programmatic
# surface, and a known MTGJSON upstream. We use it as a real second vendor for
# the precon DECKS (re-enabling the cardkingdom column) and for the sealed BOXES
# we already track via TCGPlayer. Singles come from MTGJSON, so we skip CK's
# (66 MB) singles pricelist and fetch only the small (~0.5 MB) sealed one.
CK_SEALED_URL = "https://api.cardkingdom.com/api/sealed_pricelist"
CK_WEB_BASE = "https://www.cardkingdom.com"


def _ck_price(v) -> float | None:
    """CK prices arrive as strings like '54.99'; '0.00'/missing means none,
    and so does a value that is not a finite number ('Infinity', 'NaN')."""
    f = _finite(v)
    return f if f is not None and f > 0 else None


def _ck_qty(v) -> int:
    """CK stock count as a non negative int; anything unusable counts as 0."""
    f = _finite(v)
    return int(f) if f is not None and f > 0 else 0


def _classify_ck_box(name: str) -> str | None:
    """Map a Card Kingdom sealed product name to one of our 3 box types, or None.
    CK naming differs from TCGPlayer's (uses 'Booster Box', not 'Display')."""
    l = (name or "").lower()
    if ("case" in l or "pack" in l or "prerelease" in l or "fat pack" in l
            or "bundle" in l or "commander deck" in l or "sample" in l):
        return None
    if "collector booster box" in l:
        return "collector"
    if "jumpstart" in l and "box" in l:
        return "jumpstart"
    # Play / Draft / Set boosters, and the oldest sets' plain "Booster Box",
    # all map to our generic "play" booster box.
    if ("play booster box" in l or "draft booster box" in l
            or "set booster box" in l or "booster box" in l):
        return "play"
    return None


def _ck_deck_name(name: str) -> str:
    """Extract the deck's own name from a CK commander-deck product, e.g.
    'Bloomburrow Commander Deck - Animated Army' -> 'Animated Army'."""
    if " - " in name:
        return name.split(" - ", 1)[1]
    return re.sub(r".*commander decks?\s*", "", name, flags=re.I)


CK_ANNUAL_DECK_RE = re.compile(r"^commander (19|20)\d\d - ")
# Words that say nothing about which release a CK edition is ('Universes
# Beyond: Fallout', 'Final Fantasy Commander Decks', 'Marvel Super Heroes
# Eternal-Legal'), dropped before comparing an edition with our set name.
CK_EDITION_NOISE = {"commander", "decks", "deck", "universes", "beyond", "eternal", "legal"}


def ck_edition_matches_set(edition: str, deck_set: str) -> bool:
    """Does a CK edition name the same release as our deck set? Compares the
    distinctive words, so 'Universes Beyond: Fallout' matches 'Fallout
    Commander' and 'Commander 2021' matches 'Strixhaven: School of Mages
    (Commander 2021)', while 'Bloomburrow Commander Decks' does not match
    'Wilds of Eldraine' and 'Commander 2017' does not match 'Starter Commander
    Decks'. One side's words must all appear in the other's."""
    e = tokenize(edition) - CK_EDITION_NOISE
    d = tokenize(deck_set) - CK_EDITION_NOISE
    return bool(e and d) and (e <= d or d <= e)


def _index_ck_sealed(data: list) -> dict:
    """Pure indexer (no network) so it can be unit-tested. Splits CK sealed SKUs
    into a deck list and a box index keyed by normalized edition + box type."""
    decks_idx = []
    boxes_idx: dict = {}
    for item in data:
        name = item.get("name") or ""
        ed_norm = norm(item.get("edition") or "")
        retail = _ck_price(item.get("price_retail"))
        buy = _ck_price(item.get("price_buy"))
        qty = _ck_qty(item.get("qty_retail"))
        qty_buy = _ck_qty(item.get("qty_buying"))
        rel = (item.get("url") or "").lstrip("/")
        url = f"{CK_WEB_BASE}/{rel}" if rel else CK_WEB_BASE
        low = name.lower()
        # Most precons are '<Set> Commander Deck - <Deck>', but CK names the
        # annual products 'Commander 2021 - <Deck>' (2013 to 2021).
        if "commander deck" in low or CK_ANNUAL_DECK_RE.match(low):
            # Skip multi-deck SKUs (displays/cases/"set of N") — not a single deck.
            if any(k in low for k in ("display", "case", "set of", "bundle")):
                continue
            # Flag premium re-printings so matching can deprioritize them.
            premium = any(k in low for k in ("collector", "game edition", "gift", "starter kit"))
            decks_idx.append({
                "name_norm": norm(_ck_deck_name(name)), "edition_norm": ed_norm,
                "edition": item.get("edition") or "",
                "retail": retail, "qty": qty, "buy": buy, "qty_buy": qty_buy,
                "url": url, "raw": name, "premium": premium,
            })
            continue
        t = _classify_ck_box(name)
        if not t:
            continue
        # Key by the booster kind on the SKU, not the coarse type: CK sells a
        # Draft and a Set Booster box for many sets, and the TCG row must be
        # compared with the same kind. A plain 'Booster Box' says no kind.
        t = box_kind(name) or ("booster" if t == "play" else t)
        cand = {"retail": retail, "qty": qty, "buy": buy, "qty_buy": qty_buy,
                "url": url, "name": name}
        slot = boxes_idx.setdefault(ed_norm, {})
        prev = slot.get(t)
        # Prefer a priced, in-stock candidate over an unpriced / out-of-stock one.
        better = (prev is None
                  or (prev.get("retail") is None and retail is not None)
                  or (retail is not None and (prev.get("qty") or 0) == 0 and (qty or 0) > 0))
        if better:
            slot[t] = cand
    return {"decks": decks_idx, "boxes": boxes_idx}


def fetch_ck_sealed(session: requests.Session) -> dict:
    """Fetch + index CK's sealed pricelist. Returns {"decks": [...], "boxes":
    {edition_norm: {type: {...}}}}. Cached; empty dict on failure (non-fatal)."""
    cached = getattr(fetch_ck_sealed, "_cache", None)
    if cached is not None:
        return cached
    try:
        data = fetch_json(CK_SEALED_URL, session).get("data", [])
    except Exception as e:
        print(f"  CK: sealed pricelist fetch failed (non-fatal): {e}", flush=True)
        fetch_ck_sealed._cache = {}
        return {}
    out = _index_ck_sealed(data)
    fetch_ck_sealed._cache = out
    print(f"  CK: {len(out['decks'])} commander-deck SKUs, "
          f"{len(out['boxes'])} sets with boxes", flush=True)
    return out


def _pick_ck(cands: list) -> dict | None:
    """Choose the best CK SKU among candidates: regular printing over premium
    (Collector's/Game Edition), in-stock over out-of-stock, then cheapest retail.
    Deterministic regardless of the API's list order."""
    if not cands:
        return None
    def key(c):
        return (
            1 if c.get("premium") else 0,
            0 if (c.get("qty") or 0) > 0 else 1,
            c["retail"] if c.get("retail") is not None else float("inf"),
            c.get("name_norm") or "",
        )
    return sorted(cands, key=key)[0]


def match_ck_decks(decks: list, ck_decks: list) -> dict:
    """Match each precon to a CK commander-deck SKU → vendor records keyed by
    deck id: {price, url, status, qty, buy}. Conservative to avoid false
    positives: requires the precon name to equal/contain the CK deck name AND
    the CK edition to name the same release (ck_edition_matches_set). Exact
    names beat partial ones. Unmatched decks get a CK search link with status
    'no-match'."""
    out = {}
    for d in decks:
        did = d["id"]
        dn = norm(d["name"])
        ds = norm(d.get("set", ""))
        search = ck_search_url(d["name"])
        cands = [c for c in ck_decks if dn and c["name_norm"]
                 and (dn == c["name_norm"] or dn in c["name_norm"] or c["name_norm"] in dn)]

        def set_ok(c):
            if c.get("edition"):
                return ck_edition_matches_set(c["edition"], d.get("set", ""))
            # Index built without raw editions: fall back to the old check.
            return bool(ds) and (ds in c["edition_norm"] or c["edition_norm"] in ds)
        # The release must always agree. Deck names repeat across releases
        # (Peace Offering, Draconic Domination), so a name match from another
        # edition is a different product, never a fallback.
        exact_set = [c for c in cands if dn == c["name_norm"] and set_ok(c)]
        set_match = [c for c in cands if set_ok(c)]
        chosen = _pick_ck(exact_set or set_match)
        if chosen:
            out[did] = {
                "price": chosen["retail"],
                "url": chosen["url"],
                "status": "ok" if chosen["retail"] is not None else "out-of-stock",
                "qty": chosen["qty"],
                "buy": chosen["buy"],
            }
        else:
            out[did] = {"price": None, "url": search, "status": "no-match"}
    n = sum(1 for v in out.values() if v.get("price") is not None)
    print(f"  CK: matched {n}/{len(decks)} decks to a live CK price", flush=True)
    return out


def cheapest_buyable(tcg: dict | None, ck: dict | None) -> tuple | None:
    """(vendor, price) of the cheapest price a buyer can act on today: the
    TCGplayer price, or Card Kingdom's when it has stock (an unknown stock
    count counts as in stock, as on the page). None when neither applies."""
    opts = []
    if (tcg or {}).get("price") is not None:
        opts.append(("tcgplayer", float(tcg["price"])))
    if (ck or {}).get("price") is not None and (ck.get("qty") is None or ck["qty"] > 0):
        opts.append(("cardkingdom", float(ck["price"])))
    return min(opts, key=lambda o: o[1]) if opts else None


def price_bundles(bundles: dict, tcg_results: dict, ck_results: dict) -> None:
    """Fill each bundle's savings in place, against buying every deck on its
    own at its cheapest buyable price (see cheapest_buyable).

    `components` records the deck, vendor and price behind every part of
    `individual_total`, so the page can show how the saving was worked out.
    A deck with no buyable price leaves the total and the saving unset, since
    counting it as $0 would understate the total."""
    for b in bundles.values():
        comps = []
        for did in b.get("deck_ids", []):
            pick = cheapest_buyable(tcg_results.get(did), ck_results.get(did))
            comps.append({"deck_id": did,
                          "vendor": pick[0] if pick else None,
                          "price": round(pick[1], 2) if pick else None})
        b["components"] = comps
        b["individual_basis"] = "cheapest_buyable"
        if comps and all(c["price"] is not None for c in comps):
            total = round(sum(c["price"] for c in comps), 2)
            raw = round(total - b["price"], 2)
            b["individual_total"] = total
            # Only a positive saving is reported (bundle cheaper than parts).
            b["savings"] = raw if raw > 0 else None
        else:
            b["individual_total"] = None
            b["savings"] = None
        sav = f"saves ${b['savings']:.2f}" if b["savings"] else "no saving"
        tot = f"${b['individual_total']:.2f}" if b["individual_total"] is not None else "n/a"
        print(f"  bundle [{b.get('set')}] ${b['price']:.2f} vs decks {tot}: {sav}", flush=True)


def _ck_box_for(ck_boxes: dict, set_name: str, box_type: str) -> dict | None:
    """Look up the CK box record for a (set, kind); `box_type` is the booster
    kind (play, draft, set, collector, jumpstart). CK box editions track the
    main set name (e.g. 'Bloomburrow'), but our box phase iterates the decks'
    set field which may carry 'Commander'/parentheticals ('Bloomburrow
    Commander'). Match the cleaned name first, then the raw name as fallback."""
    if not ck_boxes:
        return None
    key = norm(_clean_set_name(set_name))
    return (ck_boxes.get(key) or ck_boxes.get(norm(set_name)) or {}).get(box_type)


def fetch_box_prices(decks: list, session: requests.Session, groups: list,
                     ck_boxes: dict | None = None, guard: PhaseGuard | None = None,
                     group_cache: dict | None = None) -> dict:
    """For each unique set among the decks, find its main-set group and price
    the Play/Collector/Jumpstart boxes. Returns { set_name: [ {type, kind,
    label, name, price, price_low, price_source, url, ev} ] }. `kind` is the
    booster kind of the SKU priced (play, draft, set, collector, jumpstart);
    the Card Kingdom price and the EV model always use that same kind.
    `ev` is an expected value ESTIMATE of the singles in a box worth at least
    BOX_EV_BULK_THRESHOLD each, from MTGJSON's published booster model and the
    box's real pack count (`packs`); `ev_coverage` is the share of that value
    backed by real prices. All three are omitted when the model or prices
    aren't available.
    Resilient: sets with no boxes (Commander-only products) are simply omitted.
    `guard` ends the set loop early (run budget or repeated failures).
    `group_cache` holds groups phase 1 already fetched ({groupId: {products,
    prices, error}}); a group found there without an error is not fetched
    again.
    """
    guard = guard or PhaseGuard("boxes")
    sets = sorted({d.get("set", "") for d in decks if d.get("set")})
    setcode_map = _build_setcode_map(session)
    out: dict = {}
    for set_name in sets:
        g = find_main_set_group(groups, set_name)
        if not g:
            continue
        if not guard.ok():
            break
        cached = (group_cache or {}).get(g["groupId"])
        if cached and not cached.get("error"):
            prods, prices = cached["products"], cached["prices"]
        else:
            try:
                prods = fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/{g['groupId']}/products", session).get("results", [])
                prices = flatten_prices(fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/{g['groupId']}/prices", session).get("results", []))
            except Exception as e:
                print(f"  boxes: [{set_name}] fetch failed: {e}", flush=True)
                guard.failure(g["groupId"])
                time.sleep(0.3)  # stay polite even on the error path
                continue
            guard.success()
        # Collect ALL candidates per box type, then pick the first one that
        # actually has a usable price — so a same-type SKU with no price doesn't
        # shadow a priced one.
        candidates: dict = {}
        for p in prods:
            t = _classify_box(p.get("name") or "")
            if t:
                candidates.setdefault(t, []).append(p)
        rows = []
        for t, plist in candidates.items():
            if t == "play":
                # Stable choice across days (Play, then Draft, then Set), so
                # the row does not hop between products in API order.
                plist = sorted(plist, key=lambda p: PLAY_KIND_ORDER.index(box_kind(p.get("name")))
                               if box_kind(p.get("name")) in PLAY_KIND_ORDER else len(PLAY_KIND_ORDER))
            chosen = None
            for p in plist:
                row = prices.get(p["productId"]) or {}
                market = row.get("marketPrice")
                low = row.get("lowPrice")
                mid = row.get("midPrice")
                eff = market if market is not None else (low if low is not None else mid)
                if eff is not None:
                    src = "Market" if market is not None else ("Low" if low is not None else "Mid")
                    chosen = (p, eff, low, src)
                    break
            if not chosen:
                continue
            p, eff, low, src = chosen
            pid = p["productId"]
            kind = box_kind(p.get("name")) or t
            rows.append({
                "_pid": pid,
                "type": t,
                "kind": kind,
                "label": BOX_KIND_LABELS.get(kind) or BOX_TYPE_LABELS[t],
                "name": p.get("name"),
                "price": float(eff),
                "price_low": float(low) if low is not None else None,
                "price_source": src,
                "url": tcg_product_url(pid, p.get("name", "")),
            })
        if rows:
            # Expected-value estimate per box type (best-effort).
            try:
                ev_map = compute_box_ev(set_name, setcode_map, session,
                                        {r["type"] for r in rows},
                                        {r["type"]: r.get("name") or "" for r in rows},
                                        {r["type"]: r["_pid"] for r in rows})
            except Exception as e:
                print(f"  ev: [{set_name}] failed: {e}", flush=True)
                ev_map = {}
            for r in rows:
                r.pop("_pid", None)
                if r["type"] in ev_map:
                    r.update(ev_map[r["type"]])
                # Card Kingdom as a second box vendor (retail + stock + buylist).
                ck = _ck_box_for(ck_boxes, set_name, r["kind"])
                if ck and ck.get("retail") is not None:
                    r["ck_price"] = ck["retail"]
                    r["ck_qty"] = ck.get("qty") or 0
                    r["ck_url"] = ck.get("url")
                    r["ck_buy"] = ck.get("buy")
            # Stable order: play, collector, jumpstart.
            order = {"play": 0, "collector": 1, "jumpstart": 2}
            rows.sort(key=lambda r: order.get(r["type"], 9))
            out[set_name] = rows
        time.sleep(0.3 + random.uniform(0, 0.2))  # jittered politeness
    n_ev = sum(1 for rows in out.values() for r in rows if r.get("ev") is not None)
    print(f"  boxes: {len(out)} sets with sealed boxes ({n_ev} box EVs computed)", flush=True)
    return out


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

def _write_atomic(path: Path, text: str) -> None:
    """Write via a .tmp file and rename, so a crash never leaves half a file."""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _parse_utc(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def carry_forward(results: dict, previous: dict, keys: list, prev_updated_at,
                  now: datetime | None = None) -> list:
    """Fill keys missing from `results` (crack entries by deck id, box rows by
    set) from the previous prices.json, when this run's phase was cut short
    or failed. Each carried value gets carried_from, the time of the run that
    fetched it, and is dropped once that is over CARRY_MAX_DAYS old, so a
    phase that keeps failing still shows up in the CI coverage gate."""
    now = now or datetime.now(timezone.utc)
    carried = []
    for key in keys:
        if key in results or not isinstance(previous, dict) or key not in previous:
            continue
        val = previous[key]
        first = val[0] if isinstance(val, list) and val else val
        if not isinstance(first, dict):
            continue
        src = first.get("carried_from") or prev_updated_at
        fetched = _parse_utc(src) if src else None
        if fetched is None or now - fetched > timedelta(days=CARRY_MAX_DAYS):
            continue
        if isinstance(val, list):
            results[key] = [dict(r, carried_from=src) for r in val if isinstance(r, dict)]
        else:
            results[key] = dict(val, carried_from=src)
        carried.append(key)
    return carried


TCGCSV_LAST_UPDATED_URL = "https://tcgcsv.com/last-updated.txt"
MTGJSON_META_URL = f"{MTGJSON_BASE}/Meta.json"


def parse_tcgcsv_updated(text) -> str | None:
    """TCGCSV's last-updated.txt ('2026-10-07T20:06:09+0000') as an ISO UTC
    timestamp with offset ('2026-10-07T20:06:09+00:00'), or None."""
    raw = str(text or "").strip()
    for parse in (lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z"),
                  datetime.fromisoformat):
        try:
            dt = parse(raw)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return None


def fetch_upstream_dates(session: requests.Session) -> dict:
    """{"tcgcsv": ISO publish time of the TCGCSV snapshot, "mtgjson": MTGJSON
    build date (Meta.json meta.date)}, read once at the start of a run. A feed
    that cannot be read, or answers in an unexpected shape, gives None."""
    out: dict = {"tcgcsv": None, "mtgjson": None}
    try:
        out["tcgcsv"] = parse_tcgcsv_updated(
            _get_body(TCGCSV_LAST_UPDATED_URL, session).decode("utf-8", "replace"))
    except Exception as e:
        print(f"  upstream: could not read {TCGCSV_LAST_UPDATED_URL}: {e}", flush=True)
    try:
        meta = fetch_json(MTGJSON_META_URL, session)
        d = ((meta or {}).get("meta") or {}).get("date") if isinstance(meta, dict) else None
        out["mtgjson"] = d if isinstance(d, str) and _ISO_DATE.match(d) else None
    except Exception as e:
        print(f"  upstream: could not read {MTGJSON_META_URL}: {e}", flush=True)
    print(f"  upstream: TCGCSV {out['tcgcsv']}, MTGJSON {out['mtgjson']}", flush=True)
    return out


def merge_index_decks(card_index: dict, prev_cards: dict, deck_ids: list) -> None:
    """Add to `card_index`, in place, the cards the previous cards_index.json
    listed for `deck_ids` (decks whose crack values were carried forward),
    so the index still lists every deck whose chase cards the page shows."""
    for did in deck_ids:
        for key, e in prev_cards.items():
            if not isinstance(e, dict) or did not in (e.get("decks") or []):
                continue
            entry = card_index.setdefault(key, {"name": e.get("name") or key, "decks": []})
            if did not in entry["decks"]:
                entry["decks"].append(did)


def fetch_ck_phase(decks: list) -> tuple[dict, dict]:
    """Phase 2: (the indexed CK sealed pricelist, a CK record per deck). When
    the pricelist cannot be read, every deck gets status 'unavailable'."""
    ck_sealed = {}
    ck_results = {}
    try:
        if budget_left() <= 0:
            raise TimeoutError("run time budget spent")
        ck_session = make_session(TCGCSV_UA)
        ck_sealed = fetch_ck_sealed(ck_session)
        # No deck SKUs means the pricelist failed to load (fetch_ck_sealed
        # returns {} then), not that every deck sold out: leave ck_results
        # empty so each deck is 'unavailable' below, which history treats as
        # unknown instead of removing every Card Kingdom price.
        if ck_sealed.get("decks"):
            ck_results = match_ck_decks(decks, ck_sealed["decks"])
        else:
            print("  CK: no deck SKUs loaded; Card Kingdom marked unavailable", flush=True)
    except Exception as e:
        print(f"Card Kingdom phase failed (non-fatal): {e}", flush=True)
    if not ck_results:
        # Keep a per-deck record (search link) so the frontend stays happy.
        ck_results = {
            deck["id"]: {
                "price": None,
                "url": ck_search_url(deck["name"]),
                "status": "unavailable",
            }
            for deck in decks
        }
    return ck_sealed, ck_results


def main() -> None:
    start_run_clock()
    decks_path = Path(__file__).parent / "decks.json"
    out_path = Path(__file__).parent / "prices.json"
    history_path = Path(__file__).parent / "prices_history.json"
    decks = json.loads(decks_path.read_text())
    print(f"Loaded {len(decks)} decks\n", flush=True)
    # Fail before any fetching if the history cannot be read (load_history
    # exits non zero); a fresh history would silently erase 90 days of data.
    load_history(history_path)
    previous: dict = {}
    if out_path.exists():
        try:
            previous = loads_finite(out_path.read_text())
        except (json.JSONDecodeError, OSError):
            previous = {}
    if not isinstance(previous, dict):
        previous = {}

    upstream = fetch_upstream_dates(make_session(TCGCSV_UA))

    print("\n=== Phase 1: TCGCSV → TCGPlayer ===", flush=True)
    tcgcsv_shared: dict = {}
    tcg_results, bundles = fetch_all_tcgcsv(decks, tcgcsv_shared)

    print("\n=== Phase 2: Card Kingdom (free sealed pricelist API) ===", flush=True)
    ck_sealed, ck_results = fetch_ck_phase(decks)

    price_bundles(bundles, tcg_results, ck_results)

    print("\n=== Phase 3: Crack value (MTGJSON dual-vendor, NM) ===", flush=True)
    crack_results = {}
    crack_guard = PhaseGuard("crack")
    try:
        crack_results = fetch_crack_values(decks, make_session(TCGCSV_UA), crack_guard)
    except Exception as e:
        print(f"Crack value phase failed (non-fatal): {e}", flush=True)
        traceback.print_exc()
        crack_guard.trip(f"failed: {e}")

    print("\n=== Phase 4: Sealed box prices (per set) ===", flush=True)
    box_results = {}
    box_guard = PhaseGuard("boxes")
    try:
        if not box_guard.ok():
            raise TimeoutError("run time budget spent")
        box_session = make_session(TCGCSV_UA)
        # Phase 1's groups list and fetched groups, so no group is downloaded twice.
        box_groups = tcgcsv_shared.get("groups") or load_magic_groups(box_session)
        box_results = fetch_box_prices(decks, box_session, box_groups,
                                       ck_boxes=ck_sealed.get("boxes"), guard=box_guard,
                                       group_cache=tcgcsv_shared.get("group_cache"))
    except Exception as e:
        print(f"Box price phase failed (non-fatal): {e}", flush=True)
        traceback.print_exc()
        box_guard.trip(f"failed: {e}")

    # A phase cut short keeps yesterday's values for what it did not reach,
    # so a slow optional source does not block today's TCG and CK prices.
    crack_carried: list = []
    if crack_guard.cut_short:
        got = carry_forward(crack_results, previous.get("crack"),
                            [d["id"] for d in decks], previous.get("updated_at"))
        print(f"Crack: carried {len(got)} decks from the previous run ({crack_guard.cut_short})", flush=True)
    else:
        # A phase that ran to the end can still miss a deck whose decklist
        # failed to download: keep its previous values (marked carried_from)
        # rather than drop its singles line from the page.
        crack_carried = carry_forward(crack_results, previous.get("crack"),
                                      getattr(fetch_crack_values, "failed_ids", []),
                                      previous.get("updated_at"))
        if crack_carried:
            print(f"Crack: carried {', '.join(crack_carried)} from the previous run "
                  f"(decklist fetch failed)", flush=True)
    if box_guard.cut_short:
        got = carry_forward(box_results, previous.get("boxes"),
                            sorted({d.get("set", "") for d in decks if d.get("set")}),
                            previous.get("updated_at"))
        # A carried EV without ev_coverage predates the bulk threshold and the
        # real pack counts, and can overstate a box several times: drop it.
        for set_name in got:
            for r in box_results[set_name]:
                if "ev_coverage" not in r:
                    r.pop("ev", None)
        print(f"Boxes: carried {len(got)} sets from the previous run ({box_guard.cut_short})", flush=True)

    # --- Coverage gate: never let a degraded run clobber good data ----------
    tcg_hits = sum(1 for v in tcg_results.values() if v.get("price") is not None)
    min_hits = min_tcg_hits(len(decks))
    healthy = tcg_hits >= min_hits
    if not healthy and out_path.exists():
        raise SystemExit(
            f"Refusing to overwrite prices.json: only {tcg_hits} TCG hits "
            f"(min {min_hits} of {len(decks)} decks), likely a degraded scrape.")

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "deck_count": len(decks),
        "vendors": {
            "cardkingdom": ck_results,
            "tcgplayer": tcg_results,
        },
        "bundles": bundles,
        "crack": crack_results,
        "boxes": box_results,
        # How boxes[][].ev is computed, so a reader knows what it counts.
        "box_ev_basis": {
            "prices": "tcgplayer_retail",
            "bulk_threshold": BOX_EV_BULK_THRESHOLD,
            "bulk_value": 0,
            "min_coverage": BOX_EV_MIN_COVERAGE,
            "box_toppers": False,
        },
        # When each upstream feed this run read was published, so the CI
        # freshness gate checks the snapshot actually used.
        "upstream": upstream,
    }
    n_links = enforce_vendor_hosts(output, decks)
    if n_links:
        print(f"Links: replaced {n_links} off site vendor links with search links", flush=True)
    # Atomic write: never leave a half-written prices.json on crash/disk-full.
    # allow_nan=False: a browser cannot parse NaN or Infinity, so a value
    # that slipped past the upstream checks fails the run instead.
    _write_atomic(out_path, json.dumps(output, indent=2, allow_nan=False))

    # Reverse card->decks index (F2: "cheapest precon containing card X").
    # Kept in its own file so prices.json stays lean. Only emitted on a healthy
    # run, and never shrink a good index to a much smaller one (partial crack).
    # A crack phase that was cut short saw only some decks: keep the old index.
    # The size check counts only old index cards that list a deck still in
    # decks.json, so removing decks from the catalog never blocks the index.
    card_index = getattr(fetch_crack_values, "card_index", None)
    if card_index and healthy and not crack_guard.cut_short:
        idx_path = Path(__file__).parent / "cards_index.json"
        prev_cards: dict = {}
        if idx_path.exists():
            try:
                prev_cards = json.loads(idx_path.read_text()).get("cards", {})
            except (json.JSONDecodeError, OSError, AttributeError):
                prev_cards = {}
        if not isinstance(prev_cards, dict):
            prev_cards = {}
        merge_index_decks(card_index, prev_cards, crack_carried)
        live_ids = {d["id"] for d in decks}
        prev_n = sum(1 for e in prev_cards.values()
                     if isinstance(e, dict) and live_ids & set(e.get("decks") or []))
        if len(card_index) >= prev_n * 0.5:
            _write_atomic(idx_path, json.dumps({
                "updated_at": output["updated_at"],
                "cards": card_index,
            }, separators=(",", ":"), allow_nan=False))
            print(f"Cards idx: {len(card_index)} unique cards across decks", flush=True)
        else:
            print(f"Skipping cards_index: {len(card_index)} < half of existing {prev_n}", flush=True)

    # History is part of the same process: only record on a healthy run so a
    # partial scrape can't permanently pollute the rolling series.
    if healthy:
        update_history(decks, tcg_results, box_results, ck_results, history_path,
                       previous=previous)
    else:
        print("Skipping history update (degraded run).", flush=True)

    ck_hits = sum(1 for v in ck_results.values() if v.get("price") is not None)
    rejected = sum(1 for v in tcg_results.values() if v.get("status") == "tcgcsv-likely-single")

    print(f"\nWrote {out_path}")
    print(f"TCGPlayer: {tcg_hits}/{len(decks)} hits  (rejected {rejected} as likely singles)")
    print(f"CK:        {ck_hits}/{len(decks)} hits")
    print(f"Bundles:   {len(bundles)} sets")
    print(f"Crack val: {len(crack_results)} decks priced")
    print(f"Boxes:     {len(box_results)} sets with sealed boxes")


# Box history entries written before rows recorded their booster kind carry
# no "kind". This table, read off the git history of prices.json, says which
# kind each 'play' series priced on which dates: (first date of the next
# period or None, kind). Sets not listed priced Play Booster boxes throughout.
# On 2026-06-24 eight sets switched from a Set Booster to a Draft Booster
# box under the same key, which drew fake 20% to 44% drops on their charts.
_SET_THEN_DRAFT = (("2026-06-24", "set"), (None, "draft"))
LEGACY_PLAY_BOX_KINDS = {
    "Adventures in the Forgotten Realms": _SET_THEN_DRAFT,
    "Innistrad: Crimson Vow": _SET_THEN_DRAFT,
    "Innistrad: Midnight Hunt": _SET_THEN_DRAFT,
    "Kamigawa: Neon Dynasty": _SET_THEN_DRAFT,
    "March of the Machine": _SET_THEN_DRAFT,
    "Phyrexia: All Will Be One": _SET_THEN_DRAFT,
    "Streets of New Capenna": _SET_THEN_DRAFT,
    "The Brothers' War": _SET_THEN_DRAFT,
    "Strixhaven: School of Mages (Commander 2021)": ((None, "set"),),
    "The Lost Caverns of Ixalan": ((None, "draft"),),
    "Wilds of Eldraine": ((None, "draft"),),
    "Commander Legends: Battle for Baldur's Gate": ((None, "draft"),),
    "Commander Masters": ((None, "draft"),),
    "Dominaria United": ((None, "draft"),),
    "The Lord of the Rings: Tales of Middle-earth": ((None, "draft"),),
}


def box_key_parts(key: str) -> tuple:
    """(set, type, archived kind or None) of a box history key: the live key
    '<set>::<type>' or the archive key '<set>::<type>@<kind>'. Both belong to
    <set>, for pruning and the window's carry in alike."""
    set_name, _, rest = key.partition("::")
    box_type, at, kind = rest.partition("@")
    return set_name, box_type, (kind if at else None)


def _legacy_box_kind(key: str, date: str) -> str:
    """Kind of a box history entry recorded before kinds were stored."""
    set_name, box_type, _ = box_key_parts(key)
    if box_type != "play":
        return box_type
    for until, kind in LEGACY_PLAY_BOX_KINDS.get(set_name, ((None, "play"),)):
        if until is None or date < until:
            return kind
    return "play"


def _merge_box_series(a: list, b: list) -> list:
    """Union of two box series by date (b wins on a shared date), sorted."""
    by_date = {e["date"]: e for e in a}
    by_date.update({e["date"]: e for e in b})
    return [by_date[d] for d in sorted(by_date)]


def normalize_box_history(boxes_history: dict) -> dict:
    """Make every box series describe one product. Idempotent.

    Entries without a "kind" get one from LEGACY_PLAY_BOX_KINDS. The live key
    '<set>::<type>' (the one the page charts) keeps only entries of its latest
    kind; entries of any other kind move to '<set>::<type>@<kind>', where they
    wait in case that kind is priced again. Nothing is deleted."""
    for key in [k for k in boxes_history if box_key_parts(k)[2] is None]:
        series = boxes_history.get(key)
        if not isinstance(series, list) or not series:
            continue
        for e in series:
            if isinstance(e, dict) and "kind" not in e and "date" in e:
                e["kind"] = _legacy_box_kind(key, e["date"])
        current = series[-1].get("kind")
        boxes_history[key] = [e for e in series if e.get("kind") == current]
        for kind in {e.get("kind") for e in series} - {current}:
            moved = [e for e in series if e.get("kind") == kind]
            arch = f"{key}@{kind}"
            boxes_history[arch] = _merge_box_series(boxes_history.get(arch, []), moved)
    return boxes_history


def _box_series_for_kind(boxes_history: dict, key: str, kind: str) -> list:
    """The live series for `key`, switched to `kind` if the row changed kind:
    the old kind's entries are archived and any archived entries of the new
    kind are restored, so the chart never joins two products."""
    series = boxes_history.get(key, [])
    current = series[-1].get("kind") if series else None
    if series and current != kind:
        arch = f"{key}@{current}"
        boxes_history[arch] = _merge_box_series(boxes_history.get(arch, []), series)
        series = boxes_history.pop(f"{key}@{kind}", [])
    return series


# ---- Deck history: loading, one off migrations, recording, trimming ----

HISTORY_VENDORS = ("tcg", "ck")
# A recorded price under this fraction of the same series' median is taken
# to be a wrong product match (see purge_bad_points).
HISTORY_OUTLIER_RATIO = 0.4

# Card Kingdom keeps a retail price on sold out SKUs, and until 2026-10 the
# history recorded it as if it could be bought, which drew flat 'best' lines
# at prices nobody could pay. Read off the git history of prices.json: per
# deck, inclusive MMDD ranges in 2026 of the UTC days whose last run had a CK
# price with qty 0 (a day with no run keeps the day before's state). Runs
# recorded CK stock from CK_SOLD_OUT_FROM on, so every day from then to
# CK_SOLD_OUT_THROUGH not in a deck's ranges had it in stock. A one off
# migration (backfill_ck_sold_out) rebuilds each listed deck's series on
# those days from the prices in force, without 'ck' on sold out days. The
# table was read up to the run of 2026-10-08 05:06 UTC. The scraper
# before this change kept writing sold out prices until the change is
# deployed, so the migration also reads the stock in the previous run's
# prices.json for that run's date, and warns about any later date it cannot
# check (regenerate the table up to the deploy date and give the migration a
# new name to cover those). The table can go once its dates have aged out of
# the window (after 2027-01-07).
CK_SOLD_OUT_FROM = "2026-06-20"
CK_SOLD_OUT_THROUGH = "2026-10-08"
CK_SOLD_OUT_2026 = {
    "abzan-armor": "0713-0721 0724-0803 0806 0809-0821 0824-0825 0828-0908 0929-1008",
    "ahoy-mateys": "0622-0625 0708-0710 0712-0726 0729-0731 0810-0827 0831-0916 0920-1008",
    "animated-army": "0620-0722 0801-0820 0822-0830 0902-0908 0910-0915 0919-1008",
    "avengers-assemble": "0621 0623",
    "blame-game": "0620-0629 0712-0821 0823-0904 0906-0911 0913-1008",
    "blast-from-the-past": "0620-0629 0712-0713 0727-0824 0925-1008",
    "blood-rites": "0620-0625 0723-0812 0824-0916 0925-1008",
    "cabaretti-cacophony": "0804-0810",
    "call-for-backup": "0620-0622 0708-0723 0730-0813 0823-0908 0910-1004",
    "chaos-incarnate": "0909-0913 0924-1004",
    "corrupting-influence": "0629-0630 0730-0813",
    "coven-counters": "0912-0920 1001-1005",
    "creative-energy": "0709-0710 0718-0721 0729-0731 0819-0824",
    "dance-of-the-elements": "1001-1008",
    "deadly-disguise": "0627-0629 0730-0810 0812-0826 0831-0908 0914-1008",
    "death-toll": "0723-0731 0908 0925-1001 1004-1008",
    "deep-clue-sea": "0620-0702 0704-0731 0804-0810 0906-0908 0916-0923 0925-1008",
    "desert-bloom": "0804-0820 0831-0908",
    "doom-prevails": "0621 0623 0625-0626 0628",
    "dungeons-of-death": "0806-0810 0818-0901",
    "eldrazi-incursion": "0620-0623 0625-0706 0708-0723 0727-0731",
    "elven-council": "0620-0623 0730-0806 0809-0908 0919-0920 0925",
    "endless-punishment": "0620-0720 0722-0723 0824-0826 0831-0903 0910 0912-0916 0924-1004",
    "enduring-enchantments": "0722-0804 0808-0817 0822-0920 0922-1008",
    "explorers-of-the-deep": "0625 0703-0710 0712-0812 0814-0916 0918-1008",
    "family-matters": "0620-0707 1001-1008",
    "food-and-fellowship": "0620-0622 0709-0710 0718-0723 0725-0810 0812-0825 0913",
    "grand-larceny": "0620-0623 0625-0626 0702 0704-0713 0817-0909",
    "grave-danger": "0912-1004",
    "graveyard-overdrive": "0620-0629 0702-0723 0727 0729-0731 0804-0830 0917-0923 0925-1008",
    "growing-threat": "0620-0622 0706-0723 0725-0812",
    "hail-caesar": "0620-0623 0625-0630 0702-0723 0725-0804 0806-0904 0906-0914",
    "jeskai-striker": "0910 0925-1008",
    "jump-scare": "0621-0622 0706-0723 0829-0908 0912-0916 0929-1001 1005-1008",
    "legends-legacy": "0623-0702 0709-0713 0904-0911 0916-0925 0930-1001 1003-1008",
    "mardu-surge": "0724-0727 0729-0821 0823-0831 0904-0908 0914-0916 0925-1008",
    "masters-of-evil": "0620-0629 0701-0713 0719-0723 0726-0824",
    "miracle-worker": "0629 0705-0713 0716-0731 0804-0817 0906-0920 1006-1008",
    "mishras-burnished-banner": "0620-0804 0816-0817 0916-0925",
    "most-wanted": "0704-0715 0814-0908 0917-0921 0930-1008",
    "mutant-menace": "0625-0629 0707-0714 0716-0723 0726-0803 0807-0817 0822-0902",
    "necron-dynasties": "1006-1008",
    "painbow": "0730-0810",
    "paradox-power": "0620-0622 0704-0713 0724-0731 0813-0824",
    "peace-offering-blb": "0620-0629 0701-0714 0716-0720 0810-0813 0816-0817 0820-0907 0909-0928 1001-1008",
    "planeswalker-party": "0620-0629 0714-0817 0826-0827 0901-0907 0909-0920 0929-1008",
    "revenant-recon": "0625-0629 0730-0826 0831-0910 0916-1008",
    "riders-of-rohan": "0620-0623 0704-0714 0727-0803 0808-0824",
    "science": "0729-0804 0808-0810 0813-0904",
    "scrappy-survivors": "0620-0623 0625-0629 0701-0708 0710 0712-0723 0725-0904",
    "silverquill-influence": "0916-0920",
    "squirreled-away": "0620-0629 0701-0722 0730-0812 0815-0824 0827-0830 0901-0915 0925-0928 0930-1008",
    "sultai-arisen": "0620-0705 0707-0908 0930-1008",
    "temur-roar": "0620-0713 0715-0723 0725-0804 0806-0908 0911-0916 0918-0928 0930-1008",
    "the-hosts-of-mordor": "0620-0623 0629-0723 0725-0825 0904-0907 0909-0913 0920-0921 0926-0927 0930-1005 1007-1008",
    "timey-wimey": "0620-0622 0628-0629 0706-0714 0718-0731 0802-0824 0902 0904-0907 0917-0923 1001-1008",
    "tinker-time": "0620-0622 0624-0710 0717-0720 0822-0824 0827-0910 0921-1004",
    "token-triumph": "0906-0908 0917-0920 0924-1004",
    "tramplesaurus-rex": "1007-1008",
    "tricky-terrain": "0620-0629 0713-0723 0730-0731 0825-0830 0910-0911 0913 0917-0923 1001-1004 1006-1008",
    "turtle-power": "0720-0727",
    "urzas-iron-alliance": "0620-0626 0712 0714-0716 0720-0804 0912-0925",
    "veloci-ramp-tor": "0620-0625 0702-0716 0718-0719 0721-0803 0805-0812 0815-0827 0831-0916 0923-1008",
    "virtue-and-valor": "0620-0629 0702-0710 0712-0713 0718-0722 0727-0819 0824 0826-0913 0915-1008",
    "witherbloom-pestilence": "0904-0914 0919-0920 0926-1001 1004-1005",
    "wretched-ranks": "1007-1008",
}

# Prices recorded from a product that a later matching fix rejected: deck id
# -> {vendor: first date its values are trusted}. peace-offering-woe's Card
# Kingdom price came from the Bloomburrow deck of the same name.
KNOWN_BAD_HISTORY = {
    "peace-offering-woe": {"ck": "2026-10-09"},
}


def load_history(path: Path) -> dict:
    """Read prices_history.json, or start empty when it does not exist yet.

    A file that exists but cannot be parsed, or lacks the 'decks' map, ends
    the run with a non zero exit: carrying on would replace 90 days of
    history with one day. NaN and Infinity are read as None (dropped later)."""
    if not path.exists():
        return {"decks": {}, "boxes": {}}
    try:
        data = loads_finite(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        raise SystemExit(
            f"ERROR: {path.name} cannot be read ({e}). Refusing to start a fresh "
            f"history; restore the file from git and run again.")
    if (not isinstance(data, dict) or not isinstance(data.get("decks"), dict)
            or not isinstance(data.get("boxes", {}), dict)):
        raise SystemExit(
            f"ERROR: {path.name} does not have the expected shape (an object with "
            f"a 'decks' map). Refusing to start a fresh history; restore it from git.")
    data.setdefault("boxes", {})
    return data


def _deck_entry(e) -> dict | None:
    """A clean deck history entry: date, the finite positive vendor prices,
    and best = the lowest of them. An entry with no price keys at all is the
    'nothing buyable' marker (see record_deck_entry) and stays {date}. None
    when an entry had prices and none of them is usable."""
    if not isinstance(e, dict) or not _ISO_DATE.match(str(e.get("date", ""))):
        return None
    out = {"date": e["date"]}
    if not any(k in e for k in (*HISTORY_VENDORS, "best")):
        return out
    for k in HISTORY_VENDORS:
        v = _finite(e.get(k))
        if v is not None and v > 0:
            out[k] = round(v, 2)
    vals = [out[k] for k in HISTORY_VENDORS if k in out]
    if not vals:
        return None
    out["best"] = round(min(vals), 2)
    return out


def clean_deck_series(series) -> list:
    """Sort by date, one entry per date (the later one wins), best recomputed,
    and an entry dropped when its prices equal the one before it (history
    only records changes) or when it is a marker opening the series.
    Idempotent."""
    by_date: dict = {}
    for e in series if isinstance(series, list) else []:
        n = _deck_entry(e)
        if n:
            by_date[n["date"]] = n
    out: list = []
    for d in sorted(by_date):
        n = by_date[d]
        if not out and "best" not in n:
            continue  # a marker with nothing before it says nothing
        if out and all(out[-1].get(k) == n.get(k) for k in HISTORY_VENDORS):
            continue
        out.append(n)
    return out


def clean_box_series(series, dedupe: bool = True) -> list:
    """Box counterpart of clean_deck_series: entries {date, price, kind}.
    dedupe=False keeps repeated prices, for entries whose kind is not known
    yet (two kinds can share a price)."""
    by_date: dict = {}
    for e in series if isinstance(series, list) else []:
        if not isinstance(e, dict) or not _ISO_DATE.match(str(e.get("date", ""))):
            continue
        p = _finite(e.get("price"))
        if p is None or p <= 0:
            continue
        n = {"date": e["date"], "price": round(p, 2)}
        if isinstance(e.get("kind"), str):
            n["kind"] = e["kind"]
        by_date[n["date"]] = n
    out: list = []
    for d in sorted(by_date):
        n = by_date[d]
        if dedupe and out and out[-1]["price"] == n["price"] and out[-1].get("kind") == n.get("kind"):
            continue
        out.append(n)
    return out


def former_id_map(decks: list) -> dict:
    """former deck id -> current id, from each deck's optional formerIds. An
    id that is still a live deck is never treated as a former one."""
    current = {d["id"] for d in decks}
    out: dict = {}
    for d in decks:
        for fid in d.get("formerIds") or []:
            if isinstance(fid, str) and fid != d["id"] and fid not in current:
                out.setdefault(fid, d["id"])
    return out


def migrate_history_ids(decks_history: dict, decks: list) -> dict:
    """Move series of renamed decks to their current id (merged by date, the
    current id's entry winning a shared date) and drop series of deck ids
    that are neither current nor former, so renaming or deleting a deck in
    decks.json never leaves history the tests reject. Idempotent. Returns
    the former id map."""
    id_map = former_id_map(decks)
    for fid, did in id_map.items():
        if fid in decks_history:
            old = decks_history.pop(fid)
            decks_history[did] = clean_deck_series(old + decks_history.get(did, []))
            print(f"  history: moved {len(old)} entries from former id {fid} to {did}", flush=True)
    current = {d["id"] for d in decks}
    for did in sorted(k for k in decks_history if k not in current):
        n = len(decks_history.pop(did))
        print(f"  history: dropped {n} entries of {did}, no longer in decks.json", flush=True)
    return id_map


def _in_mmdd_ranges(date: str, spec: str, year: str = "2026") -> bool:
    if not date.startswith(year + "-"):
        return False
    md = date[5:7] + date[8:10]
    for part in spec.split():
        a, _, b = part.partition("-")
        if a <= md <= (b or a):
            return True
    return False


def _iso_days(first: str, last: str) -> list:
    """Every ISO date from `first` to `last`, inclusive."""
    d, end = datetime.fromisoformat(first).date(), datetime.fromisoformat(last).date()
    out = []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def previous_ck_stock(previous: dict | None, id_map: dict) -> tuple:
    """(date, {deck id: True when sold out, False when in stock}) from the
    prices.json the last run wrote, for decks Card Kingdom priced with a
    stock count. ("", {}) when it has no usable date or CK records."""
    prev = previous if isinstance(previous, dict) else {}
    date = str(prev.get("updated_at") or "")[:10]
    ck = (prev.get("vendors") or {}).get("cardkingdom")
    if not _ISO_DATE.match(date) or not isinstance(ck, dict):
        return "", {}
    stock = {}
    for key, rec in ck.items():
        if not isinstance(rec, dict) or _finite(rec.get("price")) is None:
            continue
        qty = _finite(rec.get("qty"))
        if qty is not None:
            stock[id_map.get(key, key)] = qty <= 0
    return date, stock


def backfill_ck_sold_out(decks_history: dict, id_map: dict,
                         previous: dict | None = None) -> tuple:
    """Rebuild the series of decks Card Kingdom had sold out on some known
    day, so that on every day whose stock is known, the prices in force hold
    'ck' only when it could be bought.

    Known days are CK_SOLD_OUT_FROM to CK_SOLD_OUT_THROUGH (sold out per
    CK_SOLD_OUT_2026, in stock otherwise), plus the previous run's date when
    that run's prices.json gives the deck's stock (it covers the last run of
    the old scraper, which can come after the table). Entries record changes,
    so fixing only entries dated inside a sold out range left the entry in
    force at the start of the range (often dated before it) counting a price
    nobody could pay, and stripped the price from the in stock days after it.
    Instead each known day gets the prices in force on it before the
    migration, minus 'ck' when sold out, and clean_deck_series folds the days
    back into changes. A day with nothing left is a 'nothing buyable' marker.
    Returns (sold out deck days that had a CK price in force, decks rebuilt)."""
    known = _iso_days(CK_SOLD_OUT_FROM, CK_SOLD_OUT_THROUGH)
    sold_by_deck = {id_map.get(k, k): {d for d in known if _in_mmdd_ranges(d, spec)}
                    for k, spec in CK_SOLD_OUT_2026.items()}
    prev_date, prev_stock = previous_ck_stock(previous, id_map)
    use_prev = bool(prev_date) and prev_date > CK_SOLD_OUT_THROUGH
    removed = rebuilt_decks = 0
    for did, series in list(decks_history.items()):
        sold = set(sold_by_deck.get(did, ()))
        days = list(known)
        if use_prev and did in prev_stock:
            days.append(prev_date)
            if prev_stock[did]:
                sold.add(prev_date)
        if not sold or not series:
            continue
        orig = sorted(series, key=lambda e: e["date"])
        rebuilt = [e for e in orig if e["date"] not in days]
        i, in_force = 0, None
        for day in sorted(days):
            while i < len(orig) and orig[i]["date"] <= day:
                in_force = orig[i]
                i += 1
            if in_force is None:
                continue  # before the series starts
            vals = {k: in_force[k] for k in HISTORY_VENDORS if k in in_force}
            if day in sold and "ck" in vals:
                del vals["ck"]
                removed += 1
            rebuilt.append({"date": day, **vals})
        decks_history[did] = clean_deck_series(rebuilt)
        rebuilt_decks += 1
    return removed, rebuilt_decks


def purge_bad_points(decks_history: dict, boxes_history: dict, id_map: dict) -> list:
    """Remove prices recorded from the wrong product before the matching
    fixes: everything KNOWN_BAD_HISTORY lists, then any deck vendor price or
    box price under HISTORY_OUTLIER_RATIO times its series' median, repeated
    until nothing more falls under (so a second call removes nothing).
    Returns (series key, date, field, value, median or None) per removal."""
    removed = []
    for key, rules in KNOWN_BAD_HISTORY.items():
        for e in decks_history.get(id_map.get(key, key)) or []:
            for vendor, trusted_from in rules.items():
                if vendor in e and e["date"] < trusted_from:
                    removed.append((id_map.get(key, key), e["date"], vendor, e.pop(vendor), None))
    for did, series in decks_history.items():
        while True:
            vals = [e[k] for e in series for k in HISTORY_VENDORS if k in e]
            if len(vals) < 2:
                break
            med = statistics.median(vals)
            hit = False
            for e in series:
                for k in HISTORY_VENDORS:
                    if k in e and e[k] < HISTORY_OUTLIER_RATIO * med:
                        removed.append((did, e["date"], k, e.pop(k), round(med, 2)))
                        hit = True
            if not hit:
                break
    for key, series in boxes_history.items():
        while len(series) >= 2:
            med = statistics.median(e["price"] for e in series)
            low = [e for e in series if e["price"] < HISTORY_OUTLIER_RATIO * med]
            if not low:
                break
            for e in low:
                removed.append((key, e["date"], "price", e["price"], round(med, 2)))
            series[:] = [e for e in series if e not in low]
    return removed


def run_history_migrations(history: dict, decks: list, previous: dict | None = None) -> None:
    """Bring a loaded history up to date in place. Safe to run on every load:
    the cleanup and id steps are idempotent, and each one off correction is
    recorded by name in history['migrations'] and never applied twice.
    `previous` is the prices.json the last run wrote, if any."""
    decks_history = {k: clean_deck_series(v) for k, v in history.get("decks", {}).items()}
    boxes_history = {k: clean_box_series(v, dedupe=False) for k, v in history.get("boxes", {}).items()}
    normalize_box_history(boxes_history)
    boxes_history = {k: clean_box_series(v) for k, v in boxes_history.items()}
    id_map = migrate_history_ids(decks_history, decks)
    # Box series of a set no longer in decks.json go the way of deleted
    # decks' series; an archive key '<set>::<type>@<kind>' belongs to <set>.
    tracked_sets = {d.get("set") for d in decks if d.get("set")}
    for key in sorted(k for k in boxes_history if box_key_parts(k)[0] not in tracked_sets):
        n = len(boxes_history.pop(key))
        print(f"  history: dropped {n} entries of box {key}, set no longer in decks.json", flush=True)
    applied = [m for m in history.get("migrations") or [] if isinstance(m, str)]

    if "ck_sold_out_2026_10" not in applied:
        n, n_decks = backfill_ck_sold_out(decks_history, id_map, previous)
        print(f"  history: rebuilt {n_decks} deck series on days of known Card Kingdom "
              f"stock; {n} sold out deck days no longer count a CK price", flush=True)
        prev_date = str((previous or {}).get("updated_at") or "")[:10]
        unchecked = sorted({e["date"] for series in decks_history.values() for e in series
                            if "ck" in e and e["date"] > CK_SOLD_OUT_THROUGH
                            and e["date"] != prev_date})
        if unchecked:
            # A GitHub Actions annotation, so it shows on the run summary.
            print(f"::warning::history: Card Kingdom stock unknown for {', '.join(unchecked)} "
                  f"(after CK_SOLD_OUT_THROUGH {CK_SOLD_OUT_THROUGH}); sold out prices "
                  f"recorded on those dates were kept", flush=True)
        applied.append("ck_sold_out_2026_10")
    if "bad_points_2026_10" not in applied:
        removed = purge_bad_points(decks_history, boxes_history, id_map)
        for key, date, field, value, med in removed:
            why = f"under {HISTORY_OUTLIER_RATIO} x median {med}" if med is not None else "known wrong product"
            print(f"  history: removed {key} {date} {field} {value} ({why})", flush=True)
        print(f"  history: removed {len(removed)} known bad points", flush=True)
        applied.append("bad_points_2026_10")

    history["decks"] = {k: s for k, s in ((k, clean_deck_series(v)) for k, v in decks_history.items()) if s}
    history["boxes"] = {k: v for k, v in boxes_history.items() if v}
    history["migrations"] = applied


# TCGplayer statuses that say a deck has no TCGplayer price today, as
# opposed to a group that failed to download, which says nothing.
TCG_NO_PRICE_STATUSES = ("tcgcsv-no-price", "tcgcsv-no-product-match",
                         "tcgcsv-no-group-match", "tcgcsv-likely-single")


def deck_vendor_state(did: str, tcg_results: dict, ck_results: dict) -> tuple[dict, set]:
    """Today's history values for a deck: ({vendor: price} observed today,
    {vendors known to have no price}). A vendor in neither (a fetch failed)
    is unknown, and the value in force carries on.
    - tcg is the TCGplayer Market price only. A Low or Mid fallback is a
      listing price, and splicing it in would make the Market series jump
      between two kinds of price, so such a day records no tcg (unknown).
    - ck counts only when in stock (the page's bestVendorPrice rule), so
      history best is always a price that could be paid."""
    vendors: dict = {}
    gone: set = set()
    tcg = tcg_results.get(did) or {}
    t = _finite(tcg.get("price"))
    if t is not None and t > 0:
        if (tcg.get("price_source") or "Market") == "Market":
            vendors["tcg"] = round(t, 2)
    elif tcg.get("status") in TCG_NO_PRICE_STATUSES:
        gone.add("tcg")
    ck = ck_results.get(did) or {}
    c = _finite(ck.get("price"))
    if c is not None and c > 0 and (ck.get("qty") is None or ck["qty"] > 0):
        vendors["ck"] = round(c, 2)
    elif (c is not None and c > 0) or ck.get("status") in ("no-match", "out-of-stock"):
        gone.add("ck")
    return vendors, gone


def record_deck_entry(series: list, today: str, vendors: dict, gone: set) -> list:
    """Return `series` with today's prices recorded.

    Today's values start from the entry in force (today's own entry on a
    same day rerun, else the latest one): vendors seen now replace their
    values, vendors known to have no price are removed, and vendors this run
    could not fetch keep the value in force. best is the minimum of what is
    left. An entry is added only when that differs from the entry before.
    When nothing is left (Card Kingdom sold out on a day TCGplayer has no
    price either), the entry is a bare {date} marker: no price could be paid
    from that date, so the last price is no longer in force. A marker is
    never the first entry of a series."""
    series = list(series)
    if series and series[-1]["date"] > today:
        return series  # clock went backwards; never write dates out of order
    if series and series[-1]["date"] == today:
        base = series.pop()
    else:
        base = series[-1] if series else {}
    merged = {k: base[k] for k in HISTORY_VENDORS if k in base}
    merged.update(vendors)
    for k in gone:
        merged.pop(k, None)
    prev = series[-1] if series else None
    if prev is None:
        if merged:
            series.append({"date": today, **merged, "best": round(min(merged.values()), 2)})
        return series
    if {k: prev[k] for k in HISTORY_VENDORS if k in prev} == merged:
        return series  # nothing changed against the entry in force
    if merged:
        series.append({"date": today, **merged, "best": round(min(merged.values()), 2)})
    else:
        series.append({"date": today})
    return series


def trim_to_window(series: list, cutoff: str, carry_in: bool = True) -> list:
    """Keep entries dated on or after `cutoff`. With carry_in, also keep the
    last entry before it when no entry falls on the cutoff itself: entries
    record changes, so that one holds the price in force when the window
    opens, and the window's low and high must include it."""
    keep = [e for e in series if e["date"] >= cutoff]
    if carry_in:
        before = [e for e in series if e["date"] < cutoff]
        if before and (not keep or keep[0]["date"] > cutoff):
            keep.insert(0, before[-1])
    return keep


def dump_history(history: dict) -> str:
    """Serialize with one deck or box series per line (sorted by key), so a
    merge conflict touches only the lines of the series that changed instead
    of the whole file. allow_nan=False refuses NaN and Infinity."""
    def one(v):
        return json.dumps(v, separators=(",", ":"), allow_nan=False)

    def block(m):
        if not m:
            return "{}"
        return "{\n" + ",\n".join(f"{one(k)}:{one(m[k])}" for k in sorted(m)) + "\n}"

    head = [f"{one(k)}:{one(v)}" for k, v in history.items() if k not in ("decks", "boxes")]
    tail = [f'"decks":{block(history.get("decks") or {})}',
            f'"boxes":{block(history.get("boxes") or {})}']
    return "{" + ",\n".join(head + tail) + "}\n"


def update_history(decks: list, tcg_results: dict,
                   box_results: dict | None = None, ck_results: dict | None = None,
                   history_path: Path | None = None, today: str | None = None,
                   previous: dict | None = None) -> None:
    """Record today's prices in prices_history.json and trim it to the last
    HISTORY_DAYS calendar days.

    File shape:
      { "updated_at", "window_days", "migrations": [one off corrections applied],
        "decks": { deck_id: [{date, tcg, ck, best} or {date}, ...] },
        "boxes": { "<set>::<type>": [{date, price, kind}, ...],
                   "<set>::<type>@<kind>": archived entries of another kind } }
    `tcg` is the TCGplayer Market price, `ck` is recorded only while Card
    Kingdom has stock, and `best` is the lowest price in the entry, so every
    chart, range and signal built on it uses prices that could be paid. A
    bare {date} entry means neither vendor had a price from that date (see
    record_deck_entry). An entry is added only when a price changes; the
    first entry may predate the window (see trim_to_window).
    """
    history_path = history_path or Path(__file__).parent / "prices_history.json"
    today = today or datetime.now(timezone.utc).date().isoformat()
    ck_results = ck_results or {}

    history = load_history(history_path)
    run_history_migrations(history, decks, previous)
    decks_history = history["decks"]
    boxes_history = history["boxes"]

    for deck in decks:
        did = deck["id"]
        vendors, gone = deck_vendor_state(did, tcg_results, ck_results)
        series = record_deck_entry(decks_history.get(did, []), today, vendors, gone)
        if series:
            decks_history[did] = series

    # ---- Box history: keyed "<set>::<type>", one {date, price, kind} series each ----
    for set_name, rows in (box_results or {}).items():
        for r in rows:
            price = _finite(r.get("price"))
            # Rows carried from an earlier run were not observed today, and a
            # Low or Mid fallback is a listing price: the box series is the
            # TCGplayer Market series, like the decks' tcg.
            if (price is None or price <= 0 or r.get("carried_from")
                    or (r.get("price_source") or "Market") != "Market"):
                continue
            key = f"{set_name}::{r.get('type')}"
            kind = r.get("kind") or r.get("type")
            series = _box_series_for_kind(boxes_history, key, kind)
            new_entry = {"date": today, "price": round(price, 2), "kind": kind}
            if series and series[-1].get("date") == today:
                series[-1].update(new_entry)  # merge (symmetry with deck branch)
            elif not (series and series[-1].get("date") > today):
                prev = series[-1] if series else None
                if not (prev and prev.get("price") == new_entry["price"]):
                    series.append(new_entry)
            boxes_history[key] = series

    # ---- Trim to the last HISTORY_DAYS calendar days ----
    cutoff = (datetime.fromisoformat(today) - timedelta(days=HISTORY_DAYS)).date().isoformat()
    for did in list(decks_history):
        decks_history[did] = trim_to_window(decks_history[did], cutoff)
    for key in list(boxes_history):
        # Archived kinds get no carry in, so they disappear once their last
        # entry leaves the window. (Sets no longer in decks.json were already
        # pruned by run_history_migrations.)
        live = box_key_parts(key)[2] is None
        series = trim_to_window(boxes_history[key], cutoff, carry_in=live)
        if series:
            boxes_history[key] = series
        else:
            del boxes_history[key]

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": HISTORY_DAYS,
        "migrations": history.get("migrations", []),
        "decks": decks_history,
        "boxes": boxes_history,
    }
    _write_atomic(history_path, dump_history(out))
    sample_lens = [len(v) for v in decks_history.values()]
    if sample_lens:
        print(f"History: {len(decks_history)} decks, max {max(sample_lens)} / median {sorted(sample_lens)[len(sample_lens)//2]} entries", flush=True)
    if boxes_history:
        print(f"History: {len(boxes_history)} box series tracked", flush=True)


if __name__ == "__main__":
    main()
