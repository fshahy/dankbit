# -*- coding: utf-8 -*-
"""Dealer Hedging Simulator — infers market-maker (dealer) gamma/delta
exposure from real trade flow and simulates the underlying-hedging
pressure that exposure implies, to surface likely price-move dynamics
(gamma-flip regime, pin/gamma-wall levels, and a short deterministic
forward candle path driven purely by that hedging pressure).

Dealer positioning convention: every trade in dankbit.trade already
carries a taker-signed direction (direction="buy" -> the taker went
long, "sell" -> the taker went short — see gamma.py/delta.py's own
_infer_sign and options.per_leg_greeks' long_call/short_call split).
This addon has no per-instrument counterparty data, so — the same
simplifying assumption every public dealer-positioning ("GEX") tool
makes — the dealer/market-maker is treated as the counterparty to
every trade: dealer_gamma(S) = -portfolio_gamma(S, trades) and
dealer_delta(S) = -portfolio_delta(S, trades), i.e. the exact mirror
image of the customer/taker curves already plotted everywhere else in
this addon (chart_png_zones' Longs/Shorts curves, per_leg_greeks'
long_*/short_* split, dankbit.bands' gamma_band/delta_band). This is
deliberately NOT based on Deribit's live open-interest snapshot (no
new external API call, no new caching) — it stays consistent with
every other Greek/flow computation in this addon, which is trade-flow
based, not OI-snapshot based.

Display-only — writes no model, exports nothing to any other engine
(Forecast/Bands/Signal Bot/etc. are all unaffected by this module)."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np

from odoo import http
from odoo.http import request

from . import delta as delta_lib
from . import gamma as gamma_lib
from . import options

# --- Dealer curves & regime -------------------------------------------------

# Dead zone (dollars of dealer dollar-gamma per $1 spot move) around zero
# below which the regime is reported "neutral" rather than forcing a call
# either way — avoids flapping between short/long-gamma labels on a
# trivially small residual exposure.
REGIME_DEAD_ZONE = 5_000.0


def classify_regime(dealer_gamma_value, dead_zone=REGIME_DEAD_ZONE):
    """"short_gamma" (dealers must trade WITH price to stay hedged —
    amplifies moves), "long_gamma" (dealers trade AGAINST price — damps/
    pins), or "neutral" within `dead_zone` of flat."""
    if dealer_gamma_value > dead_zone:
        return "long_gamma"
    if dealer_gamma_value < -dead_zone:
        return "short_gamma"
    return "neutral"


def dealer_gamma_curve(STs, trades, r=0.0):
    """Dealer's own dollar-gamma exposure across `STs` — the mirror image
    of gamma.portfolio_gamma()'s taker-signed curve (see module
    docstring)."""
    return -np.asarray(gamma_lib.portfolio_gamma(STs, trades, r=r), dtype=float)


def dealer_delta_curve(STs, trades, r=0.0):
    """Dealer's own net delta exposure across `STs` — the mirror image of
    delta.portfolio_delta()'s taker-signed curve."""
    return -np.asarray(delta_lib.portfolio_delta(STs, trades, r=r), dtype=float)


def find_gamma_flip_prices(STs, dealer_gamma_vals):
    """Zero-crossings of the dealer gamma curve — the price level(s)
    where dealer positioning flips between short gamma (dealers must
    trade WITH price, amplifying moves) and long gamma (dealers trade
    AGAINST price, damping/pinning it). Reuses the same linear-
    interpolation crossing finder every other zero-crossing in this
    addon uses (options.find_zero_crossings)."""
    return sorted(options.find_zero_crossings(STs, dealer_gamma_vals))


def nearest_gamma_flip(STs, dealer_gamma_vals, index_price):
    flips = find_gamma_flip_prices(STs, dealer_gamma_vals)
    if not flips:
        return None
    return min(flips, key=lambda p: abs(p - index_price))


