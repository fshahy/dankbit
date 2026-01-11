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

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "mm", False, 
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
        del fig

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

        # strikes = np.arange(from_price, to_price+1, 1000)
        equilibrium = self.find_delta_zero_crossing(STs, market_deltas)

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
                           view_type, False, 
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
        del fig

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
                "equilibrium": equilibrium,
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
                           "mm", False, 
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
        del fig

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

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "mm", False, 
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
        del fig

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

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "mm", False, 
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
        del fig

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

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "mm", False, 
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
        del fig

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

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "mm", False, 
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
        del fig

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
    def chart_png_oi(self, instrument, **params):
        plot_title = "Today Full OI"
        icp = request.env['ir.config_parameter'].sudo()

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

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        Vol = 0
        Len = 0
        oi_data = []
        for strike in range(int(day_from_price), int(day_to_price), strike_step):
            trades = request.env['dankbit.trade'].search(
                domain=[
                    ("name", "ilike", f"{instrument}"),
                    ("strike", "=", strike),
                    ("is_block_trade", "=", False),
                ]
            )
            oi_call, oi_put = oi.calculate_oi(trades)
            oi_data.append([strike, oi_call, oi_put])
            Vol += self._volume(trades)
            Len += len(trades)

        # price_grid = np.linspace(day_from_price, day_to_price, steps)
        max_pain_price = self.calculate_max_pain(oi_data)

        index_price = request.env['dankbit.trade'].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, day_from_price, day_to_price, steps)

        fig, ax = obj.plot_oi(index_price, oi_data, plot_title)

        ax.text(
            0.01, 0.02,
            f"{Len} Trades | Volume: {Vol}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": "oi",
                "plot_title": f"{instrument} - Today Full OI",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
                "equilibrium": max_pain_price,
            }
        )
    
    def calculate_max_pain(self, oi_data):
        # Use strikes themselves as the price grid (correct for BTC/ETH)
        price_grid = sorted({float(strike) for strike, _, _ in oi_data})

        min_payout = float("inf")
        max_pain_price = None

        for S in price_grid:
            total_payout = 0.0

            for strike, oi_call, oi_put in oi_data:
                strike = float(strike)

                if S > strike:
                    total_payout += (S - strike) * float(oi_call)
                elif S < strike:
                    total_payout += (strike - S) * float(oi_put)

            if total_payout < min_payout:
                min_payout = total_payout
                max_pain_price = S

        return max_pain_price

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

    def find_delta_zero_crossing(self, strikes, deltas):
        strikes = np.asarray(strikes, dtype=float)
        deltas  = np.asarray(deltas, dtype=float)

        # Remove NaNs
        mask = np.isfinite(deltas)
        strikes, deltas = strikes[mask], deltas[mask]

        # Find sign changes
        sign_change = np.where(np.sign(deltas[:-1]) != np.sign(deltas[1:]))[0]

        if len(sign_change) == 0:
            return None  # no delta=0 exists in this window

        i = sign_change[0]

        # Linear interpolation for better precision
        x0, x1 = strikes[i], strikes[i + 1]
        y0, y1 = deltas[i], deltas[i + 1]

        delta_zero = x0 - y0 * (x1 - x0) / (y1 - y0)
        return round(float(delta_zero))

