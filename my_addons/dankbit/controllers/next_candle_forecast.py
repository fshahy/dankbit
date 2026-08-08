# -*- coding: utf-8 -*-
"""Next Candle Forecast — a "Next Candle Forecast" engine, distinct from
forecast.py's own 18-candle simulate_forecast(): instead of walking a fresh
18-step path forward from the live index price on every call, this tracks
exactly ONE target candle at a time, per timeframe (1H/4H/1D), revised on a
fixed cadence while the *current* candle of that timeframe is still forming
(1H -> every 15min x4, 4H -> every 1h x4, 1D -> every 4h x6), then freezes
once that candle actually closes.

Ported from a spec Thales's own dev worked out with another AI agent
(2026-08-01): Open tracks the live price until the current candle closes,
then locks to its real Close; Close is built from a Greek Flow reading
aggregated across however many of the current candle's own sub-intervals
have completed so far, blended against a Prior (last completed cycle's own
final flow reading) with revision-dependent weights (25/75 -> 50/50 ->
75/25 -> 100/0 at 4 revisions, generalized below) so an early, noisy single
sub-interval reading doesn't dominate; High/Low are built AFTER Close as
wicks, and Smart Liquidity can damp Close near a level price has been
rejected from before (pushing the undamped remainder into wick instead of
discarding it).

Pure functions only (no Odoo/ORM access), mirroring forecast.py's own
style. Reuses forecast.py's existing data-shape/building blocks
(derive_levels, smart_synthetic_liquidity, session_activity_regime,
_atr14, GREEK_FLOW_REF_HOURS/_cfg, flow_imbalance) rather than forecast.py's
own simulate_forecast()/greek_flow() logic, which stays untouched — the
FlowImbalance damping and Zone Brake ForecastMove receives (see their own
module-level comment below) are this module's own independent application
of the same Thales dev feedback that motivated simulate_forecast()'s
equivalents, not a port of that engine's code.

Originally 4H-only; generalized to all three timeframes via
TIMEFRAME_CONFIG below. The per-revision Greek-flow history this needs is
NOT sourced from dankbit.forecast.snapshot's shared hourly buckets (that
model's BUCKET_HOURS=1 is too coarse for 1H's own 15-minute revisions, and
is relied on as-is by the unrelated 18-candle engine, so it isn't
touched) — instead dankbit.forecast.next_candle is self-referencing: each
persisted revision row stores the 4 raw per-leg delta-Abs values it was
computed from, and the next revision (or the next cycle's revision 1)
diffs against the previous row's own stored values. See
models/forecast_next_candle.py for how that chain is assembled; this
module just consumes the resulting `points` list. Deliberately still
deferred: full StructuralAdjustment fidelity (a single pull-to-center term
stands in for the spec's separate GAVG/R-S/BML-SMP bullets), persisted
Smart-Liquidity rejection memory (re-derived fresh from real candle
history every call), and Settings-exposed tunables (fixed module
constants for now)."""

from datetime import datetime, timedelta, timezone
from math import sqrt

from . import forecast as forecast_lib

# Per-timeframe span/revision-count/real-candle-interval. `candle_interval`
# is passed straight to dankbit.trade.get_candles()/get_candles_deribit_perpetual
# (both return "1h"/"4h"/"1d" bars — get_candles_deribit_perpetual's "4h" is
# bucketed server-side from native 60-minute Deribit bars, transparent to
# this caller) so each timeframe's ATR/wick/rejection-history/freeze-Open
# all come from real candles of a matching width, rather than always 4h
# ones.
TIMEFRAME_CONFIG = {
    "1h": {"candle_span_hours": 1, "max_revisions": 4, "candle_interval": "1h"},
    "4h": {"candle_span_hours": 4, "max_revisions": 4, "candle_interval": "4h"},
    "1d": {"candle_span_hours": 24, "max_revisions": 6, "candle_interval": "1d"},
}

