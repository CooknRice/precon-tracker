"""
Scrape precon prices.

v1.4 changes:
  - Dropped MTGStocks (their search page is a React SPA; no sealed links
    appeared in the server-rendered HTML, so v1.3 got 0/111 hits).
  - Replaced it with TCGCSV (https://tcgcsv.com), a public daily dump of
    TCGPlayer's full catalog as JSON. Gives us Market Price directly with
    no scraping, no cookies, no rate limit anxiety. Just documented
    endpoints returning clean data:
        GET /tcgplayer/1/groups
        GET /tcgplayer/1/{groupId}/products
        GET /tcgplayer/1/{groupId}/prices
  - Zulus unchanged (direct Shopify suggest.json, working fine).
  - Card Kingdom is emptied in this version. MTGStocks was our bridge to
    CK and it's gone; we'll revisit CK separately.
"""

import json
import random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

TCGCSV_BASE = "https://tcgcsv.com/tcgplayer"
MAGIC_CATEGORY = 1
TIMEOUT = 30
TCGCSV_UA = "precon-tracker/1.4 (+https://github.com/CooknRice/precon-tracker)"
POLITE_SLEEP = 1.2

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


# -------------------------------------------------------------------------
# TCGCSV helpers
# -------------------------------------------------------------------------

def norm(s: str) -> str:
    """Lowercase + strip all non-alphanumeric. Robust for fuzzy name matching."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def fetch_tcgcsv_json(path: str, session: requests.Session) -> dict:
    """GET a TCGCSV endpoint. Returns the parsed JSON or raises."""
    url = f"{TCGCSV_BASE}/{path}"
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def load_magic_groups(session: requests.Session) -> list:
    """Fetch the full list of Magic groups (sets/product lines) from TCGCSV."""
    data = fetch_tcgcsv_json(f"{MAGIC_CATEGORY}/groups", session)
    return data.get("results", [])


def pick_group_for_set(groups: list, deck_set: str) -> dict | None:
    """Match a deck's set name to a TCGCSV group.

    Prefers groups whose name contains both the set tokens AND a commander
    keyword (to land on the precon product line, not the base set). Falls
    back to longest-prefix-match if no commander-tagged group exists.
    """
    set_norm = norm(deck_set)
    if not set_norm:
        return None

    commander_kws = ["commander", "precon"]
    scored = []
    for g in groups:
        name = g.get("name") or ""
        if set_norm not in norm(name):
            continue
        name_l = name.lower()
        has_cmdr = any(k in name_l for k in commander_kws)
        # Prefer: has_commander, then shorter (less-specific variant)
        scored.append((has_cmdr, -len(name), g))

    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][2]


def pick_product_for_deck(products: list, deck_name: str) -> dict | None:
    """Find the product whose name contains the deck name (fuzzy)."""
    deck_norm = norm(deck_name)
    if not deck_norm:
        return None

    exact = []
    partial = []
    for p in products:
        pname = p.get("name") or ""
        pnorm = norm(pname)
        if deck_norm == pnorm:
            exact.append(p)
        elif deck_norm in pnorm:
            partial.append(p)

    if exact:
        return exact[0]
    if partial:
        # Prefer shortest match (least qualified / most canonical)
        partial.sort(key=lambda p: len(p.get("name") or ""))
        return partial[0]
    return None


def flatten_prices(price_results: list) -> dict:
    """Turn TCGCSV prices payload into productId -> best price record.

    A productId may have multiple rows (one per subTypeName, e.g. Normal
    vs Foil). Sealed precons are typically "Normal". We prefer "Normal",
    then fall back to whatever we find.
    """
    by_pid: dict[int, dict] = {}
    for row in price_results:
        pid = row.get("productId")
        if pid is None:
            continue
        existing = by_pid.get(pid)
        if existing is None:
            by_pid[pid] = row
            continue
        # Replace if new row is "Normal" and existing isn't
        if row.get("subTypeName") == "Normal" and existing.get("subTypeName") != "Normal":
            by_pid[pid] = row
    return by_pid


def tcg_product_url(product_id: int, product_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (product_name or "").lower()).strip("-")
    return f"https://www.tcgplayer.com/product/{product_id}/magic-{slug}" if slug else f"https://www.tcgplayer.com/product/{product_id}"


def fetch_all_tcgcsv(decks: list) -> dict:
    """Resolve all decks via TCGCSV. Returns {deck_id: price_record}."""
    session = requests.Session()
    session.headers.update({"User-Agent": TCGCSV_UA, "Accept": "application/json"})

    print("Loading TCGCSV Magic groups list...", flush=True)
    try:
        groups = load_magic_groups(session)
    except Exception as e:
        print(f"  FATAL: couldn't load groups list: {e}", flush=True)
        groups = []
    print(f"  {len(groups)} Magic groups loaded", flush=True)

    # Cache per-group fetches so we don't hit the same endpoints repeatedly
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
            time.sleep(0.3)  # gentle on tcgcsv
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
            base["status"] = f"tcgcsv-group-error"
            base["snippet"] = gc["error"][:100]
            results[deck_id] = base
            print(f"[{i:3}/{len(decks)}] {deck_name:<36} TCG ------  (group fetch error)", flush=True)
            continue

        product = pick_product_for_deck(gc["products"], deck_name)
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
# Zulus Games (unchanged from v1.2/v1.3)
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


def fetch_zulus(deck_name: str) -> dict:
    query = urllib.parse.quote(deck_name)
    api_url = (
        f"https://www.zulusgames.com/search/suggest.json"
        f"?q={query}&resources[type]=product&resources[limit]=10"
    )
    human_url = f"https://www.zulusgames.com/search?q={query}"
    result = {"price": None, "url": human_url, "status": "unknown", "snippet": None}
    try:
        r = requests.get(api_url, headers=json_headers(), timeout=TIMEOUT)
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
    except Exception as e:
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

    # Phase 1: TCGCSV → TCGPlayer Market prices for all decks in one pass.
    print("=== Phase 1: TCGCSV → TCGPlayer ===", flush=True)
    tcg_results = fetch_all_tcgcsv(decks)

    # Phase 2: Zulus direct scrape, per deck.
    print("\n=== Phase 2: Zulus Games ===", flush=True)
    zulus_results: dict[str, dict] = {}
    for i, deck in enumerate(decks, 1):
        did = deck["id"]
        name = deck["name"]
        zu = fetch_zulus(name)
        zulus_results[did] = zu
        if zu["price"]:
            print(f"[{i:3}/{len(decks)}] {name:<36} Zulus ${zu['price']:>6.2f}", flush=True)
        time.sleep(POLITE_SLEEP + random.uniform(0, 0.3))

    # Card Kingdom: empty per-deck record to preserve HTML compatibility.
    # (MTGStocks bridge is gone; direct CK is IP-blocked. Revisit later.)
    ck_results = {
        deck["id"]: {
            "price": None,
            "url": f"https://www.cardkingdom.com/catalog/search?filter%5Bname%5D={urllib.parse.quote(deck['name'])}&filter%5Btab%5D=product",
            "status": "disabled-in-v1.4",
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

    tcg_hits = sum(1 for v in tcg_results.values() if v.get("price") is not None)
    zu_hits = sum(1 for v in zulus_results.values() if v.get("price") is not None)

    print(f"\nWrote {out_path}")
    print(f"TCGPlayer: {tcg_hits}/{len(decks)} hits")
    print(f"Zulus:     {zu_hits}/{len(decks)} hits")


if __name__ == "__main__":
    main()
