# -*- coding: utf-8 -*-
"""Read-only data tools behind the /4l chat panel (see chat.py).

Every tool takes an Odoo `env` plus plain arguments and returns a small,
pre-digested JSON-serializable dict: all arithmetic, sorting, sign/side
classification and comparisons happen here, so the local LLM only ever
has to *phrase* results, never compute them (small local models are
unreliable at arithmetic — see the design notes in chat.py).

Same `iv <> 0` rule as every other Greek/trade fetch site in this addon
(see CLAUDE.md), and every window is capped at `as_of` so a historical
(Time Machine) caller can't pull in trades from after that moment.
"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytz

from . import delta
from . import gamma
from . import options

ASSETS = ("BTC", "ETH")
MAX_HOURS = 720
TIMEZONES = {"UTC": "UTC", "Berlin": "Europe/Berlin", "Tehran": "Asia/Tehran"}
BERLIN = pytz.timezone("Europe/Berlin")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hours(value, default=24):
    """Trailing-window hours, capped at MAX_HOURS. "all" or <= 0 -> None
    (no lower bound) — the model sends hours=0 for "no window", which used
    to be clamped up to 1h and silently count only the last hour."""
    if isinstance(value, str) and value.strip().lower() == "all":
        return None
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return default
    return min(MAX_HOURS, hours) if hours > 0 else None


def _window(hours, as_of):
    return (as_of - timedelta(hours=hours)) if hours else None


def active_instruments(env, asset, as_of=None):
    """Every non-expired instrument for `asset`, soonest-first — the same
    list /4l's Expiry dropdown shows. With a historical `as_of`, the ones
    active (and already trading) at that moment instead — same list /tm's
    Expiry dropdown shows."""
    Bands = env["dankbit.bands"].sudo()
    if as_of:
        exps = Bands._distinct_expirations_asof(asset, as_of, 200)
    else:
        exps = Bands._distinct_expirations(asset, _now(), 200)
    return [Bands._format_instrument(asset, e) for e in exps]


def parse_at(at_time, tz_name):
    """"YYYY-MM-DD HH:MM" in `tz_name` (UTC/Berlin/Tehran, default UTC) ->
    naive UTC datetime, or None if missing/unparseable. The conversion is
    done here, not by the model — qwen3:14b converted 14:00 Berlin (CEST)
    to 18:00 UTC instead of 12:00 (2026-09-23)."""
    text = str(at_time or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H", "%Y-%m-%d"):
        try:
            local = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    name = str(tz_name or "").strip().lower()
    tz = pytz.timezone(next((v for k, v in TIMEZONES.items() if k.lower() == name), "UTC"))
    return tz.localize(local).astimezone(pytz.utc).replace(tzinfo=None)


def as_of_stamp(as_of):
    """Which moment a result describes, in UTC and Berlin wall-clock."""
    live = as_of is None
    as_of = as_of or _now()
    return {"data_as_of_utc": as_of.strftime("%Y-%m-%d %H:%M"),
            "data_as_of_berlin": pytz.utc.localize(as_of).astimezone(BERLIN).strftime("%Y-%m-%d %H:%M"),
            "data_is": "live (now)" if live else "historical"}


def _price_grid(env, asset):
    icp = env["ir.config_parameter"].sudo()
    if asset == "BTC":
        lo = float(icp.get_param("dankbit.from_price", default=100000))
        hi = float(icp.get_param("dankbit.to_price", default=150000))
        step = float(icp.get_param("dankbit.steps", default=100))
    else:
        lo = float(icp.get_param("dankbit.eth_from_price", default=2000))
        hi = float(icp.get_param("dankbit.eth_to_price", default=5000))
        step = float(icp.get_param("dankbit.eth_steps", default=50))
    return np.arange(lo, hi, step)


def _sql_filter(asset, instrument, hours, as_of):
    """WHERE clause + params shared by the raw-SQL tools."""
    pattern = f"{instrument}-%" if instrument else f"{asset}-%"
    where = ["name ILIKE %s", "iv <> 0", "deribit_ts <= %s"]
    params = [pattern, as_of]
    start = _window(hours, as_of)
    if start:
        where.append("deribit_ts > %s")
        params.append(start)
    return " AND ".join(where), params


def _index_price(env, instrument_or_asset):
    try:
        return round(float(env["dankbit.trade"].sudo().get_index_price(instrument_or_asset) or 0.0))
    except Exception:
        return None


# ---------------------------------------------------------------- tools


def trade_summary(env, asset, hours=24, instrument=None, as_of=None):
    as_of = as_of or _now()
    hours = _hours(hours)
    where, params = _sql_filter(asset, instrument, hours, as_of)
    env.cr.execute(f"""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE direction = 'buy'),
               COUNT(*) FILTER (WHERE direction = 'sell'),
               COALESCE(SUM(amount) FILTER (WHERE option_type = 'call' AND direction = 'buy'), 0),
               COALESCE(SUM(amount) FILTER (WHERE option_type = 'call' AND direction = 'sell'), 0),
               COALESCE(SUM(amount) FILTER (WHERE option_type = 'put' AND direction = 'buy'), 0),
               COALESCE(SUM(amount) FILTER (WHERE option_type = 'put' AND direction = 'sell'), 0),
               COALESCE(SUM(price * index_price * amount), 0),
               COUNT(*) FILTER (WHERE is_block_trade)
          FROM dankbit_trade WHERE {where}
    """, params)
    n, buys, sells, cb, cs, pb, ps, prem, blocks = env.cr.fetchone()
    cb, cs, pb, ps = (round(float(x), 1) for x in (cb, cs, pb, ps))
    calls, puts = round(cb + cs, 1), round(pb + ps, 1)
    by_expiry = None
    if not instrument:
        # Ranked per-expiry split — asked "which expiry had the most
        # trades?", the model invented every per-expiry count from the
        # totals alone (2026-09-24).
        env.cr.execute(f"""
            SELECT SUBSTRING(name FROM '^[^-]+-[^-]+') AS inst, COUNT(*), COALESCE(SUM(amount), 0)
              FROM dankbit_trade WHERE {where}
             GROUP BY inst ORDER BY COUNT(*) DESC LIMIT 8
        """, params)
        by_expiry = [{"rank": i + 1, "expiry": inst, "trades": cnt, "contracts": round(float(amt), 1)}
                     for i, (inst, cnt, amt) in enumerate(env.cr.fetchall())]
    return {
        "asset": asset, "instrument": instrument or "all expiries", "window_hours": hours or "all",
        "trade_count": n, "buy_trades": buys, "sell_trades": sells,
        "more_trades_were": "buys" if buys > sells else "sells" if sells > buys else "equal",
        "call_contracts": {"bought": cb, "sold": cs, "net": round(cb - cs, 1), "total": calls},
        "put_contracts": {"bought": pb, "sold": ps, "net": round(pb - ps, 1), "total": puts},
        "more_contracts_in": "puts" if puts > calls else "calls" if calls > puts else "equal",
        # Pre-stated so the model never compares the numbers itself — it
        # wrote "more calls were bought (4,882) than puts (5,333)" (2026-09-24).
        "more_contracts_bought_in": _more(cb, pb),
        "more_contracts_sold_in": _more(cs, ps),
        "calls_overall": _net_label(cb - cs, calls), "puts_overall": _net_label(pb - ps, puts),
        "positioning_lean": _lean(_net_label(cb - cs, calls), _net_label(pb - ps, puts)),
        "premium_usd": round(float(prem)), "block_trades": blocks,
        **({"expiries_ranked_by_trades": by_expiry,
            "expiries_note": "top 8 expiries by trade count; others are not listed"} if by_expiry else {}),
    }


def _more(call_value, put_value):
    return "calls" if call_value > put_value else "puts" if put_value > call_value else "equal"


def largest_trades(env, asset, hours=24, instrument=None, n=5, block_only=False, as_of=None):
    as_of = as_of or _now()
    hours = _hours(hours)
    n = max(1, min(10, int(n or 5)))
    where, params = _sql_filter(asset, instrument, hours, as_of)
    if block_only:
        where += " AND is_block_trade"
    env.cr.execute(f"SELECT COUNT(*) FROM dankbit_trade WHERE {where}", params)
    total = env.cr.fetchone()[0]
    env.cr.execute(f"""
        SELECT name, direction, option_type, strike, amount, price * index_price * amount AS usd,
               iv, is_block_trade, deribit_ts
          FROM dankbit_trade WHERE {where}
         ORDER BY usd DESC NULLS LAST LIMIT %s
    """, params + [n])
    rows = []
    for rank, (name, side, otype, strike, amount, usd, iv, block, ts) in enumerate(env.cr.fetchall(), 1):
        parts = name.split("-")
        rows.append({
            "rank": rank, "instrument": name, "expiry": parts[1] if len(parts) > 1 else None,
            "strike": strike, "type": otype, "side": side, "contracts": round(float(amount), 1),
            "premium_usd": round(float(usd or 0)), "iv_pct": round(float(iv), 1),
            "block_trade": bool(block), "time_utc": ts.strftime("%Y-%m-%d %H:%M"),
        })
    return {"asset": asset, "instrument": instrument or "all expiries", "window_hours": hours or "all",
            "block_only": bool(block_only), "matching_trades_total": total,
            "showing": f"top {len(rows)} of {total} by premium — not a complete list" if total > len(rows)
                       else f"all {total}",
            "trades": rows}


def block_trades(env, asset, hours=24, instrument=None, n=10, as_of=None):
    """Block trades, newest first, with the total count — answers "when was
    the last block trade?" / "list block trades" (largest_trades sorts by
    premium, not time). Listed per block trade with its legs nested, since
    a flat leg list made the model mix up "15 distinct block trades" with
    "15 legs shown" (2026-09-24)."""
    as_of = as_of or _now()
    hours = _hours(hours)
    n = max(1, min(10, int(n or 10)))
    where, params = _sql_filter(asset, instrument, hours, as_of)
    where += " AND is_block_trade"
    bid = "COALESCE(block_trade_id, deribit_trade_identifier)"
    env.cr.execute(f"""
        SELECT COUNT(*), COUNT(DISTINCT {bid}), MAX(deribit_ts),
               COALESCE(SUM(amount) FILTER (WHERE option_type = 'call'), 0),
               COALESCE(SUM(amount) FILTER (WHERE option_type = 'put'), 0)
          FROM dankbit_trade WHERE {where}
    """, params)
    legs_total, total, last_ts, call_c, put_c = env.cr.fetchone()
    env.cr.execute(f"""
        WITH newest AS (
            SELECT {bid} AS bid, MAX(deribit_ts) AS ts FROM dankbit_trade WHERE {where}
             GROUP BY 1 ORDER BY 2 DESC LIMIT %s)
        SELECT newest.bid, newest.ts, t.name, t.direction, t.option_type, t.strike, t.amount,
               t.price * t.index_price * t.amount
          FROM newest
          JOIN dankbit_trade t ON COALESCE(t.block_trade_id, t.deribit_trade_identifier) = newest.bid
         WHERE {where}
         ORDER BY newest.ts DESC, newest.bid, t.name
    """, params + [n] + params)
    blocks = {}
    for b_id, ts, name, side, otype, strike, amount, usd in env.cr.fetchall():
        blk = blocks.setdefault(b_id, {"time_utc": ts.strftime("%Y-%m-%d %H:%M"), "premium_usd": 0, "legs": []})
        blk["premium_usd"] += round(float(usd or 0))
        blk["legs"].append({"instrument": name, "strike": strike, "type": otype, "side": side,
                            "contracts": round(float(amount), 1)})
    listed = list(blocks.values())
    for blk in listed:
        blk["leg_count"] = len(blk["legs"])
    return {
        "asset": asset, "instrument": instrument or "all expiries", "window_hours": hours or "all",
        "block_trades_total": total, "legs_total": legs_total,
        "latest_block_trade_utc": last_ts.strftime("%Y-%m-%d %H:%M") if last_ts else None,
        "call_contracts": round(float(call_c), 1), "put_contracts": round(float(put_c), 1),
        "showing": (f"newest {len(listed)} of {total} block trades — not a complete list" if total > len(listed)
                    else f"all {total} block trades"),
        "block_trades_newest_first": listed,
    }


def strike_activity(env, asset, instrument, hours=24, as_of=None):
    live = as_of is None
    as_of = as_of or _now()
    hours = _hours(hours)
    where, params = _sql_filter(asset, instrument, hours, as_of)
    env.cr.execute(f"""
        SELECT strike, option_type,
               COALESCE(SUM(amount) FILTER (WHERE direction = 'buy'), 0),
               COALESCE(SUM(amount) FILTER (WHERE direction = 'sell'), 0)
          FROM dankbit_trade WHERE {where}
         GROUP BY strike, option_type
    """, params)
    rows = []
    for strike, otype, bought, sold in env.cr.fetchall():
        bought, sold = round(float(bought), 1), round(float(sold), 1)
        rows.append({"strike": strike, "type": otype, "bought": bought, "sold": sold,
                     "net": round(bought - sold, 1), "total": round(bought + sold, 1)})

    def top(rs, key, reverse=True, k=5):
        return sorted(rs, key=key, reverse=reverse)[:k]

    calls = [r for r in rows if r["type"] == "call"]
    puts = [r for r in rows if r["type"] == "put"]
    return {
        "asset": asset, "instrument": instrument, "window_hours": hours or "all",
        "index_price": _index_price(env, instrument) if live else None,
        "strikes_traded": len(rows),
        "note": "each list shows at most the top 5, ranked. *_most_sold / *_most_bought = GROSS contracts sold / "
                "bought at that strike; *_net_sold / *_net_bought = sold minus bought (net). For 'sold the most' "
                "use the gross list unless the user asks for net. puts_net_sold strikes were SOLD on net, not bought",
        # Gross rankings — asked "which call strikes were sold the most?",
        # the model quoted gross sold amounts in the net list's order
        # (2026-09-24).
        "calls_most_sold": _ranked(calls, "sold"), "calls_most_bought": _ranked(calls, "bought"),
        "puts_most_sold": _ranked(puts, "sold"), "puts_most_bought": _ranked(puts, "bought"),
        # Per strike, calls + puts combined and ranked — with separate
        # call/put rows the model summed them itself and got the order
        # wrong ("70000: 538 first, 80000: 734.6 second", 2026-09-24).
        "most_traded_strikes_by_volume": _ranked_strikes(rows),
        # Net lists are pre-split so the model never has to decide itself
        # whether "bought 166.5 / sold 189.9" counts as buying.
        "puts_net_bought": top([r for r in puts if r["net"] > 0], lambda r: r["net"]),
        "puts_net_sold": top([r for r in puts if r["net"] < 0], lambda r: r["net"], reverse=False),
        "calls_net_bought": top([r for r in calls if r["net"] > 0], lambda r: r["net"]),
        "calls_net_sold": top([r for r in calls if r["net"] < 0], lambda r: r["net"], reverse=False),
    }


def _ranked(rows, field, k=5):
    top = sorted((r for r in rows if r[field] > 0), key=lambda r: r[field], reverse=True)[:k]
    return [{"rank": i + 1, "strike": r["strike"], field: r[field]} for i, r in enumerate(top)]


def _ranked_strikes(rows, k=5):
    by_strike = {}
    for r in rows:
        agg = by_strike.setdefault(r["strike"], {"strike": r["strike"], "calls": 0.0, "puts": 0.0})
        agg["calls" if r["type"] == "call" else "puts"] += r["total"]
    ranked = sorted(by_strike.values(), key=lambda a: a["calls"] + a["puts"], reverse=True)[:k]
    return [{"rank": i + 1, "strike": a["strike"], "contracts_traded": round(a["calls"] + a["puts"], 1),
             "calls": round(a["calls"], 1), "puts": round(a["puts"], 1)} for i, a in enumerate(ranked)]


def _gamma_snapshot(env, asset, instrument, hours, as_of):
    """Same math as main.py's four_leg_gamma_json (per_leg_gamma + combined
    delta zeros + combined taker gamma flips), for one instrument/window."""
    domain = [("name", "=ilike", f"{instrument}-%"), ("iv", "!=", 0), ("deribit_ts", "<=", as_of)]
    start = _window(hours, as_of)
    if start:
        domain.append(("deribit_ts", ">=", start))
    trades = env["dankbit.trade"].sudo().with_context(active_test=False).search(domain)
    if not trades:
        return None
    STs = _price_grid(env, asset)
    legs = options.per_leg_gamma(STs, trades)
    names = {"LC": "long_call", "LP": "long_put", "SC": "short_call", "SP": "short_put"}
    out_legs = {}
    for code, key in names.items():
        price = legs[key]["gamma_price"]
        out_legs[code] = {
            "gamma_peak_price": round(price) if price else None,
            "gamma_millions": round(abs(legs[key]["gamma_value"]) / 1e6, 1) if price else 0.0,
        }
    present = {k: v for k, v in out_legs.items() if v["gamma_peak_price"]}
    dominant = max(present, key=lambda k: present[k]["gamma_millions"]) if present else None

    d_arr = np.asarray(delta.portfolio_delta(STs, trades, 0.05), dtype=float)
    delta_zero = [round(p) for p in options.find_zero_crossings(STs, d_arr)]
    g_arr = np.asarray(gamma.portfolio_gamma(STs, trades, 0.05), dtype=float)
    flips = []
    for i in range(len(g_arr) - 1):
        ga, gb = g_arr[i], g_arr[i + 1]
        if not (np.isfinite(ga) and np.isfinite(gb)) or ga * gb >= 0:
            continue
        flips.append({
            "price": round(float(STs[i] - ga * (STs[i + 1] - STs[i]) / (gb - ga))),
            "gamma_below": "positive" if ga > 0 else "negative",
            "gamma_above": "positive" if gb > 0 else "negative",
        })
    return {"trade_count": len(trades), "legs": out_legs, "dominant_leg": dominant,
            "delta_zero_prices": delta_zero, "gamma_flips": flips}


def _relative(price, index_price):
    if not price or not index_price:
        return None
    diff = round(price - index_price)
    side = "above" if diff > 0 else "below" if diff < 0 else "at"
    return {"points": abs(diff), "side": side, "text": f"{abs(diff)} {side} index price" if diff else "at index price"}


def gamma_legs(env, asset, instrument, hours=24, as_of=None):
    live = as_of is None
    as_of = as_of or _now()
    hours = _hours(hours)
    snap = _gamma_snapshot(env, asset, instrument, hours, as_of)
    base = {"asset": asset, "instrument": instrument, "window_hours": hours or "all",
            "as_of_utc": as_of.strftime("%Y-%m-%d %H:%M"),
            "legend": "LC/LP/SC/SP = price where the gamma of long call / long put / short call / short put "
                      "trades peaks; dominant_leg = leg with the largest gamma; gamma is taker-signed "
                      "(option buyers' side).",
            # Stated in the result, not only the glossary — it kept calling
            # the dominant leg "the most active" (2026-09-24).
            "not_in_this_data": "trade counts or activity per leg — the dominant leg has the largest gamma, which "
                                "does NOT mean it was traded the most; do not claim which leg was most active. It also says where "
                                "gamma is concentrated, not whether positioning is bullish or bearish (that comes from net "
                                "buying/selling in the trade summary)"}
    if not snap:
        return {**base, "trade_count": 0, "note": "No trades in this expiry/window."}
    index_price = _index_price(env, instrument) if live else None
    if index_price:
        for leg in snap["legs"].values():
            leg["vs_index"] = (_relative(leg["gamma_peak_price"], index_price) or {}).get("text")
        snap["delta_zero"] = [{"price": p, "vs_index": _relative(p, index_price)["text"]} for p in snap.pop("delta_zero_prices")]
        for f in snap["gamma_flips"]:
            f["vs_index"] = _relative(f["price"], index_price)["text"]
    dom = snap["dominant_leg"]
    if dom:
        snap["dominant"] = {"leg": dom, **snap["legs"][dom]}
    return {**base, "index_price": index_price, **snap}


def compare_windows(env, asset, instrument, hours=24, hours_ago=4):
    """Gamma structure now vs. `hours_ago` hours ago, same trailing-window
    length for both, with each leg's shift pre-computed."""
    now = _now()
    hours = _hours(hours)
    hours_ago = max(1, min(MAX_HOURS, int(hours_ago or 4)))
    then = now - timedelta(hours=hours_ago)
    a = _gamma_snapshot(env, asset, instrument, hours, then)
    b = _gamma_snapshot(env, asset, instrument, hours, now)
    result = {"asset": asset, "instrument": instrument, "window_hours": hours or "all",
              "earlier_utc": then.strftime("%Y-%m-%d %H:%M"), "now_utc": now.strftime("%Y-%m-%d %H:%M")}
    if not a or not b:
        return {**result, "note": "Not enough trades at one of the two moments to compare."}
    changes = {}
    for code in ("LC", "LP", "SC", "SP"):
        pa, pb = a["legs"][code]["gamma_peak_price"], b["legs"][code]["gamma_peak_price"]
        ga, gb = a["legs"][code]["gamma_millions"], b["legs"][code]["gamma_millions"]
        entry = {"earlier_price": pa, "now_price": pb, "earlier_gamma_millions": ga, "now_gamma_millions": gb}
        if pa and pb:
            move = pb - pa
            entry["price_move"] = f"moved {'up' if move > 0 else 'down'} {abs(move)}" if move else "unchanged"
        entry["gamma_change"] = "stronger" if gb > ga else "weaker" if gb < ga else "unchanged"
        changes[code] = entry
    return {
        **result, "leg_changes": changes,
        "dominant_leg": {"earlier": a["dominant_leg"], "now": b["dominant_leg"],
                         "changed": a["dominant_leg"] != b["dominant_leg"]},
        "delta_zero_prices": {"earlier": a["delta_zero_prices"], "now": b["delta_zero_prices"]},
        "gamma_flip_prices": {"earlier": [f["price"] for f in a["gamma_flips"]],
                              "now": [f["price"] for f in b["gamma_flips"]]},
        "trade_count": {"earlier": a["trade_count"], "now": b["trade_count"]},
    }


