from io import BytesIO
import base64

from odoo import api, models, fields
from ..controllers import options


class ZonesWizard(models.TransientModel):
    _name = "dankbit.zones_wizard"
    _description = "Zones Wizard"

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
        """Same Longs-vs-Shorts zones curves as the /<instrument>/zones PNG
        route (options.build_zone_curves()/plot_zones() — see
        controllers/main.py's chart_png_zones), but against exactly the
        trades the user selected in the backend list view, not a
        DB-queried instrument/time-window domain."""
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

        long_count = len(trades.filtered(lambda t: t.direction == "buy"))
        short_count = len(trades.filtered(lambda t: t.direction == "sell"))

        # Note: as with plot_wizard.py, users may select trades spanning
        # multiple instruments/expiries — build_zone_curves() accumulates
        # them all into one pair of curves regardless.
        longs_obj, shorts_obj = options.build_zone_curves(
            f"{instrument} | {len(trades)} trade(s)", index_price, trades, from_price, to_price, steps
        )

        fig, ax = longs_obj.plot_zones(
            longs_obj.payoffs, shorts_obj.payoffs, index_price, title="Zones"
        )

        ax.text(
            0.01, 0.02,
            f"{long_count} longs\n{short_count} shorts\n(selected trades)",
            transform=ax.transAxes,
            fontsize=14,
            va="bottom",
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig
        return buf.getvalue()