# ForecastMove = clamp(flow_score) * band_width * FLOW_SCALE_FACTOR * TIME_SCALE
# — FLOW_SCALE_FACTOR kept in the same order of magnitude as forecast.py's
# own GREEK_FLOW_DELTA_IMPULSE_STRENGTH (0.16, clamped to +-0.18) so this
# engine's 4H moves (TIME_SCALE=1.0, the originally-tuned baseline) are
# roughly comparable in scale to the existing engine's own, despite
# flow_score here being an unclamped plain average rather than a single
# already-clamped impulse term.
MAX_FLOW_SCORE = 1.0
FLOW_SCALE_FACTOR = 0.15

# Per-asset normalizer for hourly_flow_signal()'s own rate() — deliberately
# NOT forecast.py's DELTA_ABS_NORMALIZER (600 BTC / 2250 ETH via cfg),
# which calibrates an ABSOLUTE dollar-Greek activity scale for that
# module's 5 other engines, evaluated against history up to ~3 real hours
# back. This engine instead measures a RATE OF CHANGE between consecutive
# revisions that can be as little as 15 minutes apart (1H timeframe) —
# real bcd_abs/bpd_abs/scd_abs/spd_abs drift by only single-digit dollars
# over that span (since they're a since-00:00-UTC cumulative aggregate,
# not a live order-book read), so reusing forecast.py's normalizer left
# flow_score chronically under 0.05 against its own +-1.0 clamp
# (MAX_FLOW_SCORE) — ForecastMove (meant to be the primary driver of
# candle body per the module docstring) came out to single-digit dollars
# on a $60k+ BTC candle, so Close barely moved from Open regardless of
# real conviction, producing a persistent doji shape (confirmed: 2-9%
# body/range on real logged rows) independent of actual market direction.
# These values are derived empirically from the actual per-revision
# deltas already logged in dankbit.forecast.next_candle (416 BTC / 412
# ETH samples across all 3 timeframes as of 2026-08-02): the raw
# 8h-normalized per-leg delta sits at BTC median~1.5/p90~37,
# ETH median~5.5/p90~130; these normalizers put the resulting |flow_score|
# median around 0.10-0.25 and p90 around 1.0 (occasionally clamping, on
# genuinely strong conviction shifts) across all 3 timeframes for both
# assets, rather than forecast.py's own normalizer's ~0.01-0.05
# median/~0.1 p90. Not asset-cfg-driven (unlike forecast.py's own
# DELTA_ABS_NORMALIZER) since Settings-exposed tunables for this engine
# are deliberately deferred — see module docstring.
NEXT_CANDLE_DELTA_ABS_NORMALIZER = {"BTC": 40.0, "ETH": 250.0}
NEXT_CANDLE_DELTA_ABS_NORMALIZER_DEFAULT = 40.0

# TIME_SCALE = sqrt(candle_span_hours / 4.0) — the standard sqrt-of-time
# vol-scaling convention, applied to both ForecastMove and
# StructuralAdjustment so a 1H candle's move is scaled down (0.5x) and a
# 1D candle's up (~2.45x) relative to the already-tuned 4H baseline
# (1.0x, unchanged). Wick sizing needs no separate scale — real
# per-timeframe ATR (see compute_wicks) already scales it correctly.
def time_scale(candle_span_hours):
    return sqrt(candle_span_hours / 4.0)


# StructuralAdjustment = (center - open) * STRUCTURAL_PULL_WEIGHT * activity
# body_mult * WEEKEND_MOVE_MULT * time_scale — a small pull-to-center
# nudge standing in for the spec's GAVG/R-S/BML-SMP bullets (derive_levels'
# own `center` already blends all three). WEEKEND_MOVE_MULT is also applied
# to ForecastMove itself (see compute_revision) — both the flow-driven move
# and the structural pull get damped equally on weekends.
STRUCTURAL_PULL_WEIGHT = 0.12
WEEKEND_MOVE_MULT = 0.6

# Wick sizing — see compute_wicks. WICK_ATR_FACTOR sets the base wick size
# as a fraction of real ATR14 (computed at that timeframe's own native
# interval); WEEKEND_TAIL_RISK_MULT only widens the LOWER wick on weekends
# (spec: Weekend Tail Risk is a Low-only factor).
WICK_ATR_FACTOR = 0.5
WEEKEND_TAIL_RISK_MULT = 1.3

