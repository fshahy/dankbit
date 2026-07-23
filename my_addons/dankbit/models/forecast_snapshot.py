# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta, timezone

from odoo import fields, models

from ..controllers import forecast as forecast_lib

_logger = logging.getLogger(__name__)

# Field names read straight off a dankbit.forecast.snapshot record into the
# plain dict forecast.py's engine expects (see per_leg_greeks/derive_levels)
# — shared by to_dict() below.
_FORECAST_SNAPSHOT_FIELDS = [
    "top", "low", "bml", "smp",
    "bcg_price", "bpg_price", "scg_price", "spg_price",
    "bcg_abs", "bpg_abs", "scg_abs", "spg_abs",
    "bcd_price", "bpd_price", "scd_price", "spd_price",
    "bcd_abs", "bpd_abs", "scd_abs", "spd_abs",
    "bct_price", "bpt_price", "sct_price", "spt_price",
    "bct_abs", "bpt_abs", "sct_abs", "spt_abs",
    "bcv_price", "bpv_price", "scv_price", "spv_price",
    "bcv_abs", "bpv_abs", "scv_abs", "spv_abs",
]

# Bucket size for the snapshot history (independent of the forecast's own
# horizon, see forecast.simulate_forecast's hours_ahead) — a snapshot is
# taken once per bucket of this size, giving the Gamma-Band Consensus
# engine and the Greek Flow Engine (see forecast.py) a real time-ordered
# history to compute slopes/flow from. 1h (not the 4h candle spacing the
# source Pine script and Dankbit's own forecast steps use) — the Greek
# Flow Engine's whole premise is detecting scan-to-scan Greek trend
# *within* the formation of one 4h forecast candle, which needs history
# spaced tighter than that candle's own step; both engines' slope/flow
# math already normalizes by real elapsed hours (gamma_band_consensus's
# "per 8 real hours" extrapolation, greek_flow's GREEK_FLOW_REF_HOURS),
# so tightening this only makes both more responsive, not incorrect.
# Populated by compute_snapshot()'s hourly cron below — before that cron
# existed, this model had no cron at all and relied solely on
# compute_and_persist() being called live from /api/forecast/<asset>
# (i.e. while the "Thales Forecast" checkbox was open somewhere); at 4h
# buckets a multi-hour viewing gap only widened the slope window, but at
# 1h buckets a reliable cadence matters more, hence the cron.
BUCKET_HOURS = 1


