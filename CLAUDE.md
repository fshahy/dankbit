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

**Zones chart (`/<instrument>/zones`):** a distinct plot mode — separates that instrument's trades since 00:00 UTC into buy-side and sell-side (trade domain uses `("name", "=ilike", f"{instrument}-%")`, an anchored/left-prefix match — not a bare `ilike` substring match — so a query for one expiry can never pull in another instrument's trades), builds two independent `OptionStrat` payoff curves (`long_call`/`long_put` for buys, `short_call`/`short_put` for sells — same payoff math as the combined-portfolio routes, just accumulated into two separate objects), and plots them together via `OptionStrat.plot_zones()`: green "Longs" curve, red "Shorts" curve, blue vertical line at index price. Restores a feature this repo had (and removed) in its early history (`d416bcd` "add zones" → removed in `6c293d6`), reimplemented against the current architecture rather than reusing the old pyplot-based code. **Auto-zoomed x-axis:** the configured `from_price`/`to_price` settings are only used for an initial wide pass to locate where the Longs/Shorts curves cross (linear interpolation on the sign change of `longs.payoffs - shorts.payoffs`, same technique as the delta=0 crossing finders); the curves are then rebuilt over `[leftmost_crossing - 2000, rightmost_crossing + 2000]` at the same `steps` resolution, so the rendered chart is tightly framed around the relevant region instead of the full (much wider) configured range. Falls back to the original wide range if the curves never cross.

**TradingView chart:** `/chart/BTC` and `/chart/ETH` serve a Lightweight Charts v4 page with live Deribit perpetual futures candles (proxied server-side) and price lines refreshed on `dankbit.refresh_interval`:
- Delta=0 lines (3 sets) — each a single `createPriceLine`, `lineWidth: 1`, `lineStyle: Solid`. Color is by crossing type, not by set: **black** ("S/D" in the legend) for "supply" (delta negative below, positive above), **red** ("Tie-out" in the legend) for "demand" (delta positive below, negative above — the important case). Set identity is shown via the line's `title` only. Every `createPriceLine` call across the chart (delta=0 sets, gamma peak/bottom, ruler) sets `axisLabelColor: 'black'` so the axis label box is always black regardless of the line's own color — only the label background is forced black, not the line stroke. `addLineSeries` (Net Call/Put Gamma) has no equivalent per-series label-color override in Lightweight Charts v4, so those labels still match their line color (blue/orange):
  - **Weekly** — sourced from `/api/delta-zero/<instrument>`
  - **Monthly** — sourced from `/api/delta-zero/<monthly-instrument>`
  - **All** — all active expiries; sourced from `/api/delta-zero-all/<asset>`