# FlowImbalance damping and Zone Brake — added on review against the same
# Thales dev PDF feedback (2026-08-03) that motivated forecast.py's own
# flow_imbalance()/_zone_brake_mult()/Breakout Gate additions (see
# CLAUDE.md's Thales Forecast candles section), but applied here
# independently — this module deliberately doesn't call into
# simulate_forecast()'s own per-step loop (see module docstring), so those
# additions never touched this engine at all. Before this: ForecastMove
# (the module docstring's own "primary driver of candle body") had no
# Activity Regime, weekend, or trade-count-conviction damping whatsoever —
# only StructuralAdjustment (a much smaller term) did. FlowImbalance reuses
# forecast.flow_imbalance() directly (a plain pure function, no cfg
# needed); Zone Brake reimplements a small local equivalent of
# forecast._zone_brake_mult() rather than importing that module-private
# name, matching this module's own established pattern of reusing
# forecast.py's PUBLIC building blocks (derive_levels, smart_synthetic_
# liquidity, session_activity_regime, _atr14) while keeping its own
# per-step math self-contained.
FLOW_IMBALANCE_NEUTRAL_THRESHOLD = 0.05
FLOW_IMBALANCE_BODY_DAMPING = 0.20

ZONE_BRAKE_ATR_DISTANCE = 0.5
ZONE_BRAKE_MIN_MOVE_MULT = 0.40


def _zone_brake_mult(distance, atr, atr_distance, min_mult):
    """Same shape as forecast._zone_brake_mult(): 1.0 (no brake) once
    `distance` from the level is at least `atr_distance` ATRs away,
    shrinking linearly down to `min_mult` as ForecastOpen closes in;
    `distance` negative (already past the level) also returns 1.0."""
    if distance is None or distance < 0:
        return 1.0
    reference = max(atr * atr_distance, 1e-9)
    if distance >= reference:
        return 1.0
    return max(distance / reference, min_mult)

# Smart Liquidity Close-damping — see apply_smart_liquidity_damping. Looks
# at the last SMART_LIQ_REJECTION_LOOKBACK *closed* real candles (at that
# timeframe's own native interval) for a rejection (wicked into the zone,
# closed back away) vs acceptance (closed through the zone) near top/low.
SMART_LIQ_REJECTION_LOOKBACK = 3

# compute_confidence — liquidity-damping penalty (points, out of 100) at
# damping == one full band_width, and the sign-agreement bonus/penalty
# across the observed F_k readings.
CONFIDENCE_LIQUIDITY_PENALTY_MAX = 15.0
CONFIDENCE_FLOW_AGREEMENT_BONUS = 5.0
CONFIDENCE_FLOW_DISAGREEMENT_PENALTY = 5.0
CONFIDENCE_MIN = 10.0
CONFIDENCE_MAX = 100.0


