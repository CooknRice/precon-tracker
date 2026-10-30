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
| `🔎` | Staple finder (card search) |
| `📊` | Portfolio panel |
| `◐` | Theme toggle |
| `●` | Live price dot |
| `✓` | Owned deck |
| `🟢 🟡 🔴` | Buy signal levels (good buy / fair / hold) |
| `▲` | "Profit to crack" flag |
| `⚠` | Stale data and blocked storage banners |
| `→` | Outbound "view on …" links and arrows in ranges |

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
  `.staple-cardname` / `.staple-empty` / `.staple-alts` and `.staple-alt`
  ("Did you mean" buttons): the cheapest precon containing a card.
- **Best buys feed:** `section.best-deals` (`.best-deals-head`,
  `.best-deals-grid`) of `.best-deal` / `.uf-item` tiles (`.bd-name`,
  `.bd-price`, `.bd-disc`) with a `.uf-kind` badge (`.uf-deck`, `.uf-box`,
  `.uf-bundle`). Bundles are links to their product page.
- Deal of the Day and the feed render once, after `prices_history.json` has
  loaded or failed; until then they show a `.pending` placeholder.
- `.portfolio` (`.portfolio-grid` / `.portfolio-head` / `.pf-stat` /
  `.pf-label` / `.pf-val` / `.pf-sub`): owned collection value against MSRP.
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
  earlier run.
- Signals: `.signals-row` with one `.buy-signal` (`.buy` / `.ok` / `.hold`) ·
  `.power-row` (`.power-score` / `.power-label` / `.power-bracket` /
  `.power-divider` / `.power-scan`).
- Actions: `.vendor-row` / `.vendor-btn`, `.watch-toggle`, `.own-toggle`,
  `.target-row` / `.target-input` / `.target-hit` / `.target-err`,
  `.compare-toggle`. Toggling one keeps focus on the same control after the
  grid rerenders.
- `.bundle-note` (`.bundle-label` / `.bundle-savings` / `.bundle-basis`): the
  set bundle price, and "saves $X" only when the saving rounds to at least
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
  `.compare-modal-close` / `.compare-table` (`th.deck-h`).
- Price chart: `.chart-modal-inner` / `.chart-header` / `.chart-title` /
  `.chart-subtitle` / `.chart-svg` / `.chart-canvas-wrap` / `.chart-summary` /
  `.chart-price-now` / `.chart-delta` / `.chart-range` / `.range-btn` (1W, 1M,
  3M) / `.chart-note` / `.chart-empty`. Inline `.sparkline` previews open it.
  - Points are placed by calendar date as a **step line** (a price holds until
    the next change). The range buttons cut by date back from the history
    file's date and carry in the price at the cutoff.
  - The line is the recorded history (`best`: TCGplayer Market, or Card
    Kingdom in stock). Today's headline price, when it differs, is drawn apart
    from the line as a burgundy `.buy-dot` joined by a dashed `.buy-link`; a
    listing price is never spliced into the recorded line.
  - A gap in history (no tracked buyable price) ends the line instead of
    bridging it.

### Misc
- `.sr-only` (visually hidden text for screen readers), `.footer-note`
  (sources, privacy, Fan Content notice), `.empty`.

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
  Links built from data go through `safeUrl`, which allows only
  tcgplayer.com, cardkingdom.com and scrycheck.com; the fixed footer links are
  written into the page.
- **Data:** `prices.json` (live prices from TCGplayer and Card Kingdom, crack
  values, bundles, boxes), `prices_history.json` (90 calendar days of changes),
  `cards_index.json` (card to decks), `decks.json` (catalog). Regenerated daily
  at 01:23 UTC by `scrape_prices.py` in GitHub Actions; served by GitHub Pages
  (`.nojekyll`, so these `.md` files are served raw, never as `.html`). How each
  number is computed is in `DATA-SOURCES.md`.
- **Saved data:** watchlist, owned decks and targets live in `localStorage`
  (`precon-*`). Each save rereads storage and applies one change, a `storage`
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
