"""Deterministic 240-minute forecast-candle overlay for /4l/<asset>.

The /4l chart's dashed AVG line is the present-leg mean of the four
gamma-peak PRICES (BCG/BPG/SCG/SPG), colored by the dominant leg (largest
|gamma value|). It reads price direction well but LAGS — every trade in
the window contributes equally, so the level only migrates once fresh
flow outweighs stale flow.

A trade's creation timestamp carries information. The forecast pulls
price toward a **two-phase target**: first `p_slow` (the dominant leg's
own gamma-peak — the exact value the /4l AVG-line *label* shows; used
flat, since the user's premise is that this level is what price seeks),
then, once price reaches it, `p_avg` (the equal-weight mean of every
present leg's gamma-peak — where the dashed AVG *line* actually sits).
Creation time still drives the rest: recency-weighted per-leg fast peaks
(`p_avg`'s forward migration), a least-squares slope of signed
gamma-notional over 20-minute buckets (`flow_impulse` — is directional
flow building or draining?), recent trade-cluster density (`burst`), and
a confidence haircut when the recency-weighted dominant peak (`p_fast`)
disagrees with `p_slow`.

Fully deterministic — no GBM/random term anywhere, matching
controllers/forecast.py's own engine. Nothing here is persisted; the
/4l page recomputes on every poll.

Pure functions only (no Odoo/ORM). The controller
(main.py:four_leg_forecast_json) extracts plain trade data and the
per-leg slow/fast gamma-peak prices, then calls
simulate_four_leg_forecast() for the candle path.
"""

import math

from . import gamma as gamma_lib


# Horizon is hard-fixed (product decision) — no dropdown.
HORIZON_MINUTES = 240

# The path is ALWAYS simulated at this fine internal step, then rolled up
# (OHLC) to whatever display timeframe the /4l page asked for — so the 1h
# view (the trading timeframe) and the 15m view show the exact same path,
# and a 1h candle's high/low reflect the true intra-hour extremes
# (e.g. a real long lower wick when price dipped to p_slow and closed
# back up within the hour) instead of a per-timeframe wick heuristic.
SIM_STEP_MINUTES = 15

# For now the forecast is pinned to the NEAREST active expiry and a
# trailing trade window — it deliberately ignores whatever Expiry/
# Window/Cumulative the /4l page has selected (per request: "make sure
# this forecast is applied only for nearest expiry"). The window was 4h
# originally; widened to 8h per user feedback 2026-09-02 that a longer
# lookback "better reflects reality" (more flow history feeding the
# equal-weight `p_slow`/`p_avg`, while the 60-min-half-life `p_fast`
# still tracks just the recent tip). This also sets the `level_met_recently()`
# lookback and the forward `HORIZON_MINUTES` is unchanged (still 240 —
# how far the candles project, a separate hard-fixed decision).
# Following the page selection, and an accuracy log, are deferred.
NEAREST_EXPIRY_WINDOW_HOURS = 8

# ----- signal-extraction tunables (consumed by the helpers below) -----
# Age-bucketing for the recency-weighted "fast" per-leg gamma-peak.
FAST_AGE_BUCKETS = 6
FAST_HALF_LIFE_MINUTES = 60.0
# Signed-flow-imbalance slope binning.
FLOW_BIN_MINUTES = 20
# Burst intensity (recent trade density vs window baseline).
BURST_LOOKBACK_MINUTES = 60
BURST_MAX_MULT = 1.5
BURST_MIN_MULT = 0.6

