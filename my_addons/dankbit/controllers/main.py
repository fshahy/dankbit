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
            ("expiration", ">=", datetime.now()),
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
        _logger.info(f"Generating dealer state for {instrument} with domain {domain} and mode {mode}")
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
        current_delta = self.find_current_delta(STs, market_deltas, index_price)

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           plot_title, False, 
                           plot_title, 
                           width=width, 
                           height=height)

        volume = self._volume(trades)
        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume} | {last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M')}",
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
                "current_delta": current_delta,
            }
        )

    @http.route("/<string:instrument>", type="http", auth="public", website=True)
    def chart_png_day(self, instrument, **params):
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
        plot_title = f"Tau={int(tau)}h"

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
        current_delta = self.find_current_delta(STs, market_deltas, index_price)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "taker", False, 
                           plot_title,
                           width=width,
                           height=height)

        volume = self._volume(trades)
        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume} | {last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M')}",
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
                "current_delta": current_delta,
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
        current_delta = self.find_current_delta(STs, market_deltas, index_price)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "taker", False, 
                           plot_title,
                           width=width,
                           height=height)
        
        volume = self._volume(trades)
        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume} | {last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M')}",
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
                "current_delta": current_delta,
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
        current_delta = self.find_current_delta(STs, market_deltas, index_price)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "taker", False, 
                           plot_title,
                           width=width,
                           height=height)
        
        volume = self._volume(trades)
        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume} | {last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M')}",
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
                "current_delta": current_delta,
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
        current_delta = self.find_current_delta(STs, market_deltas, index_price)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "taker", False, 
                           plot_title,
                           width=width,
                           height=height)
        
        volume = self._volume(trades)
        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume} | {last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M')}",
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
                "current_delta": current_delta,
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
        current_delta = self.find_current_delta(STs, market_deltas, index_price)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "taker", False, 
                           plot_title,
                           width=width,
                           height=height)
        
        volume = self._volume(trades)
        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume} | {last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M')}",
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
                "current_delta": current_delta,
            }
        )

    @http.route("/<string:instrument>/a", type="http", auth="public", website=True)
    def chart_png_all(self, instrument, **params):
        plot_title = "All"
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

        domain=[
            ("name", "ilike", f"{instrument}"),
        ]

        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
        is_call = []

        # trades = [t for t in trades if from_price <= t.strike <= to_price]

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
        market_deltas = delta.portfolio_delta(STs, trades, 0.05, mode="structure")
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05, mode="structure")
        current_delta = self.find_current_delta(STs, market_deltas, index_price)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "taker", False, 
                           plot_title,
                           width=width,
                           height=height)

        volume = self._volume(trades)
        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume} | {last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M')}",
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
                "plot_name": "All",
                "plot_title": f"{instrument} - All",
                "refresh_interval": refresh_interval*5,
                "image_b64": image_b64,
                "current_delta": current_delta,
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
        current_delta = self.find_current_delta(STs, market_deltas, index_price)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           "taker", False, 
                           plot_title, 
                           width=width, 
                           height=height)
        
        volume = self._volume(trades)
        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trades | Volume: {volume} | {last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M')}",
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
                "current_delta": current_delta,
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
        strikes = range(int(day_from_price), int(day_to_price), strike_step)
        for strike in strikes:
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
                "oi_data": oi_data,
            }
        )
    
    def _volume(self, trades):
        vol = 0.0
        for trade in trades:
            vol += abs(trade.amount)

        return round(vol)

    def find_current_delta(self, STs, market_deltas, index_price):
        STs = np.asarray(STs, dtype=float)
        market_deltas = np.asarray(market_deltas, dtype=float)

        if STs.size == 0 or market_deltas.size == 0 or STs.size != market_deltas.size:
            return 0

        # Find delta at the price point closest to current index price
        idx = np.abs(STs - float(index_price)).argmin()

        return round(float(market_deltas[idx]), 2)
    