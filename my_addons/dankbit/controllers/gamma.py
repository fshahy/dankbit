import numpy as np
from math import exp, log, sqrt
from scipy.stats import norm

from scipy.stats import norm
import numpy as np
from datetime import datetime

# --- Black-Scholes Gamma ---
def bs_gamma(S, K, T, r, sigma):
    S = np.asarray(S, dtype=float)
    if T <= 0 or sigma <= 0:
        return np.zeros_like(S, dtype=float)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    return norm.pdf(d1) / (S * sigma * np.sqrt(T))

def _infer_sign(trd):
    if hasattr(trd, "direction"):
        s = str(trd.direction).lower()
        if s in ("buy", "long", "+", "1"):
            return 1.0
        if s in ("sell", "short", "-", "-1"):
            return -1.0
    amt = getattr(trd, "amount", getattr(trd, "qty", 0.0))
    return 1.0 if amt >= 0 else -1.0

# --- Portfolio Gamma ---
def portfolio_gamma(S, trades, r=0.0):
    total = np.zeros_like(S, dtype=float) if np.ndim(S) else 0.0
    for trd in trades:
        T      = 1/365
        sigma  = trd.iv/100
        sign   = _infer_sign(trd)
        qty    = trd.amount
        gamma  = bs_gamma(S, trd.strike, T, r, sigma)
        total += sign * qty * gamma
    return total