# ----- path tunables (consumed by simulate_four_leg_forecast) -----
# The pull is a gap-closing rate, not a decaying nudge: each real hour it
# closes GAP_CLOSE_PER_HOUR of the remaining (target − price) distance, so
# over the 4h horizon ~1−(1−rate)^4 of the way to the target is covered
# regardless of timeframe — the forecast visibly commits to the level the
# AVG line points at, rather than barely drifting toward it. (Was a
# decaying `K_PULL·(target−price)·e^(−t/TAU)` term that only travelled
# ~35% of a 2-ATR gap in 4h — per user feedback 2026-09-02 that the
# candles didn't reflect "price will touch this level".)
GAP_CLOSE_PER_HOUR = 0.30
K_PUSH = 0.55           # per-step push from timed order-flow imbalance (× ATR)
TAU_HOURS = 3.0         # exponential decay of the push impulse over the horizon
MOVE_CAP_ATR = 0.90     # per-step |move| ceiling, in ATR units
# The move is scaled by (MOVE_CONF_FLOOR + (1−floor)·confidence) rather than
# by confidence outright — a low-confidence reading still points clearly at
# the target (per the same 2026-09-02 feedback), it just shows more
# uncertainty through wider wicks (WICK_CONF_WIDEN).
MOVE_CONF_FLOOR = 0.6
WICK_CONF_WIDEN = 0.5   # base wick ×(1 + WICK_CONF_WIDEN·(1−confidence))
WICK_FACTOR = 0.45      # base wick = WICK_FACTOR × ATR × sqrt(min(step, WICK_STEP_CAP))
WICK_STEP_CAP = 6       # cap the sqrt(step) wick growth (16 steps at 15m would blow out)
WICK_WALL_EXTRA_ATR = 0.6  # max extra wick toward an un-broken target level
P_GAMMA_CLAMP_ATR = 3.0    # targets clamped to their base ± this × ATR × sqrt(n)

# Two-phase target (per user observation, 2026-09-02): price first
# gravitates toward the DOMINANT leg's own gamma-peak (`p_slow` — the
# exact value the /4l AVG-line label shows, e.g. "SP 75900"; deliberately
# NOT drifted — the user's premise is that the label value itself is the
# level price seeks), then, once it has reached/crossed that level,
# rotates toward the drawn AVG line (`p_avg` — the equal-weight mean of
# all present legs' gamma-peaks, where the dashed line actually sits;
# this one DOES migrate, by `drift_v_avg`). The pull target latches from
# the dominant wall to the avg line the step price first comes within
# TOUCH_TOLERANCE_ATR × ATR of it (or crosses through it).
TOUCH_TOLERANCE_ATR = 0.35
# Creation-time weighting still feeds the forecast (flow_impulse, burst,
# drift_v_avg) — and here: when the recency-weighted dominant peak
# (`p_fast`) sits far from `p_slow`, the level is contested, so confidence
# is haircut up to CONF_DISAGREE_MAX_HAIRCUT as |p_fast − p_slow| grows to
# CONF_DISAGREE_ATR × ATR.
CONF_DISAGREE_ATR = 1.5
CONF_DISAGREE_MAX_HAIRCUT = 0.5

# Gamma-strength "reach" (per user observation, 2026-09-02): the "| -31M"
# figure on the /4l AVG-line label is the average dollar-gamma VALUE
# across the present legs. Below ~GAMMA_STRENGTH_REF_M million the gamma
# concentration is too thin to actually drag price to the level; above
# it, more likely. `gamma_reach_factor()` maps |avg gamma value| to a
# GAMMA_REACH_FLOOR..GAMMA_REACH_CEIL multiplier that throttles the pull
# rate (a weak reading drifts toward but falls short of the target) and
# haircuts confidence (wider wicks).
#
# PER-ASSET and window-dependent. BTC: the user's reference for the
# **8h** window is 100M (was ~30M on the old 4h window — user feedback
# 2026-09-02). ETH: still an estimate (~7M, scaled to ETH's ~15x-smaller
# gamma-notional scale — same reason forecast.GAMMA_ABS_NORMALIZER is
# per-asset), pending the user's own ETH 8h-window observation.
GAMMA_STRENGTH_REF_M = {"BTC": 100.0, "ETH": 7.0}
GAMMA_REACH_FLOOR = 0.25
GAMMA_REACH_CEIL = 1.15

