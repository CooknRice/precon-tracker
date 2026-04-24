"""
Scrape Card Kingdom and Zulus Games for precon prices.

v1.2 changes:
  - Zulus: additionally reject "Collector's Edition" and "Deluxe"
    variants. These are real MTG products at real (high) prices,
    but they're not the standard precon Isaac is tracking.
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
POLITE_SLEEP = 1.5


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


def fetch_cardkingdom(deck_name: str) -> dict:
    query = urllib.parse.quote(deck_name)
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
    """Accept only standard MTG commander precons.

    Must contain: magic/mtg AND commander/precon
    Must NOT contain: collector, deluxe (premium variants, not the base deck)
    """
    if not title:
        return False
    t = title.lower()
    has_magic = "magic" in t or "mtg" in t
    has_commander = "commander" in t or "precon" in t
    is_premium_variant = "collector" in t or "deluxe" in t
    return has_magic and has_commander and not is_premium_variant


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
