import base64
import json
import numpy as np
from datetime import datetime, timedelta, timezone
from io import BytesIO
from matplotlib import transforms as mtransforms
from odoo import http
from odoo.http import request
from . import options
from . import delta
from . import gamma
from . import next_candle_forecast

# /4l/<asset>'s own "Window" dropdown numeric choice set — 4h/8h/12h/
# 24h/48h/72h — split out from Y_CHART_WINDOW_HOURS_CHOICES (which
# /gt/<asset> still uses unchanged) once this page's own choice set grew
# past that shared 12/24/48 tuple, so /gt/<asset>'s own "Window"
# dropdown is unaffected. 16h/20h/48h/72h and the "All" (no-window-bound)
# option were offered at one point and removed per product decision;
# 3h/5h/7h were added later, filling in the gaps left in the original
# 1h/2h/4h/6h/8h/12h set; 24h and "All" were both re-added afterwards
# per later product decisions; 1h/2h/3h/4h/5h/6h/7h/8h were then removed
# in a further round, leaving only 12h/24h in this tuple; 8h was then
# re-added per a later product decision, and 4h after that; 1h/2h/3h/
# 5h/6h/7h were then re-added per a further product decision, filling
# this tuple back out to every 1h step from 1h through 8h plus 12h/24h;
# 1h/2h/3h/5h/6h/7h were then removed again and 48h/72h added per a
# still later product decision, leaving 4h/8h/12h/24h/48h/72h. "All"
# (`?hours=all`) is handled as a separate string sentinel in
# four_leg_gamma_json, not a member of this tuple — it skips the
# trailing-hours trade filter entirely rather than mapping to a number
# of hours.
FOUR_LEG_WINDOW_HOURS_CHOICES = (4, 8, 12, 24, 48, 72)

# /4l/<asset>'s own "Window" dropdown default — also the fallback used
# by four_leg_gamma_json when `?hours=` is missing/malformed, same as
# every other *_DEFAULT_WINDOW_HOURS fallback in this file. Was a number
# (8, i.e. 8h) until 1h-8h were removed from FOUR_LEG_WINDOW_HOURS_CHOICES
# above, at which point the default moved to the "all" string sentinel
# (see four_leg_gamma_json); moved back to 8 (8h) per product decision —
# also cuts the routine per-poll cost of the page's other 4 lines, since
# "all" meant every poll scanned that instrument's entire trade history —
# then to 24 (24h) per a later product decision, then to 4 (4h) per a
# still later product decision, then back to 24 (24h) once more per a
# still later product decision.
FOUR_LEG_DEFAULT_WINDOW_HOURS = 24

def _forecast_leg_completeness(snapshot):
    """Return whether all four participant legs have any usable Greek pair.

    A whole absent Buyer-Put/Seller-Put/etc. branch previously left BML/SMP
    free to jump to a grid edge and then became a seemingly valid expiry
    anchor.  One non-zero price+strength pair in gamma/delta/theta/vega is
    enough to keep a leg; an entirely absent leg makes the anchor unavailable.
    Works with both snapshot records and preview dictionaries.
    """
    def value(name):
        if isinstance(snapshot, dict):
            return snapshot.get(name)
        return getattr(snapshot, name, None)

    missing = []
    for leg in ("bc", "bp", "sc", "sp"):
        supported = False
        for greek in ("g", "d", "t", "v"):
            price = value(leg + greek + "_price")
            strength = value(leg + greek + "_abs")
            if price and float(price) > 0 and strength and float(strength) > 0:
                supported = True
                break
        if not supported:
            missing.append(leg.upper())
    return not missing, missing


def _forecast_quality_warning(points, anchor_meta, index_price, sigma_annual, timeframe):
    """Assess confidence separately from the Forecast geometry.

    The restored Forecast engine is intentionally allowed to show its raw
    directional path again.  This guard does not reshape, clamp, reject, or
    hide a candle.  It raises a warning only when option-data quality is low
    *and* the resulting path is statistically large relative to the current
    implied-volatility move for the selected candle width/horizon.
    """
    available = [a for a in (anchor_meta or []) if a.get("available")]
    coverage = len(available) / 3.0
    confidences = [float(a.get("confidence") or 0.0) for a in available]
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    quality_score = max(min(coverage * 60.0 + mean_confidence * 0.40, 100.0), 0.0)
    low_quality = quality_score < 65.0

    step_hours = {"1h": 1.0, "4h": 4.0, "1d": 24.0}.get(timeframe, 4.0)
    base_price = float(index_price or 0.0)
    sigma = float(sigma_annual or 0.0)
    max_body_sigma = 0.0
    max_path_sigma = 0.0
    if base_price > 0 and sigma > 0:
        for point in points or []:
            hours_out = max(float(point.get("hours") or step_hours), step_hours)
            body_expected = float(base_price * sigma * np.sqrt(step_hours / (365.0 * 24.0)))
            path_expected = float(base_price * sigma * np.sqrt(hours_out / (365.0 * 24.0)))
            body_sigma = abs(float(point["close"]) - float(point["open"])) / max(body_expected, 1e-9)
            path_sigma = abs(float(point["close"]) - base_price) / max(path_expected, 1e-9)
            max_body_sigma = max(max_body_sigma, body_sigma)
            max_path_sigma = max(max_path_sigma, path_sigma)

    extremity = float(max(max_body_sigma, max_path_sigma))
    extreme = bool(extremity >= 2.25)
    active = bool(low_quality and extreme)
    missing_reasons = [
        a.get("unavailable_reason") for a in (anchor_meta or [])
        if not a.get("available") and a.get("unavailable_reason")
    ]
    return {
        "active": active,
        "code": "LOW_OPTION_DATA_EXTREME_FORECAST" if active else None,
        "severity": "high" if active and extremity >= 3.5 else ("warning" if active else "none"),
        "quality_score": round(quality_score, 1),
        "quality_level": "low" if low_quality else ("medium" if quality_score < 80.0 else "good"),
        "anchor_coverage": len(available),
        "mean_anchor_confidence": round(mean_confidence, 1),
        "max_body_sigma": round(max_body_sigma, 2),
        "max_path_sigma": round(max_path_sigma, 2),
        "missing_reasons": missing_reasons,
        "message": "Low option-data quality: the Forecast path may be exaggerated." if active else None,
        "message_fa": "کیفیت داده آپشن پایین است؛ حرکت کندل‌های فورکست ممکن است افراطی باشد." if active else None,
    }


def _compose_unified_forecast(raw_points, anchors=None, count=18, max_count=18, timeframe="4h"):
    """Build a continuous three-expiry path around up to three anchors.

    Keep the original calculated body sizes. Intermediate bodies are corrected
    only to connect expiry anchors without a gap. Data-quality risk is reported
    separately; it must not flatten bodies or turn displacement into wicks.
    """
    limit = max(1, min(int(count or max_count), int(max_count)))
    source = list(raw_points or [])[:limit]
    if not source:
        return []

    anchors = anchors or {}
    candles_per_day = {"1h": 24, "4h": 6, "1d": 1}.get(timeframe, 6)
    anchor_by_index = {
        expiry_index * candles_per_day: candle
        for expiry_index, candle in anchors.items()
        if candle and expiry_index * candles_per_day < len(source)
    }

    def geometry(raw, new_open, body=None, wick_source=None, mode=None):
        old_open = float(raw["open"])
        old_close = float(raw["close"])
        old_high = float(raw["high"])
        old_low = float(raw["low"])
        candle_body = old_close - old_open if body is None else float(body)
        wick = wick_source or raw
        wick_open = float(wick["open"])
        wick_close = float(wick["close"])
        wick_high = float(wick["high"])
        wick_low = float(wick["low"])
        upper_wick = max(0.0, wick_high - max(wick_open, wick_close))
        lower_wick = max(0.0, min(wick_open, wick_close) - wick_low)
        new_close = float(new_open) + candle_body
        return {
            "hours": raw.get("hours"), "open": float(new_open),
            "high": max(float(new_open), new_close) + upper_wick,
            "low": min(float(new_open), new_close) - lower_wick,
            "close": new_close, "mode": mode or raw.get("mode"),
        }

    output = []
    first_anchor = anchor_by_index.get(0)
    if first_anchor:
        output.append(geometry(
            source[0], float(source[0]["open"]),
            body=float(first_anchor["close"]) - float(first_anchor["open"]),
            wick_source=first_anchor, mode="expiry_anchor_1",
        ))
    else:
        output.append(dict(source[0]))

    cursor = 1
    for anchor_index in sorted(i for i in anchor_by_index if i > 0):
        anchor = anchor_by_index[anchor_index]
        intermediate = source[cursor:anchor_index]
        target_open = float(source[anchor_index]["open"])
        if intermediate:
            raw_total = sum(float(p["close"]) - float(p["open"]) for p in intermediate)
            correction = (target_open - float(output[-1]["close"]) - raw_total) / len(intermediate)
            for raw in intermediate:
                adjusted_body = float(raw["close"]) - float(raw["open"]) + correction
                output.append(geometry(raw, output[-1]["close"], body=adjusted_body))
        output.append(geometry(
            source[anchor_index], float(output[-1]["close"]),
            body=float(anchor["close"]) - float(anchor["open"]),
            wick_source=anchor,
            mode="expiry_anchor_%s" % (anchor_index // candles_per_day + 1),
        ))
        cursor = anchor_index + 1

    for raw in source[cursor:]:
        output.append(geometry(raw, float(output[-1]["close"])))
    return output


def _rsi_forecast_geometry(points, timeframe_candles, hourly_candles, timeframe="4h", period=14, ma_period=14):
    """Adjust only early Forecast wick geometry from RSI mean-reversion risk.

    Open/Close and therefore direction, path and EA locations are immutable.
    A large RSI-vs-RSI-MA gap can lengthen the rejection-side wick, but the
    effect is progressively suppressed while closed 1H price momentum in the
    prevailing direction remains strong. Recomputed on every request.
    """
    output = [dict(p) for p in (points or [])]
    tf_closed = list(timeframe_candles or [])[:-1]
    h1_closed = list(hourly_candles or [])[:-1]

    def value(row, key):
        aliases = {"o": "open", "h": "high", "l": "low", "c": "close"}
        return float(row.get(key, row.get(aliases[key], 0.0)) or 0.0)

    def atr(rows, length=14):
        if len(rows) < 2:
            return 0.0
        ranges = []
        for prev, current in zip(rows[-length - 1:-1], rows[-length:]):
            high, low, prev_close = value(current, "h"), value(current, "l"), value(prev, "c")
            ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return sum(ranges) / len(ranges) if ranges else 0.0

    def rsi_values(closes, length):
        if len(closes) <= length:
            return []
        deltas = [b - a for a, b in zip(closes, closes[1:])]
        gains = [max(d, 0.0) for d in deltas]
        losses = [max(-d, 0.0) for d in deltas]
        avg_gain = sum(gains[:length]) / length
        avg_loss = sum(losses[:length]) / length
        values = [100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)]
        for gain, loss in zip(gains[length:], losses[length:]):
            avg_gain = (avg_gain * (length - 1) + gain) / length
            avg_loss = (avg_loss * (length - 1) + loss) / length
            values.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
        return values

    closes = [value(c, "c") for c in tf_closed]
    rsi_series = rsi_values(closes, period)
    if len(rsi_series) < ma_period or not output:
        return output, {"available": False}
    rsi = rsi_series[-1]
    rsi_ma = sum(rsi_series[-ma_period:]) / ma_period
    gap = rsi - rsi_ma
    gap_strength = max(min((abs(gap) - 5.0) / 10.0, 1.0), 0.0)
    correction_sign = 1.0 if gap < 0 else -1.0 if gap > 0 else 0.0

    atr_1h = atr(h1_closed)
    recent = h1_closed[-8:]
    persistence_sign = -correction_sign
    momentum = 0.0
    if gap_strength and len(recent) >= 4 and atr_1h > 0:
        cumulative = persistence_sign * (value(recent[-1], "c") - value(recent[0], "o"))
        cumulative_score = max(min(cumulative / (2.0 * atr_1h), 1.0), 0.0)
        bodies = [value(c, "c") - value(c, "o") for c in recent]
        body_total = sum(abs(b) for b in bodies) or 1.0
        body_score = max(min(sum(max(persistence_sign * b, 0.0) for b in bodies) / body_total, 1.0), 0.0)
        locations = []
        for candle in recent[-4:]:
            high, low = value(candle, "h"), value(candle, "l")
            location = (value(candle, "c") - low) / max(high - low, 1e-9)
            locations.append(location if persistence_sign > 0 else 1.0 - location)
        close_score = sum(locations) / len(locations)
        h1_closes = [value(c, "c") for c in h1_closed]
        def ema(values, length):
            alpha = 2.0 / (length + 1.0)
            result = float(values[0])
            for item in values[1:]:
                result = alpha * float(item) + (1.0 - alpha) * result
            return result
        ema_now = ema(h1_closes[-60:], 20)
        previous_closes = h1_closes[-66:-6] if len(h1_closes) >= 66 else h1_closes[:-6]
        ema_prev = ema(previous_closes, 20) if previous_closes else ema_now
        slope_score = max(min(persistence_sign * (ema_now - ema_prev) / max(atr_1h, 1e-9), 1.0), 0.0)
        momentum = max(min(0.35 * cumulative_score + 0.25 * body_score + 0.20 * close_score + 0.20 * slope_score, 1.0), 0.0)

    effective = gap_strength * (1.0 - 0.90 * momentum)
    tf_atr = atr(tf_closed)
    affected = {"1h": 6, "4h": 3, "1d": 1}.get(timeframe, 3)
    max_wick = 0.20 * tf_atr
    for index in range(min(affected, len(output))):
        decay = (affected - index) / affected
        adjustment = max_wick * effective * decay
        if correction_sign > 0:
            # RSI below its average: express upward correction probability as
            # an upper rejection wick without changing the bearish body/path.
            output[index]["high"] = max(float(output[index]["high"]), max(float(output[index]["open"]), float(output[index]["close"])) + adjustment)
        elif correction_sign < 0:
            # RSI above its average: express downward correction probability
            # as a lower rejection wick without changing the bullish body/path.
            output[index]["low"] = min(float(output[index]["low"]), min(float(output[index]["open"]), float(output[index]["close"])) - adjustment)
    return output, {
        "available": True, "rsi": round(rsi, 2), "rsi_ma": round(rsi_ma, 2),
        "gap": round(gap, 2), "gap_strength": round(gap_strength, 3),
        "momentum_1h": round(momentum * 100.0, 1),
        "effective_geometry_weight": round(effective, 3), "affected_candles": affected,
    }


