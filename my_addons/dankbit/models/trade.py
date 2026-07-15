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
    Returns None on failure unless raise_on_fail=True.
    """

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
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
        params = {}
        URL = "https://www.deribit.com/api/v2/public/get_index_price"

        if instrument.startswith("BTC"):
            params = {"index_name": "btc_usdt"}
        if instrument.startswith("ETH"):
            params = {"index_name": "eth_usdt"}

        # read timeout from config (seconds)
        timeout = 5.0
        try:
            icp = self.env["ir.config_parameter"]
            timeout = float(icp.get_param("dankbit.deribit_timeout", default=5.0))
            cache_ttl = float(icp.get_param("dankbit.deribit_cache_ttl", default=30.0))
        except Exception:
            cache_ttl = 30.0

        # consult cache first — keyed by currency to avoid BTC/ETH collision
        currency = "BTC" if instrument.startswith("BTC") else "ETH"
        cache_key = f"index_price_{currency}"
        now_ts = time_module.time()
        cached = _DERIBIT_CACHE.get(cache_key, {})
        if cached and cached.get("value") is not None and (now_ts - cached.get("ts", 0) < cache_ttl):
            return cached.get("value")

        data = _safe_deribit_request(URL, params=params, timeout=timeout)
        if data and isinstance(data, dict):
            val = data.get("result", {}).get("index_price", 0.0)
            _DERIBIT_CACHE[cache_key] = {"ts": now_ts, "value": val}
            return val
        else:
            if cached and cached.get("value") is not None:
                _logger.warning("get_index_price: using stale cached value")
                return cached.get("value")
            _logger.exception("get_index_price failed and no cache available")
            return 0.0

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

                # pick up exactly after the last known trade
                start_ts = int(dt_obj.timestamp() * 1000) + 1
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
                # Robust request with backoff
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

                # advance pagination timestamp
                start_ts = trades[-1]["timestamp"] + 1
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
        __NOW__/__LAST_2H__/__LAST_4H__/__LAST_8H__ placeholder tokens in the
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
