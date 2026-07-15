import base64
import json
import requests as _requests
import numpy as np
from datetime import datetime, timedelta, timezone
from io import BytesIO
from matplotlib import transforms as mtransforms
from odoo import http
from odoo.http import request
from . import options
from . import delta
from . import gamma
from . import theta


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
    @http.route("/help", auth="public", type="http", website=True)
    def help_page(self):
        return request.render("dankbit.dankbit_help")

    @http.route("/<string:instrument>/s", type="http", auth="public", website=True)
    def chart_slideshow(self, instrument):
        return request.render("dankbit.dankbit_slideshow", {
            "instrument": instrument,
            "hours_list": [0, 4, 8, 12, 24],
        })

    @http.route("/<string:instrument>/<int:hours>", type="http", auth="public", website=True)
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
            ("expiration", ">=", datetime.now()),
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

    @http.route("/<string:instrument>/zones", type="http", auth="public", website=True)
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

        midnight_utc = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).strftime("%Y-%m-%d %H:%M:%S")
        domain = [
            ("name", "=ilike", f"{instrument}-%"),
            ("expiration", ">=", datetime.now()),
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

        # Short Max/Long Min/box info used to be drawn inside the PNG itself
        # (matplotlib ax.text) — now rendered as page HTML (top-left overlay,
        # see dankbit_page template) instead, off the same summary dankbit.
        # zones.extrema uses, so the two can never disagree.
        summary = options.zone_summary(longs_obj.STs, longs_obj.payoffs, shorts_obj.payoffs)

        def _format_box(box):
            # box is None (no crossing at all), or a (low, high) pair that
            # collapses to a single price when only one curve contributed a
            # crossing — shown as one number rather than a zero-width range.
            if box is None:
                return "n/a"
            low, high = box
            if low == high:
                return "${:,.0f}".format(low)
            return "${:,.0f} - ${:,.0f}".format(low, high)

        top_box = _format_box(summary["top_box"])
        bottom_box = _format_box(summary["bottom_box"])
        top_intersection = "n/a" if summary["top_intersection"] is None else "${:,.0f}".format(summary["top_intersection"])
        bottom_intersection = "n/a" if summary["bottom_intersection"] is None else "${:,.0f}".format(summary["bottom_intersection"])

        # Long call trades since 00:00 UTC, restricted to the single nearest
        # (soonest-to-expire) expiry among `trades` — same "next expiry only"
        # restriction dankbit.zones.extrema uses, in case `instrument` isn't
        # already a single fully-qualified expiry. Price where dollar gamma
        # peaks over the same zoomed price grid the zones curves use.
        next_expiration = min(trades.mapped("expiration")) if trades else None
        long_calls = trades.filtered(
            lambda t: t.direction == "buy" and t.option_type == "call" and t.expiration == next_expiration
        )
        long_call_gamma_curve = gamma.portfolio_gamma(longs_obj.STs, long_calls)
        long_call_gamma_peak_index = int(np.argmax(long_call_gamma_curve))
        long_call_gamma_peak_price = float(longs_obj.STs[long_call_gamma_peak_index])
        long_call_gamma_peak_value = float(long_call_gamma_curve[long_call_gamma_peak_index])

        long_puts = trades.filtered(
            lambda t: t.direction == "buy" and t.option_type == "put" and t.expiration == next_expiration
        )
        long_put_gamma_curve = gamma.portfolio_gamma(longs_obj.STs, long_puts)
        long_put_gamma_peak_index = int(np.argmax(long_put_gamma_curve))
        long_put_gamma_peak_price = float(longs_obj.STs[long_put_gamma_peak_index])
        long_put_gamma_peak_value = float(long_put_gamma_curve[long_put_gamma_peak_index])

        # Short positions carry negative gamma (portfolio_gamma's sign for
        # "sell" is -1), so the relevant extremum is where the curve bottoms
        # out (argmin), not peaks.
        short_calls = trades.filtered(
            lambda t: t.direction == "sell" and t.option_type == "call" and t.expiration == next_expiration
        )
        short_call_gamma_curve = gamma.portfolio_gamma(longs_obj.STs, short_calls)
        short_call_gamma_bottom_index = int(np.argmin(short_call_gamma_curve))
        short_call_gamma_bottom_price = float(longs_obj.STs[short_call_gamma_bottom_index])
        short_call_gamma_bottom_value = float(short_call_gamma_curve[short_call_gamma_bottom_index])

        short_puts = trades.filtered(
            lambda t: t.direction == "sell" and t.option_type == "put" and t.expiration == next_expiration
        )
        short_put_gamma_curve = gamma.portfolio_gamma(longs_obj.STs, short_puts)
        short_put_gamma_bottom_index = int(np.argmin(short_put_gamma_curve))
        short_put_gamma_bottom_price = float(longs_obj.STs[short_put_gamma_bottom_index])
        short_put_gamma_bottom_value = float(short_put_gamma_curve[short_put_gamma_bottom_index])

        # Delta-saturation asset prices - same options.delta_saturation_price()
        # dankbit.zones.extrema's own delta_band uses, against this same
        # next-expiry-only long_calls/long_puts/short_calls/short_puts, so
        # this page can never disagree with delta_band on these 4 points.
        # Calls saturate ITM at high S ('max' edge), puts at low S ('min'
        # edge), independent of long/short.
        long_call_delta_price = options.delta_saturation_price(
            longs_obj.STs, long_calls, self.DELTA_SATURATION_FRACTION, "max")
        long_put_delta_price = options.delta_saturation_price(
            longs_obj.STs, long_puts, self.DELTA_SATURATION_FRACTION, "min")
        short_call_delta_price = options.delta_saturation_price(
            longs_obj.STs, short_calls, self.DELTA_SATURATION_FRACTION, "max")
        short_put_delta_price = options.delta_saturation_price(
            longs_obj.STs, short_puts, self.DELTA_SATURATION_FRACTION, "min")

        # Portfolio-delta value at each saturation price above — interpolated
        # off the same curve delta_saturation_price() searches (rather than
        # just DELTA_SATURATION_FRACTION * the curve's own edge value), so
        # the fallback case (curve never reaches the 90% threshold in this
        # window, saturation price falls back to the STs edge) still reports
        # the real value at that price instead of a threshold never reached.
        long_call_delta_value = float(np.interp(
            long_call_delta_price, longs_obj.STs, delta.portfolio_delta(longs_obj.STs, long_calls)))
        long_put_delta_value = float(np.interp(
            long_put_delta_price, longs_obj.STs, delta.portfolio_delta(longs_obj.STs, long_puts)))
        short_call_delta_value = float(np.interp(
            short_call_delta_price, longs_obj.STs, delta.portfolio_delta(longs_obj.STs, short_calls)))
        short_put_delta_value = float(np.interp(
            short_put_delta_price, longs_obj.STs, delta.portfolio_delta(longs_obj.STs, short_puts)))

        # Price where each leg's portfolio theta is most extreme over the
        # same zoomed price grid gamma/delta use — mirroring gamma's
        # peak/bottom pattern, but sign-flipped: long positions carry
        # negative theta (decay cost), so their extremum is the trough
        # (argmin, worst decay); short positions carry positive theta
        # (decay gain), so theirs is the peak (argmax, best decay). r=0.0
        # to match every other Greek computed on this page (see delta/gamma
        # above — zones deliberately doesn't use the r=0.05 the
        # combined-portfolio routes use).
        long_call_theta_curve = theta.portfolio_theta(longs_obj.STs, long_calls)
        long_call_theta_index = int(np.argmin(long_call_theta_curve))
        long_call_theta_price = float(longs_obj.STs[long_call_theta_index])
        long_call_theta_value = float(long_call_theta_curve[long_call_theta_index])

        long_put_theta_curve = theta.portfolio_theta(longs_obj.STs, long_puts)
        long_put_theta_index = int(np.argmin(long_put_theta_curve))
        long_put_theta_price = float(longs_obj.STs[long_put_theta_index])
        long_put_theta_value = float(long_put_theta_curve[long_put_theta_index])

        short_call_theta_curve = theta.portfolio_theta(longs_obj.STs, short_calls)
        short_call_theta_index = int(np.argmax(short_call_theta_curve))
        short_call_theta_price = float(longs_obj.STs[short_call_theta_index])
        short_call_theta_value = float(short_call_theta_curve[short_call_theta_index])

        short_put_theta_curve = theta.portfolio_theta(longs_obj.STs, short_puts)
        short_put_theta_index = int(np.argmax(short_put_theta_curve))
        short_put_theta_price = float(longs_obj.STs[short_put_theta_index])
        short_put_theta_value = float(short_put_theta_curve[short_put_theta_index])

        # Each line is {text, color} — color is None for the default
        # (black) styling every line used before per-line colors were
        # needed; only section headers like "Gamma" below set one.
        def _line(text, color=None):
            return {"text": text, "color": color}

        zone_info_lines = [
            _line("Short Max: ${:,.0f}".format(summary["short_max_price"])),
            _line("Long Min: ${:,.0f}".format(summary["long_min_price"])),
            _line(" "),  # blank spacer line — a truly empty div collapses to zero height
            _line(f"Top Box: {top_box}"),
            _line(f"Bottom Box: {bottom_box}"),
            _line(" "),
            _line(f"Top Intersection: {top_intersection}"),
            _line(f"Bottom Intersection: {bottom_intersection}"),
            _line(" "),
            _line("Gamma", color="violet"),
            _line("Long Call Gamma Peak: ${:,.0f}".format(long_call_gamma_peak_price)),
            _line("Long Put Gamma Peak: ${:,.0f}".format(long_put_gamma_peak_price)),
            _line("Short Call Gamma Bottom: ${:,.0f}".format(short_call_gamma_bottom_price)),
            _line("Short Put Gamma Bottom: ${:,.0f}".format(short_put_gamma_bottom_price)),
            _line(" "),
            _line("Long Call Gamma Peak Value: {:,.0f}".format(abs(long_call_gamma_peak_value) / 1_000_000)),
            _line("Long Put Gamma Peak Value: {:,.0f}".format(abs(long_put_gamma_peak_value) / 1_000_000)),
            _line("Short Call Gamma Bottom Value: {:,.0f}".format(abs(short_call_gamma_bottom_value) / 1_000_000)),
            _line("Short Put Gamma Bottom Value: {:,.0f}".format(abs(short_put_gamma_bottom_value) / 1_000_000)),
            _line(" "),
            _line("Delta", color="green"),
            _line("Long Call Delta: ${:,.0f}".format(long_call_delta_price)),
            _line("Long Put Delta: ${:,.0f}".format(long_put_delta_price)),
            _line("Short Call Delta: ${:,.0f}".format(short_call_delta_price)),
            _line("Short Put Delta: ${:,.0f}".format(short_put_delta_price)),
            _line(" "),
            _line("Long Call Delta Value: {:,.0f}".format(abs(long_call_delta_value) / 10)),
            _line("Long Put Delta Value: {:,.0f}".format(abs(long_put_delta_value) / 10)),
            _line("Short Call Delta Value: {:,.0f}".format(abs(short_call_delta_value) / 10)),
            _line("Short Put Delta Value: {:,.0f}".format(abs(short_put_delta_value) / 10)),
        ]

        # Theta gets its own top-right overlay (see .zone-info-right in
        # dankbit_page) rather than sitting in the top-left zone_info_lines
        # column with everything else.
        theta_info_lines = [
            _line("Theta", color="orange"),
            _line("Long Call Theta: ${:,.0f}".format(long_call_theta_price)),
            _line("Long Put Theta: ${:,.0f}".format(long_put_theta_price)),
            _line("Short Call Theta: ${:,.0f}".format(short_call_theta_price)),
            _line("Short Put Theta: ${:,.0f}".format(short_put_theta_price)),
            _line(" "),
            _line("Long Call Theta Value: {:,.0f}".format(abs(long_call_theta_value) / 10_000)),
            _line("Long Put Theta Value: {:,.0f}".format(abs(long_put_theta_value) / 10_000)),
            _line("Short Call Theta Value: {:,.0f}".format(abs(short_call_theta_value) / 10_000)),
            _line("Short Put Theta Value: {:,.0f}".format(abs(short_put_theta_value) / 10_000)),
        ]

        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": "Zones",
                "plot_title": f"{instrument} - Zones",
                "refresh_interval": refresh_interval,
                "image_b64": image_b64,
                "zone_info_lines": zone_info_lines,
                "theta_info_lines": theta_info_lines,
            }
        )

    # instrument, trades since 00:00 UTC, single-leg delta/gamma routes
    # (/lp, /lc, /sp, /sc) — maps each route's short key to which trades to
    # keep (direction/option_type), which OptionStrat leg method accumulates
    # them, and the delta-saturation fraction/side (see
    # options.delta_saturation_price, shared with dankbit.zones.extrema's
    # own delta_band) that locates the green marker line: calls saturate ITM
    # at high S, puts at low S, independent of long/short — each leg's own
    # sign is inherited automatically from its curve's value at that edge.
    DELTA_SATURATION_FRACTION = 0.9
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
        # since 00:00 UTC — same domain convention as chart_png_zones, so a
        # query for one expiry can't pull in another instrument's trades.
        midnight_utc = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).strftime("%Y-%m-%d %H:%M:%S")
        domain = [
            ("name", "=ilike", f"{instrument}-%"),
            ("expiration", ">=", datetime.now()),
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

    @http.route("/<string:instrument>/lp", type="http", auth="public", website=True)
    def chart_png_long_puts(self, instrument):
        return self._chart_png_single_leg(instrument, "lp")

    @http.route("/<string:instrument>/lc", type="http", auth="public", website=True)
    def chart_png_long_calls(self, instrument):
        return self._chart_png_single_leg(instrument, "lc")

    @http.route("/<string:instrument>/sp", type="http", auth="public", website=True)
    def chart_png_short_puts(self, instrument):
        return self._chart_png_single_leg(instrument, "sp")

    @http.route("/<string:instrument>/sc", type="http", auth="public", website=True)
    def chart_png_short_calls(self, instrument):
        return self._chart_png_single_leg(instrument, "sc")

    @http.route("/<string:instrument>", type="http", auth="public", website=True)
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

    @http.route("/<string:asset>/weekly", type="http", auth="public", website=True)
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

    @http.route("/<string:asset>/monthly", type="http", auth="public", website=True)
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

    @http.route("/i/<string:instrument>", type="http", auth="public", website=True)
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

    @http.route("/api/delta-zero/<string:instrument>", type="http", auth="public", website=False, csrf=False)
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

    @http.route("/api/delta-zero-all/<string:asset>", type="http", auth="public", website=False, csrf=False)
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

    @http.route("/api/delta-zero-tomorrow/<string:asset>", type="http", auth="public", website=False, csrf=False)
    def delta_zero_tomorrow_json(self, asset):
        payload = self._delta_zero_for_calendar_day(asset, 1)
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/delta-zero-day-after-tomorrow/<string:asset>", type="http", auth="public", website=False, csrf=False)
    def delta_zero_day_after_tomorrow_json(self, asset):
        payload = self._delta_zero_for_calendar_day(asset, 2)
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/gamma-levels/<string:instrument>", type="http", auth="public", website=False, csrf=False)
    def gamma_levels_json(self, instrument):
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
        g_arr = gamma.portfolio_gamma(STs, agg_trades, 0.05)
        d_arr = delta.portfolio_delta(STs, agg_trades, 0.05)

        peaks   = [{"price": px, "delta_positive": bool(np.interp(px, STs, d_arr) > 0)}
                   for px, _ in self.find_gamma_peaks(STs, g_arr)]
        bottoms = [{"price": px, "delta_positive": bool(np.interp(px, STs, d_arr) > 0)}
                   for px, _ in self.find_gamma_bottoms(STs, g_arr)]

        payload = {
            "asset": asset,
            "expiry": expiry_str,
            "peaks": peaks,
            "bottoms": bottoms,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/zones-extrema/<string:asset>", type="http", auth="public", website=False, csrf=False)
    def zones_extrema_json(self, asset):
        """One point per instrument stored in dankbit.zones.extrema (each
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

        cr = request.env.cr
        cr.execute("""
            SELECT instrument, index_price, top_intersection, bottom_intersection,
                   top_intersection_positive, bottom_intersection_positive, gamma_band, delta_band
            FROM dankbit_zones_extrema
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
            instrument, index_price, top_intersection, bottom_intersection,
            top_intersection_positive, bottom_intersection_positive, gamma_band, delta_band,
        ) in rows:
            expiration = expiry_by_instrument.get(instrument)
            if not expiration:
                # No trades found at all for this instrument any more —
                # nothing to anchor the point's time to.
                continue
            ts = expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)
            series.append({
                "t": int(ts.timestamp() * 1000),
                "index_price": float(index_price or 0.0),
                "top_intersection": float(top_intersection or 0.0),
                "bottom_intersection": float(bottom_intersection or 0.0),
                # Whether the payoff at that intersection sits above/below
                # the zero line — drives the +/- marker on the chart, not
                # the point's own price/position.
                "top_intersection_positive": bool(top_intersection_positive),
                "bottom_intersection_positive": bool(bottom_intersection_positive),
                "gamma_band": float(gamma_band or 0.0),
                "delta_band": float(delta_band or 0.0),
            })
        series.sort(key=lambda r: r["t"])

        payload = {
            "asset": asset,
            "zones_extrema": series,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/zones-box/<string:asset>", type="http", auth="public", website=False, csrf=False)
    def zones_box_json(self, asset):
        """Live zones-box boundaries for the nearest active expiry —
        computed fresh on every request via dankbit.zones.extrema.get_box(),
        not read from stored history: nothing reads box-boundary history,
        only the latest value is ever drawn, so those 4 fields are never
        persisted. As a side effect, get_box() does upsert that instrument's
        zones-extrema record (index_price/top_intersection/
        bottom_intersection) on every call — piggybacking the per-expiry
        history this endpoint's own polling interval instead of a separate
        cron (see dankbit.zones.extrema._persist_extrema) — UNLESS an
        explicit `?hours=` override is given, in which case get_box()
        computes against that trailing-hours trade window instead of the
        default since-00:00-UTC-through-now one and skips persistence
        entirely (display-only — see dankbit.zones.extrema.get_box). Driven
        by /chart/<asset>'s 00:00-UTC-vs-trailing-hours radio toggle (see
        dankbit_templates.xml); omitting it leaves this endpoint's behavior
        exactly as before."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        hours_param = request.httprequest.args.get("hours")
        hours = int(hours_param) if hours_param else None

        data = request.env["dankbit.zones.extrema"].get_box(asset, hours=hours)
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
                "short_max_price": float(data["short_max_price"]),
                "long_min_price": float(data["long_min_price"]),
            }
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/trial-points/<string:asset>", type="http", auth="public", website=False, csrf=False)
    def trial_points_json(self, asset):
        """EXPERIMENTAL trial feature — deliberately kept fully independent
        from dankbit.zones.extrema: no shared code path, no persistence,
        never calls that model or any of its methods, so it can never
        affect the existing Zones Extrema lines/boxes, their cron, or their
        persisted history. Computes bottom_intersection/top_intersection
        (the lowest/highest Longs-vs-Shorts payoff-curve crossing) and
        gamma_band (average of the 4 per-leg gamma extrema) for `asset`'s
        nearest active expiry, using trades from the trailing
        `dankbit.zones_box_window_hours` hours through now (the same
        setting the zones-box trade-window radio toggle uses) — same
        underlying math as dankbit.zones.extrema._compute_asset()
        (options.build_zone_curves()/find_zero_crossings(),
        gamma.portfolio_gamma() with the same r=0.0 convention), but
        reimplemented standalone here rather than calling that method.
        Drawn on /chart/<asset> as three single-point markers (red/green/
        violet) at the current time rather than the existing time-series
        lines. Every field is None when there's nothing computable."""
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

        window_hours = int(icp.get_param("dankbit.zones_box_window_hours", default=8))
        index_price = request.env["dankbit.trade"].get_index_price(asset)

        bottom_intersection = None
        top_intersection = None
        gamma_band = None
        trade_count = 0

        if index_price:
            as_of = datetime.now(timezone.utc).replace(tzinfo=None)
            cr = request.env.cr
            cr.execute("""
                SELECT DISTINCT expiration FROM dankbit_trade
                WHERE name ILIKE %s AND expiration >= %s
                ORDER BY expiration ASC
                LIMIT 1
            """, (f"{asset}-%", as_of))
            row = cr.fetchone()
            target_expiration = row[0] if row else None

            if target_expiration:
                instrument = (
                    f"{asset}-{target_expiration.day}"
                    f"{target_expiration.strftime('%b').upper()}{target_expiration.strftime('%y')}"
                )
                window_start = as_of - timedelta(hours=window_hours)
                Trade = request.env["dankbit.trade"].with_context(active_test=False)
                domain = [
                    ("name", "=ilike", f"{asset}-%"),
                    ("expiration", "=", target_expiration),
                    ("deribit_ts", ">=", window_start),
                    ("deribit_ts", "<=", as_of),
                ]
                trades = Trade.search(domain=domain)
                trade_count = len(trades)

                if trades:
                    longs_obj, shorts_obj = options.build_zone_curves(
                        instrument, index_price, trades, from_price, to_price, steps
                    )
                    STs = longs_obj.STs
                    crossings = options.find_zero_crossings(STs, longs_obj.payoffs - shorts_obj.payoffs)
                    if crossings:
                        top_intersection = float(max(crossings))
                        bottom_intersection = float(min(crossings))

                    long_calls = trades.filtered(lambda t: t.direction == "buy" and t.option_type == "call")
                    long_puts = trades.filtered(lambda t: t.direction == "buy" and t.option_type == "put")
                    short_calls = trades.filtered(lambda t: t.direction == "sell" and t.option_type == "call")
                    short_puts = trades.filtered(lambda t: t.direction == "sell" and t.option_type == "put")

                    long_call_peak = float(STs[int(np.argmax(gamma.portfolio_gamma(STs, long_calls)))])
                    long_put_peak = float(STs[int(np.argmax(gamma.portfolio_gamma(STs, long_puts)))])
                    short_call_bottom = float(STs[int(np.argmin(gamma.portfolio_gamma(STs, short_calls)))])
                    short_put_bottom = float(STs[int(np.argmin(gamma.portfolio_gamma(STs, short_puts)))])
                    gamma_band = (long_call_peak + long_put_peak + short_call_bottom + short_put_bottom) / 4.0

        payload = {
            "asset": asset,
            "window_hours": window_hours,
            "bottom_intersection": bottom_intersection,
            "top_intersection": top_intersection,
            "gamma_band": gamma_band,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/zones-extrema-refresh/<string:asset>/<int:expiry_index>", type="http", auth="public", website=False, csrf=False)
    def zones_extrema_refresh_json(self, asset, expiry_index):
        """Triggers a live dankbit.zones.extrema.get_box_n(asset, expiry_index)
        computation for expiry_index 1 upward (0, the nearest expiry, already
        gets this as a side effect of /api/zones-box, the only one that draws
        a box) — no box is ever drawn for these, only the Top/Bottom
        Intersection and Gamma Band term-structure lines, which
        read every persisted row for the asset via /api/zones-extrema/<asset>
        regardless of expiry_index. Called by the TradingView chart's
        periodic refresh purely to keep those rows fresh at
        dankbit.refresh_interval while the page is open, the same way
        zones-box polling already does for expiry_index 0 — the 15-minute
        compute_snapshot() cron is the fallback for when nobody's watching.
        Returns just enough to confirm what happened, not the 4 box-boundary
        fields (nothing needs them here since no box is drawn)."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        Extrema = request.env["dankbit.zones.extrema"]
        if not (0 <= expiry_index < Extrema.TRACKED_EXPIRY_COUNT):
            return request.make_response(
                json.dumps({"error": "expiry_index out of range"}),
                headers=[("Content-Type", "application/json")],
            )

        data = Extrema.get_box_n(asset, expiry_index)
        payload = {
            "asset": asset,
            "expiry_index": expiry_index,
            "instrument": data["instrument"] if data else None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/nearest-expiry/<string:asset>", type="http", auth="public", website=False, csrf=False)
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
        expiry = request.env["dankbit.zones.extrema"].nearest_expiry(asset)
        payload = {
            "asset": asset,
            "expiry": expiry,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/klines/<string:asset>", type="http", auth="public", website=False, csrf=False)
    def klines_proxy(self, asset, interval="4h", limit="500"):
        instrument_map = {"BTC": "BTC-PERPETUAL", "ETH": "ETH-PERPETUAL"}
        instrument = instrument_map.get(asset.upper(), asset.upper() + "-PERPETUAL")
        resolution_map = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                          "1h": 60, "1d": "1D"}
        limit_int = int(limit)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # Deribit has no native 4h resolution (240 is rejected as "unsupported
        # resolution") — fetch 1h candles and aggregate every 4 into one,
        # bucketed by timestamp (not position) so buckets stay calendar-aligned
        # to 00:00 UTC regardless of the fetch window's exact boundaries.
        bucket_hours = 4 if interval == "4h" else 1
        resolution = 60 if interval == "4h" else resolution_map.get(interval, 360)
        granularity_ms = (86400000 if resolution == "1D"
                          else int(resolution) * 60 * 1000)
        start_ms = now_ms - limit_int * bucket_hours * granularity_ms
        url = (f"https://www.deribit.com/api/v2/public/get_tradingview_chart_data"
               f"?instrument_name={instrument}&resolution={resolution}"
               f"&start_timestamp={start_ms}&end_timestamp={now_ms}")
        resp = _requests.get(url, timeout=10).json()
        result = resp.get("result", {})
        ticks  = result.get("ticks",  [])
        opens  = result.get("open",   [])
        highs  = result.get("high",   [])
        lows   = result.get("low",    [])
        closes = result.get("close",  [])
        # Deribit returns oldest-first
        candles = [
            {"t": ticks[i], "o": opens[i], "h": highs[i], "l": lows[i], "c": closes[i]}
            for i in range(len(ticks))
        ]

        if interval == "4h":
            bucket_ms = 4 * 3600 * 1000
            buckets = {}
            for c in candles:
                key = c["t"] // bucket_ms
                if key not in buckets:
                    buckets[key] = {"t": key * bucket_ms, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]}
                else:
                    b = buckets[key]
                    b["h"] = max(b["h"], c["h"])
                    b["l"] = min(b["l"], c["l"])
                    b["c"] = c["c"]
            candles = [buckets[k] for k in sorted(buckets)]

        candles = candles[::-1]  # newest-first for frontend
        return request.make_response(
            json.dumps({"result": candles}),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    # ------------------------------------------------------------------
    # TradingView Lightweight Charts pages
    # ------------------------------------------------------------------

    def _build_tv_chart_context(self, asset):
        """Shared context-building for /chart/<asset> and /my/<asset> — both
        render the same dankbit_tv_chart_until template; /my/<asset> just
        additionally sets show_gamma_point so the template also draws the
        gamma point line (see gamma_point_json). Returns (context, None) on
        success or (None, error_message) if the weekly expiry isn't
        configured/valid for `asset`, so callers can render that as a plain
        text response the same way this route always has."""
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

        return {
            "instrument": instrument,
            "asset": asset,
            "expiry": expiry_str,
            "monthly_instrument": monthly_instrument,
            "refresh_interval": refresh_interval,
            "zones_box_refresh_interval": zones_box_refresh_interval,
            "zones_box_window_hours": zones_box_window_hours,
            "show_daily_lines": show_daily_lines,
            "show_weekly_lines": show_weekly_lines,
            "show_monthly_lines": show_monthly_lines,
            "show_gamma_point": "false",
        }, None

    @http.route("/chart/<string:asset>", type="http", auth="public", website=True)
    def chart_tv(self, asset):
        asset = asset.upper()

        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        ctx, error = self._build_tv_chart_context(asset)
        if error:
            return request.make_response(error, headers=[("Content-Type", "text/plain")])

        return request.render("dankbit.dankbit_tv_chart_until", ctx)

    @http.route("/my/<string:asset>", type="http", auth="public", website=True)
    def my_chart_tv(self, asset):
        """Same page as /chart/<asset> (identical template/context), plus the
        gamma point line (see gamma_point_json) — /chart/<asset> itself is
        unaffected, it always passes show_gamma_point=false."""
        asset = asset.upper()

        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        ctx, error = self._build_tv_chart_context(asset)
        if error:
            return request.make_response(error, headers=[("Content-Type", "text/plain")])

        ctx["show_gamma_point"] = "true"
        return request.render("dankbit.dankbit_tv_chart_until", ctx)

    @http.route("/api/gamma-point/<string:asset>/<int:hours>", type="http", auth="public", website=False, csrf=False)
    def gamma_point_json(self, asset, hours):
        """gamma_point: average of 4 per-leg gamma extrema — Long Call/Put
        Gamma Peak (argmax), Short Call/Put Gamma Bottom (argmin, since
        short positions carry negative gamma) — across *every* active
        expiry for `asset`, restricted to trades from the trailing `hours`
        hours. No configured default any more (dankbit.gamma_point_window_hours
        was removed) — /my/<asset> always calls this once per window in
        GAMMA_POINT_HOURS = [4, 8, 12, 24] (see dankbit_templates.xml), each
        drawn as its own titled price line. dominant_leg: whichever of the
        4 legs (Long Call/Put, Short Call/Put) has the largest absolute
        dollar-gamma magnitude at its own peak/bottom — the biggest
        contributor to the averaged gamma_point; appended to each line's
        title. Both None when there are no trades at all in this window."""
        asset = asset.upper()
        window_hours = hours
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
              AND deribit_ts >= NOW() - (%s * INTERVAL '1 hour')
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{asset}%', window_hours))
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

        gamma_point = None
        dominant_leg = None
        if agg_trades:
            long_calls_agg = [t for t in agg_trades if t.direction == "buy" and t.option_type == "call"]
            long_puts_agg = [t for t in agg_trades if t.direction == "buy" and t.option_type == "put"]
            short_calls_agg = [t for t in agg_trades if t.direction == "sell" and t.option_type == "call"]
            short_puts_agg = [t for t in agg_trades if t.direction == "sell" and t.option_type == "put"]

            long_call_gamma_curve = gamma.portfolio_gamma(STs, long_calls_agg, 0.05)
            long_put_gamma_curve = gamma.portfolio_gamma(STs, long_puts_agg, 0.05)
            short_call_gamma_curve = gamma.portfolio_gamma(STs, short_calls_agg, 0.05)
            short_put_gamma_curve = gamma.portfolio_gamma(STs, short_puts_agg, 0.05)

            long_call_gamma_peak_idx = int(np.argmax(long_call_gamma_curve))
            long_put_gamma_peak_idx = int(np.argmax(long_put_gamma_curve))
            short_call_gamma_bottom_idx = int(np.argmin(short_call_gamma_curve))
            short_put_gamma_bottom_idx = int(np.argmin(short_put_gamma_curve))

            long_call_gamma_peak_price = float(STs[long_call_gamma_peak_idx])
            long_put_gamma_peak_price = float(STs[long_put_gamma_peak_idx])
            short_call_gamma_bottom_price = float(STs[short_call_gamma_bottom_idx])
            short_put_gamma_bottom_price = float(STs[short_put_gamma_bottom_idx])

            gamma_point = (
                long_call_gamma_peak_price + long_put_gamma_peak_price
                + short_call_gamma_bottom_price + short_put_gamma_bottom_price
            ) / 4.0

            # Dominant leg: whichever of the 4 extrema has the largest
            # absolute dollar-gamma magnitude at its own peak/bottom — the
            # one contributing the most weight to the averaged gamma_point.
            leg_magnitudes = {
                "Long Call": abs(float(long_call_gamma_curve[long_call_gamma_peak_idx])),
                "Long Put": abs(float(long_put_gamma_curve[long_put_gamma_peak_idx])),
                "Short Call": abs(float(short_call_gamma_curve[short_call_gamma_bottom_idx])),
                "Short Put": abs(float(short_put_gamma_curve[short_put_gamma_bottom_idx])),
            }
            dominant_leg = max(leg_magnitudes, key=leg_magnitudes.get)

        payload = {
            "asset": asset,
            "gamma_point": gamma_point,
            "dominant_leg": dominant_leg,
            "window_hours": window_hours,
            "trade_count": trade_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )
