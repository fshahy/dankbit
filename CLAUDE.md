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
The `UID` and `GID` env vars are also needed so the `web` container writes files as the host user.

> **Production note:** `config/odoo.conf` contains `dbfilter = ^db1$` — update this to match the actual database name.

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
2. **REST backfill:** `Trade.get_last_trades()` — runs daily via cron; ensures no trades were missed by paging through the last few days of Deribit REST API history.

**PNG chart rendering:** Per-expiry routes like `/BTC-7MAY26` aggregate all trades for that expiry, compute a Black-Scholes portfolio delta and dollar-gamma (GEX = Γ × S²) curve, and return a PNG via matplotlib's Agg backend (`Figure + FigureCanvas`, never `pyplot` — server-safe). Overlaid lines: gamma peaks (dashed black), gamma=0 crossings (solid black). The `/i/<instrument>` route additionally overlays a delta=0 line (green).

**TradingView chart:** `/chart/BTC` and `/chart/ETH` serve a Lightweight Charts v4 page with live XT.com perpetual futures candles (proxied server-side) and price lines refreshed on `dankbit.refresh_interval`:
- Delta=0 lines (4 sets) — each a single `createPriceLine`, `lineWidth: 1`, `lineStyle: Solid`:
  - **Daily 24H** (orange) — nearest active expiry, last 24 h of trades; sourced from `/api/delta-zero-daily/<asset>`
  - **Weekly** (green) — sourced from `/api/delta-zero/<instrument>`
  - **Monthly** (blue) — sourced from `/api/delta-zero/<monthly-instrument>`
  - **All** (black) — all active expiries; sourced from `/api/delta-zero-all/<asset>`
  - Midpoint line when exactly 2 crossings: black `LargeDashed`, `lineWidth: 1`, title `Middle <Set>`
- Gamma peak/bottom lines: Weekly (lineWidth 2) and Monthly (lineWidth 1) — violet, sourced from `/api/gamma-levels/<instrument>`
- Footer shows: `<daily-expiry>  ·  <weekly-expiry>  ·  <monthly-expiry>  ·  N trades  ·  HH:MM:SS`

## Odoo Addon (`my_addons/dankbit/`)

The addon depends only on `website`. Key components:

**Models:**
- `dankbit.trade` — Core model. Fields map directly to Deribit trade fields. `strike` and `option_type` are computed from `name` (instrument name like `BTC-29NOV24-98000-P`). `days_to_expiry` is UTC-safe. `get_hours_to_expiry()` returns continuous time used in Black-Scholes.
- `res.config.settings` extension — Chart price ranges, refresh interval, Deribit cache TTL, weekly/monthly expiries for BTC and ETH.

**Controllers (`main.py`):**
- PNG routes: `/<instrument>`, `/<instrument>/<hours>`, `/<instrument>/D<days>`, `/i/<instrument>`
- JSON API: `/api/delta-zero/<instrument>`, `/api/delta-zero-all/<asset>`, `/api/delta-zero-daily/<asset>`, `/api/gamma-levels/<instrument>`, `/api/klines/<asset>`
- TradingView page: `/chart/<asset>` (reads weekly expiry from settings, returns 404 for unconfigured assets)
- Key helpers:
  - `find_gamma_peaks(STs, gamma_curve, min_fraction)` — local maxima of the gamma curve
  - `find_gamma_bottoms(STs, gamma_curve, min_fraction)` — local minima of the gamma curve
  - `find_gamma_zero_crossings(STs, gamma_curve)` — linear interpolation for gamma=0 crossings
- `delta.py` / `gamma.py` — Pure Black-Scholes functions. Dollar gamma is `Γ × S²`. No side effects.
- `options.py` — `OptionStrat` class. Accumulates legs, then `plot()` returns a matplotlib `Figure`. Uses Agg backend; do not import `pyplot` here.

**Settings fields (`res.config.settings`):**
- `from_price`, `to_price`, `steps` — BTC chart price range
- `eth_from_price`, `eth_to_price`, `eth_steps` — ETH chart price range
- `refresh_interval` — page auto-refresh interval (seconds)
- `deribit_timeout`, `deribit_cache_ttl` — Deribit API behaviour
- `weekly_expiry`, `monthly_expiry` — BTC expiry strings (e.g. `BTC-4JUL26`)
- `eth_weekly_expiry`, `eth_monthly_expiry` — ETH expiry strings (e.g. `ETH-4JUL26`)

**Scheduled crons (data/ir_cron.xml):**
- Daily: `get_last_trades()` — ensures no trades were missed (e.g. during brief downtime)
- Daily: `_delete_expired_trades()` — archives expired trades

**Caching:** `trade.py` uses a module-level `_DERIBIT_CACHE` dict (key → `{ts, value}`) for Deribit index price and instrument lookups. TTL is configurable via settings (`deribit_cache_ttl`, default 300s).

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

- Candles sourced from XT.com perpetual futures via `/api/klines/<asset>` proxy (avoids CORS)
- Candles refresh every 5 seconds; delta=0 lines refresh on `dankbit.refresh_interval`
- Berlin timezone applied via `berlinOffset` computed from `Intl` API
- Timeframe buttons: 5m / 1h / 4h — each sets a per-timeframe visible window via `applyVisibleRange()` (windows: 5m=7d, 1h=30d, 4h=21d)
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
