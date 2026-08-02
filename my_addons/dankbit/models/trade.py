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

# Separate cache for Kraken Futures candles (get_candles_kraken_futures) —
# same short-fixed-TTL reasoning as _BINANCE_CACHE above, just a distinct
# dict/key-space since this is a different upstream API.
_KRAKEN_CACHE = {}
_KRAKEN_CANDLES_CACHE_TTL = 5.0

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

def _safe_kraken_futures_request(
    url,
    params,
    timeout=10.0,
    retries=3,
    backoff=0.4,
    raise_on_fail=False,
):
    """Same robust GET-with-retries/backoff shape as _safe_binance_request
    above, for Kraken Futures' public charts API instead — a failure there
    is signalled by the response body simply lacking a "candles" key
    (no {"code"/"error": ...} envelope the way Binance/Deribit use), so
    that's what's checked here rather than a specific error key."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict) or "candles" not in data:
                raise RuntimeError(f"Kraken Futures error: {data}")
            return data
        except Exception as e:
            _logger.warning(
                "Kraken Futures request failed (%d/%d) %s params=%s error=%s",
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

    _KRAKEN_FUTURES_SYMBOL_MAP = {"BTC": "PF_XBTUSD", "ETH": "PF_ETHUSD"}

    def get_candles_kraken_futures(self, asset, interval="4h", limit=500):
        """Real Kraken Futures candles (USD-margined perpetuals — PF_XBTUSD/
        PF_ETHUSD), oldest-first, same {t, o, h, l, c} shape get_candles()
        returns — used by /gt/<asset> and /4l/<asset> (both via the shared
        klines_futures_proxy route), per product decision to source those
        two pages' candles from Kraken Futures instead of Binance spot;
        every other TradingView page in this addon (/chart, /oi, /mp)
        keeps using get_candles()/Binance unchanged.

        Kraken Futures' public charts API (https://futures.kraken.com/api/
        charts/v1/trade/<symbol>/<resolution>) has the same native 15m/1h/
        4h/1d resolution strings this addon's own interval values already
        use, and its own `count` param returns exactly the most recent N
        candles ending "now" oldest-first — no aggregation or window math
        needed here either, same as get_candles()'s own Binance call.
        Cached in the separate _KRAKEN_CACHE dict (same short fixed
        _KRAKEN_CANDLES_CACHE_TTL=5s reasoning as _BINANCE_CANDLES_CACHE_TTL
        — this gets polled every 5s per open /gt tab)."""
        symbol = self._KRAKEN_FUTURES_SYMBOL_MAP.get(asset.upper(), "PF_" + asset.upper() + "USD")

        cache_key = f"kraken_candles_{symbol}_{interval}_{limit}"
        now_ts = time_module.time()
        cached = _KRAKEN_CACHE.get(cache_key, {})
        if cached and cached.get("value") is not None and (now_ts - cached.get("ts", 0) < _KRAKEN_CANDLES_CACHE_TTL):
            return cached.get("value")

        url = f"https://futures.kraken.com/api/charts/v1/trade/{symbol}/{interval}"
        data = _safe_kraken_futures_request(url, params={"count": limit}, timeout=10.0)
        if not data:
            if cached and cached.get("value") is not None:
                _logger.warning("get_candles_kraken_futures: using stale cached value for %s", cache_key)
                return cached.get("value")
            _logger.exception("get_candles_kraken_futures failed and no cache available for %s", cache_key)
            return []

        # Kraken Futures candle row: {"time": ms, "open", "high", "low",
        # "close", "volume"} — oldest-first already, matching this
        # function's own documented return order (same as get_candles()).
        candles = [
            {"t": int(row["time"]), "o": float(row["open"]), "h": float(row["high"]), "l": float(row["low"]), "c": float(row["close"])}
            for row in data["candles"]
        ]
        _KRAKEN_CACHE[cache_key] = {"ts": now_ts, "value": candles}
        return candles

    def _get_latest_trade_ts_for_instrument(self, instrument_name: str):
        return self.with_context(active_test=False).search(
            [("name", "=", instrument_name)], order="deribit_ts desc", limit=1
        )

    # ========== FETCHING & INGESTION ==========

    # run by scheduled action
    def get_last_trades(self):
        """
        Hardened REST-only trade importer.
        - Full history already exists → incremental fetch per instrument.
        - Uses timestamp-based pagination (Deribit REST's only supported method).
        - Ensures no gaps, no flooding, no duplicate inserts.
        - Gracefully handles Deribit rate-limit, empty responses, and pagination quirks.
        - One instrument's failure can't take down the rest of the run: each
          instrument's fetch is wrapped in its own try/except, so a single
          malformed response/record only skips that instrument for this
          cycle rather than aborting every instrument still left in
          option_instruments (the next cron cycle resumes it normally,
          since start_ts is always recomputed from the DB's last committed
          trade).
        """

        option_instruments = [
            inst for inst in self._get_instruments()
            if inst.get("kind") == "option"
        ]

        icp = self.env["ir.config_parameter"]
        try:
            timeout = float(icp.get_param("dankbit.deribit_timeout", default=5.0))
        except Exception:
            timeout = 5.0

        URL = "https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time"

        # critical: if DB already contains full history → always start from last trade timestamp
        # NEVER limit by "days ago" again
        base_start = 0  # REST can only return what it still retains internally

        for inst in option_instruments:
            inst_name = inst.get("instrument_name")
            if not inst_name:
                continue

            try:
                latest_trade = self._get_latest_trade_ts_for_instrument(inst_name)

                # choose correct starting point
                if latest_trade and latest_trade.deribit_ts:
                    dt_val = latest_trade.deribit_ts
                    if isinstance(dt_val, str):
                        dt_obj = fields.Datetime.from_string(dt_val)
                    else:
                        dt_obj = dt_val
                    if dt_obj.tzinfo is None:
                        dt_obj = dt_obj.replace(tzinfo=timezone.utc)

                    # Resume AT the last known trade's timestamp, not one ms
                    # past it: Deribit's start_timestamp bound is inclusive,
                    # and options books can have multiple trades landing in
                    # the exact same millisecond (multi-leg/block fills). A
                    # "+1" here would permanently skip any sibling trades at
                    # that same millisecond that weren't in the last fetched
                    # page. The one guaranteed re-fetch of the boundary
                    # trade itself is cheap: _create_new_trade() already
                    # silently drops it via the deribit_trade_identifier
                    # unique-constraint conflict.
                    start_ts = int(dt_obj.timestamp() * 1000)
                else:
                    # fallback (fresh DB case, or an instrument with zero trades)
                    start_ts = base_start

                now_ts = int(time_module.time() * 1000)

                if start_ts >= now_ts:
                    _logger.debug("Skipping %s — already up to date (start_ts=%s >= now_ts=%s)", inst_name, start_ts, now_ts)
                    continue

                _logger.info(
                    "Fetching trades for %s from %s → %s",
                    inst_name, start_ts, now_ts
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
                        "end_timestamp": now_ts,
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