- Gamma peak/bottom lines: Weekly (lineWidth 2) and Monthly (lineWidth 1) — violet, sourced from `/api/gamma-levels/<instrument>`. Each peak/bottom entry is `{price, delta_positive}` — the endpoints compute `delta.portfolio_delta` over the same `STs`/`agg_trades` used for gamma, then `np.interp(px, STs, d_arr) > 0` at the extremum's price. Title suffix combines gamma sign (implicit from peak `∧` = gamma>0, bottom `∨` = gamma<0, guaranteed by `find_gamma_peaks`/`find_gamma_bottoms`'s threshold logic) with delta sign into standard options-Greeks labels: peak+`delta_positive` → `Long Call`, peak+not → `Long Put`, bottom+`delta_positive` → `Short Put`, bottom+not → `Short Call` (e.g. `Weekly ∧ Long Put`, `Monthly ∨ Short Call`) — gamma is always positive for long option exposure and negative for short, so this combination is a technically valid characterization of the net Greeks profile at that price. `drawGammaLines(data, lineStore, lineWidth, prefix, color)` takes an explicit `color` (defaults to `'violet'`).
- Quadrant Gamma lines (2) — `Net Call Gamma` (`#1565c0`) and `Net Put Gamma` (`#e65100`), `addLineSeries` on their own `priceScaleId: 'qg'`, confined to a bottom panel via `scaleMargins: { top: 0.8, bottom: 0 }` — a separate scale from candles, so the axis label shows the real (unrescaled) gamma value, and dragging/zooming the main candle price axis doesn't affect this panel (or vice versa); the time axis is always shared regardless, so horizontal scroll/pan stays in sync. Real values are also in `/api/quadrant-gamma/<asset>`. The endpoint truncates each `computed_at` to the hour (minutes/seconds zeroed) before building `"t"`, and collapses any same-hour duplicates to the latest row — the stored `computed_at` values themselves are left untouched, only the API response is rounded. Only shown on the **1h** timeframe — `refreshQuadrantGamma()` clears both series (`setData([])`) when `INTERVAL !== '1h'`, and `setTf()` calls it on every timeframe switch. Net = buyer + seller per quadrant (seller is already negative, per `portfolio_gamma`'s direction sign) — no backfill, starts empty and grows one point per hour as `dankbit.quadrant.gamma`'s cron runs
- Zones Extrema lines (2) — `Short Max Price` (green, `#2e7d32` — the "top"/max point) and `Long Min Price` (red, `#c62828` — the "bottom"/min point), `addLineSeries` on the **default/main price scale** (no `priceScaleId` override) since these are plain BTC/ETH price levels, not dollar amounts — they render directly alongside the candles rather than in the 'qg' bottom panel. Sourced from `/api/zones-extrema/<asset>`, each point is `dankbit.zones.extrema`'s `short_max_price`/`long_min_price` at that row's `computed_at`, connected point-to-point by the line series (same mechanism as Quadrant Gamma's Net Call/Put lines, just on the main scale instead of 'qg'). Only shown on the **4h** timeframe — `refreshZonesExtrema()` clears both series when `INTERVAL !== '4h'`, and `setTf()`/`init()` call it every timeframe switch.
- Footer legend (`#dz-legend`, fixed below the status line): black "S/D", red "Tie-out", violet "Gamma Extrema", blue "Net Call Gamma", orange (`#e65100`) "Net Put Gamma", dark green (`#2e7d32`) "Short Max Price", dark red (`#c62828`) "Long Min Price"
- Footer status shows: `<weekly-expiry>  ·  <monthly-expiry>  ·  N trades  ·  HH:MM:SS`; trade count = all active trades up to and including monthly expiry (from monthly endpoint)

## Odoo Addon (`my_addons/dankbit/`)

The addon depends only on `website`. Key components:

**Models:**
- `dankbit.trade` — Core model. Fields map directly to Deribit trade fields. `strike` and `option_type` are computed from `name` (instrument name like `BTC-29NOV24-98000-P`). `days_to_expiry` is UTC-safe. `get_hours_to_expiry()` returns continuous time used in Black-Scholes.
- `dankbit.quadrant.gamma` — Hourly snapshot of dollar gamma (Γ × S²) split into 4 quadrants (buyer/seller × call/put) per asset, computed from the trailing 24h of active-expiry trades at the current index price. `compute_snapshot()` is the cron entry point (BTC then ETH, one row per asset per run); skips creating a row for an asset if the index price fetch fails, rather than persisting a misleading zero.
- `dankbit.zones.extrema` — Every-4-hours snapshot of two prices per asset: `short_max_price` (where the Shorts payoff curve peaks) and `long_min_price` (where the Longs payoff curve bottoms out). `compute_snapshot()` is the cron entry point (BTC then ETH, one row per asset per run): for each asset it first finds that asset's single nearest (soonest-to-expire) active expiry (`search_read` ordered by `expiration asc`, `limit=1`), then reuses the `/<instrument>/zones` route's exact logic — trades since 00:00 UTC restricted to that one expiry (`("name", "=ilike", f"{asset}-%")` anchored match + `("expiration", "=", nearest_expiration)`), `OptionStrat.long_call`/`short_call`/etc. to build the two curves, then the same crossing-based ±$2000 zoom before locating the extrema. Skips creating a row for an asset if the index price fetch fails, if it has no active expiry at all, **or if zero trades match the nearest expiry's since-midnight window** — an all-zero payoffs curve has no real extremum, and writing one anyway would silently store the configured price-range floor (e.g. `dankbit.from_price`) as if it were real data.
  - `backfill(start=None, days=10, interval_hours=4)` — wipes all existing rows and recomputes history at a fixed cadence. There's no historical index-price API, so each tick's `index_price` is approximated from that asset's own trade data (the closest trade's `index_price` field at-or-before the tick). If `start` is omitted, ticks anchor to *now* and walk backwards `days` days — deliberately not calendar-snapped, since a tick landing exactly on 00:00 UTC makes the since-midnight window collapse to zero width (both bounds equal midnight), which the model now simply skips rather than storing a misleading snapshot. When `start` is given, ticks walk forward from it every `interval_hours` up to now. To align with the real 4h candle-bucket grid (`klines_proxy` buckets by raw epoch time, always landing on 00:00/04:00/08:00/12:00/16:00/20:00 UTC), pick a `start` on that same UTC grid — which, conveniently, is exactly what Berlin-labeled 02:00/06:00/10:00/14:00/18:00/22:00 maps to whenever Berlin is UTC+2 (CEST).
- `res.config.settings` extension — Chart price ranges, refresh interval, Deribit cache TTL, weekly/monthly expiries for BTC and ETH.

**Controllers (`main.py`):**
- PNG routes: `/<instrument>`, `/<instrument>/<hours>`, `/<instrument>/D<days>`, `/i/<instrument>`, `/<instrument>/s` (slideshow of hours_list `[0, 4, 8, 12, 24]`), `/<asset>/weekly`, `/<asset>/monthly` (bookmarkable shortcuts — read `weekly_expiry`/`monthly_expiry` (or `eth_*` for ETH) from settings and delegate straight into `chart_png_until`, so they always render identically to `/i/<asset>-<expiry>` for whatever expiry is currently configured; 404 for assets other than BTC/ETH), `/<instrument>/zones` (Longs-vs-Shorts payoff zones for that instrument's trades since 00:00 UTC — see below)
- JSON API: `/api/delta-zero/<instrument>`, `/api/delta-zero-all/<asset>`, `/api/gamma-levels/<instrument>`, `/api/klines/<asset>`, `/api/quadrant-gamma/<asset>`, `/api/zones-extrema/<asset>` (30-day lookback of `dankbit.zones.extrema` rows, one entry per snapshot with `t`, `index_price`, `short_max_price`, `long_min_price`)
- TradingView page: `/chart/<asset>` (reads weekly expiry from settings, returns 404 for unconfigured assets)
- Key helpers:
  - `find_gamma_peaks(STs, gamma_curve, min_fraction)` — local maxima of the gamma curve
  - `find_gamma_bottoms(STs, gamma_curve, min_fraction)` — local minima of the gamma curve
- `delta.py` / `gamma.py` — Pure Black-Scholes functions. Dollar gamma is `Γ × S²`. No side effects.
- `options.py` — `OptionStrat` class. Accumulates legs, then `plot()` returns a matplotlib `Figure`; `plot_zones(longs_curve, shorts_curve, index_price, title)` renders the Longs/Shorts zones chart from two separately-accumulated `OptionStrat` instances. Uses Agg backend; do not import `pyplot` here.

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
- Every 4 hours: `compute_snapshot()` (on `dankbit.zones.extrema`) — snapshots `short_max_price`/`long_min_price` for BTC and ETH from trades since 00:00 UTC

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

Query params for PNG routes: `from_price`, `to_price` (price range), `width`, `height` (figure inches).

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
