"""Lightweight integrity tests for the data files. Run with `python3 -m pytest`
or just `python3 test_data_integrity.py`. Pure stdlib — no pytest needed.
"""

import json
import re
from pathlib import Path

import jsonschema

ROOT = Path(__file__).parent


def load(name: str):
    return json.loads((ROOT / name).read_text())


def test_decks_schema():
    schema = load("decks.schema.json")
    decks = load("decks.json")
    jsonschema.validate(decks, schema)


def test_unique_deck_ids():
    decks = load("decks.json")
    ids = [d["id"] for d in decks]
    assert len(ids) == len(set(ids)), "duplicate deck ids in decks.json"


def test_id_slug_format():
    decks = load("decks.json")
    for d in decks:
        assert re.fullmatch(r"[a-z0-9-]+", d["id"]), f"bad id slug: {d['id']!r}"


def test_prices_match_decks():
    """Every deck in decks.json has a corresponding entry in each vendor map."""
    decks = load("decks.json")
    prices = load("prices.json")
    deck_ids = {d["id"] for d in decks}
    for vendor, entries in prices["vendors"].items():
        missing = deck_ids - set(entries.keys())
        assert not missing, f"{vendor} missing entries for: {sorted(missing)[:5]}..."
        extra = set(entries.keys()) - deck_ids
        assert not extra, f"{vendor} has stale entries for: {sorted(extra)[:5]}..."


def test_prices_shape():
    """Each vendor entry has the expected keys."""
    prices = load("prices.json")
    for vendor, entries in prices["vendors"].items():
        for did, entry in entries.items():
            assert "price" in entry, f"{vendor}/{did} missing price key"
            assert "url" in entry, f"{vendor}/{did} missing url key"
            assert "status" in entry, f"{vendor}/{did} missing status key"
            if entry["price"] is not None:
                assert isinstance(entry["price"], (int, float)), f"{vendor}/{did} non-numeric price"
                assert entry["price"] > 0, f"{vendor}/{did} non-positive price {entry['price']}"


def test_crack_shape():
    """crack map: keys are real deck ids; buy/sell numbers non-negative;
    sell never exceeds buy for the same vendor."""
    prices = load("prices.json")
    deck_ids = {d["id"] for d in load("decks.json")}
    crack = prices.get("crack", {})
    for did, c in crack.items():
        assert did in deck_ids, f"crack has stale deck id: {did}"
        for k in ("tcg", "cardkingdom", "sell_tcg", "sell_cardkingdom"):
            v = c.get(k)
            if v is not None:
                assert isinstance(v, (int, float)) and v >= 0, f"crack {did}.{k} bad: {v}"
        # Sell must not exceed buy for the same vendor (economically impossible).
        if c.get("sell_cardkingdom") is not None and c.get("cardkingdom") is not None:
            assert c["sell_cardkingdom"] <= c["cardkingdom"] + 1e-6, f"crack {did}: CK sell > buy"


def test_bundles_shape():
    """bundles: deck_ids reference real decks; price positive; savings>0 or null."""
    prices = load("prices.json")
    deck_ids = {d["id"] for d in load("decks.json")}
    for bid, b in prices.get("bundles", {}).items():
        assert isinstance(b.get("price"), (int, float)) and b["price"] > 0, f"bundle {bid} bad price"
        for did in b.get("deck_ids", []):
            assert did in deck_ids, f"bundle {bid} references unknown deck {did}"
        if b.get("savings") is not None:
            assert b["savings"] > 0, f"bundle {bid} non-positive savings stored: {b['savings']}"


def test_boxes_shape():
    """boxes: each entry has type/price; price positive; no duplicate type per set."""
    prices = load("prices.json")
    valid_types = {"play", "collector", "jumpstart"}
    for set_name, rows in prices.get("boxes", {}).items():
        seen = set()
        for r in rows:
            assert r.get("type") in valid_types, f"box {set_name} bad type {r.get('type')}"
            assert r["type"] not in seen, f"box {set_name} duplicate type {r['type']}"
            seen.add(r["type"])
            assert isinstance(r.get("price"), (int, float)) and r["price"] > 0, f"box {set_name} bad price"


def test_history_shape():
    """If prices_history.json exists, it has the expected shape."""
    p = ROOT / "prices_history.json"
    if not p.exists():
        return  # built up over time by the scraper
    history = json.loads(p.read_text())
    assert "decks" in history
    deck_ids = {d["id"] for d in load("decks.json")}
    for did, series in history["decks"].items():
        assert did in deck_ids, f"history has stale deck id: {did}"
        assert isinstance(series, list)
        for entry in series:
            assert "date" in entry and re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["date"])
            for k in ("tcg", "zulus"):
                if k in entry:
                    assert isinstance(entry[k], (int, float)) and entry[k] > 0
    # Box history (optional key): keyed "<set>::<type>", entries {date, price}.
    for key, series in (history.get("boxes") or {}).items():
        assert "::" in key, f"bad box history key: {key}"
        assert isinstance(series, list)
        for entry in series:
            assert "date" in entry and re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["date"])
            assert isinstance(entry.get("price"), (int, float)) and entry["price"] > 0


def test_crack_top_cards():
    """top_cards (chase-card breakdown, F1): list of {name, price}, prices
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
            assert isinstance(p, (int, float)) and p > 0, f"crack {did} top card bad price {p}"
            if prev is not None:
                assert p <= prev + 1e-6, f"crack {did} top_cards not sorted desc"
            prev = p


def test_boxes_ev():
    """box EV (F3): when present, a positive number labeled as an estimate."""
    prices = load("prices.json")
    for set_name, rows in prices.get("boxes", {}).items():
        for r in rows:
            if "ev" in r and r["ev"] is not None:
                assert isinstance(r["ev"], (int, float)) and r["ev"] > 0, \
                    f"box {set_name}/{r.get('type')} bad ev {r['ev']}"


def test_cards_index():
    """cards_index.json (F2 reverse staple finder): if present, maps card name
    keys to {name, decks:[real deck ids]}."""
    p = ROOT / "cards_index.json"
    if not p.exists():
        return  # produced alongside prices.json by the scraper
    idx = json.loads(p.read_text())
    cards = idx.get("cards")
    assert isinstance(cards, dict) and cards, "cards_index has no cards map"
    deck_ids = {d["id"] for d in load("decks.json")}
    for key, entry in cards.items():
        assert isinstance(entry.get("name"), str) and entry["name"], f"card {key!r} missing name"
        decks = entry.get("decks")
        assert isinstance(decks, list) and decks, f"card {key!r} has no decks"
        for did in decks:
            assert did in deck_ids, f"card {key!r} references unknown deck {did}"
        assert len(decks) == len(set(decks)), f"card {key!r} has duplicate deck ids"


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    raise SystemExit(failures)
