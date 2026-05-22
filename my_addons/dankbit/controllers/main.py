import base64
import numpy as np
from io import BytesIO
from odoo import http
from odoo.http import request
from . import options
from . import delta
from . import gamma


class ChartController(http.Controller):
    @http.route("/help", auth="public", type="http", website=True)
    def help_page(self):
        return request.render("dankbit.dankbit_help")

    @http.route("/<string:instrument>", type="http", auth="public", website=True)
    def chart_png_all(self, instrument, **params):
        if instrument.upper() in ("BTC", "ETH"):
            return request.not_found()

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

        for trade in trades:
            if trade.option_type == "call":
                if trade.direction == "buy":
                    obj.long_call(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    obj.short_call(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                if trade.direction == "buy":
                    obj.long_put(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    obj.short_put(trade.strike, trade.price * trade.index_price)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)
        current_delta = self.find_current_delta(STs, market_deltas, index_price)

        width = int(params.get("width", 18))
        height = int(params.get("height", 8))

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, 
                           True, 
                           width=width,
                           height=height)

        net_calls, net_puts = self._net_volume(trades)
        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        last_ts = last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M') if last_trade else "—"
        ax.text(
            0.01, 0.04,
            f"{len(trades)} Trades | Net Calls: {net_calls:+.2f} | Net Puts: {net_puts:+.2f}",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.text(
            0.01, 0.01,
            f"Last trade: {last_ts}",
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

    def _net_volume(self, trades):
        net_calls = net_puts = 0.0
        for t in trades:
            signed = t.amount if t.direction == "buy" else -t.amount
            if t.option_type == "call":
                net_calls += signed
            elif t.option_type == "put":
                net_puts += signed
        return round(net_calls, 2), round(net_puts, 2)

    def find_current_delta(self, STs, market_deltas, index_price):
        STs = np.asarray(STs, dtype=float)
        market_deltas = np.asarray(market_deltas, dtype=float)

        if STs.size == 0 or market_deltas.size == 0 or STs.size != market_deltas.size:
            return 0

        # Find delta at the price point closest to current index price
        idx = np.abs(STs - float(index_price)).argmin()

        return round(float(market_deltas[idx]), 2)
    