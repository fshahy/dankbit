# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from odoo import fields, models

from ..controllers import options as options_lib
from ..controllers import forecast as forecast_lib

_logger = logging.getLogger(__name__)


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

    @staticmethod
    def _format_instrument(asset, exp):
        """`asset` + a raw expiration datetime -> Deribit-style instrument
        string (e.g. 'BTC-9JUL26'), the convention every other expiry
        identifier in this addon uses — shared by nearest_expiry/next_expiry
        so the two can't drift on formatting."""
        return f"{asset}-{exp.day}{exp.strftime('%b').upper()}{exp.strftime('%y')}"

    def _nth_active_expiry(self, asset, n):
        """The (n+1)-th soonest active expiry for `asset` (n=0 nearest, n=1
        the one after that, etc. — same soonest-first ordering as
        _compute_asset's own `expiry_index`), as a full Deribit-style
        instrument string. Shared by nearest_expiry/next_expiry/
        nearest_expiry_plus_2/nearest_expiry_plus_3 so they can't drift on
        lookup or formatting. Returns None if there's no active expiry at
        that position."""
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        expirations = self._distinct_expirations(asset, as_of, n + 1)
        if len(expirations) <= n:
            return None
        return self._format_instrument(asset, expirations[n])

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
        return self._nth_active_expiry(asset, 0)

    def next_expiry(self, asset):
        """The active expiry immediately after the nearest one for `asset`
        (expiry_index=1 in _compute_asset's soonest-first ordering), as a
        full Deribit-style instrument string — same cheap standalone lookup
        as nearest_expiry, for the Gamma Chart's "Gamma Tops" checkbox's
        "Nearest + 1" scope. Returns None if there's no such expiry (e.g.
        only one active expiry left)."""
        return self._nth_active_expiry(asset, 1)

    def nearest_expiry_plus_2(self, asset):
        """Same as next_expiry, two expiries out (expiry_index=2) — feeds
        the Gamma Chart's "Gamma Tops" checkbox's "Nearest + 2" scope."""
        return self._nth_active_expiry(asset, 2)

    def nearest_expiry_plus_3(self, asset):
        """Same as next_expiry, three expiries out (expiry_index=3) — feeds
        the Gamma Chart's "Gamma Tops" checkbox's "Nearest + 3" scope."""
        return self._nth_active_expiry(asset, 3)

    def _compute_asset(self, asset, expiry_index=0, hours=None):
        """Compute index_price, the highest/lowest Longs-vs-Shorts curve
        intersection (high_resistance/low_support — not relative to
        index_price, see below), gamma_band (average of
        the 4 gamma extrema — see below), plus the 4 zero-crossing box
        boundaries for `asset` as of now, for one specific active expiry
        only — mirrors the /<instrument>/zones PNG route called with that
        specific instrument, aggregated per-asset. Trades are taken since
        today's UTC midnight by default; passing `hours` instead restricts
        to the trailing `hours` hours through now — used by /chart/<asset>'s
        00:00-UTC-vs-trailing-hours radio toggle (see get_box,
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

        window_start = (
            as_of - timedelta(hours=hours) if hours is not None
            else as_of.replace(hour=0, minute=0, second=0, microsecond=0)
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
        # accounted for by per_leg_greeks().
        gamma_band = (legs["bcg_price"] + legs["bpg_price"] + legs["scg_price"] + legs["spg_price"]) / 4.0

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
        delta_band = (legs["bcd_price"] + legs["bpd_price"] + legs["scd_price"] + legs["spd_price"]) / 4.0

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

    def _persist_extrema(self, data):
        """Upsert the one record for `data['instrument']` — only the
        historical-line fields (computed_at/index_price/high_resistance/
        low_support/gamma_band/delta_band/smart_liq_upper_price/
        smart_liq_lower_price/high_zone/low_zone/middle_zone/
        seller_max_profit/buyer_max_loss, plus the 32 per-leg gamma/delta/
        theta/vega price+Abs fields — see _PER_LEG_GREEK_FIELDS);
        computed_at is refreshed to `data['computed_at']` (the moment
        _compute_asset() ran) on every upsert, not just set once at
        creation. The 4 box-boundary fields in `data` are never persisted, only ever read
        live off the return value (see get_box), since nothing reads
        box-boundary history.

        Called only from compute_snapshot()'s 4-hourly cron (see
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
    # by the 4-hourly compute_snapshot() cron (see below) — there is no
    # browser-triggered live path for any of them; the "Zones" checkbox's
    # own live box rendering goes through get_box() -> _compute_asset()
    # directly and never persists.
    TRACKED_EXPIRY_COUNT = 3

    def get_box_n(self, asset, expiry_index):
        """Bands computation for `asset`'s `expiry_index`-th soonest
        active expiry, computed fresh on every call and persisted via
        _persist_extrema. The *only* caller, for every expiry_index
        (including 0), is compute_snapshot()'s 4-hourly cron — there is no
        HTTP route or browser-triggered path that reaches this method at
        all, by design (see TRACKED_EXPIRY_COUNT). /api/zones-box/<asset>
        (the one that actually renders the yellow box on the chart) goes
        through get_box(), which calls _compute_asset() directly instead of
        this method, precisely so that live page views never persist
        anything. The 4 box-boundary fields themselves are still never
        persisted, only the computed_at moment's index_price/
        high_resistance/low_support/gamma_band/delta_band (see
        _persist_extrema)."""
        data = self._compute_asset(asset, expiry_index=expiry_index)
        if data:
            self._persist_extrema(data)
        return data

    def get_box(self, asset, hours=None):
        """Live zones-box boundaries for `asset`'s nearest active expiry,
        for /api/zones-box/<asset> — the one that actually renders the
        yellow (and teal) box on the chart. Always computed via
        _compute_asset() directly, never via get_box_n(), so a live page
        view can never persist anything into dankbit.bands: the nearest
        expiry's row (expiry_index 0) is refreshed *only* by
        compute_snapshot()'s 4-hourly cron, exactly like expiry_index 1/2
        (see TRACKED_EXPIRY_COUNT) — no browser action, for any expiry_index,
        ever writes to this model. An explicit `hours` overrides the default
        since-00:00-UTC-through-now trade window with the trailing `hours`
        hours instead — driven by /chart/<asset>'s 00:00-UTC-vs-trailing-
        hours radio toggle (see dankbit_templates.xml); either way this is
        display-only and doesn't touch the shared history other viewers/
        expiries' term-structure lines depend on."""
        return self._compute_asset(asset, expiry_index=0, hours=hours)

    def compute_snapshot(self):
        """Cron entry point (every 4 hours — see data/ir_cron.xml) — the
        *sole* source of truth for every tracked expiry_index, including 0
        (the nearest expiry): no browser action ever computes or persists
        into dankbit.bands, for any expiry_index (see TRACKED_EXPIRY_COUNT/
        get_box_n/get_box). /api/zones-box/<asset>'s own live polling still
        computes the yellow box's boundaries fresh on every request for
        instant rendering, but via get_box() -> _compute_asset() directly,
        never through get_box_n(), so it never writes here — this cron is
        the only path that keeps dankbit.bands rows updating at all. Only
        touches dankbit.bands (via _persist_extrema) — the TradingView
        horizontal price lines (delta=0, gamma peak/bottom) are untouched by
        this or any cron."""
        for asset in ("BTC", "ETH"):
            for expiry_index in range(self.TRACKED_EXPIRY_COUNT):
                self.get_box_n(asset, expiry_index)
