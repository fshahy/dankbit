import numpy as np
from scipy.stats import norm


# ============================================================
# Pure Black–Scholes Theta, expressed as $/day (annualized theta ÷ 365,
# the convention traders actually quote — the cost/gain of holding this
# position for one more calendar day, not the raw per-year value).
# ============================================================
def bs_theta(S, K, T, r, sigma, option_type="call", min_time_hours=1.0):
    S = np.asarray(S, dtype=float)

    eps_years = min_time_hours / (24.0 * 365.0)
    sigma_eps = 1e-4

    T_eff = max(T, eps_years)
    sigma_eff = max(sigma, sigma_eps)

    d1 = (
        np.log(S / K)
        + (r + 0.5 * sigma_eff ** 2) * T_eff
    ) / (sigma_eff * np.sqrt(T_eff))
    d2 = d1 - sigma_eff * np.sqrt(T_eff)

    decay = -(S * sigma_eff * norm.pdf(d1)) / (2.0 * np.sqrt(T_eff))

    if option_type == "call":
        theta = decay - r * K * np.exp(-r * T_eff) * norm.cdf(d2)
    else:
        theta = decay + r * K * np.exp(-r * T_eff) * norm.cdf(-d2)

    return theta / 365.0


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
# Portfolio Theta ($/day)
# ============================================================
def portfolio_theta(S, trades, r=0.0, min_hours=1.0):
    total = np.zeros_like(S, dtype=float) if np.ndim(S) else 0.0

    for trd in trades:
        hours_to_expiry = trd.get_hours_to_expiry()
        T = hours_to_expiry / (24.0 * 365.0)

        sigma = trd.iv / 100.0
        sign = _infer_sign(trd)
        weight = trd.amount

        theta = bs_theta(
            S=S,
            K=trd.strike,
            T=T,
            r=r,
            sigma=sigma,
            option_type=trd.option_type,
            min_time_hours=min_hours,
        )

        total += sign * weight * theta

    return total
