# -*- coding: utf-8 -*-
"""Display-only probabilistic market-maker hedge simulator."""
import json
import math
from datetime import datetime, timedelta, timezone
from odoo import http
from odoo.http import request

YEAR = 365.0 * 86400.0


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def _iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat()


def _last_friday(day):
    return day.weekday() == 4 and (day + timedelta(days=7)).month != day.month


def _metrics(spot, strike, iv, years, is_put):
    if min(spot, strike, iv, years) <= 0:
        return 0.0, 0.0
    sigma = iv / 100.0
    root = math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * years) / (sigma * root)
    density = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    cdf = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return (cdf - 1.0 if is_put else cdf), density / (spot * sigma * root)


def _reply(payload, status=200):
    return request.make_response(json.dumps(payload), status=status, headers=[
        ("Content-Type", "application/json"), ("Cache-Control", "no-cache")])


class MMHedgeController(http.Controller):
    @http.route("/api/mm-hedge/<string:asset>", type="http", auth="user",
                website=False, csrf=False)
    def mm_hedge(self, asset, **params):
        asset = asset.upper()
        if asset not in ("BTC", "ETH"):
            return _reply({"error": "Unknown asset"}, 400)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cr = request.env.cr
        cr.execute("""SELECT DISTINCT expiration FROM dankbit_trade
          WHERE name ILIKE %s AND expiration>%s AND active=TRUE AND iv<>0
          ORDER BY expiration""", (asset + "-%", now))
        expiries = [row[0] for row in cr.fetchall()]
        ordinal = expiries[:3]
        specs = []
        for index, expiry in enumerate(ordinal):
            specs.append(("D" + str(index + 1), expiry, "short"))
        used = set(ordinal)
        for expiry in expiries:
            day = expiry.date()
            if expiry in used:
                continue
            if _last_friday(day) and day.month == 12:
                kind = "YE"
            elif _last_friday(day):
                kind = "M"
            elif day.weekday() == 4:
                kind = "W"
            else:
                continue
            specs.append((kind, expiry, "structural"))
        # Keep representative horizons bounded while retaining selectable dates.
        bounded = []
        limits = {"D1": 1, "D2": 1, "D3": 1, "W": 3, "M": 3, "YE": 1}
        for kind in limits:
            bounded.extend([row for row in specs if row[0] == kind][:limits[kind]])
        specs = sorted(bounded, key=lambda row: row[1])
        requested = set((params.get("expiries") or "auto").split(","))
        selected = specs if "auto" in requested else [row for row in specs if _iso(row[1]) in requested]
        if not selected:
            return _reply({"asset": asset, "available": False, "display_only": True,
                           "expiries": [{"kind": k, "expiry": _iso(e), "group": g} for k,e,g in specs],
                           "targets": [], "generated_at": _iso(now)})

        selected_expiries = [row[1] for row in selected]
        cr.execute("""SELECT expiration,name,strike,option_type,direction,
          date_trunc('hour',deribit_ts),SUM(amount),
          SUM(iv*amount)/NULLIF(SUM(amount),0),BOOL_OR(is_block_trade),
          BOOL_OR(block_trade_id IS NOT NULL),COUNT(*),MIN(deribit_ts)
          FROM dankbit_trade WHERE name ILIKE %s AND expiration=ANY(%s)
          AND active=TRUE AND iv<>0 AND deribit_ts<=%s
          GROUP BY expiration,name,strike,option_type,direction,date_trunc('hour',deribit_ts)
          ORDER BY expiration,date_trunc('hour',deribit_ts)""",
                   (asset + "-%", selected_expiries, now))
        rows = cr.fetchall()
        spot = float(request.env["dankbit.trade"].get_index_price(asset) or 0.0)
        try:
            open_interest = request.env["dankbit.trade"].get_open_interest_by_currency(asset) or {}
            oi_available = bool(open_interest)
        except Exception:
            open_interest, oi_available = {}, False
        candles = request.env["dankbit.trade"].get_candles(asset, interval="1h", limit=12) or []
        ordered = sorted(candles, key=lambda c: int(c.get("t", 0)))
        closes = [float(c["c"]) for c in ordered if c.get("c") is not None]
        current_move = closes[-1] - closes[-5] if len(closes) >= 5 else (closes[-1] - closes[0] if len(closes) >= 2 else 0.0)

        candidates = []
        total_rows = 0
        for kind, expiry, group_name in selected:
            relevant = [row for row in rows if row[0] == expiry]
            if not relevant or spot <= 0:
                continue
            remaining_years = max(60.0, (expiry - now).total_seconds()) / YEAR
            avg_iv_num = sum(float(row[7] or 0) * float(row[6] or 0) for row in relevant)
            avg_iv_den = sum(float(row[6] or 0) for row in relevant)
            avg_iv = avg_iv_num / avg_iv_den if avg_iv_den else 50.0
            expected = max(spot * avg_iv / 100.0 * math.sqrt(remaining_years), spot * 0.01)
            grid_low, grid_high = max(1.0, spot - max(spot * .18, expected * 2.5)), spot + max(spot * .18, expected * 2.5)
            grid = [grid_low + (grid_high-grid_low) * i / 160.0 for i in range(161)]
            positions, first_seen = [], None
            identity_sum = relevance_sum = amount_sum = 0.0
            for row in relevant:
                _, name, strike, option_type, direction, trade_hour, amount, iv, is_block, has_block_id, trade_count, first_trade = row
                amount = float(amount or 0.0)
                if amount <= 0 or option_type not in ("call", "put") or direction not in ("buy", "sell"):
                    continue
                # Identity uses execution-pattern evidence only. Greeks and
                # moneyness deliberately belong to Hedge Relevance below.
                volume_score = _clamp(math.log1p(amount) / math.log(51.0))
                repeat_score = _clamp(math.log1p(int(trade_count or 0)) / math.log(11.0))
                block_score = 1.0 if (is_block or has_block_id) else 0.0
                mm_identity = _clamp(.15 + .30*volume_score + .20*repeat_score +
                                     .35*block_score, .10, .92)
                age_hours = max(0.0, (now - trade_hour).total_seconds()/3600.0)
                original_hours = max(1.0, (expiry - trade_hour).total_seconds()/3600.0)
                half_life = _clamp(original_hours*.28, 6.0, 720.0)
                freshness = .20 + .80 * 2**(-age_hours/half_life)
                oi = float(open_interest.get(name) or 0.0)
                oi_support = _clamp(oi / max(amount, 1e-9)) if oi_available else .65
                survival = freshness * (.35 + .65*oi_support)
                _, gamma_now = _metrics(spot, float(strike), float(iv or avg_iv),
                                        remaining_years, option_type == "put")
                atm_relevance = math.exp(-abs(math.log(max(float(strike), 1.0) / spot)) / .22)
                gamma_relevance = 1.0 - math.exp(-abs(gamma_now) * spot / 4.0)
                hours_left = max(0.0, (expiry-now).total_seconds()/3600.0)
                urgency = math.exp(-hours_left/720.0)
                hedge_relevance = _clamp(.35*atm_relevance + .35*gamma_relevance +
                                         .15*urgency + .15*oi_support, .05, 1.0)
                # Maker is inferred as the opposite option side of the taker.
                maker_sign = -1.0 if direction == "buy" else 1.0
                effective_amount = amount * mm_identity * hedge_relevance * survival
                positions.append((float(strike), option_type == "put", float(iv or avg_iv),
                                  maker_sign, effective_amount))
                identity_sum += mm_identity * amount
                relevance_sum += hedge_relevance * amount
                amount_sum += amount
                first_seen = first_trade if first_seen is None else min(first_seen, first_trade)
                total_rows += int(trade_count or 0)
            if not positions:
                continue
            curve = []
            for price in grid:
                net_gamma = net_delta = 0.0
                for strike, is_put, iv, maker_sign, amount in positions:
                    delta, gamma = _metrics(price, strike, iv, remaining_years, is_put)
                    net_gamma += maker_sign * gamma * amount * price
                    net_delta += maker_sign * delta * amount
                curve.append((price, net_gamma, -net_delta))
            max_gamma = max(abs(point[1]) for point in curve) or 1e-9
            peak = max(curve, key=lambda point: abs(point[1]) *
                       (.25 + .75*math.exp(-abs(point[0]-spot)/max(expected,1e-9))))
            proximity = math.exp(-abs(peak[0]-spot)/max(expected,1e-9))
            mm_identity = identity_sum/max(amount_sum,1e-9)
            hedge_relevance = relevance_sum/max(amount_sum,1e-9)
            concentration = abs(peak[1])/max_gamma
            score = abs(peak[1]) * (.45+.55*proximity)
            if peak[1] < 0:
                reaction = "ACCELERATE"
                direction = "UP" if peak[0] >= spot else "DOWN"
            else:
                arrival_direction = 1 if peak[0] >= spot else -1
                opposing = (current_move > 0 and arrival_direction > 0) or (current_move < 0 and arrival_direction < 0)
                reaction = "REVERSE" if opposing and abs(current_move) > spot*.002 else "PIN"
                direction = "DOWN" if peak[0] >= spot else "UP" if reaction == "REVERSE" else "FLAT"
            quality = (.25 + .20*_clamp(math.sqrt(len(relevant))/12) +
                       .25*mm_identity + .15*hedge_relevance +
                       (.15 if oi_available else 0))
            confidence = round(100*_clamp(quality*concentration, .25, .88))
            candidates.append({"kind":kind, "group":group_name, "expiry":_iso(expiry),
                "price":round(peak[0],2), "reaction":reaction, "direction":direction,
                "confidence":confidence, "score":score, "net_mm_gamma":peak[1],
                "expected_hedge":peak[2], "mm_identity":round(mm_identity*100),
                "hedge_relevance":round(hedge_relevance*100),
                "first_seen":_iso(first_seen) if first_seen else None,
                "trade_count":sum(int(row[10] or 0) for row in relevant)})

        targets = []
        for group_name, label in (("short", "SHORT-TERM"), ("structural", "STRUCTURAL")):
            choices = [row for row in candidates if row["group"] == group_name]
            if choices:
                winner = max(choices, key=lambda row: row["score"])
                winner = dict(winner)
                winner["horizon"] = label
                winner.pop("score", None)
                targets.append(winner)
        return _reply({"asset":asset, "available":bool(targets), "display_only":True,
            "engine":"MM Hedge Simulator", "targets":targets,
            "expiries":[{"kind":k,"expiry":_iso(e),"group":g,
                         "selected":any(e==x[1] and k==x[0] for x in selected)} for k,e,g in specs],
            "index_price":spot, "current_move":current_move, "trade_count":total_rows,
            "oi_available":oi_available, "generated_at":_iso(now),
            "limitations":"Probabilistic inference from public taker-side trades; hedge venue and actual inventory are unknown."})
