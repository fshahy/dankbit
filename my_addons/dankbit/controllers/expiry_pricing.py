# -*- coding: utf-8 -*-
"""Independent Expiry Trace engine. Its output is never consumed by Forecast."""
import json
import math
from datetime import datetime, timedelta, timezone
from odoo import http
from odoo.http import request

BUCKETS = ("BC", "BP", "SC", "SP")
SIGN = {"BC": 1, "BP": -1, "SC": -1, "SP": 1}
YEAR = 365.0 * 86400


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def iso(x):
    return x.replace(tzinfo=timezone.utc).isoformat()


def parse_iso(x):
    try:
        d = datetime.fromisoformat((x or "").replace("Z", "+00:00"))
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    except ValueError:
        return None


def response(data, status=200):
    return request.make_response(json.dumps(data), status=status, headers=[
        ("Content-Type", "application/json"), ("Cache-Control", "no-cache")])


def metrics(s, k, iv, t, put):
    if min(s, k, iv, t) <= 0:
        return (0, 0, 0, 0)
    v = iv / 100.0
    rt = math.sqrt(t)
    d1 = (math.log(s / k) + .5 * v * v * t) / (v * rt)
    d2 = d1 - v * rt
    cdf = lambda z: .5 * (1 + math.erf(z / math.sqrt(2)))
    pdf = math.exp(-.5 * d1 * d1) / math.sqrt(2 * math.pi)
    value = k * cdf(-d2) - s * cdf(-d1) if put else s * cdf(d1) - k * cdf(d2)
    delta = cdf(d1) - 1 if put else cdf(d1)
    return max(value, 0), delta, pdf / (s * v * rt), s * pdf * rt / 100


def interval_seconds(x):
    return {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[x]


def auto_interval(hours):
    return "15m" if hours <= 24 else "1h" if hours <= 96 else "4h" if hours <= 720 else "1d"


class ExpiryTraceController(http.Controller):
    @http.route("/api/expiry-trace/expiries/<string:asset>", type="http",
                auth="user", website=False, csrf=False)
    def expiries(self, asset):
        asset = asset.upper()
        if asset not in ("BTC", "ETH"):
            return response({"error": "Unknown asset"}, 400)
        request.env.cr.execute("""
          SELECT DISTINCT expiration FROM dankbit_trade
          WHERE name ILIKE %s AND expiration >= %s AND iv <> 0 ORDER BY expiration
        """, (asset + "-%", datetime.utcnow() - timedelta(days=365)))
        return response({"asset": asset, "expiries": [iso(x[0]) for x in request.env.cr.fetchall()]})

    @http.route("/api/expiry-pricing/<string:asset>", type="http",
                auth="user", website=False, csrf=False)
    def trace(self, asset, **p):
        asset, now = asset.upper(), datetime.now(timezone.utc).replace(tzinfo=None)
        if asset not in ("BTC", "ETH"):
            return response({"error": "Unknown asset"}, 400)
        mode = (p.get("mode") or "live").lower()
        if mode == "live":
            as_of = now
            trade_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
            request.env.cr.execute("""SELECT MIN(expiration) FROM dankbit_trade
              WHERE name ILIKE %s AND expiration>%s AND active=TRUE AND iv<>0""",
                                   (asset + "-%", now))
            expiry = request.env.cr.fetchone()[0]
        else:
            expiry, trade_from, as_of = map(parse_iso, (p.get("expiry"), p.get("trade_from"), p.get("as_of")))
            if not all((expiry, trade_from, as_of)):
                return response({"error": "Expiry, Trade From and As Of are required"}, 400)
            if as_of > now + timedelta(minutes=1) or not trade_from < as_of <= expiry:
                return response({"error": "Required order: Trade From < As Of <= Expiry; As Of cannot be future"}, 400)
            if as_of - trade_from > timedelta(days=365):
                return response({"error": "Trade window cannot exceed 365 days"}, 400)
        if not expiry:
            return response({"asset": asset, "available": False, "mode": mode,
                             "display_only": True, "generated_at": iso(now)})

        span = max(.25, (expiry - trade_from).total_seconds() / 3600)
        interval = "1h" if mode == "live" else (p.get("interval") or "auto")
        interval = auto_interval(span) if interval == "auto" else interval
        if interval not in ("15m", "1h", "4h", "1d"):
            return response({"error": "Unsupported interval"}, 400)
        try:
            auto_count = 24 if mode == "live" else int(clamp(math.ceil(span * 3600 / interval_seconds(interval)) + 1, 24, 180))
            count = auto_count if (p.get("candles") or "auto") == "auto" else int(p["candles"])
            count = int(clamp(count, 12, 180))
        except ValueError:
            return response({"error": "Invalid candle count"}, 400)

        request.env.cr.execute("""SELECT strike,option_type,direction,amount,iv,
          deribit_ts,index_price,is_block_trade,block_trade_id FROM dankbit_trade
          WHERE name ILIKE %s AND expiration=%s AND iv<>0
          AND deribit_ts>=%s AND deribit_ts<=%s ORDER BY deribit_ts""",
                               (asset + "-%", expiry, trade_from, as_of))
        raw = request.env.cr.fetchall()
        cs = request.env["dankbit.trade"].get_candles_coinbase(
            asset, interval=interval, limit=min(500, count + 4),
            as_of_ts=int(as_of.replace(tzinfo=timezone.utc).timestamp())) or []
        actual = []
        for c in sorted(cs, key=lambda z: int(z.get("t", 0))):
            ts = int(c.get("t", 0)); ts = ts // 1000 if ts > 100000000000 else ts
            dt = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
            if trade_from <= dt <= as_of:
                actual.append({"time": ts, "open": float(c["o"]), "high": float(c["h"]),
                               "low": float(c["l"]), "close": float(c["c"]), "kind": "actual"})
        actual = actual[-count:]
        spot = actual[-1]["close"] if actual else float(request.env["dankbit.trade"].get_index_price(asset) or 0)
        if not spot and raw:
            spot = float(raw[-1][6] or 0)
        recent = [(float(r[4]), float(r[3])) for r in raw if r[4] and r[3] and r[5] >= as_of-timedelta(hours=6)]
        pool = recent or [(float(r[4]), float(r[3])) for r in raw if r[4] and r[3]]
        asof_iv = sum(iv*a for iv,a in pool)/sum(a for _,a in pool) if pool else 0

        contracts = {b: 0.0 for b in BUCKETS}
        effective = {b: 0.0 for b in BUCKETS}
        adjusted = {b: 0.0 for b in BUCKETS}
        viability_weight = {b: 0.0 for b in BUCKETS}
        iv_weight = {b: 0.0 for b in BUCKETS}
        vega = {b: 0.0 for b in BUCKETS}
        levels = {b: [0.0, 0.0] for b in BUCKETS}
        latest = None
        remain_t = max((expiry-as_of).total_seconds(), 60)/YEAR
        for k, typ, side, amount, iv, entered, entry_spot, block, block_id in raw:
            amount, entry_spot = float(amount or 0), float(entry_spot or spot or 0)
            if amount <= 0 or entry_spot <= 0 or typ not in ("call","put") or side not in ("buy","sell"):
                continue
            b = ("B" if side=="buy" else "S") + ("C" if typ=="call" else "P")
            entry_t = max((expiry-entered).total_seconds(), 60)/YEAR
            ev, delta, gamma, entry_vega = metrics(entry_spot, float(k), float(iv), entry_t, typ=="put")
            tv, _, _, current_vega = metrics(entry_spot, float(k), float(iv), remain_t, typ=="put")
            cv, _, _, _ = metrics(entry_spot, float(k), asof_iv or float(iv), remain_t, typ=="put")
            viability = clamp(tv/max(ev, 1e-9), 0, 1.25)
            ratio = cv/max(tv, 1e-9)
            iv_factor = clamp(ratio if side=="buy" else 1/max(ratio, 1e-9), .65, 1.35)
            age = max(0, (as_of-entered).total_seconds()/3600)
            half = clamp((expiry-entered).total_seconds()/3600*.18, 3, 168)
            fresh = .25 + .75*2**(-age/half)
            quality = 1.12 if block or block_id else 1
            gamma_weight = amount*abs(gamma)*entry_spot*fresh*quality
            base = amount*(abs(delta)+abs(gamma)*entry_spot)*fresh*quality
            contracts[b] += amount; effective[b] += base; adjusted[b] += base*viability*iv_factor
            viability_weight[b] += base*viability; iv_weight[b] += base*iv_factor
            vega[b] += amount*current_vega
            levels[b][0] += float(k)*gamma_weight; levels[b][1] += gamma_weight
            latest = entered if latest is None else max(latest, entered)

        expected = max(spot*(asof_iv/100)*math.sqrt(remain_t) if spot and asof_iv else 0, spot*.003 if spot else 1)
        fallback = {"BC":spot+expected*.55, "BP":spot-expected*.55, "SC":spot+expected, "SP":spot-expected}
        targets = {b.lower()+"g": levels[b][0]/levels[b][1] if levels[b][1] else fallback[b] for b in BUCKETS}
        start = actual[0]["open"] if actual else (float(raw[0][6] or spot) if raw else spot)
        realised = (spot-start)/max(expected,1e-9)
        remaining, group = {}, {}
        for b in BUCKETS:
            completion = clamp(realised*SIGN[b])
            feasibility = math.exp(-.55*abs(targets[b.lower()+"g"]-spot)/max(expected,1e-9))
            remaining[b] = adjusted[b]*(1-completion)*feasibility
            viability = viability_weight[b]/effective[b] if effective[b] else 0
            iv_adj = iv_weight[b]/effective[b] if effective[b] else 1
            state = ("No Flow" if not effective[b] else "Mostly Priced" if completion>=.75
                     else "Theta Eroding" if viability<.4 else "Volatility Supported" if iv_adj>=1.12
                     else "Seller Carry Working" if b[0]=="S" and viability>=.65 else "Residual Pressure")
            group[b] = {"time_viability":round(viability,4), "theta_erosion":round(1-min(viability,1),4),
                        "iv_adjustment":round(iv_adj,4), "directional_completion":round(completion,4),
                        "feasibility":round(feasibility,4), "vega_exposure":round(vega[b],4), "state":state}
        fresh = latest is not None and as_of-latest <= timedelta(hours=3)
        if fresh:
            for b in BUCKETS:
                if group[b]["state"]=="Residual Pressure" and remaining[b]>0:
                    group[b]["state"]="Fresh Pricing Pressure"
        def score(flow):
            bull, bear = flow["BC"]+flow["SP"], flow["BP"]+flow["SC"]
            return (bull-bear)/(bull+bear) if bull+bear else 0
        raw_score, direction = score(effective), score(remaining)
        buyer, seller = remaining["BC"]+remaining["BP"], remaining["SC"]+remaining["SP"]
        viability = sum(viability_weight.values())/max(sum(effective.values()),1e-9)
        iv_adj = sum(iv_weight.values())/max(sum(effective.values()),1e-9)
        regime = ("Gamma / Theta Pin" if abs(direction)<.1 and viability<.55 else
                  "Seller Range" if seller>buyer*1.3 else "Volatility Expansion" if iv_adj>=1.12 and abs(direction)>=.18 else
                  ("Fresh " if fresh else "")+("Bullish Pricing" if direction>=.18 else "Bearish Pricing") if abs(direction)>=.18 else
                  "Rotation Candidate" if sum(remaining.values()) else "Low Confidence")
        confidence = round(100*(.35*abs(direction)+.3*clamp(len(raw)/80)+.2*clamp(viability)+.15*(1 if fresh else .35)))

        # One user-facing verdict. The four bucket states remain diagnostics only.
        current_sign = 1 if realised > .12 else -1 if realised < -.12 else 0
        pressure_sign = 1 if direction > .12 else -1 if direction < -.12 else 0
        nearest_key = min(targets, key=lambda key: abs(targets[key]-spot))
        nearest_distance = abs(targets[nearest_key]-spot)/max(expected, 1e-9)
        if current_sign and pressure_sign == -current_sign:
            verdict, arrow = "ROTATE", ("↑" if pressure_sign > 0 else "↓")
            reason = "Opposing residual pressure is stronger"
        elif abs(direction) < .12 or (nearest_distance <= .18 and (seller >= buyer or viability < .55)):
            verdict, arrow = "PIN", "↔"
            reason = "Balanced pressure and time decay favour a hold"
        else:
            verdict, arrow = "CONTINUE", ("↑" if (pressure_sign or current_sign) > 0 else "↓")
            reason = "Residual pressure supports the current path"
        if verdict == "CONTINUE":
            level_key = "bcg" if arrow == "↑" else "bpg"
            preposition = "toward"
        else:
            level_key = nearest_key
            preposition = "near" if verdict == "PIN" else "from"
        unified_comment = {"state":verdict, "direction":arrow, "level":level_key.upper(),
                           "price":round(targets[level_key], 2), "preposition":preposition,
                           "confidence":confidence, "reason":reason}

        future, prev = [], (actual[-1]["close"] if actual else spot)
        slots = min(max(0,count-len(actual)), max(0,int(math.ceil((expiry-as_of).total_seconds()/interval_seconds(interval)))))
        for i in range(slots):
            decay=math.exp(-i/max(5,slots*.45)); target=targets["bcg"] if direction>=0 else targets["bpg"]
            step=(target-prev)*(.08+.08*abs(direction))*decay+direction*expected*.07*decay
            if prev>targets["scg"]: step-=(prev-targets["scg"])*.2
            if prev<targets["spg"]: step+=(targets["spg"]-prev)*.2
            close=prev+clamp(step,-expected*.18,expected*.18); wick=expected*(.025+.035*(1-abs(direction)))
            future.append({"time":int((as_of+timedelta(seconds=interval_seconds(interval)*(i+1))).replace(tzinfo=timezone.utc).timestamp()),
                           "open":prev,"high":max(prev,close)+wick,"low":min(prev,close)-wick,
                           "close":close,"kind":"forecast","confidence":round(confidence*decay)})
            prev=close
        return response({"asset":asset,"available":True,"display_only":True,"engine":"Expiry Trace Engine",
          "path_name":"Traced Price Path","mode":mode,"refresh_seconds":900,"generated_at":iso(now),
          "trade_from":iso(trade_from),"as_of":iso(as_of),"expiry":iso(expiry),
          "hours_to_expiry":round(max(0,(expiry-as_of).total_seconds()/3600),2),"interval":interval,
          "candle_count":count,"spot":spot,"atm_iv":round(asof_iv,3),"expected_move":expected,
          "contracts":contracts,"effective_flow":effective,"adjusted_flow":adjusted,
          "remaining_pressure":remaining,"group_metrics":group,"raw_direction_score":round(raw_score,4),
          "direction_score":round(direction,4),"confidence":confidence,"regime":regime,"targets":targets,
          "unified_comment":unified_comment,
          "actual":actual,"forecast":future,"trade_count":len(raw),"price_source":"Coinbase Spot"})
