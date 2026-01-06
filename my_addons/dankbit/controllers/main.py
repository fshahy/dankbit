import base64
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
import matplotlib.pyplot as plt


_logger = logging.getLogger(__name__)

class ChartController(http.Controller):
    @http.route("/help", auth="public", type="http", website=True)
    def help_page(self):
        return request.render("dankbit.dankbit_help")

    @http.route("/dashboard", auth="public", type="http", website=True)
    def dashboard(self, **params):
        icp = request.env["ir.config_parameter"].sudo()

        today = params.get("today", None)
        width = int(params.get("width", 6))
        height = int(params.get("height", 8))

        day_from_price = 0
        day_to_price = 1000
        if today.startswith("BTC"):
            day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
            day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        if today.startswith("ETH"):
            day_from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            day_to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))

        from_price = int(params.get("from_price", day_from_price))
        to_price = int(params.get("to_price", day_to_price))


        #TODO: error check

        return request.render("dankbit.dankbit_dashboard", {
            "today": today,
            "width": width,
            "height": height,
            "from_price": from_price,
            "to_price": to_price,
            "currency": today[0:3],
        })

    @http.route("/<string:instrument>", type="http", auth="public", website=True)
    def dispatcher(self, instrument, **params):
        icp = request.env["ir.config_parameter"].sudo()

        day_from_price = 0
        day_to_price = 1000
        steps = 1

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=7.0))

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

        screenshot = params.get("screenshot", None)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        if instrument == "BTC":
            plot_title = f"Dealer State"

            day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
            day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))

            from_price = int(params.get("from_price", day_from_price))
            to_price = int(params.get("to_price", day_to_price))

            domain.append(("name", "ilike", "BTC"))
            return self.chart_png_dealer_state(
                                    instrument,
                                    domain,
                                    from_price=from_price, 
                                    to_price=to_price, 
                                    steps=steps, 
                                    refresh_interval=refresh_interval, 
                                    plot_title=plot_title, 
                                    mode=mode, 
                                    tau=tau,
                                    screenshot=screenshot,
                                    width=width,
                                    height=height)
        elif instrument == "ETH":
            plot_title = f"Dealer State"

            day_from_price = float(icp.get_param("dankbit.eth_from_price", default=100000))
            day_to_price = float(icp.get_param("dankbit.eth_to_price", default=150000))
            steps = int(icp.get_param("dankbit.eth_steps", default=100))

            from_price = int(params.get("from_price", day_from_price))
            to_price = int(params.get("to_price", day_to_price))

            domain.append(("name", "ilike", "ETH"))
            return self.chart_png_dealer_state(instrument, 
                                    domain, 
                                    from_price=from_price, 
                                    to_price=to_price, 
                                    steps=steps, 
                                    refresh_interval=refresh_interval, 
                                    plot_title=plot_title, 
                                    mode=mode, 
                                    tau=tau,
                                    screenshot=screenshot,
                                    width=width,
                                    height=height)
        else:
            return request.make_response(
                f"<h3>Route not found</h3><p>Instrument '{instrument}' is not supported.</p>",
                headers=[("Content-Type", "text/html")],
                status=404,
            )

    def chart_png_dealer_state(self, 
                         instrument,
                         domain, 
                         from_price, 
                         to_price, 
                         steps, 
                         refresh_interval, 
                         plot_title, 
                         mode, 
                         tau,
                         screenshot,
                         width,
                         height):
        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
        is_call = []

        trades = [t for t in trades if from_price <= t.strike <= to_price]

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

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mode=mode, tau=tau)

        gamma_peak_value = self.find_positive_gamma_peak(-market_gammas)
        if gamma_peak_value < 0:
            gamma_peak_value = 0
        
        if instrument.startswith("BTC"):
            gamma_peak_value = round(gamma_peak_value*1000)
        elif instrument.startswith("ETH"):
            gamma_peak_value = round(gamma_peak_value*100)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "mm", False, plot_title, width=width, height=height)

        volume = self._volume(trades)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)

        if screenshot:
            buf.seek(0)
            request.env["dankbit.screenshot"].sudo().create({
                "name": f"{instrument} - Dealer State",
                "timestamp": fields.Datetime.now(),
                "image_png": base64.b64encode(buf.read()),
            })

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": "dealer_state",
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

        from_price = int(params.get("from_price", day_from_price))
        to_price = int(params.get("to_price", day_to_price))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=7.0))

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
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
        is_call = []

        trades = [t for t in trades if from_price <= t.strike <= to_price]

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

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mode=mode, tau=tau)

        gamma_peak_value = self.find_positive_gamma_peak(-market_gammas)
        if gamma_peak_value < 0:
            gamma_peak_value = 0

        if instrument.startswith("BTC"):
            gamma_peak_value = round(gamma_peak_value*1000)
        elif instrument.startswith("ETH"):
            gamma_peak_value = round(gamma_peak_value*100)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           view_type, False, plot_title,
                           width=width,
                           height=height)

        volume = self._volume(trades)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume}",
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
                "plot_name": "today",
                "plot_title": f"{instrument} - Today",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
                "gamma_peak_value": gamma_peak_value,
            }
        )

    @http.route("/<string:instrument>/c", type="http", auth="public", website=True)
    def chart_png_calls(self, instrument, **params):
        plot_title = "Calls"
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

        from_price = int(params.get("from_price", day_from_price))
        to_price = int(params.get("to_price", day_to_price))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=7.0))

        tau_param = params.get("tau", None)
        if tau_param is not None:
            tau = float(tau_param)

        start_ts = datetime.now() - timedelta(hours=tau)

        domain=[
            ("name", "ilike", f"{instrument}"),
            ("option_type", "=", "call"),
            ("deribit_ts", ">=", start_ts),
        ]

        mode = params.get("mode", "flow")
        if mode not in ["flow", "structure"]:
            raise ValueError(f"Unknown mode: {mode}")
        if mode and mode == "structure":
            domain.append(("oi_reconciled", "=", True))

        trades = request.env['dankbit.trade'].sudo().search(domain=domain)

        index_price = request.env['dankbit.trade'].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
        is_call = []

        trades = [t for t in trades if from_price <= t.strike <= to_price]

        for trade in trades:
            is_call.append(True)
            if trade.direction == "buy":
                obj.long_call(trade.strike, trade.price * trade.index_price)
            elif trade.direction == "sell":
                obj.short_call(trade.strike, trade.price * trade.index_price)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mode=mode, tau=tau)

        gamma_peak_value = self.find_positive_gamma_peak(-market_gammas)
        if gamma_peak_value < 0:
            gamma_peak_value = 0

        if instrument.startswith("BTC"):
            gamma_peak_value = round(gamma_peak_value*1000)
        elif instrument.startswith("ETH"):
            gamma_peak_value = round(gamma_peak_value*100)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "taker", False, 
                           plot_title,
                           width=width,
                           height=height)
        
        volume = self._volume(trades)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume}",
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
                "plot_name": "calls",
                "plot_title": f"{instrument} - calls",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
                "gamma_peak_value": gamma_peak_value,
            }
        )

    @http.route("/<string:instrument>/p", type="http", auth="public", website=True)
    def chart_png_puts(self, instrument, **params):
        plot_title = "Puts"
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

        from_price = int(params.get("from_price", day_from_price))
        to_price = int(params.get("to_price", day_to_price))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=7.0))

        tau_param = params.get("tau", None)
        if tau_param is not None:
            tau = float(tau_param)

        start_ts = datetime.now() - timedelta(hours=tau)

        domain=[
            ("name", "ilike", f"{instrument}"),
            ("option_type", "=", "put"),
            ("deribit_ts", ">=", start_ts),
        ]

        mode = params.get("mode", "flow")
        if mode not in ["flow", "structure"]:
            raise ValueError(f"Unknown mode: {mode}")
        if mode and mode == "structure":
            domain.append(("oi_reconciled", "=", True))

        trades = request.env['dankbit.trade'].sudo().search(domain=domain)

        index_price = request.env['dankbit.trade'].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
        is_call = []

        trades = [t for t in trades if from_price <= t.strike <= to_price]

        for trade in trades:
            is_call.append(False)
            if trade.direction == "buy":
                obj.long_put(trade.strike, trade.price * trade.index_price)
            elif trade.direction == "sell":
                obj.short_put(trade.strike, trade.price * trade.index_price)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mode=mode, tau=tau)

        gamma_peak_value = self.find_positive_gamma_peak(-market_gammas)
        if gamma_peak_value < 0:
            gamma_peak_value = 0

        if instrument.startswith("BTC"):
            gamma_peak_value = round(gamma_peak_value*1000)
        elif instrument.startswith("ETH"):
            gamma_peak_value = round(gamma_peak_value*100)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "taker", 
                           False, plot_title,
                           width=width,
                           height=height)
        
        volume = self._volume(trades)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume}",
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
                "plot_name": "puts",
                "plot_title": f"{instrument} - puts",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
                "gamma_peak_value": gamma_peak_value,
            }
        )

    @http.route("/<string:instrument>/b", type="http", auth="public", website=True)
    def chart_png_buys(self, instrument, **params):
        plot_title = "Buys"
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

        from_price = int(params.get("from_price", day_from_price))
        to_price = int(params.get("to_price", day_to_price))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=7.0))

        tau_param = params.get("tau", None)
        if tau_param is not None:
            tau = float(tau_param)

        start_ts = datetime.now() - timedelta(hours=tau)

        domain=[
            ("name", "ilike", f"{instrument}"),
            ("direction", "=", "buy"),
            ("deribit_ts", ">=", start_ts),
        ]

        mode = params.get("mode", "flow")
        if mode not in ["flow", "structure"]:
            raise ValueError(f"Unknown mode: {mode}")
        if mode and mode == "structure":
            domain.append(("oi_reconciled", "=", True))

        trades = request.env['dankbit.trade'].sudo().search(domain=domain)

        index_price = request.env['dankbit.trade'].sudo().get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
        is_call = []

        trades = [t for t in trades if from_price <= t.strike <= to_price]

        for trade in trades:
            if trade.option_type == "call":
                is_call.append(True)
                obj.long_call(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                is_call.append(False)
                obj.long_put(trade.strike, trade.price * trade.index_price)

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

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "taker", False, 
                           plot_title,
                           width=width,
                           height=height)
        
        volume = self._volume(trades)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume}",
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
                "plot_name": "buys",
                "plot_title": f"{instrument} - buys",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
                "gamma_peak_value": gamma_peak_value,
            }
        )
    
    @http.route("/<string:instrument>/s", type="http", auth="public", website=True)
    def chart_png_sells(self, instrument, **params):
        plot_title = "Sells"
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

        from_price = int(params.get("from_price", day_from_price))
        to_price = int(params.get("to_price", day_to_price))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=7.0))

        tau_param = params.get("tau", None)
        if tau_param is not None:
            tau = float(tau_param)

        start_ts = datetime.now() - timedelta(hours=tau)

        domain=[
            ("name", "ilike", f"{instrument}"),
            ("direction", "=", "sell"),
            ("deribit_ts", ">=", start_ts),
        ]

        mode = params.get("mode", "flow")
        if mode not in ["flow", "structure"]:
            raise ValueError(f"Unknown mode: {mode}")
        if mode and mode == "structure":
            domain.append(("oi_reconciled", "=", True))

        trades = request.env['dankbit.trade'].sudo().search(domain=domain)

        index_price = request.env['dankbit.trade'].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
        is_call = []

        trades = [t for t in trades if from_price <= t.strike <= to_price]

        for trade in trades:
            if trade.option_type == "call":
                is_call.append(True)
                obj.short_call(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                is_call.append(False)
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

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "taker", 
                           False, plot_title,
                           width=width,
                           height=height)
        
        volume = self._volume(trades)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume}",
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
                "plot_name": "sells",
                "plot_title": f"{instrument} - sells",
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

        from_price = int(params.get("from_price", day_from_price))
        to_price = int(params.get("to_price", day_to_price))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        tau = float(icp.get_param("dankbit.greeks_gamma_decay_tau_hours", default=7.0))

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

        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
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

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mode=mode, tau=tau)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mode=mode, tau=tau)

        gamma_peak_value = self.find_positive_gamma_peak(-market_gammas)
        if gamma_peak_value < 0:
            gamma_peak_value = 0

        if instrument.startswith("BTC"):
            gamma_peak_value = round(gamma_peak_value*1000)
        elif instrument.startswith("ETH"):
            gamma_peak_value = round(gamma_peak_value*100)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, "mm", False, plot_title=plot_title, width=width, height=height)
        
        volume = self._volume(trades)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume}",
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
                "plot_name": "strike",
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
                "plot_name": "full_oi",
                "plot_title": f"{instrument} - Taker Full OI",
                "refresh_interval": 3600,
                "image_b64": image_b64,
            }
        )

    def _volume(self, trades):
        vol = 0.0
        for trade in trades:
            vol += abs(trade.amount)

        return round(vol)

    def find_positive_gamma_peak(self, gammas):
        gammas = np.asarray(gammas, dtype=float)

        # Keep only positive gamma values
        pos_gammas = gammas[gammas > 0.0] 

        if pos_gammas.size == 0:
            return 0  # explicit: no positive gamma regime

        return np.max(gammas)