class _AggTrade:
    """Stand-in for a dankbit.trade row — just the attributes
    gamma.portfolio_gamma() reads."""
    __slots__ = ("direction", "option_type", "strike", "iv", "amount", "expiration")

    def __init__(self, direction, option_type, strike, iv, amount, expiration):
        self.direction, self.option_type, self.strike = direction, option_type, strike
        self.iv, self.amount, self.expiration = iv, amount, expiration

    def get_hours_to_expiry(self):
        return max(0.0, (self.expiration - _now()).total_seconds() / 3600.0) if self.expiration else 0.0


def _dominant_leg_fast(env, asset, instrument, hours, as_of):
    """Dominant gamma leg over possibly hundreds of thousands of trades.
    Every trade of one expiry shares the same time to expiry, so trades are
    pre-aggregated in SQL by (strike, type, side, IV rounded to 1 vol point,
    amount-weighted mean IV kept) — BTC-25SEP26's 196k trades become ~6k
    rows, and the all-history snapshot drops from ~85 s to a few seconds.
    Same gamma.portfolio_gamma() and per-leg argmax/argmin rule as
    options.per_leg_gamma(); only the IV grouping differs (<= 0.5 pt)."""
    where, params = _sql_filter(asset, instrument, hours, as_of)
    env.cr.execute(f"""
        SELECT direction, option_type, strike, SUM(amount * iv) / NULLIF(SUM(amount), 0), SUM(amount),
               MIN(expiration)
          FROM dankbit_trade WHERE {where} AND amount > 0
         GROUP BY direction, option_type, strike, ROUND(iv::numeric, 0)
    """, params)
    rows = [_AggTrade(*r) for r in env.cr.fetchall() if r[3]]
    if not rows:
        return None
    STs = _price_grid(env, asset)
    legs = {"LC": ("buy", "call", "long_call"), "LP": ("buy", "put", "long_put"),
            "SC": ("sell", "call", "short_call"), "SP": ("sell", "put", "short_put")}
    strength = {}
    for code, (side, otype, key) in legs.items():
        leg = [t for t in rows if t.direction == side and t.option_type == otype]
        if leg:
            curve = gamma.portfolio_gamma(STs, leg, r=0.0)
            strength[code] = abs(float(curve[int(options._GAMMA_VEGA_ARGFN[key](curve))]))
    return max(strength, key=strength.get) if strength else None


