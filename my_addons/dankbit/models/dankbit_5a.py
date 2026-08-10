# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from odoo import fields, models

from ..controllers import options as options_lib

_logger = logging.getLogger(__name__)

_SCOPES = ("nearest", "weekly", "monthly", "all", "day")


class Dankbit5A(models.Model):
    _name = "dankbit.5a"
    _description = "Dankbit 5A Gamma Snapshot"
    _order = "asset, computed_at desc"

    """Append-only snapshot backing /5a/<asset>'s 5 horizontal gamma
    lines (Nearest/Weekly/Monthly/All/Last 24h) — same "create a brand
    new row every cron tick" shape as dankbit.live.band, not the
    continuously-refined-then-frozen upsert dankbit.bands uses per
    instrument, since there's no single "instrument" this model is
    keyed on (the All/Last 24h scopes span every non-expired
    instrument at once).

    Exists because /api/5a-gamma/<asset> (five_a_gamma_json) used to
    compute all 5 scopes fresh on every request via up to 5 separate
    options.per_leg_gamma() calls, most over an unbounded trade
    window — the heaviest single request in this addon. compute_and_create()
    now does that same computation on a 15-minute cron
    (dankbit_compute_5a_cron) instead, and the controller just reads
    the latest row per asset — a cheap search() instead of a multi-
    second curve rebuild on every page load/poll."""

    asset = fields.Char(required=True, index=True)
    computed_at = fields.Datetime(string="Computed At", default=fields.Datetime.now, index=True)

    nearest_instrument = fields.Char(string="Nearest Instrument")
    nearest_avg_price = fields.Float(string="Nearest Avg Price", digits=(16, 4))
    nearest_avg_value = fields.Float(string="Nearest Avg Value", digits=(16, 4))
    nearest_trade_count = fields.Integer(string="Nearest Trade Count")

    weekly_instrument = fields.Char(string="Weekly Instrument")
    weekly_avg_price = fields.Float(string="Weekly Avg Price", digits=(16, 4))
    weekly_avg_value = fields.Float(string="Weekly Avg Value", digits=(16, 4))
    weekly_trade_count = fields.Integer(string="Weekly Trade Count")

    monthly_instrument = fields.Char(string="Monthly Instrument")
    monthly_avg_price = fields.Float(string="Monthly Avg Price", digits=(16, 4))
    monthly_avg_value = fields.Float(string="Monthly Avg Value", digits=(16, 4))
    monthly_trade_count = fields.Integer(string="Monthly Trade Count")

    # "all"/"day" span every non-expired instrument at once — no single
    # instrument to report, same None-instrument convention
    # five_a_gamma_json's own scope_from_trades() always used for these 2.
    all_avg_price = fields.Float(string="All Avg Price", digits=(16, 4))
    all_avg_value = fields.Float(string="All Avg Value", digits=(16, 4))
    all_trade_count = fields.Integer(string="All Trade Count")

    day_avg_price = fields.Float(string="Last 24h Avg Price", digits=(16, 4))
    day_avg_value = fields.Float(string="Last 24h Avg Value", digits=(16, 4))
    day_trade_count = fields.Integer(string="Last 24h Trade Count")

    def compute_and_create(self, asset):
        """Same 5-scope average-gamma computation five_a_gamma_json
        (main.py) used to run inline on every request — moved here so
        the 15-minute cron (compute_snapshot()) is the only thing that
        ever pays for it now. Always creates a new row, even when a
        scope has zero trades (reported as the addon's usual 0.0/0
        "absent" sentinel, same as scope_from_trades() always did) —
        unlike dankbit.bands' per-instrument upsert, there's nothing to
        skip-if-unchanged here, and an empty reading right after
        install is itself useful signal (nothing computable yet)."""
        icp = self.env["ir.config_parameter"].sudo()
        if asset == "BTC":
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            step = float(icp.get_param("dankbit.steps", default=100))
        else:
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            step = float(icp.get_param("dankbit.eth_steps", default=50))

        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        STs = np.arange(from_price, to_price, step, dtype=np.float64)

        def scope_from_trades(trades):
            if not trades:
                return 0.0, 0.0, 0
            legs = options_lib.per_leg_gamma(STs, trades)
            pairs = [
                (legs[k]["gamma_price"], legs[k]["gamma_value"])
                for k in ("long_call", "long_put", "short_call", "short_put")
                if legs[k]["gamma_price"]
            ]
            if not pairs:
                return 0.0, 0.0, len(trades)
            avg_price = sum(p for p, _ in pairs) / len(pairs)
            avg_value = sum(v for _, v in pairs) / len(pairs)
            return avg_price, avg_value, len(trades)

        # Naive UTC, same as every other `expiration` domain comparison
        # in this addon.
        def cumulative_through(config_key):
            instrument = icp.get_param(config_key, default="").upper()
            parts = instrument.split("-", 1) if instrument else []
            if len(parts) != 2:
                return self.env["dankbit.trade"], None
            try:
                expiry_dt = datetime.strptime(parts[1], "%d%b%y").replace(hour=8)
            except ValueError:
                return self.env["dankbit.trade"], None
            domain = [
                ("name", "=ilike", f"{asset}-%"),
                ("expiration", ">=", as_of),
                ("expiration", "<=", expiry_dt),
            ]
            return self.env["dankbit.trade"].search(domain), instrument

        bands_model = self.env["dankbit.bands"]
        nearest_expirations = bands_model._distinct_expirations(asset, as_of, 1)
        nearest_instrument = bands_model._format_instrument(asset, nearest_expirations[0]) if nearest_expirations else None
        nearest_trades = (
            self.env["dankbit.trade"].with_context(active_test=False).search([("name", "=ilike", f"{nearest_instrument}-%")])
            if nearest_instrument else self.env["dankbit.trade"]
        )

        weekly_key = "dankbit.eth_weekly_expiry" if asset == "ETH" else "dankbit.weekly_expiry"
        monthly_key = "dankbit.eth_monthly_expiry" if asset == "ETH" else "dankbit.monthly_expiry"
        weekly_trades, weekly_instrument = cumulative_through(weekly_key)
        monthly_trades, monthly_instrument = cumulative_through(monthly_key)

        all_trades = self.env["dankbit.trade"].search([
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", ">=", as_of),
        ])
        # Same "all non-expired instruments" domain as `all`, further
        # restricted to a rolling trailing-24h deribit_ts window (not a
        # UTC-midnight boundary).
        day_trades = self.env["dankbit.trade"].search([
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", ">=", as_of),
            ("deribit_ts", ">=", as_of - timedelta(hours=24)),
        ])

        nearest_price, nearest_value, nearest_count = scope_from_trades(nearest_trades)
        weekly_price, weekly_value, weekly_count = scope_from_trades(weekly_trades)
        monthly_price, monthly_value, monthly_count = scope_from_trades(monthly_trades)
        all_price, all_value, all_count = scope_from_trades(all_trades)
        day_price, day_value, day_count = scope_from_trades(day_trades)

        return self.sudo().create({
            "asset": asset,
            "computed_at": as_of,
            "nearest_instrument": nearest_instrument,
            "nearest_avg_price": nearest_price,
            "nearest_avg_value": nearest_value,
            "nearest_trade_count": nearest_count,
            "weekly_instrument": weekly_instrument,
            "weekly_avg_price": weekly_price,
            "weekly_avg_value": weekly_value,
            "weekly_trade_count": weekly_count,
            "monthly_instrument": monthly_instrument,
            "monthly_avg_price": monthly_price,
            "monthly_avg_value": monthly_value,
            "monthly_trade_count": monthly_count,
            "all_avg_price": all_price,
            "all_avg_value": all_value,
            "all_trade_count": all_count,
            "day_avg_price": day_price,
            "day_avg_value": day_value,
            "day_trade_count": day_count,
        })

    def compute_snapshot(self):
        """Cron entry point (every 15 minutes — see data/ir_cron.xml) —
        the sole source of truth for this model, same isolated-per-asset-
        savepoint pattern dankbit.live.band's own compute_snapshot() uses
        so one asset's failure can't block the other's row for that
        tick. No live request path ever writes here — five_a_gamma_json
        only reads the latest row per asset."""
        for asset in ("BTC", "ETH"):
            try:
                with self.env.cr.savepoint():
                    self.compute_and_create(asset)
            except Exception:
                _logger.exception("dankbit.5a: isolated failure for %s", asset)

    def latest(self, asset):
        """Latest row for `asset`, or an empty recordset if the cron
        hasn't run yet (e.g. right after install) — five_a_gamma_json
        falls back to the usual 0.0/0 "absent" scope shape in that
        case rather than computing live."""
        return self.search([("asset", "=", asset)], order="computed_at desc", limit=1)

    def to_dict(self):
        """This row as the plain dict five_a_gamma_json serves —
        identical shape to what that route used to build inline from a
        live computation, so the /5a/<asset> template's own JS needs no
        changes."""
        self.ensure_one()
        generated_at = None
        if self.computed_at:
            generated_at = int(self.computed_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
        return {
            "asset": self.asset,
            "generated_at": generated_at,
            "scopes": {
                "nearest": {
                    "instrument": self.nearest_instrument or None,
                    "avg_price": self.nearest_avg_price,
                    "avg_value": self.nearest_avg_value,
                    "trade_count": self.nearest_trade_count,
                },
                "weekly": {
                    "instrument": self.weekly_instrument or None,
                    "avg_price": self.weekly_avg_price,
                    "avg_value": self.weekly_avg_value,
                    "trade_count": self.weekly_trade_count,
                },
                "monthly": {
                    "instrument": self.monthly_instrument or None,
                    "avg_price": self.monthly_avg_price,
                    "avg_value": self.monthly_avg_value,
                    "trade_count": self.monthly_trade_count,
                },
                "all": {
                    "instrument": None,
                    "avg_price": self.all_avg_price,
                    "avg_value": self.all_avg_value,
                    "trade_count": self.all_trade_count,
                },
                "day": {
                    "instrument": None,
                    "avg_price": self.day_avg_price,
                    "avg_value": self.day_avg_value,
                    "trade_count": self.day_trade_count,
                },
            },
        }

    @staticmethod
    def empty_dict(asset):
        """Same shape as to_dict(), all scopes at the 0.0/0 "absent"
        sentinel — served by five_a_gamma_json when no row exists yet
        for `asset` (e.g. right after install, before the first cron
        tick), instead of falling back to a live computation."""
        return {
            "asset": asset,
            "generated_at": None,
            "scopes": {
                scope: {"instrument": None, "avg_price": 0.0, "avg_value": 0.0, "trade_count": 0}
                for scope in _SCOPES
            },
        }
