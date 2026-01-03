import base64
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
import matplotlib.pyplot as plt


_logger = logging.getLogger(__name__)

class ChartController(http.Controller):
    @http.route("/help", auth="public", type="http", website=True)
    def help_page(self):
        return request.render("dankbit.dankbit_help")

    @http.route("/<string:instrument>", type="http", auth="public", website=True)
    def dispatcher(self, instrument, **params):
        icp = request.env["ir.config_parameter"].sudo()

        day_from_price = 0
        day_to_price = 1000
        steps = 1

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=6.0))

        tau_param = params.get("tau", None)
        if tau_param is not None:
            tau = float(tau_param)

        start_ts = datetime.now() - timedelta(hours=tau)

        domain=[
            ("deribit_ts", ">=", start_ts),
        ]

        mode = params.get("mode", "flow")
        if mode not in ["flow", "structure"]:
            raise ValueError(f"Unknown mode: {mode}")
        if mode and mode == "structure":
            domain.append(("oi_reconciled", "=", True))

        if instrument == "BTC":
            plot_title = f"Dealer State"

            day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
            day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))

            domain.append(("name", "ilike", "BTC"))
            return self.chart_png_dealer_state(
                                    instrument,
                                    domain,
                                    day_from_price=day_from_price, 
                                    day_to_price=day_to_price, 
                                    steps=steps, 
                                    refresh_interval=refresh_interval, 
                                    plot_title=plot_title, 
                                    mode=mode, 
                                    tau=tau)
        elif instrument == "ETH":
            plot_title = f"Dealer State"

            day_from_price = float(icp.get_param("dankbit.eth_from_price", default=100000))
            day_to_price = float(icp.get_param("dankbit.eth_to_price", default=150000))
            steps = int(icp.get_param("dankbit.eth_steps", default=100))

            domain.append(("name", "ilike", "ETH"))
            return self.chart_png_dealer_state(instrument, 
                                    domain, 
                                    day_from_price=day_from_price, 
                                    day_to_price=day_to_price, 
                                    steps=steps, 
                                    refresh_interval=refresh_interval, 
                                    plot_title=plot_title, 
                                    mode=mode, 
                                    tau=tau)
        else:
            return request.make_response(
                f"<h3>Route not found</h3><p>Instrument '{instrument}' is not supported.</p>",
                headers=[("Content-Type", "text/html")],
                status=404,
            )

    def chart_png_dealer_state(self, 
                         instrument,
                         domain, 
                         day_from_price, 
                         day_to_price, 
                         steps, 
                         refresh_interval, 
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mode=mode, tau=tau)

        gamma_peak_value = self.find_positive_gamma_peak(-market_gammas)
        if gamma_peak_value < 0:
            gamma_peak_value = 0
        
        if instrument.startswith("BTC"):
            gamma_peak_value = round(gamma_peak_value*1000)
        elif instrument.startswith("ETH"):
            gamma_peak_value = round(gamma_peak_value*100)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "mm", False, plot_title)

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

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_title": f"{instrument} - {plot_title}",
                "refresh_interval": refresh_interval*5,
                "image_b64": image_b64,
                "gamma_peak_value": gamma_peak_value,
            }
        )

    @http.route("/<string:instrument>/<string:view_type>", type="http", auth="public", website=True)
    def chart_png_day(self, instrument, view_type, **params):
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
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=6.0))

        tau_param = params.get("tau", None)
        if tau_param is not None:
            tau = float(tau_param)

        start_ts = datetime.now() - timedelta(hours=tau)

        domain=[
            ("name", "ilike", f"{instrument}"),
            ("deribit_ts", ">=", start_ts),
        ]

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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mode=mode, tau=tau)

        gamma_peak_value = self.find_positive_gamma_peak(-market_gammas)
        if gamma_peak_value < 0:
            gamma_peak_value = 0

        if instrument.startswith("BTC"):
            gamma_peak_value = round(gamma_peak_value*1000)
        elif instrument.startswith("ETH"):
            gamma_peak_value = round(gamma_peak_value*100)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, view_type, False, plot_title)

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

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_title": f"{instrument} - Today",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
                "gamma_peak_value": gamma_peak_value,
            }
        )

    @http.route("/<string:instrument>/<int:strike>", type="http", auth="public", website=True)
    def chart_png_strike(self, instrument, strike, **params):
        plot_title = f"Strike {strike}"
        icp = request.env['ir.config_parameter'].sudo()

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
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=6.0))

        tau_param = params.get("tau", None)
        if tau_param is not None:
            tau = float(tau_param)

        start_ts = datetime.now() - timedelta(hours=tau)

        domain=[
            ("name", "ilike", f"{instrument}"),
            ("strike", "=", int(strike)),
            ("deribit_ts", ">=", start_ts),
        ]

        mode = params.get("mode", "flow")
        if mode not in ["flow", "structure"]:
            raise ValueError(f"Unknown mode: {mode}")
        if mode and mode == "structure":
            domain.append(("oi_reconciled", "=", True))
        
        trades = request.env['dankbit.trade'].search(
            domain=domain
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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mode=mode, tau=tau)

        gamma_peak_value = self.find_positive_gamma_peak(-market_gammas)
        if gamma_peak_value < 0:
            gamma_peak_value = 0

        if instrument.startswith("BTC"):
            gamma_peak_value = round(gamma_peak_value*1000)
        elif instrument.startswith("ETH"):
            gamma_peak_value = round(gamma_peak_value*100)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "mm", False, plot_title=plot_title)
        
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

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_title": f"{instrument} - {plot_title}",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
                "gamma_peak_value": gamma_peak_value,
            }
        )

    @http.route("/<string:instrument>/oi", type="http", auth="public", website=True)
    def chart_png_full_oi(self, instrument):
        icp = request.env["ir.config_parameter"].sudo()

        # --- price range ---
        if instrument.upper() == "BTC":
            day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
            day_to_price   = float(icp.get_param("dankbit.to_price", default=150000))
            steps          = int(icp.get_param("dankbit.steps", default=100))
            strike_step    = 1000
        elif instrument.upper() == "ETH":
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

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_title": f"{instrument} - Taker Full OI",
                "refresh_interval": 3600,
                "image_b64": image_b64,
            }
        )

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

    def find_positive_gamma_peak(self, gammas):
        gammas = np.asarray(gammas, dtype=float)

        # Keep only positive gamma values
        pos_gammas = gammas[gammas > 0.0] 

        if pos_gammas.size == 0:
            return 0  # explicit: no positive gamma regime

        return np.max(gammas)
