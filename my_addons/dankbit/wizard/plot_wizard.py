from io import BytesIO
import base64
import logging

from odoo import api, models, fields
from ..controllers import options
from ..controllers import delta
from ..controllers import gamma
import matplotlib.pyplot as plt
import numpy as np


_logger = logging.getLogger(__name__)

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
            res["dankbit_view_type"] = self.env.context["dankbit_view_type"]
            records = self.env[active_model].browse(active_ids)
            png_data = self._plot(records, res["dankbit_view_type"])
            res["image_png"] = base64.b64encode(png_data)

        return res
    
    def _plot(self, trades, dankbit_view_type):
        icp = self.env['ir.config_parameter'].sudo()

        mock_0dte = icp.get_param('dankbit.mock_0dte')

        day_from_price = 0
        day_to_price = 1000
        steps = 1
        instrument = trades[0].name[0:3]
        if instrument.startswith("BTC"):
            day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
            day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            day_from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            day_to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))
        
        index_price = self.env['dankbit.trade'].sudo().get_index_price(instrument)
        # Note: here it is possible that users selects multiple instruments.
        obj = options.OptionStrat(f"{instrument} | Plotting {len(trades)} trades", index_price, day_from_price, day_to_price, steps)
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

        fig, _ = obj.plot(index_price, market_deltas, market_gammas, dankbit_view_type, True)
        
        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return buf.getvalue()
