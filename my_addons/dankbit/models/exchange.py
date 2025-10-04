# -*- coding: utf-8 -*-

from odoo import api, fields, models


class Exchange(models.Model):
    _name = "dankbit.exchange"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    client_id = fields.Char(required=True)
    client_secret = fields.Char(required=True)
    trades_url = fields.Char()
    index_price_url = fields.Char()
    