def dealer_gamma_walls(trades, index_price, r=0.0, top_n=12):
    """Dealer dollar-gamma AT THE CURRENT SPOT, aggregated per real listed
    strike (not a synthetic price grid) — the classic "gamma by strike"
    bar-chart view. Frozen at index_price like every public gamma-by-
    strike chart (a per-contract dollar-gamma value technically varies
    with spot, but by convention this view answers "how much gamma sits
    at each strike right now", not "at every possible future spot" —
    that's what dealer_gamma_curve()/the flip point are for).

    Returns the `top_n` strikes by |dealer dollar gamma| descending, each
    {"strike", "dealer_gamma"} (dealer-signed: positive = dealer long
    gamma at that strike = a pinning magnet, negative = dealer short
    gamma = an acceleration level). Same "ignore iv=0" rule every
    Greek-feeding fetch site in this addon applies."""
    by_strike = {}
    for trd in trades:
        if trd.iv == 0:
            continue
        T = trd.get_hours_to_expiry() / (24.0 * 365.0)
        sigma = trd.iv / 100.0
        # Dealer is the counterparty: customer buy -> dealer short (-1),
        # customer sell -> dealer long (+1) — see module docstring.
        if trd.direction == "buy":
            dealer_sign = -1.0
        elif trd.direction == "sell":
            dealer_sign = 1.0
        else:
            continue
        g = float(gamma_lib.bs_gamma(index_price, trd.strike, T, r, sigma))
        by_strike[trd.strike] = by_strike.get(trd.strike, 0.0) + dealer_sign * trd.amount * g

    walls = [{"strike": float(k), "dealer_gamma": float(v)} for k, v in by_strike.items() if v]
    walls.sort(key=lambda w: abs(w["dealer_gamma"]), reverse=True)
    return walls[:top_n]


# --- Simulator ---------------------------------------------------------

def _atr14(candles):
    """Classic ATR over the last 14 real candles (true range = max of
    high-low, |high-prevclose|, |low-prevclose|) — same formula
    forecast.py's own module-private _atr14 uses, reimplemented locally
    rather than imported since that helper is private to forecast.py
    (matching next_candle_forecast.py's own established precedent of
    locally reimplementing rather than reaching into another module's
    private helpers)."""
    if len(candles) < 15:
        return None
    trs = []
    for i in range(len(candles) - 14, len(candles)):
        c, prev = candles[i], candles[i - 1]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - prev["c"]), abs(c["l"] - prev["c"])))
    return sum(trs) / len(trs)


def _trigger_return(candles, lookback=4):
    """Sum of the last `lookback` closed candles' own fractional returns
    — the short realized-momentum "trigger" simulate_hedging_path()
    amplifies (short-gamma regime) or damps (long-gamma regime), rather
    than inventing a direction from nothing at the starting spot."""
    if len(candles) < lookback + 1:
        return 0.0
    total = 0.0
    for c in candles[-lookback:]:
        if c["o"]:
            total += (c["c"] - c["o"]) / c["o"]
    return total


# Per-step tunables — fixed module constants for now (a v1 heuristic,
# same "documented but not yet backtested/settings-exposed" status this
# addon's own newer confidence/scoring heuristics carry, e.g.
# dankbit.forecast.next_candle's own compute_confidence()).
SENSITIVITY = 0.22             # how strongly hedge-flow pressure scales into a step move, as a fraction of ATR
DELTA_NORMALIZER = 250.0       # portfolio_delta's own scale is "in the hundreds" (see options.py's delta_saturation_price docstring) — divides the raw hedge-flow before tanh-squashing it to [-1, 1]
AMPLIFY_MULT = 1.6             # short-gamma regime: momentum multiplier (dealers trade WITH price -> amplifies)
DAMPEN_MULT = 0.45             # long-gamma regime: momentum multiplier (dealers trade AGAINST price -> damps)
MOMENTUM_DECAY = 0.90          # per-step decay of the initial realized-momentum trigger
PIN_PULL_STRENGTH = 0.35       # long-gamma regime: max per-step pull toward the nearest gamma wall, as a fraction of ATR
PIN_RANGE_ATR = 3.0            # how many ATRs away a gamma wall can still be felt as a pin
MAX_STEP_ATR_FRACTION = 0.6    # hard cap on any single step's move, as a fraction of ATR
WICK_ATR_FRACTION = 0.12       # deterministic wick sizing — no random component, matching this addon's own Thales Forecast engine's "fully deterministic" design