def current_candle_bounds(now_utc, candle_span_hours):
    """Floor `now_utc` to the `candle_span_hours` UTC grid (e.g. 1h -> every
    hour; 4h -> 00/04/08/12/16/20; 24h -> midnight UTC) -> that candle's own
    start, plus the target candle's start (its own close, `candle_span_hours`
    later) and how many hours have elapsed into it."""
    epoch_hours = int(now_utc.timestamp() // 3600)
    start_epoch_hours = (epoch_hours // candle_span_hours) * candle_span_hours
    current_candle_start = datetime.fromtimestamp(start_epoch_hours * 3600, tz=timezone.utc)
    target_time = current_candle_start + timedelta(hours=candle_span_hours)
    elapsed_hours = (now_utc - current_candle_start).total_seconds() / 3600.0
    return current_candle_start, target_time, elapsed_hours


def revision_for_elapsed_hours(elapsed_hours, candle_span_hours, max_revisions):
    """1..max_revisions — Revision 1 fires one revision-step into the
    current candle (not immediately at its open, matching the spec's own
    08:00-12:00 worked example: Revision 1 at 09:00, ..., Final/Freeze at
    12:00). revision_step = candle_span_hours / max_revisions (1h/4=15min,
    4h/4=1h, 24h/6=4h)."""
    revision_step = candle_span_hours / max_revisions
    return min(max(round(elapsed_hours / revision_step), 1), max_revisions)


def hourly_flow_signal(older_point, newer_point, asset):
    """One "F_k" reading — the same buyer/seller call/put delta-flow-signal
    formula forecast.greek_flow() uses (bullish-positive sign convention),
    evaluated between two CONSECUTIVE points in the self-referencing chain
    (plain dicts with "bcd_abs"/"bpd_abs"/"scd_abs"/"spd_abs" +
    "bucket_epoch" — see models/forecast_next_candle.py for how these are
    assembled from dankbit.forecast.next_candle's own prior rows) rather
    than greek_flow()'s own current-vs-oldest-of-3-history diff. Uses
    NEXT_CANDLE_DELTA_ABS_NORMALIZER (this engine's own per-asset
    normalizer, not forecast.py's cfg-driven DELTA_ABS_NORMALIZER — see
    that constant's own comment for why the two must differ)."""
    normalizer = NEXT_CANDLE_DELTA_ABS_NORMALIZER.get(asset, NEXT_CANDLE_DELTA_ABS_NORMALIZER_DEFAULT)
    hours_ago = max((newer_point["bucket_epoch"] - older_point["bucket_epoch"]) / 3600.0, 1e-6)

    def rate(key):
        return (newer_point[key] - older_point[key]) * forecast_lib.GREEK_FLOW_REF_HOURS / hours_ago / normalizer

    buyer_call = rate("bcd_abs")
    buyer_put = rate("bpd_abs")
    seller_call = rate("scd_abs")
    seller_put = rate("spd_abs")
    return (buyer_call - seller_call) - (buyer_put - seller_put)


def aggregate_flow_score(f_values, prior_flow, revision, max_revisions):
    """FlowScore(k) = ObservedWeight_k * mean(F1..Fk) + PriorWeight_k * PreviousFlow
    — the spec's exact two-stage aggregation (see module docstring).
    ObservedWeight_k = revision/max_revisions, PriorWeight_k = 1 -
    ObservedWeight_k — reproduces the spec's literal 25/50/75/100 schedule
    at max_revisions=4, generalizes cleanly to any revision count (e.g.
    1/6..6/6 for a 6-revision timeframe). `prior_flow` is the previous
    completed cycle's own final (revision==max_revisions) flow_score, or
    0.0 on an asset/timeframe's very first run. Falls back to `prior_flow`
    alone when there's no F_k data yet at all (e.g. this is the very first
    revision ever logged for this asset/timeframe, so there's no prior row
    to diff against)."""
    if not f_values:
        return prior_flow
    current_flow = sum(f_values) / len(f_values)
    observed_weight = revision / max_revisions
    prior_weight = 1.0 - observed_weight
    return observed_weight * current_flow + prior_weight * prior_flow


def compute_structural_adjustment(current, open_price, is_weekend, activity, candle_span_hours):
    """Small pull-to-center term standing in for the spec's GAVG/R-S/
    BML-SMP/Activity Regime/Weekend/session-coefficient StructuralAdjustment
    bullets — see module-level STRUCTURAL_* comment. `activity` is
    forecast.session_activity_regime()'s own result dict (or None)."""
    center = forecast_lib.derive_levels(current)["center"]
    activity_mult = activity.get("body_mult", 1.0) if activity else 1.0
    weekend_mult = WEEKEND_MOVE_MULT if is_weekend else 1.0
    return (center - open_price) * STRUCTURAL_PULL_WEIGHT * activity_mult * weekend_mult * time_scale(candle_span_hours)


def apply_smart_liquidity_damping(raw_close, open_price, top, low, closed_candles, band_width, atr):
    """Damps `raw_close` toward the relevant zone edge (top for a bullish
    move, low for a bearish one) when recent REAL candles (at that
    timeframe's own native interval) show that edge was rejected (wicked
    into, closed back away) rather than accepted (closed through it) —
    reproducing the spec's own worked example (Close damped to
    63,400-63,500, the undamped remainder pushed into Upper Wick instead
    of discarded). `closed_candles` should already be just the last few
    fully-closed real candles (not the still-forming one) — see
    compute_revision. No persisted rejection-history state: re-derived
    fresh from real candle history on every call.

    Returns (close, leftover_upper, leftover_lower, adjustment) — exactly
    one of leftover_upper/leftover_lower is non-zero (the side that got
    damped), adjustment is the signed close delta (damped_close - raw_close)
    for confidence-penalty/diagnostic purposes."""
    move = raw_close - open_price
    if move == 0 or not closed_candles:
        return raw_close, 0.0, 0.0, 0.0

    margin = max(band_width * 0.01, atr * 0.1)
    recent = closed_candles[-SMART_LIQ_REJECTION_LOOKBACK:]

    if move > 0:
        zone_edge = top
        accepted = any(c["c"] > zone_edge for c in recent)
        rejected = any(c["h"] >= zone_edge - margin and c["c"] < zone_edge - margin for c in recent)
        if rejected and not accepted and raw_close > zone_edge - margin:
            damped_close = zone_edge - margin
            leftover = raw_close - damped_close
            return damped_close, leftover, 0.0, damped_close - raw_close
    else:
        zone_edge = low
        accepted = any(c["c"] < zone_edge for c in recent)
        rejected = any(c["l"] <= zone_edge + margin and c["c"] > zone_edge + margin for c in recent)
        if rejected and not accepted and raw_close < zone_edge + margin:
            damped_close = zone_edge + margin
            leftover = damped_close - raw_close
            return damped_close, 0.0, leftover, damped_close - raw_close

    return raw_close, 0.0, 0.0, 0.0


def compute_wicks(open_price, close_price, current, atr, activity, is_weekend, leftover_upper=0.0, leftover_lower=0.0):
    """ForecastHigh/ForecastLow, built AFTER Close per the spec: base wick
    from real ATR14 (at that timeframe's own native interval) x that
    side's own call/put Abs-strength dominance (call gamma/delta/vega for
    Upper, put gamma/delta/vega for Lower) x the current Activity Regime's
    own wick_mult, lower side additionally widened by
    WEEKEND_TAIL_RISK_MULT on weekends, plus whichever leftover
    Smart-Liquidity-damping remainder applies. Literally
    ForecastHigh = max(Open,Close) + UpperWick, ForecastLow = min(Open,Close) - LowerWick.
    Needs no explicit time-scale factor — ATR is already computed from
    that timeframe's own real candles, so it scales correctly on its own."""
    call_strength = (current["bcg_abs"] + current["scg_abs"] + current["bcd_abs"] + current["scd_abs"] + current["bcv_abs"] + current["scv_abs"]) / 6.0
    put_strength = (current["bpg_abs"] + current["spg_abs"] + current["bpd_abs"] + current["spd_abs"] + current["bpv_abs"] + current["spv_abs"]) / 6.0
    total_strength = max(call_strength + put_strength, 1e-9)
    call_ratio = call_strength / total_strength
    put_ratio = 1.0 - call_ratio

    wick_mult = activity.get("wick_mult", 1.0) if activity else 1.0
    base_wick = atr * WICK_ATR_FACTOR * wick_mult

    upper_wick = base_wick * (0.5 + call_ratio) + leftover_upper
    lower_wick = base_wick * (0.5 + put_ratio) + leftover_lower
    if is_weekend:
        lower_wick *= WEEKEND_TAIL_RISK_MULT

    forecast_high = max(open_price, close_price) + upper_wick
    forecast_low = min(open_price, close_price) - lower_wick
    return forecast_high, forecast_low


def compute_confidence(revision, max_revisions, smart_liq_adjustment, band_width, f_values):
    """observed_weight*100 baseline (e.g. 25/50/75/100 at 4 revisions),
    penalized by how much Smart-Liquidity damping moved Close (relative to
    band_width) and by disagreement in sign across the observed F_k
    readings — a documented v1 heuristic, since the spec gives qualitative
    rules for Confidence, not an exact formula."""
    observed_weight = revision / max_revisions
    confidence = observed_weight * 100.0
    confidence -= min(abs(smart_liq_adjustment) / max(band_width, 1e-9), 1.0) * CONFIDENCE_LIQUIDITY_PENALTY_MAX
    if len(f_values) >= 2:
        signs = [1 if v > 0 else (-1 if v < 0 else 0) for v in f_values]
        if signs[0] != 0 and all(s == signs[0] for s in signs):
            confidence += CONFIDENCE_FLOW_AGREEMENT_BONUS
        else:
            confidence -= CONFIDENCE_FLOW_DISAGREEMENT_PENALTY
    return max(min(confidence, CONFIDENCE_MAX), CONFIDENCE_MIN)


def find_candle_by_open_time(real_candles, start_dt):
    """The real candle (oldest-first {t,o,h,l,c} dicts, t in ms) whose own
    open time matches `start_dt`, within a minute's tolerance — used to
    freeze Open to the actual just-closed candle's Close once a target
    candle's own revision cycle completes. None if not found (e.g. candle
    history doesn't reach back far enough yet)."""
    target_ms = int(start_dt.timestamp() * 1000)
    for c in real_candles:
        if abs(c["t"] - target_ms) < 60_000:
            return c
    return None


def compute_revision(asset, timeframe, now_utc, index_price, current_snapshot, points, real_candles,
                      prior_flow, activity, is_weekend, cfg=None):
    """The orchestrating pure function for one revision computation.

    `asset` — "BTC"/"ETH", selects NEXT_CANDLE_DELTA_ABS_NORMALIZER for
    hourly_flow_signal() below.
    `timeframe` — one of TIMEFRAME_CONFIG's keys ("1h"/"4h"/"1d").
    `current_snapshot` — to_dict()-shaped dict for the freshest per-leg
    Greek reading (used for derive_levels/top/low/per-leg Abs fields).
    `points` — oldest-first plain dicts ({"bcd_abs",...,"bucket_epoch"})
    forming the self-referencing chain for this cycle (see
    models/forecast_next_candle.py) — consecutive pairs give F1..Fk. May
    have fewer than revision+1 entries if history doesn't reach back that
    far yet (e.g. this asset/timeframe's very first cycle).
    `real_candles` — oldest-first real {t,o,h,l,c} candles at this
    timeframe's own native interval (Binance), used for ATR14,
    Smart-Liquidity rejection history, and freezing Open.
    `prior_flow` — the previous completed cycle's own final flow_score.
    `activity` — session_activity_regime()'s result dict, or None.

    Returns a plain dict with every field dankbit.forecast.next_candle
    needs to persist for this revision."""
    cfg_tf = TIMEFRAME_CONFIG[timeframe]
    candle_span_hours = cfg_tf["candle_span_hours"]
    max_revisions = cfg_tf["max_revisions"]

    levels = forecast_lib.derive_levels(current_snapshot, cfg=cfg)
    band_width = levels["band_width"]
    top, low = current_snapshot["top"], current_snapshot["low"]

    current_candle_start, target_time, elapsed_hours = current_candle_bounds(now_utc, candle_span_hours)
    revision = revision_for_elapsed_hours(elapsed_hours, candle_span_hours, max_revisions)
    is_final = revision >= max_revisions

    f_values = [
        hourly_flow_signal(points[i - 1], points[i], asset)
        for i in range(1, len(points))
    ][:revision]
    flow_score = aggregate_flow_score(f_values, prior_flow, revision, max_revisions)
    clamped_flow_score = max(min(flow_score, MAX_FLOW_SCORE), -MAX_FLOW_SCORE)

    if is_final:
        closed_candle = find_candle_by_open_time(real_candles, current_candle_start)
        forecast_open = closed_candle["c"] if closed_candle else index_price
    else:
        forecast_open = index_price

    atr = forecast_lib._atr14(real_candles) or (band_width * 0.1)

    structural_adjustment = compute_structural_adjustment(current_snapshot, forecast_open, is_weekend, activity, candle_span_hours)

    # ForecastMove damping — see the module-level FlowImbalance/Zone Brake
    # comment above. Previously this term (the docstring's own "primary
    # driver of candle body") moved at full strength regardless of Activity
    # Regime, weekend, or how one-sided the raw Long/Short trade count
    # actually was; only StructuralAdjustment got any of that damping.
    activity_mult = activity.get("body_mult", 1.0) if activity else 1.0
    weekend_move_mult = WEEKEND_MOVE_MULT if is_weekend else 1.0
    flow_imb = forecast_lib.flow_imbalance(current_snapshot.get("long_trade_count", 0), current_snapshot.get("short_trade_count", 0))
    imbalance_mult = 1.0 - FLOW_IMBALANCE_BODY_DAMPING if (flow_imb is not None and abs(flow_imb) < FLOW_IMBALANCE_NEUTRAL_THRESHOLD) else 1.0

    forecast_move = clamped_flow_score * band_width * FLOW_SCALE_FACTOR * time_scale(candle_span_hours)
    forecast_move *= activity_mult * weekend_move_mult * imbalance_mult

    # Zone Brake — shrink the move further as it heads toward `top`
    # (bullish) or `low` (bearish) within ZONE_BRAKE_ATR_DISTANCE ATRs of
    # that level, continuous and always-on, layered on top of (not instead
    # of) apply_smart_liquidity_damping()'s own binary rejection/acceptance
    # check just below — this dampens the *approach*, that one hard-caps an
    # actual rejected crossing.
    zone_brake_mult = 1.0
    if forecast_move > 0 and top:
        zone_brake_mult = _zone_brake_mult(top - forecast_open, atr, ZONE_BRAKE_ATR_DISTANCE, ZONE_BRAKE_MIN_MOVE_MULT)
    elif forecast_move < 0 and low:
        zone_brake_mult = _zone_brake_mult(forecast_open - low, atr, ZONE_BRAKE_ATR_DISTANCE, ZONE_BRAKE_MIN_MOVE_MULT)
    forecast_move *= zone_brake_mult

    raw_close = forecast_open + forecast_move + structural_adjustment

    closed_candles = real_candles[:-1] if len(real_candles) > 1 else []
    forecast_close, leftover_upper, leftover_lower, smart_liq_adjustment = apply_smart_liquidity_damping(
        raw_close, forecast_open, top, low, closed_candles, band_width, atr,
    )

    forecast_high, forecast_low = compute_wicks(
        forecast_open, forecast_close, current_snapshot, atr, activity, is_weekend,
        leftover_upper, leftover_lower,
    )

    confidence = compute_confidence(revision, max_revisions, smart_liq_adjustment, band_width, f_values)

    return {
        "timeframe": timeframe,
        "current_candle_start": current_candle_start.replace(tzinfo=None),
        "target_time": target_time.replace(tzinfo=None),
        "revision": revision,
        "max_revisions": max_revisions,
        "is_final": is_final,
        "forecast_open": forecast_open,
        "forecast_close": forecast_close,
        "forecast_high": forecast_high,
        "forecast_low": forecast_low,
        "confidence": confidence,
        "greek_flow_score": flow_score,
        "structural_adjustment": structural_adjustment,
        "smart_liquidity_adjustment": smart_liq_adjustment,
        # Combined activity/weekend/FlowImbalance/Zone Brake multiplier
        # actually applied to ForecastMove this revision (see above) — 1.0
        # means none of the 4 dampers fired; kept as one diagnostic number
        # rather than 4 separate fields, since what matters for reviewing a
        # candle after the fact is how much the move got shrunk overall.
        "flow_move_damping_mult": activity_mult * weekend_move_mult * imbalance_mult * zone_brake_mult,
        "activity_regime": activity.get("regime") if activity else None,
        "is_weekend": is_weekend,
        # The 4 raw values needed to compute the NEXT revision's own F_k
        # reading (see models/forecast_next_candle.py's self-referencing
        # chain) — persisted verbatim onto this revision's own row.
        "bcd_abs": current_snapshot["bcd_abs"],
        "bpd_abs": current_snapshot["bpd_abs"],
        "scd_abs": current_snapshot["scd_abs"],
        "spd_abs": current_snapshot["spd_abs"],
    }
