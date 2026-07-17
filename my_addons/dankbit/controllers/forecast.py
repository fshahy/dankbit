# -*- coding: utf-8 -*-

import numpy as np


def simulate_path(index_price, sigma_annual, hours_ahead=48, step_hours=4, substeps=48, seed=None):
    """One simulated GBM price path for `index_price`, under the same
    risk-neutral (r=0.0) convention this addon's other Greeks use — no
    assumed drift beyond the standard GBM variance-drag term
    (-0.5*sigma^2*dt per sub-step). `seed` (see forecast_json — derived
    from the current UTC hour, so the path is stable between polls within
    the same hour but reseeds as new IV data comes in) makes the path
    reproducible for a given call rather than jittering on every request.
    `step_hours` defaults to 4 (matching the TradingView chart's 4h
    timeframe, the only one the Forecast candles are shown on — see
    forecast_json) with `substeps` scaled up to match (48 sub-steps per
    4h candle = the same ~5-minute sub-step resolution the earlier
    1h/12-substep default used). Each candle is built from `substeps`
    finer GBM increments (open = the candle's starting price, close = its
    last sub-step, high/low = the sub-steps' own max/min) so it has a real
    intra-candle range, not just a single jump per candle. One point per
    `step_hours` out to `hours_ahead`; each {hours, open, high, low,
    close}."""
    rng = np.random.default_rng(seed)
    dt = (step_hours / substeps) / (24.0 * 365.0)
    drift = -0.5 * sigma_annual ** 2 * dt
    vol = sigma_annual * np.sqrt(dt)

    points = []
    S = index_price
    hours = 0.0
    n_candles = int(round(hours_ahead / step_hours))
    for _ in range(n_candles):
        hours += step_hours
        open_ = S
        path = [S]
        for _ in range(substeps):
            z = rng.standard_normal()
            S = S * np.exp(drift + vol * z)
            path.append(S)
        points.append({
            "hours": hours,
            "open": float(open_),
            "high": float(max(path)),
            "low": float(min(path)),
            "close": float(S),
        })
    return points


# Annualized mean-reversion speed (kappa) for simulate_path_with_levels' pull
# toward gamma_band, in log-price space — kappa=300 gives an OU half-life of
# ln(2)/300 years (~20 hours), so a deviation from gamma_band is mostly
# reverted by the end of the default 48h horizon without swamping the
# short-term noise term.
MEAN_REVERSION_SPEED = 300.0

# How much of a top_intersection/bottom_intersection breach is given back on
# the same sub-step — 0.5 means a move 1% past the level lands back at 0.5%
# past it: a soft reflection, not a hard clamp to the level itself.
BARRIER_SOFTNESS = 0.5


def simulate_path_with_levels(
    index_price, sigma_annual, top_intersection, bottom_intersection, gamma_band,
    hours_ahead=48, step_hours=4, substeps=48, seed=None,
    reversion_speed=MEAN_REVERSION_SPEED, barrier_softness=BARRIER_SOFTNESS,
):
    """Second forecast path ("Forecast 2") — same GBM engine as
    simulate_path, but additionally pulled toward `gamma_band` (mean-
    reversion center, in log-price space, replacing simulate_path's plain
    variance-drag-only drift) and softly reflected off `top_intersection`/
    `bottom_intersection` whenever a sub-step lands beyond them (see
    BARRIER_SOFTNESS) — the same three levels the TradingView chart draws
    as the Zones Extrema lines (green/red/dashed violet), which the plain
    forecast ignores entirely. `top_intersection`/`bottom_intersection` may
    each be None to skip that side's barrier (e.g. when a curve never
    crossed zero — see dankbit.zones.extrema, where 0.0 means "no
    crossing"). Same {hours, open, high, low, close} shape as
    simulate_path."""
    rng = np.random.default_rng(seed)
    dt = (step_hours / substeps) / (24.0 * 365.0)
    vol = sigma_annual * np.sqrt(dt)
    log_center = np.log(gamma_band)

    points = []
    S = index_price
    hours = 0.0
    n_candles = int(round(hours_ahead / step_hours))
    for _ in range(n_candles):
        hours += step_hours
        open_ = S
        path = [S]
        for _ in range(substeps):
            z = rng.standard_normal()
            X = np.log(S)
            X = X + reversion_speed * (log_center - X) * dt - 0.5 * sigma_annual ** 2 * dt + vol * z
            S = np.exp(X)
            if top_intersection and S > top_intersection:
                S = top_intersection - (S - top_intersection) * barrier_softness
            if bottom_intersection and S < bottom_intersection:
                S = bottom_intersection + (bottom_intersection - S) * barrier_softness
            path.append(S)
        points.append({
            "hours": hours,
            "open": float(open_),
            "high": float(max(path)),
            "low": float(min(path)),
            "close": float(S),
        })
    return points
