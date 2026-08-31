# -*- coding: utf-8 -*-
"""Display-only option strategy detection and payoff curves.

This controller is intentionally isolated from Forecast, Gamma Horizon,
Bands, Smart Liquidity, Greeks Flow, FOMO and Signal Bot.  It reads the same
public Deribit trades but never writes a model or exports a value to another
engine.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from odoo import http
from odoo.http import request


def _is_last_friday(day):
    return day.weekday() == 4 and (day + timedelta(days=7)).month != day.month


def _side_sign(direction):
    return 1.0 if direction == "buy" else -1.0


def _leg_label(leg):
    return "%s %s %s" % (
        "Buy" if leg["direction"] == "buy" else "Sell",
        f'{leg["strike"]:,.0f}',
        "Call" if leg["option_type"] == "call" else "Put",
    )


def _classify_strategy(legs):
    """Return (name, bias) for one normalized leg group."""
    legs = sorted(legs, key=lambda row: (row["expiration"], row["strike"], row["option_type"]))
    count = len(legs)
    if count == 1:
        leg = legs[0]
        long_side = leg["direction"] == "buy"
        name = ("Long " if long_side else "Short ") + ("Call" if leg["option_type"] == "call" else "Put")
        bias = "bullish" if (leg["option_type"] == "call") == long_side else "bearish"
        return name, bias

    expiries = {leg["expiration"] for leg in legs}
    types = {leg["option_type"] for leg in legs}
    directions = {leg["direction"] for leg in legs}

    if len(expiries) > 1:
        if count == 2 and len(types) == 1 and len(directions) == 2:
            same_strike = legs[0]["strike"] == legs[1]["strike"]
            return ("Calendar Spread" if same_strike else "Diagonal Spread"), "term"
        return "Multi-Expiry Structure", "term"

    if count == 2:
        a, b = sorted(legs, key=lambda row: row["strike"])
        equal_ratio = abs(a["amount"] - b["amount"]) <= 0.08 * max(a["amount"], b["amount"], 1e-9)
        if len(types) == 1:
            if not equal_ratio:
                return ("Call Ratio Spread" if a["option_type"] == "call" else "Put Ratio Spread"), "directional"
            if a["option_type"] == "call":
                if a["direction"] == "buy" and b["direction"] == "sell":
                    return "Bull Call Spread", "bullish"
                if a["direction"] == "sell" and b["direction"] == "buy":
                    return "Bear Call Spread", "bearish"
            else:
                if a["direction"] == "sell" and b["direction"] == "buy":
                    return "Bear Put Spread", "bearish"
                if a["direction"] == "buy" and b["direction"] == "sell":
                    return "Bull Put Spread", "bullish"
            return "Same-Side Vertical", "volatility"

        call = next(leg for leg in legs if leg["option_type"] == "call")
        put = next(leg for leg in legs if leg["option_type"] == "put")
        same_strike = call["strike"] == put["strike"]
        if call["direction"] == put["direction"]:
            prefix = "Long" if call["direction"] == "buy" else "Short"
            return f"{prefix} {'Straddle' if same_strike else 'Strangle'}", "volatility" if prefix == "Long" else "neutral"
        if call["direction"] == "buy" and put["direction"] == "sell":
            return "Bullish Risk Reversal", "bullish"
        return "Bearish Risk Reversal", "bearish"

    if count == 3 and len(types) == 1:
        ordered = sorted(legs, key=lambda row: row["strike"])
        outer_same = ordered[0]["direction"] == ordered[2]["direction"]
        middle_opposite = ordered[1]["direction"] != ordered[0]["direction"]
        outer_amount = (ordered[0]["amount"] + ordered[2]["amount"]) / 2.0
        ratio_ok = abs(ordered[1]["amount"] - 2.0 * outer_amount) <= 0.20 * max(2.0 * outer_amount, 1e-9)
        if outer_same and middle_opposite and ratio_ok:
            prefix = "Long" if ordered[0]["direction"] == "buy" else "Short"
            return f"{prefix} {'Call' if ordered[0]['option_type'] == 'call' else 'Put'} Butterfly", "neutral"

    if count == 4:
        puts = sorted([leg for leg in legs if leg["option_type"] == "put"], key=lambda row: row["strike"])
        calls = sorted([leg for leg in legs if leg["option_type"] == "call"], key=lambda row: row["strike"])
        if len(puts) == 2 and len(calls) == 2:
            long_wings = puts[0]["direction"] == "buy" and puts[1]["direction"] == "sell" and calls[0]["direction"] == "sell" and calls[1]["direction"] == "buy"
            short_wings = puts[0]["direction"] == "sell" and puts[1]["direction"] == "buy" and calls[0]["direction"] == "buy" and calls[1]["direction"] == "sell"
            if long_wings:
                return "Iron Condor", "neutral"
            if short_wings:
                return "Reverse Iron Condor", "volatility"
        if len(types) == 1:
            ordered = sorted(legs, key=lambda row: row["strike"])
            pattern = [row["direction"] for row in ordered]
            if pattern in (["buy", "sell", "sell", "buy"], ["sell", "buy", "buy", "sell"]):
                return ("Long Condor" if pattern[0] == "buy" else "Short Condor"), "neutral"

    return f"Custom {count}-Leg Structure", "complex"


def _normalize_legs(rows):
    """Merge identical legs without losing premium/amount information."""
    buckets = {}
    for row in rows:
        key = (row["instrument"], row["direction"])
        bucket = buckets.setdefault(key, dict(row, amount=0.0, premium_amount=0.0))
        amount = float(row["amount"] or 0.0)
        bucket["amount"] += amount
        bucket["premium_amount"] += float(row["price"] or 0.0) * amount
        bucket["timestamp"] = max(bucket["timestamp"], row["timestamp"])
    legs = []
    for bucket in buckets.values():
        amount = bucket.pop("amount")
        premium_amount = bucket.pop("premium_amount")
        bucket["amount"] = amount
        bucket["price"] = premium_amount / amount if amount else 0.0
        legs.append(bucket)
    return sorted(legs, key=lambda row: (row["expiration"], row["strike"], row["option_type"], row["direction"]))


def _payoff_curve(legs, spot):
    strikes = [float(leg["strike"]) for leg in legs]
    low = max(0.01, min(strikes + [spot]) * 0.72)
    high = max(strikes + [spot]) * 1.28
    if high <= low:
        high = low * 1.5
    points = []
    for idx in range(121):
        underlying = low + (high - low) * idx / 120.0
        pnl = 0.0
        for leg in legs:
            sign = _side_sign(leg["direction"])
            intrinsic = max(underlying - leg["strike"], 0.0) if leg["option_type"] == "call" else max(leg["strike"] - underlying, 0.0)
            # Deribit inverse option premiums are quoted in the base asset.
            # Converting at the trade's own index price produces a readable,
            # explicitly approximate USD payoff for comparative visualization.
            trade_spot = float(leg.get("index_price") or spot or 0.0)
            premium_usd = float(leg["price"] or 0.0) * trade_spot
            pnl += sign * (intrinsic - premium_usd) * float(leg["amount"] or 0.0)
        points.append({"price": underlying, "value": pnl})

    breakevens = []
    for left, right in zip(points, points[1:]):
        if left["value"] == 0:
            breakevens.append(left["price"])
        elif left["value"] * right["value"] < 0:
            ratio = abs(left["value"]) / (abs(left["value"]) + abs(right["value"]))
            breakevens.append(left["price"] + ratio * (right["price"] - left["price"]))
    values = [point["value"] for point in points]
    return points, breakevens[:4], max(values), min(values)


def _serialize_strategy(group_id, source, confidence, rows, spot):
    legs = _normalize_legs(rows)
    if not legs:
        return None
    name, bias = _classify_strategy(legs)
    payoff, breakevens, max_profit, max_loss = _payoff_curve(legs, spot)
    expiry = max(leg["expiration"] for leg in legs)
    timestamp = max(leg["timestamp"] for leg in legs)
    net_premium = 0.0
    for leg in legs:
        trade_spot = float(leg.get("index_price") or spot or 0.0)
        net_premium += -_side_sign(leg["direction"]) * float(leg["price"] or 0.0) * trade_spot * float(leg["amount"] or 0.0)
    return {
        "id": group_id,
        "name": name,
        "bias": bias,
        "source": source,
        "confidence": confidence,
        "expiry": expiry.replace(tzinfo=timezone.utc).isoformat(),
        "entered_at": timestamp.replace(tzinfo=timezone.utc).isoformat(),
        "net_premium_usd": net_premium,
        "max_profit_in_view_usd": max_profit,
        "max_loss_in_view_usd": max_loss,
        "breakevens": breakevens,
        "payoff": payoff,
        "legs": [{
            "instrument": leg["instrument"],
            "label": _leg_label(leg),
            "direction": leg["direction"],
            "option_type": leg["option_type"],
            "strike": leg["strike"],
            "amount": leg["amount"],
            "price": leg["price"],
            "expiration": leg["expiration"].replace(tzinfo=timezone.utc).isoformat(),
        } for leg in legs],
    }


class OptionStrategyRadarController(http.Controller):

    @http.route("/api/option-strategy-radar/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def option_strategy_radar_json(self, asset):
        asset = asset.upper()
        if asset not in ("BTC", "ETH"):
            return request.make_response(json.dumps({"error": "Unknown asset"}), headers=[("Content-Type", "application/json")])

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        daily_dates = {
            (now + timedelta(days=offset)).date(): f"D{offset}"
            for offset in (1, 2, 3)
        }
        cr = request.env.cr
        cr.execute("""
            SELECT DISTINCT expiration FROM dankbit_trade
             WHERE name ILIKE %s AND expiration > %s AND active = TRUE AND iv <> 0
             ORDER BY expiration
        """, (f"{asset}-%", now))
        expirations = [row[0] for row in cr.fetchall() if row[0]]
        horizon_labels = defaultdict(list)
        selected = []
        for expiry in expirations:
            daily_label = daily_dates.get(expiry.date())
            if daily_label:
                selected.append(expiry)
                horizon_labels[expiry].append(daily_label)
        weekly = [exp for exp in expirations if exp.date().weekday() == 4 and not _is_last_friday(exp.date())][:3]
        monthly = [exp for exp in expirations if _is_last_friday(exp.date()) and exp.date().month != 12][:3]
        year_end = [exp for exp in expirations if _is_last_friday(exp.date()) and exp.date().month == 12][:1]
        for label, targets in (("W", weekly), ("M", monthly), ("YE", year_end)):
            for expiry in targets:
                selected.append(expiry)
                horizon_labels[expiry].append(label)
        selected = sorted(set(selected))
        if not selected:
            payload = {"asset": asset, "strategies": [], "summary": {}, "display_only": True}
            return request.make_response(json.dumps(payload), headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")])

        cr.execute("""
            SELECT name, strike, option_type, direction, expiration, amount,
                   price, index_price, deribit_ts, deribit_trade_identifier,
                   block_trade_id, is_block_trade
              FROM dankbit_trade
             WHERE name ILIKE %s AND expiration = ANY(%s) AND active = TRUE
               AND iv <> 0 AND deribit_ts IS NOT NULL AND deribit_ts <= %s
               AND (block_trade_id IS NOT NULL OR deribit_ts >= %s)
             ORDER BY deribit_ts
        """, (f"{asset}-%", selected, now, now - timedelta(hours=120)))
        raw_rows = cr.fetchall()
        rows = [{
            "instrument": row[0], "strike": float(row[1]), "option_type": row[2],
            "direction": row[3], "expiration": row[4], "amount": float(row[5] or 0.0),
            "price": float(row[6] or 0.0), "index_price": float(row[7] or 0.0),
            "timestamp": row[8], "trade_id": row[9], "block_id": row[10],
            "is_block": bool(row[11] or row[10]),
        } for row in raw_rows if row[1] and row[2] in ("call", "put") and row[5]]

        spot = float(request.env["dankbit.trade"].get_index_price(asset) or 0.0)
        block_groups = defaultdict(list)
        screen_candidates = []
        for row in rows:
            if row["block_id"]:
                block_groups[str(row["block_id"])].append(row)
            elif not row["is_block"]:
                screen_candidates.append(row)

        strategies = []
        for block_id, group in block_groups.items():
            strategy = _serialize_strategy("block:" + block_id, "confirmed_block", 98, group, spot)
            if strategy:
                strategy["horizon"] = "/".join(horizon_labels.get(max(row["expiration"] for row in group), [])) or "TERM"
                strategies.append(strategy)

        # Conservative public-screen inference: only 2-4 trades sharing an
        # expiry and a three-second bucket, with closely matched amounts.
        # A trade is consumed once because each bucket is evaluated once.
        screen_groups = defaultdict(list)
        for row in screen_candidates:
            bucket = int(row["timestamp"].replace(tzinfo=timezone.utc).timestamp() // 3)
            screen_groups[(row["expiration"], bucket)].append(row)
        for (expiry, bucket), group in screen_groups.items():
            if not 2 <= len(group) <= 4:
                continue
            amounts = [row["amount"] for row in group]
            if min(amounts) <= 0 or max(amounts) / min(amounts) > 1.08:
                continue
            distinct_legs = {(row["instrument"], row["direction"]) for row in group}
            if len(distinct_legs) < 2:
                continue
            time_span = (max(row["timestamp"] for row in group) - min(row["timestamp"] for row in group)).total_seconds()
            confidence = int(max(60, min(84, 84 - time_span * 5 - (max(amounts) / min(amounts) - 1) * 100)))
            strategy = _serialize_strategy(f"screen:{expiry.isoformat()}:{bucket}", "probable_screen", confidence, group, spot)
            if strategy and not strategy["name"].startswith("Custom"):
                strategy["horizon"] = "/".join(horizon_labels.get(expiry, [])) or "TERM"
                strategies.append(strategy)

        strategies.sort(key=lambda row: row["entered_at"], reverse=True)
        strategies.sort(key=lambda row: (row["source"] != "confirmed_block", -row["confidence"]))
        strategies = strategies[:120]
        summary = {
            "confirmed_blocks": sum(row["source"] == "confirmed_block" for row in strategies),
            "probable_screen": sum(row["source"] == "probable_screen" for row in strategies),
            "bullish": sum(row["bias"] == "bullish" for row in strategies),
            "bearish": sum(row["bias"] == "bearish" for row in strategies),
            "neutral_or_volatility": sum(row["bias"] in ("neutral", "volatility") for row in strategies),
        }
        payload = {
            "asset": asset, "spot": spot, "strategies": strategies, "summary": summary,
            "generated_at": datetime.now(timezone.utc).isoformat(), "display_only": True,
            "payoff_note": "Approximate USD-equivalent payoff; inverse premiums converted at trade-time index price.",
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )
