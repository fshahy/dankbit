# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta, timezone

import numpy as np
from odoo import fields, models

from ..controllers import options as options_lib

_logger = logging.getLogger(__name__)


class ZonesExtrema(models.Model):
    _name = "dankbit.zones.extrema"
    _order = "computed_at desc"

    asset = fields.Char(required=True, index=True)
    computed_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    index_price = fields.Float(digits=(16, 4))
    short_max_price = fields.Float(digits=(16, 4))
    long_min_price = fields.Float(digits=(16, 4))

    def compute_snapshot(self):
        for asset in ("BTC", "ETH"):
            self._snapshot_asset(asset)

    def backfill(self, start=None, days=10, interval_hours=4):
        """Wipe all existing snapshots and recompute a full history at a
        fixed cadence (every `interval_hours`) — 2 rows (BTC + ETH) per tick.
        There is no historical index-price API, so each tick's index_price is
        approximated from that asset's own trade data: the `index_price`
        field recorded on the closest trade at-or-before the tick.

        If `start` (a naive UTC datetime) is given, ticks run forward from
        `start` every `interval_hours` up to now — `days`/backwards-anchoring
        is only used when `start` is omitted. Note a tick landing exactly on
        00:00 UTC makes the "since midnight" trade window collapse to zero
        width (`deribit_ts >= midnight` and `<= as_of` both equal midnight),
        producing a structurally empty, meaningless snapshot regardless of
        actual trading activity — pick a `start` hour that avoids that if it
        matters for your grid."""
        self.search([]).unlink()

        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
        if start is not None:
            start_tick = start
            end_tick = now
        else:
            end_tick = now
            start_tick = end_tick - timedelta(days=days)

        Trade = self.env["dankbit.trade"].with_context(active_test=False)

        tick = start_tick
        while tick <= end_tick:
            for asset in ("BTC", "ETH"):
                price_row = Trade.search_read(
                    domain=[("name", "=ilike", f"{asset}-%"), ("deribit_ts", "<=", tick)],
                    fields=["index_price"],
                    order="deribit_ts desc",
                    limit=1,
                )
                if not price_row or not price_row[0]["index_price"]:
                    _logger.warning(
                        "backfill: no historical index price for %s at %s, skipping", asset, tick
                    )
                    continue
                self._snapshot_asset(asset, as_of=tick, index_price=price_row[0]["index_price"])
            tick += timedelta(hours=interval_hours)

    def _snapshot_asset(self, asset, as_of=None, index_price=None):
        """Create one zones-extrema row for `asset` as of `as_of` (defaults to
        now), using trades since that day's UTC midnight for that asset's
        single nearest (soonest-to-expire, relative to `as_of`) expiry only —
        mirrors the /<instrument>/zones PNG route called with that specific
        instrument, aggregated per-asset (dankbit.quadrant.gamma loops
        BTC/ETH the same way). Shared by the live 4-hourly cron
        (`index_price=None` → live Deribit lookup) and `backfill()` (explicit
        historical `index_price` passed in)."""
        icp = self.env["ir.config_parameter"].sudo()
        as_of = as_of or datetime.now(timezone.utc).replace(tzinfo=None)

        if asset == "BTC":
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        else:
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        if index_price is None:
            index_price = self.env["dankbit.trade"].get_index_price(asset)
        if not index_price:
            _logger.warning("_snapshot_asset: no index price for %s, skipping snapshot", asset)
            return

        # active_test=False: a historical `as_of` may name an expiry that has
        # since passed and been archived (active=False) by _delete_expired_trades.
        Trade = self.env["dankbit.trade"].with_context(active_test=False)

        nearest = Trade.search_read(
            domain=[("name", "=ilike", f"{asset}-%"), ("expiration", ">=", as_of)],
            fields=["expiration"],
            order="expiration asc",
            limit=1,
        )
        if not nearest:
            _logger.warning("_snapshot_asset: no active expiry for %s, skipping snapshot", asset)
            return
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
            # that looks like real data. Skip the snapshot instead.
            _logger.warning(
                "_snapshot_asset: no trades for %s nearest expiry as of %s, skipping snapshot",
                asset, as_of,
            )
            return

        def build_curves(fp, tp, st):
            longs = options_lib.OptionStrat(asset, index_price, fp, tp, st)
            shorts = options_lib.OptionStrat(asset, index_price, fp, tp, st)
            for trade in trades:
                if trade.direction == "buy":
                    if trade.option_type == "call":
                        longs.long_call(trade.strike, trade.price * trade.index_price)
                    elif trade.option_type == "put":
                        longs.long_put(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    if trade.option_type == "call":
                        shorts.short_call(trade.strike, trade.price * trade.index_price)
                    elif trade.option_type == "put":
                        shorts.short_put(trade.strike, trade.price * trade.index_price)
            return longs, shorts

        longs_obj, shorts_obj = build_curves(from_price, to_price, steps)

        # Same crossing-based zoom as /<instrument>/zones — narrows the search
        # to the region where the extrema actually live.
        STs = longs_obj.STs
        diff = longs_obj.payoffs - shorts_obj.payoffs
        crossings = []
        for i in range(len(diff) - 1):
            if not (np.isfinite(diff[i]) and np.isfinite(diff[i + 1])):
                continue
            if diff[i] * diff[i + 1] < 0:
                px = float(STs[i] - diff[i] * (STs[i + 1] - STs[i]) / (diff[i + 1] - diff[i]))
                crossings.append(px)

        if crossings:
            zoom_from = min(crossings) - 2000
            zoom_to = max(crossings) + 2000
            longs_obj, shorts_obj = build_curves(zoom_from, zoom_to, steps)

        STs = longs_obj.STs
        short_max_price = float(STs[int(np.argmax(shorts_obj.payoffs))])
        long_min_price = float(STs[int(np.argmin(longs_obj.payoffs))])

        self.create({
            "asset": asset,
            "computed_at": as_of,
            "index_price": index_price,
            "short_max_price": short_max_price,
            "long_min_price": long_min_price,
        })