def _net_flow(env, asset, instrument, hours, as_of):
    where, params = _sql_filter(asset, instrument, hours, as_of)
    env.cr.execute(f"""
        SELECT COUNT(*),
               COALESCE(SUM(amount) FILTER (WHERE option_type = 'call' AND direction = 'buy'), 0),
               COALESCE(SUM(amount) FILTER (WHERE option_type = 'call' AND direction = 'sell'), 0),
               COALESCE(SUM(amount) FILTER (WHERE option_type = 'put' AND direction = 'buy'), 0),
               COALESCE(SUM(amount) FILTER (WHERE option_type = 'put' AND direction = 'sell'), 0)
          FROM dankbit_trade WHERE {where}
    """, params)
    n, cb, cs, pb, ps = env.cr.fetchone()
    cb, cs, pb, ps = (float(x) for x in (cb, cs, pb, ps))
    return {"trades": n, "contracts": cb + cs + pb + ps, "call_net": cb - cs, "put_net": pb - ps,
            "call_total": cb + cs, "put_total": pb + ps}


def _lean(call_label, put_label):
    """Taker positioning lean from the call/put net labels. Pre-stated:
    asked "do traders expect a fall?", the model answered that net put
    buying means traders expect a RISE (2026-09-24)."""
    votes = {"net bought": 1, "net sold": -1}
    score = votes.get(call_label, 0) - votes.get(put_label, 0)  # calls bought / puts sold = bullish
    if score > 0:
        return "bullish-leaning (calls net bought and/or puts net sold)"
    if score < 0:
        return "bearish-leaning (puts net bought and/or calls net sold) — net put buying can also be hedging"
    return "mixed or balanced (no clear lean)"


