from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    from_price = fields.Float(
        string="From price",
        config_parameter="dankbit.from_price"
    )

    to_price = fields.Float(
        string="To price",
        config_parameter="dankbit.to_price"
    )

    steps = fields.Integer(
        string="Steps",
        config_parameter="dankbit.steps"
    )
    refresh_interval = fields.Integer(
        string="Refresh interval",
        config_parameter="dankbit.refresh_interval"
    )

    zone_from_price = fields.Float(
        string="Zone from price",
        config_parameter="dankbit.zone_from_price"
    )

    zone_to_price = fields.Float(
        string="Zone to price",
        config_parameter="dankbit.zone_to_price"
    )

    show_red_line = fields.Boolean(
        string="Show red line",
        config_parameter="dankbit.show_red_line"
    )

    from_days_ago = fields.Integer(
        string="Data from days ago",
        config_parameter="dankbit.from_days_ago",
    )

    last_hedging_time = fields.Datetime(
        string="Last Hedging Time",
        config_parameter="dankbit.last_hedging_time"
    )