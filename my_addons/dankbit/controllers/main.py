import gzip
import base64
import numpy as np
import os
from datetime import datetime, timezone, timedelta
from io import BytesIO
import logging
from odoo import fields, http
from odoo.http import request
from . import options
from . import delta
from . import gamma
from . import oi
from zoneinfo import ZoneInfo
import matplotlib.pyplot as plt


_logger = logging.getLogger(__name__)

class ChartController(http.Controller):
    @staticmethod
    def _get_today_midnight_ts():
        # Current date in UTC
        today = datetime.now(timezone.utc).date()

        # Midnight today in UTC
        midnight = datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=timezone.utc)

        # Unix timestamp
        return int(midnight.timestamp()) * 1000    

    @staticmethod
    def _get_yesterday_midnight_ts():
        # Current UTC date
        today = datetime.now(timezone.utc).date()

        # Yesterday’s date
        yesterday = today - timedelta(days=1)

        # Build yesterday’s midnight in UTC
        midnight = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc)

        # Return Unix timestamp in milliseconds
        return int(midnight.timestamp() * 1000)

    @staticmethod
    def get_midnight_ts(days_offset=0):
        tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        target_day = now + timedelta(days=-days_offset)
        midnight = target_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight

    @http.route('/help', auth='public', type='http', website=True)
    def help_page(self):
        return request.render('dankbit.dankbit_help')

    @http.route("/<string:instrument>/calls", type="http", auth="public", website=True)
    def chart_png_calls(self, instrument):
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))

        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts),
                ("option_type", "=", "call"),
            ]
        )

        index_price = request.env['dankbit.trade'].sudo().get_index_price()
        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)
        is_call = []

        for trade in trades:
            is_call.append(True)
            if trade.direction == "buy":
                obj.long_call(trade.strike, trade.price * trade.index_price)
            elif trade.direction == "sell":
                obj.short_call(trade.strike, trade.price * trade.index_price)

        STs = np.arange(day_from_price, day_to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)

        fig = obj.plot(index_price, market_deltas, market_gammas, "taker", show_red_line, "Calls", width=18, height=8)
        
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

    @http.route("/<string:instrument>/puts", type="http", auth="public", website=True)
    def chart_png_puts(self, instrument):
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))

        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts),
                ("option_type", "=", "put")
            ]
        )

        index_price = request.env['dankbit.trade'].sudo().get_index_price()
        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)
        is_call = []

        for trade in trades:
            is_call.append(False)
            if trade.direction == "buy":
                obj.long_put(trade.strike, trade.price * trade.index_price)
            elif trade.direction == "sell":
                obj.short_put(trade.strike, trade.price * trade.index_price)

        STs = np.arange(day_from_price, day_to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)

        fig = obj.plot(index_price, market_deltas, market_gammas, "taker", show_red_line, "Puts", width=18, height=8)
        
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
    
    @http.route("/<string:instrument>/buys", type="http", auth="public", website=True)
    def chart_png_buys(self, instrument):
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))

        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts),
                ("direction", "=", "buy")
            ]
        )

        index_price = request.env['dankbit.trade'].sudo().get_index_price()
        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)
        is_call = []

        for trade in trades:
            if trade.option_type == "call":
                is_call.append(True)
                obj.long_call(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                is_call.append(False)
                obj.long_put(trade.strike, trade.price * trade.index_price)

        STs = np.arange(day_from_price, day_to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)

        fig = obj.plot(index_price, market_deltas, market_gammas, "taker", show_red_line, "Buys", width=18, height=8)
        
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
    
    @http.route("/<string:instrument>/sells", type="http", auth="public", website=True)
    def chart_png_sells(self, instrument):
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))

        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts),
                ("direction", "=", "sell")
            ]
        )

        index_price = request.env['dankbit.trade'].sudo().get_index_price()
        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)
        is_call = []

        for trade in trades:
            if trade.option_type == "call":
                is_call.append(True)
                obj.short_call(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                is_call.append(False)
                obj.short_put(trade.strike, trade.price * trade.index_price)

        STs = np.arange(day_from_price, day_to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)

        fig = obj.plot(index_price, market_deltas, market_gammas, "taker", show_red_line, "Sells", width=18, height=8)
        
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

    @http.route([
        "/<string:instrument>/<string:veiw_type>", 
        "/<string:instrument>/<string:veiw_type>/<int:hours_ago>",
        "/<string:instrument>/<string:veiw_type>/<string:take_screenshot>"
    ], type="http", auth="public", website=True)
    def chart_png_day(self, instrument, veiw_type, hours_ago=None, take_screenshot=None):
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))

        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        if hours_ago:
            start_ts = datetime.now() - timedelta(hours=hours_ago)

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts)
            ]
        )

        index_price = request.env['dankbit.trade'].sudo().get_index_price()
        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)
        is_call = []

        for trade in trades:
            if trade.option_type == "call":
                is_call.append(True)
                if trade.direction == "buy":
                    obj.long_call(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    obj.short_call(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                is_call.append(False)
                if trade.direction == "buy":
                    obj.long_put(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    obj.short_put(trade.strike, trade.price * trade.index_price)

        STs = np.arange(day_from_price, day_to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)

        if not hours_ago:
            hours_ago = "Daily"
        else:
            hours_ago = f"{hours_ago}H"

        fig = obj.plot(index_price, market_deltas, market_gammas, veiw_type, show_red_line, hours_ago, width=18, height=8)
        
        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0) 

        if take_screenshot and take_screenshot in ["y", "Y"]:
            request.env["dankbit.screenshot"].sudo().create({
                "name": instrument,
                "timestamp": fields.Datetime.now(),
                "image_png": base64.b64encode(buf.read()),
            })

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
    
    @http.route("/<string:instrument>/strike/<int:strike>", type="http", auth="public", website=True)
    def chart_png_strike(self, instrument, strike):
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))

        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts),
                ("strike", "=", int(strike)),
            ]
        )

        index_price = request.env['dankbit.trade'].sudo().get_index_price()
        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)
        is_call = []

        for trade in trades:
            if trade.option_type == "call":
                is_call.append(True)
                if trade.direction == "buy":
                    obj.long_call(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    obj.short_call(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                is_call.append(False)
                if trade.direction == "buy":
                    obj.long_put(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    obj.short_put(trade.strike, trade.price * trade.index_price)

        STs = np.arange(day_from_price, day_to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)

        fig = obj.plot(index_price, market_deltas, market_gammas, "mm", show_red_line, f"Strike {strike}", width=18, height=8)
        
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

    @http.route("/<string:instrument>/zones", type="http", auth="public", website=True)
    def chart_png_zones(self, instrument):
        icp = request.env['ir.config_parameter'].sudo()

        zone_from_price = float(icp.get_param("dankbit.zone_from_price", default=100000))
        zone_to_price = float(icp.get_param("dankbit.zone_to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))

        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        long_trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("direction", "=", "buy"),
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts)
            ]
        )

        short_trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("direction", "=", "sell"),
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts)
            ]
        )

        index_price = request.env['dankbit.trade'].sudo().get_index_price()
        obj = options.OptionStrat(instrument, index_price, zone_from_price, zone_to_price, steps)
        is_call = []

        for trade in long_trades:
            if trade.option_type == "call":
                is_call.append(True)
                obj.add_call_to_longs(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                is_call.append(False)
                obj.add_put_to_longs(trade.strike, trade.price * trade.index_price)

        for trade in short_trades:
            if trade.option_type == "call":
                is_call.append(True)
                obj.add_call_to_shorts(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                is_call.append(False)
                obj.add_put_to_shorts(trade.strike, trade.price * trade.index_price)

        fig = obj.plot_zones(index_price)

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

    @http.route("/<string:instrument>/oi", type="http", auth="public", website=True)
    def chart_png_oi(self, instrument):
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000)) + 10000.0 # +1000 to have more space in oi view
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000)) - 10000.0 # -1000 to have more space in oi view
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))

        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        oi_data = []
        for strike in range(int(day_from_price), int(day_to_price), 1000):
            trades = request.env['dankbit.trade'].sudo().search(
                domain=[
                    ("name", "ilike", f"{instrument}"),
                    ("deribit_ts", ">=", start_ts),
                    ("strike", "=", strike),
                ]
            )
            oi_call, oi_put = oi.calculate_oi(strike, trades)
            oi_data.append([strike, oi_call, oi_put])


        index_price = request.env['dankbit.trade'].sudo().get_index_price()
        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)

        fig = obj.plot_oi(index_price, oi_data)

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