def _net_label(net, total):
    # Under 5% of that side's own volume counts as balanced, not a direction.
    if not total or abs(net) < 0.05 * total:
        return "balanced"
    return "net bought" if net > 0 else "net sold"


def recent_vs_total(env, asset, instrument, hours=4, as_of=None):
    """Is recent flow changing the existing positioning ("the book")? The
    last `hours` of one expiry's trades vs. its whole history up to as_of,
    with every comparison pre-stated — the model was unreliable comparing
    two raw summaries itself."""
    as_of = as_of or _now()
    hours = _hours(hours, 4) or 4
    recent = _net_flow(env, asset, instrument, hours, as_of)
    total = _net_flow(env, asset, instrument, None, as_of)
    base = {"asset": asset, "instrument": instrument, "recent_window_hours": hours,
            "compared_with": "whole history of this expiry"}
    if not recent["trades"] or not total["trades"]:
        return {**base, "note": "No trades in the recent window (or in this expiry at all) to compare."}

    sides, against = {}, []
    for side in ("call", "put"):
        r_net, t_net = recent[f"{side}_net"], total[f"{side}_net"]
        r_lab = _net_label(r_net, recent[f"{side}_total"])
        t_lab = _net_label(t_net, total[f"{side}_total"])
        if "balanced" in (r_lab, t_lab):
            relation = "no clear direction to compare"
        elif r_lab == t_lab:
            relation = "SAME direction as the whole history (adding to the existing positioning)"
        else:
            relation = "OPPOSITE to the whole history (reducing or reversing the existing positioning)"
            against.append(f"{side}s")
        sides[f"{side}s"] = {
            "recent": {"net_contracts": round(r_net, 1), "direction": r_lab},
            "whole_history": {"net_contracts": round(t_net, 1), "direction": t_lab},
            "recent_vs_whole": relation,
        }

    dom_r = _dominant_leg_fast(env, asset, instrument, hours, as_of)
    dom_t = _dominant_leg_fast(env, asset, instrument, None, as_of)
    share = round(100 * recent["contracts"] / total["contracts"], 1) if total["contracts"] else 0.0

    if against:
        verdict = (f"Recent flow is going AGAINST the existing positioning in {' and '.join(against)}; "
                   f"the recent window is {share}% of all contracts traded in this expiry.")
    else:
        verdict = (f"Recent flow is NOT going against the existing positioning; "
                   f"the recent window is {share}% of all contracts traded in this expiry.")
    if dom_r and dom_t:
        verdict += (f" Dominant gamma leg: {dom_r} recently vs {dom_t} over the whole history."
                    if dom_r != dom_t else f" Dominant gamma leg is {dom_r} in both.")
    return {
        **base,
        "verdict": verdict,
        "recent_positioning_lean": _lean(sides["calls"]["recent"]["direction"], sides["puts"]["recent"]["direction"]),
        "whole_history_positioning_lean": _lean(sides["calls"]["whole_history"]["direction"],
                                                sides["puts"]["whole_history"]["direction"]),
        "recent_share_of_all_contracts_pct": share,
        "trade_count": {"recent": recent["trades"], "whole_history": total["trades"]},
        **sides,
        "dominant_gamma_leg": {"recent": dom_r, "whole_history": dom_t,
                               "same": dom_r == dom_t if dom_r and dom_t else None},
        "legend": "LC/LP/SC/SP = long call/long put/short call/short put; net bought = more contracts bought "
                  "than sold (taker side).",
    }


