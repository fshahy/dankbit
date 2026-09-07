import base64
import json
import math
import numpy as np
from datetime import datetime, timedelta, timezone
from io import BytesIO
from matplotlib import transforms as mtransforms
from odoo import http
from odoo.http import request
from . import options
from . import delta
from . import gamma
from . import next_candle_forecast

# Every trailing-hours value the raw `?hours=` param on
# /api/four-leg-gamma accepts, and the set _auto_window_hours() resolves
# "auto" into: every integer 1..72 hours, plus 96/120/144/168/192/216/
# 240/480/720 (4d..30d). This grew organically over many product
# decisions (the 1..72 fill-in and the multi-day entries were added for
# the since-removed /l24 and /ft pages' own "Window" dropdowns); the
# pages that survive (/4l/<asset> and /tm/<asset>) both offer only an
# 1h..8h/24h/All subset of it, so many of these values (and the
# "auto"/"midnight" sentinels) are now only reachable by a direct API
# call.
# "All" (`?hours=all`), "midnight" (`?hours=midnight`, since-00:00-UTC),
# and "auto" (`?hours=auto`, dynamic time-to-expiry sizing — see
# _auto_window_hours) are each handled as their own string sentinel in
# four_leg_gamma_json, not members of this tuple — "all" skips the
# trailing-hours trade filter entirely rather than mapping to a number
# of hours, "midnight" maps to options.day_window_start() instead of a
# fixed hour count, and "auto" resolves to one of this tuple's own
# values by calling _auto_window_hours() rather than being one itself.
FOUR_LEG_WINDOW_HOURS_CHOICES = tuple(range(1, 73)) + (96, 120, 144, 168, 192, 216, 240, 480, 720)

# /4l/<asset>'s own "Window" dropdown default — also the fallback used
# by four_leg_gamma_json when `?hours=` is missing/malformed, same as
# every other *_DEFAULT_WINDOW_HOURS fallback in this file. Was a number
# (8, i.e. 8h) until 1h-8h were removed from FOUR_LEG_WINDOW_HOURS_CHOICES
# above, at which point the default moved to the "all" string sentinel
# (see four_leg_gamma_json); moved back to 8 (8h) per product decision —
# also cuts the routine per-poll cost of the page's other 4 lines, since
# "all" meant every poll scanned that instrument's entire trade history —
# then to 24 (24h) per a later product decision, then to 4 (4h) per a
# still later product decision, then back to 24 (24h) once more per a
# still later product decision.
FOUR_LEG_DEFAULT_WINDOW_HOURS = 24

# /ll/<asset> (the LL chart — ll_avg_gamma_json) computes one 4-leg gamma
# AVG per active expiry, then draws only the 2 with the biggest |AVG gamma
# value|. It has no "Expiry" dropdown — every active expiry is considered
# on every poll, each an isolated options.per_leg_gamma() curve build, so
# the loop is capped at the soonest N expiries to bound worst-case cost
# (Deribit realistically lists ~18-21 active BTC/ETH expiries at once, and
# the far-dated ones carry negligible flow anyway — the top-2 by |gamma|
# are always among the nearer, actively-traded ones).
LL_MAX_EXPIRIES = 24


def _auto_window_hours(dte_hours):
    """`?hours=auto` on /api/four-leg-gamma resolves to this — the
    smallest FOUR_LEG_WINDOW_HOURS_CHOICES bucket that still covers
    `dte_hours` (hours remaining until the resolved expiry's own
    settlement), i.e. "look back across this option's whole remaining
    life, snapped to the nearest offered bucket." No published
    professional-desk formula exists for this exact trailing-trade-flow
    case (dealer-positioning tools like SpotGamma key off a full open-
    interest snapshot instead, not a trade-flow lookback), but the
    intuition mirrors how short-dated/0DTE desks actually behave: older
    flow is more likely already rolled/closed as expiry nears, so a
    shrinking lookback keeps the 4 gamma legs weighted toward genuinely
    current positioning instead of stale history. Naturally floors at
    this tuple's own smallest bucket (1h) for anything at/past expiry
    and caps at its largest (720h/30d) for a Weekly/Monthly expiry with
    weeks left, without any separate min/max clamp needed. `None` (no
    resolvable expiration — e.g. Expiry=All, which has no single
    instrument to key a DTE off) falls back to FOUR_LEG_DEFAULT_WINDOW_HOURS."""
    if dte_hours is None:
        return FOUR_LEG_DEFAULT_WINDOW_HOURS
    for choice in FOUR_LEG_WINDOW_HOURS_CHOICES:
        if choice >= dte_hours:
            return choice
    return FOUR_LEG_WINDOW_HOURS_CHOICES[-1]

