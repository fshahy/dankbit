# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Stack

```bash
# Start all services
docker compose up -d

# Rebuild after code changes
docker compose up -d --build

# Rebuild a single service (e.g. after editing dankbit_ws_batch.py)
docker compose up -d --build dankbit_ws

# Tail logs
docker compose logs -f
docker compose logs -f dankbit_ws

# Restart just the Odoo addon (no rebuild needed for Python changes to my_addons)
docker compose restart web
```

After first startup, install the Dankbit addon via Odoo UI: Apps → search "Dankbit" → Install.

**Required `.env` variables:**
```
DANKBIT_POSTGRES_DB=<db_name>   # any name; db1 is the development default
DERIBIT_KEY=<key>
DERIBIT_SECRET=<secret>
```
The `UID` and `GID` env vars are also needed so the `web` container writes files as the host user (dev default: `1000`/`1000`). `docker-compose.yml` sets `user: "${UID}:${GID}"` on the `web` service and `chown`s the Odoo data dir to that UID/GID before starting — the values must match whichever host user owns the bind-mounted directories, so Odoo's writes (filestore, logs) land with an owner the host can actually manage. Check with `ls -ln <bind-mounted dir>` if unsure. Setting it to `0`/`0` (root) is correct only when the production host directory itself is root-owned — it also means Odoo runs as root inside the container, a broader security surface than an unprivileged UID.

> **Production note:** `config/odoo.conf` contains `dbfilter = ^db1$` — update this to match the actual database name.
> **Production note:** production's `.env` uses `UID=0`/`GID=0` (root), unlike the dev default of `1000`/`1000` — see above for when that's the correct choice.

## Architecture

Two Docker services talk to a shared PostgreSQL database:

```
Deribit WS API ──► dankbit_ws (Python asyncio) ──► PostgreSQL
                                                        │
                   Deribit REST API ◄── Odoo cron       │
                                                        ▼
                                               Odoo 18 (web:8069)
                                                        │
                               ┌───────────────────────┤
                               │                       │
                        PNG charts                TradingView
                  /BTC-<EXPIRY>, /i/BTC-<EXPIRY>  /chart/BTC
```

**Trade data enters two ways:**
1. **WebSocket (primary):** `dankbit_ws_service/dankbit_ws_batch.py` — connects to Deribit, authenticates, fetches all BTC+ETH option instruments, subscribes to `trades.<instrument>.raw` channels in chunks of 400, inserts rows directly into PostgreSQL with `ON CONFLICT IGNORE` on `deribit_trade_identifier`.
2. **REST backfill:** `Trade.get_last_trades()` — runs every minute via cron; ensures no trades were missed by paging through the last few days of Deribit REST API history.

**PNG chart rendering:** Per-expiry routes like `/BTC-7MAY26` aggregate all trades for that expiry, compute a Black-Scholes portfolio delta and dollar-gamma (GEX = Γ × S²) curve, and return a PNG via matplotlib's Agg backend (`Figure + FigureCanvas`, never `pyplot` — server-safe). Overlaid lines: gamma peaks and bottoms (dashed black), delta=0 crossings (solid — green for "supply" [delta negative below, positive above], red for "demand" [delta positive below, negative above]).

