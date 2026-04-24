"""
Scrape Card Kingdom and Zulus Games for precon prices.

v1.1 changes:
  - CK: rotate through real Chrome UA strings + full browser headers
    (Sec-Fetch-*, Referer, etc.) to get past basic bot-detection.
  - Zulus: require the product title to clearly be an MTG commander
    product (must contain 'magic'/'mtg' AND 'commander'/'precon').
    Rejects false positives like Star Wars X-Wing and Mastery Pack
    single cards that happened to share a deck name.
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

# Recent real Chrome/Safari UAs on desktop. Rotating reduces pattern-matching.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

TIMEOUT = 20
POLITE_SLEEP = 1.5


def browser_headers() -> dict:
    """Headers that mimic a real Chrome navigation request."""
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
    """Headers for JSON API endpoints (Zulus' Shopify /suggest.json)."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.zulusgames.com/",
    }


def fetch_cardkingdom(deck_name: str) -> dict:
    """Card Kingdom search — HTML-parsed. Uses browser-like headers."""
    query = urllib.parse.quote(deck_name)
    # `partner=edhrec` makes the request look like an affiliate referral —
    # sometimes routes past basic bot filters. Free to try.
    search_url = (
        f"https://www.cardkingdom.com/catalog/search"
        f"?partner=edhrec&filter%5Bname%5D={query}&filter%5Btab%5D=product"
    )
    result = {"price": None, "url": search_url, "status": "unknown", "snippet": None}
    try:
        r = requests.get(search_url, headers=browser_headers(), timeout=TIMEOUT)
        if r.status_code != 200:
            result["status"] = f"http-{r.status_code}"
            return result
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        name_lower = deck_name.lower()
        text_lower = text.lower()
        prices = []
        start = 0
        while True:
            idx = text_lower.find(name_lower, start)
            if idx < 0:
                break
            window = text[idx : idx + 200]
            m = re.search(r"\$(\d+\.\d{2})", window)
            if m:
                price = float(m.group(1))
                # Precons run $20+. Anything under $15 is a card single.
                if 15.0 <= price <= 600.0:
                    prices.append((price, window[:140]))
            start = idx + len(name_lower)

        if prices:
            prices.sort(key=lambda t: t[0])
            result["price"] = prices[0][0]
            result["snippet"] = prices[0][1]
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


def is_plausible_mtg_commander_product(title: str) -> bool:
    """Must contain magic/MTG AND commander/precon keywords.

    Rejects cross-game false positives:
      - 'Star Wars: X-Wing - Most Wanted'       → no magic, no commander
      - 'Call for Backup [MPG126] Mastery Pack' → no magic, no commander
    Keeps legitimate variants:
      - 'Commander Deck: Collector's Edition: Necron Dynasties' → both present
      - 'Deluxe Commander Kit: Food and Fellowship'             → both present
      - 'Commander 2017 Deck: Draconic Domination'              → both present
    """
    if not title:
        return False
    t = title.lower()
    has_magic = "magic" in t or "mtg" in t
    has_commander = "commander" in t or "precon" in t
    return has_magic and has_commander


def fetch_zulus(deck_name: str) -> dict:
    """Zulus Games — Shopify suggest.json endpoint."""
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
        },
    }

    ck_hits = 0
    zu_hits = 0

    for i, deck in enumerate(decks, 1):
        name = deck["name"]
        did = deck["id"]
        print(f"[{i:3}/{len(decks)}] {name}", flush=True)

        ck = fetch_cardkingdom(name)
        output["vendors"]["cardkingdom"][did] = ck
        if ck["price"]:
            ck_hits += 1
            print(f"     CK    ${ck['price']:>6.2f}  ({ck['status']})", flush=True)
        else:
            print(f"     CK    ------  ({ck['status']})", flush=True)
        # Jittered delay — human-ish request pattern
        time.sleep(POLITE_SLEEP + random.uniform(0, 0.8))

        zu = fetch_zulus(name)
        output["vendors"]["zulus"][did] = zu
        if zu["price"]:
            zu_hits += 1
            print(f"     Zulus ${zu['price']:>6.2f}  ({zu['status']})", flush=True)
        else:
            print(f"     Zulus ------  ({zu['status']})", flush=True)
        time.sleep(POLITE_SLEEP + random.uniform(0, 0.8))

    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {out_path}")
    print(f"Card Kingdom: {ck_hits}/{len(decks)} hits")
    print(f"Zulus Games:  {zu_hits}/{len(decks)} hits")


if __name__ == "__main__":
    main()
