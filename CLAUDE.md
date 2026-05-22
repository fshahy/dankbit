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
                                               /BTC, /ETH routes
                                               (PNG chart responses)
```

**Trade data enters two ways:**
1. **WebSocket (primary):** `dankbit_ws_service/dankbit_ws_batch.py` — connects to Deribit, authenticates, fetches all BTC+ETH option instruments, subscribes to `trades.<instrument>.raw` channels in chunks of 400, inserts rows directly into PostgreSQL with `ON CONFLICT IGNORE` on `deribit_trade_identifier`.
2. **REST backfill:** `Trade.get_last_trades()` — runs daily via cron; ensures no trades were missed by paging through the last few days of Deribit REST API history.

**Chart rendering:** Per-expiry routes like `/BTC-7MAY26` query all active trades for that expiry, compute a Black-Scholes portfolio delta and dollar-gamma (GEX = Γ × S²) curve, and return a PNG rendered with matplotlib's Agg backend (`Figure + FigureCanvas`, never `pyplot` — server-safe). `/BTC` and `/ETH` without an expiry return 404.

## Odoo Addon (`my_addons/dankbit/`)

The addon depends only on `website`. Key components:

**Models:**
- `dankbit.trade` — Core model. Fields map directly to Deribit trade fields. `strike` and `option_type` are computed from `name` (instrument name like `BTC-29NOV24-98000-P`). `days_to_expiry` is UTC-safe. `get_hours_to_expiry()` returns continuous time used in Black-Scholes.
- `res.config.settings` extension — Chart price ranges, refresh interval, Black-Scholes floor (`greeks_min_time_hours`), and Deribit cache TTL.

**Controllers:**
- `main.py` — Route handler for `/<instrument>` (e.g. `/BTC-7MAY26`, `/ETH-30MAY26`). Rejects bare `/BTC` and `/ETH` with 404. Builds `OptionStrat`, calls delta/gamma aggregators, returns PNG.
- `delta.py` / `gamma.py` — Pure Black-Scholes functions. Dollar gamma is `Γ × S²`. No side effects.
- `options.py` — `OptionStrat` class. Accumulates legs, then `plot()` returns a matplotlib `Figure`. Uses Agg backend; do not import `pyplot` here.

**Scheduled crons (data/ir_cron.xml):**
- Daily: `get_last_trades()` — active by default; ensures no trades were missed (e.g. during brief downtime)
- Daily: `_delete_expired_trades()` — active by default; archives expired trades

**Caching:** `trade.py` uses a module-level `_DERIBIT_CACHE` dict (key → `{ts, value}`) for Deribit index price and instrument lookups. TTL is configurable via settings (`deribit_cache_ttl`, default 300s).

## WebSocket Service (`dankbit_ws_service/`)

Single file: `dankbit_ws_batch.py`. Authenticates with `DERIBIT_KEY`/`DERIBIT_SECRET` env vars using `client_credentials` grant before fetching instruments. Reconnects every 3 seconds on any failure. Rate-limit (`over_limit`) errors trigger a 0.5s sleep and retry.

The DB connection uses `autocommit=True` — no explicit transaction management needed.

## URL Reference

| URL | Description |
|-----|-------------|
| `/BTC-7MAY26` | BTC options for a specific expiry |
| `/ETH-30MAY26` | ETH options for a specific expiry |
| `/help` | Payoff diagram reference page |

Always use a specific expiry in the URL. `/BTC` and `/ETH` (without expiry) are technically supported but should not be used — they aggregate all trades across all expiries, which is a very large dataset and produces a meaningless chart.

Query params: `from_price`, `to_price` (price range), `width`, `height` (figure inches).

## Odoo Gotchas

- **Manifest licence field:** Odoo only accepts a fixed set of strings. Use `"Other OSI approved licence"` (British spelling) for MIT — `"MIT"` alone will fail validation.

## Odoo Module Reload

Python changes to `my_addons/` take effect after `docker compose restart web`. XML/view changes require upgrading the module in Odoo UI (Settings → Activate developer mode → Apps → Dankbit → Upgrade) or via:

```bash
docker compose exec web odoo -d <db_name> -u dankbit --stop-after-init
```
