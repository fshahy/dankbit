# -*- coding: utf-8 -*-

from datetime import timedelta

from odoo import fields, models


class HttpLog(models.Model):
    _name = "dankbit.http.log"
    _order = "request_datetime desc"

    request_datetime = fields.Datetime(required=True, index=True, default=fields.Datetime.now)
    ip_address = fields.Char()
    user_agent = fields.Char()
    url = fields.Char()

    def _delete_old_logs(self):
        """Cron entry point (daily — see data/ir_cron.xml). Prunes log rows
        older than 3 days so this table doesn't grow unbounded."""
        cutoff = fields.Datetime.now() - timedelta(days=3)
        self.sudo().search([("request_datetime", "<", cutoff)]).unlink()
