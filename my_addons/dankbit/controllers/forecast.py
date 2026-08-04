# -*- coding: utf-8 -*-
"""Forecast — port of Thales's "Thales Bands" Pine indicator's forecast-
candle engine onto Dankbit's own live-computed Greeks (no manual data entry:
see dankbit.forecast.snapshot for where the per-leg numbers below come
from). Pure functions only, mirroring delta.py/gamma.py's own style — no
Odoo/ORM access, no side effects.

Structural difference from the source Pine script: Thales's rows are
manually-dated CSV entries approaching a *known* future expiry, so its
forecast loop interpolates between a "selected" (current) and "previous"
row that are both real, already-known data points. Dankbit has no
foreknowledge of future Greek levels, so there is only ever one "current"
set of levels (top/low/gamma/curve/theta/delta) — it stays constant across
every forecast step, exactly like this addon's earlier, now-removed
GBM-based forecast engines' own gamma_band/sigma_annual inputs. The only thing that varies over real time is the
*history* used by the Gamma-Band Consensus engine's slope math (see
gamma_band_consensus below), which compares the current snapshot against
the last couple of persisted dankbit.forecast.snapshot rows.

Ported from a newer Pine version (v6.2.1) than the original port: added
Smart Role-Aware Synthetic Liquidity (smart_synthetic_liquidity —
computes liquidity levels straight from the per-leg Greeks, replacing
Dankbit's earlier manually-entered dankbit.liquidity.snapshot workflow,
removed once this automated equivalent existed), Gamma-Band Trend Lock
(locks against a one-candle countertrend flip while a strong 3-way-
aligned consensus holds, see the gb_counter_trend_locked block in
simulate_forecast), and the Wick-to-Body Acceptance Engine
(wick_to_body_acceptance — converts part of an asymmetric wick into the
candle body when one side's Market-Maker force dominance and
independent signals agree). The newest version's other additions (a
deeper Market-Maker engine with per-leg delta-confirmation/momentum-
factor/contest-detection, full regime-adaptive coefficient blending,
granular per-phase weekend move caps, theta breakout impulse, gamma-abs
pull/pin/shock multipliers, and confirmed-scenario/live-bar gating) were
deliberately deferred to a later pass — see each function's own
docstring for what it does and doesn't cover.
"""

import math
from collections import defaultdict
from datetime import datetime, timezone

from . import delta as delta_lib
from . import gamma as gamma_lib
from . import options as options_lib
from . import theta as theta_lib
from . import vega as vega_lib

# Fallback real-hours-ago gap between snapshots when `candles` is empty
# (no real klines available to anchor "now" to) — matches
# dankbit.forecast.snapshot.BUCKET_HOURS.
BUCKET_HOURS_FALLBACK = 4.0


def per_leg_greeks(STs, trades):
    """Per-leg gamma/delta/theta/vega extrema (price + abs value, matching
    the /<instrument>/zones page's info-overlay scaling: gamma value/1e6,
    delta value/10, theta value/1e4, vega value/100) for the 4 legs in
    `trades` — Thales's BCG/BPG/SCG/SPG, BCD/BPD/SCD/SPD, BCT/BPT/SCT/SPT,
    BCV/BPV/SCV/SPV plus their *Abs strength counterparts. `trades` should
    already be filtered to one expiry.

    A thin Pine-naming translation layer over options.per_leg_greeks() —
    the actual computation (which leg peaks/bottoms via which argmax/argmin,
    delta-saturation side per leg) lives there now, shared with
    chart_png_zones (main.py) and dankbit.bands's gamma_band/
    delta_band, so all three can never quietly disagree on these numbers
    for the same trades."""
    legs = options_lib.per_leg_greeks(STs, trades)
    lc, lp, sc, sp = legs["long_call"], legs["long_put"], legs["short_call"], legs["short_put"]

    return {
        "bcg_price": lc["gamma_price"], "bpg_price": lp["gamma_price"],
        "scg_price": sc["gamma_price"], "spg_price": sp["gamma_price"],
        "bcg_abs": abs(lc["gamma_value"]) / 1_000_000, "bpg_abs": abs(lp["gamma_value"]) / 1_000_000,
        "scg_abs": abs(sc["gamma_value"]) / 1_000_000, "spg_abs": abs(sp["gamma_value"]) / 1_000_000,
        "bcd_price": lc["delta_price"], "bpd_price": lp["delta_price"],
        "scd_price": sc["delta_price"], "spd_price": sp["delta_price"],
        "bcd_abs": abs(lc["delta_value"]) / 10, "bpd_abs": abs(lp["delta_value"]) / 10,
        "scd_abs": abs(sc["delta_value"]) / 10, "spd_abs": abs(sp["delta_value"]) / 10,
        "bct_price": lc["theta_price"], "bpt_price": lp["theta_price"],
        "sct_price": sc["theta_price"], "spt_price": sp["theta_price"],
        "bct_abs": abs(lc["theta_value"]) / 10_000, "bpt_abs": abs(lp["theta_value"]) / 10_000,
        "sct_abs": abs(sc["theta_value"]) / 10_000, "spt_abs": abs(sp["theta_value"]) / 10_000,
        "bcv_price": lc["vega_price"], "bpv_price": lp["vega_price"],
        "scv_price": sc["vega_price"], "spv_price": sp["vega_price"],
        "bcv_abs": abs(lc["vega_value"]) / 100, "bpv_abs": abs(lp["vega_value"]) / 100,
        "scv_abs": abs(sc["vega_value"]) / 100, "spv_abs": abs(sp["vega_value"]) / 100,
    }


# ============================================================
# Trade-weighted per-leg extraction — an ALTERNATIVE to per_leg_greeks()
# above, ported at the explicit request of Thales's original indicator
# author from his own live-dashboard reference implementation
# (ThalesFeatureBuilder.build/_max_level in his Python demo). Deliberately
# NOT a replacement for per_leg_greeks(): that function stays the single
# source of truth for /<instrument>/zones and dankbit.bands (see
# options.per_leg_greeks's own docstring), so this module's forecast-only
# use of the function below can never change what either of those surfaces
# displays. Gated behind res.config.settings' forecast_trade_weighted_greeks
# toggle (default off) — see dankbit.forecast.snapshot.compute_and_persist.
#
# Conceptual difference from per_leg_greeks(): that function asks "at which
# PRICE does this leg's AGGREGATED portfolio Greek curve peak/bottom",
# built from a synthetic price grid across every trade in the leg. This
# function instead asks "which SINGLE TRADE currently carries the largest
# weighted Greek force", and reports THAT TRADE'S OWN STRIKE as the level —
# his own reference implementation evaluates each instrument's greeks via a
# live Deribit order-book lookup (one REST call per instrument, evaluated
# at current mark/index price); this port evaluates the equivalent
# per-contract Black-Scholes greek analytically instead (this addon
# already treats bs_delta/bs_gamma/bs_theta/bs_vega as authoritative
# everywhere else), avoiding an extra live API round-trip per instrument
# on every forecast computation.
#
# Calibration note: the resulting *_abs values are NOT on the same numeric
# scale as per_leg_greeks()'s (dollar-Greek-curve-extremum-based) *_abs
# values — they're a trade-prominence score (amount * open_interest *
# |per-contract greek|) in bespoke units, scaled below only to land in a
# roughly similar order of magnitude. Every downstream engine that reads
# *_abs fields (vega_regime, market_maker_gamma_contest,
# smart_synthetic_liquidity, greek_flow, session_activity_score) was tuned
# against per_leg_greeks()'s own scale — per the source demo's own README
# ("outputs must be calibrated against real backtesting before analytical
# use"), this toggle should be validated against real forecast accuracy
# (see dankbit.forecast.log) before relying on it, not assumed correct.
# ============================================================
def trade_weighted_per_leg_greeks(trades, index_price, open_interest):
    """`trades` is any iterable of dankbit.trade-like records (needs
    .strike, .option_type, .direction, .amount, .iv, .name,
    .get_hours_to_expiry()); `open_interest` is a {instrument_name: OI}
    dict (see dankbit.trade.get_open_interest_by_currency) — an instrument
    missing from it falls back to a weight of 1 (unknown, not zero), same
    "missing means unknown" convention ChartController._gamma_by_strike()
    already uses for this same lookup. Returns the same 32-key dict shape
    as per_leg_greeks() (bcg_price/bcg_abs/.../spv_price/spv_abs)."""
    buckets = defaultdict(list)  # "{B|S}{C|P}{G|D|T|V}" -> [(strike, weight), ...]
    for t in trades:
        weight_base = max(t.amount or 0.0, 0.0001) * max(open_interest.get(t.name) or 1, 1)
        T = t.get_hours_to_expiry() / (24.0 * 365.0)
        sigma = (t.iv or 0.0) / 100.0
        gamma_w = abs(float(gamma_lib.bs_gamma(index_price, t.strike, T, 0.0, sigma))) * weight_base
        delta_w = abs(float(delta_lib.bs_delta(index_price, t.strike, T, 0.0, sigma, option_type=t.option_type))) * weight_base
        theta_w = abs(float(theta_lib.bs_theta(index_price, t.strike, T, 0.0, sigma, option_type=t.option_type))) * weight_base
        vega_w = abs(float(vega_lib.bs_vega(index_price, t.strike, T, 0.0, sigma))) * weight_base

        role = "B" if t.direction == "buy" else "S"
        cp = "C" if t.option_type == "call" else "P"
        buckets[f"{role}{cp}G"].append((t.strike, gamma_w))
        buckets[f"{role}{cp}D"].append((t.strike, delta_w))
        buckets[f"{role}{cp}T"].append((t.strike, theta_w))
        buckets[f"{role}{cp}V"].append((t.strike, vega_w))

    def max_level(key):
        rows = buckets.get(key)
        if not rows:
            return None, 0.0
        level, strength = max(rows, key=lambda row: abs(row[1]))
        return float(level), float(abs(strength))

    result = {}
    for bucket_role, out_role in (("BC", "bc"), ("BP", "bp"), ("SC", "sc"), ("SP", "sp")):
        for letter, out_letter, scale in (("G", "g", 1_000_000.0), ("D", "d", 10.0), ("T", "t", 10_000.0), ("V", "v", 100.0)):
            price, strength = max_level(f"{bucket_role}{letter}")
            result[f"{out_role}{out_letter}_price"] = price
            result[f"{out_role}{out_letter}_abs"] = strength / scale
    return result


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
GAMMA_CENTER_WEIGHT = 0.70
CURVE_CENTER_WEIGHT = 0.20
THETA_CENTER_WEIGHT = 0.10

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

# Per-asset dollar-Greek activity scale — "how big does a leg's own
# bcg_abs/bcd_abs/bct_abs/bcv_abs (etc.) need to be to count as strong
# conviction." Read by vega_regime, market_maker_gamma_contest,
# smart_synthetic_liquidity, greek_flow, and session_activity_score to
# turn a raw dollar-Greek magnitude into a clamped 0-3ish "activity"
# score that gates/scales those engines. These 4 constants are the
# BTC-calibrated defaults (per Thales dev feedback that the forecast
# reads as BTC-optimized — BTC options trade at strikes/notional roughly
# one to two orders of magnitude larger than ETH's, so a BTC-tuned
# DELTA_ABS_NORMALIZER=600 risked leaving ETH's raw values permanently
# under VEGA_MIN_ACTIVITY/VEGA_DOMINANCE_THRESHOLD, keeping those 5
# engines near-inert for ETH regardless of real market conviction).
# Deliberately promoted to the SAME "top-level tunable" exception
# derive_levels' GAMMA_CENTER_WEIGHT/CURVE_CENTER_WEIGHT/
# THETA_CENTER_WEIGHT already are: every nested helper below that reads
# one of these 4 takes its own `cfg=None` parameter and resolves via
# _cfg(cfg, "...") rather than the bare module constant, so
# simulate_forecast can pass its own per-asset cfg straight through
# (get_forecast_cfg(asset) on dankbit.forecast.snapshot builds that cfg
# from res.config.settings' eth_forecast_gamma_abs_normalizer/etc. for
# ETH, forecast_gamma_abs_normalizer/etc. for BTC — see that method and
# res_config_settings.py). Every other constant private to those same
# nested helpers (SMART_LIQ_*/SYNTH_LIQ_*/MM_*/VEGA_*/SESSION_ACTIVITY_*)
# stays un-cfg-overridable, same "top-level only" scoping rule as before
# — only these 4 became an exception, not a wholesale change to that rule.
GAMMA_ABS_NORMALIZER = 0.15
DELTA_ABS_NORMALIZER = 600.0
THETA_ABS_NORMALIZER = 500.0
VEGA_ABS_NORMALIZER = 2500.0