# ----- confidence tunables -----
# Below this many dominant-leg trades the reading is too thin to act on.
THIN_DOM_TRADES = 15
# Dominant-leg trade count that scores full volume-confidence, per asset.
FULL_CONF_DOM_TRADES = {"BTC": 80.0, "ETH": 40.0}
LOW_CONFIDENCE_MODE_THRESHOLD = 0.35


def atr14(candles):
    """Classic ATR over the last 14 candles (true range = max of high-low,
    |high-prevclose|, |low-prevclose|). `candles` are {t,o,h,l,c} dicts
    oldest-first, as dankbit.trade.get_candles_coinbase() returns. None
    with fewer than 15 candles. Same definition forecast._atr14 uses;
    duplicated here to keep this module free of the heavy forecast.py
    import."""
    if len(candles) < 15:
        return None
    trs = []
    for i in range(len(candles) - 14, len(candles)):
        c, prev = candles[i], candles[i - 1]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - prev["c"]), abs(c["l"] - prev["c"])))
    return sum(trs) / len(trs)


def compute_flow_impulse(events, window_hours):
    """Signed order-flow imbalance *slope* over the window, normalized to
    roughly [-1.5, 1.5].

    `events` — list of (age_hours, signed_notional) where signed_notional
    is `amount × |Γ| × sign`, sign +1 for buy-call / sell-put (bullish),
    -1 for buy-put / sell-call (bearish) — the same convention
    forecast.greek_flow / market_maker_gamma_contest use. age 0 == now.

    Bins the window into FLOW_BIN_MINUTES buckets (most-recent bucket =
    highest x), least-squares fits the signed sum per bucket, and scales
    the slope by the total absolute notional so a rising bullish tilt in
    the newest buckets reads as a positive impulse regardless of overall
    volume. Returns 0.0 with no events or no signed magnitude at all."""
    if not events:
        return 0.0
    bin_h = FLOW_BIN_MINUTES / 60.0
    nbins = max(2, int(math.ceil(window_hours / bin_h)))
    sums = [0.0] * nbins
    denom = 0.0
    for age, signed in events:
        b = int(age / bin_h) if age > 0 else 0
        if b >= nbins:
            b = nbins - 1
        sums[nbins - 1 - b] += signed   # recent -> higher index
        denom += abs(signed)
    if denom <= 0:
        return 0.0
    xs = list(range(nbins))
    mean_x = sum(xs) / nbins
    mean_y = sum(sums) / nbins
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, sums))
    den = sum((x - mean_x) ** 2 for x in xs)
    slope = (num / den) if den else 0.0
    impulse = (slope * nbins) / denom
    return max(-1.5, min(1.5, impulse))


def compute_burst(ages_hours, window_hours):
    """Recent trade density vs the window baseline, clamped to
    [BURST_MIN_MULT, BURST_MAX_MULT]. >1 == the last hour's trades are
    arriving faster than the window's average inter-arrival time (a
    cluster/burst, a stronger signal than the same volume spread thin).
    1.0 (neutral) when there isn't enough to judge."""
    if len(ages_hours) < 4 or window_hours <= 0:
        return 1.0
    baseline_iat = window_hours / len(ages_hours)
    lookback_h = BURST_LOOKBACK_MINUTES / 60.0
    recent = sorted(a for a in ages_hours if 0 <= a <= lookback_h)
    if len(recent) < 3:
        return 1.0
    span = (recent[-1] - recent[0]) or lookback_h
    recent_iat = span / (len(recent) - 1)
    if recent_iat <= 0:
        return BURST_MAX_MULT
    ratio = baseline_iat / recent_iat
    return max(BURST_MIN_MULT, min(BURST_MAX_MULT, ratio))


