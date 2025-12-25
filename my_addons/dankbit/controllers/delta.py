from datetime import datetime, timezone
import math
import numpy as np
from scipy.stats import norm
from odoo.http import request as _odoo_request


# --- Black-Scholes Delta ---
def bs_delta(S, K, T, r, sigma, trade_ts, option_type="call"):
    S = np.asarray(S, dtype=float)
    tau_seconds = 14400  # 4h decay

    try:
        icp = _odoo_request.env["ir.config_parameter"].sudo()
        hours = float(icp.get_param("dankbit.greeks_min_time_hours", default=1.0))
    except Exception:
        hours = 1.0

    eps_years = hours / (24.0 * 365.0)
    sigma_eps = 1e-4

    T_eff = max(T, eps_years)
    sigma_eff = max(sigma, sigma_eps)

    d1 = (
        np.log(S / K)
        + (r + 0.5 * sigma_eff**2) * T_eff
    ) / (sigma_eff * np.sqrt(T_eff))

    delta = norm.cdf(d1) if option_type == "call" else norm.cdf(d1) - 1

    # -------------------------------
    # Time-decay weighting (SAFE)
    # -------------------------------
    if trade_ts is not None:
        now = datetime.now(timezone.utc)

        if trade_ts.tzinfo is None:
            trade_ts = trade_ts.replace(tzinfo=timezone.utc)

        dt = (now - trade_ts).total_seconds()
        if dt > 0:
            delta *= math.exp(-dt / tau_seconds)
        else:
            delta *= 0.0

    return delta

def _infer_sign(trd):
    if trd.direction == "buy":
        return 1.0
    elif trd.direction == "sell":
        return -1.0
    else:
        return 0.0

# --- Portfolio Delta ---
def portfolio_delta(S, trades, r=0.0, mock_0dte=False, mode="flow"):
    total = np.zeros_like(S, dtype=float) if np.ndim(S) else 0.0

    for trd in trades:
        hours_to_expiry = trd.get_hours_to_expiry()
        T = hours_to_expiry / (24.0 * 365.0)
        if mock_0dte:
            T = 0.0

        sigma = trd.iv / 100.0
        sign  = _infer_sign(trd)

        if mode == "flow":
            weight = trd.amount
        elif mode == "structure":
            weight = trd.oi_impact
        else:
            raise ValueError(f"Unknown mode: {mode}")

        if weight == 0:
            continue

        delta_flow = bs_delta(
            S,
            trd.strike,
            T,
            r,
            sigma,
            trd.deribit_ts,
            trd.option_type,
        )

        if mode == "flow":
            persistence = 1.0
        elif mode == "structure":
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

        total += sign * weight * delta_flow * persistence

    return total
