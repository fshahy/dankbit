import gzip
# import os
import math

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
    @http.route("/i/<string:instrument>/<string:veiw_type>/<int:hours_ago>", type="http", auth="public", website=True)
    def chart_png(self, instrument, veiw_type, hours_ago):
        icp = request.env['ir.config_parameter'].sudo()

        from_price = float(icp.get_param("dankbit.from_price", default=110000))
        to_price = float(icp.get_param("dankbit.to_price", default=130000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        index_price = request.env['dankbit.trade'].sudo().get_index_price()

        if hours_ago == 0:
            hours_ago == 24

        from_time = datetime.now() - timedelta(hours=hours_ago)

        tz = ZoneInfo("Europe/Berlin")
        now = datetime.now(tz)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # midnight_yesterday = midnight - timedelta(days=1)

        if hours_ago == 0:
            from_time = midnight

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", from_time)
            ]
        )

        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
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

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)

        fig = obj.plot(index_price, market_deltas, market_gammas, veiw_type, hours_ago)

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)

        # compress with gzip
        png_data = buf.getvalue()
        compressed_data = gzip.compress(png_data)

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Encoding", "gzip"),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(compressed_data, headers=headers)

    @http.route('/i/<string:today_instrument>', auth='public', type='http', website=True)
    def iframe_dashboard(self, today_instrument):
        vals = {
            "today": today_instrument,
        }
        return request.render('dankbit.image_dashboard', vals)