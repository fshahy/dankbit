# -*- coding: utf-8 -*-
"""Forecast 3 — port of Thales's "Thales Bands" Pine indicator's forecast-
candle engine onto Dankbit's own live-computed Greeks (no manual data entry:
see dankbit.forecast3.snapshot for where the per-leg numbers below come
from). Pure functions only, mirroring delta.py/gamma.py's own style — no
Odoo/ORM access, no side effects.

Structural difference from the source Pine script: Thales's rows are
manually-dated CSV entries approaching a *known* future expiry, so its
forecast loop interpolates between a "selected" (current) and "previous"
row that are both real, already-known data points. Dankbit has no
foreknowledge of future Greek levels, so there is only ever one "current"
set of levels (top/low/gamma/curve/theta/delta) — it stays constant across
every forecast step, exactly like Forecast/Forecast 2's own gamma_band/
sigma_annual inputs. The only thing that varies over real time is the
*history* used by the Gamma-Band Consensus engine's slope math (see
gamma_band_consensus below), which compares the current snapshot against
the last couple of persisted dankbit.forecast3.snapshot rows.
"""

import math
from datetime import datetime, timezone

import numpy as np

from . import delta as delta_lib
from . import gamma as gamma_lib
from . import theta as theta_lib
from . import vega as vega_lib
from .options import delta_saturation_price

# Same 90%-of-own-extreme convention options.delta_saturation_price() uses
# elsewhere in this addon (see chart_png_zones) — an independent constant
# rather than importing ChartController's, to keep this module decoupled
# from main.py.
DELTA_SATURATION_FRACTION = 0.9

# Fallback real-hours-ago gap between snapshots when `candles` is empty
# (no real klines available to anchor "now" to) — matches
# dankbit.forecast3.snapshot.BUCKET_HOURS.
BUCKET_HOURS_FALLBACK = 4.0


def per_leg_greeks(STs, trades):
    """Per-leg gamma/delta/theta/vega extrema (price + abs value, matching
    the /<instrument>/zones page's info-overlay scaling: gamma value/1e6,
    delta value/10, theta value/1e4, vega value/100) for the 4 legs in
    `trades` — Thales's BCG/BPG/SCG/SPG, BCD/BPD/SCD/SPD, BCT/BPT/SCT/SPT,
    BCV/BPV/SCV/SPV plus their *Abs strength counterparts. `trades` should
    already be filtered to one expiry."""
    long_calls = trades.filtered(lambda t: t.direction == "buy" and t.option_type == "call")
    long_puts = trades.filtered(lambda t: t.direction == "buy" and t.option_type == "put")
    short_calls = trades.filtered(lambda t: t.direction == "sell" and t.option_type == "call")
    short_puts = trades.filtered(lambda t: t.direction == "sell" and t.option_type == "put")

    def extreme(curve, argfn):
        idx = int(argfn(curve))
        return float(STs[idx]), float(curve[idx])

    bcg_price, bcg_val = extreme(gamma_lib.portfolio_gamma(STs, long_calls), np.argmax)
    bpg_price, bpg_val = extreme(gamma_lib.portfolio_gamma(STs, long_puts), np.argmax)
    scg_price, scg_val = extreme(gamma_lib.portfolio_gamma(STs, short_calls), np.argmin)
    spg_price, spg_val = extreme(gamma_lib.portfolio_gamma(STs, short_puts), np.argmin)

    bct_price, bct_val = extreme(theta_lib.portfolio_theta(STs, long_calls), np.argmin)
    bpt_price, bpt_val = extreme(theta_lib.portfolio_theta(STs, long_puts), np.argmin)
    sct_price, sct_val = extreme(theta_lib.portfolio_theta(STs, short_calls), np.argmax)
    spt_price, spt_val = extreme(theta_lib.portfolio_theta(STs, short_puts), np.argmax)

    bcv_price, bcv_val = extreme(vega_lib.portfolio_vega(STs, long_calls), np.argmax)
    bpv_price, bpv_val = extreme(vega_lib.portfolio_vega(STs, long_puts), np.argmax)
    scv_price, scv_val = extreme(vega_lib.portfolio_vega(STs, short_calls), np.argmin)
    spv_price, spv_val = extreme(vega_lib.portfolio_vega(STs, short_puts), np.argmin)

    def delta_leg(leg_trades, side):
        price = delta_saturation_price(STs, leg_trades, DELTA_SATURATION_FRACTION, side)
        value = float(np.interp(price, STs, delta_lib.portfolio_delta(STs, leg_trades)))
        return price, value

    bcd_price, bcd_val = delta_leg(long_calls, "max")
    bpd_price, bpd_val = delta_leg(long_puts, "min")
    scd_price, scd_val = delta_leg(short_calls, "max")
    spd_price, spd_val = delta_leg(short_puts, "min")

    return {
        "bcg_price": bcg_price, "bpg_price": bpg_price, "scg_price": scg_price, "spg_price": spg_price,
        "bcg_abs": abs(bcg_val) / 1_000_000, "bpg_abs": abs(bpg_val) / 1_000_000,
        "scg_abs": abs(scg_val) / 1_000_000, "spg_abs": abs(spg_val) / 1_000_000,
        "bcd_price": bcd_price, "bpd_price": bpd_price, "scd_price": scd_price, "spd_price": spd_price,
        "bcd_abs": abs(bcd_val) / 10, "bpd_abs": abs(bpd_val) / 10,
        "scd_abs": abs(scd_val) / 10, "spd_abs": abs(spd_val) / 10,
        "bct_price": bct_price, "bpt_price": bpt_price, "sct_price": sct_price, "spt_price": spt_price,
        "bct_abs": abs(bct_val) / 10_000, "bpt_abs": abs(bpt_val) / 10_000,
        "sct_abs": abs(sct_val) / 10_000, "spt_abs": abs(spt_val) / 10_000,
        "bcv_price": bcv_price, "bpv_price": bpv_price, "scv_price": scv_price, "spv_price": spv_price,
        "bcv_abs": abs(bcv_val) / 100, "bpv_abs": abs(bpv_val) / 100,
        "scv_abs": abs(scv_val) / 100, "spv_abs": abs(spv_val) / 100,
    }


def weighted_avg2(price_a, price_b, weight_a, weight_b):
    """Thales's f_weightedAvg2: average of two (possibly-absent) prices,
    each weighted, falling back to plain average when weights are absent
    or non-positive."""
    weighted_sum = 0.0
    weight_sum = 0.0
    if price_a is not None:
        w = weight_a if weight_a and weight_a > 0 else 1.0
        weighted_sum += price_a * w
        weight_sum += w
    if price_b is not None:
        w = weight_b if weight_b and weight_b > 0 else 1.0
        weighted_sum += price_b * w
        weight_sum += w
    return weighted_sum / weight_sum if weight_sum > 0 else None


def level_proximity(price_value, level_value, band_width, max_distance_band):
    """Thales's f_levelProximity: 1.0 at the level itself, fading linearly
    to 0.0 at `max_distance_band` band-widths away."""
    if price_value is None or level_value is None:
        return 0.0
    max_distance = max(band_width * max(max_distance_band, 0.01), 1e-9)
    return max(1.0 - abs(price_value - level_value) / max_distance, 0.0)


# Same weights Thales's Gamma-Curve/Theta center blend defaults to
# (forecastGammaCenterWeight/forecastCurveCenterWeight/forecastThetaCenterWeight);
# forecastDeltaCenterWeight defaults to 0 in the source script (delta only
# feeds the shock modules by default, not the blended center) so it is
# omitted here.
GAMMA_CENTER_WEIGHT = 0.55
CURVE_CENTER_WEIGHT = 0.30
THETA_CENTER_WEIGHT = 0.15

# Gamma-Curve Divergence Mode's reweighting, used instead of the defaults
# above whenever the plain gamma average and the curve (BML/SMP) average
# disagree by more than gammaCurveDivergenceThreshold band-widths.
DIVERGENCE_THRESHOLD = 0.22
DIVERGENCE_GAMMA_WEIGHT = 0.42
DIVERGENCE_CURVE_WEIGHT = 0.43
DIVERGENCE_THETA_WEIGHT = 0.15

