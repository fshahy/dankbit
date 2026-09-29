# -*- coding: utf-8 -*-
"""Independent, display-only long-term expiry path scenario.

The endpoint reads Deribit option trades and computes its own expiry nodes.
It does not persist data or feed Forecast, Gamma Horizon, MM Hedge, Bands,
Smart Liquidity, Greeks Flow, FOMO, Anchors, or Signal Bot.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from odoo import http
from odoo.http import request


def _as_naive_utc(value):
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _is_last_friday(day):
    return day.weekday() == 4 and (day + timedelta(days=7)).month != day.month


def _expiry_kind(expiry):
    day = _as_naive_utc(expiry).date()
    if day.month == 12 and _is_last_friday(day):
        return "YE"
    if _is_last_friday(day):
        return "M"
    return "W"


def _select_expiries(expirations, now):
    """Keep available Friday expiries from the next Friday through Dec 31."""
    now = _as_naive_utc(now)
    year_end = datetime(now.year + 1, 1, 1)
    rows = sorted({ _as_naive_utc(expiry) for expiry in expirations
                    if expiry and now < _as_naive_utc(expiry) < year_end
                    and _as_naive_utc(expiry).weekday() == 4 })
    return [(expiry, _expiry_kind(expiry)) for expiry in rows]


def _normal_pdf(value):
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _normal_cdf(value):
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _option_greeks(spot, strike, years, sigma, option_type):
    """Black-Scholes Greeks with r=q=0; gamma is dollar gamma (Γ × S²)."""
    spot = float(spot or 0.0)
    strike = float(strike or 0.0)
    sigma = float(sigma or 0.0)
    years = max(float(years or 0.0), 1.0 / (24.0 * 365.0))
    if spot <= 0.0 or strike <= 0.0 or sigma <= 0.0:
        return 0.0, 0.0, 0.0, 0.0
    sigma = max(sigma, 1e-4)
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * years) / (sigma * root_t)
    density = _normal_pdf(d1)
    call_delta = _normal_cdf(d1)
    delta = call_delta if option_type == "call" else call_delta - 1.0
    gamma_dollar = density / (spot * sigma * root_t) * spot * spot
    theta_day = -(spot * density * sigma) / (2.0 * root_t * 365.0)
    vega_point = spot * density * root_t * 0.01
    return delta, gamma_dollar, theta_day, vega_point


def _safe_number(value, digits=8):
    value = float(value or 0.0)
    return round(value, digits) if math.isfinite(value) else 0.0


def _clamp(value, low, high):
    return max(low, min(high, float(value)))


def _iso_utc(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _build_expiry_node(expiry, kind, rows, now, spot):
    """Aggregate full expiry life, then age flow using freshness and Theta.

    Trade direction is a flow proxy, not proof of an open position. Theta's
    retention adjustment is therefore deliberately mild and is disclosed in
    the returned method note.
    """
    half_life_days = {"W": 6.0, "M": 15.0, "YE": 75.0}.get(kind, 15.0)
    grouped = {}
    first_seen = None
    raw_amount = 0.0
    trade_count = 0
    theta_retention_numerator = 0.0

    for row in rows:
        (row_expiry, strike, option_type, direction, trade_hour,
         amount, avg_iv, avg_price, avg_index, count, first_trade_ts) = row
        if row_expiry != expiry or option_type not in ("call", "put") or direction not in ("buy", "sell"):
            continue
        strike = float(strike or 0.0)
        amount = float(amount or 0.0)
        iv_percent = float(avg_iv or 0.0)
        entry_spot = float(avg_index or 0.0)
        entry_price = float(avg_price or 0.0)
        if min(strike, amount, iv_percent, entry_spot) <= 0.0:
            continue

        trade_hour = _as_naive_utc(trade_hour)
        age_hours = max(0.0, (now - trade_hour).total_seconds() / 3600.0)
        age_days = age_hours / 24.0
        freshness = 0.40 + 0.60 * math.pow(2.0, -age_days / half_life_days)

        # Theta estimates the option-premium erosion since the hourly trade
        # cohort entered. It only reduces this module's proxy exposure; it
        # never supplies an underlying-price direction.
        years_at_entry = max((expiry - trade_hour).total_seconds() / (365.0 * 86400.0), 1.0 / (24.0 * 365.0))
        _, _, entry_theta_day, _ = _option_greeks(
            entry_spot, strike, years_at_entry, iv_percent / 100.0, option_type,
        )
        premium_usd = entry_price * entry_spot
        erosion_ratio = abs(entry_theta_day) * age_days / max(premium_usd, entry_spot * 1e-6, 1e-9)
        theta_retention = 1.0 / (1.0 + max(0.0, erosion_ratio))
        weighted_amount = amount * freshness * theta_retention
        if weighted_amount <= 0.0:
            continue

        key = (int(strike), option_type, direction)
        bucket = grouped.setdefault(key, {
            "amount": 0.0, "iv_amount": 0.0, "entry_spot_amount": 0.0,
            "entry_price_amount": 0.0,
        })
        bucket["amount"] += weighted_amount
        bucket["iv_amount"] += iv_percent * weighted_amount
        bucket["entry_spot_amount"] += entry_spot * weighted_amount
        bucket["entry_price_amount"] += entry_price * weighted_amount
        raw_amount += amount
        trade_count += int(count or 0)
        theta_retention_numerator += amount * theta_retention
        stamp = first_trade_ts or trade_hour
        stamp = _as_naive_utc(stamp)
        first_seen = stamp if first_seen is None else min(first_seen, stamp)

    if not grouped or spot <= 0.0:
        return None

    expiry_years = max((expiry - now).total_seconds() / (365.0 * 86400.0), 1.0 / (24.0 * 365.0))
    gamma_grid_low = max(spot * 0.50, min([spot] + [key[0] for key in grouped]) * 0.92)
    gamma_grid_high = max(spot * 1.50, max([spot] + [key[0] for key in grouped]) * 1.08)
    if gamma_grid_high <= gamma_grid_low:
        gamma_grid_low, gamma_grid_high = spot * 0.75, spot * 1.25
    grid = [gamma_grid_low + (gamma_grid_high - gamma_grid_low) * i / 240.0 for i in range(241)]
    gamma_curves = {"buy": [0.0] * len(grid), "sell": [0.0] * len(grid)}

    net_delta = gross_delta = net_gamma = gross_gamma = 0.0
    net_theta = gross_theta = net_vega = gross_vega = 0.0
    iv_weighted = iv_weight = 0.0
    atm_iv_weighted = atm_iv_weight = 0.0
    gross_amount = 0.0
    for (strike, option_type, direction), bucket in grouped.items():
        amount = bucket["amount"]
        iv_percent = bucket["iv_amount"] / amount if amount else 0.0
        sigma = iv_percent / 100.0
        sign = 1.0 if direction == "buy" else -1.0
        delta, gamma_dollar, theta_day, vega_point = _option_greeks(
            spot, strike, expiry_years, sigma, option_type,
        )
        delta_value = delta * spot * amount
        gamma_value = gamma_dollar * amount
        theta_value = theta_day * amount
        vega_value = vega_point * amount
        net_delta += sign * delta_value
        gross_delta += abs(delta_value)
        net_gamma += sign * gamma_value
        gross_gamma += abs(gamma_value)
        net_theta += sign * theta_value
        gross_theta += abs(theta_value)
        net_vega += sign * vega_value
        gross_vega += abs(vega_value)
        iv_weighted += iv_percent * amount
        iv_weight += amount
        gross_amount += amount
        if abs(strike / spot - 1.0) <= 0.20:
            atm_iv_weighted += iv_percent * amount
            atm_iv_weight += amount

        for index, scenario_spot in enumerate(grid):
            _, scenario_gamma, _, _ = _option_greeks(
                scenario_spot, strike, expiry_years, sigma, option_type,
            )
            gamma_curves[direction][index] += amount * scenario_gamma

    buy_index = max(range(len(grid)), key=lambda i: gamma_curves["buy"][i])
    sell_index = max(range(len(grid)), key=lambda i: gamma_curves["sell"][i])
    buyer_price = grid[buy_index] if gamma_curves["buy"][buy_index] > 0 else None
    seller_price = grid[sell_index] if gamma_curves["sell"][sell_index] > 0 else None
    peaks = [price for price in (buyer_price, seller_price) if price is not None]
    if not peaks:
        return None
    target_price = sum(peaks) / len(peaks)
    avg_iv = atm_iv_weighted / atm_iv_weight if atm_iv_weight else (iv_weighted / iv_weight if iv_weight else 0.0)
    gamma_bias = _clamp(net_gamma / gross_gamma, -1.0, 1.0) if gross_gamma else 0.0
    delta_bias = _clamp(net_delta / gross_delta, -1.0, 1.0) if gross_delta else 0.0
    vega_bias = _clamp(net_vega / gross_vega, -1.0, 1.0) if gross_vega else 0.0
    theta_bias = _clamp(net_theta / gross_theta, -1.0, 1.0) if gross_theta else 0.0

    return {
        "expiry": _iso_utc(expiry), "kind": kind, "price": _safe_number(target_price, 2),
        "buyer_price": _safe_number(buyer_price, 2) if buyer_price is not None else None,
        "seller_price": _safe_number(seller_price, 2) if seller_price is not None else None,
        "gamma_strength": _safe_number(gross_gamma), "net_gamma": _safe_number(net_gamma),
        "gamma_bias": _safe_number(gamma_bias), "delta_notional": _safe_number(net_delta),
        "delta_bias": _safe_number(delta_bias), "theta_per_day": _safe_number(net_theta),
        "theta_bias": _safe_number(theta_bias), "vega_per_iv_point": _safe_number(net_vega),
        "vega_bias": _safe_number(vega_bias), "iv_percent": _safe_number(avg_iv, 4),
        "trade_count": trade_count, "raw_amount": _safe_number(raw_amount, 2),
        "weighted_amount": _safe_number(gross_amount, 2),
        "theta_retention_percent": _safe_number(100.0 * theta_retention_numerator / raw_amount, 1) if raw_amount else 0.0,
        "first_seen": _iso_utc(first_seen), "half_life_days": half_life_days,
        "data_quality": int(_clamp(25.0 + 18.0 * math.log1p(max(trade_count, 0)), 0.0, 95.0)),
    }


class ExpiryWaveController(http.Controller):

    @http.route("/api/expiry-wave/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def expiry_wave_json(self, asset):
        asset = asset.upper()
        if asset not in ("BTC", "ETH"):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        year_end = datetime(now.year + 1, 1, 1)
        cr = request.env.cr
        cr.execute("""
            SELECT DISTINCT expiration
              FROM dankbit_trade
             WHERE name ILIKE %s
               AND expiration > %s
               AND expiration < %s
               AND active = TRUE
               AND iv <> 0
             ORDER BY expiration
        """, (f"{asset}-%", now, year_end))
        expirations = [row[0] for row in cr.fetchall() if row and row[0]]
        selected = _select_expiries(expirations, now)
        if not selected:
            payload = {
                "asset": asset, "available": False, "nodes": [], "trade_count": 0,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "display_only": True,
            }
            return request.make_response(
                json.dumps(payload),
                headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
            )

        expiry_values = [expiry for expiry, _kind in selected]
        cr.execute("""
            SELECT expiration, strike, option_type, direction,
                   date_trunc('hour', deribit_ts) AS trade_hour,
                   SUM(amount),
                   SUM(iv * amount) / NULLIF(SUM(amount), 0),
                   SUM(price * amount) / NULLIF(SUM(amount), 0),
                   SUM(index_price * amount) / NULLIF(SUM(amount), 0),
                   COUNT(*), MIN(deribit_ts)
              FROM dankbit_trade
             WHERE name ILIKE %s
               AND expiration = ANY(%s)
               AND active = TRUE
               AND iv <> 0
               AND deribit_ts IS NOT NULL
               AND deribit_ts <= %s
             GROUP BY expiration, strike, option_type, direction, trade_hour
             ORDER BY expiration, trade_hour
        """, (f"{asset}-%", expiry_values, now))
        rows = cr.fetchall()
        spot = float(request.env["dankbit.trade"].get_index_price(asset) or 0.0)

        nodes = []
        total_trade_count = 0
        for expiry, kind in selected:
            node = _build_expiry_node(
                expiry, kind, rows, now, spot,
            )
            if node:
                nodes.append(node)
                total_trade_count += int(node["trade_count"])

        cumulative_variance = 0.0
        previous_time = now
        for node in nodes:
            expiry = datetime.fromisoformat(node["expiry"].replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
            interval_days = max(0.0, (expiry - previous_time).total_seconds() / 86400.0)
            sigma = max(0.0, float(node["iv_percent"] or 0.0) / 100.0)
            cumulative_variance += (spot * sigma) ** 2 * interval_days / 365.0
            one_sigma = math.sqrt(max(cumulative_variance, 0.0))
            # Vega changes the breadth under an IV repricing scenario; Delta
            # skews the corridor; signed Gamma changes its width modestly.
            width = one_sigma * _clamp(1.0 + 0.20 * node["vega_bias"] + 0.15 * node["gamma_bias"], 0.70, 1.30)
            delta_skew = _clamp(node["delta_bias"], -1.0, 1.0) * 0.18
            node["range_low"] = _safe_number(node["price"] - width * (1.0 - delta_skew), 2)
            node["range_high"] = _safe_number(node["price"] + width * (1.0 + delta_skew), 2)
            node["days_from_now"] = round(max(0.0, (expiry - now).total_seconds() / 86400.0), 2)
            previous_time = expiry

        payload = {
            "asset": asset, "available": bool(nodes), "nodes": nodes,
            "spot": _safe_number(spot, 2), "trade_count": total_trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "year_end": year_end.replace(tzinfo=timezone.utc).isoformat(),
            "display_only": True,
            "method": {
                "node_source": "full recorded trade history for each expiry, from its first stored trade to now",
                "gamma": "buyer/seller dollar-Gamma peak midpoint by expiry",
                "delta": "directional asymmetry of the uncertainty corridor",
                "vega": "IV-sensitivity adjustment to corridor width",
                "theta": "approximate decay adjustment to trade-flow relevance only",
                "timing": "expiry dates are timeline anchors, not promised price-touch dates",
                "limitations": "trade direction is aggressor flow, not confirmed dealer inventory; public trades do not reveal whether a position remains open or was later closed",
            },
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )
