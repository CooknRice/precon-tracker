"""
Scrape precon prices.

v1.3 changes:
  - Dropped direct Card Kingdom scraping (blocked at IP layer from GitHub
    Actions, confirmed by v1.1/v1.2 runs returning 100% HTTP 403).
  - Added MTGStocks-based lookup. MTGStocks aggregates pricing across
    MTG vendors and serves everything as server-rendered HTML. A single
    product-page fetch yields:
        - TCGPlayer Market price (aggregated recent-sold)
        - TCGPlayer Low price (cheapest current listing)
        - Card Kingdom's current listing price
        - Clean direct URLs for both vendors (stripped of MTGStocks
          affiliate tracking params)
  - prices.json now has three vendors: cardkingdom, zulus, tcgplayer.
"""

import json
import random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

TIMEOUT = 20
POLITE_SLEEP = 1.2     # delay between hitting different hosts
INTRA_SLEEP = 0.6      # delay between two requests to the same host


def browser_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Ch-Ua": '"Chromium";v="126", "Not.A/Brand";v="24", "Google Chrome";v="126"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.google.com/",
    }


def json_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.zulusgames.com/",
    }


def fetch_mtgstocks(deck_name: str) -> dict:
    """One MTGStocks lookup returns both TCGPlayer and Card Kingdom data.

    Returns a dict with two sub-dicts: 'tcgplayer' and 'cardkingdom',
    each with the usual {price, url, status, snippet} shape. The tcgplayer
    dict additionally has 'price_low' for the cheapest current listing.
    """
    q = urllib.parse.quote(deck_name)
    tcg_fallback_url = f"https://www.tcgplayer.com/search/magic/product?q={q}"
    ck_fallback_url = (
        f"https://www.cardkingdom.com/catalog/search"
        f"?filter%5Bname%5D={q}&filter%5Btab%5D=product"
    )
    result = {
        "tcgplayer":   {"price": None, "price_low": None, "url": tcg_fallback_url, "status": "unknown", "snippet": None},
        "cardkingdom": {"price": None, "url": ck_fallback_url, "status": "unknown", "snippet": None},
    }

    try:
        # Step 1: search
        search_url = f"https://www.mtgstocks.com/search?q={q}"
        r = requests.get(search_url, headers=browser_headers(), timeout=TIMEOUT)
        if r.status_code != 200:
            status = f"mtgs-search-http-{r.status_code}"
            result["tcgplayer"]["status"] = status
            result["cardkingdom"]["status"] = status
            return result

        soup = BeautifulSoup(r.text, "html.parser")
        # First sealed-product hit. Links look like "/sealed/8914-tarkir-...".
        sealed_link = soup.find("a", href=re.compile(r"^/sealed/\d+-"))
        if not sealed_link:
            result["tcgplayer"]["status"] = "mtgs-no-sealed-match"
            result["cardkingdom"]["status"] = "mtgs-no-sealed-match"
            return result

        time.sleep(INTRA_SLEEP)

        # Step 2: product page
        product_url = f"https://www.mtgstocks.com{sealed_link['href']}"
        r2 = requests.get(product_url, headers=browser_headers(), timeout=TIMEOUT)
        if r2.status_code != 200:
            status = f"mtgs-product-http-{r2.status_code}"
            result["tcgplayer"]["status"] = status
            result["cardkingdom"]["status"] = status
            return result

        soup2 = BeautifulSoup(r2.text, "html.parser")
        text = soup2.get_text(" ", strip=True)

        # Parse the "Low $X Average $X Market $X MSRP $X" summary
        market_m = re.search(r"Market\s*\$(\d+\.\d{2})", text)
        low_m = re.search(r"Low\s*\$(\d+\.\d{2})", text)
        if market_m:
            result["tcgplayer"]["price"] = float(market_m.group(1))
            if low_m:
                result["tcgplayer"]["price_low"] = float(low_m.group(1))
            result["tcgplayer"]["status"] = "ok"
            result["tcgplayer"]["snippet"] = f"TCG Market (via MTGStocks): {deck_name}"

            # Extract the real TCGPlayer URL out of the partner.tcgplayer.com wrapper
            tcg_link = soup2.find("a", href=re.compile(r"partner\.tcgplayer\.com"))
            if tcg_link:
                u_match = re.search(r"[?&]u=([^&]+)", tcg_link["href"])
                if u_match:
                    result["tcgplayer"]["url"] = urllib.parse.unquote(u_match.group(1))
        else:
            result["tcgplayer"]["status"] = "mtgs-no-market-price"

        # Parse Card Kingdom listing (they appear on the same MTGStocks page)
        ck_link = soup2.find("a", href=re.compile(r"cardkingdom\.com/mtg-sealed"))
        if ck_link:
            ck_text = ck_link.get_text(" ", strip=True)
            ck_price_m = re.search(r"\$(\d+\.\d{2})", ck_text)
            if ck_price_m:
                result["cardkingdom"]["price"] = float(ck_price_m.group(1))
                result["cardkingdom"]["status"] = "ok"
                result["cardkingdom"]["snippet"] = f"CK (via MTGStocks): {deck_name}"
                # Strip affiliate query params from the URL
                result["cardkingdom"]["url"] = ck_link["href"].split("?")[0]
            else:
                result["cardkingdom"]["status"] = "mtgs-no-ck-price"
        else:
            result["cardkingdom"]["status"] = "mtgs-no-ck-listing"

        return result
    except requests.RequestException as e:
        err = f"error-{type(e).__name__}"
        result["tcgplayer"]["status"] = err
        result["cardkingdom"]["status"] = err
        return result
    except Exception as e:
        err = f"parse-error-{type(e).__name__}"
        result["tcgplayer"]["status"] = err
        result["cardkingdom"]["status"] = err
        return result


