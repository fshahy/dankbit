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
