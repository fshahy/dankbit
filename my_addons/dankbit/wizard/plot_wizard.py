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
            png_data = self._plot_be_taker(records, res["dankbit_view_type"])
            res["image_png"] = base64.b64encode(png_data)

        return res
    
    def _plot_be_taker(self, trades, dankbit_view_type):
        icp = self.env['ir.config_parameter'].sudo()

        day_from_price = float(icp.get_param("dankbit.from_price", default=100000))
        day_to_price = float(icp.get_param("dankbit.to_price", default=150000))
        steps = int(icp.get_param("dankbit.steps", default=100))
        show_red_line = icp.get_param("dankbit.show_red_line")

        index_price = self.env['dankbit.trade'].sudo().get_index_price()
        obj = options.OptionStrat("instrument", index_price, day_from_price, day_to_price, steps)
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

        # map backend 'be_*' view types to the public-facing ones so
        # wizard-generated plots match the URL-rendered charts.
        view_type = dankbit_view_type
        if isinstance(view_type, str):
            if view_type == 'be_taker':
                view_type = 'taker'
            elif view_type == 'be_mm':
                view_type = 'mm'

        fig, _ = obj.plot(index_price, market_deltas, market_gammas, view_type, show_red_line, width=18, height=8)
        
        buf = BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return buf.getvalue()
