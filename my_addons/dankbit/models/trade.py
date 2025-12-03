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

def _safe_deribit_request(url, params, timeout=5.0, retries=2, backoff=0.5):
    """Make a requests.get call with retries and exponential backoff.
    Returns parsed JSON on success, or None on persistent failure.
    """
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            _logger.warning("Deribit request failed (attempt %d/%d) %s %s: %s",
                            attempt + 1, retries + 1, url, params, e)
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
            else:
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
    iv = fields.Float(string="IV %", digits=(2, 2), required=True)
    amount = fields.Float(digits=(6, 2), required=True)
    contracts = fields.Float(digits=(6, 2))
    deribit_ts = fields.Datetime()
    deribit_trade_identifier = fields.Float(digits=(15, 0), string="Deribit Trade ID", required=True)
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

    def get_index_price(self):
        _logger.info("------------------- get_index_price -------------------")
        URL = "https://www.deribit.com/api/v2/public/get_index_price"
        params = {"index_name": "btc_usdt"}

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
                backoff = 0.2
                for attempt in range(5):
                    try:
                        data = _safe_deribit_request(URL, params=params, timeout=timeout)
                        break
                    except Exception as e:
                        _logger.warning(
                            "Deribit request failed (%s/%s) %s params=%s: %s",
                            attempt+1, 5, URL, params, e
                        )
                        time.sleep(backoff)
                        backoff *= 2
                else:
                    _logger.error("Giving up on %s after repeated errors.", inst_name)
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
                    time.sleep(0.05)
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
                time.sleep(0.05 + random.random() * 0.02)

            # per-instrument commit
            self.env.cr.commit()

            _logger.info("Finished fetching trades for %s", inst_name)


    def _get_tomorrows_ts(self):
        # Current UTC time
        now = datetime.now(pytz.utc)
        # Tomorrow's date
        tomorrow = now.date() + timedelta(days=1)
        # Tomorrow at 08:00 GMT
        target = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, 0, 0, tzinfo=timezone.utc)
        # Milliseconds since epoch
        return int(target.timestamp() * 1000)

    def _get_instruments(self):
        URL = "https://www.deribit.com/api/v2/public/get_instruments"
        params = {
            "currency": "BTC",
            "kind": "option",
            "expired": "false"
        }

        timeout = 5.0
        try:
            icp = self.env['ir.config_parameter'].sudo()
            timeout = float(icp.get_param('dankbit.deribit_timeout', default=5.0))
            cache_ttl = float(icp.get_param('dankbit.deribit_cache_ttl', default=300.0))
        except Exception:
            cache_ttl = 300.0

        now_ts = time_module.time()
        cached = _DERIBIT_CACHE.get('instruments', {})
        if cached and cached.get('value') is not None and (now_ts - cached.get('ts', 0) < cache_ttl):
            return cached.get('value')

        data = _safe_deribit_request(URL, params=params, timeout=timeout)
        if data and isinstance(data, dict):
            instruments = data.get("result", [])
            _DERIBIT_CACHE['instruments'] = {'ts': now_ts, 'value': instruments}
            return instruments
        else:
            _logger.exception("_get_instruments failed and no cache available")
            return cached.get('value', [])

    def _create_new_trade(self, trade, expiration_ts):
        exists = self.env["dankbit.trade"].search(
            domain=[("deribit_trade_identifier", "=", trade.get("trade_id"))],
            limit=1
        )

        icp = self.env['ir.config_parameter'].sudo()
        try:
            start_from_ts = int(icp.get_param("dankbit.from_days_ago", default=2))
        except Exception:
            start_from_ts = 2

        start_ts = self._get_midnight_dt(start_from_ts)

        # skip anything older than our configured window
        if exists or trade.get("timestamp", 0) <= start_ts:
            return

        # convert timestamps to timezone-aware UTC datetimes and store
        try:
            deribit_dt = datetime.fromtimestamp(trade["timestamp"]/1000, tz=timezone.utc)
            exp_dt = datetime.fromtimestamp(expiration_ts/1000, tz=timezone.utc) if expiration_ts else None
            deribit_str = fields.Datetime.to_string(deribit_dt)
            exp_str = fields.Datetime.to_string(exp_dt) if exp_dt else False
        except Exception:
            # fallback to raw string format if conversion fails
            deribit_str = datetime.fromtimestamp(trade["timestamp"]/1000).strftime('%Y-%m-%d %H:%M:%S')
            exp_str = datetime.fromtimestamp(expiration_ts/1000).strftime('%Y-%m-%d %H:%M:%S') if expiration_ts else False

        block_trade_id = trade.get("block_trade_id")
        is_block_trade = (
            trade.get("is_block_trade")
            or trade.get("block_trade")
            or bool(block_trade_id)
        )

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
            "contracts": trade.get("contracts", trade.get("amount")),  # fallback to amount
            "deribit_ts": deribit_str,
            "expiration": exp_str,
            "is_block_trade": is_block_trade,
            "block_trade_id": block_trade_id if block_trade_id else None,
        }
        self.env["dankbit.trade"].create(vals)
        _logger.info('*** Trade Created: %s (trade_id=%s) ***',
                    trade.get("instrument_name"), trade.get("trade_id"))

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
            domain=[("expiration", "<", fields.Datetime.now())]
        ).write({"active": False})

    def get_btc_option_name_for_yesterday(self):
        yesterday = datetime.now() - timedelta(days=1)
        instrument = f"BTC-{yesterday.day}{yesterday.strftime('%b').upper()}{yesterday.strftime('%y')}"
        return instrument

    # run by scheduled action
    def _take_screenshot(self):
        now = datetime.now().time()
        start = time(5, 0)   # 05:00
        end   = time(9, 0)   # 09:00

        if not (start <= now <= end):
            _logger.info("Skipping screenshot: outside time window.")
            return  # skip outside window
        
        btc_today = self.get_btc_option_name_for_yesterday()
        # Use configured base URL so this works both on dankbit.com and locally.
        icp = self.env['ir.config_parameter'].sudo()
        try:
            base_url = icp.get_base_url()
        except Exception:
            # fallback to param (older Odoo versions)
            base_url = icp.get_param('web.base.url', default='http://localhost:8069')

        # Build the URL robustly and allow local hosts.
        full_url = f"{base_url.rstrip('/')}/{btc_today}/mm/y"
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
                "dankbit_view_type": "be_taker",
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
                "dankbit_view_type": "be_mm",
            }
        }

class DankbitScreenshot(models.Model):
    _name = "dankbit.screenshot"
    _description = "Dankbit Screenshot"
    _order = "timestamp asc"

    name = fields.Char(required=True)
    timestamp = fields.Datetime(string="Timestamp", default=lambda self: fields.Datetime.now())
    image_png = fields.Binary(string="Chart Image", attachment=True)