# Seller-weighted theta pin center (Thales's useSellerWeightedThetaPin,
# on by default) — short theta dominates where price tends to pin.
BUYER_THETA_BASE_WEIGHT = 0.25
SELLER_THETA_BASE_WEIGHT = 0.75

GAMMA_ABS_NORMALIZER = 0.15
DELTA_ABS_NORMALIZER = 600.0
THETA_ABS_NORMALIZER = 500.0
VEGA_ABS_NORMALIZER = 2500.0


def derive_levels(current):
    """From one per_leg_greeks()+snapshot dict, derive the blended
    gamma/curve/theta averages and the weighted center Thales's forecast
    loop pulls price toward. `current` is a plain dict with the
    dankbit.forecast3.snapshot field names (bcg_price, bcg_abs, bml, smp,
    top, low, ...)."""
    bcg_w = min(max(current["bcg_abs"] / GAMMA_ABS_NORMALIZER, 0.0), 2.0)
    bpg_w = min(max(current["bpg_abs"] / GAMMA_ABS_NORMALIZER, 0.0), 2.0)
    scg_w = min(max(current["scg_abs"] / GAMMA_ABS_NORMALIZER, 0.0), 2.0)
    spg_w = min(max(current["spg_abs"] / GAMMA_ABS_NORMALIZER, 0.0), 2.0)
    buyer_gamma = weighted_avg2(current["bcg_price"], current["bpg_price"], bcg_w, bpg_w)
    seller_gamma = weighted_avg2(current["scg_price"], current["spg_price"], scg_w, spg_w)
    gamma_avg = weighted_avg2(buyer_gamma, seller_gamma, bcg_w + bpg_w, scg_w + spg_w)

    curve_avg = (current["bml"] + current["smp"]) / 2.0

    buyer_theta = (current["bct_price"] + current["bpt_price"]) / 2.0
    seller_theta = (current["sct_price"] + current["spt_price"]) / 2.0
    theta_weight_sum = BUYER_THETA_BASE_WEIGHT + SELLER_THETA_BASE_WEIGHT
    theta_avg = (buyer_theta * BUYER_THETA_BASE_WEIGHT + seller_theta * SELLER_THETA_BASE_WEIGHT) / theta_weight_sum

    band_width = max(current["top"] - current["low"], 1e-9)
    divergent = abs(gamma_avg - curve_avg) / band_width >= DIVERGENCE_THRESHOLD
    gamma_w = DIVERGENCE_GAMMA_WEIGHT if divergent else GAMMA_CENTER_WEIGHT
    curve_w = DIVERGENCE_CURVE_WEIGHT if divergent else CURVE_CENTER_WEIGHT
    theta_w = DIVERGENCE_THETA_WEIGHT if divergent else THETA_CENTER_WEIGHT
    center = (gamma_avg * gamma_w + curve_avg * curve_w + theta_avg * theta_w) / (gamma_w + curve_w + theta_w)

    return {
        "gamma_avg": gamma_avg, "curve_avg": curve_avg, "theta_avg": theta_avg,
        "center": center, "band_width": band_width, "divergent": divergent,
        "buyer_gamma": buyer_gamma, "seller_gamma": seller_gamma,
    }


# ============================================================
# Gamma-Band Consensus backbone (Thales's f_gammaBandConsensus) — compares
# the current snapshot against the last 2 persisted history rows to get a
# top/low/gamma slope (per 8 real hours, normalized by band width), then
# derives a consensus direction/strength when at least 2 of the 3 series
# agree on direction.
# ============================================================
SLOPE_DEAD_ZONE = 0.035
SLOPE_FULL_STRENGTH = 0.18
CURRENT_SLOPE_WEIGHT = 0.65


def gamma_band_consensus(sel_top, prev_top, prev_prev_top,
                          sel_low, prev_low, prev_prev_low,
                          sel_gamma, prev_gamma, prev_prev_gamma,
                          sel_hours_ago, prev_hours_ago, prev_prev_hours_ago,
                          band_width):
    """`sel_*` is the current snapshot; `prev_*`/`prev_prev_*` are the last
    2 persisted dankbit.forecast3.snapshot rows (newest first). `*_hours_ago`
    is how many real hours before now each point was taken (0 for `sel`).
    Returns a dict with consensus_direction (-1/0/1), consensus_strength
    (0-1), conflict_strength, and the individual top/low/gamma slopes."""
    current_hours = max(abs(sel_hours_ago - prev_hours_ago), 1.0)
    previous_hours = max(abs(prev_hours_ago - prev_prev_hours_ago), 1.0)

    def slope(sel_v, prev_v, prev_prev_v):
        cur = (sel_v - prev_v) * 8.0 / current_hours / band_width
        old = (prev_v - prev_prev_v) * 8.0 / previous_hours / band_width
        has_previous = prev_hours_ago != prev_prev_hours_ago and sel_hours_ago != prev_hours_ago
        return cur * CURRENT_SLOPE_WEIGHT + old * (1.0 - CURRENT_SLOPE_WEIGHT) if has_previous else cur

    top_slope = slope(sel_top, prev_top, prev_prev_top)
    low_slope = slope(sel_low, prev_low, prev_prev_low)
    gamma_slope = slope(sel_gamma, prev_gamma, prev_prev_gamma)

    def direction(s):
        return 1 if s > SLOPE_DEAD_ZONE else (-1 if s < -SLOPE_DEAD_ZONE else 0)

    top_dir, low_dir, gamma_dir = direction(top_slope), direction(low_slope), direction(gamma_slope)

    confirms_top = gamma_dir != 0 and top_dir == gamma_dir
    confirms_low = gamma_dir != 0 and low_dir == gamma_dir
    all_aligned = confirms_top and confirms_low
    conflict = gamma_dir != 0 and top_dir != 0 and low_dir != 0 and top_dir == low_dir and gamma_dir == -top_dir

    full_slope = max(SLOPE_FULL_STRENGTH, SLOPE_DEAD_ZONE + 0.001)
    top_strength = min(abs(top_slope) / full_slope, 1.0)
    low_strength = min(abs(low_slope) / full_slope, 1.0)
    gamma_strength = min(abs(gamma_slope) / full_slope, 1.0)

    if all_aligned:
        consensus_direction = gamma_dir
        consensus_strength = (top_strength + low_strength + gamma_strength) / 3.0
    elif confirms_top:
        consensus_direction = gamma_dir
        consensus_strength = (top_strength + gamma_strength) / 2.0
    elif confirms_low:
        consensus_direction = gamma_dir
        consensus_strength = (low_strength + gamma_strength) / 2.0
    else:
        consensus_direction = 0
        consensus_strength = 0.0
    conflict_strength = (top_strength + low_strength + gamma_strength) / 3.0 if conflict else 0.0

    return {
        "top_slope": top_slope, "low_slope": low_slope, "gamma_slope": gamma_slope,
        "consensus_direction": consensus_direction, "consensus_strength": consensus_strength,
        "conflict": conflict, "conflict_strength": conflict_strength,
        "confirms_top": confirms_top, "confirms_low": confirms_low, "all_aligned": all_aligned,
    }


RECLAIM_MIN_STRENGTH = 0.45
RECLAIM_STRENGTH = 0.11
RECLAIM_MAX_IMPULSE = 0.075
RECLAIM_MAX_DISTANCE_BAND = 0.75


def gamma_band_reclaim_bias(consensus, projected_price, current_close, gamma_ref,
                             lower_band, upper_band, band_width, step):
    """Thales's f_gammaBandReclaimBias: once price has broken back to the
    "wrong" side of the gamma reference under a strong, all-3-aligned
    consensus, bias it back toward the gamma reference (a "reclaim")."""
    if not (consensus["all_aligned"] and consensus["consensus_strength"] >= RECLAIM_MIN_STRENGTH):
        return 0.0, False, None

    safe_band = max(band_width, 1e-9)
    direction = consensus["consensus_direction"]
    bullish = direction > 0 and projected_price < gamma_ref and current_close < gamma_ref and current_close >= lower_band - safe_band * 0.25
    bearish = direction < 0 and projected_price > gamma_ref and current_close > gamma_ref and current_close <= upper_band + safe_band * 0.25
    if not (bullish or bearish):
        return 0.0, False, None

    target = gamma_ref
    distance = abs((gamma_ref - projected_price) if bullish else (projected_price - gamma_ref)) / safe_band
    if distance > RECLAIM_MAX_DISTANCE_BAND:
        return 0.0, False, None

    raw = ((target - projected_price) / safe_band) * RECLAIM_STRENGTH * consensus["consensus_strength"] * (0.86 ** step)
    impulse = max(min(raw, RECLAIM_MAX_IMPULSE), 0.0) if bullish else min(max(raw, -RECLAIM_MAX_IMPULSE), 0.0)
    return impulse, True, target


# ============================================================
# Delta Shock Module (Thales's f_deltaShockModule) — put/call delta-
# saturation levels breaking outside the top/low band, confirmed by price
# also being on the correct side of the gamma reference.
# ============================================================
DELTA_SHOCK_THRESHOLD = 0.16
DELTA_SHOCK_STRENGTH = 0.48
DELTA_SHOCK_PROXIMITY_PCT = 0.15
PARTIAL_DELTA_DRIFT_STRENGTH = 0.11


def delta_shock_module(put_delta_level, call_delta_level, lower_band, upper_band,
                        projected_price, current_close, gamma_ref, confirmed_below, confirmed_above,
                        band_width, put_strength_mult, call_strength_mult):
    """Returns (impulse, bear_shock, bull_shock)."""
    put_below = put_delta_level is not None and put_delta_level < lower_band
    call_above = call_delta_level is not None and call_delta_level > upper_band
    raw_below_gamma = projected_price < gamma_ref or current_close < gamma_ref
    raw_above_gamma = projected_price > gamma_ref or current_close > gamma_ref
    price_below_red = projected_price < lower_band or current_close < lower_band
    price_above_green = projected_price > upper_band or current_close > upper_band
    price_near_red = projected_price <= lower_band + band_width * DELTA_SHOCK_PROXIMITY_PCT or current_close <= lower_band + band_width * DELTA_SHOCK_PROXIMITY_PCT
    price_near_green = projected_price >= upper_band - band_width * DELTA_SHOCK_PROXIMITY_PCT or current_close >= upper_band - band_width * DELTA_SHOCK_PROXIMITY_PCT
    put_distance = (lower_band - put_delta_level) / band_width if put_below else 0.0
    call_distance = (call_delta_level - upper_band) / band_width if call_above else 0.0

    bear_confirm_ok = price_below_red or price_near_red
    bull_confirm_ok = price_above_green or price_near_green
    bear_shock = put_below and confirmed_below and bear_confirm_ok and (price_below_red or put_distance >= DELTA_SHOCK_THRESHOLD)
    bull_shock = call_above and confirmed_above and bull_confirm_ok and (price_above_green or call_distance >= DELTA_SHOCK_THRESHOLD)

    impulse = 0.0
    if bear_shock:
        impulse = ((put_delta_level - projected_price) / band_width) * DELTA_SHOCK_STRENGTH * put_strength_mult
    elif bull_shock:
        impulse = ((call_delta_level - projected_price) / band_width) * DELTA_SHOCK_STRENGTH * call_strength_mult
    elif put_below and raw_below_gamma:
        impulse = ((put_delta_level - projected_price) / band_width) * PARTIAL_DELTA_DRIFT_STRENGTH * put_strength_mult
    elif call_above and raw_above_gamma:
        impulse = ((call_delta_level - projected_price) / band_width) * PARTIAL_DELTA_DRIFT_STRENGTH * call_strength_mult
    return impulse, bear_shock, bull_shock


# ============================================================
# Gamma Shock Module (Thales's f_gammaShockModule) — a real close beyond
# the top/low band, confirmed by price also being on the correct side of
# the gamma reference; the continuation-vs-retest split from the source
# script (which needs several bars of forward-looking retest logic
# per-step) is simplified to a single continuation reaction, since Dankbit
# generates the whole path in one batch rather than bar-by-bar.
# ============================================================
SHOCK_BODY_ATR_MULT = 1.20
SHOCK_BREAK_THRESHOLD = 0.09
SHOCK_CONTINUATION_STRENGTH = 0.70


def gamma_shock_module(last_close, last_open, atr, lower_band, upper_band, band_width,
                        gamma_ref, confirmed_below, confirmed_above, bml, smp, put_delta, call_delta):
    """Returns (impulse, bear_active, bull_active, strength)."""
    body_size = abs(last_close - last_open)
    atr_safe = max(atr, 1e-9)
    strong_body = body_size >= atr_safe * SHOCK_BODY_ATR_MULT
    bear_break = (lower_band - last_close) / band_width if last_close < lower_band else 0.0
    bull_break = (last_close - upper_band) / band_width if last_close > upper_band else 0.0

    bear_active = last_close < lower_band and confirmed_below and (strong_body or bear_break >= SHOCK_BREAK_THRESHOLD)
    bull_active = last_close > upper_band and confirmed_above and (strong_body or bull_break >= SHOCK_BREAK_THRESHOLD)

    strength = 0.0
    impulse = 0.0
    if bear_active:
        strength = min(max(bear_break / max(SHOCK_BREAK_THRESHOLD, 0.01), body_size / max(atr_safe * SHOCK_BODY_ATR_MULT, 1e-9)), 2.0) / 2.0
        target = min(lower_band, bml, put_delta) if put_delta is not None else min(lower_band, bml)
        impulse = ((target - last_close) / band_width) * SHOCK_CONTINUATION_STRENGTH * (0.70 + strength * 0.60)
    elif bull_active:
        strength = min(max(bull_break / max(SHOCK_BREAK_THRESHOLD, 0.01), body_size / max(atr_safe * SHOCK_BODY_ATR_MULT, 1e-9)), 2.0) / 2.0
        target = max(upper_band, smp, call_delta) if call_delta is not None else max(upper_band, smp)
        impulse = ((target - last_close) / band_width) * SHOCK_CONTINUATION_STRENGTH * (0.70 + strength * 0.60)
    return impulse, bear_active, bull_active, strength


# ============================================================
# Vega Regime Engine (Thales's vega block) — buyer vs seller vega
# dominance expands/compresses the wick, and (when the vega-heavy side
# agrees with the Gamma-Band consensus direction) nudges the body too.
# ============================================================
VEGA_MIN_ACTIVITY = 0.12
VEGA_DOMINANCE_THRESHOLD = 1.45
VEGA_PROXIMITY_BAND = 0.45
VEGA_EXPANSION_WICK_BOOST = 0.18
VEGA_DIRECTIONAL_IMPULSE_STRENGTH = 0.03
VEGA_MAX_DIRECTIONAL_IMPULSE = 0.025
VEGA_COMPRESSION_BODY_DAMPING = 0.12
VEGA_COMPRESSION_WICK_DAMPING = 0.08


