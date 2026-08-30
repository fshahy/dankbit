# -*- coding: utf-8 -*-

import random
from datetime import datetime, timedelta, timezone
import logging
import requests, time as time_module

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Simple in-memory cache to avoid hitting Deribit too often.
# Keys: 'index_price', 'instruments', optionally others.
_DERIBIT_CACHE = {
    "index_price": {"ts": 0, "value": None},
    "instruments": {"ts": 0, "value": None},
}

# Separate cache for Binance candles (get_candles) — short fixed TTL rather
# than the configurable dankbit.deribit_cache_ttl, since this isn't a
# Deribit call at all; see get_candles's own docstring for why a cache is
# needed here (every open TradingView chart tab polls this every 5s).
_BINANCE_CACHE = {}
_BINANCE_CANDLES_CACHE_TTL = 5.0

# Separate cache for Coinbase spot candles (get_candles_coinbase) — same
# short-fixed-TTL reasoning as _BINANCE_CACHE above, just a distinct
# dict/key-space since this is a different upstream API/venue than either
# Binance (get_candles) or the Deribit index calls cached in _DERIBIT_CACHE.
_COINBASE_CACHE = {}
_COINBASE_CANDLES_CACHE_TTL = 5.0

# get_last_trades()'s recently-expired grace window (see
# _get_recently_expired_instruments) — both this REST cron and the WS
# ingestion service (dankbit_ws_batch.py) only ever discover instruments
# via Deribit's "expired: false" filter, so without this an instrument
# that expires between two polls permanently drops out of both
# ingestion paths, silently losing any trades that landed between the
# last poll before expiry and the actual expiration moment.
#
# get_last_trades() itself only runs nightly (see data/ir_cron.xml) — the
# WS service is the real-time primary, this cron is purely the "make sure
# we didn't miss anything, e.g. because the server was down" backstop —
# so the gap between two runs is ~24h under normal operation, not ~1
# minute. 3 days comfortably covers that plus a day or two of the server
# actually being down (the scenario this cron exists for in the first
# place). get_last_trades() re-fetches each tracked instrument's full
# history every run (see its own docstring), so tracking an
# already-fully-caught-up expired instrument for the rest of this window
# still costs one bounded REST fetch per night, not zero — just cheap,
# since it settles into a single near-empty page once every trade is
# already in the DB.
RECENTLY_EXPIRED_GRACE_MINUTES = 3 * 24 * 60

def _safe_binance_request(
    url,
    params,
    timeout=10.0,
    retries=3,
    backoff=0.4,
    raise_on_fail=False,
):
    """Same robust GET-with-retries/backoff shape as _safe_deribit_request
    below, for Binance's public REST API instead — Binance signals errors
    via a JSON {"code": ..., "msg": ...} envelope (sometimes alongside a
    non-2xx status, sometimes not, e.g. rate limiting), so the body is
    checked even on an otherwise-200 response, same reasoning as Deribit's
    own {"error": ...} envelope check."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "code" in data and "msg" in data:
                raise RuntimeError(f"Binance error: {data}")
            return data
        except Exception as e:
            _logger.warning(
                "Binance request failed (%d/%d) %s params=%s error=%s",
                attempt, retries, url, params, e
            )
            if attempt < retries:
                time_module.sleep(backoff * (2 ** (attempt - 1)))
            else:
                if raise_on_fail:
                    raise
                return None

def _safe_coinbase_request(
    url,
    params,
    timeout=10.0,
    retries=3,
    backoff=0.4,
    raise_on_fail=False,
):
    """Same robust GET-with-retries/backoff shape as _safe_binance_request/
    _safe_deribit_request above, for Coinbase Exchange's public REST API.
    Coinbase signals errors via a normal non-2xx status (caught by
    raise_for_status()) with a JSON {"message": ...} body — checked here too
    as defense-in-depth, same reasoning as the other two helpers' own
    envelope checks."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "message" in data:
                raise RuntimeError(f"Coinbase error: {data}")
            return data
        except Exception as e:
            _logger.warning(
                "Coinbase request failed (%d/%d) %s params=%s error=%s",
                attempt, retries, url, params, e
            )
            if attempt < retries:
                time_module.sleep(backoff * (2 ** (attempt - 1)))
            else:
                if raise_on_fail:
                    raise
                return None

def _safe_deribit_request(
    url,
    params,
    timeout=5.0,
    retries=3,
    backoff=0.4,
    raise_on_fail=False,
):
    """
    Robust GET with retries and exponential backoff.
    Returns parsed JSON dict on success.
    Returns None on failure (network error, non-2xx status, or a Deribit-
    level {"error": ...} envelope in an otherwise-200 response — e.g. rate
    limiting, which Deribit signals inside the JSON body rather than via
    HTTP status, so resp.raise_for_status() alone can't catch it) unless
    raise_on_fail=True.
    """

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"Deribit error: {data['error']}")
            return data
        except Exception as e:
            _logger.warning(
                "Deribit request failed (%d/%d) %s params=%s error=%s",
                attempt, retries, url, params, e
            )
            if attempt < retries:
                time_module.sleep(backoff * (2 ** (attempt - 1)))
            else:
                if raise_on_fail:
                    raise
                return None