def simulate_hedging_path(STs, dealer_gamma_vals, dealer_delta_vals, walls, index_price, atr,
                           trigger_return, hours_ahead=24, step_hours=1):
    """Deterministic forward candle path driven purely by simulated
    dealer hedging pressure — NOT blended with the existing Thales
    Forecast engine (forecast.py); the value of this simulator is
    showing what dealer hedging ALONE implies. Every step combines 3
    explainable terms:

    1. Hedge-flow pressure: as simulated spot S drifts from index_price,
       dealers' own net delta exposure changes; to stay hedged they must
       trade -(dealer_delta(S) - dealer_delta(index_price)) of the
       underlying. A positive value means dealers must BUY -> bullish
       price pressure.
    2. Regime-scaled momentum: the recent realized `trigger_return` is
       amplified in a short-gamma regime (dealers trade WITH price) or
       damped in a long-gamma regime (dealers trade AGAINST price),
       decaying by MOMENTUM_DECAY each step.
    3. Gamma-wall pin: in a long-gamma regime only, a pull toward the
       nearest strong dealer-long-gamma wall within PIN_RANGE_ATR ATRs,
       modeling dealers defending that level.

    No random/GBM term anywhere (same "fully deterministic" design this
    addon's own Thales Forecast engine documents) — every candle is a
    direct function of current dealer positioning and recent real price
    action. `atr` of 0/None falls back to 0.5% of index_price so the
    simulation still produces a (small) path rather than dividing by
    zero. Returns a list of {"hours", "open", "high", "low", "close",
    "regime", "pressure", "hedge_flow"} dicts, `hours` being an offset
    (not an absolute time) so the caller anchors it to whichever "now"
    it cares about."""
    atr = atr or (index_price * 0.005)
    dealer_delta_at_s0 = float(np.interp(index_price, STs, dealer_delta_vals))
    pin_walls = [w for w in walls if w["dealer_gamma"] > 0]

    candles = []
    S = index_price
    n_steps = max(1, int(round(hours_ahead / step_hours)))
    for step in range(1, n_steps + 1):
        g = float(np.interp(S, STs, dealer_gamma_vals))
        d = float(np.interp(S, STs, dealer_delta_vals))
        regime = classify_regime(g)

        hedge_flow = -(d - dealer_delta_at_s0)
        pressure = float(np.tanh(hedge_flow / DELTA_NORMALIZER))

        regime_mult = AMPLIFY_MULT if regime == "short_gamma" else (DAMPEN_MULT if regime == "long_gamma" else 1.0)
        momentum_component = trigger_return * regime_mult * (MOMENTUM_DECAY ** step)

        pin_component = 0.0
        if regime == "long_gamma" and pin_walls:
            nearest_wall = min(pin_walls, key=lambda w: abs(w["strike"] - S))
            distance_atr = abs(S - nearest_wall["strike"]) / atr
            if distance_atr < PIN_RANGE_ATR:
                pin_component = -((S - nearest_wall["strike"]) / atr) / PIN_RANGE_ATR * PIN_PULL_STRENGTH

        step_return = SENSITIVITY * pressure + momentum_component + pin_component
        step_return = max(-MAX_STEP_ATR_FRACTION, min(MAX_STEP_ATR_FRACTION, step_return))
        step_move = step_return * atr

        open_, close_ = S, S + step_move
        wick = WICK_ATR_FRACTION * atr
        candles.append({
            "hours": step * step_hours,
            "open": open_, "close": close_,
            "high": max(open_, close_) + wick,
            "low": min(open_, close_) - wick,
            "regime": regime,
            "pressure": pressure,
            "hedge_flow": hedge_flow,
        })
        S = close_
    return candles