def vega_regime(current, projected_price, band_width, consensus_direction, consensus_strength, step):
    """Returns (impulse, wick_multiplier, expansion_active, compression_active)."""
    buyer_norm = min(max((current["bcv_abs"] + current["bpv_abs"]) / VEGA_ABS_NORMALIZER, 0.0), 3.0)
    seller_norm = min(max((current["scv_abs"] + current["spv_abs"]) / VEGA_ABS_NORMALIZER, 0.0), 3.0)
    total_norm = buyer_norm + seller_norm

    bmv = (current["bcv_price"] + current["bpv_price"]) / 2.0
    smv = (current["scv_price"] + current["spv_price"]) / 2.0
    bmv_prox = level_proximity(projected_price, bmv, band_width, VEGA_PROXIMITY_BAND)
    smv_prox = level_proximity(projected_price, smv, band_width, VEGA_PROXIMITY_BAND)
    vega_proximity = max(bmv_prox, smv_prox)
    vega_energy = total_norm * (0.50 + 0.50 * vega_proximity)
    enough_vega = vega_energy >= VEGA_MIN_ACTIVITY

    expansion_active = enough_vega and buyer_norm > seller_norm * VEGA_DOMINANCE_THRESHOLD
    compression_active = enough_vega and seller_norm > buyer_norm * VEGA_DOMINANCE_THRESHOLD

    wick_mult = 1.0
    body_mult = 1.0
    impulse = 0.0
    if expansion_active:
        wick_mult = 1.0 + min(vega_energy * VEGA_EXPANSION_WICK_BOOST, 0.60)
        if consensus_direction != 0:
            raw = consensus_direction * vega_energy * consensus_strength * VEGA_DIRECTIONAL_IMPULSE_STRENGTH * (0.86 ** step)
            impulse = max(min(raw, VEGA_MAX_DIRECTIONAL_IMPULSE), -VEGA_MAX_DIRECTIONAL_IMPULSE)
    elif compression_active:
        damp = min(vega_energy * VEGA_COMPRESSION_BODY_DAMPING, 0.45)
        body_mult = max(1.0 - damp, 0.45)
        wick_mult = max(1.0 - min(vega_energy * VEGA_COMPRESSION_WICK_DAMPING, 0.25), 0.75)
    return impulse, wick_mult, body_mult, expansion_active, compression_active


# ============================================================
# Market-Maker Gamma Contest Engine (Thales's MM block) — buyer-call
# breakout pressure vs seller-call pin pressure (and the put-side mirror),
# each scaled by proximity of price to that leg's own gamma level.
# ============================================================
MM_GAMMA_PROXIMITY_BAND = 0.30
MM_BUYER_ACCEL_STRENGTH = 0.16
MM_SELLER_PIN_STRENGTH = 0.14
MM_BODY_SCALE = 1.00
MM_MAX_IMPULSE = 0.18
MM_WICK_BIAS_STRENGTH = 0.22


def market_maker_gamma_contest(current, projected_price, band_width, body_confidence_hint, step):
    """Returns (impulse, upper_wick_boost, lower_wick_boost)."""
    def norm(v):
        return min(max(v / GAMMA_ABS_NORMALIZER, 0.0), 1.5)

    bcg_w, bpg_w = norm(current["bcg_abs"]), norm(current["bpg_abs"])
    scg_w, spg_w = norm(current["scg_abs"]), norm(current["spg_abs"])

    bcg_prox = level_proximity(projected_price, current["bcg_price"], band_width, MM_GAMMA_PROXIMITY_BAND)
    scg_prox = level_proximity(projected_price, current["scg_price"], band_width, MM_GAMMA_PROXIMITY_BAND)
    bpg_prox = level_proximity(projected_price, current["bpg_price"], band_width, MM_GAMMA_PROXIMITY_BAND)
    spg_prox = level_proximity(projected_price, current["spg_price"], band_width, MM_GAMMA_PROXIMITY_BAND)

    call_breakout = bcg_w * bcg_prox
    call_pin = scg_w * scg_prox
    put_breakdown = bpg_w * bpg_prox
    put_support = spg_w * spg_prox

    upper_activity = min((bcg_w * bcg_prox + scg_w * scg_prox) / 2.0, 1.5)
    lower_activity = min((bpg_w * bpg_prox + spg_w * spg_prox) / 2.0, 1.5)

    upper_impulse = call_breakout * MM_BUYER_ACCEL_STRENGTH - call_pin * MM_SELLER_PIN_STRENGTH
    lower_impulse = put_breakdown * MM_BUYER_ACCEL_STRENGTH - put_support * MM_SELLER_PIN_STRENGTH

    impulse = (upper_impulse - lower_impulse) * MM_BODY_SCALE * body_confidence_hint * (0.80 ** step)
    impulse = max(min(impulse, MM_MAX_IMPULSE), -MM_MAX_IMPULSE)

    upper_wick_boost = upper_activity * MM_WICK_BIAS_STRENGTH
    lower_wick_boost = lower_activity * MM_WICK_BIAS_STRENGTH
    return impulse, upper_wick_boost, lower_wick_boost


# ============================================================
# Liquidity Map Engine (Thales's liquidity block) — manually-entered
# CoinGlass resting-liquidity levels (see dankbit.liquidity.snapshot) act
# as magnets/rejection points: whichever side has the stronger, closer
# liquidity pulls price its way (once dominant enough over the other
# side), and a real candle sweeping through a level and reversing away
# from it fires an extra rejection impulse. Fully inert (returns 0
# impulse, no floors/compression) whenever no liquidity has been entered
# yet for this asset — same na-safe behavior the source script has for a
# blank liquidity column.
# ============================================================
LIQUIDITY_VOLUME_NORMALIZER = 1000.0
LIQUIDITY_DISTANCE_BAND_WIDTH = 0.60
LIQUIDITY_DOMINANCE_THRESHOLD = 1.15
LIQUIDITY_BODY_BIAS_STRENGTH = 0.18
LIQUIDITY_BODY_CONFIDENCE_FLOOR = 0.60
LIQUIDITY_SWEEP_IMPULSE_STRENGTH = 0.22
LIQUIDITY_SWEEP_BODY_BOOST = 1.25
LIQUIDITY_ALIGNED_WICK_COMPRESSION = 0.25
LIQUIDITY_OPPOSITE_WICK_COMPRESSION = 0.35
LIQUIDITY_SWEEP_WICK_COMPRESSION = 0.70


def liquidity_map_engine(lower_liq_price, lower_liq_m, upper_liq_price, upper_liq_m,
                          projected_price, center, band_width, last_candle, body_mult, step):
    """Returns a dict: impulse, has_map, lower_dominant, upper_dominant,
    upper_swept_rejected, lower_swept_rejected, bias_abs, sweep_body_boost
    — `center` (the blended gamma/curve/theta price) stands in for
    Thales's own price-vs-forecast-center structure check, and
    `last_candle` (most recent real 4h OHLC dict) for its swept-and-
    rejected detection."""
    has_map = bool(lower_liq_price and lower_liq_m and upper_liq_price and upper_liq_m)
    empty = {"impulse": 0.0, "has_map": False, "lower_dominant": False, "upper_dominant": False,
             "upper_swept_rejected": False, "lower_swept_rejected": False,
             "bias_abs": 0.0, "sweep_body_boost": 1.0}
    if not has_map:
        return empty

    lower_below = lower_liq_price < projected_price
    upper_above = upper_liq_price > projected_price
    lower_proximity = level_proximity(projected_price, lower_liq_price, band_width, LIQUIDITY_DISTANCE_BAND_WIDTH) if lower_below else 0.0
    upper_proximity = level_proximity(projected_price, upper_liq_price, band_width, LIQUIDITY_DISTANCE_BAND_WIDTH) if upper_above else 0.0
    lower_strength = min(max(lower_liq_m / LIQUIDITY_VOLUME_NORMALIZER, 0.0), 2.0)
    upper_strength = min(max(upper_liq_m / LIQUIDITY_VOLUME_NORMALIZER, 0.0), 2.0)
    lower_attraction = lower_strength * lower_proximity
    upper_attraction = upper_strength * upper_proximity
    total_attraction = lower_attraction + upper_attraction
    bias = (upper_attraction - lower_attraction) / total_attraction if total_attraction > 0 else 0.0
    bias_abs = abs(bias)

    lower_dominant = lower_attraction > upper_attraction * LIQUIDITY_DOMINANCE_THRESHOLD
    upper_dominant = upper_attraction > lower_attraction * LIQUIDITY_DOMINANCE_THRESHOLD
    price_structure_bear = projected_price < center
    price_structure_bull = projected_price > center

    last_close = last_candle["c"] if last_candle else projected_price
    last_open = last_candle["o"] if last_candle else projected_price
    upper_swept_rejected = bool(last_candle) and last_candle["h"] >= upper_liq_price and last_close < upper_liq_price and last_close < last_open
    lower_swept_rejected = bool(last_candle) and last_candle["l"] <= lower_liq_price and last_close > lower_liq_price and last_close > last_open

    impulse = 0.0
    if lower_dominant and price_structure_bear:
        impulse = -bias_abs * LIQUIDITY_BODY_BIAS_STRENGTH * body_mult * (0.80 ** step)
    elif upper_dominant and price_structure_bull:
        impulse = bias_abs * LIQUIDITY_BODY_BIAS_STRENGTH * body_mult * (0.80 ** step)
    if upper_swept_rejected:
        impulse += -LIQUIDITY_SWEEP_IMPULSE_STRENGTH * body_mult * (0.80 ** step)
    if lower_swept_rejected:
        impulse += LIQUIDITY_SWEEP_IMPULSE_STRENGTH * body_mult * (0.80 ** step)

    sweep_body_boost = LIQUIDITY_SWEEP_BODY_BOOST if (
        (upper_swept_rejected and last_close < last_open) or (lower_swept_rejected and last_close > last_open)
    ) else 1.0

    return {
        "impulse": impulse, "has_map": True,
        "lower_dominant": lower_dominant, "upper_dominant": upper_dominant,
        "upper_swept_rejected": upper_swept_rejected, "lower_swept_rejected": lower_swept_rejected,
        "bias_abs": bias_abs, "sweep_body_boost": sweep_body_boost,
    }


