import math
from datetime import datetime, timezone
import numpy as np
from scipy.stats import norm
from odoo.http import request as _odoo_request


# --- Black-Scholes Gamma ---
def bs_gamma(S, K, T, r, sigma, trade_ts):
    S = np.asarray(S, dtype=float)

    try:
        icp = _odoo_request.env['ir.config_parameter'].sudo()
        hours = float(icp.get_param('dankbit.greeks_min_time_hours', default=1.0))
        tau_seconds = float(
            icp.get_param(
                'dankbit.greeks_gamma_decay_tau_seconds',
                default=21600  # 6h
            )
        )
    except Exception:
        hours = 1.0
        tau_seconds = 21600

    # --- Small-time regularization ---
    eps_years = hours / (24.0 * 365.0)
    sigma_eps = 1e-4

    T_eff = max(T, eps_years)
    sigma_eff = max(sigma, sigma_eps)

    d1 = (
        np.log(S / K)
        + (r + 0.5 * sigma_eff**2) * T_eff
    ) / (sigma_eff * np.sqrt(T_eff))

    gamma = norm.pdf(d1) / (S * sigma_eff * np.sqrt(T_eff))

    # --- Time-decay weighting ---
    if trade_ts is not None:
        now = datetime.now(timezone.utc)

        if trade_ts.tzinfo is None:
            trade_ts = trade_ts.replace(tzinfo=timezone.utc)

        dt = (now - trade_ts).total_seconds()

        if dt > 0:
            gamma *= math.exp(-dt / tau_seconds)
        else:
            gamma *= 0.0

    return gamma

def _infer_sign(trd):
    if trd.direction == "buy":
        return 1.0
    elif trd.direction == "sell":
        return -1.0
    else:
        return 0.0

# --- Portfolio Gamma ---
def portfolio_gamma(S, trades, r=0.0, mock_0dte=False, mode="raw"):
    total = np.zeros_like(S, dtype=float) if np.ndim(S) else 0.0

    for trd in trades:
        hours_to_expiry = trd.get_hours_to_expiry()
        T = hours_to_expiry / (24.0 * 365.0)
        if mock_0dte:
            T = 0.0

        sigma = trd.iv / 100.0
        sign  = _infer_sign(trd)

        if mode == "raw":
            weight = trd.amount
        elif mode == "oi":
            weight = trd.oi_impact
        else:
            raise ValueError(f"Unknown mode: {mode}")

        if weight == 0:
            continue

        gamma_flow = bs_gamma(
            S,
            trd.strike,
            T,
            r,
            sigma,
            trd.deribit_ts,
        )

        if mode == "raw":
            persistence = 1.0
        elif mode == "oi":
            if trd.oi_impact is None:
                persistence = 1.0
            elif trd.amount:
                persistence = min(
                    1.0,
                    abs(trd.oi_impact) / max(abs(trd.amount), 1e-6)
                )
            else:
                persistence = 0.0
        else:
            raise ValueError(f"Unknown mode: {mode}")

        total += sign * weight * gamma_flow * persistence

    return total
