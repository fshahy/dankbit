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


