# -*- coding: utf-8 -*-

from odoo import fields, models


class ChatLog(models.Model):
    """One row per question asked in the /4l chat panel (controllers/chat.py)
    — the question, which tools ran with what arguments and results, the
    answer, and any numbers the answer stated that the tool results don't
    back up. Review material for choosing/tuning the local model; nothing
    reads this back."""
    _name = "dankbit.chat.log"
    _description = "Dankbit Chat Log"
    _order = "create_date desc"

    user_id = fields.Many2one("res.users", string="User", index=True)
    asset = fields.Char(index=True)
    instrument = fields.Char(string="Page Expiry")
    window_hours = fields.Char(string="Page Window")
    question = fields.Text(required=True)
    answer = fields.Text()
    tool_calls = fields.Text(help="JSON: every tool call with its arguments and result")
    unverified_numbers = fields.Char(help="Numbers in the final answer not found in the tool results")
    retried = fields.Boolean(help="The first answer had unverified numbers and a rewrite was requested")
    elapsed_seconds = fields.Float(digits=(10, 1))
    model = fields.Char()
    error = fields.Text()
