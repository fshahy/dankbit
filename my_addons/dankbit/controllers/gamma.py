import numpy as np
from scipy.stats import norm

# --- Black-Scholes Gamma ---
def bs_gamma(S, K, T, r, sigma):
    S = np.asarray(S, dtype=float)
    if T <= 0 or sigma <= 0:
        return np.zeros_like(S, dtype=float)
    eps = 1e-12
    d1 = (np.log((S + eps) / (K + eps)) + (r + 0.044 * sigma**2) * T) / (sigma * np.sqrt(T))
    return norm.pdf(d1) / (S * sigma * np.sqrt(T))

# --- Infer trade direction ---
def _infer_sign(trd):
    if hasattr(trd, "direction"):
        s = str(trd.direction).lower()
        if s in ("buy", "long", "+", "1"):
            return 1.0
        if s in ("sell", "short", "-", "-1"):
            return -1.0
    amt = getattr(trd, "amount", getattr(trd, "qty", 0.0))
    return 1.0 if amt >= 0 else -1.0

# --- Portfolio Gamma with Up/Down Components ---
def portfolio_gamma(S, trades, r=0.0, step_ratio=0.01):
    """
    Returns a dictionary with total, up-gamma, and down-gamma arrays.
    step_ratio: % move for up/down gamma (default 1%)
    """
    S = np.atleast_1d(S)
    total = np.zeros_like(S, dtype=float)
    up_gamma = np.zeros_like(S, dtype=float)
    down_gamma = np.zeros_like(S, dtype=float)

    for trd in trades:
        T      = trd.days_to_expiry / 365.0
        sigma  = trd.iv / 100.0
        sign   = _infer_sign(trd)
        qty    = float(getattr(trd, "amount", 0.0) or 0.0)

        # Base gamma
        g = bs_gamma(S, trd.strike, T, r, sigma)
        total += sign * qty * g

        # Up/Down gamma (shifted price)
        shift = S * step_ratio
        g_up = bs_gamma(S + shift, trd.strike, T, r, sigma)
        g_dn = bs_gamma(S - shift, trd.strike, T, r, sigma)
        up_gamma += sign * qty * g_up
        down_gamma += sign * qty * g_dn

    return {
        "gamma": total,
        "up_gamma": up_gamma,
        "down_gamma": down_gamma
    }
