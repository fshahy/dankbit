# -*- coding: utf-8 -*-

import random
import pytz
from datetime import datetime, timezone, timedelta, time
import logging
import requests, time as time_module

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Simple in-memory cache to avoid hitting Deribit too often.
# Keys: 'index_price', 'instruments', optionally others.
_DERIBIT_CACHE = {
    'index_price': {'ts': 0, 'value': None},
    'instruments': {'ts': 0, 'value': None},
}

# Simple in-memory cache for OI per instrument
# { instrument_name: {'ts': epoch_seconds, 'value': float} }
_DERIBIT_OI_CACHE = {}

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
    # hours_to_expiry = (expiration - now).total_seconds() / 3600
    block_trade_id = fields.Char(
        string="Block Trade ID",
        help="Deribit-assigned ID if this trade was executed as a block trade."
    )
    is_block_trade = fields.Boolean(
        string="Is Block Trade",
        default=False,
        help="True if this trade came from a Deribit block trade event."
    )
    oi_impact = fields.Float(
        string="OI Impact",
        default=0.0,
        help="Allocated OI change for this trade"
    )

    oi_reconciled = fields.Boolean(
        string="OI Reconciled",
        default=False,
        index=True,
        help="Whether OI impact has been reconciled"
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


    @staticmethod
    def fetch_deribit_open_interest(instrument_name: str):
        DERIBIT_URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_instrument"
        CACHE_TTL = 15.0

        now_ts = time_module.time()
        cached = _DERIBIT_OI_CACHE.get(instrument_name)

        # prune old cache entries occasionally
        if len(_DERIBIT_OI_CACHE) > 5000:
            cutoff = now_ts - 300
            for k in list(_DERIBIT_OI_CACHE.keys()):
                if _DERIBIT_OI_CACHE[k]["ts"] < cutoff:
                    _DERIBIT_OI_CACHE.pop(k, None)

        if cached and (now_ts - cached["ts"] < CACHE_TTL):
            return cached["value"]

        data = _safe_deribit_request(
            DERIBIT_URL,
            params={"instrument_name": instrument_name},
            timeout=10.0,
        )

        if not data or "result" not in data or not data["result"]:
            # network / API failure → use cache if possible
            if cached:
                _logger.warning("OI fetch failed for %s, using cached value", instrument_name)
                return cached["value"]

            # no cache → signal failure
            _logger.warning("OI fetch failed for %s, no cache available", instrument_name)
            return None

        oi = float(data["result"][0].get("open_interest", 0.0))

        _DERIBIT_OI_CACHE[instrument_name] = {
            "ts": now_ts,
            "value": oi,
        }
        return oi
    
    @staticmethod
    def expiry_window():
        now = datetime.now(timezone.utc)
        return (now + timedelta(hours=32)).timestamp() * 1000
            
    def _cron_fetch_oi_snapshots(self):
        Snapshot = self.env["dankbit.oi_snapshot"]
        now = fields.Datetime.now()

        # 1) Get authoritative instrument list from Deribit
        instruments = self._get_instruments()

        if not instruments:
            _logger.warning("No instruments returned from Deribit for OI snapshot.")
            return

        # 2) Snapshot OI for every eligible option instrument
        window = self.expiry_window()

        for inst in instruments:
            try:
                if inst.get("kind") != "option":
                    continue

                instrument_name = inst.get("instrument_name")
                expiration_ts = inst.get("expiration_timestamp")

                if not instrument_name or not expiration_ts or expiration_ts > window:
                    continue

                # convert expiration timestamp
                exp_dt = datetime.fromtimestamp(
                    expiration_ts / 1000,
                    tz=timezone.utc
                )

                # apply your expiry cutoff logic
                if exp_dt < self._get_tomorrows_ts():
                    continue

                # polite pacing (Deribit friendly)
                time_module.sleep(0.03 + random.random() * 0.02)

                oi = self.fetch_deribit_open_interest(instrument_name)
                if oi is None:
                    continue

                last = Snapshot.search(
                    [("name", "=", instrument_name)],
                    order="timestamp desc",
                    limit=1
                )
                if last and (now - last.timestamp).total_seconds() < 60:
                    continue

                Snapshot.create({
                    "name": instrument_name,
                    "open_interest": oi,
                    "timestamp": now,
                })
                _logger.info(
                    "OI snapshot taken for %s: %.2f",
                    instrument_name,
                    oi,
                )

            except Exception as e:
                _logger.warning(
                    "OI snapshot failed for %s: %s",
                    inst.get("instrument_name"),
                    e,
                )
                # IMPORTANT: rollback to keep cron alive
                self.env.cr.rollback()
                continue

    def reconcile_oi_impact(self, instrument_name):
        Trade = self.env["dankbit.trade"]
        Snapshot = self.env["dankbit.oi_snapshot"]

        snaps = Snapshot.search(
            [("name", "=", instrument_name)],
            order="timestamp desc",
            limit=2,
        )
        if len(snaps) < 2:
            return

        newer, older = snaps[0], snaps[1]

        if newer.timestamp <= older.timestamp:
            return

        delta_oi = float(newer.open_interest) - float(older.open_interest)

        trades = Trade.search([
            ("name", "=", instrument_name),
            ("is_block_trade", "=", False),
            ("deribit_ts", ">", older.timestamp),
            ("deribit_ts", "<=", newer.timestamp),
            ("oi_reconciled", "=", False),
        ])

        if not trades:
            return

        if delta_oi == 0.0:
            trades.write({"oi_impact": 0.0, "oi_reconciled": True})
            return

        total_volume = sum(abs(t.amount or 0.0) for t in trades)
        if total_volume <= 0.0:
            trades.write({"oi_impact": 0.0, "oi_reconciled": True})
            return

        for t in trades:
            weight = abs(t.amount or 0.0) / total_volume
            t.oi_impact = delta_oi * weight
            t.oi_reconciled = True
            _logger.info(
                "Reconciled OI impact for trade %s: %.2f (weight %.4f)",
                t.deribit_trade_identifier,
                t.oi_impact,
                weight,
            )

    def _cron_reconcile_oi(self):
        Snapshot = self.env["dankbit.oi_snapshot"]

        # get instruments that actually have snapshots
        groups = Snapshot.read_group(
            domain=[
                ("timestamp", ">=", fields.Datetime.now() - timedelta(hours=1)),
            ],
            fields=["name"],
            groupby=["name"],
        )

        for g in groups:
            instrument = g.get("name")
            if not instrument:
                continue

            try:
                self.reconcile_oi_impact(instrument)
            except Exception:
                self.env.cr.rollback()
                _logger.exception("OI reconciliation failed for %s", instrument)
                continue

    @api.depends('expiration')
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
        _logger.info("------------------- get_index_price -------------------")
        params = {}
        URL = "https://www.deribit.com/api/v2/public/get_index_price"

        if instrument.startswith("BTC"):
            params = {"index_name": "btc_usdt"}
        if instrument.startswith("ETH"):
            params = {"index_name": "eth_usdt"}
        

        # read timeout from config (seconds)
        timeout = 5.0
        try:
            icp = self.env['ir.config_parameter'].sudo()
            timeout = float(icp.get_param('dankbit.deribit_timeout', default=5.0))
            cache_ttl = float(icp.get_param('dankbit.deribit_cache_ttl', default=30.0))
        except Exception:
            cache_ttl = 30.0

        # consult cache first
        now_ts = time_module.time()
        cached = _DERIBIT_CACHE.get('index_price', {})
        if cached and cached.get('value') is not None and (now_ts - cached.get('ts', 0) < cache_ttl):
            return cached.get('value')

        data = _safe_deribit_request(URL, params=params, timeout=timeout)
        if data and isinstance(data, dict):
            val = data.get("result", {}).get("index_price", 0.0)
            _DERIBIT_CACHE['index_price'] = {'ts': now_ts, 'value': val}
            return val
        else:
            # on failure, fall back to last cached value if available
            if cached and cached.get('value') is not None:
                _logger.warning("get_index_price: using stale cached value")
                return cached.get('value')
            _logger.exception("get_index_price failed and no cache available")
            return 0.0

    # (kept for compatibility: global latest; useful elsewhere)
    def _get_latest_trade_ts(self):
        return self.search([], order="deribit_ts desc", limit=1)

    # NEW: per-instrument latest trade timestamp (fixes missing inactive strikes)
    def _get_latest_trade_ts_for_instrument(self, instrument_name: str):
        return self.search([("name", "=", instrument_name)], order="deribit_ts desc", limit=1)

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

        icp = self.env['ir.config_parameter'].sudo()
        try:
            timeout = float(icp.get_param("dankbit.deribit_timeout", default=5.0))
        except Exception:
            timeout = 5.0

        URL = "https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time"
        now_ts = int(time_module.time() * 1000)

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

    @staticmethod
    def _get_tomorrows_ts():
        """
        Return tomorrow's option expiry timestamp at 08:00 UTC
        as a timezone-aware datetime.
        """

        now = datetime.now(timezone.utc)
        tomorrow = now.date() + timedelta(days=1)

        expiry_dt = datetime(
            year=tomorrow.year,
            month=tomorrow.month,
            day=tomorrow.day,
            hour=8,
            minute=0,
            second=0,
            tzinfo=timezone.utc,
        )

        return expiry_dt

    def _get_instruments(self):
        URL = "https://www.deribit.com/api/v2/public/get_instruments"

        timeout = 5.0
        try:
            icp = self.env['ir.config_parameter'].sudo()
            timeout = float(icp.get_param('dankbit.deribit_timeout', default=5.0))
            cache_ttl = float(icp.get_param('dankbit.deribit_cache_ttl', default=300.0))
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
        try:
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

            self.env["dankbit.trade"].create(vals)

        except Exception as e:
            # VERY IMPORTANT: rollback poisoned transaction
            self.env.cr.rollback()

            # duplicate trade → safe to ignore
            if "deribit_trade_identifier" in str(e):
                return

            _logger.exception("Failed to create trade %s", trade.get("trade_id"))
            raise



    @staticmethod
    def _get_midnight_dt(days_offset=0):
        """
        Return midnight UTC minus 'days_offset' days, in milliseconds since epoch.
        Example:
            _get_midnight_dt(0)  → today's midnight UTC (ms)
            _get_midnight_dt(1)  → yesterday's midnight UTC (ms)
        """
        now = datetime.now(timezone.utc)
        midnight = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=timezone.utc)
        return int((midnight - timedelta(days=days_offset)).timestamp() * 1000)

    # run by scheduled action
    def _delete_expired_trades(self):
        self.env['dankbit.trade'].search(
            domain=[
                ("expiration", "<", fields.Datetime.now()), 
                ("active", "=", True)
            ]
        ).write({"active": False})

    def get_btc_option_name_for_tomorrow_expiry(self):
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        instrument = f"BTC-{tomorrow.day}{tomorrow.strftime('%b').upper()}{tomorrow.strftime('%y')}"
        return instrument

    # run by scheduled action
    def _take_screenshot(self):
        now = datetime.now().time()
        start = time(5, 0)   # 05:00
        end   = time(9, 0)   # 09:00

        if not (start <= now <= end):
            _logger.info("Skipping screenshot: outside time window.")
            return  # skip outside window
        
        btc_today = self.get_btc_option_name_for_tomorrow_expiry()
        # Use configured base URL so this works both on dankbit.com and locally.
        icp = self.env['ir.config_parameter'].sudo()
        try:
            base_url = icp.get_base_url()
        except Exception:
            # fallback to param (older Odoo versions)
            base_url = icp.get_param('web.base.url', default='http://localhost:8069')

        # Build the URL robustly and allow local hosts.
        full_url = f"{base_url.rstrip('/')}/{btc_today}/mm/4"
        _logger.info("Taking screenshot using URL: %s", full_url)

        # timeout configurable (seconds)
        try:
            timeout = float(icp.get_param('dankbit.screenshot_timeout', default=3.0))
        except Exception:
            timeout = 3.0

        try:
            response = requests.get(full_url, timeout=timeout)
            response.raise_for_status()
            self.env.cr.commit()
            _msg = f"✅ Called {full_url} — {response.status_code}"
        except requests.exceptions.SSLError as e:
            # Retry without SSL verification for local dev servers with self-signed certs
            _logger.warning("SSL error when calling %s: %s — retrying with verify=False", full_url, e)
            try:
                response = requests.get(full_url, timeout=timeout, verify=False)
                response.raise_for_status()
                self.env.cr.commit()
                _msg = f"✅ Called {full_url} (insecure) — {response.status_code}"
            except Exception as e2:
                _msg = f"❌ Error calling {full_url} (insecure retry): {e2}"
        except Exception as e:
            _msg = f"❌ Error calling {full_url}: {e}"

        self.env['ir.logging'].sudo().create({
            'name': 'Dankbit Screenshot Taker',
            'type': 'server',
            'dbname': self._cr.dbname,
            'level': 'info',
            'message': _msg,
            'path': __name__,
            'func': '_take_screenshot',
            'line': '0',
        })
        return True

    def open_plot_wizard_taker(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": "dankbit.plot_wizard",
            "view_mode": "form",
            "view_id": self.env.ref("dankbit.view_plot_wizard_form").id,
            "target": "new",
            'context': {
                "dankbit_view_type": "taker",
            }
        }

    def open_plot_wizard_mm(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": "dankbit.plot_wizard",
            "view_mode": "form",
            "view_id": self.env.ref("dankbit.view_plot_wizard_form").id,
            "target": "new",
            'context': {
                "dankbit_view_type": "mm",
            }
        }

class DankbitScreenshot(models.Model):
    _name = "dankbit.screenshot"
    _description = "Dankbit Screenshot"
    _order = "timestamp asc"

    name = fields.Char(required=True)
    timestamp = fields.Datetime(string="Timestamp", default=lambda self: fields.Datetime.now())
    image_png = fields.Binary(string="Chart Image", attachment=True)


class DankbitOISnapshot(models.Model):
    _name = "dankbit.oi_snapshot"
    _description = "Option Open Interest Snapshot"
    _order = "timestamp desc"

    name = fields.Char(required=True, index=True)
    open_interest = fields.Float(required=True)
    timestamp = fields.Datetime(required=True, index=True)