def derive_levels(current, cfg=None):
    """From one per_leg_greeks()+snapshot dict, derive the blended
    gamma/curve/theta averages and the weighted center Thales's forecast
    loop pulls price toward. `current` is a plain dict with the
    dankbit.forecast.snapshot field names (bcg_price, bcg_abs, bml, smp,
    top, low, ...).

    `buyer_gamma`/`seller_gamma`/`gamma_avg` are the PLAIN unweighted
    averages (buyerGammaVal/sellerGammaVal/avgGammaVal in the source
    script — (BCG+BPG)/2 and (SCG+SPG)/2), not the "Gamma Pressure
    Center" the script's "Calculate Gamma Pressure Center From S
    Strengths" toggle computes: tracing every use of that weighted value
    in the source script (v6.2.1) shows it only ever feeds a diagnostic
    label (diagGammaPressureCenter) — the real forecast center blend and
    the Gamma-Band Consensus slope math both read from the plain-average
    arrays (stepGammaAvg/selectedSimpleGammaAvg) instead. An earlier
    version of this port used the strength-weighted average here, which
    didn't match the reference script's actual (if seemingly
    unintentional) behavior; corrected to match after review.

    `cfg` (optional, see simulate_forecast/_cfg) can override
    GAMMA_CENTER_WEIGHT/CURVE_CENTER_WEIGHT/THETA_CENTER_WEIGHT — the
    only knob in this function exposed to res.config.settings' "Thales
    Forecast" section, added after the original Pine script's own author
    asked for the forecast to weight gamma more heavily so candles track
    the gamma reference line more closely. The Gamma-Curve Divergence
    Mode reweighting below (DIVERGENCE_*) is unaffected by cfg — that
    mode deliberately pulls gamma's weight back down toward parity with
    curve when the two disagree, a separate risk-management behavior
    from Thales's own source that isn't part of this request."""
    cfg = cfg or {}
    GAMMA_CENTER_WEIGHT = _cfg(cfg, "GAMMA_CENTER_WEIGHT")
    CURVE_CENTER_WEIGHT = _cfg(cfg, "CURVE_CENTER_WEIGHT")
    THETA_CENTER_WEIGHT = _cfg(cfg, "THETA_CENTER_WEIGHT")

    buyer_gamma = (current["bcg_price"] + current["bpg_price"]) / 2.0
    seller_gamma = (current["scg_price"] + current["spg_price"]) / 2.0
    gamma_avg = (buyer_gamma + seller_gamma) / 2.0

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
    2 persisted dankbit.forecast.snapshot rows (newest first). `*_hours_ago`
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
# Gamma-Band Term-Structure Bias — Dankbit-original, not ported from
# Thales's source script (which only ever holds one expiry's worth of
# manually-entered levels at a time, so it has no notion of a forward
# multi-expiry term structure at all). The chart's own violet dashed
# Gamma Band line (dankbit.bands.gamma_band, one persisted point per
# tracked expiry, connected point-to-point at each instrument's own
# expiration time — see CLAUDE.md's TradingView Chart Notes, Bands
# lines) visibly rises or falls going forward whenever the next tracked
# expiry's own gamma_band differs from the nearest one's. Added on user
# feedback that the forecast candles should visually track that same
# forward direction: when the dashed line is heading up toward the next
# tracked expiry, the forecast should trend up, and vice versa.
# gamma_band_term_slope() reads exactly those same two points (the
# line's forward-most segment) and turns their slope into a direction +
# strength, fed into simulate_forecast's impulse cascade as one more
# additive, step-decayed term alongside — not overriding — every other
# engine, so a strong shock/momentum/liquidity signal can still dominate
# a given step.
# ============================================================
GAMMA_BAND_TERM_SLOPE_REF_HOURS = 24.0
GAMMA_BAND_TERM_SLOPE_DEAD_ZONE = 0.03
GAMMA_BAND_TERM_SLOPE_FULL_STRENGTH = 0.20
GAMMA_BAND_TERM_SLOPE_IMPULSE_STRENGTH = 0.16
GAMMA_BAND_TERM_SLOPE_MAX_IMPULSE = 0.20


def gamma_band_term_slope(term_structure, band_width):
    """Forward slope of the on-chart Gamma Band dashed line. `term_structure`
    is main.py's _gamma_band_term_structure(asset) result: up to 2 dicts,
    soonest-expiring tracked instrument first, each {"gamma_band",
    "expiration_epoch"} — already restricted to expiries still ahead of
    now, so index 0/1 are exactly the two points forming the line's
    forward-most segment. Returns (direction -1/0/1, strength 0-1,
    normalized_slope). Inert ((0, 0.0, 0.0)) with fewer than 2 forward
    points — e.g. dankbit.bands hasn't tracked a 2nd expiry yet — same
    na-safe convention this module's other history-dependent signals use.

    Normalized per GAMMA_BAND_TERM_SLOPE_REF_HOURS real hours and by
    band_width, the same per-real-hour/band-width scale
    gamma_band_consensus's own slope() uses — just a longer reference
    window (24h vs. 8h), since this line's two points are typically days
    apart rather than hours."""
    if not term_structure or len(term_structure) < 2:
        return 0, 0.0, 0.0
    a, b = term_structure[0], term_structure[1]
    hours_gap = max((b["expiration_epoch"] - a["expiration_epoch"]) / 3600.0, 1.0)
    raw_slope = (b["gamma_band"] - a["gamma_band"]) / hours_gap
    normalized = raw_slope * GAMMA_BAND_TERM_SLOPE_REF_HOURS / max(band_width, 1e-9)
    direction = 1 if normalized > GAMMA_BAND_TERM_SLOPE_DEAD_ZONE else (-1 if normalized < -GAMMA_BAND_TERM_SLOPE_DEAD_ZONE else 0)
    strength = min(abs(normalized) / max(GAMMA_BAND_TERM_SLOPE_FULL_STRENGTH, 0.001), 1.0)
    return direction, strength, normalized


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
                        band_width, put_strength_mult, call_strength_mult, threshold_mult=1.0):
    """Returns (impulse, bear_shock, bull_shock). `threshold_mult` (session ×
    weekend, see simulate_forecast's `threshold_mult`) scales
    DELTA_SHOCK_THRESHOLD up on a weekend/quiet session so a shock needs a
    proportionally bigger break to qualify — was computed in
    simulate_forecast but never actually passed into this function until
    this fix; see that function's own docstring."""
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
    effective_threshold = DELTA_SHOCK_THRESHOLD * threshold_mult

    bear_confirm_ok = price_below_red or price_near_red
    bull_confirm_ok = price_above_green or price_near_green
    bear_shock = put_below and confirmed_below and bear_confirm_ok and (price_below_red or put_distance >= effective_threshold)
    bull_shock = call_above and confirmed_above and bull_confirm_ok and (price_above_green or call_distance >= effective_threshold)

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
                        gamma_ref, confirmed_below, confirmed_above, bml, smp, put_delta, call_delta,
                        threshold_mult=1.0):
    """Returns (impulse, bear_active, bull_active, strength). `threshold_mult`
    (session × weekend, see simulate_forecast's `threshold_mult`) scales
    SHOCK_BREAK_THRESHOLD up on a weekend/quiet session so a real close
    needs to break proportionally further past the band to activate — was
    computed in simulate_forecast but never actually passed into this
    function until this fix; see that function's own docstring."""
    body_size = abs(last_close - last_open)
    atr_safe = max(atr, 1e-9)
    strong_body = body_size >= atr_safe * SHOCK_BODY_ATR_MULT
    bear_break = (lower_band - last_close) / band_width if last_close < lower_band else 0.0
    bull_break = (last_close - upper_band) / band_width if last_close > upper_band else 0.0
    effective_break_threshold = SHOCK_BREAK_THRESHOLD * threshold_mult

    bear_active = last_close < lower_band and confirmed_below and (strong_body or bear_break >= effective_break_threshold)
    bull_active = last_close > upper_band and confirmed_above and (strong_body or bull_break >= effective_break_threshold)

    strength = 0.0
    impulse = 0.0
    if bear_active:
        strength = min(max(bear_break / max(effective_break_threshold, 0.01), body_size / max(atr_safe * SHOCK_BODY_ATR_MULT, 1e-9)), 2.0) / 2.0
        target = min(lower_band, bml, put_delta) if put_delta is not None else min(lower_band, bml)
        impulse = ((target - last_close) / band_width) * SHOCK_CONTINUATION_STRENGTH * (0.70 + strength * 0.60)
    elif bull_active:
        strength = min(max(bull_break / max(effective_break_threshold, 0.01), body_size / max(atr_safe * SHOCK_BODY_ATR_MULT, 1e-9)), 2.0) / 2.0
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


def vega_regime(current, projected_price, band_width, consensus_direction, consensus_strength, step, cfg=None):
    """Returns (impulse, wick_multiplier, expansion_active, compression_active).
    `cfg` (optional, see simulate_forecast/_cfg) can override VEGA_ABS_NORMALIZER
    — the per-asset scale for "how big is a big dollar-vega leg" (see
    GAMMA_ABS_NORMALIZER/DELTA_ABS_NORMALIZER/THETA_ABS_NORMALIZER/
    VEGA_ABS_NORMALIZER's own module-level comment for why this needed to
    become asset-aware). Every other constant in this function stays
    nested-helper-private, same "top-level only" rule derive_levels'
    own cfg parameter already established."""
    VEGA_ABS_NORMALIZER = _cfg(cfg, "VEGA_ABS_NORMALIZER")
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
# Thales's own mmMinimumOutcomeForce — a floor used to normalize the raw
# call/put breakout-vs-pin force magnitude into a 0-1 scale for
# wick_to_body_acceptance (see below); this port doesn't use it for the
# newest version's contest-detection/outcome-labeling, which is deferred.
MM_MINIMUM_OUTCOME_FORCE = 0.08


def market_maker_gamma_contest(current, projected_price, band_width, body_confidence_hint, step, cfg=None):
    """Returns a dict: impulse, upper_wick_boost, lower_wick_boost, plus
    upper_force_total/upper_net_force/lower_force_total/lower_net_force —
    the LOCAL (price-proximity-gated) call/put breakout-vs-pin forces,
    exposed for wick_to_body_acceptance. The newest Pine version's deeper
    enrichment of these forces (per-leg delta-confirmation multipliers,
    real-candle momentum factors, seller theta/rejection pin context,
    contest detection/outcome labeling) is deferred — this keeps the
    simpler proximity-only weighting the rest of this function already
    had. `cfg` (optional, see simulate_forecast/_cfg) can override
    GAMMA_ABS_NORMALIZER — the per-asset dollar-gamma scale; see that
    constant's own module-level comment. Every other constant here stays
    nested-helper-private."""
    GAMMA_ABS_NORMALIZER = _cfg(cfg, "GAMMA_ABS_NORMALIZER")

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
    return {
        "impulse": impulse, "upper_wick_boost": upper_wick_boost, "lower_wick_boost": lower_wick_boost,
        "upper_force_total": call_breakout + call_pin, "upper_net_force": call_breakout - call_pin,
        "lower_force_total": put_breakdown + put_support, "lower_net_force": put_breakdown - put_support,
    }


# ============================================================
# Smart Role-Aware Synthetic Liquidity (Thales's newest-version engine)
# — the source script's newer data format dropped its manually-typed
# CoinGlass lower/upper liquidity columns entirely and replaced them with
# levels computed straight from the same per-leg gamma/delta/theta/vega
# Greeks this module already has: seller call/put legs act in a "pin"
# role (short options a market-maker must defend, i.e. resistance/
# support), buyer call/put legs act in a "sweep" role (long options whose
# holders push price toward their strike), each weighted by that leg's
# own Abs strength. A seller call/put's delta contribution flips from
# pin to sweep once a real close has pushed convincingly (by
# SMART_LIQ_SELLER_DELTA_FLIP_BUFFER band-widths) past that leg's own
# delta level — the theory being that once the market maker's short
# option is decisively broken, their hedging flips them from a price
# anchor into a momentum accelerant. The raw pin/sweep blend is then
# nudged toward (if close to) or damped away from (if far past) the
# nearest zones-box edge (top/low), mirroring the source script's
# f_smartBandAdjustedLevel/f_smartBandAdjustedStrength. This fully
# replaces Dankbit's earlier manually-entered dankbit.liquidity.snapshot
# workflow (removed once this automated equivalent existed) — see
# simulate_forecast, which feeds this straight into liquidity_map_engine
# with no manual data involved.
# ============================================================
SMART_LIQ_SELLER_PIN_WEIGHT = 1.10
SMART_LIQ_BUYER_SWEEP_WEIGHT = 0.85
SMART_LIQ_SELLER_DELTA_PIN_WEIGHT = 0.45
SMART_LIQ_SELLER_DELTA_FLIP_WEIGHT = 0.80
SMART_LIQ_SELLER_DELTA_FLIP_BUFFER = 0.03
SMART_LIQ_BAND_MERGE_DISTANCE = 0.18
SMART_LIQ_BAND_REINFORCE_MULT = 1.15
SMART_LIQ_OUTSIDE_BAND_DISTANCE = 0.45
SMART_LIQ_OUTSIDE_BAND_BLEND = 0.35
SMART_LIQ_OUTSIDE_BAND_DAMPING = 0.35
SYNTH_LIQ_GAMMA_WEIGHT = 1.00
SYNTH_LIQ_DELTA_WEIGHT = 0.85
SYNTH_LIQ_THETA_WEIGHT = 0.45
SYNTH_LIQ_VEGA_WEIGHT = 0.65


def _synth_liq_weight(level, abs_val, normalizer, multiplier):
    if level is None or abs_val is None or normalizer <= 0:
        return 0.0
    return min(max(abs(abs_val) / normalizer, 0.0), 2.0) * multiplier


def _smart_band_adjusted_level(raw_level, band_level, band_width, merge_distance, outside_distance, outside_blend):
    if raw_level is None or band_level is None or band_width <= 0:
        return raw_level
    d = abs(raw_level - band_level) / band_width
    if d <= merge_distance:
        return weighted_avg2(raw_level, band_level, 0.35, 0.65)
    if d > outside_distance:
        return band_level + (raw_level - band_level) * outside_blend
    return raw_level


def _smart_band_adjusted_strength(raw_level, band_level, band_width, base_weight, merge_distance, band_mult, outside_distance, outside_damp):
    if raw_level is None or band_level is None or band_width <= 0:
        return base_weight
    d = abs(raw_level - band_level) / band_width
    if d <= merge_distance:
        return base_weight * band_mult
    if d > outside_distance:
        return base_weight * outside_damp
    return base_weight


