# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta, timezone

from odoo import fields, models

from ..controllers import forecast as forecast_lib
from ..controllers import next_candle_forecast

_logger = logging.getLogger(__name__)


def _epoch(dt):
    """Odoo stores naive UTC datetimes — this makes that explicit before
    converting to epoch seconds for hourly_flow_signal()'s own hours-ago
    normalization."""
    return dt.replace(tzinfo=timezone.utc).timestamp()


class ForecastNextCandle(models.Model):
    _name = "dankbit.forecast.next_candle"
    _order = "generated_at desc"

    # Append-only log — one NEW row per revision per target candle (never
    # upserted in place, unlike dankbit.forecast.snapshot's own per-bucket
    # upsert), since the whole point is to keep every revision's own
    # forecast around to measure whether accuracy improves 1->2->3->final
    # (see check_accuracy). Distinct from dankbit.forecast.log, which logs
    # a fresh full 18-candle simulate_forecast() path per cron tick — this
    # model instead tracks exactly ONE target candle at a time per
    # timeframe (1H/4H/1D — see controllers/next_candle_forecast.py's
    # TIMEFRAME_CONFIG), revised on a fixed cadence while its current
    # candle is still forming, then frozen.
    #
    # Self-referencing: bcd_abs/bpd_abs/scd_abs/spd_abs (below) are stored
    # on every row so the NEXT revision (or the next cycle's revision 1)
    # can diff against them via next_candle_forecast.hourly_flow_signal()
    # to compute its own Greek-flow reading, without needing a separate
    # snapshot table — this is what lets 1H work at all (its 15-minute
    # revisions are far finer-grained than dankbit.forecast.snapshot's own
    # BUCKET_HOURS=1, which that unrelated model keeps unchanged for the
    # existing 18-candle engine).
    asset = fields.Char(required=True, index=True)
    timeframe = fields.Char(required=True, default="4h", index=True)
    expiry_index = fields.Integer(required=True, default=0, index=True)
    expiry_instrument = fields.Char(index=True)

    target_time = fields.Datetime(required=True, index=True)
    current_candle_start = fields.Datetime(required=True)
    revision = fields.Integer(required=True)
    max_revisions = fields.Integer()
    is_final = fields.Boolean(index=True)
    generated_at = fields.Datetime(required=True, index=True)

    forecast_open = fields.Float(digits=(16, 4))
    forecast_close = fields.Float(digits=(16, 4))
    forecast_high = fields.Float(digits=(16, 4))
    forecast_low = fields.Float(digits=(16, 4))
    confidence = fields.Float(digits=(16, 2))

    greek_flow_score = fields.Float(digits=(16, 6))
    structural_adjustment = fields.Float(digits=(16, 4))
    smart_liquidity_adjustment = fields.Float(digits=(16, 4))
    # Combined Activity Regime / weekend / FlowImbalance / Zone Brake
    # multiplier actually applied to ForecastMove this revision (see
    # controllers/next_candle_forecast.py's compute_revision) — 1.0 means
    # none of the 4 dampers fired that revision. Added per the same Thales
    # dev PDF review (2026-08-03) that motivated forecast.py's own
    # flow_imbalance()/_zone_brake_mult(), applied here independently
    # since this engine doesn't call simulate_forecast()'s per-step loop.
    flow_move_damping_mult = fields.Float(digits=(16, 4))
    activity_regime = fields.Char()
    is_weekend = fields.Boolean()

    # Raw per-leg delta-Abs values this revision was computed from — see
    # the self-referencing-chain comment above.
    bcd_abs = fields.Float(digits=(16, 4))
    bpd_abs = fields.Float(digits=(16, 4))
    scd_abs = fields.Float(digits=(16, 4))
    spd_abs = fields.Float(digits=(16, 4))

    snapshot_id = fields.Many2one("dankbit.forecast.snapshot", ondelete="set null", index=True)

    # Backfilled by check_accuracy() once target_time's candle has actually
    # closed. Unlike dankbit.forecast.log (whose ground truth is a LATER
    # ROW of the same table), the ground truth here is a REAL market
    # candle (Binance, via dankbit.trade.get_candles) — every revision row
    # for the same target_time gets backfilled against that same actual
    # outcome, which is what lets a pivot view show whether close_error
    # shrinks across revision 1->2->3->final ("Revision accuracy"), while
    # filtering is_final=True gives "Final accuracy" (Close/High/Low
    # Error, Direction Accuracy, Range Coverage).
    actual_open = fields.Float(digits=(16, 4))
    actual_close = fields.Float(digits=(16, 4))
    actual_high = fields.Float(digits=(16, 4))
    actual_low = fields.Float(digits=(16, 4))
    close_error = fields.Float(digits=(16, 4))
    close_error_pct = fields.Float(digits=(16, 4))
    direction_correct = fields.Boolean()
    high_error = fields.Float(digits=(16, 4))
    low_error = fields.Float(digits=(16, 4))
    # Fraction of the actual candle's own [low, high] range covered by the
    # forecast's own [low, high] range (1.0 = fully contained).
    range_coverage = fields.Float(digits=(16, 4))
    checked_at = fields.Datetime()

    _sql_constraints = [
        ("instrument_timeframe_target_revision_uniq",
         "unique (asset, expiry_index, expiry_instrument, timeframe, target_time, revision)",
         "Only one row is kept per asset/expiry instrument/timeframe/target/revision."),
    ]

    def compute_and_log(self, timeframe="4h"):
        """Cron entry point — one cron per timeframe, each at that
        timeframe's own revision cadence (1H every 15min, 4H hourly, 1D
        every 4h — see data/ir_cron.xml). Loops BTC/ETH, mirroring
        dankbit.bands.compute_snapshot()'s own asset-loop pattern."""
        tracked = self.env["dankbit.bands"].TRACKED_EXPIRY_COUNT
        for asset in ("BTC", "ETH"):
            for expiry_index in range(tracked):
                try:
                    # One bad/thin expiry must not prevent the remaining daily
                    # anchors from being produced in the same cron cycle.
                    with self.env.cr.savepoint():
                        self._compute_and_log_asset(asset, timeframe, expiry_index=expiry_index)
                except Exception:
                    _logger.exception(
                        "forecast.next_candle: isolated failure for %s/%s E%s",
                        asset, timeframe, expiry_index + 1,
                    )

    def _compute_and_log_asset(self, asset, timeframe, expiry_index=0):
        cfg_tf = next_candle_forecast.TIMEFRAME_CONFIG[timeframe]
        Snapshot = self.env["dankbit.forecast.snapshot"]
        Trade = self.env["dankbit.trade"]

        # Still the cheapest way to get a fresh live per-leg Greek dict
        # (dankbit.bands._compute_asset() under the hood) regardless of
        # timeframe — also a harmless side-effect keep-alive for the
        # unrelated 18-candle engine's own hourly bucket table.
        current_record = Snapshot.compute_and_persist(
            asset, expiry_index=expiry_index, future_days_only=True,
        )
        if not current_record:
            _logger.info("forecast.next_candle: nothing computable yet for %s/%s, skipping", asset, timeframe)
            return

        index_price = Trade.get_index_price(asset)
        if not index_price:
            _logger.info("forecast.next_candle: no index price for %s/%s, skipping", asset, timeframe)
            return

        now_utc = datetime.now(timezone.utc)
        current_candle_start, target_time, _elapsed = next_candle_forecast.current_candle_bounds(
            now_utc, cfg_tf["candle_span_hours"],
        )
        # Each expiry owns one daily anchor in the three-day path.  Expiry 0
        # predicts the immediate next candle; expiry 1/2 predict the
        # corresponding first candle one/two days farther out while keeping
        # the same revision cadence during the current source candle.
        anchor_offset = timedelta(hours=24 * expiry_index)
        target_time += anchor_offset
        current_candle_start_naive = current_candle_start.replace(tzinfo=None)
        target_time_naive = target_time.replace(tzinfo=None)

        current_dict = current_record.to_dict()
        expiry_instrument = current_record.expiry_instrument

        # Self-referencing chain: baseline (this cycle's own "point 0",
        # i.e. the Greeks as of this candle's own open) is the PREVIOUS
        # cycle's final row — its own generated_at sits right at this
        # cycle's open, since one cycle's freeze moment IS the next
        # cycle's own start. Then every revision already logged so far
        # this cycle, then the current live reading as the newest point.
        prior_final_row = self.sudo().search([
            ("asset", "=", asset), ("timeframe", "=", timeframe),
            ("expiry_index", "=", expiry_index),
            ("expiry_instrument", "=", expiry_instrument),
            ("target_time", "=", (current_candle_start + anchor_offset).replace(tzinfo=None)),
            ("is_final", "=", True),
        ], limit=1)
        this_cycle_rows = self.sudo().search([
            ("asset", "=", asset), ("timeframe", "=", timeframe),
            ("expiry_index", "=", expiry_index),
            ("expiry_instrument", "=", expiry_instrument),
            ("target_time", "=", target_time_naive),
        ], order="revision asc")

        points = []
        if prior_final_row:
            points.append({
                "bcd_abs": prior_final_row.bcd_abs, "bpd_abs": prior_final_row.bpd_abs,
                "scd_abs": prior_final_row.scd_abs, "spd_abs": prior_final_row.spd_abs,
                "bucket_epoch": _epoch(prior_final_row.generated_at),
            })
        for row in this_cycle_rows:
            points.append({
                "bcd_abs": row.bcd_abs, "bpd_abs": row.bpd_abs,
                "scd_abs": row.scd_abs, "spd_abs": row.spd_abs,
                "bucket_epoch": _epoch(row.generated_at),
            })
        points.append({
            "bcd_abs": current_dict["bcd_abs"], "bpd_abs": current_dict["bpd_abs"],
            "scd_abs": current_dict["scd_abs"], "spd_abs": current_dict["spd_abs"],
            "bucket_epoch": now_utc.timestamp(),
        })

        real_candles = Trade.get_candles(asset, interval=cfg_tf["candle_interval"], limit=40)

        cfg, _horizon = Snapshot.get_forecast_cfg(asset)

        prior_flow = prior_final_row.greek_flow_score if prior_final_row else 0.0

        synthetic_liq = None
        if current_dict.get("top") and current_dict.get("low"):
            band_width = max(current_dict["top"] - current_dict["low"], 1e-9)
            synthetic_liq = forecast_lib.smart_synthetic_liquidity(
                current_dict, current_dict["top"], current_dict["low"], band_width, index_price, cfg=cfg,
            )
        score = forecast_lib.session_activity_score(current_dict, synthetic_liq, cfg=cfg)
        historical_scores = Snapshot.session_activity_history(
            asset, now_utc.hour, cfg=cfg, expiry_index=expiry_index,
        )
        activity = forecast_lib.session_activity_regime(score, historical_scores)

        is_weekend = current_candle_start.weekday() >= 5

        result = next_candle_forecast.compute_revision(
            asset, timeframe, now_utc, index_price, current_dict, points, real_candles,
            prior_flow, activity, is_weekend, cfg=cfg,
        )
        result["target_time"] = target_time_naive

        existing = self.sudo().search([
            ("asset", "=", asset), ("timeframe", "=", result["timeframe"]),
            ("expiry_index", "=", expiry_index),
            ("expiry_instrument", "=", expiry_instrument),
            ("target_time", "=", result["target_time"]), ("revision", "=", result["revision"]),
        ], limit=1)
        if existing:
            return

        self.sudo().create({
            "asset": asset,
            "expiry_index": expiry_index,
            "expiry_instrument": expiry_instrument,
            "timeframe": result["timeframe"],
            "target_time": result["target_time"],
            "current_candle_start": result["current_candle_start"],
            "revision": result["revision"],
            "max_revisions": result["max_revisions"],
            "is_final": result["is_final"],
            "generated_at": now_utc.replace(tzinfo=None),
            "forecast_open": result["forecast_open"],
            "forecast_close": result["forecast_close"],
            "forecast_high": result["forecast_high"],
            "forecast_low": result["forecast_low"],
            "confidence": result["confidence"],
            "greek_flow_score": result["greek_flow_score"],
            "structural_adjustment": result["structural_adjustment"],
            "smart_liquidity_adjustment": result["smart_liquidity_adjustment"],
            "flow_move_damping_mult": result["flow_move_damping_mult"],
            "activity_regime": result["activity_regime"],
            "is_weekend": result["is_weekend"],
            "bcd_abs": result["bcd_abs"],
            "bpd_abs": result["bpd_abs"],
            "scd_abs": result["scd_abs"],
            "spd_abs": result["spd_abs"],
            "snapshot_id": current_record.id,
        })

    def check_accuracy(self):
        """Cron entry point (hourly). Unlike dankbit.forecast.log's own
        pure-SQL self-join (which compares against a LATER ROW of the same
        table), the ground truth here is a REAL market candle, not another
        forecast row — so this is a Python loop: for every not-yet-checked
        row whose target candle has actually closed (per that row's OWN
        timeframe span), find that real candle (at that timeframe's own
        native interval) and backfill actual_open/close/high/low plus the
        derived error/direction/coverage fields. Backfills every revision
        row for a given target_time against the same actual outcome (not
        just is_final), so a pivot grouped by revision shows whether
        close_error shrinks across revisions."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = self.sudo().search([("checked_at", "=", False)])
        if not rows:
            return

        candles_cache = {}
        checked = 0
        for row in rows:
            cfg_tf = next_candle_forecast.TIMEFRAME_CONFIG.get(row.timeframe)
            if not cfg_tf:
                continue
            if row.target_time + timedelta(hours=cfg_tf["candle_span_hours"]) > now:
                continue

            cache_key = (row.asset, cfg_tf["candle_interval"])
            if cache_key not in candles_cache:
                candles_cache[cache_key] = self.env["dankbit.trade"].get_candles(
                    row.asset, interval=cfg_tf["candle_interval"], limit=60,
                )
            candle = next_candle_forecast.find_candle_by_open_time(
                candles_cache[cache_key], row.target_time.replace(tzinfo=timezone.utc),
            )
            if not candle:
                continue

            actual_open, actual_high, actual_low, actual_close = candle["o"], candle["h"], candle["l"], candle["c"]
            close_error = row.forecast_close - actual_close
            close_error_pct = (close_error / actual_close * 100.0) if actual_close else None
            direction_correct = (row.forecast_close - row.forecast_open) * (actual_close - actual_open) >= 0
            overlap = max(0.0, min(row.forecast_high, actual_high) - max(row.forecast_low, actual_low))
            actual_range = max(actual_high - actual_low, 1e-9)

            row.write({
                "actual_open": actual_open, "actual_close": actual_close,
                "actual_high": actual_high, "actual_low": actual_low,
                "close_error": close_error, "close_error_pct": close_error_pct,
                "direction_correct": direction_correct,
                "high_error": row.forecast_high - actual_high,
                "low_error": row.forecast_low - actual_low,
                "range_coverage": min(overlap / actual_range, 1.0),
                "checked_at": now,
            })
            checked += 1
        _logger.info("forecast.next_candle: checked accuracy for %s rows", checked)

    def latest_for_dashboard(self, asset, timeframe="4h", expiry_index=0):
        """The single latest (highest generated_at) row for `asset`'s
        currently-open target candle at `timeframe` — for the Delta
        Chart's small status pill (see /api/next-candle-forecast/<asset>)."""
        return self.sudo().search([
            ("asset", "=", asset), ("timeframe", "=", timeframe),
            ("expiry_index", "=", expiry_index),
        ], order="generated_at desc", limit=1)

    def preview_for_dashboard(self, asset, timeframe="4h", hours=2, expiry_index=0):
        """Compute one non-persisted Next Candle preview from only the
        selected trailing option-flow window.  Saved revision chains are not
        mixed into this preview and no accuracy/log table is written."""
        cfg_tf = next_candle_forecast.TIMEFRAME_CONFIG[timeframe]
        Snapshot = self.env["dankbit.forecast.snapshot"]
        Trade = self.env["dankbit.trade"]
        current_dict = Snapshot.live_snapshot_dict(
            asset, hours, expiry_index=expiry_index, future_days_only=True,
        )
        index_price = Trade.get_index_price(asset)
        if not current_dict or not index_price:
            return None

        now_utc = datetime.now(timezone.utc)
        current_candle_start, _target_time, _elapsed = next_candle_forecast.current_candle_bounds(
            now_utc, cfg_tf["candle_span_hours"],
        )
        real_candles = Trade.get_candles(asset, interval=cfg_tf["candle_interval"], limit=40)
        cfg, _horizon = Snapshot.get_forecast_cfg(asset)

        synthetic_liq = None
        if current_dict.get("top") and current_dict.get("low"):
            band_width = max(current_dict["top"] - current_dict["low"], 1e-9)
            synthetic_liq = forecast_lib.smart_synthetic_liquidity(
                current_dict, current_dict["top"], current_dict["low"], band_width, index_price, cfg=cfg,
            )
        score = forecast_lib.session_activity_score(current_dict, synthetic_liq, cfg=cfg)
        historical_scores = Snapshot.session_activity_history(
            asset, now_utc.hour, cfg=cfg, expiry_index=expiry_index,
        )
        activity = forecast_lib.session_activity_regime(score, historical_scores)

        # Greek Flow for a 2h/4h preview comes from the scan-to-scan changes
        # that actually occurred inside that selected window.  The structural
        # snapshot above remains strictly trailing-window based; only the four
        # Delta-Abs history fields are read from the hourly snapshot log.  This
        # avoids mixing all-day levels into the preview geometry while still
        # preserving the information the Greek-Flow engine is specifically
        # designed to measure: how incoming option flow changed each hour.
        since = (now_utc - timedelta(hours=hours)).replace(tzinfo=None)
        flow_rows = Snapshot.sudo().search([
            ("asset", "=", asset),
            ("expiry_index", "=", expiry_index),
            ("expiry_instrument", "=", current_dict.get("expiry_instrument")),
            ("bucket_start", ">=", since),
        ], order="bucket_start asc")
        points = [{
            "bcd_abs": row.bcd_abs, "bpd_abs": row.bpd_abs,
            "scd_abs": row.scd_abs, "spd_abs": row.spd_abs,
            "bucket_epoch": _epoch(row.bucket_start),
        } for row in flow_rows]

        # At least two scans are required for a rate of change.  A just-installed
        # system safely starts neutral until the second hourly snapshot arrives.
        if not points:
            points = [{
                "bcd_abs": current_dict["bcd_abs"], "bpd_abs": current_dict["bpd_abs"],
                "scd_abs": current_dict["scd_abs"], "spd_abs": current_dict["spd_abs"],
                "bucket_epoch": now_utc.timestamp(),
            }]
        result = next_candle_forecast.compute_revision(
            asset, timeframe, now_utc, index_price, current_dict, points, real_candles,
            0.0, activity, current_candle_start.weekday() >= 5, cfg=cfg,
        )
        result["window_hours"] = hours
        result["expiry_index"] = expiry_index
        result["expiry_instrument"] = current_dict.get("expiry_instrument")
        missing_legs = []
        for leg in ("bc", "bp", "sc", "sp"):
            supported = any(
                current_dict.get(leg + greek + "_price")
                and float(current_dict.get(leg + greek + "_price")) > 0
                and current_dict.get(leg + greek + "_abs")
                and float(current_dict.get(leg + greek + "_abs")) > 0
                for greek in ("g", "d", "t", "v")
            )
            if not supported:
                missing_legs.append(leg.upper())
        result["data_complete"] = not missing_legs
        result["missing_legs"] = missing_legs
        return result