# ============================================================
# Option Cluster Structure Engine (Thales's cluster block) — how tightly
# top/low/gamma/BML/SMP agree with each other (dispersion), and whether
# they're all moving the same direction since the last snapshot
# (alignment). Compressed + aligned-expanding clusters get a confidence
# floor; compressed-only clusters get damped.
# ============================================================
CLUSTER_COMPRESSED_THRESHOLD = 0.18
CLUSTER_EXPANSION_THRESHOLD = 0.025
CLUSTER_ALIGNMENT_THRESHOLD = 0.60
CLUSTER_BODY_BOOST = 0.45
CLUSTER_BODY_CONFIDENCE_FLOOR = 0.58
CLUSTER_COMPRESSION_BODY_DAMPING = 0.15
CLUSTER_COMPRESSION_WICK_COMPRESSION = 0.30


def _cluster_values(top, low, buyer_gamma, seller_gamma, bml, smp):
    return [v for v in (top, low, buyer_gamma, seller_gamma, bml, smp) if v is not None]


def cluster_dispersion(top, low, buyer_gamma, seller_gamma, bml, smp):
    values = _cluster_values(top, low, buyer_gamma, seller_gamma, bml, smp)
    band_width = max(top - low, 1e-9)
    if len(values) <= 1:
        return None
    mean = sum(values) / len(values)
    dev = sum(abs(v - mean) for v in values)
    return (dev / len(values)) / band_width


def cluster_center(top, low, buyer_gamma, seller_gamma, bml, smp):
    values = _cluster_values(top, low, buyer_gamma, seller_gamma, bml, smp)
    return sum(values) / len(values) if values else None


def cluster_alignment(cur, prev):
    """Direction agreement between the current and previous snapshot's
    top/low/buyer-gamma/seller-gamma/BML/SMP values."""
    pos_move = neg_move = 0.0
    for key in ("top", "low", "buyer_gamma", "seller_gamma", "bml", "smp"):
        a, b = cur.get(key), prev.get(key)
        if a is None or b is None:
            continue
        d = a - b
        if d > 0:
            pos_move += abs(d)
        elif d < 0:
            neg_move += abs(d)
    total = pos_move + neg_move
    if total <= 0:
        return 0.0, 1.0
    return abs(pos_move - neg_move) / total, (1.0 if pos_move >= neg_move else -1.0)


# ============================================================
# Gamma Neutral / Hysteresis Zone — dampens the body near the gamma
# reference (chop) unless a shock/momentum move is already active.
# ============================================================
GAMMA_NEUTRAL_BAND_PCT = 0.10
GAMMA_NEUTRAL_ATR_MULT = 0.35
NEAR_GAMMA_BODY_DAMPING = 0.40
NEAR_GAMMA_WICK_EXPANSION = 0.25


def gamma_neutral_score(projected_price, current_close, gamma_ref, band_width, atr):
    width = max(max(band_width * GAMMA_NEUTRAL_BAND_PCT, atr * GAMMA_NEUTRAL_ATR_MULT), 1e-9)
    distance = min(abs(projected_price - gamma_ref), abs(current_close - gamma_ref))
    return max(1.0 - distance / width, 0.0)


# ============================================================
# Session-aware + weekend/regime multiplier tables (Thales's own defaults,
# UTC-only — sessionTimezone defaults to "Etc/UTC" in the source script, and
# every other server-side computation in this addon already works in UTC).
# ============================================================
def session_name(hour_utc):
    if 0 <= hour_utc < 8:
        return "Asia"
    if 8 <= hour_utc < 13:
        return "London"
    if 13 <= hour_utc < 16:
        return "Overlap"
    if 16 <= hour_utc < 21:
        return "NY"
    return "PostNY"


SESSION_BODY_FACTOR = {"Asia": 0.70, "London": 0.90, "Overlap": 1.05, "NY": 0.95, "PostNY": 0.70}
SESSION_ATR_FACTOR = {"Asia": 0.75, "London": 0.95, "Overlap": 1.10, "NY": 1.00, "PostNY": 0.75}
SESSION_SHOCK_FACTOR = {"Asia": 0.65, "London": 0.95, "Overlap": 1.10, "NY": 1.00, "PostNY": 0.65}
SESSION_THRESHOLD_FACTOR = {"Asia": 1.25, "London": 1.05, "Overlap": 0.90, "NY": 1.00, "PostNY": 1.25}
SESSION_FIRST_MOVE_ATR = {"Asia": 0.35, "London": 0.55, "Overlap": 0.75, "NY": 0.60, "PostNY": 0.35}

WEEKEND_BODY_FACTOR = 0.65
WEEKEND_ATR_FACTOR = 0.75
WEEKEND_SHOCK_FACTOR = 0.75
WEEKEND_THRESHOLD_FACTOR = 1.20
WEEKEND_PER_CANDLE_MOVE_ATR = 0.26
WEEKEND_TOTAL_MOVE_ATR = 1.45
MAX_FIRST_WEEKEND_MOVE_ATR = 0.45
FIRST_WEEKEND_CANDLE_DAMPENING = 0.55

LOW_VOLUME_REGIME_BLEND = 0.50
HIGH_VOLUME_REGIME_BLEND = 0.55
WEEKDAY_PULL_FACTOR, WEEKEND_PULL_FACTOR = 1.00, 0.60
LOW_VOL_PULL_FACTOR, HIGH_VOL_PULL_FACTOR = 0.75, 1.05
WEEKDAY_SHOCK_FACTOR, WEEKEND_SHOCK_REGIME_FACTOR = 1.00, 0.70
LOW_VOL_SHOCK_FACTOR, HIGH_VOL_SHOCK_FACTOR = 0.70, 1.10

FORECAST_PULL_FACTOR = 0.55
FORECAST_SLOPE_FACTOR = 0.35
FORECAST_BODY_FACTOR = 0.30
FORECAST_CURVE_EXTREME_BODY_WEIGHT = 0.26
FORECAST_WICK_FACTOR = 0.35
FORECAST_ATR_FACTOR = 0.30
FORECAST_CURVE_WICK_WEIGHT = 0.42
FORECAST_DISAGREEMENT_DAMPING = 0.28
FORECAST_DISAGREEMENT_WICK_BOOST = 0.10

