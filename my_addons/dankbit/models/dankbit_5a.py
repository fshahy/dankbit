# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from odoo import fields, models

from ..controllers import options as options_lib

_logger = logging.getLogger(__name__)

_SCOPES = ("all", "hour4", "hour8", "nearest_hour4", "nearest_hour8")


class Dankbit5A(models.Model):
    _name = "dankbit.5a"
    _description = "Dankbit 5A Gamma Snapshot"
    _order = "asset, computed_at desc"

    """Append-only snapshot backing /5a/<asset>'s 5 horizontal gamma
    lines (All/Last 4h/Last 8h/Nearest 4h/Nearest 8h) — same "create a
    brand new row every cron tick" shape as dankbit.live.band, not the
    continuously-refined-then-frozen upsert dankbit.bands uses per
    instrument. All/Last 4h/Last 8h have no single "instrument" they're
    keyed on (every non-expired instrument at once, just over different
    trailing-hours windows); Nearest 4h/Nearest 8h are isolated to the
    single soonest-expiring active instrument instead, same resolution
    `dankbit.bands.nearest_expiry()` uses, over those same two
    trailing-hours windows.

    Exists because /api/5a-gamma/<asset> (five_a_gamma_json) used to
    compute all scopes fresh on every request via separate
    options.per_leg_gamma() calls, most over an unbounded trade
    window — the heaviest single request in this addon. compute_and_create()
    now does that same computation on a 15-minute cron
    (dankbit_compute_5a_cron) instead, and the controller just reads
    the latest row per asset — a cheap search() instead of a multi-
    second curve rebuild on every page load/poll."""

    asset = fields.Char(required=True, index=True)
    computed_at = fields.Datetime(string="Computed At", default=fields.Datetime.now, index=True)

    # All/Last 4h/Last 8h span every non-expired instrument at once —
    # no single instrument to report, same None-instrument convention
    # this model has always used for its multi-instrument scopes.
    all_avg_price = fields.Float(string="All Avg Price", digits=(16, 4))
    all_avg_value = fields.Float(string="All Avg Value", digits=(16, 4))
    all_trade_count = fields.Integer(string="All Trade Count")

    hour4_avg_price = fields.Float(string="Last 4h Avg Price", digits=(16, 4))
    hour4_avg_value = fields.Float(string="Last 4h Avg Value", digits=(16, 4))
    hour4_trade_count = fields.Integer(string="Last 4h Trade Count")

    hour8_avg_price = fields.Float(string="Last 8h Avg Price", digits=(16, 4))
    hour8_avg_value = fields.Float(string="Last 8h Avg Value", digits=(16, 4))
    hour8_trade_count = fields.Integer(string="Last 8h Trade Count")

    # Nearest 4h/Nearest 8h are both isolated to this same single
    # soonest-expiring active instrument — one shared instrument field
    # rather than two, since a given row can only ever have one
    # "nearest" instrument at the moment it was computed.
    nearest_instrument = fields.Char(string="Nearest Instrument")

    nearest_hour4_avg_price = fields.Float(string="Nearest 4h Avg Price", digits=(16, 4))
    nearest_hour4_avg_value = fields.Float(string="Nearest 4h Avg Value", digits=(16, 4))
    nearest_hour4_trade_count = fields.Integer(string="Nearest 4h Trade Count")

    nearest_hour8_avg_price = fields.Float(string="Nearest 8h Avg Price", digits=(16, 4))
    nearest_hour8_avg_value = fields.Float(string="Nearest 8h Avg Value", digits=(16, 4))
    nearest_hour8_trade_count = fields.Integer(string="Nearest 8h Trade Count")

    def compute_and_create(self, asset):
        """Same average-gamma computation five_a_gamma_json (main.py)
        used to run inline on every request — moved here so the
        15-minute cron (compute_snapshot()) is the only thing that
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
        # in this addon. Every non-expired instrument for `asset`, no
        # expiry cutoff.
        all_trades = self.env["dankbit.trade"].search([
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", ">=", as_of),
        ])
        # Same "all non-expired instruments" domain as `all`, further
        # restricted to a rolling trailing-N-hour deribit_ts window (not
        # a UTC-midnight boundary).
        hour4_trades = self.env["dankbit.trade"].search([
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", ">=", as_of),
            ("deribit_ts", ">=", as_of - timedelta(hours=4)),
        ])
        hour8_trades = self.env["dankbit.trade"].search([
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", ">=", as_of),
            ("deribit_ts", ">=", as_of - timedelta(hours=8)),
        ])

        # Nearest 4h/Nearest 8h — isolated to the single soonest-expiring
        # active instrument (same resolution dankbit.bands.nearest_expiry()
        # uses), restricted to the same 2 trailing-hours windows as
        # hour4_trades/hour8_trades above rather than every non-expired
        # instrument.
        bands_model = self.env["dankbit.bands"]
        nearest_expirations = bands_model._distinct_expirations(asset, as_of, 1)
        nearest_instrument = bands_model._format_instrument(asset, nearest_expirations[0]) if nearest_expirations else None
        if nearest_instrument:
            nearest_domain_base = self.env["dankbit.trade"].with_context(active_test=False)
            nearest_hour4_trades = nearest_domain_base.search([
                ("name", "=ilike", f"{nearest_instrument}-%"),
                ("deribit_ts", ">=", as_of - timedelta(hours=4)),
            ])
            nearest_hour8_trades = nearest_domain_base.search([
                ("name", "=ilike", f"{nearest_instrument}-%"),
                ("deribit_ts", ">=", as_of - timedelta(hours=8)),
            ])
        else:
            nearest_hour4_trades = nearest_hour8_trades = self.env["dankbit.trade"]

        all_price, all_value, all_count = scope_from_trades(all_trades)
        hour4_price, hour4_value, hour4_count = scope_from_trades(hour4_trades)
        hour8_price, hour8_value, hour8_count = scope_from_trades(hour8_trades)
        nearest_hour4_price, nearest_hour4_value, nearest_hour4_count = scope_from_trades(nearest_hour4_trades)
        nearest_hour8_price, nearest_hour8_value, nearest_hour8_count = scope_from_trades(nearest_hour8_trades)

        return self.sudo().create({
            "asset": asset,
            "computed_at": as_of,
            "all_avg_price": all_price,
            "all_avg_value": all_value,
            "all_trade_count": all_count,
            "hour4_avg_price": hour4_price,
            "hour4_avg_value": hour4_value,
            "hour4_trade_count": hour4_count,
            "hour8_avg_price": hour8_price,
            "hour8_avg_value": hour8_value,
            "hour8_trade_count": hour8_count,
            "nearest_instrument": nearest_instrument,
            "nearest_hour4_avg_price": nearest_hour4_price,
            "nearest_hour4_avg_value": nearest_hour4_value,
            "nearest_hour4_trade_count": nearest_hour4_count,
            "nearest_hour8_avg_price": nearest_hour8_price,
            "nearest_hour8_avg_value": nearest_hour8_value,
            "nearest_hour8_trade_count": nearest_hour8_count,
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
                "all": {
                    "instrument": None,
                    "avg_price": self.all_avg_price,
                    "avg_value": self.all_avg_value,
                    "trade_count": self.all_trade_count,
                },
                "hour4": {
                    "instrument": None,
                    "avg_price": self.hour4_avg_price,
                    "avg_value": self.hour4_avg_value,
                    "trade_count": self.hour4_trade_count,
                },
                "hour8": {
                    "instrument": None,
                    "avg_price": self.hour8_avg_price,
                    "avg_value": self.hour8_avg_value,
                    "trade_count": self.hour8_trade_count,
                },
                "nearest_hour4": {
                    "instrument": self.nearest_instrument or None,
                    "avg_price": self.nearest_hour4_avg_price,
                    "avg_value": self.nearest_hour4_avg_value,
                    "trade_count": self.nearest_hour4_trade_count,
                },
                "nearest_hour8": {
                    "instrument": self.nearest_instrument or None,
                    "avg_price": self.nearest_hour8_avg_price,
                    "avg_value": self.nearest_hour8_avg_value,
                    "trade_count": self.nearest_hour8_trade_count,
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
