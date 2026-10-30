# Price Data Sources: what we use, what we rejected, and why

A record of where MTG Tracker's numbers come from, how each one is computed,
and which sources were evaluated and deliberately **not** adopted. Written so
these decisions don't get relitigated.

Last reviewed: **2026-10-30** (after the audit fix round and the Star Trek
preorders, see `IMPROVEMENTS-LOG.md` row 6)

---

## Sources in use

| Source | What it gives us | Access |
|---|---|---|
| **TCGplayer** (via [TCGCSV](https://tcgcsv.com/)) | Deck, sealed box and set bundle prices: Market, lowest listing, product links | Free public mirror of TCGplayer's catalog and prices, republished daily around 20:05 UTC |
| **Card Kingdom** | Deck and box retail price, stock count and buylist price | Free official [sealed pricelist API](https://api.cardkingdom.com/api/sealed_pricelist), no auth |
| **[MTGJSON](https://mtgjson.com/)** | Decklists, per card prices (crack value), sealed product contents (bundles, pack counts), booster models (box EV) | Free, MIT License (see [Attribution and licences](#attribution-and-licences)) |
| **[ScryCheck](https://scrycheck.com/precons)** | Power score and bracket per deck | Hand entered in `decks.json`; the scraper never calls it |
| **Wizards of the Coast** | Product names, release dates, recent MSRPs, face commanders | Hand entered in `decks.json` from Wizards product pages, buyers guides and decklists |

Only TCGplayer and Card Kingdom are price **vendors** (places the page sends
you to buy). Zulus Games and Mana Pool were removed on 2026-10-07 for terms
reasons, and Amazon was never added (see [Evaluated and rejected](#evaluated-and-rejected)).

The scraper (`scrape_prices.py`) runs once a day in GitHub Actions at 01:23 UTC
and writes `prices.json`, `prices_history.json` and `cards_index.json`. It
records the publish date of each upstream feed it read in `prices.json`
`upstream`, for example `{"tcgcsv": "2026-10-29T20:06:01+00:00", "mtgjson": "2026-10-30"}`.
Counts below are from the run of 2026-10-30 unless a date says otherwise.

---

## TCGplayer (via TCGCSV)

### What "Market" means

TCGplayer's [Market Price](https://help.tcgplayer.com/hc/en-us/articles/222376867-What-do-the-different-price-points-on-TCGplayer-com-mean)
is computed from **recent completed sales** on TCGplayer.com. It is a sales
based figure, not a seller's asking price, so it is the same kind of signal as
eBay sold listings (see [How accurate is this?](#how-accurate-is-this)).
TCGCSV also gives **Low** (the lowest current listing) and **Mid**.

Because Market looks backwards, it can sit **below every current listing**:
nobody is offering the product at that price today. On 2026-10-30 that was
true for 16 of 121 decks.

### What we record and show

- `prices.json` `vendors.tcgplayer[id]`: `price` is Market. Only when a product
  has no Market price does it fall back to Low, then Mid, and `price_source`
  says which (all 121 decks were Market on 2026-10-30). `price_low` is the
  lowest current listing. `url` is the product page.
- **History records Market only.** `prices_history.json` `tcg` is the
  TCGplayer Market series. A day on the Low or Mid fallback is not recorded
  (the Market value in force carries on), and a listing price is never spliced
  into the series or a chart.
- **The page headlines the price you can pay.** That is Market, or the lowest
  listing when Market sits below every listing, and the label says which:
  "TCGPlayer Market" or "TCGPlayer lowest listing", with the other figure shown
  beside it as a note. Card Kingdom wins the headline instead when it is in
  stock and cheaper. Every signal on the page (discount, targets, Deal of the
  Day, feed, sort, compare, portfolio) reads this one price.
- **Charts show only what was recorded.** The sparkline, the chart line and
  the printed range use only the recorded series (`best`: TCGplayer Market, or
  Card Kingdom in stock when lower). When today's headline differs from where
  the line ends (for example a lowest listing headline), the chart draws it
  apart from the line as a hollow dot joined by a dashed link, with a note
  under the chart, and leaves it out of the change, the range and the trend.

### How decks and boxes are matched

- **Groups** (TCGplayer's sets): token based matching against the deck's set,
  with `GROUP_OVERRIDES` first (Commander 2021 maps to group 2801) and two
  release guards: pinned tokens (a year, or a token in parentheses) must
  appear in the group, and a group published more than 90 days after the
  decks' release is rejected.
- **Products**: a listing of another kind is rejected (Collector's Edition,
  Game Edition, Minimal Packaging, Deluxe, Deck Only, prerelease, bundles and
  similar, unless the deck's own name carries the word). A precon named
  product ranks above one that merely has a price, and anything under $5 is
  rejected as a single (`PRICE_FLOOR`). CI also checks that every vendor
  listing names its deck.
- **Boxes**: the `play` row covers Play, Draft and Set Booster boxes. It
  prefers Play, then Draft, then Set, records the choice as `kind`, and names
  it in `label` ("Draft Booster Box"). Card Kingdom is joined by the same kind
  and EV uses that kind's booster model. A plain "Booster Display" or "Booster
  Box" that names no kind (Mystery Booster Commander Edition) is a `play` row
  of kind `booster` with the `label` "Booster Box". The page tile shows the
  SKU's own kind text after " - " ("Booster Display" for that set) and uses
  `label` only when the name has none. Other rows are `collector` and
  `jumpstart`.
- Every box row carries `set_release`, the ISO date the set's main TCGplayer
  group was published (`publishedOn`), or null when TCGCSV gives none.

Coverage on 2026-10-30: 121 of 121 decks, 33 set bundles, 71 box rows across
34 sets (32 deck sets plus the two [box only sets](#box-only-sets)).

---

## Card Kingdom

- One request to the official sealed pricelist gives every SKU's retail price,
  stock count (`qty`) and buylist price (`buy`).
- The indexer accepts deck SKUs named like "Commander 2021 - Deck". The CK
  edition must name the same release as the deck's set (a word comparison);
  there is no name only fallback across releases.
- **Only stock counts.** A sold out deck (`qty` 0) never wins the headline and
  is never recorded in history; the page shows it as a reference line marked
  "(out of stock)". An unknown stock count counts as in stock. In the
  committed data of 2026-10-30 (written 18:54 UTC), 121 of 121 decks matched
  and 18 were sold out (two of them Star Trek preorders).
- If the pricelist fails to load, every deck is marked `unavailable` (not
  `no-match`) and history keeps the previous CK values for that day.
- The buylist feeds the crack math ("profit to crack" uses the real CK
  buylist only).

### Vendor links

The scraper keeps a vendor link only if it is `https` on exactly
`www.tcgplayer.com` or `www.cardkingdom.com`; anything else is replaced by that
vendor's search link before `prices.json` is written. On the page, every link
built from data goes through `safeUrl`, which allows only tcgplayer.com,
cardkingdom.com and scrycheck.com; the fixed footer links (TCGCSV, MTGJSON,
Wizards, GitHub) are written into the page itself.

---

## MTGJSON

Files read each run: `DeckList.json` and each deck's own file (contents, the
commander zone, `isFoil` and `isEtched`), `AllPricesToday.json.gz` (about
5.5 MB gzipped: TCGplayer retail and Card Kingdom retail and buylist per card
and finish), `SetList.json` (sealed product contents), each set's booster
model, and `Meta.json` (publish date). Everything is downloaded fresh each
run, about 150 requests; caching it is deferred (see the log).

- **Decklist matching**: by name, and the decklist must be released within 30
  days of the deck (`DECK_FILE_MAX_DAYS`), so a reprint with the same name
  years apart is skipped. Four Final Fantasy decks are pinned in
  `DECK_FILE_OVERRIDES`. 117 of 121 decks match today; the four Star Trek
  preorders have no MTGJSON decklist yet (see [Preorders](#preorders)).
- **Crack value** (`prices.json` `crack`): the sum of every card's Near Mint
  price, per vendor. Each card is priced in the finish the precon holds (foil
  or etched), falling back to another finish only when that one has no price,
  and the CK buylist is read in the same finish as the CK retail price.
  `card_count`, `priced` and `missing` count card copies (a 100 card deck
  reports 100).
  - On the page, **buy** is the cheaper of the two retail totals and **sell**
    is the real Card Kingdom buylist. `sell_tcg` (70% of TCGplayer retail) is
    only ever shown as a labelled estimate.
  - "Profit to crack" needs a margin of at least the larger of $10 or 15% of
    the sealed price, and is stated before tax, shipping and grading.
- **`cards_index.json`**: card name to the decks that contain it, built from
  the decklists; it powers the staple finder.
- **When MTGJSON fails**: a deck whose decklist fails to download keeps the
  previous run's crack values (`carried_from`, at most 3 days old) and its
  `cards_index` entries. A `DeckList.json` outage fails the crack phase and
  carries every deck the same way.
- **When MTGJSON has no decklist yet** (a new or preorder deck), the run logs
  "no MTGJSON decklist yet, crack value left out". The deck gets no `crack`
  entry and no `cards_index` entries, it is not counted as a failure, and no
  stale value is carried. The crack and card index floors count only released
  decks (release date on or before the run), so a preorder never lowers them;
  a released deck still waiting for its decklist does count against them.

---

## Box EV rules

`prices.json` records the rules in `box_ev_basis`:

```
{"prices": "tcgplayer_retail", "bulk_threshold": 1.0, "bulk_value": 0,
 "min_coverage": 0.9, "box_toppers": false}
```

- **What it is**: the expected TCGplayer retail value of the cards in one box,
  counting only cards worth **$1.00 or more** each. Cheaper cards count as $0,
  because they sell only as bulk. Seller fees and box toppers are not included.
- **Per pack**: MTGJSON's booster model (sheet weights times each card's
  TCGplayer retail). Etched and foil only sheets are priced in their own
  finish.
- **Pack count**: read from MTGJSON sealed product contents for the exact SKU
  priced (box topper packs excluded), stored as `packs`. If no SKU matches, the
  set's other MTGJSON boxes of the same kind are used when they all agree. A
  typical count for the kind (`PACKS_BY_KIND`) is never used for an EV: with
  no MTGJSON count the EV is left out and the run logs "no MTGJSON pack count
  ..., EV left out". A set with no MTGJSON booster model yet gets no EV either
  ("MTGJSON TRK has no booster model yet").
- **Coverage**: `ev_coverage` is the share of the estimated value backed by
  real card prices, weighted by value. An unpriced card is guessed from priced
  cards of its rarity on its own sheet, then of its rarity and finish in the
  model, then a fixed prior. The EV is published only at **0.90 or more**.
  March of the Machine's collector box is withheld today (0.65), because 4 of
  its 5 serialized Praetors are unpriced and carry about a third of its value.
- **The honest result**: on 2026-10-30, 67 of 71 box rows have an EV and only
  5 are at or above the box price (Commander Legends: Battle for Baldur's Gate
  collector, Commander Masters play, Marvel Super Heroes Jumpstart, Modern
  Horizons 3 play, Murders at Karlov Manor play). When bulk counted at retail
  and pack counts came from a fixed table, 31 boxes looked like profitable
  cracks. Fewer wins is the correct answer, not a regression. The four rows
  with no EV are March of the Machine collector (coverage), Mystery Booster
  Commander Edition (no MTGJSON pack count) and both Star Trek boxes (no
  booster model yet).
- The page's EV note builds its rules from `box_ev_basis` ("Only cards worth
  $1.00 or more count; cheaper cards count as $0, since they rarely sell."),
  so the text follows the data.
- It is an average over many boxes; any single box varies widely.

---

## Set bundle savings basis

- **Which products are bundles**: a TCGplayer product is attached to a set's
  decks only when MTGJSON's sealed contents list exactly one of each of our
  decks (and at least half our deck names appear), its name passes a same
  release word check, and it is not a premium edition or starter kit. Without
  MTGJSON contents, a count in the name must equal our deck count, or the
  price must fall within 0.5 to 1.3 times the members' total.
  `contents_source` says which test passed (`mtgjson`, `name` or `price`); all
  33 bundles are `mtgjson` today.
- **Savings**: `individual_total` is the sum, over the set's decks, of each
  deck's cheapest price a buyer can pay today: TCGplayer (Market, or the
  lowest listing when Market sits below every listing) or Card Kingdom when
  in stock (`individual_basis: "cheapest_payable"`). `savings` is that total
  minus the bundle's own payable price (its lowest listing when its Market
  sits below every listing), recorded only when positive. `components` lists
  the deck, vendor, price and `kind` (Market, lowest listing, Mid, or in
  stock) behind each part.
- The page shows the bundle at that same payable price, with Market as a note
  when the two differ, so the headline, the saving and its basis follow one
  rule.
- The page shows "saves $X" only when the saving rounds to at least $0.01,
  with the basis beside it. On 2026-10-30, 28 of 33 bundles show a saving.
- Example: the Star Trek Commander Deck Case (MTGJSON's Set of 4) is $223.63
  at TCGplayer Market. Its four decks on that basis (TCGplayer Market $66.67
  and $72.62, Card Kingdom in stock $64.99 twice) total $269.27, so it saves
  $45.64.

---

## Box only sets

Boxes are priced for every set that has a deck in the catalog, plus the sets
in `BOX_ONLY_SETS` in `scrape_prices.py`: draftable sets worth tracking that
have no Commander precon of ours.

| Set | TCGplayer group | `set_release` | Rows on 2026-10-30 |
|---|---|---|---|
| The Hobbit | 24683 | 2026-08-14 | Play $126.17 (EV $63.33, 30 packs), Collector $620.25 (EV $489.64, 12 packs) |
| Mystery Booster Commander Edition | 24777 | 2026-10-01 | Booster Display $365.83 (no EV: MTGJSON has no pack count; no Card Kingdom listing) |

- A box only set is matched like any other (its own main TCGplayer group, Card
  Kingdom by edition and kind), and its history is kept and pruned the same
  way. Removing a set from the list drops its box rows and, on the next run,
  its box history.
- Mystery Booster Commander Edition carries "Commander" in its name, so it is
  also in `DRAFTABLE_COMMANDER`; otherwise the main group matcher would skip it
  as a precon group.
- Its `set_release` is TCGCSV's `publishedOn` (2026-10-01), not MTGJSON's
  release date (2026-10-19). The page orders box only sets by it and prints its
  month under the set name ("Oct 2026"), or the preorder text when it is in the
  future. With MTGJSON's date the set would sort above Reality Fracture (decks
  dated 2026-10-02) instead of below it.
- **Order on the page**: box sets are ordered by the newest deck release in the
  set, else by `set_release`, newest first. Star Trek therefore sorts by its
  decks' 2026-11-13, then come Reality Fracture, Mystery Booster Commander
  Edition and The Hobbit.

---

## Preorders

A deck whose `releaseDate` is after the visitor's local today is a
**preorder**. The four Star Trek Commander decks (Federation Fleet, Landing
Party, Klingon Fury, We Are the Borg; TCGplayer group 24770 "Commander: Star
Trek"; release 2026-11-13; MSRP $74.99) are the current ones. Their
Collector's Editions are not catalog entries, and the TCGplayer picker rejects
them.

- **Prices**: vendors list preorders, so they are priced and recorded in
  history like any deck (TCGplayer Market $66.67 to $72.62 on 2026-10-30; Card
  Kingdom $64.99 to $74.99, two in stock). The Star Trek Deck Case is attached
  as their set bundle, and the Star Trek play and collector boxes are priced.
- **What is absent until upstream catches up**: MTGJSON has no decklists for
  them yet, so no crack value, chase cards or staple finder entries; MTGJSON
  has no Star Trek booster model, so no box EV; ScryCheck has no pages, so no
  power score. Each appears on the first run after its source publishes, with
  no catalog edit except adding the ScryCheck fields.
- **On the page**: the card is labelled clearly (for example "Preorder ·
  releases Nov 13"), and the deck is kept out of Deal of the Day, the best
  discount stats, buy signals and low badges, because a preorder price is
  not a discount on a released product.

---

## ScryCheck

`powerScore` (0 to 10) and `powerBracket` (B1 to B5) in `decks.json` were
regenerated from one ScryCheck snapshot on 2026-10-07, and `scryCheckSlug`
links each card to its ScryCheck page. They are **automated estimates**; the
page says so and links ScryCheck's own [accuracy notes](https://scrycheck.com/about).
The older `power` key (on 111 decks) is no longer read by anything.

ScryCheck has no page yet for the four Star Trek decks (their
`scryCheckSlug` URLs return 404 on 2026-10-30), so they carry no
`powerScore` or `powerBracket`. The page treats a missing score as "not
published" and still renders the card. Add both fields once ScryCheck
publishes them.

---

## The deck catalog (`decks.json`)

Hand maintained, newest release first, 121 decks in 35 sets on 2026-10-30.
Fields:

| Field | Meaning |
|---|---|
| `id` | URL safe slug; never reused |
| `name`, `set`, `setType`, `releaseDate` | Exact product name as Wizards and the vendors list it, its set, and the US release date |
| `colors` | Color identity of the command zone, in WUBRG order |
| `commander` | The deck's **face commander as Wizards presents it** (box front, product page or decklist text). Use the MTGJSON commander zone only when Wizards does not single one out |
| `partner` | Only a true second commander in the command zone (today: the three Doctor Who decks and Frodo with Sam) |
| `altCommanders` | Optional list of the box's other foil cards that can lead the deck, in deck order (MTGJSON `isFoil` plus `leadershipSkills.commander`, excluding Backgrounds and plain planeswalkers) |
| `desc` | One line description, checked against the cards' oracle text |
| `msrp` | US MSRP in dollars; drives the discount math, never 0 |
| `powerScore`, `powerBracket`, `scryCheckSlug` | ScryCheck data (above) |
| `formerIds` | Optional ids the deck had before a rename; the scraper moves their history and the page moves saved user data |

Known cases where the face commander differs from MTGJSON's commander zone:
turtle-power (Leonardo, the Balance; the zone holds Heroes in a Half Shell) and
the-fantastic-four (Mister Fantastic; the zone holds Invisible Woman).

The old hand typed price keys (`currentPrice`, `priceSource`, `priceDate`,
`singlesValue`) are gone from every entry: live prices come only from the
scraper.

**MSRPs**: Wizards figures back Star Trek ($74.99, from the Wizards Star Trek
buyers guide), Foundations Commander Decks, Reality Fracture, Marvel Super
Heroes, Secrets of Strixhaven, TMNT, Lorwyn Eclipsed, Edge of Eternities,
Final Fantasy, Tarkir: Dragonstorm and Aetherdrift. Decks released before
Foundations (2024-11-15) keep the catalog's long standing figures as they
are, with no estimate label; tracing them to Wizards sources is deferred.

**When adding a deck**: take the name, date and MSRP from Wizards; the face
commander from the box front or product page; `partner`, `altCommanders` and
`colors` from the MTGJSON deck file. Run `python3 test_data_integrity.py
--pending-scrape` before the next scrape. Some set names keep a suffix
("Aetherdrift Commander", "Bloomburrow Commander", "Strixhaven: School of Mages
(Commander 2021)") because the scraper matches TCGplayer groups and boxes by
set name; leave them.

---

## Price history (`prices_history.json`)

- **Window**: 90 calendar days, plus one carry in entry per series (the price
  in force when the window opens). An entry is written only when a price
  changes; a same day rerun merges into that day's entry.
- **Deck entries** `{date, tcg, ck, best}`: `tcg` is TCGplayer Market, `ck` is
  recorded only while Card Kingdom has stock, and `best` is the lower of the
  two, so it is never a sold out Card Kingdom price. It is not always a price
  you can pay that day: TCGplayer Market can sit below every listing (on
  2026-10-30 necron-dynasties ends at $281.02 while its cheapest listing is
  $300.00). A bare `{date}` entry means
  neither vendor had a tracked buyable price from that date. A vendor whose
  fetch failed carries its value in force into a new entry instead of
  dropping out.
- **Box entries** `{date, price, kind}` under `"<set>::<type>"`. Entries of a
  kind the row no longer shows are archived under `"<set>::<type>@<kind>"`, so
  a switch from Set to Draft Booster never splices two products into one line
  (8 sets switched on 2026-06-24; the Commander 2021 play box restarted as a
  Draft Booster series).
- **Renames and removals**: series under a `formerIds` id merge into the
  current id. Series of decks no longer in `decks.json`, and box series of
  sets in neither `decks.json` nor `BOX_ONLY_SETS`, are dropped.
- **Format**: standard JSON, one series per line, so merge conflicts stay
  local. NaN and Infinity are refused on write and read as missing.
- **A corrupt history stops the run** (exit 1) instead of being replaced with
  one day. To recover, restore the last good copy from git
  (`git checkout <commit> -- prices_history.json`) and rerun.

### One off corrections (`migrations`)

Each runs once and is recorded by name in `migrations`:

- `ck_sold_out_2026_10`: until October the history recorded Card Kingdom
  prices on days it was sold out, drawing flat `best` lines nobody could pay.
  `CK_SOLD_OUT_2026` in `scrape_prices.py` holds each deck's sold out days,
  read from the git history of `prices.json` (the last run of each UTC day)
  from `CK_SOLD_OUT_FROM` (2026-06-20, the first run with stock counts) to
  `CK_SOLD_OUT_THROUGH` (2026-10-08). The migration rebuilds each deck series
  from the prices in force each day; applied to the canonical history on
  2026-10-30, it rebuilt 69 deck series, so 2672 sold out deck days no longer
  count a CK price.
- `bad_points_2026_10`: drops values under 0.4 times their series median, plus
  `KNOWN_BAD_HISTORY` (the peace-offering-woe CK series, which was really the
  Bloomburrow deck). On the canonical history it removed nothing: the outliers
  it targeted were Zulus and Mana Pool values already stripped on 2026-10-07,
  and peace-offering-woe left the catalog.

The first run on the canonical history logged `::warning::history: Card
Kingdom stock unknown for <dates>` for the 19 dates from 2026-10-09 to
2026-10-28: the old scraper kept running on main after `CK_SOLD_OUT_THROUGH`,
so Card Kingdom prices it recorded on those dates while sold out are still in
`best`. Because `ck_sold_out_2026_10` is now listed in `migrations`, widening
the table under the same name does nothing; regenerate `CK_SOLD_OUT_2026` from
the git history of `prices.json` up to the deploy date, move
`CK_SOLD_OUT_THROUGH`, and give the migration a new name (for example
`ck_sold_out_2026_10b`). The table can be deleted once its dates leave the
90 day window (after 2027-01-27 if it is extended to 2026-10-28).

---

## How a run protects the data

- **Budget**: the fetch phases share 15 minutes (`RUN_BUDGET_SECONDS`). A
  server's Retry-After is honoured up to 10 s, and reading one response body
  is capped at 120 s.
- **Circuit breaker**: 5 distinct failing TCGCSV groups (or crack sets) in a
  row end a phase early; one broken group no longer stops it.
- **Carry forward**: if the crack or box phase is cut short or fails, the
  previous run's values are kept for what it missed, marked `carried_from`,
  for at most 3 days. Carried box rows never enter history, and
  `cards_index.json` is not shrunk on such a day.
- **Degraded run**: below 40% of the catalog with a TCGplayer price (49 of 121
  decks), the scraper refuses to overwrite a good `prices.json` or touch
  history. The floor is a share, so shrinking the catalog never blocks later
  runs.
- **No non finite numbers**: upstream values that are not finite count as
  missing, and every writer uses `allow_nan=False`.

### CI checks

- `scrape-prices.yml` runs at 01:23 UTC (6:23 PM PDT, 5:23 PM PST), after
  TCGCSV's daily publish, so each history day gets exactly one snapshot even
  when GitHub starts the job hours late. It is two jobs: `scrape` (read only
  token: install, `--catalog`, scraper, gate) hands the three data files to
  `commit` (write token, runs no third party code, accepts only those three
  files as strict JSON).
- The gate is `python test_data_integrity.py --gate --previous <last committed
  data>`: every data test, coverage floors, a check that the files come from
  this run, upstream freshness (a TCGCSV older than 3 days blocks the commit; a
  MTGJSON older than 7 days only warns, because crack and EV values carry
  forward), and no sharp drop against the last run. History is compared over
  its last 60 days, so trimming and one off migrations pass.
- **Coverage floors scale with the catalog**, so adding or removing decks never
  blocks the scheduled runs: TCGplayer and Card Kingdom prices on at least 84%
  of decks, fully priced crack entries on at least 90% of released decks
  (release date on or before the run), box rows for at least 75% of the
  tracked sets that have had boxes (a box series in history or a row this run,
  so Commander only sets never count), and at least 25 `cards_index` cards per
  released deck. The tests read
  `BOX_ONLY_SETS` from the scraper, so box only rows and the `booster` kind
  pass.
- **Big moves**: the gate fails if more than 3 decks or boxes (or 3%) move
  over 1.5 times since their previous history entry, or any one over 2.5 times.
  A deck whose previous entry predates its release (a preorder price) is only
  noted, since a whole Commander set can reprice on release day.
  After an intentional matcher fix, run the workflow by hand with
  `allow_price_jumps` (`ALLOW_PRICE_JUMPS=1` locally).
- Other data checks: CK prices mostly end in .99 or .49 and TCG prices mostly
  do not; median bands for box CK/TCG, box EV/price and crack CK/TCG; each
  bundle component must equal the price a buyer can pay at that vendor
  today (TCGplayer Market, or its lowest listing when Market sits below every
  listing; Card Kingdom in stock) and be the lower of the two, and the saving
  must equal the components' total minus the bundle's own payable price;
  bundle and box links must be on the vendor's https host; no generated name
  or label may contain markup or control characters.
- `validate.yml` runs on every push to main and every pull request without
  scraping: catalog checks, data tests in `--pending-scrape` mode, the page
  shell check (`--page`: the CSP hash of the inline script, and that every
  file the manifest and service worker name exists), py_compile, and a syntax
  check of the page script, `sw.js` and the manifest. It is the fast signal
  after a hand edit; a stale CSP hash fails it.
- Local modes of `test_data_integrity.py`: plain (committed data, offline),
  `--catalog`, `--pending-scrape` (after editing `decks.json`, before a
  scrape), `--page`, `--gate [--previous DIR]`, and `--root DIR`. Floors and
  bands are documented at the top of the file.
- `requirements.txt` is an exact, hashed lock (its header says how to update
  it), installed with `--require-hashes` on Python 3.12. `certifi` is pinned
  at 2026.10.19 or later because older bundles still trust the compromised
  emSign roots (GHSA-6q48-6c7w-9gw2).
- `.github/dependabot.yml` proposes weekly updates for those pins and for the
  action SHA pins, each once it is 14 days old. Security updates skip that
  wait, but only after Dependabot alerts are switched on in the repository's
  settings (Settings > Advanced Security), which only the owner can do.

---

## How accurate is this?

Cross checked against [PriceCharting](https://www.pricecharting.com/) (computed
purely from **completed eBay sales**) in July 2026:

| Box | Our TCG price | eBay realized | Gap | Card Kingdom | CK premium |
|---|---|---|---|---|---|
| Tarkir Dragonstorm Collector | $414 | $414.75 | **−0.2%** | $500 | +17% |
| Tarkir Dragonstorm Play | $137 | $130.96 | **+4.4%** | $170 | +23% |
| Duskmourn Collector | $621 | $588.07 | **+5.3%** | $750 | +22% |

**Takeaway:** TCGplayer Market is a realized sales consensus, the same kind of
signal as eBay sold listings, and it landed within about 5% of true realized
prices. Card Kingdom sat about 17% to 23% above it, an upper retail bound. It
is also the industry standard anchor (Scryfall and MTGGoldfish both use
TCGplayer Market).

---

## Evaluated and rejected

### Zulus Games: dropped 2026-10-07 (terms, accuracy and cost)

Used as a third deck vendor until 2026-10-07, then removed completely.

- **Scraped against its Terms of Service.** The scraper hit its storefront
  search endpoint with rotating, spoofed browser User Agents and a forged
  Referer. That is not a sanctioned access route, and no honest version of it
  exists.
- **Thin coverage.** It matched only about 27 of 121 decks.
- **It sent buyers to sold out listings.** 15 of its 16 "best price" wins were
  sold out: the API reports an `available` flag for every product and the
  scraper never read it. Sold out asks sit near MSRP while the real offers are
  gone, so they won the best price headline, Deal of the Day and the price
  history `best` series.
- **It cost the most.** About 80% of each daily run's time went to Zulus
  requests.

### Mana Pool: dropped 2026-10-07 (terms)

Used from 2026-07-24 to 2026-10-07 as the fourth vendor and the **only** source
of realized sales, then removed completely.

- **Its terms bar automated downloading and redistribution of its data without
  written consent.** A public daily tracker does both, and we never had that
  consent. Mana Pool comes back only with written permission.
- **What went with it:** every sold side signal (the "last sold", "avg sold"
  and "vs asking" lines on decks and boxes), Mana Pool box prices, and the self
  contradiction guard that voided any Mana Pool ask under half its own recent
  sales.

On 2026-10-07 `prices_history.json` was migrated to match: the `zulus` and `mp`
fields were stripped from every deck entry, `best` was recomputed from `tcg`
and `ck`, and entries left identical to the previous one were dropped.

The seven decks that only Zulus or Mana Pool priced are all resolved. The five
Strixhaven Commander 2021 decks now price at TCGplayer (group override) and
Card Kingdom. Draconic Domination and Vampiric Bloodlust were misnamed: they
are Draconic Destruction and Vampiric Bloodline (renamed with `formerIds`) and
price normally. Faerie Schemes, Animus Antiquity, Master of Keys and the
Wilds of Eldraine Peace Offering never existed as Commander products and were
removed, taking the catalog from 121 to 117 (the four Star Trek preorders
brought it back to 121).

### Amazon: rejected (feasibility **and** value)

- **Official API is gated and mid retirement.** PA-API 5 was deprecated
  2026-04-30 and the endpoint retired **2026-05-15**, replaced by the Creators
  API. Either way access requires being an approved Amazon Associate with
  ongoing qualifying sales (about 10 per trailing 30 days). A hobby project with
  no affiliate sales cannot get or keep access, by design.
- **Scraping is out.** Amazon's Conditions of Use explicitly prohibit
  collecting listings and prices, and Amazon blocks datacenter and CI IP ranges,
  exactly what GitHub Actions runs on.
- **Even if we had it, the data is weak for MTG sealed.** Product identity is
  fine (clean WotC ASINs), but there's frequently *no price to show*: Amazon
  first party MTG sealed inventory is largely absent. What does appear is third
  party marketplace pricing that swings wildly on the same ASIN, and when
  present it lands right where we already are (for example Scions & Spellcraft:
  our TCG **$62.67** vs Amazon 3P $65.88 vs CK $69.99, near zero marginal
  information). Plus a real counterfeit and resealed return problem specific to
  MTG.
- **Nobody else does it either.** MTGGoldfish uses "TCGplayer Mid,
  CardKingdom, and Ebay"; SpellBook lists 16+ sources with no Amazon; MTGStocks
  uses TCG + CK. Even *bettercardsource.com*, a precon tracker that IS an Amazon
  Associate, sources its prices from TCGplayer and Mana Pool and uses Amazon
  only for outbound affiliate links.

### Keepa: technically viable, rejected on value

The one route that genuinely **bypasses the Amazon Associates gate**: a
documented REST API at `api.keepa.com` (current price, multi year history, buy
box, offers, sales rank), no affiliate account or qualifying sales needed,
callable from a datacenter IP without proxies.

Rejected because:
- **Paid, with no free tier and no sandbox.** €49/mo (Starter) is the floor of
  the API ladder, and we'd use about 0.5% of it. (A €29/mo "Keepa Pro" tier has
  been reported to include 1 token/min of API access, enough for about 115
  products/day if sharded across about 3 runs, but this is *unconfirmed* and
  Keepa calls it "primarily intended for testing.")
- **Licence is a gray zone for a public hobby site.** Their API terms say it's
  "available solely for business purposes… exclusively to entrepreneurs,"
  limited to "the user's own business purposes," and ban resale without written
  consent. They don't explicitly forbid *displaying* data, and staff have told a
  user to chart raw API history on his own site, but it's genuinely ambiguous.
  An email to info@keepa.com would settle it (their terms name email as a valid
  consent channel). Unambiguously off limits regardless: embedding the Keepa
  Box, hotlinking graph.keepa.com.
- **Provenance risk.** Keepa's data is substantially crowd scraped via its
  browser extension, which Amazon's own terms forbid. That risk sits with Keepa
  (they warrant non infringement), but their liability is capped at about 2×
  fees paid while the customer indemnifies them.

**Decision:** not worth €49/mo to add a frequently blank, noisy column that
duplicates information we already have to within about 5%.

### CamelCamelCamel: rejected (dead end)

- **No public API, and there never has been** (`/api` and `/developers` 404).
- Only two **site wide RSS firehoses** survive (`/top_drops/feed`,
  `/popular.xml`), with no per product or per search feed. They emit
  unstructured title strings with no way to request a specific ASIN, so they're
  useless for a fixed watchlist.
- **Their ToS bans both halves of what we'd do**, verbatim: *"Scraping and/or
  other automated data collection from our website and/or emails is
  prohibited. Republishing our data is prohibited."* Enforced with Cloudflare
  challenges; their robots.txt disallows AI and agent crawlers.
- **It sits on the same gate anyway.** CCC's data comes from Amazon's PA-API *as
  an Associate* (their founder is on record they've deliberately never
  scraped). Routing through CCC doesn't launder around the affiliate
  requirement; it just swaps an Amazon eligibility problem for a CCC contract
  breach.

CamelCamelCamel and Keepa are independent companies (no ownership link).

### Cardmarket (EU): wanted, but blocked

The largest genuine gap (see below). Its API is **restricted to approved
professional sellers** via a manual approval process, and it explicitly
prohibits apps that "constantly only request the public Marketplace resources…
on consecutive days", which is precisely a daily price tracker.

### eBay sold listings: wanted, but gated

Sold and completed listing data is not openly available via API. Note also
that eBay has owned TCGplayer since 2022, so eBay and TCGplayer Market are no
longer *fully* independent venues.

---

## Known gaps (accepted)

1. **No European market.** Cardmarket (EUR) prices are independently
   determined, not a currency conversion of US prices. We are a **US only**
   view, a deliberate scope choice, not an oversight.
2. **No independent realized price cross check.** Reopened on 2026-10-07. From
   2026-07-24 Mana Pool's `recent_sales` filled this gap, but its terms bar the
   way we used it, so it was dropped. TCGplayer Market is itself a realized
   sales consensus, but nothing independent of it checks it now.
3. **No other large US retailers** (SCG, ChannelFireball, CoolStuffInc). These
   are *ask* prices that sit at or above the realized market, so they add less
   than they appear to.
4. **Catalog edges.** The page says "since 2021", but the two Kaldheim
   Commander decks (Elven Empire and Phantom Premonition, 2021-02-05) are not
   in the catalog. Every other Commander deck since 2021 in MTGJSON's
   DeckList is, apart from Secret Lair decks and Collector's Editions, which
   are left out on purpose. The four Star Trek decks (2026-11-13) are in the
   catalog ahead of MTGJSON, which has no decklist for them yet; Marvel
   Darkhold Destiny (2027-04-09) waits for a TCGplayer product and MSRP. Pre
   Foundations MSRPs are not yet traced to Wizards sources.

**Overall:** a faithful **US realized consensus** view, not a full global
picture.

## If we ever want to close the gaps

Ranked by value per effort, and both far better value than Amazon:

1. **PriceCharting** (or an equivalent eBay sold aggregator): the only
   verified, fetchable realized eBay signal found; would add an independent
   cross check.
2. **Cardmarket EUR via Scryfall or MTGJSON**: the practical way to get EU
   coverage without Cardmarket's restricted API.
3. Optionally one or two more US retailer asks to widen the buy side view, but
   only from retailers whose terms allow automated collection and display.

---

## Attribution and licences

- **MTGJSON** data is used under the MIT License, Copyright © 2018 to present
  Zachary Halpern. `cards_index.json`, the crack totals and the box EVs are
  derived from it; the full notice is in [`NOTICE.md`](NOTICE.md), and the page
  footer links the licence.
- **TCGplayer** prices come via **TCGCSV**, and **Card Kingdom** prices, stock
  and buylist come from its public pricelist; both are credited in the page
  footer and every price links to its product page.
- **ScryCheck** power scores are credited and linked in the footer and on each
  card.
- The footer carries Wizards of the Coast's **Fan Content Policy** notice: MTG
  Tracker is unofficial Fan Content, not approved or endorsed by Wizards.