GAMMA_BAND_OPPOSITE_WICK_COMPRESSION = 0.18
GAMMA_BAND_CONFIRMED_TARGET_BOOST = 0.55
GAMMA_BAND_CONFIDENCE_BOOST = 0.20
GAMMA_BAND_CONFLICT_BODY_DAMPING = 0.30
GAMMA_BAND_CONFLICT_WICK_EXPANSION = 0.25
GAMMA_BAND_OPPOSING_MAGNET_DAMPING = 0.60

GAMMA_CONFIRM_BARS = 2
GAMMA_CONFIRM_BUFFER_PCT = 0.06

MOMENTUM_BODY_ATR_MULT = 0.85
LIQUIDITY_SWEEP_LOOKBACK = 6
MOMENTUM_BODY_CONFIDENCE_FLOOR = 0.62
MOMENTUM_WICK_COMPRESSION = 0.75
SWEEP_REJECTION_BODY_BOOST = 1.35


def _gamma_confirmation(closes, gamma_ref, buffer):
    """Whether the last GAMMA_CONFIRM_BARS *real* closes are consistently
    above/below `gamma_ref` by at least `buffer` — Thales's own bar-by-bar
    confirmation collapses to "the last N already-closed real candles"
    here, since Dankbit computes the whole forecast in one batch rather
    than re-evaluating live each bar."""
    if len(closes) < GAMMA_CONFIRM_BARS:
        return False, False
    recent = closes[-GAMMA_CONFIRM_BARS:]
    above = all(c > gamma_ref + buffer for c in recent)
    below = all(c < gamma_ref - buffer for c in recent)
    return above, below


def _momentum_override(candles, atr):
    """Whether the most recent real candle shows a strong, liquidity-sweep-
    confirmed directional break — Thales's momentum/liquidity-sweep
    override, computed once from real data and applied uniformly across
    every forecast step (same as the source script, which also computes
    this once per execution)."""
    if len(candles) < LIQUIDITY_SWEEP_LOOKBACK + 2:
        return False, False, 1.0
    last = candles[-1]
    prev_confirmed_close = candles[-2]["c"]
    body = last["c"] - last["o"]
    body_atr_ratio = abs(body) / max(atr, 1e-9)
    lookback = candles[-(LIQUIDITY_SWEEP_LOOKBACK + 1):-1]
    recent_high = max(c["h"] for c in lookback)
    recent_low = min(c["l"] for c in lookback)
    bearish_sweep = last["h"] > recent_high and last["c"] < last["o"]
    bullish_sweep = last["l"] < recent_low and last["c"] > last["o"]
    momentum_bear = body_atr_ratio >= MOMENTUM_BODY_ATR_MULT and body < 0
    momentum_bull = body_atr_ratio >= MOMENTUM_BODY_ATR_MULT and body > 0
    override_bear = momentum_bear and (bearish_sweep or last["c"] < prev_confirmed_close)
    override_bull = momentum_bull and (bullish_sweep or last["c"] > prev_confirmed_close)
    sweep_boost = SWEEP_REJECTION_BODY_BOOST if (override_bear and bearish_sweep) or (override_bull and bullish_sweep) else 1.0
    return override_bear, override_bull, sweep_boost


def _atr14(candles):
    """Classic ATR over the last 14 real candles (true range = max of
    high-low, |high-prevclose|, |low-prevclose|) — Thales's chartAtr14,
    computed here from the same Deribit 4h candles /api/klines/<asset>
    serves, rather than assumed from implied vol, since real historical
    bars are available."""
    if len(candles) < 15:
        return None
    trs = []
    for i in range(len(candles) - 14, len(candles)):
        c, prev = candles[i], candles[i - 1]
        tr = max(c["h"] - c["l"], abs(c["h"] - prev["c"]), abs(c["l"] - prev["c"]))
        trs.append(tr)
    return sum(trs) / len(trs)


