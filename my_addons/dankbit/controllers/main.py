import numpy as np
from datetime import datetime, timedelta
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
    def dispatcher(self, instrument, **params):
        icp = request.env["ir.config_parameter"]

        day_from_price = 0
        day_to_price = 1000
        steps = 1

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        mock_0dte = icp.get_param("dankbit.mock_0dte")
        show_red_line = icp.get_param("dankbit.show_red_line")
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=6.0))

        start_ts = datetime.now() - timedelta(days=1)

        tau_param = params.get("tau", None)
        if tau_param is not None:
            tau = float(tau_param)
        mode = "flow"

        if instrument == "BTC":
            plot_title = f"Dealer State"

            day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
            day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))

            domain=[
                ("name", "ilike", "BTC"),
                ("is_block_trade", "=", False),
                ("deribit_ts", ">=", start_ts),
            ]
            return self.chart_png_dealer_state(
                                    instrument,
                                    domain,
                                    day_from_price=day_from_price, 
                                    day_to_price=day_to_price, 
                                    steps=steps, 
                                    refresh_interval=refresh_interval, 
                                    mock_0dte=mock_0dte, 
                                    show_red_line=show_red_line, 
                                    plot_title=plot_title, 
                                    mode=mode, 
                                    tau=tau)
        elif instrument == "ETH":
            plot_title = f"Dealer State"

            day_from_price = float(icp.get_param("dankbit.eth_from_price", default=100000))
            day_to_price = float(icp.get_param("dankbit.eth_to_price", default=150000))
            steps = int(icp.get_param("dankbit.eth_steps", default=100))

            domain=[
                ("name", "ilike", "ETH"),
                ("is_block_trade", "=", False),
                ("deribit_ts", ">=", start_ts),
            ]
            return self.chart_png_dealer_state(instrument, 
                                    domain, 
                                    day_from_price=day_from_price, 
                                    day_to_price=day_to_price, 
                                    steps=steps, 
                                    refresh_interval=refresh_interval, 
                                    mock_0dte=mock_0dte, 
                                    show_red_line=show_red_line, 
                                    plot_title=plot_title, 
                                    mode=mode, 
                                    tau=tau)
        elif instrument.startswith("BTC-") or instrument.startswith("ETH-"):
            return self.instrument_home_page(instrument)

    def instrument_home_page(self, instrument):
        values = {
            "instrument": instrument,
        }
        return request.render("dankbit.dankbit_instrument_home_page", values)

    def chart_png_dealer_state(self, 
                         instrument,
                         domain, 
                         day_from_price, 
                         day_to_price, 
                         steps, 
                         refresh_interval, 
                         mock_0dte, 
                         show_red_line, 
                         plot_title, 
                         mode, 
                         tau):
        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte, mode=mode, tau=tau)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "mm", show_red_line, plot_title)

        volume = self._atm_volume(trades, float(index_price), atm_pct=0.01)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades (24H) | ATM Volume: {volume} | Mode: {mode} | Tau: {tau}H",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="btc_g_global.png"'),
            ("Refresh", refresh_interval*10),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route([
        "/<string:instrument>/<string:view_type>", 
        "/<string:instrument>/<string:view_type>/l/<int:minutes_ago>", 
        "/<string:instrument>/<string:view_type>/<int:from_hour>", 
    ], type="http", auth="public", website=True)
    def chart_png_day(self, instrument, view_type, from_hour=0, minutes_ago=0, **params):
        if view_type not in ["taker", "mm"]:
            return f"<h3>Nothing here.</h3>"
        
        plot_title = view_type
        icp = request.env["ir.config_parameter"]

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
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=6.0))

        if last_hedging_time:
            start_ts = fields.Datetime.from_string(last_hedging_time)

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

        tau_param = params.get("tau", None)
        if tau_param is not None:
            tau = float(tau_param)
        mode = params.get("mode", "flow")
        if mode not in ["flow", "structure"]:
            raise ValueError(f"Unknown mode: {mode}")
        if mode and mode == "structure":
            domain.append(("oi_reconciled", "=", True))

        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte, mode=mode, tau=tau)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, view_type, show_red_line, plot_title)

        volume = self._atm_volume(trades, float(index_price), atm_pct=0.01)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | ATM Volume: {volume} | Mode: {mode} | Tau: {tau}H",
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
        icp = request.env["ir.config_parameter"]

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
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=6.0))

        domain=[
            ("name", "ilike", f"{instrument}"),
            ("is_block_trade", "=", False),
        ]

        tau_param = params.get("tau", None)
        if tau_param is not None:
            tau = float(tau_param)
        mode = params.get("mode", "flow")
        if mode not in ["flow", "structure"]:
            raise ValueError(f"Unknown mode: {mode}")
        if mode and mode == "structure":
            domain.append(("oi_reconciled", "=", True))

        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte, mode=mode, tau=tau)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, view_type, show_red_line, plot_title)

        volume = self._atm_volume(trades, float(index_price), atm_pct=0.01)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | ATM Volume: {volume} | Mode: {mode} | Tau: {tau}H",
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
            ("Refresh", refresh_interval*10),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route("/<string:instrument>/<int:strike>", type="http", auth="public", website=True)
    def chart_png_strike(self, instrument, strike):
        plot_title = f"Dealer State at Strike {strike}"
        icp = request.env['ir.config_parameter']

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
        start_ts = datetime.now() - timedelta(days=1)
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=6.0))

        trades = request.env['dankbit.trade'].search(
            domain=[
                ("name", "ilike", f"{instrument}"),
                ("strike", "=", int(strike)),
                ("deribit_ts", ">=", start_ts),
                ("is_block_trade", "=", False),
            ]
        )

        index_price = request.env["dankbit.trade"].get_index_price(instrument)

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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mock_0dte=False, mode="flow", tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mock_0dte=False, mode="flow", tau=tau)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "mm", show_red_line, plot_title=plot_title)
        
        volume = self._atm_volume(trades, float(index_price), atm_pct=0.01)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | ATM Volume: {volume} | Mode: flow | Tau: {tau}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)

        headers = [
            ("Content-Type", "image/png"), 
            ("Cache-Control", "no-cache"),
            ("Content-Disposition", f'inline; filename="{instrument}_strike_{strike}.png"'),
            ("Refresh", refresh_interval),
        ]
        return request.make_response(buf.getvalue(), headers=headers)

    @http.route("/<string:instrument>/oi", type="http", auth="public", website=True)
    def chart_png_full_oi(self, instrument):
        icp = request.env["ir.config_parameter"]

        # --- price range ---
        if instrument.startswith("BTC"):
            day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
            day_to_price   = float(icp.get_param("dankbit.to_price", default=150000))
            steps          = int(icp.get_param("dankbit.steps", default=100))
            strike_step    = 1000
        elif instrument.startswith("ETH"):
            day_from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            day_to_price   = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps          = int(icp.get_param("dankbit.eth_steps", default=10))
            strike_step    = 25
        else:
            return "<h3>Unsupported instrument</h3>"

        # ------------------------------------------------------------------
        # REAL OI SOURCE (Deribit snapshot, NOT trades)
        # ------------------------------------------------------------------
        try:
            # Expected to return list of dicts:
            # { strike, option_type, open_interest }
            oi_snapshot = oi.get_oi_snapshot(instrument)
        except Exception as e:
            _logger.exception("Failed to fetch Deribit OI snapshot")
            return f"<h3>OI fetch failed: {e}</h3>"

        # --- aggregate per strike ---
        oi_map = {}
        for row in oi_snapshot:
            strike = int(row["strike"])
            if strike < day_from_price or strike > day_to_price:
                continue

            if strike not in oi_map:
                oi_map[strike] = {"call": 0.0, "put": 0.0}

            oi_map[strike][row["option_type"]] += float(row["open_interest"])

        # --- build sorted plot data ---
        oi_data = []
        for strike in range(int(day_from_price), int(day_to_price), strike_step):
            call_oi = oi_map.get(strike, {}).get("call", 0.0)
            put_oi  = oi_map.get(strike, {}).get("put", 0.0)
            oi_data.append([strike, call_oi, put_oi])
            _logger.info([strike, call_oi, put_oi])

        # ------------------------------------------------------------------
        # plotting (unchanged)
        # ------------------------------------------------------------------
        index_price = request.env["dankbit.trade"].get_index_price(instrument)
        obj = options.OptionStrat(
            instrument,
            index_price,
            day_from_price,
            day_to_price,
            steps
        )

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

    def _atm_volume(self, trades, index_price, atm_pct=0.01):
        """
        Calculate ATM volume.
        
        trades: iterable of trade records (must have .strike and .amount)
        index_price: current underlying price
        atm_pct: ATM band as percentage (default ±1%)
        
        Returns: float
        """
        lower = index_price * (1.0 - atm_pct)
        upper = index_price * (1.0 + atm_pct)

        vol = 0.0
        for trade in trades:
            if lower <= trade.strike <= upper:
                vol += abs(trade.amount)

        return round(vol)