class ForecastSnapshot(models.Model):
    _name = "dankbit.forecast.snapshot"
    _order = "bucket_start"

    # One row per (asset, bucket_start) — continuously refined while its
    # bucket is "current", then frozen once time moves past it, exactly the
    # same "continuously-refined-then-frozen" pattern dankbit.bands
    # uses per-instrument (see _persist_extrema there), just keyed by a
    # rolling 4h time bucket instead of an expiry instrument. This is the
    # real historical time series Thales's manually-dated CSV rows provide
    # in the source Pine script — Dankbit has no human typing in daily
    # snapshots, so this model exists purely to give the Gamma-Band
    # Consensus engine (forecast.gamma_band_consensus) the 3 real
    # historical points it needs to compute a top/low/gamma slope over
    # actual elapsed time.
    asset = fields.Char(required=True, index=True)
    bucket_start = fields.Datetime(required=True, index=True)
    index_price = fields.Float(digits=(16, 4))

    # Band edges — same values as dankbit.bands's high_zone_max/
    # low_zone_min (each curve's own highest/lowest zero-crossing, same
    # skip-absent-sides rule options.zone_summary() uses), read straight
    # off that model's _compute_asset() rather than re-derived here,
    # reused as Thales's manually-drawn Resistance/Support reference band.
    top = fields.Float(digits=(16, 4))
    low = fields.Float(digits=(16, 4))

    # Per-leg gamma extrema (price where that leg's dollar-gamma curve
    # peaks/bottoms) plus the peak/bottom value itself, abs/1_000_000 (same
    # scale the /<instrument>/zones page's Gamma Peak Value lines use) —
    # Thales's BCG/BPG/SCG/SPG + BCGAbs/BPGAbs/SCGAbs/SPGAbs.
    bcg_price = fields.Float(digits=(16, 4))
    bpg_price = fields.Float(digits=(16, 4))
    scg_price = fields.Float(digits=(16, 4))
    spg_price = fields.Float(digits=(16, 4))
    bcg_abs = fields.Float(digits=(16, 4))
    bpg_abs = fields.Float(digits=(16, 4))
    scg_abs = fields.Float(digits=(16, 4))
    spg_abs = fields.Float(digits=(16, 4))

    # Per-leg delta-saturation prices/values, abs/10 — Thales's
    # BCD/BPD/SCD/SPD + BCDAbs/BPDAbs/SCDAbs/SPDAbs.
    bcd_price = fields.Float(digits=(16, 4))
    bpd_price = fields.Float(digits=(16, 4))
    scd_price = fields.Float(digits=(16, 4))
    spd_price = fields.Float(digits=(16, 4))
    bcd_abs = fields.Float(digits=(16, 4))
    bpd_abs = fields.Float(digits=(16, 4))
    scd_abs = fields.Float(digits=(16, 4))
    spd_abs = fields.Float(digits=(16, 4))

    # Per-leg theta extrema prices/values, abs/10_000 — Thales's
    # BCT/BPT/SCT/SPT + BCTAbs/BPTAbs/SCTAbs/SPTAbs.
    bct_price = fields.Float(digits=(16, 4))
    bpt_price = fields.Float(digits=(16, 4))
    sct_price = fields.Float(digits=(16, 4))
    spt_price = fields.Float(digits=(16, 4))
    bct_abs = fields.Float(digits=(16, 4))
    bpt_abs = fields.Float(digits=(16, 4))
    sct_abs = fields.Float(digits=(16, 4))
    spt_abs = fields.Float(digits=(16, 4))

    # Per-leg vega extrema prices/values, abs/100 — Thales's
    # BCV/BPV/SCV/SPV + BCVAbs/BPVAbs/SCVAbs/SPVAbs.
    bcv_price = fields.Float(digits=(16, 4))
    bpv_price = fields.Float(digits=(16, 4))
    scv_price = fields.Float(digits=(16, 4))
    spv_price = fields.Float(digits=(16, 4))
    bcv_abs = fields.Float(digits=(16, 4))
    bpv_abs = fields.Float(digits=(16, 4))
    scv_abs = fields.Float(digits=(16, 4))
    spv_abs = fields.Float(digits=(16, 4))

    # Curve extremes — Thales's BML (Buyer Max Loss = Longs curve bottom)
    # and SMP (Seller Max Profit = Shorts curve peak); same values
    # dankbit.bands's buyer_max_loss/seller_max_profit already
    # track, read from that model's _compute_asset() (see
    # compute_and_persist) but kept as this model's own columns rather
    # than a relational reference, since this snapshot's whole point is to
    # freeze one moment's values into its own row.
    bml = fields.Float(digits=(16, 4))
    smp = fields.Float(digits=(16, 4))

    _sql_constraints = [
        ("asset_bucket_uniq", "unique (asset, bucket_start)",
         "Only one Forecast snapshot is kept per asset per time bucket."),
    ]

    def _bucket_start_for(self, as_of):
        epoch_hours = int(as_of.timestamp() // 3600)
        bucket_epoch_hours = (epoch_hours // BUCKET_HOURS) * BUCKET_HOURS
        return datetime.fromtimestamp(bucket_epoch_hours * 3600, tz=timezone.utc).replace(tzinfo=None)

    def compute_and_persist(self, asset):
        """Compute this moment's per-leg Greeks/band data for `asset` (nearest
        active expiry, trades since 00:00 UTC) and upsert it into the current
        4h bucket's row, refining it in place until the bucket rolls over
        (see BUCKET_HOURS). Returns the upserted record, or None if there's
        nothing computable yet (no index price, no active expiry, or no
        trades in the window — a real gap, not written as a row of zeroes).

        Everything below is read straight off
        dankbit.bands._compute_asset(asset, expiry_index=0) — the
        exact same computation that feeds the TradingView chart's yellow
        zones box, and which already returns high_zone_max/low_zone_min
        (identical to this row's own top/low: each curve's own highest/
        lowest zero-crossing, same skip-absent-sides rule) and all 32
        per-leg gamma/delta/theta/vega price+Abs fields (via its own call
        into forecast_lib.per_leg_greeks()) since that model started
        persisting them too. This method used to re-fetch `asset`'s trades
        and call build_zone_curves()/per_leg_greeks() a second time just to
        get numbers _compute_asset() had already computed a moment
        earlier — removed as pure duplicate work now that both models
        agree on field names, so Thales Forecast's band and per-leg
        Greeks can never quietly drift from what's actually drawn
        elsewhere in the app, and never do the same query/curve-build
        twice for one asset."""
        bands_data = self.env["dankbit.bands"]._compute_asset(asset, expiry_index=0)
        if not bands_data:
            _logger.warning("forecast.compute_and_persist: dankbit.bands has nothing computable for %s, skipping", asset)
            return None

        top = bands_data["high_zone_max"]
        low = bands_data["low_zone_min"]
        if not top or not low:
            _logger.warning("forecast.compute_and_persist: no band for %s, skipping", asset)
            return None

        as_of = bands_data["computed_at"]

        vals = {
            "asset": asset,
            "bucket_start": self._bucket_start_for(as_of),
            "index_price": bands_data["index_price"],
            "top": top,
            "low": low,
            "bml": bands_data["buyer_max_loss"],
            "smp": bands_data["seller_max_profit"],
        }
        vals.update({f: bands_data[f] for f in self.env["dankbit.bands"]._PER_LEG_GREEK_FIELDS})

        record = self.sudo().search([
            ("asset", "=", asset),
            ("bucket_start", "=", vals["bucket_start"]),
        ], limit=1)
        if record:
            record.write(vals)
            return record
        return self.sudo().create(vals)

    def recent_history(self, asset, limit=3):
        """The `limit` most recent snapshot rows for `asset`, newest first —
        feeds forecast.gamma_band_consensus's 3-point slope calculation
        and forecast.greek_flow's Delta/Vega Flow + Smart Liquidity Drift
        calculation. History accumulates both from compute_snapshot()'s
        hourly cron (see below) and, in between cron ticks, as a side
        effect of compute_and_persist() being called live from
        /api/forecast/<asset> while the "Thales Forecast" checkbox is
        open somewhere — either path upserts the same current bucket, so
        they can never conflict, only refine the same row. A missed cron
        tick just widens the slope/flow window rather than breaking
        anything (see gamma_band_consensus()'s and greek_flow()'s
        real-elapsed-hours normalization)."""
        return self.sudo().search(
            [("asset", "=", asset)], order="bucket_start desc", limit=limit
        )

    def to_dict(self):
        """This record as a plain dict keyed exactly as forecast.py's engine
        expects (see per_leg_greeks/derive_levels), plus bucket_epoch (UTC
        epoch seconds) for the Gamma-Band Consensus/center-slope real-
        elapsed-time math."""
        self.ensure_one()
        data = {f: getattr(self, f) for f in _FORECAST_SNAPSHOT_FIELDS}
        data["bucket_epoch"] = self.bucket_start.replace(tzinfo=timezone.utc).timestamp()
        return data

    def get_forecast_cfg(self):
        """Reads res.config.settings' "Thales Forecast" section into a cfg
        dict + a dict of horizon kwargs, for forecast.simulate_forecast.
        Lives here (rather than on the controller, where it used to be) so
        get_forecast_points() below — and thus both /api/forecast/<asset>
        and dankbit.forecast.log's cron — read the exact same live-
        configured tunables, without needing an HTTP request context.
        Falls back to simulate_forecast's own hardcoded defaults (repeated
        here verbatim) for any setting left unset — same default-in-the-
        reading-code convention every other dankbit.* config_parameter
        uses in this addon (see res_config_settings.py)."""
        icp = self.env["ir.config_parameter"].sudo()

        def f(key, default):
            return float(icp.get_param(f"dankbit.{key}", default))

        cfg = {}
        cfg["GAMMA_CENTER_WEIGHT"] = f("forecast_gamma_center_weight", 0.70)
        cfg["CURVE_CENTER_WEIGHT"] = f("forecast_curve_center_weight", 0.20)
        cfg["THETA_CENTER_WEIGHT"] = f("forecast_theta_center_weight", 0.10)
        cfg["FORECAST_PULL_FACTOR"] = f("forecast_pull_factor", 0.55)
        cfg["FORECAST_SLOPE_FACTOR"] = f("forecast_slope_factor", 0.35)
        cfg["FORECAST_BODY_FACTOR"] = f("forecast_body_factor", 0.42)
        cfg["FORECAST_CURVE_EXTREME_BODY_WEIGHT"] = f("forecast_curve_extreme_body_weight", 0.26)
        cfg["FORECAST_WICK_FACTOR"] = f("forecast_wick_factor", 0.35)
        cfg["FORECAST_ATR_FACTOR"] = f("forecast_atr_factor", 0.3)
        cfg["FORECAST_CURVE_WICK_WEIGHT"] = f("forecast_curve_wick_weight", 0.42)
        cfg["GAMMA_BAND_OPPOSITE_WICK_COMPRESSION"] = f("forecast_gb_opposite_wick_compression", 0.18)
        cfg["GAMMA_BAND_CONFIRMED_TARGET_BOOST"] = f("forecast_gb_confirmed_target_boost", 0.55)
        cfg["GAMMA_BAND_CONFIDENCE_BOOST"] = f("forecast_gb_confidence_boost", 0.2)
        cfg["GAMMA_BAND_CONFLICT_BODY_DAMPING"] = f("forecast_gb_conflict_body_damping", 0.3)
        cfg["GAMMA_BAND_CONFLICT_WICK_EXPANSION"] = f("forecast_gb_conflict_wick_expansion", 0.25)
        cfg["GAMMA_BAND_OPPOSING_MAGNET_DAMPING"] = f("forecast_gb_opposing_magnet_damping", 0.6)
        cfg["GAMMA_BAND_TREND_LOCK_STRENGTH"] = f("forecast_gb_trend_lock_strength", 0.55)
        cfg["GAMMA_BAND_COUNTER_BODY_DAMPING"] = f("forecast_gb_counter_body_damping", 0.18)
        cfg["GAMMA_BAND_COUNTER_MAX_OPP_IMPULSE"] = f("forecast_gb_counter_max_opp_impulse", 0.03)
        cfg["GAMMA_BAND_COUNTER_ESCAPE_ATR"] = f("forecast_gb_counter_escape_atr", 0.95)
        cfg["GAMMA_BAND_TERM_SLOPE_IMPULSE_STRENGTH"] = f("forecast_gb_term_slope_impulse_strength", 0.16)
        cfg["GAMMA_BAND_TERM_SLOPE_MAX_IMPULSE"] = f("forecast_gb_term_slope_max_impulse", 0.2)
        cfg["GAMMA_CONFIRM_BUFFER_PCT"] = f("forecast_gamma_confirm_buffer_pct", 0.06)
        cfg["CLUSTER_ALIGNMENT_THRESHOLD"] = f("forecast_cluster_alignment_threshold", 0.6)
        cfg["CLUSTER_BODY_CONFIDENCE_FLOOR"] = f("forecast_cluster_body_confidence_floor", 0.58)
        cfg["CLUSTER_COMPRESSED_THRESHOLD"] = f("forecast_cluster_compressed_threshold", 0.18)
        cfg["CLUSTER_COMPRESSION_BODY_DAMPING"] = f("forecast_cluster_compression_body_damping", 0.15)
        cfg["CLUSTER_COMPRESSION_WICK_COMPRESSION"] = f("forecast_cluster_compression_wick_compression", 0.3)
        cfg["CLUSTER_EXPANSION_THRESHOLD"] = f("forecast_cluster_expansion_threshold", 0.025)
        cfg["LIQUIDITY_ALIGNED_WICK_COMPRESSION"] = f("forecast_liquidity_aligned_wick_compression", 0.25)
        cfg["LIQUIDITY_BODY_CONFIDENCE_FLOOR"] = f("forecast_liquidity_body_confidence_floor", 0.6)
        cfg["LIQUIDITY_OPPOSITE_WICK_COMPRESSION"] = f("forecast_liquidity_opposite_wick_compression", 0.35)
        cfg["LIQUIDITY_SWEEP_WICK_COMPRESSION"] = f("forecast_liquidity_sweep_wick_compression", 0.7)
        cfg["MOMENTUM_BODY_CONFIDENCE_FLOOR"] = f("forecast_momentum_body_confidence_floor", 0.62)
        cfg["MOMENTUM_WICK_COMPRESSION"] = f("forecast_momentum_wick_compression", 0.75)
        cfg["NEAR_GAMMA_BODY_DAMPING"] = f("forecast_near_gamma_body_damping", 0.4)
        cfg["NEAR_GAMMA_WICK_EXPANSION"] = f("forecast_near_gamma_wick_expansion", 0.25)
        cfg["HIGH_VOL_PULL_FACTOR"] = f("forecast_high_vol_pull_factor", 1.05)
        cfg["LOW_VOL_PULL_FACTOR"] = f("forecast_low_vol_pull_factor", 0.75)
        cfg["WEEKDAY_PULL_FACTOR"] = f("forecast_weekday_pull_factor", 1.0)
        cfg["HIGH_VOL_SHOCK_FACTOR"] = f("forecast_high_vol_shock_factor", 1.1)
        cfg["LOW_VOL_SHOCK_FACTOR"] = f("forecast_low_vol_shock_factor", 0.7)
        cfg["WEEKDAY_SHOCK_FACTOR"] = f("forecast_weekday_shock_factor", 1.0)
        cfg["WEEKEND_ATR_FACTOR"] = f("forecast_weekend_atr_factor", 0.75)
        cfg["WEEKEND_BODY_FACTOR"] = f("forecast_weekend_body_factor", 0.65)
        cfg["WEEKEND_SHOCK_FACTOR"] = f("forecast_weekend_shock_factor", 0.75)
        cfg["BUCKET_HOURS_FALLBACK"] = f("forecast_bucket_hours_fallback", 4.0)

        cfg["SESSION_BODY_FACTOR"] = {
            "Asia": f("session_body_asia", 0.7),
            "London": f("session_body_london", 0.9),
            "Overlap": f("session_body_overlap", 1.05),
            "NY": f("session_body_ny", 0.95),
            "PostNY": f("session_body_postny", 0.7),
        }
        cfg["SESSION_ATR_FACTOR"] = {
            "Asia": f("session_atr_asia", 0.75),
            "London": f("session_atr_london", 0.95),
            "Overlap": f("session_atr_overlap", 1.1),
            "NY": f("session_atr_ny", 1.0),
            "PostNY": f("session_atr_postny", 0.75),
        }
        cfg["SESSION_SHOCK_FACTOR"] = {
            "Asia": f("session_shock_asia", 0.65),
            "London": f("session_shock_london", 0.95),
            "Overlap": f("session_shock_overlap", 1.1),
            "NY": f("session_shock_ny", 1.0),
            "PostNY": f("session_shock_postny", 0.65),
        }
        cfg["SESSION_FIRST_MOVE_ATR"] = {
            "Asia": f("session_firstmove_asia", 0.35),
            "London": f("session_firstmove_london", 0.55),
            "Overlap": f("session_firstmove_overlap", 0.75),
            "NY": f("session_firstmove_ny", 0.6),
            "PostNY": f("session_firstmove_postny", 0.35),
        }

        horizon = {
            "hours_ahead": int(icp.get_param("dankbit.forecast_hours_ahead", 72)),
            "step_hours": int(icp.get_param("dankbit.forecast_step_hours", 4)),
            "start_offset_hours": int(icp.get_param("dankbit.forecast_start_offset_hours", 4)),
        }
        return cfg, horizon

    def get_forecast_points(self, asset):
        """Single source of truth for "compute the Thales Forecast candles
        for `asset` right now" — everything /api/forecast/<asset>
        (main.py's forecast_json) needs, factored out here (using
        self.env rather than the HTTP request global) so
        dankbit.forecast.log's cron (see that model) can run the exact
        same computation without an HTTP request context. Returns
        {"generated_at", "index_price", "sigma_annual", "points"} —
        `points` is forecast.simulate_forecast()'s own {hours, open, high,
        low, close, mode} dicts, `hours` left as an offset rather than
        converted to an absolute time, so each caller anchors it to
        whichever "now" it cares about (forecast_json uses its own
        request-time now_ms; dankbit.forecast.log uses this method's own
        `generated_at`, captured once up front so every point in one run
        shares the same anchor). Empty `points` (and possibly-None
        index_price/sigma_annual) means nothing was computable yet for
        this asset (no index price, no active expiry, or no trades in the
        current 00:00-UTC window — see compute_and_persist)."""
        generated_at = datetime.now(timezone.utc)
        index_price = self.env["dankbit.trade"].get_index_price(asset)

        cr = self.env.cr
        cr.execute("""
            SELECT SUM(iv * amount) / NULLIF(SUM(amount), 0)
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND deribit_ts >= NOW() - INTERVAL '24 hours'
        """, (f"{asset}-%",))
        avg_iv_row = cr.fetchone()
        sigma_annual = float(avg_iv_row[0]) / 100.0 if avg_iv_row and avg_iv_row[0] else None

        current_record = self.compute_and_persist(asset)

        points = []
        if index_price and sigma_annual and current_record:
            history_records = self.recent_history(asset, limit=4).filtered(
                lambda r: r.bucket_start != current_record.bucket_start
            )[:3]
            current = current_record.to_dict()
            history = [r.to_dict() for r in history_records]
            candles = self.env["dankbit.trade"].get_candles(asset, interval="4h", limit=40)
            gamma_band_term_structure = self.env["dankbit.bands"].gamma_band_term_structure(asset)
            cfg, horizon = self.get_forecast_cfg()
            points = forecast_lib.simulate_forecast(
                index_price, sigma_annual, current, history, candles, cfg=cfg,
                gamma_band_term_structure=gamma_band_term_structure, **horizon,
            )

        return {
            "generated_at": generated_at,
            "index_price": index_price,
            "sigma_annual": sigma_annual,
            "points": points,
        }

    def compute_snapshot(self):
        """Cron entry point (hourly — see data/ir_cron.xml), mirroring
        dankbit.bands.compute_snapshot()'s own asset-loop pattern. Unlike
        that model, this one already had a live-write path
        (compute_and_persist() called from /api/forecast/<asset> while
        the Thales Forecast checkbox is open) before this cron existed —
        this cron doesn't replace that path, it just guarantees the
        roughly-hourly cadence recent_history()'s consumers (Gamma-Band
        Consensus, Greek Flow) need even when nobody has the chart open."""
        for asset in ("BTC", "ETH"):
            self.compute_and_persist(asset)