class Trade(models.Model):
    _name = "dankbit.trade"
    _order = "deribit_ts desc"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    strike = fields.Integer(compute="_compute_strike", store=True)
    expiration = fields.Datetime()
    index_price = fields.Float(digits=(16, 4))
    price = fields.Float(digits=(16, 4), required=True)
    mark_price = fields.Float(digits=(16, 4))
    option_type = fields.Text(compute="_compute_type", store=True)
    direction = fields.Selection([("buy", "Buy"), ("sell", "Sell")], required=True)
    iv = fields.Float(string="IV %", digits=(8, 4), required=True)
    amount = fields.Float(digits=(6, 2), required=True)
    deribit_ts = fields.Datetime()
    deribit_trade_identifier = fields.Char(string="Deribit Trade ID", required=True)
    trade_seq = fields.Float(digits=(15, 0))
    days_to_expiry = fields.Integer(
        string="Days to Expiry",
        compute="_compute_days_to_expiry"
    )
    block_trade_id = fields.Char(
        string="Block Trade ID",
        help="Deribit-assigned ID if this trade was executed as a block trade."
    )
    is_block_trade = fields.Boolean(
        string="Is Block Trade",
        default=False,
        help="True if this trade came from a Deribit block trade event."
    )

    def get_hours_to_expiry(self):
        """
        Continuous time to expiry in hours (UTC-safe).
        Used ONLY for greeks, not UI logic.
        """
        if not self.expiration:
            return 0.0

        now = datetime.now(timezone.utc)

        exp = self.expiration
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)

        seconds = (exp - now).total_seconds()
        return max(seconds / 3600.0, 0.0)

    @api.depends("expiration")
    def _compute_days_to_expiry(self):
        """Compute remaining days until expiration from current UTC date."""
        now = datetime.now(timezone.utc)
        today = now.date()
        for rec in self:
            if rec.expiration:
                expiry_date = rec.expiration.astimezone(timezone.utc).date()
                rec.days_to_expiry = (expiry_date - today).days
            else:
                rec.days_to_expiry = 0

    _sql_constraints = [
        ("deribit_trade_identifier_uniqe", "unique (deribit_trade_identifier)",
         "The Deribit trade ID must be unique!")
    ]

    @api.depends("name")
    def _compute_type(self):
        for rec in self:
            if rec.name:
                if rec.name[-1] == "P":
                    rec.option_type = "put"
                elif rec.name[-1] == "C":
                    rec.option_type = "call"
                else:
                    rec.option_type = False
            else:
                rec.option_type = False

    @api.depends("name")
    def _compute_strike(self):
        for rec in self:
            try:
                # Deribit format: BTC-29NOV24-98000-P
                rec.strike = int(str(rec.name).split("-")[2]) if rec.name else 0
            except Exception:
                rec.strike = 0

    def get_index_price(self, instrument):
        """The live "current price" reference used for chart marker lines
        (the blue `axvline` on PNG/zones charts), dankbit.bands's `role_close`
        (Smart Liquidity pin/sweep flip detection), and the Thales Forecast
        engine's `projected_open`/ATR-fallback — NOT for the Greek curves
        themselves (delta.portfolio_delta/gamma.portfolio_gamma/etc. evaluate
        across the configured STs price grid using each trade's own strike/
        IV/expiry; this value never enters that Black-Scholes math as `S`,
        confirmed across every call site before switching this off Deribit —
        see chat history). Moved to Binance spot (`/api/v3/ticker/price`) per
        product decision, same as get_candles() — keeps this consistent with
        the forecast engine's own `last_close`/`last_open` (now also Binance-
        sourced via get_candles(), see there), which simulate_forecast()
        compares `projected_open`/this value against extensively. The one
        exception where this value *does* feed Black-Scholes directly:
        forecast.trade_weighted_per_leg_greeks() (behind the off-by-default
        forecast_trade_weighted_greeks toggle) evaluates each trade's
        per-contract greek at this price, mirroring Thales's own live-order-
        book-at-current-price design — intentional, not a bug, if that
        toggle is ever enabled."""
        currency = "BTC" if instrument.startswith("BTC") else "ETH" if instrument.startswith("ETH") else None
        if not currency:
            return 0.0
        symbol = self._BINANCE_SYMBOL_MAP.get(currency, currency + "USDT")

        # read timeout/cache TTL from the same settings get_candles() and the
        # old Deribit path both already use — kept as-is (not renamed to
        # "binance_*") to avoid a settings migration for what's still just
        # "how long to trust a cached public-API price".
        timeout = 5.0
        cache_ttl = 30.0
        try:
            icp = self.env["ir.config_parameter"]
            timeout = float(icp.get_param("dankbit.deribit_timeout", default=5.0))
            cache_ttl = float(icp.get_param("dankbit.deribit_cache_ttl", default=30.0))
        except Exception:
            pass

        # consult cache first — keyed by currency to avoid BTC/ETH collision;
        # _BINANCE_CACHE (not _DERIBIT_CACHE) since this is no longer a
        # Deribit call.
        cache_key = f"index_price_{currency}"
        now_ts = time_module.time()
        cached = _BINANCE_CACHE.get(cache_key, {})
        if cached and cached.get("value") is not None and (now_ts - cached.get("ts", 0) < cache_ttl):
            return cached.get("value")

        url = "https://api.binance.com/api/v3/ticker/price"
        data = _safe_binance_request(url, params={"symbol": symbol}, timeout=timeout)
        if data and isinstance(data, dict) and data.get("price") is not None:
            val = float(data["price"])
            _BINANCE_CACHE[cache_key] = {"ts": now_ts, "value": val}
            return val
        else:
            if cached and cached.get("value") is not None:
                _logger.warning("get_index_price: using stale cached value")
                return cached.get("value")
            _logger.exception("get_index_price failed and no cache available")
            return 0.0

    def get_open_interest_by_currency(self, asset):
        """Current open interest (contracts outstanding) for every option
        instrument on `asset`, from Deribit's public get_book_summary_by_currency
        — one bulk call per currency, no auth needed, unlike per-instrument
        ticker lookups. Used by ChartController._gamma_by_strike to cap the
        net position it derives from cumulative signed trade flow, so
        historical round-tripped volume at a strike can't imply a larger
        position than what's actually outstanding right now. Cached the
        same way get_index_price is (_DERIBIT_CACHE, dankbit.deribit_cache_ttl),
        keyed by currency. Returns {instrument_name: open_interest} — empty
        dict for an unknown asset or on total failure with no cache."""
        currency = "BTC" if asset.upper().startswith("BTC") else "ETH" if asset.upper().startswith("ETH") else None
        if not currency:
            return {}

        URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
        params = {"currency": currency, "kind": "option"}

        timeout = 5.0
        cache_ttl = 30.0
        try:
            icp = self.env["ir.config_parameter"]
            timeout = float(icp.get_param("dankbit.deribit_timeout", default=5.0))
            cache_ttl = float(icp.get_param("dankbit.deribit_cache_ttl", default=30.0))
        except Exception:
            pass

        cache_key = f"open_interest_{currency}"
        now_ts = time_module.time()
        cached = _DERIBIT_CACHE.get(cache_key, {})
        if cached and cached.get("value") is not None and (now_ts - cached.get("ts", 0) < cache_ttl):
            return cached.get("value")

        data = _safe_deribit_request(URL, params=params, timeout=timeout)
        if data and isinstance(data, dict):
            result = data.get("result", []) or []
            val = {row["instrument_name"]: float(row.get("open_interest") or 0.0) for row in result}
            _DERIBIT_CACHE[cache_key] = {"ts": now_ts, "value": val}
            return val
        else:
            if cached and cached.get("value") is not None:
                _logger.warning("get_open_interest_by_currency: using stale cached value")
                return cached.get("value")
            _logger.exception("get_open_interest_by_currency failed and no cache available")
            return {}

    _BINANCE_SYMBOL_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

    def get_candles(self, asset, interval="4h", limit=500):
        """Real Binance SPOT candles, oldest-first — shared by
        ChartController.klines_proxy (which reverses to newest-first for the
        frontend, i.e. every TradingView chart's actual candlestick series)
        and dankbit.forecast.snapshot.get_forecast_points (which needs real
        historical bars for the Thales Forecast engine's ATR/momentum/
        liquidity-sweep detection, see controllers/forecast.py) — moved off
        Deribit perpetual futures to Binance spot per product decision.
        This is the only thing that changed: the options Greeks/curves/
        bands themselves stay entirely Deribit-based (see get_index_price,
        get_open_interest_by_currency), since those are priced/settled
        against Deribit's own index — only the OHLC price-action reference
        drawn as the chart's candles (and read by the forecast engine's ATR/
        momentum context, since both share this one function) moved.

        Binance's REST klines endpoint has native 1h/4h/1d resolutions
        (unlike Deribit, which had no native 4h and needed this function to
        fetch 1h bars and aggregate every 4 into one) and returns exactly
        the most recent `limit` bars ending "now" with no explicit
        start/end window needed — no aggregation or window math required
        here at all. Cached (_BINANCE_CACHE, a short fixed
        _BINANCE_CANDLES_CACHE_TTL, deliberately not the configurable
        dankbit.deribit_cache_ttl since this isn't a Deribit call) since
        this gets polled every 5s per open chart tab (see
        dankbit_templates.xml's updateLatestCandle), so concurrent viewers
        of the same asset/interval within that window share one upstream
        call rather than each hitting Binance directly. Lives here (rather
        than on the controller) since it's a plain public REST lookup with
        no Odoo/HTTP-request dependency, the same category of call
        get_index_price/get_open_interest_by_currency above already
        handle — letting the forecast-log cron reuse it without needing an
        HTTP request context."""
        symbol = self._BINANCE_SYMBOL_MAP.get(asset.upper(), asset.upper() + "USDT")

        cache_key = f"candles_{symbol}_{interval}_{limit}"
        now_ts = time_module.time()
        cached = _BINANCE_CACHE.get(cache_key, {})
        if cached and cached.get("value") is not None and (now_ts - cached.get("ts", 0) < _BINANCE_CANDLES_CACHE_TTL):
            return cached.get("value")

        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        data = _safe_binance_request(url, params=params, timeout=10.0)
        if not data:
            if cached and cached.get("value") is not None:
                _logger.warning("get_candles: using stale cached value for %s", cache_key)
                return cached.get("value")
            _logger.exception("get_candles failed and no cache available for %s", cache_key)
            return []

        # Binance kline row: [openTime, open, high, low, close, volume,
        # closeTime, quoteVolume, trades, takerBuyBase, takerBuyQuote,
        # unused] — oldest-first already, matching this function's own
        # documented return order.
        candles = [
            {"t": int(row[0]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4])}
            for row in data
        ]
        _BINANCE_CACHE[cache_key] = {"ts": now_ts, "value": candles}
        return candles

    @staticmethod
    def _bucket_candles(candles, bucket_seconds):
        """Merge oldest-first {t, o, h, l, c} 1-bar candles into
        bucket_seconds-wide bars. Buckets by UTC epoch boundary
        (candle_t // bucket_ms), not by grouping every run of N bars
        starting from whichever bar happened to be first in the fetch
        window — so a 4h bucket always lands on 00:00/04:00/... UTC
        regardless of the request's own start_timestamp, and a still-
        forming trailing bucket (fewer than N source bars so far) is
        included as a partial bar rather than dropped."""
        buckets = {}
        order = []
        bucket_ms = bucket_seconds * 1000
        for c in candles:
            bucket_start = (c["t"] // bucket_ms) * bucket_ms
            if bucket_start not in buckets:
                buckets[bucket_start] = []
                order.append(bucket_start)
            buckets[bucket_start].append(c)
        return [
            {
                "t": bucket_start,
                "o": buckets[bucket_start][0]["o"],
                "h": max(b["h"] for b in buckets[bucket_start]),
                "l": min(b["l"] for b in buckets[bucket_start]),
                "c": buckets[bucket_start][-1]["c"],
            }
            for bucket_start in order
        ]

    _COINBASE_SYMBOL_MAP = {"BTC": "BTC-USD", "ETH": "ETH-USD"}
    # Coinbase Exchange's native candle granularities (seconds) — no native
    # 4h bucket (same gap Deribit perpetuals had), so "4h" resolves to the
    # native 3600 (1h) granularity here and gets bucketed in Python
    # afterward, same technique as the old Deribit-perpetual path this
    # replaced. 5m/15m/1h/1d map directly onto Coinbase's own
    # 300/900/3600/86400. (5m added for /sli/<asset>'s own Timeframe
    # dropdown — no other page requests it.)
    _COINBASE_GRANULARITY_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 3600, "1d": 86400}
    _COINBASE_MAX_CANDLES_PER_REQUEST = 300

    def get_candles_coinbase(self, asset, interval="4h", limit=500, as_of_ts=None):
        """Real Coinbase Exchange spot candles (BTC-USD/ETH-USD), oldest-
        first, same {t, o, h, l, c} shape get_candles() returns — used by
        /4l/<asset> (via klines_coinbase_proxy), per
        product decision to move that page off Deribit perpetual futures
        onto Coinbase spot; every other TradingView page in this addon
        (/chart) keeps using get_candles()/Binance spot unchanged. Also
        fixes the daily-timeframe discrepancy that page used to have
        against the Delta Chart: Deribit's own daily bars were bucketed on
        an 08:00 UTC boundary (its option-settlement time), so "today"'s
        bar didn't appear until 08:00 UTC — Coinbase's daily granularity is
        UTC-midnight-anchored, same as Binance's, so this is no longer an
        issue.

        `as_of_ts` (unix seconds, default None) anchors the fetch window's
        right edge — None means "ending now" (every existing caller's
        behavior, unchanged); a real value returns the `limit` bars ending
        at/before that moment instead, i.e. "candles as of a past point in
        time" — used by /tm/<asset> (the Time Machine page, see
        controllers/main.py's time_machine_chart) to show historical
        candles leading up to a user-picked date rather than the live
        present.

        Coinbase's public /products/<id>/candles endpoint caps each
        request at _COINBASE_MAX_CANDLES_PER_REQUEST (300) bars and takes
        an explicit start/end window (unix seconds) rather than a simple
        "give me the most recent N" limit param, so this paginates
        backward in up to-300-bar windows (deduped by timestamp, since a
        window's own start/end edges can overlap the next page's) until
        `fetch_limit` native bars are collected or the exchange returns
        fewer rows than requested (i.e. its own history is exhausted).
        "4h" fetches native 60-minute bars (4x `limit`, so there's enough
        raw history to bucket) and merges them into 4h bars via
        _bucket_candles() — same fetch-1h-then-aggregate step the old
        Deribit-perpetual path used.

        Cached in the separate _COINBASE_CACHE dict (same short fixed
        _COINBASE_CANDLES_CACHE_TTL=5s reasoning as _BINANCE_CANDLES_CACHE_TTL
        — this gets polled every 5s per open /4l or /aa tab), keyed by the
        *requested* interval/limit/as_of_ts (not the native granularity
        actually fetched), so a 4h request and a 1h request never collide
        in the cache despite both hitting Coinbase at granularity=3600, and
        a historical as_of_ts request never collides with (or evicts) the
        live one."""
        product_id = self._COINBASE_SYMBOL_MAP.get(asset.upper(), asset.upper() + "-USD")
        aggregate_4h = interval == "4h"
        granularity = self._COINBASE_GRANULARITY_SECONDS.get(interval, 3600)
        fetch_limit = limit * 4 if aggregate_4h else limit

        now_ts = time_module.time()
        cache_key = f"coinbase_candles_{product_id}_{interval}_{limit}_{int(as_of_ts) if as_of_ts is not None else 'live'}"
        cached = _COINBASE_CACHE.get(cache_key, {})
        if cached and cached.get("value") is not None and (now_ts - cached.get("ts", 0) < _COINBASE_CANDLES_CACHE_TTL):
            return cached.get("value")

        url = f"https://api.exchange.coinbase.com/products/{product_id}/candles"
        by_t = {}
        end_ts = int(as_of_ts) if as_of_ts is not None else int(now_ts)
        remaining = fetch_limit
        while remaining > 0:
            batch = min(remaining, self._COINBASE_MAX_CANDLES_PER_REQUEST)
            start_ts = end_ts - batch * granularity
            params = {"granularity": granularity, "start": start_ts, "end": end_ts}
            data = _safe_coinbase_request(url, params=params, timeout=10.0)
            if not data:
                break
            new_rows = 0
            oldest_t = None
            # Coinbase's candle row is [time, low, high, open, close,
            # volume], newest-first.
            for row in data:
                t = int(row[0])
                oldest_t = t if oldest_t is None else min(oldest_t, t)
                if t not in by_t:
                    by_t[t] = {"t": t * 1000, "o": float(row[3]), "h": float(row[2]), "l": float(row[1]), "c": float(row[4])}
                    new_rows += 1
            if oldest_t is None:
                break
            end_ts = oldest_t - 1  # next page ends just before this page's oldest bar
            remaining -= new_rows
            if len(data) < batch:
                break  # exchange has no more history behind this point

        if not by_t:
            if cached and cached.get("value") is not None:
                _logger.warning("get_candles_coinbase: using stale cached value for %s", cache_key)
                return cached.get("value")
            _logger.exception("get_candles_coinbase failed and no cache available for %s", cache_key)
            return []

        candles = [by_t[t] for t in sorted(by_t)]

        if aggregate_4h:
            candles = self._bucket_candles(candles, 4 * granularity)

        candles = candles[-limit:]
        _COINBASE_CACHE[cache_key] = {"ts": now_ts, "value": candles}
        return candles

    # ========== FETCHING & INGESTION ==========

    # run by scheduled action
    def get_last_trades(self):
        """
        Hardened REST-only backfill importer — run nightly (see
        data/ir_cron.xml), not the primary ingestion path: the WS
        service (dankbit_ws_service/dankbit_ws_batch.py) is real-time and
        primary; this is purely the safety net for whatever it missed,
        e.g. if the server/WS was down for a stretch.
        - Always fetches each instrument's FULL retained trade history
          (start_timestamp=0 through expiration/now), not an incremental
          fetch resumed from the DB's latest known trade — resuming from
          the latest trade only backfills the tail, so a WS outage earlier
          in the day (that later reconnected and kept ingesting) would
          leave that earlier gap permanently unfetched forever, since
          nothing ever looks behind the DB's current latest trade again.
          A full re-fetch is only viable because this cron runs once a
          day, not on a tight polling loop; re-fetching already-known
          trades is cheap/idempotent since _create_new_trade() silently
          drops duplicates via the deribit_trade_identifier unique
          constraint.
        - Uses timestamp-based pagination (Deribit REST's only supported method).
        - Ensures no gaps, no flooding, no duplicate inserts.
        - Gracefully handles Deribit rate-limit, empty responses, and pagination quirks.
        - One instrument's failure can't take down the rest of the run: each
          instrument's fetch is wrapped in its own try/except, so a single
          malformed response/record only skips that instrument for this
          cycle rather than aborting every instrument still left in
          option_instruments (the next cron cycle re-fetches it from
          scratch regardless, since start_ts is always 0).
        - Also covers instruments that expired since the last poll (see
          _get_recently_expired_instruments/RECENTLY_EXPIRED_GRACE_MINUTES):
          _get_instruments() alone only returns currently-active
          ("expired: false") instruments, so without this, any trade that
          landed between the last poll before an instrument's expiry and
          its actual expiration timestamp would never be fetched by
          either this cron or the WS ingestion service (which has the
          same expired=false-only discovery). These are fetched FIRST,
          ahead of the (usually far larger) active-instrument list: Deribit
          itself only keeps a settled instrument's trade history queryable
          via this REST endpoint for a limited time after expiry (observed
          well under RECENTLY_EXPIRED_GRACE_MINUTES's own 3-day window), so
          if this run is what's finally catching one of these instruments
          up, that data can age out of Deribit's own retention while this
          run is still working through thousands of already-current active
          instruments ahead of it — small/urgent goes first, large/routine
          goes second.
        """

        # Keyed by instrument_name to dedupe: an instrument can in
        # principle appear in both lists in the same tick (its "active"
        # cache entry can be up to deribit_cache_ttl stale), so a plain
        # concatenation could fetch it twice this run — harmless
        # (idempotent) but wasteful. Recently-expired entries are added
        # first so they win the ordering (see the docstring above); the
        # setdefault on the active list just means an instrument already
        # queued from the expired pass doesn't get a second, redundant
        # entry.
        option_instruments_by_name = {}
        for inst in self._get_recently_expired_instruments():
            if inst.get("kind") == "option" and inst.get("instrument_name"):
                option_instruments_by_name[inst["instrument_name"]] = inst
        for inst in self._get_instruments():
            if inst.get("kind") == "option" and inst.get("instrument_name"):
                option_instruments_by_name.setdefault(inst["instrument_name"], inst)
        option_instruments = list(option_instruments_by_name.values())

        icp = self.env["ir.config_parameter"]
        try:
            timeout = float(icp.get_param("dankbit.deribit_timeout", default=5.0))
        except Exception:
            timeout = 5.0

        URL = "https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time"

        # Always fetch each instrument's full history from scratch — see
        # the docstring above for why this run no longer resumes from the
        # DB's latest known trade.
        base_start = 0  # REST can only return what it still retains internally

        for inst in option_instruments:
            inst_name = inst.get("instrument_name")
            if not inst_name:
                continue

            try:
                start_ts = base_start
                now_ts = int(time_module.time() * 1000)

                # For an already-expired instrument (see
                # _get_recently_expired_instruments), Deribit will never
                # have a trade past its own expiration_timestamp — cap the
                # fetch window there instead of "now" so we don't ask for a
                # range Deribit can never answer.
                expiration_ts = inst.get("expiration_timestamp")
                end_ts = min(now_ts, expiration_ts) if expiration_ts else now_ts

                if start_ts >= end_ts:
                    # Defensive only — with start_ts always 0 this fires
                    # only if expiration_ts is degenerate (<= 0).
                    _logger.debug("Skipping %s — degenerate fetch window (start_ts=%s >= end_ts=%s)", inst_name, start_ts, end_ts)
                    continue

                _logger.info(
                    "Fetching trades for %s from %s → %s",
                    inst_name, start_ts, end_ts
                )

                #
                # Pagination loop
                #
                # Deribit REST pagination works ONLY via timestamp windows.
                # “has_more” sometimes appears even when “trades=[]”, so we need safety exits.
                #
                empty_pages = 0
                max_empty_pages = 3       # prevent infinite loops
                max_pages = 5000          # safety guard

                pages = 0

                while pages < max_pages:
                    pages += 1

                    params = {
                        "instrument_name": inst_name,
                        "count": 1000,
                        "start_timestamp": start_ts,
                        "end_timestamp": end_ts,
                        "sorting": "asc",
                    }

                    #
                    # Robust request with backoff — _safe_deribit_request()
                    # already retries (and eventually gives up with None)
                    # on a Deribit-level {"error": ...} body, e.g. rate
                    # limiting, not just on network/HTTP failures.
                    #
                    data = _safe_deribit_request(URL, params=params, timeout=timeout)
                    if not data:
                        _logger.warning("Deribit request failed for %s, stopping pagination.", inst_name)
                        break

                    if not data or "result" not in data:
                        _logger.warning("No valid result for %s", inst_name)
                        break

                    trades = data["result"].get("trades", [])

                    #
                    # Handle empty page
                    #
                    if not trades:
                        empty_pages += 1

                        # if Deribit signals more but gives nothing — bail
                        if empty_pages >= max_empty_pages:
                            _logger.warning(
                                "Stopping early for %s due to repeated empty pages.",
                                inst_name
                            )
                            break

                        # chill and try next cycle
                        time_module.sleep(0.05)
                        continue

                    #
                    # Insert all trades in chronological order
                    #
                    for trd in trades:
                        self._create_new_trade(
                            trd,
                            inst.get("expiration_timestamp")
                        )

                    # advance pagination timestamp — inclusive, same
                    # same-millisecond reasoning as the initial start_ts above.
                    start_ts = trades[-1]["timestamp"]
                    empty_pages = 0

                    #
                    # break if no more pages
                    #
                    if not data["result"].get("has_more"):
                        break

                    # polite pacing
                    time_module.sleep(0.05 + random.random() * 0.02)

                # per-instrument commit
                self.env.cr.commit()

                _logger.info("Finished fetching trades for %s", inst_name)
            except Exception:
                _logger.exception(
                    "Unexpected error fetching trades for %s, skipping to next instrument.",
                    inst_name,
                )
                self.env.cr.rollback()

            # polite pause between instruments to avoid hammering Deribit
            time_module.sleep(0.1 + random.random() * 0.05)

    @api.model
    def get_last_trade(self, instrument_name):
        """
        Returns the latest trade for a given instrument
        ordered by timestamp descending.
        """
        if not instrument_name:
            return self.browse()

        return self.search(
            [("name", "ilike", instrument_name)],
            order="deribit_ts desc, id desc",
            limit=1,
        )

    def _get_instruments(self):
        URL = "https://www.deribit.com/api/v2/public/get_instruments"

        timeout = 5.0
        try:
            icp = self.env["ir.config_parameter"]
            timeout = float(icp.get_param("dankbit.deribit_timeout", default=5.0))
            cache_ttl = float(icp.get_param("dankbit.deribit_cache_ttl", default=300.0))
        except Exception:
            cache_ttl = 300.0

        now_ts = time_module.time()
        all_instruments = []

        for currency in ("BTC", "ETH"):
            cache_key = f"instruments_{currency}"
            cached = _DERIBIT_CACHE.get(cache_key, {})

            if (
                cached
                and cached.get("value") is not None
                and (now_ts - cached.get("ts", 0) < cache_ttl)
            ):
                all_instruments.extend(cached["value"])
                continue

            params = {
                "currency": currency,
                "kind": "option",
                "expired": "false",
            }

            data = _safe_deribit_request(URL, params=params, timeout=timeout)

            if data and isinstance(data, dict):
                instruments = data.get("result", [])
                _DERIBIT_CACHE[cache_key] = {
                    "ts": now_ts,
                    "value": instruments,
                }
                all_instruments.extend(instruments)
            else:
                _logger.warning(
                    "Failed to fetch %s instruments from Deribit, using cache if available",
                    currency,
                )
                if cached and cached.get("value"):
                    all_instruments.extend(cached["value"])

        return all_instruments

    def _get_recently_expired_instruments(self, grace_minutes=RECENTLY_EXPIRED_GRACE_MINUTES):
        """Option instruments whose expiration_timestamp falls within the
        last `grace_minutes` minutes, per currency — see
        RECENTLY_EXPIRED_GRACE_MINUTES's own comment for why get_last_trades()
        needs this at all: _get_instruments() alone only ever returns
        currently-active ("expired: false") instruments, so an instrument
        that expires between two polls would otherwise drop out of that
        list forever, silently losing any trades that landed between the
        last poll before its expiry and the expiration moment itself.

        Deribit's own "expired: true" filter returns every option ever
        settled for that currency (years of history) — fetching that full
        list is one bounded REST call, but iterating all of it every tick
        would grow unbounded over time, so it's filtered down to the
        grace window here before being handed back to the caller. Cached
        the same way/TTL as _get_instruments()'s own active list (a
        separate cache key/currency, so neither list can evict the
        other's cached value)."""
        URL = "https://www.deribit.com/api/v2/public/get_instruments"

        timeout = 5.0
        try:
            icp = self.env["ir.config_parameter"]
            timeout = float(icp.get_param("dankbit.deribit_timeout", default=5.0))
            cache_ttl = float(icp.get_param("dankbit.deribit_cache_ttl", default=300.0))
        except Exception:
            cache_ttl = 300.0

        now_ts = time_module.time()
        cutoff_ms = int((now_ts - grace_minutes * 60) * 1000)
        recently_expired = []

        for currency in ("BTC", "ETH"):
            cache_key = f"expired_instruments_{currency}"
            cached = _DERIBIT_CACHE.get(cache_key, {})

            if (
                cached
                and cached.get("value") is not None
                and (now_ts - cached.get("ts", 0) < cache_ttl)
            ):
                instruments = cached["value"]
            else:
                params = {
                    "currency": currency,
                    "kind": "option",
                    "expired": "true",
                }

                data = _safe_deribit_request(URL, params=params, timeout=timeout)

                if data and isinstance(data, dict):
                    instruments = data.get("result", [])
                    _DERIBIT_CACHE[cache_key] = {
                        "ts": now_ts,
                        "value": instruments,
                    }
                else:
                    _logger.warning(
                        "Failed to fetch expired %s instruments from Deribit, using cache if available",
                        currency,
                    )
                    instruments = cached.get("value") or []

            recently_expired.extend(
                inst for inst in instruments
                if inst.get("expiration_timestamp") and inst["expiration_timestamp"] >= cutoff_ms
            )

        return recently_expired

    def _create_new_trade(self, trade, expiration_ts):
        deribit_dt = datetime.fromtimestamp(trade["timestamp"] / 1000, tz=timezone.utc)
        exp_dt = datetime.fromtimestamp(expiration_ts / 1000, tz=timezone.utc) if expiration_ts else None

        vals = {
            "name": trade.get("instrument_name"),
            "iv": trade.get("iv"),
            "index_price": trade.get("index_price"),
            "price": trade.get("price"),
            "mark_price": trade.get("mark_price"),
            "direction": trade.get("direction"),
            "trade_seq": trade.get("trade_seq"),
            "deribit_trade_identifier": trade.get("trade_id"),
            "amount": trade.get("amount"),
            "deribit_ts": fields.Datetime.to_string(deribit_dt),
            "expiration": fields.Datetime.to_string(exp_dt) if exp_dt else False,
            "is_block_trade": bool(
                trade.get("is_block_trade")
                or trade.get("block_trade")
                or trade.get("block_trade_id")
            ),
            "block_trade_id": trade.get("block_trade_id"),
        }

        try:
            with self.env.cr.savepoint():
                self.env["dankbit.trade"].create(vals)
        except Exception as e:
            if "deribit_trade_identifier" in str(e):
                return  # WS already inserted this trade — skip silently
            _logger.exception("Failed to create trade %s", trade.get("trade_id"))
            raise

    # run by scheduled action
    def _delete_expired_trades(self):
        self.env["dankbit.trade"].search(
            domain=[
                ("expiration", "<", fields.Datetime.now()), 
                ("active", "=", True)
            ]
        ).write({"active": False})

    @api.model
    def get_views(self, views, options=None):
        """Stamps the "Last N Hours" search filters (see trade_views.xml)
        with concrete UTC timestamps computed server-side, replacing
        __NOW__/__LAST_2H__/__LAST_4H__/__LAST_8H__/__LAST_24H__ placeholder tokens in the
        search view's arch. Those filters can't compute "now" as a plain
        domain expression themselves: Odoo's client-side domain evaluator
        (py_date.js) only implements datetime.datetime.now() using the
        browser's local wall-clock components (no utcnow(), no tz-aware
        conversion), so a client-evaluated "last N hours" filter would be
        off by the viewing user's UTC offset when compared against the
        naive-UTC deribit_ts/expiration columns — the same class of bug
        just fixed server-side in controllers/main.py's ORM domains.
        Substituting in the server's own UTC clock here avoids that
        entirely. Runs once per search-view fetch (e.g. page load), not
        live on every filter toggle — same effective freshness as any other
        "recent" filter in this addon."""
        res = super().get_views(views, options=options)
        search_view = res.get("views", {}).get("search")
        if search_view and "arch" in search_view:
            now = fields.Datetime.now()
            replacements = {
                "__NOW__": fields.Datetime.to_string(now),
                "__LAST_2H__": fields.Datetime.to_string(now - timedelta(hours=2)),
                "__LAST_4H__": fields.Datetime.to_string(now - timedelta(hours=4)),
                "__LAST_8H__": fields.Datetime.to_string(now - timedelta(hours=8)),
                "__LAST_24H__": fields.Datetime.to_string(now - timedelta(hours=24)),
            }
            arch = search_view["arch"]
            for token, value in replacements.items():
                arch = arch.replace(token, value)
            search_view["arch"] = arch
        return res

    def open_plot_wizard_taker(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": "dankbit.plot_wizard",
            "view_mode": "form",
            "view_id": self.env.ref("dankbit.view_plot_wizard_form").id,
            "target": "new",
            "context": {
                "dankbit_view_type": "taker",
            }
        }

    def open_zones_wizard(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": "dankbit.zones_wizard",
            "view_mode": "form",
            "view_id": self.env.ref("dankbit.view_zones_wizard_form").id,
            "target": "new",
        }
