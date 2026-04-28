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