def gamma_reach_factor(avg_gamma_value, asset="BTC"):
    """GAMMA_REACH_FLOOR..GAMMA_REACH_CEIL multiplier on how far the
    forecast travels toward the pull target, from the strength of the
    average dollar-gamma value (the "| -31M" figure on the /4l AVG-line
    label — the mean of the present legs' `gamma_value`). Per user
    observation (2026-09-02): |value| < ~`GAMMA_STRENGTH_REF_M[asset]`
    million → the gamma concentration is too thin to pull price to the
    level; above it, more likely. Linear in |value|/ref, clamped."""
    strength_m = abs(avg_gamma_value or 0.0) / 1e6
    ref = GAMMA_STRENGTH_REF_M.get(asset, GAMMA_STRENGTH_REF_M["BTC"])
    return max(GAMMA_REACH_FLOOR, min(GAMMA_REACH_CEIL, strength_m / ref))


def level_met_recently(candles, level, atr, lookback_steps):
    """True if any of the last `lookback_steps` real candles' [low, high]
    range came within TOUCH_TOLERANCE_ATR × ATR of `level`.

    Per user observation (2026-09-02): if real price has already reached
    the dominant-leg wall (`p_slow`, the AVG-line label value) within the
    trailing window (`NEAREST_EXPIRY_WINDOW_HOURS`, 8h), that target is
    "done" — the forecast starts in phase 2 (heading toward the drawn AVG
    line) rather than seeking the wall again. `candles` are the {t,o,h,l,c}
    dicts the ATR is computed from (15m, oldest-first)."""
    if not candles or not atr:
        return False
    tol = TOUCH_TOLERANCE_ATR * atr
    for c in candles[-lookback_steps:]:
        if c["l"] - tol <= level <= c["h"] + tol:
            return True
    return False


def per_trade_signed_notional(amount, direction, option_type, strike, t_years, iv_pct, ref_price):
    """`amount × |dollar-gamma at ref_price| × bullish-sign` for one
    trade — the term compute_flow_impulse() sums per time bucket. Returns
    0.0 for an unusable trade (iv 0, missing strike/expiry)."""
    sigma = (iv_pct or 0.0) / 100.0
    if sigma <= 0 or not strike or not t_years or t_years <= 0:
        return 0.0
    g = abs(float(gamma_lib.bs_gamma(ref_price, strike, t_years, 0.0, sigma)))
    bullish = (direction == "buy" and option_type == "call") or (direction == "sell" and option_type == "put")
    return (amount or 0.0) * g * (1.0 if bullish else -1.0)


def compute_confidence(asset, dom_trade_count, dom_gamma_abs, other_gamma_abs,
                       burst, p_slow, from_price, to_price, grid_step):
    """`{"value", "vol_conf", "sep", "burst_conf", "base", "trade_target",
    "next_leg_abs", "zeroed"}` — the 0.0–1.0 base confidence that scales
    the forecast body plus its component breakdown (rendered in the /4l
    Forecast panel). `value` is 0.0 (`zeroed=True`) when the dominant-leg
    sample is too thin or its gamma peak is pinned against the price-grid
    edge (a clamp artifact, not a real level). Otherwise blends volume
    adequacy, how cleanly the dominant leg out-weighs the next leg, and
    burst intensity. The caller applies the `p_fast`-disagreement and
    `gamma_reach` haircuts on top of `value`."""
    target = FULL_CONF_DOM_TRADES.get(asset, 80.0)
    dom_abs = abs(dom_gamma_abs)
    nxt = max((abs(v) for v in other_gamma_abs), default=0.0)
    vol_conf = min(1.0, dom_trade_count / target) if target else 0.0
    sep = 1.0 if dom_abs <= 0 else max(0.2, min(1.0, (dom_abs - nxt) / dom_abs))
    burst_conf = min(1.0, max(0.0, burst))
    base = max(0.0, min(1.0, 0.55 * vol_conf + 0.30 * sep + 0.15 * burst_conf))

    edge = 3 * max(grid_step, 1.0)
    zeroed = dom_trade_count < THIN_DOM_TRADES or p_slow <= from_price + edge or p_slow >= to_price - edge
    return {
        "value": 0.0 if zeroed else base,
        "vol_conf": vol_conf, "sep": sep, "burst_conf": burst_conf, "base": base,
        "trade_target": target, "next_leg_abs": nxt, "zeroed": bool(zeroed),
    }


