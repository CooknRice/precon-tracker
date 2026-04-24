"""
Scrape Card Kingdom and Zulus Games for precon prices.

Reads decks.json (111 decks: id, name, set)
Writes prices.json (per-vendor, per-deck: price, url, status, snippet)

Designed to degrade gracefully: a single deck failing should not crash the run.
Every failure is captured in the `status` field so the HTML tracker can still
show a sensible fallback.
"""

import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- Config ---
UA = "Mozilla/5.0 (compatible; precon-price-tracker/1.0; github.com/YOUR_GH_USERNAME/precon-tracker)"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 20
# Polite delay between requests. Daily job → no need to hurry.
POLITE_SLEEP = 1.2


def fetch_cardkingdom(deck_name: str) -> dict:
    """Card Kingdom search — HTML-parsed."""
    query = urllib.parse.quote(deck_name)
    search_url = (
        f"https://www.cardkingdom.com/catalog/search"
        f"?filter%5Bname%5D={query}&filter%5Btab%5D=product"
    )
    result = {"price": None, "url": search_url, "status": "unknown", "snippet": None}
    try:
        r = requests.get(search_url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            result["status"] = f"http-{r.status_code}"
            return result
        soup = BeautifulSoup(r.text, "html.parser")
        # Strategy: look at the full page text. Any occurrence of the deck name
        # near a $XX.XX price is a candidate. Take the lowest.
        text = soup.get_text(" ", strip=True)

        # Iterate each occurrence of the deck name (case-insensitive) and look
        # for a price within ~120 chars after it.
        name_lower = deck_name.lower()
        text_lower = text.lower()
        prices = []
        start = 0
        while True:
            idx = text_lower.find(name_lower, start)
            if idx < 0:
                break
            window = text[idx : idx + 200]
            # The first $XX.XX after the name is the product price.
            m = re.search(r"\$(\d+\.\d{2})", window)
            if m:
                price = float(m.group(1))
                # Filter obvious singles/chase cards — precons are $20+ sealed products.
                # Any "price" under $15 is almost certainly a card single, not a deck.
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


def fetch_zulus(deck_name: str) -> dict:
    """Zulus Games — Shopify suggest.json endpoint (structured JSON)."""
    query = urllib.parse.quote(deck_name)
    api_url = (
        f"https://www.zulusgames.com/search/suggest.json"
        f"?q={query}&resources[type]=product&resources[limit]=10"
    )
    human_url = f"https://www.zulusgames.com/search?q={query}"
    result = {"price": None, "url": human_url, "status": "unknown", "snippet": None}
    try:
        r = requests.get(api_url, headers=HEADERS, timeout=TIMEOUT)
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
            title = (p.get("title") or "").lower()
            if name_lower not in title:
                continue
            price_str = p.get("price")
            if not price_str:
                continue
            try:
                price = float(price_str)
            except (TypeError, ValueError):
                continue
            if price < 10.0:
                # guard against accessories/sleeves that sneak into search
                continue
            candidates.append((price, p.get("title"), p.get("url")))

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
        time.sleep(POLITE_SLEEP)

        zu = fetch_zulus(name)
        output["vendors"]["zulus"][did] = zu
        if zu["price"]:
            zu_hits += 1
            print(f"     Zulus ${zu['price']:>6.2f}  ({zu['status']})", flush=True)
        else:
            print(f"     Zulus ------  ({zu['status']})", flush=True)
        time.sleep(POLITE_SLEEP)

    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {out_path}")
    print(f"Card Kingdom: {ck_hits}/{len(decks)} hits")
    print(f"Zulus Games:  {zu_hits}/{len(decks)} hits")


if __name__ == "__main__":
    main()
