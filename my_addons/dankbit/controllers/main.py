# controllers/main.py
import gzip
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
import matplotlib.pyplot as plt


_logger = logging.getLogger(__name__)

class ChartController(http.Controller):
    @http.route("/i/<string:instrument>/<string:veiw_type>/<int:hours_ago>", type="http", auth="public", website=True, csrf=False)
    def chart_png(self, instrument, veiw_type, hours_ago):
        from_price = 110000.00
        to_price = 130000.00
        step = 100
        index_price = request.env['dankbit.trade'].sudo().get_index_price()
        if hours_ago == 0:
            hours_ago == 24

        now_ms = int(time.time() * 1000)
        # ago_in_ms = now_ms - (hours_ago * 60 * 60 * 1000) # created in last 24 hours
        from_time = datetime.now() - timedelta(hours=hours_ago)

        tz = ZoneInfo("Europe/Berlin")
        now = datetime.now(tz)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_yesterday = midnight - timedelta(days=1)


        if hours_ago == 0:
            from_time = midnight_yesterday


        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", from_time)
            ]
        )

        obj = options.OptionStrat(instrument, index_price, from_price, to_price, step)
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

        STs = np.arange(from_price, to_price, step)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)

        fig = obj.plot(index_price, market_deltas, market_gammas, veiw_type, hours_ago)

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)

        # berlin_time = datetime.now(ZoneInfo("Europe/Berlin"))
        # filename = berlin_time.strftime("%Y_%m_%d_%H_%M")
        # os.makedirs(f"/mnt/screenshots/{instrument}", exist_ok=True)
        # with open(f"/mnt/screenshots/{instrument}/{filename}.png", "wb") as screenshot:
        #     screenshot.write(buf.getvalue())

        # compress with gzip
        png_data = buf.getvalue()
        compressed_data = gzip.compress(png_data)

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Encoding", "gzip"),
            ("Refresh", 60),
        ]
        return request.make_response(compressed_data, headers=headers)

    # @http.route('/i/clear', type='http', csrf=False)
    # def clear_trades2(self):
    #     request.env['dankbit.trade'].search(
    #         domain=[]
    #     ).unlink()

    #     return request.redirect('/odoo/')