def max_pain(env, asset, instrument):
    """Max Pain for one expiry — same computation as the /4l "Max Pain"
    line (four_leg_gamma_json): options.split_open_interest() +
    options.max_pain_price() over LIVE open interest, so it doesn't depend
    on any trade window."""
    oi_map = env["dankbit.trade"].sudo().get_open_interest_by_currency(asset)
    call_oi, put_oi = options.split_open_interest(oi_map, instrument)
    price, _ = options.max_pain_price(call_oi, put_oi)
    base = {"asset": asset, "instrument": instrument,
            "basis": "current open interest = contracts still open (NOT traded volume — for traded volume per "
                     "strike use the strike activity data), same as the Max Pain line on the chart; "
                     "open-interest history is not stored, so this is always the live value"}
    if price is None:
        return {**base, "note": "No open interest found for this expiry."}
    index_price = _index_price(env, instrument)
    call_total, put_total = round(sum(call_oi.values()), 1), round(sum(put_oi.values()), 1)
    # Per-strike OI, pre-sorted — without it the model presented the TOTAL
    # put OI as the top strike's own OI.
    per_strike = [{"strike": k, "calls": round(call_oi.get(k, 0.0), 1), "puts": round(put_oi.get(k, 0.0), 1),
                   "total": round(call_oi.get(k, 0.0) + put_oi.get(k, 0.0), 1)}
                  for k in set(call_oi) | set(put_oi)]
    return {
        **base, "max_pain_price": round(price), "index_price": index_price,
        "max_pain_vs_index": (_relative(price, index_price) or {}).get("text"),
        "open_interest_contracts": {"calls": call_total, "puts": put_total,
                                    "total": round(call_total + put_total, 1)},
        "more_open_interest_in": "puts" if put_total > call_total else "calls" if call_total > put_total else "equal",
        "top_strikes_by_total_open_interest": sorted(per_strike, key=lambda r: r["total"], reverse=True)[:5],
        "top_call_open_interest_strikes": [{"strike": r["strike"], "calls": r["calls"]} for r in
                                           sorted(per_strike, key=lambda r: r["calls"], reverse=True)[:3] if r["calls"]],
        "top_put_open_interest_strikes": [{"strike": r["strike"], "puts": r["puts"]} for r in
                                          sorted(per_strike, key=lambda r: r["puts"], reverse=True)[:3] if r["puts"]],
    }


def _range(lo, hi):
    """(lo, hi) -> {"low", "high", "text"} with 0/None = absent."""
    lo, hi = float(lo or 0.0), float(hi or 0.0)
    if lo <= 0 and hi <= 0:
        return None
    lo, hi = sorted(v for v in (lo or hi, hi or lo))
    return {"low": round(lo), "high": round(hi),
            "text": f"{round(lo)}" if round(lo) == round(hi) else f"{round(lo)} to {round(hi)}"}


def _where_is_price(index_price, rng):
    if not index_price or not rng:
        return None
    if index_price > rng["high"]:
        return f"index price is {round(index_price - rng['high'])} above it"
    if index_price < rng["low"]:
        return f"index price is {round(rng['low'] - index_price)} below it"
    return "index price is inside it"