def _mode(move, confidence):
    if confidence < LOW_CONFIDENCE_MODE_THRESHOLD:
        return "low_confidence"
    if move > 0:
        return "drift_up"
    if move < 0:
        return "drift_down"
    return "flat"


def simulate_four_leg_forecast(p_slow, flow_impulse, burst,
                               confidence, atr, last_close, from_price, to_price,
                               tf_minutes, p_avg=None, drift_v_avg=0.0,
                               start_phase2=False, gamma_reach=1.0,
                               horizon_minutes=HORIZON_MINUTES):
    """Build the forecast-candle path.

    `p_slow` — the dominant leg's own gamma-peak price, i.e. the exact
    value the /4l AVG-line **label** shows. This is the phase-1 pull
    target, used **flat** (not drifted) — the user's premise is that the
    label value itself is the level price seeks.
    `p_avg` — the drawn AVG **line**: equal-weight mean of every present
    leg's gamma-peak (defaults to `p_slow` when not supplied — then there
    is no distinct second phase). `drift_v_avg` — $/hour that avg line is
    migrating (from the mean of the present legs' recency-weighted fast
    peaks); the phase-2 target `p_avg + drift_v_avg·t` DOES migrate.
    `flow_impulse` / `burst` — from the helpers above (both recency-
    weighted). `confidence` — from compute_confidence(), already
    haircut by the caller for `p_fast`-vs-`p_slow` disagreement and for
    weak `gamma_reach`. `gamma_reach` — `gamma_reach_factor()` of the
    average dollar-gamma value (the "| -31M" label figure); < 1 throttles
    the pull rate so a thin reading drifts toward but falls short of the
    target.
    `atr` — ATR14 on SIM_STEP_MINUTES (15m) candles. Prices anchored at
    `last_close`; the client re-chains body/wick geometry onto the live
    candle, so absolute levels here only need to be self-consistent.

    **Two-phase target.** The pull target starts at the flat dominant
    wall `p_slow` and latches to the migrating avg line `p_avg(t)` the
    first step price reaches within TOUCH_TOLERANCE_ATR × ATR of `p_slow`
    or crosses through it — "price first goes toward what the AVG label
    shows, then toward the AVG line itself". `start_phase2=True` (set by
    the caller via `level_met_recently()` when real price already reached
    `p_slow` in the trailing window) skips phase 1 entirely — the target
    is already done, so every candle heads for the AVG line.

    The pull closes `GAP_CLOSE_PER_HOUR` of the remaining (target −
    price) gap per real hour (compounded by each step's own length), so
    the path covers a fixed fraction of the way to the target over the 4h
    horizon. `push` (the order-flow impulse) is additive on top and fades
    as `e^(−t/TAU_HOURS)`. The combined per-step move is capped at
    MOVE_CAP_ATR × ATR then scaled by `MOVE_CONF_FLOOR + (1−floor)·
    confidence` (a low-confidence forecast still points at the target, it
    just carries wider wicks).

    **The path is always simulated at SIM_STEP_MINUTES (15m) then rolled
    up (OHLC) to `tf_minutes`** — so the 1h and 15m views show the exact
    same path, and a 1h candle's high/low are the true extremes of its
    four 15m sub-steps (a genuine long lower wick when price dipped to
    `p_slow` and closed back up within the hour), not a per-timeframe
    wick heuristic.

    Returns [] on any guard trip (zero confidence, no ATR). Otherwise a
    list of {"t" (step index), "open", "high", "low", "close", "mode"}
    at `tf_minutes` resolution.
    """
    if confidence <= 0 or not atr or atr <= 0 or not last_close:
        return []
    if p_avg is None:
        p_avg = p_slow
    n_internal = max(1, int(round(horizon_minutes / SIM_STEP_MINUTES)))
    tf_h = SIM_STEP_MINUTES / 60.0
    reach = P_GAMMA_CLAMP_ATR * atr * math.sqrt(n_internal)
    p_dom = max(max(from_price, p_slow - reach), min(min(to_price, p_slow + reach), p_slow))
    avg_lo, avg_hi = max(from_price, p_avg - reach), min(to_price, p_avg + reach)
    touch_tol = TOUCH_TOLERANCE_ATR * atr
    move_conf = MOVE_CONF_FLOOR + (1.0 - MOVE_CONF_FLOOR) * confidence
    wick_widen = 1.0 + WICK_CONF_WIDEN * (1.0 - confidence)
    # gamma_reach throttles the pull rate: a thin avg-gamma reading (the
    # "| -31M" figure) drifts toward the target but falls short of it.
    pull_frac = (1.0 - (1.0 - GAP_CLOSE_PER_HOUR) ** tf_h) * gamma_reach

    sim = []
    running_open = float(last_close)
    phase2 = bool(start_phase2) and abs(p_avg - p_dom) > touch_tol
    for step in range(1, n_internal + 1):
        t = step * tf_h
        p_avg_t = max(avg_lo, min(avg_hi, p_avg + drift_v_avg * t))

        if not phase2:
            prev_open = sim[-1]["open"] if sim else float(last_close)
            last_lo = sim[-1]["low"] if sim else float(last_close)
            last_hi = sim[-1]["high"] if sim else float(last_close)
            reached = (
                abs(running_open - p_dom) <= touch_tol
                or (running_open - p_dom) * (prev_open - p_dom) < 0
                or last_lo - touch_tol <= p_dom <= last_hi + touch_tol  # a wick pierced the level
            )
            if reached and abs(p_avg_t - p_dom) > touch_tol:
                phase2 = True
        target = p_avg_t if phase2 else p_dom

        pull = pull_frac * (target - running_open)
        push = K_PUSH * flow_impulse * burst * atr * math.exp(-t / TAU_HOURS)
        move = pull + push
        cap = MOVE_CAP_ATR * atr
        move = max(-cap, min(cap, move)) * move_conf
        close = running_open + move

        base_wick = WICK_FACTOR * atr * math.sqrt(min(step, WICK_STEP_CAP)) * wick_widen
        up_wick = dn_wick = base_wick
        body_hi, body_lo = max(running_open, close), min(running_open, close)
        if target > body_hi:
            up_wick += min(WICK_WALL_EXTRA_ATR * atr, (target - body_hi) * 0.5)
        elif target < body_lo:
            dn_wick += min(WICK_WALL_EXTRA_ATR * atr, (body_lo - target) * 0.5)

        mode = _mode(move, confidence)
        if phase2 and mode not in ("flat", "low_confidence"):
            mode += "_to_avg"

        sim.append({
            "open": running_open, "close": close,
            "high": body_hi + up_wick, "low": body_lo - dn_wick, "mode": mode,
        })
        running_open = close

    # Roll the 15m sim up to the requested display timeframe (OHLC).
    group = max(1, int(round(tf_minutes / SIM_STEP_MINUTES)))
    points = []
    for i in range(0, len(sim), group):
        chunk = sim[i:i + group]
        points.append({
            "t": len(points) + 1,
            "open": round(chunk[0]["open"], 2),
            "close": round(chunk[-1]["close"], 2),
            "high": round(max(c["high"] for c in chunk), 2),
            "low": round(min(c["low"] for c in chunk), 2),
            "mode": chunk[-1]["mode"],
        })
    return points
