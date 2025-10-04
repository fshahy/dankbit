# controllers/main.py
import os

import numpy as np
from datetime import datetime, timedelta
from io import BytesIO
import logging
from odoo import http
from odoo.http import request
from . import options
from . import delta
from . import gamma
import requests, time
from zoneinfo import ZoneInfo


_logger = logging.getLogger(__name__)

class ChartController(http.Controller):
    @http.route("/i/<string:instrument>/<string:veiw_type>/<int:hours_ago>", type="http", auth="public", website=True, csrf=False)
    def chart_png(self, instrument, veiw_type, hours_ago):
        if hours_ago == 0:
            hours_ago == 24

        all_instruments = request.env["dankbit.trade"]._get_instruments()

        selected_expiry = [
            inst for inst in all_instruments 
            if inst["instrument_name"].startswith(instrument) and 
            inst["kind"] == "option"
        ]

        now_ms = int(time.time() * 1000)
        ago_in_ms = now_ms - (hours_ago * 60 * 60 * 1000) # created in last 24 hours
        URL = "https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time"

        for inst in selected_expiry:
            params = {
                "instrument_name": inst["instrument_name"],
                "count": 1000,
                "start_timestamp": ago_in_ms,
                "end_timestamp": now_ms,
                "sorting": "desc"
            }
            resp = requests.get(URL, params=params).json()

            if "result" in resp:
                trades = resp["result"]["trades"]
                
                for trd in trades:
                    request.env['dankbit.trade']._create_new_trade(trd, inst["expiration_timestamp"])
        # -----------------
        index_price = request.env['dankbit.trade'].get_index_price()

        from_time = datetime.now() - timedelta(hours=hours_ago)
        #---------------
        tz = ZoneInfo("Europe/Berlin")
        now = datetime.now(tz)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # yesterday_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        if hours_ago == 0:
            from_time = midnight
        #---------------

        trades = request.env['dankbit.trade'].search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", from_time)
            ]
        )

        obj = options.OptionStrat(instrument, index_price)
        is_call = []

        for trade in trades:
            if trade.option_type == "call":
                is_call.append(True)
                if trade.direction == "buy":
                    obj.long_call(trade.strike, trade.price)
                elif trade.direction == "sell":
                    obj.short_call(trade.strike, trade.price)
            elif trade.option_type == "put":
                is_call.append(False)
                if trade.direction == "buy":
                    obj.long_put(trade.strike, trade.price)
                elif trade.direction == "sell":
                    obj.short_put(trade.strike, trade.price)

        STs = np.arange(index_price-10000, index_price+10000, 100)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)

        fig = obj.plot(index_price, market_deltas, market_gammas, veiw_type, hours_ago)

        buf = BytesIO()
        fig.savefig(buf, format="png")

        berlin_time = datetime.now(ZoneInfo("Europe/Berlin"))
        filename = berlin_time.strftime("%Y_%m_%d_%H_%M")
        os.makedirs(f"/mnt/screenshots/{instrument}", exist_ok=True)
        with open(f"/mnt/screenshots/{instrument}/{filename}.png", "wb") as screenshot:
            screenshot.write(buf.getvalue())

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Refresh", 120),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route('/i/clear', type='http', csrf=False)
    def clear_trades2(self):
        request.env['dankbit.trade'].search(
            domain=[]
        ).unlink()

        return request.redirect('/odoo/')