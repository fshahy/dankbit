# -*- coding: utf-8 -*-

from odoo import fields, models


class LiquiditySnapshot(models.Model):
    _name = "dankbit.liquidity.snapshot"
    _order = "as_of desc"

    # Manually entered from CoinGlass (no API integration exists for this —
    # see forecast3.py's Liquidity Map Engine and the discussion in
    # CLAUDE.md's Forecast 3 section) — twice a day per asset, matching the
    # user's own 12h liquidity-window cadence. `as_of` is the moment this
    # reading is valid for, not a creation timestamp, so a form entered
    # late (or backdated) still lands in the right place in the history
    # get_latest() below reads. Cadence-agnostic on purpose: get_latest()
    # just takes whichever row is freshest at-or-before "now", so this
    # works whether entries land every 12h, 8h, or irregularly.
    asset = fields.Selection([("BTC", "BTC"), ("ETH", "ETH")], required=True, index=True)
    as_of = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    lower_liq_price = fields.Float(digits=(16, 4))
    lower_liq_m = fields.Float(string="Lower Liq (M)", digits=(16, 4))
    upper_liq_price = fields.Float(digits=(16, 4))
    upper_liq_m = fields.Float(string="Upper Liq (M)", digits=(16, 4))
    note = fields.Char()

    def get_latest(self, asset, as_of=None):
        """The freshest entry for `asset` at or before `as_of` (default:
        now) — used by dankbit.forecast3.snapshot.compute_and_persist() to
        feed forecast3.py's Liquidity Map Engine. Returns an empty
        recordset if nothing has been entered yet (or nothing old enough),
        which callers treat the same as "no liquidity data available" —
        same as Thales's own na-handling when a row's liquidity columns
        are blank."""
        cutoff = as_of or fields.Datetime.now()
        return self.search([("asset", "=", asset), ("as_of", "<=", cutoff)], limit=1, order="as_of desc")
