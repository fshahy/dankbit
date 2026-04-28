import math
from datetime import datetime, timezone
import numpy as np
from scipy.stats import norm


# ============================================================
# Pure Black–Scholes Delta (NO decay, NO memory)
# ============================================================
def bs_delta(S, K, T, r, sigma, option_type="call", min_time_hours=1.0):
    S = np.asarray(S, dtype=float)

    eps_years = min_time_hours / (24.0 * 365.0)
    sigma_eps = 1e-4

    T_eff = max(T, eps_years)
    sigma_eff = max(sigma, sigma_eps)

    d1 = (
        np.log(S / K)
        + (r + 0.5 * sigma_eff ** 2) * T_eff
    ) / (sigma_eff * np.sqrt(T_eff))

    if option_type == "call":
        return norm.cdf(d1)
    else:
        return norm.cdf(d1) - 1.0


# ============================================================
# Trade sign
# ============================================================
def _infer_sign(trd):
    if trd.direction == "buy":
        return 1.0
    elif trd.direction == "sell":
        return -1.0
    return 0.0


# ============================================================
# Portfolio Delta (FLOW decays, STRUCTURE does not)
# ============================================================
def portfolio_delta(S, trades, r=0.0, mode="flow", min_hours=1.0, tau=6.0):
    total = np.zeros_like(S, dtype=float) if np.ndim(S) else 0.0
    tau_seconds = float(tau) * 3600.0
    now = datetime.now(timezone.utc)

    for trd in trades:
        hours_to_expiry = trd.get_hours_to_expiry()
        T = hours_to_expiry / (24.0 * 365.0)

        sigma = trd.iv / 100.0
        sign = _infer_sign(trd)
        weight = trd.amount

        delta = bs_delta(
            S=S,
            K=trd.strike,
            T=T,
            r=r,
            sigma=sigma,
            option_type=trd.option_type,
            min_time_hours=min_hours,
        )

        # --- Apply decay ONLY in FLOW mode ---
        if mode == "flow" and trd.deribit_ts:
            ts = trd.deribit_ts
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            dt = (now - ts).total_seconds()
            if dt > 0:
                delta *= math.exp(-dt / tau_seconds)
            else:
                delta *= 0.0

        # --- Persistence (structure smoothing) ---
        if mode == "structure":
            delta *= 1.0
            persistence = 1.0
        else:
            persistence = 1.0

        total += sign * weight * delta * persistence

    return total
