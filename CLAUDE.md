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

**TradingView chart:** `/chart/BTC` and `/chart/ETH` serve a Lightweight Charts v4 page with live Deribit perpetual futures candles (proxied server-side) and price lines refreshed on `dankbit.refresh_interval`:
- Delta=0 lines (7 sets) — each a single `createPriceLine`, `lineWidth: 1`, `lineStyle: Solid`. Color is by crossing type, not by set: **black** ("S/D" in the legend) for "supply" (delta negative below, positive above), **red** ("Tie-out" in the legend) for "demand" (delta positive below, negative above — the important case). Set identity is shown via the line's `title` only. Every `createPriceLine` call across the chart (delta=0 sets, gamma peak/bottom, ruler) sets `axisLabelColor: 'black'` so the axis label box is always black regardless of the line's own color — only the label background is forced black, not the line stroke. `addLineSeries` (Net Call/Put Gamma) has no equivalent per-series label-color override in Lightweight Charts v4, so those labels still match their line color (blue/orange):
  - **Daily 24H** — nearest active expiry, last 24 h of trades; sourced from `/api/delta-zero-daily/<asset>`
  - **Daily+1 24H** — second nearest active expiry, last 24 h of trades; sourced from `/api/delta-zero-daily2/<asset>`
  - **Daily** (midpoint titled `Middle Daily`) — the expiry landing exactly 1 calendar day from now (Deribit daily options settle 08:00 UTC), using *all* trades for that expiry (no 24h filter); sourced from `/api/delta-zero-tomorrow/<asset>`
  - **Daily+1** (midpoint titled `Middle Daily+1`) — same as the `Daily` set but 2 calendar days ahead; sourced from `/api/delta-zero-day-after-tomorrow/<asset>`
  - Note: **Daily 24H**'s midpoint is titled `Middle Daily 24H` and **Daily+1 24H**'s is `Middle Daily+1 24H` (not the shorter `Middle Daily`/`Middle Daily+1` used above) — kept distinct from the `Daily`/`Daily+1` sets' midpoints
  - **`Daily`/`Daily+1` vs `Daily 24H`/`Daily+1 24H` are not the same expiry selection**, even though they usually land on the same dates: the `24H` sets track nearest/second-nearest *active* (not-yet-settled) expiry, which rolls forward at each expiry's 08:00 UTC settlement — so in the pre-settlement window each day, `Daily 24H` still points at *today's* expiry while `Daily` already points at *tomorrow's* (and likewise `Daily+1 24H` at tomorrow's vs `Daily+1` at the day after). They only coincide during the ~22h/day after today's settlement and before tomorrow's.
  - **Weekly** — sourced from `/api/delta-zero/<instrument>`
  - **Monthly** — sourced from `/api/delta-zero/<monthly-instrument>`
  - **All** — all active expiries; sourced from `/api/delta-zero-all/<asset>`
  - Midpoint lines, black `LargeDashed`, `lineWidth: 1`: with exactly 2 crossings, a single unnumbered `Middle <Set>`; with N>2 crossings, N-1 lines between each consecutive sorted pair, titled `Middle <Set> 1`, `Middle <Set> 2`, ... (0 or 1 crossings draw none)
- Gamma peak/bottom lines: Weekly (lineWidth 2) and Monthly (lineWidth 1) — violet, sourced from `/api/gamma-levels/<instrument>`; title suffix is `∧` for peaks and `∨` for bottoms (e.g. `Weekly ∧`, `Monthly ∨`)
- Quadrant Gamma lines (2) — `Net Call Gamma` (`#1565c0`) and `Net Put Gamma` (`#e65100`), `addLineSeries` on their own `priceScaleId: 'qg'`, confined to a bottom panel via `scaleMargins: { top: 0.8, bottom: 0 }` — a separate scale from candles, so the axis label shows the real (unrescaled) gamma value, and dragging/zooming the main candle price axis doesn't affect this panel (or vice versa); the time axis is always shared regardless, so horizontal scroll/pan stays in sync. Real values are also in `/api/quadrant-gamma/<asset>`. The endpoint truncates each `computed_at` to the hour (minutes/seconds zeroed) before building `"t"`, and collapses any same-hour duplicates to the latest row — the stored `computed_at` values themselves are left untouched, only the API response is rounded. Only shown on the **1h** timeframe — `refreshQuadrantGamma()` clears both series (`setData([])`) when `INTERVAL !== '1h'`, and `setTf()` calls it on every timeframe switch. Net = buyer + seller per quadrant (seller is already negative, per `portfolio_gamma`'s direction sign) — no backfill, starts empty and grows one point per hour as `dankbit.quadrant.gamma`'s cron runs
- Footer legend (`#dz-legend`, fixed below the status line): black "S/D", red "Tie-out", violet "Gamma Extrema", blue "Net Call Gamma", orange "Net Put Gamma"
- Footer status shows: `<daily-expiry>  ·  <weekly-expiry>  ·  <monthly-expiry>  ·  N trades  ·  HH:MM:SS`; trade count = all active trades up to and including monthly expiry (from monthly endpoint)

## Odoo Addon (`my_addons/dankbit/`)

The addon depends only on `website`. Key components:

