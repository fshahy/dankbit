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

# /gt/<asset>'s own trade window — no model/table backs this page at
# all, everything is recomputed fresh on every request via
# dankbit.bands._compute_asset(). The 3 expiry gammas (nearest/weekly/
# monthly) use this same trailing-hours window (passed as
# _compute_asset()'s own `hours` param — not that method's own
# UTC-midnight default), user-selectable via the page's own "Window"
# dropdown (?hours= query param on /api/gamma-triple/<asset>).
Y_CHART_DEFAULT_WINDOW_HOURS = 12
Y_CHART_WINDOW_HOURS_CHOICES = (12, 24, 48)

# /4l/<asset>'s own "Window" dropdown numeric choice set — 8h/12h/24h —
# split out from Y_CHART_WINDOW_HOURS_CHOICES (which /gt/<asset> still
# uses unchanged) once this page's own choice set grew past that shared
# 12/24/48 tuple, so /gt/<asset>'s own "Window" dropdown is unaffected.
# 16h/20h/48h/72h and the "All" (no-window-bound) option were offered at
# one point and removed per product decision; 3h/5h/7h were added later,
# filling in the gaps left in the original 1h/2h/4h/6h/8h/12h set; 24h
# and "All" were both re-added afterwards per later product decisions;
# 1h/2h/3h/4h/5h/6h/7h/8h were then removed in a further round, leaving
# only 12h/24h in this tuple; 8h was then re-added per a later product
# decision. "All" (`?hours=all`) is handled as a separate string sentinel
# in four_leg_gamma_json, not a member of this tuple — it skips the
# trailing-hours trade filter entirely rather than mapping to a number
# of hours.
FOUR_LEG_WINDOW_HOURS_CHOICES = (8, 12, 24)

# /4l/<asset>'s own "Window" dropdown default — also the fallback used
# by four_leg_gamma_json when `?hours=` is missing/malformed, same as
# every other *_DEFAULT_WINDOW_HOURS fallback in this file. Was a number
# (8, i.e. 8h) until 1h-8h were removed from FOUR_LEG_WINDOW_HOURS_CHOICES
# above, at which point the default moved to the "all" string sentinel
# (see four_leg_gamma_json) rather than to another numeric choice.
FOUR_LEG_DEFAULT_WINDOW_HOURS = "all"

# /mw/<asset>'s own "Window" dropdown numeric choice set — 12h/24h, kept
# as its own constant rather than extending Y_CHART_WINDOW_HOURS_CHOICES,
# so /gt's and /4l's own "Window" dropdowns are unaffected. Both this
# tuple and /4l/<asset>'s own FOUR_LEG_WINDOW_HOURS_CHOICES were reduced
# to 12h/24h independently, in separate product decisions, then 8h was
# re-added to /4l/<asset>'s own tuple only (a further independent
# decision) — the two are kept as independent constants, not aliased to
# each other, so they no longer have to stay identical.
# Previously 4h/8h/12h/16h/20h/24h/48h/72h plus a no-window-bound "All"
# option; 16h/20h/48h/72h/All were removed and 1h/2h/6h/12h added per
# product decision, across three rounds (12h was dropped in the first
# round, then re-added — alongside 24h being dropped — in a second, then
# 6h added in a third; the "All" no-window-bound mode — `?hours=all` —
# was removed along with its dropdown option; see mw_gamma_json). 24h and
# "All" were both re-added afterwards per later product decisions, then
# 1h/2h/4h/6h/8h were removed in a further round, leaving only 12h/24h in
# this tuple. "All" is handled as a separate string sentinel in
# mw_gamma_json, not a member of this tuple, and skips the trailing-hours
# trade filter entirely rather than mapping to a number of hours.
MW_WINDOW_HOURS_CHOICES = (12, 24)

# /mw/<asset>'s own "Window" dropdown default — kept as its own constant
# since this page's own default is an independent product decision.
# Was a number (4, i.e. 4h) until 4h was removed from
# MW_WINDOW_HOURS_CHOICES above, at which point the default moved to the
# "all" string sentinel (see mw_gamma_json) rather than to another
# numeric choice.
MW_DEFAULT_WINDOW_HOURS = "all"