def smart_synthetic_liquidity(current, top, low, band_width, role_close, cfg=None):
    """Returns {lower_liq_price, lower_liq_m, upper_liq_price, upper_liq_m}
    computed purely from `current`'s per-leg Greeks (see per_leg_greeks) —
    no manual data entry needed. `role_close` is the most recent real
    close, used only to detect a seller-delta "flip" from pin to sweep.
    `cfg` (optional, see simulate_forecast/_cfg) can override
    GAMMA_ABS_NORMALIZER/DELTA_ABS_NORMALIZER/THETA_ABS_NORMALIZER/
    VEGA_ABS_NORMALIZER — the per-asset dollar-Greek scale every weight
    below is built from; see those constants' own module-level comment.
    Every other constant here (SMART_LIQ_*/SYNTH_LIQ_*) stays
    nested-helper-private, same "top-level only" rule derive_levels'
    own cfg parameter already established."""
    GAMMA_ABS_NORMALIZER = _cfg(cfg, "GAMMA_ABS_NORMALIZER")
    DELTA_ABS_NORMALIZER = _cfg(cfg, "DELTA_ABS_NORMALIZER")
    THETA_ABS_NORMALIZER = _cfg(cfg, "THETA_ABS_NORMALIZER")
    VEGA_ABS_NORMALIZER = _cfg(cfg, "VEGA_ABS_NORMALIZER")
    upper_flip_level = max(top, current["scd_price"])
    lower_flip_level = min(low, current["spd_price"])
    seller_call_flipped = role_close > upper_flip_level + band_width * SMART_LIQ_SELLER_DELTA_FLIP_BUFFER
    seller_put_flipped = role_close < lower_flip_level - band_width * SMART_LIQ_SELLER_DELTA_FLIP_BUFFER

    up_pin_sum = up_pin_w = up_sweep_sum = up_sweep_w = 0.0
    low_pin_sum = low_pin_w = low_sweep_sum = low_sweep_w = 0.0

    # Upper side: seller-call pin roles, buyer-call sweep roles.
    sw = _synth_liq_weight(current["scg_price"], current["scg_abs"], GAMMA_ABS_NORMALIZER, SYNTH_LIQ_GAMMA_WEIGHT * SMART_LIQ_SELLER_PIN_WEIGHT)
    if sw > 0:
        up_pin_sum += current["scg_price"] * sw
        up_pin_w += sw
    sw = _synth_liq_weight(current["sct_price"], current["sct_abs"], THETA_ABS_NORMALIZER, SYNTH_LIQ_THETA_WEIGHT * SMART_LIQ_SELLER_PIN_WEIGHT)
    if sw > 0:
        up_pin_sum += current["sct_price"] * sw
        up_pin_w += sw
    sw = _synth_liq_weight(current["scv_price"], current["scv_abs"], VEGA_ABS_NORMALIZER, SYNTH_LIQ_VEGA_WEIGHT * SMART_LIQ_SELLER_PIN_WEIGHT * 0.70)
    if sw > 0:
        up_pin_sum += current["scv_price"] * sw
        up_pin_w += sw
    sw = _synth_liq_weight(current["scd_price"], current["scd_abs"], DELTA_ABS_NORMALIZER, SYNTH_LIQ_DELTA_WEIGHT * SMART_LIQ_SELLER_PIN_WEIGHT * SMART_LIQ_SELLER_DELTA_PIN_WEIGHT)
    if sw > 0:
        if seller_call_flipped:
            flip_sw = sw * SMART_LIQ_SELLER_DELTA_FLIP_WEIGHT
            up_sweep_sum += current["scd_price"] * flip_sw
            up_sweep_w += flip_sw
        else:
            up_pin_sum += current["scd_price"] * sw
            up_pin_w += sw
    sw = _synth_liq_weight(current["bcg_price"], current["bcg_abs"], GAMMA_ABS_NORMALIZER, SYNTH_LIQ_GAMMA_WEIGHT * SMART_LIQ_BUYER_SWEEP_WEIGHT)
    if sw > 0:
        up_sweep_sum += current["bcg_price"] * sw
        up_sweep_w += sw
    sw = _synth_liq_weight(current["bcd_price"], current["bcd_abs"], DELTA_ABS_NORMALIZER, SYNTH_LIQ_DELTA_WEIGHT * SMART_LIQ_BUYER_SWEEP_WEIGHT)
    if sw > 0:
        up_sweep_sum += current["bcd_price"] * sw
        up_sweep_w += sw
    sw = _synth_liq_weight(current["bcv_price"], current["bcv_abs"], VEGA_ABS_NORMALIZER, SYNTH_LIQ_VEGA_WEIGHT * SMART_LIQ_BUYER_SWEEP_WEIGHT)
    if sw > 0:
        up_sweep_sum += current["bcv_price"] * sw
        up_sweep_w += sw
    sw = _synth_liq_weight(current["bct_price"], current["bct_abs"], THETA_ABS_NORMALIZER, SYNTH_LIQ_THETA_WEIGHT * SMART_LIQ_BUYER_SWEEP_WEIGHT * 0.35)
    if sw > 0:
        up_sweep_sum += current["bct_price"] * sw
        up_sweep_w += sw

    # Lower side: seller-put pin roles, buyer-put sweep roles (mirror of above).
    sw = _synth_liq_weight(current["spg_price"], current["spg_abs"], GAMMA_ABS_NORMALIZER, SYNTH_LIQ_GAMMA_WEIGHT * SMART_LIQ_SELLER_PIN_WEIGHT)
    if sw > 0:
        low_pin_sum += current["spg_price"] * sw
        low_pin_w += sw
    sw = _synth_liq_weight(current["spt_price"], current["spt_abs"], THETA_ABS_NORMALIZER, SYNTH_LIQ_THETA_WEIGHT * SMART_LIQ_SELLER_PIN_WEIGHT)
    if sw > 0:
        low_pin_sum += current["spt_price"] * sw
        low_pin_w += sw
    sw = _synth_liq_weight(current["spv_price"], current["spv_abs"], VEGA_ABS_NORMALIZER, SYNTH_LIQ_VEGA_WEIGHT * SMART_LIQ_SELLER_PIN_WEIGHT * 0.70)
    if sw > 0:
        low_pin_sum += current["spv_price"] * sw
        low_pin_w += sw
    sw = _synth_liq_weight(current["spd_price"], current["spd_abs"], DELTA_ABS_NORMALIZER, SYNTH_LIQ_DELTA_WEIGHT * SMART_LIQ_SELLER_PIN_WEIGHT * SMART_LIQ_SELLER_DELTA_PIN_WEIGHT)
    if sw > 0:
        if seller_put_flipped:
            flip_sw = sw * SMART_LIQ_SELLER_DELTA_FLIP_WEIGHT
            low_sweep_sum += current["spd_price"] * flip_sw
            low_sweep_w += flip_sw
        else:
            low_pin_sum += current["spd_price"] * sw
            low_pin_w += sw
    sw = _synth_liq_weight(current["bpg_price"], current["bpg_abs"], GAMMA_ABS_NORMALIZER, SYNTH_LIQ_GAMMA_WEIGHT * SMART_LIQ_BUYER_SWEEP_WEIGHT)
    if sw > 0:
        low_sweep_sum += current["bpg_price"] * sw
        low_sweep_w += sw
    sw = _synth_liq_weight(current["bpd_price"], current["bpd_abs"], DELTA_ABS_NORMALIZER, SYNTH_LIQ_DELTA_WEIGHT * SMART_LIQ_BUYER_SWEEP_WEIGHT)
    if sw > 0:
        low_sweep_sum += current["bpd_price"] * sw
        low_sweep_w += sw
    sw = _synth_liq_weight(current["bpv_price"], current["bpv_abs"], VEGA_ABS_NORMALIZER, SYNTH_LIQ_VEGA_WEIGHT * SMART_LIQ_BUYER_SWEEP_WEIGHT)
    if sw > 0:
        low_sweep_sum += current["bpv_price"] * sw
        low_sweep_w += sw
    sw = _synth_liq_weight(current["bpt_price"], current["bpt_abs"], THETA_ABS_NORMALIZER, SYNTH_LIQ_THETA_WEIGHT * SMART_LIQ_BUYER_SWEEP_WEIGHT * 0.35)
    if sw > 0:
        low_sweep_sum += current["bpt_price"] * sw
        low_sweep_w += sw

    up_pin_level = up_pin_sum / up_pin_w if up_pin_w > 0 else None
    up_sweep_level = up_sweep_sum / up_sweep_w if up_sweep_w > 0 else None
    low_pin_level = low_pin_sum / low_pin_w if low_pin_w > 0 else None
    low_sweep_level = low_sweep_sum / low_sweep_w if low_sweep_w > 0 else None

    upper_raw = weighted_avg2(up_pin_level, up_sweep_level, up_pin_w, up_sweep_w)
    lower_raw = weighted_avg2(low_pin_level, low_sweep_level, low_pin_w, low_sweep_w)
    raw_upper_weight = up_pin_w + up_sweep_w
    raw_lower_weight = low_pin_w + low_sweep_w

    upper_price = _smart_band_adjusted_level(upper_raw, top, band_width, SMART_LIQ_BAND_MERGE_DISTANCE, SMART_LIQ_OUTSIDE_BAND_DISTANCE, SMART_LIQ_OUTSIDE_BAND_BLEND)
    lower_price = _smart_band_adjusted_level(lower_raw, low, band_width, SMART_LIQ_BAND_MERGE_DISTANCE, SMART_LIQ_OUTSIDE_BAND_DISTANCE, SMART_LIQ_OUTSIDE_BAND_BLEND)
    upper_m = _smart_band_adjusted_strength(upper_raw, top, band_width, raw_upper_weight, SMART_LIQ_BAND_MERGE_DISTANCE, SMART_LIQ_BAND_REINFORCE_MULT, SMART_LIQ_OUTSIDE_BAND_DISTANCE, SMART_LIQ_OUTSIDE_BAND_DAMPING) * LIQUIDITY_VOLUME_NORMALIZER
    lower_m = _smart_band_adjusted_strength(lower_raw, low, band_width, raw_lower_weight, SMART_LIQ_BAND_MERGE_DISTANCE, SMART_LIQ_BAND_REINFORCE_MULT, SMART_LIQ_OUTSIDE_BAND_DISTANCE, SMART_LIQ_OUTSIDE_BAND_DAMPING) * LIQUIDITY_VOLUME_NORMALIZER

    return {"lower_liq_price": lower_price, "lower_liq_m": lower_m, "upper_liq_price": upper_price, "upper_liq_m": upper_m}


