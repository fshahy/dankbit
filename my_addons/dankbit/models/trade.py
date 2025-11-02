# -*- coding: utf-8 -*-

import pytz
from datetime import datetime, timezone, timedelta
import logging
import requests, time

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
            _logger.warning("Deribit request failed (attempt %d/%d) %s %s: %s", attempt + 1, retries + 1, url, params, e)
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
            else:
                return None

class Trade(models.Model):
    _name = "dankbit.trade"
    _order = "deribit_ts desc"

    name = fields.Char(required=True)
    strike = fields.Integer(compute="_compute_strike", store=True)
    expiration = fields.Datetime()
    index_price = fields.Float(digits=(16, 4))
    price = fields.Float(digits=(16, 4), required=True)
    mark_price = fields.Float(digits=(16, 4), required=True)
    option_type = fields.Text(compute="_compute_type", store=True)
    direction = fields.Selection([("buy", "Buy"), ("sell", "Sell")], required=True)
    iv = fields.Float(string="IV %", digits=(2, 2), required=True)
    amount = fields.Float(digits=(6, 2), required=True)
    contracts = fields.Float(digits=(6, 2), required=True)
    deribit_ts = fields.Datetime()
    deribit_trade_identifier = fields.Float(digits=(15, 0), string="Deribit Trade ID", required=True)
    trade_seq = fields.Float(digits=(15, 0), required=True)
    days_to_expiry = fields.Integer(
        string="Days to Expiry",
        compute="_compute_days_to_expiry",
        store=True
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
            if rec.name[-1] == "P":
                rec.option_type = "put"
            elif rec.name[-1] == "C":
                rec.option_type = "call"

    @api.depends("name")
    def _compute_strike(self):
        for rec in self:
            rec.strike = rec.name.split("-")[2]

    def get_index_price(self):
        _logger.info("------------------- get_index_price -------------------")
        URL = "https://www.deribit.com/api/v2/public/get_index_price"
        params = {
            "index_name": "btc_usdt",
        }
        # read timeout from config (seconds)
        timeout = 5.0
        try:
            icp = self.env['ir.config_parameter'].sudo()
            timeout = float(icp.get_param('dankbit.deribit_timeout', default=5.0))
            cache_ttl = float(icp.get_param('dankbit.deribit_cache_ttl', default=30.0))
        except Exception:
            cache_ttl = 30.0
        # consult cache first
        now_ts = time.time()
        cached = _DERIBIT_CACHE.get('index_price', {})
        if cached and cached.get('value') is not None and (now_ts - cached.get('ts', 0) < cache_ttl):
            return cached.get('value')

        # perform request with retries/backoff
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

    def _get_latest_trade_ts(self):
        return self.search([], order="deribit_ts desc", limit=1)

    # run by scheduled action
    def get_last_trades(self):
        all_instruments = self._get_instruments()
        
        option_instruments = [
            inst for inst in all_instruments 
            if inst["kind"] == "option"
        ]

        icp = self.env['ir.config_parameter'].sudo()
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))

        # read timeout config
        timeout = 5.0
        try:
            timeout = float(icp.get_param('dankbit.deribit_timeout', default=5.0))
        except Exception:
            timeout = 5.0

        latest_trade_ts = self._get_latest_trade_ts()
        now_ts = int(time.time() * 1000)
        start_ts = None
        # I do not want to fetch unwanted data
        if latest_trade_ts: # we have some data
            start_ts = int(latest_trade_ts.deribit_ts.timestamp())
        else: # db is empty
            start_ts = self._get_midnight_dt(start_from_ts)

        if start_ts:
            URL = "https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time"
            for inst in option_instruments:
                params = {
                    "instrument_name": inst["instrument_name"],
                    "count": 1000,
                    "start_timestamp": start_ts,
                    "end_timestamp": now_ts,
                    "sorting": "desc"
                }
                try:
                    data = _safe_deribit_request(URL, params=params, timeout=timeout)
                except Exception as e:
                    _logger.exception("get_last_trades: request failed for %s: %s", inst.get('instrument_name'), e)
                    data = None

                # small sleep to avoid hammering the API in a tight loop
                time.sleep(0.05)

                if "result" in data:
                    trades = data["result"].get("trades", [])
                    for trd in trades:
                        self._create_new_trade(trd, inst["expiration_timestamp"])
                
                # commit to db before going to next instrument
                self.env.cr.commit()

    def _get_tomorrows_ts(self):
        # Current UTC time
        now = datetime.now(pytz.utc)

        # Tomorrow's date
        tomorrow = now.date() + timedelta(days=1)

        # Tomorrow at 08:00 GMT
        target = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, 0, 0, tzinfo=timezone.utc)

        # Convert to milliseconds since epoch
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
        # consult cache
        now_ts = time.time()
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
            domain=[("deribit_trade_identifier", "=", trade["trade_id"])],
            limit=1
        )

        icp = self.env['ir.config_parameter'].sudo()
        start_from_ts = int(icp.get_param("dankbit.from_days_ago", default=2))

        start_ts = self._get_midnight_dt(start_from_ts)

        if not exists and trade["timestamp"] > start_ts:
            self.env["dankbit.trade"].create({
                "name": trade["instrument_name"],
                "iv": trade["iv"],
                "index_price": trade["index_price"],
                "price": trade["price"],
                "mark_price": trade["mark_price"],
                "direction": trade["direction"],
                "trade_seq": trade["trade_seq"],
                "deribit_trade_identifier": trade["trade_id"],
                "amount": trade["amount"],
                "contracts": trade["contracts"],
                "deribit_ts": datetime.fromtimestamp(trade["timestamp"]/1000).strftime('%Y-%m-%d %H:%M:%S'),
                "expiration": datetime.fromtimestamp(expiration_ts/1000).strftime('%Y-%m-%d %H:%M:%S'),
            })
            _logger.info(f'*** Trade Created: {trade["instrument_name"]} ***')

    @staticmethod
    def _get_midnight_dt(days_offset=0):
        """
        Return a timezone-aware datetime (UTC) for midnight with optional day offset.
        Compatible with PostgreSQL and Odoo domains.
        
        Example:
            _get_midnight_dt()    → today's midnight UTC
            _get_midnight_dt(-1)  → yesterday's midnight UTC
        """
        now = datetime.now(timezone.utc)
        midnight = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=timezone.utc)
        return int((midnight + timedelta(days=-days_offset)).timestamp()) * 1000

    # run by scheduled action
    def _delete_expired_trades(self):
        self.env['dankbit.trade'].search(
            domain=[("expiration", "<", fields.Datetime.now())]
        ).unlink()

    def get_btc_option_name_for_today(self):
        tomorrow = datetime.now() + timedelta(days=1)
        instrument = f"BTC-{tomorrow.day}{tomorrow.strftime('%b').upper()}{tomorrow.strftime('%y')}"
        return instrument
    
    # run by scheduled action
    def _take_screenshot(self):
        btc_today = self.get_btc_option_name_for_today()
        base_url = self.env['ir.config_parameter'].sudo().get_base_url()
        full_url = f"https://dankbit.com/{btc_today}/mm/y"
        _logger.info(full_url)
        try:
            response = requests.get(full_url, timeout=1)
            response.raise_for_status()
            self.env.cr.commit()
            _msg = f"✅ Called {full_url} — {response.status_code}"
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
