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
import random
import re
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TCGCSV_BASE = "https://tcgcsv.com/tcgplayer"
MAGIC_CATEGORY = 1
TIMEOUT = 30
TCGCSV_UA = "precon-tracker/1.5 (+https://github.com/CooknRice/precon-tracker)"
POLITE_SLEEP = 1.2
PRICE_FLOOR = 5.0  # USD; any "deck" priced below this is rejected as a single
HISTORY_DAYS = 90  # rolling window of price snapshots kept per deck
# Below this many TCGPlayer hits a run is treated as degraded: the scraper
# refuses to overwrite an existing good prices.json / pollute history.
MIN_TCG_HITS = 50

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

# Tokens to ignore when comparing set names. Common English fillers plus
# Magic-specific noise that appears in different positions across naming
# conventions (decks.json vs TCGCSV groups).
STOPWORDS = {
    "the", "of", "a", "and", "an", "in", "to", "with", "for", "on", "at",
}
# Treated as semi-noise: not required for match, but tracked for scoring.
SEMI_NOISE = {"commander", "kit", "deck", "decks", "precon", "starter"}


# -------------------------------------------------------------------------
# TCGCSV helpers
# -------------------------------------------------------------------------

def norm(s: str) -> str:
    """Lowercase + strip all non-alphanumeric. For fuzzy product-name match."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def tokenize(s: str) -> set:
    """Lowercase, split on non-alphanumeric, drop pure stopwords."""
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in STOPWORDS}


def make_session(user_agent: str) -> requests.Session:
    """Session with exponential-backoff retry on transient failures.

    Retries on 429 (rate limit) and 5xx, with backoff 1s, 2s, 4s. urllib3
    honours Retry-After headers automatically. Connection errors get the
    same treatment via `connect=` and `read=`.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    retry = Retry(
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


def fetch_tcgcsv_json(path: str, session: requests.Session) -> dict:
    url = f"{TCGCSV_BASE}/{path}"
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
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


def pick_group_for_set(groups: list, deck_set: str) -> dict | None:
    """Token-set group matching with distinctive-token fallback."""
    deck_tokens = tokenize(deck_set)
    if not deck_tokens:
        return None

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
    candidates = []
    for g in groups:
        name_tokens = tokenize(g.get("name") or "")
        if required.issubset(name_tokens):
            candidates.append((score_group(g), g))

    # Fallback: strict containment failed (often because decks.json set
    # name has more tokens than the canonical TCGCSV name, e.g. "Strixhaven:
    # School of Mages (Commander 2021)" vs "Commander 2021: Strixhaven").
    # Try the single longest distinctive token (>=5 chars).
    if not candidates:
        long_tokens = sorted([t for t in required if len(t) >= 5], key=len, reverse=True)
        for token in long_tokens:
            for g in groups:
                if token in (g.get("name") or "").lower():
                    candidates.append((score_group(g), g))
            if candidates:
                break

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


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
        #   - not a bundle (those are tracked separately)
        #   - not a kit-only (prefer real Deck SKU)
        #   - has any price (don't pick a price-less SKU when one is priced)
        #   - passes floor (single-card sanity check)
        #   - is precon-named (deck/kit/commander/bundle)
        #   - is exact name match
        #   - shorter name
        score = (
            not is_bundle,
            not is_kit_only,
            has_any_price,
            passes_floor,
            is_precon_named,
            is_exact,
            -len(pname),
        )
        candidates.append((score, p))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def find_bundle_in_products(products: list, set_name: str) -> dict | None:
    """Pick the most likely 'set bundle' product (case / set-of-N / bundle).

    Bundle products contain all precons in a set. Their names typically
    include keywords like 'Case', 'Bundle', 'Set of 5', 'Deck Set', etc.
    We exclude card sleeves, tokens, and other non-bundle accessories.
    """
    BUNDLE_KEYWORDS = ("commander deck case", "commander kit case", "deck case",
                       "decks bundle", "deck bundle", "deck set", "set of 5",
                       "set of 4", "all decks", "5-pack", "5 pack", "all 5", "all 4",
                       "complete set", "commander collection")
    EXCLUDE_KEYWORDS = ("token", "sleeve", "playmat", "binder", "deck box", "card box")

    candidates = []
    for p in products:
        name = (p.get("name") or "")
        nl = name.lower()
        if any(x in nl for x in EXCLUDE_KEYWORDS):
            continue
        # Must look like a multi-deck bundle, not a single deck.
        match_kw = next((k for k in BUNDLE_KEYWORDS if k in nl), None)
        if not match_kw:
            continue
        # Score: prefer "case" matches (canonical), then longer keyword matches.
        score = ("case" in nl, "bundle" in nl, len(match_kw), -len(name))
        candidates.append((score, p))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _bundle_claimed_count(name: str) -> int | None:
    """If a bundle name advertises how many decks it holds ('Set of 5',
    'All 4', '5-pack'), return that count so savings math can verify it
    matches the decks we actually have. None when not stated."""
    m = re.search(r"set of (\d+)|all (\d+)|(\d+)\s*[- ]?pack", (name or "").lower())
    if m:
        for g in m.groups():
            if g:
                return int(g)
    return None


def flatten_prices(price_results: list) -> dict:
    """Reduce TCGCSV price rows to one record per productId. Prefer 'Normal'."""
    by_pid: dict[int, dict] = {}
    for row in price_results:
        pid = row.get("productId")
        if pid is None:
            continue
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


def fetch_all_tcgcsv(decks: list) -> tuple[dict, dict]:
    """Returns (per_deck_results, bundles_by_set).

    bundles_by_set maps set name → bundle info (price/url/deck_ids/etc.)
    A bundle is a "Commander Deck Case" / "Set of N" SKU that contains
    every precon from the same set.
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

    def ensure_group(group: dict) -> dict:
        gid = group["groupId"]
        cached = group_cache.get(gid)
        # Reuse only SUCCESSFUL results. A cached error is NOT reused, so a
        # transient failure on one deck's pass doesn't permanently zero the
        # group for every later deck/bundle that needs it — they retry.
        if cached is not None and not cached.get("error"):
            return cached
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
        time.sleep(0.3 + random.uniform(0, 0.2))  # jittered politeness
        group_cache[gid] = result
        return result

    results: dict[str, dict] = {}
    for i, deck in enumerate(decks, 1):
        deck_id = deck["id"]
        deck_name = deck["name"]
        deck_set = deck.get("set", "")

        fallback_url = f"https://www.tcgplayer.com/search/magic/product?q={urllib.parse.quote(deck_name)}"
        base = {
            "price": None, "price_low": None,
            "url": fallback_url,
            "status": "unknown",
            "snippet": None,
        }

        group = pick_group_for_set(groups, deck_set)
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
    # Bundle pass — for each group that contains 2+ of our decks, look
    # for a "set bundle" SKU (Case / Bundle / Set of N) and price it.
    # ----------------------------------------------------------------
    print("\n=== Phase 1b: TCGPlayer set bundles ===", flush=True)
    bundles: dict[str, dict] = {}
    deck_by_id = {d["id"]: d for d in decks}
    deck_ids_by_set: dict[str, list] = {}
    for d in decks:
        deck_ids_by_set.setdefault(d.get("set", ""), []).append(d["id"])

    for set_name, deck_ids in deck_ids_by_set.items():
        if len(deck_ids) < 2:
            continue  # bundles only make sense for sets with multiple precons
        group = pick_group_for_set(groups, set_name)
        if not group:
            continue
        gc = group_cache.get(group["groupId"]) or ensure_group(group)
        if gc.get("error"):
            continue
        bundle_product = find_bundle_in_products(gc["products"], set_name)
        if not bundle_product:
            continue
        bpid = bundle_product["productId"]
        prow = gc["prices"].get(bpid) or {}
        market = prow.get("marketPrice")
        low = prow.get("lowPrice")
        mid = prow.get("midPrice")
        eff = market if market is not None else (low if low is not None else mid)
        src = "Market" if market is not None else ("Low" if low is not None else ("Mid" if mid is not None else None))
        if eff is None:
            print(f"  [{set_name}] bundle found but no price: {bundle_product.get('name')}", flush=True)
            continue
        # Sum of individual deck prices, for "saves $X" math. Only meaningful
        # when EVERY member deck is priced — otherwise unpriced decks count as
        # $0 and understate the total (yielding a misleading/negative savings).
        member_prices = [results.get(did, {}).get("price") for did in deck_ids]
        all_priced = all(p is not None for p in member_prices)
        # If the bundle name advertises a deck count, only trust the savings math
        # when it matches the decks we have (else we'd compare mismatched sets).
        claimed = _bundle_claimed_count(bundle_product.get("name") or "")
        count_ok = claimed is None or claimed == len(deck_ids)
        if all_priced and count_ok:
            individual_total = sum(member_prices)
            raw_savings = individual_total - eff
            # Clamp: only report a positive saving (bundle cheaper than parts).
            savings = raw_savings if raw_savings > 0 else None
        else:
            individual_total = None  # partial coverage → don't claim a total
            savings = None
        bundle_id = re.sub(r"[^a-z0-9]+", "-", set_name.lower()).strip("-") + "-bundle"
        bundles[bundle_id] = {
            "name": bundle_product.get("name"),
            "set": set_name,
            "deck_ids": deck_ids,
            "deck_count": len(deck_ids),
            "price": float(eff),
            "price_source": src,
            "individual_total": round(individual_total, 2) if individual_total is not None else None,
            "savings": round(savings, 2) if savings is not None else None,
            "url": tcg_product_url(bpid, bundle_product.get("name", "")),
            "snippet": bundle_product.get("name"),
        }
        sav_str = f"  saves ${savings:.2f}" if savings and savings > 0 else ""
        print(f"  [{set_name}] {bundle_product.get('name')} → ${eff:.2f}{sav_str}", flush=True)

    print(f"\nFound {len(bundles)} set bundle{'s' if len(bundles) != 1 else ''}", flush=True)
    return results, bundles


# -------------------------------------------------------------------------
# Zulus Games (unchanged)
# -------------------------------------------------------------------------

def json_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.zulusgames.com/",
    }


def is_plausible_mtg_commander_product(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    has_magic = "magic" in t or "mtg" in t
    has_commander = "commander" in t or "precon" in t
    is_premium = "collector" in t or "deluxe" in t
    return has_magic and has_commander and not is_premium


def fetch_zulus(deck_name: str, session: requests.Session) -> dict:
    query = urllib.parse.quote(deck_name)
    api_url = (
        f"https://www.zulusgames.com/search/suggest.json"
        f"?q={query}&resources[type]=product&resources[limit]=10"
    )
    human_url = f"https://www.zulusgames.com/search?q={query}"
    result = {"price": None, "url": human_url, "status": "unknown", "snippet": None}
    try:
        r = session.get(api_url, headers=json_headers(), timeout=TIMEOUT)
        if r.status_code != 200:
            result["status"] = f"http-{r.status_code}"
            return result
        data = r.json()
        products = (
            data.get("resources", {})
            .get("results", {})
            .get("products", [])
        )
        name_lower = deck_name.lower()
        candidates = []
        for p in products:
            title = p.get("title") or ""
            if name_lower not in title.lower():
                continue
            if not is_plausible_mtg_commander_product(title):
                continue
            price_str = p.get("price")
            if not price_str:
                continue
            try:
                price = float(price_str)
            except (TypeError, ValueError):
                continue
            if price < 10.0:
                continue
            candidates.append((price, title, p.get("url")))
        if candidates:
            candidates.sort(key=lambda t: t[0])
            price, title, rel_url = candidates[0]
            result["price"] = price
            result["snippet"] = title
            if rel_url:
                result["url"] = f"https://www.zulusgames.com{rel_url}"
            result["status"] = "ok"
        else:
            result["status"] = "no-match"
        return result
    except requests.RequestException as e:
        result["status"] = f"error-{type(e).__name__}"
        return result
    except (ValueError, KeyError, AttributeError) as e:
        # JSON decode errors and shape mismatches when the API changes.
        # Real bugs (e.g. NameError) propagate so we notice them in CI.
        result["status"] = f"parse-error-{type(e).__name__}"
        return result


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
# The remaining unmatched decks — Animus Antiquity, Master of Keys
# (Assassin's Creed), Faerie Schemes (Wilds of Eldraine), Take Flight
# (Starter Commander) — are simply absent from MTGJSON, so no override
# is possible until they're catalogued there.
DECK_FILE_OVERRIDES = {
    "revival-trance": "RevivalTranceFinalFantasyVi_FIC",
    "limit-break": "LimitBreakFinalFantasyVii_FIC",
    "counter-blitz": "CounterBlitzFinalFantasyX_FIC",
    "scions-and-spellcraft": "ScionsSpellcraftFinalFantasyXiv_FIC",
}


def build_deck_mtgjson_map(decks: list, session: requests.Session) -> dict:
    """Map each of our deck ids → MTGJSON deck fileName.

    First by normalized-name match (Commander-type decks only, so we don't
    grab theme/sample decks), then apply DECK_FILE_OVERRIDES for the
    name-mismatch cases.
    """
    try:
        data = fetch_json(f"{MTGJSON_BASE}/DeckList.json", session)
    except Exception as e:
        print(f"  crack: couldn't load MTGJSON DeckList: {e}", flush=True)
        return dict(DECK_FILE_OVERRIDES)  # overrides still usable offline-ish
    mtg_decks = data.get("data", [])
    by_norm = {}
    valid_files = set()
    for m in mtg_decks:
        valid_files.add(m.get("fileName"))
        if "commander" not in (m.get("type") or "").lower():
            continue
        by_norm.setdefault(norm(m.get("name") or ""), m.get("fileName"))
    mapping = {}
    for d in decks:
        fn = by_norm.get(norm(d["name"]))
        if fn:
            mapping[d["id"]] = fn
    # Apply overrides, but only if the target file actually exists in the
    # current DeckList (guards against a renamed/removed MTGJSON file).
    for did, fn in DECK_FILE_OVERRIDES.items():
        if fn in valid_files:
            mapping[did] = fn
        else:
            print(f"  crack: override file missing in MTGJSON: {fn}", flush=True)
    return mapping


def fetch_json(url: str, session: requests.Session) -> dict:
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_gz_json(url: str, session: requests.Session) -> dict:
    """GET a gzipped JSON file and return the parsed object."""
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    with gzip.open(io.BytesIO(r.content), "rt", encoding="utf-8") as f:
        return json.load(f)


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
    val = byday[max(keys)]
    return float(val) if val is not None else None


def _get_allprices(session: requests.Session) -> dict:
    """Download + parse MTGJSON AllPricesToday ONCE per run (it's ~60MB gz /
    hundreds of MB decompressed). Cached on the function so the crack phase and
    the box-EV phase share a single download instead of fetching it twice."""
    cached = getattr(_get_allprices, "_data", None)
    if cached is None:
        cached = fetch_gz_json(ALLPRICES_URL, session).get("data", {})
        _get_allprices._data = cached
    return cached


def fetch_crack_values(decks: list, session: requests.Session) -> dict:
    """Per-deck 'crack value' = sum of every card's Near Mint retail price,
    computed separately for TCGPlayer and Card Kingdom.

    Pipeline: MTGJSON decklist (uuid + count per card) → MTGJSON
    AllPricesToday (NM retail price per uuid, both vendors).
    Resilient: unmatched decks and unpriced cards are skipped, not fatal.
    Returns { deck_id: {tcg, cardkingdom, card_count, priced, missing} }.
    """
    deck_map = build_deck_mtgjson_map(decks, session)
    print(f"  crack: matched {len(deck_map)}/{len(decks)} decks to MTGJSON", flush=True)
    if not deck_map:
        return {}

    # 1. Pull each deck's card list (uuid + count). Collect unique uuids.
    deck_cards: dict[str, list] = {}
    need_uuids: set = set()
    for d in decks:
        fn = deck_map.get(d["id"])
        if not fn:
            continue
        try:
            dj = fetch_json(f"{MTGJSON_BASE}/decks/{fn}.json", session)
        except Exception as e:
            print(f"  crack: [{d['id']}] deck fetch failed: {e}", flush=True)
            continue
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
                if u:
                    cards.append((u, cnt, nm, is_basic))
                    need_uuids.add(u)
        deck_cards[d["id"]] = cards
        time.sleep(0.05)

    # 2. Download today's prices once; keep only the uuids we need (both
    #    vendors, normal/non-foil retail = Near Mint).
    print(f"  crack: downloading MTGJSON prices ({len(need_uuids)} unique cards)...", flush=True)
    try:
        pdata = _get_allprices(session)
    except Exception as e:
        print(f"  crack: price file download failed: {e}", flush=True)
        return {}
    price_by_uuid: dict = {}
    for u in need_uuids:
        paper = (pdata.get(u) or {}).get("paper", {})
        ckp = paper.get("cardkingdom") or {}
        tcgp = paper.get("tcgplayer") or {}
        tcg_retail = _latest_price(((tcgp.get("retail") or {}).get("normal")))
        ck_retail = _latest_price(((ckp.get("retail") or {}).get("normal")))
        ck_buylist = _latest_price(((ckp.get("buylist") or {}).get("normal")))
        price_by_uuid[u] = (tcg_retail, ck_retail, ck_buylist)
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
        for u, cnt, nm, is_basic in cards:
            tcg_r, ck_r, ck_bl = price_by_uuid.get(u, (None, None, None))
            if tcg_r is not None:
                tcg_buy += tcg_r * cnt
                tcg_sell_est += tcg_r * TCG_SELL_RATE * cnt
                tcg_n += 1
            if ck_r is not None:
                ck_buy += ck_r * cnt
                ck_n += 1
            if ck_bl is not None:
                ck_sell += ck_bl * cnt  # cards CK won't buy simply add $0
            if tcg_r is None and ck_r is None:
                missing += 1
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
                "card_count": len(cards),
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

# Packs per display box, by our box type and MTGJSON booster key.
BOX_PACKS = {"play": 30, "collector": 12, "jumpstart": 24}
BOX_BOOSTER_KEY = {"play": "play", "collector": "collector", "jumpstart": "jumpstart"}


def _build_setcode_map(session: requests.Session) -> dict:
    """normalized set name -> MTGJSON set code, from SetList.json."""
    try:
        data = fetch_json(f"{MTGJSON_BASE}/SetList.json", session)
    except Exception as e:
        print(f"  ev: couldn't load SetList: {e}", flush=True)
        return {}
    out = {}
    for s in data.get("data", []):
        code = s.get("code")
        nm = s.get("name")
        if code and nm:
            out[norm(nm)] = code
    return out


def _pack_ev_from_booster(booster_cfg: dict, price_of_uuid):
    """Expected value of ONE pack using MTGJSON's published booster model.

    booster_cfg has:
      - 'boosters': list of {weight, contents:{sheetName: count}}
      - 'boostersTotalWeight': sum of weights
      - 'sheets': {sheetName: {'totalWeight':N, 'foil':bool, 'cards':{uuid:weight}}}
    EV = sum over booster configs (weighted) of sum over sheet slots of
         (count * expected card price on that sheet).
    price_of_uuid(uuid, is_foil) returns a float price or None (treated as 0).
    A sheet's `foil` flag selects the foil finish for the SAME card uuids.
    Returns (ev, coverage) where coverage is the weighted fraction of pack slots
    that resolved to a real price (low coverage => unreliable EV).
    """
    sheets = booster_cfg.get("sheets") or {}
    boosters = booster_cfg.get("boosters") or []
    total_w = booster_cfg.get("boostersTotalWeight") or sum(b.get("weight", 0) for b in boosters)
    if not boosters or not total_w:
        return None, 0.0

    # Per-sheet expected price + the fraction of that sheet's weight that priced.
    sheet_ev = {}
    sheet_cov = {}
    for name, sh in sheets.items():
        cards = sh.get("cards") or {}
        sw = sh.get("totalWeight") or sum(cards.values())
        is_foil = bool(sh.get("foil"))
        if not sw:
            sheet_ev[name] = 0.0
            sheet_cov[name] = 0.0
            continue
        acc = 0.0
        priced_w = 0.0
        for uuid, w in cards.items():
            p = price_of_uuid(uuid, is_foil)
            if p:
                acc += (w / sw) * p
                priced_w += w
        sheet_ev[name] = acc
        sheet_cov[name] = priced_w / sw

    ev = 0.0
    cov_num = 0.0
    cov_den = 0.0
    for b in boosters:
        bw = b.get("weight", 0)
        if not bw:
            continue
        pack = 0.0
        for sheet_name, count in (b.get("contents") or {}).items():
            pack += sheet_ev.get(sheet_name, 0.0) * count
            cov_num += (bw / total_w) * count * sheet_cov.get(sheet_name, 0.0)
            cov_den += (bw / total_w) * count
        ev += (bw / total_w) * pack
    coverage = (cov_num / cov_den) if cov_den else 0.0
    return ev, coverage


def compute_box_ev(set_name: str, setcode_map: dict, session: requests.Session, want_types: set) -> dict:
    """Return {box_type: ev_per_box} for the given set, using MTGJSON's set
    file (booster config + per-card foil/nonfoil prices). Estimate — see the
    UI note. Returns {} when the set/booster/price data isn't available."""
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
        paper = (allp.get(uuid) or {}).get("paper", {})
        tcg = (paper.get("tcgplayer") or {}).get("retail") or {}
        nf = _latest_price(tcg.get("normal"))
        fo = _latest_price(tcg.get("foil"))
        # Foil sheets reference the same uuids; price the requested finish,
        # falling back to the other finish only when the requested one is absent.
        if is_foil:
            return fo if fo is not None else nf
        return nf if nf is not None else fo

    out = {}
    for t in want_types:
        bkey = BOX_BOOSTER_KEY.get(t)
        cfg = booster.get(bkey) if bkey else None
        # Pre-2024 sets predate Play Boosters: their box is a Draft/Set booster.
        if not cfg and t == "play":
            cfg = booster.get("draft") or booster.get("set")
        if not cfg:
            continue
        pack_ev, coverage = _pack_ev_from_booster(cfg, price_of)
        # Skip publishing a misleadingly-low EV when most slots had no price.
        if pack_ev is None or coverage < 0.5:
            continue
        out[t] = round(pack_ev * BOX_PACKS.get(t, 1), 2)
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
    """CK prices arrive as strings like '54.99'; '0.00'/missing means none."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


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
        qty = item.get("qty_retail") or 0
        qty_buy = item.get("qty_buying") or 0
        rel = (item.get("url") or "").lstrip("/")
        url = f"{CK_WEB_BASE}/{rel}" if rel else CK_WEB_BASE
        low = name.lower()
        if "commander deck" in low:
            # Skip multi-deck SKUs (displays/cases/"set of N") — not a single deck.
            if any(k in low for k in ("display", "case", "set of", "bundle")):
                continue
            # Flag premium re-printings so matching can deprioritize them.
            premium = any(k in low for k in ("collector", "game edition", "gift", "starter kit"))
            decks_idx.append({
                "name_norm": norm(_ck_deck_name(name)), "edition_norm": ed_norm,
                "retail": retail, "qty": qty, "buy": buy, "qty_buy": qty_buy,
                "url": url, "raw": name, "premium": premium,
            })
            continue
        t = _classify_ck_box(name)
        if not t:
            continue
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
    positives: requires the precon name to equal/contain the CK deck-name; when
    several candidates remain, requires the set to correspond (or a unique hit).
    Unmatched decks get a CK search link with status 'no-match'."""
    out = {}
    for d in decks:
        did = d["id"]
        dn = norm(d["name"])
        ds = norm(d.get("set", ""))
        search = (f"{CK_WEB_BASE}/catalog/search?filter%5Bname%5D="
                  f"{urllib.parse.quote(d['name'])}&filter%5Btab%5D=product")
        cands = [c for c in ck_decks if dn and c["name_norm"]
                 and (dn == c["name_norm"] or dn in c["name_norm"] or c["name_norm"] in dn)]
        set_ok = lambda c: ds and (ds in c["edition_norm"] or c["edition_norm"] in ds)
        exact_set = [c for c in cands if dn == c["name_norm"] and set_ok(c)]
        exact = [c for c in cands if dn == c["name_norm"]]
        set_match = [c for c in cands if set_ok(c)]
        # Single-candidate fallback only when it's name-exact or the set matches —
        # never auto-accept a bare cross-set substring hit.
        loose = cands if (len(cands) == 1 and (cands[0]["name_norm"] == dn or set_ok(cands[0]))) else []
        chosen = _pick_ck(exact_set or exact or set_match or loose)
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


def _ck_box_for(ck_boxes: dict, set_name: str, box_type: str) -> dict | None:
    """Look up the CK box record for a (set, type). CK box editions track the
    main set name (e.g. 'Bloomburrow'), but our box phase iterates the decks'
    set field which may carry 'Commander'/parentheticals ('Bloomburrow
    Commander'). Match the cleaned name first, then the raw name as fallback."""
    if not ck_boxes:
        return None
    key = norm(_clean_set_name(set_name))
    return (ck_boxes.get(key) or ck_boxes.get(norm(set_name)) or {}).get(box_type)


def fetch_box_prices(decks: list, session: requests.Session, groups: list,
                     ck_boxes: dict | None = None) -> dict:
    """For each unique set among the decks, find its main-set group and price
    the Play/Collector/Jumpstart boxes. Returns { set_name: [ {type, label,
    name, price, price_low, price_source, url, ev} ] }.
    `ev` is an Expected-Value ESTIMATE of the singles in a box, from MTGJSON's
    published booster model (omitted when the model/prices aren't available).
    Resilient: sets with no boxes (Commander-only products) are simply omitted.
    """
    sets = sorted({d.get("set", "") for d in decks if d.get("set")})
    setcode_map = _build_setcode_map(session)
    out: dict = {}
    for set_name in sets:
        g = find_main_set_group(groups, set_name)
        if not g:
            continue
        try:
            prods = fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/{g['groupId']}/products", session).get("results", [])
            prices = flatten_prices(fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/{g['groupId']}/prices", session).get("results", []))
        except Exception as e:
            print(f"  boxes: [{set_name}] fetch failed: {e}", flush=True)
            time.sleep(0.3)  # stay polite even on the error path
            continue
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
            rows.append({
                "type": t,
                "label": BOX_TYPE_LABELS[t],
                "name": p.get("name"),
                "price": float(eff),
                "price_low": float(low) if low is not None else None,
                "price_source": src,
                "url": tcg_product_url(pid, p.get("name", "")),
            })
        if rows:
            # Expected-value estimate per box type (best-effort).
            try:
                ev_map = compute_box_ev(set_name, setcode_map, session, {r["type"] for r in rows})
            except Exception as e:
                print(f"  ev: [{set_name}] failed: {e}", flush=True)
                ev_map = {}
            for r in rows:
                if r["type"] in ev_map:
                    r["ev"] = ev_map[r["type"]]
                # Card Kingdom as a second box vendor (retail + stock + buylist).
                ck = _ck_box_for(ck_boxes, set_name, r["type"])
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

def main() -> None:
    decks_path = Path(__file__).parent / "decks.json"
    out_path = Path(__file__).parent / "prices.json"
    decks = json.loads(decks_path.read_text())
    print(f"Loaded {len(decks)} decks\n", flush=True)

    print("=== Phase 1: TCGCSV → TCGPlayer ===", flush=True)
    tcg_results, bundles = fetch_all_tcgcsv(decks)

    print("\n=== Phase 2: Zulus Games ===", flush=True)
    zulus_session = make_session(USER_AGENTS[0])
    zulus_results: dict[str, dict] = {}
    for i, deck in enumerate(decks, 1):
        did = deck["id"]
        name = deck["name"]
        try:
            zu = fetch_zulus(name, zulus_session)
        except Exception as e:
            # One deck's unexpected error must not discard the whole run (TCG
            # already succeeded). Log loudly so real bugs are still visible.
            zu = {"price": None, "url": None, "status": f"error-{type(e).__name__}"}
            print(f"[{i:3}/{len(decks)}] {name:<36} Zulus ERROR: {e}", flush=True)
            traceback.print_exc()
        zulus_results[did] = zu
        if zu.get("price"):
            print(f"[{i:3}/{len(decks)}] {name:<36} Zulus ${zu['price']:>6.2f}", flush=True)
        time.sleep(POLITE_SLEEP + random.uniform(0, 0.3))

    print("\n=== Phase 2.5: Card Kingdom (free sealed pricelist API) ===", flush=True)
    ck_sealed = {}
    ck_results = {}
    try:
        ck_session = make_session(TCGCSV_UA)
        ck_sealed = fetch_ck_sealed(ck_session)
        ck_results = match_ck_decks(decks, ck_sealed.get("decks", []))
    except Exception as e:
        print(f"Card Kingdom phase failed (non-fatal): {e}", flush=True)
    if not ck_results:
        # Keep a per-deck record (search link) so the frontend stays happy.
        ck_results = {
            deck["id"]: {
                "price": None,
                "url": f"https://www.cardkingdom.com/catalog/search?filter%5Bname%5D={urllib.parse.quote(deck['name'])}&filter%5Btab%5D=product",
                "status": "unavailable",
            }
            for deck in decks
        }

    print("\n=== Phase 3: Crack value (MTGJSON dual-vendor, NM) ===", flush=True)
    crack_results = {}
    try:
        crack_results = fetch_crack_values(decks, make_session(TCGCSV_UA))
    except Exception as e:
        print(f"Crack value phase failed (non-fatal): {e}", flush=True)
        traceback.print_exc()

    print("\n=== Phase 4: Sealed box prices (per set) ===", flush=True)
    box_results = {}
    try:
        box_session = make_session(TCGCSV_UA)
        box_groups = load_magic_groups(box_session)
        box_results = fetch_box_prices(decks, box_session, box_groups,
                                       ck_boxes=ck_sealed.get("boxes"))
    except Exception as e:
        print(f"Box price phase failed (non-fatal): {e}", flush=True)
        traceback.print_exc()

    # --- Coverage gate: never let a degraded run clobber good data ----------
    tcg_hits = sum(1 for v in tcg_results.values() if v.get("price") is not None)
    healthy = tcg_hits >= MIN_TCG_HITS
    if not healthy and out_path.exists():
        raise SystemExit(
            f"Refusing to overwrite prices.json: only {tcg_hits} TCG hits "
            f"(min {MIN_TCG_HITS}) — likely a degraded scrape.")

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "deck_count": len(decks),
        "vendors": {
            "cardkingdom": ck_results,
            "zulus": zulus_results,
            "tcgplayer": tcg_results,
        },
        "bundles": bundles,
        "crack": crack_results,
        "boxes": box_results,
    }
    # Atomic write: never leave a half-written prices.json on crash/disk-full.
    tmp_path = out_path.parent / (out_path.name + ".tmp")
    tmp_path.write_text(json.dumps(output, indent=2))
    tmp_path.replace(out_path)

    # Reverse card->decks index (F2: "cheapest precon containing card X").
    # Kept in its own file so prices.json stays lean. Only emitted on a healthy
    # run, and never shrink a good index to a much smaller one (partial crack).
    card_index = getattr(fetch_crack_values, "card_index", None)
    if card_index and healthy:
        idx_path = Path(__file__).parent / "cards_index.json"
        prev_n = 0
        if idx_path.exists():
            try:
                prev_n = len(json.loads(idx_path.read_text()).get("cards", {}))
            except (json.JSONDecodeError, OSError):
                prev_n = 0
        if len(card_index) >= prev_n * 0.5:
            tmp_idx = idx_path.parent / (idx_path.name + ".tmp")
            tmp_idx.write_text(json.dumps({
                "updated_at": output["updated_at"],
                "cards": card_index,
            }, separators=(",", ":")))
            tmp_idx.replace(idx_path)
            print(f"Cards idx: {len(card_index)} unique cards across decks", flush=True)
        else:
            print(f"Skipping cards_index: {len(card_index)} < half of existing {prev_n}", flush=True)

    # History is part of the same process: only record on a healthy run so a
    # partial scrape can't permanently pollute the rolling series.
    if healthy:
        update_history(decks, tcg_results, zulus_results, box_results, ck_results)
    else:
        print("Skipping history update (degraded run).", flush=True)

    zu_hits = sum(1 for v in zulus_results.values() if v.get("price") is not None)
    rejected = sum(1 for v in tcg_results.values() if v.get("status") == "tcgcsv-likely-single")

    print(f"\nWrote {out_path}")
    print(f"TCGPlayer: {tcg_hits}/{len(decks)} hits  (rejected {rejected} as likely singles)")
    print(f"Zulus:     {zu_hits}/{len(decks)} hits")
    print(f"Bundles:   {len(bundles)} sets")
    print(f"Crack val: {len(crack_results)} decks priced")
    print(f"Boxes:     {len(box_results)} sets with sealed boxes")


def update_history(decks: list, tcg_results: dict, zulus_results: dict,
                   box_results: dict | None = None, ck_results: dict | None = None) -> None:
    """Append today's prices to prices_history.json and trim to HISTORY_DAYS.

    File shape:
      { "decks": { deck_id: [{date, tcg, zulus, ck, best}, ...] },
        "boxes": { "<set>::<type>": [{date, price}, ...] } }
    `best` is the cheapest available vendor price that day (drives alerts).
    Skips appending if today's present-vendor prices match the most-recent
    entry's, so the file stays small for items that don't move much.
    """
    history_path = Path(__file__).parent / "prices_history.json"
    today = datetime.now(timezone.utc).date().isoformat()
    ck_results = ck_results or {}

    if history_path.exists():
        try:
            history = json.loads(history_path.read_text())
        except json.JSONDecodeError:
            print(f"  WARN: {history_path.name} unreadable, starting fresh", flush=True)
            history = {}
    else:
        history = {}

    decks_history = history.get("decks") if isinstance(history.get("decks"), dict) else {}

    for deck in decks:
        did = deck["id"]
        tcg_price = tcg_results.get(did, {}).get("price")
        zu_price = zulus_results.get(did, {}).get("price")
        ck_price = ck_results.get(did, {}).get("price")
        if tcg_price is None and zu_price is None and ck_price is None:
            continue  # nothing to record

        series = decks_history.get(did, [])
        new_entry = {"date": today}
        if tcg_price is not None:
            new_entry["tcg"] = round(float(tcg_price), 2)
        if zu_price is not None:
            new_entry["zulus"] = round(float(zu_price), 2)
        if ck_price is not None:
            new_entry["ck"] = round(float(ck_price), 2)
        avail = [p for p in (tcg_price, zu_price, ck_price) if p is not None]
        if avail:
            new_entry["best"] = round(float(min(avail)), 2)

        if series and series[-1].get("date") == today:
            # Same-day rerun: MERGE, don't replace, so a transient single-vendor
            # failure on a rerun doesn't wipe a value captured earlier today.
            series[-1].update(new_entry)
        else:
            prev = series[-1] if series else None
            # Compare only the vendors PRESENT today: a vendor that simply went
            # missing this run must not look like a price change (which would
            # churn the rolling window with duplicate-ish entries).
            present = [k for k in new_entry if k != "date"]
            same = prev is not None and bool(present) and all(prev.get(k) == new_entry.get(k) for k in present)
            if not same:
                series.append(new_entry)

        # Trim to rolling window
        if len(series) > HISTORY_DAYS:
            series = series[-HISTORY_DAYS:]
        decks_history[did] = series

    # ---- Box history: keyed "<set>::<type>", one {date, price} series each ----
    boxes_history = history.get("boxes") if isinstance(history.get("boxes"), dict) else {}
    if box_results:
        for set_name, rows in box_results.items():
            for r in rows:
                price = r.get("price")
                if price is None:
                    continue
                key = f"{set_name}::{r.get('type')}"
                series = boxes_history.get(key, [])
                new_entry = {"date": today, "price": round(float(price), 2)}
                if series and series[-1].get("date") == today:
                    series[-1].update(new_entry)  # merge (symmetry with deck branch)
                else:
                    prev = series[-1] if series else None
                    if not (prev and prev.get("price") == new_entry["price"]):
                        series.append(new_entry)
                if len(series) > HISTORY_DAYS:
                    series = series[-HISTORY_DAYS:]
                boxes_history[key] = series

    history = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": HISTORY_DAYS,
        "decks": decks_history,
        "boxes": boxes_history,
    }
    history_path.write_text(json.dumps(history, separators=(",", ":")))
    sample_lens = [len(v) for v in decks_history.values()]
    if sample_lens:
        print(f"History: {len(decks_history)} decks, max {max(sample_lens)} / median {sorted(sample_lens)[len(sample_lens)//2]} entries", flush=True)
    if boxes_history:
        print(f"History: {len(boxes_history)} box series tracked", flush=True)


if __name__ == "__main__":
    main()