def zones_and_bands(env, asset):
    """What the Delta Chart draws for `asset`: the nearest expiry's
    High/Low Zone boxes (yellow) and Middle Zone (teal, SMP/BML) — the
    last session-confirmed values, same as /api/zones-box/<asset>'s
    default 00:00 UTC view (dankbit.bands.get_box) — plus the Bands lines
    (High/Resistance, Low/Support, Gamma Band) and Smart Liquidity levels
    of the 3 tracked expiries, from the persisted dankbit.bands rows the
    chart's Bands/Smart Liquidity layers read. Live only (the confirmed
    rows are overwritten in place, no history)."""
    Bands = env["dankbit.bands"].sudo()
    index_price = _index_price(env, asset)
    result = {"asset": asset, "index_price": index_price,
              "basis": "the Delta Chart's Zones boxes (nearest expiry, last session-confirmed values; they only "
                       "change at the London / NY-London confirmation sessions) and Bands lines (one point per "
                       "tracked expiry). Current values only — no history.",
              "legend": "High Zone = the highest points where the Longs and Shorts payoff curves cross zero, Low "
                        "Zone = the lowest ones (each zone uses both curves); Middle Zone = between "
                        "Seller Max Profit (SMP) and Buyer Max Loss (BML); High/Resistance and Low/Support = "
                        "where the Longs and Shorts payoff curves cross; Gamma Band = average gamma peak price "
                        "of the 4 legs; Smart Liquidity = option-derived liquidity level above/below price."}
    try:
        box = Bands.get_box(asset)
    except Exception:
        box = None
    if box:
        high = _range(box.get("high_zone_min"), box.get("high_zone_max"))
        low = _range(box.get("low_zone_min"), box.get("low_zone_max"))
        middle = _range(box.get("middle_zone_min"), box.get("middle_zone_max"))
        confirmed = box.get("computed_at")
        result["zones"] = {
            "instrument": box.get("instrument"),
            "high_zone": high and {**high, "vs_index": _where_is_price(index_price, high)},
            "low_zone": low and {**low, "vs_index": _where_is_price(index_price, low)},
            "middle_zone": middle and {**middle, "vs_index": _where_is_price(index_price, middle)},
            "seller_max_profit_SMP": round(box["seller_max_profit"]) if box.get("seller_max_profit") else None,
            "buyer_max_loss_BML": round(box["buyer_max_loss"]) if box.get("buyer_max_loss") else None,
            "confirmed_at_utc": confirmed.strftime("%Y-%m-%d %H:%M") if hasattr(confirmed, "strftime") else None,
            "confirmation": box.get("zone_confirmation_mode"),
        }
    else:
        result["zones"] = {"note": "No confirmed zones yet for the nearest expiry."}

    bands = []
    for inst in active_instruments(env, asset)[:Bands.TRACKED_EXPIRY_COUNT]:
        row = Bands.search([("instrument", "=", inst)], limit=1)
        if not row:
            bands.append({"instrument": inst, "note": "not confirmed yet"})
            continue
        up, lo = row.smart_liq_upper_price, row.smart_liq_lower_price
        up_s, lo_s = row.smart_liq_upper_strength, row.smart_liq_lower_strength
        bands.append({
            "instrument": inst,
            "high_resistance": round(row.high_resistance) if row.high_resistance else None,
            "low_support": round(row.low_support) if row.low_support else None,
            "gamma_band": round(row.gamma_band) if row.gamma_band else None,
            "gamma_band_vs_index": (_relative(row.gamma_band, index_price) or {}).get("text"),
            "smart_liquidity_upper": round(up) if up else None,
            "smart_liquidity_lower": round(lo) if lo else None,
            "stronger_smart_liquidity_side": ("upper" if up_s > lo_s else "lower" if lo_s > up_s else "equal")
                                             if (up_s or lo_s) else None,
            "updated_utc": row.computed_at.strftime("%Y-%m-%d %H:%M") if row.computed_at else None,
            "freshness": _hourly_freshness(row.computed_at),
        })
    result["bands_by_expiry_soonest_first"] = bands
    return result


def _hourly_freshness(when):
    """For data an hourly cron refreshes: older than 2 hours means the
    cron is behind or off."""
    if not when:
        return None
    age_h = (_now() - when).total_seconds() / 3600
    return "current" if age_h <= 2 else (f"STALE: last updated {age_h:.1f} hours ago (hourly updates may be "
                                         "paused) — tell the user")


_SIGNAL_TITLES = {"official": "OFFICIAL", "shadow": "SHADOW"}


def signal_bot(env, asset):
    """The Signal Bot's latest recorded decision for `asset` — same row
    the Delta Chart's Signal Bot panel shows (signal_bot_json): an active
    official plan if there is one, else today's newest decision."""
    Signal = env["dankbit.signal"].sudo()
    today = _now().date()
    row = (Signal.search([("asset", "=", asset), ("kind", "=", "official"), ("state", "=", "active")],
                         order="evaluated_at desc, id desc", limit=1)
           or Signal.search([("asset", "=", asset), ("utc_day", "=", today)],
                            order="evaluated_at desc, id desc", limit=1))
    week_start = today - timedelta(days=today.weekday())
    weekly = Signal.search_count([("asset", "=", asset), ("utc_day", ">=", week_start), ("kind", "=", "official")])
    base = {"asset": asset, "official_signals_this_week": weekly, "weekly_limit": 3,
            "basis": "the Signal Bot's own recorded decision (evaluated hourly), shown as-is — not advice"}
    if not row:
        return {**base, "note": "The Signal Bot has not evaluated this asset today."}
    direction = row.direction or "neutral"
    if row.state == "missed":
        title = f"MISSED ENTRY {direction.upper()}"
    elif row.kind in _SIGNAL_TITLES:
        title = f"{_SIGNAL_TITLES[row.kind]} {direction.upper()}"
    elif row.setup_stage in ("armed", "setup") and direction != "neutral":
        title = f"{row.setup_stage.upper()} {direction.upper()}"
    else:
        title = "NO TRADE"
    price = lambda v: round(v) if v and v > 0 else None
    state_label = dict(Signal._fields["state"].selection).get(row.state, row.state)
    return {
        **base,
        "decision": title,
        "kind": row.kind, "state": state_label, "direction": direction,
        "evaluated_at_utc": row.evaluated_at.strftime("%Y-%m-%d %H:%M") if row.evaluated_at else None,
        "freshness": _hourly_freshness(row.evaluated_at) if row.state not in ("active",) else None,
        "entry": price(row.entry), "stop_loss": price(row.stop_loss), "target": price(row.target),
        "risk_reward": round(row.risk_reward, 2) if row.risk_reward else None,
        "trend": {"daily": row.trend_daily or "neutral", "daily_tactical": row.trend_daily_tactical or "neutral",
                  "4h": row.trend_4h or "neutral", "1h": row.trend_1h or "neutral"},
        "rtm_zone": _range(row.rtm_zone_low, row.rtm_zone_high),
        "rtm_structure": dict(Signal._fields["rtm_structure"].selection).get(row.rtm_structure) if row.rtm_structure else None,
        "entry_model": dict(Signal._fields["entry_model"].selection).get(row.entry_model) if row.entry_model else None,
        "expires_at_utc": row.expires_at.strftime("%Y-%m-%d %H:%M") if row.expires_at else None,
        "reasons": [r.strip() for r in (row.reason or "").split(";") if r.strip()][:6],
    }


