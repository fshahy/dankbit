# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timezone

from odoo import fields, models

from ..controllers import options as options_lib

_logger = logging.getLogger(__name__)


class ZonesExtrema(models.Model):
    _name = "dankbit.zones.extrema"
    _order = "computed_at desc"

    asset = fields.Char(required=True, index=True)
    computed_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    index_price = fields.Float(digits=(16, 4))
    top_intersection = fields.Float(digits=(16, 4))
    bottom_intersection = fields.Float(digits=(16, 4))

    def compute_snapshot(self):
        for asset in ("BTC", "ETH"):
            self._snapshot_asset(asset)

    def _compute_asset(self, asset):
        """Compute index_price, the Longs-vs-Shorts intersection above/below
        price (top_intersection/bottom_intersection), plus the 4 zero-crossing
        box boundaries for `asset` as of now, using trades since today's UTC
        midnight for that asset's single nearest (soonest-to-expire) expiry
        only — mirrors the /<instrument>/zones PNG route called with that
        specific instrument, aggregated per-asset (dankbit.quadrant.gamma
        loops BTC/ETH the same way). Returns None if there's nothing
        computable (missing index price/expiry/trades); callers decide what,
        if anything, to persist from the result."""
        icp = self.env["ir.config_parameter"].sudo()
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)

        if asset == "BTC":
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        else:
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        index_price = self.env["dankbit.trade"].get_index_price(asset)
        if not index_price:
            _logger.warning("_compute_asset: no index price for %s, skipping", asset)
            return None

        Trade = self.env["dankbit.trade"].with_context(active_test=False)

        nearest = Trade.search_read(
            domain=[("name", "=ilike", f"{asset}-%"), ("expiration", ">=", as_of)],
            fields=["expiration"],
            order="expiration asc",
            limit=1,
        )
        if not nearest:
            _logger.warning("_compute_asset: no active expiry for %s, skipping", asset)
            return None
        nearest_expiration = nearest[0]["expiration"]

        midnight_utc = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        domain = [
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", "=", nearest_expiration),
            ("deribit_ts", ">=", midnight_utc),
            ("deribit_ts", "<=", as_of),
        ]
        trades = Trade.search(domain=domain)
        if not trades:
            # No trades since midnight for the nearest expiry (e.g. thin/no
            # activity right before it rolls off) — an all-zero payoffs curve
            # has no real extrema, and argmax/argmin would trivially return
            # index 0 (the configured price-range floor), a meaningless value
            # that looks like real data. Skip instead.
            _logger.warning(
                "_compute_asset: no trades for %s nearest expiry as of %s, skipping",
                asset, as_of,
            )
            return None

        longs_obj, shorts_obj = options_lib.build_zone_curves(
            asset, index_price, trades, from_price, to_price, steps
        )

        STs = longs_obj.STs

        # Zero-crossings of each curve, split into the nearest one above and
        # the nearest one below the current index price (a curve may cross
        # zero more than once, or not at all on a given side — 0.0 means "no
        # crossing on that side", not a real price).
        short_crossings = options_lib.find_zero_crossings(STs, shorts_obj.payoffs)
        long_crossings = options_lib.find_zero_crossings(STs, longs_obj.payoffs)
        short_above = [c for c in short_crossings if c > index_price]
        short_below = [c for c in short_crossings if c < index_price]
        long_above = [c for c in long_crossings if c > index_price]
        long_below = [c for c in long_crossings if c < index_price]

        # Longs-vs-Shorts intersection (where the two payoff curves cross
        # each other, not where either crosses zero), nearest above/below the
        # current index price — same computation as options.zone_summary()'s
        # top_intersection/bottom_intersection, and the same sign-change
        # build_zone_curves() finds internally for its own ±$2000 auto-zoom.
        diff = longs_obj.payoffs - shorts_obj.payoffs
        lvs_crossings = options_lib.find_zero_crossings(STs, diff)
        lvs_above = [c for c in lvs_crossings if c > index_price]
        lvs_below = [c for c in lvs_crossings if c < index_price]

        return {
            "asset": asset,
            "computed_at": as_of,
            "index_price": index_price,
            "top_intersection": min(lvs_above) if lvs_above else 0.0,
            "bottom_intersection": max(lvs_below) if lvs_below else 0.0,
            "short_zero_above_price": min(short_above) if short_above else 0.0,
            "long_zero_above_price": min(long_above) if long_above else 0.0,
            "short_zero_below_price": max(short_below) if short_below else 0.0,
            "long_zero_below_price": max(long_below) if long_below else 0.0,
        }

    def _snapshot_asset(self, asset):
        """Persist a zones-extrema row on the (fixed, every-4h) cron — only
        the historical-line fields (top_intersection/bottom_intersection);
        the box-boundary fields are never persisted, only computed live and
        on demand (see get_box), since the boxes only ever need the latest
        value and are polled far more often than this cron runs."""
        data = self._compute_asset(asset)
        if data is None:
            return
        self.create({
            "asset": data["asset"],
            "computed_at": data["computed_at"],
            "index_price": data["index_price"],
            "top_intersection": data["top_intersection"],
            "bottom_intersection": data["bottom_intersection"],
        })

    def get_box(self, asset):
        """Live zones-box boundaries for `asset`, computed fresh on every
        call — nothing persisted. Called directly by the
        /api/zones-box/<asset> controller."""
        return self._compute_asset(asset)