def simulate_forecast3(index_price, sigma_annual, current, history, candles,
                        hours_ahead=24, step_hours=4, start_offset_hours=4):
    """The full forecast-candle cascade, ported from Thales's per-step Pine
    loop. `current` and each row of `history` are dankbit.forecast3.snapshot
    field dicts (newest history row first); `candles` are recent real 4h
    OHLC dicts, oldest first, with the last entry being the most recently
    fetched (still-forming) real candle. Deterministic — unlike
    Forecast/Forecast 2, there is no GBM/random component anywhere in this
    engine (matching the source script, which has none either); every
    candle is a direct function of the current Greeks and recent price
    action. Returns a list of {hours, open, high, low, close, mode} dicts,
    one per step_hours-spaced candle out to hours_ahead. `hours_ahead=24`
    (6 candles) matches Thales's own `forecastCandleCount` default —
    shorter than Forecast/Forecast 2's 48h horizon, since nothing in the
    engine assumes a specific cutoff (the per-step decay terms like
    `0.75 ** step`/`0.82 ** step`/etc. just keep tapering either way), this
    is a plain parameter choice, not a structural limit."""
    levels = derive_levels(current)
    band_width = levels["band_width"]
    top, low = current["top"], current["low"]
    gamma_ref = levels["gamma_avg"]

    now_utc = candles[-1]["t"] / 1000.0 if candles else None
    atr = _atr14(candles) or (index_price * sigma_annual * math.sqrt(4.0 / (24.0 * 365.0)))

    # Gamma-Band Consensus needs 2 real historical points besides "now".
    consensus = None
    if len(history) >= 2:
        prev, prev_prev = history[0], history[1]
        prev_levels = derive_levels(prev)
        prev_prev_levels = derive_levels(prev_prev)
        prev_hours_ago = (now_utc - prev["bucket_epoch"]) / 3600.0 if now_utc else BUCKET_HOURS_FALLBACK
        prev_prev_hours_ago = (now_utc - prev_prev["bucket_epoch"]) / 3600.0 if now_utc else BUCKET_HOURS_FALLBACK * 2
        consensus = gamma_band_consensus(
            top, prev["top"], prev_prev["top"],
            low, prev["low"], prev_prev["low"],
            gamma_ref, prev_levels["gamma_avg"], prev_prev_levels["gamma_avg"],
            0.0, prev_hours_ago, prev_prev_hours_ago, band_width,
        )
    if consensus is None:
        consensus = {"consensus_direction": 0, "consensus_strength": 0.0, "conflict": False,
                     "conflict_strength": 0.0, "confirms_top": False, "confirms_low": False, "all_aligned": False}

    # Center slope: how much the blended center has moved since the
    # previous snapshot, per real hour — the forecast's only source of
    # "momentum" for the base pull term (see FORECAST_SLOPE_FACTOR below).
    center_slope = 0.0
    if history:
        prev_levels = derive_levels(history[0])
        prev_hours_ago = max((now_utc - history[0]["bucket_epoch"]) / 3600.0, 1.0) if now_utc else BUCKET_HOURS_FALLBACK
        center_slope = (levels["center"] - prev_levels["center"]) / prev_hours_ago

    gamma_confirm_buffer = band_width * GAMMA_CONFIRM_BUFFER_PCT
    closes = [c["c"] for c in candles]
    confirmed_above, confirmed_below = _gamma_confirmation(closes, gamma_ref, gamma_confirm_buffer)
    momentum_bear, momentum_bull, sweep_boost = _momentum_override(candles, atr)

    put_delta_level = weighted_avg2(current["bpd_price"], current["spd_price"], 1.0, 1.0)
    call_delta_level = weighted_avg2(current["bcd_price"], current["scd_price"], 1.0, 1.0)

    def _strength_mult(abs_a, abs_b):
        norm = min(max(abs_a + abs_b, 0.0) / DELTA_ABS_NORMALIZER, 1.5)
        return max(min(1.0 + (norm - 0.50) * 0.30, 1.60), 0.60)

    put_strength_mult = _strength_mult(current["bpd_abs"], current["spd_abs"])
    call_strength_mult = _strength_mult(current["bcd_abs"], current["scd_abs"])

    # Option Cluster Structure Engine — how tightly top/low/gamma/BML/SMP
    # agree with each other right now (dispersion) and whether they moved
    # together since the previous snapshot (alignment); computed once and
    # applied uniformly per step, same as the other "computed from current
    # vs. history" signals above.
    dispersion = cluster_dispersion(top, low, levels["buyer_gamma"], levels["seller_gamma"], current["bml"], current["smp"])
    compression_score = 0.0
    expansion_score = 0.0
    cluster_direction_matches_body = None
    if history:
        prev_levels = derive_levels(history[0])
        prev_dispersion = cluster_dispersion(history[0]["top"], history[0]["low"], prev_levels["buyer_gamma"], prev_levels["seller_gamma"], history[0]["bml"], history[0]["smp"])
        if dispersion is not None and prev_dispersion is not None:
            dispersion_change = dispersion - prev_dispersion
            expansion_score = min(max(dispersion_change / max(CLUSTER_EXPANSION_THRESHOLD, 0.001), 0.0), 1.0)
        alignment, cluster_direction = cluster_alignment(
            {"top": top, "low": low, "buyer_gamma": levels["buyer_gamma"], "seller_gamma": levels["seller_gamma"], "bml": current["bml"], "smp": current["smp"]},
            {"top": history[0]["top"], "low": history[0]["low"], "buyer_gamma": prev_levels["buyer_gamma"], "seller_gamma": prev_levels["seller_gamma"], "bml": history[0]["bml"], "smp": history[0]["smp"]},
        )
    else:
        alignment, cluster_direction = 0.0, 1.0
    if dispersion is not None:
        compression_score = max((CLUSTER_COMPRESSED_THRESHOLD - dispersion) / max(CLUSTER_COMPRESSED_THRESHOLD, 0.001), 0.0)
    cluster_center_price = cluster_center(top, low, levels["buyer_gamma"], levels["seller_gamma"], current["bml"], current["smp"])
    cluster_directional_expansion = expansion_score > 0 and alignment >= CLUSTER_ALIGNMENT_THRESHOLD
    cluster_compressed = compression_score > 0

    points = []
    projected_open = index_price
    n_candles = int(round(hours_ahead / step_hours))
    for step in range(n_candles):
        hours_out = start_offset_hours + step * step_hours
        candle_dt_hour = (now_utc + hours_out * 3600.0) if now_utc else None
        if candle_dt_hour is not None:
            candle_dt = datetime.fromtimestamp(candle_dt_hour, tz=timezone.utc)
            hour_utc = candle_dt.hour
            is_weekend = candle_dt.weekday() >= 5  # Saturday=5, Sunday=6
        else:
            hour_utc = 12
            is_weekend = False
        sess = session_name(hour_utc)

        body_mult = SESSION_BODY_FACTOR[sess] * (WEEKEND_BODY_FACTOR if is_weekend else 1.0)
        atr_mult = SESSION_ATR_FACTOR[sess] * (WEEKEND_ATR_FACTOR if is_weekend else 1.0)
        shock_mult = SESSION_SHOCK_FACTOR[sess] * (WEEKEND_SHOCK_FACTOR if is_weekend else 1.0)
        threshold_mult = SESSION_THRESHOLD_FACTOR[sess] * (WEEKEND_THRESHOLD_FACTOR if is_weekend else 1.0)
        effective_atr = atr * atr_mult

        low_vol_regime = sess in ("Asia", "PostNY")
        high_vol_regime = sess in ("London", "Overlap", "NY")
        pull_mult = (LOW_VOL_PULL_FACTOR if low_vol_regime else HIGH_VOL_PULL_FACTOR if high_vol_regime else WEEKDAY_PULL_FACTOR)
        combined_body_mult = max(body_mult * pull_mult, 0.45)
        combined_wick_mult = max(atr_mult, 0.40)
        combined_shock_mult = max(shock_mult * (LOW_VOL_SHOCK_FACTOR if low_vol_regime else HIGH_VOL_SHOCK_FACTOR if high_vol_regime else WEEKDAY_SHOCK_FACTOR), 0.35)

        gamma_gap = levels["center"] - projected_open
        if consensus["consensus_direction"] != 0 and (
            (consensus["consensus_direction"] > 0 and gamma_gap < 0) or
            (consensus["consensus_direction"] < 0 and gamma_gap > 0)
        ):
            gamma_gap *= max(1.0 - consensus["consensus_strength"] * GAMMA_BAND_OPPOSING_MAGNET_DAMPING, 0.15)

        base_pull_impulse = (gamma_gap / band_width) * FORECAST_PULL_FACTOR * combined_body_mult
        slope_impulse = (center_slope / band_width) * FORECAST_SLOPE_FACTOR * combined_body_mult * (0.75 ** step)

        last_body = (candles[-1]["c"] - candles[-1]["o"]) if candles else 0.0
        current_body_impulse = (last_body / band_width) * FORECAST_BODY_FACTOR * (0.75 ** step) * combined_body_mult
        if momentum_bear or momentum_bull:
            current_body_impulse *= sweep_boost

        center_bias = (levels["center"] - projected_open) + slope_impulse * band_width + last_body * 0.25
        upper_curve_pull = (current["smp"] - projected_open) / band_width
        lower_curve_pull = (current["bml"] - projected_open) / band_width
        curve_extreme_impulse = (upper_curve_pull if center_bias >= 0 else lower_curve_pull) * FORECAST_CURVE_EXTREME_BODY_WEIGHT * combined_body_mult

        gb_impulse = 0.0
        if consensus["consensus_direction"] != 0:
            impulse_scale = GAMMA_BAND_CONFIRMED_TARGET_BOOST if consensus["all_aligned"] else 0.14
            gb_impulse = consensus["consensus_direction"] * consensus["consensus_strength"] * impulse_scale * combined_body_mult * (0.82 ** step)
            gb_impulse = max(min(gb_impulse, 0.22), -0.22)

        reclaim_impulse, reclaim_active, _reclaim_target = gamma_band_reclaim_bias(
            consensus, projected_open, closes[-1] if closes else projected_open, gamma_ref, low, top, band_width, step,
        )

        vega_impulse, vega_wick_mult, vega_body_mult, vega_expansion, vega_compression = vega_regime(
            current, projected_open, band_width, consensus["consensus_direction"], consensus["consensus_strength"], step,
        )

        delta_impulse, bear_delta_shock, bull_delta_shock = delta_shock_module(
            put_delta_level, call_delta_level, low, top, projected_open,
            closes[-1] if closes else projected_open, gamma_ref, confirmed_below, confirmed_above,
            band_width, put_strength_mult, call_strength_mult,
        )

        gamma_shock_impulse, bear_shock, bull_shock, shock_strength = gamma_shock_module(
            closes[-1] if closes else projected_open, candles[-1]["o"] if candles else projected_open,
            atr, low, top, band_width, gamma_ref, confirmed_below, confirmed_above,
            current["bml"], current["smp"], put_delta_level, call_delta_level,
        )

        mm_impulse, mm_upper_wick_boost, mm_lower_wick_boost = market_maker_gamma_contest(
            current, projected_open, band_width, combined_body_mult, step,
        )

        liquidity = liquidity_map_engine(
            current["lower_liq_price"], current["lower_liq_m"], current["upper_liq_price"], current["upper_liq_m"],
            projected_open, levels["center"], band_width, candles[-1] if candles else None, combined_body_mult, step,
        )
        if liquidity["has_map"] and not (momentum_bear or momentum_bull):
            # Momentum's own sweep boost already covers a real breakout;
            # apply liquidity's separately only when momentum itself isn't
            # already driving the current-body term, so the two don't
            # double-boost the same real candle.
            current_body_impulse *= liquidity["sweep_body_boost"]

        any_shock_active = bear_delta_shock or bull_delta_shock or bear_shock or bull_shock
        body_confidence = 1.0 * vega_body_mult
        wick_expansion = 1.0 * vega_wick_mult

        if consensus["consensus_direction"] != 0:
            body_confidence = min(body_confidence * (1.0 + consensus["consensus_strength"] * GAMMA_BAND_CONFIDENCE_BOOST * (1.0 if consensus["all_aligned"] else 0.65)), 1.0)
        elif consensus["conflict"]:
            body_confidence *= max(1.0 - consensus["conflict_strength"] * GAMMA_BAND_CONFLICT_BODY_DAMPING, 0.35)
            wick_expansion *= 1.0 + consensus["conflict_strength"] * GAMMA_BAND_CONFLICT_WICK_EXPANSION

        neutral_score = 0.0
        if not any_shock_active:
            neutral_score = gamma_neutral_score(projected_open, closes[-1] if closes else projected_open, gamma_ref, band_width, effective_atr)
            if neutral_score > 0:
                body_confidence *= max(1.0 - NEAR_GAMMA_BODY_DAMPING * neutral_score, 0.35)
                wick_expansion *= 1.0 + NEAR_GAMMA_WICK_EXPANSION * neutral_score

        # Option Cluster Structure Engine (computed once above, applied
        # every step): a compressed cluster (levels bunched tightly
        # together) damps the body and compresses the wick — nothing
        # decisive is happening; an aligned, expanding cluster whose
        # direction matches the candle's own body raises the confidence
        # floor instead, since several independent Greeks agreeing is a
        # stronger signal than any single one.
        if cluster_compressed:
            body_confidence *= 1.0 - compression_score * CLUSTER_COMPRESSION_BODY_DAMPING
            wick_expansion *= 1.0 - compression_score * CLUSTER_COMPRESSION_WICK_COMPRESSION
        if cluster_directional_expansion and cluster_center_price is not None:
            body_escaped = abs(projected_open - cluster_center_price) / band_width > max(dispersion or 0.0, 0.05)
            body_matches = (cluster_direction > 0 and projected_open >= (closes[-1] if closes else projected_open)) or \
                           (cluster_direction < 0 and projected_open < (closes[-1] if closes else projected_open))
            if body_escaped and body_matches:
                body_confidence = max(body_confidence, CLUSTER_BODY_CONFIDENCE_FLOOR)

        if momentum_bear or momentum_bull:
            body_confidence = max(body_confidence, MOMENTUM_BODY_CONFIDENCE_FLOOR)
            wick_expansion *= MOMENTUM_WICK_COMPRESSION

        if liquidity["has_map"]:
            body_direction_bear = projected_open >= (closes[-1] if closes else projected_open)
            body_direction_bull = not body_direction_bear
            liquidity_aligned = (liquidity["lower_dominant"] and body_direction_bear and projected_open < levels["center"]) or \
                                 (liquidity["upper_dominant"] and body_direction_bull and projected_open > levels["center"])
            if liquidity_aligned:
                body_confidence = max(body_confidence, LIQUIDITY_BODY_CONFIDENCE_FLOOR)
                wick_expansion *= 1.0 - liquidity["bias_abs"] * LIQUIDITY_ALIGNED_WICK_COMPRESSION
            elif liquidity["lower_dominant"] or liquidity["upper_dominant"]:
                wick_expansion *= 1.0 - liquidity["bias_abs"] * LIQUIDITY_OPPOSITE_WICK_COMPRESSION
            if liquidity["upper_swept_rejected"] or liquidity["lower_swept_rejected"]:
                body_confidence = max(body_confidence, LIQUIDITY_BODY_CONFIDENCE_FLOOR)
                wick_expansion *= LIQUIDITY_SWEEP_WICK_COMPRESSION

        forecast_impulse = (
            base_pull_impulse + slope_impulse + current_body_impulse + curve_extreme_impulse
            + gb_impulse + reclaim_impulse + vega_impulse + delta_impulse + gamma_shock_impulse + mm_impulse
            + liquidity["impulse"]
        ) * body_confidence
        impulse_limit = 0.85 if any_shock_active else 0.45
        forecast_impulse = max(min(forecast_impulse, impulse_limit), -impulse_limit)

        step_move = band_width * forecast_impulse
        if step == 0:
            first_move_cap = max(effective_atr * SESSION_FIRST_MOVE_ATR[sess], 1e-9)
            step_move = max(min(step_move, first_move_cap), -first_move_cap)

        projected_close = projected_open + step_move

        upper_target_sum, upper_target_weight = top, 1.0
        lower_target_sum, lower_target_weight = low, 1.0
        if consensus["confirms_top"]:
            boost = GAMMA_BAND_CONFIRMED_TARGET_BOOST * consensus["consensus_strength"] * (1.25 if consensus["all_aligned"] else 1.0)
            upper_target_sum += top * boost
            upper_target_weight += boost
        if consensus["confirms_low"]:
            boost = GAMMA_BAND_CONFIRMED_TARGET_BOOST * consensus["consensus_strength"] * (1.25 if consensus["all_aligned"] else 1.0)
            lower_target_sum += low * boost
            lower_target_weight += boost
        upper_target_sum += current["smp"] * FORECAST_CURVE_WICK_WEIGHT
        upper_target_weight += FORECAST_CURVE_WICK_WEIGHT
        lower_target_sum += current["bml"] * FORECAST_CURVE_WICK_WEIGHT
        lower_target_weight += FORECAST_CURVE_WICK_WEIGHT
        upper_target = max(upper_target_sum / upper_target_weight, max(projected_open, projected_close))
        lower_target = min(lower_target_sum / lower_target_weight, min(projected_open, projected_close))

        body_top = max(projected_open, projected_close)
        body_bottom = min(projected_open, projected_close)
        upper_room = max(upper_target - body_top, 0.0)
        lower_room = max(body_bottom - lower_target, 0.0)
        projected_bull = projected_close >= projected_open

        upper_wick = ((upper_room * FORECAST_WICK_FACTOR + effective_atr * FORECAST_ATR_FACTOR) * wick_expansion
                      if projected_bull else (upper_room * FORECAST_WICK_FACTOR * 0.5 + effective_atr * FORECAST_ATR_FACTOR * 0.5) * wick_expansion)
        lower_wick = ((lower_room * FORECAST_WICK_FACTOR * 0.5 + effective_atr * FORECAST_ATR_FACTOR * 0.5) * wick_expansion
                      if projected_bull else (lower_room * FORECAST_WICK_FACTOR + effective_atr * FORECAST_ATR_FACTOR) * wick_expansion)

        upper_wick *= 1.0 + min(mm_upper_wick_boost, 1.25)
        lower_wick *= 1.0 + min(mm_lower_wick_boost, 1.25)
        if consensus["consensus_direction"] != 0:
            cut = min(consensus["consensus_strength"] * GAMMA_BAND_OPPOSITE_WICK_COMPRESSION, 0.70)
            if consensus["consensus_direction"] > 0:
                lower_wick *= 1.0 - cut
            else:
                upper_wick *= 1.0 - cut

        projected_high = max(body_top + max(upper_wick, 0.0), body_top)
        projected_low = min(body_bottom - max(lower_wick, 0.0), body_bottom)

        mode = []
        if any_shock_active:
            mode.append("Shock")
        if consensus["all_aligned"]:
            mode.append("G-B Triple " + ("Bull" if consensus["consensus_direction"] > 0 else "Bear"))
        elif consensus["conflict"]:
            mode.append("Gamma-Band Conflict")
        if reclaim_active:
            mode.append("Reclaim")
        if vega_expansion:
            mode.append("Vega Expansion")
        elif vega_compression:
            mode.append("Vega Compression")
        if neutral_score > 0.15 and not any_shock_active:
            mode.append("Gamma Neutral")
        if liquidity["upper_swept_rejected"] or liquidity["lower_swept_rejected"]:
            mode.append("Liquidity Swept")
        elif liquidity["lower_dominant"] or liquidity["upper_dominant"]:
            mode.append("Liquidity Magnet")
        mode.append(sess)

        points.append({
            "hours": hours_out,
            "open": float(projected_open), "high": float(projected_high),
            "low": float(projected_low), "close": float(projected_close),
            "mode": " / ".join(mode),
        })
        projected_open = projected_close

    return points
