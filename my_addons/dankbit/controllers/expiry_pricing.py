# -*- coding: utf-8 -*-
"""Independent, display-only nearest-expiry pricing compass.

Nothing in this module is imported by Forecast, Gamma Horizon, Strategy Radar,
Bands, Smart Liquidity or Signal Bot.  It only reads trades/candles and returns
a visual scenario which is recalculated by the browser every fifteen minutes.
"""

import json
import math
from datetime import datetime, timedelta, timezone

from odoo import http
from odoo.http import request


def _cdf(value):
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _greeks(spot, strike, iv_percent, years, option_type):
    if min(spot, strike, iv_percent, years) <= 0:
        return 0.0, 0.0
    sigma = iv_percent / 100.0
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * years) / (sigma * root_t)
    density = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    gamma = density / (spot * sigma * root_t)
    delta = _cdf(d1) if option_type == "call" else _cdf(d1) - 1.0
    return delta, gamma


def _weighted_level(rows, key, fallback):
    chosen = [row for row in rows if row["bucket"] == key and row["gamma_weight"] > 0]
    total = sum(row["gamma_weight"] for row in chosen)
    return sum(row["strike"] * row["gamma_weight"] for row in chosen) / total if total else fallback


def _clamp(value, low, high):
    return max(low, min(high, value))


