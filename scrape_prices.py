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

import json
import random
import re
import time
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
    return r.json()


def load_magic_groups(session: requests.Session) -> list:
    data = fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/groups", session)
    return data.get("results", [])


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
        # Set name was nothing but noise words — give up cleanly.
        return None

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

    When `prices` is supplied, products priced below PRICE_FLOOR are
    deprioritised (still considered as a last-resort fallback, but only
    if no plausibly-priced precon match exists). This avoids wasting the
    matched-product slot on a same-named single card.
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

        # Price-floor signal: products priced below the floor are almost
        # always single cards. Demote them so a real precon wins ties.
        passes_floor = True
        if prices is not None:
            row = prices.get(p.get("productId"))
            market = row.get("marketPrice") if row else None
            if market is not None and market < PRICE_FLOOR:
                passes_floor = False

        # Sortable score: floor-passing > precon-named > exact > shortest.
        score = (passes_floor, is_precon_named, is_exact, -len(pname))
        candidates.append((score, p))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


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


def fetch_all_tcgcsv(decks: list) -> dict:
    session = make_session(TCGCSV_UA)

    print("Loading TCGCSV Magic groups list...", flush=True)
    try:
        groups = load_magic_groups(session)
    except Exception as e:
        print(f"  FATAL: couldn't load groups list: {e}", flush=True)
        groups = []
    print(f"  {len(groups)} Magic groups loaded\n", flush=True)

    group_cache: dict[int, dict] = {}

    def ensure_group(group: dict) -> dict:
        gid = group["groupId"]
        if gid in group_cache:
            return group_cache[gid]
        try:
            prod_data = fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/{gid}/products", session)
            price_data = fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/{gid}/prices", session)
            group_cache[gid] = {
                "name": group.get("name"),
                "products": prod_data.get("results", []),
                "prices": flatten_prices(price_data.get("results", [])),
                "error": None,
            }
            time.sleep(0.3)
        except Exception as e:
            group_cache[gid] = {
                "name": group.get("name"),
                "products": [],
                "prices": {},
                "error": str(e),
            }
        return group_cache[gid]

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
        price_row = gc["prices"].get(pid)
        market = price_row.get("marketPrice") if price_row else None
        low = price_row.get("lowPrice") if price_row else None
        url = tcg_product_url(pid, product.get("name", ""))

        # Price-floor sanity check: precons are never < $5. If the matched
        # product is, we almost certainly hit a single card with the same
        # name (e.g. "Cloud's Limit Break" instead of the Limit Break deck).
        if market is not None and market < PRICE_FLOOR:
            results[deck_id] = {
                "price": None, "price_low": None,
                "url": fallback_url,
                "status": "tcgcsv-likely-single",
                "snippet": f"rejected: {product.get('name')} (Market ${float(market):.2f})",
            }
            print(f"[{i:3}/{len(decks)}] {deck_name:<36} TCG ------  (rejected ${float(market):.2f}: likely single card)", flush=True)
            continue

        if market is not None:
            results[deck_id] = {
                "price": float(market),
                "price_low": float(low) if low is not None else None,
                "url": url,
                "status": "ok",
                "snippet": product.get("name"),
            }
            low_str = f" / low ${float(low):.2f}" if low else ""
            print(f"[{i:3}/{len(decks)}] {deck_name:<36} TCG ${float(market):>6.2f}  (Market{low_str})", flush=True)
        else:
            results[deck_id] = {
                "price": None, "price_low": None,
                "url": url,
                "status": "tcgcsv-no-market-price",
                "snippet": product.get("name"),
            }
            print(f"[{i:3}/{len(decks)}] {deck_name:<36} TCG ------  (no market price)", flush=True)

    return results


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
# Main
# -------------------------------------------------------------------------

def main() -> None:
    decks_path = Path(__file__).parent / "decks.json"
    out_path = Path(__file__).parent / "prices.json"
    decks = json.loads(decks_path.read_text())
    print(f"Loaded {len(decks)} decks\n", flush=True)

    print("=== Phase 1: TCGCSV → TCGPlayer ===", flush=True)
    tcg_results = fetch_all_tcgcsv(decks)

    print("\n=== Phase 2: Zulus Games ===", flush=True)
    zulus_session = make_session(USER_AGENTS[0])
    zulus_results: dict[str, dict] = {}
    for i, deck in enumerate(decks, 1):
        did = deck["id"]
        name = deck["name"]
        zu = fetch_zulus(name, zulus_session)
        zulus_results[did] = zu
        if zu["price"]:
            print(f"[{i:3}/{len(decks)}] {name:<36} Zulus ${zu['price']:>6.2f}", flush=True)
        time.sleep(POLITE_SLEEP + random.uniform(0, 0.3))

    # Card Kingdom: empty per-deck record retained only for HTML compatibility.
    ck_results = {
        deck["id"]: {
            "price": None,
            "url": f"https://www.cardkingdom.com/catalog/search?filter%5Bname%5D={urllib.parse.quote(deck['name'])}&filter%5Btab%5D=product",
            "status": "disabled-in-v1.5",
            "snippet": None,
        }
        for deck in decks
    }

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "deck_count": len(decks),
        "vendors": {
            "cardkingdom": ck_results,
            "zulus": zulus_results,
            "tcgplayer": tcg_results,
        },
    }
    out_path.write_text(json.dumps(output, indent=2))

    update_history(decks, tcg_results, zulus_results)

    tcg_hits = sum(1 for v in tcg_results.values() if v.get("price") is not None)
    zu_hits = sum(1 for v in zulus_results.values() if v.get("price") is not None)
    rejected = sum(1 for v in tcg_results.values() if v.get("status") == "tcgcsv-likely-single")

    print(f"\nWrote {out_path}")
    print(f"TCGPlayer: {tcg_hits}/{len(decks)} hits  (rejected {rejected} as likely singles)")
    print(f"Zulus:     {zu_hits}/{len(decks)} hits")


def update_history(decks: list, tcg_results: dict, zulus_results: dict) -> None:
    """Append today's prices to prices_history.json and trim to HISTORY_DAYS.

    File shape: { "decks": { deck_id: [{date: 'YYYY-MM-DD', tcg: 49.99, zulus: 47.50}, ...] } }
    Skips appending if today's prices match the most-recent entry's, so the
    file stays small for decks that don't move much.
    """
    history_path = Path(__file__).parent / "prices_history.json"
    today = datetime.now(timezone.utc).date().isoformat()

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
        if tcg_price is None and zu_price is None:
            continue  # nothing to record

        series = decks_history.get(did, [])
        # If today's entry already exists, replace it (re-runs in same day).
        # Otherwise compare to the previous entry; only append when changed.
        new_entry = {"date": today}
        if tcg_price is not None:
            new_entry["tcg"] = round(float(tcg_price), 2)
        if zu_price is not None:
            new_entry["zulus"] = round(float(zu_price), 2)

        if series and series[-1].get("date") == today:
            series[-1] = new_entry
        else:
            prev = series[-1] if series else None
            same = prev and prev.get("tcg") == new_entry.get("tcg") and prev.get("zulus") == new_entry.get("zulus")
            if not same:
                series.append(new_entry)

        # Trim to rolling window
        if len(series) > HISTORY_DAYS:
            series = series[-HISTORY_DAYS:]
        decks_history[did] = series

    history = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": HISTORY_DAYS,
        "decks": decks_history,
    }
    history_path.write_text(json.dumps(history, separators=(",", ":")))
    sample_lens = [len(v) for v in decks_history.values()]
    if sample_lens:
        print(f"History: {len(decks_history)} decks, max {max(sample_lens)} / median {sorted(sample_lens)[len(sample_lens)//2]} entries")


if __name__ == "__main__":
    main()