# --- Controller ----------------------------------------------------------

# Curve resolution sent to the client — the full from_price/to_price/steps
# STs grid can run into the thousands of points, far more than a chart
# needs; coarsened to keep the JSON payload small.
CURVE_POINTS = 150


class DealerHedgingController(http.Controller):

    @http.route("/api/dealer-hedging/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def dealer_hedging_json(self, asset, **kw):
        """Computed fresh on every request — no model/table behind this
        route. Backs the /dhs/<asset> ("Dealer Hedging Simulator") page.
        See the module docstring for the dealer-positioning convention
        and simulate_hedging_path() for the forecast path's own design.

        ?expiry_index= (0/1/2, default 0) selects one of
        dankbit.bands.TRACKED_EXPIRY_COUNT active expiries (0 = nearest),
        trades ISOLATED to that one instrument (same anchored `=ilike`
        domain every other per-expiry route in this addon uses).
        ?expiry=all instead considers every one of asset's own
        non-expired instruments at once (CUMULATIVE), matching
        four_leg_gamma_json's own "all" mode.

        ?hours= is a trailing-hours trade window (int, clamped 1..8760),
        or the literal "midnight" (options.day_window_start) or "all"
        (default) — every trade for the resolved instrument(s) since
        they started trading, not just today: unlike the rest of this
        addon's "since 00:00 UTC" zones/bands convention, a dealer's
        real current hedge position accumulates over an instrument's
        whole life, not just today's flow, so "all" is the more
        representative default here.

        Returns {asset, instrument, expiry_index, window_hours,
        index_price, trade_count, dealer_gamma_at_spot,
        dealer_delta_at_spot, regime, gamma_flip_price,
        gamma_flip_prices, gamma_walls, curve: {prices, dealer_gamma,
        dealer_delta} (a coarse ~150-point profile for plotting),
        simulation: {hours_ahead, step_hours, candles}}. Empty/None
        fields follow this addon's usual nothing-computable-yet
        convention when there's no active expiry or no matching
        trades."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        icp = request.env["ir.config_parameter"].sudo()
        if asset == "BTC":
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            step = float(icp.get_param("dankbit.steps", default=100))
        else:
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            step = float(icp.get_param("dankbit.eth_steps", default=50))
        STs = np.arange(from_price, to_price, step, dtype=np.float64)

        args = request.httprequest.args
        expiry_param = (args.get("expiry") or "").lower()
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        bands_model = request.env["dankbit.bands"]
        trade_model = request.env["dankbit.trade"].with_context(active_test=False)

        instrument = None
        expiry_index = None
        if expiry_param == "all":
            domain = [("name", "=ilike", f"{asset}-%"), ("iv", "!=", 0), ("expiration", ">=", as_of)]
        else:
            try:
                expiry_index = max(0, min(2, int(args.get("expiry_index", 0))))
            except (TypeError, ValueError):
                expiry_index = 0
            expirations = bands_model._distinct_expirations(asset, as_of, expiry_index + 1)
            if len(expirations) <= expiry_index:
                payload = {
                    "asset": asset, "instrument": None, "expiry_index": expiry_index,
                    "window_hours": None, "index_price": 0.0, "trade_count": 0,
                    "dealer_gamma_at_spot": 0.0, "dealer_delta_at_spot": 0.0, "regime": "neutral",
                    "gamma_flip_price": None, "gamma_flip_prices": [], "gamma_walls": [],
                    "curve": None, "simulation": None,
                }
                return request.make_response(
                    json.dumps(payload),
                    headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
                )
            instrument = bands_model._format_instrument(asset, expirations[expiry_index])
            domain = [("name", "=ilike", f"{instrument}-%"), ("iv", "!=", 0)]

        hours_param = args.get("hours", "all")
        if hours_param in ("all", "midnight"):
            hours = hours_param
        else:
            try:
                hours = max(1, min(int(hours_param), 8760))
            except (TypeError, ValueError):
                hours = "all"

        if hours == "midnight":
            domain.append(("deribit_ts", ">=", options.day_window_start(as_of)))
        elif hours != "all":
            domain.append(("deribit_ts", ">=", as_of - timedelta(hours=hours)))

        trades = trade_model.search(domain)
        index_price = trade_model.get_index_price(asset)

        if not trades or not index_price:
            payload = {
                "asset": asset, "instrument": instrument, "expiry_index": expiry_index,
                "window_hours": hours, "index_price": index_price, "trade_count": len(trades),
                "dealer_gamma_at_spot": 0.0, "dealer_delta_at_spot": 0.0, "regime": "neutral",
                "gamma_flip_price": None, "gamma_flip_prices": [], "gamma_walls": [],
                "curve": None, "simulation": None,
            }
            return request.make_response(
                json.dumps(payload),
                headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
            )

        dealer_gamma_vals = dealer_gamma_curve(STs, trades)
        dealer_delta_vals = dealer_delta_curve(STs, trades)

        dealer_gamma_at_spot = float(np.interp(index_price, STs, dealer_gamma_vals))
        dealer_delta_at_spot = float(np.interp(index_price, STs, dealer_delta_vals))
        regime = classify_regime(dealer_gamma_at_spot)
        flips = find_gamma_flip_prices(STs, dealer_gamma_vals)
        flip_price = nearest_gamma_flip(STs, dealer_gamma_vals, index_price)
        walls = dealer_gamma_walls(trades, index_price)

        curve_n = min(len(STs), CURVE_POINTS)
        curve_idx = np.linspace(0, len(STs) - 1, curve_n).astype(int)

        real_candles = trade_model.get_candles(asset, interval="1h", limit=60)
        atr = _atr14(real_candles)
        trigger = _trigger_return(real_candles)
        simulation_candles = simulate_hedging_path(
            STs, dealer_gamma_vals, dealer_delta_vals, walls, index_price, atr, trigger,
        )

        payload = {
            "asset": asset, "instrument": instrument, "expiry_index": expiry_index,
            "window_hours": hours, "index_price": index_price, "trade_count": len(trades),
            "dealer_gamma_at_spot": dealer_gamma_at_spot, "dealer_delta_at_spot": dealer_delta_at_spot,
            "regime": regime, "gamma_flip_price": flip_price, "gamma_flip_prices": flips,
            "gamma_walls": walls,
            "curve": {
                "prices": [float(STs[i]) for i in curve_idx],
                "dealer_gamma": [float(dealer_gamma_vals[i]) for i in curve_idx],
                "dealer_delta": [float(dealer_delta_vals[i]) for i in curve_idx],
            },
            "simulation": {"hours_ahead": 24, "step_hours": 1, "candles": simulation_candles},
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/dhs/<string:asset>", type="http", auth="user", website=True)
    def dealer_hedging_chart(self, asset):
        """Standalone TradingView-style page — the "Dealer Hedging
        Simulator": real Binance-spot candles (/api/klines/<asset>, same
        source the Delta Chart uses) plus dealer-positioning overlays
        (gamma-flip line, top gamma-wall levels) and a forward
        candlestick path driven purely by simulated dealer hedging
        pressure (see controllers/dealer_hedging.py). No model/table
        behind this page — 404 for an unrecognized asset."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        ctx = {"asset": asset, "refresh_interval": refresh_interval}
        return request.render("dankbit.dankbit_dealer_hedging_chart", ctx)
