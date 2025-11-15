import numpy as np
from scipy.stats import norm
import logging
from odoo.http import request as _odoo_request

_logger = logging.getLogger(__name__)


def bs_delta(S, K, T, r, sigma, option_type="call"):
    S = np.asarray(S, dtype=float)
    # Small-time regularization: treat very small T as epsilon so greeks
    # remain finite and visible on plots. Epsilon (in hours) can be set via
    # config parameter 'dankbit.greeks_min_time_hours' (default 1 hour).
    try:
        icp = _odoo_request.env['ir.config_parameter'].sudo()
        hours = float(icp.get_param('dankbit.greeks_min_time_hours', default=1.0))
    except Exception:
        hours = 1.0

    # convert hours to years
    eps_years = hours / (24.0 * 365.0)

    # avoid zero or negative vol causing division-by-zero; clamp to small positive
    sigma_eps = 1e-4
    T_eff = max(T, eps_years)
    sigma_eff = max(sigma, sigma_eps)

    d1 = (np.log(S / K) + (r + 0.5 * sigma_eff**2) * T_eff) / (sigma_eff * np.sqrt(T_eff))
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

def portfolio_delta(S, trades, r=0.0, mock_0dte=False):
    total = np.zeros_like(S, dtype=float) if np.ndim(S) else 0.0
    for trd in trades:
        T = trd.days_to_expiry/365
        if mock_0dte == "True":
            T = 0
        sigma  = trd.iv/100
        sign   = _infer_sign(trd)
        qty    = trd.amount
        delta  = bs_delta(S, trd.strike, T, r, sigma, trd.option_type)
        total += sign * qty * delta
    return total


