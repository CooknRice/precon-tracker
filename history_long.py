"""Long price history: one TCGplayer Market point a week per product.

history_long.json holds, for every tracked deck, box type and set bundle, one
TCGplayer Market price a week: backfilled once from TCGCSV's daily price
archive (https://tcgcsv.com/docs, first archive 2024-02-08) and then extended
by the daily scrape through update_weekly(). The 90 day daily history stays
in prices_history.json; this file only adds the long view.

File shape (dump() writes it this way, one series per line, keys sorted):

    {"schema":1,
    "source":"TCGplayer Market price, one point a week (TCGCSV archive, then the daily scrape)",
    "series":{
    "box:<set name>::<type>":[["2024-02-09",120.5],...],
    "bundle:<bundle id>":[...],
    "deck:<deck id>":[["2024-02-09",41.23],["2024-02-16",40.1],...]
    }}

Dates follow prices_history.json: a point is dated by the run that would
have published it. The daily run is scheduled for 01:23 UTC and then reads
the TCGCSV snapshot of the evening before (built about 20:05 UTC), so the
archive of day D is the point of day D + 1. That holds for runs that start
before about 20:05 UTC, which most do although they often start hours late
(checked against prices_history.json: every point of the Fridays whose run
came before 20:05 UTC is equal). A run that starts later reads the snapshot
of its own day, so its point is one snapshot later than its date says; this
happened on two of the backfilled Fridays and will happen to a weekly point
update_weekly adds on such a day.

Used by scrape_prices.py main() on healthy runs:

    doc = history_long.load(path)     # a damaged file: marked, left alone
    if history_long.update_weekly(doc, prices, decks, run_date, box_sets):
        history_long.save(path, doc)

and once, locally, for the backfill from archives already on disk (it never
downloads anything):

    python3 history_long.py backfill --archives DIR --out history_long.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA = 1
SOURCE = "TCGplayer Market price, one point a week (TCGCSV archive, then the daily scrape)"
STEP_DAYS = 7               # a new point once the last one is at least this old
FIRST_ARCHIVE = "2024-02-08"  # TCGCSV's first daily archive
MAX_LATER_DAYS = 3          # a missing archive day is replaced by one up to 3 days later
SNAPSHOT_TO_RUN_DAYS = 1    # archive of day D = the run of day D + 1 (see above)
MAGIC_CATEGORY = "1"        # TCGplayer category id of Magic: The Gathering
ARCHIVE_NAME = re.compile(r"^prices-(\d{4}-\d{2}-\d{2})\.ppmd\.7z$")
PRODUCT_URL = re.compile(r"^https://www\.tcgplayer\.com/product/(\d+)(?:[/?#]|$)")
KEY_PREFIXES = ("deck:", "box:", "bundle:")
# load() sets this key, with the reason, on the empty doc it returns for a
# file that exists but cannot be used; dump() never writes it.
UNREADABLE = "_unreadable"


# ---- small helpers ----

def empty_doc() -> dict:
    return {"schema": SCHEMA, "source": SOURCE, "series": {}}


def _day(v) -> date | None:
    """A calendar date from a date, a datetime or an ISO 'YYYY-MM-DD' string."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _price(v) -> float | None:
    """A positive finite dollar amount rounded to cents, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if not math.isfinite(f) or f <= 0:
        return None
    f = round(f, 2)
    return f if f > 0 else None


def _clean_series(v) -> list:
    """Valid [date, price] points only, ascending, one per date (the last wins)."""
    by_date: dict = {}
    if isinstance(v, list):
        for p in v:
            if isinstance(p, (list, tuple)) and len(p) == 2:
                d, pr = _day(p[0]), _price(p[1])
                if d is not None and pr is not None:
                    by_date[d.isoformat()] = pr
    return [[d, by_date[d]] for d in sorted(by_date)]


def _thin(points: list, prefer=frozenset()) -> list:
    """Drop points closer than STEP_DAYS to the point kept before them.

    With `prefer` (ISO dates), the points on those dates are kept first and
    every other point only where it is at least STEP_DAYS from all kept
    points: the backfill prefers archives of the exact target day, so a
    replacement archive a few days late never pushes out the next week."""
    if prefer:
        keep = _thin([p for p in points if p[0] in prefer])
        kept = [date.fromisoformat(d) for d, _p in keep]
        for d, p in points:
            if d in prefer:
                continue
            day = date.fromisoformat(d)
            if all(abs((day - k).days) >= STEP_DAYS for k in kept):
                keep.append([d, p])
                kept.append(day)
        return sorted(keep)
    out: list = []
    last = None
    for d, p in points:
        day = date.fromisoformat(d)
        if last is None or (day - last).days >= STEP_DAYS:
            out.append([d, p])
            last = day
    return out


def _series(doc: dict) -> dict:
    s = doc.get("series")
    if not isinstance(s, dict):
        s = {}
        doc["series"] = s
    return s


def former_id_map(decks) -> dict:
    """former deck id -> current id, from decks.json formerIds (the scraper's
    rule: an id that is still a live deck is never a former one)."""
    if not isinstance(decks, list):
        return {}
    current = {d.get("id") for d in decks if isinstance(d, dict) and isinstance(d.get("id"), str)}
    out: dict = {}
    for d in decks:
        if not isinstance(d, dict) or not isinstance(d.get("id"), str):
            continue
        for fid in d.get("formerIds") or []:
            if isinstance(fid, str) and fid != d["id"] and fid not in current:
                out.setdefault(fid, d["id"])
    return out


def _current_deck_ids(decks) -> set | None:
    if not isinstance(decks, list):
        return None
    ids = {d.get("id") for d in decks if isinstance(d, dict) and isinstance(d.get("id"), str)}
    return ids or None


def move_renamed(doc: dict, decks) -> int:
    """Move deck series of renamed decks to their current id (merged by date,
    the current id's point winning a shared date, then thinned to one point a
    week). Returns how many series moved."""
    series = _series(doc)
    moved = 0
    for fid, did in former_id_map(decks).items():
        old_key, new_key = f"deck:{fid}", f"deck:{did}"
        if old_key not in series:
            continue
        old = _clean_series(series.pop(old_key))
        new = _clean_series(series.get(new_key))
        merged = {d: p for d, p in old}
        merged.update({d: p for d, p in new})
        series[new_key] = _thin([[d, merged[d]] for d in sorted(merged)])
        moved += 1
    return moved


def drop_untracked(doc: dict, decks, box_sets=None) -> int:
    """Drop the series of decks no longer in decks.json (move_renamed has
    already moved those of former ids) and, when `box_sets` is given, the box
    series of sets the box phase no longer prices, the way prices_history.json
    drops them (scrape_prices.migrate_history_ids, run_history_migrations).
    Nothing is dropped when decks gives no deck ids or box_sets is empty, so
    an unread decks.json never empties the file. Bundle series stay: a bundle
    the matcher misses for a day is only absent from that day's prices.json.
    Returns how many series were dropped."""
    series = _series(doc)
    current = _current_deck_ids(decks)
    sets = set(box_sets) if box_sets else None
    gone = []
    for key in sorted(series):
        if current is not None and key.startswith("deck:") and key[5:] not in current:
            gone.append(key)
        elif sets is not None and key.startswith("box:") and key[4:].rpartition("::")[0] not in sets:
            gone.append(key)
    for key in gone:
        n = len(series.pop(key) or [])
        print(f"  history_long: dropped {n} points of {key}, no longer tracked", flush=True)
    return len(gone)


# ---- the interface scrape_prices.py uses ----

def load(path) -> dict:
    """The doc at `path`. A missing file gives an empty doc (the first run
    starts the file). A file that exists but cannot be read or parsed, or
    holds no "series" object, gives an empty doc marked with UNREADABLE (the
    reason): update_weekly then adds nothing and save refuses it, so a damaged
    file (cut short, or holding merge conflict markers) is left for a person
    to restore instead of being replaced by one point per product. Malformed
    points and series of a readable file are dropped."""
    doc = empty_doc()
    p = Path(path)
    if not p.exists():
        return doc
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        doc[UNREADABLE] = f"{type(e).__name__}: {e}"[:200]
        return doc
    series = raw.get("series") if isinstance(raw, dict) else None
    if not isinstance(series, dict):
        doc[UNREADABLE] = 'no "series" object'
        return doc
    for k, v in series.items():
        if isinstance(k, str) and k.startswith(KEY_PREFIXES):
            pts = _clean_series(v)
            if pts:
                doc["series"][k] = pts
    return doc


def _candidates(prices: dict, decks) -> list:
    """(series key, record) for every deck, box row and bundle in prices.json
    whose TCGplayer price is a Market price observed this run."""
    out: list = []
    seen: set = set()
    id_map = former_id_map(decks)
    current = _current_deck_ids(decks)

    tcg = (prices.get("vendors") or {}).get("tcgplayer") if isinstance(prices.get("vendors"), dict) else None
    if isinstance(tcg, dict):
        # current ids first, so a stale former id record never wins
        for did in sorted(tcg, key=lambda k: (k in id_map, str(k))):
            if not isinstance(did, str):
                continue
            cur = id_map.get(did, did)
            if current is not None and cur not in current:
                continue
            out.append((f"deck:{cur}", tcg[did]))

    boxes = prices.get("boxes")
    if isinstance(boxes, dict):
        for set_name, rows in boxes.items():
            if not isinstance(set_name, str) or not isinstance(rows, list):
                continue
            for r in rows:
                # rows carried from an earlier run were not observed today
                if isinstance(r, dict) and isinstance(r.get("type"), str) and not r.get("carried_from"):
                    out.append((f"box:{set_name}::{r['type']}", r))

    bundles = prices.get("bundles")
    if isinstance(bundles, dict):
        for bid, b in bundles.items():
            if isinstance(bid, str):
                out.append((f"bundle:{bid}", b))

    unique = []
    for key, rec in out:
        if key not in seen and isinstance(rec, dict):
            seen.add(key)
            unique.append((key, rec))
    return unique


def update_weekly(doc, prices, decks, run_date, box_sets=None) -> int:
    """Append [run_date, price] to every deck, box row and bundle series whose
    TCGplayer price in `prices` (prices.json) is a Market price
    (price_source == "Market"), when the series is empty or its last point is
    at least STEP_DAYS before run_date. Series of renamed decks (decks.json
    formerIds) move to the current id first, and series no longer tracked
    are dropped (drop_untracked; box series only when `box_sets`, the sets
    the box phase prices, is given). A doc load() marked UNREADABLE is left
    alone. Returns the number of points added; never raises."""
    added = 0
    try:
        if not isinstance(doc, dict):
            return 0
        if doc.get(UNREADABLE):
            print(f"  history_long: file could not be read ({doc[UNREADABLE]}), "
                  f"nothing added", flush=True)
            return 0
        doc["schema"] = SCHEMA
        doc["source"] = SOURCE
        series = _series(doc)
        day = _day(run_date)
        try:
            move_renamed(doc, decks)
        except Exception as e:
            print(f"  history_long: rename step skipped: {e}", flush=True)
        try:
            drop_untracked(doc, decks, box_sets)
        except Exception as e:
            print(f"  history_long: untracked series step skipped: {e}", flush=True)
        if day is None or not isinstance(prices, dict):
            return 0
        today = day.isoformat()
        for key, rec in _candidates(prices, decks):
            try:
                if rec.get("price_source") != "Market":
                    continue
                price = _price(rec.get("price"))
                if price is None:
                    continue
                pts = series.get(key)
                if not isinstance(pts, list):
                    pts = []
                if pts:
                    last = _day(pts[-1][0]) if isinstance(pts[-1], (list, tuple)) and pts[-1] else None
                    if last is None:
                        pts = _clean_series(pts)
                        last = _day(pts[-1][0]) if pts else None
                    if last is not None and (day - last).days < STEP_DAYS:
                        continue  # also covers a clock that went backwards
                pts.append([today, price])
                series[key] = pts
                added += 1
            except Exception as e:
                print(f"  history_long: skipped {key}: {e}", flush=True)
    except Exception as e:
        print(f"  history_long: update stopped: {e}", flush=True)
    return added


def dump(doc: dict) -> str:
    """Deterministic text: header keys on their own lines, then one series
    per line, keys sorted, dates ascending. Refuses NaN and Infinity."""
    def one(v):
        return json.dumps(v, separators=(",", ":"), allow_nan=False)

    series = doc.get("series") if isinstance(doc, dict) else None
    series = series if isinstance(series, dict) else {}
    lines = [f"{one(k)}:{one(_clean_series(series[k]))}" for k in sorted(series)]
    body = "{\n" + ",\n".join(lines) + "\n}" if lines else "{}"
    return f'{{"schema":{SCHEMA},\n"source":{one(SOURCE)},\n"series":{body}}}\n'


def save(path, doc: dict) -> None:
    """Write dump(doc) to `path` atomically (a temporary file in the same
    folder, flushed, then renamed over the old file). Refuses a doc load()
    marked UNREADABLE, which would replace the damaged file with nothing."""
    path = Path(path)
    if isinstance(doc, dict) and doc.get(UNREADABLE):
        raise ValueError(f"{path.name} could not be read ({doc[UNREADABLE]}); not overwriting it")
    text = dump(doc)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent or "."))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp makes the file 0600; the other data files are 0644.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- one time backfill from TCGCSV archives already on disk ----

def product_id(url) -> int | None:
    m = PRODUCT_URL.match(url or "") if isinstance(url, str) else None
    return int(m.group(1)) if m else None


def product_ids(prices: dict, decks) -> dict:
    """series key -> current TCGplayer product id, from the url fields of
    prices.json (vendors.tcgplayer, boxes, bundles). Deck keys use current
    deck ids."""
    out: dict = {}
    current = _current_deck_ids(decks)
    id_map = former_id_map(decks)
    for did, r in ((prices.get("vendors") or {}).get("tcgplayer") or {}).items():
        cur = id_map.get(did, did)
        pid = product_id((r or {}).get("url"))
        if pid and (current is None or cur in current):
            out.setdefault(f"deck:{cur}", pid)
    for set_name, rows in (prices.get("boxes") or {}).items():
        for r in rows or []:
            pid = product_id((r or {}).get("url"))
            if pid and isinstance(r.get("type"), str):
                out.setdefault(f"box:{set_name}::{r['type']}", pid)
    for bid, b in (prices.get("bundles") or {}).items():
        pid = product_id((b or {}).get("url"))
        if pid:
            out.setdefault(f"bundle:{bid}", pid)
    return out


def archives_on_disk(adir: Path) -> dict:
    """archive date (ISO) -> path, for prices-YYYY-MM-DD.ppmd.7z files."""
    out = {}
    for f in adir.iterdir():
        m = ARCHIVE_NAME.match(f.name)
        if m and f.is_file() and _day(m.group(1)):
            out[m.group(1)] = f
    return out


def run_day_of(archive_day: str) -> str:
    """The date of the point an archive gives (archive of day D: day D + 1)."""
    return (date.fromisoformat(archive_day) + timedelta(days=SNAPSHOT_TO_RUN_DAYS)).isoformat()


def weekly_plan(available: dict, first: str, last: str) -> tuple:
    """([(target date, archive date used)], [targets with no archive]): one
    target every STEP_DAYS from `first` to `last`; a missing day is replaced
    by the first archive at most MAX_LATER_DAYS later."""
    plan, missing = [], []
    t, end = date.fromisoformat(first), date.fromisoformat(last)
    while t <= end:
        for k in range(MAX_LATER_DAYS + 1):
            d = (t + timedelta(days=k)).isoformat()
            if d in available:
                plan.append((t.isoformat(), d))
                break
        else:
            missing.append(t.isoformat())
        t += timedelta(days=STEP_DAYS)
    return plan, missing


def _extract_magic(archive: Path, dest: Path) -> None:
    """Extract the Magic category of one archive into the empty folder
    `dest`, with bsdtar (libarchive reads 7z PPMd) or else py7zr."""
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        r = subprocess.run([bsdtar, "-x", "-f", str(archive), "-C", str(dest), f"*/{MAGIC_CATEGORY}/*"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"bsdtar could not read {archive.name}: {r.stderr.strip()[:300]}")
        return
    try:
        import py7zr  # optional, only for machines without bsdtar
    except ImportError:
        raise RuntimeError("neither bsdtar nor py7zr is available to read 7z PPMd archives")
    with py7zr.SevenZipFile(archive, "r") as z:
        names = [n for n in z.getnames() if n.split("/")[1:2] == [MAGIC_CATEGORY]]
        z.extract(path=dest, targets=names)


def read_market_prices(archive: Path, wanted: set) -> dict:
    """product id -> marketPrice (None when TCGplayer had none) for the
    wanted ids in one archive. Prefers the 'Normal' row of a product."""
    found: dict = {}
    tmp = Path(tempfile.mkdtemp(prefix="tcgcsv-"))
    try:
        _extract_magic(archive, tmp)
        for day_dir in tmp.iterdir():
            cat = day_dir / MAGIC_CATEGORY
            if day_dir.is_symlink() or not cat.is_dir() or cat.is_symlink():
                continue
            for group in cat.iterdir():
                f = group / "prices"
                if group.is_symlink() or f.is_symlink() or not f.is_file():
                    continue
                doc = json.loads(f.read_text(encoding="utf-8"))
                for row in (doc.get("results") or []) if isinstance(doc, dict) else []:
                    pid = row.get("productId") if isinstance(row, dict) else None
                    if pid not in wanted:
                        continue
                    found.setdefault(pid, []).append(
                        (row.get("subTypeName") != "Normal", _price(row.get("marketPrice"))))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = {}
    for pid, rows in found.items():
        priced = sorted((not_normal, i, mp) for i, (not_normal, mp) in enumerate(rows) if mp is not None)
        out[pid] = priced[0][2] if priced else None
    return out


def backfill(adir: Path, out: Path, prices: dict, decks, first: str = FIRST_ARCHIVE,
             last: str | None = None, workers: int = 4) -> dict:
    """Build the weekly series from the archives in `adir`, keep the points
    the existing `out` file holds outside the rebuilt range (before `first`,
    and after the last archive: the daily run's), write `out` and return a
    report."""
    t0 = time.monotonic()
    ids = product_ids(prices, decks)
    wanted = set(ids.values())
    available = archives_on_disk(adir)
    if not available:
        raise SystemExit(f"no prices-YYYY-MM-DD.ppmd.7z archives in {adir}")
    last = last or max(available)
    plan, missing = weekly_plan(available, first, last)

    def one(item):
        target, d = item
        return target, d, read_market_prices(available[d], wanted)

    points: dict = {k: [] for k in ids}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for target, d, mk in pool.map(one, plan):
            run_day = run_day_of(d)
            for key, pid in ids.items():
                p = mk.get(pid)
                if p is not None:
                    points[key].append([run_day, p])
            print(f"  {d}: {sum(1 for v in mk.values() if v is not None)} of {len(wanted)} priced", flush=True)

    doc = empty_doc()
    old_doc = load(out)
    if old_doc.get(UNREADABLE):
        print(f"  {out.name} could not be read ({old_doc[UNREADABLE]}); rebuilding it "
              f"from the archives alone", flush=True)
    old = old_doc["series"]
    # Points from an archive of the exact target day win over a replacement
    # archive's (see _thin); old points dated before the rebuilt range (a run
    # with --first) or after its last point (the daily run's) are kept.
    exact = frozenset(run_day_of(d) for t, d in plan if t == d)
    start = run_day_of(plan[0][0]) if plan else None
    for key in sorted(set(points) | set(old)):
        pts = _clean_series(points.get(key, []))
        if pts:
            earlier = [p for p in old.get(key, []) if start and p[0] < start]
            later = [p for p in old.get(key, []) if p[0] > pts[-1][0]]
            pts = _thin(earlier + pts + later, exact)
        else:
            pts = old.get(key, [])
        if pts:
            doc["series"][key] = pts
    save(out, doc)
    return {
        "archives_used": len(plan),
        "first_archive": plan[0][1] if plan else None,
        "last_archive": plan[-1][1] if plan else None,
        "replaced_days": [[t, d] for t, d in plan if t != d],
        "missing_targets": missing,
        "products": len(ids),
        "series_written": len(doc["series"]),
        "no_points": sorted(k for k in ids if not points[k]),
        "seconds": round(time.monotonic() - t0, 1),
        "bytes": out.stat().st_size,
    }


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Long TCGplayer price history (history_long.json).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="build history_long.json from TCGCSV archives on disk (no download)")
    b.add_argument("--archives", required=True, type=Path, help="folder of prices-YYYY-MM-DD.ppmd.7z files")
    b.add_argument("--out", required=True, type=Path, help="history_long.json to write")
    b.add_argument("--prices", type=Path, default=here / "prices.json", help="prices.json for the product ids")
    b.add_argument("--decks", type=Path, default=here / "decks.json", help="decks.json for current deck ids")
    b.add_argument("--first", default=FIRST_ARCHIVE, help="first target date (default %(default)s)")
    b.add_argument("--last", default=None, help="last target date (default: latest archive on disk)")
    b.add_argument("--workers", type=int, default=4, help="archives read at once (default %(default)s)")
    a = ap.parse_args(argv)
    prices = json.loads(a.prices.read_text(encoding="utf-8"))
    decks = json.loads(a.decks.read_text(encoding="utf-8"))
    report = backfill(a.archives.expanduser(), a.out, prices, decks, a.first, a.last, a.workers)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