def _next_candle(env, asset, timeframe):
    row = env["dankbit.forecast.next_candle"].sudo().latest_for_dashboard(asset, timeframe)
    if not row:
        return {"timeframe": timeframe, "note": "The engine has not produced this forecast yet."}
    span = {"1h": 1, "4h": 4, "1d": 24}[timeframe]
    o, c = row.forecast_open, row.forecast_close
    return {
        "timeframe": timeframe,
        "candle_utc": f"{row.target_time:%Y-%m-%d %H:%M} to {row.target_time + timedelta(hours=span):%Y-%m-%d %H:%M}",
        "open": round(o), "close": round(c), "high": round(row.forecast_high), "low": round(row.forecast_low),
        "direction": "up (close above open)" if c > o else "down (close below open)" if c < o else "flat",
        "close_minus_open": round(c - o),
        "confidence_pct": round(row.confidence or 0),
        "revision": f"{row.revision} of {row.max_revisions}" + (" (final)" if row.is_final else ""),
        "computed_at_utc": f"{row.generated_at:%Y-%m-%d %H:%M}" if row.generated_at else None,
        "freshness": _forecast_freshness(row, span),
    }


def next_candle_forecast(env, asset):
    """The Thales Next Candle Forecast engine's latest stored revision for
    the NEXT 1h, 4h and 1d candle of the nearest expiry (the "EA1" anchor
    the Delta Chart's Thales Forecast series is built around) — read from
    dankbit.forecast.next_candle, never computed here. All three are
    returned so a question about any horizon ("next 8 hours") gets real
    data — asked for 8h with a 1h/4h/1d-only argument, the model refused
    instead of answering (2026-09-24)."""
    return {
        "asset": asset,
        "expiry": env["dankbit.bands"].sudo().nearest_expiry(asset),
        "basis": "the Thales Forecast engine's own stored output — one forecast per timeframe, each for the "
                 "NEXT candle only (the candle after the current one), revised during the current candle. "
                 "Report it as the engine's forecast, not your own prediction. There are no forecasts for "
                 "other horizons: for a different period, give the candles below that overlap it and state "
                 "their exact times.",
        "next_candles": [_next_candle(env, asset, tf) for tf in ("1h", "4h", "1d")],
    }


def _forecast_freshness(row, span):
    """A revision is due every span/max_revisions hours; older than 1.5
    steps means the revision cron is behind or off — say so, since the
    model otherwise presents an hours-old revision as current."""
    if not row.generated_at or not row.max_revisions:
        return None
    age_h = (_now() - row.generated_at).total_seconds() / 3600
    if age_h <= 1.5 * span / row.max_revisions:
        return "current"
    return (f"STALE: computed {age_h:.1f} hours ago; newer revisions are missing (forecast updates may be "
            "paused) — tell the user")


def dominant_legs_by_expiry(env, asset, hours=24, as_of=None):
    """Every active expiry's dominant gamma leg in one call, ranked by
    gamma size — exactly the lines the /ll chart draws (same
    main.ll_dominant_legs() the chart's /api/ll-gamma uses). Before this,
    "which expiry has the strongest gamma?" could only check the 2 expiries
    MAX_TOOL_CALLS allows and presented the bigger as the strongest overall
    (2026-09-24)."""
    from . import main as dankbit_main  # controller module; imported lazily
    live = as_of is None
    as_of = as_of or _now()
    hours = _hours(hours)
    expiries = dankbit_main.ll_dominant_legs(env, asset, _price_grid(env, asset), as_of, _window(hours, as_of),
                                             historical=not live)
    base = {"asset": asset, "window_hours": hours or "all", "expiries_considered": len(expiries),
            "legend": "each expiry's dominant leg = its leg with the largest gamma (LC/LP/SC/SP = long call / "
                      "long put / short call / short put); ranked by that gamma, largest first — the same "
                      "lines the LL chart draws. A dominant leg shows where gamma is concentrated, not whether "
                      "positioning is bullish or bearish, and not which leg was traded most"}
    if not expiries:
        return {**base, "note": "No expiry has trades in this window."}
    index_price = _index_price(env, asset) if live else None
    ranked = [{"rank": i + 1, "expiry": e["instrument"], "dominant_leg": e["dominant_leg"],
               "gamma_peak_price": round(e["dominant_price"]),
               "gamma_millions": round(e["abs_value"] / 1e6, 1),
               "vs_index": (_relative(e["dominant_price"], index_price) or {}).get("text"),
               # All of this expiry's trades in the window, every leg — named
               # so it isn't read as the dominant leg's own count (it was).
               "trades_in_this_expiry_all_legs": e["trade_count"],
               # Pre-computed so the model never divides: "2.7 times smaller"
               # was its own arithmetic (2026-09-24).
               "vs_strongest": ("the strongest" if i == 0 else
                                f"{expiries[0]['abs_value'] / e['abs_value']:.1f} times smaller than the strongest"
                                if e["abs_value"] else None)}
              for i, e in enumerate(expiries[:12])]
    return {**base, "index_price": index_price, "strongest": ranked[0], "ranked_by_gamma": ranked,
            **({"note": f"showing the top 12 of {len(expiries)}"} if len(expiries) > 12 else {})}


# ------------------------------------------------------- tool registry

_ASSET = {"type": "string", "enum": list(ASSETS)}
_INSTR = {"type": "string", "description": "Expiry instrument, e.g. BTC-25SEP26. Must be one of the active expiries."}
_HOURS = {"type": "integer", "description": "Trailing window in hours, e.g. 4, 24, 72. Use 0 for no window "
                                            "(all trades / whole history)."}
_AT = {"type": "string", "description": "Only for questions about a PAST moment: that moment as YYYY-MM-DD HH:MM, "
                                        "in the timezone the user used. Omit for now."}
_TZ = {"type": "string", "enum": list(TIMEZONES), "description": "Timezone of at_time (default UTC)."}


