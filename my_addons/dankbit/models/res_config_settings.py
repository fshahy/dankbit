from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    from_price = fields.Float(
        string="From price",
        config_parameter="dankbit.from_price"
    )

    to_price = fields.Float(
        string="To price",
        config_parameter="dankbit.to_price"
    )

    eth_from_price = fields.Float(
        string="ETH From price",
        config_parameter="dankbit.eth_from_price"
    )

    eth_to_price = fields.Float(
        string="ETH To price",
        config_parameter="dankbit.eth_to_price"
    )

    steps = fields.Integer(
        string="Steps",
        config_parameter="dankbit.steps"
    )

    eth_steps = fields.Integer(
        string="ETH Steps",
        config_parameter="dankbit.eth_steps"
    )

    refresh_interval = fields.Integer(
        string="Refresh interval",
        config_parameter="dankbit.refresh_interval"
    )
    
    deribit_timeout = fields.Float(
        string="Deribit API timeout (s)",
        config_parameter="dankbit.deribit_timeout",
        help="Timeout in seconds for calls to Deribit public APIs."
    )

    deribit_cache_ttl = fields.Float(
        string="Deribit cache TTL (s)",
        config_parameter="dankbit.deribit_cache_ttl",
        help="Time-to-live in seconds for cached Deribit responses (index/instruments)."
    )

    weekly_expiry = fields.Char(
        string="Weekly Expiry",
        config_parameter="dankbit.weekly_expiry",
    )

    monthly_expiry = fields.Char(
        string="Monthly Expiry",
        config_parameter="dankbit.monthly_expiry",
    )

    eth_weekly_expiry = fields.Char(
        string="ETH Weekly Expiry",
        config_parameter="dankbit.eth_weekly_expiry",
    )

    eth_monthly_expiry = fields.Char(
        string="ETH Monthly Expiry",
        config_parameter="dankbit.eth_monthly_expiry",
    )

    # Not using config_parameter= here: Odoo's generic config_parameter
    # handling for Boolean fields treats a False value the same as "delete
    # the parameter" (ir.config_parameter.set_param() special-cases Python
    # False as "unset"), so unchecking one of these and saving would silently
    # revert to the field's default=True on next read instead of persisting
    # False. get_values()/set_values() below store an explicit "True"/"False"
    # string instead, which set_param() writes normally (only a real Python
    # False/None triggers the delete-on-unset behavior, not the string).
    show_daily_lines = fields.Boolean(
        string="Show Daily Lines",
        default=True,
        help="Show the Daily 24H / Daily+1 24H delta=0 lines on the TradingView chart.",
    )

    show_weekly_lines = fields.Boolean(
        string="Show Weekly Lines",
        default=True,
        help="Show the Weekly delta=0 and Weekly gamma peak/bottom lines on the TradingView chart.",
    )

    show_monthly_lines = fields.Boolean(
        string="Show Monthly Lines",
        default=True,
        help="Show the Monthly delta=0 and Monthly gamma peak/bottom lines on the TradingView chart.",
    )

    def get_values(self):
        res = super().get_values()
        icp = self.env["ir.config_parameter"].sudo()
        res.update(
            show_daily_lines=icp.get_param("dankbit.show_daily_lines", "True") == "True",
            show_weekly_lines=icp.get_param("dankbit.show_weekly_lines", "True") == "True",
            show_monthly_lines=icp.get_param("dankbit.show_monthly_lines", "True") == "True",
        )
        return res

    def set_values(self):
        super().set_values()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("dankbit.show_daily_lines", str(self.show_daily_lines))
        icp.set_param("dankbit.show_weekly_lines", str(self.show_weekly_lines))
        icp.set_param("dankbit.show_monthly_lines", str(self.show_monthly_lines))


