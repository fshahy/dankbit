# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timezone

from odoo import fields, models

_logger = logging.getLogger(__name__)


class LiveBand(models.Model):
    _name = "dankbit.live.band"
    _order = "asset, computed_at"

    """An hourly, append-only time series of the same 5 headline numbers
    dankbit.bands itself carries for the nearest expiry — High/Resistance,
    Low/Support, Gamma Band, Smart Liquidity Upper/Lower — but shaped
    completely differently: dankbit.bands keeps ONE
    continuously-refined-then-frozen row per INSTRUMENT (a term structure
    across expiries); this model instead creates a brand NEW row every
    time its own cron runs (a real point-in-time reading), so a chart can
    show how these numbers actually moved hour to hour for one asset, not
    just their final value per expiry. Each reading uses the plain
    since-00:00-Iran default window (Asia/Tehran, via
    options.day_window_start() — same as dankbit.bands' own cron), not a
    rolling window.

    Entirely independent of dankbit.bands — separate model/table/cron,
    reusing dankbit.bands._compute_asset() unmodified (the same pure
    computation, called fresh) rather than reading dankbit.bands' own
    persisted table or touching its compute_snapshot()/cron in any way,
    so /chart/<asset>, /oi/<asset>, and /gamma/<instrument> are all
    completely unaffected. Feeds /z/<asset> alone.
    """

    asset = fields.Char(required=True, index=True)
    computed_at = fields.Datetime(string="Computed At", default=fields.Datetime.now, index=True)
    instrument = fields.Char(string="Instrument")
    high_resistance = fields.Float(string="High/Resistance", digits=(16, 4))
    low_support = fields.Float(string="Low/Support", digits=(16, 4))
    gamma_band = fields.Float(string="Gamma Band", digits=(16, 4))
    smart_liq_upper = fields.Float(string="Smart Liquidity Upper", digits=(16, 4))
    smart_liq_lower = fields.Float(string="Smart Liquidity Lower", digits=(16, 4))

    def compute_and_create(self, asset):
        """Computes the nearest expiry's dankbit.bands._compute_asset()
        result (same since-00:00-Iran default window that model's own
        cron uses — not a rolling window, since this is meant to mirror
        the same numbers dankbit.bands itself would persist for that
        instrument, just as an unbounded log instead of one frozen row)
        and creates ONE NEW row from it — never an upsert, unlike every
        other snapshot model in this addon. Writes nothing if nothing is
        computable (missing index price/expiry/trades), same
        nothing-computable-yet-skip convention every other cron here
        follows."""
        data = self.env["dankbit.bands"]._compute_asset(asset, expiry_index=0)
        if not data:
            _logger.warning("dankbit.live.band: nothing computable for %s, skipping", asset)
            return None

        vals = {
            "asset": asset,
            "computed_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "instrument": data["instrument"],
            "high_resistance": data["high_resistance"],
            "low_support": data["low_support"],
            "gamma_band": data["gamma_band"],
            "smart_liq_upper": data["smart_liq_upper_price"],
            "smart_liq_lower": data["smart_liq_lower_price"],
        }
        return self.sudo().create(vals)

    def compute_snapshot(self):
        """Cron entry point (hourly — see data/ir_cron.xml) — the sole
        source of truth for this model: loops BTC/ETH, creating one new
        row each for the nearest expiry's current reading. No live
        request path writes here at all — only this cron ever creates a
        row, matching "every running of the cron job" literally."""
        for asset in ("BTC", "ETH"):
            self.compute_and_create(asset)
