import numpy as np
from scipy.stats import norm


def bs_delta(S, K, T, r, sigma, option_type="call"):
    S = np.asarray(S, dtype=float)
    if T <= 0 or sigma <= 0:
        return np.zeros_like(S, dtype=float)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    return norm.cdf(d1) if option_type == "call" else norm.cdf(d1) - 1

def _infer_sign(trd):
    if hasattr(trd, "direction"):
        s = str(trd.direction).lower()
        if s in ("buy", "long", "+", "1"):
            return 1.0
        if s in ("sell", "short", "-", "-1"):
            return -1.0
    # Fallback: sign from amount
    amt = getattr(trd, "amount", getattr(trd, "qty", 0.0))
    return 1.0 if amt >= 0 else -1.0

def portfolio_delta(S, trades, r=0.0):
    total = np.zeros_like(S, dtype=float) if np.ndim(S) else 0.0
    for trd in trades:
        T      = trd.days_to_expiry/365
        sigma  = trd.iv/100
        sign   = _infer_sign(trd)
        qty    = trd.amount
        delta  = bs_delta(S, trd.strike, T, r, sigma, trd.option_type)
        total += sign * qty * delta
    return total