# ============================================================
# Liquidity Map Engine (Thales's liquidity block) — resting-liquidity
# levels act as magnets/rejection points: whichever side has the
# stronger, closer liquidity pulls price its way (once dominant enough
# over the other side), and a real candle sweeping through a level and
# reversing away from it fires an extra rejection impulse. Fully inert
# (returns 0 impulse, no floors/compression) whenever neither side
# produced a level — same na-safe behavior the source script has for a
# blank liquidity column. Fed entirely by smart_synthetic_liquidity()
# (computed from this expiry's own Greeks, no manual data entry needed)
# — see simulate_forecast.
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
# Greek Flow Engine — Dankbit-original, not ported from Thales's source
# Pine script (see this module's top docstring; every other engine above
# either transliterates a named block from the script or, like Smart
# Synthetic Liquidity, replaces a manually-entered CSV column with an
# equivalent computed from these same Greeks). Motivated by a design
# conversation on making forecast candle BODIES track reality more
# closely: every engine above reacts only to the current scan's static
# Greek levels/magnitudes; nothing anywhere in this cascade asks whether
# a leg's own conviction is *growing or draining* scan-to-scan, or
# whether a synthetic liquidity level is drifting toward or away from
# price. This engine adds exactly that, from the same per-leg Abs fields
# already loaded on every dankbit.forecast.snapshot row — no new data
# source, no new model. Needs at least 1 real prior snapshot row
# (history[0]); inert (impulse 0.0, multipliers 1.0, fakeout_risk False)
# until one exists, same na-safe convention liquidity_map_engine uses
# for a blank liquidity column.
#
# Three signals, in the same directional-sign convention as
# market_maker_gamma_contest (positive = bullish contribution):
#   - Delta Flow: rate of change of bcd_abs/bpd_abs/scd_abs/spd_abs
#     (buyer conviction growing = directional push; seller conviction
#     growing = pin/resistance building = opposing push — exactly
#     market_maker_gamma_contest's call_breakout/call_pin split, just
#     applied to a *trend* instead of a magnitude) — feeds `impulse`.
#   - Vega Flow: same rate-of-change treatment for bcv_abs/bpv_abs vs.
#     scv_abs/spv_abs, but vega has no inherent bullish/bearish direction
#     (mirrors vega_regime's own framing) — growing buyer-side vega
#     conviction means expansion is *accelerating* (wider wick_mult);
#     growing seller-side conviction means compression is accelerating
#     (damped body_confidence_mult) — feeds body_confidence_mult/wick_mult
#     instead of impulse.
#   - Smart Liquidity Drift: re-evaluates smart_synthetic_liquidity() on
#     history[0]'s own Greeks (same top/low/band_width/last_close as the
#     live call — see greek_flow's docstring for why last_close is
#     deliberately reused rather than reconstructed for that historical
#     moment) and compares how far upper_liq_price/lower_liq_price sat
#     from last_close then vs. now; a level closing the gap contributes a
#     small impulse toward it. A level opening the gap contributes
#     nothing here — liquidity_map_engine's own proximity-based decay
#     already prices that in, so there's no need to double-penalize it.
#
# Fakeout Risk: when Delta Flow's direction disagrees with the most
# recent real candle's own body direction (option flow isn't confirming
# what price already did), body_confidence_mult is damped and wick_mult
# expanded — the same GAMMA_BAND_CONFLICT_*-style damping
# gamma_band_consensus's own conflict case already uses elsewhere in
# this cascade. When the two agree instead, a small confirmation boost
# is applied, mirroring GAMMA_BAND_CONFIDENCE_BOOST's role for consensus.
#
# Deliberately NOT cfg-overridable: constants private to a nested helper
# (vega_regime, market_maker_gamma_contest, smart_synthetic_liquidity's
# internals, ...) are already excluded from res.config.settings' "Thales
# Forecast" section by the "top-level only" scoping rule documented on
# simulate_forecast/CLAUDE.md; this engine, called the same way those
# are (once per step, from inside simulate_forecast's loop), follows
# the same rule rather than becoming a special case. The one exception is
# GREEK_FLOW_IMPULSE_WEIGHT (Greek Flow Priority Hybrid, see its own
# definition near the other FORECAST_* top-level tunables): a
# simulate_forecast-body-level multiplier on this function's own
# `impulse` return value, not a change to anything computed inside this
# function — so it's cfg-overridable under the same "top-level only" rule
# that excludes everything else here, not a special case either.
# ============================================================
GREEK_FLOW_REF_HOURS = 8.0  # same reference window gamma_band_consensus's slope() extrapolates to — keeps flow rates on the same normalized-per-8h scale as the rest of this file
# How many prior dankbit.forecast.snapshot rows get_forecast_points() supplies
# as `history` (see that method's history_records[:3]) — the closest fit here
# to Thales's own live-dashboard demo (SessionActivityRegimeEngine aside,
# see GreekFlowEngine.lookback_hours=4/required_snapshots=4 in his reference
# implementation), since BUCKET_HOURS=1 means 3 history rows span up to ~3
# real hours back, alongside `current` for 4 total points. Used both as the
# window's oldest-endpoint selector (see `prev` below — his own engine diffs
# the window's FIRST point against its LAST, not just the single most recent
# prior one) and as the denominator of the confidence/coverage scaling
# applied to the resulting impulse (his own `cov = len(win)/required_snapshots`).
GREEK_FLOW_LOOKBACK_SNAPSHOTS = 3
GREEK_FLOW_DELTA_IMPULSE_STRENGTH = 0.16
GREEK_FLOW_MAX_DELTA_IMPULSE = 0.18
GREEK_FLOW_LIQ_DRIFT_IMPULSE_STRENGTH = 0.10
GREEK_FLOW_MAX_LIQ_DRIFT_IMPULSE = 0.12
GREEK_FLOW_VEGA_WICK_STRENGTH = 0.35
GREEK_FLOW_VEGA_BODY_DAMPING = 0.20
GREEK_FLOW_DEAD_ZONE = 0.05
GREEK_FLOW_FAKEOUT_BODY_DAMPING = 0.35
GREEK_FLOW_FAKEOUT_WICK_EXPANSION = 0.30
GREEK_FLOW_CONFIRM_CONFIDENCE_BOOST = 0.20
GREEK_FLOW_DECAY = 0.84  # per-step impulse decay, same shape as vega_regime's 0.86**step/mm's 0.80**step — a fresh mid-range choice since this engine has no direct Thales analog to match