**Models:**
- `dankbit.trade` — Core model. Fields map directly to Deribit trade fields. `strike` and `option_type` are computed from `name` (instrument name like `BTC-29NOV24-98000-P`). `days_to_expiry` is UTC-safe. `get_hours_to_expiry()` returns continuous time used in Black-Scholes.
- `dankbit.quadrant.gamma` — Hourly snapshot of dollar gamma (Γ × S²) split into 4 quadrants (buyer/seller × call/put) per asset, computed from the trailing 24h of active-expiry trades at the current index price. `compute_snapshot()` is the cron entry point (BTC then ETH, one row per asset per run); skips creating a row for an asset if the index price fetch fails, rather than persisting a misleading zero.
- `res.config.settings` extension — Chart price ranges, refresh interval, Deribit cache TTL, weekly/monthly expiries for BTC and ETH.

**Controllers (`main.py`):**
- PNG routes: `/<instrument>`, `/<instrument>/<hours>`, `/<instrument>/D<days>`, `/i/<instrument>`, `/<instrument>/s` (slideshow of hours_list `[0, 4, 8, 12, 24]`)
- JSON API: `/api/delta-zero/<instrument>`, `/api/delta-zero-all/<asset>`, `/api/delta-zero-daily/<asset>`, `/api/delta-zero-daily2/<asset>`, `/api/delta-zero-tomorrow/<asset>`, `/api/delta-zero-day-after-tomorrow/<asset>`, `/api/gamma-levels/<instrument>`, `/api/klines/<asset>`, `/api/quadrant-gamma/<asset>`
  - `_delta_zero_for_calendar_day(asset, days_ahead)` — shared helper behind the tomorrow/day-after-tomorrow endpoints; targets the expiry at exactly `now + days_ahead` days (08:00 UTC), exact-match on `expiration`, no trade-time filter
- TradingView page: `/chart/<asset>` (reads weekly expiry from settings, returns 404 for unconfigured assets)
- Key helpers:
  - `find_gamma_peaks(STs, gamma_curve, min_fraction)` — local maxima of the gamma curve
  - `find_gamma_bottoms(STs, gamma_curve, min_fraction)` — local minima of the gamma curve
- `delta.py` / `gamma.py` — Pure Black-Scholes functions. Dollar gamma is `Γ × S²`. No side effects.
- `options.py` — `OptionStrat` class. Accumulates legs, then `plot()` returns a matplotlib `Figure`. Uses Agg backend; do not import `pyplot` here.

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

**Caching:** `trade.py` uses a module-level `_DERIBIT_CACHE` dict (key → `{ts, value}`) for Deribit index price and instrument lookups. TTL is configurable via settings (`deribit_cache_ttl`, default 300s).

**Backend menu:** `Dankbit` root menu (`trade_views.xml`) with two items — `Trades` (`dankbit.trade` list/form) and `Quadrant Gamma` (`quadrant_gamma_views.xml`, `dankbit.quadrant.gamma` list/form/search with BTC/ETH filters).

## WebSocket Service (`dankbit_ws_service/`)

Single file: `dankbit_ws_batch.py`. Authenticates with `DERIBIT_KEY`/`DERIBIT_SECRET` env vars using `client_credentials` grant before fetching instruments. Reconnects every 3 seconds on any failure. Rate-limit (`over_limit`) errors trigger a 0.5s sleep and retry.

The DB connection uses `autocommit=True` — no explicit transaction management needed.

## URL Reference

| URL | Description |
|-----|-------------|
| `/BTC-<EXPIRY>` | BTC PNG chart for a specific expiry (e.g. `/BTC-25JUL26`) |
| `/ETH-<EXPIRY>` | ETH PNG chart for a specific expiry |
| `/i/<EXPIRY>` | PNG chart for all expiries up to selected date; includes delta=0 line |
| `/chart/BTC` | Live TradingView chart; reads weekly expiry from settings |
| `/chart/ETH` | Live TradingView chart for ETH |
| `/help` | Payoff diagram reference page |

Query params for PNG routes: `from_price`, `to_price` (price range), `width`, `height` (figure inches).

## TradingView Chart Notes

- Candles sourced from Deribit perpetual futures (`BTC-PERPETUAL`, `ETH-PERPETUAL`) via `/api/klines/<asset>` proxy (avoids CORS). Uses `get_tradingview_chart_data` endpoint. Deribit returns oldest-first parallel arrays (`ticks`, `open`, `high`, `low`, `close`); proxy zips and reverses to newest-first `{t,o,h,l,c}` format (t in ms).
- Candles refresh every 5 seconds; delta=0 lines refresh on `dankbit.refresh_interval`
- Berlin timezone applied via `berlinOffset` computed from `Intl` API
- Timeframe buttons: 1m / 1h (default) — visible windows: 1m=1d, 1h=30d; Deribit resolution: 1m→1, 1h→60
- Ruler tool: click twice on chart to measure price-to-price percentage
- Do **not** change `title:` values in `createPriceLine()` calls — the user maintains these manually

## Odoo Gotchas

- **Manifest licence field:** Odoo only accepts a fixed set of strings. Use `"Other OSI approved licence"` (British spelling) for MIT — `"MIT"` alone will fail validation.

## Odoo Module Reload

Python changes to `my_addons/` take effect after `docker compose restart web`. XML/view changes and new model fields require upgrading the module:

```bash
docker compose exec web odoo -d <db_name> -u dankbit --stop-after-init
docker compose restart web
```
