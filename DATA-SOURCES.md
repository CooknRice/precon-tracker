# Price Data Sources — what we use, what we rejected, and why

A record of where MTG Tracker's prices come from, how accurate they are, and
which sources were evaluated and deliberately **not** adopted. Written so these
decisions don't get re-litigated.

Last reviewed: **2026-07-23**

---

## Sources in use

| Source | What it gives us | Access |
|---|---|---|
| **TCGplayer Market** (via [TCGCSV](https://tcgcsv.com/)) | Primary price anchor for decks + sealed boxes | Free public mirror of TCGplayer's API, ~24h fresh |
| **Card Kingdom** | Retail + buylist for decks and boxes | Free public [sealed pricelist API](https://api.cardkingdom.com/api/sealed_pricelist), no auth |
| **Mana Pool** | 4th vendor + **realized sales** for decks and boxes | Free public [API](https://manapool.com/api/v1/prices/sealed), no auth |
| **Zulus Games** | Third vendor (partial: ~18/115 decks) | Direct JSON search endpoint |
| **MTGJSON** | Decklists, singles prices (crack value), booster models (box EV) | Free, MIT-licensed |

### How accurate is this?

Cross-checked against [PriceCharting](https://www.pricecharting.com/) (computed
purely from **completed eBay sales**) in July 2026:

| Box | Our TCG price | eBay-realized | Gap | Card Kingdom | CK premium |
|---|---|---|---|---|---|
| Tarkir Dragonstorm Collector | $414 | $414.75 | **−0.2%** | $500 | +17% |
| Tarkir Dragonstorm Play | $137 | $130.96 | **+4.4%** | $170 | +23% |
| Duskmourn Collector | $621 | $588.07 | **+5.3%** | $750 | +22% |

**Takeaway:** TCGplayer "Market" is not a retailer ask price — it's a realized-sales
consensus computed from completed multi-seller transactions, so it's the *same kind*
of signal as eBay sold listings. It lands within ~5% of true realized prices. Card
Kingdom consistently sits ~17–23% above and functions as our upper retail bound.
This is also the industry-standard anchor (Scryfall and MTGGoldfish both use
TCGplayer Market specifically).

---

## Evaluated and rejected

### Amazon — rejected (feasibility **and** value)

- **Official API is gated and mid-retirement.** PA-API 5 was deprecated 2026-04-30
  and the endpoint retired **2026-05-15**, replaced by the Creators API. Either way
  access requires being an approved Amazon Associate with ongoing qualifying sales
  (~10 per trailing 30 days). A hobby project with no affiliate sales cannot get or
  keep access — by design.
- **Scraping is out.** Amazon's Conditions of Use explicitly prohibit collecting
  listings/prices, and Amazon blocks datacenter/CI IP ranges — exactly what GitHub
  Actions runs on.
- **Even if we had it, the data is weak for MTG sealed.** Product identity is fine
  (clean WotC ASINs), but there's frequently *no price to show* — Amazon first-party
  MTG sealed inventory is largely absent. What does appear is third-party
  marketplace pricing that swings wildly on the same ASIN, and when present it lands
  right where we already are (e.g. Scions & Spellcraft: our TCG **$62.67** vs Amazon
  3P $65.88 vs CK $69.99 — near-zero marginal information). Plus a real
  counterfeit/resealed-return problem specific to MTG.
- **Nobody else does it either.** MTGGoldfish uses "TCGplayer Mid, CardKingdom, and
  Ebay"; SpellBook lists 16+ sources with no Amazon; MTGStocks uses TCG + CK. Even
  *bettercardsource.com* — a precon tracker that IS an Amazon Associate — sources its
  prices from TCGplayer/Mana Pool and uses Amazon only for outbound affiliate links.

### Keepa — technically viable, rejected on value

The one route that genuinely **bypasses the Amazon Associates gate**: a documented
REST API at `api.keepa.com` (current price, multi-year history, buy-box, offers,
sales rank), no affiliate account or qualifying sales needed, callable from a
datacenter IP without proxies.

Rejected because:
- **Paid, with no free tier and no sandbox.** €49/mo (Starter) is the floor of the
  API ladder — we'd use ~0.5% of it. (A €29/mo "Keepa Pro" tier has been reported to
  include 1 token/min of API access, enough for ~115 products/day if sharded across
  ~3 runs, but this is *unconfirmed* and Keepa calls it "primarily intended for
  testing.")
- **Licence is a gray zone for a public hobby site.** Their API terms say it's
  "available solely for business purposes… exclusively to entrepreneurs," limited to
  "the user's own business purposes," and ban resale without written consent. They
  don't explicitly forbid *displaying* data, and staff have told a user to chart raw
  API history on his own site — but it's genuinely ambiguous. An email to
  info@keepa.com would settle it (their terms name email as a valid consent channel).
  Unambiguously off-limits regardless: embedding the Keepa Box, hotlinking
  graph.keepa.com.
- **Provenance risk.** Keepa's data is substantially crowd-scraped via its browser
  extension, which Amazon's own terms forbid. That risk sits with Keepa (they warrant
  non-infringement), but their liability is capped at ~2× fees paid while the customer
  indemnifies them.

**Decision:** not worth €49/mo to add a frequently-blank, noisy column that duplicates
information we already have to within ~5%. Buying on TCGplayer at our tracked price
already leaves you ahead of what an Amazon column would reveal.

### CamelCamelCamel — rejected (dead end)

- **No public API, and there never has been** (`/api` and `/developers` 404).
- Only two **site-wide RSS firehoses** survive (`/top_drops/feed`, `/popular.xml`) —
  no per-product or per-search feed. They emit unstructured title strings with no way
  to request a specific ASIN, so they're useless for a fixed watchlist.
- **Their ToS bans both halves of what we'd do**, verbatim: *"Scraping and/or other
  automated data collection from our website and/or emails is prohibited.
  Republishing our data is prohibited."* Enforced with Cloudflare challenges; their
  robots.txt disallows AI/agent crawlers.
- **It sits on the same gate anyway** — CCC's data comes from Amazon's PA-API *as an
  Associate* (their founder is on record they've deliberately never scraped). Routing
  through CCC doesn't launder around the affiliate requirement; it just swaps an
  Amazon eligibility problem for a CCC contract breach.

CamelCamelCamel and Keepa are independent companies (no ownership link).

### Cardmarket (EU) — wanted, but blocked

The largest genuine gap (see below). Its API is **restricted to approved professional
sellers** via a manual approval process, and it explicitly prohibits apps that
"constantly only request the public Marketplace resources… on consecutive days" —
which is precisely a daily price tracker.

### eBay sold listings — wanted, but gated

Sold/completed-listing data is not openly available via API. Note also that eBay has
owned TCGplayer since 2022, so eBay and TCGplayer Market are no longer *fully*
independent venues.

---

## Known gaps (accepted)

1. **No European market.** Cardmarket (EUR) prices are independently determined, not
   a currency conversion of US prices. We are effectively a **US-only** view — and
   that is a deliberate scope choice, not an oversight.
2. ~~**No independent realized-price cross-check.**~~ **CLOSED (2026-07-24)** — Mana
   Pool's free public API returns `recent_sales`: actual completed transactions with
   timestamp, price and quantity. We now show "last sold" on 95 decks and 58 box rows.
   This is what PriceCharting was going to cost $49/mo for. It is a *non-eBay*,
   *non-TCGplayer* realized signal, so it is genuinely independent of our anchor.
3. **No other large US retailers** (SCG, ChannelFireball, CoolStuffInc). These are
   *ask* prices that sit at or above the realized market, so they add less than they
   appear to.

**Overall:** a faithful **US realized-consensus** view, not a full global picture.

## If we ever want to close the gaps

Ranked by value-per-effort, and both far better value than Amazon:

1. **PriceCharting** (or an equivalent eBay-sold aggregator) — the only verified,
   fetchable realized-eBay signal found; would add an independent cross-check.
2. **Cardmarket EUR via Scryfall/MTGJSON** — the practical way to get EU coverage
   without Cardmarket's restricted API.
3. Optionally one or two more US retailer asks to widen the best-buy-side view that
   Zulus only partly fills.
