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
    # Whether the payoff value at top_intersection/bottom_intersection (where
    # the Longs and Shorts curves cross each other) is above (True) or below
    # (False) the zero-payoff line — the crossing's x-position doesn't say
    # anything about its y-value, see _compute_asset(). Drives the +/- marker
    # drawn above each point on the TradingView chart's Zones Extrema lines.
    top_intersection_positive = fields.Boolean()
    bottom_intersection_positive = fields.Boolean()
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
        """Compute index_price, the highest/lowest Longs-vs-Shorts curve
        intersection (top_intersection/bottom_intersection — not relative to
        index_price, see below), middle_band (average of
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

        # Zero-crossings of each curve. Current price is deliberately not a
        # factor here (same principle as top_intersection/bottom_intersection
        # below): a box boundary is a property of where a curve crosses zero,
        # not of where the index price happens to sit relative to it. Each
        # curve's own highest crossing feeds the "above" box side, its lowest
        # feeds the "below" side — labels kept for backward compatibility
        # (API/JS field names), even though they no longer mean "above/below
        # current price". A curve with only one crossing contributes that
        # same value to both sides; 0.0 still means "no crossing at all" on
        # that curve, not "no crossing on this side".
        short_crossings = options_lib.find_zero_crossings(STs, shorts_obj.payoffs)
        long_crossings = options_lib.find_zero_crossings(STs, longs_obj.payoffs)
        short_above = [max(short_crossings)] if short_crossings else []
        short_below = [min(short_crossings)] if short_crossings else []
        long_above = [max(long_crossings)] if long_crossings else []
        long_below = [min(long_crossings)] if long_crossings else []

        # Longs-vs-Shorts intersection (where the two payoff curves cross
        # each other, not where either crosses zero) — same computation as
        # options.zone_summary()'s top_intersection/bottom_intersection, and
        # the same sign-change build_zone_curves() finds internally for its
        # own ±$2000 auto-zoom. top/bottom are simply the highest/lowest of
        # *all* crossings found, not relative to index_price: when the
        # curves only cross once, that single crossing can land on either
        # side of the current price by a trivial amount, which used to make
        # the "other" field silently read 0.0 even though the plot clearly
        # showed one real intersection — labels kept, but index_price no
        # longer factors into which crossing is "top" vs "bottom".
        diff = longs_obj.payoffs - shorts_obj.payoffs
        lvs_crossings = options_lib.find_zero_crossings(STs, diff)

        # Sign of the payoff *at* each intersection — the crossing's x-price
        # says nothing about whether the curves meet above or below the
        # zero-payoff line (both curves can be simultaneously positive,
        # negative, or straddling zero at that point). longs_obj.payoffs is
        # interchangeable with shorts_obj.payoffs here since the two are
        # equal (by definition) at a crossing; interpolated, not read off
        # the nearest grid point, for a value consistent with the
        # interpolated crossing price itself.
        top_intersection = max(lvs_crossings) if lvs_crossings else 0.0
        bottom_intersection = min(lvs_crossings) if lvs_crossings else 0.0
        top_intersection_positive = bool(np.interp(top_intersection, STs, longs_obj.payoffs) > 0) if lvs_crossings else False
        bottom_intersection_positive = bool(np.interp(bottom_intersection, STs, longs_obj.payoffs) > 0) if lvs_crossings else False

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
            "top_intersection": top_intersection,
            "bottom_intersection": bottom_intersection,
            "top_intersection_positive": top_intersection_positive,
            "bottom_intersection_positive": bottom_intersection_positive,
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
            "top_intersection_positive": data["top_intersection_positive"],
            "bottom_intersection_positive": data["bottom_intersection_positive"],
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