def is_plausible_mtg_commander_product(title: str) -> bool:
    """Zulus filter: accept only standard MTG commander precons."""
    if not title:
        return False
    t = title.lower()
    has_magic = "magic" in t or "mtg" in t
    has_commander = "commander" in t or "precon" in t
    is_premium = "collector" in t or "deluxe" in t
    return has_magic and has_commander and not is_premium


def fetch_zulus(deck_name: str) -> dict:
    """Zulus Games via Shopify /search/suggest.json."""
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


def main():
    decks_path = Path(__file__).parent / "decks.json"
    out_path = Path(__file__).parent / "prices.json"
    decks = json.loads(decks_path.read_text())
    print(f"Loaded {len(decks)} decks", flush=True)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "deck_count": len(decks),
        "vendors": {
            "cardkingdom": {},
            "zulus": {},
            "tcgplayer": {},
        },
    }

    tcg_hits = 0
    ck_hits = 0
    zu_hits = 0

    for i, deck in enumerate(decks, 1):
        name = deck["name"]
        did = deck["id"]
        print(f"[{i:3}/{len(decks)}] {name}", flush=True)

        # MTGStocks → TCG Market + CK listing
        mtgs = fetch_mtgstocks(name)
        output["vendors"]["tcgplayer"][did] = mtgs["tcgplayer"]
        output["vendors"]["cardkingdom"][did] = mtgs["cardkingdom"]

        tcg = mtgs["tcgplayer"]
        ck = mtgs["cardkingdom"]
        if tcg["price"]:
            tcg_hits += 1
            low_str = f" / low ${tcg['price_low']:.2f}" if tcg.get("price_low") else ""
            print(f"     TCG   ${tcg['price']:>6.2f}  (Market{low_str})", flush=True)
        else:
            print(f"     TCG   ------  ({tcg['status']})", flush=True)
        if ck["price"]:
            ck_hits += 1
            print(f"     CK    ${ck['price']:>6.2f}", flush=True)
        else:
            print(f"     CK    ------  ({ck['status']})", flush=True)

        time.sleep(POLITE_SLEEP + random.uniform(0, 0.5))

        # Zulus
        zu = fetch_zulus(name)
        output["vendors"]["zulus"][did] = zu
        if zu["price"]:
            zu_hits += 1
            print(f"     Zulus ${zu['price']:>6.2f}", flush=True)
        else:
            print(f"     Zulus ------  ({zu['status']})", flush=True)

        time.sleep(POLITE_SLEEP + random.uniform(0, 0.5))

    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {out_path}")
    print(f"TCGPlayer:    {tcg_hits}/{len(decks)} hits")
    print(f"Card Kingdom: {ck_hits}/{len(decks)} hits")
    print(f"Zulus Games:  {zu_hits}/{len(decks)} hits")


if __name__ == "__main__":
    main()
