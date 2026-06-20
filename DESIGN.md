# MTG Tracker — Design System

A reference for the visual language of MTG Tracker, written so a designer (or a
tool like Claude Design) can extend the site while staying consistent. Every
value here is taken from the live styles in `index.html` — there is no separate
stylesheet or build step (see [Tech context](#tech-context)).

---

## 1. Aesthetic

**An antique financial ledger, for Magic singles.** Warm, editorial, and quiet —
not a neon "deals" site. Think aged paper and ink, a serif masthead, monospaced
figures for anything numeric, generous whitespace, and restrained accent color
used only to mean something (a deal, a saving, a link).

Principles:
- **Warm neutrals first, accents sparingly.** Most of the page is paper + ink.
  Color appears only on prices, savings, links, and status.
- **Numbers are monospaced.** Prices, dates, ranges, counts → JetBrains Mono.
- **Flat, soft-edged surfaces.** Rounded cards, hairline dividers, no heavy
  shadows or gradients (one barely-there radial paper texture aside).
- **Two moods, one system.** A dark "ledger at night" default and a light
  "parchment" theme, built from the *same* tokens (see §2).

---

## 2. Theming

Both themes are defined by overriding the **same CSS custom properties** on the
root. Dark is the default (`:root`); light is `[data-theme="light"]`.

```
:root { … dark token values … }
[data-theme="light"] { … same token names, light values … }
```

- The theme is toggled by the `◐` button (`.theme-toggle`), persisted to
  `localStorage`, and respects `prefers-color-scheme` on first visit.
- **Because every component uses `var(--token)`, it themes automatically.** The
  only manual work is for elements that hardcode a near-white text color on a
  dark fill — those get an explicit `[data-theme="light"]` override (see the
  name classes like `.card-name`, `.dod-name`, `.staple-row-name`).

> **Rule for new work:** never hardcode a hex color. Use a token. If a token
> doesn't fit, add it to *both* theme blocks.

---

## 3. Color tokens

| Token | Dark (default) | Light (`[data-theme="light"]`) | Use |
|---|---|---|---|
| `--paper` | `#15120e` | `#f3ead4` | Page background |
| `--paper-deep` | `#1d1915` | `#fbf6e7` | Cards / panels (legacy) |
| `--paper-line` | `#2c2720` | `#ddd0b2` | Borders / dividers |
| `--surface` | `#1c1a17` | `#fbf6e7` | Card / panel base (modern) |
| `--surface-2` | `#232019` | `#f1e7cd` | Nested blocks (price card, rows) |
| `--hairline` | `rgba(237,227,202,.08)` | `rgba(44,36,22,.12)` | Soft divider |
| `--ink` | `#ede3ca` | `#2c2416` | Primary text |
| `--ink-soft` | `#c9c0a8` | `#4c4029` | Secondary text |
| `--ink-muted` | `#8a8168` | `#6f6044` | Labels |
| `--ink-faded` | `#625b49` | `#9c8c6a` | Faint hints |
| `--burgundy` | `#d97485` | `#9d2f44` | **Prices, deals** |
| `--burgundy-soft` | `#bd5d6e` | `#b0405a` | Cheaper-vendor emphasis |
| `--gold` | `#c69a3e` | `#936812` | **Links, highlights, deal badge** |
| `--gold-bright` | `#e4b954` | `#b3852a` | Hover / bright accent |
| `--sage` | `#6e8a55` | `#4d6a35` | **Good / owned / savings / best price** |
| `--deal-bg` | `#2c2518` | `#efe4c4` | Deal-card background |
| `--owned-bg` | `#1f2a1c` | `#e7eed6` | Owned-card background |

**Mana pip colors** (WUBRG + colorless), for the deck color identity dots
(`.mana-pip`, `.cb-*`):

| Token | Dark | Light |
|---|---|---|
| `--w` (white) | `#e8dcb0` | `#c9b878` |
| `--u` (blue) | `#6da8db` | `#3f7fb5` |
| `--b` (black) | `#524a52` | `#6a6168` |
| `--r` (red) | `#d87256` | `#c0572f` |
| `--g` (green) | `#7ba068` | `#5a803f` |
| `--c` (colorless) | `#9b9586` | `#8a8470` |

### Semantic accent usage (important — keep this consistent)
- **Burgundy** = money. Live prices, the deal-of-the-day price, discount badges.
- **Gold** = interactive / featured. Links, hovers, the "DEAL OF THE DAY" chip.
- **Sage** = positive value. Savings, "best price" headline, "profit to crack"
  flag, owned/watchlist state, EV that beats box price.

---

## 4. Typography

Three families, loaded from Google Fonts:

| Token | Family | Role |
|---|---|---|
| `--font-display` | **Cormorant Garamond** (500/600, italic 500) | Masthead `.title`, deal name, section flourishes |
| `--font-ui` | **Inter** (400–700) | Body, deck names, buttons, most UI |
| `--font-mono` | **JetBrains Mono** (400/500) | All numerics: prices, dates, ranges, labels, counts |

- Base: `15px / 1.55` Inter on the body.
- Masthead `.title` is the only large display type; the accented word is wrapped
  in `<em>` and rendered in burgundy italic (e.g. **MTG** *Tracker*).
- Uppercase + letterspacing (`text-transform: uppercase; letter-spacing: ~.08em`)
  is the convention for mono labels (stat labels, box type, count line).

---

## 5. Shape, spacing & layout

| Token | Value | Use |
|---|---|---|
| `--radius` | `14px` | Cards, panels |
| `--radius-sm` | `9px` | Inner blocks, buttons, inputs |
| `--radius-pill` | `999px` | Chips, badges, toggles |

- **Page container:** `.wrap` — `max-width: 1280px`, padding `32px 28px 80px`,
  centered.
- **Deck grid:** `.grid` — responsive auto-fill card grid.
- **Dividers:** 1px `var(--hairline)` (or `--paper-line`).
- **Background texture:** the body has a very subtle 3-stop radial gradient in
  gold/burgundy/cream at ~0.01–0.06 opacity, `background-attachment: fixed`.
  Keep new full-bleed surfaces transparent so it shows through.
- **Mobile:** single-column; the controls row (`.count-line`) wraps and the
  `.sort-select` is capped at `max-width:100%` to avoid overflow. New rows of
  pills/controls should `flex-wrap` similarly.

---

## 6. Iconography & emoji conventions

Emoji are used as compact, consistent glyphs (no icon font):

| Glyph | Meaning |
|---|---|
| `🏆` | Best buys / best price across vendors |
| `★` | Deal of the day, all-time-low badge |
| `🔎` | Staple finder (card search) |
| `📊` | Portfolio panel |
| `◐` | Theme toggle |
| `↑ ↓ →` | Price forecast direction |
| `🟢 🟡 🔴` | Buy-signal levels (good / fair / hold) |
| `▲` | "Profit to crack" flag |

---

## 7. Component inventory

Grouped by area. Each is a class (or class family) in `index.html`; the render
logic is in the inline `<script>`.

### Header / chrome
- `.title-block`, `.title`, `.tagline` — masthead + subtitle.
- `.meta-line` — right-aligned "Last verified / Coverage / Tracked decks".
- `.theme-toggle` — `◐` light/dark button.
- `.live-status`, `.stale-banner` — data-freshness indicators.

### Headline modules (top of page)
- `.deal-of-day` + `.dod-main` / `.dod-name` / `.dod-price(-now/-sub)` /
  `.dod-why` / `.dod-flag` — the featured single best deal.
- `.staple-finder` + `.staple-head` / `.staple-input` / `.staple-results` /
  `.staple-row` (`.staple-row-name` / `.staple-row-set` / `.staple-row-price`) /
  `.staple-cardname` / `.staple-empty` — "cheapest precon containing a card".
- **Unified feed:** `.uf-item` + `.uf-kind` (kind badges: Deck / Box / Bundle) —
  "🏆 Best buys across everything".
- `.best-deals` (`.best-deals-grid` / `.best-deals-head` / `.best-deal` / `.bd-*`)
  and `.crack-deals` (`.crack-deals-note`) — ranked deal panels.
- `.portfolio` (`.portfolio-grid` / `.portfolio-head` / `.pf-stat` / `.pf-label`
  / `.pf-val` / `.pf-sub`) — owned-collection value vs MSRP.
- `.stats` / `.stat` / `.stat-label` / `.stat-val` / `.stat-sub` — summary tiles.

### Deck card (`.card`)
- Identity: `.card-top` / `.card-name` / `.card-set` / `.card-setrow` /
  `.card-colors` / `.mana-pip` / `.card-commander` / `.card-desc` / `.card-meta`.
- Pricing block: `.price-row` / `.price-main` / `.price-label` / `.price-source`
  / `.price-msrp` / `.price-live-dot` / `.price-live-other` / `.price-low-note`
  / `.price-unverified` / `.price-checkprompt`.
- Value lines (stacked under price): `.price-range` (90-day range + near-low /
  ★ all-time-low) · `.price-zulus` (second-vendor rows for **Zulus** and **Card
  Kingdom**, `.cheaper` when they beat TCG, `.vendor-oos` when out of stock) ·
  `.price-forecast` (~30-day projection) · `.price-best` (`🏆 Best of N` cross-
  vendor winner, `.price-best-save`).
- Singles: `.crack-value` (`.worth` / `▲ crack-flag`) · `.chase-cards`
  (top cards driving value, `.chase-price` / `.chase-share`).
- Signals: `.buy-signal` (`.buy`/`.ok`/`.hold`) · `.curve-tag` · `.signals-row` ·
  `.power-row` (`.power-score` / `.power-label` / `.power-bracket` /
  `.power-divider` / `.power-scan`).
- Actions: `.vendor-row` / `.vendor-btn`, `.watch-toggle`, `.own-toggle`,
  `.target-row` / `.target-input` / `.target-hit`, `.compare-toggle`.
- `.bundle-note` (`.bundle-label` / `.bundle-savings`) — set-bundle savings.

### Sealed boxes
- `.box-section` / `.box-toggle(-hint)` / `.box-body` / `.box-set(-name)` /
  `.box-grid`.
- `.box-item` + `.box-item-type` / `.box-item-price` / `.box-item-low` /
  `.box-ev` (`.good` when EV ≥ price) / `.box-ck` (Card Kingdom price,
  `.cheaper`) / `.box-item-link` / `.box-spark`. `.box-ev-note` = methodology.

### Filters & controls
- `.filters` / `.filter-group` / `.filter-label` / `.search-input` /
  `.sort-select` / `.count-line` / `.reset-btn` / `.scope-chip`.
- Color filter: `.color-filter` / `.color-btn` / `.cb-w|u|b|r|g|c`.
- Power filter: `.bracket-filter` / `.bracket-chip` / `.power-bracket`.

### Overlays
- Compare: `.compare-bar` (`.compare-bar-actions`) / `.compare-modal`
  (`.compare-modal-inner` / `.compare-modal-close`) / `.compare-table`.
- Price chart: `.chart-modal-inner` / `.chart-header` / `.chart-title` /
  `.chart-subtitle` / `.chart-svg` / `.chart-canvas-wrap` / `.chart-summary` /
  `.chart-price-now` / `.chart-delta` / `.chart-range` / `.range-btn` /
  `.chart-empty`. Inline `.sparkline` previews open this.

### Misc
- `.discount-badge`, `.empty`.

---

## 8. Conventions for new components

1. **Tokens only** — color via `var(--…)`; radius via `--radius*`; fonts via
   `--font-*`. No raw hex, no new font.
2. **Numbers are mono** (`--font-mono`); prose is Inter; only the masthead/flourish
   is Cormorant.
3. **Theme both ways** — verify contrast in light *and* dark. If you must put
   light text on a colored fill, add a `[data-theme="light"]` override.
4. **Accent = meaning** — burgundy for money, gold for links/featured, sage for
   savings/positive. Don't decorate with them.
5. **Mobile-safe** — single column at narrow widths; wrap control rows; cap any
   intrinsic-width control (selects) at `max-width:100%`.
6. **Estimates are labeled** — anything modeled (box EV, forecast, TCG sell
   estimate) carries a visible `*`/note. Keep that honesty.

---

## Tech context

- **Single file:** all HTML, CSS (in one `<style>`), and JS (one inline
  `<script>`, vanilla — no framework, no build step) live in `index.html`.
- **Data:** `prices.json` (live multi-vendor prices), `prices_history.json`
  (90-day series), `cards_index.json` (card → decks), `decks.json` (catalog).
  Regenerated daily by `scrape_prices.py` via GitHub Actions; served by GitHub
  Pages. A service worker (`sw.js`) caches the shell + JSON.
- **Canonical repo:** `~/precon-tracker` (has the GitHub remote / Pages deploy).
  Point design tooling and round-trips here — not at any local preview copy.
- **Vendors:** TCGPlayer (via TCGCSV), Card Kingdom (public pricelist API),
  Zulus (direct). Singles/crack data via MTGJSON. Amazon is intentionally out of
  scope.