def _parse_as_of_param(raw):
    """Parses the `?as_of=` query param shared by expiries_json/
    klines_coinbase_proxy/four_leg_gamma_json for /tm/<asset> (the Time
    Machine page, see time_machine_chart below) into a naive-UTC datetime,
    clamped to not exceed "now" (a future as_of has no real historical
    meaning here — every one of this param's consumers already treats
    "now" as the live/default case). `raw` is a naive-UTC ISO string,
    e.g. "2026-03-01T14:30" — no timezone suffix, interpreted as UTC
    directly (matching this addon's UTC-anchored trade-window convention
    throughout, see options.day_window_start). The /tm/<asset> page's own
    "As Of" picker is Europe/Berlin wall-clock time, but its template
    converts that to UTC client-side (berlinNaiveToUtcIso()) before
    appending `&as_of=` to any fetch, so this function still always
    receives a UTC value (a tz-aware string is also normalised below, but
    the /tm client never sends one). Returns None for a missing/malformed
    value, same as every other query param in this file — callers fall
    back to "now" in that case, so a Time Machine URL with no `as_of`
    still behaves exactly like the live /4l/<asset> page it's based on."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return min(parsed, now)


def _forecast_leg_completeness(snapshot):
    """Return whether all four participant legs have any usable Greek pair.

    A whole absent Buyer-Put/Seller-Put/etc. branch previously left BML/SMP
    free to jump to a grid edge and then became a seemingly valid expiry
    anchor.  One non-zero price+strength pair in gamma/delta/theta/vega is
    enough to keep a leg; an entirely absent leg makes the anchor unavailable.
    Works with both snapshot records and preview dictionaries.
    """
    def value(name):
        if isinstance(snapshot, dict):
            return snapshot.get(name)
        return getattr(snapshot, name, None)

    missing = []
    for leg in ("bc", "bp", "sc", "sp"):
        supported = False
        for greek in ("g", "d", "t", "v"):
            price = value(leg + greek + "_price")
            strength = value(leg + greek + "_abs")
            if price and float(price) > 0 and strength and float(strength) > 0:
                supported = True
                break
        if not supported:
            missing.append(leg.upper())
    return not missing, missing


def _forecast_quality_warning(points, anchor_meta, index_price, sigma_annual, timeframe):
    """Assess confidence separately from the Forecast geometry.

    The restored Forecast engine is intentionally allowed to show its raw
    directional path again.  This guard does not reshape, clamp, reject, or
    hide a candle.  It raises a warning only when option-data quality is low
    *and* the resulting path is statistically large relative to the current
    implied-volatility move for the selected candle width/horizon.
    """
    available = [a for a in (anchor_meta or []) if a.get("available")]
    coverage = sum(float(a.get("quality_weight", 1.0)) for a in available) / 3.0
    total_anchor_weight = sum(float(a.get("quality_weight", 1.0)) for a in available)
    mean_confidence = (
        sum(float(a.get("confidence") or 0.0) * float(a.get("quality_weight", 1.0)) for a in available)
        / total_anchor_weight
        if total_anchor_weight > 0.0 else 0.0
    )
    quality_score = max(min(coverage * 60.0 + mean_confidence * 0.40, 100.0), 0.0)
    low_quality = quality_score < 65.0

    step_hours = {"1h": 1.0, "4h": 4.0, "1d": 24.0}.get(timeframe, 4.0)
    base_price = float(index_price or 0.0)
    sigma = float(sigma_annual or 0.0)
    max_body_sigma = 0.0
    max_path_sigma = 0.0
    if base_price > 0 and sigma > 0:
        for point in points or []:
            hours_out = max(float(point.get("hours") or step_hours), step_hours)
            body_expected = float(base_price * sigma * np.sqrt(step_hours / (365.0 * 24.0)))
            path_expected = float(base_price * sigma * np.sqrt(hours_out / (365.0 * 24.0)))
            body_sigma = abs(float(point["close"]) - float(point["open"])) / max(body_expected, 1e-9)
            path_sigma = abs(float(point["close"]) - base_price) / max(path_expected, 1e-9)
            max_body_sigma = max(max_body_sigma, body_sigma)
            max_path_sigma = max(max_path_sigma, path_sigma)

    extremity = float(max(max_body_sigma, max_path_sigma))
    extreme = bool(extremity >= 2.25)
    active = bool(low_quality)
    missing_reasons = [
        a.get("unavailable_reason") for a in (anchor_meta or [])
        if a.get("unavailable_reason")
    ]
    return {
        "active": active,
        "code": ("LOW_OPTION_DATA_EXTREME_FORECAST" if extreme else "LOW_OPTION_DATA") if active else None,
        "severity": "high" if active and extremity >= 3.5 else ("warning" if active else "none"),
        "quality_score": round(quality_score, 1),
        "quality_level": "low" if low_quality else ("medium" if quality_score < 80.0 else "good"),
        "anchor_coverage": len(available),
        "weighted_anchor_coverage": round(coverage, 3),
        "mean_anchor_confidence": round(mean_confidence, 1),
        "max_body_sigma": round(max_body_sigma, 2),
        "max_path_sigma": round(max_path_sigma, 2),
        "extreme": extreme,
        "missing_reasons": missing_reasons,
        "message": "Low option-data quality: the Forecast path may be exaggerated." if active else None,
        "message_fa": "کیفیت داده آپشن پایین است؛ حرکت کندل‌های فورکست ممکن است افراطی باشد." if active else None,
    }


def _compose_unified_forecast(raw_points, anchors=None, count=18, max_count=18, timeframe="4h"):
    """Build one continuous path with calibrated, propagating expiry anchors.

    EA1/EA2/EA3 keep their independently revised Option-Flow bodies, but a
    same-direction confluence of Band-High/Band-Low/Gamma, Greek Flow and
    Smart Liquidity may calibrate (never reverse) that body.  The reliable
    flow component of each anchor then crossfades into the candles before the
    next anchor instead of disappearing immediately after the EA candle.

    The overlay is deliberately applied before the final weekend safety pass:
    every Saturday/Sunday body remains subject to the original 0.45/0.26 ATR
    first/subsequent caps and the 1.45 ATR total weekend budget.
    """
    limit = max(1, min(int(count or max_count), int(max_count)))
    source = list(raw_points or [])[:limit]
    if not source:
        return []

    anchors = anchors or {}
    candles_per_day = {"1h": 24, "4h": 6, "1d": 1}.get(timeframe, 6)
    anchor_by_index = {
        expiry_index * candles_per_day: candle
        for expiry_index, candle in anchors.items()
        if candle and expiry_index * candles_per_day < len(source)
    }

    def sign(value, epsilon=1e-9):
        value = float(value or 0.0)
        return 1 if value > epsilon else -1 if value < -epsilon else 0

    def reliability(anchor):
        revision = float(anchor.get("revision") or 0.0)
        maximum = max(float(anchor.get("max_revisions") or revision or 1.0), 1.0)
        confidence = max(min(float(anchor.get("confidence") or 0.0) / 100.0, 1.0), 0.0)
        quality_weight = max(min(float(anchor.get("quality_weight", 1.0)), 1.0), 0.0)
        return max(min((revision / maximum) * confidence * quality_weight, 1.0), 0.0)

    def confluence_multiplier(anchor, raw):
        """Confidence calibration only; it cannot create/reverse direction."""
        anchor_direction = sign(float(anchor["close"]) - float(anchor["open"]))
        flow_direction = sign(anchor.get("greek_flow_score")) or anchor_direction
        raw_direction = sign(float(raw.get("close", 0.0)) - float(raw.get("open", 0.0)))
        if (not anchor_direction or flow_direction != anchor_direction
                or (raw_direction and raw_direction != anchor_direction)):
            return 1.0, 0

        confirmations = 0
        # all_aligned means the independently moving upper band, lower band
        # and Gamma path agree. Count those three components explicitly.
        if raw.get("gb_all_aligned") and int(raw.get("gb_consensus_direction") or 0) == anchor_direction:
            confirmations += 3
        elif int(raw.get("gb_consensus_direction") or 0) == anchor_direction:
            confirmations += 1
        if sign(raw.get("impulse_greek_flow")) == anchor_direction:
            confirmations += 1
        if sign(raw.get("impulse_liquidity")) == anchor_direction:
            confirmations += 1

        # 0-2 agreements do not boost; 3/4/5 map to 1.03/1.07/1.20.
        boost_by_count = {3: 1.03, 4: 1.07, 5: 1.20}
        multiplier = boost_by_count.get(min(confirmations, 5), 1.0)
        if anchor.get("is_weekend") or raw.get("is_weekend"):
            multiplier = min(multiplier, 1.10)
        # An incomplete/low-confidence revision receives only the reliable
        # fraction of the proposed extra boost.
        multiplier = 1.0 + (multiplier - 1.0) * reliability(anchor)
        return multiplier, confirmations

    def calibrated_anchor(anchor, raw):
        item = dict(anchor)
        body = float(item["close"]) - float(item["open"])
        structural = float(item.get("structural_adjustment") or 0.0)
        flow_component = body - structural
        multiplier, confirmations = confluence_multiplier(item, raw)
        calibrated_flow = flow_component * multiplier
        proposed_body = structural + calibrated_flow
        raw_body = float(raw["close"]) - float(raw["open"])
        quality_weight = max(min(float(item.get("quality_weight", 1.0)), 1.0), 0.0)
        # Keep Partial/Stale anchors and their real Prior Flow, but let them
        # bend the base Forecast only in proportion to data quality.
        item["calibrated_body"] = raw_body + quality_weight * (proposed_body - raw_body)
        flow_direction = sign(item.get("greek_flow_score")) or sign(calibrated_flow)
        item["reliable_flow"] = (
            calibrated_flow * reliability(item)
            if sign(calibrated_flow) == flow_direction else 0.0
        )
        item["confluence_multiplier"] = multiplier
        item["confluence_confirmations"] = confirmations
        return item

    calibrated = {
        index: calibrated_anchor(anchor, source[index])
        for index, anchor in anchor_by_index.items()
    }

    weekend_started = False
    weekend_move = 0.0
    weekend_reference_atr = 0.0

    def safe_body(raw, body):
        """Final guard: anchor overlays cannot bypass weekend ATR limits."""
        nonlocal weekend_started, weekend_move, weekend_reference_atr
        body = float(body)
        if not raw.get("is_weekend"):
            weekend_started = False
            weekend_move = 0.0
            weekend_reference_atr = 0.0
            return body
        effective_atr = max(float(raw.get("effective_atr") or 0.0), 1e-9)
        if not weekend_started:
            weekend_started = True
            weekend_reference_atr = effective_atr
            per_candle_cap = 0.45 * effective_atr
        else:
            per_candle_cap = 0.26 * effective_atr
        remaining = max(1.45 * weekend_reference_atr - weekend_move, 0.0)
        cap = min(per_candle_cap, remaining)
        body = max(min(body, cap), -cap)
        weekend_move += abs(body)
        return body

    def geometry(raw, new_open, body=None, wick_source=None, mode=None):
        old_open = float(raw["open"])
        old_close = float(raw["close"])
        old_high = float(raw["high"])
        old_low = float(raw["low"])
        candle_body = safe_body(raw, old_close - old_open if body is None else float(body))
        wick = wick_source or raw
        wick_open = float(wick["open"])
        wick_close = float(wick["close"])
        wick_high = float(wick["high"])
        wick_low = float(wick["low"])
        upper_wick = max(0.0, wick_high - max(wick_open, wick_close))
        lower_wick = max(0.0, min(wick_open, wick_close) - wick_low)
        new_close = float(new_open) + candle_body
        return {
            "hours": raw.get("hours"), "open": float(new_open),
            "high": max(float(new_open), new_close) + upper_wick,
            "low": min(float(new_open), new_close) - lower_wick,
            "close": new_close, "mode": mode or raw.get("mode"),
        }

    output = []
    first_anchor = calibrated.get(0)
    if first_anchor:
        output.append(geometry(
            source[0], float(source[0]["open"]),
            body=float(first_anchor["calibrated_body"]),
            wick_source=first_anchor, mode="expiry_anchor_1",
        ))
    else:
        output.append(dict(source[0]))

    cursor = 1
    previous_anchor_index = 0 if first_anchor else None
    for anchor_index in sorted(i for i in calibrated if i > 0):
        anchor = calibrated[anchor_index]
        intermediate = source[cursor:anchor_index]
        if intermediate:
            left_anchor = calibrated.get(previous_anchor_index) if previous_anchor_index is not None else None
            segment_span = anchor_index - (previous_anchor_index if previous_anchor_index is not None else cursor - 1)
            for offset, raw in enumerate(intermediate, start=1):
                progress = min(max(offset / max(segment_span, 1), 0.0), 1.0)
                left_flow = float(left_anchor.get("reliable_flow", 0.0)) if left_anchor else 0.0
                right_flow = float(anchor.get("reliable_flow", 0.0))
                blended_flow = left_flow * (1.0 - progress) + right_flow * progress
                # The main 18-candle engine already contains Greek Flow; only
                # a residual share is propagated to avoid double counting.
                overlay = 0.35 * blended_flow
                atr_cap = 0.12 * max(float(raw.get("effective_atr") or 0.0), 0.0)
                if atr_cap:
                    overlay = max(min(overlay, atr_cap), -atr_cap)
                raw_body = float(raw["close"]) - float(raw["open"])
                # A conflicting raw candle is not force-reversed: propagation
                # is reduced to 25%; aligned/neutral candles take full overlay.
                if sign(raw_body) and sign(blended_flow) and sign(raw_body) != sign(blended_flow):
                    overlay *= 0.25
                # No absolute-price snap-back to the raw EA open: the anchor
                # candle is body-defined and starts at the preceding close.
                # A snap-back correction would cancel a persistent same-side
                # flow and recreate the tiny intermediate candles this bridge
                # is meant to fix.
                adjusted_body = raw_body + overlay
                output.append(geometry(raw, output[-1]["close"], body=adjusted_body))
        output.append(geometry(
            source[anchor_index], float(output[-1]["close"]),
            body=float(anchor["calibrated_body"]),
            wick_source=anchor,
            mode="expiry_anchor_%s" % (anchor_index // candles_per_day + 1),
        ))
        cursor = anchor_index + 1
        previous_anchor_index = anchor_index

    tail_anchor = calibrated.get(previous_anchor_index) if previous_anchor_index is not None else None
    tail_length = max(len(source) - cursor, 1)
    for offset, raw in enumerate(source[cursor:], start=1):
        raw_body = float(raw["close"]) - float(raw["open"])
        decay = max(1.0 - offset / (tail_length + 1.0), 0.0)
        overlay = 0.35 * float(tail_anchor.get("reliable_flow", 0.0)) * decay if tail_anchor else 0.0
        atr_cap = 0.12 * max(float(raw.get("effective_atr") or 0.0), 0.0)
        if atr_cap:
            overlay = max(min(overlay, atr_cap), -atr_cap)
        if sign(raw_body) and sign(overlay) and sign(raw_body) != sign(overlay):
            overlay *= 0.25
        output.append(geometry(raw, float(output[-1]["close"]), body=raw_body + overlay))
    return output


def _rsi_forecast_geometry(points, timeframe_candles, hourly_candles, timeframe="4h", period=14, ma_period=14):
    """Adjust only early Forecast wick geometry from RSI mean-reversion risk.

    Open/Close and therefore direction, path and EA locations are immutable.
    A large RSI-vs-RSI-MA gap can lengthen the rejection-side wick, but the
    effect is progressively suppressed while closed 1H price momentum in the
    prevailing direction remains strong. Recomputed on every request.
    """
    output = [dict(p) for p in (points or [])]
    tf_closed = list(timeframe_candles or [])[:-1]
    h1_closed = list(hourly_candles or [])[:-1]

    def value(row, key):
        aliases = {"o": "open", "h": "high", "l": "low", "c": "close"}
        return float(row.get(key, row.get(aliases[key], 0.0)) or 0.0)

    def atr(rows, length=14):
        if len(rows) < 2:
            return 0.0
        ranges = []
        for prev, current in zip(rows[-length - 1:-1], rows[-length:]):
            high, low, prev_close = value(current, "h"), value(current, "l"), value(prev, "c")
            ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return sum(ranges) / len(ranges) if ranges else 0.0

    def rsi_values(closes, length):
        if len(closes) <= length:
            return []
        deltas = [b - a for a, b in zip(closes, closes[1:])]
        gains = [max(d, 0.0) for d in deltas]
        losses = [max(-d, 0.0) for d in deltas]
        avg_gain = sum(gains[:length]) / length
        avg_loss = sum(losses[:length]) / length
        values = [100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)]
        for gain, loss in zip(gains[length:], losses[length:]):
            avg_gain = (avg_gain * (length - 1) + gain) / length
            avg_loss = (avg_loss * (length - 1) + loss) / length
            values.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
        return values

    closes = [value(c, "c") for c in tf_closed]
    rsi_series = rsi_values(closes, period)
    if len(rsi_series) < ma_period or not output:
        return output, {"available": False}
    rsi = rsi_series[-1]
    rsi_ma = sum(rsi_series[-ma_period:]) / ma_period
    gap = rsi - rsi_ma
    gap_strength = max(min((abs(gap) - 5.0) / 10.0, 1.0), 0.0)
    correction_sign = 1.0 if gap < 0 else -1.0 if gap > 0 else 0.0

    atr_1h = atr(h1_closed)
    recent = h1_closed[-8:]
    persistence_sign = -correction_sign
    momentum = 0.0
    if gap_strength and len(recent) >= 4 and atr_1h > 0:
        cumulative = persistence_sign * (value(recent[-1], "c") - value(recent[0], "o"))
        cumulative_score = max(min(cumulative / (2.0 * atr_1h), 1.0), 0.0)
        bodies = [value(c, "c") - value(c, "o") for c in recent]
        body_total = sum(abs(b) for b in bodies) or 1.0
        body_score = max(min(sum(max(persistence_sign * b, 0.0) for b in bodies) / body_total, 1.0), 0.0)
        locations = []
        for candle in recent[-4:]:
            high, low = value(candle, "h"), value(candle, "l")
            location = (value(candle, "c") - low) / max(high - low, 1e-9)
            locations.append(location if persistence_sign > 0 else 1.0 - location)
        close_score = sum(locations) / len(locations)
        h1_closes = [value(c, "c") for c in h1_closed]
        def ema(values, length):
            alpha = 2.0 / (length + 1.0)
            result = float(values[0])
            for item in values[1:]:
                result = alpha * float(item) + (1.0 - alpha) * result
            return result
        ema_now = ema(h1_closes[-60:], 20)
        previous_closes = h1_closes[-66:-6] if len(h1_closes) >= 66 else h1_closes[:-6]
        ema_prev = ema(previous_closes, 20) if previous_closes else ema_now
        slope_score = max(min(persistence_sign * (ema_now - ema_prev) / max(atr_1h, 1e-9), 1.0), 0.0)
        momentum = max(min(0.35 * cumulative_score + 0.25 * body_score + 0.20 * close_score + 0.20 * slope_score, 1.0), 0.0)

    effective = gap_strength * (1.0 - 0.90 * momentum)
    tf_atr = atr(tf_closed)
    affected = {"1h": 6, "4h": 3, "1d": 1}.get(timeframe, 3)
    max_wick = 0.20 * tf_atr
    for index in range(min(affected, len(output))):
        decay = (affected - index) / affected
        adjustment = max_wick * effective * decay
        if correction_sign > 0:
            # RSI below its average: express upward correction probability as
            # an upper rejection wick without changing the bearish body/path.
            output[index]["high"] = max(float(output[index]["high"]), max(float(output[index]["open"]), float(output[index]["close"])) + adjustment)
        elif correction_sign < 0:
            # RSI above its average: express downward correction probability
            # as a lower rejection wick without changing the bullish body/path.
            output[index]["low"] = min(float(output[index]["low"]), min(float(output[index]["open"]), float(output[index]["close"])) - adjustment)
    return output, {
        "available": True, "rsi": round(rsi, 2), "rsi_ma": round(rsi_ma, 2),
        "gap": round(gap, 2), "gap_strength": round(gap_strength, 3),
        "momentum_1h": round(momentum * 100.0, 1),
        "effective_geometry_weight": round(effective, 3), "affected_candles": affected,
    }


class _AggTrade:
    """SQL-aggregated trade row — duck-typed for portfolio_delta/gamma."""
    __slots__ = ("strike", "option_type", "direction", "amount", "iv", "_expiration")

    def __init__(self, strike, option_type, direction, expiration, amount, iv):
        self.strike = strike
        self.option_type = option_type
        self.direction = direction
        self.amount = amount
        self.iv = iv
        self._expiration = expiration

    def get_hours_to_expiry(self):
        if not self._expiration:
            return 0.0
        now = datetime.now(timezone.utc)
        exp = self._expiration if self._expiration.tzinfo else self._expiration.replace(tzinfo=timezone.utc)
        return max((exp - now).total_seconds() / 3600.0, 0.0)


class ChartController(http.Controller):
    @http.route("/help", auth="user", type="http", website=True)
    def help_page(self):
        return request.render("dankbit.dankbit_help")

    @http.route("/<string:instrument>/s", type="http", auth="user", website=True)
    def chart_slideshow(self, instrument):
        return request.render("dankbit.dankbit_slideshow", {
            "instrument": instrument,
            "hours_list": [0, 4, 8, 12, 24],
        })

    @http.route("/<string:instrument>/<int:hours>", type="http", auth="user", website=True)
    def chart_png_hours(self, instrument, hours):
        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        domain = [
            ("name", "ilike", f"{instrument}"),
            ("expiration", ">=", datetime.now(timezone.utc).replace(tzinfo=None)),
            ("deribit_ts", ">=", cutoff),
            ("iv", "!=", 0),
        ]

        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)

        for trade in trades:
            if trade.option_type == "call":
                if trade.direction == "buy":
                    obj.long_call(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    obj.short_call(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                if trade.direction == "buy":
                    obj.long_put(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    obj.short_put(trade.strike, trade.price * trade.index_price)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)
        fig, ax = obj.plot(index_price,
                           market_deltas,
                           market_gammas,
                           False,
                           title=f"{hours}H",
                           width=18,
                           height=8)

        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        d_arr = np.asarray(market_deltas, dtype=float)
        g_arr = np.asarray(market_gammas, dtype=float)
        d_lim = float(np.max(np.abs(d_arr[np.isfinite(d_arr)]))) if np.any(np.isfinite(d_arr)) else 1.0
        g_lim = float(np.max(np.abs(g_arr[np.isfinite(g_arr)]))) if np.any(np.isfinite(g_arr)) else 1.0

        for px, gval in self.find_gamma_peaks(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            # normalised positions of gamma and delta at this x (0=bottom, 1=top of axes)
            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            # pick the y fraction furthest from both curves
            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for px, gval in self.find_gamma_bottoms(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            # normalised positions of gamma and delta at this x (0=bottom, 1=top of axes)
            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            # pick the y fraction furthest from both curves
            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                demand = d_arr[i] > 0
                color = "red" if demand else "green"
                ax.axvline(x=px, color=color, linewidth=1.2, linestyle="-", alpha=0.8)
                g_norm = 0.5 + 0.5 * (float(np.interp(px, STs, g_arr)) / g_lim) if g_lim else 0.5
                y = 0.04 if g_norm > 0.5 else 0.96
                ax.text(px, y, f"${px:,.0f}", transform=trans, color=color,
                        fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                        rotation=90)

        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        last_ts = last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M') if last_trade else "—"
        ax.text(
            0.01, 0.04,
            f"{len(trades)} Trades ({hours}h)",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.text(
            0.01, 0.01,
            f"Last trade: {last_ts}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": f"{hours}h",
                "plot_title": f"{instrument} - Last {hours}h",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
            }
        )

    @http.route("/<string:instrument>/zones", type="http", auth="user", website=True)
    def chart_png_zones(self, instrument):
        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        # The most recent UTC midnight (see options.day_window_start).
        midnight_utc = options.day_window_start(
            datetime.now(timezone.utc).replace(tzinfo=None)
        ).strftime("%Y-%m-%d %H:%M:%S")
        domain = [
            ("name", "=ilike", f"{instrument}-%"),
            ("expiration", ">=", datetime.now(timezone.utc).replace(tzinfo=None)),
            ("deribit_ts", ">=", midnight_utc),
            ("iv", "!=", 0),
        ]
        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)

        long_count = len(trades.filtered(lambda t: t.direction == "buy"))
        short_count = len(trades.filtered(lambda t: t.direction == "sell"))
        longs_obj, shorts_obj = options.build_zone_curves(
            instrument, index_price, trades, from_price, to_price, steps
        )

        fig, ax = longs_obj.plot_zones(
            longs_obj.payoffs, shorts_obj.payoffs, index_price, title="Zones", width=3.5
        )

        ax.text(
            0.01, 0.02,
            f"{long_count} longs\n{short_count} shorts\n(since 00:00 UTC)",
            transform=ax.transAxes,
            fontsize=14,
            va="bottom",
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")

        # Seller Max Profit/Buyer Max Loss/zone info used to be drawn inside
        # the PNG itself (matplotlib ax.text) — now rendered as page HTML
        # (top-left overlay, see dankbit_page template) instead, off the
        # same summary dankbit.bands uses, so the two can never
        # disagree.
        summary = options.zone_summary(longs_obj.STs, longs_obj.payoffs, shorts_obj.payoffs)

        def _format_zone(zone):
            # zone is None (no crossing at all), or a (low, high) pair that
            # collapses to a single price when only one curve contributed a
            # crossing — shown as one number rather than a zero-width range.
            if zone is None:
                return "n/a"
            low, high = zone
            if low == high:
                return "${:,.0f}".format(low)
            return "${:,.0f} - ${:,.0f}".format(low, high)

        high_zone = _format_zone(summary["high_zone"])
        low_zone = _format_zone(summary["low_zone"])
        middle_zone = _format_zone(summary["middle_zone"])
        high_resistance = "n/a" if summary["high_resistance"] is None else "${:,.0f}".format(summary["high_resistance"])
        low_support = "n/a" if summary["low_support"] is None else "${:,.0f}".format(summary["low_support"])

        # Restrict to the single nearest (soonest-to-expire) expiry among
        # `trades` — same "next expiry only" restriction dankbit.bands
        # uses, in case `instrument` isn't already a single fully-qualified
        # expiry — then delegate the actual per-leg gamma/delta/theta/vega
        # extrema computation to options.per_leg_greeks(), the single source
        # of truth also used by dankbit.bands's gamma_band/delta_band
        # and forecast.per_leg_greeks(), so this page can never disagree
        # with either on these numbers. r=0.0 throughout (per_leg_greeks'
        # own default) to match every other Greek computed on this page —
        # zones deliberately doesn't use the r=0.05 the combined-portfolio
        # routes use.
        next_expiration = min(trades.mapped("expiration")) if trades else None
        next_expiration_trades = trades.filtered(lambda t: t.expiration == next_expiration)
        legs = options.per_leg_greeks(longs_obj.STs, next_expiration_trades)
        lc, lp, sc, sp = legs["long_call"], legs["long_put"], legs["short_call"], legs["short_put"]

        bcg_price, bcg_value = lc["gamma_price"], lc["gamma_value"]
        bpg_price, bpg_value = lp["gamma_price"], lp["gamma_value"]
        scg_price, scg_value = sc["gamma_price"], sc["gamma_value"]
        spg_price, spg_value = sp["gamma_price"], sp["gamma_value"]

        bcd_price, bcd_value = lc["delta_price"], lc["delta_value"]
        bpd_price, bpd_value = lp["delta_price"], lp["delta_value"]
        scd_price, scd_value = sc["delta_price"], sc["delta_value"]
        spd_price, spd_value = sp["delta_price"], sp["delta_value"]

        bct_price, bct_value = lc["theta_price"], lc["theta_value"]
        bpt_price, bpt_value = lp["theta_price"], lp["theta_value"]
        sct_price, sct_value = sc["theta_price"], sc["theta_value"]
        spt_price, spt_value = sp["theta_price"], sp["theta_value"]

        bcv_price, bcv_value = lc["vega_price"], lc["vega_value"]
        bpv_price, bpv_value = lp["vega_price"], lp["vega_value"]
        scv_price, scv_value = sc["vega_price"], sc["vega_value"]
        spv_price, spv_value = sp["vega_price"], sp["vega_value"]

        # Each line is {text, color} — color is None for the default
        # (black) styling every line used before per-line colors were
        # needed; only section headers like "Gamma" below set one.
        def _line(text, color=None):
            return {"text": text, "color": color}

        # A leg with zero trades in this window has no gamma/delta/theta/
        # vega curve to peak/bottom at — options.per_leg_greeks() reports
        # that as price=None rather than a fake price-grid-edge value (see
        # its own docstring). "n/a", same as High/Resistance's/
        # Low/Support's own None-handling just above.
        def _price(p):
            return "n/a" if p is None else "${:,.0f}".format(p)

        zone_info_lines = [
            _line("Seller Max Profit (SMP): ${:,.0f}".format(summary["seller_max_profit"])),
            _line("Buyer Max Loss (BML): ${:,.0f}".format(summary["buyer_max_loss"])),
            _line(" "),  # blank spacer line — a truly empty div collapses to zero height
            _line(f"High Zone: {high_zone}"),
            _line(f"Low Zone: {low_zone}"),
            _line(f"Middle Zone: {middle_zone}"),
            _line(" "),
            _line(f"High/Resistance: {high_resistance}"),
            _line(f"Low/Support: {low_support}"),
            _line(" "),
            _line("Gamma", color="violet"),
            _line(f"Buyer Call Gamma (BCG): {_price(bcg_price)}"),
            _line(f"Buyer Put Gamma (BPG): {_price(bpg_price)}"),
            _line(f"Seller Call Gamma (SCG): {_price(scg_price)}"),
            _line(f"Seller Put Gamma (SPG): {_price(spg_price)}"),
            _line(" "),
            _line("BCG Abs.: {:,.0f}".format(abs(bcg_value) / 1_000_000)),
            _line("BPG Abs.: {:,.0f}".format(abs(bpg_value) / 1_000_000)),
            _line("SCG Abs.: {:,.0f}".format(abs(scg_value) / 1_000_000)),
            _line("SPG Abs.: {:,.0f}".format(abs(spg_value) / 1_000_000)),
            _line(" "),
            _line("Delta", color="green"),
            _line(f"Buyer Call Delta (BCD): {_price(bcd_price)}"),
            _line(f"Buyer Put Delta (BPD): {_price(bpd_price)}"),
            _line(f"Seller Call Delta (SCD): {_price(scd_price)}"),
            _line(f"Seller Put Delta (SPD): {_price(spd_price)}"),
            _line(" "),
            _line("BCD Abs.: {:,.0f}".format(abs(bcd_value) / 10)),
            _line("BPD Abs.: {:,.0f}".format(abs(bpd_value) / 10)),
            _line("SCD Abs.: {:,.0f}".format(abs(scd_value) / 10)),
            _line("SPD Abs.: {:,.0f}".format(abs(spd_value) / 10)),
        ]

        # Theta and Vega get their own top-right overlay (see
        # .zone-info-right in dankbit_page) rather than sitting in the
        # top-left zone_info_lines column with everything else.
        right_info_lines = [
            _line("Theta", color="orange"),
            _line(f"Buyer Call Theta (BCT): {_price(bct_price)}"),
            _line(f"Buyer Put Theta (BPT): {_price(bpt_price)}"),
            _line(f"Seller Call Theta (SCT): {_price(sct_price)}"),
            _line(f"Seller Put Theta (SPT): {_price(spt_price)}"),
            _line(" "),
            _line("BCT Abs.: {:,.0f}".format(abs(bct_value) / 10_000)),
            _line("BPT Abs.: {:,.0f}".format(abs(bpt_value) / 10_000)),
            _line("SCT Abs.: {:,.0f}".format(abs(sct_value) / 10_000)),
            _line("SPT Abs.: {:,.0f}".format(abs(spt_value) / 10_000)),
            _line(" "),
            _line("Vega", color="blue"),
            _line(f"Buyer Call Vega (BCV): {_price(bcv_price)}"),
            _line(f"Buyer Put Vega (BPV): {_price(bpv_price)}"),
            _line(f"Seller Call Vega (SCV): {_price(scv_price)}"),
            _line(f"Seller Put Vega (SPV): {_price(spv_price)}"),
            _line(" "),
            # Raw portfolio-vega values sit in the low thousands here — an
            # order of magnitude below theta's ten-thousands and well above
            # delta's tens — so /100 keeps these in the same easy-to-copy
            # 2-3 digit range as the other Value lines (see gamma's /1e6,
            # theta's /1e4, delta's /10 above).
            _line("BCV Abs.: {:,.0f}".format(abs(bcv_value) / 100)),
            _line("BPV Abs.: {:,.0f}".format(abs(bpv_value) / 100)),
            _line("SCV Abs.: {:,.0f}".format(abs(scv_value) / 100)),
            _line("SPV Abs.: {:,.0f}".format(abs(spv_value) / 100)),
        ]

        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": "Zones",
                "plot_title": f"{instrument} - Zones",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
                "zone_info_lines": zone_info_lines,
                "theta_info_lines": right_info_lines,
            }
        )

    # instrument, trades since 00:00 UTC, single-leg delta/gamma routes
    # (/lp, /lc, /sp, /sc) — maps each route's short key to which trades to
    # keep (direction/option_type), which OptionStrat leg method accumulates
    # them, and the delta-saturation fraction/side (see
    # options.delta_saturation_price, shared with dankbit.bands's
    # own delta_band) that locates the green marker line: calls saturate ITM
    # at high S, puts at low S, independent of long/short — each leg's own
    # sign is inherited automatically from its curve's value at that edge.
    DELTA_SATURATION_FRACTION = options.DELTA_SATURATION_FRACTION
    _LEG_ROUTES = {
        "lp": {"direction": "buy", "option_type": "put", "method": "long_put", "label": "Long Puts",
               "saturation_fraction": DELTA_SATURATION_FRACTION, "saturation_side": "min"},
        "lc": {"direction": "buy", "option_type": "call", "method": "long_call", "label": "Long Calls",
               "saturation_fraction": DELTA_SATURATION_FRACTION, "saturation_side": "max"},
        "sp": {"direction": "sell", "option_type": "put", "method": "short_put", "label": "Short Puts",
               "saturation_fraction": DELTA_SATURATION_FRACTION, "saturation_side": "min"},
        "sc": {"direction": "sell", "option_type": "call", "method": "short_call", "label": "Short Calls",
               "saturation_fraction": DELTA_SATURATION_FRACTION, "saturation_side": "max"},
    }

    def _annotate_gamma_delta_crossings(self, ax, STs, market_deltas, market_gammas):
        """Gamma peak/bottom markers (dashed black) and delta=0 crossings
        (solid — green for "supply", red for "demand") — same overlay drawn
        inline by chart_png_hours/chart_png_all, factored out here so the 4
        single-leg routes below don't each carry their own copy."""
        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        d_arr = np.asarray(market_deltas, dtype=float)
        g_arr = np.asarray(market_gammas, dtype=float)
        d_lim = float(np.max(np.abs(d_arr[np.isfinite(d_arr)]))) if np.any(np.isfinite(d_arr)) else 1.0
        g_lim = float(np.max(np.abs(g_arr[np.isfinite(g_arr)]))) if np.any(np.isfinite(g_arr)) else 1.0

        for px, gval in self.find_gamma_peaks(STs, market_gammas) + self.find_gamma_bottoms(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                demand = d_arr[i] > 0
                color = "red" if demand else "green"
                ax.axvline(x=px, color=color, linewidth=1.2, linestyle="-", alpha=0.8)
                g_norm = 0.5 + 0.5 * (float(np.interp(px, STs, g_arr)) / g_lim) if g_lim else 0.5
                y = 0.04 if g_norm > 0.5 else 0.96
                ax.text(px, y, f"${px:,.0f}", transform=trans, color=color,
                        fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                        rotation=90)

    def _chart_png_single_leg(self, instrument, leg_key):
        cfg = self._LEG_ROUTES[leg_key]
        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        # Anchored/left-prefix match (not a bare ilike substring) and trades
        # since the most recent UTC midnight (options.day_window_start) —
        # same domain convention as chart_png_zones, so a query for one
        # expiry can't pull in another instrument's trades.
        midnight_utc = options.day_window_start(
            datetime.now(timezone.utc).replace(tzinfo=None)
        ).strftime("%Y-%m-%d %H:%M:%S")
        domain = [
            ("name", "=ilike", f"{instrument}-%"),
            ("expiration", ">=", datetime.now(timezone.utc).replace(tzinfo=None)),
            ("deribit_ts", ">=", midnight_utc),
            ("direction", "=", cfg["direction"]),
            ("option_type", "=", cfg["option_type"]),
            ("iv", "!=", 0),
        ]
        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
        leg_method = getattr(obj, cfg["method"])
        for trade in trades:
            leg_method(trade.strike, trade.price * trade.index_price)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)
        fig, ax = obj.plot(index_price,
                           market_deltas,
                           market_gammas,
                           False,
                           title=cfg["label"],
                           width=18,
                           height=8)

        self._annotate_gamma_delta_crossings(ax, STs, market_deltas, market_gammas)

        # Delta-saturation marker: the price where this leg's delta curve
        # reaches 90% of its own extreme value in this window and flattens
        # into a straight line (deep enough ITM to "trade like synthetic
        # stock") — same options.delta_saturation_price() dankbit.zones.
        # extrema's own delta_band uses, so the two can never disagree on
        # this point. Relative to the curve's own extreme, not an absolute
        # delta value, since portfolio_delta's scale depends on how much
        # volume traded (can be in the hundreds), not a fixed [-1, 1] range.
        saturation_price = options.delta_saturation_price(
            STs, trades, cfg["saturation_fraction"], cfg["saturation_side"]
        )
        ax.axvline(x=saturation_price, color="green", linewidth=1.5, linestyle="-", alpha=0.9)
        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        ax.text(saturation_price, 0.96, f"${saturation_price:,.0f}", transform=trans, color="green",
                fontsize=9, ha="right", va="top", rotation=90)

        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        last_ts = last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M') if last_trade else "—"
        ax.text(
            0.01, 0.04,
            f"{len(trades)} Trades (since 00:00 UTC)",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.text(
            0.01, 0.01,
            f"Last trade: {last_ts}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": leg_key.upper(),
                "plot_title": f"{instrument} - {cfg['label']}",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
            }
        )

    @http.route("/<string:instrument>/lp", type="http", auth="user", website=True)
    def chart_png_long_puts(self, instrument):
        return self._chart_png_single_leg(instrument, "lp")

    @http.route("/<string:instrument>/lc", type="http", auth="user", website=True)
    def chart_png_long_calls(self, instrument):
        return self._chart_png_single_leg(instrument, "lc")

    @http.route("/<string:instrument>/sp", type="http", auth="user", website=True)
    def chart_png_short_puts(self, instrument):
        return self._chart_png_single_leg(instrument, "sp")

    @http.route("/<string:instrument>/sc", type="http", auth="user", website=True)
    def chart_png_short_calls(self, instrument):
        return self._chart_png_single_leg(instrument, "sc")

    @http.route("/<string:instrument>", type="http", auth="user", website=True)
    def chart_png_all(self, instrument):
        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        cr = request.env.cr
        cr.execute("""
            SELECT
                strike,
                option_type,
                direction,
                expiration,
                SUM(amount)                                AS total_amount,
                SUM(iv * amount) / NULLIF(SUM(amount), 0) AS weighted_iv,
                COUNT(*)                                   AS trade_count
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= NOW()
              AND active = TRUE
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{instrument}%',))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0],
                option_type=row[1],
                direction=row[2],
                expiration=row[3],
                amount=float(row[4]),
                iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, agg_trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, agg_trades, 0.05)
        fig, ax = obj.plot(index_price,
                           market_deltas,
                           market_gammas,
                           False,
                           title="Structure",
                           width=18,
                           height=8)

        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        d_arr = np.asarray(market_deltas, dtype=float)
        g_arr = np.asarray(market_gammas, dtype=float)
        d_lim = float(np.max(np.abs(d_arr[np.isfinite(d_arr)]))) if np.any(np.isfinite(d_arr)) else 1.0
        g_lim = float(np.max(np.abs(g_arr[np.isfinite(g_arr)]))) if np.any(np.isfinite(g_arr)) else 1.0

        for px, gval in self.find_gamma_peaks(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            # normalised positions of gamma and delta at this x (0=bottom, 1=top of axes)
            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            # pick the y fraction furthest from both curves
            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for px, gval in self.find_gamma_bottoms(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            # normalised positions of gamma and delta at this x (0=bottom, 1=top of axes)
            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            # pick the y fraction furthest from both curves
            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                demand = d_arr[i] > 0
                color = "red" if demand else "green"
                ax.axvline(x=px, color=color, linewidth=1.2, linestyle="-", alpha=0.8)
                g_norm = 0.5 + 0.5 * (float(np.interp(px, STs, g_arr)) / g_lim) if g_lim else 0.5
                y = 0.04 if g_norm > 0.5 else 0.96
                ax.text(px, y, f"${px:,.0f}", transform=trans, color=color,
                        fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                        rotation=90)

        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        last_ts = last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M') if last_trade else "—"
        ax.text(
            0.01, 0.04,
            f"{trade_count} Trades",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.text(
            0.01, 0.01,
            f"Last trade: {last_ts}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": "All",
                "plot_title": f"{instrument} - All",
                "refresh_interval": refresh_interval*5,
                "image_b64": image_b64,
            }
        )

    @http.route("/<string:asset>/weekly", type="http", auth="user", website=True)
    def chart_png_weekly(self, asset):
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()
        icp = request.env["ir.config_parameter"].sudo()
        param = "dankbit.eth_weekly_expiry" if asset.startswith("ETH") else "dankbit.weekly_expiry"
        instrument = icp.get_param(param, default="").upper()
        if not instrument:
            return request.make_response(
                f"Weekly Expiry for {asset} is not configured. Set it in Settings → Dankbit.",
                headers=[("Content-Type", "text/plain")],
            )
        return self.chart_png_until(instrument)

    @http.route("/<string:asset>/monthly", type="http", auth="user", website=True)
    def chart_png_monthly(self, asset):
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()
        icp = request.env["ir.config_parameter"].sudo()
        param = "dankbit.eth_monthly_expiry" if asset.startswith("ETH") else "dankbit.monthly_expiry"
        instrument = icp.get_param(param, default="").upper()
        if not instrument:
            return request.make_response(
                f"Monthly Expiry for {asset} is not configured. Set it in Settings → Dankbit.",
                headers=[("Content-Type", "text/plain")],
            )
        return self.chart_png_until(instrument)

    @http.route("/i/<string:instrument>", type="http", auth="user", website=True)
    def chart_png_until(self, instrument):
        # instrument is e.g. "BTC-3JUL26" — asset prefix + expiry, no strike/type
        parts = instrument.split("-", 1)
        if len(parts) != 2:
            return request.not_found()
        asset = parts[0].upper()
        expiry_str = parts[1].upper()

        try:
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(
                hour=8, tzinfo=timezone.utc
            )
        except ValueError:
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if asset.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if asset.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        cr = request.env.cr
        cr.execute("""
            SELECT
                strike,
                option_type,
                direction,
                expiration,
                SUM(amount)                                AS total_amount,
                SUM(iv * amount) / NULLIF(SUM(amount), 0) AS weighted_iv,
                COUNT(*)                                   AS trade_count
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= NOW()
              AND expiration <= %s
              AND active = TRUE
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{asset}%', expiry_dt))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0],
                option_type=row[1],
                direction=row[2],
                expiration=row[3],
                amount=float(row[4]),
                iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        index_price = request.env["dankbit.trade"].get_index_price(asset)
        obj = options.OptionStrat(asset, index_price, from_price, to_price, steps)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, agg_trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, agg_trades, 0.05)
        fig, ax = obj.plot(index_price,
                           market_deltas,
                           market_gammas,
                           False,
                           title=f"Until {expiry_str}",
                           width=18,
                           height=8)

        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        d_arr = np.asarray(market_deltas, dtype=float)
        g_arr = np.asarray(market_gammas, dtype=float)
        d_lim = float(np.max(np.abs(d_arr[np.isfinite(d_arr)]))) if np.any(np.isfinite(d_arr)) else 1.0
        g_lim = float(np.max(np.abs(g_arr[np.isfinite(g_arr)]))) if np.any(np.isfinite(g_arr)) else 1.0

        for px, gval in self.find_gamma_peaks(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for px, gval in self.find_gamma_bottoms(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)


        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                demand = d_arr[i] > 0
                color = "red" if demand else "green"
                ax.axvline(x=px, color=color, linewidth=1.2, linestyle="-", alpha=0.8)
                g_norm = 0.5 + 0.5 * (float(np.interp(px, STs, g_arr)) / g_lim) if g_lim else 0.5
                y = 0.04 if g_norm > 0.5 else 0.96
                ax.text(px, y, f"${px:,.0f}", transform=trans, color=color,
                        fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                        rotation=90)

        last_trade = request.env["dankbit.trade"].get_last_trade(asset)
        last_ts = last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M') if last_trade else "—"
        ax.text(
            0.01, 0.04,
            f"{trade_count} Trades (until {expiry_str})",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.text(
            0.01, 0.01,
            f"Last trade: {last_ts}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": f"Until {expiry_str}",
                "plot_title": f"{asset} - Until {expiry_str}",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
            }
        )
    
    def find_gamma_peaks(self, STs, gamma_curve, min_fraction=0.15):
        STs = np.asarray(STs, dtype=float)
        g = np.asarray(gamma_curve, dtype=float)

        if g.size < 3:
            return []

        finite = np.isfinite(g)
        if not np.any(finite):
            return []

        g_max = np.max(np.abs(g[finite]))
        if g_max == 0:
            return []

        threshold = min_fraction * g_max
        extrema = []

        for i in range(1, len(g) - 1):
            if not np.isfinite(g[i]):
                continue
            if g[i] > g[i - 1] and g[i] > g[i + 1] and g[i] > threshold:
                extrema.append((float(STs[i]), float(g[i])))

        return extrema

    def find_gamma_bottoms(self, STs, gamma_curve, min_fraction=0.15):
        STs = np.asarray(STs, dtype=float)
        g = np.asarray(gamma_curve, dtype=float)

        if g.size < 3:
            return []

        finite = np.isfinite(g)
        if not np.any(finite):
            return []

        g_max = np.max(np.abs(g[finite]))
        if g_max == 0:
            return []

        threshold = min_fraction * g_max
        extrema = []

        for i in range(1, len(g) - 1):
            if not np.isfinite(g[i]):
                continue
            if g[i] < g[i - 1] and g[i] < g[i + 1] and g[i] < -threshold:
                extrema.append((float(STs[i]), float(g[i])))

        return extrema

    # ------------------------------------------------------------------
    # JSON API endpoints
    # ------------------------------------------------------------------

    @staticmethod
    def _gamma_horizon_is_last_friday(day):
        """True for the last Friday of ``day``'s calendar month."""
        return day.weekday() == 4 and (day + timedelta(days=7)).month != day.month

    @staticmethod
    def _gamma_horizon_peak(STs, trades):
        """Return the strongest absolute Gamma price and its strength.

        Buyer and seller portfolios are evaluated independently by the caller;
        taking the absolute curve here therefore works for both signs without
        changing any of the existing Gamma/Forecast engines.
        """
        if not trades:
            return None, 0.0
        curve = np.asarray(gamma.portfolio_gamma(STs, trades, 0.05), dtype=float)
        finite = np.isfinite(curve)
        if not finite.any():
            return None, 0.0
        magnitude = np.where(finite, np.abs(curve), -np.inf)
        idx = int(np.argmax(magnitude))
        strength = float(magnitude[idx])
        if not np.isfinite(strength) or strength <= 0:
            return None, 0.0
        return float(STs[idx]), strength

    @http.route("/api/gamma-horizon/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def gamma_horizon_json(self, asset):
        """Display-only term Gamma levels for tomorrow/week/month/year-end.

        This endpoint deliberately has no persistence and is not imported by
        Forecast, Bands, Smart Liquidity, Greeks Flow, FOMO, Anchors or Signal
        Bot.  Each Deribit trade is already unique in ``dankbit_trade`` and is
        counted once.  Its amount receives a 40% permanent component plus a
        60% exponential freshness component; the half-life varies by horizon.
        """
        asset = asset.upper()
        if asset not in ("BTC", "ETH"):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        tomorrow = (now + timedelta(days=1)).date()
        cr = request.env.cr
        cr.execute("""
            SELECT DISTINCT expiration
              FROM dankbit_trade
             WHERE name ILIKE %s
               AND expiration > %s
               AND active = TRUE
               AND iv <> 0
             ORDER BY expiration
        """, (f"{asset}-%", now))
        expirations = [row[0] for row in cr.fetchall() if row[0]]

        daily_targets = [exp for exp in expirations if exp.date() == tomorrow][:1]
        weekly_targets = [
            exp for exp in expirations
            if exp.date().weekday() == 4
            and not self._gamma_horizon_is_last_friday(exp.date())
        ][:3]
        monthly_targets = [
            exp for exp in expirations
            if self._gamma_horizon_is_last_friday(exp.date())
            and not (exp.date().month == 12)
        ][:3]
        year_end_targets = [
            exp for exp in expirations
            if self._gamma_horizon_is_last_friday(exp.date())
            and exp.date().month == 12
        ][:1]

        specs = []
        specs += [("D1", exp, 24.0, 72.0) for exp in daily_targets]
        specs += [("W", exp, 6.0 * 24.0, None) for exp in weekly_targets]
        specs += [("M", exp, 15.0 * 24.0, None) for exp in monthly_targets]
        specs += [("YE", exp, 75.0 * 24.0, None) for exp in year_end_targets]
        selected = sorted(set(exp for _, exp, _, _ in specs))

        if not selected:
            payload = {"asset": asset, "levels": [], "trade_count": 0,
                       "generated_at": datetime.now(timezone.utc).isoformat()}
            return request.make_response(
                json.dumps(payload),
                headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
            )

        # Hourly aggregation preserves time-decay accuracy while keeping the
        # endpoint bounded even for a heavily traded year-end expiry.
        cr.execute("""
            SELECT expiration, strike, option_type, direction,
                   date_trunc('hour', deribit_ts) AS trade_hour,
                   SUM(amount),
                   SUM(iv * amount) / NULLIF(SUM(amount), 0),
                   COUNT(*)
              FROM dankbit_trade
             WHERE name ILIKE %s
               AND expiration = ANY(%s)
               AND active = TRUE
               AND iv <> 0
               AND deribit_ts IS NOT NULL
               AND deribit_ts <= %s
             GROUP BY expiration, strike, option_type, direction, trade_hour
             ORDER BY expiration, trade_hour
        """, (f"{asset}-%", selected, now))
        rows = cr.fetchall()

        icp = request.env["ir.config_parameter"].sudo()
        if asset == "BTC":
            base_from = float(icp.get_param("dankbit.from_price", default=100000))
            base_to = float(icp.get_param("dankbit.to_price", default=150000))
            base_step = max(50.0, float(icp.get_param("dankbit.steps", default=100)))
        else:
            base_from = float(icp.get_param("dankbit.eth_from_price", default=2000))
            base_to = float(icp.get_param("dankbit.eth_to_price", default=5000))
            base_step = max(5.0, float(icp.get_param("dankbit.eth_steps", default=50)))
        index_price = float(request.env["dankbit.trade"].get_index_price(asset) or 0.0)

        levels = []
        total_trade_count = 0
        for kind, expiry, half_life_hours, max_age_hours in specs:
            relevant = [row for row in rows if row[0] == expiry]
            if max_age_hours is not None:
                relevant = [
                    row for row in relevant
                    if max(0.0, (now - row[4]).total_seconds() / 3600.0) <= max_age_hours
                ]
            if not relevant:
                continue

            # Combine hourly rows into Gamma inputs after applying freshness.
            grouped = {}
            first_seen = None
            trade_count = 0
            for row in relevant:
                _, strike, option_type, direction, trade_hour, amount, avg_iv, count = row
                age_hours = max(0.0, (now - trade_hour).total_seconds() / 3600.0)
                freshness = 0.40 + 0.60 * math.pow(2.0, -age_hours / half_life_hours)
                weighted_amount = float(amount or 0.0) * freshness
                if weighted_amount <= 0:
                    continue
                key = (int(strike), option_type, direction)
                bucket = grouped.setdefault(key, {"amount": 0.0, "iv_amount": 0.0})
                bucket["amount"] += weighted_amount
                bucket["iv_amount"] += float(avg_iv or 0.0) * weighted_amount
                trade_count += int(count or 0)
                first_seen = trade_hour if first_seen is None else min(first_seen, trade_hour)

            if not grouped:
                continue
            strikes = [key[0] for key in grouped]
            low_anchor = min(strikes + ([index_price] if index_price > 0 else []))
            high_anchor = max(strikes + ([index_price] if index_price > 0 else []))
            pad = max(base_step * 5.0, (high_anchor - low_anchor) * 0.08)
            grid_from = min(base_from, low_anchor - pad)
            grid_to = max(base_to, high_anchor + pad)
            step = max(base_step, (grid_to - grid_from) / 1800.0)
            STs = np.arange(grid_from, grid_to + step, step)

            buyer_trades, seller_trades = [], []
            for (strike, option_type, direction), bucket in grouped.items():
                amount = bucket["amount"]
                trd = _AggTrade(
                    strike=strike, option_type=option_type, direction=direction,
                    expiration=expiry, amount=amount,
                    iv=bucket["iv_amount"] / amount if amount else 0.0,
                )
                (buyer_trades if direction == "buy" else seller_trades).append(trd)

            buyer_price, buyer_strength = self._gamma_horizon_peak(STs, buyer_trades)
            seller_price, seller_strength = self._gamma_horizon_peak(STs, seller_trades)
            prices = [p for p in (buyer_price, seller_price) if p is not None]
            if not prices:
                continue
            # Same semantic as Thales's existing mean Gamma: midpoint of the
            # strongest buyer/seller Gamma prices, never injected back into it.
            mean_price = float(sum(prices) / len(prices))
            total_trade_count += trade_count
            levels.append({
                "kind": kind,
                "expiry": expiry.replace(tzinfo=timezone.utc).isoformat(),
                "price": mean_price,
                "buyer_price": buyer_price,
                "seller_price": seller_price,
                "buyer_strength": buyer_strength,
                "seller_strength": seller_strength,
                "trade_count": trade_count,
                "first_seen": first_seen.replace(tzinfo=timezone.utc).isoformat() if first_seen else None,
                "half_life_hours": half_life_hours,
            })

        payload = {
            "asset": asset,
            "levels": levels,
            "trade_count": total_trade_count,
            "index_price": index_price,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "display_only": True,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/delta-zero/<string:instrument>", type="http", auth="user", website=False, csrf=False)
    def delta_zero_json(self, instrument):
        parts = instrument.upper().split("-", 1)
        if len(parts) != 2:
            return request.make_response(
                json.dumps({"error": "Invalid instrument — expected ASSET-EXPIRY e.g. BTC-3JUL26"}),
                headers=[("Content-Type", "application/json")],
            )

        asset, expiry_str = parts
        try:
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
        except ValueError:
            return request.make_response(
                json.dumps({"error": "Invalid expiry format — expected DDMMMYY e.g. 3JUL26"}),
                headers=[("Content-Type", "application/json")],
            )

        icp = request.env["ir.config_parameter"].sudo()
        if asset.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        elif asset.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))
        else:
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        cr = request.env.cr
        cr.execute("""
            SELECT strike, option_type, direction, expiration,
                   SUM(amount), SUM(iv * amount) / NULLIF(SUM(amount), 0), COUNT(*)
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= NOW()
              AND expiration <= %s
              AND active = TRUE
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{asset}%', expiry_dt))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0], option_type=row[1], direction=row[2],
                expiration=row[3], amount=float(row[4]), iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        STs = np.arange(from_price, to_price, steps)
        d_arr = np.asarray(delta.portfolio_delta(STs, agg_trades, 0.05), dtype=float)

        crossings = []
        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                crossings.append({
                    "price": px,
                    "type": "demand" if d_arr[i] > 0 else "supply",
                })

        index_price = request.env["dankbit.trade"].get_index_price(asset)
        payload = {
            "asset": asset,
            "expiry": expiry_str,
            "delta_zero": crossings,
            "index_price": index_price,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/delta-zero-all/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def delta_zero_all_json(self, asset):
        asset = asset.upper()
        icp = request.env["ir.config_parameter"].sudo()
        if asset.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        elif asset.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))
        else:
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        cr = request.env.cr
        cr.execute("""
            SELECT strike, option_type, direction, expiration,
                   SUM(amount), SUM(iv * amount) / NULLIF(SUM(amount), 0), COUNT(*)
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= NOW()
              AND active = TRUE
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{asset}%',))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0], option_type=row[1], direction=row[2],
                expiration=row[3], amount=float(row[4]), iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        STs = np.arange(from_price, to_price, steps)
        d_arr = np.asarray(delta.portfolio_delta(STs, agg_trades, 0.05), dtype=float)

        crossings = []
        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                crossings.append({
                    "price": px,
                    "type": "demand" if d_arr[i] > 0 else "supply",
                })

        index_price = request.env["dankbit.trade"].get_index_price(asset)
        payload = {
            "asset": asset,
            "delta_zero": crossings,
            "index_price": index_price,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    def _delta_zero_for_calendar_day(self, asset, days_ahead):
        """Delta=0 crossings for the specific expiry landing `days_ahead`
        calendar days from now (UTC), restricted to trades from the trailing
        24h. Shared by /api/delta-zero-tomorrow (days_ahead=1) and
        /api/delta-zero-day-after-tomorrow (days_ahead=2) so the two can
        never disagree on how a calendar-day expiry/trade-window is
        computed."""
        asset = asset.upper()
        icp = request.env["ir.config_parameter"].sudo()
        if asset.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        elif asset.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))
        else:
            return {"error": "Unknown asset"}

        target_day = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).date()
        expiry_str = f"{target_day.day}{target_day.strftime('%b').upper()}{target_day.strftime('%y')}"

        cr = request.env.cr
        cr.execute("""
            SELECT strike, option_type, direction, expiration,
                   SUM(amount), SUM(iv * amount) / NULLIF(SUM(amount), 0), COUNT(*)
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND active = TRUE
              AND deribit_ts >= NOW() - INTERVAL '24 hours'
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'{asset}-{expiry_str}-%',))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0], option_type=row[1], direction=row[2],
                expiration=row[3], amount=float(row[4]), iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        STs = np.arange(from_price, to_price, steps)
        d_arr = np.asarray(delta.portfolio_delta(STs, agg_trades, 0.05), dtype=float)

        crossings = []
        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                crossings.append({
                    "price": px,
                    "type": "demand" if d_arr[i] > 0 else "supply",
                })

        index_price = request.env["dankbit.trade"].get_index_price(asset)
        return {
            "asset": asset,
            "expiry": expiry_str,
            "delta_zero": crossings,
            "index_price": index_price,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    @http.route("/api/delta-zero-tomorrow/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def delta_zero_tomorrow_json(self, asset):
        payload = self._delta_zero_for_calendar_day(asset, 1)
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/delta-zero-day-after-tomorrow/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def delta_zero_day_after_tomorrow_json(self, asset):
        payload = self._delta_zero_for_calendar_day(asset, 2)
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/bands/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def bands_json(self, asset):
        """One point per instrument stored in dankbit.bands (each
        instrument has exactly one, continuously-refined-then-frozen row —
        see that model's _persist_extrema()), positioned on the chart at that
        instrument's own expiration time rather than a stored poll
        timestamp (there isn't one anymore). The expiration lookup is a
        single grouped query against dankbit_trade rather than parsing each
        instrument's day-string suffix and assuming a settlement hour —
        consistent with how the zones-box endpoints derive their own
        right-edge time. Raw SQL bypasses the ORM's implicit active=True
        filter, so past-expiry (and thus archived) instruments' trades are
        still found — their rows must keep contributing a fixed historical
        point on the chart even after they're no longer "active"."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        hours_param = request.httprequest.args.get("hours")
        try:
            hours = int(hours_param) if hours_param else None
        except (TypeError, ValueError):
            hours = None
        if hours is not None and not 1 <= hours <= 168:
            hours = None

        cr = request.env.cr
        cr.execute("""
            SELECT instrument, index_price, high_resistance, low_support,
                   high_resistance_positive, low_support_positive, gamma_band, delta_band,
                   smart_liq_upper_price, smart_liq_lower_price,
                   smart_liq_upper_strength, smart_liq_lower_strength
            FROM dankbit_bands
            WHERE asset = %s
        """, (asset,))
        rows = cr.fetchall()

        expiry_by_instrument = {}
        if rows:
            instruments = [row[0] for row in rows]
            cr.execute("""
                SELECT SUBSTRING(name FROM '^[^-]+-[^-]+') AS instrument, MIN(expiration) AS expiration
                FROM dankbit_trade
                WHERE SUBSTRING(name FROM '^[^-]+-[^-]+') = ANY(%s)
                GROUP BY instrument
            """, (instruments,))
            expiry_by_instrument = dict(cr.fetchall())

        series = []
        for (
            instrument, index_price, high_resistance, low_support,
            high_resistance_positive, low_support_positive, gamma_band, delta_band,
            smart_liq_upper_price, smart_liq_lower_price,
            smart_liq_upper_strength, smart_liq_lower_strength,
        ) in rows:
            expiration = expiry_by_instrument.get(instrument)
            if not expiration:
                # No trades found at all for this instrument any more —
                # nothing to anchor the point's time to.
                continue
            ts = expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)
            series.append({
                "instrument": instrument,
                "t": int(ts.timestamp() * 1000),
                "confirmation_status": "confirmed",
                "expiry_index": None,
                "index_price": float(index_price or 0.0),
                "high_resistance": float(high_resistance or 0.0),
                "low_support": float(low_support or 0.0),
                # Whether the payoff at that intersection sits above/below
                # the zero line — drives the +/- marker on the chart, not
                # the point's own price/position.
                "high_resistance_positive": bool(high_resistance_positive),
                "low_support_positive": bool(low_support_positive),
                "gamma_band": float(gamma_band or 0.0),
                "delta_band": float(delta_band or 0.0),
                "smart_liq_upper_price": float(smart_liq_upper_price or 0.0),
                "smart_liq_lower_price": float(smart_liq_lower_price or 0.0),
                "smart_liq_upper_strength": float(smart_liq_upper_strength or 0.0),
                "smart_liq_lower_strength": float(smart_liq_lower_strength or 0.0),
            })

        # Keep all three active expiry anchors visible.  A newly rolled E2/E3
        # can exist in option trades before its structural Bands row passes
        # the mature-session confirmation gate.  Previously that missing row
        # vanished from the response, making the Green/Red paths stop one or
        # two days early.  Serve a non-persisted raw calculation for only the
        # missing active instruments and label it provisional.  The chart
        # renders these additions dashed; confirmed history is never replaced.
        bands_model = request.env["dankbit.bands"]
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        expirations = bands_model._distinct_expirations(
            asset, now_naive, bands_model.TRACKED_EXPIRY_COUNT,
            future_days_only=True,
        )
        active_instruments = {
            bands_model._format_instrument(asset, expiration): (expiry_index, expiration)
            for expiry_index, expiration in enumerate(expirations)
        }
        by_instrument = {row["instrument"]: row for row in series}
        provisional_rejections = []
        current_index_price = float(request.env["dankbit.trade"].get_index_price(asset) or 0.0)
        valid_history = [
            row for row in sorted(series, key=lambda item: item["t"])
            if float(row.get("high_resistance") or 0.0) > float(row.get("low_support") or 0.0) > 0.0
        ]
        last_valid_band = valid_history[-1] if valid_history else None
        for instrument, (expiry_index, expiration) in active_instruments.items():
            confirmed = by_instrument.get(instrument)
            if confirmed is not None:
                confirmed["expiry_index"] = expiry_index
                continue
            live = bands_model._compute_asset(
                asset, expiry_index=expiry_index, future_days_only=True,
            )
            band_ok, band_reason = bands_model._band_quality_gate(live) if live else (False, "no-live-option-data")
            raw_high = float((live or {}).get("high_resistance") or 0.0)
            raw_low = float((live or {}).get("low_support") or 0.0)
            index_price = float((live or {}).get("index_price") or current_index_price)

            # Quality Gate classifies; it never removes a chart component.
            # A collapsed/missing point inherits the last valid structural
            # width around its own raw centre (or current index), and remains
            # dashed with an explicit warning. Raw values stay in the API.
            display_high, display_low = raw_high, raw_low
            if not band_ok and (raw_high <= raw_low or raw_low <= 0.0):
                if last_valid_band:
                    fallback_width = max(
                        float(last_valid_band["high_resistance"]) - float(last_valid_band["low_support"]),
                        1.0,
                    )
                    fallback_center = (
                        (raw_high + raw_low) / 2.0 if raw_high > 0.0 and raw_low > 0.0
                        else index_price or (float(last_valid_band["high_resistance"]) + float(last_valid_band["low_support"])) / 2.0
                    )
                    display_high = fallback_center + fallback_width / 2.0
                    display_low = fallback_center - fallback_width / 2.0
                elif index_price > 0.0:
                    fallback_width = index_price * 0.015
                    display_high = index_price + fallback_width / 2.0
                    display_low = index_price - fallback_width / 2.0
            if display_high <= display_low or display_low <= 0.0:
                # No defensible absolute value exists yet. Keep the expiry in
                # diagnostics; historical lines and every other chart layer
                # still render independently.
                provisional_rejections.append({
                    "instrument": instrument, "expiry_index": expiry_index,
                    "reason": band_reason,
                })
                continue
            exp_ts = expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)
            provisional = {
                "instrument": instrument,
                "t": int(exp_ts.timestamp() * 1000),
                "confirmation_status": "provisional",
                "expiry_index": expiry_index,
                "index_price": index_price,
                "high_resistance": display_high,
                "low_support": display_low,
                "raw_high_resistance": raw_high,
                "raw_low_support": raw_low,
                "quality_status": "valid" if band_ok else "low",
                "quality_reason": None if band_ok else band_reason,
                "high_resistance_positive": bool((live or {}).get("high_resistance_positive")),
                "low_support_positive": bool((live or {}).get("low_support_positive")),
                "gamma_band": float((live or {}).get("gamma_band") or (last_valid_band or {}).get("gamma_band") or 0.0),
                "delta_band": float((live or {}).get("delta_band") or 0.0),
                "smart_liq_upper_price": float((live or {}).get("smart_liq_upper_price") or 0.0),
                "smart_liq_lower_price": float((live or {}).get("smart_liq_lower_price") or 0.0),
                "smart_liq_upper_strength": float((live or {}).get("smart_liq_upper_strength") or 0.0),
                "smart_liq_lower_strength": float((live or {}).get("smart_liq_lower_strength") or 0.0),
            }
            if not band_ok:
                provisional_rejections.append({
                    "instrument": instrument, "expiry_index": expiry_index,
                    "reason": band_reason,
                })
            series.append(provisional)
            by_instrument[instrument] = provisional
            if band_ok:
                last_valid_band = provisional

        # In trailing-window preview mode the structural Green/Red Bands
        # remain the persisted session-confirmed values, while Gamma and
        # Smart Liquidity are replaced by a fresh computation using only
        # the selected option-flow window.  Nothing is persisted here.
        if hours is not None:
            for expiry_index in range(request.env["dankbit.bands"].TRACKED_EXPIRY_COUNT):
                live = request.env["dankbit.bands"]._compute_asset(
                    asset, expiry_index=expiry_index, hours=hours,
                    future_days_only=True,
                )
                if not live:
                    continue
                expiration = live["expiration"]
                expiration = expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)
                row = by_instrument.get(live["instrument"])
                if row is None:
                    row = {
                        "instrument": live["instrument"],
                        "t": int(expiration.timestamp() * 1000),
                        "confirmation_status": "provisional",
                        "expiry_index": expiry_index,
                        "index_price": float(live["index_price"] or 0.0),
                        "high_resistance": 0.0, "low_support": 0.0,
                        "high_resistance_positive": False, "low_support_positive": False,
                        "delta_band": 0.0,
                    }
                    series.append(row)
                    by_instrument[live["instrument"]] = row
                row.update({
                    "gamma_band": float(live["gamma_band"] or 0.0),
                    "smart_liq_upper_price": float(live["smart_liq_upper_price"] or 0.0),
                    "smart_liq_lower_price": float(live["smart_liq_lower_price"] or 0.0),
                    "smart_liq_upper_strength": float(live["smart_liq_upper_strength"] or 0.0),
                    "smart_liq_lower_strength": float(live["smart_liq_lower_strength"] or 0.0),
                })
        series.sort(key=lambda r: r["t"])

        payload = {
            "asset": asset,
            "bands": series,
            "provisional_rejections": provisional_rejections,
            "window_hours": hours,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/zones-box/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def zones_box_json(self, asset):
        """Nearest-expiry zones for the chart.

        The default since-00:00 path serves the last session-confirmed (or
        two-hour emergency-confirmed) High/Low/Middle zones.  `?hours=` is an
        explicit non-persisted live analytical preview of a trailing window.
        Forecast snapshots use their own hourly raw computation and are not
        constrained by this display endpoint.
        """
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        hours_param = request.httprequest.args.get("hours")
        if hours_param == "auto":
            # "Auto" in the Delta Chart's Option Flow dropdown — size the
            # trailing trade window off the nearest expiry's own time to
            # settlement, the same shrink-as-expiry-nears sizing
            # /4l/<asset>'s own "Auto" window uses (see _auto_window_hours).
            # Resolved server-side so the client doesn't need the expiry.
            bands_model = request.env["dankbit.bands"]
            as_of = datetime.now(timezone.utc).replace(tzinfo=None)
            expirations = bands_model._distinct_expirations(asset, as_of, 1)
            dte_hours = (
                (expirations[0] - as_of).total_seconds() / 3600.0
                if expirations else None
            )
            hours = _auto_window_hours(dte_hours)
        else:
            try:
                hours = int(hours_param) if hours_param else None
            except (TypeError, ValueError):
                hours = None
            # Prevent malformed/manual requests from creating an unbounded trade
            # query.  The chart currently sends 2, 4, or the configured window.
            if hours is not None and not 1 <= hours <= 168:
                hours = None

        data = request.env["dankbit.bands"].get_box(asset, hours=hours)
        if not data:
            payload = {"asset": asset, "box": None}
        else:
            computed_at = data["computed_at"].replace(tzinfo=timezone.utc)
            expiration = data["expiration"].replace(tzinfo=timezone.utc)
            payload = {
                "asset": asset,
                "t": int(computed_at.timestamp() * 1000),
                "expiration": int(expiration.timestamp() * 1000),
                "index_price": float(data["index_price"]),
                "short_zero_above_price": float(data["short_zero_above_price"]),
                "long_zero_above_price": float(data["long_zero_above_price"]),
                "short_zero_below_price": float(data["short_zero_below_price"]),
                "long_zero_below_price": float(data["long_zero_below_price"]),
                "seller_max_profit": float(data["seller_max_profit"]),
                "buyer_max_loss": float(data["buyer_max_loss"]),
                "zone_confirmation_mode": data.get("zone_confirmation_mode", "unknown"),
            }
        # Resolved trailing window in hours (None on the default 00:00-UTC
        # path) — echoed so the Delta Chart footer can show what "Auto (Time
        # to Expiry)" actually picked, same as /4l/<asset>'s own window_hours.
        payload["window_hours"] = hours
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/nearest-expiry/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def nearest_expiry_json(self, asset):
        """The single nearest active expiry for `asset`, as a full
        instrument string (e.g. "BTC-9JUL26") — the same expiry the yellow
        zones boxes use, but a cheap standalone lookup (no curve-building)
        so the TradingView footer can show it without waiting on the
        boxes' own full computation."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )
        expiry = request.env["dankbit.bands"].nearest_expiry(asset)
        payload = {
            "asset": asset,
            "expiry": expiry,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/last-trade/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def last_trade_json(self, asset):
        """The most recent trade's own deribit_ts for `asset`, asset-wide
        (dankbit.trade.get_last_trade()'s plain `ilike` match, not
        restricted to any one instrument/expiry) — a cheap standalone
        lookup, no curve-building, feeding the TradingView footer's
        "Last trade:" text so a stalled dankbit_ws ingestion service (its
        DB connection can die without crashing the container, silently
        dropping every trade — see dankbit_ws_batch.py) shows up as a
        growing gap between this timestamp and now, at a glance, without
        needing to check the container logs or query the DB directly.
        `last_trade_ts` is `None` if this asset has no trades at all."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )
        last_trade = request.env["dankbit.trade"].get_last_trade(asset)
        last_trade_ts = (
            last_trade.deribit_ts.replace(tzinfo=timezone.utc).isoformat()
            if last_trade and last_trade.deribit_ts else None
        )
        active_trade_count = request.env["dankbit.trade"].search_count([
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", ">", datetime.now(timezone.utc).replace(tzinfo=None)),
            ("active", "=", True),
        ])
        payload = {
            "asset": asset,
            "last_trade_ts": last_trade_ts,
            "active_trade_count": active_trade_count,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/live-band/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def live_band_json(self, asset):
        """Every persisted dankbit.live.band row for `asset`, oldest-first
        — feeds the Delta Chart's per-hour Smart Liquidity dominance dots
        (see dankbit_templates.xml's "Bands" checkbox). Read-only, like
        every other endpoint reading a cron-fed snapshot model: it never
        calls compute_and_create() itself — only dankbit.live.band's own
        hourly cron ever creates a row."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        cr = request.env.cr
        cr.execute("""
            SELECT computed_at, expiry_index, instrument, expiration,
                   high_resistance, low_support,
                   gamma_band, smart_liq_upper, smart_liq_lower,
                   smart_liq_upper_strength, smart_liq_lower_strength,
                   smart_liq_upper_avg, smart_liq_lower_avg
            FROM dankbit_live_band
            WHERE asset = %s
            ORDER BY computed_at ASC, expiry_index ASC
        """, (asset,))
        points = []
        for (
            computed_at, expiry_index, instrument, expiration,
            high_resistance, low_support,
            gamma_band, smart_liq_upper, smart_liq_lower,
            smart_liq_upper_strength, smart_liq_lower_strength,
            smart_liq_upper_avg, smart_liq_lower_avg,
        ) in cr.fetchall():
            ts = computed_at if computed_at.tzinfo else computed_at.replace(tzinfo=timezone.utc)
            points.append({
                "t": int(ts.timestamp() * 1000),
                "expiry_index": int(expiry_index or 0),
                "instrument": instrument,
                "expiration": int((expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)).timestamp() * 1000) if expiration else None,
                "high_resistance": float(high_resistance or 0.0),
                "low_support": float(low_support or 0.0),
                "gamma_band": float(gamma_band or 0.0),
                "smart_liq_upper": float(smart_liq_upper or 0.0),
                "smart_liq_lower": float(smart_liq_lower or 0.0),
                "smart_liq_upper_strength": float(smart_liq_upper_strength or 0.0),
                "smart_liq_lower_strength": float(smart_liq_lower_strength or 0.0),
                "smart_liq_upper_avg": float(smart_liq_upper_avg or 0.0),
                "smart_liq_lower_avg": float(smart_liq_lower_avg or 0.0),
            })

        payload = {
            "asset": asset,
            "points": points,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/klines/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def klines_proxy(self, asset, interval="4h", limit="500"):
        candles = request.env["dankbit.trade"].get_candles(asset, interval=interval, limit=int(limit))
        candles = candles[::-1]  # newest-first for frontend
        return request.make_response(
            json.dumps({"result": candles}),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/klines-coinbase/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def klines_coinbase_proxy(self, asset, interval="4h", limit="500"):
        """Coinbase-spot equivalent of klines_proxy above — sourced from
        dankbit.trade.get_candles_coinbase() instead of get_candles()
        (Binance spot). Used by /4l/<asset>'s own candle
        series, per product decision to move that page off Deribit
        perpetual futures onto Coinbase spot (was
        /api/klines-futures/<asset> when it sourced Deribit
        perpetuals instead).

        An optional `?as_of=` query param (see _parse_as_of_param) anchors
        the returned window's right edge to that past moment instead of
        "now" — the `limit` candles ending at/before it, forwarded as
        get_candles_coinbase()'s own `as_of_ts` (unix seconds). Used by
        /tm/<asset> (Time Machine) for its own candle series; every other
        caller (no `as_of`) is unaffected."""
        as_of_override = _parse_as_of_param(request.httprequest.args.get("as_of"))
        as_of_ts = as_of_override.replace(tzinfo=timezone.utc).timestamp() if as_of_override is not None else None
        candles = request.env["dankbit.trade"].get_candles_coinbase(
            asset, interval=interval, limit=int(limit), as_of_ts=as_of_ts,
        )
        candles = candles[::-1]  # newest-first for frontend
        return request.make_response(
            json.dumps({"result": candles}),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/expiries/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def expiries_json(self, asset):
        """Every non-expired (active) instrument for `asset`, soonest-first
        — dankbit.bands._distinct_expirations()/_format_instrument(), the
        same helpers the ordinal Nearest/Nearest+1/Nearest+2 family below
        and /api/nearest-expiry/<asset> already use, just returning the
        full active set instead of one ordinal slot. Backs /4l/<asset>'s
        own "Expiry" dropdown (four_leg_gamma_chart_templates.xml), which
        used to offer a fixed Nearest/Weekly/Monthly option set — replaced
        per product decision with this dynamically-loaded list of every
        actually-active expiry, so a real expiry can be picked directly
        instead of only through the weekly_expiry/monthly_expiry settings
        indirection. `limit=200` is generous headroom over the dozen or so
        expiries actually active at once in practice. This route only
        lists instruments — `/api/four-leg-gamma/<asset>`'s own
        `?expiry=nearest`/`weekly`/`monthly`/etc. resolutions are
        unchanged and still reachable by a direct API call, just no
        longer offered from this page's dropdown.

        An optional `?as_of=` query param (see _parse_as_of_param) switches
        this from "every currently-active expiry" to "every expiry that
        had already traded and hadn't yet expired as of that past moment"
        — via dankbit.bands._distinct_expirations_asof() instead of the
        plain _distinct_expirations() the live (no as_of) path still uses.
        Backs /tm/<asset>'s (Time Machine) own "Expiry" dropdown,
        reloaded every time the user picks a different As-Of date — the
        set of instruments active as of that moment moves with it. The
        dropdown was briefly removed when /tm/<asset> was rebuilt around
        the since-removed /l24/<asset> page's own always-cumulative
        design, then restored when /tm/<asset> was re-synced back onto
        /4l/<asset> (see time_machine_chart)."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )
        bands_model = request.env["dankbit.bands"]
        as_of_override = _parse_as_of_param(request.httprequest.args.get("as_of"))
        if as_of_override is not None:
            expirations = bands_model._distinct_expirations_asof(asset, as_of_override, 200)
        else:
            as_of = datetime.now(timezone.utc).replace(tzinfo=None)
            expirations = bands_model._distinct_expirations(asset, as_of, 200)
        expiries = [bands_model._format_instrument(asset, exp) for exp in expirations]
        payload = {"asset": asset, "expiries": expiries}
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    def _resolve_four_leg_trades(self, asset):
        """Shared trade-domain resolution for the 4-leg-gamma family of
        routes — `?expiry=` / `?instrument=` / `?cumulative=` /
        `?from_instrument=`+`?to_instrument=` / `?hours=` / `?as_of=` —
        exactly as four_leg_gamma_json documents them. Returns a dict:
        {trades (recordset), instrument (str|None), hours (int|"all"|
        "midnight"), STs (np.ndarray), from_price, to_price, step, as_of,
        from_instrument (raw param|None), to_instrument (raw param|None),
        ft_range (bool)}. `asset` must already be upper-cased/validated.

        Extracted from four_leg_gamma_json's own body so the resolution
        stays in one place (it was briefly also shared with a since-removed
        /api/four-leg-forecast/<asset> route)."""
        icp = request.env["ir.config_parameter"].sudo()
        if asset == "BTC":
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            step = float(icp.get_param("dankbit.steps", default=100))
        else:
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            step = float(icp.get_param("dankbit.eth_steps", default=50))

        expiry_ordinals = {"nearest": 0, "nearest_plus_1": 1, "nearest_plus_2": 2}
        cumulative_modes = ("weekly", "monthly", "all")
        expiry_mode = (request.httprequest.args.get("expiry") or "").lower()
        if expiry_mode not in expiry_ordinals and expiry_mode not in cumulative_modes:
            expiry_mode = "nearest"

        as_of_override = _parse_as_of_param(request.httprequest.args.get("as_of"))
        as_of = as_of_override if as_of_override is not None else datetime.now(timezone.utc).replace(tzinfo=None)
        hours_param = request.httprequest.args.get("hours")

        # Resolve which single instrument (if any) this Expiry mode
        # isolates to — and, only when "?hours=auto" actually needs it
        # below, that instrument's own settlement time — before touching
        # `hours`/`window_start`, since Auto's own window size depends on
        # this resolution. "all" isolates to no single instrument at all,
        # so target_expiration stays None there (Auto falls back to
        # FOUR_LEG_DEFAULT_WINDOW_HOURS, same as a missing/malformed
        # ?hours= value — see _auto_window_hours).
        instrument = None
        target_expiration = None
        bands_model = request.env["dankbit.bands"]
        # `?instrument=` — sent by /4l/<asset>'s own "Expiry" dropdown
        # (each option is a real instrument string, not a mode), and by
        # /tm/<asset>'s own "Expiry" dropdown paired with `?as_of=` (see
        # time_machine_chart) — bypassing the mode resolution below
        # entirely — ISOLATED to that one instrument's own trades, same
        # domain the ordinal Nearest/Nearest+1/Nearest+2 family below uses.
        instrument_override = (request.httprequest.args.get("instrument") or "").upper() or None
        cumulative_param = (request.httprequest.args.get("cumulative") or "").lower() in ("1", "true")
        # `?from_instrument=`/`?to_instrument=` — used to back the
        # since-removed /ft/<asset> ("From/To") page's own Expiry
        # dropdowns; the param pair is unchanged and still reachable by a
        # direct API call (each a real instrument string, same as
        # `?instrument=` above, not a mode). Takes priority over both
        # `?instrument=` and `?expiry=` entirely: trades are CUMULATIVE
        # across every one of `asset`'s own instruments whose own
        # `expiration` falls between the two resolved expirations,
        # inclusive — order-independent (swapped if given backwards), so
        # picking "From" after "To" in the UI still works. Each side is
        # resolved to its own expiration the same way `?instrument=`'s own
        # `?hours=auto`/`?cumulative=` lookup already does — a cheap
        # single-row trade search, since every instrument this dropdown can
        # offer (sourced from /api/expiries/<asset>, i.e.
        # dankbit.bands._distinct_expirations()) has at least one trade by
        # construction. Falls through to the ordinary `?instrument=`/
        # `?expiry=` resolution below (empty trades if neither of those is
        # set either) if either side's own trade lookup comes up empty —
        # same nothing-computable-yet convention every other route in this
        # file follows, rather than fabricating a one-sided range.
        from_instrument_param = (request.httprequest.args.get("from_instrument") or "").upper() or None
        to_instrument_param = (request.httprequest.args.get("to_instrument") or "").upper() or None
        ft_range = bool(from_instrument_param and to_instrument_param)
        from_expiration = None
        to_expiration = None
        if ft_range:
            ft_trades = request.env["dankbit.trade"].with_context(active_test=False)
            from_trade = ft_trades.search([("name", "=ilike", f"{from_instrument_param}-%")], limit=1)
            to_trade = ft_trades.search([("name", "=ilike", f"{to_instrument_param}-%")], limit=1)
            if from_trade and to_trade:
                from_expiration, to_expiration = from_trade.expiration, to_trade.expiration
                if from_expiration > to_expiration:
                    from_expiration, to_expiration = to_expiration, from_expiration
                # Feeds `?hours=auto` below (_auto_window_hours) — the
                # range's own farthest-out (later) edge is the natural
                # "time to expiry" reference for a cumulative range, same
                # role the single resolved instrument's own expiration
                # plays for every other `?hours=auto` path in this method.
                target_expiration = to_expiration
            else:
                ft_range = False

        if not ft_range:
            if instrument_override:
                instrument = instrument_override
                if hours_param == "auto" or cumulative_param:
                    exp_trade = request.env["dankbit.trade"].with_context(active_test=False).search(
                        [("name", "=ilike", f"{instrument}-%")], limit=1,
                    )
                    if exp_trade:
                        target_expiration = exp_trade.expiration
            elif expiry_mode in expiry_ordinals:
                expiry_index = expiry_ordinals[expiry_mode]
                expirations = bands_model._distinct_expirations(asset, as_of, expiry_index + 1)
                if len(expirations) > expiry_index:
                    target_expiration = expirations[expiry_index]
                    instrument = bands_model._format_instrument(asset, target_expiration)
            elif expiry_mode in ("weekly", "monthly"):
                # weekly/monthly — ISOLATED to that one configured instrument's
                # own trades, same anchored `=ilike` domain (and active_test
                # bypass, in case that instrument has since expired and been
                # archived) the ordinal Nearest/Nearest+1/Nearest+2 family
                # above uses. Previously CUMULATIVE through every active
                # instrument up to that expiry's own date (the "all" branch
                # below still is); changed per product decision so Weekly/
                # Monthly line up with the ordinal family's single-instrument
                # isolation instead.
                if asset == "ETH":
                    expiry_param = "dankbit.eth_weekly_expiry" if expiry_mode == "weekly" else "dankbit.eth_monthly_expiry"
                else:
                    expiry_param = "dankbit.weekly_expiry" if expiry_mode == "weekly" else "dankbit.monthly_expiry"
                instrument = icp.get_param(expiry_param, default="").upper() or None
                if instrument and hours_param == "auto":
                    # A cheap single-row lookup, only run when Auto actually
                    # needs a time-to-expiry — the weekly/monthly domain
                    # itself (below) never needed this instrument's own
                    # expiration datetime before Auto existed.
                    exp_trade = request.env["dankbit.trade"].with_context(active_test=False).search(
                        [("name", "=ilike", f"{instrument}-%")], limit=1,
                    )
                    if exp_trade:
                        target_expiration = exp_trade.expiration

        # "all" (?hours=all) skips the trailing-hours trade filter entirely;
        # "midnight" (?hours=midnight) restricts to trades since the most
        # recent UTC midnight (options.day_window_start) instead of a
        # trailing-hours window; "auto" (?hours=auto) sizes the window off
        # how close the resolved expiry above actually is (see
        # _auto_window_hours) — all three checked before the int() parse
        # below so none is mistaken for a malformed value and overwritten
        # with the default.
        if hours_param in ("all", "midnight"):
            hours = hours_param
        elif hours_param == "auto":
            dte_hours = (
                (target_expiration - as_of).total_seconds() / 3600.0
                if target_expiration is not None else None
            )
            hours = _auto_window_hours(dte_hours)
        else:
            try:
                hours = int(hours_param)
            except (TypeError, ValueError):
                hours = None
            if hours not in FOUR_LEG_WINDOW_HOURS_CHOICES:
                hours = FOUR_LEG_DEFAULT_WINDOW_HOURS

        if hours == "all":
            window_start = None
        elif hours == "midnight":
            window_start = options.day_window_start(as_of)
        else:
            window_start = as_of - timedelta(hours=hours)

        trades = request.env["dankbit.trade"]
        if ft_range:
            # From/To range — cumulative across every instrument whose own
            # expiration falls between the two resolved expirations,
            # inclusive. Same domain shape as the "all" cumulative branch
            # below, just bounded on both ends instead of only a lower one.
            domain = [
                ("name", "=ilike", f"{asset}-%"),
                ("expiration", ">=", from_expiration), ("expiration", "<=", to_expiration),
                ("iv", "!=", 0), ("deribit_ts", "<=", as_of),
            ]
            if window_start is not None:
                domain.append(("deribit_ts", ">=", window_start))
            trades = trades.with_context(active_test=False).search(domain)
        elif instrument_override:
            if cumulative_param and instrument_override and target_expiration is not None:
                # Cumulative from as_of through and including the selected
                # expiry — every instrument in that expiration range, not
                # just the one the Expiry dropdown picked. See this
                # method's own docstring above.
                domain = [
                    ("name", "=ilike", f"{asset}-%"),
                    ("expiration", ">=", as_of), ("expiration", "<=", target_expiration),
                    ("iv", "!=", 0), ("deribit_ts", "<=", as_of),
                ]
                if window_start is not None:
                    domain.append(("deribit_ts", ">=", window_start))
                trades = trades.with_context(active_test=False).search(domain)
            elif instrument:
                domain = [("name", "=ilike", f"{instrument}-%"), ("iv", "!=", 0), ("deribit_ts", "<=", as_of)]
                if window_start is not None:
                    domain.append(("deribit_ts", ">=", window_start))
                trades = trades.with_context(active_test=False).search(domain)
        elif expiry_mode in expiry_ordinals:
            if instrument:
                domain = [("name", "=ilike", f"{instrument}-%"), ("iv", "!=", 0), ("deribit_ts", "<=", as_of)]
                if window_start is not None:
                    domain.append(("deribit_ts", ">=", window_start))
                trades = trades.with_context(active_test=False).search(domain)
        elif expiry_mode == "all":
            # "all" — cumulative across every one of asset's own non-expired
            # (as of `as_of`) instruments (same domain the since-removed
            # /mwa/<asset>'s own mwa_gamma_json used). Unlike weekly/
            # monthly below, there's no single instrument to isolate to.
            # `deribit_ts <= as_of` is always applied (not just when a
            # trailing-hours Window is selected) — a no-op for the live
            # case (nothing has deribit_ts in the future of "now"), but
            # required for /tm/<asset> (Time Machine)'s historical `as_of`:
            # without it, Window=All would include every trade for a
            # tracked instrument regardless of whether it happened before
            # or after the selected past moment.
            domain = [
                ("name", "=ilike", f"{asset}-%"), ("expiration", ">=", as_of),
                ("iv", "!=", 0), ("deribit_ts", "<=", as_of),
            ]
            if window_start is not None:
                domain.append(("deribit_ts", ">=", window_start))
            trades = request.env["dankbit.trade"].with_context(active_test=False).search(domain)
        else:
            # weekly/monthly — instrument already resolved above.
            if instrument:
                domain = [("name", "=ilike", f"{instrument}-%"), ("iv", "!=", 0), ("deribit_ts", "<=", as_of)]
                if window_start is not None:
                    domain.append(("deribit_ts", ">=", window_start))
                trades = trades.with_context(active_test=False).search(domain)

        STs = np.arange(from_price, to_price, step, dtype=np.float64)

        return {
            "trades": trades, "instrument": instrument, "hours": hours, "STs": STs,
            "from_price": from_price, "to_price": to_price, "step": step, "as_of": as_of,
            "from_instrument": from_instrument_param if ft_range else None,
            "to_instrument": to_instrument_param if ft_range else None,
            "ft_range": ft_range,
        }

    @http.route("/api/four-leg-gamma/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def four_leg_gamma_json(self, asset):
        """Computed fresh on every request — no model/table behind this
        route: 4-leg gamma extrema (BCG/BPG/SCG/SPG) over `asset`'s own
        trailing trades, restricted to an expiry chosen via the page's
        own "Expiry" dropdown — an optional `?expiry=` query param, one
        of two families:

        - An explicit `?instrument=` query param (e.g. "BTC-25JUL26")
          takes priority over `?expiry=` entirely and ISOLATES trades to
          that one instrument directly, same anchored `=ilike` domain as
          the ordinal family below — sent by `/4l/<asset>`'s own
          "Expiry" dropdown (each option resolves to a real instrument
          picked from `/api/expiries/<asset>` rather than a mode string).
          `/tm/<asset>` (Time Machine) sends this too, from its own
          "Expiry" dropdown (populated from `/api/expiries/<asset>?as_of=`),
          paired with its own `?as_of=` param so the isolation targets
          that historical moment (see time_machine_chart).

        - "nearest" (default — the currently soonest-expiring active
          instrument), "nearest_plus_1", or "nearest_plus_2" (the
          1st/2nd active expiry after the nearest one), mapped to an
          ordinal index (0-2) and resolved via
          dankbit.bands._distinct_expirations()/_format_instrument()
          directly rather than dankbit.bands._compute_asset(), since
          this route only needs the instrument string, not a full curve
          build. Trades are ISOLATED to that one resolved instrument's
          own trades via the same anchored `=ilike` domain
          chart_png_zones uses (`f"{instrument}-%"`, left-prefix match
          so one expiry's query can never pull in another's trades).

        - "weekly" or "monthly" — resolved against the configured
          weekly_expiry/monthly_expiry instrument for `asset`
          (eth_-prefixed for ETH, same convention
          _build_tv_chart_context() uses). Trades are ISOLATED to that
          one configured instrument's own trades, same anchored `=ilike`
          domain (and active_test bypass, in case that instrument has
          since expired and been archived) as the ordinal family above —
          previously CUMULATIVE through every active instrument whose
          own expiration was <= that expiry's day-suffix (the same
          expiration-column cutoff gamma_by_strike_until_json/
          _gamma_by_strike still use for their own Weekly/Monthly
          scopes); changed per product decision so Weekly/Monthly line
          up with the ordinal family's single-instrument isolation
          instead of pulling in every other active expiry's trades too.

        - "all" — skips the weekly/monthly instrument lookup entirely
          and considers every one of `asset`'s own non-expired
          instruments (`expiration >= now`, no upper bound — same
          no-expiry-cutoff domain gamma_by_strike_json's own "All" scope
          uses), trades CUMULATIVE across all of them — unlike Weekly/
          Monthly above, there's no single instrument to isolate to.
          This is the same domain the since-removed /mwa/<asset> page's
          own mwa_gamma_json route used, ported onto this route's own
          "Expiry" dropdown alongside its original ordinal options
          rather than as a separate page, once /mwa/<asset> was folded
          into this one and removed. The since-removed /l24/<asset> page
          sent this unconditionally (it had no "Expiry" dropdown — its 4
          gamma legs were always cumulative across every active expiry);
          /tm/<asset> (Time Machine) also drove this branch during its
          own /l24-based phase, paired with its own `?as_of=` param —
          this "all" branch's own `expiration >= as_of` condition (via
          the shared `as_of` variable every branch in this route reads
          instead of calling `datetime.now()` inline) and its
          `active_test=False` bypass were built to serve that historical
          case; /tm/<asset> has since been re-synced onto /4l/<asset> and
          now sends `?instrument=` (optionally `?cumulative=1`) +
          `?as_of=` instead, but those paths carry the same `as_of`/
          `active_test=False` treatment.

        Any other/missing `?expiry=` value falls back to "nearest". The
        trailing-hours trade window is independently user-selectable via
        the page's own "Window" dropdown — an optional `?hours=` query
        param, restricted to FOUR_LEG_WINDOW_HOURS_CHOICES (every
        integer 1..72 plus 96/120/144/168/192/216/240/480/720; each
        page's own "Window" dropdown offers a different subset of these
        — see the constant's own comment above — or the literal string
        "all", skipping the trailing-hours trade filter entirely, or the
        literal string "midnight", restricting to trades since the most
        recent UTC midnight (options.day_window_start) instead of a
        trailing-hours count — same day-boundary convention chart_png_zones/
        the /lp,lc,sp,sc single-leg routes/dankbit.bands._compute_asset's
        default window already use, or the literal string "auto",
        resolved via _auto_window_hours() to the smallest
        FOUR_LEG_WINDOW_HOURS_CHOICES bucket that still covers however
        many hours remain until the resolved Expiry's own settlement —
        shrinking the lookback as expiry nears, on the reasoning that
        stale/rolled flow matters less the closer price gets to
        settlement (see _auto_window_hours' own docstring for the
        rationale and why there's no direct "professional practice" to
        point to here); falls back to FOUR_LEG_DEFAULT_WINDOW_HOURS when
        Expiry=All (no single instrument to compute a time-to-expiry
        against) — any other/missing `?hours=` value falls back to
        FOUR_LEG_DEFAULT_WINDOW_HOURS=24)
        — applies to both expiry families the same way. The resolved
        numeric window is echoed back as the response's own
        `window_hours` field (always an int, even when `?hours=auto`
        resolved it, so the client can display what Auto actually
        picked). Computed via
        options.per_leg_gamma() — a
        gamma-only slice of options.per_leg_greeks() (the single source
        of truth for the full gamma/delta/theta/vega set, also used by
        dankbit.bands/dankbit.forecast.snapshot/chart_png_zones) that
        skips the theta/vega/delta curves this route never reads (see
        per_leg_gamma()'s own docstring in options.py). Feeds
        /4l/<asset>'s own 4 horizontal gamma-price lines — the page's
        own "refreshFourLegGamma()" timer poll is skipped client-side
        while a cumulative "weekly"/"monthly"/"all" mode is selected,
        same reasoning /mwa/<asset> never polled this class of
        computation on a timer at all (options.per_leg_gamma() loops
        per trade in plain Python, so cost scales directly with trade
        count — potentially every trade for every active instrument at
        once). No points at all (same nothing-computable-yet convention
        every other route in this addon follows) when nothing is active
        at that ordinal position, or (for the cumulative family) when
        the selected expiry isn't configured for `asset`, is malformed,
        or has no matching trades in the resolved window. `points`
        holds exactly one (current) reading, kept as a list for
        shape-compatibility with the page's own existing
        points[points.length-1] read.

        An optional `?cumulative=1` (or "true") query param — only
        meaningful alongside `?instrument=` — switches that branch from
        ISOLATING to the given instrument's own trades to a CUMULATIVE
        domain across every one of `asset`'s own instruments whose own
        `expiration` falls between `as_of` (or its `?as_of=` override)
        and the given instrument's own expiration, inclusive — "from
        as_of through and including this cutoff". The trailing-hours
        Window (`?hours=`) still
        applies on top, same as the isolated path. /4l/<asset> and
        /tm/<asset> (Time Machine) each used to drive this via their own
        "Cumulative" checkbox (removed per product decision); the param
        itself is unchanged and still reachable by a direct API call.
        Requires a lookup of
        the given instrument's own expiration (the same single-row
        lookup `?hours=auto` already performs for `?instrument=`, now
        also run whenever `?cumulative=` is set) — falls back to the
        ordinary isolated behavior if that instrument has no trades at
        all (nothing to resolve a cutoff expiration from).

        A separate `?from_instrument=`/`?to_instrument=` query param pair —
        both required together, and taking priority over `?instrument=`/
        `?expiry=` entirely — switches to a CUMULATIVE domain spanning
        every one of `asset`'s own instruments whose own `expiration` falls
        between the two resolved expirations, inclusive (order-independent
        — swapped if given backwards). Each side is resolved to its own
        expiration via the same single-row trade lookup `?instrument=`'s
        own `?hours=auto`/`?cumulative=` handling already performs; falling
        back to the ordinary `?instrument=`/`?expiry=` resolution (or empty
        trades if neither is set) if either side's lookup finds no trades
        at all. Used to back the since-removed /ft/<asset> ("From/To")
        page's own "From"/"To" Expiry dropdowns; the param pair is
        unchanged and still reachable by a direct API call.
        The trailing-hours Window (`?hours=`) still applies on top, same as
        every other branch — including `?hours=auto`: the range's own
        farther-out (later, post-swap) edge is used as the `_auto_window_hours()`
        time-to-expiry reference, the same role a single resolved
        instrument's own expiration plays for every other `?hours=auto`
        path in this method. Response echoes the resolved
        `from_instrument`/`to_instrument` strings (`None` on any other
        path), alongside the existing `instrument` field (`None` here too,
        since this is a range rather than a single instrument, same as the
        cumulative "all" branch below).

        An optional `?as_of=` query param (see _parse_as_of_param)
        substitutes for "now" everywhere in this function — every
        `?expiry=`/`?instrument=`/`?hours=`
        resolution above is already written relative to a local `as_of`
        variable rather than calling datetime.now() inline, so overriding
        just that one assignment retargets the whole computation at a
        past moment with no other changes needed. Used by /tm/<asset>
        (Time Machine) to compute the 4 gamma legs as they stood at a
        user-picked historical date; every trade domain search below
        already runs with `active_test=False` for exactly this reason —
        so archived (long-since-expired) trades from that period are
        still found — except the cumulative "all" branch, which now also
        bypasses active_test unconditionally (harmless for the live/no
        as_of case too, since that branch's own `expiration >= as_of`
        condition already excludes anything old enough to have been
        archived when as_of is "now").
        """
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        resolved = self._resolve_four_leg_trades(asset)
        trades = resolved["trades"]
        STs = resolved["STs"]
        instrument = resolved["instrument"]
        hours = resolved["hours"]
        as_of = resolved["as_of"]
        ft_range = resolved["ft_range"]
        from_instrument_param = resolved["from_instrument"]
        to_instrument_param = resolved["to_instrument"]

        points = []
        if trades:
            # per_leg_gamma() rather than per_leg_greeks() — only
            # gamma_price/gamma_value are read below, so the theta/
            # vega/delta curves per_leg_greeks() would also compute
            # are pure waste here (see per_leg_gamma()'s docstring in
            # options.py).
            legs = options.per_leg_gamma(STs, trades)
            # A leg with zero trades reports gamma_price as None (no
            # curve to peak/bottom at) — collapsed to 0.0 here, same
            # "0.0 = absent" sentinel the client's own
            # `if (latest.bcg_price)` falsy checks already expect
            # (dankbit_four_leg_gamma_chart_templates.xml).
            point = {
                "t": int(as_of.replace(tzinfo=timezone.utc).timestamp() * 1000),
                "trade_count": len(trades),
                "bcg_price": legs["long_call"]["gamma_price"] or 0.0, "bcg_value": legs["long_call"]["gamma_value"],
                "bpg_price": legs["long_put"]["gamma_price"] or 0.0, "bpg_value": legs["long_put"]["gamma_value"],
                "scg_price": legs["short_call"]["gamma_price"] or 0.0, "scg_value": legs["short_call"]["gamma_value"],
                "spg_price": legs["short_put"]["gamma_price"] or 0.0, "spg_value": legs["short_put"]["gamma_value"],
            }
            points.append(point)

        # `hours` here is either "all"/"midnight" (as requested) or a
        # plain int — for a numeric Window selection that int is just an
        # echo of the request, but for "?hours=auto" it's the resolved
        # bucket _auto_window_hours() actually picked, which the client
        # has no other way to know (it only sent the literal "auto").
        payload = {
            "asset": asset, "instrument": instrument, "window_hours": hours, "points": points,
            "from_instrument": from_instrument_param if ft_range else None,
            "to_instrument": to_instrument_param if ft_range else None,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/ll-gamma/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def ll_avg_gamma_json(self, asset, **kw):
        """Computed fresh on every request — no model/table behind this
        route. Backs the /ll/<asset> ("LL") chart: for each of the soonest
        LL_MAX_EXPIRIES active expiries of `asset`, isolate that
        instrument's own trades (anchored `=ilike`, `iv <> 0`,
        `deribit_ts <= now`, optionally within a trailing `?hours=` window)
        and run options.per_leg_gamma() — the same gamma-only slice of
        options.per_leg_greeks() four_leg_gamma_json uses — to get that
        expiry's 4 leg gamma peak/bottom prices+values (BCG/BPG/SCG/SPG).
        Each expiry is then reduced to ONE reading: the present-leg average
        of those 4 prices (`avg_price`) and of those 4 values
        (`avg_value`) — "present" meaning a leg with at least one trade
        (options.per_leg_gamma() reports an absent leg's price as None),
        so a missing leg is excluded from the mean rather than dragging it
        toward 0, same convention /4l/<asset>'s own client-side AVG line
        uses.

        Response `expiries` is sorted by `abs_value` (|avg_value|)
        descending, so the client draws the top 2 — "only show 2 lines
        with the biggest absolute gamma value". No "Expiry" dropdown on
        the LL page: every active expiry is always considered, unlike
        four_leg_gamma_json which isolates to one instrument at a time.
        The trailing-hours Window (`?hours=`) is the LL page's only
        trade-domain control — same FOUR_LEG_WINDOW_HOURS_CHOICES /
        "all" / "midnight" resolution four_leg_gamma_json uses, falling
        back to FOUR_LEG_DEFAULT_WINDOW_HOURS for a missing/malformed
        value. `?hours=auto` is NOT supported here (no single expiry to
        size a time-to-expiry window against).

        Returns {asset, window_hours (the resolved int, or the string
        "all"/"midnight"), trade_count (summed across every returned
        expiry), expiries: [{instrument, expiration (epoch ms), avg_price,
        avg_value, abs_value, dominant_leg ("LC"/"LP"/"SC"/"SP" — the
        present leg with the largest |gamma value|), trade_count}, ...]}.
        `expiries` is empty (same nothing-computable-yet convention every
        other route in this addon follows) when nothing is active / no
        expiry has any usable trades in the window.
        """
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        icp = request.env["ir.config_parameter"].sudo()
        if asset == "BTC":
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            step = float(icp.get_param("dankbit.steps", default=100))
        else:
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            step = float(icp.get_param("dankbit.eth_steps", default=50))
        STs = np.arange(from_price, to_price, step, dtype=np.float64)

        hours_param = request.httprequest.args.get("hours")
        if hours_param in ("all", "midnight"):
            hours = hours_param
        else:
            try:
                hours = int(hours_param)
            except (TypeError, ValueError):
                hours = None
            if hours not in FOUR_LEG_WINDOW_HOURS_CHOICES:
                hours = FOUR_LEG_DEFAULT_WINDOW_HOURS

        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        if hours == "all":
            window_start = None
        elif hours == "midnight":
            window_start = options.day_window_start(as_of)
        else:
            window_start = as_of - timedelta(hours=hours)

        _LEG_CODES = {"long_call": "LC", "long_put": "LP", "short_call": "SC", "short_put": "SP"}

        bands_model = request.env["dankbit.bands"]
        expirations = bands_model._distinct_expirations(asset, as_of, LL_MAX_EXPIRIES)

        trade_model = request.env["dankbit.trade"].with_context(active_test=False)
        expiries = []
        for exp in expirations:
            instrument = bands_model._format_instrument(asset, exp)
            domain = [
                ("name", "=ilike", f"{instrument}-%"),
                ("iv", "!=", 0), ("deribit_ts", "<=", as_of),
            ]
            if window_start is not None:
                domain.append(("deribit_ts", ">=", window_start))
            trades = trade_model.search(domain)
            if not trades:
                continue

            legs = options.per_leg_gamma(STs, trades)
            present = [
                (_LEG_CODES[name], leg["gamma_price"], leg["gamma_value"])
                for name, leg in legs.items()
                if leg["gamma_price"] is not None
            ]
            if not present:
                continue

            avg_price = sum(p for _, p, _ in present) / len(present)
            avg_value = sum(v for _, _, v in present) / len(present)
            dominant_code = max(present, key=lambda t: abs(t[2]))[0]
            expiries.append({
                "instrument": instrument,
                "expiration": int(exp.replace(tzinfo=timezone.utc).timestamp() * 1000),
                "avg_price": avg_price,
                "avg_value": avg_value,
                "abs_value": abs(avg_value),
                "dominant_leg": dominant_code,
                "trade_count": len(trades),
            })

        expiries.sort(key=lambda e: e["abs_value"], reverse=True)

        payload = {
            "asset": asset,
            "window_hours": hours,
            "trade_count": sum(e["trade_count"] for e in expiries),
            "expiries": expiries,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/4l/<string:asset>", type="http", auth="user", website=True)
    def four_leg_gamma_chart(self, asset):
        """Standalone TradingView page — 4 horizontal price lines (BCG/
        BPG/SCG/SPG — where each of the 4 trade legs' own portfolio
        dollar-gamma curve, over trailing trades restricted to one
        expiry, peaks or bottoms), recomputed live on every poll via
        /api/four-leg-gamma (no model/table behind this page), alongside
        real Coinbase spot candles (dankbit.trade.get_candles_coinbase(),
        same /api/klines-coinbase/<asset> proxy /tm/<asset> uses —
        Coinbase has no native 4h resolution, so "4h" is built
        server-side by fetching native 60-minute bars and bucketing
        every 4 into one; 15m/1h/1d map directly onto Coinbase's own
        900/3600/86400-second granularities). This page originally
        sourced Deribit perpetual-futures candles at a fixed 1h
        timeframe, then moved to Binance spot candles since Deribit's
        chart API has no native 4h resolution, then moved to Kraken
        Futures to keep this page on perpetuals (not spot) while still
        getting a native 4h/1d resolution, then moved back to Deribit
        perpetuals once the missing native 4h bucket was built
        server-side instead of switching exchanges for it — moved to
        Coinbase spot per a later product decision (also incidentally
        fixing the daily-timeframe "no candle for today" discrepancy
        against the Delta Chart, since Deribit's own daily bars were
        08:00-UTC-anchored rather than UTC-midnight-anchored like
        Coinbase's/Binance's).
        Own "Timeframe" dropdown (15m/1h/4h/1d, 1d default — 15m was
        added since Coinbase natively
        supports it too, and was originally also this page's own default
        until it was changed to 4h per product decision (matching the
        Delta/Gamma/Strike Gamma charts' own default), then to 1d per a
        further product decision (2026-08-19)),
        own "Expiry" dropdown — Nearest/Nearest+1 on the dropdown itself
        (Nearest default, same nearest+1 ordinal notion /gt/<asset>'s own
        2nd price line used before that page was removed; trades
        ISOLATED to that one resolved instrument; Nearest+2 stays a
        valid `?expiry=nearest_plus_2` value four_leg_gamma_json accepts
        but has no dropdown entry of its own), PLUS
        Weekly/Monthly/All (the configured weekly_expiry/monthly_expiry
        instrument for `asset`; "All" considers every one of the
        asset's own non-expired instruments, trades CUMULATIVE across
        all of them — Weekly/Monthly instead have trades ISOLATED to
        that one configured instrument, same as the ordinal family,
        changed per product decision from an earlier CUMULATIVE-through-
        that-expiry behavior) — see four_leg_gamma_json for the full
        history/resolution of both option families, including how
        Weekly/Monthly/All were originally this route's own options,
        removed per product decision in favor of Nearest+1/Nearest+2,
        then re-added alongside them once the standalone /mwa/<asset>
        page (which had carried that Weekly/Monthly/All + cumulative
        behavior in the interim) was folded back into this one and
        removed. Own "Window"
        dropdown (1h/2h/3h/4h/5h/6h/7h/8h/24h/All, 4h default;
        /tm/<asset> mirrors it), independent of the "Expiry" dropdown —
        a subset of FOUR_LEG_WINDOW_HOURS_CHOICES; this set and its
        default have moved many times across product decisions (see the
        FOUR_LEG_WINDOW_HOURS_CHOICES comment in this file). A "00:00 UTC"
        (?hours=midnight — trades since the most recent UTC midnight,
        options.day_window_start) and an "Auto (Time to Expiry)"
        (?hours=auto — shrinks the window as the selected Expiry's own
        settlement nears, see _auto_window_hours) option were both offered
        here at various points; the server-side resolutions for both are
        unchanged and still reachable by a direct API call, just no longer
        in the dropdown. A vertical marker line showing where the selected
        Window's trailing-hours cutoff falls is drawn on the candle chart
        (#window-vline).
        Renders its own standalone template
        (dankbit_four_leg_gamma_chart). Polls the 4 gamma-price lines on
        the general dankbit.refresh_interval, same as every other
        page's own refresh rate — EXCEPT while a cumulative
        Weekly/Monthly/All "Expiry" is selected, where that poll is
        skipped client-side (same reasoning the since-removed
        /mwa/<asset> page never polled this class of computation on a
        timer at all — see four_leg_gamma_json's own docstring); the
        candle series' own 5s auto-refresh is unaffected either way. A
        7th line, teal large-dashed "AVG WEEKLY"
        (refreshAvgGammaWeekly() in the template's own script), is
        drawn only while the
        "Expiry" dropdown is on "Nearest" — the configured
        weekly_expiry/eth_weekly_expiry instrument's own 4-leg gamma
        average, computed over its ENTIRE trade history
        (?expiry=weekly&hours=all on the same /api/four-leg-gamma
        endpoint the "Weekly" Expiry dropdown option itself resolves
        against), independent of the page's own Window selection, as
        a second forward-looking reference alongside Nearest+1's."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        ctx = {"asset": asset, "refresh_interval": refresh_interval}
        return request.render("dankbit.dankbit_four_leg_gamma_chart", ctx)

    @http.route("/ll/<string:asset>", type="http", auth="user", website=True)
    def ll_avg_gamma_chart(self, asset):
        """Standalone TradingView page — structurally a stripped-down
        /4l/<asset> (four_leg_gamma_chart): same Coinbase-spot candle
        source, same Timeframe/Window/Theme/Window Line/Ruler/Settings/
        Refresh controls, same single vertical Window reference line.
        Differences: NO "Expiry" dropdown, and NO "Gamma Legs"/"AVG"/
        "N+1 AVG" checkboxes. Instead of 4 per-leg gamma lines for one
        chosen expiry, it draws at most 2 horizontal lines — the 4-leg
        gamma AVG (present-leg average of BCG/BPG/SCG/SPG) for whichever 2
        active expiries currently have the biggest |AVG gamma value|,
        recomputed live on every poll (dankbit.refresh_interval) via
        /api/ll-gamma/<asset> (no model/table behind this page). Renders
        its own standalone template (dankbit_ll_avg_gamma_chart). 404 for
        an unrecognized asset."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        ctx = {"asset": asset, "refresh_interval": refresh_interval}
        return request.render("dankbit.dankbit_ll_avg_gamma_chart", ctx)

    @http.route("/tm/<string:asset>", type="http", auth="user", website=True)
    def time_machine_chart(self, asset):
        """"Time Machine" — a standalone TradingView page, structurally a
        historical-replay sibling of /4l/<asset> (four_leg_gamma_chart):
        same Coinbase-spot candle source, same 4-gamma-leg (BCG/BPG/SCG/
        SPG) + dashed AVG line rendering (dominant-leg-derived AVG color/
        label, SC/SP labels with no combined magnitude suffix), same
        Timeframe/Expiry/Window/Theme/Window Line/Ruler/
        Settings controls, same per-instrument-ISOLATED
        4-leg computation, and the same single vertical Window reference
        line + label. Like /4l it carries an "Expiry" dropdown
        (dynamically loaded from /api/expiries/<asset>). (Both pages had a
        "Cumulative" checkbox — ?cumulative=1 — removed per product
        decision; the route mode is unchanged, still reachable by a direct
        API call.) Its "Window" dropdown mirrors /4l's own
        (1h/2h/3h/4h/5h/6h/7h/8h/24h/All, 4h default) — this page was
        rebuilt around the since-removed /l24/<asset> page's feature set
        for a while (an always-cumulative-across-every-expiry design with
        no Expiry dropdown), then moved back onto /4l and re-synced to its
        current state per a later request. NOT carried over from /4l: the "Refresh
        Lines" button and the "Last trade:" footer — a frozen As-Of view
        has nothing to force a re-fetch of (changing As Of/Timeframe/
        Expiry/Window is already how a fresh read happens), and "Last
        trade:" is a live data-staleness check with no meaning for a
        historical moment. Own Timeframe defaults to 1h (was 4h — this
        page's own original default — changed per product decision so it
        agreed with the then-current /l24/<asset> page's own 1h; kept at
        1h through the move back onto /4l, whose own default is 4h).

        Everything on this page is anchored to a user-picked past "As Of"
        date instead of "now", using this addon's ~9 months of retained
        trade history (including long-expired, archived instruments) to
        let a user scroll back and study how the 4-leg gamma structure
        lined up against price for a past moment. The "As Of" picker (and
        this page's own URL `?as_of=`) is EUROPE/BERLIN wall-clock time,
        DST-aware — per product decision, since this Thales tool's users
        think in Berlin time; the template converts it to UTC client-side
        (berlinNaiveToUtcIso()) before any backend call, so every endpoint
        still receives UTC (AS_OF_ISO), matching this addon's UTC-anchored
        trade-window convention. The one deliberate difference from /4l's
        own Window vertical line: it is measured back from AS_OF_ISO
        (asOfEpochSeconds(), Date.parse(AS_OF_ISO + 'Z')) rather than real
        "now", since "testing the past" only makes sense relative to the
        moment being inspected, not actual current time.

        Renders its own standalone template (dankbit_time_machine_chart),
        not dankbit_four_leg_gamma_chart — the two pages share no markup,
        since this one needs an "As Of" date/time picker in place of the
        live page's auto-refresh polling (a past "as of" moment is frozen
        by definition, so nothing here polls on a timer — no candle poll,
        no gamma-leg poll, and unlike /4l/<asset>'s own 5s vline-advance
        tick, no periodic vline reposition either; every fetch and every
        vline reposition only re-runs when the user changes As Of/
        Timeframe/Expiry/Window by hand, or pans/zooms/resizes
        the chart).
        This rebuild needed zero backend/controller changes: every fetch
        just adds `&as_of=<naive-UTC ISO-8601>` (the Berlin picker value
        already converted to UTC client-side) to the same 3 endpoints
        /4l/<asset> uses — /api/klines-coinbase/<asset> (historical
        candles ending at that moment, via get_candles_coinbase()'s own
        `as_of_ts` param), /api/expiries/<asset> (every instrument that
        had already traded and hadn't yet expired as of that moment, via
        dankbit.bands._distinct_expirations_asof()), and
        /api/four-leg-gamma/<asset> with
        `?instrument=<i>&hours=<N>&as_of=<AS_OF_ISO>` — that route's every `?expiry=`/`?instrument=`/
        `?hours=` resolution is already written relative to a local
        `as_of` variable, and its trade searches already run with
        `active_test=False`, so archived (long-since-expired) instruments'
        trades from that period are still found. No new backend
        computation exists for this page — every number it shows is
        something /4l/<asset> can already compute for "now"; this page
        just asks those same 3 endpoints about a different point in time.

        `?as_of=` on this page's own URL (e.g. /tm/BTC?as_of=2026-03-01T00:00,
        read as 00:00 BERLIN on that date) pre-fills the "As Of" input so a
        specific historical view is bookmarkable/shareable; missing/malformed
        defaults to the current Berlin date and hour with minutes zeroed
        (client-side, not a server-side default — the picker only ever
        operates at hour granularity), so a bare /tm/BTC starts out looking
        like a frozen snapshot of the live page before the user picks an
        earlier date."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        as_of_param = request.httprequest.args.get("as_of") or ""
        # The Window dropdown's options are hardcoded in the template
        # (/4l's own Auto/8h/24h/48h/3d/4d/5d/All set), not context-driven.
        ctx = {"asset": asset, "as_of": as_of_param}
        return request.render("dankbit.dankbit_time_machine_chart", ctx)

    @http.route("/api/forecast/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def forecast_json(self, asset, **kw):
        """Unified 4H Forecast path.  Candle one is the revised Next Candle
        Greek-Flow result; later candles retain the deterministic Thales
        Gamma/Bands/Zones/Smart-Liquidity geometry and continue from its close.

        The Thales Forecast candle engine is a full port of Thales's
        "Thales Bands" Pine indicator's forecast-candle engine (see
        forecast.py) onto Dankbit's own live per-leg gamma/delta/theta/vega
        Greeks, rather than Thales's manually-typed-in daily CSV rows.
        Unlike this addon's earlier, now-removed GBM-based forecast
        engines, this path is fully deterministic — no random component
        anywhere, matching the source script, which has none either; every
        candle is a direct function of the current Greeks, the last couple
        of persisted dankbit.forecast.snapshot rows (for the Gamma-Band
        Consensus slope), recent real candles matching the selected timeframe (for ATR/momentum/
        liquidity-sweep detection), and the forward Gamma Band dashed-line
        points (so the forecast trends the same direction as that line —
        see forecast.gamma_band_term_slope). The actual computation lives
        on dankbit.forecast.snapshot.get_forecast_points() — this route is
        now just that method plus JSON serialization, so dankbit.forecast.log's
        cron (see that model) can run the exact same computation without
        an HTTP request context. `points` is empty when there's nothing
        computable yet for this asset (no index price, no active expiry,
        or no trades in the current 00:00-UTC window — see
        dankbit.forecast.snapshot.compute_and_persist)."""
        asset = asset.upper()
        if asset not in ("BTC", "ETH"):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        hours_param = request.httprequest.args.get("hours")
        try:
            hours = int(hours_param) if hours_param else None
        except (TypeError, ValueError):
            hours = None
        if hours is not None and not 1 <= hours <= 168:
            hours = None

        timeframe = request.httprequest.args.get("timeframe", "4h")
        if timeframe not in next_candle_forecast.TIMEFRAME_CONFIG:
            timeframe = "4h"
        max_counts = {"1h": 72, "4h": 18, "1d": 3}
        max_count = max_counts[timeframe]
        count_param = request.httprequest.args.get("count")
        try:
            forecast_count = max(1, min(int(count_param or max_count), max_count))
        except (TypeError, ValueError):
            forecast_count = max_count

        result = request.env["dankbit.forecast.snapshot"].get_forecast_points(
            asset, hours=hours, timeframe=timeframe,
        )
        ForecastNext = request.env["dankbit.forecast.next_candle"]
        bands_model = request.env["dankbit.bands"]
        forecast_expirations = bands_model._distinct_expirations(
            asset, datetime.now(timezone.utc).replace(tzinfo=None), 3,
            future_days_only=True,
        )
        anchors = {}
        anchor_meta = []
        for expiry_index in range(3):
            preview = ForecastNext.preview_for_dashboard(
                asset, timeframe=timeframe, hours=hours, expiry_index=expiry_index,
            ) if hours is not None else None
            next_row = ForecastNext.latest_for_dashboard(
                asset, timeframe=timeframe, expiry_index=expiry_index,
            ) if hours is None else None
            latest_any = None
            meta = {
                "expiry_index": expiry_index, "available": False,
                "expiry_instrument": (
                    bands_model._format_instrument(asset, forecast_expirations[expiry_index])
                    if expiry_index < len(forecast_expirations) else None
                ),
                "revision": None, "max_revisions": None, "confidence": None,
                "is_preview": False, "window_hours": None,
                "quality_tier": "fallback", "quality_weight": 0.0,
                "unavailable_reason": "not-generated-yet",
            }
            if hours is None and not next_row:
                latest_any = ForecastNext.sudo().search([
                    ("asset", "=", asset), ("timeframe", "=", timeframe),
                    ("expiry_index", "=", expiry_index),
                ], order="generated_at desc", limit=1)
                if latest_any:
                    meta["unavailable_reason"] = (
                        "stale-expiry-row"
                        if meta["expiry_instrument"] and latest_any.expiry_instrument != meta["expiry_instrument"]
                        else "stale-target-row"
                    )
            if next_row and meta["expiry_instrument"] and next_row.expiry_instrument != meta["expiry_instrument"]:
                # Never leak today's stale ordinal row into tomorrow's E1.
                next_row = None
                meta["unavailable_reason"] = "stale-expiry-row"
            source = None
            source_is_preview = False
            missing_legs = []
            quality_tier = "fallback"
            quality_weight = 0.0
            quality_reason = meta["unavailable_reason"]

            if preview:
                source = preview
                source_is_preview = True
                missing_legs = list(preview.get("missing_legs") or [])
                quality_tier = "valid" if not missing_legs else "partial"
                quality_weight = 1.0 if not missing_legs else max(0.20, 1.0 - 0.25 * len(missing_legs))
                quality_reason = None if not missing_legs else "missing-critical-leg:" + ",".join(missing_legs)
                anchors[expiry_index] = {
                    "open": source["forecast_open"], "high": source["forecast_high"],
                    "low": source["forecast_low"], "close": source["forecast_close"],
                    "confidence": source["confidence"],
                    "revision": source["revision"], "max_revisions": source["max_revisions"],
                    "greek_flow_score": source.get("greek_flow_score", 0.0),
                    "structural_adjustment": source.get("structural_adjustment", 0.0),
                    "is_weekend": bool(source.get("is_weekend")),
                    "quality_weight": quality_weight, "quality_tier": quality_tier,
                }
            elif next_row:
                source = next_row
                complete, missing_legs = _forecast_leg_completeness(next_row.snapshot_id) if next_row.snapshot_id else (False, ["SNAPSHOT"])
                quality_tier = "valid" if complete else "partial"
                quality_weight = 1.0 if complete else max(0.20, 1.0 - 0.25 * len(missing_legs))
                quality_reason = None if complete else "missing-critical-leg:" + ",".join(missing_legs)
                anchors[expiry_index] = {
                    "open": source.forecast_open, "high": source.forecast_high,
                    "low": source.forecast_low, "close": source.forecast_close,
                    "confidence": source.confidence,
                    "revision": source.revision, "max_revisions": source.max_revisions,
                    "greek_flow_score": source.greek_flow_score,
                    "structural_adjustment": source.structural_adjustment,
                    "is_weekend": source.is_weekend,
                    "quality_weight": quality_weight, "quality_tier": quality_tier,
                }
            elif latest_any and meta["expiry_instrument"] and latest_any.expiry_instrument == meta["expiry_instrument"]:
                # Same-expiry stale target is informative, but capped at 20%.
                # Prior Flow remains the real stored value; no synthetic flow.
                source = latest_any
                quality_tier = "stale"
                quality_weight = 0.20
                quality_reason = "stale-target-row"
                anchors[expiry_index] = {
                    "open": source.forecast_open, "high": source.forecast_high,
                    "low": source.forecast_low, "close": source.forecast_close,
                    "confidence": source.confidence,
                    "revision": source.revision, "max_revisions": source.max_revisions,
                    "greek_flow_score": source.greek_flow_score,
                    "structural_adjustment": source.structural_adjustment,
                    "is_weekend": source.is_weekend,
                    "quality_weight": quality_weight, "quality_tier": quality_tier,
                }

            if source:
                meta.update({
                    "available": True,
                    "revision": source["revision"] if source_is_preview else source.revision,
                    "expiry_instrument": source.get("expiry_instrument") if source_is_preview else source.expiry_instrument,
                    "max_revisions": source["max_revisions"] if source_is_preview else source.max_revisions,
                    "confidence": source["confidence"] if source_is_preview else source.confidence,
                    "is_preview": source_is_preview,
                    "window_hours": source.get("window_hours") if source_is_preview else None,
                    "quality_tier": quality_tier,
                    "quality_weight": quality_weight,
                    "unavailable_reason": quality_reason,
                })
            anchor_meta.append(meta)

        unified = _compose_unified_forecast(
            result["points"], anchors, forecast_count,
            max_count=max_count, timeframe=timeframe,
        )
        # RSI is deliberately retrospective and never participates in the
        # forward direction/EA calculation. It only shapes rejection wicks on
        # the first few candles, with a closed-1H momentum persistence gate.
        Trade = request.env["dankbit.trade"]
        try:
            timeframe_candles = Trade.get_candles(asset, interval=timeframe, limit=260) or []
            hourly_candles = (timeframe_candles if timeframe == "1h"
                              else Trade.get_candles(asset, interval="1h", limit=260) or [])
            unified, rsi_context = _rsi_forecast_geometry(
                unified, timeframe_candles, hourly_candles, timeframe=timeframe,
            )
        except Exception:
            _logger.exception("RSI Forecast geometry failed for %s %s; serving untouched Forecast", asset, timeframe)
            rsi_context = {"available": False, "error": "geometry-unavailable"}
        forecast_warning = _forecast_quality_warning(
            unified, anchor_meta, result["index_price"], result["sigma_annual"], timeframe,
        )
        generated_at = result["generated_at"]
        now_ms = int(generated_at.timestamp() * 1000)
        points = [
            {
                "t": now_ms + int(p["hours"] * 3600 * 1000),
                "open": p["open"], "high": p["high"], "low": p["low"], "close": p["close"],
                "mode": p["mode"],
            }
            for p in unified
        ]

        payload = {
            "asset": asset,
            "index_price": result["index_price"],
            "sigma_annual": result["sigma_annual"],
            "window_hours": result.get("window_hours"),
            "timeframe": timeframe,
            "max_forecast_count": max_count,
            "forecast_count": len(points),
            "first_candle": anchor_meta[0],
            "anchors": anchor_meta,
            "forecast_warning": forecast_warning,
            "rsi_context": rsi_context,
            "points": points,
            "generated_at": generated_at.isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/next-candle-forecast/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def next_candle_forecast_json(self, asset):
        """The "Next Candle Forecast" small status pill + candle on the
        Delta Chart — a distinct feature from /api/forecast/<asset> above
        (that one's own 18-candle rolling series is untouched): tracks
        exactly ONE target candle at a time, per timeframe (1H/4H/1D — see
        `?timeframe=`), revised on that timeframe's own fixed cadence
        while its current candle is still forming (see
        dankbit.forecast.next_candle and controllers/next_candle_forecast.py).
        Returns the latest (highest-revision) row for the asset's
        currently-open target candle, or `{"asset": asset, "row": None}`
        if nothing has been computed yet (e.g. that timeframe's own
        compute_and_log() cron hasn't run, or isn't active)."""
        asset = asset.upper()
        if asset not in ("BTC", "ETH"):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        timeframe = request.httprequest.args.get("timeframe", "4h")
        if timeframe not in next_candle_forecast.TIMEFRAME_CONFIG:
            timeframe = "4h"
        try:
            expiry_index = max(0, min(int(request.httprequest.args.get("expiry_index", 0)), 2))
        except (TypeError, ValueError):
            expiry_index = 0

        hours_param = request.httprequest.args.get("hours")
        try:
            hours = int(hours_param) if hours_param else None
        except (TypeError, ValueError):
            hours = None
        if hours is not None and not 1 <= hours <= 168:
            hours = None

        ForecastNext = request.env["dankbit.forecast.next_candle"]
        preview = ForecastNext.preview_for_dashboard(
            asset, timeframe=timeframe, hours=hours, expiry_index=expiry_index,
        ) if hours is not None else None
        row = ForecastNext.latest_for_dashboard(
            asset, timeframe=timeframe, expiry_index=expiry_index,
        ) if hours is None else None
        row_payload = None
        if preview:
            row_payload = {
                "timeframe": preview["timeframe"],
                "expiry_index": expiry_index,
                "expiry_instrument": preview.get("expiry_instrument"),
                "target_time": preview["target_time"].isoformat(),
                "revision": preview["revision"],
                "max_revisions": preview["max_revisions"],
                "is_final": preview["is_final"],
                "confidence": preview["confidence"],
                "forecast_open": preview["forecast_open"],
                "forecast_close": preview["forecast_close"],
                "forecast_high": preview["forecast_high"],
                "forecast_low": preview["forecast_low"],
                "window_hours": preview["window_hours"],
                "is_preview": True,
            }
        elif row:
            row_payload = {
                "timeframe": row.timeframe,
                "expiry_index": row.expiry_index,
                "expiry_instrument": row.expiry_instrument,
                "target_time": row.target_time.isoformat(),
                "revision": row.revision,
                "max_revisions": row.max_revisions,
                "is_final": row.is_final,
                "confidence": row.confidence,
                "forecast_open": row.forecast_open,
                "forecast_close": row.forecast_close,
                "forecast_high": row.forecast_high,
                "forecast_low": row.forecast_low,
                "window_hours": None,
                "is_preview": False,
            }
        payload = {
            "asset": asset,
            "row": row_payload,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route('/api/signal-bot/<string:asset>', type='http', auth='user', methods=['GET'], csrf=False)
    def signal_bot_json(self, asset, **kwargs):
        """Latest auditable Signal Bot decision for the Delta Chart's
        "Signal Bot" panel. auth="user" like every other route in this
        addon (see Odoo Gotchas in CLAUDE.md) — the browser's own session
        cookie covers this same-origin fetch() once the chart page itself
        is loaded.

        An active official decision remains displayed even after later hourly
        audit rows are written. Otherwise the newest hourly decision explains
        why the bot is still waiting.
        """
        asset = (asset or '').upper()
        if asset not in ('BTC', 'ETH'):
            return request.make_response(
                json.dumps({"error": "Unsupported asset"}),
                headers=[("Content-Type", "application/json")], status=400,
            )
        Signal = request.env["dankbit.signal"].sudo()
        utc_day = datetime.now(timezone.utc).date()
        active_official = Signal.search([
            ("asset", "=", asset), ("kind", "=", "official"),
            ("state", "=", "active"),
        ], order="evaluated_at desc, id desc", limit=1)
        row = active_official or Signal.search([
            ("asset", "=", asset), ("utc_day", "=", utc_day),
        ], order="evaluated_at desc, id desc", limit=1)
        week_start = utc_day - timedelta(days=utc_day.weekday())
        weekly_count = Signal.search_count([
            ("asset", "=", asset), ("utc_day", ">=", week_start),
            ("kind", "=", "official"),
        ])
        payload = {"asset": asset, "utc_day": utc_day.isoformat(),
                   "weekly_signal_count": weekly_count, "weekly_signal_limit": 3,
                   "signal": None}
        if row:
            try:
                audit_snapshot = json.loads(row.snapshot_json or "{}")
            except (TypeError, ValueError):
                audit_snapshot = {}
            payload["signal"] = {
                "id": row.id,
                "kind": row.kind,
                "state": row.state,
                "direction": row.direction,
                "evaluated_at": row.evaluated_at.isoformat() if row.evaluated_at else None,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "entry": row.entry,
                "stop_loss": row.stop_loss,
                "target": row.target,
                "risk_reward": row.risk_reward,
                "swing_level": row.swing_level,
                "atr_4h": row.atr_4h,
                "stop_basis": row.stop_basis or "",
                "trend_daily": row.trend_daily or "neutral",
                "trend_daily_tactical": row.trend_daily_tactical or "neutral",
                "trend_4h": row.trend_4h or "neutral",
                "trend_1h": row.trend_1h or "neutral",
                "trend_score_daily": row.trend_score_daily,
                "trend_score_daily_tactical": row.trend_score_daily_tactical,
                "trend_score_4h": row.trend_score_4h,
                "gamma_direction_score": row.gamma_direction_score,
                "greek_direction_score": row.greek_direction_score,
                "weekly_signal_count": weekly_count,
                "weekly_signal_limit": 3,
                "setup_stage": row.setup_stage or "none",
                "setup_path": row.setup_path or "",
                "plan_status": row.plan_status or "",
                "entry_zone_low": row.entry_zone_low,
                "entry_zone_high": row.entry_zone_high,
                "invalidation_level": row.invalidation_level,
                "target_2": row.target_2,
                "breakout_level": row.breakout_level,
                "breakout_at": row.breakout_at.isoformat() if row.breakout_at else None,
                "plan_provisional": row.plan_provisional,
                "trigger_level": row.trigger_level,
                "trigger_basis": row.trigger_basis or "",
                "liquidity_swept": row.liquidity_swept,
                "rtm_score": row.rtm_score,
                "rtm_zone_low": row.rtm_zone_low,
                "rtm_zone_high": row.rtm_zone_high,
                "rtm_zone_type": row.rtm_zone_type or "",
                "rtm_structure": row.rtm_structure or "",
                "rtm_opposing_structure": row.rtm_opposing_structure or "",
                "rtm_opposing_level": row.rtm_opposing_level,
                "rtm_path_clear": row.rtm_path_clear,
                "rtm_touch_count": row.rtm_touch_count,
                "rtm_fresh": row.rtm_fresh,
                "rtm_base_candles": row.rtm_base_candles,
                "rtm_departure_score": row.rtm_departure_score,
                "rtm_compression": row.rtm_compression,
                "rtm_gap": row.rtm_gap,
                "rtm_nested": row.rtm_nested,
                "entry_model": row.entry_model or "",
                "entry_source": (
                    "1H %s proximal" % (row.rtm_structure or "RTM").upper()
                    if row.rtm_zone_type else "Current-price/base engine"
                ),
                "stop_source": row.stop_basis or "",
                "rtm_candidates_1h": audit_snapshot.get("rtm_candidates_1h", []),
                "quality_score": row.quality_score,
                "final_score": row.final_score,
                "result_r": row.result_r,
                "reason": row.reason or "",
                "is_weekend": row.is_weekend,
            }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route('/signal-bot/report', type='http', auth='user', methods=['GET'], csrf=False)
    def signal_bot_report_page(self, **kwargs):
        """Human-readable automatic forward-test dashboard."""
        return request.render("dankbit.signal_bot_report")

    @http.route('/api/signal-bot/report', type='http', auth='user', methods=['GET'], csrf=False)
    def signal_bot_report_json(self, **kwargs):
        """Aggregate immutable Signal Bot outcomes into auditable R metrics."""
        Signal = request.env["dankbit.signal"].sudo()
        try:
            days = int(kwargs.get("days", 30))
        except (TypeError, ValueError):
            days = 30
        days = days if days in (0, 7, 30, 90, 365) else 30
        asset = str(kwargs.get("asset", "all") or "all").upper()
        kind = str(kwargs.get("kind", "official") or "official").lower()
        entry_model = str(kwargs.get("entry_model", "all") or "all").lower()
        if asset not in ("ALL", "BTC", "ETH"):
            asset = "ALL"
        if kind not in ("all", "official", "shadow"):
            kind = "official"
        allowed_models = (
            "all", "ftb_confirmed", "ftb_reclaim_shadow", "later_touch_shadow",
            "breakout_watch", "breakout_retest_shadow", "breakout_retest_confirmed",
        )
        if entry_model not in allowed_models:
            entry_model = "all"

        common_domain = [("kind", "in", ("official", "shadow"))]
        if days:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
            common_domain.append(("evaluated_at", ">=", cutoff))
        if asset != "ALL":
            common_domain.append(("asset", "=", asset))
        if kind != "all":
            common_domain.append(("kind", "=", kind))
        if entry_model != "all":
            common_domain.append(("entry_model", "=", entry_model))

        closed = Signal.search(
            common_domain + [("state", "in", ("tp", "sl", "expired", "ambiguous"))],
            order="evaluated_at asc, id asc",
        )
        open_count = Signal.search_count(
            common_domain + [("state", "in", ("active", "watch"))],
        )

        def row_r(row):
            return float(row.result_r or 0.0)

        def stats(rows):
            rows = list(rows)
            resolved = [r for r in rows if r.state in ("tp", "sl")]
            performance = [r for r in rows if r.state != "ambiguous"]
            wins = sum(1 for r in resolved if r.state == "tp")
            losses = sum(1 for r in resolved if r.state == "sl")
            expired = sum(1 for r in rows if r.state == "expired")
            ambiguous = sum(1 for r in rows if r.state == "ambiguous")
            values = [row_r(r) for r in performance]
            gross_profit = sum(v for v in values if v > 0)
            gross_loss = abs(sum(v for v in values if v < 0))
            equity = peak = max_drawdown = 0.0
            loss_streak = max_loss_streak = 0
            curve = []
            for row in performance:
                equity += row_r(row)
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, peak - equity)
                if row_r(row) < 0:
                    loss_streak += 1
                    max_loss_streak = max(max_loss_streak, loss_streak)
                else:
                    loss_streak = 0
                curve.append({
                    "time": row.evaluated_at.isoformat() if row.evaluated_at else None,
                    "equity": round(equity, 4),
                })
            planned_rr = [float(r.risk_reward or 0.0) for r in rows if r.risk_reward > 0]
            return {
                "closed": len(rows), "resolved": len(resolved),
                "wins": wins, "losses": losses, "expired": expired,
                "ambiguous": ambiguous,
                "win_rate": round(100.0 * wins / len(resolved), 2) if resolved else None,
                "positive_rate": round(100.0 * sum(v > 0 for v in values) / len(values), 2) if values else None,
                "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
                "profit_factor_infinite": bool(gross_profit and not gross_loss),
                "gross_profit_r": round(gross_profit, 4), "gross_loss_r": round(gross_loss, 4),
                "expectancy_r": round(sum(values) / len(values), 4) if values else None,
                "total_r": round(sum(values), 4),
                "max_drawdown_r": round(max_drawdown, 4),
                "max_loss_streak": max_loss_streak,
                "average_rr": round(sum(planned_rr) / len(planned_rr), 3) if planned_rr else None,
                "curve": curve,
            }

        def breakdown(field_name):
            grouped = {}
            for row in closed:
                key = str(getattr(row, field_name, False) or "unspecified")
                grouped.setdefault(key, []).append(row)
            result = []
            for key, rows in grouped.items():
                item = stats(rows)
                item.pop("curve", None)
                item["name"] = key
                result.append(item)
            return sorted(result, key=lambda item: item["closed"], reverse=True)

        summary = stats(closed)
        sample = summary["resolved"]
        sample_label = ("Insufficient" if sample < 30 else "Preliminary" if sample < 60
                        else "Meaningful" if sample < 150 else "Mature")
        recent = []
        for row in reversed(closed[-50:]):
            recent.append({
                "id": row.id, "time": row.evaluated_at.isoformat() if row.evaluated_at else None,
                "asset": row.asset, "kind": row.kind, "direction": row.direction,
                "state": row.state, "entry_model": row.entry_model or "",
                "rtm_structure": row.rtm_structure or "", "rtm_score": row.rtm_score,
                "risk_reward": row.risk_reward, "result_r": row.result_r,
            })
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filters": {"days": days, "asset": asset, "kind": kind, "entry_model": entry_model},
            "open_count": open_count, "sample_label": sample_label,
            "summary": summary,
            "breakdowns": {
                "asset": breakdown("asset"), "entry_model": breakdown("entry_model"),
                "rtm_structure": breakdown("rtm_structure"), "direction": breakdown("direction"),
            },
            "recent": recent,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    # ------------------------------------------------------------------
    # TradingView Lightweight Charts pages
    # ------------------------------------------------------------------

    def _build_tv_chart_context(self, asset):
        """Context-building for /chart/<asset> (dankbit_tv_chart_until
        template). show_gamma_point/show_strike_gamma are always "false"
        here now — they used to be flipped to "true" by the since-removed
        /oi/<asset> and /mp/<instrument> routes to draw the "Top OI(s)"/
        "Strike Gamma" indicators on this same shared template; kept as
        explicit context keys (rather than removed) since the template's
        own gating logic (e.g. MINIMAL_CHART) still reads them. Returns
        (context, None) on success or (None, error_message) if the weekly
        expiry isn't configured/valid for `asset`, so callers can render
        that as a plain text response the same way this route always
        has."""
        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        zones_box_refresh_interval = int(icp.get_param("dankbit.zones_box_refresh_interval", default=3600))
        zones_box_window_hours = int(icp.get_param("dankbit.zones_box_window_hours", default=8))
        # Thales Forecast candle colors — rendering-only, read here (not
        # dankbit.forecast.snapshot.get_forecast_cfg()) since they only
        # affect the client-side forecastSeries, not simulate_forecast()'s
        # own math.
        forecast_up_color = icp.get_param("dankbit.forecast_up_color", default="#a5d6a7")
        forecast_down_color = icp.get_param("dankbit.forecast_down_color", default="#ef9a9a")
        forecast_wick_up_color = icp.get_param("dankbit.forecast_wick_up_color", default="#66bb6a")
        forecast_wick_down_color = icp.get_param("dankbit.forecast_wick_down_color", default="#e57373")

        if asset.startswith("ETH"):
            weekly_param = "dankbit.eth_weekly_expiry"
            monthly_param = "dankbit.eth_monthly_expiry"
        else:
            weekly_param = "dankbit.weekly_expiry"
            monthly_param = "dankbit.monthly_expiry"

        instrument = icp.get_param(weekly_param, default="").upper()

        if not instrument:
            return None, f"Weekly Expiry for {asset} is not configured. Set it in Settings → Dankbit."

        parts = instrument.split("-", 1)
        if len(parts) != 2:
            return None, f"Weekly Expiry '{instrument}' is invalid — expected format: {asset}-3JUL26."

        expiry_str = parts[1]

        monthly_instrument = icp.get_param(monthly_param, default="").upper()

        # Delta Chart's window title shows the nearest active expiry rather
        # than the configured weekly expiry (the "expiry" field above) —
        # showing the weekly one there had been read by Thales's dev as the
        # expiry the chart's trades/indicators are scoped to, which isn't
        # true for the Bands/Zones/gamma-band indicators (all nearest-expiry
        # based). Falls back to the weekly expiry's own day-suffix if there's
        # no active expiry at all, so the title never renders blank.
        nearest_instrument = request.env["dankbit.bands"].nearest_expiry(asset)
        nearest_expiry_str = nearest_instrument.split("-", 1)[1] if nearest_instrument else expiry_str

        return {
            "instrument": instrument,
            "asset": asset,
            "expiry": expiry_str,
            "nearest_expiry": nearest_expiry_str,
            "monthly_instrument": monthly_instrument,
            "refresh_interval": refresh_interval,
            "zones_box_refresh_interval": zones_box_refresh_interval,
            "zones_box_window_hours": zones_box_window_hours,
            "show_gamma_point": "false",
            "show_strike_gamma": "false",
            "strike_gamma_instrument": "",
            "forecast_up_color": forecast_up_color,
            "forecast_down_color": forecast_down_color,
            "forecast_wick_up_color": forecast_wick_up_color,
            "forecast_wick_down_color": forecast_wick_down_color,
        }, None

    @http.route("/chart/<string:asset>", type="http", auth="user", website=True)
    def chart_tv(self, asset):
        asset = asset.upper()

        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        ctx, error = self._build_tv_chart_context(asset)
        if error:
            return request.make_response(error, headers=[("Content-Type", "text/plain")])

        return request.render("dankbit.dankbit_tv_chart_until", ctx)