class _AggTrade:
    """SQL-aggregated trade row — duck-typed for portfolio_delta/gamma."""
    __slots__ = ("strike", "option_type", "direction", "amount", "iv", "_expiration")

    def __init__(self, strike, option_type, direction, expiration, amount, iv):
        self.strike = strike
        self.option_type = option_type
        self.direction = direction
        self.amount = amount
        self.iv = iv
        self._expiration = expiration

    def get_hours_to_expiry(self):
        if not self._expiration:
            return 0.0
        now = datetime.now(timezone.utc)
        exp = self._expiration if self._expiration.tzinfo else self._expiration.replace(tzinfo=timezone.utc)
        return max((exp - now).total_seconds() / 3600.0, 0.0)


class ChartController(http.Controller):
    @http.route("/help", auth="user", type="http", website=True)
    def help_page(self):
        return request.render("dankbit.dankbit_help")

    @http.route("/<string:instrument>/s", type="http", auth="user", website=True)
    def chart_slideshow(self, instrument):
        return request.render("dankbit.dankbit_slideshow", {
            "instrument": instrument,
            "hours_list": [0, 4, 8, 12, 24],
        })

    @http.route("/<string:instrument>/<int:hours>", type="http", auth="user", website=True)
    def chart_png_hours(self, instrument, hours):
        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        domain = [
            ("name", "ilike", f"{instrument}"),
            ("expiration", ">=", datetime.now(timezone.utc).replace(tzinfo=None)),
            ("deribit_ts", ">=", cutoff),
            ("iv", "!=", 0),
        ]

        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)

        for trade in trades:
            if trade.option_type == "call":
                if trade.direction == "buy":
                    obj.long_call(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    obj.short_call(trade.strike, trade.price * trade.index_price)
            elif trade.option_type == "put":
                if trade.direction == "buy":
                    obj.long_put(trade.strike, trade.price * trade.index_price)
                elif trade.direction == "sell":
                    obj.short_put(trade.strike, trade.price * trade.index_price)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)
        fig, ax = obj.plot(index_price,
                           market_deltas,
                           market_gammas,
                           False,
                           title=f"{hours}H",
                           width=18,
                           height=8)

        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        d_arr = np.asarray(market_deltas, dtype=float)
        g_arr = np.asarray(market_gammas, dtype=float)
        d_lim = float(np.max(np.abs(d_arr[np.isfinite(d_arr)]))) if np.any(np.isfinite(d_arr)) else 1.0
        g_lim = float(np.max(np.abs(g_arr[np.isfinite(g_arr)]))) if np.any(np.isfinite(g_arr)) else 1.0

        for px, gval in self.find_gamma_peaks(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            # normalised positions of gamma and delta at this x (0=bottom, 1=top of axes)
            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            # pick the y fraction furthest from both curves
            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for px, gval in self.find_gamma_bottoms(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            # normalised positions of gamma and delta at this x (0=bottom, 1=top of axes)
            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            # pick the y fraction furthest from both curves
            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                demand = d_arr[i] > 0
                color = "red" if demand else "green"
                ax.axvline(x=px, color=color, linewidth=1.2, linestyle="-", alpha=0.8)
                g_norm = 0.5 + 0.5 * (float(np.interp(px, STs, g_arr)) / g_lim) if g_lim else 0.5
                y = 0.04 if g_norm > 0.5 else 0.96
                ax.text(px, y, f"${px:,.0f}", transform=trans, color=color,
                        fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                        rotation=90)

        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        last_ts = last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M') if last_trade else "—"
        ax.text(
            0.01, 0.04,
            f"{len(trades)} Trades ({hours}h)",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.text(
            0.01, 0.01,
            f"Last trade: {last_ts}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": f"{hours}h",
                "plot_title": f"{instrument} - Last {hours}h",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
            }
        )

    @http.route("/<string:instrument>/zones", type="http", auth="user", website=True)
    def chart_png_zones(self, instrument):
        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        # The most recent UTC midnight (see options.day_window_start).
        midnight_utc = options.day_window_start(
            datetime.now(timezone.utc).replace(tzinfo=None)
        ).strftime("%Y-%m-%d %H:%M:%S")
        domain = [
            ("name", "=ilike", f"{instrument}-%"),
            ("expiration", ">=", datetime.now(timezone.utc).replace(tzinfo=None)),
            ("deribit_ts", ">=", midnight_utc),
            ("iv", "!=", 0),
        ]
        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)

        long_count = len(trades.filtered(lambda t: t.direction == "buy"))
        short_count = len(trades.filtered(lambda t: t.direction == "sell"))
        longs_obj, shorts_obj = options.build_zone_curves(
            instrument, index_price, trades, from_price, to_price, steps
        )

        fig, ax = longs_obj.plot_zones(
            longs_obj.payoffs, shorts_obj.payoffs, index_price, title="Zones", width=3.5
        )

        ax.text(
            0.01, 0.02,
            f"{long_count} longs\n{short_count} shorts\n(since 00:00 UTC)",
            transform=ax.transAxes,
            fontsize=14,
            va="bottom",
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")

        # Seller Max Profit/Buyer Max Loss/zone info used to be drawn inside
        # the PNG itself (matplotlib ax.text) — now rendered as page HTML
        # (top-left overlay, see dankbit_page template) instead, off the
        # same summary dankbit.bands uses, so the two can never
        # disagree.
        summary = options.zone_summary(longs_obj.STs, longs_obj.payoffs, shorts_obj.payoffs)

        def _format_zone(zone):
            # zone is None (no crossing at all), or a (low, high) pair that
            # collapses to a single price when only one curve contributed a
            # crossing — shown as one number rather than a zero-width range.
            if zone is None:
                return "n/a"
            low, high = zone
            if low == high:
                return "${:,.0f}".format(low)
            return "${:,.0f} - ${:,.0f}".format(low, high)

        high_zone = _format_zone(summary["high_zone"])
        low_zone = _format_zone(summary["low_zone"])
        middle_zone = _format_zone(summary["middle_zone"])
        high_resistance = "n/a" if summary["high_resistance"] is None else "${:,.0f}".format(summary["high_resistance"])
        low_support = "n/a" if summary["low_support"] is None else "${:,.0f}".format(summary["low_support"])

        # Restrict to the single nearest (soonest-to-expire) expiry among
        # `trades` — same "next expiry only" restriction dankbit.bands
        # uses, in case `instrument` isn't already a single fully-qualified
        # expiry — then delegate the actual per-leg gamma/delta/theta/vega
        # extrema computation to options.per_leg_greeks(), the single source
        # of truth also used by dankbit.bands's gamma_band/delta_band
        # and forecast.per_leg_greeks(), so this page can never disagree
        # with either on these numbers. r=0.0 throughout (per_leg_greeks'
        # own default) to match every other Greek computed on this page —
        # zones deliberately doesn't use the r=0.05 the combined-portfolio
        # routes use.
        next_expiration = min(trades.mapped("expiration")) if trades else None
        next_expiration_trades = trades.filtered(lambda t: t.expiration == next_expiration)
        legs = options.per_leg_greeks(longs_obj.STs, next_expiration_trades)
        lc, lp, sc, sp = legs["long_call"], legs["long_put"], legs["short_call"], legs["short_put"]

        bcg_price, bcg_value = lc["gamma_price"], lc["gamma_value"]
        bpg_price, bpg_value = lp["gamma_price"], lp["gamma_value"]
        scg_price, scg_value = sc["gamma_price"], sc["gamma_value"]
        spg_price, spg_value = sp["gamma_price"], sp["gamma_value"]

        bcd_price, bcd_value = lc["delta_price"], lc["delta_value"]
        bpd_price, bpd_value = lp["delta_price"], lp["delta_value"]
        scd_price, scd_value = sc["delta_price"], sc["delta_value"]
        spd_price, spd_value = sp["delta_price"], sp["delta_value"]

        bct_price, bct_value = lc["theta_price"], lc["theta_value"]
        bpt_price, bpt_value = lp["theta_price"], lp["theta_value"]
        sct_price, sct_value = sc["theta_price"], sc["theta_value"]
        spt_price, spt_value = sp["theta_price"], sp["theta_value"]

        bcv_price, bcv_value = lc["vega_price"], lc["vega_value"]
        bpv_price, bpv_value = lp["vega_price"], lp["vega_value"]
        scv_price, scv_value = sc["vega_price"], sc["vega_value"]
        spv_price, spv_value = sp["vega_price"], sp["vega_value"]

        # Each line is {text, color} — color is None for the default
        # (black) styling every line used before per-line colors were
        # needed; only section headers like "Gamma" below set one.
        def _line(text, color=None):
            return {"text": text, "color": color}

        # A leg with zero trades in this window has no gamma/delta/theta/
        # vega curve to peak/bottom at — options.per_leg_greeks() reports
        # that as price=None rather than a fake price-grid-edge value (see
        # its own docstring). "n/a", same as High/Resistance's/
        # Low/Support's own None-handling just above.
        def _price(p):
            return "n/a" if p is None else "${:,.0f}".format(p)

        zone_info_lines = [
            _line("Seller Max Profit (SMP): ${:,.0f}".format(summary["seller_max_profit"])),
            _line("Buyer Max Loss (BML): ${:,.0f}".format(summary["buyer_max_loss"])),
            _line(" "),  # blank spacer line — a truly empty div collapses to zero height
            _line(f"High Zone: {high_zone}"),
            _line(f"Low Zone: {low_zone}"),
            _line(f"Middle Zone: {middle_zone}"),
            _line(" "),
            _line(f"High/Resistance: {high_resistance}"),
            _line(f"Low/Support: {low_support}"),
            _line(" "),
            _line("Gamma", color="violet"),
            _line(f"Buyer Call Gamma (BCG): {_price(bcg_price)}"),
            _line(f"Buyer Put Gamma (BPG): {_price(bpg_price)}"),
            _line(f"Seller Call Gamma (SCG): {_price(scg_price)}"),
            _line(f"Seller Put Gamma (SPG): {_price(spg_price)}"),
            _line(" "),
            _line("BCG Abs.: {:,.0f}".format(abs(bcg_value) / 1_000_000)),
            _line("BPG Abs.: {:,.0f}".format(abs(bpg_value) / 1_000_000)),
            _line("SCG Abs.: {:,.0f}".format(abs(scg_value) / 1_000_000)),
            _line("SPG Abs.: {:,.0f}".format(abs(spg_value) / 1_000_000)),
            _line(" "),
            _line("Delta", color="green"),
            _line(f"Buyer Call Delta (BCD): {_price(bcd_price)}"),
            _line(f"Buyer Put Delta (BPD): {_price(bpd_price)}"),
            _line(f"Seller Call Delta (SCD): {_price(scd_price)}"),
            _line(f"Seller Put Delta (SPD): {_price(spd_price)}"),
            _line(" "),
            _line("BCD Abs.: {:,.0f}".format(abs(bcd_value) / 10)),
            _line("BPD Abs.: {:,.0f}".format(abs(bpd_value) / 10)),
            _line("SCD Abs.: {:,.0f}".format(abs(scd_value) / 10)),
            _line("SPD Abs.: {:,.0f}".format(abs(spd_value) / 10)),
        ]

        # Theta and Vega get their own top-right overlay (see
        # .zone-info-right in dankbit_page) rather than sitting in the
        # top-left zone_info_lines column with everything else.
        right_info_lines = [
            _line("Theta", color="orange"),
            _line(f"Buyer Call Theta (BCT): {_price(bct_price)}"),
            _line(f"Buyer Put Theta (BPT): {_price(bpt_price)}"),
            _line(f"Seller Call Theta (SCT): {_price(sct_price)}"),
            _line(f"Seller Put Theta (SPT): {_price(spt_price)}"),
            _line(" "),
            _line("BCT Abs.: {:,.0f}".format(abs(bct_value) / 10_000)),
            _line("BPT Abs.: {:,.0f}".format(abs(bpt_value) / 10_000)),
            _line("SCT Abs.: {:,.0f}".format(abs(sct_value) / 10_000)),
            _line("SPT Abs.: {:,.0f}".format(abs(spt_value) / 10_000)),
            _line(" "),
            _line("Vega", color="blue"),
            _line(f"Buyer Call Vega (BCV): {_price(bcv_price)}"),
            _line(f"Buyer Put Vega (BPV): {_price(bpv_price)}"),
            _line(f"Seller Call Vega (SCV): {_price(scv_price)}"),
            _line(f"Seller Put Vega (SPV): {_price(spv_price)}"),
            _line(" "),
            # Raw portfolio-vega values sit in the low thousands here — an
            # order of magnitude below theta's ten-thousands and well above
            # delta's tens — so /100 keeps these in the same easy-to-copy
            # 2-3 digit range as the other Value lines (see gamma's /1e6,
            # theta's /1e4, delta's /10 above).
            _line("BCV Abs.: {:,.0f}".format(abs(bcv_value) / 100)),
            _line("BPV Abs.: {:,.0f}".format(abs(bpv_value) / 100)),
            _line("SCV Abs.: {:,.0f}".format(abs(scv_value) / 100)),
            _line("SPV Abs.: {:,.0f}".format(abs(spv_value) / 100)),
        ]

        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": "Zones",
                "plot_title": f"{instrument} - Zones",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
                "zone_info_lines": zone_info_lines,
                "theta_info_lines": right_info_lines,
            }
        )

    # instrument, trades since 00:00 UTC, single-leg delta/gamma routes
    # (/lp, /lc, /sp, /sc) — maps each route's short key to which trades to
    # keep (direction/option_type), which OptionStrat leg method accumulates
    # them, and the delta-saturation fraction/side (see
    # options.delta_saturation_price, shared with dankbit.bands's
    # own delta_band) that locates the green marker line: calls saturate ITM
    # at high S, puts at low S, independent of long/short — each leg's own
    # sign is inherited automatically from its curve's value at that edge.
    DELTA_SATURATION_FRACTION = options.DELTA_SATURATION_FRACTION
    _LEG_ROUTES = {
        "lp": {"direction": "buy", "option_type": "put", "method": "long_put", "label": "Long Puts",
               "saturation_fraction": DELTA_SATURATION_FRACTION, "saturation_side": "min"},
        "lc": {"direction": "buy", "option_type": "call", "method": "long_call", "label": "Long Calls",
               "saturation_fraction": DELTA_SATURATION_FRACTION, "saturation_side": "max"},
        "sp": {"direction": "sell", "option_type": "put", "method": "short_put", "label": "Short Puts",
               "saturation_fraction": DELTA_SATURATION_FRACTION, "saturation_side": "min"},
        "sc": {"direction": "sell", "option_type": "call", "method": "short_call", "label": "Short Calls",
               "saturation_fraction": DELTA_SATURATION_FRACTION, "saturation_side": "max"},
    }

    def _annotate_gamma_delta_crossings(self, ax, STs, market_deltas, market_gammas):
        """Gamma peak/bottom markers (dashed black) and delta=0 crossings
        (solid — green for "supply", red for "demand") — same overlay drawn
        inline by chart_png_hours/chart_png_all, factored out here so the 4
        single-leg routes below don't each carry their own copy."""
        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        d_arr = np.asarray(market_deltas, dtype=float)
        g_arr = np.asarray(market_gammas, dtype=float)
        d_lim = float(np.max(np.abs(d_arr[np.isfinite(d_arr)]))) if np.any(np.isfinite(d_arr)) else 1.0
        g_lim = float(np.max(np.abs(g_arr[np.isfinite(g_arr)]))) if np.any(np.isfinite(g_arr)) else 1.0

        for px, gval in self.find_gamma_peaks(STs, market_gammas) + self.find_gamma_bottoms(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                demand = d_arr[i] > 0
                color = "red" if demand else "green"
                ax.axvline(x=px, color=color, linewidth=1.2, linestyle="-", alpha=0.8)
                g_norm = 0.5 + 0.5 * (float(np.interp(px, STs, g_arr)) / g_lim) if g_lim else 0.5
                y = 0.04 if g_norm > 0.5 else 0.96
                ax.text(px, y, f"${px:,.0f}", transform=trans, color=color,
                        fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                        rotation=90)

    def _chart_png_single_leg(self, instrument, leg_key):
        cfg = self._LEG_ROUTES[leg_key]
        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        # Anchored/left-prefix match (not a bare ilike substring) and trades
        # since the most recent UTC midnight (options.day_window_start) —
        # same domain convention as chart_png_zones, so a query for one
        # expiry can't pull in another instrument's trades.
        midnight_utc = options.day_window_start(
            datetime.now(timezone.utc).replace(tzinfo=None)
        ).strftime("%Y-%m-%d %H:%M:%S")
        domain = [
            ("name", "=ilike", f"{instrument}-%"),
            ("expiration", ">=", datetime.now(timezone.utc).replace(tzinfo=None)),
            ("deribit_ts", ">=", midnight_utc),
            ("direction", "=", cfg["direction"]),
            ("option_type", "=", cfg["option_type"]),
            ("iv", "!=", 0),
        ]
        trades = request.env["dankbit.trade"].search(domain=domain)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)
        leg_method = getattr(obj, cfg["method"])
        for trade in trades:
            leg_method(trade.strike, trade.price * trade.index_price)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, trades, 0.05)
        fig, ax = obj.plot(index_price,
                           market_deltas,
                           market_gammas,
                           False,
                           title=cfg["label"],
                           width=18,
                           height=8)

        self._annotate_gamma_delta_crossings(ax, STs, market_deltas, market_gammas)

        # Delta-saturation marker: the price where this leg's delta curve
        # reaches 90% of its own extreme value in this window and flattens
        # into a straight line (deep enough ITM to "trade like synthetic
        # stock") — same options.delta_saturation_price() dankbit.zones.
        # extrema's own delta_band uses, so the two can never disagree on
        # this point. Relative to the curve's own extreme, not an absolute
        # delta value, since portfolio_delta's scale depends on how much
        # volume traded (can be in the hundreds), not a fixed [-1, 1] range.
        saturation_price = options.delta_saturation_price(
            STs, trades, cfg["saturation_fraction"], cfg["saturation_side"]
        )
        ax.axvline(x=saturation_price, color="green", linewidth=1.5, linestyle="-", alpha=0.9)
        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        ax.text(saturation_price, 0.96, f"${saturation_price:,.0f}", transform=trans, color="green",
                fontsize=9, ha="right", va="top", rotation=90)

        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        last_ts = last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M') if last_trade else "—"
        ax.text(
            0.01, 0.04,
            f"{len(trades)} Trades (since 00:00 UTC)",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.text(
            0.01, 0.01,
            f"Last trade: {last_ts}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": leg_key.upper(),
                "plot_title": f"{instrument} - {cfg['label']}",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
            }
        )

    @http.route("/<string:instrument>/lp", type="http", auth="user", website=True)
    def chart_png_long_puts(self, instrument):
        return self._chart_png_single_leg(instrument, "lp")

    @http.route("/<string:instrument>/lc", type="http", auth="user", website=True)
    def chart_png_long_calls(self, instrument):
        return self._chart_png_single_leg(instrument, "lc")

    @http.route("/<string:instrument>/sp", type="http", auth="user", website=True)
    def chart_png_short_puts(self, instrument):
        return self._chart_png_single_leg(instrument, "sp")

    @http.route("/<string:instrument>/sc", type="http", auth="user", website=True)
    def chart_png_short_calls(self, instrument):
        return self._chart_png_single_leg(instrument, "sc")

    @http.route("/<string:instrument>", type="http", auth="user", website=True)
    def chart_png_all(self, instrument):
        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        cr = request.env.cr
        cr.execute("""
            SELECT
                strike,
                option_type,
                direction,
                expiration,
                SUM(amount)                                AS total_amount,
                SUM(iv * amount) / NULLIF(SUM(amount), 0) AS weighted_iv,
                COUNT(*)                                   AS trade_count
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= NOW()
              AND active = TRUE
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{instrument}%',))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0],
                option_type=row[1],
                direction=row[2],
                expiration=row[3],
                amount=float(row[4]),
                iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, agg_trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, agg_trades, 0.05)
        fig, ax = obj.plot(index_price,
                           market_deltas,
                           market_gammas,
                           False,
                           title="Structure",
                           width=18,
                           height=8)

        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        d_arr = np.asarray(market_deltas, dtype=float)
        g_arr = np.asarray(market_gammas, dtype=float)
        d_lim = float(np.max(np.abs(d_arr[np.isfinite(d_arr)]))) if np.any(np.isfinite(d_arr)) else 1.0
        g_lim = float(np.max(np.abs(g_arr[np.isfinite(g_arr)]))) if np.any(np.isfinite(g_arr)) else 1.0

        for px, gval in self.find_gamma_peaks(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            # normalised positions of gamma and delta at this x (0=bottom, 1=top of axes)
            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            # pick the y fraction furthest from both curves
            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for px, gval in self.find_gamma_bottoms(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            # normalised positions of gamma and delta at this x (0=bottom, 1=top of axes)
            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            # pick the y fraction furthest from both curves
            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                demand = d_arr[i] > 0
                color = "red" if demand else "green"
                ax.axvline(x=px, color=color, linewidth=1.2, linestyle="-", alpha=0.8)
                g_norm = 0.5 + 0.5 * (float(np.interp(px, STs, g_arr)) / g_lim) if g_lim else 0.5
                y = 0.04 if g_norm > 0.5 else 0.96
                ax.text(px, y, f"${px:,.0f}", transform=trans, color=color,
                        fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                        rotation=90)

        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        last_ts = last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M') if last_trade else "—"
        ax.text(
            0.01, 0.04,
            f"{trade_count} Trades",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.text(
            0.01, 0.01,
            f"Last trade: {last_ts}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": "All",
                "plot_title": f"{instrument} - All",
                "refresh_interval": refresh_interval*5,
                "image_b64": image_b64,
            }
        )

    @http.route("/<string:asset>/weekly", type="http", auth="user", website=True)
    def chart_png_weekly(self, asset):
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()
        icp = request.env["ir.config_parameter"].sudo()
        param = "dankbit.eth_weekly_expiry" if asset.startswith("ETH") else "dankbit.weekly_expiry"
        instrument = icp.get_param(param, default="").upper()
        if not instrument:
            return request.make_response(
                f"Weekly Expiry for {asset} is not configured. Set it in Settings → Dankbit.",
                headers=[("Content-Type", "text/plain")],
            )
        return self.chart_png_until(instrument)

    @http.route("/<string:asset>/monthly", type="http", auth="user", website=True)
    def chart_png_monthly(self, asset):
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()
        icp = request.env["ir.config_parameter"].sudo()
        param = "dankbit.eth_monthly_expiry" if asset.startswith("ETH") else "dankbit.monthly_expiry"
        instrument = icp.get_param(param, default="").upper()
        if not instrument:
            return request.make_response(
                f"Monthly Expiry for {asset} is not configured. Set it in Settings → Dankbit.",
                headers=[("Content-Type", "text/plain")],
            )
        return self.chart_png_until(instrument)

    @http.route("/i/<string:instrument>", type="http", auth="user", website=True)
    def chart_png_until(self, instrument):
        # instrument is e.g. "BTC-3JUL26" — asset prefix + expiry, no strike/type
        parts = instrument.split("-", 1)
        if len(parts) != 2:
            return request.not_found()
        asset = parts[0].upper()
        expiry_str = parts[1].upper()

        try:
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(
                hour=8, tzinfo=timezone.utc
            )
        except ValueError:
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if asset.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if asset.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        cr = request.env.cr
        cr.execute("""
            SELECT
                strike,
                option_type,
                direction,
                expiration,
                SUM(amount)                                AS total_amount,
                SUM(iv * amount) / NULLIF(SUM(amount), 0) AS weighted_iv,
                COUNT(*)                                   AS trade_count
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= NOW()
              AND expiration <= %s
              AND active = TRUE
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{asset}%', expiry_dt))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0],
                option_type=row[1],
                direction=row[2],
                expiration=row[3],
                amount=float(row[4]),
                iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        index_price = request.env["dankbit.trade"].get_index_price(asset)
        obj = options.OptionStrat(asset, index_price, from_price, to_price, steps)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, agg_trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, agg_trades, 0.05)
        fig, ax = obj.plot(index_price,
                           market_deltas,
                           market_gammas,
                           False,
                           title=f"Until {expiry_str}",
                           width=18,
                           height=8)

        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        d_arr = np.asarray(market_deltas, dtype=float)
        g_arr = np.asarray(market_gammas, dtype=float)
        d_lim = float(np.max(np.abs(d_arr[np.isfinite(d_arr)]))) if np.any(np.isfinite(d_arr)) else 1.0
        g_lim = float(np.max(np.abs(g_arr[np.isfinite(g_arr)]))) if np.any(np.isfinite(g_arr)) else 1.0

        for px, gval in self.find_gamma_peaks(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for px, gval in self.find_gamma_bottoms(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)


        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                demand = d_arr[i] > 0
                color = "red" if demand else "green"
                ax.axvline(x=px, color=color, linewidth=1.2, linestyle="-", alpha=0.8)
                g_norm = 0.5 + 0.5 * (float(np.interp(px, STs, g_arr)) / g_lim) if g_lim else 0.5
                y = 0.04 if g_norm > 0.5 else 0.96
                ax.text(px, y, f"${px:,.0f}", transform=trans, color=color,
                        fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                        rotation=90)

        last_trade = request.env["dankbit.trade"].get_last_trade(asset)
        last_ts = last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M') if last_trade else "—"
        ax.text(
            0.01, 0.04,
            f"{trade_count} Trades (until {expiry_str})",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.text(
            0.01, 0.01,
            f"Last trade: {last_ts}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": f"Until {expiry_str}",
                "plot_title": f"{asset} - Until {expiry_str}",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
            }
        )
    
    def find_gamma_peaks(self, STs, gamma_curve, min_fraction=0.15):
        STs = np.asarray(STs, dtype=float)
        g = np.asarray(gamma_curve, dtype=float)

        if g.size < 3:
            return []

        finite = np.isfinite(g)
        if not np.any(finite):
            return []

        g_max = np.max(np.abs(g[finite]))
        if g_max == 0:
            return []

        threshold = min_fraction * g_max
        extrema = []

        for i in range(1, len(g) - 1):
            if not np.isfinite(g[i]):
                continue
            if g[i] > g[i - 1] and g[i] > g[i + 1] and g[i] > threshold:
                extrema.append((float(STs[i]), float(g[i])))

        return extrema

    def find_gamma_bottoms(self, STs, gamma_curve, min_fraction=0.15):
        STs = np.asarray(STs, dtype=float)
        g = np.asarray(gamma_curve, dtype=float)

        if g.size < 3:
            return []

        finite = np.isfinite(g)
        if not np.any(finite):
            return []

        g_max = np.max(np.abs(g[finite]))
        if g_max == 0:
            return []

        threshold = min_fraction * g_max
        extrema = []

        for i in range(1, len(g) - 1):
            if not np.isfinite(g[i]):
                continue
            if g[i] < g[i - 1] and g[i] < g[i + 1] and g[i] < -threshold:
                extrema.append((float(STs[i]), float(g[i])))

        return extrema

    # ------------------------------------------------------------------
    # JSON API endpoints
    # ------------------------------------------------------------------

    @http.route("/api/delta-zero/<string:instrument>", type="http", auth="user", website=False, csrf=False)
    def delta_zero_json(self, instrument):
        parts = instrument.upper().split("-", 1)
        if len(parts) != 2:
            return request.make_response(
                json.dumps({"error": "Invalid instrument — expected ASSET-EXPIRY e.g. BTC-3JUL26"}),
                headers=[("Content-Type", "application/json")],
            )

        asset, expiry_str = parts
        try:
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
        except ValueError:
            return request.make_response(
                json.dumps({"error": "Invalid expiry format — expected DDMMMYY e.g. 3JUL26"}),
                headers=[("Content-Type", "application/json")],
            )

        icp = request.env["ir.config_parameter"].sudo()
        if asset.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        elif asset.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))
        else:
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        cr = request.env.cr
        cr.execute("""
            SELECT strike, option_type, direction, expiration,
                   SUM(amount), SUM(iv * amount) / NULLIF(SUM(amount), 0), COUNT(*)
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= NOW()
              AND expiration <= %s
              AND active = TRUE
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{asset}%', expiry_dt))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0], option_type=row[1], direction=row[2],
                expiration=row[3], amount=float(row[4]), iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        STs = np.arange(from_price, to_price, steps)
        d_arr = np.asarray(delta.portfolio_delta(STs, agg_trades, 0.05), dtype=float)

        crossings = []
        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                crossings.append({
                    "price": px,
                    "type": "demand" if d_arr[i] > 0 else "supply",
                })

        index_price = request.env["dankbit.trade"].get_index_price(asset)
        payload = {
            "asset": asset,
            "expiry": expiry_str,
            "delta_zero": crossings,
            "index_price": index_price,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/delta-zero-all/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def delta_zero_all_json(self, asset):
        asset = asset.upper()
        icp = request.env["ir.config_parameter"].sudo()
        if asset.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        elif asset.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))
        else:
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        cr = request.env.cr
        cr.execute("""
            SELECT strike, option_type, direction, expiration,
                   SUM(amount), SUM(iv * amount) / NULLIF(SUM(amount), 0), COUNT(*)
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= NOW()
              AND active = TRUE
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{asset}%',))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0], option_type=row[1], direction=row[2],
                expiration=row[3], amount=float(row[4]), iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        STs = np.arange(from_price, to_price, steps)
        d_arr = np.asarray(delta.portfolio_delta(STs, agg_trades, 0.05), dtype=float)

        crossings = []
        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                crossings.append({
                    "price": px,
                    "type": "demand" if d_arr[i] > 0 else "supply",
                })

        index_price = request.env["dankbit.trade"].get_index_price(asset)
        payload = {
            "asset": asset,
            "delta_zero": crossings,
            "index_price": index_price,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    def _delta_zero_for_calendar_day(self, asset, days_ahead):
        """Delta=0 crossings for the specific expiry landing `days_ahead`
        calendar days from now (UTC), restricted to trades from the trailing
        24h. Shared by /api/delta-zero-tomorrow (days_ahead=1) and
        /api/delta-zero-day-after-tomorrow (days_ahead=2) so the two can
        never disagree on how a calendar-day expiry/trade-window is
        computed."""
        asset = asset.upper()
        icp = request.env["ir.config_parameter"].sudo()
        if asset.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        elif asset.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))
        else:
            return {"error": "Unknown asset"}

        target_day = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).date()
        expiry_str = f"{target_day.day}{target_day.strftime('%b').upper()}{target_day.strftime('%y')}"

        cr = request.env.cr
        cr.execute("""
            SELECT strike, option_type, direction, expiration,
                   SUM(amount), SUM(iv * amount) / NULLIF(SUM(amount), 0), COUNT(*)
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND active = TRUE
              AND deribit_ts >= NOW() - INTERVAL '24 hours'
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'{asset}-{expiry_str}-%',))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0], option_type=row[1], direction=row[2],
                expiration=row[3], amount=float(row[4]), iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        STs = np.arange(from_price, to_price, steps)
        d_arr = np.asarray(delta.portfolio_delta(STs, agg_trades, 0.05), dtype=float)

        crossings = []
        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                crossings.append({
                    "price": px,
                    "type": "demand" if d_arr[i] > 0 else "supply",
                })

        index_price = request.env["dankbit.trade"].get_index_price(asset)
        return {
            "asset": asset,
            "expiry": expiry_str,
            "delta_zero": crossings,
            "index_price": index_price,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    @http.route("/api/delta-zero-tomorrow/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def delta_zero_tomorrow_json(self, asset):
        payload = self._delta_zero_for_calendar_day(asset, 1)
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/delta-zero-day-after-tomorrow/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def delta_zero_day_after_tomorrow_json(self, asset):
        payload = self._delta_zero_for_calendar_day(asset, 2)
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/bands/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def bands_json(self, asset):
        """One point per instrument stored in dankbit.bands (each
        instrument has exactly one, continuously-refined-then-frozen row —
        see that model's _persist_extrema()), positioned on the chart at that
        instrument's own expiration time rather than a stored poll
        timestamp (there isn't one anymore). The expiration lookup is a
        single grouped query against dankbit_trade rather than parsing each
        instrument's day-string suffix and assuming a settlement hour —
        consistent with how the zones-box endpoints derive their own
        right-edge time. Raw SQL bypasses the ORM's implicit active=True
        filter, so past-expiry (and thus archived) instruments' trades are
        still found — their rows must keep contributing a fixed historical
        point on the chart even after they're no longer "active"."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        hours_param = request.httprequest.args.get("hours")
        try:
            hours = int(hours_param) if hours_param else None
        except (TypeError, ValueError):
            hours = None
        if hours is not None and not 1 <= hours <= 168:
            hours = None

        cr = request.env.cr
        cr.execute("""
            SELECT instrument, index_price, high_resistance, low_support,
                   high_resistance_positive, low_support_positive, gamma_band, delta_band,
                   smart_liq_upper_price, smart_liq_lower_price,
                   smart_liq_upper_strength, smart_liq_lower_strength
            FROM dankbit_bands
            WHERE asset = %s
        """, (asset,))
        rows = cr.fetchall()

        expiry_by_instrument = {}
        if rows:
            instruments = [row[0] for row in rows]
            cr.execute("""
                SELECT SUBSTRING(name FROM '^[^-]+-[^-]+') AS instrument, MIN(expiration) AS expiration
                FROM dankbit_trade
                WHERE SUBSTRING(name FROM '^[^-]+-[^-]+') = ANY(%s)
                GROUP BY instrument
            """, (instruments,))
            expiry_by_instrument = dict(cr.fetchall())

        series = []
        for (
            instrument, index_price, high_resistance, low_support,
            high_resistance_positive, low_support_positive, gamma_band, delta_band,
            smart_liq_upper_price, smart_liq_lower_price,
            smart_liq_upper_strength, smart_liq_lower_strength,
        ) in rows:
            expiration = expiry_by_instrument.get(instrument)
            if not expiration:
                # No trades found at all for this instrument any more —
                # nothing to anchor the point's time to.
                continue
            ts = expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)
            series.append({
                "instrument": instrument,
                "t": int(ts.timestamp() * 1000),
                "confirmation_status": "confirmed",
                "expiry_index": None,
                "index_price": float(index_price or 0.0),
                "high_resistance": float(high_resistance or 0.0),
                "low_support": float(low_support or 0.0),
                # Whether the payoff at that intersection sits above/below
                # the zero line — drives the +/- marker on the chart, not
                # the point's own price/position.
                "high_resistance_positive": bool(high_resistance_positive),
                "low_support_positive": bool(low_support_positive),
                "gamma_band": float(gamma_band or 0.0),
                "delta_band": float(delta_band or 0.0),
                "smart_liq_upper_price": float(smart_liq_upper_price or 0.0),
                "smart_liq_lower_price": float(smart_liq_lower_price or 0.0),
                "smart_liq_upper_strength": float(smart_liq_upper_strength or 0.0),
                "smart_liq_lower_strength": float(smart_liq_lower_strength or 0.0),
            })

        # Keep all three active expiry anchors visible.  A newly rolled E2/E3
        # can exist in option trades before its structural Bands row passes
        # the mature-session confirmation gate.  Previously that missing row
        # vanished from the response, making the Green/Red paths stop one or
        # two days early.  Serve a non-persisted raw calculation for only the
        # missing active instruments and label it provisional.  The chart
        # renders these additions dashed; confirmed history is never replaced.
        bands_model = request.env["dankbit.bands"]
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        expirations = bands_model._distinct_expirations(
            asset, now_naive, bands_model.TRACKED_EXPIRY_COUNT,
            future_days_only=True,
        )
        active_instruments = {
            bands_model._format_instrument(asset, expiration): (expiry_index, expiration)
            for expiry_index, expiration in enumerate(expirations)
        }
        by_instrument = {row["instrument"]: row for row in series}
        for instrument, (expiry_index, expiration) in active_instruments.items():
            confirmed = by_instrument.get(instrument)
            if confirmed is not None:
                confirmed["expiry_index"] = expiry_index
                continue
            live = bands_model._compute_asset(
                asset, expiry_index=expiry_index, future_days_only=True,
            )
            if not live or not live.get("high_resistance") or not live.get("low_support"):
                continue
            exp_ts = expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)
            provisional = {
                "instrument": instrument,
                "t": int(exp_ts.timestamp() * 1000),
                "confirmation_status": "provisional",
                "expiry_index": expiry_index,
                "index_price": float(live.get("index_price") or 0.0),
                "high_resistance": float(live.get("high_resistance") or 0.0),
                "low_support": float(live.get("low_support") or 0.0),
                "high_resistance_positive": bool(live.get("high_resistance_positive")),
                "low_support_positive": bool(live.get("low_support_positive")),
                "gamma_band": float(live.get("gamma_band") or 0.0),
                "delta_band": float(live.get("delta_band") or 0.0),
                "smart_liq_upper_price": float(live.get("smart_liq_upper_price") or 0.0),
                "smart_liq_lower_price": float(live.get("smart_liq_lower_price") or 0.0),
                "smart_liq_upper_strength": float(live.get("smart_liq_upper_strength") or 0.0),
                "smart_liq_lower_strength": float(live.get("smart_liq_lower_strength") or 0.0),
            }
            series.append(provisional)
            by_instrument[instrument] = provisional

        # In trailing-window preview mode the structural Green/Red Bands
        # remain the persisted session-confirmed values, while Gamma and
        # Smart Liquidity are replaced by a fresh computation using only
        # the selected option-flow window.  Nothing is persisted here.
        if hours is not None:
            for expiry_index in range(request.env["dankbit.bands"].TRACKED_EXPIRY_COUNT):
                live = request.env["dankbit.bands"]._compute_asset(
                    asset, expiry_index=expiry_index, hours=hours,
                    future_days_only=True,
                )
                if not live:
                    continue
                expiration = live["expiration"]
                expiration = expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)
                row = by_instrument.get(live["instrument"])
                if row is None:
                    row = {
                        "instrument": live["instrument"],
                        "t": int(expiration.timestamp() * 1000),
                        "confirmation_status": "provisional",
                        "expiry_index": expiry_index,
                        "index_price": float(live["index_price"] or 0.0),
                        "high_resistance": 0.0, "low_support": 0.0,
                        "high_resistance_positive": False, "low_support_positive": False,
                        "delta_band": 0.0,
                    }
                    series.append(row)
                    by_instrument[live["instrument"]] = row
                row.update({
                    "gamma_band": float(live["gamma_band"] or 0.0),
                    "smart_liq_upper_price": float(live["smart_liq_upper_price"] or 0.0),
                    "smart_liq_lower_price": float(live["smart_liq_lower_price"] or 0.0),
                    "smart_liq_upper_strength": float(live["smart_liq_upper_strength"] or 0.0),
                    "smart_liq_lower_strength": float(live["smart_liq_lower_strength"] or 0.0),
                })
        series.sort(key=lambda r: r["t"])

        payload = {
            "asset": asset,
            "bands": series,
            "window_hours": hours,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/zones-box/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def zones_box_json(self, asset):
        """Nearest-expiry zones for the chart.

        The default since-00:00 path serves the last session-confirmed (or
        two-hour emergency-confirmed) High/Low/Middle zones.  `?hours=` is an
        explicit non-persisted live analytical preview of a trailing window.
        Forecast snapshots use their own hourly raw computation and are not
        constrained by this display endpoint.
        """
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        hours_param = request.httprequest.args.get("hours")
        try:
            hours = int(hours_param) if hours_param else None
        except (TypeError, ValueError):
            hours = None
        # Prevent malformed/manual requests from creating an unbounded trade
        # query.  The chart currently sends 2, 4, or the configured window.
        if hours is not None and not 1 <= hours <= 168:
            hours = None

        data = request.env["dankbit.bands"].get_box(asset, hours=hours)
        if not data:
            payload = {"asset": asset, "box": None}
        else:
            computed_at = data["computed_at"].replace(tzinfo=timezone.utc)
            expiration = data["expiration"].replace(tzinfo=timezone.utc)
            payload = {
                "asset": asset,
                "t": int(computed_at.timestamp() * 1000),
                "expiration": int(expiration.timestamp() * 1000),
                "index_price": float(data["index_price"]),
                "short_zero_above_price": float(data["short_zero_above_price"]),
                "long_zero_above_price": float(data["long_zero_above_price"]),
                "short_zero_below_price": float(data["short_zero_below_price"]),
                "long_zero_below_price": float(data["long_zero_below_price"]),
                "seller_max_profit": float(data["seller_max_profit"]),
                "buyer_max_loss": float(data["buyer_max_loss"]),
                "zone_confirmation_mode": data.get("zone_confirmation_mode", "unknown"),
            }
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/nearest-expiry/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def nearest_expiry_json(self, asset):
        """The single nearest active expiry for `asset`, as a full
        instrument string (e.g. "BTC-9JUL26") — the same expiry the yellow
        zones boxes use, but a cheap standalone lookup (no curve-building)
        so the TradingView footer can show it without waiting on the
        boxes' own full computation."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )
        expiry = request.env["dankbit.bands"].nearest_expiry(asset)
        payload = {
            "asset": asset,
            "expiry": expiry,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/last-trade/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def last_trade_json(self, asset):
        """The most recent trade's own deribit_ts for `asset`, asset-wide
        (dankbit.trade.get_last_trade()'s plain `ilike` match, not
        restricted to any one instrument/expiry) — a cheap standalone
        lookup, no curve-building, feeding the TradingView footer's
        "Last trade:" text so a stalled dankbit_ws ingestion service (its
        DB connection can die without crashing the container, silently
        dropping every trade — see dankbit_ws_batch.py) shows up as a
        growing gap between this timestamp and now, at a glance, without
        needing to check the container logs or query the DB directly.
        `last_trade_ts` is `None` if this asset has no trades at all."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )
        last_trade = request.env["dankbit.trade"].get_last_trade(asset)
        last_trade_ts = (
            last_trade.deribit_ts.replace(tzinfo=timezone.utc).isoformat()
            if last_trade and last_trade.deribit_ts else None
        )
        payload = {
            "asset": asset,
            "last_trade_ts": last_trade_ts,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/live-band/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def live_band_json(self, asset):
        """Every persisted dankbit.live.band row for `asset`, oldest-first
        — feeds the Delta Chart's per-hour Smart Liquidity dominance dots
        (see dankbit_templates.xml's "Bands" checkbox). Read-only, like
        every other endpoint reading a cron-fed snapshot model: it never
        calls compute_and_create() itself — only dankbit.live.band's own
        hourly cron ever creates a row."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        cr = request.env.cr
        cr.execute("""
            SELECT computed_at, expiry_index, instrument, expiration,
                   high_resistance, low_support,
                   gamma_band, smart_liq_upper, smart_liq_lower,
                   smart_liq_upper_strength, smart_liq_lower_strength,
                   smart_liq_upper_avg, smart_liq_lower_avg
            FROM dankbit_live_band
            WHERE asset = %s
            ORDER BY computed_at ASC, expiry_index ASC
        """, (asset,))
        points = []
        for (
            computed_at, expiry_index, instrument, expiration,
            high_resistance, low_support,
            gamma_band, smart_liq_upper, smart_liq_lower,
            smart_liq_upper_strength, smart_liq_lower_strength,
            smart_liq_upper_avg, smart_liq_lower_avg,
        ) in cr.fetchall():
            ts = computed_at if computed_at.tzinfo else computed_at.replace(tzinfo=timezone.utc)
            points.append({
                "t": int(ts.timestamp() * 1000),
                "expiry_index": int(expiry_index or 0),
                "instrument": instrument,
                "expiration": int((expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)).timestamp() * 1000) if expiration else None,
                "high_resistance": float(high_resistance or 0.0),
                "low_support": float(low_support or 0.0),
                "gamma_band": float(gamma_band or 0.0),
                "smart_liq_upper": float(smart_liq_upper or 0.0),
                "smart_liq_lower": float(smart_liq_lower or 0.0),
                "smart_liq_upper_strength": float(smart_liq_upper_strength or 0.0),
                "smart_liq_lower_strength": float(smart_liq_lower_strength or 0.0),
                "smart_liq_upper_avg": float(smart_liq_upper_avg or 0.0),
                "smart_liq_lower_avg": float(smart_liq_lower_avg or 0.0),
            })

        payload = {
            "asset": asset,
            "points": points,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/klines/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def klines_proxy(self, asset, interval="4h", limit="500"):
        candles = request.env["dankbit.trade"].get_candles(asset, interval=interval, limit=int(limit))
        candles = candles[::-1]  # newest-first for frontend
        return request.make_response(
            json.dumps({"result": candles}),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/klines-futures/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def klines_futures_proxy(self, asset, interval="4h", limit="500"):
        """Deribit perpetual-futures equivalent of klines_proxy above —
        sourced from dankbit.trade.get_candles_deribit_perpetual() instead
        of get_candles() (Binance spot). Used by /4l/<asset>'s own candle
        series, per product decision to keep that page on Deribit's own
        perpetuals rather than switching every TradingView page's candle
        source."""
        candles = request.env["dankbit.trade"].get_candles_deribit_perpetual(asset, interval=interval, limit=int(limit))
        candles = candles[::-1]  # newest-first for frontend
        return request.make_response(
            json.dumps({"result": candles}),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/four-leg-gamma/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def four_leg_gamma_json(self, asset):
        """Computed fresh on every request — no model/table behind this
        route: 4-leg gamma extrema (BCG/BPG/SCG/SPG) over `asset`'s own
        trailing trades, restricted to an expiry chosen via the page's
        own "Expiry" dropdown — an optional `?expiry=` query param, one
        of two families:

        - "nearest" (default — the currently soonest-expiring active
          instrument), "nearest_plus_1", or "nearest_plus_2" (the
          1st/2nd active expiry after the nearest one), mapped to an
          ordinal index (0-2) and resolved via
          dankbit.bands._distinct_expirations()/_format_instrument()
          directly rather than dankbit.bands._compute_asset(), since
          this route only needs the instrument string, not a full curve
          build. Trades are ISOLATED to that one resolved instrument's
          own trades via the same anchored `=ilike` domain
          chart_png_zones uses (`f"{instrument}-%"`, left-prefix match
          so one expiry's query can never pull in another's trades).

        - "weekly", "monthly", or "all" — resolved against the
          configured weekly_expiry/monthly_expiry instrument for `asset`
          (eth_-prefixed for ETH, same convention
          _build_tv_chart_context() uses); "all" skips that lookup
          entirely and considers every one of `asset`'s own non-expired
          instruments (`expiration >= now`, no upper bound — same
          no-expiry-cutoff domain gamma_by_strike_json's own "All"
          scope uses). Unlike the ordinal family above, trades here are
          CUMULATIVE through the selected expiry — every active
          instrument for `asset` whose own expiration is <= that
          expiry's day-suffix (same expiration-column cutoff
          gamma_by_strike_until_json/_gamma_by_strike use for their own
          Weekly/Monthly scopes, not a per-instrument name match). This
          is the same domain the since-removed /mwa/<asset> page's own
          mwa_gamma_json route used, ported onto this route's own
          "Expiry" dropdown alongside its original ordinal options
          rather than as a separate page, once /mwa/<asset> was folded
          into this one and removed.

        Any other/missing `?expiry=` value falls back to "nearest". The
        trailing-hours trade window is independently user-selectable via
        the page's own "Window" dropdown — an optional `?hours=` query
        param, restricted to FOUR_LEG_WINDOW_HOURS_CHOICES (4/8/12/24/
        48/72 — or the literal string "all", skipping the trailing-hours
        trade filter entirely; any other/missing value falls back to
        FOUR_LEG_DEFAULT_WINDOW_HOURS=24) — applies to both expiry
        families the same way. Computed via options.per_leg_gamma() — a
        gamma-only slice of options.per_leg_greeks() (the single source
        of truth for the full gamma/delta/theta/vega set, also used by
        dankbit.bands/dankbit.forecast.snapshot/chart_png_zones) that
        skips the theta/vega/delta curves this route never reads (see
        per_leg_gamma()'s own docstring in options.py). Feeds
        /4l/<asset>'s own 4 horizontal gamma-price lines — the page's
        own "refreshFourLegGamma()" timer poll is skipped client-side
        while a cumulative "weekly"/"monthly"/"all" mode is selected,
        same reasoning /mwa/<asset> never polled this class of
        computation on a timer at all (options.per_leg_gamma() loops
        per trade in plain Python, so cost scales directly with trade
        count — potentially every trade for every active instrument at
        once). No points at all (same nothing-computable-yet convention
        every other route in this addon follows) when nothing is active
        at that ordinal position, or (for the cumulative family) when
        the selected expiry isn't configured for `asset`, is malformed,
        or has no matching trades in the resolved window. `points`
        holds exactly one (current) reading, kept as a list for
        shape-compatibility with the page's own existing
        points[points.length-1] read."""
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

        expiry_ordinals = {"nearest": 0, "nearest_plus_1": 1, "nearest_plus_2": 2}
        cumulative_modes = ("weekly", "monthly", "all")
        expiry_mode = (request.httprequest.args.get("expiry") or "").lower()
        if expiry_mode not in expiry_ordinals and expiry_mode not in cumulative_modes:
            expiry_mode = "nearest"

        # "all" (?hours=all) skips the trailing-hours trade filter entirely
        # — checked before the int() parse below so it isn't mistaken for
        # a malformed value and overwritten with the default.
        hours_param = request.httprequest.args.get("hours")
        if hours_param == "all":
            hours = "all"
        else:
            try:
                hours = int(hours_param)
            except (TypeError, ValueError):
                hours = None
            if hours not in FOUR_LEG_WINDOW_HOURS_CHOICES:
                hours = FOUR_LEG_DEFAULT_WINDOW_HOURS

        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        window_start = as_of - timedelta(hours=hours) if hours != "all" else None

        instrument = None
        trades = request.env["dankbit.trade"]
        if expiry_mode in expiry_ordinals:
            expiry_index = expiry_ordinals[expiry_mode]
            bands_model = request.env["dankbit.bands"]
            expirations = bands_model._distinct_expirations(asset, as_of, expiry_index + 1)
            if len(expirations) > expiry_index:
                instrument = bands_model._format_instrument(asset, expirations[expiry_index])
            if instrument:
                domain = [("name", "=ilike", f"{instrument}-%"), ("iv", "!=", 0)]
                if window_start is not None:
                    domain += [("deribit_ts", ">=", window_start), ("deribit_ts", "<=", as_of)]
                trades = trades.with_context(active_test=False).search(domain)
        else:
            # weekly/monthly/all — cumulative through expiry, same domain
            # the since-removed /mwa/<asset>'s own mwa_gamma_json used.
            if expiry_mode != "all":
                if asset == "ETH":
                    expiry_param = "dankbit.eth_weekly_expiry" if expiry_mode == "weekly" else "dankbit.eth_monthly_expiry"
                else:
                    expiry_param = "dankbit.weekly_expiry" if expiry_mode == "weekly" else "dankbit.monthly_expiry"
                instrument = icp.get_param(expiry_param, default="").upper() or None

            # Naive UTC, same as every other `expiration` domain comparison
            # in this file (see chart_png_until's own as_of/window_start).
            expiry_dt = None
            parts = instrument.split("-", 1) if instrument else []
            if len(parts) == 2:
                try:
                    expiry_dt = datetime.strptime(parts[1], "%d%b%y").replace(hour=8)
                except ValueError:
                    expiry_dt = None

            if expiry_mode == "all" or expiry_dt:
                domain = [("name", "=ilike", f"{asset}-%"), ("expiration", ">=", as_of), ("iv", "!=", 0)]
                if expiry_dt:
                    domain.append(("expiration", "<=", expiry_dt))
                if window_start is not None:
                    domain += [("deribit_ts", ">=", window_start), ("deribit_ts", "<=", as_of)]
                trades = request.env["dankbit.trade"].search(domain)

        STs = np.arange(from_price, to_price, step, dtype=np.float64)

        points = []
        if trades:
            # per_leg_gamma() rather than per_leg_greeks() — only
            # gamma_price/gamma_value are read below, so the theta/
            # vega/delta curves per_leg_greeks() would also compute
            # are pure waste here (see per_leg_gamma()'s docstring in
            # options.py).
            legs = options.per_leg_gamma(STs, trades)
            # A leg with zero trades reports gamma_price as None (no
            # curve to peak/bottom at) — collapsed to 0.0 here, same
            # "0.0 = absent" sentinel the client's own
            # `if (latest.bcg_price)` falsy checks already expect
            # (dankbit_four_leg_gamma_chart_templates.xml).
            points.append({
                "t": int(as_of.replace(tzinfo=timezone.utc).timestamp() * 1000),
                "trade_count": len(trades),
                "bcg_price": legs["long_call"]["gamma_price"] or 0.0, "bcg_value": legs["long_call"]["gamma_value"],
                "bpg_price": legs["long_put"]["gamma_price"] or 0.0, "bpg_value": legs["long_put"]["gamma_value"],
                "scg_price": legs["short_call"]["gamma_price"] or 0.0, "scg_value": legs["short_call"]["gamma_value"],
                "spg_price": legs["short_put"]["gamma_price"] or 0.0, "spg_value": legs["short_put"]["gamma_value"],
            })

        payload = {"asset": asset, "instrument": instrument, "points": points}
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/4l/<string:asset>", type="http", auth="user", website=True)
    def four_leg_gamma_chart(self, asset):
        """Standalone TradingView page — 4 horizontal price lines (BCG/
        BPG/SCG/SPG — where each of the 4 trade legs' own portfolio
        dollar-gamma curve, over trailing trades restricted to one
        expiry, peaks or bottoms), recomputed live on every poll via
        /api/four-leg-gamma (no model/table behind this page), alongside
        real Deribit perpetual-futures candles (dankbit.trade.
        get_candles_deribit_perpetual(), same /api/klines-futures/<asset>
        proxy /gt/<asset> uses — Deribit has no native 4h resolution, so
        "4h" is built server-side by fetching native 60-minute bars and
        bucketing every 4 into one; 15m/1h/1d map directly onto Deribit's
        own resolution strings). This page originally sourced Deribit
        perpetual-futures candles at a fixed 1h timeframe, then moved to
        Binance spot candles since Deribit's chart API has no native 4h
        resolution, then moved to Kraken Futures to keep this page on
        perpetuals (not spot) while still getting a native 4h/1d
        resolution — moved back to Deribit perpetuals per product
        decision, now that the 4h bucket is built server-side instead.
        Own "Timeframe" dropdown (15m/1h/4h/1d, 4h default — 15m was
        added to match /gt/<asset>'s own dropdown, since Deribit natively
        supports it too, and was originally also this page's own default
        until it was changed to 4h per product decision, matching the
        Delta/Gamma/Strike Gamma charts' own default),
        own "Expiry" dropdown — Nearest/Nearest+1/Nearest+2 (Nearest
        default, same nearest+1/nearest+2 ordinal notion /gt/<asset>'s
        own 2nd/3rd price lines used before that page was removed;
        trades ISOLATED to that one resolved instrument), PLUS
        Weekly/Monthly/All (the configured weekly_expiry/monthly_expiry
        instrument for `asset`, "All" considering every one of the
        asset's own non-expired instruments; trades CUMULATIVE through
        the selected expiry) — see four_leg_gamma_json for the full
        history/resolution of both option families, including how
        Weekly/Monthly/All were originally this route's own options,
        removed per product decision in favor of Nearest+1/Nearest+2,
        then re-added alongside them once the standalone /mwa/<asset>
        page (which had carried that Weekly/Monthly/All + cumulative
        behavior in the interim) was folded back into this one and
        removed. Own "Window"
        dropdown (4h/8h/12h/24h/48h/72h/All — FOUR_LEG_WINDOW_HOURS_
        CHOICES plus the "All" no-window-bound option, 24h default,
        independent of the "Expiry" dropdown; this choice set has moved
        several times across product decisions (see
        FOUR_LEG_WINDOW_HOURS_CHOICES/FOUR_LEG_DEFAULT_WINDOW_HOURS in
        this file for the full history), most recently narrowing from
        every 1h step 1h-8h plus 12h/24h/All down to 4h/8h/12h/24h/48h/
        72h/All; see four_leg_gamma_json for how each option resolves). A vertical
        marker line showing where the selected Window's trailing-hours
        cutoff falls used to be drawn on the candle chart (#window-vline)
        but was removed per product decision.
        Renders its own standalone template
        (dankbit_four_leg_gamma_chart). Polls the 4 gamma-price lines on
        the general dankbit.refresh_interval, same as every other
        page's own refresh rate — EXCEPT while a cumulative
        Weekly/Monthly/All "Expiry" is selected, where that poll is
        skipped client-side (same reasoning the since-removed
        /mwa/<asset> page never polled this class of computation on a
        timer at all — see four_leg_gamma_json's own docstring); the
        candle series' own 5s auto-refresh is unaffected either way."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        ctx = {"asset": asset, "refresh_interval": refresh_interval}
        return request.render("dankbit.dankbit_four_leg_gamma_chart", ctx)

    @http.route("/api/forecast/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def forecast_json(self, asset, **kw):
        """Unified 4H Forecast path.  Candle one is the revised Next Candle
        Greek-Flow result; later candles retain the deterministic Thales
        Gamma/Bands/Zones/Smart-Liquidity geometry and continue from its close.

        The Thales Forecast candle engine is a full port of Thales's
        "Thales Bands" Pine indicator's forecast-candle engine (see
        forecast.py) onto Dankbit's own live per-leg gamma/delta/theta/vega
        Greeks, rather than Thales's manually-typed-in daily CSV rows.
        Unlike this addon's earlier, now-removed GBM-based forecast
        engines, this path is fully deterministic — no random component
        anywhere, matching the source script, which has none either; every
        candle is a direct function of the current Greeks, the last couple
        of persisted dankbit.forecast.snapshot rows (for the Gamma-Band
        Consensus slope), recent real candles matching the selected timeframe (for ATR/momentum/
        liquidity-sweep detection), and the forward Gamma Band dashed-line
        points (so the forecast trends the same direction as that line —
        see forecast.gamma_band_term_slope). The actual computation lives
        on dankbit.forecast.snapshot.get_forecast_points() — this route is
        now just that method plus JSON serialization, so dankbit.forecast.log's
        cron (see that model) can run the exact same computation without
        an HTTP request context. `points` is empty when there's nothing
        computable yet for this asset (no index price, no active expiry,
        or no trades in the current 00:00-UTC window — see
        dankbit.forecast.snapshot.compute_and_persist)."""
        asset = asset.upper()
        if asset not in ("BTC", "ETH"):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        hours_param = request.httprequest.args.get("hours")
        try:
            hours = int(hours_param) if hours_param else None
        except (TypeError, ValueError):
            hours = None
        if hours is not None and not 1 <= hours <= 168:
            hours = None

        timeframe = request.httprequest.args.get("timeframe", "4h")
        if timeframe not in next_candle_forecast.TIMEFRAME_CONFIG:
            timeframe = "4h"
        max_counts = {"1h": 72, "4h": 18, "1d": 3}
        max_count = max_counts[timeframe]
        count_param = request.httprequest.args.get("count")
        try:
            forecast_count = max(1, min(int(count_param or max_count), max_count))
        except (TypeError, ValueError):
            forecast_count = max_count

        result = request.env["dankbit.forecast.snapshot"].get_forecast_points(
            asset, hours=hours, timeframe=timeframe,
        )
        ForecastNext = request.env["dankbit.forecast.next_candle"]
        bands_model = request.env["dankbit.bands"]
        forecast_expirations = bands_model._distinct_expirations(
            asset, datetime.now(timezone.utc).replace(tzinfo=None), 3,
            future_days_only=True,
        )
        anchors = {}
        anchor_meta = []
        for expiry_index in range(3):
            preview = ForecastNext.preview_for_dashboard(
                asset, timeframe=timeframe, hours=hours, expiry_index=expiry_index,
            ) if hours is not None else None
            next_row = ForecastNext.latest_for_dashboard(
                asset, timeframe=timeframe, expiry_index=expiry_index,
            ) if hours is None else None
            meta = {
                "expiry_index": expiry_index, "available": False,
                "expiry_instrument": (
                    bands_model._format_instrument(asset, forecast_expirations[expiry_index])
                    if expiry_index < len(forecast_expirations) else None
                ),
                "revision": None, "max_revisions": None, "confidence": None,
                "is_preview": False, "window_hours": None,
                "unavailable_reason": "not-generated-yet",
            }
            if next_row and meta["expiry_instrument"] and next_row.expiry_instrument != meta["expiry_instrument"]:
                # Never leak today's stale ordinal row into tomorrow's E1.
                next_row = None
                meta["unavailable_reason"] = "stale-expiry-row"
            if preview and not preview.get("data_complete", True):
                meta["unavailable_reason"] = "missing-critical-leg:" + ",".join(preview.get("missing_legs") or [])
                preview = None
            if next_row and not preview:
                complete, missing_legs = _forecast_leg_completeness(next_row.snapshot_id) if next_row.snapshot_id else (False, ["SNAPSHOT"])
                if not complete:
                    next_row = None
                    meta["unavailable_reason"] = "missing-critical-leg:" + ",".join(missing_legs)
            if preview:
                anchors[expiry_index] = {
                    "open": preview["forecast_open"], "high": preview["forecast_high"],
                    "low": preview["forecast_low"], "close": preview["forecast_close"],
                    "confidence": preview["confidence"],
                }
                meta.update({
                    "available": True, "revision": preview["revision"],
                    "expiry_instrument": preview.get("expiry_instrument"),
                    "max_revisions": preview["max_revisions"],
                    "confidence": preview["confidence"], "is_preview": True,
                    "window_hours": preview["window_hours"],
                    "unavailable_reason": None,
                })
            elif next_row:
                anchors[expiry_index] = {
                    "open": next_row.forecast_open, "high": next_row.forecast_high,
                    "low": next_row.forecast_low, "close": next_row.forecast_close,
                    "confidence": next_row.confidence,
                }
                meta.update({
                    "available": True, "revision": next_row.revision,
                    "expiry_instrument": next_row.expiry_instrument,
                    "max_revisions": next_row.max_revisions,
                    "confidence": next_row.confidence,
                    "unavailable_reason": None,
                })
            anchor_meta.append(meta)

        unified = _compose_unified_forecast(
            result["points"], anchors, forecast_count,
            max_count=max_count, timeframe=timeframe,
        )
        # RSI is deliberately retrospective and never participates in the
        # forward direction/EA calculation. It only shapes rejection wicks on
        # the first few candles, with a closed-1H momentum persistence gate.
        Trade = request.env["dankbit.trade"]
        try:
            timeframe_candles = Trade.get_candles(asset, interval=timeframe, limit=260) or []
            hourly_candles = (timeframe_candles if timeframe == "1h"
                              else Trade.get_candles(asset, interval="1h", limit=260) or [])
            unified, rsi_context = _rsi_forecast_geometry(
                unified, timeframe_candles, hourly_candles, timeframe=timeframe,
            )
        except Exception:
            _logger.exception("RSI Forecast geometry failed for %s %s; serving untouched Forecast", asset, timeframe)
            rsi_context = {"available": False, "error": "geometry-unavailable"}
        forecast_warning = _forecast_quality_warning(
            unified, anchor_meta, result["index_price"], result["sigma_annual"], timeframe,
        )
        generated_at = result["generated_at"]
        now_ms = int(generated_at.timestamp() * 1000)
        points = [
            {
                "t": now_ms + int(p["hours"] * 3600 * 1000),
                "open": p["open"], "high": p["high"], "low": p["low"], "close": p["close"],
                "mode": p["mode"],
            }
            for p in unified
        ]

        payload = {
            "asset": asset,
            "index_price": result["index_price"],
            "sigma_annual": result["sigma_annual"],
            "window_hours": result.get("window_hours"),
            "timeframe": timeframe,
            "max_forecast_count": max_count,
            "forecast_count": len(points),
            "first_candle": anchor_meta[0],
            "anchors": anchor_meta,
            "forecast_warning": forecast_warning,
            "rsi_context": rsi_context,
            "points": points,
            "generated_at": generated_at.isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/next-candle-forecast/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def next_candle_forecast_json(self, asset):
        """The "Next Candle Forecast" small status pill + candle on the
        Delta Chart — a distinct feature from /api/forecast/<asset> above
        (that one's own 18-candle rolling series is untouched): tracks
        exactly ONE target candle at a time, per timeframe (1H/4H/1D — see
        `?timeframe=`), revised on that timeframe's own fixed cadence
        while its current candle is still forming (see
        dankbit.forecast.next_candle and controllers/next_candle_forecast.py).
        Returns the latest (highest-revision) row for the asset's
        currently-open target candle, or `{"asset": asset, "row": None}`
        if nothing has been computed yet (e.g. that timeframe's own
        compute_and_log() cron hasn't run, or isn't active)."""
        asset = asset.upper()
        if asset not in ("BTC", "ETH"):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        timeframe = request.httprequest.args.get("timeframe", "4h")
        if timeframe not in next_candle_forecast.TIMEFRAME_CONFIG:
            timeframe = "4h"
        try:
            expiry_index = max(0, min(int(request.httprequest.args.get("expiry_index", 0)), 2))
        except (TypeError, ValueError):
            expiry_index = 0

        hours_param = request.httprequest.args.get("hours")
        try:
            hours = int(hours_param) if hours_param else None
        except (TypeError, ValueError):
            hours = None
        if hours is not None and not 1 <= hours <= 168:
            hours = None

        ForecastNext = request.env["dankbit.forecast.next_candle"]
        preview = ForecastNext.preview_for_dashboard(
            asset, timeframe=timeframe, hours=hours, expiry_index=expiry_index,
        ) if hours is not None else None
        row = ForecastNext.latest_for_dashboard(
            asset, timeframe=timeframe, expiry_index=expiry_index,
        ) if hours is None else None
        row_payload = None
        if preview:
            row_payload = {
                "timeframe": preview["timeframe"],
                "expiry_index": expiry_index,
                "expiry_instrument": preview.get("expiry_instrument"),
                "target_time": preview["target_time"].isoformat(),
                "revision": preview["revision"],
                "max_revisions": preview["max_revisions"],
                "is_final": preview["is_final"],
                "confidence": preview["confidence"],
                "forecast_open": preview["forecast_open"],
                "forecast_close": preview["forecast_close"],
                "forecast_high": preview["forecast_high"],
                "forecast_low": preview["forecast_low"],
                "window_hours": preview["window_hours"],
                "is_preview": True,
            }
        elif row:
            row_payload = {
                "timeframe": row.timeframe,
                "expiry_index": row.expiry_index,
                "expiry_instrument": row.expiry_instrument,
                "target_time": row.target_time.isoformat(),
                "revision": row.revision,
                "max_revisions": row.max_revisions,
                "is_final": row.is_final,
                "confidence": row.confidence,
                "forecast_open": row.forecast_open,
                "forecast_close": row.forecast_close,
                "forecast_high": row.forecast_high,
                "forecast_low": row.forecast_low,
                "window_hours": None,
                "is_preview": False,
            }
        payload = {
            "asset": asset,
            "row": row_payload,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route('/api/signal-bot/<string:asset>', type='http', auth='user', methods=['GET'], csrf=False)
    def signal_bot_json(self, asset, **kwargs):
        """Latest auditable Signal Bot decision for the Delta Chart's
        "Signal Bot" panel. auth="user" like every other route in this
        addon (see Odoo Gotchas in CLAUDE.md) — the browser's own session
        cookie covers this same-origin fetch() once the chart page itself
        is loaded.

        An active official decision remains displayed even after later hourly
        audit rows are written. Otherwise the newest hourly decision explains
        why the bot is still waiting.
        """
        asset = (asset or '').upper()
        if asset not in ('BTC', 'ETH'):
            return request.make_response(
                json.dumps({"error": "Unsupported asset"}),
                headers=[("Content-Type", "application/json")], status=400,
            )
        Signal = request.env["dankbit.signal"].sudo()
        utc_day = datetime.now(timezone.utc).date()
        active_official = Signal.search([
            ("asset", "=", asset), ("kind", "=", "official"),
            ("state", "=", "active"),
        ], order="evaluated_at desc, id desc", limit=1)
        row = active_official or Signal.search([
            ("asset", "=", asset), ("utc_day", "=", utc_day),
        ], order="evaluated_at desc, id desc", limit=1)
        week_start = utc_day - timedelta(days=utc_day.weekday())
        weekly_count = Signal.search_count([
            ("asset", "=", asset), ("utc_day", ">=", week_start),
            ("kind", "=", "official"),
        ])
        payload = {"asset": asset, "utc_day": utc_day.isoformat(),
                   "weekly_signal_count": weekly_count, "weekly_signal_limit": 3,
                   "signal": None}
        if row:
            payload["signal"] = {
                "id": row.id,
                "kind": row.kind,
                "state": row.state,
                "direction": row.direction,
                "evaluated_at": row.evaluated_at.isoformat() if row.evaluated_at else None,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "entry": row.entry,
                "stop_loss": row.stop_loss,
                "target": row.target,
                "risk_reward": row.risk_reward,
                "swing_level": row.swing_level,
                "atr_4h": row.atr_4h,
                "stop_basis": row.stop_basis or "",
                "trend_daily": row.trend_daily or "neutral",
                "trend_daily_tactical": row.trend_daily_tactical or "neutral",
                "trend_4h": row.trend_4h or "neutral",
                "trend_1h": row.trend_1h or "neutral",
                "trend_score_daily": row.trend_score_daily,
                "trend_score_daily_tactical": row.trend_score_daily_tactical,
                "trend_score_4h": row.trend_score_4h,
                "gamma_direction_score": row.gamma_direction_score,
                "greek_direction_score": row.greek_direction_score,
                "weekly_signal_count": weekly_count,
                "weekly_signal_limit": 3,
                "setup_stage": row.setup_stage or "none",
                "setup_path": row.setup_path or "",
                "plan_status": row.plan_status or "",
                "entry_zone_low": row.entry_zone_low,
                "entry_zone_high": row.entry_zone_high,
                "invalidation_level": row.invalidation_level,
                "target_2": row.target_2,
                "breakout_level": row.breakout_level,
                "breakout_at": row.breakout_at.isoformat() if row.breakout_at else None,
                "plan_provisional": row.plan_provisional,
                "trigger_level": row.trigger_level,
                "trigger_basis": row.trigger_basis or "",
                "liquidity_swept": row.liquidity_swept,
                "rtm_score": row.rtm_score,
                "rtm_zone_low": row.rtm_zone_low,
                "rtm_zone_high": row.rtm_zone_high,
                "rtm_zone_type": row.rtm_zone_type or "",
                "rtm_structure": row.rtm_structure or "",
                "rtm_opposing_structure": row.rtm_opposing_structure or "",
                "rtm_opposing_level": row.rtm_opposing_level,
                "rtm_path_clear": row.rtm_path_clear,
                "rtm_touch_count": row.rtm_touch_count,
                "rtm_fresh": row.rtm_fresh,
                "rtm_base_candles": row.rtm_base_candles,
                "rtm_departure_score": row.rtm_departure_score,
                "rtm_compression": row.rtm_compression,
                "rtm_gap": row.rtm_gap,
                "rtm_nested": row.rtm_nested,
                "entry_model": row.entry_model or "",
                "quality_score": row.quality_score,
                "final_score": row.final_score,
                "result_r": row.result_r,
                "reason": row.reason or "",
                "is_weekend": row.is_weekend,
            }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route('/signal-bot/report', type='http', auth='user', methods=['GET'], csrf=False)
    def signal_bot_report_page(self, **kwargs):
        """Human-readable automatic forward-test dashboard."""
        return request.render("dankbit.signal_bot_report")

    @http.route('/api/signal-bot/report', type='http', auth='user', methods=['GET'], csrf=False)
    def signal_bot_report_json(self, **kwargs):
        """Aggregate immutable Signal Bot outcomes into auditable R metrics."""
        Signal = request.env["dankbit.signal"].sudo()
        try:
            days = int(kwargs.get("days", 30))
        except (TypeError, ValueError):
            days = 30
        days = days if days in (0, 7, 30, 90, 365) else 30
        asset = str(kwargs.get("asset", "all") or "all").upper()
        kind = str(kwargs.get("kind", "official") or "official").lower()
        entry_model = str(kwargs.get("entry_model", "all") or "all").lower()
        if asset not in ("ALL", "BTC", "ETH"):
            asset = "ALL"
        if kind not in ("all", "official", "shadow"):
            kind = "official"
        allowed_models = (
            "all", "ftb_confirmed", "ftb_reclaim_shadow", "later_touch_shadow",
            "breakout_watch", "breakout_retest_shadow", "breakout_retest_confirmed",
        )
        if entry_model not in allowed_models:
            entry_model = "all"

        common_domain = [("kind", "in", ("official", "shadow"))]
        if days:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
            common_domain.append(("evaluated_at", ">=", cutoff))
        if asset != "ALL":
            common_domain.append(("asset", "=", asset))
        if kind != "all":
            common_domain.append(("kind", "=", kind))
        if entry_model != "all":
            common_domain.append(("entry_model", "=", entry_model))

        closed = Signal.search(
            common_domain + [("state", "in", ("tp", "sl", "expired", "ambiguous"))],
            order="evaluated_at asc, id asc",
        )
        open_count = Signal.search_count(
            common_domain + [("state", "in", ("active", "watch"))],
        )

        def row_r(row):
            return float(row.result_r or 0.0)

        def stats(rows):
            rows = list(rows)
            resolved = [r for r in rows if r.state in ("tp", "sl")]
            performance = [r for r in rows if r.state != "ambiguous"]
            wins = sum(1 for r in resolved if r.state == "tp")
            losses = sum(1 for r in resolved if r.state == "sl")
            expired = sum(1 for r in rows if r.state == "expired")
            ambiguous = sum(1 for r in rows if r.state == "ambiguous")
            values = [row_r(r) for r in performance]
            gross_profit = sum(v for v in values if v > 0)
            gross_loss = abs(sum(v for v in values if v < 0))
            equity = peak = max_drawdown = 0.0
            loss_streak = max_loss_streak = 0
            curve = []
            for row in performance:
                equity += row_r(row)
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, peak - equity)
                if row_r(row) < 0:
                    loss_streak += 1
                    max_loss_streak = max(max_loss_streak, loss_streak)
                else:
                    loss_streak = 0
                curve.append({
                    "time": row.evaluated_at.isoformat() if row.evaluated_at else None,
                    "equity": round(equity, 4),
                })
            planned_rr = [float(r.risk_reward or 0.0) for r in rows if r.risk_reward > 0]
            return {
                "closed": len(rows), "resolved": len(resolved),
                "wins": wins, "losses": losses, "expired": expired,
                "ambiguous": ambiguous,
                "win_rate": round(100.0 * wins / len(resolved), 2) if resolved else None,
                "positive_rate": round(100.0 * sum(v > 0 for v in values) / len(values), 2) if values else None,
                "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
                "profit_factor_infinite": bool(gross_profit and not gross_loss),
                "gross_profit_r": round(gross_profit, 4), "gross_loss_r": round(gross_loss, 4),
                "expectancy_r": round(sum(values) / len(values), 4) if values else None,
                "total_r": round(sum(values), 4),
                "max_drawdown_r": round(max_drawdown, 4),
                "max_loss_streak": max_loss_streak,
                "average_rr": round(sum(planned_rr) / len(planned_rr), 3) if planned_rr else None,
                "curve": curve,
            }

        def breakdown(field_name):
            grouped = {}
            for row in closed:
                key = str(getattr(row, field_name, False) or "unspecified")
                grouped.setdefault(key, []).append(row)
            result = []
            for key, rows in grouped.items():
                item = stats(rows)
                item.pop("curve", None)
                item["name"] = key
                result.append(item)
            return sorted(result, key=lambda item: item["closed"], reverse=True)

        summary = stats(closed)
        sample = summary["resolved"]
        sample_label = ("Insufficient" if sample < 30 else "Preliminary" if sample < 60
                        else "Meaningful" if sample < 150 else "Mature")
        recent = []
        for row in reversed(closed[-50:]):
            recent.append({
                "id": row.id, "time": row.evaluated_at.isoformat() if row.evaluated_at else None,
                "asset": row.asset, "kind": row.kind, "direction": row.direction,
                "state": row.state, "entry_model": row.entry_model or "",
                "rtm_structure": row.rtm_structure or "", "rtm_score": row.rtm_score,
                "risk_reward": row.risk_reward, "result_r": row.result_r,
            })
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filters": {"days": days, "asset": asset, "kind": kind, "entry_model": entry_model},
            "open_count": open_count, "sample_label": sample_label,
            "summary": summary,
            "breakdowns": {
                "asset": breakdown("asset"), "entry_model": breakdown("entry_model"),
                "rtm_structure": breakdown("rtm_structure"), "direction": breakdown("direction"),
            },
            "recent": recent,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    # ------------------------------------------------------------------
    # TradingView Lightweight Charts pages
    # ------------------------------------------------------------------

    def _build_tv_chart_context(self, asset):
        """Context-building for /chart/<asset> (dankbit_tv_chart_until
        template). show_gamma_point/show_strike_gamma are always "false"
        here now — they used to be flipped to "true" by the since-removed
        /oi/<asset> and /mp/<instrument> routes to draw the "Top OI(s)"/
        "Strike Gamma" indicators on this same shared template; kept as
        explicit context keys (rather than removed) since the template's
        own gating logic (e.g. MINIMAL_CHART) still reads them. Returns
        (context, None) on success or (None, error_message) if the weekly
        expiry isn't configured/valid for `asset`, so callers can render
        that as a plain text response the same way this route always
        has."""
        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        zones_box_refresh_interval = int(icp.get_param("dankbit.zones_box_refresh_interval", default=3600))
        zones_box_window_hours = int(icp.get_param("dankbit.zones_box_window_hours", default=8))
        # QWeb's t-att-* omits the attribute entirely when the value is a
        # falsy Python bool/None, so pass "true"/"false" strings (always
        # truthy) rather than real booleans — otherwise data-show-daily=false
        # would render as no attribute at all, indistinguishable from unset.
        show_daily_lines = "true" if icp.get_param("dankbit.show_daily_lines", default="True") == "True" else "false"
        show_weekly_lines = "true" if icp.get_param("dankbit.show_weekly_lines", default="True") == "True" else "false"
        show_monthly_lines = "true" if icp.get_param("dankbit.show_monthly_lines", default="True") == "True" else "false"

        # Thales Forecast candle colors — rendering-only, read here (not
        # dankbit.forecast.snapshot.get_forecast_cfg()) since they only
        # affect the client-side forecastSeries, not simulate_forecast()'s
        # own math.
        forecast_up_color = icp.get_param("dankbit.forecast_up_color", default="#a5d6a7")
        forecast_down_color = icp.get_param("dankbit.forecast_down_color", default="#ef9a9a")
        forecast_wick_up_color = icp.get_param("dankbit.forecast_wick_up_color", default="#66bb6a")
        forecast_wick_down_color = icp.get_param("dankbit.forecast_wick_down_color", default="#e57373")

        if asset.startswith("ETH"):
            weekly_param = "dankbit.eth_weekly_expiry"
            monthly_param = "dankbit.eth_monthly_expiry"
        else:
            weekly_param = "dankbit.weekly_expiry"
            monthly_param = "dankbit.monthly_expiry"

        instrument = icp.get_param(weekly_param, default="").upper()

        if not instrument:
            return None, f"Weekly Expiry for {asset} is not configured. Set it in Settings → Dankbit."

        parts = instrument.split("-", 1)
        if len(parts) != 2:
            return None, f"Weekly Expiry '{instrument}' is invalid — expected format: {asset}-3JUL26."

        expiry_str = parts[1]

        monthly_instrument = icp.get_param(monthly_param, default="").upper()

        # Delta Chart's window title shows the nearest active expiry rather
        # than the configured weekly expiry (the "expiry" field above) —
        # showing the weekly one there had been read by Thales's dev as the
        # expiry the chart's trades/indicators are scoped to, which isn't
        # true for the Bands/Zones/gamma-band indicators (all nearest-expiry
        # based). Falls back to the weekly expiry's own day-suffix if there's
        # no active expiry at all, so the title never renders blank.
        nearest_instrument = request.env["dankbit.bands"].nearest_expiry(asset)
        nearest_expiry_str = nearest_instrument.split("-", 1)[1] if nearest_instrument else expiry_str

        return {
            "instrument": instrument,
            "asset": asset,
            "expiry": expiry_str,
            "nearest_expiry": nearest_expiry_str,
            "monthly_instrument": monthly_instrument,
            "refresh_interval": refresh_interval,
            "zones_box_refresh_interval": zones_box_refresh_interval,
            "zones_box_window_hours": zones_box_window_hours,
            "show_daily_lines": show_daily_lines,
            "show_weekly_lines": show_weekly_lines,
            "show_monthly_lines": show_monthly_lines,
            "show_gamma_point": "false",
            "show_strike_gamma": "false",
            "strike_gamma_instrument": "",
            "forecast_up_color": forecast_up_color,
            "forecast_down_color": forecast_down_color,
            "forecast_wick_up_color": forecast_wick_up_color,
            "forecast_wick_down_color": forecast_wick_down_color,
        }, None

    @http.route("/chart/<string:asset>", type="http", auth="user", website=True)
    def chart_tv(self, asset):
        asset = asset.upper()

        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        ctx, error = self._build_tv_chart_context(asset)
        if error:
            return request.make_response(error, headers=[("Content-Type", "text/plain")])

        return request.render("dankbit.dankbit_tv_chart_until", ctx)