def _compose_unified_forecast(raw_points, anchors=None, count=18, max_count=18, timeframe="4h"):
    """Build a continuous three-expiry path around up to three anchors.

    Anchor 0 is the immediate Next-Candle result. Anchor 1/2 use the Greek
    Flow/Gamma/Zones/Smart-Liquidity candle shape computed independently from
    the second/third expiry and sit one/two days farther into the path. Raw
    Thales bodies between anchors are smoothly corrected so the path reaches
    the next anchor without a discontinuity; their wick geometry is retained.
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
            # Greek Flow owns the first candle's body and wick geometry, but
            # never its absolute opening price.  The opening price belongs to
            # the continuous market/forecast chain.  Anchoring here to the raw
            # forecast path also prevents an older persisted E1 revision from
            # creating a price gap before the first forecast candle.
            source[0], float(source[0]["open"]),
            body=first_anchor["close"] - first_anchor["open"],
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
        # With adjacent anchors (Daily), continuity takes precedence. With
        # intermediate candles, the distributed correction lands exactly on
        # the structural raw target-open for this expiry anchor.
        anchor_open = float(output[-1]["close"])
        output.append(geometry(
            source[anchor_index], anchor_open,
            body=float(anchor["close"]) - float(anchor["open"]),
            wick_source=anchor, mode="expiry_anchor_%s" % (anchor_index // candles_per_day + 1),
        ))
        cursor = anchor_index + 1

    for raw in source[cursor:]:
        new_open = float(output[-1]["close"])
        output.append(geometry(raw, new_open))
    return output


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

    def _gamma_by_strike(self, asset, expiry_cutoff=None, expiry_exact=None):
        """Combined portfolio dollar-gamma evaluated at each distinct strike
        that has ever traded. Three mutually exclusive trade-selection
        modes: every active expiry up to and including `expiry_cutoff`
        (cumulative — pass `expiry_cutoff`), every active expiry at all
        (pass neither), or trades whose expiration exactly matches
        `expiry_exact` alone (isolated — pass `expiry_exact`; that one
        instrument's own trades only, not folded in with any sooner
        expiry's). No trailing-hours restriction in any mode. Unlike a
        peak/bottom search over a synthetic price grid, this evaluates
        gamma.portfolio_gamma() directly at each real strike price —
        feeds the Gamma Chart's "Top OI(s)" checkbox, which fetches
        this via gamma_by_strike_json (no cutoff — "All") and
        gamma_by_strike_until_json 2 more times (expiry_cutoff =
        configured weekly expiry / configured monthly expiry, for
        "Weekly"/"Monthly" respectively) and, from each of those 3
        datasets, marks only the single highest-positive-gamma strike
        rather than drawing every strike; and feeds /mp/<instrument>'s
        "Strike Gamma"
        indicator via gamma_by_strike_at_json (expiry_exact — isolated to
        that one instrument, unlike Top OI(s)' cumulative scopes above),
        which draws every strike rather than collapsing to one. Returns
        (strikes, trade_count), strikes a price-sorted list
        of {"price", "gamma", "long_call", "long_put", "short_call",
        "short_put"} dicts (raw/unscaled — display scaling is the caller's
        job) — the 4 leg fields are that same combined `gamma` value's own
        breakdown (same long_call/long_put/short_call/short_put split
        options.per_leg_greeks() uses), not a separate computation: `gamma`
        is literally their sum, so the split always reconciles exactly. Or
        (None, 0) for an unknown asset.

        Net position, capped by real open interest. Per instrument, buy
        volume minus sell volume (not raw cumulative volume) is what
        approximates current market positioning — a trader who bought 10
        then later sold 10 to close nets to 0, same as it should — but
        summed since the instrument's creation with no time bound, that
        net can in principle still exceed what's actually outstanding
        right now (e.g. from data gaps). Each instrument's net is clamped
        to dankbit.trade.get_open_interest_by_currency()'s real
        open_interest for that instrument (Deribit's own live count,
        fetched fresh — a single cached bulk call per asset, not
        per-instrument) whenever that instrument appears in the response;
        left unclamped if Deribit's response doesn't include it (treated
        as "unknown", not "zero", so a transient gap in that one response
        can't zero out a real position here)."""
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return None, 0

        cr = request.env.cr
        query_params = [f'%{asset}%']
        cutoff_sql = ""
        if expiry_exact:
            cutoff_sql = "AND expiration = %s"
            query_params.append(expiry_exact)
        elif expiry_cutoff:
            cutoff_sql = "AND expiration <= %s"
            query_params.append(expiry_cutoff)
        cr.execute(f"""
            SELECT name, strike, option_type, direction, expiration,
                   SUM(amount), SUM(iv * amount) / NULLIF(SUM(amount), 0), COUNT(*)
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= NOW()
              AND active = TRUE
              AND iv <> 0
              {cutoff_sql}
            GROUP BY name, strike, option_type, direction, expiration
        """, query_params)
        rows = cr.fetchall()
        trade_count = sum(int(row[7]) for row in rows)

        by_instrument = {}
        for name, strike, option_type, direction, expiration, amount, avg_iv, _count in rows:
            amount = float(amount)
            entry = by_instrument.setdefault(name, {
                "strike": strike, "option_type": option_type, "expiration": expiration,
                "buy_amount": 0.0, "sell_amount": 0.0, "iv_numerator": 0.0,
            })
            if direction == "buy":
                entry["buy_amount"] += amount
            else:
                entry["sell_amount"] += amount
            entry["iv_numerator"] += float(avg_iv or 0.01) * amount

        oi_map = request.env["dankbit.trade"].get_open_interest_by_currency(asset)

        agg_trades = []
        for name, e in by_instrument.items():
            net = e["buy_amount"] - e["sell_amount"]
            cap = oi_map.get(name)
            if cap is not None and abs(net) > cap:
                net = cap if net > 0 else -cap
            if net == 0:
                continue
            total_amount = e["buy_amount"] + e["sell_amount"]
            agg_trades.append(_AggTrade(
                strike=e["strike"], option_type=e["option_type"],
                direction="buy" if net > 0 else "sell",
                expiration=e["expiration"], amount=abs(net),
                iv=(e["iv_numerator"] / total_amount) if total_amount else 0.01,
            ))

        # Same long_call/long_put/short_call/short_put split
        # options.per_leg_greeks() uses — each instrument already landed in
        # exactly one bucket above (one option_type, one net direction), so
        # this is just not collapsing that split before summing, not a new
        # computation. `gamma` is the sum of the 4 legs (not a separate
        # portfolio_gamma() call over all of agg_trades) so the breakdown
        # always adds up to the combined value exactly, not just
        # approximately.
        leg_defs = (
            ("long_call", "buy", "call"), ("long_put", "buy", "put"),
            ("short_call", "sell", "call"), ("short_put", "sell", "put"),
        )
        legs = {
            leg_name: [t for t in agg_trades if t.direction == direction and t.option_type == option_type]
            for leg_name, direction, option_type in leg_defs
        }

        strikes = []
        for k in sorted({t.strike for t in agg_trades}):
            S = np.array([float(k)])
            entry = {"price": float(k)}
            for leg_name, leg_trades in legs.items():
                entry[leg_name] = float(gamma.portfolio_gamma(S, leg_trades, 0.05)[0]) if leg_trades else 0.0
            entry["gamma"] = entry["long_call"] + entry["long_put"] + entry["short_call"] + entry["short_put"]
            strikes.append(entry)
        return strikes, trade_count

    def _max_pain_for_expiry(self, asset, expiry_str):
        """Max Pain strike for `asset`'s option chain expiring at
        `expiry_str` (e.g. "25JUL26", the same day-suffix parsed out of
        the URL by gamma_by_strike_at_json) — feeds /mp/<instrument>'s
        orange Max Pain line. Built from
        dankbit.trade.get_open_interest_by_currency()'s real live open
        interest directly (the same bulk call _gamma_by_strike already
        makes), not from dankbit's own recorded trades — every strike
        with real outstanding OI belongs in the chain even if this addon
        never logged a trade there, unlike _gamma_by_strike's strike list
        (which only covers strikes it has trades for). Instrument names
        encode strike + option type (e.g. "BTC-25JUL26-98000-C"), parsed
        the same way dankbit.trade's own strike/option_type computed
        fields are. None if the chain has no open interest at all."""
        oi_map = request.env["dankbit.trade"].get_open_interest_by_currency(asset)
        prefix = f"{asset}-{expiry_str}-"
        call_oi, put_oi = {}, {}
        for name, oi in oi_map.items():
            if not name.startswith(prefix):
                continue
            parts = name.split("-")
            if len(parts) != 4:
                continue
            try:
                strike = int(parts[2])
            except ValueError:
                continue
            if name[-1] == "C":
                call_oi[strike] = call_oi.get(strike, 0.0) + oi
            elif name[-1] == "P":
                put_oi[strike] = put_oi.get(strike, 0.0) + oi
        strikes = sorted(set(call_oi) | set(put_oi))
        return options.max_pain(strikes, call_oi, put_oi)

    @http.route("/api/gamma-by-strike/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def gamma_by_strike_json(self, asset):
        """Per-strike combined portfolio dollar-gamma, every trade through
        expiry, all active expiries — feeds the Gamma Chart's "Top OI(s)"
        checkbox's "All" scope (see TradingView Chart Notes). See
        _gamma_by_strike()."""
        asset = asset.upper()
        strikes, trade_count = self._gamma_by_strike(asset)
        if strikes is None:
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        payload = {
            "asset": asset,
            "strikes": strikes,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/gamma-by-strike-until/<string:instrument>", type="http", auth="user", website=False, csrf=False)
    def gamma_by_strike_until_json(self, instrument):
        """Same per-strike computation as gamma_by_strike_json, but
        restricted to every active expiry up to and including `instrument`'s
        own day-suffix — expiration >= NOW() AND expiration <= <that
        expiry>, same day-suffix parsing as the rest of this file. One
        generic route reused for 2 different cutoffs by the caller passing
        a different instrument, not 2 near-duplicate routes. Feeds the
        Gamma Chart's "Top OI(s)" checkbox's "Weekly" (instrument=
        INSTRUMENT) and "Monthly" (instrument=MONTHLY_INST) scopes — one
        checkbox now fetches this route 2 times (plus gamma_by_strike_json
        once for "All") and, from each of the 3 resulting datasets, marks
        only the single highest-positive-gamma strike (see
        drawGammaTopLine in dankbit_templates.xml) rather than drawing
        every strike. Note these cutoffs are cumulative, not isolated to a
        single expiry — same "aggregate everything up to this date"
        convention the Weekly/Monthly bookmarks and the /i/<expiry> PNG
        route already use, not "only this one expiry's own trades." See
        _gamma_by_strike()."""
        parts = instrument.upper().split("-", 1)
        if len(parts) != 2:
            return request.make_response(
                json.dumps({"error": "Invalid instrument — expected ASSET-EXPIRY e.g. BTC-4JUL26"}),
                headers=[("Content-Type", "application/json")],
            )

        asset, expiry_str = parts
        try:
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
        except ValueError:
            return request.make_response(
                json.dumps({"error": "Invalid expiry format — expected DDMMMYY e.g. 4JUL26"}),
                headers=[("Content-Type", "application/json")],
            )

        strikes, trade_count = self._gamma_by_strike(asset, expiry_cutoff=expiry_dt)
        if strikes is None:
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        payload = {
            "asset": asset,
            "expiry": expiry_str,
            "strikes": strikes,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/gamma-by-strike-at/<string:instrument>", type="http", auth="user", website=False, csrf=False)
    def gamma_by_strike_at_json(self, instrument):
        """Same per-strike computation as gamma_by_strike_json, but
        isolated to trades whose expiration exactly matches `instrument`'s
        own day-suffix — expiration = <that expiry> alone, not
        expiration <= <that expiry> the way gamma_by_strike_until_json
        works. Feeds /mp/<instrument>'s "Strike Gamma" indicator
        (gamma_by_strike_chart) — every strike drawn, restricted to just
        that one instrument's own trades, not folded in with any sooner
        expiry's the way every gamma_by_strike_until_json caller (Gamma
        Tops' Nearest/Nearest+1/+2/+3/Weekly/Monthly scopes) is. See
        _gamma_by_strike()."""
        parts = instrument.upper().split("-", 1)
        if len(parts) != 2:
            return request.make_response(
                json.dumps({"error": "Invalid instrument — expected ASSET-EXPIRY e.g. BTC-4JUL26"}),
                headers=[("Content-Type", "application/json")],
            )

        asset, expiry_str = parts
        try:
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
        except ValueError:
            return request.make_response(
                json.dumps({"error": "Invalid expiry format — expected DDMMMYY e.g. 4JUL26"}),
                headers=[("Content-Type", "application/json")],
            )

        strikes, trade_count = self._gamma_by_strike(asset, expiry_exact=expiry_dt)
        if strikes is None:
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        payload = {
            "asset": asset,
            "expiry": expiry_str,
            "strikes": strikes,
            "trade_count": trade_count,
            "max_pain": self._max_pain_for_expiry(asset, expiry_str),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
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

        # In trailing-window preview mode the structural Green/Red Bands
        # remain the persisted session-confirmed values, while Gamma and
        # Smart Liquidity are replaced by a fresh computation using only
        # the selected option-flow window.  Nothing is persisted here.
        if hours is not None:
            by_instrument = {row["instrument"]: row for row in series}
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

    @staticmethod
    def _gamma_triple_dominant_leg(data):
        """Which of the 4 legs (Buyer Call/Buyer Put/Seller Call/Seller
        Put) dominates a dankbit.bands._compute_asset() result's own
        gamma_band average — the leg whose bcg_abs/bpg_abs/scg_abs/spg_abs
        (already same-scale abs(gamma_value)/1e6 magnitudes, see
        forecast.per_leg_greeks()) is largest, same "biggest |value| wins"
        rule /mp/<instrument>'s own dominantLeg() applies per-strike, just
        against this expiry's 4 aggregate leg extrema instead of one
        strike's 4 leg contributions. Returns a `(leg, value)` tuple —
        "LC"/"LP"/"SC"/"SP" plus that leg's own Abs value, rounded to match
        the /<instrument>/zones page's own Abs. lines convention — or
        `(None, None)` if `data` is falsy (nothing computable at that
        expiry_index)."""
        if not data:
            return None, None
        legs = {
            "LC": data["bcg_abs"], "LP": data["bpg_abs"],
            "SC": data["scg_abs"], "SP": data["spg_abs"],
        }
        leg = max(legs, key=legs.get)
        return leg, round(legs[leg])

    @http.route("/api/gamma-triple/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def gamma_triple_json(self, asset):
        """/gt/<asset>'s own and only endpoint — a single current
        snapshot (not a time series; nothing here is ever persisted),
        computed fresh on every request straight off
        dankbit.bands._compute_asset() (reused unmodified), same
        live-compute-nothing-persisted pattern /api/zones-box/<asset>
        uses: nearest/nearest+1/nearest+2 expiry gamma_band, all over the
        same trailing-hours window, user-selectable via the page's own
        "Window" dropdown: an optional `?hours=` query param, restricted to
        Y_CHART_WINDOW_HOURS_CHOICES (any other/missing value falls
        back to Y_CHART_DEFAULT_WINDOW_HOURS, same defensive restriction
        pattern the dropdown itself enforces client-side). "nearest+1"/
        "nearest+2" are simply `_compute_asset(asset, expiry_index=1, ...)`/
        `_compute_asset(asset, expiry_index=2, ...)` — the active expiries
        right after the nearest one — same ordinal-index call as
        "nearest" (`expiry_index=0`). Each line's own dominant leg
        (`_gamma_triple_dominant_leg()` — "LC"/"LP"/"SC"/"SP", whichever of
        that expiry's 4 gamma extrema has the largest abs magnitude, plus
        that leg's own rounded Abs value) is also returned, so the page's
        own price-line titles can show which leg is driving that expiry's
        gamma_band average, and by how much. This route
        originally also reported weekly/monthly expiry gamma_band
        (resolved via a since-removed `_y_chart_expiry_index_for_instrument`
        /`_y_chart_gamma_band_for_instrument` pair that turned the
        configured weekly_expiry/monthly_expiry instrument string into an
        ordinal expiry_index); both fields and their /gt/<asset> price
        lines were removed per product decision. No model/table backs
        this at all."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        hours_param = request.httprequest.args.get("hours")
        try:
            hours = int(hours_param)
        except (TypeError, ValueError):
            hours = None
        if hours not in Y_CHART_WINDOW_HOURS_CHOICES:
            hours = Y_CHART_DEFAULT_WINDOW_HOURS

        nearest_data = request.env["dankbit.bands"]._compute_asset(
            asset, expiry_index=0, hours=hours,
        )
        nearest_instrument = nearest_data["instrument"] if nearest_data else None
        nearest_gamma = nearest_data["gamma_band"] if nearest_data else None
        nearest_dominant_leg, nearest_dominant_leg_value = self._gamma_triple_dominant_leg(nearest_data)

        nearest_plus_1_data = request.env["dankbit.bands"]._compute_asset(
            asset, expiry_index=1, hours=hours,
        )
        nearest_plus_1_instrument = nearest_plus_1_data["instrument"] if nearest_plus_1_data else None
        nearest_plus_1_gamma = nearest_plus_1_data["gamma_band"] if nearest_plus_1_data else None
        nearest_plus_1_dominant_leg, nearest_plus_1_dominant_leg_value = self._gamma_triple_dominant_leg(nearest_plus_1_data)

        nearest_plus_2_data = request.env["dankbit.bands"]._compute_asset(
            asset, expiry_index=2, hours=hours,
        )
        nearest_plus_2_instrument = nearest_plus_2_data["instrument"] if nearest_plus_2_data else None
        nearest_plus_2_gamma = nearest_plus_2_data["gamma_band"] if nearest_plus_2_data else None
        nearest_plus_2_dominant_leg, nearest_plus_2_dominant_leg_value = self._gamma_triple_dominant_leg(nearest_plus_2_data)

        payload = {
            "asset": asset,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "nearest_instrument": nearest_instrument,
            "nearest_gamma_band": nearest_gamma or 0.0,
            "nearest_dominant_leg": nearest_dominant_leg,
            "nearest_dominant_leg_value": nearest_dominant_leg_value,
            "nearest_plus_1_instrument": nearest_plus_1_instrument,
            "nearest_plus_1_gamma_band": nearest_plus_1_gamma or 0.0,
            "nearest_plus_1_dominant_leg": nearest_plus_1_dominant_leg,
            "nearest_plus_1_dominant_leg_value": nearest_plus_1_dominant_leg_value,
            "nearest_plus_2_instrument": nearest_plus_2_instrument,
            "nearest_plus_2_gamma_band": nearest_plus_2_gamma or 0.0,
            "nearest_plus_2_dominant_leg": nearest_plus_2_dominant_leg,
            "nearest_plus_2_dominant_leg_value": nearest_plus_2_dominant_leg_value,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/gt/<string:asset>", type="http", auth="user", website=True)
    def gamma_triple_chart(self, asset):
        """Minimal TradingView chart — candles plus 3 horizontal price
        lines (nearest/nearest+1/nearest+2 expiry gamma_band) over the
        same trailing-hours window, user-selectable via the page's own
        "Window" dropdown (12/24/48, default
        Y_CHART_DEFAULT_WINDOW_HOURS=12 — see gamma_triple_json) via
        dankbit.bands._compute_asset() (reused unmodified), all 3
        recomputed live on every poll, nothing persisted, so
        /chart/<asset>, /oi/<asset>, and /mp/<instrument> are all
        completely unaffected. Renders its own standalone template
        (dankbit_gamma_triple_chart). Polls on the general
        dankbit.refresh_interval (not zones_box_refresh_interval) so the
        lines visibly move on the same cadence the user configures for
        every other page's own refresh rate."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        ctx = {"asset": asset, "refresh_interval": refresh_interval}
        return request.render("dankbit.dankbit_gamma_triple_chart", ctx)

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
        of get_candles() (Binance spot). Used by /gt/<asset>'s,
        /4l/<asset>'s, and /mw/<asset>'s own candle series, per product
        decision to keep those three pages on Deribit's own perpetuals
        rather than switching every TradingView page's candle source."""
        candles = request.env["dankbit.trade"].get_candles_deribit_perpetual(asset, interval=interval, limit=int(limit))
        candles = candles[::-1]  # newest-first for frontend
        return request.make_response(
            json.dumps({"result": candles}),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/four-leg-gamma/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def four_leg_gamma_json(self, asset):
        """Computed fresh on every request — no model/table behind this
        route (same live-compute-nothing-persisted pattern
        /api/gamma-triple/<asset> uses): 4-leg gamma extrema (BCG/BPG/
        SCG/SPG) over `asset`'s own trailing trades, restricted to a
        single expiry chosen via the page's own "Expiry" dropdown — an
        optional `?expiry=` query param, one of "nearest" (default — the
        currently soonest-expiring active instrument), "nearest_plus_1", or
        "nearest_plus_2" (the 1st/2nd active expiry after the nearest one —
        same ordinal notion /api/gamma-triple/<asset>'s own nearest+1/
        nearest+2 lines use), mapped to an ordinal index (0/1/2) and
        resolved via dankbit.bands._distinct_expirations()/
        _format_instrument() directly rather than
        dankbit.bands._compute_asset(), since this route only needs the
        instrument string, not a full curve build — any other/missing
        value falls back to "nearest". This route originally also offered
        "weekly"/"monthly" (the configured weekly_expiry/monthly_expiry
        instrument) and "all" (every active instrument for `asset`, no
        expiry restriction — this route's original, pre-dropdown
        behavior); all 3 were removed per product decision in favor of
        "nearest_plus_1"/"nearest_plus_2". The trailing-hours trade window
        is independently user-selectable via the page's own "Window"
        dropdown — an optional `?hours=` query param, restricted to
        FOUR_LEG_WINDOW_HOURS_CHOICES (8/12/24 — or the literal string
        "all", skipping the trailing-hours trade filter entirely; any
        other/missing value falls back to
        FOUR_LEG_DEFAULT_WINDOW_HOURS="all" — this page's own choice
        set/default, split out from /api/gamma-triple/<asset>'s own
        Y_CHART_WINDOW_HOURS_CHOICES/Y_CHART_DEFAULT_WINDOW_HOURS once
        this page's own set grew past that shared 12/24/48 tuple, since
        the two pages' choice sets are independent product decisions).
        48h/72h were offered at one point and removed per product
        decision; `?hours=all` was removed alongside them and later
        re-added per a later product decision; 1h/2h/3h/4h/5h/6h/7h/8h
        were removed in a further round, leaving 12h/24h/All, and the
        default moved from 8h to "All" at that point; 8h was then
        re-added per a still later product decision (default stayed
        "All"). Trades are restricted
        to the
        resolved instrument via the same anchored `=ilike` domain
        chart_png_zones uses (`f"{instrument}-%"`, left-prefix match so
        one expiry's query can never pull in another's trades). Computed
        via options.per_leg_greeks() — the single source of truth for this
        computation, also used by dankbit.bands/dankbit.forecast.snapshot/
        chart_png_zones. Feeds /4l/<asset>'s own 4 horizontal gamma-price
        lines. No points at all (same nothing-computable-yet convention
        every other route in this addon follows) when nothing is active at
        that ordinal position. `points` holds exactly one (current)
        reading, kept as a list for shape-compatibility with the page's
        own existing points[points.length-1] read."""
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
        expiry_mode = (request.httprequest.args.get("expiry") or "").lower()
        if expiry_mode not in expiry_ordinals:
            expiry_mode = "nearest"
        expiry_index = expiry_ordinals[expiry_mode]

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

        bands_model = request.env["dankbit.bands"]
        instrument = None
        expirations = bands_model._distinct_expirations(asset, as_of, expiry_index + 1)
        if len(expirations) > expiry_index:
            instrument = bands_model._format_instrument(asset, expirations[expiry_index])
        name_prefix = instrument

        trades = request.env["dankbit.trade"]
        if name_prefix:
            domain = [("name", "=ilike", f"{name_prefix}-%")]
            if hours != "all":
                window_start = as_of - timedelta(hours=hours)
                domain += [("deribit_ts", ">=", window_start), ("deribit_ts", "<=", as_of)]
            trades = trades.with_context(active_test=False).search(domain)

        points = []
        if trades:
            STs = np.arange(from_price, to_price, step, dtype=np.float64)
            legs = options.per_leg_greeks(STs, trades)
            # A leg with zero trades reports gamma_price as None (no curve
            # to peak/bottom at) — collapsed to 0.0 here, same "0.0 =
            # absent" sentinel the client's own `if (latest.bcg_price)`
            # falsy checks (and its AVG-line filter(Boolean)) already
            # expect (dankbit_four_leg_gamma_chart_templates.xml).
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
        own "Expiry" dropdown (Nearest/Nearest+1/Nearest+2, Nearest default
        — this route originally also offered Weekly/Monthly/All, removed
        per product decision in favor of Nearest+1/Nearest+2, same
        nearest+1/nearest+2 ordinal notion /gt/<asset>'s own 2nd/3rd price
        lines use), and own "Window"
        dropdown (8h/12h/24h/All — FOUR_LEG_WINDOW_HOURS_
        CHOICES plus the "All" no-window-bound option, split out from
        /gt/<asset>'s own Y_CHART_WINDOW_HOURS_CHOICES (12/24/48) once
        this page's own set grew, "All" default, independent of the
        "Expiry" dropdown; 16h/20h/48h/72h/All were offered at one point
        and removed per product decision; 3h/5h/7h were added later,
        filling in the gaps left in the original 1h/2h/4h/6h/8h/12h set,
        and 24h and "All" were both re-added afterwards per later
        product decisions; 1h/2h/3h/4h/5h/6h/7h/8h were then removed in
        a further round, leaving only 12h/24h/All, and the default moved
        from 8h to "All" at that point; 8h was then re-added per a still
        later product decision (default stayed "All"); see
        four_leg_gamma_json for how each option resolves). A vertical
        marker line showing where the selected Window's trailing-hours
        cutoff falls used to be drawn on the candle chart (#window-vline)
        but was removed per product decision.
        Renders its own standalone template
        (dankbit_four_leg_gamma_chart). Polls on the general
        dankbit.refresh_interval, same as every other page's own refresh
        rate."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        ctx = {"asset": asset, "refresh_interval": refresh_interval}
        return request.render("dankbit.dankbit_four_leg_gamma_chart", ctx)

    @http.route("/api/mw-gamma/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def mw_gamma_json(self, asset):
        """Computed fresh on every request — no model/table behind this
        route (same live-compute-nothing-persisted pattern
        /api/four-leg-gamma/<asset> uses): 4-leg gamma extrema (BCG/BPG/
        SCG/SPG) via options.per_leg_greeks(), for /mw/<asset>'s own
        "Expiry" dropdown — an optional `?expiry=` query param, one of
        "weekly" (default) or "monthly", resolved against the configured
        weekly_expiry/monthly_expiry instrument for `asset` (eth_-prefixed
        for ETH, same convention _build_tv_chart_context() uses) — any
        other/missing value falls back to "weekly".

        Unlike /api/four-leg-gamma/<asset> (isolated to one exact
        instrument's own trades via an anchored `name` match), the trade
        domain here is CUMULATIVE through the selected expiry — every
        active instrument for `asset` whose own expiration is <= that
        expiry's day-suffix (same expiration-column cutoff
        gamma_by_strike_until_json/_gamma_by_strike use for their own
        Weekly/Monthly scopes, not a per-instrument name match) — further
        restricted, same query-param convention as /api/four-leg-gamma/
        <asset>'s own "Window" dropdown, to a trailing-hours trade window
        (?hours=, one of MW_WINDOW_HOURS_CHOICES — 12/24 — or the literal
        string "all", skipping the `deribit_ts` filter entirely; falling
        back to MW_DEFAULT_WINDOW_HOURS="all" for any other/missing
        value). The no-window-bound "All" option existed at one point and
        was removed per product decision along with 12h/16h/20h/48h/72h,
        then re-added per a later product decision; 1h/2h/4h/6h/8h were
        removed in a further round, leaving 12h/24h/All.
        Feeds
        /mw/<asset>'s 4 horizontal gamma-price lines. No points at all
        (same nothing-computable-yet convention every other route in this
        addon follows) when the selected expiry isn't configured for
        `asset`, is malformed, or has no matching trades in the resolved
        window."""
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

        expiry_mode = (request.httprequest.args.get("expiry") or "").lower()
        if expiry_mode not in ("weekly", "monthly"):
            expiry_mode = "weekly"
        if asset == "ETH":
            expiry_param = "dankbit.eth_weekly_expiry" if expiry_mode == "weekly" else "dankbit.eth_monthly_expiry"
        else:
            expiry_param = "dankbit.weekly_expiry" if expiry_mode == "weekly" else "dankbit.monthly_expiry"
        instrument = icp.get_param(expiry_param, default="").upper()

        # "all" (?hours=all) skips the trailing-hours trade filter entirely
        # — checked before the int() parse below so it isn't mistaken for
        # a malformed value and overwritten with the default (see
        # four_leg_gamma_json's own identical handling).
        hours_param = request.httprequest.args.get("hours")
        if hours_param == "all":
            hours = "all"
        else:
            try:
                hours = int(hours_param)
            except (TypeError, ValueError):
                hours = None
            if hours not in MW_WINDOW_HOURS_CHOICES:
                hours = MW_DEFAULT_WINDOW_HOURS

        as_of = datetime.now(timezone.utc).replace(tzinfo=None)

        # Naive UTC, same as every other `expiration` domain comparison
        # in this file (see chart_png_until's own as_of/window_start
        # above) — the ORM's Datetime fields are stored naive.
        expiry_dt = None
        parts = instrument.split("-", 1) if instrument else []
        if len(parts) == 2:
            try:
                expiry_dt = datetime.strptime(parts[1], "%d%b%y").replace(hour=8)
            except ValueError:
                expiry_dt = None

        points = []
        if expiry_dt:
            domain = [
                ("name", "=ilike", f"{asset}-%"),
                ("expiration", ">=", as_of),
                ("expiration", "<=", expiry_dt),
            ]
            if hours != "all":
                window_start = as_of - timedelta(hours=hours)
                domain += [("deribit_ts", ">=", window_start), ("deribit_ts", "<=", as_of)]
            trades = request.env["dankbit.trade"].search(domain)
            if trades:
                STs = np.arange(from_price, to_price, step, dtype=np.float64)
                legs = options.per_leg_greeks(STs, trades)
                # See four_leg_gamma_json's own comment above — a leg with
                # zero trades reports gamma_price as None, collapsed to 0.0
                # here to match the client's existing 0.0-means-absent
                # falsy checks.
                points.append({
                    "t": int(as_of.replace(tzinfo=timezone.utc).timestamp() * 1000),
                    "trade_count": len(trades),
                    "bcg_price": legs["long_call"]["gamma_price"] or 0.0, "bcg_value": legs["long_call"]["gamma_value"],
                    "bpg_price": legs["long_put"]["gamma_price"] or 0.0, "bpg_value": legs["long_put"]["gamma_value"],
                    "scg_price": legs["short_call"]["gamma_price"] or 0.0, "scg_value": legs["short_call"]["gamma_value"],
                    "spg_price": legs["short_put"]["gamma_price"] or 0.0, "spg_value": legs["short_put"]["gamma_value"],
                })

        payload = {
            "asset": asset, "instrument": instrument or None, "expiry_mode": expiry_mode,
            "window_hours": hours, "points": points,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/mw/<string:asset>", type="http", auth="user", website=True)
    def mw_gamma_chart(self, asset):
        """Standalone TradingView page — same shape as /4l/<asset> (own
        template, Deribit perpetual-futures candles via
        get_candles_deribit_perpetual()/api/klines-futures/<asset>, no
        model/table behind this page, refreshed live on every poll), but
        its own "Expiry" dropdown offers Weekly/Monthly (the configured
        weekly_expiry/monthly_expiry instrument for `asset`, Monthly
        default) instead of Nearest/Nearest+1/Nearest+2, and the
        underlying trade domain for the 4 gamma legs is CUMULATIVE
        through the selected expiry rather than isolated to one
        instrument — see mw_gamma_json. Own "Window" dropdown
        (12h/24h/All — MW_WINDOW_HOURS_CHOICES plus the "All"
        no-window-bound option, kept as its own constant/product decision
        independent of /4l/<asset>'s own "Window" dropdown
        (FOUR_LEG_WINDOW_HOURS_CHOICES, which additionally offers 8h,
        re-added there per a later product decision that did not apply
        to this page — the two pages' dropdowns were both reduced to
        12h/24h/All independently and aren't aliased to each other, so
        one page's dropdown changing doesn't imply the other's does too)
        — "All" default, MW_DEFAULT_WINDOW_HOURS; 1h/2h/4h/6h/8h were
        removed from this page's own dropdown per product decision, at
        which point the default moved from 4h to "All")
        and "Timeframe" dropdown (15m/1h/4h/1d, same options as
        /4l/<asset>'s own, but 1d default here — an independent product
        decision from that page's own 4h default). Renders its own standalone template
        (dankbit_mw_gamma_chart). Polls on the general
        dankbit.refresh_interval, same as every other page's own refresh
        rate."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        ctx = {"asset": asset, "refresh_interval": refresh_interval}
        return request.render("dankbit.dankbit_mw_gamma_chart", ctx)

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
            if preview:
                anchors[expiry_index] = {
                    "open": preview["forecast_open"], "high": preview["forecast_high"],
                    "low": preview["forecast_low"], "close": preview["forecast_close"],
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

    # ------------------------------------------------------------------
    # TradingView Lightweight Charts pages
    # ------------------------------------------------------------------

    def _build_tv_chart_context(self, asset):
        """Shared context-building for /chart/<asset>, /oi/<asset>, and
        /mp/<instrument> — all three render the same
        dankbit_tv_chart_until template; /oi/<asset> additionally sets
        show_gamma_point so the template also draws the "Top OI(s)"
        indicator, and /mp/<instrument> additionally sets
        show_strike_gamma + strike_gamma_instrument so the template draws
        the restored per-strike "Strike Gamma" lines instead (see
        gamma_by_strike_chart). Returns (context, None) on success or
        (None, error_message) if the weekly expiry isn't configured/valid
        for `asset`, so callers can render that as a plain text response
        the same way this route always has."""
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

    @http.route("/oi/<string:asset>", type="http", auth="user", website=True)
    def oi_chart_tv(self, asset):
        """Same page as /chart/<asset> (identical template/context), plus the
        "Top OI(s)" indicator — /chart/<asset> itself is
        unaffected, it always passes show_gamma_point=false. Every route in
        this addon is auth="user"; a logged-out request to any of them is
        redirected to the login page instead of rendering/responding."""
        asset = asset.upper()

        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        ctx, error = self._build_tv_chart_context(asset)
        if error:
            return request.make_response(error, headers=[("Content-Type", "text/plain")])

        ctx["show_gamma_point"] = "true"
        return request.render("dankbit.dankbit_tv_chart_until", ctx)

    @http.route("/mp/<string:instrument>", type="http", auth="user", website=True)
    def gamma_by_strike_chart(self, instrument):
        """Minimal TradingView chart (candles only, same template/context as
        /chart/<asset> and /oi/<asset>) plus the restored "Strike Gamma"
        indicator — one price line per distinct strike that has ever
        traded, gray-to-black by |gamma| magnitude, signed dollar-gamma
        title plus dominant-leg suffix (see drawStrikeGammaLines in
        dankbit_templates.xml). Unlike /oi/<asset>'s "Top OI(s)" (one line
        per scope, top strike only), this draws every strike, isolated to
        trades whose expiration exactly matches `instrument`'s own expiry —
        not folded in with any sooner expiry's the way every
        gamma-by-strike-until scope (Top OI(s)' Nearest/Weekly/Monthly/
        etc.) is. Client-side fetches /api/gamma-by-strike-at/<instrument>
        (gamma_by_strike_at_json -> _gamma_by_strike(..., expiry_exact=...)),
        a sibling of gamma_by_strike_until_json rather than that same route,
        precisely so this page's isolated scope can't leak into Top OI(s)'
        intentionally cumulative one. `instrument` is a full Deribit-style
        string, e.g. BTC-25JUL26 — same ASSET-DDMMMYY parsing
        gamma_by_strike_until_json uses."""
        instrument = instrument.upper()
        parts = instrument.split("-", 1)
        if len(parts) != 2:
            return request.make_response(
                "Invalid instrument — expected ASSET-EXPIRY e.g. BTC-4JUL26",
                headers=[("Content-Type", "text/plain")],
            )
        asset, expiry_str = parts
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()
        try:
            datetime.strptime(expiry_str, "%d%b%y")
        except ValueError:
            return request.make_response(
                "Invalid expiry format — expected DDMMMYY e.g. 4JUL26",
                headers=[("Content-Type", "text/plain")],
            )

        ctx, error = self._build_tv_chart_context(asset)
        if error:
            return request.make_response(error, headers=[("Content-Type", "text/plain")])

        ctx["show_strike_gamma"] = "true"
        ctx["strike_gamma_instrument"] = instrument
        return request.render("dankbit.dankbit_tv_chart_until", ctx)