class ExpiryPricingController(http.Controller):

    @http.route("/api/expiry-pricing/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def expiry_pricing_json(self, asset):
        asset = asset.upper()
        if asset not in ("BTC", "ETH"):
            return request.make_response(json.dumps({"error": "Unknown asset"}), headers=[("Content-Type", "application/json")])

        now_aware = datetime.now(timezone.utc)
        now = now_aware.replace(tzinfo=None)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cr = request.env.cr
        cr.execute("""
            SELECT MIN(expiration) FROM dankbit_trade
             WHERE name ILIKE %s AND expiration > %s AND active = TRUE AND iv <> 0
        """, (f"{asset}-%", now))
        expiry = cr.fetchone()[0]
        if not expiry:
            payload = {"asset": asset, "available": False, "display_only": True,
                       "generated_at": now_aware.isoformat(), "refresh_seconds": 900}
            return request.make_response(json.dumps(payload), headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")])

        cr.execute("""
            SELECT strike, option_type, direction, amount, iv, deribit_ts,
                   index_price, is_block_trade, block_trade_id
              FROM dankbit_trade
             WHERE name ILIKE %s AND expiration = %s AND active = TRUE AND iv <> 0
               AND deribit_ts >= %s AND deribit_ts <= %s
             ORDER BY deribit_ts
        """, (f"{asset}-%", expiry, day_start, now))
        raw = cr.fetchall()
        spot = float(request.env["dankbit.trade"].get_index_price(asset) or 0.0)
        if not spot and raw:
            spot = float(raw[-1][6] or 0.0)
        years = max((expiry - now).total_seconds(), 60.0) / (365.0 * 86400.0)

        rows = []
        flows = {"BC": 0.0, "BP": 0.0, "SC": 0.0, "SP": 0.0}
        contracts = {"BC": 0.0, "BP": 0.0, "SC": 0.0, "SP": 0.0}
        iv_numer = iv_denom = 0.0
        latest_trade = None
        for strike, option_type, direction, amount, iv, entered, index_price, is_block, block_id in raw:
            if not strike or option_type not in ("call", "put") or direction not in ("buy", "sell"):
                continue
            amount = float(amount or 0.0)
            if amount <= 0:
                continue
            trade_spot = float(index_price or spot or 0.0)
            delta, gamma = _greeks(max(trade_spot, 1e-9), float(strike), float(iv or 0.0), years, option_type)
            age_hours = max(0.0, (now - entered).total_seconds() / 3600.0)
            freshness = 0.35 + 0.65 * math.pow(2.0, -age_hours / 4.0)
            quality = 1.12 if (is_block or block_id) else 1.0
            bucket = ("B" if direction == "buy" else "S") + ("C" if option_type == "call" else "P")
            delta_weight = amount * abs(delta) * freshness * quality
            gamma_weight = amount * abs(gamma) * max(trade_spot, 1.0) * freshness * quality
            effective = delta_weight + gamma_weight
            flows[bucket] += effective
            contracts[bucket] += amount
            iv_numer += float(iv or 0.0) * amount
            iv_denom += amount
            latest_trade = entered if latest_trade is None else max(latest_trade, entered)
            rows.append({"bucket": bucket, "strike": float(strike), "gamma_weight": gamma_weight})

        atm_iv = iv_numer / iv_denom if iv_denom else 0.0
        expected_move = spot * (atm_iv / 100.0) * math.sqrt(years) if spot and atm_iv else 0.0
        expected_move = max(expected_move, spot * 0.003 if spot else 1.0)
        targets = {
            "bcg": _weighted_level(rows, "BC", spot + expected_move * 0.55),
            "bpg": _weighted_level(rows, "BP", spot - expected_move * 0.55),
            "scg": _weighted_level(rows, "SC", spot + expected_move),
            "spg": _weighted_level(rows, "SP", spot - expected_move),
        }

        bullish = flows["BC"] + flows["SP"]
        bearish = flows["BP"] + flows["SC"]
        total = bullish + bearish
        direction_score = (bullish - bearish) / total if total else 0.0
        buyer_total = flows["BC"] + flows["BP"]
        seller_total = flows["SC"] + flows["SP"]
        buyer_bias = (flows["BC"] - flows["BP"]) / buyer_total if buyer_total else 0.0
        seller_bias = (flows["SP"] - flows["SC"]) / seller_total if seller_total else 0.0
        count_factor = _clamp(len(raw) / 80.0, 0.0, 1.0)
        freshness_factor = 0.0 if latest_trade is None else _clamp(1.0 - (now - latest_trade).total_seconds() / 14400.0, 0.0, 1.0)
        confidence = round(100.0 * (0.35 * abs(direction_score) + 0.35 * count_factor + 0.30 * freshness_factor))

        # Closed/current real UTC-day candles.  Future candles are a coherent,
        # deterministic path; no random term means identical inputs never repaint.
        candles_raw = request.env["dankbit.trade"].get_candles(asset, interval="1h", limit=48) or []
        actual = []
        for candle in sorted(candles_raw, key=lambda item: int(item.get("t", 0))):
            ts_ms = int(candle.get("t", 0))
            stamp = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
            if day_start <= stamp <= now:
                actual.append({"time": ts_ms // 1000, "open": float(candle["o"]), "high": float(candle["h"]), "low": float(candle["l"]), "close": float(candle["c"]), "kind": "actual"})
        actual = actual[-24:]
        anchor = actual[-1]["close"] if actual else spot
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        future_slots = max(0, 24 - len(actual))
        expiry_slots = max(0, int(math.ceil((expiry - current_hour - timedelta(hours=1)).total_seconds() / 3600.0)))
        future_slots = min(future_slots, expiry_slots)
        forecast = []
        prev = anchor
        for idx in range(future_slots):
            decay = math.exp(-idx / 8.0)
            target = targets["bcg"] if direction_score >= 0 else targets["bpg"]
            attraction = (target - prev) * (0.12 + 0.10 * abs(direction_score)) * decay
            flow_step = direction_score * expected_move * 0.095 * decay
            barrier = 0.0
            if prev > targets["scg"]:
                barrier -= (prev - targets["scg"]) * 0.22
            if prev < targets["spg"]:
                barrier += (targets["spg"] - prev) * 0.22
            close = prev + attraction + flow_step + barrier
            max_step = expected_move * 0.20
            close = prev + _clamp(close - prev, -max_step, max_step)
            body = abs(close - prev)
            conflict = 1.0 - abs(direction_score)
            wick = expected_move * (0.025 + 0.035 * conflict) * (1.0 + idx / 48.0)
            forecast.append({
                "time": int((current_hour + timedelta(hours=idx + 1)).replace(tzinfo=timezone.utc).timestamp()),
                "open": prev, "high": max(prev, close) + wick, "low": min(prev, close) - wick,
                "close": close, "kind": "forecast", "confidence": round(confidence * decay),
            })
            prev = close

        if abs(direction_score) < 0.12:
            regime = "Theta / Gamma Pin"
        elif seller_total > buyer_total * 1.25:
            regime = "Seller Range"
        elif direction_score > 0 and buyer_bias < -0.20:
            regime = "Bullish Rotation"
        elif direction_score < 0 and buyer_bias > 0.20:
            regime = "Bearish Rotation"
        else:
            regime = "Bullish Continuation" if direction_score > 0 else "Bearish Continuation"

        payload = {
            "asset": asset, "available": True, "display_only": True,
            "refresh_seconds": 900, "generated_at": now_aware.isoformat(),
            "expiry": expiry.replace(tzinfo=timezone.utc).isoformat(), "hours_to_expiry": round(max(0.0, (expiry - now).total_seconds() / 3600.0), 2),
            "spot": spot, "atm_iv": round(atm_iv, 3), "expected_move": expected_move,
            "contracts": contracts, "effective_flow": flows,
            "direction_score": round(direction_score, 4), "buyer_bias": round(buyer_bias, 4), "seller_bias": round(seller_bias, 4),
            "confidence": confidence, "regime": regime, "targets": targets,
            "actual": actual, "forecast": forecast, "trade_count": len(raw),
        }
        return request.make_response(json.dumps(payload), headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")])
