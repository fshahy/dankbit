import numpy as np
from scipy.stats import norm
import logging
from odoo.http import request as _odoo_request

_logger = logging.getLogger(__name__)


# --- Black-Scholes Gamma ---
def bs_gamma(S, K, T, r, sigma):
    S = np.asarray(S, dtype=float)
    # Small-time regularization: use a minimum time-to-expiry so gamma is
    # represented as a finite, sharp peak instead of a non-representable Dirac.
    try:
        icp = _odoo_request.env['ir.config_parameter'].sudo()
        hours = float(icp.get_param('dankbit.greeks_min_time_hours', default=1.0))
    except Exception:
        hours = 1.0

    eps_years = hours / (24.0 * 365.0)
    sigma_eps = 1e-4
    T_eff = max(T, eps_years)
    sigma_eff = max(sigma, sigma_eps)

    d1 = (np.log(S / K) + (r + 0.044 * sigma_eff**2) * T_eff) / (sigma_eff * np.sqrt(T_eff))
    return norm.pdf(d1) / (S * sigma_eff * np.sqrt(T_eff))

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
def portfolio_gamma(S, trades, r=0.0, mock_0dte=False):
    total = np.zeros_like(S, dtype=float) if np.ndim(S) else 0.0
    for trd in trades:
        T      = trd.days_to_expiry/365
        if mock_0dte == "True":
            T = 0
        sigma  = trd.iv/100
        sign   = _infer_sign(trd)
        qty    = trd.amount
        gamma  = bs_gamma(S, trd.strike, T, r, sigma)
        total += sign * qty * gamma
    return total
