# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timezone

import numpy as np

from odoo import fields, models

from ..controllers import gamma as gamma_lib
from ..controllers import options as options_lib

_logger = logging.getLogger(__name__)


class ZonesExtrema(models.Model):
    _name = "dankbit.zones.extrema"
    _order = "instrument"

    # One record per instrument (e.g. "BTC-10JUL26"), not per snapshot — see
    # _persist_extrema(). There is deliberately no computed_at/timestamp
    # field: the record's position on the TradingView chart is that
    # instrument's own expiration time (looked up from dankbit_trade.expiration
    # when the API serves this data), not when a row was last written.
    asset = fields.Char(required=True, index=True)
    instrument = fields.Char(required=True, index=True)
    index_price = fields.Float(digits=(16, 4))
    top_intersection = fields.Float(digits=(16, 4))
    bottom_intersection = fields.Float(digits=(16, 4))
    middle_band = fields.Float(digits=(16, 4))

    _sql_constraints = [
        ("instrument_uniq", "unique (instrument)", "Only one zones-extrema record is kept per instrument."),
    ]

    def _distinct_expirations(self, asset, as_of, limit):
        """The `limit` soonest distinct active expirations for `asset`,
        soonest-first. Raw SQL DISTINCT (not search_read+limit, and not
        read_group, which buckets Datetime fields by month by default) — a
        plain limit=N on trade rows could return N rows that all share the
        same nearest expiration (100k+ trades on the nearest expiry alone
        isn't unusual), silently breaking "Nth expiry" semantics;
        DISTINCT+ORDER BY+LIMIT is also far cheaper than fetching enough rows
        to dedupe in Python on a live, frequently-polled route."""
        self.env.cr.execute(
            """
            SELECT DISTINCT expiration FROM dankbit_trade
            WHERE name ILIKE %s AND expiration >= %s
            ORDER BY expiration ASC
            LIMIT %s
            """,
            (f"{asset}-%", as_of, limit),
        )
        return [row[0] for row in self.env.cr.fetchall()]

    def nearest_expiry(self, asset):
        """The single nearest active expiry for `asset`, as a Deribit-style
        day-string (e.g. '9JUL26') — same lookup _compute_asset() uses
        internally for expiry_index=0, exposed standalone (and cheaply, with
        no curve-building) for the TradingView footer, which shows this
        regardless of timeframe unlike the boxes themselves. Returns None if
        there's no active expiry at all."""
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        expirations = self._distinct_expirations(asset, as_of, 1)
        if not expirations:
            return None
        exp = expirations[0]
        return f"{exp.day}{exp.strftime('%b').upper()}{exp.strftime('%y')}"

    def _compute_asset(self, asset, expiry_index=0):
        """Compute index_price, the Longs-vs-Shorts intersection above/below
        price (top_intersection/bottom_intersection), middle_band (average of
        the 4 gamma extrema — see below), plus the 4 zero-crossing box
        boundaries for `asset` as of now, using trades since today's UTC
        midnight for one specific active expiry only — mirrors the
        /<instrument>/zones PNG route called with that specific instrument,
        aggregated per-asset (dankbit.quadrant.gamma loops BTC/ETH the same
        way). `expiry_index` selects which active expiry, in soonest-first
        order: 0 (default) is the nearest one, 1 is the next one after that,
        etc. The result includes that expiry's own `expiration` datetime
        (Deribit's real settlement time, e.g. 08:00 UTC — read directly off
        dankbit_trade.expiration rather than assumed/hardcoded) so callers
        needing "when does this expiry actually end" — e.g. the TradingView
        zones boxes' right edge — don't have to re-derive it. Returns None
        if there's nothing computable (missing index price/expiry at that
        index/trades); callers decide what, if anything, to persist from
        the result."""
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

        expirations = self._distinct_expirations(asset, as_of, expiry_index + 1)
        if len(expirations) <= expiry_index:
            _logger.warning(
                "_compute_asset: no active expiry at index %s for %s, skipping",
                expiry_index, asset,
            )
            return None
        target_expiration = expirations[expiry_index]
        instrument = (
            f"{asset}-{target_expiration.day}"
            f"{target_expiration.strftime('%b').upper()}{target_expiration.strftime('%y')}"
        )

        midnight_utc = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        domain = [
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", "=", target_expiration),
            ("deribit_ts", ">=", midnight_utc),
            ("deribit_ts", "<=", as_of),
        ]
        trades = Trade.search(domain=domain)
        if not trades:
            # No trades since midnight for this expiry (e.g. thin/no activity
            # right before it rolls off) — an all-zero payoffs curve has no
            # real extrema, and argmax/argmin would trivially return index 0
            # (the configured price-range floor), a meaningless value that
            # looks like real data. Skip instead.
            _logger.warning(
                "_compute_asset: no trades for %s expiry index %s as of %s, skipping",
                asset, expiry_index, as_of,
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

        # Middle band: average of the 4 gamma extrema the /<instrument>/zones
        # PNG page's info overlay shows (Long Call/Put Gamma Peak, Short
        # Call/Put Gamma Bottom) — same computation as chart_png_zones,
        # against this same `trades`/`STs` (already the single target
        # expiry's since-midnight trades, so no separate "nearest expiry
        # among trades" re-filtering is needed here unlike chart_png_zones,
        # which accepts a possibly-multi-expiry `trades` set). Short
        # positions carry negative gamma (portfolio_gamma's sign for "sell"
        # is -1), so their extremum is a trough (argmin), not a peak.
        long_calls = trades.filtered(lambda t: t.direction == "buy" and t.option_type == "call")
        long_call_gamma_peak_price = float(STs[int(np.argmax(gamma_lib.portfolio_gamma(STs, long_calls)))])

        long_puts = trades.filtered(lambda t: t.direction == "buy" and t.option_type == "put")
        long_put_gamma_peak_price = float(STs[int(np.argmax(gamma_lib.portfolio_gamma(STs, long_puts)))])

        short_calls = trades.filtered(lambda t: t.direction == "sell" and t.option_type == "call")
        short_call_gamma_bottom_price = float(STs[int(np.argmin(gamma_lib.portfolio_gamma(STs, short_calls)))])

        short_puts = trades.filtered(lambda t: t.direction == "sell" and t.option_type == "put")
        short_put_gamma_bottom_price = float(STs[int(np.argmin(gamma_lib.portfolio_gamma(STs, short_puts)))])

        middle_band = (
            long_call_gamma_peak_price + long_put_gamma_peak_price
            + short_call_gamma_bottom_price + short_put_gamma_bottom_price
        ) / 4.0

        return {
            "asset": asset,
            "instrument": instrument,
            "computed_at": as_of,
            "expiration": target_expiration,
            "index_price": index_price,
            "top_intersection": min(lvs_above) if lvs_above else 0.0,
            "bottom_intersection": max(lvs_below) if lvs_below else 0.0,
            "middle_band": middle_band,
            "short_zero_above_price": min(short_above) if short_above else 0.0,
            "long_zero_above_price": min(long_above) if long_above else 0.0,
            "short_zero_below_price": max(short_below) if short_below else 0.0,
            "long_zero_below_price": max(long_below) if long_below else 0.0,
        }

    def _persist_extrema(self, data):
        """Upsert the one record for `data['instrument']` — only the
        historical-line fields (index_price/top_intersection/
        bottom_intersection/middle_band); the 4 box-boundary fields in `data` are never
        persisted, only ever read live off the return value (see get_box/
        get_box_next), since nothing reads box-boundary history.

        Called from both get_box() (nearest expiry) and get_box_next() (the
        expiry after that) — i.e. piggybacked on the existing zones-box
        polling that already happens at `dankbit.refresh_interval` on every
        TradingView chart page. Since an instrument is typically "next" for
        a while before it becomes "nearest", this means its row starts
        accumulating (and getting refined) even before get_box() ever
        touches it. compute_snapshot() (below) additionally calls
        get_box()/get_box_next() on a 15-minute cron as a fallback for when
        nobody's actually viewing the chart — without it, an instrument that
        was never nearest/next while anyone happened to be watching would
        never get a row at all.

        Either way, this is still enough to build a connected multi-expiry
        history: while an instrument (e.g. "BTC-10JUL26") is nearest/next,
        every poll refines its one row right up until it expires and rolls
        off the active list; at that point a *different* instrument
        ("BTC-11JUL26") becomes nearest/next, so this starts a new row for
        it instead of overwriting the old one. The old row is simply never
        touched again, freezing at its last computed value — which is
        exactly the final point the TradingView chart needs for that expiry
        (see /api/zones-extrema/<asset>).

        get_box()/get_box_next() are `auth="public"` routes, so this runs
        under the anonymous public user by default — which has no access
        rights at all on dankbit.zones.extrema (only base.group_user does,
        see ir.model.access.csv). sudo() here mirrors how the rest of this
        codebase already elevates for public-facing reads/writes (e.g.
        ir.config_parameter.sudo())."""
        self = self.sudo()
        vals = {
            "asset": data["asset"],
            "instrument": data["instrument"],
            "index_price": data["index_price"],
            "top_intersection": data["top_intersection"],
            "bottom_intersection": data["bottom_intersection"],
            "middle_band": data["middle_band"],
        }
        record = self.search([("instrument", "=", data["instrument"])], limit=1)
        if record:
            record.write(vals)
        else:
            self.create(vals)

    def get_box(self, asset):
        """Live zones-box boundaries for `asset`'s nearest active expiry,
        computed fresh on every call. Called directly by the
        /api/zones-box/<asset> controller — as a side effect of every such
        call, also upserts that instrument's zones-extrema record (see
        _persist_extrema); the 4 box-boundary fields themselves are still
        never persisted, only the computed_at moment's index_price/
        top_intersection/bottom_intersection."""
        data = self._compute_asset(asset)
        if data:
            self._persist_extrema(data)
        return data

    def get_box_next(self, asset):
        """Same as get_box(), but for the active expiry immediately after the
        nearest one (expiry_index=1) — a second, independent zones box.
        Called directly by the /api/zones-box-next/<asset> controller, and
        upserts that (different) instrument's zones-extrema record the same
        way get_box() does."""
        data = self._compute_asset(asset, expiry_index=1)
        if data:
            self._persist_extrema(data)
        return data

    def compute_snapshot(self):
        """Cron entry point (every 15 minutes — see data/ir_cron.xml) — a
        fallback so instrument rows keep updating even when nobody's
        actually viewing /chart/BTC or /chart/ETH. get_box()/get_box_next()
        normally only ever run as a side effect of that page's live zones-box
        polling; with no cron at all, an instrument that's never "nearest" or
        "next" while anyone happens to be watching would never get a row —
        a real gap in the per-expiry history, not just a staler point. Calls
        the exact same get_box()/get_box_next() the live endpoints call, for
        both BTC and ETH, so this cron can never compute or persist anything
        a live page view wouldn't have. Only touches dankbit.zones.extrema
        (via _persist_extrema) — the TradingView horizontal price lines
        (delta=0, gamma peak/bottom) are untouched by this or any cron."""
        for asset in ("BTC", "ETH"):
            self.get_box(asset)
            self.get_box_next(asset)
