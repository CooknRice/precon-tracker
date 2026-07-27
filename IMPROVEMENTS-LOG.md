# Improvements Log

A running record of self-directed improvement iterations on MTG Tracker, so
each round builds on the last instead of repeating it.

**Rules each iteration follows:**
1. Read this log first — never redo finished work.
2. Pick the single highest-value improvement not yet done, weighing visuals/UX,
   features, data, performance, correctness and accessibility.
3. Verify before claiming: `py_compile`, `test_data_integrity.py`, a real
   browser check (both themes, mobile), and no console errors.
4. Commit + push to canonical (`~/precon-tracker`), rebasing over the bot's
   daily commit; confirm the push landed via the GitHub API.
5. Append one entry below.

Constraints that hold across all iterations: **US prices only**; no paid data
sources; no European/Cardmarket data. See `DATA-SOURCES.md` for what has been
evaluated and rejected, and `DESIGN.md` for the visual system.

---

## Baseline (2026-07-24)

State when the loop started — all shipped and verified:

- **4 vendors**: TCGplayer (via TCGCSV), Card Kingdom, Mana Pool, Zulus, with a
  best-price-across-vendors headline on every deck.
- **Realized sales** from Mana Pool (`recent_sales`) on 95 decks / 58 box rows —
  the only sold-side signal; everything else is an asking price.
- **Sealed boxes**: 31 sets, expected-value estimates, per-vendor comparison.
- **Buying intelligence**: 90-day history, best-price sparklines and charts,
  all-time-low badges, buy signals, ~30-day forecast, crack value (buy/sell),
  chase cards, set bundles, staple finder, portfolio P&L, watchlist + targets.
- **Quality**: 18/18 data-integrity tests (run in CI), JSON schema validation,
  coverage gates, atomic writes, PWA, light/dark themes, keyboard a11y.

| # | Date | Improvement | Verified by |
|---|------|-------------|-------------|
| — | 2026-07-24 | Baseline above | 18/18 tests, browser, live push |
| 3 | 2026-07-27 | **Made the numbers mean what they say.** Three displayed figures were measured and found wrong: (a) **box EV pack counts** — `BOX_PACKS["play"]=30`, but 14 of 28 "play" boxes are pre-2024 *Draft Booster* displays holding **36** packs, understating their EV by ~17%; now derived from the SKU name we actually priced (`packs_for_sku`). (b) The **"Nd" range label** counted history *entries*, but entries are written only on price changes — so an 89-day window displayed as "74d", wrong on **111/111** decks; now a real calendar span (`spanDays`). (c) **"★ all-time low" compared across a shifting basis**: `tcg` appears in 99% of history entries but `mp` in 4%, so adding Mana Pool made `best` drop on **20 decks in one day** — a vendor onboarding, not a price move — firing false badges. Now the winning vendor must have ≥21 days of its own history and is compared against **its own** past prices. | 21/21 tests; EV change was surgical — exactly the 14 Draft-booster boxes moved, each +20–21% (=36/30), no other type touched; all-time-low simulated old-vs-new on real history (**8 false badges suppressed**, total 15→18 as per-vendor comparison surfaced genuine lows); spot-checked quandrix-unlimited (74 entries / 89-day span) now renders "89d"; both themes, 375px mobile, zero console errors |
| 2 | 2026-07-27 | **Said it once, and only when true.** Removed four sources of false precision and redundancy, all measured first: (a) the **~30-day forecast** — it damped its own extrapolation by half, ran on ≤14 change-triggered points, produced confidence bands up to **290%** of price, and on **23 decks** directly contradicted the buy signal (same trend rendered as "trending down — good buy" *and* "cheaper in 30 days — wait", three lines apart); (b) the **release-curve tag** — a pure function of releaseDate, which the card already prints, reading "Maturing ↑" on **105 of 115** decks with its "Just released" state matching **zero** (2 dead CSS rules); (c) the **"Best deals right now"** panel — overlapped the unified feed on **4 of 6** picks, and Deal-of-the-Day is the feed's #1 by construction; (d) the **"Best value to crack"** panel — typically 2 decks, the second worth **$0.13** net. Kept Deal-of-the-Day (hero), the unified feed (only panel spanning decks+boxes+bundles), and per-card crack value. Also swept ~1.4 KB of now-dead CSS (`.curve-tag*`, `.price-forecast*`, `.crack-deals*`, `.bd-margin/crackline/sealed/low`). | 21/21 tests; every claim verified against real data before deleting; browser-checked in both themes + 375px mobile with zero console errors and no horizontal overflow; **502px less scrolling** to the first deck card (3494→2992) |
| 1 | 2026-07-27 | **Stopped recommending the wrong product.** Vendor matching could price a deck off a different SKU — and because wrong SKUs are cheaper, they won the 🏆 best-price headline. Urza's Iron Alliance showed **$40.55 from a prerelease pack** (real ~$156, and Mana Pool's own sales said $166); Lorehold Legacies **$34.99 from a "(Deck Only)"** SKU (real $90); 6 of 18 Zulus matches were Commander Kits or boxless SKUs. Root cause was matching on name without checking product kind or release. Added `is_same_product_kind()` (shared, asymmetric so a deck whose own name carries a marker doesn't self-reject), `name_match_is_same_release()` (extra words beyond the deck name must be generic or in our set name), and a self-consistency guard voiding any asking price under half that vendor's own realized sales. Also removed a duplicate `test_prices_schema` that had been silently shadowing a better one. | 21/21 tests (3 new ones **confirmed failing on the old data first**); guards unit-tested on 15 real cases; audited every dropped match to prove no over-rejection — recovered 5 legitimate Commander-2021 decks a blunter first attempt had killed; browser-verified the false headline is gone and Lorehold now shows the correct $59.99 |
