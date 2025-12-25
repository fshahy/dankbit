import numpy as np
from datetime import datetime, timedelta
from io import BytesIO
import logging
from odoo import http
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
    
    @http.route("/help", auth="public", type="http", website=True)
    def help_page(self):
        return request.render("dankbit.dankbit_help")

    @http.route("/<string:instrument>", type="http", auth="public", website=True)
    def instrument_home_page(self, instrument):
        values = {
            "instrument": instrument,
        }
        return request.render("dankbit.dankbit_instrument_home_page", values)

    @http.route([
        "/<string:instrument>/<string:view_type>", 
        "/<string:instrument>/<string:view_type>/l/<int:minutes_ago>", 
        "/<string:instrument>/<string:view_type>/<int:from_hour>", 
    ], type="http", auth="public", website=True)
    def chart_png_day(self, instrument, view_type, from_hour=0, minutes_ago=0, **params):
        if view_type not in ["taker", "mm"]:
            return f"<h3>Nothing here.</h3>"
        
        plot_title = view_type
        icp = request.env["ir.config_parameter"].sudo()

        day_from_price = 0
        day_to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
            day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            day_from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            day_to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        show_red_line = icp.get_param("dankbit.show_red_line")
        last_hedging_time = icp.get_param("dankbit.last_hedging_time")
        mock_0dte = icp.get_param("dankbit.mock_0dte")
        start_from_ts = int(icp.get_param("dankbit.from_days_ago"))
        start_ts = self.get_midnight_ts(days_offset=start_from_ts)

        if last_hedging_time:
            start_ts = last_hedging_time

        if from_hour:
            start_ts = self.get_ts_from_hour(from_hour)
            plot_title = f"{plot_title} from {str(from_hour)}:00 UTC"

        if minutes_ago:
            start_ts = datetime.now() - timedelta(minutes=minutes_ago)
            plot_title = f"{plot_title} last {str(minutes_ago)} minutes"

        domain=[
            ("name", "ilike", f"{instrument}"),
            ("deribit_ts", ">=", start_ts),
            ("is_block_trade", "=", False),
        ]

        mode = params.get("mode", "flow")
        if mode and mode == "structure":
            domain.append(("oi_reconciled", "=", True))

        trades = request.env["dankbit.trade"].sudo().search(domain=domain)

        index_price = request.env["dankbit.trade"].sudo().get_index_price(instrument)
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte, mode="flow")
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte, mode="flow")

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, view_type, show_red_line)
        
        ax.text(
            0.01, 0.02,
            f"{len(trades)} trades | mode: {mode}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_{view_type}_{from_hour}H_day.png"'),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route("/<string:instrument>/<string:view_type>/a", type="http", auth="public", website=True)
    def chart_png_all(self, instrument, view_type, **params):
        plot_title = f"{view_type} all"
        icp = request.env["ir.config_parameter"].sudo()

        day_from_price = 0
        day_to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
            day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            day_from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            day_to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        mock_0dte = icp.get_param("dankbit.mock_0dte")
        show_red_line = icp.get_param("dankbit.show_red_line")

        domain=[
            ("name", "ilike", f"{instrument}"),
            ("is_block_trade", "=", False),
        ]

        mode = params.get("mode", "flow")
        if mode and mode == "structure":
            domain.append(("oi_reconciled", "=", True))

        trades = request.env["dankbit.trade"].sudo().search(domain=domain)

        index_price = request.env["dankbit.trade"].sudo().get_index_price(instrument)
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte, mode="flow")
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte, mode="flow")

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, view_type, show_red_line)
        
        ax.text(
            0.01, 0.02,
            f"{len(trades)} trades | mode: {mode}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_{view_type}_all.png"'),
            ("Refresh", refresh_interval*5),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route([
        "/<string:instrument>/oi",
        "/<string:instrument>/oi/<int:from_hour>",
        ], type="http", auth="public", website=True)
    def chart_png_full_oi(self, instrument):
        icp = request.env["ir.config_parameter"].sudo()

        day_from_price = 0
        day_to_price = 1000
        steps = 1
        strike_step = 1000
        if instrument.startswith("BTC"):
            day_from_price = float(icp.get_param("dankbit.from_price", default=100000)) 
            day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            day_from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            day_to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=10))
            strike_step = 25

        oi_data = []
        for strike in range(int(day_from_price), int(day_to_price), strike_step):
            trades = request.env["dankbit.trade"].sudo().search(
                domain=[
                    ("name", "ilike", f"{instrument}"),
                    ("strike", "=", strike),
                    ("is_block_trade", "=", False),
                ]
            )
            oi_call, oi_put = oi.calculate_oi(strike, trades)
            oi_data.append([strike, oi_call, oi_put])

        index_price = request.env["dankbit.trade"].sudo().get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)

        fig = obj.plot_oi(index_price, oi_data)

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_full_oi.png"'),
        ]
        return request.make_response(buf.getvalue(), headers=headers)