def _spec(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


TOOL_SPECS = [
    _spec("trade_summary", "Overall option flow: trade count, buys vs sells, call vs put contracts, premium, "
          "block trades. Leave instrument empty for all expiries.",
          {"asset": _ASSET, "hours": _HOURS, "instrument": _INSTR, "at_time": _AT, "timezone": _TZ},
          ["asset", "hours"]),
    _spec("largest_trades", "The biggest individual option trades by premium. Leave instrument empty for all expiries.",
          {"asset": _ASSET, "hours": _HOURS, "instrument": _INSTR,
           "n": {"type": "integer", "description": "How many, 1-10"},
           "block_only": {"type": "boolean", "description": "Only block trades"},
           "at_time": _AT, "timezone": _TZ}, ["asset", "hours"]),
    _spec("block_trades", "Block trades, newest first: when the last block trade was, how many there were, "
          "and the list. Leave instrument empty for all expiries.",
          {"asset": _ASSET, "hours": _HOURS, "instrument": _INSTR,
           "n": {"type": "integer", "description": "How many block trades to list, 1-10"},
           "at_time": _AT, "timezone": _TZ}, ["asset", "hours"]),
    _spec("gamma_legs", "Gamma structure of one expiry: gamma peak price of long call/long put/short call/"
          "short put trades, the dominant leg, delta-zero prices and gamma-flip prices.",
          {"asset": _ASSET, "instrument": _INSTR, "hours": _HOURS, "at_time": _AT, "timezone": _TZ},
          ["asset", "instrument", "hours"]),
    _spec("dominant_legs_by_expiry", "Every active expiry's dominant gamma leg, ranked by gamma size: which "
          "expiry has the strongest/weakest gamma, and each expiry's dominant leg and its price (the LL chart's "
          "lines). Use for comparing expiries or for 'all expiries'.",
          {"asset": _ASSET, "hours": _HOURS, "at_time": _AT, "timezone": _TZ}, ["asset", "hours"]),
    _spec("strike_activity", "Traded VOLUME per strike for one expiry: the most traded strikes (ranked) and "
          "which strikes are net bought or net sold, for calls and puts. (Persian: حجم معاملات)",
          {"asset": _ASSET, "instrument": _INSTR, "hours": _HOURS, "at_time": _AT, "timezone": _TZ},
          ["asset", "instrument", "hours"]),
    _spec("max_pain", "Open interest (OI, open contracts — not traded volume) of one expiry: max pain (the strike where option buyers lose the most "
          "at expiry), call/put open interest totals, and the strikes with the most open interest (OI).",
          {"asset": _ASSET, "instrument": _INSTR, "at_time": _AT, "timezone": _TZ}, ["asset", "instrument"]),
    _spec("recent_vs_total", "Is recent flow changing the book / existing positioning of one expiry? Compares "
          "the last N hours of trades with the whole history of that expiry: net call and put buying, dominant "
          "gamma leg, and whether recent flow goes with or against the existing positioning.",
          {"asset": _ASSET, "instrument": _INSTR,
           "hours": {"type": "integer", "description": "The RECENT window in hours (default 4)."},
           "at_time": _AT, "timezone": _TZ}, ["asset", "instrument"]),
    _spec("zones_and_bands", "The Delta Chart's levels: High Zone, Low Zone and Middle Zone (SMP/BML) of the "
          "nearest expiry, and per expiry the High/Resistance, Low/Support, Gamma Band and Smart Liquidity levels. "
          "Current values only. (Persian: زون بالا / زون پایین / زون میانی, مقاومت, حمایت, باند گاما, "
          "اسمارت لیکوییدیتی)", {"asset": _ASSET}, ["asset"]),
    _spec("signal_bot", "The Signal Bot's latest decision: official/shadow/no trade, direction, entry, stop loss, "
          "target, risk/reward, trends and its reasons. (Persian: سیگنال بات, سیگنال)", {"asset": _ASSET}, ["asset"]),
    _spec("next_candle_forecast", "The Thales Forecast engine's forecast (use for ANY forecast question, any "
          "horizon): the next 1h, 4h and 1d candle with open, close, high, low, direction and confidence. "
          "(Persian: پیش‌بینی, فورکست)", {"asset": _ASSET}, ["asset"]),
    _spec("compare_windows", "How the gamma structure of one expiry changed between now and N hours ago.",
          {"asset": _ASSET, "instrument": _INSTR, "hours": _HOURS,
           "hours_ago": {"type": "integer", "description": "How many hours back to compare against"}},
          ["asset", "instrument", "hours_ago"]),
]

TOOLS = {
    "trade_summary": trade_summary,
    "largest_trades": largest_trades,
    "block_trades": block_trades,
    "gamma_legs": gamma_legs,
    "dominant_legs_by_expiry": dominant_legs_by_expiry,
    "strike_activity": strike_activity,
    "compare_windows": compare_windows,
    "recent_vs_total": recent_vs_total,
    "max_pain": max_pain,
    "zones_and_bands": zones_and_bands,
    "signal_bot": signal_bot,
    "next_candle_forecast": next_candle_forecast,
}
# Tools whose numbers must match the chart's own lines: an unrequested
# hours=0 ("all") from the model is replaced by the page's Window.
CHART_WINDOW_TOOLS = {"gamma_legs", "strike_activity", "compare_windows", "dominant_legs_by_expiry"}
NEEDS_INSTRUMENT = {"recent_vs_total", "gamma_legs", "strike_activity", "compare_windows", "max_pain"}
# Tools that accept a historical `as_of` (from the model's at_time/timezone).
AS_OF_TOOLS = {"trade_summary", "largest_trades", "block_trades", "gamma_legs", "strike_activity",
               "dominant_legs_by_expiry",
               "recent_vs_total"}
# `hours` is the RECENT window here, compared against all history by the
# tool itself — never forced to "all" (that would compare history with itself).
RECENT_WINDOW_TOOLS = {"recent_vs_total"}
ALLOWED_ARGS = {
    "trade_summary": {"asset", "hours", "instrument"},
    "largest_trades": {"asset", "hours", "instrument", "n", "block_only"},
    "block_trades": {"asset", "hours", "instrument", "n"},
    "gamma_legs": {"asset", "instrument", "hours"},
    "dominant_legs_by_expiry": {"asset", "hours"},
    "strike_activity": {"asset", "instrument", "hours"},
    "compare_windows": {"asset", "instrument", "hours", "hours_ago"},
    "recent_vs_total": {"asset", "instrument", "hours"},
    "max_pain": {"asset", "instrument"},
    "zones_and_bands": {"asset"},
    "signal_bot": {"asset"},
    "next_candle_forecast": {"asset"},
}
