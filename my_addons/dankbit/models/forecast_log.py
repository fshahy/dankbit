# -*- coding: utf-8 -*-

import logging
from datetime import timedelta

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ForecastLog(models.Model):
    _name = "dankbit.forecast.log"
    _order = "generated_at desc, hours_ahead"

    # One row per forecasted candle per cron tick (not one row per tick) —
    # 18 rows per (asset, generated_at), one per forecast.simulate_forecast()
    # step — so accuracy stats can be grouped/aggregated directly in Odoo's
    # own list/pivot views (e.g. "average error by hours_ahead" or by
    # `mode`) without unpacking a JSON blob first. Exists purely for
    # testing/improving the Thales Forecast engine (see
    # controllers/forecast.py); nothing else in this addon reads this
    # model. `index_price`/`sigma_annual` are the anchor values
    # dankbit.forecast.snapshot.get_forecast_points() used to generate the
    # whole run, denormalized onto every one of that run's rows for
    # convenience (same value repeated per run, not a relational lookup).
    #
    # There is deliberately no separate "actual observed price" table or
    # column: this model doubles as its own ground-truth price history,
    # since every cron tick's own index_price is a real observed price at
    # that tick's own generated_at. To check a candle's accuracy once its
    # target_time has passed, look up whichever later row's own
    # generated_at sits closest to that target_time and compare its
    # index_price against this row's close — a self-join on this same
    # table, e.g.:
    #   SELECT a.asset, a.hours_ahead, a.close AS predicted,
    #          b.index_price AS actual, a.close - b.index_price AS error
    #   FROM dankbit_forecast_log a
    #   JOIN LATERAL (
    #       SELECT index_price FROM dankbit_forecast_log b
    #       WHERE b.asset = a.asset AND b.generated_at >= a.target_time
    #       ORDER BY b.generated_at ASC LIMIT 1
    #   ) b ON true
    #   WHERE a.target_time <= NOW();
    #
    # Beyond the OHLC prediction itself, every row also carries the
    # per-engine impulse breakdown and regime/context flags active for
    # that candle (see the field groups below and
    # forecast.simulate_forecast's own point-dict docstring) — added so a
    # candle's accuracy can be attributed to a specific engine or regime
    # after the fact (e.g. "is Greek Flow's own impulse actually
    # correlated with lower error, now that it's the primary driver?")
    # instead of relying on `mode`'s free-text label alone.
    asset = fields.Char(required=True, index=True)
    generated_at = fields.Datetime(required=True, index=True)
    index_price = fields.Float(digits=(16, 4))
    sigma_annual = fields.Float(digits=(16, 6))
    hours_ahead = fields.Integer(required=True)
    target_time = fields.Datetime(required=True, index=True)
    open = fields.Float(digits=(16, 4))
    high = fields.Float(digits=(16, 4))
    low = fields.Float(digits=(16, 4))
    close = fields.Float(digits=(16, 4))
    mode = fields.Char()

    # The dankbit.forecast.snapshot bucket this run's Greeks/bands came
    # from — lets a row be traced back to the full raw levels (top/low/
    # bml/smp, all 32 per-leg gamma/delta/theta/vega fields) that produced
    # it, instead of guessing the nearest bucket by timestamp. None on old
    # rows logged before this field existed.
    snapshot_id = fields.Many2one("dankbit.forecast.snapshot", ondelete="set null", index=True)

    # Final net impulse (post every clamp — Gamma-Band Trend Lock,
    # weekend caps, the ±0.45/±0.85 shock-vs-normal limit) that actually
    # produced this candle's step_move, plus its per-engine breakdown —
    # see forecast.simulate_forecast's own `forecast_impulse` sum. Exists
    # so a bad candle's cause can be attributed to one engine's own
    # calibration instead of guessed at from `mode`'s free-text label.
    forecast_impulse = fields.Float(digits=(16, 6))
    impulse_base_pull = fields.Float(digits=(16, 6))
    impulse_slope = fields.Float(digits=(16, 6))
    impulse_current_body = fields.Float(digits=(16, 6))
    impulse_curve_extreme = fields.Float(digits=(16, 6))
    impulse_gamma_band = fields.Float(digits=(16, 6))
    impulse_gamma_band_reclaim = fields.Float(digits=(16, 6))
    impulse_vega = fields.Float(digits=(16, 6))
    impulse_delta_shock = fields.Float(digits=(16, 6))
    impulse_gamma_shock = fields.Float(digits=(16, 6))
    impulse_mm_contest = fields.Float(digits=(16, 6))
    impulse_liquidity = fields.Float(digits=(16, 6))
    impulse_greek_flow = fields.Float(digits=(16, 6))
    impulse_term_slope = fields.Float(digits=(16, 6))
    impulse_fomo_carry = fields.Float(digits=(16, 6))

    # Regime/context flags active when this candle was generated — the
    # same signals `mode` summarizes as free text, broken out here so they
    # can be filtered/grouped on directly in list/pivot views instead of
    # string-parsed.
    is_weekend = fields.Boolean()
    session_name = fields.Char()
    activity_regime = fields.Char()
    gb_consensus_direction = fields.Integer()
    gb_consensus_strength = fields.Float(digits=(16, 4))
    gb_all_aligned = fields.Boolean()
    gb_trend_locked = fields.Boolean()
    any_shock_active = fields.Boolean()
    fakeout_risk = fields.Boolean()
    gamma_neutral_score = fields.Float(digits=(16, 4))
    absorption_mode = fields.Char()
    effective_atr = fields.Float(digits=(16, 4))
    fomo_active = fields.Boolean(index=True)
    fomo_direction = fields.Integer()
    fomo_score = fields.Float(digits=(16, 4))
    fomo_raw_score = fields.Float(digits=(16, 4))
    fomo_move_atr = fields.Float(digits=(16, 4))
    fomo_exhaustion = fields.Float(digits=(16, 4))
    fomo_option_alignment = fields.Float(digits=(16, 4))
    fomo_option_quality = fields.Float(digits=(16, 4))
    fomo_strength = fields.Float(digits=(16, 4))

    # Backfilled by check_accuracy() once target_time has passed — see
    # that method's own docstring for how actual_price is sourced.
    actual_price = fields.Float(digits=(16, 4))
    error = fields.Float(digits=(16, 4))
    error_pct = fields.Float(digits=(16, 4))
    checked_at = fields.Datetime()

    def log_forecast(self):
        """Cron entry point (hourly — see data/ir_cron.xml; tightened from an
        initial 4 hours, which matched forecast.simulate_forecast's own
        step_hours default so each tick's candles landed close to a prior
        tick's own target times, per Thales dev request for more frequent
        accuracy sampling — ticks now overlap rather than lining up 1:1 with
        the candle spacing, which check_accuracy() below doesn't care about
        but does mean ~4x the row volume for the same real-world span). For
        each of
        BTC/ETH, calls dankbit.forecast.snapshot.get_forecast_points() (the
        exact same computation /api/forecast/<asset> serves) and persists
        one row per returned candle. A run with nothing computable yet
        (see get_forecast_points) writes nothing for that asset, rather
        than a row of zeroes."""
        Snapshot = self.env["dankbit.forecast.snapshot"]
        for asset in ("BTC", "ETH"):
            result = Snapshot.get_forecast_points(asset)
            if not result["points"]:
                _logger.info("forecast.log: nothing computable yet for %s, skipping", asset)
                continue
            generated_at = result["generated_at"].replace(tzinfo=None)
            vals_list = [{
                "asset": asset,
                "generated_at": generated_at,
                "index_price": result["index_price"],
                "sigma_annual": result["sigma_annual"],
                "snapshot_id": result.get("snapshot_id"),
                "hours_ahead": p["hours"],
                "target_time": generated_at + timedelta(hours=p["hours"]),
                "open": p["open"], "high": p["high"], "low": p["low"], "close": p["close"],
                "mode": p["mode"],
                "forecast_impulse": p["forecast_impulse"],
                "impulse_base_pull": p["impulse_base_pull"],
                "impulse_slope": p["impulse_slope"],
                "impulse_current_body": p["impulse_current_body"],
                "impulse_curve_extreme": p["impulse_curve_extreme"],
                "impulse_gamma_band": p["impulse_gamma_band"],
                "impulse_gamma_band_reclaim": p["impulse_gamma_band_reclaim"],
                "impulse_vega": p["impulse_vega"],
                "impulse_delta_shock": p["impulse_delta_shock"],
                "impulse_gamma_shock": p["impulse_gamma_shock"],
                "impulse_mm_contest": p["impulse_mm_contest"],
                "impulse_liquidity": p["impulse_liquidity"],
                "impulse_greek_flow": p["impulse_greek_flow"],
                "impulse_term_slope": p["impulse_term_slope"],
                "impulse_fomo_carry": p.get("impulse_fomo_carry", 0.0),
                "is_weekend": p["is_weekend"],
                "session_name": p["session_name"],
                "activity_regime": p["activity_regime"],
                "gb_consensus_direction": p["gb_consensus_direction"],
                "gb_consensus_strength": p["gb_consensus_strength"],
                "gb_all_aligned": p["gb_all_aligned"],
                "gb_trend_locked": p["gb_trend_locked"],
                "any_shock_active": p["any_shock_active"],
                "fakeout_risk": p["fakeout_risk"],
                "gamma_neutral_score": p["gamma_neutral_score"],
                "absorption_mode": p["absorption_mode"],
                "effective_atr": p["effective_atr"],
                "fomo_active": p.get("fomo_active", False),
                "fomo_direction": p.get("fomo_direction", 0),
                "fomo_score": p.get("fomo_score", 0.0),
                "fomo_raw_score": p.get("fomo_raw_score", 0.0),
                "fomo_move_atr": p.get("fomo_move_atr", 0.0),
                "fomo_exhaustion": p.get("fomo_exhaustion", 0.0),
                "fomo_option_alignment": p.get("fomo_option_alignment", 0.0),
                "fomo_option_quality": p.get("fomo_option_quality", 0.0),
                "fomo_strength": p.get("fomo_strength", 0.0),
            } for p in result["points"]]
            self.sudo().create(vals_list)

    def check_accuracy(self):
        """Cron entry point (hourly — dankbit_check_forecast_accuracy_cron).
        Backfills actual_price/error/error_pct/checked_at on every row whose
        target_time has passed and hasn't been checked yet, via the self-join
        documented above: for each such row, finds the same-asset row with
        the earliest generated_at at/after that target_time (DISTINCT ON,
        ordered by that later row's generated_at) and takes its index_price
        as the actual observed price. Raw SQL (bypasses the ORM, like every
        other bulk/aggregate lookup in this addon) since this is a
        set-based backfill across potentially many rows, not a per-record
        write. Rows with no later observation yet (recent history) are
        simply left unchecked and retried on the next tick, same
        nothing-computable-yet-skip convention every other cron here
        follows rather than writing a placeholder."""
        self.env.cr.execute("""
            UPDATE dankbit_forecast_log a
            SET actual_price = m.actual_price,
                error = a.close - m.actual_price,
                error_pct = CASE WHEN m.actual_price != 0
                            THEN (a.close - m.actual_price) / m.actual_price * 100
                            ELSE NULL END,
                checked_at = NOW()
            FROM (
                SELECT DISTINCT ON (a2.id) a2.id AS log_id, b2.index_price AS actual_price
                FROM dankbit_forecast_log a2
                JOIN dankbit_forecast_log b2
                  ON b2.asset = a2.asset AND b2.generated_at >= a2.target_time
                WHERE a2.target_time <= NOW() AND a2.checked_at IS NULL
                ORDER BY a2.id, b2.generated_at ASC
            ) m
            WHERE a.id = m.log_id
        """)
        _logger.info("forecast.log: checked accuracy for %s rows", self.env.cr.rowcount)
