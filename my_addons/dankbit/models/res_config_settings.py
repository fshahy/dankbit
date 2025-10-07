from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    from_price = fields.Float(
        string="From Price",
        config_parameter="dankbit.from_price"
    )

    to_price = fields.Float(
        string="To Price",
        config_parameter="dankbit.to_price"
    )

    steps = fields.Integer(
        string="Steps",
        config_parameter="dankbit.steps"
    )
    refresh_interval = fields.Integer(
        string="Refresh Interval",
        config_parameter="dankbit.refresh_interval"
    )

    last_hedging_time = fields.Datetime(
        string="Last Hedging Time",
        config_parameter="dankbit.last_hedging_time"
    )

    day_from_price = fields.Float(
        string="Day From Price",
        config_parameter="dankbit.day_from_price"
    )

    day_to_price = fields.Float(
        string="Day To Price",
        config_parameter="dankbit.day_to_price"
    )
