# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from odoo import fields, models

from ..controllers import options as options_lib
from ..controllers import forecast as forecast_lib

_logger = logging.getLogger(__name__)


def _avg_present(*values):
    """Average of whichever of `values` are non-zero (0.0 is this
    addon's standing "absent" sentinel for a price-like field — see
    forecast.per_leg_greeks()), not a fixed division by len(values). A
    leg with no trades in the window has no gamma/delta extremum at all;
    blindly dividing by 4 would silently drag the average toward 0 for
    every such absent leg, same bug the /4l and /mwa AVG price line
    (dankbit_four_leg_gamma_chart_templates.xml) was already written to
    avoid on the frontend. 0.0 (not None) when nothing is present, same
    convention every other "absent" field on this model already uses."""
    present = [v for v in values if v]
    return sum(present) / len(present) if present else 0.0


class Bands(models.Model):
    _name = "dankbit.bands"
    _order = "instrument"

    # One record per instrument (e.g. "BTC-10JUL26"), not per snapshot — see
    # _persist_extrema(). The record's position on the TradingView chart is
    # still that instrument's own expiration time (looked up from
    # dankbit_trade.expiration when the API serves this data), never this
    # field — computed_at is for backend visibility only (e.g. "how stale is
    # this row"), refreshed to the moment _compute_asset() ran on every
    # _persist_extrema() upsert, not just when the row was first created.
    computed_at = fields.Datetime(string="Computed At", default=fields.Datetime.now)
    asset = fields.Char(required=True, index=True)
    instrument = fields.Char(required=True, index=True)
    index_price = fields.Float(digits=(16, 4))
    # High/Resistance and Low/Support — the highest/lowest price where the
    # Longs-vs-Shorts payoff curves cross each other (not where either
    # crosses zero) — renamed from top_intersection/bottom_intersection to
    # match Thales's own "high"/"low" (Resistance/Support) terminology for
    # this reference band, since that's the concept these stand in for.
    high_resistance = fields.Float(string="High/Resistance", digits=(16, 4))
    low_support = fields.Float(string="Low/Support", digits=(16, 4))
    # Whether the payoff value at high_resistance/low_support (where the
    # Longs and Shorts curves cross each other) is above (True) or below
    # (False) the zero-payoff line — the crossing's x-position doesn't say
    # anything about its y-value, see _compute_asset(). Drives the +/- marker
    # drawn above each point on the TradingView chart's High/Resistance and
    # Low/Support lines.
    high_resistance_positive = fields.Boolean(string="High/Resistance Positive")
    low_support_positive = fields.Boolean(string="Low/Support Positive")
    gamma_band = fields.Float(digits=(16, 4))
    delta_band = fields.Float(digits=(16, 4))
    # Thales's newer-version Smart Role-Aware Synthetic Liquidity levels
    # (forecast.smart_synthetic_liquidity) — the same upper/lower levels
    # the Thales Forecast candle engine already uses internally
    # (liquidity_map_engine), computed here from this same instrument's
    # own high_zone_max/low_zone_min/per-leg Greeks/index_price (see
    # _compute_asset) rather than a separate live computation, so this
    # model's own values can never disagree with what that engine sees.
    # 0.0 means absent — that side's blend had no contributing legs (see
    # smart_synthetic_liquidity/weighted_avg2), or high_zone_max<=
    # low_zone_min (no band to compute against) — same "0.0 = absent"
    # convention high_zone_min/max etc. already use on this model.
    smart_liq_upper_price = fields.Float(string="Smart Liquidity Upper", digits=(16, 4))
    smart_liq_lower_price = fields.Float(string="Smart Liquidity Lower", digits=(16, 4))
    # Strength (smart_synthetic_liquidity's upper_liq_m/lower_liq_m) behind
    # the 2 prices above — which side is the stronger/more dominant
    # liquidity level, not just where it sits. Same "0.0 = absent"
    # convention as the price fields.
    smart_liq_upper_strength = fields.Float(string="Smart Liquidity Upper Strength", digits=(16, 4))
    smart_liq_lower_strength = fields.Float(string="Smart Liquidity Lower Strength", digits=(16, 4))
    # High Zone / Low Zone / Middle Zone — same definitions as
    # options.zone_summary()'s high_zone/low_zone/middle_zone (see the
    # /<instrument>/zones PNG page's info overlay): high_zone/low_zone are
    # each curve's own highest/lowest zero-crossing (min/max of the two
    # curves' contributions, a degenerate equal pair when only one curve
    # crosses); middle_zone is min/max of seller_max_profit/buyer_max_loss,
    # always defined. Each stored as a _min/_max pair (a Float can't hold a
    # range) — 0.0 on both sides of high_zone/low_zone means neither curve
    # ever crossed zero, same "0.0 = absent" convention this model already
    # uses for high_resistance/low_support.
    high_zone_min = fields.Float(string="High Zone Min", digits=(16, 4))
    high_zone_max = fields.Float(string="High Zone Max", digits=(16, 4))
    low_zone_min = fields.Float(string="Low Zone Min", digits=(16, 4))
    low_zone_max = fields.Float(string="Low Zone Max", digits=(16, 4))
    middle_zone_min = fields.Float(string="Middle Zone Min", digits=(16, 4))
    middle_zone_max = fields.Float(string="Middle Zone Max", digits=(16, 4))
    # Official display-zone confirmation metadata.  The raw/live zone is
    # still recomputed every hour for Forecast snapshots; these fields tell
    # the chart when the persisted High/Low/Middle zones last passed the
    # session/quality gate (or the two-snapshot structural override).
    zones_confirmed_at = fields.Datetime(string="Zones Confirmed At")
    zones_confirmation_reason = fields.Char(string="Zones Confirmation Reason")
    # Pending hourly candidate used only to require two mutually consistent
    # observations before an out-of-session structural replacement.  It is
    # deliberately separate from the official *_zone_* fields, so an hourly
    # noisy calculation can never leak onto the displayed chart.
    zone_candidate_at = fields.Datetime(string="Zone Candidate At")
    zone_candidate_hits = fields.Integer(string="Zone Candidate Hits", default=0)
    candidate_high_zone_min = fields.Float(digits=(16, 4))
    candidate_high_zone_max = fields.Float(digits=(16, 4))
    candidate_low_zone_min = fields.Float(digits=(16, 4))
    candidate_low_zone_max = fields.Float(digits=(16, 4))
    candidate_middle_zone_min = fields.Float(digits=(16, 4))
    candidate_middle_zone_max = fields.Float(digits=(16, 4))
    candidate_seller_max_profit = fields.Float(digits=(16, 4))
    candidate_buyer_max_loss = fields.Float(digits=(16, 4))
    # Per-leg gamma/delta/theta/vega prices + Abs strength values — same
    # forecast.per_leg_greeks() computation (a thin Pine-naming layer over
    # options.per_leg_greeks()) already used by dankbit.forecast.snapshot,
    # so this model's per-leg numbers can never quietly disagree with the
    # Thales Forecast engine's or the /<instrument>/zones PNG page's own
    # info overlay for the same trades. *_price is the raw extremum price;
    # *_abs is abs(value)/scale (1e6 gamma, 10 delta, 1e4 theta, 100 vega —
    # same scaling the PNG page's own Abs. lines use), not rounded further.
    bcg_price = fields.Float(string="Buyer Call Gamma (BCG)", digits=(16, 4))
    bcg_abs = fields.Float(string="BCG Abs.", digits=(16, 4))
    bpg_price = fields.Float(string="Buyer Put Gamma (BPG)", digits=(16, 4))
    bpg_abs = fields.Float(string="BPG Abs.", digits=(16, 4))
    scg_price = fields.Float(string="Seller Call Gamma (SCG)", digits=(16, 4))
    scg_abs = fields.Float(string="SCG Abs.", digits=(16, 4))
    spg_price = fields.Float(string="Seller Put Gamma (SPG)", digits=(16, 4))
    spg_abs = fields.Float(string="SPG Abs.", digits=(16, 4))
    bcd_price = fields.Float(string="Buyer Call Delta (BCD)", digits=(16, 4))
    bcd_abs = fields.Float(string="BCD Abs.", digits=(16, 4))
    bpd_price = fields.Float(string="Buyer Put Delta (BPD)", digits=(16, 4))
    bpd_abs = fields.Float(string="BPD Abs.", digits=(16, 4))
    scd_price = fields.Float(string="Seller Call Delta (SCD)", digits=(16, 4))
    scd_abs = fields.Float(string="SCD Abs.", digits=(16, 4))
    spd_price = fields.Float(string="Seller Put Delta (SPD)", digits=(16, 4))
    spd_abs = fields.Float(string="SPD Abs.", digits=(16, 4))
    bct_price = fields.Float(string="Buyer Call Theta (BCT)", digits=(16, 4))
    bct_abs = fields.Float(string="BCT Abs.", digits=(16, 4))
    bpt_price = fields.Float(string="Buyer Put Theta (BPT)", digits=(16, 4))
    bpt_abs = fields.Float(string="BPT Abs.", digits=(16, 4))
    sct_price = fields.Float(string="Seller Call Theta (SCT)", digits=(16, 4))
    sct_abs = fields.Float(string="SCT Abs.", digits=(16, 4))
    spt_price = fields.Float(string="Seller Put Theta (SPT)", digits=(16, 4))
    spt_abs = fields.Float(string="SPT Abs.", digits=(16, 4))
    bcv_price = fields.Float(string="Buyer Call Vega (BCV)", digits=(16, 4))
    bcv_abs = fields.Float(string="BCV Abs.", digits=(16, 4))
    bpv_price = fields.Float(string="Buyer Put Vega (BPV)", digits=(16, 4))
    bpv_abs = fields.Float(string="BPV Abs.", digits=(16, 4))
    scv_price = fields.Float(string="Seller Call Vega (SCV)", digits=(16, 4))
    scv_abs = fields.Float(string="SCV Abs.", digits=(16, 4))
    spv_price = fields.Float(string="Seller Put Vega (SPV)", digits=(16, 4))
    spv_abs = fields.Float(string="SPV Abs.", digits=(16, 4))
    # Seller Max Profit / Buyer Max Loss — where the Shorts payoff curve
    # peaks and where the Longs curve bottoms out (renamed from
    # short_max_price/long_min_price to match Thales's own SMP/BML
    # terminology, see dankbit.forecast.snapshot's bml/smp fields) — this
    # model's original two fields (see git history: 8c59981), repurposed
    # into top_intersection/bottom_intersection in 8da5a1a and since
    # reintroduced as their own fields alongside those, computed by the
    # same _compute_asset()/_persist_extrema() path as gamma_band/delta_band
    # (not the old standalone 4h snapshot cron). Same values
    # options.zone_summary()'s seller_max_profit/buyer_max_loss show on the
    # /<instrument>/zones PNG page's info overlay.
    seller_max_profit = fields.Float(string="Seller Max Profit (SMP)", digits=(16, 4))
    buyer_max_loss = fields.Float(string="Buyer Max Loss (BML)", digits=(16, 4))

    _sql_constraints = [
        ("instrument_uniq", "unique (instrument)", "Only one bands record is kept per instrument."),
    ]

    # The 32 per-leg gamma/delta/theta/vega price + Abs field names —
    # exactly forecast_lib.per_leg_greeks()'s own dict keys, which this
    # model's fields are named to match 1:1 (see _compute_asset/
    # _persist_extrema). Listed once here rather than by hand in both
    # places.
    _PER_LEG_GREEK_FIELDS = [
        "bcg_price", "bcg_abs", "bpg_price", "bpg_abs",
        "scg_price", "scg_abs", "spg_price", "spg_abs",
        "bcd_price", "bcd_abs", "bpd_price", "bpd_abs",
        "scd_price", "scd_abs", "spd_price", "spd_abs",
        "bct_price", "bct_abs", "bpt_price", "bpt_abs",
        "sct_price", "sct_abs", "spt_price", "spt_abs",
        "bcv_price", "bcv_abs", "bpv_price", "bpv_abs",
        "scv_price", "scv_abs", "spv_price", "spv_abs",
    ]

    def _distinct_expirations(self, asset, as_of, limit, future_days_only=False):
        """The `limit` soonest distinct active expirations for `asset`,
        soonest-first. Raw SQL DISTINCT (not search_read+limit, and not
        read_group, which buckets Datetime fields by month by default) — a
        plain limit=N on trade rows could return N rows that all share the
        same nearest expiration (100k+ trades on the nearest expiry alone
        isn't unusual), silently breaking "Nth expiry" semantics;
        DISTINCT+ORDER BY+LIMIT is also far cheaper than fetching enough rows
        to dedupe in Python on a live, frequently-polled route."""
        lower_bound = as_of
        if future_days_only:
            # Forecast E1/E2/E3 are calendar-day anchors: today's still-open
            # contract must never occupy E1 before its settlement hour.
            lower_bound = as_of.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        self.env.cr.execute(
            """
            SELECT DISTINCT expiration FROM dankbit_trade
            WHERE name ILIKE %s AND expiration >= %s
            ORDER BY expiration ASC
            LIMIT %s
            """,
            (f"{asset}-%", lower_bound, limit),
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @staticmethod
    def _format_instrument(asset, exp):
        """`asset` + a raw expiration datetime -> Deribit-style instrument
        string (e.g. 'BTC-9JUL26'), the convention every other expiry
        identifier in this addon uses."""
        return f"{asset}-{exp.day}{exp.strftime('%b').upper()}{exp.strftime('%y')}"

    def nearest_expiry(self, asset):
        """The single nearest active expiry for `asset`, as a full
        Deribit-style instrument string (e.g. 'BTC-9JUL26', matching the
        convention every other expiry identifier in this addon uses —
        weekly_expiry/monthly_expiry, INSTRUMENT/MONTHLY_INST, this
        model's own `instrument` field) — same lookup _compute_asset() uses
        internally for expiry_index=0, exposed standalone (and cheaply, with
        no curve-building) for the TradingView footer, which shows this
        regardless of timeframe unlike the boxes themselves. Returns None if
        there's no active expiry at all."""
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        expirations = self._distinct_expirations(asset, as_of, 1)
        if not expirations:
            return None
        return self._format_instrument(asset, expirations[0])

    def _compute_asset(self, asset, expiry_index=0, hours=None, future_days_only=False):
        """Compute index_price, the highest/lowest Longs-vs-Shorts curve
        intersection (high_resistance/low_support — not relative to
        index_price, see below), gamma_band (average of
        the 4 gamma extrema — see below), plus the 4 zero-crossing box
        boundaries for `asset` as of now, for one specific active expiry
        only — mirrors the /<instrument>/zones PNG route called with that
        specific instrument, aggregated per-asset. Trades are taken since
        the most recent UTC midnight by default (options.day_window_start);
        passing `hours` instead restricts
        to the trailing `hours` hours through now — used by /chart/<asset>'s
        00:00-vs-trailing-hours radio toggle (see get_box,
        dankbit_templates.xml). `expiry_index` selects which active expiry, in soonest-first
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

        expirations = self._distinct_expirations(
            asset, as_of, expiry_index + 1, future_days_only=future_days_only,
        )
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

        window_start = (
            as_of - timedelta(hours=hours) if hours is not None
            else options_lib.day_window_start(as_of)
        )
        domain = [
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", "=", target_expiration),
            ("deribit_ts", ">=", window_start),
            ("deribit_ts", "<=", as_of),
        ]
        trades = Trade.search(domain=domain)
        if not trades:
            # No trades in the trade window for this expiry (e.g. thin/no
            # activity right before it rolls off) — an all-zero payoffs
            # curve has no real extrema, and argmax/argmin would trivially
            # return index 0 (the configured price-range floor), a
            # meaningless value that looks like real data. Skip instead.
            _logger.warning(
                "_compute_asset: no trades for %s expiry index %s as of %s, skipping",
                asset, expiry_index, as_of,
            )
            return None

        # Raw Long/Short trade counts (same direction == "buy"/"sell" split
        # main.py's chart_png_zones already shows on the /<instrument>/zones
        # PNG page's own "N longs / N shorts (since 00:00 UTC)" annotation)
        # — feeds forecast.flow_imbalance()'s FlowImbalance damping (see
        # CLAUDE.md's Thales Forecast candles section, per Thales dev
        # feedback that a near-equal Long/Short count should keep the
        # forecast body smaller even when the Greek levels point one way).
        # Extra return field, not persisted onto this model (see the 4
        # zero-crossing box-boundary fields below for the same pattern) —
        # dankbit.forecast.snapshot reads it straight off this dict.
        long_trade_count = len(trades.filtered(lambda t: t.direction == "buy"))
        short_trade_count = len(trades.filtered(lambda t: t.direction == "sell"))

        longs_obj, shorts_obj = options_lib.build_zone_curves(
            asset, index_price, trades, from_price, to_price, steps
        )

        STs = longs_obj.STs

        # Where the Shorts curve peaks and the Longs curve bottoms out — same
        # computation as options.zone_summary()'s seller_max_profit/
        # buyer_max_loss, against this same longs_obj/shorts_obj.
        seller_max_profit = float(STs[int(np.argmax(shorts_obj.payoffs))])
        buyer_max_loss = float(STs[int(np.argmin(longs_obj.payoffs))])

        # Zero-crossings of each curve. Current price is deliberately not a
        # factor here (same principle as high_resistance/low_support
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

        # High Zone / Low Zone — same "each curve's own highest/lowest
        # zero-crossing" definition as options.zone_summary()'s
        # high_zone/low_zone (see the /<instrument>/zones PNG page's info
        # overlay), built from the same short_crossings/long_crossings
        # already computed above rather than calling zone_summary() and
        # re-finding the crossings a second time. 0.0/0.0 means neither
        # curve ever crossed zero, same convention short_above/etc. use.
        high_zone_prices = short_above + long_above
        low_zone_prices = short_below + long_below
        high_zone_min = min(high_zone_prices) if high_zone_prices else 0.0
        high_zone_max = max(high_zone_prices) if high_zone_prices else 0.0
        low_zone_min = min(low_zone_prices) if low_zone_prices else 0.0
        low_zone_max = max(low_zone_prices) if low_zone_prices else 0.0

        # Middle Zone — bounded by seller_max_profit/buyer_max_loss (min/max
        # of the two), same as options.zone_summary()'s middle_zone. Always
        # defined, unlike high_zone/low_zone, since seller_max_profit/
        # buyer_max_loss are argmax/argmin over the full curve, not
        # zero-crossings.
        middle_zone_min = min(seller_max_profit, buyer_max_loss)
        middle_zone_max = max(seller_max_profit, buyer_max_loss)

        # Longs-vs-Shorts intersection (where the two payoff curves cross
        # each other, not where either crosses zero) — same computation as
        # options.zone_summary()'s high_resistance/low_support, and
        # the same sign-change build_zone_curves() finds internally for its
        # own ±$2000 auto-zoom. high/low are simply the highest/lowest of
        # *all* crossings found, not relative to index_price: when the
        # curves only cross once, that single crossing can land on either
        # side of the current price by a trivial amount, which used to make
        # the "other" field silently read 0.0 even though the plot clearly
        # showed one real intersection — labels kept, but index_price no
        # longer factors into which crossing is "high" vs "low".
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
        high_resistance = max(lvs_crossings) if lvs_crossings else 0.0
        low_support = min(lvs_crossings) if lvs_crossings else 0.0
        high_resistance_positive = bool(np.interp(high_resistance, STs, longs_obj.payoffs) > 0) if lvs_crossings else False
        low_support_positive = bool(np.interp(low_support, STs, longs_obj.payoffs) > 0) if lvs_crossings else False

        # Per-leg gamma/delta/theta/vega prices + Abs strength values, via
        # forecast.per_leg_greeks() (a thin Pine-naming layer over
        # options.per_leg_greeks(), the single source of truth for this
        # computation — chart_png_zones, dankbit.forecast.snapshot, and
        # this model can never quietly disagree). `trades` is already this
        # target expiry's since-midnight set, so no separate "nearest
        # expiry among trades" re-filtering is needed here unlike
        # chart_png_zones, which accepts a possibly-multi-expiry `trades`
        # set. Returns bcg_price/bcg_abs.../spv_price/spv_abs — the exact
        # field names this model persists, spread directly into the
        # returned dict below.
        legs = forecast_lib.per_leg_greeks(STs, trades)

        # Gamma band: average of the 4 gamma extrema the /<instrument>/zones
        # PNG page's info overlay shows (Buyer Call Gamma/Buyer Put Gamma,
        # Seller Call Gamma/Seller Put Gamma — BCG/BPG/SCG/SPG). Short
        # positions carry negative gamma (portfolio_gamma's sign for "sell"
        # is -1), so their extremum is a trough, not a peak — already
        # accounted for by per_leg_greeks(). Averaged via _avg_present(), not
        # a fixed /4.0 — a leg with zero trades in this window reports
        # *_price as 0.0/absent (see forecast.per_leg_greeks()), and a plain
        # /4.0 would silently drag the whole band toward the configured
        # chart price floor for every such quiet leg instead of excluding it.
        gamma_band = _avg_present(legs["bcg_price"], legs["bpg_price"], legs["scg_price"], legs["spg_price"])

        # Delta band: average of the price where each leg's delta curve
        # reaches 90% of its own extreme value in this window (see
        # options.delta_saturation_price/DELTA_SATURATION_FRACTION) — deep
        # enough ITM that the option has stopped behaving like an option and
        # starts moving ~1:1 with the underlying, i.e. where the sigmoid-
        # shaped delta curve stops curving and flattens into a straight
        # line. Relative to the curve's own extreme, not an absolute delta
        # value: portfolio_delta sums sign*amount*per-contract delta across
        # every matching trade, so its scale reflects total traded size (can
        # be in the hundreds), not a single option's [-1, 1] range — shared
        # with the /<instrument>/lp,lc,sp,sc single-leg routes' own green
        # marker line, so the two can never disagree on where this point is.
        # Same _avg_present() treatment as gamma_band above, for the same
        # reason — a quiet leg must be excluded, not averaged in at 0.0.
        delta_band = _avg_present(legs["bcd_price"], legs["bpd_price"], legs["scd_price"], legs["spd_price"])

        # Smart Role-Aware Synthetic Liquidity (see forecast.
        # smart_synthetic_liquidity) — against this same instrument's own
        # high_zone_max/low_zone_min band and per-leg legs dict (already
        # shaped exactly as that function expects, same keys
        # per_leg_greeks() returns), role_close = this same index_price.
        # 0.0 means absent (degenerate top<=low band, or that side's blend
        # had no contributing legs — see weighted_avg2), same "0.0 = absent"
        # convention high_zone_min/max etc. already use on this model,
        # rather than introducing None as a second sentinel type here.
        smart_liq_top, smart_liq_low = high_zone_max, low_zone_min
        smart_liq_band_width = smart_liq_top - smart_liq_low
        smart_liq_upper_price = 0.0
        smart_liq_lower_price = 0.0
        smart_liq_upper_strength = 0.0
        smart_liq_lower_strength = 0.0
        if smart_liq_band_width > 0:
            smart_liq = forecast_lib.smart_synthetic_liquidity(
                legs, smart_liq_top, smart_liq_low, smart_liq_band_width, index_price,
            )
            smart_liq_upper_price = smart_liq["upper_liq_price"] or 0.0
            smart_liq_lower_price = smart_liq["lower_liq_price"] or 0.0
            smart_liq_upper_strength = smart_liq["upper_liq_m"] or 0.0
            smart_liq_lower_strength = smart_liq["lower_liq_m"] or 0.0

        return {
            "asset": asset,
            "instrument": instrument,
            "computed_at": as_of,
            "expiration": target_expiration,
            "index_price": index_price,
            "long_trade_count": long_trade_count,
            "short_trade_count": short_trade_count,
            "high_resistance": high_resistance,
            "low_support": low_support,
            "high_resistance_positive": high_resistance_positive,
            "low_support_positive": low_support_positive,
            "gamma_band": gamma_band,
            "delta_band": delta_band,
            "smart_liq_upper_price": smart_liq_upper_price,
            "smart_liq_lower_price": smart_liq_lower_price,
            "smart_liq_upper_strength": smart_liq_upper_strength,
            "smart_liq_lower_strength": smart_liq_lower_strength,
            "high_zone_min": high_zone_min,
            "high_zone_max": high_zone_max,
            "low_zone_min": low_zone_min,
            "low_zone_max": low_zone_max,
            "middle_zone_min": middle_zone_min,
            "middle_zone_max": middle_zone_max,
            "seller_max_profit": seller_max_profit,
            "buyer_max_loss": buyer_max_loss,
            "short_zero_above_price": min(short_above) if short_above else 0.0,
            "long_zero_above_price": min(long_above) if long_above else 0.0,
            "short_zero_below_price": max(short_below) if short_below else 0.0,
            "long_zero_below_price": max(long_below) if long_below else 0.0,
            # Individual per-leg gamma/delta/theta/vega prices + Abs values
            # (bcg_price/bcg_abs.../spv_price/spv_abs) behind gamma_band/
            # delta_band and this model's own per-leg fields — spread
            # straight from forecast_lib.per_leg_greeks()'s dict, whose
            # keys already match this model's field names 1:1.
            **legs,
        }

    def _persist_extrema(self, data, zones_confirmation_reason="session-confirmed"):
        """Upsert the one record for `data['instrument']` — only the
        historical-line fields (computed_at/index_price/high_resistance/
        low_support/gamma_band/delta_band/smart_liq_upper_price/
        smart_liq_lower_price/high_zone/low_zone/middle_zone/
        seller_max_profit/buyer_max_loss, plus the 32 per-leg gamma/delta/
        theta/vega price+Abs fields — see _PER_LEG_GREEK_FIELDS);
        computed_at is refreshed to `data['computed_at']` (the moment
        _compute_asset() ran) on every confirmed upsert.  Zone boundaries,
        SMP/BML, and their confirmation metadata are persisted as the
        official display snapshot; raw hourly Forecast values are kept on
        the separate forecast.snapshot path.

        Called only from compute_snapshot()'s hourly cron (see
        TRACKED_EXPIRY_COUNT below), for every tracked expiry_index
        including 0 — there is no browser-triggered live path at all,
        deliberately, for any of them: /api/zones-box/<asset> (the "Zones"
        checkbox) computes the nearest expiry's box boundaries fresh on
        every request via get_box() -> _compute_asset() directly, without
        ever routing through here, and the "Bands" checkbox's
        refreshBands() only reads already-persisted rows
        (/api/bands/<asset>). So opening the chart, toggling either
        checkbox, or switching timeframe can never affect when these rows
        update. Since an instrument is typically "tracked" (index 1+) for a
        while before it becomes nearest, its row starts accumulating (and
        getting refined) via the cron well before it ever becomes the
        nearest expiry.

        Either way, this is still enough to build a connected multi-expiry
        history: while an instrument (e.g. "BTC-10JUL26") is tracked, every
        poll refines its one row right up until it expires and rolls off the
        active list; at that point a *different* instrument ("BTC-11JUL26")
        takes its place, so this starts a new row for it instead of
        overwriting the old one. The old row is simply never touched again,
        freezing at its last computed value — which is exactly the final
        point the TradingView chart needs for that expiry (see
        /api/bands/<asset>).

get_box_n() (the only caller of this method) is itself only ever
        called by compute_snapshot()'s cron, not from any public HTTP
        route — get_box() (which *is* reached from the public
        /api/zones-box/<asset> route) deliberately calls _compute_asset()
        directly instead, bypassing get_box_n()/this method entirely, so
        that no anonymous request can ever write here. sudo() is kept
        anyway as a defensive backstop (the cron's own user_id isn't
        guaranteed to have write access on dankbit.bands, which only
        grants base.group_user — see ir.model.access.csv) and mirrors how
        the rest of this codebase already elevates for writes that
        shouldn't depend on the caller's own permissions (e.g.
        ir.config_parameter.sudo())."""
        self = self.sudo()
        vals = {
            "computed_at": data["computed_at"],
            "asset": data["asset"],
            "instrument": data["instrument"],
            "index_price": data["index_price"],
            "high_resistance": data["high_resistance"],
            "low_support": data["low_support"],
            "high_resistance_positive": data["high_resistance_positive"],
            "low_support_positive": data["low_support_positive"],
            "gamma_band": data["gamma_band"],
            "delta_band": data["delta_band"],
            "smart_liq_upper_price": data["smart_liq_upper_price"],
            "smart_liq_lower_price": data["smart_liq_lower_price"],
            "smart_liq_upper_strength": data["smart_liq_upper_strength"],
            "smart_liq_lower_strength": data["smart_liq_lower_strength"],
            "high_zone_min": data["high_zone_min"],
            "high_zone_max": data["high_zone_max"],
            "low_zone_min": data["low_zone_min"],
            "low_zone_max": data["low_zone_max"],
            "middle_zone_min": data["middle_zone_min"],
            "middle_zone_max": data["middle_zone_max"],
            "seller_max_profit": data["seller_max_profit"],
            "buyer_max_loss": data["buyer_max_loss"],
            "zones_confirmed_at": data["computed_at"],
            "zones_confirmation_reason": zones_confirmation_reason,
            "zone_candidate_at": False,
            "zone_candidate_hits": 0,
            "candidate_high_zone_min": 0.0,
            "candidate_high_zone_max": 0.0,
            "candidate_low_zone_min": 0.0,
            "candidate_low_zone_max": 0.0,
            "candidate_middle_zone_min": 0.0,
            "candidate_middle_zone_max": 0.0,
            "candidate_seller_max_profit": 0.0,
            "candidate_buyer_max_loss": 0.0,
            **{f: data[f] for f in self._PER_LEG_GREEK_FIELDS},
        }
        record = self.search([("instrument", "=", data["instrument"])], limit=1)
        if record:
            record.write(vals)
        else:
            self.create(vals)

    # How many active expiries (soonest-first, 0 = nearest) get a persisted
    # bands row at all — the TradingView chart only draws an actual
    # box for expiry_index 0 (yellow), but every index up to this bound still
    # feeds the High/Resistance, Low/Support, and Gamma Band term-structure
    # lines (see get_box_n/refreshBands), which render whatever rows
    # exist for the asset regardless of whether a box was ever drawn for
    # them. Every tracked expiry_index, including 0, is only ever computed
    # by the hourly compute_snapshot() cron (see below) — there is no
    # browser-triggered live path for any of them; the "Zones" checkbox's
    # own live box rendering goes through get_box() -> _compute_asset()
    # directly and never persists.
    TRACKED_EXPIRY_COUNT = 3

    # Thales online safety layer:
    # Green/Red Bands are STRUCTURAL levels (upper/lower Longs-vs-Shorts
    # intersections).  They should not be re-confirmed at 00:00 UTC when
    # the since-midnight option-flow window is still thin, and they should
    # not be pulled inward by Smart Liquidity.  The hourly cron may still
    # compute raw values, but persisted/displayed Bands are confirmed only
    # in mature market sessions and only when the data-quality gate passes.
    # Times are UTC.
    BAND_CONFIRMATION_WINDOWS_UTC = (
        (8, 30, 10, 30, "London Mature"),
        (14, 0, 15, 30, "NY/London Overlap"),
    )

    def _band_confirmation_session(self, when_utc=None):
        """Return the session name if `when_utc` is inside a mature
        Band-confirmation window; otherwise return None.  Midnight UTC is
        deliberately never a confirmation point: at that moment the daily
        trade window has just reset, so the curve intersections are often
        under-sampled and can collapse into unrealistically tight bands."""
        when_utc = when_utc or datetime.now(timezone.utc)
        if when_utc.tzinfo is None:
            when_utc = when_utc.replace(tzinfo=timezone.utc)
        minute_of_day = when_utc.hour * 60 + when_utc.minute
        for sh, sm, eh, em, name in self.BAND_CONFIRMATION_WINDOWS_UTC:
            start = sh * 60 + sm
            end = eh * 60 + em
            if start <= minute_of_day <= end:
                return name
        return None

    def _band_quality_gate(self, data):
        """True when a freshly computed raw band is reliable enough to
        replace the last confirmed Green/Red Bands.  This prevents the
        common 00:00/low-flow failure mode where high_resistance and
        low_support are produced from too few trades or collapse into a
        narrow, non-structural range."""
        icp = self.env["ir.config_parameter"].sudo()
        asset = (data.get("asset") or "BTC").upper()
        index_price = float(data.get("index_price") or 0.0)
        high = float(data.get("high_resistance") or 0.0)
        low = float(data.get("low_support") or 0.0)
        if not index_price or not high or not low or high <= low:
            return False, "missing-or-collapsed-intersections"

        total_trades = int(data.get("long_trade_count") or 0) + int(data.get("short_trade_count") or 0)
        long_trades = int(data.get("long_trade_count") or 0)
        short_trades = int(data.get("short_trade_count") or 0)
        min_total = int(icp.get_param(
            "dankbit.band_min_total_trades_btc" if asset.startswith("BTC") else "dankbit.band_min_total_trades_eth",
            default=80 if asset.startswith("BTC") else 40,
        ))
        min_side = int(icp.get_param(
            "dankbit.band_min_side_trades_btc" if asset.startswith("BTC") else "dankbit.band_min_side_trades_eth",
            default=10 if asset.startswith("BTC") else 5,
        ))
        if total_trades < min_total or long_trades < min_side or short_trades < min_side:
            return False, "insufficient-long-short-sample"

        min_width_pct = float(icp.get_param("dankbit.band_min_width_pct", default=0.015))
        if (high - low) < index_price * min_width_pct:
            return False, "band-width-below-minimum"

        greek_abs_fields = [f for f in self._PER_LEG_GREEK_FIELDS if f.endswith("_abs")]
        total_greek_abs = sum(abs(float(data.get(f) or 0.0)) for f in greek_abs_fields)
        if total_greek_abs <= 0:
            return False, "no-greek-strength"

        return True, "confirmed"

    def _zone_quality_gate(self, data):
        """Validate the raw hourly High/Low/Middle zone geometry.

        Thin High/Low zones are valid and are intentionally *not* rejected:
        their width is the real distance between the Buyer and Seller
        break-even crossings.  The gate instead requires both curves to
        contribute a crossing, sane ordering, a bounded maximum width, a
        valid SMP/BML middle zone, and the same trade/Greek reliability used
        for structural Bands.
        """
        band_ok, band_reason = self._band_quality_gate(data)
        if not band_ok:
            return False, band_reason

        required_crossings = (
            "short_zero_above_price", "long_zero_above_price",
            "short_zero_below_price", "long_zero_below_price",
        )
        if any(float(data.get(name) or 0.0) <= 0 for name in required_crossings):
            return False, "missing-buyer-or-seller-break-even"

        high_min = float(data.get("high_zone_min") or 0.0)
        high_max = float(data.get("high_zone_max") or 0.0)
        low_min = float(data.get("low_zone_min") or 0.0)
        low_max = float(data.get("low_zone_max") or 0.0)
        middle_min = float(data.get("middle_zone_min") or 0.0)
        middle_max = float(data.get("middle_zone_max") or 0.0)
        index_price = float(data.get("index_price") or 0.0)
        if not all((high_min, high_max, low_min, low_max, middle_min, middle_max, index_price)):
            return False, "missing-zone-boundary"
        if high_max < high_min or low_max < low_min or middle_max < middle_min:
            return False, "inverted-zone-boundary"
        if low_max >= high_min:
            return False, "overlapping-high-low-zones"
        if not (low_max <= middle_max and middle_min <= high_min):
            return False, "middle-zone-outside-outer-zones"

        icp = self.env["ir.config_parameter"].sudo()
        max_width_pct = float(icp.get_param("dankbit.zone_max_width_pct", default=0.03))
        if (high_max - high_min) > index_price * max_width_pct:
            return False, "high-zone-too-wide"
        if (low_max - low_min) > index_price * max_width_pct:
            return False, "low-zone-too-wide"

        # The Green/Red intersections are independently computed, but under
        # healthy curve geometry they sit inside or close to the corresponding
        # break-even zone.  A generous tolerance catches broken/noisy curves
        # without forcing genuinely thin zones to be widened.
        proximity_pct = float(icp.get_param("dankbit.zone_band_proximity_pct", default=0.003))
        proximity = index_price * proximity_pct
        high_band = float(data.get("high_resistance") or 0.0)
        low_band = float(data.get("low_support") or 0.0)
        if high_band < high_min - proximity or high_band > high_max + proximity:
            return False, "upper-band-not-near-high-zone"
        if low_band < low_min - proximity or low_band > low_max + proximity:
            return False, "lower-band-not-near-low-zone"

        return True, "zone-quality-confirmed"

    @staticmethod
    def _zone_center(data, prefix):
        return (
            float(data.get(f"{prefix}_zone_min") or 0.0)
            + float(data.get(f"{prefix}_zone_max") or 0.0)
        ) / 2.0

    def _zone_structural_change_gate(self, record, data):
        """Whether a valid hourly candidate is materially different enough
        to justify an out-of-session replacement after two matching scans."""
        if not record or not (record.zones_confirmed_at or (record.high_zone_min and record.high_zone_max and record.low_zone_min and record.low_zone_max)):
            return False, "no-confirmed-zone-baseline"

        index_price = float(data.get("index_price") or 0.0)
        if not index_price:
            return False, "missing-index-price"
        icp = self.env["ir.config_parameter"].sudo()
        outer_shift_pct = float(icp.get_param("dankbit.zone_emergency_outer_shift_pct", default=0.006))
        middle_shift_pct = float(icp.get_param("dankbit.zone_emergency_middle_shift_pct", default=0.004))
        width_ratio = float(icp.get_param("dankbit.zone_emergency_width_ratio", default=1.75))
        breakout_buffer_pct = float(icp.get_param("dankbit.zone_emergency_breakout_buffer_pct", default=0.002))

        old_high_center = (record.high_zone_min + record.high_zone_max) / 2.0
        old_low_center = (record.low_zone_min + record.low_zone_max) / 2.0
        old_middle_center = (record.middle_zone_min + record.middle_zone_max) / 2.0
        new_high_center = self._zone_center(data, "high")
        new_low_center = self._zone_center(data, "low")
        new_middle_center = self._zone_center(data, "middle")

        if abs(new_high_center - old_high_center) >= index_price * outer_shift_pct:
            return True, "upper-zone-center-shift"
        if abs(new_low_center - old_low_center) >= index_price * outer_shift_pct:
            return True, "lower-zone-center-shift"
        if abs(new_middle_center - old_middle_center) >= index_price * middle_shift_pct:
            return True, "middle-zone-center-shift"

        def width_changed(old_min, old_max, new_min, new_max):
            old_width = max(float(old_max) - float(old_min), 0.0)
            new_width = max(float(new_max) - float(new_min), 0.0)
            floor = max(index_price * 0.00025, 1.0)
            old_width = max(old_width, floor)
            new_width = max(new_width, floor)
            return max(old_width, new_width) / min(old_width, new_width) >= width_ratio

        if width_changed(record.high_zone_min, record.high_zone_max, data["high_zone_min"], data["high_zone_max"]):
            return True, "upper-zone-width-regime-change"
        if width_changed(record.low_zone_min, record.low_zone_max, data["low_zone_min"], data["low_zone_max"]):
            return True, "lower-zone-width-regime-change"
        if width_changed(record.middle_zone_min, record.middle_zone_max, data["middle_zone_min"], data["middle_zone_max"]):
            return True, "middle-zone-width-regime-change"

        breakout_buffer = index_price * breakout_buffer_pct
        min_breakout_migration = index_price * middle_shift_pct
        if (
            index_price > record.high_zone_max + breakout_buffer
            and new_high_center > old_high_center + min_breakout_migration
        ):
            return True, "price-accepted-above-confirmed-zone"
        if (
            index_price < record.low_zone_min - breakout_buffer
            and new_low_center < old_low_center - min_breakout_migration
        ):
            return True, "price-accepted-below-confirmed-zone"
        return False, "no-material-zone-change"

    def _stage_zone_candidate(self, record, data):
        """Store an hourly candidate and return its consecutive hit count.

        A candidate counts as consecutive when all three zone centers remain
        within the configured similarity distance of the previous candidate.
        This prevents one noisy hourly curve from triggering the emergency
        path outside the normal confirmation sessions.
        """
        if not record:
            return 0
        index_price = float(data.get("index_price") or 0.0)
        icp = self.env["ir.config_parameter"].sudo()
        tolerance = index_price * float(icp.get_param("dankbit.zone_candidate_similarity_pct", default=0.0025))
        previous = {
            "high_zone_min": record.candidate_high_zone_min,
            "high_zone_max": record.candidate_high_zone_max,
            "low_zone_min": record.candidate_low_zone_min,
            "low_zone_max": record.candidate_low_zone_max,
            "middle_zone_min": record.candidate_middle_zone_min,
            "middle_zone_max": record.candidate_middle_zone_max,
        }
        has_previous = bool(record.zone_candidate_at and record.zone_candidate_hits)
        matching = has_previous and all(
            abs(self._zone_center(data, prefix) - self._zone_center(previous, prefix)) <= tolerance
            for prefix in ("high", "low", "middle")
        )
        hits = int(record.zone_candidate_hits or 0) + 1 if matching else 1
        record.sudo().write({
            "zone_candidate_at": data["computed_at"],
            "zone_candidate_hits": hits,
            "candidate_high_zone_min": data["high_zone_min"],
            "candidate_high_zone_max": data["high_zone_max"],
            "candidate_low_zone_min": data["low_zone_min"],
            "candidate_low_zone_max": data["low_zone_max"],
            "candidate_middle_zone_min": data["middle_zone_min"],
            "candidate_middle_zone_max": data["middle_zone_max"],
            "candidate_seller_max_profit": data["seller_max_profit"],
            "candidate_buyer_max_loss": data["buyer_max_loss"],
        })
        return hits

    def _gamma_quality_gate(self, data):
        """True when the freshly computed Gamma Average is reliable enough
        to update the live Gamma Band independently from confirmed
        Green/Red Bands.  This keeps the middle Gamma Magnet more current
        than the structural Bands, while still avoiding the 00:00 UTC
        low-volume distortion problem.

        Unlike _band_quality_gate(), this does not require both Long and
        Short sides to be well populated, because a one-sided gamma shock is
        a useful live signal.  It only requires a minimum total sample, a
        valid gamma price, and non-zero gamma strength.
        """
        icp = self.env["ir.config_parameter"].sudo()
        asset = (data.get("asset") or "BTC").upper()
        gamma_band = float(data.get("gamma_band") or 0.0)
        if not gamma_band:
            return False, "missing-gamma-band"

        total_trades = int(data.get("long_trade_count") or 0) + int(data.get("short_trade_count") or 0)
        min_total = int(icp.get_param(
            "dankbit.gamma_live_min_total_trades_btc" if asset.startswith("BTC") else "dankbit.gamma_live_min_total_trades_eth",
            default=40 if asset.startswith("BTC") else 20,
        ))
        if total_trades < min_total:
            return False, "insufficient-gamma-sample"

        gamma_abs = sum(abs(float(data.get(f) or 0.0)) for f in ("bcg_abs", "bpg_abs", "scg_abs", "spg_abs"))
        min_gamma_abs = float(icp.get_param(
            "dankbit.gamma_live_min_abs_btc" if asset.startswith("BTC") else "dankbit.gamma_live_min_abs_eth",
            default=0.0001,
        ))
        if gamma_abs < min_gamma_abs:
            return False, "no-live-gamma-strength"

        return True, "live-gamma-confirmed"

    def _persist_live_gamma(self, data):
        """Update only the live middle Gamma Average and the per-leg gamma
        values on an existing dankbit.bands row, without touching the
        confirmed structural Green/Red Bands.

        The row is not created here.  If an instrument has never passed the
        session-confirmed Band gate, creating a row from a low-volume hourly
        gamma-only update would still leak unconfirmed high/low values into
        /api/bands.  In that case we skip until the first confirmed Band
        snapshot exists.
        """
        self = self.sudo()
        record = self.search([("instrument", "=", data["instrument"])], limit=1)
        if not record:
            _logger.info(
                "dankbit.bands: skipped live gamma update for %s because no confirmed band row exists yet",
                data.get("instrument"),
            )
            return False

        vals = {
            "computed_at": data["computed_at"],
            "index_price": data["index_price"],
            "gamma_band": data["gamma_band"],
            "bcg_price": data["bcg_price"],
            "bcg_abs": data["bcg_abs"],
            "bpg_price": data["bpg_price"],
            "bpg_abs": data["bpg_abs"],
            "scg_price": data["scg_price"],
            "scg_abs": data["scg_abs"],
            "spg_price": data["spg_price"],
            "spg_abs": data["spg_abs"],
        }
        record.write(vals)
        return True

    def get_box_n(self, asset, expiry_index, persist=True):
        """Bands computation for `asset`'s `expiry_index`-th soonest
        active expiry, computed fresh on every call and persisted via
        _persist_extrema. The *only* caller, for every expiry_index
        (including 0), is compute_snapshot()'s hourly cron — there is no
        HTTP route or browser-triggered path that reaches this method at
        all, by design (see TRACKED_EXPIRY_COUNT). /api/zones-box/<asset>
        (the one that actually renders the yellow box on the chart) goes
        through get_box(), which calls _compute_asset() directly instead of
        this method, precisely so that live page views never persist
        anything.  This defensive legacy entry point now applies the same
        session and quality gates as compute_snapshot() before persisting."""
        data = self._compute_asset(asset, expiry_index=expiry_index)
        if data and persist:
            session_name = self._band_confirmation_session(datetime.now(timezone.utc))
            band_ok, _ = self._band_quality_gate(data)
            zone_ok, _ = self._zone_quality_gate(data)
            if session_name and band_ok and zone_ok:
                self._persist_extrema(data, zones_confirmation_reason=f"session:{session_name}")
        return data

    def get_box(self, asset, hours=None):
        """Display boundaries for the nearest expiry's zone boxes.

        The standard (since-00:00 UTC) chart path returns the last
        session-confirmed/emergency-confirmed High/Low/Middle zones from the
        persisted row.  A fresh computation is still made to resolve the
        current nearest expiry and expiration, but its noisy hourly zone
        values never replace the displayed fields here.  Forecast snapshots
        continue to call _compute_asset() directly and therefore retain the
        raw hourly zones for internal calculations.

        An explicit trailing `hours` request remains an intentional live
        analytical preview of that alternate trade window; it is never
        persisted and never changes the official confirmed zones.
        """
        data = self._compute_asset(asset, expiry_index=0, hours=hours)
        if not data or hours is not None:
            if data:
                data["zone_confirmation_mode"] = "live-trailing-window"
            return data

        record = self.sudo().search([("instrument", "=", data["instrument"])], limit=1)
        if not record or not (record.zones_confirmed_at or (record.high_zone_min and record.high_zone_max and record.low_zone_min and record.low_zone_max)):
            # Never fabricate a confirmed box before the first valid session.
            return None

        data.update({
            "computed_at": record.zones_confirmed_at or record.computed_at,
            "short_zero_above_price": record.high_zone_min,
            "long_zero_above_price": record.high_zone_max,
            "short_zero_below_price": record.low_zone_min,
            "long_zero_below_price": record.low_zone_max,
            "high_zone_min": record.high_zone_min,
            "high_zone_max": record.high_zone_max,
            "low_zone_min": record.low_zone_min,
            "low_zone_max": record.low_zone_max,
            "middle_zone_min": record.middle_zone_min,
            "middle_zone_max": record.middle_zone_max,
            "seller_max_profit": record.seller_max_profit,
            "buyer_max_loss": record.buyer_max_loss,
            "zone_confirmation_mode": record.zones_confirmation_reason or "legacy-confirmed",
        })
        return data

    def gamma_band_term_structure(self, asset):
        """The forward points of the chart's own violet dashed Gamma Band
        line (this model's own gamma_band field, /api/bands/<asset>) for
        forecast_lib.gamma_band_term_slope() — up to 2 dicts, soonest-
        expiring tracked instrument first, each {"gamma_band",
        "expiration_epoch"}. Same persisted dankbit_bands rows +
        dankbit_trade expiration lookup /api/bands/<asset> serves, but
        restricted to `t.expiration > NOW()` (so an already-expired,
        frozen-in-place row can't be mistaken for a forward point) and to
        just the 2 tracked instruments soonest to expire — exactly the two
        points forming that line's forward-most segment on the chart.
        Fewer than 2 rows (e.g. this model hasn't tracked a 2nd expiry yet)
        means gamma_band_term_slope() treats the bias as inert, same
        na-safe convention that engine's other history-dependent signals
        use. Lives here (rather than on the controller, where it used to
        be) since it's a plain query against this model's own persisted
        rows — moving it here lets the forecast-log cron reuse it without
        needing an HTTP request context."""
        cr = self.env.cr
        cr.execute("""
            SELECT b.gamma_band, t.expiration
            FROM dankbit_bands b
            JOIN (
                SELECT SUBSTRING(name FROM '^[^-]+-[^-]+') AS instrument, MIN(expiration) AS expiration
                FROM dankbit_trade
                WHERE name ILIKE %s
                GROUP BY instrument
            ) t ON t.instrument = b.instrument
            WHERE b.asset = %s AND t.expiration > NOW()
            ORDER BY t.expiration ASC
            LIMIT 2
        """, (f"{asset}-%", asset))
        term_structure = []
        for gamma_band, expiration in cr.fetchall():
            exp_ts = expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)
            term_structure.append({"gamma_band": float(gamma_band or 0.0), "expiration_epoch": exp_ts.timestamp()})
        return term_structure

    def compute_snapshot(self):
        """Cron entry point for confirmed structural Bands/Zones plus live Gamma.

        Raw zones are calculated hourly.  Official displayed High/Low/Middle
        zones update with the Green/Red Bands only inside a mature session
        after both quality gates pass.  Outside those sessions, a material
        structural change needs two consistent hourly candidates before it
        can replace the official snapshot.  The middle Gamma Average remains
        independently live behind its lighter quality gate.
        """
        now_utc = datetime.now(timezone.utc)
        session_name = self._band_confirmation_session(now_utc)

        if not session_name:
            _logger.info(
                "dankbit.bands: outside mature band session (%s UTC); confirmed bands stay unchanged, live gamma may update",
                now_utc.strftime("%H:%M"),
            )

        for asset in ("BTC", "ETH"):
            for expiry_index in range(self.TRACKED_EXPIRY_COUNT):
                data = self._compute_asset(asset, expiry_index=expiry_index)
                if not data:
                    continue

                structural_confirmed = False
                if session_name:
                    band_ok, band_reason = self._band_quality_gate(data)
                    zone_ok, zone_reason = self._zone_quality_gate(data)
                    if band_ok and zone_ok:
                        self._persist_extrema(data, zones_confirmation_reason=f"session:{session_name}")
                        structural_confirmed = True
                        _logger.info(
                            "dankbit.bands: confirmed Bands/Zones for %s expiry_index=%s in %s "
                            "(high=%s low=%s high_zone=%s..%s low_zone=%s..%s middle=%s..%s gamma=%s)",
                            asset, expiry_index, session_name,
                            data.get("high_resistance"), data.get("low_support"),
                            data.get("high_zone_min"), data.get("high_zone_max"),
                            data.get("low_zone_min"), data.get("low_zone_max"),
                            data.get("middle_zone_min"), data.get("middle_zone_max"),
                            data.get("gamma_band"),
                        )
                    else:
                        _logger.warning(
                            "dankbit.bands: raw %s expiry_index=%s not confirmed in %s: band=%s zone=%s "
                            "(high=%s low=%s longs=%s shorts=%s)",
                            asset, expiry_index, session_name, band_reason, zone_reason,
                            data.get("high_resistance"), data.get("low_support"),
                            data.get("long_trade_count"), data.get("short_trade_count"),
                        )
                else:
                    zone_ok, zone_reason = self._zone_quality_gate(data)
                    record = self.sudo().search([("instrument", "=", data["instrument"])], limit=1)
                    if zone_ok and record:
                        structural_change, structural_reason = self._zone_structural_change_gate(record, data)
                        hits = self._stage_zone_candidate(record, data)
                        if structural_change and hits >= 2:
                            reason = f"emergency:{structural_reason}:two-hour-confirmed"
                            self._persist_extrema(data, zones_confirmation_reason=reason)
                            structural_confirmed = True
                            _logger.warning(
                                "dankbit.bands: emergency Bands/Zones replacement for %s expiry_index=%s "
                                "after %s consistent hourly candidates (%s)",
                                asset, expiry_index, hits, structural_reason,
                            )
                        elif structural_change:
                            _logger.info(
                                "dankbit.bands: staged structural zone candidate for %s expiry_index=%s "
                                "(%s, hits=%s/2)",
                                asset, expiry_index, structural_reason, hits,
                            )
                    elif not zone_ok:
                        _logger.info(
                            "dankbit.bands: hourly zone candidate rejected for %s expiry_index=%s: %s",
                            asset, expiry_index, zone_reason,
                        )

                if structural_confirmed:
                    continue

                gamma_ok, gamma_reason = self._gamma_quality_gate(data)
                if not gamma_ok:
                    _logger.info(
                        "dankbit.bands: skipped live gamma for %s expiry_index=%s: %s",
                        asset, expiry_index, gamma_reason,
                    )
                    continue
                if self._persist_live_gamma(data):
                    _logger.info(
                        "dankbit.bands: live gamma updated for %s expiry_index=%s (gamma=%s)",
                        asset, expiry_index, data.get("gamma_band"),
                    )
