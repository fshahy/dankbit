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

    day_from_price = fields.Float(
        string="Day From Price",
        config_parameter="dankbit.day_from_price"
    )

    day_to_price = fields.Float(
        string="Day To Price",
        config_parameter="dankbit.day_to_price"
    )

    show_red_line = fields.Boolean(
        string="Show Red Line",
        config_parameter="dankbit.show_red_line"
    )

    start_from_ts = fields.Selection(
        [
            ("today_midnight", "Today Midnight"),
            ("yesterday_midnight", "Yesterday Midnight"),
        ],
        string="Download Data Starting From",
        default="yesterday_midnight",
        config_parameter="dankbit.start_from_ts",
    )