import gzip
# import os
import math

import numpy as np
from datetime import datetime, timedelta
from io import BytesIO
import logging
from odoo import fields, http
from odoo.http import request
from . import options
from . import delta
from . import gamma
import requests, time
from zoneinfo import ZoneInfo
import matplotlib.pyplot as plt


_logger = logging.getLogger(__name__)

class ChartController(http.Controller):
    @http.route("/<string:instrument>/<string:veiw_type>/<int:hours_ago>", type="http", auth="public", website=True)
    def chart_png(self, instrument, veiw_type, hours_ago):
        icp = request.env['ir.config_parameter'].sudo()

        from_price = float(icp.get_param("dankbit.from_price", default=110000))
        to_price = float(icp.get_param("dankbit.to_price", default=130000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        index_price = request.env['dankbit.trade'].sudo().get_index_price()

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

        fig = obj.plot(index_price, market_deltas, market_gammas, veiw_type)

        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0)
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

    @http.route('/<string:today_instrument>/<int:hours_ago>', auth='public', type='http', website=True)
    def chart_png_iframe(self, today_instrument, hours_ago):
        vals = {
            "today": today_instrument,
            "hours_ago": hours_ago,
        }
        return request.render('dankbit.image_dashboard', vals)
    
    @http.route("/<string:instrument>/<string:veiw_type>/last", type="http", auth="public", website=True)
    def chart_from_png(self, instrument, veiw_type):
        icp = request.env['ir.config_parameter'].sudo()

        from_price = float(icp.get_param("dankbit.from_price", default=110000))
        to_price = float(icp.get_param("dankbit.to_price", default=130000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        last_hedging_time = icp.get_param("dankbit.last_hedging_time")

        index_price = request.env['dankbit.trade'].sudo().get_index_price()

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", last_hedging_time)
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

        fig = obj.plot(index_price, market_deltas, market_gammas, veiw_type)

        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0)
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

    @http.route('/<string:today_instrument>/last', auth='public', type='http', website=True)
    def chart_from_png_iframe(self, today_instrument):
        vals = {
            "today": today_instrument,
            "hours_ago": "last",
        }
        return request.render('dankbit.image_dashboard', vals)
    
    @http.route("/<string:instrument>/<string:veiw_type>/day", type="http", auth="public", website=True)
    def chart_png_day(self, instrument, veiw_type):
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.day_from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.day_to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        index_price = request.env['dankbit.trade'].sudo().get_index_price()

        tz = ZoneInfo("Europe/Berlin")
        now = datetime.now(tz)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", midnight)
            ]
        )

        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)
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

        STs = np.arange(day_from_price, day_to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)

        fig = obj.plot(index_price, market_deltas, market_gammas, veiw_type, width=18, height=8)

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