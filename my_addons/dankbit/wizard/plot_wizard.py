from io import BytesIO
import base64

from odoo import api, models, fields
from ..controllers import options
from ..controllers import delta
from ..controllers import gamma
import numpy as np


class PlotWizard(models.TransientModel):
    _name = "dankbit.plot_wizard"
    _description = "Plot Wizard"

    image_png = fields.Binary("Generated Image")
    
    @api.model
    def default_get(self, fields_list):
        """Executed when the wizard opens."""
        res = super().default_get(fields_list)
        active_ids = self.env.context.get("active_ids")
        active_model = self.env.context.get("active_model")

        if active_ids and active_model:
            records = self.env[active_model].browse(active_ids)
            png_data = self._plot(records)
            res["image_png"] = base64.b64encode(png_data)

        return res
    
    def _plot(self, trades):
        icp = self.env["ir.config_parameter"]

        from_price = 0
        to_price = 1000
        steps = 1
        instrument = trades[0].name[0:3]
        if instrument.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        index_price = self.env["dankbit.trade"].get_index_price(instrument)
        # Note: here it is possible that users selects multiple instruments.
        obj = options.OptionStrat(f"{instrument} | {len(trades)} trade(s)", index_price, from_price, to_price, steps)

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

        fig, ax = obj.plot(index_price, market_deltas, market_gammas, False)

        ax.text(
            0.01, 0.02,
            f"{len(trades)} Trade(s)",
            transform=ax.transAxes,
            fontsize=14,
        )
        
        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig
        return buf.getvalue()
