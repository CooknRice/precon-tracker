# MTG Tracker: Design System

A reference for the visual language of MTG Tracker, written so a designer (or a
tool like Claude Design) can extend the site while staying consistent. Every
value here is taken from the live styles in `index.html`; there is no separate
stylesheet or build step (see [Tech context](#tech-context)).

---

## 1. Aesthetic

**An antique financial ledger, for Magic singles.** Warm, editorial and quiet,
not a neon "deals" site. Think aged paper and ink, a serif masthead, monospaced
figures for anything numeric, generous whitespace, and restrained accent color
used only to mean something (a deal, a saving, a link).

Principles:
- **Warm neutrals first, accents sparingly.** Most of the page is paper and
  ink. Color appears only on prices, savings, links and status.
- **Numbers are monospaced.** Prices, dates, ranges, counts: JetBrains Mono.
- **Flat, soft edged surfaces.** Rounded cards, hairline dividers, no heavy
  shadows or gradients (one barely there radial paper texture aside).
- **Two moods, one system.** A dark "ledger at night" default and a light
  "parchment" theme, built from the *same* tokens (see §2).
- **Say only what is true.** Every price is labelled with the vendor and the
  kind of price it is; estimates carry a visible note.

---

## 2. Theming

Both themes are defined by overriding the **same CSS custom properties** on the
root. Dark is the default (`:root`); light is `[data-theme="light"]`.

```
:root { … dark token values … }
[data-theme="light"] { … same token names, light values … }
```

- The theme is toggled by the `◐` button (`.theme-toggle`), saved in
  `localStorage` (`precon-theme`), and follows `prefers-color-scheme` on a
  first visit. `applyTheme()` also rewrites both `<meta name="theme-color">`
  tags to the active theme's `--paper`, so the browser chrome matches.
- **Because every component uses `var(--token)`, it themes automatically.**
  Names flip through `--ink-strong`. The few manual light overrides that remain
  are for text on tinted fills: `.discount-badge`, `.stale-banner`,
  `.uf-kind.uf-deck` and `.uf-box`, the unchecked own and watch toggles (a
  dark wash instead of a black one), and gold filled chips and buttons
  (`--gold-fill`).
- **Contrast:** all text passes WCAG AA (4.5:1) on every surface in both
  themes, the owned card included. Check any new pairing in both themes.

> **Rule for new work:** never hardcode a hex color. Use a token. If a token
> doesn't fit, add it to *both* theme blocks. (A few older rgba washes and the
> light overrides above still use literals; don't add more.)

---

## 3. Color tokens

| Token | Dark (default) | Light (`[data-theme="light"]`) | Use |
|---|---|---|---|
| `--paper` | `#15120e` | `#f3ead4` | Page background, browser theme color |
| `--paper-deep` | `#1d1915` | `#fbf6e7` | Cards and panels (legacy) |
| `--paper-line` | `#2c2720` | `#ddd0b2` | Borders and dividers |
| `--surface` | `#1c1a17` | `#fbf6e7` | Card and panel base (modern) |
| `--surface-2` | `#232019` | `#f1e7cd` | Nested blocks (price card, rows) |
| `--hairline` | `rgba(237,227,202,.08)` | `rgba(44,36,22,.12)` | Soft divider |
| `--ink` | `#ede3ca` | `#2c2416` | Primary text |
| `--ink-strong` | `#f5efe2` | `#2c2416` | Brightest text: deck, box and deal names |
| `--ink-soft` | `#c9c0a8` | `#4c4029` | Secondary text |
| `--ink-muted` | `#a39a80` | `#5e5138` | Labels, MSRP (real content, AA everywhere) |
| `--ink-faded` | `#978d74` | `#6f6248` | Faint hints, keys, notes (still AA) |
| `--burgundy` | `#d97485` | `#9d2f44` | **Prices, deals** |
| `--burgundy-soft` | `#bd5d6e` | `#b0405a` | Cheaper vendor emphasis |
| `--gold` | `#c69a3e` | `#7d5a0f` | **Links, highlights** |
| `--gold-bright` | `#e4b954` | `#80600f` | Hover, focus rings; text and outlines in light |
| `--gold-fill` | `#e4b954` | `#b3852a` | Gold **fills** behind dark text (active chips, primary buttons, deal badge) |
| `--sage` | `#6e8a55` | `#4d6a35` | Owned and positive **fills, borders, lines** |
| `--sage-text` | `#86a36b` | `#4d6a35` | Sage used as **text** (near low, profit to crack, target hit, cheaper CK) |
| `--success` | `#5ad487` | `#276b35` | Positive green text: savings, gains, good EV, good buy |
| `--live` | `#3ec66a` | `#1f6f39` | "Live" data indicator dot and vendor names in the footer |
| `--deal-bg` | `#2c2518` | `#efe4c4` | Deal card background |
| `--owned-bg` | `#1f2a1c` | `#e7eed6` | Owned card background |

**Mana pip colors** (WUBRG plus colorless), for the deck color identity dots
(`.mana-pip`, `.pip-*`) and the color filter (`.cb-*`). Light fills are light
enough for their dark letters to clear 4.5:1:

| Token | Dark | Light |
|---|---|---|
| `--w` (white) | `#e8dcb0` | `#d1c38c` |
| `--u` (blue) | `#6da8db` | `#6095c2` |
| `--b` (black) | `#524a52` | `#6a6168` |
| `--r` (red) | `#d87256` | `#cb7452` |
| `--g` (green) | `#7ba068` | `#73935c` |
| `--c` (colorless) | `#9b9586` | `#928d7a` |

### Semantic accent usage (keep this consistent)
- **Burgundy** = money. Live prices, the Deal of the Day price, discount badges,
  today's price marker on charts.
- **Gold** = interactive or featured. Links, hovers, focus rings, the
  "DEAL OF THE DAY" chip, and the preorder chip and box set preorder date
  (outlined or text, never a fill). Use `--gold-fill` for a fill under dark text, never
  `--gold`.
- **Sage / success** = positive value. Savings, near low and the low badge,
  "profit to crack", owned and watchlist state, EV that beats the box price.
  Use `--sage-text` or `--success` for text and `--sage` for fills and lines.

---

## 4. Typography

Three families, loaded from Google Fonts:

| Token | Family | Role |
|---|---|---|
| `--font-display` | **Cormorant Garamond** (500/600, italic 500) | Masthead `.title`, tagline, deal name, section flourishes |
| `--font-ui` | **Inter** (400 to 700) | Body, deck names, buttons, most UI |
| `--font-mono` | **JetBrains Mono** (400/500) | All numerics: prices, dates, ranges, labels, counts |

- Base: `15px / 1.55` Inter on the body.
- Masthead `h1.title` is the only large display type; the accented word is
  wrapped in `<em>` and rendered in burgundy (e.g. **MTG** *Tracker*).
- Uppercase plus letterspacing (`text-transform: uppercase; letter-spacing:
  about .08em`) is the convention for mono labels (stat labels, box type, count
  line).
- Headings: one `h1` (masthead); each section head is an `h2` (the feed, staple
  finder and portfolio heads share `.section-head`, the boxes head is
  `h2.box-heading`, and visually hidden ones use `.sr-only`); every deck card
  name is an `h3`.

---

## 5. Shape, spacing & layout

| Token | Value | Use |
|---|---|---|
| `--radius` | `14px` | Cards, panels |
| `--radius-sm` | `9px` | Inner blocks, buttons, inputs |
| `--radius-pill` | `999px` | Chips, badges, toggles |

- **Page container:** `.wrap`, `max-width: 1280px`, padding `32px 28px 80px`,
  centered. The page content sits in one `<main>`.
- **Deck grid:** `.grid`, a responsive auto fill card grid. Cards use
  `content-visibility: auto` so off screen cards cost little.
- **Dividers:** 1px `var(--hairline)` (or `--paper-line`).
- **Background texture:** the body has a very subtle 3 stop radial gradient in
  gold, burgundy and cream at about 0.01 to 0.06 opacity,
  `background-attachment: fixed`. Keep new full bleed surfaces transparent so
  it shows through.
- **Mobile:** single column with no horizontal scroll down to 320px. Control
  rows wrap, `.sort-wrap` keeps its label on one line, and selects are capped at
  `max-width: 100%`. On coarse pointers the own, watch, theme and color buttons
  grow to 40px touch targets.
- **Motion:** a `prefers-reduced-motion: reduce` block turns off animations
  and transitions (the live dot pulse) and the hover lifts on cards, tiles and
  buttons.

---

## 6. Iconography & glyph conventions

Emoji and Unicode glyphs are used as compact, consistent marks (no icon font):

| Glyph | Meaning |
|---|---|
| `🏆` | Best buys feed head |
| `★` | Low badge ("★ 90 day low"), watched deck, "Watching only" filter |
| `🔎` | Where to get a card (the finder) |
| `🧾` | Plan a buy (want list planner) |
| `📊` | Portfolio panel |
| `◐` | Theme toggle |
| `●` | Live price dot |
| `✓` | Owned deck; "✓ Owned" on a box or bundle `.own-chip` |
| `🟢 🟡 🔴` | Buy signal levels (good buy / fair / hold) |
| `▲` | "Profit to crack" flag |
| `⚠` | Stale data and blocked storage banners |
| `→` | Outbound "view on …" links and arrows in ranges |
| `n/a*` | A card price left out as an outlier; the asterisk points to the reason in its title and in the card preview |
| `≈` | A modeled figure: est. singles, the typical simulated box |

---

## 7. Component inventory

Grouped by area. Each is a class (or class family) in `index.html`; the render
logic is in the inline `<script>`.

### Header and chrome
- `.title-block`, `h1.title`, `.tagline`: masthead and subtitle.
- `.meta-line`: right aligned "Prices updated" date and coverage.
- `.theme-toggle`: `◐` light/dark button.
- `.live-status` (`.live-dot`, `.ok`, `.offline`) inside the stats row, and
  `.stale-banner`: data freshness. `#storageBanner` reuses `.stale-banner` to
  warn when the browser blocks saving.
- All page dates (Prices updated, live status, stale banner, chart axes) are
  the scrape's UTC calendar day.

### Headline modules (top of page)
- `.deal-of-day` with `.dod-main` / `.dod-name` / `.dod-open` (the inner button
  that opens the chart) / `.dod-price(-now/-sub)` / `.dod-msrp` / `.dod-why`
  (`.dod-sep`) / `.dod-flag`: the single best deal, a labelled section.
- `.staple-finder` with `.staple-head` / `.staple-input` / `.staple-results` /
  `.staple-row` (`.staple-row-name` / `.staple-row-set` / `.staple-row-price`) /
  `.staple-empty` / `.staple-alts` and `.staple-alt` ("Did you mean"
  buttons): "Where to get a card". The search waits 250 ms after typing,
  matches names in both card indexes (`cards_index.json` and
  `box_index.json`, loaded together on first use) and shows the best match
  in a `.finder` (shared with the finder dialog):
  - `.fd-name` (Cormorant italic), then three `.fd-block`s, each headed by
    an `h4.fd-label` with a `.fd-count`:
  - **Buy the single** (`.fd-single`): `.fd-best` with the `.fd-price`
    (burgundy, links to the printing on TCGPlayer or a Card Kingdom search)
    and `.fd-what` ("TCGPlayer retail · TRC 429, nonfoil · as printed in
    Federation Fleet"): the lower of the two retail prices of the cheapest
    printing and finish in the lists loaded, with outlier guarded TCGPlayer
    prices already left out (the `.fd-note` counts them). Deck files load by
    themselves for a card in at most 25 decks; past that, and for box files
    (up to about 1.3 MB each), only on request (`.fd-acts` buttons). Files
    load at most 6 at a time, with a `.fd-progress` status line ("Loading
    35 precon lists: 12 of 35").
  - **Precons with it** (`.fd-decks`): today's rows as `.staple-row`s,
    cheapest to buy first, 8 at first (`.fd-more` "Show all 120 precons"),
    each with "its copy $1.55 TCG, nonfoil" once its file is in. A row opens
    the deck's chart, as before.
  - **Boxes that can hold it** (`.fd-boxes`, `.fd-box-list` of `.fd-box`
    with `.fd-box-open`, `.fd-odds`, `.fd-box-meta`): per box type from
    `box_index.json` (the card key, then its alias), finishes together:
    "in about 41% of boxes · about 0.51 copies per box" (or "about 1 in 25
    boxes"), the finishes, the box price you can pay and its EV. Packs come
    from the prices.json row whose `cards_file` and `cards_kind` match; a
    row with no `packs` (Mystery Booster Commander Edition) shows the chance
    per pack only. **Never a cost per copy figure.** A box opens its card
    list.
- `.planner` (`.planner-head`, `.planner-toggle` with `aria-expanded` and
  `.planner-hint`, `.planner-body`): "Plan a buy", closed until opened, so
  it moves nothing on first paint. `.planner-input` (a labelled textarea),
  `.planner-help`, `.planner-opts` (two `.cv-check` boxes: leave out decks
  I own; leave out cards my decks already give me), `.planner-actions`
  (`.planner-go`, `.planner-status` role status). Results in
  `.planner-results`:
  - `.plan-head` (focus target after a change), `.plan-block`s: "Not found"
    (`.plan-list`, `.plan-miss`, "Did you mean" `.staple-alt`s that rewrite
    that line and plan again), "In no precon we track" (each opens the
    finder dialog), "Staples in more than 25 precons" (left out unless
    ticked; ticking loads every list that holds it), "Your decks already
    give".
  - `.plan-table` (a real table with a caption; under 720 px each row
    becomes a block of labelled figures from `data-label`): Precon
    (`.plan-deck` opens its card list, `.plan-set`, `.plan-names`), Covers
    ("1 of your 3 cards", copies), Your cards in it, Deck price (with
    `.plan-vendor`), Difference ("$28.84 less than the deck", or "more",
    `.plan-more` in `--sage-text`), Rest of the deck (TCGPlayer singles not
    covered, an extra that does not rank). Ranked by the value covered minus
    the deck price; 15 rows, then "Show all".
  - Parsing: plain names, "1 Sol Ring", "1x Sol Ring", Moxfield, Archidekt
    and Arena exports ((SET) numbers, `*F*`, `[Category]`, `^tag^`, `#tag`
    and "SB:" stripped); headers, `#` and `//` comments skipped; the first
    250 nonblank lines. Names match the index keys (the scraper's `norm`)
    and aliases; unmatched ones get up to three suggestions by shared
    letter pairs.
  - Each wanted card is valued at its cheapest printing in the precon lists
    loaded (the lower retail price, outlier guarded), counted up to the
    copies wanted, so one odd printing cannot make a deck look cheap.
- **Best buys feed:** `section.best-deals` (`.best-deals-head`,
  `.best-deals-grid`) of `.best-deal` / `.uf-item` tiles (`.bd-name`,
  `.bd-price`, `.bd-disc`) with a `.uf-kind` badge (`.uf-deck`, `.uf-box`,
  `.uf-bundle`). Bundles are links to their product page.
- Deal of the Day and the feed render once, after `prices_history.json` has
  loaded or failed; until then they show a `.pending` placeholder.
- `.since-strip` (`.since-top` / `.since-head` / `.since-dismiss` /
  `.since-list` / `.since-open` / `.since-num` (`.now`, `.good`) /
  `.since-note`): "Since your last visit on Nov 1", up to 6 changes for
  watched or owned decks and owned boxes since the previous visit (a price
  target reached, a new low over the recorded range, a move of 10% or more,
  "from $A to $B"). It shows only when that visit is a day old or more,
  appears once the price history is in (above the stats, so mostly below
  the first screen), and Dismiss hides it for this visit. Each name is a
  button that opens its chart. Computed in the browser; nothing is sent.
- `.portfolio` (`.portfolio-grid` / `.portfolio-head` / `.pf-stat` /
  `.pf-label` / `.pf-val` / `.pf-sub`): the collection panel ("Your
  collection"), shown when the collection holds anything. Stats: copies
  (sealed and opened), "Value now" (sealed copies at the price to buy today,
  never called a sale price), "Paid" (what the visitor entered), "vs paid"
  (only items with both), "CK cash" (Card Kingdom's live cash offer times
  the sealed copies it takes, `buy_qty`; shown only when the data has an
  offer and a maximum; checked with real data on 2026-11-15) and "Singles"
  (opened decks, TCGPlayer retail).
  - `.col-trend` (`.col-trend-note`): "Value over time" sparkline (daily
    history only); it opens the chart dialog on the collection line
    (today's sealed copies at each day's recorded price, from the first day
    all of them have one, with a note saying what is left out). Once the
    weekly history has loaded the chart line also reaches back before the
    daily window and includes bundles; a missing week for any item breaks
    it. Its change is drawn neutral, not as a buyer's verdict.
  - `.col-list` of `.col-item` (`.col-name` with a `.uf-kind` badge,
    `.col-title`; `.col-fields` of labelled `.col-field` / `.col-flabel` /
    `.col-input` for Copies, Paid each and Opened, and `.col-remove`;
    `.col-figs` with `.col-money`, `.col-quiet`, `.col-up`, `.col-down`).
    Fields save on change; a value that fails its check is put back with a
    note in `.col-status`. A deck's Paid starts at its MSRP.
  - `.col-hint`: how to add items, and a link to the saved data controls.
- `.own-chip` (`.checked`, `aria-pressed`): the Own button on each box tile
  and in each deck's set bundle note. Outlined; owned is `--owned-bg` with
  `--sage-text`. The visible "Own" or "✓ Owned" is hidden from screen
  readers, which hear "Own <product>" and the pressed state.
- `.link-notice`: at the top of `<main>`, when a share link names a deck or
  box the page does not list (or a deck whose card list is not out yet, whose
  chart opens instead). Dismissable.
- `.stats` / `.stat` / `.stat-label` / `.stat-val` / `.stat-sub`: summary tiles.

### Deck card (`.card`, states `.owned`, `.deal`, `.watching`, `.target-hit`)
- Identity: `.card-top` / `h3.card-name` / `.card-set` / `.card-setrow` /
  `.card-colors` / `.mana-pip` / `.card-commander` (commander, and `&` partner
  when the command zone holds two) / `.card-alt-commanders` (`.alt-key`, the
  box's alternate commanders) / `.card-desc` / `.card-meta`.
- **Headline price** (`.price-row` / `.price-main` / `.price-label` /
  `.price-live-dot` / `.price-msrp` / `.discount-badge`): the cheapest place to
  buy right now. `.price-label` names the vendor and the kind of price:
  "TCGPlayer Market", "TCGPlayer lowest listing" (when Market sits below every
  listing) or "Card Kingdom" (in stock only). `.price-low-note` (`.note-key`)
  shows the other TCGplayer figure or the CK stock count. With no live price
  the block shows MSRP for reference (`.price-unverified`,
  `.price-checkprompt`).
- Value lines under the price: `.price-range` (the tracked range in calendar
  days, `.near-low` with "near low" or "★ N day low", judged on that tracked
  range) and `.price-vendor`
  (the other vendor as a reference line with `(+$X)`, or `(out of stock)`, in
  `.vendor-oos`).
- Singles: `.crack-value` (`.worth`, `.crack-flag` "▲ profit to crack … before
  fees", `.crack-est` for the labelled TCG 70% estimate) · `.chase-cards`
  (`.chase-price` / `.chase-share`) · `.data-carried` when a value comes from an
  earlier run. `.card-meta` keeps the singles line and the top cards side by
  side and wraps the two lines below to the full width:
  - `.deck-exits` (`.exits-key` "Exits", `.exit-num`): "Card Kingdom buys
    sealed: $81.00 cash (up to 6) · singles to CK: $45.10", or "Card Kingdom
    is not buying this deck sealed" when CK lists the deck with no live offer
    (`buy` null). The sealed figure is CK's live cash offer with its maximum
    quantity (`buy`, `buy_qty`); the singles figure is `crack.sell_cardkingdom`.
    A deck CK does not list gets no sealed part. Facts side by side: no profit
    or arbitrage wording, no badge, no verdict. The title adds the store
    credit note.
  - `.deck-profile` (`.profile-names`): "38 new cards · 40% of singles value"
    or "All reprints", then "1 Game Changer (Farewell)" when any, from
    `crack[deck].profile`. A Game Changer is a fact about a card, never shown
    as the reason for the deck's bracket; ScryCheck's page, linked from the
    power row, explains its own rating.
- Signals: `.signals-row` with one `.buy-signal` (`.buy` / `.ok` / `.hold`) ·
  `.power-row` (`.power-score` / `.power-label` / `.power-bracket` /
  `.power-divider` / `.power-scan`).
- Actions: `.vendor-row` / `.vendor-btn`, `.watch-toggle`, `.own-toggle`,
  `.target-row` / `.target-input` / `.target-hit` / `.target-err`,
  `.compare-toggle`. Toggling one keeps focus on the same control after the
  grid rerenders.
- `.bundle-note` (`.bundle-label` / `.bundle-savings` / `.bundle-basis`): the
  set bundle price, its chart control (a `.sparkline` of the last 90 days
  with `data-bundle-chart` once the weekly history has loaded; before that
  a `.bundle-chart` button of the same 60 by 18 px box, so nothing moves;
  bundles have no daily history, and the weekly file loads only when a
  chart opens), and "saves $X" only when the saving rounds to at least
  $0.01, with the basis of the saving (each deck at the lower of TCGplayer
  Market and Card Kingdom in stock, with the parts in its tooltip).
- **Preorder label**: a deck whose `releaseDate` is after the visitor's local
  today is a preorder. Its `.price-label` leads with "Preorder" instead of
  "Live", and `.signals-row` shows a `.preorder-chip` ("Preorder · releases
  Nov 13", a gold outlined pill with no fill) in place of the buy signal. A
  preorder still shows its live prices, but never a discount badge, buy
  signal or low badge, and it never
  appears in Deal of the Day, the best buys feed or the best discount stats.
  The compare table adds the same text under its release date.
- **Missing upstream data** (a new or preorder deck): with no MTGJSON decklist
  a preorder shows "Singles value appears once the decklist is published." in
  `.crack-value.data-carried` (no chase cards), and with no ScryCheck score its
  `.power-row` reads "Not rated on ScryCheck yet →" and links ScryCheck's
  precon list. Never render "$0" or an empty placeholder for data that is not
  published yet.

### Sealed boxes
- `h2.box-heading` holding `.box-toggle` (`.box-toggle-hint`), then
  `.box-body` / `.box-set` (`.box-set-name`) / `.box-grid`.
- Box sets are ordered newest first by the newest deck release in the set,
  else by the row's `set_release`, so box only sets (The Hobbit, Mystery
  Booster Commander Edition) slot in by their own release date. Under each set
  name, `.box-set-sub` gives the release date, or the preorder text (with
  `.preorder`) for a set not yet released, and "boxes only, no precons" for a
  box only set. Boxes of an unreleased set make no claim in the best buys
  feed.
- `.box-item` tiles: `.box-item-type` (the SKU's own kind text, the part of
  its name after " - ", such as "Booster Display" for Mystery Booster
  Commander Edition; the data `label` is used only when the name has no
  " - ") /
  `.box-item-price` (the price you can pay, with `.box-item-low` for the Market
  or lowest listing note) / `.box-ev` (`.good` when EV is at least the price) /
  `.box-ck` (Card Kingdom, `.cheaper`) / `.box-item-link` / `.box-spark` (the
  sparkline button that opens the chart). A box with no EV (no MTGJSON pack
  count or booster model yet, or coverage under 90%) shows no EV line.
  `.box-ev-note` builds its rules from `box_ev_basis` (only cards worth $1.00
  or more count, box toppers left out, the coverage minimum).
  - `.box-ckbuy`: "CK buys this box: $150.00 cash (up to 8)", only while CK is
    buying (`ck_buy` with `ck_buy_qty`); nothing when it is not.
  - `.box-spread`: "Typical box ≈ $165 · 8 in 10 between $100 and $265
    (simulated)", from the row's `spread` (p50, p10, p90, already rounded by
    the scraper), shown only beside an EV line. Never a chance of profit or a
    break even rate, and never a change from the day before.
  - `.box-credit-note` (inside `.box-ev-note`, when any box has an offer): the
    store credit note (see Tech context, `CK_CREDIT`) with a link to Card
    Kingdom's sealed buylist page.

### Filters and controls
- `.filters` / `.filter-group` / `.filter-label` / `.search-input` /
  `.filter-select` / `.sort-wrap` / `.sort-select` / `.count-line` /
  `.reset-btn` / `.scope-chip`. Every control has a label.
- Color filter: `.color-filter` / `.color-btn` / `.cb-w|u|b|r|g|c`.
- Power filter: `.bracket-filter` / `.bracket-chip` / `.power-bracket`.
- Toggle buttons (color, bracket, scope, chart range) use `aria-pressed`.

### Overlays
- Dialogs (`#compareModal`, `#chartModal`, both `.compare-modal` with
  `role="dialog"`) and the `.compare-bar` live **outside** `.wrap`. Opening one
  makes the page inert, traps Tab, closes on Escape and returns focus to the
  control that opened it.
- Compare: `.compare-bar` (`.compare-bar-actions`) / `.compare-modal-inner` /
  `.compare-modal-close` / `.compare-table` (`th.deck-h`). The "CK buys
  sealed" row gives Card Kingdom's live cash offer and its maximum ("$84.00
  cash up to 18"), "not buying", or n/a when CK does not list the deck; a
  `.compare-note` under the table explains it and carries the store credit
  note when any compared deck has an offer.
- Price chart: `.chart-modal-inner` / `.chart-header` / `.chart-title` /
  `.chart-subtitle` / `.chart-svg` / `.chart-canvas-wrap` / `.chart-summary` /
  `.chart-price-now` / `.chart-delta` / `.chart-range` / `.range-btn` (1W, 1M,
  3M, 6M, 1Y, All) / `.chart-note` / `.chart-empty`. Inline `.sparkline`
  previews open it.
  - **Long history.** `history_long.json` (one TCGPlayer Market point a
    week from TCGCSV's archive, from Feb 2024; about 570 KB) loads only when
    a chart opens. Inside the daily window the daily points win; the weekly
    points fill the time before the first daily day and are drawn as their
    own `.price-line.weekly` (`--gold`, thinner) that holds to the first
    daily day and stops there, so the two sources are never joined. A week
    with no point (more than 10 days between points) ends the line a week
    after the last point: a gap, never an interpolation. A `.daily-line`
    marker reads "daily from here". `.chart-long` (`#chartLongText`) says
    "One point a week before Aug 17, 2026, from TCGCSV's archive of
    TCGPlayer Market prices", and for decks that Card Kingdom has no
    history that far back; while loading it says so, and on failure it
    offers `.chart-retry` "Try again" (focus moves to the line).
  - A range that would show the same as All is `disabled` (faded), never
    hidden, so the row never moves; while the weekly file loads, 6M and 1Y
    wait for it. Axes past about 8 months label months with years.
  - `.msrp-line` (dashed `--ink-muted`, labelled "MSRP $59.99", decks only,
    inside the y range) and `.release-line` (dotted `--sage`, "Release Mar
    8, 2024"; presale points can sit before it). Labels are
    `.chart-mark-label`. No trend line, no forecast.
  - The svg's `aria-label` is a sentence: the dates, first and last price,
    low, high and MSRP.
  - Set bundles get a chart (title the bundle, subtitle "Set bundle ·
    TCGPlayer Market price"): the weekly points plus today's Market price,
    no share link.
  - Points are placed by calendar date as a **step line** (a price holds until
    the next change). The range buttons cut by date back from the history
    file's date and carry in the price at the cutoff.
  - The line is the recorded history (`best`: TCGplayer Market, or Card
    Kingdom in stock). Today's headline price, when it differs, is drawn apart
    from the line as a burgundy `.buy-dot` joined by a dashed `.buy-link`; a
    listing price is never spliced into the recorded line.
  - A gap in history (no tracked buyable price) ends the line instead of
    bridging it.

- Card lists: `#cardsModal` (`.compare-modal.cards-modal`), opened by the
  deck card's `.view-cards` button (or a click on the deck card outside its
  controls) and a box row's `.box-cards` button. Tiles `.cv-tile` and list
  rows `.cv-row` each hold one `.cv-open` button (image and name); box tiles
  add an odds chip. Card images keep Scryfall's whole 488:680 frame: badges
  and prices sit in the caption, never on the artwork.
  - Card preview `#cvPreview` (role dialog, inside `#cardsModal`): opens on
    mouse hover after 250 ms or on keyboard focus, pins on click, tap or
    Enter, and becomes a bottom sheet under 640 px (no hover there).
  - Keyword tooltip `#cvKwTip` (role tooltip) on each `.cv-kw` term in the
    preview's rules text; terms come from `keywords.json`, loaded on the
    first preview.
  - Badges in the caption (`.cv-badge`): finish, copies, `.cv-new` "New" (a
    printing MTGJSON does not list as a reprint; deck lists only; gold
    outline, no fill) and `.cv-gc` "Game Changer" (Wizards' list; plain
    outline). The preview repeats them under its meta line (`.cv-pv-flags`).
  - **Outlier prices** (`.cv-price.cv-outlier`): a TCGPlayer price the
    scraper left out (card file header `outliers`; at least $10 and more
    than 20 times Card Kingdom's for the same printing and finish) reads
    "n/a*", links nowhere, and carries the reason in its title and for screen
    readers: "No TCGPlayer price: the $31.53 matched to this card (foil) is
    more than 20 times Card Kingdom's foil price of $0.79, so it is left out
    as unreliable." The preview shows the same sentence (`.cv-pv-outlier`)
    and the list header counts such cards. The wording says what was
    matched to the card, never that this printing is listed at that price.
  - **Deck header strip** `.cv-profile` (three `.cv-pf-block` sections, each
    with an `h3.cv-pf-label`): the mana curve of the nonland cards
    (`.cv-curve`, one `.cv-curve-col` per mana value 0 to 7+, bars in a
    fixed 48px track, the count above and an sr-only sentence per column),
    the type mix (`.cv-mix`, copies per type) and the TCGPlayer value split
    between new cards and reprints (`.cv-split` bar, `.cv-split-new` in
    `--gold-fill`, the rest `--ink-faded`, with `.cv-key` swatches and the
    figures in text). Copies count, as in the totals.
  - **New cards only** (`.cv-f-new`, `.cv-check`): a labelled checkbox among
    the deck controls; hidden for a deck with no new cards, cleared by Clear
    filters.
  - **Box spread** `.cv-spread` (box lists, per type, when the box file has
    `kinds[kind].spread`): "10,000 simulated boxes · TCGPlayer retail before
    fees", a `.cv-spread-lead` line (typical box, 8 in 10, half), and a
    `.cv-hist` histogram (`role="img"` with a full sentence as its label;
    20 `.cv-hist-col` bars, the middle 8 in 10 in `.mid` with `--ink-soft`,
    the tails `--ink-faded` at reduced opacity; `.cv-hist-axis` gives start,
    middle and end). The note says the last bar also holds every box above
    its range (at most 1 in 100). Figures are rounded like the row spread
    ($1, $5 or $10 by size), so they match the box tile.
  - `details.cv-how` "How this is worked out" (`.cv-how-body`,
    `.cv-how-top`): the pack count and that MTGJSON publishes the pack
    contents, the $1 rule and toppers (from `box_ev_basis`), coverage
    (`ev_coverage` of the box row), the 10,000 simulated boxes, that the
    average uses expected copies while the simulation draws whole packs (both
    figures), the five cards that carry the most value (price times expected
    copies per box, with their share of the average), and that it is not a
    forecast. Never a chance of profit or a break even rate.
  - `.cv-pv-also` / `.cv-pv-find` in the preview: "Also in 3 precons and
    2 boxes" (other than the list it is shown in; "Where else to get this
    card" until both indexes, loaded on the first preview, are in). It
    opens the finder dialog in place of the card list.
  - Escape closes the innermost layer: tooltip, then preview, then dialog.
    After Escape closes a preview, hover opens nothing until the mouse moves.
  - `.dlg-actions` / `.cv-actions` under the title bar: "Copy link"; for a
    deck also "Copy decklist" ("1 Card Name" per line, commanders first;
    enabled once the file is in), `.cv-link` "Commander page on EDHREC" and
    "Find combos on Commander Spellbook" (where the copied list is pasted).
    `.dlg-status` says what happened ("Link copied", or the link itself when
    the clipboard refuses).
- Finder dialog `#findModal` (`.find-modal-inner`, `#findModalTitle`
  "Where to get Sol Ring", `#findBody` as a `.finder`): `.dlg-actions`
  with "← Back to the card list" (only when opened from a preview; it
  reopens that list) and "Copy link". `?card=<key>` (the card's index key,
  `[a-z0-9]`) opens it; a key that is no card says so inside the dialog.
  Rows in it open a deck's chart or a box's card list, replacing it.
- **Share links and Back.** `?deck=<id>` opens a deck's card list and
  `?deck=<id>&view=chart` its chart; `?box=<stem>:<kind>` opens a box type's
  card list (`<stem>` is the box card file's name, `<kind>` its type) and
  `&view=chart` its chart. Renamed ids resolve through `formerIds` and the
  address is rewritten to the current id. The price chart dialog has a
  "Copy link" too (hidden for the collection line). Opening any dialog by
  hand pushes a history entry, so Back closes it and Forward reopens it;
  Close, Escape and the backdrop step back over that entry. A share link
  opened from outside becomes an entry over the plain page, so Back closes
  the dialog instead of leaving the site. Filter parameters stay beside the
  dialog's (filters still use `replaceState`). A box tab change rewrites the
  address to that type.

### Misc
- `.sr-only` (visually hidden text for screen readers), `.footer-note`
  (sources, privacy, Fan Content notice), `.empty`.
- `.saved-data` (`.saved-data-head` / `.saved-data-actions` /
  `.saved-data-status` / `.saved-data-note`) in the footer: "Back up"
  downloads every saved key as `mtg-tracker-backup-YYYY-MM-DD.json`;
  "Restore" reads one back after a confirmation that names what it replaces.
  Every key is checked whole (types, ranges, id shapes); a key that fails is
  left out and named, anything the file lacks stays as it is, and a file
  that is not a backup changes nothing. The note warns that Safari can clear
  a site's storage after seven days of use without a visit.

---

## 8. Conventions for new components

1. **Tokens only:** color via `var(--…)`; radius via `--radius*`; fonts via
   `--font-*`. No raw hex, no new font.
2. **Numbers are mono** (`--font-mono`); prose is Inter; only the masthead and
   flourishes are Cormorant.
3. **Theme both ways:** verify AA contrast in light *and* dark. Gold behind
   dark text is `--gold-fill`; sage as text is `--sage-text`.
4. **Accent = meaning:** burgundy for money, gold for links and featured, sage
   for savings and positive. Don't decorate with them.
5. **Mobile safe:** single column at narrow widths; wrap control rows; cap any
   intrinsic width control (selects) at `max-width: 100%`; check 320px.
6. **Accessible by default:** real buttons and links, a label on every control,
   `aria-pressed` on toggles, visible `:focus-visible` rings
   (`--gold-bright`), and screen reader context via `.sr-only` when several
   cards share a control name.
7. **Honest labels:** every price says whose price and which kind (Market,
   lowest listing, in stock). Anything modeled (box EV, the TCG sell estimate)
   carries a visible note, history lines show only recorded values, and a
   preorder says it is one.

---

## Tech context

- **Single file:** all HTML, CSS (in one `<style>`) and JS (one inline
  `<script>`, vanilla, no framework, no build step) live in `index.html`.
- **Content Security Policy:** GitHub Pages cannot send headers, so a meta CSP
  in `index.html` allows only same origin data, Google Fonts and the page's own
  inline script, by **sha256 hash**. Any edit to the inline script must
  recompute that hash, or the browser refuses to run the app:
  `python3 -c "import re,hashlib,base64;s=open('index.html').read();b=re.search(r'<script>(.*?)</script>',s,re.S).group(1);print(base64.b64encode(hashlib.sha256(b.encode()).digest()).decode())"`.
  Setting `LIVE_PRICES_REMOTE_URL` would also need that host in `connect-src`.
  Links built from data go through `safeUrl` (HTML) or `safeHref` (a DOM
  property), which allow only www.tcgplayer.com, www.cardkingdom.com,
  scrycheck.com, edhrec.com and commanderspellbook.com; the fixed footer
  links are written into the page. The EDHREC slug is the face commander's
  name with accents stripped, lowercase, apostrophes, commas and periods
  removed and other runs of non alphanumeric characters turned into one "-".
  The page never fetches EDHREC (its terms forbid automated queries).
- **Dated constants:** `CK_CREDIT` (Card Kingdom pays 130% of a sealed cash
  offer as store credit, checked 2026-11-14 on
  https://www.cardkingdom.com/purchasing/mtg_sealed) is typed in by hand,
  never scraped; update its date when checking again. `OUTLIER_RATIO` (20)
  mirrors the scraper's outlier rule and only words the note.
- **Data:** `prices.json` (live prices from TCGplayer and Card Kingdom, crack
  values, bundles, boxes), `prices_history.json` (90 calendar days of changes),
  `history_long.json` (one TCGPlayer Market point a week per deck, box and
  bundle since Feb 2024, loaded when a chart opens), `cards_index.json`
  (card to decks) and `box_index.json` (card to box types with the chance
  per pack, about 2.2 MB, loaded with it by the finder, the planner and the
  first card preview), `decks.json` (catalog). Regenerated daily
  at 01:23 UTC by `scrape_prices.py` in GitHub Actions; served by GitHub Pages
  (`.nojekyll`, so these `.md` files are served raw, never as `.html`). How each
  number is computed is in `DATA-SOURCES.md`.
- **Saved data:** watchlist, owned decks and targets live in `localStorage`
  (`precon-*`), with `precon-collection` (`{"schema":1,"items":{"deck:<id>"
  | "box:<set name>::<type>" | "bundle:<id>": {qty, paid, opened}}}`),
  `precon-lastvisit` (a local date, written once the price history has
  loaded), `precon-theme` and `precon-cardview`. `precon-owned` stays the
  owned flag: a deck is in the collection exactly when it is owned, and
  every load and save matches the two (existing owners get one copy per
  owned deck, priced at MSRP, on their first visit with this version). Each save rereads storage and applies one change, a `storage`
  event keeps tabs in sync, and renamed ids move through `decks.json`
  `formerIds` (written back). An id that is in neither `decks.json` nor any
  `formerIds` is removed from storage on the next visit; only the id of an
  entry still in `decks.json` but skipped as malformed is hidden but kept.
  **Removing a deck from the catalog, even briefly, erases every visitor's
  watch, owned and target data for it.** Record a rename in `formerIds`, and
  delete an entry only when the deck should leave the page for good.
- **Service worker** (`sw.js`, cache `mtg-tracker-v7`):
  - Navigations are network first and stored under one key, `./index.html`,
    whatever the query. JSON data is network first too, revalidated with ETag
    or If-Modified-Since and kept in the unversioned `mtg-tracker-data` cache.
    Both wait at most 4 s (`NETWORK_TIMEOUT_MS`) before serving the cached
    copy, while the network answer still refreshes the cache. Data falls back
    to the cached copy on a network or HTTP error.
  - `install` precaches the shell (required), the icons and manifest (best
    effort), and `decks.json`, `prices.json` and `prices_history.json` (best
    effort), so a first visit works offline. A cached JSON file is rewritten
    only when its ETag, or its Last-Modified plus Content-Length, changes.
  - `activate` deletes only `mtg-tracker-*` and `precon-ledger-*` caches and
    always claims clients.
  - Bump `CACHE` whenever `sw.js` or its `ASSETS` list changes; `index.html`
    changes need no bump.
- **Icons:** `icon-maskable-192.png` and `icon-maskable-512.png` are
  `icon-512.png` scaled to 80% on a full bleed `#15120e` background, inside the
  maskable safe zone. Regenerate them if `icon-512.png` changes.
- **Canonical repo:** `~/precon-tracker` (has the GitHub remote and Pages
  deploy). Point design tooling and round trips here, not at any local preview
  copy.
- **Vendors:** TCGplayer (via TCGCSV) and Card Kingdom (official pricelist
  API) only. Singles and crack data via MTGJSON; power scores from ScryCheck.
  Amazon, Zulus Games and Mana Pool are intentionally out of scope (see
  `DATA-SOURCES.md`).