**Zones chart (`/<instrument>/zones`):** a distinct plot mode — separates that instrument's trades since 00:00 UTC into buy-side and sell-side (trade domain uses `("name", "=ilike", f"{instrument}-%")`, an anchored/left-prefix match — not a bare `ilike` substring match — so a query for one expiry can never pull in another instrument's trades), builds two independent `OptionStrat` payoff curves via `options.build_zone_curves()` (`long_call`/`long_put` for buys, `short_call`/`short_put` for sells — same payoff math as the combined-portfolio routes, just accumulated into two separate objects, plus the crossing-based auto-zoom below — same function `dankbit.zones.extrema` calls, see Models), and plots them together via `OptionStrat.plot_zones()`: green "Longs" curve, red "Shorts" curve, blue vertical line at index price — `plot_zones()` itself draws no text overlay. The `Short Max`/`Long Min`/`Top Box`/`Bottom Box` info (previously drawn inside the PNG via `ax.text()`) is now rendered as page HTML instead, alongside further per-expiry stats: `chart_png_zones` calls `options.zone_summary(STs, longs_curve, shorts_curve, index_price)` (module-level function, same extrema/box-boundary definitions as `dankbit.zones.extrema` — see Models) for `Short Max`/`Long Min`/`Top Box`/`Bottom Box`/`Top Intersection`/`Bottom Intersection`, then separately computes 4 gamma extrema restricted to the single nearest expiry among that route's own `trades` (mirroring `dankbit.zones.extrema`'s "next expiry only" restriction) via `gamma.portfolio_gamma()`: `Long Call Gamma Peak`/`Long Put Gamma Peak` (price where the long-call/put gamma curve peaks, `argmax`) and `Short Call Gamma Bottom`/`Short Put Gamma Bottom` (price where the short-call/put gamma curve bottoms out, `argmin` — short positions carry negative gamma, so the relevant extremum is a trough, not a peak). All of this is assembled into `zone_info_lines` (grouped into 4 blocks — price extrema; boxes; gamma peaks/bottoms; intersections — separated by blank spacer lines, a `" "` non-breaking space since a truly empty/whitespace-only `<div>` can collapse to zero height) and passed to the shared `dankbit_page` template's `zone_info_lines` context var, rendered as a fixed top-left overlay div (`.zone-info`, `user-select: text` so it's copyable, only shown when that var is present, so every other PNG route using `dankbit_page` is unaffected). A separate bottom-left annotation (added by `chart_png_zones` itself, still drawn inside the image via `ax.text()`, not moved) shows the long/short trade counts since 00:00 UTC. Restores a feature this repo had (and removed) in its early history (`d416bcd` "add zones" → removed in `6c293d6`), reimplemented against the current architecture rather than reusing the old pyplot-based code. **Auto-zoomed x-axis:** the configured `from_price`/`to_price` settings are only used for an initial wide pass to locate where the Longs/Shorts curves cross (linear interpolation on the sign change of `longs.payoffs - shorts.payoffs`, same technique as the delta=0 crossing finders); the curves are then rebuilt over `[leftmost_crossing - margin_below, rightmost_crossing + margin_above]` at the same `steps` resolution, so the rendered chart is tightly framed around the relevant region instead of the full (much wider) configured range. Margin is asset-dependent: BTC uses ±$2000; ETH uses ±$100 (ETH's much smaller price scale made the ±$2000 BTC margin blow out the auto-zoom). Falls back to the original wide range if the curves never cross.

**TradingView chart:** `/chart/BTC` and `/chart/ETH` serve a Lightweight Charts v4 page with live Deribit perpetual futures candles (proxied server-side) and price lines refreshed on `dankbit.refresh_interval`:
- Delta=0 lines (3 sets) — each a single `createPriceLine`, `lineWidth: 1`, `lineStyle: Solid`. Color is by crossing type, not by set: **black** ("S/D" in the legend) for "supply" (delta negative below, positive above), **red** ("Tie-out" in the legend) for "demand" (delta positive below, negative above — the important case). Set identity is shown via the line's `title` only. Every `createPriceLine` call across the chart (delta=0 sets, gamma peak/bottom, ruler) sets `axisLabelColor: 'black'` so the axis label box is always black regardless of the line's own color — only the label background is forced black, not the line stroke. `addLineSeries` (Net Call/Put Gamma) has no equivalent per-series label-color override in Lightweight Charts v4, so those labels still match their line color (blue/orange):
  - **Weekly** — sourced from `/api/delta-zero/<instrument>`
  - **Monthly** — sourced from `/api/delta-zero/<monthly-instrument>`
  - **All** — all active expiries; sourced from `/api/delta-zero-all/<asset>`
- Daily 24H / Daily+1 24H delta=0 lines — two distinct, always-**black** `createPriceLine` sets (not type-colored like Weekly/Monthly/All, and with **no footer-legend entry**), titles `Daily 24H`/`Daily+1 24H`, sourced from `/api/delta-zero-tomorrow/<asset>` and `/api/delta-zero-day-after-tomorrow/<asset>` respectively — both thin wrappers around the shared `_delta_zero_for_calendar_day(asset, days_ahead)` helper (`days_ahead=1`/`2`) so the two can never disagree on how a calendar-day expiry/trade-window is computed. Each targets the specific expiry landing `days_ahead` calendar days out (UTC) — computed directly as `today + N days` formatted to Deribit's no-leading-zero day convention (e.g. `7JUL26`), not "nearest active expiry" (`MIN(expiration) >= NOW()`), since the latter can still resolve to *today's* not-yet-happened expiry. Restricted to trades from the trailing 24h (`deribit_ts >= NOW() - INTERVAL '24 hours'`), matching Quadrant Gamma's trade-window convention. Replaces the earlier, unused `/api/delta-zero-next/<asset>` endpoint (nearest-expiry + all-time trades, never wired to the frontend).
- Gamma peak/bottom lines: Weekly (lineWidth 2) and Monthly (lineWidth 1) — violet, sourced from `/api/gamma-levels/<instrument>`. Each peak/bottom entry is `{price, delta_positive}` — the endpoints compute `delta.portfolio_delta` over the same `STs`/`agg_trades` used for gamma, then `np.interp(px, STs, d_arr) > 0` at the extremum's price. Title suffix combines gamma sign (implicit from peak `∧` = gamma>0, bottom `∨` = gamma<0, guaranteed by `find_gamma_peaks`/`find_gamma_bottoms`'s threshold logic) with delta sign into standard options-Greeks labels: peak+`delta_positive` → `Long Call`, peak+not → `Long Put`, bottom+`delta_positive` → `Short Put`, bottom+not → `Short Call` (e.g. `Weekly ∧ Long Put`, `Monthly ∨ Short Call`) — gamma is always positive for long option exposure and negative for short, so this combination is a technically valid characterization of the net Greeks profile at that price. `drawGammaLines(data, lineStore, lineWidth, prefix, color)` takes an explicit `color` (defaults to `'violet'`).
- Quadrant Gamma lines (2) — `Net Call Gamma` (`#1565c0`) and `Net Put Gamma` (`#e65100`), `addLineSeries` on their own `priceScaleId: 'qg'`, confined to a bottom panel via `scaleMargins: { top: 0.8, bottom: 0 }` — a separate scale from candles, so the axis label shows the real (unrescaled) gamma value, and dragging/zooming the main candle price axis doesn't affect this panel (or vice versa); the time axis is always shared regardless, so horizontal scroll/pan stays in sync. Real values are also in `/api/quadrant-gamma/<asset>`. The endpoint truncates each `computed_at` to the hour (minutes/seconds zeroed) before building `"t"`, and collapses any same-hour duplicates to the latest row — the stored `computed_at` values themselves are left untouched, only the API response is rounded. Only shown on the **1h** timeframe — `refreshQuadrantGamma()` clears both series (`setData([])`) when `INTERVAL !== '1h'`, and `setTf()` calls it on every timeframe switch. Net = buyer + seller per quadrant (seller is already negative, per `portfolio_gamma`'s direction sign) — no backfill, starts empty and grows one point per hour as `dankbit.quadrant.gamma`'s cron runs
- Zones Extrema lines (2) — `Top Intersection` (green, `#2e7d32`) and `Bottom Intersection` (red, `#c62828`), `addLineSeries` on the **default/main price scale** (no `priceScaleId` override) since these are plain BTC/ETH price levels, not dollar amounts — they render directly alongside the candles rather than in the 'qg' bottom panel. Sourced from `/api/zones-extrema/<asset>`, each point is `dankbit.zones.extrema`'s `top_intersection`/`bottom_intersection` at that row's `computed_at` — the nearest Longs-vs-Shorts payoff-curve crossing above/below the index price at that tick, not either curve's own zero-crossing — connected point-to-point by the line series (same mechanism as Quadrant Gamma's Net Call/Put lines, just on the main scale instead of 'qg'). The endpoint truncates each `computed_at` to the hour (minutes/seconds zeroed) before building `"t"`, and collapses any same-hour duplicates to the latest row — the stored `computed_at` values themselves are left untouched, only the API response is rounded (same pattern as `/api/quadrant-gamma`). Only shown on the **4h** timeframe — `refreshZonesExtrema()` clears both series when `INTERVAL !== '4h'`, and `setTf()`/`init()` call it every timeframe switch.
- Zones boxes (2, plus 2 more for the next expiry — see below) — filled rectangles (`#fbc02d`, 20% opacity fill, matching top+bottom border lines) marking the above-price and below-price zero-crossing zones for the **nearest** active expiry. Unlike every other indicator here, these are **not** read from stored history — sourced live from `/api/zones-box/<asset>`, which calls `dankbit.zones.extrema.get_box()` to compute fresh on every request via the same `_compute_asset()` helper `_snapshot_asset()` uses, but returns the result without persisting it (see Models: nothing reads box-boundary history, only the latest value is ever drawn). Each box is built from two `addBaselineSeries` (`aboveBoxSeries`/`belowBoxSeries` — a flat top-price line filled down to a `baseValue` bottom price) plus a plain `addLineSeries` for the bottom edge (`aboveBoxBottomLine`/`belowBoxBottomLine`), since `addBaselineSeries` only strokes its top edge. Top/bottom of each box are `max`/`min` of that side's two zero-crossing prices (`short_zero_above_price`/`long_zero_above_price` for the above box, `short_zero_below_price`/`long_zero_below_price` for the below box); a `0.0` value means "no crossing on that side" and clears the box instead of drawing a bogus one at the configured price-range floor. Time span is `[snapTo18(computed_at - 2 days), computed_at]` — the right edge is now (the current candle), no forward extension; the left edge is 2 days back, snapped to 18:00 on that calendar day (`snapTo18()`) rather than keeping `computed_at`'s own time-of-day — anchored to the live lookup's own timestamp (recomputed — and thus re-anchored — on every poll, not fixed to a stored tick). Only shown on the **4h** timeframe, same gating as the Zones Extrema lines.
- Zones boxes, next expiry (2) — identical mechanism to the above, but for the active expiry immediately *after* the nearest one, in blue (`#1e88e5`, 20% opacity fill) instead of yellow so the two expiries stay visually distinguishable when their price zones overlap, and a shorter 1-day lookback (`[snapTo18(computed_at - 1 day), computed_at]`, vs the yellow boxes' 2-day lookback) since it's a nearer-term view. `aboveBoxSeriesNext`/`belowBoxSeriesNext`/`aboveBoxBottomLineNext`/`belowBoxBottomLineNext`, sourced from `/api/zones-box-next/<asset>` → `dankbit.zones.extrema.get_box_next()` → `_compute_asset(asset, expiry_index=1)`. `_compute_asset`'s `expiry_index` param picks the Nth distinct active expiry (soonest-first) via a raw `SELECT DISTINCT expiration ... ORDER BY ... LIMIT` query (not `search_read`+`limit`, which could return N rows all sharing the nearest expiration since a single expiry can have 100k+ trades; not `read_group`, which buckets Datetime fields by month by default) — `expiry_index=0` (default) is the nearest expiry, used everywhere else in this file.
- No footer legend — removed entirely (`#dz-legend` div and its CSS); all lines/series remain, just without a color-key legend.
- Footer status shows: `<weekly-expiry>  ·  <monthly-expiry>  ·  N trades  ·  Nearest: <expiry>`; trade count = all active trades up to and including monthly expiry (from monthly endpoint). The `Nearest:` segment is the same nearest-active-expiry the yellow zones boxes use (`dankbit.zones.extrema.nearest_expiry()`, a cheap standalone `SELECT DISTINCT expiration ... LIMIT 1` lookup, no curve-building) — shown regardless of timeframe (`refreshNearestExpiry()`, unconditional unlike the 4h-only boxes), via a shared `updateStatusLine()` that both it and `refreshMonthlyDeltaZero()` call so neither overwrites the other's half of the status text.

## Odoo Addon (`my_addons/dankbit/`)

The addon depends only on `website`. Key components:

**Models:**
- `dankbit.trade` — Core model. Fields map directly to Deribit trade fields. `strike` and `option_type` are computed from `name` (instrument name like `BTC-29NOV24-98000-P`). `days_to_expiry` is UTC-safe. `get_hours_to_expiry()` returns continuous time used in Black-Scholes.
- `dankbit.quadrant.gamma` — Hourly snapshot of dollar gamma (Γ × S²) split into 4 quadrants (buyer/seller × call/put) per asset, computed from the trailing 24h of active-expiry trades at the current index price. `compute_snapshot()` is the cron entry point (BTC then ETH, one row per asset per run); skips creating a row for an asset if the index price fetch fails, rather than persisting a misleading zero.
- `dankbit.zones.extrema` — Snapshot of two prices per asset: `top_intersection`/`bottom_intersection`, the nearest Longs-vs-Shorts payoff-curve crossing above/below the current index price (where the two curves cross each other, not where either crosses zero — same `longs.payoffs - shorts.payoffs` sign-change `build_zone_curves()` finds internally for its own ±$2000 auto-zoom, just filtered by side and exposed here). `_compute_asset(asset, expiry_index=0)` is the shared computation core (the `expiry_index`-th soonest active expiry — 0 is nearest, 1 is the one after that, via a raw `SELECT DISTINCT expiration` query so a single expiry's 100k+ trades can't crowd out the next one — trades since 00:00 UTC for that expiry, `options.build_zone_curves()`, then `top_intersection`/`bottom_intersection` plus 4 zero-crossing box boundaries — see below) — returns `None` if there's nothing computable (missing index price/expiry at that index/since-midnight trades) rather than writing a row that would silently look like real data (e.g. the configured price-range floor). Three callers use it differently:
  - `compute_snapshot()` → `_snapshot_asset()` (always `expiry_index=0`) is the every-4-hours cron entry point (BTC then ETH, one row per asset per run) — **persists only** `top_intersection`/`bottom_intersection` (the fields the TradingView Zones Extrema *lines* plot as history).
  - `get_box(asset)` — called live by `/api/zones-box/<asset>` on every request; returns `_compute_asset()`'s (nearest-expiry) result (including the 4 zero-crossing box fields) **without persisting it**. These aren't stored: nothing reads box-boundary history, only the latest value is ever drawn (see TradingView chart's Zones boxes), so recomputing on demand avoids writing a DB row every tick for data nothing reads historically.
  - `get_box_next(asset)` — same as `get_box()` but `expiry_index=1` (the expiry right after the nearest one); called live by `/api/zones-box-next/<asset>` for the chart's second, blue-colored pair of boxes.
  - `nearest_expiry(asset)` — standalone cheap lookup (no curve-building), returning just the nearest active expiry as a Deribit-style day-string (e.g. `"9JUL26"`); shares `_distinct_expirations()` with `_compute_asset()` rather than duplicating the DISTINCT-expiration SQL. Called by `/api/nearest-expiry/<asset>` for the TradingView footer's `Nearest:` text.
- `res.config.settings` extension — Chart price ranges, refresh interval, Deribit cache TTL, weekly/monthly expiries for BTC and ETH.

**Controllers (`main.py`):**
- PNG routes: `/<instrument>`, `/<instrument>/<hours>`, `/i/<instrument>`, `/<instrument>/s` (slideshow of hours_list `[0, 4, 8, 12, 24]`), `/<asset>/weekly`, `/<asset>/monthly` (bookmarkable shortcuts — read `weekly_expiry`/`monthly_expiry` (or `eth_*` for ETH) from settings and delegate straight into `chart_png_until`, so they always render identically to `/i/<asset>-<expiry>` for whatever expiry is currently configured; 404 for assets other than BTC/ETH), `/<instrument>/zones` (Longs-vs-Shorts payoff zones for that instrument's trades since 00:00 UTC — see below)
- JSON API: `/api/delta-zero/<instrument>`, `/api/delta-zero-all/<asset>`, `/api/delta-zero-tomorrow/<asset>`, `/api/delta-zero-day-after-tomorrow/<asset>` (calendar day-N expiry, trailing-24h trades only), `/api/gamma-levels/<instrument>`, `/api/klines/<asset>`, `/api/quadrant-gamma/<asset>`, `/api/zones-extrema/<asset>` (30-day lookback of stored `dankbit.zones.extrema` rows, one entry per snapshot with `t`, `index_price`, `top_intersection`, `bottom_intersection`), `/api/zones-box/<asset>` (live, non-persisted — calls `get_box()` fresh on every request; `t`, `index_price`, `short_zero_above_price`, `long_zero_above_price`, `short_zero_below_price`, `long_zero_below_price`), `/api/zones-box-next/<asset>` (same shape, but `get_box_next()` — the expiry after the nearest one), `/api/nearest-expiry/<asset>` (cheap standalone lookup — `dankbit.zones.extrema.nearest_expiry()`, no curve-building; `expiry` e.g. `"9JUL26"`)
- TradingView page: `/chart/<asset>` (reads weekly expiry from settings, returns 404 for unconfigured assets)
- Key helpers:
  - `find_gamma_peaks(STs, gamma_curve, min_fraction)` — local maxima of the gamma curve
  - `find_gamma_bottoms(STs, gamma_curve, min_fraction)` — local minima of the gamma curve
- `delta.py` / `gamma.py` — Pure Black-Scholes functions. Dollar gamma is `Γ × S²`. No side effects.
- `options.py` — `OptionStrat` class. Accumulates legs, then `plot()` returns a matplotlib `Figure`; `plot_zones(longs_curve, shorts_curve, index_price, title)` renders the Longs/Shorts zones chart from two separately-accumulated `OptionStrat` instances. Uses Agg backend; do not import `pyplot` here. `build_zone_curves(instrument_name, index_price, trades, from_price, to_price, steps)` is the shared Longs/Shorts curve-building + crossing-based ±$2000 zoom logic (module-level function, not a method) — used by both `chart_png_zones` and `dankbit.zones.extrema._compute_asset`, so the PNG chart, the cron-stored extrema, and the live zones-box lookup can never compute different values for the same trades. `find_zero_crossings(STs, curve)` is the shared zero-crossing finder (linear interpolation on each sign change) used by `build_zone_curves`'s own Longs-vs-Shorts crossing and by `_compute_asset`'s per-curve above/below-price crossings. `zone_summary(STs, longs_curve, shorts_curve, index_price)` computes `short_max_price`/`long_min_price`, the top/bottom box boundaries, and `top_intersection`/`bottom_intersection` (nearest Longs-vs-Shorts crossing above/below price) from an already-built pair of curves — used by `chart_png_zones` to populate the zones-PNG page's top-left info overlay (see Zones chart above); a separate, differently-shaped computation in `dankbit.zones.extrema._compute_asset` (needs the 4 raw box fields individually rather than a summarized pair, and doesn't need `short_max_price`/`long_min_price` at all since it no longer persists them) duplicates the box and intersection math rather than sharing this function.

**Settings fields (`res.config.settings`):**
- `from_price`, `to_price`, `steps` — BTC chart price range
- `eth_from_price`, `eth_to_price`, `eth_steps` — ETH chart price range
- `refresh_interval` — page auto-refresh interval (seconds)
- `deribit_timeout`, `deribit_cache_ttl` — Deribit API behaviour
- `weekly_expiry`, `monthly_expiry` — BTC expiry strings (e.g. `BTC-4JUL26`)
- `eth_weekly_expiry`, `eth_monthly_expiry` — ETH expiry strings (e.g. `ETH-4JUL26`)

**Scheduled crons (data/ir_cron.xml, all `active=False` by default — activate manually post-install):**
- Every minute: `get_last_trades()` — ensures no trades were missed (e.g. during brief downtime)
- Daily: `_delete_expired_trades()` — archives expired trades
- Hourly: `compute_snapshot()` (on `dankbit.quadrant.gamma`) — snapshots the 4 quadrant-gamma metrics for BTC and ETH from the last 24h of trades
- Every 4 hours: `compute_snapshot()` (on `dankbit.zones.extrema`) — snapshots `top_intersection`/`bottom_intersection` for BTC and ETH from trades since 00:00 UTC. The zero-crossing box fields are *not* on this (or any) cron — they're computed live, on demand, by `/api/zones-box/<asset>` (see Models) instead.

**Caching:** `trade.py` uses a module-level `_DERIBIT_CACHE` dict (key → `{ts, value}`) for Deribit index price and instrument lookups. TTL is configurable via settings (`deribit_cache_ttl`, default 300s).

**Backend menu:** `Dankbit` root menu (`trade_views.xml`) with three items — `Trades` (`dankbit.trade` list/form), `Quadrant Gamma` (`quadrant_gamma_views.xml`, `dankbit.quadrant.gamma` list/form/search with BTC/ETH filters), and `Zones Extrema` (`zones_extrema_views.xml`, `dankbit.zones.extrema` list/form/search with BTC/ETH filters).

## WebSocket Service (`dankbit_ws_service/`)

Single file: `dankbit_ws_batch.py`. Authenticates with `DERIBIT_KEY`/`DERIBIT_SECRET` env vars using `client_credentials` grant before fetching instruments. Reconnects every 3 seconds on any failure. Rate-limit (`over_limit`) errors trigger a 0.5s sleep and retry.

The DB connection uses `autocommit=True` — no explicit transaction management needed.

## URL Reference

| URL | Description |
|-----|-------------|
| `/BTC-<EXPIRY>` | BTC PNG chart for a specific expiry (e.g. `/BTC-25JUL26`) |
| `/ETH-<EXPIRY>` | ETH PNG chart for a specific expiry |
| `/i/<EXPIRY>` | PNG chart for all expiries up to selected date; includes delta=0 line |
| `/BTC/weekly`, `/ETH/weekly` | Same as `/i/<asset>-<expiry>`, using the currently configured weekly expiry — bookmark once, always current |
| `/BTC/monthly`, `/ETH/monthly` | Same as above, using the configured monthly expiry |
| `/<instrument>/zones` | Longs (green) vs Shorts (red) payoff curves for that instrument's trades since 00:00 UTC (e.g. `/BTC-25JUL26/zones`) |
| `/chart/BTC` | Live TradingView chart; reads weekly expiry from settings |
| `/chart/ETH` | Live TradingView chart for ETH |
| `/help` | Payoff diagram reference page |

## TradingView Chart Notes

- Candles sourced from Deribit perpetual futures (`BTC-PERPETUAL`, `ETH-PERPETUAL`) via `/api/klines/<asset>` proxy (avoids CORS). Uses `get_tradingview_chart_data` endpoint. Deribit returns oldest-first parallel arrays (`ticks`, `open`, `high`, `low`, `close`); proxy zips and reverses to newest-first `{t,o,h,l,c}` format (t in ms). **Deribit has no native 4h resolution** (`resolution=240` errors with "unsupported resolution" — valid values are 1, 3, 5, 10, 15, 30, 60, 120, 180, 360, 720, `1D`); the `4h` interval fetches native 1h candles and aggregates every 4 into one OHLC bar server-side, bucketed by `t // (4*3600*1000)` (timestamp-based, not positional) so buckets stay calendar-aligned to 00:00 UTC regardless of the fetch window's boundaries.
- Candles refresh every 5 seconds; delta=0 lines refresh on `dankbit.refresh_interval`
- Berlin timezone applied via `berlinOffset` computed from `Intl` API
- Timeframe buttons: 1h (default) / 4h / 1d — visible windows: 1h=30d, 4h=15d, 1d=365d; Deribit resolution: 1h→60, 1d→1D, 4h→aggregated from 1h (see above). The 4h window was deliberately narrowed from an initial 90d/30d — `loadCandles()` always fetches a flat 500 bars regardless of timeframe, and Lightweight Charts renders candle body width as a fixed proportion of `barSpacing`; cramming too many bars into one screen (as 90d did, ~3.2px/bar) made each candle's body-to-gap ratio look like a sparse, "half-empty" chart. 15d keeps `barSpacing` comfortably in the ~17px range.
- Ruler tool: click twice on chart to measure price-to-price percentage
- Crosshair price label (`crosshair.horzLine.labelBackgroundColor`) is set to blue — the library default (`#131722`, near-black) was indistinguishable from the black `axisLabelColor` used on every indicator price line
- Do **not** change `title:` values in `createPriceLine()` calls — the user maintains these manually

## Odoo Gotchas

- **Manifest licence field:** Odoo only accepts a fixed set of strings. Use `"Other OSI approved licence"` (British spelling) for MIT — `"MIT"` alone will fail validation.

## Odoo Module Reload

Python changes to `my_addons/` take effect after `docker compose restart web`. XML/view changes and new model fields require upgrading the module:

```bash
docker compose exec web odoo -d <db_name> -u dankbit --stop-after-init
docker compose restart web
```