def greek_flow(current, history, synthetic_liq, top, low, band_width, last_close, last_open, step,
                session_flow_mult=1.0, session_fakeout_mult=1.0, cfg=None):
    """See the module-level "Greek Flow Engine" comment block above for
    the full design. `synthetic_liq` is the CURRENT smart_synthetic_liquidity()
    result — already computed once in simulate_forecast and passed in
    here rather than recomputed, same as every other caller of it.
    `cfg` (optional, see simulate_forecast/_cfg) can override
    DELTA_ABS_NORMALIZER/VEGA_ABS_NORMALIZER (used directly below by
    Delta/Vega Flow's own rate() calls) and is also forwarded to the
    internal smart_synthetic_liquidity() re-evaluation of `prev` (Smart
    Liquidity Drift), so a historical-moment recomputation never uses a
    different asset's normalizer than the live one it's compared
    against. Every other GREEK_FLOW_* constant here stays
    nested-helper-private, same "top-level only" rule derive_levels'
    own cfg parameter already established.
    `history` is newest-first; the OLDEST entry available (history[-1],
    up to GREEK_FLOW_LOOKBACK_SNAPSHOTS back) is diffed against `current`
    — matching Thales's own GreekFlowEngine, whose window compares its
    first point against its last rather than only ever the single most
    recent prior one — so this widens as more history accumulates, up to
    that cap, rather than always being a 1-bucket-wide comparison.
    `coverage` (len(history)/GREEK_FLOW_LOOKBACK_SNAPSHOTS, capped at 1)
    scales the resulting impulse down when fewer snapshots are available
    than the target window, mirroring his own `cov` confidence term.
    `session_flow_mult`/`session_fakeout_mult` are
    session_activity_regime()'s own greek_flow_mult/fakeout_mult for the
    current scan (his SessionActivityRegimeEngine feeds an equivalent
    session_greek_flow_multiplier into his GreekFlowEngine.compute()) —
    1.0/1.0 when no activity regime was computed, a no-op.

    Returns a dict: impulse (own directional push, decayed by step, kept
    fully separate from every other engine's own impulse term rather
    than reaching into e.g. liquidity_map_engine's own accumulator),
    body_confidence_mult, wick_mult, fakeout_risk (bool), plus
    delta_flow_signal/vega_flow_signal (unscaled, for diagnostics/mode)."""
    empty = {
        "impulse": 0.0, "body_confidence_mult": 1.0, "wick_mult": 1.0,
        "fakeout_risk": False, "delta_flow_signal": 0.0, "vega_flow_signal": 0.0,
    }
    if not history:
        return empty

    DELTA_ABS_NORMALIZER = _cfg(cfg, "DELTA_ABS_NORMALIZER")
    VEGA_ABS_NORMALIZER = _cfg(cfg, "VEGA_ABS_NORMALIZER")

    prev = history[min(len(history), GREEK_FLOW_LOOKBACK_SNAPSHOTS) - 1]
    coverage = min(len(history) / GREEK_FLOW_LOOKBACK_SNAPSHOTS, 1.0)
    hours_ago = max((current["bucket_epoch"] - prev["bucket_epoch"]) / 3600.0, 1.0)

    def rate(key, normalizer):
        return (current[key] - prev[key]) * GREEK_FLOW_REF_HOURS / hours_ago / normalizer

    buyer_call_delta_flow = rate("bcd_abs", DELTA_ABS_NORMALIZER)
    buyer_put_delta_flow = rate("bpd_abs", DELTA_ABS_NORMALIZER)
    seller_call_delta_flow = rate("scd_abs", DELTA_ABS_NORMALIZER)
    seller_put_delta_flow = rate("spd_abs", DELTA_ABS_NORMALIZER)
    delta_flow_signal = (buyer_call_delta_flow - seller_call_delta_flow) - (buyer_put_delta_flow - seller_put_delta_flow)

    buyer_vega_flow = rate("bcv_abs", VEGA_ABS_NORMALIZER) + rate("bpv_abs", VEGA_ABS_NORMALIZER)
    seller_vega_flow = rate("scv_abs", VEGA_ABS_NORMALIZER) + rate("spv_abs", VEGA_ABS_NORMALIZER)
    vega_flow_signal = buyer_vega_flow - seller_vega_flow

    prev_liq = smart_synthetic_liquidity(prev, top, low, band_width, last_close, cfg=cfg)
    liq_drift_impulse = 0.0
    for side_price, side_prev_price in (
        (synthetic_liq.get("upper_liq_price"), prev_liq.get("upper_liq_price")),
        (synthetic_liq.get("lower_liq_price"), prev_liq.get("lower_liq_price")),
    ):
        if side_price is None or side_prev_price is None:
            continue
        gap_now = abs(side_price - last_close)
        gap_prev = abs(side_prev_price - last_close)
        approach_rate = (gap_prev - gap_now) * GREEK_FLOW_REF_HOURS / hours_ago / band_width
        if approach_rate > 0:
            direction = 1.0 if side_price > last_close else -1.0
            liq_drift_impulse += direction * min(approach_rate, 1.0) * GREEK_FLOW_LIQ_DRIFT_IMPULSE_STRENGTH
    liq_drift_impulse = max(min(liq_drift_impulse, GREEK_FLOW_MAX_LIQ_DRIFT_IMPULSE), -GREEK_FLOW_MAX_LIQ_DRIFT_IMPULSE)

    delta_impulse = max(min(delta_flow_signal * GREEK_FLOW_DELTA_IMPULSE_STRENGTH, GREEK_FLOW_MAX_DELTA_IMPULSE), -GREEK_FLOW_MAX_DELTA_IMPULSE)
    impulse = (delta_impulse + liq_drift_impulse) * coverage * session_flow_mult * (GREEK_FLOW_DECAY ** step)

    body_confidence_mult = 1.0
    wick_mult = 1.0
    if vega_flow_signal > GREEK_FLOW_DEAD_ZONE:
        wick_mult = 1.0 + min(vega_flow_signal * GREEK_FLOW_VEGA_WICK_STRENGTH, 0.50)
    elif vega_flow_signal < -GREEK_FLOW_DEAD_ZONE:
        damping = min(abs(vega_flow_signal) * GREEK_FLOW_VEGA_BODY_DAMPING, 0.35)
        body_confidence_mult = max(1.0 - damping, 0.55)
        wick_mult = max(1.0 - min(abs(vega_flow_signal) * 0.15, 0.20), 0.80)

    price_direction = 1 if last_close > last_open else (-1 if last_close < last_open else 0)
    flow_direction = 1 if delta_flow_signal > GREEK_FLOW_DEAD_ZONE else (-1 if delta_flow_signal < -GREEK_FLOW_DEAD_ZONE else 0)
    fakeout_risk = price_direction != 0 and flow_direction != 0 and price_direction != flow_direction
    if fakeout_risk:
        body_confidence_mult *= 1.0 - GREEK_FLOW_FAKEOUT_BODY_DAMPING * session_fakeout_mult
        wick_mult *= 1.0 + GREEK_FLOW_FAKEOUT_WICK_EXPANSION * session_fakeout_mult
    elif flow_direction != 0 and flow_direction == price_direction:
        body_confidence_mult = min(body_confidence_mult * (1.0 + GREEK_FLOW_CONFIRM_CONFIDENCE_BOOST), 1.0)

    return {
        "impulse": impulse,
        "body_confidence_mult": max(body_confidence_mult, 0.30),
        "wick_mult": max(wick_mult, 0.30),
        "fakeout_risk": fakeout_risk,
        "delta_flow_signal": delta_flow_signal,
        "vega_flow_signal": vega_flow_signal,
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
# Wick-to-Body Acceptance Engine (Thales's newest-version addition) —
# converts part of an overly asymmetric wick into the candle body when
# one side's Market-Maker force clearly dominates and enough independent
# signals (force dominance, real-candle momentum, liquidity dominance)
# agree — modeling a market that "accepts" a directional move rather than
# merely wicking through it and snapping back. Simplified relative to the
# source script: uses only the LOCAL (price-proximity-gated) MM forces
# from market_maker_gamma_contest, not the newest version's richer
# "global pressure" blend (which needs the deferred per-leg delta-
# confirmation/momentum-factor MM enrichment — see that function).
# ============================================================
WICK_ABSORPTION_MIN_ASYMMETRY = 0.58
WICK_ABSORPTION_ACCEPTANCE_THRESHOLD = 0.10
WICK_ABSORPTION_FLIP_THRESHOLD = 0.72
WICK_ABSORPTION_NORMAL_MAX_SHARE = 0.30
WICK_ABSORPTION_WEEKEND_MAX_SHARE = 0.20
WICK_ABSORPTION_WEEKEND_RECOVERY_BONUS = 0.08
WICK_ABSORPTION_MOMENTUM_MAX_SHARE = 0.35
WICK_ABSORPTION_SHOCK_MAX_SHARE = 0.40
WICK_ABSORPTION_RESIDUAL_FLOOR = 0.45
WICK_ABSORPTION_MOMENTUM_WEIGHT = 0.20
WICK_ABSORPTION_LIQUIDITY_WEIGHT = 0.15


def wick_to_body_acceptance(
    projected_open, projected_close, upper_wick, lower_wick,
    mm_upper_force_total, mm_upper_net_force, mm_lower_force_total, mm_lower_net_force,
    momentum_bull, momentum_bear, last_close, last_open, atr,
    liquidity, price_structure_bull, price_structure_bear,
    bull_shock_active, bear_shock_active,
    gb_consensus_active, gb_all_aligned, gb_consensus_direction, gb_consensus_strength,
    is_weekend, gamma_neutral_active, any_shock_active,
):
    """Returns (adjusted_close, mode_text) — mode_text is "" when no
    absorption happened. `upper_wick`/`lower_wick` are this step's
    already-computed (pre-absorption) wick sizes."""
    body_top = max(projected_open, projected_close)
    body_bottom = min(projected_open, projected_close)
    pre_absorption_high = body_top + upper_wick
    pre_absorption_low = body_bottom - lower_wick
    total_wick = upper_wick + lower_wick
    upper_asym = upper_wick / total_wick if total_wick > 1e-9 else 0.5
    lower_asym = lower_wick / total_wick if total_wick > 1e-9 else 0.5
    denom = max(1.0 - WICK_ABSORPTION_MIN_ASYMMETRY, 0.01)
    upper_asym_score = min(max((upper_asym - WICK_ABSORPTION_MIN_ASYMMETRY) / denom, 0.0), 1.0)
    lower_asym_score = min(max((lower_asym - WICK_ABSORPTION_MIN_ASYMMETRY) / denom, 0.0), 1.0)

    upper_dominance = max(mm_upper_net_force, 0.0) / mm_upper_force_total if mm_upper_force_total > 0 else 0.0
    lower_dominance = max(mm_lower_net_force, 0.0) / mm_lower_force_total if mm_lower_force_total > 0 else 0.0
    upper_magnitude = min(mm_upper_force_total / max(MM_MINIMUM_OUTCOME_FORCE * 2.0, 0.01), 1.0)
    lower_magnitude = min(mm_lower_force_total / max(MM_MINIMUM_OUTCOME_FORCE * 2.0, 0.01), 1.0)

    body_atr_ratio = abs(last_close - last_open) / max(atr, 1e-9)
    upper_momentum_accept = 1.0 if momentum_bull else (min(body_atr_ratio / max(MOMENTUM_BODY_ATR_MULT, 0.01), 1.0) if last_close > last_open else 0.0)
    lower_momentum_accept = 1.0 if momentum_bear else (min(body_atr_ratio / max(MOMENTUM_BODY_ATR_MULT, 0.01), 1.0) if last_close < last_open else 0.0)

    upper_liq_accept = min(liquidity["bias_abs"], 1.0) if (liquidity["upper_dominant"] and price_structure_bull) else 0.0
    lower_liq_accept = min(liquidity["bias_abs"], 1.0) if (liquidity["lower_dominant"] and price_structure_bear) else 0.0

    base_weight = max(1.0 - WICK_ABSORPTION_MOMENTUM_WEIGHT - WICK_ABSORPTION_LIQUIDITY_WEIGHT, 0.0)
    upper_score = upper_dominance * upper_magnitude * (base_weight + upper_momentum_accept * WICK_ABSORPTION_MOMENTUM_WEIGHT + upper_liq_accept * WICK_ABSORPTION_LIQUIDITY_WEIGHT)
    lower_score = lower_dominance * lower_magnitude * (base_weight + lower_momentum_accept * WICK_ABSORPTION_MOMENTUM_WEIGHT + lower_liq_accept * WICK_ABSORPTION_LIQUIDITY_WEIGHT)

    if bull_shock_active and upper_dominance > 0:
        upper_score = max(upper_score, 0.85 * upper_magnitude)
    if bear_shock_active and lower_dominance > 0:
        lower_score = max(lower_score, 0.85 * lower_magnitude)
    if gamma_neutral_active and not any_shock_active:
        upper_score *= 0.65
        lower_score *= 0.65

    if gb_consensus_active:
        bonus = GAMMA_BAND_WEEKEND_WICK_ACCEPTANCE_BONUS if is_weekend else GAMMA_BAND_WEEKDAY_WICK_ACCEPTANCE_BONUS
        if gb_consensus_direction > 0:
            upper_score += gb_consensus_strength * bonus
        elif gb_consensus_direction < 0:
            lower_score += gb_consensus_strength * bonus

    upper_score = min(max(upper_score, 0.0), 1.0)
    lower_score = min(max(lower_score, 0.0), 1.0)

    upper_body_aligned = projected_close >= projected_open or upper_score >= WICK_ABSORPTION_FLIP_THRESHOLD
    lower_body_aligned = projected_close <= projected_open or lower_score >= WICK_ABSORPTION_FLIP_THRESHOLD
    upper_candidate = upper_asym >= WICK_ABSORPTION_MIN_ASYMMETRY and upper_score >= WICK_ABSORPTION_ACCEPTANCE_THRESHOLD and upper_body_aligned
    lower_candidate = lower_asym >= WICK_ABSORPTION_MIN_ASYMMETRY and lower_score >= WICK_ABSORPTION_ACCEPTANCE_THRESHOLD and lower_body_aligned
    upper_priority = upper_asym_score * upper_score if upper_candidate else 0.0
    lower_priority = lower_asym_score * lower_score if lower_candidate else 0.0
    absorb_upper = upper_candidate and upper_priority > lower_priority
    absorb_lower = lower_candidate and lower_priority >= upper_priority and lower_priority > 0

    if not (absorb_upper or absorb_lower):
        return projected_close, ""

    active_score = upper_score if absorb_upper else lower_score
    active_asym_score = upper_asym_score if absorb_upper else lower_asym_score
    active_momentum = momentum_bull if absorb_upper else momentum_bear
    active_shock = bull_shock_active if absorb_upper else bear_shock_active

    weekend_recovery_used = 0.0
    if is_weekend:
        weekend_recovery_used = active_score * WICK_ABSORPTION_WEEKEND_RECOVERY_BONUS
        if gb_all_aligned:
            weekend_recovery_used += gb_consensus_strength * GAMMA_BAND_WEEKEND_RECOVERY_BONUS

    max_share = (WICK_ABSORPTION_WEEKEND_MAX_SHARE + weekend_recovery_used) if is_weekend else WICK_ABSORPTION_NORMAL_MAX_SHARE
    if active_momentum:
        candidate = min(WICK_ABSORPTION_MOMENTUM_MAX_SHARE, WICK_ABSORPTION_WEEKEND_MAX_SHARE + WICK_ABSORPTION_WEEKEND_RECOVERY_BONUS + 0.05) if is_weekend else WICK_ABSORPTION_MOMENTUM_MAX_SHARE
        max_share = max(max_share, candidate)
    if active_shock:
        candidate = min(WICK_ABSORPTION_SHOCK_MAX_SHARE, WICK_ABSORPTION_WEEKEND_MAX_SHARE + WICK_ABSORPTION_WEEKEND_RECOVERY_BONUS + 0.10) if is_weekend else WICK_ABSORPTION_SHOCK_MAX_SHARE
        max_share = max(max_share, candidate)
    max_share = min(max_share, max(1.0 - WICK_ABSORPTION_RESIDUAL_FLOOR, 0.0))

    wick_body_share = max_share * math.sqrt(max(active_score * active_asym_score, 0.0))
    wick_body_share = min(max(wick_body_share, 0.0), max_share)

    upper_flip_distance = (projected_open - projected_close) if (absorb_upper and projected_close < projected_open) else 0.0
    lower_flip_distance = (projected_close - projected_open) if (absorb_lower and projected_close > projected_open) else 0.0
    if absorb_upper:
        shift = upper_flip_distance + upper_wick * wick_body_share
    else:
        shift = -(lower_flip_distance + lower_wick * wick_body_share)
    adjusted_close = projected_close + shift

    if absorb_upper:
        adjusted_close = min(adjusted_close, pre_absorption_high - upper_wick * WICK_ABSORPTION_RESIDUAL_FLOOR)
    if absorb_lower:
        adjusted_close = max(adjusted_close, pre_absorption_low + lower_wick * WICK_ABSORPTION_RESIDUAL_FLOOR)

    mode = "Wick→Body " + ("Up" if absorb_upper else "Down")
    return adjusted_close, mode


# ============================================================
# Session Activity Regime Engine — ported from Thales's own live-dashboard
# reference implementation (SessionActivityRegimeEngine in his Python demo,
# sent directly by the indicator's original author) at his request. This is
# independent of, and additive alongside, the fixed SESSION_BODY_FACTOR/etc.
# table below: that table damps/boosts by TIME of day (Asia is always
# quieter than the NY/London overlap); this engine instead damps/boosts by
# how unusually active THIS scan's actual Greek exposure is relative to the
# historical average for that same hour-of-day — a quiet Asia session and an
# unusually wild Asia session get the same SESSION_BODY_FACTOR entry, but
# different session_activity_regime() multipliers. Computed once per call
# (both the current score and the historical baseline are step-invariant)
# and applied uniformly across every forecast step, mirroring his own
# design: one scan produces one regime, fed into the whole candle path, not
# recomputed per step. His own engine does no scan-to-scan flow analysis of
# its own — the coupling with Greek Flow (greek_flow_mult/fakeout_mult) also
# mirrors his GreekFlowEngine.compute(session_greek_flow_multiplier=...).
# ============================================================
SESSION_ACTIVITY_LOW_THRESHOLD = 0.70
SESSION_ACTIVITY_HIGH_THRESHOLD = 1.35
SESSION_ACTIVITY_EXTREME_THRESHOLD = 2.25
SESSION_ACTIVITY_LOW_BODY_DAMPING = 0.65
SESSION_ACTIVITY_LOW_WICK_BOOST = 1.25
SESSION_ACTIVITY_LOW_FLOW_DAMPING = 0.70
SESSION_ACTIVITY_LOW_FAKEOUT_MULT = 1.25
SESSION_ACTIVITY_HIGH_BODY_BOOST = 1.20
SESSION_ACTIVITY_HIGH_FLOW_BOOST = 1.25
SESSION_ACTIVITY_HIGH_FAKEOUT_MULT = 0.90
SESSION_ACTIVITY_EXTREME_BODY_BOOST = 1.35
SESSION_ACTIVITY_EXTREME_WICK_BOOST = 1.10
SESSION_ACTIVITY_EXTREME_FLOW_BOOST_MULT = 1.15
SESSION_ACTIVITY_EXTREME_FAKEOUT_MULT = 0.80


def session_activity_score(current, synthetic_liq=None, cfg=None):
    """`cfg` (optional, see simulate_forecast/_cfg) can override
    GAMMA_ABS_NORMALIZER/DELTA_ABS_NORMALIZER/THETA_ABS_NORMALIZER/
    VEGA_ABS_NORMALIZER — see those constants' own module-level comment.
    Every other SESSION_ACTIVITY_* constant here stays nested-helper-
    private, same "top-level only" rule derive_levels' own cfg parameter
    already established.

    Thales's own activity-scoring formula (his `_score`/`f_score`: the
    sum of the 16 Abs strength fields plus half the Smart Liquidity
    upper/lower strengths), adapted to Dankbit's own field units: his raw
    BCGAbs/etc. are single-trade amount*OI*|greek| weights (his
    feature_builder.py), Dankbit's bcg_abs/etc. are aggregated dollar-Greek
    curve extrema divided by a display scale (see per_leg_greeks) — neither
    lands in the same numeric range his thresholds below assume, so each
    field is first normalized the same way vega_regime/market_maker_gamma_contest
    already do (dividing by that Greek's own *_ABS_NORMALIZER, clamped to
    [0, 3]) before summing, rather than reusing his raw-sum formula verbatim.
    `synthetic_liq` (smart_synthetic_liquidity()'s result, optional) adds
    the same 0.5-weighted liquidity-strength term his own score includes;
    omitted (not zeroed) when unavailable, e.g. a historical row computed
    before this addon tracked Smart Liquidity, so it doesn't artificially
    undercount that row's activity."""
    GAMMA_ABS_NORMALIZER = _cfg(cfg, "GAMMA_ABS_NORMALIZER")
    DELTA_ABS_NORMALIZER = _cfg(cfg, "DELTA_ABS_NORMALIZER")
    THETA_ABS_NORMALIZER = _cfg(cfg, "THETA_ABS_NORMALIZER")
    VEGA_ABS_NORMALIZER = _cfg(cfg, "VEGA_ABS_NORMALIZER")

    def norm(fields, normalizer):
        return sum(min(max((current.get(f) or 0.0) / normalizer, 0.0), 3.0) for f in fields)

    score = (
        norm(("bcg_abs", "bpg_abs", "scg_abs", "spg_abs"), GAMMA_ABS_NORMALIZER)
        + norm(("bcd_abs", "bpd_abs", "scd_abs", "spd_abs"), DELTA_ABS_NORMALIZER)
        + norm(("bct_abs", "bpt_abs", "sct_abs", "spt_abs"), THETA_ABS_NORMALIZER)
        + norm(("bcv_abs", "bpv_abs", "scv_abs", "spv_abs"), VEGA_ABS_NORMALIZER)
    )
    if synthetic_liq:
        upper_m = min(max((synthetic_liq.get("upper_liq_m") or 0.0) / LIQUIDITY_VOLUME_NORMALIZER, 0.0), 3.0)
        lower_m = min(max((synthetic_liq.get("lower_liq_m") or 0.0) / LIQUIDITY_VOLUME_NORMALIZER, 0.0), 3.0)
        score += 0.5 * (upper_m + lower_m)
    return score


def session_activity_regime(current_score, historical_scores):
    """Classifies `current_score` (session_activity_score()'s output for
    the current scan) as low/normal/high/extreme relative to the mean of
    `historical_scores` — same-hour-of-day historical activity scores, see
    dankbit.forecast.snapshot.session_activity_history(). Non-positive
    entries are dropped from the baseline (matching his own
    `[x for x in ... if x>0]` filter); `relative_score` falls back to 1.0
    (i.e. "normal") when no usable baseline exists yet, e.g. right after
    this addon starts tracking history for a given hour-of-day — same
    na-safe convention this module's other history-dependent signals use.
    Returns a dict: regime, body_mult, wick_mult, greek_flow_mult,
    fakeout_mult (feeds greek_flow()'s session_flow_mult/
    session_fakeout_mult), relative_score."""
    positive_history = [x for x in (historical_scores or []) if x and x > 0]
    baseline = sum(positive_history) / len(positive_history) if positive_history else None
    relative = current_score / baseline if baseline else 1.0

    if relative >= SESSION_ACTIVITY_EXTREME_THRESHOLD:
        return {
            "regime": "extreme", "body_mult": SESSION_ACTIVITY_EXTREME_BODY_BOOST,
            "wick_mult": SESSION_ACTIVITY_EXTREME_WICK_BOOST,
            "greek_flow_mult": SESSION_ACTIVITY_HIGH_FLOW_BOOST * SESSION_ACTIVITY_EXTREME_FLOW_BOOST_MULT,
            "fakeout_mult": SESSION_ACTIVITY_EXTREME_FAKEOUT_MULT, "relative_score": relative,
        }
    if relative >= SESSION_ACTIVITY_HIGH_THRESHOLD:
        return {
            "regime": "high", "body_mult": SESSION_ACTIVITY_HIGH_BODY_BOOST, "wick_mult": 1.0,
            "greek_flow_mult": SESSION_ACTIVITY_HIGH_FLOW_BOOST,
            "fakeout_mult": SESSION_ACTIVITY_HIGH_FAKEOUT_MULT, "relative_score": relative,
        }
    if relative <= SESSION_ACTIVITY_LOW_THRESHOLD:
        return {
            "regime": "low", "body_mult": SESSION_ACTIVITY_LOW_BODY_DAMPING,
            "wick_mult": SESSION_ACTIVITY_LOW_WICK_BOOST, "greek_flow_mult": SESSION_ACTIVITY_LOW_FLOW_DAMPING,
            "fakeout_mult": SESSION_ACTIVITY_LOW_FAKEOUT_MULT, "relative_score": relative,
        }
    return {"regime": "normal", "body_mult": 1.0, "wick_mult": 1.0, "greek_flow_mult": 1.0,
            "fakeout_mult": 1.0, "relative_score": relative}


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
FORECAST_BODY_FACTOR = 0.42
FORECAST_CURVE_EXTREME_BODY_WEIGHT = 0.26
FORECAST_WICK_FACTOR = 0.35
FORECAST_ATR_FACTOR = 0.30
FORECAST_CURVE_WICK_WEIGHT = 0.42
FORECAST_DISAGREEMENT_DAMPING = 0.28
FORECAST_DISAGREEMENT_WICK_BOOST = 0.10

# Greek Flow Priority Hybrid — per user/Thales-dev discussion, Greek Flow
# (see greek_flow() below) should become the PRIMARY driver of the 4H
# candle's body direction/close, while every structural engine below
# (Smart Liquidity, Gamma Band, Curve Engine, Session Activity, Vega
# Regime) keeps acting as a constraint on that flow rather than being
# removed — flow says which way the market just leaned; the structural
# engines say how far that lean is actually allowed to travel before
# it's absorbed into a wick instead of the body. This is the one
# top-level lever for that priority: it scales ONLY flow["impulse"]
# (greek_flow()'s own internal calibration constants stay nested-helper-
# private and un-cfg-overridable, same "top-level only" rule as
# vega_regime/market_maker_gamma_contest/smart_synthetic_liquidity's own
# internals) where it's added into forecast_impulse alongside every
# other engine's own impulse term — the other engines' impulses, and
# every body_confidence_mult/wick_mult constraint in the cascade, are
# untouched, so a strong shock/consensus/liquidity signal can still
# override a weak or fake-out flow reading. Defaults above 1.0 (unlike
# every other *_WEIGHT/*_FACTOR constant in this file, which default to
# an as-ported-from-Thales baseline) specifically to make Greek Flow the
# dominant additive voice by default, matching the "Priority Hybrid"
# conclusion of that discussion over the prior "one engine among many"
# weighting.
GREEK_FLOW_IMPULSE_WEIGHT = 1.75

GAMMA_BAND_OPPOSITE_WICK_COMPRESSION = 0.18
GAMMA_BAND_CONFIRMED_TARGET_BOOST = 0.55
GAMMA_BAND_CONFIDENCE_BOOST = 0.20
GAMMA_BAND_CONFLICT_BODY_DAMPING = 0.30
GAMMA_BAND_CONFLICT_WICK_EXPANSION = 0.25
GAMMA_BAND_OPPOSING_MAGNET_DAMPING = 0.60
GAMMA_BAND_WEEKDAY_WICK_ACCEPTANCE_BONUS = 0.26
GAMMA_BAND_WEEKEND_WICK_ACCEPTANCE_BONUS = 0.22
GAMMA_BAND_WEEKEND_RECOVERY_BONUS = 0.10

# Gamma-Band Trend Lock (Thales's newest-version addition) — once an
# all-3-aligned consensus is strong and a real close still sits on its
# "correct" side of the gamma reference, a single countertrend real
# candle is treated as a retest rather than a reversal: its body impulse
# is damped and the forecast's opposite-direction impulse is capped,
# until a real candle actually escapes back through the gamma reference
# with enough body-to-ATR force (GAMMA_BAND_COUNTER_ESCAPE_ATR) to earn
# the lock being lifted.
GAMMA_BAND_TREND_LOCK_STRENGTH = 0.55
GAMMA_BAND_COUNTER_BODY_DAMPING = 0.18
GAMMA_BAND_COUNTER_MAX_OPP_IMPULSE = 0.030
GAMMA_BAND_COUNTER_ESCAPE_ATR = 0.95

GAMMA_CONFIRM_BARS = 2
GAMMA_CONFIRM_BUFFER_PCT = 0.06

MOMENTUM_BODY_ATR_MULT = 0.85
LIQUIDITY_SWEEP_LOOKBACK = 6
MOMENTUM_BODY_CONFIDENCE_FLOOR = 0.62
MOMENTUM_WICK_COMPRESSION = 0.75
SWEEP_REJECTION_BODY_BOOST = 1.35


# ============================================================
# FlowImbalance damping, Zone Brake, and the Breakout Gate — added per
# Thales dev feedback (a chat transcript reviewing the 18-candle forecast
# against a live BTC-4aug26 example, 2026-08-03; see CLAUDE.md's Thales
# Forecast candles section for the full discussion). His critique: the
# engine correctly identified a bullish target (price above the
# equilibrium zone, Call-side Greek levels clustered higher) but rode
# there with too much confidence given the raw Long/Short trade count at
# 00:00 UTC was near-neutral (142 vs 148 in his example), and pushed
# straight through the Zone High resistance edge without any real
# confirmation that the level had actually been broken. His own stated
# first fix: reduce body intensity ~20% under a near-neutral Long/Short
# split, brake within ~0.5 ATR of the Zone High/Low edge, and gate
# crossing that edge behind confirmation — the 3 pieces below. Every
# other idea from that transcript (an explicit DirectionScore, a 3-stage
# target path, independent High/Low wick construction, per-horizon
# confidence decay, etc.) is deliberately deferred, scoped separately.
# ============================================================
FLOW_IMBALANCE_NEUTRAL_THRESHOLD = 0.05
FLOW_IMBALANCE_BODY_DAMPING = 0.20

ZONE_BRAKE_ATR_DISTANCE = 0.5
ZONE_BRAKE_MIN_BODY_MULT = 0.40

BREAKOUT_GATE_WICK_BLEED = 0.6


def flow_imbalance(long_count, short_count):
    """(Longs-Shorts)/(Longs+Shorts) — how one-sided the raw trade count
    behind this scan's window actually was, independent of what the
    Greek levels themselves suggest. A near-equal split (his example:
    142 long vs 148 short, |imbalance| ~= 0.02) means no real directional
    edge in who's actually trading, so simulate_forecast's own per-step
    loop shrinks body_confidence by FLOW_IMBALANCE_BODY_DAMPING whenever
    |imbalance| stays under FLOW_IMBALANCE_NEUTRAL_THRESHOLD, even when
    the target itself stays bullish/bearish. Returns None (not 0.0) when
    there's no trade-count data at all — e.g. a dankbit.forecast.snapshot
    row frozen before long_trade_count/short_trade_count existed — so the
    caller can skip the damping outright rather than reading "unknown" as
    "perfectly balanced"."""
    total = long_count + short_count
    if total <= 0:
        return None
    return (long_count - short_count) / total


def _zone_brake_mult(distance, effective_atr, atr_distance, min_mult):
    """How much to shrink body_confidence as the projected price nears a
    resistance/support level it's heading toward — 1.0 (no brake) once
    `distance` away is at least `atr_distance` ATRs, shrinking linearly
    down to `min_mult` as it closes in (floored there rather than at 0,
    so a genuinely strong signal can still produce some body right up
    against the level). `distance` negative (price already past the
    level) also returns 1.0 — the brake's job is only to slow an approach,
    not to keep dragging on a level that's already behind the candle."""
    if distance is None or distance < 0:
        return 1.0
    reference = max(effective_atr * atr_distance, 1e-9)
    if distance >= reference:
        return 1.0
    return max(distance / reference, min_mult)


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


def _cfg(cfg, name):
    """Returns cfg[name] if the caller supplied an override for it, else
    this module's own hardcoded constant of that name. Used by
    simulate_forecast's own top-level tunables (see its `cfg` parameter
    and res.config.settings' "Thales Forecast" section) and by the small,
    explicitly-documented set of nested-helper functions that also take
    their own `cfg=None` parameter (derive_levels' GAMMA_CENTER_WEIGHT/
    CURVE_CENTER_WEIGHT/THETA_CENTER_WEIGHT; vega_regime/
    market_maker_gamma_contest/smart_synthetic_liquidity/greek_flow/
    session_activity_score's GAMMA_ABS_NORMALIZER/DELTA_ABS_NORMALIZER/
    THETA_ABS_NORMALIZER/VEGA_ABS_NORMALIZER) — every other constant
    private to a nested helper function is NOT overridable this way and
    keeps using its bare module-level name directly; see each such
    function's own docstring for which exception (if any) applies."""
    return cfg[name] if cfg and name in cfg else globals()[name]


def simulate_forecast(index_price, sigma_annual, current, history, candles,
                        hours_ahead=72, step_hours=4, start_offset_hours=4, cfg=None,
                        gamma_band_term_structure=None, session_activity_history=None):
    """The full forecast-candle cascade, ported from Thales's per-step Pine
    loop. `current` and each row of `history` are dankbit.forecast.snapshot
    field dicts (newest history row first); `candles` are recent real 4h
    OHLC dicts, oldest first, with the last entry being the most recently
    fetched (still-forming) real candle. `gamma_band_term_structure` is
    main.py's _gamma_band_term_structure(asset) result — up to 2 forward
    dankbit.bands points (soonest-expiring tracked instrument first) feeding
    gamma_band_term_slope() (see that function/its own section header for
    why this exists — added so the forecast tracks the same forward
    direction as the chart's own dashed Gamma Band line).
    `session_activity_history` is dankbit.forecast.snapshot.
    session_activity_history()'s result for the current hour-of-day — same-
    hour-of-day historical Greek-activity scores feeding
    session_activity_regime() (see that function). None/empty means no
    regime multiplier is applied (equivalent to "normal", every multiplier
    1.0). Deterministic —
    unlike this
    addon's earlier, now-removed GBM-based forecast engines, there is no
    random component anywhere in this engine (matching the source script,
    which has none either); every
    candle is a direct function of the current Greeks and recent price
    action. Returns a list of dicts, one per step_hours-spaced candle out
    to hours_ahead: {hours, open, high, low, close, mode} — the only 6 keys
    forecast_json's public API surfaces — plus a set of diagnostic-only
    keys (forecast_impulse, the impulse_* per-engine breakdown, and the
    is_weekend/session_name/activity_regime/gb_*/any_shock_active/
    fakeout_risk/gamma_neutral_score/absorption_mode/effective_atr regime
    context) consumed only by dankbit.forecast.log, for attributing a
    candle's accuracy to a specific engine/regime after the fact rather
    than guessing from `mode`'s free-text label. `hours_ahead=72`
    (18 candles) — Thales's own `forecastCandleCount` default is 6 (24h),
    widened here since nothing in the engine assumes a specific cutoff
    (the per-step decay terms like `0.75 ** step`/`0.82 ** step`/etc. just
    keep tapering either way), so this is a plain parameter choice, not a
    structural limit.

    `cfg` (optional dict, keyed by the module constant's own name, e.g.
    {"FORECAST_PULL_FACTOR": 0.6, "SESSION_BODY_FACTOR": {...}}) lets a
    caller override this function's own top-level tunables — the ones
    referenced directly in this function's body, listed exhaustively
    right below — without touching this module's hardcoded defaults for
    anyone who doesn't pass a cfg (e.g. tests, or a call with cfg=None).
    Sourced from res.config.settings' "Thales Forecast" section by
    main.py's forecast_json, falling back to these same hardcoded
    defaults for any setting left unset. Constants private to nested
    helper functions (vega_regime, market_maker_gamma_contest, cluster_*,
    smart_synthetic_liquidity's internals, delta_shock_module,
    gamma_shock_module, etc.) are NOT overridable via cfg — see the
    "Top-level only" scoping decision in CLAUDE.md's Forecast candles
    section."""
    cfg = cfg or {}
    FORECAST_PULL_FACTOR = _cfg(cfg, "FORECAST_PULL_FACTOR")
    FORECAST_SLOPE_FACTOR = _cfg(cfg, "FORECAST_SLOPE_FACTOR")
    FORECAST_BODY_FACTOR = _cfg(cfg, "FORECAST_BODY_FACTOR")
    FORECAST_CURVE_EXTREME_BODY_WEIGHT = _cfg(cfg, "FORECAST_CURVE_EXTREME_BODY_WEIGHT")
    FORECAST_WICK_FACTOR = _cfg(cfg, "FORECAST_WICK_FACTOR")
    FORECAST_ATR_FACTOR = _cfg(cfg, "FORECAST_ATR_FACTOR")
    FORECAST_CURVE_WICK_WEIGHT = _cfg(cfg, "FORECAST_CURVE_WICK_WEIGHT")
    GREEK_FLOW_IMPULSE_WEIGHT = _cfg(cfg, "GREEK_FLOW_IMPULSE_WEIGHT")
    # Only read directly here for _strength_mult's own use below — every
    # other consumer of these 4 (vega_regime, market_maker_gamma_contest,
    # smart_synthetic_liquidity, greek_flow, session_activity_score)
    # resolves its own copy via cfg=cfg passed straight through, same
    # pattern derive_levels' own cfg parameter already uses.
    DELTA_ABS_NORMALIZER = _cfg(cfg, "DELTA_ABS_NORMALIZER")
    GAMMA_BAND_OPPOSITE_WICK_COMPRESSION = _cfg(cfg, "GAMMA_BAND_OPPOSITE_WICK_COMPRESSION")
    GAMMA_BAND_CONFIRMED_TARGET_BOOST = _cfg(cfg, "GAMMA_BAND_CONFIRMED_TARGET_BOOST")
    GAMMA_BAND_CONFIDENCE_BOOST = _cfg(cfg, "GAMMA_BAND_CONFIDENCE_BOOST")
    GAMMA_BAND_CONFLICT_BODY_DAMPING = _cfg(cfg, "GAMMA_BAND_CONFLICT_BODY_DAMPING")
    GAMMA_BAND_CONFLICT_WICK_EXPANSION = _cfg(cfg, "GAMMA_BAND_CONFLICT_WICK_EXPANSION")
    GAMMA_BAND_OPPOSING_MAGNET_DAMPING = _cfg(cfg, "GAMMA_BAND_OPPOSING_MAGNET_DAMPING")
    GAMMA_BAND_TREND_LOCK_STRENGTH = _cfg(cfg, "GAMMA_BAND_TREND_LOCK_STRENGTH")
    GAMMA_BAND_COUNTER_BODY_DAMPING = _cfg(cfg, "GAMMA_BAND_COUNTER_BODY_DAMPING")
    GAMMA_BAND_COUNTER_MAX_OPP_IMPULSE = _cfg(cfg, "GAMMA_BAND_COUNTER_MAX_OPP_IMPULSE")
    GAMMA_BAND_COUNTER_ESCAPE_ATR = _cfg(cfg, "GAMMA_BAND_COUNTER_ESCAPE_ATR")
    GAMMA_BAND_TERM_SLOPE_IMPULSE_STRENGTH = _cfg(cfg, "GAMMA_BAND_TERM_SLOPE_IMPULSE_STRENGTH")
    GAMMA_BAND_TERM_SLOPE_MAX_IMPULSE = _cfg(cfg, "GAMMA_BAND_TERM_SLOPE_MAX_IMPULSE")
    GAMMA_CONFIRM_BUFFER_PCT = _cfg(cfg, "GAMMA_CONFIRM_BUFFER_PCT")
    CLUSTER_ALIGNMENT_THRESHOLD = _cfg(cfg, "CLUSTER_ALIGNMENT_THRESHOLD")
    CLUSTER_BODY_CONFIDENCE_FLOOR = _cfg(cfg, "CLUSTER_BODY_CONFIDENCE_FLOOR")
    CLUSTER_COMPRESSED_THRESHOLD = _cfg(cfg, "CLUSTER_COMPRESSED_THRESHOLD")
    CLUSTER_COMPRESSION_BODY_DAMPING = _cfg(cfg, "CLUSTER_COMPRESSION_BODY_DAMPING")
    CLUSTER_COMPRESSION_WICK_COMPRESSION = _cfg(cfg, "CLUSTER_COMPRESSION_WICK_COMPRESSION")
    CLUSTER_EXPANSION_THRESHOLD = _cfg(cfg, "CLUSTER_EXPANSION_THRESHOLD")
    LIQUIDITY_ALIGNED_WICK_COMPRESSION = _cfg(cfg, "LIQUIDITY_ALIGNED_WICK_COMPRESSION")
    LIQUIDITY_BODY_CONFIDENCE_FLOOR = _cfg(cfg, "LIQUIDITY_BODY_CONFIDENCE_FLOOR")
    LIQUIDITY_OPPOSITE_WICK_COMPRESSION = _cfg(cfg, "LIQUIDITY_OPPOSITE_WICK_COMPRESSION")
    LIQUIDITY_SWEEP_WICK_COMPRESSION = _cfg(cfg, "LIQUIDITY_SWEEP_WICK_COMPRESSION")
    MOMENTUM_BODY_CONFIDENCE_FLOOR = _cfg(cfg, "MOMENTUM_BODY_CONFIDENCE_FLOOR")
    MOMENTUM_WICK_COMPRESSION = _cfg(cfg, "MOMENTUM_WICK_COMPRESSION")
    NEAR_GAMMA_BODY_DAMPING = _cfg(cfg, "NEAR_GAMMA_BODY_DAMPING")
    NEAR_GAMMA_WICK_EXPANSION = _cfg(cfg, "NEAR_GAMMA_WICK_EXPANSION")
    HIGH_VOL_PULL_FACTOR = _cfg(cfg, "HIGH_VOL_PULL_FACTOR")
    LOW_VOL_PULL_FACTOR = _cfg(cfg, "LOW_VOL_PULL_FACTOR")
    WEEKDAY_PULL_FACTOR = _cfg(cfg, "WEEKDAY_PULL_FACTOR")
    HIGH_VOL_SHOCK_FACTOR = _cfg(cfg, "HIGH_VOL_SHOCK_FACTOR")
    LOW_VOL_SHOCK_FACTOR = _cfg(cfg, "LOW_VOL_SHOCK_FACTOR")
    WEEKDAY_SHOCK_FACTOR = _cfg(cfg, "WEEKDAY_SHOCK_FACTOR")
    WEEKEND_ATR_FACTOR = _cfg(cfg, "WEEKEND_ATR_FACTOR")
    WEEKEND_BODY_FACTOR = _cfg(cfg, "WEEKEND_BODY_FACTOR")
    WEEKEND_SHOCK_FACTOR = _cfg(cfg, "WEEKEND_SHOCK_FACTOR")
    BUCKET_HOURS_FALLBACK = _cfg(cfg, "BUCKET_HOURS_FALLBACK")
    SESSION_BODY_FACTOR = _cfg(cfg, "SESSION_BODY_FACTOR")
    SESSION_ATR_FACTOR = _cfg(cfg, "SESSION_ATR_FACTOR")
    SESSION_SHOCK_FACTOR = _cfg(cfg, "SESSION_SHOCK_FACTOR")
    SESSION_FIRST_MOVE_ATR = _cfg(cfg, "SESSION_FIRST_MOVE_ATR")
    FLOW_IMBALANCE_NEUTRAL_THRESHOLD = _cfg(cfg, "FLOW_IMBALANCE_NEUTRAL_THRESHOLD")
    FLOW_IMBALANCE_BODY_DAMPING = _cfg(cfg, "FLOW_IMBALANCE_BODY_DAMPING")
    ZONE_BRAKE_ATR_DISTANCE = _cfg(cfg, "ZONE_BRAKE_ATR_DISTANCE")
    ZONE_BRAKE_MIN_BODY_MULT = _cfg(cfg, "ZONE_BRAKE_MIN_BODY_MULT")
    BREAKOUT_GATE_WICK_BLEED = _cfg(cfg, "BREAKOUT_GATE_WICK_BLEED")

    levels = derive_levels(current, cfg)
    band_width = levels["band_width"]
    top, low = current["top"], current["low"]
    gamma_ref = levels["gamma_avg"]

    now_utc = candles[-1]["t"] / 1000.0 if candles else None
    atr = _atr14(candles) or (index_price * sigma_annual * math.sqrt(4.0 / (24.0 * 365.0)))

    # Gamma-Band Consensus needs 2 real historical points besides "now".
    consensus = None
    if len(history) >= 2:
        prev, prev_prev = history[0], history[1]
        prev_levels = derive_levels(prev, cfg)
        prev_prev_levels = derive_levels(prev_prev, cfg)
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

    # FlowImbalance — step-invariant, computed once from `current`'s own
    # raw Long/Short trade count (see flow_imbalance()'s own docstring).
    flow_imb = flow_imbalance(current["long_trade_count"], current["short_trade_count"])

    # Center slope: how much the blended center has moved since the
    # previous snapshot, per real hour — the forecast's only source of
    # "momentum" for the base pull term (see FORECAST_SLOPE_FACTOR below).
    center_slope = 0.0
    if history:
        prev_levels = derive_levels(history[0], cfg)
        prev_hours_ago = max((now_utc - history[0]["bucket_epoch"]) / 3600.0, 1.0) if now_utc else BUCKET_HOURS_FALLBACK
        center_slope = (levels["center"] - prev_levels["center"]) / prev_hours_ago

    gamma_confirm_buffer = band_width * GAMMA_CONFIRM_BUFFER_PCT
    closes = [c["c"] for c in candles]
    confirmed_above, confirmed_below = _gamma_confirmation(closes, gamma_ref, gamma_confirm_buffer)
    momentum_bear, momentum_bull, sweep_boost = _momentum_override(candles, atr)

    # Gamma-Band Trend Lock — computed once, from the current consensus and
    # the single most recent real candle (both step-invariant), same as
    # consensus/momentum above.
    last_close = closes[-1] if closes else index_price
    last_open = candles[-1]["o"] if candles else index_price
    current_body_atr_ratio = abs(last_close - last_open) / max(atr, 1e-9)
    gb_strong_trend_lock = consensus["all_aligned"] and consensus["consensus_strength"] >= GAMMA_BAND_TREND_LOCK_STRENGTH
    gb_locked_bear_context = gb_strong_trend_lock and consensus["consensus_direction"] < 0 and last_close < gamma_ref - gamma_confirm_buffer
    gb_locked_bull_context = gb_strong_trend_lock and consensus["consensus_direction"] > 0 and last_close > gamma_ref + gamma_confirm_buffer
    gb_counter_body_against_consensus = (gb_locked_bear_context and last_close > last_open) or (gb_locked_bull_context and last_close < last_open)
    gb_counter_gamma_escape = (
        (gb_locked_bear_context and last_close > gamma_ref + gamma_confirm_buffer) or
        (gb_locked_bull_context and last_close < gamma_ref - gamma_confirm_buffer)
    )
    gb_counter_trend_escape = gb_counter_body_against_consensus and current_body_atr_ratio >= GAMMA_BAND_COUNTER_ESCAPE_ATR and gb_counter_gamma_escape
    gb_counter_trend_locked = gb_counter_body_against_consensus and not gb_counter_trend_escape
    if gb_counter_trend_locked:
        momentum_bear = False
        momentum_bull = False

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
        prev_levels = derive_levels(history[0], cfg)
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

    # Smart Role-Aware Synthetic Liquidity — computed once (all its inputs
    # are step-invariant).
    synthetic_liq = smart_synthetic_liquidity(current, top, low, band_width, last_close, cfg=cfg)
    lower_liq_price, lower_liq_m = synthetic_liq["lower_liq_price"], synthetic_liq["lower_liq_m"]
    upper_liq_price, upper_liq_m = synthetic_liq["upper_liq_price"], synthetic_liq["upper_liq_m"]

    # Session Activity Regime — computed once (current score + historical
    # baseline are both step-invariant), applied uniformly across every
    # step below, same as his own SessionActivityRegimeEngine feeding one
    # multiplier set into the whole forecast run.
    activity = session_activity_regime(session_activity_score(current, synthetic_liq, cfg=cfg), session_activity_history)

    # Gamma-Band Term-Structure Bias — computed once (both points, and thus
    # the slope between them, are step-invariant).
    term_direction, term_strength, _term_slope_norm = gamma_band_term_slope(gamma_band_term_structure, band_width)

    points = []
    projected_open = index_price
    n_candles = int(round(hours_ahead / step_hours))
    # Weekend move-cap state (see the WEEKEND_PER_CANDLE_MOVE_ATR block
    # below) — weekend_started tracks whether we're already past the first
    # weekend candle (so FIRST_WEEKEND_CANDLE_DAMPENING/
    # MAX_FIRST_WEEKEND_MOVE_ATR only apply once per weekend, not every
    # step), weekend_cumulative_move tracks the running total |move| since
    # the weekend started, reset the moment a step lands back on a weekday.
    weekend_started = False
    weekend_cumulative_move = 0.0
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
        combined_body_mult = max(body_mult * pull_mult * activity["body_mult"], 0.45)
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
        if gb_counter_trend_locked:
            current_body_impulse *= GAMMA_BAND_COUNTER_BODY_DAMPING

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
            cfg=cfg,
        )

        delta_impulse, bear_delta_shock, bull_delta_shock = delta_shock_module(
            put_delta_level, call_delta_level, low, top, projected_open,
            closes[-1] if closes else projected_open, gamma_ref, confirmed_below, confirmed_above,
            band_width, put_strength_mult, call_strength_mult, threshold_mult=threshold_mult,
        )
        # combined_shock_mult (session x weekend x low/high-vol regime) was
        # already computed above but never actually applied anywhere —
        # confirmed dead weight before this fix, which is why a Friday-
        # evening/weekend shock fired at full weekday strength. Scaled here
        # rather than inside delta_shock_module so both its full-shock and
        # partial-drift branches get the same session-aware damping.
        delta_impulse *= combined_shock_mult

        gamma_shock_impulse, bear_shock, bull_shock, shock_strength = gamma_shock_module(
            closes[-1] if closes else projected_open, candles[-1]["o"] if candles else projected_open,
            atr, low, top, band_width, gamma_ref, confirmed_below, confirmed_above,
            current["bml"], current["smp"], put_delta_level, call_delta_level, threshold_mult=threshold_mult,
        )
        gamma_shock_impulse *= combined_shock_mult

        mm = market_maker_gamma_contest(current, projected_open, band_width, combined_body_mult, step, cfg=cfg)
        mm_impulse = mm["impulse"]
        mm_upper_wick_boost = mm["upper_wick_boost"]
        mm_lower_wick_boost = mm["lower_wick_boost"]

        liquidity = liquidity_map_engine(
            lower_liq_price, lower_liq_m, upper_liq_price, upper_liq_m,
            projected_open, levels["center"], band_width, candles[-1] if candles else None, combined_body_mult, step,
        )
        if liquidity["has_map"] and not (momentum_bear or momentum_bull):
            # Momentum's own sweep boost already covers a real breakout;
            # apply liquidity's separately only when momentum itself isn't
            # already driving the current-body term, so the two don't
            # double-boost the same real candle.
            current_body_impulse *= liquidity["sweep_body_boost"]

        flow = greek_flow(current, history, synthetic_liq, top, low, band_width, last_close, last_open, step,
                           session_flow_mult=activity["greek_flow_mult"], session_fakeout_mult=activity["fakeout_mult"],
                           cfg=cfg)

        term_slope_impulse = 0.0
        if term_direction != 0:
            term_slope_impulse = term_direction * term_strength * GAMMA_BAND_TERM_SLOPE_IMPULSE_STRENGTH * combined_body_mult * (0.88 ** step)
            term_slope_impulse = max(min(term_slope_impulse, GAMMA_BAND_TERM_SLOPE_MAX_IMPULSE), -GAMMA_BAND_TERM_SLOPE_MAX_IMPULSE)

        any_shock_active = bear_delta_shock or bull_delta_shock or bear_shock or bull_shock
        body_confidence = 1.0 * vega_body_mult * flow["body_confidence_mult"]
        wick_expansion = 1.0 * vega_wick_mult * flow["wick_mult"] * activity["wick_mult"]

        if consensus["consensus_direction"] != 0:
            body_confidence = min(body_confidence * (1.0 + consensus["consensus_strength"] * GAMMA_BAND_CONFIDENCE_BOOST * (1.0 if consensus["all_aligned"] else 0.65)), 1.0)
        elif consensus["conflict"]:
            body_confidence *= max(1.0 - consensus["conflict_strength"] * GAMMA_BAND_CONFLICT_BODY_DAMPING, 0.35)
            wick_expansion *= 1.0 + consensus["conflict_strength"] * GAMMA_BAND_CONFLICT_WICK_EXPANSION

        # FlowImbalance damping — see flow_imbalance()'s own docstring.
        # A near-neutral raw Long/Short trade count means no real
        # directional edge in who's actually trading, so the body stays
        # smaller even when the target itself (gamma_ref/consensus/etc.
        # above) is confidently bullish or bearish.
        if flow_imb is not None and abs(flow_imb) < FLOW_IMBALANCE_NEUTRAL_THRESHOLD:
            body_confidence *= 1.0 - FLOW_IMBALANCE_BODY_DAMPING

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

        raw_impulse_sum = (
            base_pull_impulse + slope_impulse + current_body_impulse + curve_extreme_impulse
            + gb_impulse + reclaim_impulse + vega_impulse + delta_impulse + gamma_shock_impulse + mm_impulse
            + liquidity["impulse"] + flow["impulse"] * GREEK_FLOW_IMPULSE_WEIGHT + term_slope_impulse
        )

        # Zone Brake — as the pre-confidence impulse points toward `top`
        # (bullish) or `low` (bearish) within ZONE_BRAKE_ATR_DISTANCE ATRs
        # of that level, shrink body_confidence rather than letting the
        # candle close in on it at full strength; his own worked example
        # only covers the resistance side, mirrored here for support too,
        # same as every other paired long/short mechanism in this engine.
        # `top`/`low` can be 0.0 (absent, see dankbit.bands's own "0.0 =
        # absent" convention), hence the truthiness check.
        if raw_impulse_sum > 0 and top:
            body_confidence *= _zone_brake_mult(top - projected_open, effective_atr, ZONE_BRAKE_ATR_DISTANCE, ZONE_BRAKE_MIN_BODY_MULT)
        elif raw_impulse_sum < 0 and low:
            body_confidence *= _zone_brake_mult(projected_open - low, effective_atr, ZONE_BRAKE_ATR_DISTANCE, ZONE_BRAKE_MIN_BODY_MULT)

        forecast_impulse = raw_impulse_sum * body_confidence
        if gb_counter_trend_locked:
            allowed_opposite = GAMMA_BAND_COUNTER_MAX_OPP_IMPULSE * max(1.0 - consensus["consensus_strength"], 0.05)
            if consensus["consensus_direction"] < 0 and forecast_impulse > 0:
                forecast_impulse = min(forecast_impulse, allowed_opposite)
            elif consensus["consensus_direction"] > 0 and forecast_impulse < 0:
                forecast_impulse = max(forecast_impulse, -allowed_opposite)
        impulse_limit = 0.85 if any_shock_active else 0.45
        forecast_impulse = max(min(forecast_impulse, impulse_limit), -impulse_limit)

        step_move = band_width * forecast_impulse
        if step == 0:
            first_move_cap = max(effective_atr * SESSION_FIRST_MOVE_ATR[sess], 1e-9)
            step_move = max(min(step_move, first_move_cap), -first_move_cap)

        # Weekend move cap — the rest of the cascade above (combined_body_mult,
        # combined_shock_mult, the shock modules' own threshold_mult) already
        # softens weekend candles, but nothing previously stopped a strong
        # shock/consensus signal from compounding across many weekend steps
        # in a row (crypto trades 24/7, but real weekend moves are usually
        # much calmer than a weekday session — WEEKEND_PER_CANDLE_MOVE_ATR/
        # WEEKEND_TOTAL_MOVE_ATR/MAX_FIRST_WEEKEND_MOVE_ATR/
        # FIRST_WEEKEND_CANDLE_DAMPENING were defined for exactly this but,
        # like threshold_mult/combined_shock_mult above, were never actually
        # wired in — confirmed dead constants before this fix). The first
        # weekend candle gets its own tighter cap (a Friday-close-into-
        # Saturday transition is where a spurious "shock" is most likely to
        # have just fired); every subsequent weekend candle is capped
        # per-step and against a running total so a multi-candle cascade
        # can't quietly rebuild the same large move one small-looking step
        # at a time.
        if is_weekend:
            if not weekend_started:
                first_weekend_cap = max(effective_atr * MAX_FIRST_WEEKEND_MOVE_ATR, 1e-9)
                step_move = max(min(step_move * FIRST_WEEKEND_CANDLE_DAMPENING, first_weekend_cap), -first_weekend_cap)
                weekend_started = True
            else:
                per_candle_cap = max(effective_atr * WEEKEND_PER_CANDLE_MOVE_ATR, 1e-9)
                step_move = max(min(step_move, per_candle_cap), -per_candle_cap)
            remaining_budget = max(atr * WEEKEND_TOTAL_MOVE_ATR - weekend_cumulative_move, 0.0)
            step_move = max(min(step_move, remaining_budget), -remaining_budget)
            weekend_cumulative_move += abs(step_move)
        else:
            weekend_started = False
            weekend_cumulative_move = 0.0

        projected_close = projected_open + step_move

        # Breakout Gate — a projected close shouldn't cross above `top`
        # (the Zone High upper edge) or below `low` just because the
        # Greek levels sit that way; his own feedback wants a real
        # crossing conditioned on actual structural confirmation. Reused
        # here rather than inventing a second mechanism: Gamma-Band
        # Consensus's own confirms_top/confirms_low (a real top/low/gamma
        # slope agreement over actual elapsed history, gated to the
        # matching direction) plus Greek Flow's own directional impulse
        # sign agreeing. Only fires on an actual crossing attempt
        # (projected_open starts on the near side of the level) — a
        # candle that's already past it isn't dragged back. Blocked
        # excess bleeds into wick (BREAKOUT_GATE_WICK_BLEED) instead of
        # being discarded, so a rejected level still shows as tested
        # rather than a flat ceiling — see the upper_wick/lower_wick
        # block below.
        pending_upper_bleed = 0.0
        pending_lower_bleed = 0.0
        upper_gate_open = consensus["confirms_top"] and consensus["consensus_direction"] > 0 and flow["impulse"] >= 0
        lower_gate_open = consensus["confirms_low"] and consensus["consensus_direction"] < 0 and flow["impulse"] <= 0
        if top and projected_open <= top and projected_close > top and not upper_gate_open:
            pending_upper_bleed = (projected_close - top) * BREAKOUT_GATE_WICK_BLEED
            projected_close = top
        if low and projected_open >= low and projected_close < low and not lower_gate_open:
            pending_lower_bleed = (low - projected_close) * BREAKOUT_GATE_WICK_BLEED
            projected_close = low

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

        # Breakout Gate's blocked excess (see above) — shows the level as
        # tested/rejected rather than a flat ceiling/floor.
        upper_wick += pending_upper_bleed
        lower_wick += pending_lower_bleed

        upper_wick *= 1.0 + min(mm_upper_wick_boost, 1.25)
        lower_wick *= 1.0 + min(mm_lower_wick_boost, 1.25)
        if consensus["consensus_direction"] != 0:
            cut = min(consensus["consensus_strength"] * GAMMA_BAND_OPPOSITE_WICK_COMPRESSION, 0.70)
            if consensus["consensus_direction"] > 0:
                lower_wick *= 1.0 - cut
            else:
                upper_wick *= 1.0 - cut

        pre_absorption_high = body_top + max(upper_wick, 0.0)
        pre_absorption_low = body_bottom - max(lower_wick, 0.0)
        adjusted_close, absorption_mode = wick_to_body_acceptance(
            projected_open, projected_close, max(upper_wick, 0.0), max(lower_wick, 0.0),
            mm["upper_force_total"], mm["upper_net_force"], mm["lower_force_total"], mm["lower_net_force"],
            momentum_bull, momentum_bear, last_close, last_open, atr,
            liquidity, projected_open > levels["center"], projected_open < levels["center"],
            bull_delta_shock or bull_shock, bear_delta_shock or bear_shock,
            consensus["consensus_direction"] != 0, consensus["all_aligned"],
            consensus["consensus_direction"], consensus["consensus_strength"],
            is_weekend, neutral_score > 0 and not any_shock_active, any_shock_active,
        )
        if absorption_mode:
            projected_close = adjusted_close
            projected_bull = projected_close >= projected_open
            body_top = max(projected_open, projected_close)
            body_bottom = min(projected_open, projected_close)
            upper_wick = max(pre_absorption_high - body_top, 0.0)
            lower_wick = max(body_bottom - pre_absorption_low, 0.0)

        projected_high = max(body_top + max(upper_wick, 0.0), body_top)
        projected_low = min(body_bottom - max(lower_wick, 0.0), body_bottom)

        mode = []
        if absorption_mode:
            mode.append(absorption_mode)
        if gb_counter_trend_locked:
            mode.append("G-B " + ("Bear" if consensus["consensus_direction"] < 0 else "Bull") + " Trend Lock")
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
        if flow["fakeout_risk"]:
            mode.append("Flow Fakeout")
        if term_direction != 0:
            mode.append("Gamma Band Term " + ("Up" if term_direction > 0 else "Down"))
        if activity["regime"] != "normal":
            mode.append("Activity " + activity["regime"].capitalize())
        if is_weekend:
            mode.append("Weekend")
        mode.append(sess)

        points.append({
            "hours": hours_out,
            "open": float(projected_open), "high": float(projected_high),
            "low": float(projected_low), "close": float(projected_close),
            "mode": " / ".join(mode),
            # Diagnostic fields, consumed only by dankbit.forecast.log (the
            # forecast_json API route keeps whitelisting just the 6 keys
            # above) — the final clamped net impulse that actually produced
            # step_move, plus its per-engine breakdown, so a bad candle can
            # be attributed to one engine instead of guessed at from `mode`
            # alone.
            "forecast_impulse": float(forecast_impulse),
            "impulse_base_pull": float(base_pull_impulse),
            "impulse_slope": float(slope_impulse),
            "impulse_current_body": float(current_body_impulse),
            "impulse_curve_extreme": float(curve_extreme_impulse),
            "impulse_gamma_band": float(gb_impulse),
            "impulse_gamma_band_reclaim": float(reclaim_impulse),
            "impulse_vega": float(vega_impulse),
            "impulse_delta_shock": float(delta_impulse),
            "impulse_gamma_shock": float(gamma_shock_impulse),
            "impulse_mm_contest": float(mm_impulse),
            "impulse_liquidity": float(liquidity["impulse"]),
            "impulse_greek_flow": float(flow["impulse"] * GREEK_FLOW_IMPULSE_WEIGHT),
            "impulse_term_slope": float(term_slope_impulse),
            # Regime/context flags active for this candle — same signals
            # `mode` summarizes as free text, broken out here so they can be
            # filtered/grouped on directly instead of string-parsed.
            "is_weekend": bool(is_weekend),
            "session_name": sess,
            "activity_regime": activity["regime"],
            "gb_consensus_direction": int(consensus["consensus_direction"]),
            "gb_consensus_strength": float(consensus["consensus_strength"]),
            "gb_all_aligned": bool(consensus["all_aligned"]),
            "gb_trend_locked": bool(gb_counter_trend_locked),
            "any_shock_active": bool(any_shock_active),
            "fakeout_risk": bool(flow["fakeout_risk"]),
            "gamma_neutral_score": float(neutral_score),
            "absorption_mode": absorption_mode or None,
            "effective_atr": float(effective_atr),
        })
        projected_open = projected_close

    return points
