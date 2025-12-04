import base64
import numpy as np
import time
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

_INDEX_CACHE = {
    "timestamp": 0,
    "price": None,
}

_CACHE_TTL = 120 # in seconds

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
    
    @staticmethod
    def get_ts_from_hour(from_hour):
        tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        from_hour_ts = now.replace(hour=from_hour, minute=0, second=0, microsecond=0)
        return from_hour_ts
    
    @http.route('/help', auth='public', type='http', website=True)
    def help_page(self):
        return request.render('dankbit.dankbit_help')

    @http.route([
        "/<string:instrument>/c",
        "/<string:instrument>/c/<int:from_hour>",
        ], type="http", auth="public", website=True)
    def chart_png_calls(self, instrument, from_hour=0):
        plot_title = "calls"
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        last_hedging_time = icp.get_param("dankbit.last_hedging_time")
        mock_0dte = icp.get_param('dankbit.mock_0dte')
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))
        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        if last_hedging_time:
            start_ts = last_hedging_time

        if from_hour:
            start_ts = self.get_ts_from_hour(from_hour)
            plot_title = f"{plot_title} from {str(from_hour)}:00 UTC"

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("option_type", "=", "call"),
                ("deribit_ts", ">=", start_ts),
                ("is_block_trade", "=", False),
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "taker", show_red_line, strike=plot_title)
        
        ax.text(
            0.01, 0.02,
            f"{len(trades)} trades",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0) 

        request.env["dankbit.screenshot"].sudo().create({
            "name": f"{instrument} - calls",
            "timestamp": fields.Datetime.now(),
            "image_png": base64.b64encode(buf.read()),
        })

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_calls.png"'),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route([
        "/<string:instrument>/p",
        "/<string:instrument>/p/<int:from_hour>",
        ], type="http", auth="public", website=True)
    def chart_png_puts(self, instrument, from_hour=0):
        plot_title = "puts"
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        last_hedging_time = icp.get_param("dankbit.last_hedging_time")
        mock_0dte = icp.get_param('dankbit.mock_0dte')
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))
        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        if last_hedging_time:
            start_ts = last_hedging_time

        if from_hour:
            start_ts = self.get_ts_from_hour(from_hour)
            plot_title = f"{plot_title} from {str(from_hour)}:00 UTC"

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("option_type", "=", "put"),
                ("deribit_ts", ">=", start_ts),
                ("is_block_trade", "=", False),
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "taker", show_red_line, strike=plot_title)
        
        ax.text(
            0.01, 0.02,
            f"{len(trades)} trades",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0) 

        request.env["dankbit.screenshot"].sudo().create({
            "name": f"{instrument} - puts",
            "timestamp": fields.Datetime.now(),
            "image_png": base64.b64encode(buf.read()),
        })

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_puts.png"'),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(buf.getvalue(), headers=headers)
    
    @http.route([
        "/<string:instrument>/b",
        "/<string:instrument>/b/<int:from_hour>",
        ], type="http", auth="public", website=True)
    def chart_png_buys(self, instrument, from_hour=0):
        plot_title = "buys"
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        last_hedging_time = icp.get_param("dankbit.last_hedging_time")
        mock_0dte = icp.get_param('dankbit.mock_0dte')
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))
        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        if last_hedging_time:
            start_ts = last_hedging_time

        if from_hour:
            start_ts = self.get_ts_from_hour(from_hour)
            plot_title = f"{plot_title} from {str(from_hour)}:00 UTC"

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("direction", "=", "buy"),
                ("deribit_ts", ">=", start_ts),
                ("is_block_trade", "=", False),
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "taker", show_red_line, strike=plot_title)
        
        ax.text(
            0.01, 0.02,
            f"{len(trades)} trades",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0) 

        request.env["dankbit.screenshot"].sudo().create({
            "name": f"{instrument} - buys",
            "timestamp": fields.Datetime.now(),
            "image_png": base64.b64encode(buf.read()),
        })

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_buys.png"'),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(buf.getvalue(), headers=headers)
    
    @http.route([
        "/<string:instrument>/s",
        "/<string:instrument>/s/<int:from_hour>",
        ], type="http", auth="public", website=True)
    def chart_png_sells(self, instrument, from_hour=0):
        plot_title = "sells"
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        last_hedging_time = icp.get_param("dankbit.last_hedging_time")
        mock_0dte = icp.get_param('dankbit.mock_0dte')
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))
        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        if last_hedging_time:
            start_ts = last_hedging_time

        if from_hour:
            start_ts = self.get_ts_from_hour(from_hour)
            plot_title = f"{plot_title} from {str(from_hour)}:00 UTC"

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("direction", "=", "sell"),
                ("deribit_ts", ">=", start_ts),
                ("is_block_trade", "=", False),
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "taker", show_red_line, strike=plot_title)
        
        ax.text(
            0.01, 0.02,
            f"{len(trades)} trades",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0) 

        request.env["dankbit.screenshot"].sudo().create({
            "name": f"{instrument} - sells",
            "timestamp": fields.Datetime.now(),
            "image_png": base64.b64encode(buf.read()),
        })

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_sells.png"'),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route("/<string:instrument>/strike/<int:strike>", type="http", auth="public", website=True)
    def chart_png_strike(self, instrument, strike):
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        last_hedging_time = icp.get_param("dankbit.last_hedging_time")
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))
        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        if last_hedging_time:
            start_ts = last_hedging_time

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("strike", "=", int(strike)),
                ("deribit_ts", ">=", start_ts),
                ("is_block_trade", "=", False),
            ]
        )

        now = time.time()
        if _INDEX_CACHE["price"] and (now - _INDEX_CACHE["timestamp"] < _CACHE_TTL):
            index_price = _INDEX_CACHE["price"]
        else:
            index_price = request.env['dankbit.trade'].sudo().get_index_price()
            _INDEX_CACHE["price"] = index_price
            _INDEX_CACHE["timestamp"] = now

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

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "mm", show_red_line, strike=int(strike))
        
        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_{strike}.png"'),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route([
        "/<string:instrument>/<string:view_type>", 
        "/<string:instrument>/<string:view_type>/l/<int:hours_ago>", 
        "/<string:instrument>/<string:view_type>/<int:from_hour>", 
    ], type="http", auth="public", website=True)
    def chart_png_day(self, instrument, view_type, from_hour=0, hours_ago=0):
        plot_title = view_type
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        last_hedging_time = icp.get_param("dankbit.last_hedging_time")
        mock_0dte = icp.get_param('dankbit.mock_0dte')
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))
        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        if last_hedging_time:
            start_ts = last_hedging_time

        if from_hour:
            start_ts = self.get_ts_from_hour(from_hour)
            plot_title = f"{plot_title} from {str(from_hour)}:00 UTC"

        if hours_ago:
            start_ts = datetime.now() - timedelta(hours=hours_ago)
            plot_title = f"{plot_title} last {str(hours_ago)} hours"

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts),
                ("is_block_trade", "=", False),
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, view_type, show_red_line, strike=plot_title)
        
        ax.text(
            0.01, 0.02,
            f"{len(trades)} trades",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0) 

        request.env["dankbit.screenshot"].sudo().create({
            "name": f"{instrument} - {plot_title}",
            "timestamp": fields.Datetime.now(),
            "image_png": base64.b64encode(buf.read()),
        })

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_{view_type}_{from_hour}H_day.png"'),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(buf.getvalue(), headers=headers)


    @http.route("/<string:instrument>/<string:view_type>/a", type="http", auth="public", website=True)
    def chart_png_all(self, instrument, view_type):
        plot_title = f"{view_type} all"
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        mock_0dte = icp.get_param('dankbit.mock_0dte')
        show_red_line = icp.get_param("dankbit.show_red_line")

        trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("is_block_trade", "=", False),
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, view_type, show_red_line, strike=plot_title)
        
        ax.text(
            0.01, 0.02,
            f"{len(trades)} trades",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0) 

        request.env["dankbit.screenshot"].sudo().create({
            "name": f"{instrument} - all",
            "timestamp": fields.Datetime.now(),
            "image_png": base64.b64encode(buf.read()),
        })

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_{view_type}_all.png"'),
            ("Refresh", refresh_interval*5),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route("/<string:instrument>/z", type="http", auth="public", website=True)
    def chart_png_zones(self, instrument):
        icp = request.env['ir.config_parameter'].sudo()

        zone_from_price = float(icp.get_param("dankbit.zone_from_price", default=100000))
        zone_to_price = float(icp.get_param("dankbit.zone_to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        last_hedging_time = icp.get_param("dankbit.last_hedging_time")
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))
        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        if last_hedging_time:
            start_ts = last_hedging_time

        long_trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("direction", "=", "buy"),
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts),
                ("is_block_trade", "=", False),
            ]
        )

        short_trades = request.env['dankbit.trade'].sudo().search(
            domain=[
                ("direction", "=", "sell"),
                ("name", "ilike", f"{instrument}"),
                ("deribit_ts", ">=", start_ts),
                ("is_block_trade", "=", False),
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

        fig, ax = obj.plot_zones(index_price)

        ax.text(
            0.01, 0.02,
            f"{len(long_trades)} longs - {len(short_trades)} shorts",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0) 

        request.env["dankbit.screenshot"].sudo().create({
            "name": f"{instrument} - zones",
            "timestamp": fields.Datetime.now(),
            "image_png": base64.b64encode(buf.read()),
        })

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_zones.png"'),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route([
        "/<string:instrument>/oi",
        "/<string:instrument>/oi/<int:from_hour>",
        ], type="http", auth="public", website=True)
    def chart_png_oi(self, instrument, from_hour=0):
        plot_title = "OI"
        icp = request.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000)) + 10000.0 # +1000 to have more space in oi view
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000)) - 10000.0 # -1000 to have more space in oi view
        steps = int(icp.get_param("dankbit.steps", default=100))
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        last_hedging_time = icp.get_param("dankbit.last_hedging_time")
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))
        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        if last_hedging_time:
            start_ts = last_hedging_time

        if from_hour:
            start_ts = self.get_ts_from_hour(from_hour)
            plot_title = f"{plot_title} from {str(from_hour)}:00 UTC"

        oi_data = []
        for strike in range(int(day_from_price), int(day_to_price), 1000):
            trades = request.env['dankbit.trade'].sudo().search(
                domain=[
                    ("name", "ilike", f"{instrument}"),
                    ("strike", "=", strike),
                    ("deribit_ts", ">=", start_ts),
                    ("is_block_trade", "=", False),
                ]
            )
            oi_call, oi_put = oi.calculate_oi(strike, trades)
            oi_data.append([strike, oi_call, oi_put])


        index_price = request.env['dankbit.trade'].sudo().get_index_price()
        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)

        fig = obj.plot_oi(index_price, oi_data, plot_title)

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0) 

        request.env["dankbit.screenshot"].sudo().create({
            "name": f"{instrument} - oi",
            "timestamp": fields.Datetime.now(),
            "image_png": base64.b64encode(buf.read()),
        })

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_oi.png"'),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route("/<string:instrument>/sts", type="http", auth="public")
    def plot_scrollable_strikes_auto(self, instrument):
        """
        Fullscreen strike viewer — supports:
        - Mouse wheel
        - Keyboard arrows
        - Touch swipe (left/right or up/down)
        """
        env = request.env
        icp = env['ir.config_parameter'].sudo()
        Trade = env["dankbit.trade"].sudo()
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))
        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        strikes = sorted(set(Trade.search([
            ("name", "ilike", instrument),
            ("deribit_ts", ">=", start_ts),
        ]).mapped("strike")))

        if not strikes:
            return f"<h3>No strikes found for {instrument}</h3>"

        # Cache and index price
        now = time.time()
        if _INDEX_CACHE["price"] and (now - _INDEX_CACHE["timestamp"] < _CACHE_TTL):
            index_price = _INDEX_CACHE["price"]
        else:
            index_price = Trade.get_index_price()
            _INDEX_CACHE["price"] = index_price
            _INDEX_CACHE["timestamp"] = now

        # Find starting strike (lowest below index)
        lower_strikes = [s for s in strikes if s <= index_price]
        start_strike = max(lower_strikes) if lower_strikes else strikes[0]
        start_index = strikes.index(start_strike)
        img_urls = [f"/{instrument}/strike/{s}" for s in strikes]

        html = f"""
        <html>
            <head>
                <title>{instrument} Strike Viewer (start {start_strike})</title>
                <style>
                    html, body {{
                        margin: 0;
                        padding: 0;
                        height: 100%;
                        width: 100%;
                        background-color: #e6e6e6;
                        overflow: hidden;
                        font-family: Arial, sans-serif;
                        color: #333;
                        -webkit-user-select: none;
                        -ms-user-select: none;
                        user-select: none;
                        touch-action: pan-y pinch-zoom;
                    }}
                    .viewer {{
                        position: relative;
                        width: 100%;
                        height: 100%;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        overflow: hidden;
                    }}
                    .viewer img {{
                        max-width: 100%;
                        max-height: 100%;
                        object-fit: contain;
                        border: none;
                        transition: opacity 0.25s ease-in-out;
                        touch-action: none;
                    }}
                    .label {{
                        position: absolute;
                        top: 10px;
                        left: 50%;
                        transform: translateX(-50%);
                        background: rgba(255,255,255,0.8);
                        padding: 6px 14px;
                        border-radius: 6px;
                        font-weight: bold;
                        font-size: 18px;
                    }}
                    .help {{
                        position: absolute;
                        bottom: 10px;
                        left: 50%;
                        transform: translateX(-50%);
                        font-size: 14px;
                        color: #444;
                        opacity: 0.7;
                        background: rgba(255,255,255,0.5);
                        padding: 4px 8px;
                        border-radius: 4px;
                    }}
                </style>
            </head>
            <body>
                <div class="viewer">
                    <img id="strikeImage" src="{img_urls[start_index]}" alt="strike image" />
                    <div class="label" id="strikeLabel">{strikes[start_index]}</div>
                    <div class="help">Swipe ◀▶ or scroll ↑↓ or use ← → keys</div>
                </div>

                <script>
                    const strikes = {strikes};
                    const urls = {img_urls};
                    let index = {start_index};
                    const img = document.getElementById('strikeImage');
                    const label = document.getElementById('strikeLabel');

                    function showStrike(newIndex) {{
                        if (newIndex < 0) newIndex = urls.length - 1;
                        if (newIndex >= urls.length) newIndex = 0;
                        img.style.opacity = 0;
                        setTimeout(() => {{
                            img.src = urls[newIndex];
                            label.textContent = strikes[newIndex];
                            img.style.opacity = 1;
                            index = newIndex;
                        }}, 150);
                    }}

                    // Mouse wheel
                    window.addEventListener('wheel', (e) => {{
                        if (e.deltaY < 0) showStrike(index + 1);
                        else if (e.deltaY > 0) showStrike(index - 1);
                    }}, {{ passive: true }});

                    // Keyboard
                    window.addEventListener('keydown', (e) => {{
                        if (['ArrowRight', 'ArrowUp'].includes(e.key)) showStrike(index + 1);
                        if (['ArrowLeft', 'ArrowDown'].includes(e.key)) showStrike(index - 1);
                    }});

                    // Touch gestures
                    let touchStartX = 0, touchStartY = 0;
                    let touchEndX = 0, touchEndY = 0;

                    function handleSwipe() {{
                        const dx = touchEndX - touchStartX;
                        const dy = touchEndY - touchStartY;
                        if (Math.abs(dx) > Math.abs(dy)) {{
                            if (dx > 30) showStrike(index - 1);   // swipe right → prev
                            else if (dx < -30) showStrike(index + 1);  // swipe left → next
                        }} else {{
                            if (dy > 30) showStrike(index - 1);   // swipe down → prev
                            else if (dy < -30) showStrike(index + 1);  // swipe up → next
                        }}
                    }}

                    img.addEventListener('touchstart', (e) => {{
                        const t = e.changedTouches[0];
                        touchStartX = t.screenX;
                        touchStartY = t.screenY;
                    }}, {{ passive: true }});

                    img.addEventListener('touchend', (e) => {{
                        const t = e.changedTouches[0];
                        touchEndX = t.screenX;
                        touchEndY = t.screenY;
                        handleSwipe();
                    }}, {{ passive: true }});
                </script>
            </body>
        </html>
        """
        return html
