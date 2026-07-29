import numpy as np
from scipy.stats import norm


# ============================================================
# Black–Scholes Vega, expressed as $ per 1 IV percentage point (raw vega ÷
# 100) — the convention traders quote, same reasoning as bs_theta's ÷365
# (see theta.py). Vega has no call/put distinction in Black-Scholes (same
# formula either way), matching gamma.py's bs_gamma, which also takes no
# option_type.
# ============================================================
def bs_vega(S, K, T, r, sigma, min_time_hours=1.0):
    S = np.asarray(S, dtype=float)

    eps_years = min_time_hours / (24.0 * 365.0)
    sigma_eps = 1e-4

    T_eff = max(T, eps_years)
    sigma_eff = max(sigma, sigma_eps)

    d1 = (
        np.log(S / K)
        + (r + 0.5 * sigma_eff ** 2) * T_eff
    ) / (sigma_eff * np.sqrt(T_eff))

    vega = S * norm.pdf(d1) * np.sqrt(T_eff)
    return vega / 100.0


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
# Portfolio Vega ($ per 1 IV point)
# ============================================================
def portfolio_vega(S, trades, r=0.0, min_hours=1.0):
    total = np.zeros_like(S, dtype=float) if np.ndim(S) else 0.0

    for trd in trades:
        hours_to_expiry = trd.get_hours_to_expiry()
        T = hours_to_expiry / (24.0 * 365.0)

        sigma = trd.iv / 100.0
        sign = _infer_sign(trd)
        weight = trd.amount

        vega = bs_vega(
            S=S,
            K=trd.strike,
            T=T,
            r=r,
            sigma=sigma,
            min_time_hours=min_hours,
        )

        total += sign * weight * vega

    return total
