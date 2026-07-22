# -*- coding: utf-8 -*-
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

# ✅ Server-safe Matplotlib (NO pyplot!)
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from matplotlib.ticker import MultipleLocator
import matplotlib.patheffects as path_effects

from . import delta as delta_lib
from . import gamma as gamma_lib
from . import theta as theta_lib
from . import vega as vega_lib

# The fraction of a leg's own curve extreme delta_saturation_price() (below)
# looks for — 90% of the way from ATM to fully saturated ITM. Single source
# of truth: main.py's ChartController and forecast.py both used to keep
# their own separate copy of this same 0.9 value.
DELTA_SATURATION_FRACTION = 0.9


class OptionStrat:
    def __init__(self, name, S0, from_price, to_price, step):
        self.name = name
        self.S0 = S0
        self.STs = np.arange(from_price, to_price, step, dtype=np.float64)
        self.payoffs = np.zeros_like(self.STs, dtype=np.float64)

    def long_call(self, K, C, Q=1):
        self.payoffs += (np.maximum(self.STs - K, 0) - C) * Q

    def short_call(self, K, C, Q=1):
        self.payoffs += (-np.maximum(self.STs - K, 0) + C) * Q

    def long_put(self, K, P, Q=1):
        self.payoffs += (np.maximum(K - self.STs, 0) - P) * Q

    def short_put(self, K, P, Q=1):
        self.payoffs += (-np.maximum(K - self.STs, 0) + P) * Q

    # =========================================================
    # BASELINE PLOT — SERVER-SAFE (NO pyplot), EXPLICIT FIG/AXES
    # =========================================================
    def plot(
        self,
        index_price,
        market_delta,
        market_gammas,
        show_red_line,
        title="-",
        width=18,
        height=8,
    ):
        # ✅ Create a fresh figure per request (no global state)
        fig = Figure(figsize=(width, height), dpi=120)
        FigureCanvas(fig)  # attach Agg canvas so fig.canvas.draw() works reliably
        ax = fig.add_subplot(111)

        # Rotate x tick labels (pyplot-free)
        ax.tick_params(axis="x", labelrotation=90)

        # Tick spacing
        if self.name.startswith("BTC"):
            ax.xaxis.set_major_locator(MultipleLocator(1000))
        elif self.name.startswith("ETH"):
            ax.xaxis.set_major_locator(MultipleLocator(100))

        ax.grid(True)

        md = np.asarray(market_delta, dtype=float) if market_delta is not None else np.zeros_like(self.STs)
        mg = np.asarray(market_gammas, dtype=float) if market_gammas is not None else np.zeros_like(self.STs)

        # Resample if sizes mismatch
        if md.size != self.STs.size:
            md = np.interp(self.STs, np.linspace(self.STs.min(), self.STs.max(), md.size), md)
        if mg.size != self.STs.size:
            mg = np.interp(self.STs, np.linspace(self.STs.min(), self.STs.max(), mg.size), mg)

        pnl_curve = self.payoffs
        delta_curve = md
        gamma_curve = mg
        pnl_label = "Taker P&L"

        # Clip x-range so nothing from outside the domain “sticks”
        ax.set_xlim(float(self.STs.min()), float(self.STs.max()))

        # =====================================================
        # DELTA AXIS (PRIMARY) — ZERO CENTERED
        # =====================================================
        ax.plot(self.STs, delta_curve, color="green", label="Delta")

        if np.any(np.isfinite(delta_curve)):
            dmax = float(np.max(np.abs(delta_curve)))
            if dmax > 0:
                ax.set_ylim(-dmax, dmax)

        ax.axhline(0, color="black", linewidth=1)
        ax.axvline(x=index_price, color="blue")
        ax.tick_params(axis="y", labelcolor="green")

        # =====================================================
        # GAMMA AXIS (SECONDARY) — ZERO CENTERED
        # =====================================================
        axg = ax.twinx()
        axg.plot(
            self.STs,
            gamma_curve,
            color="violet",
            linewidth=2.0,
            alpha=0.9,
            label="Gamma",
        )
        axg.tick_params(axis="y", labelcolor="violet")

        if np.any(np.isfinite(gamma_curve)):
            gmax = float(np.max(np.abs(gamma_curve)))
            if gmax > 0:
                axg.set_ylim(-gmax, gmax)

        axg.fill_between(
            self.STs,
            gamma_curve,
            0,
            where=(gamma_curve < 0),
            color="violet",
            alpha=0.25,
            interpolate=True,
        )

        # =====================================================
        # P&L AXIS (THIRD) — ZERO CENTERED
        # =====================================================
        axp = None
        if show_red_line:
            axp = ax.twinx()
            axp.spines["right"].set_position(("outward", 60))
            axp.plot(self.STs, pnl_curve, color="red", label=pnl_label)
            axp.tick_params(axis="y", labelcolor="red")

            if np.any(np.isfinite(pnl_curve)):
                pmax = float(np.max(np.abs(pnl_curve)))
                if pmax > 0:
                    axp.set_ylim(-pmax, pmax)

        # axg (and axp, if present) are twin axes created after ax, so by
        # default they stack visually on top of ax — washing out anything
        # drawn on ax afterward (e.g. the caller's axvline/text overlays)
        # wherever their alpha-blended fills overlap. Bring ax back to the
        # top of the stack and hide its opaque background so axg/axp still
        # show through everywhere else.
        top_zorder = max(a.get_zorder() for a in (axg, axp) if a is not None)
        ax.set_zorder(top_zorder + 1)
        ax.patch.set_visible(False)

        now = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M")
        ax.set_title(f"{self.name} | {now} UTC | {title}")
        ax.set_xlabel(f"${self.S0:,.0f}", fontsize=10, color="blue")

        # Combine legends from all axes
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = axg.get_legend_handles_labels()
        h = h1 + h2
        l = l1 + l2
        if show_red_line and axp is not None:
            h3, l3 = axp.get_legend_handles_labels()
            h += h3
            l += l3

        # ax.legend(h, l, loc="upper right", framealpha=0.85)

        # self.add_dankbit_signature(ax)
        return fig, ax

    # =========================================================
    # ZONES PLOT — separate Longs (buy-side) vs Shorts (sell-side)
    # payoff curves, server-safe, no pyplot
    # =========================================================
    def plot_zones(self, longs_curve, shorts_curve, index_price, title="Zones", width=18, height=8):
        fig = Figure(figsize=(width, height), dpi=120)
        FigureCanvas(fig)
        ax = fig.add_subplot(111)

        ax.tick_params(axis="x", labelrotation=90)

        if self.name.startswith("BTC"):
            ax.xaxis.set_major_locator(MultipleLocator(1000))
        elif self.name.startswith("ETH"):
            ax.xaxis.set_major_locator(MultipleLocator(100))

        ax.grid(True)
        ax.set_xlim(float(self.STs.min()), float(self.STs.max()))

        ax.plot(self.STs, longs_curve, color="green", label="Longs")
        ax.plot(self.STs, shorts_curve, color="red", label="Shorts")
        ax.axhline(0, color="black", linewidth=1)
        ax.axvline(x=index_price, color="blue")

        now = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M")
        ax.set_title(f"{self.name} | {now} UTC")
        ax.set_xlabel(f"${self.S0:,.0f}", fontsize=10, color="blue")
        ax.legend(loc="upper right", framealpha=0.85)

        return fig, ax

    # =====================================================
    # Signature (server-safe: ensure canvas exists)
    # =====================================================
    def add_dankbit_signature(self, ax, alpha=0.5, fontsize=16):
        fig = ax.figure

        # Ensure we have a canvas attached (needed for bbox measurement)
        if getattr(fig, "canvas", None) is None:
            FigureCanvas(fig)

        # --- Force legend into top-right ---
        old_legend = ax.get_legend()
        if old_legend:
            handles, labels = old_legend.legendHandles, [t.get_text() for t in old_legend.texts]
            legend = ax.legend(handles, labels, loc="upper right", framealpha=0.85)
        else:
            legend = ax.legend(loc="upper right", framealpha=0.85)

        legend.get_frame().set_alpha(0.85)

        # draw now so we can measure bbox
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()

        # --- Legend bbox in axes coords ---
        lbbox = legend.get_window_extent(renderer)
        lbbox_axes = ax.transAxes.inverted().transform_bbox(lbbox)

        legend_left_x = lbbox_axes.x0
        legend_top_y = lbbox_axes.y1

        # --- signature position: slightly left of legend ---
        pad = 0.015
        sig_x = legend_left_x - pad
        sig_y = legend_top_y - 0.01

        # clamp inside plot
        if sig_x < 0.02:
            sig_x = 0.02

        # --- Signature text ---
        color = "#6c2bd9"
        t = ax.text(
            sig_x,
            sig_y,
            "Dankbit™",
            transform=ax.transAxes,
            fontsize=fontsize,
            color=color,
            alpha=alpha,
            ha="right",
            va="top",
            fontweight="bold",
            family="monospace",
        )
        t.set_path_effects([path_effects.withStroke(linewidth=3, alpha=0.3, foreground="white")])


def find_zero_crossings(STs, curve):
    """Return the list of prices where `curve` crosses zero, via linear
    interpolation on each sign change (STs must be ascending). Same technique
    used by the delta=0 finders elsewhere in this codebase, and by
    build_zone_curves()'s own Longs-vs-Shorts crossing below."""
    crossings = []
    for i in range(len(curve) - 1):
        a, b = curve[i], curve[i + 1]
        if not (np.isfinite(a) and np.isfinite(b)):
            continue
        if a * b < 0:
            px = float(STs[i] - a * (STs[i + 1] - STs[i]) / (b - a))
            crossings.append(px)
    return crossings


def delta_saturation_price(STs, trades, fraction, stop_at):
    """Interpolated price where portfolio_delta(trades) first reaches
    `fraction` of its own extreme value at the `stop_at` edge of STs (e.g.
    fraction=0.9, stop_at='max' -> the price where the curve has climbed to
    90% of whatever value it reaches at the highest price in this window)
    — the point where the sigmoid-shaped delta curve stops curving and
    flattens into a straight line (deep enough ITM that the option starts
    trading like synthetic stock).

    Relative to the curve's own extreme, not an absolute delta value:
    portfolio_delta sums sign*amount*per-contract delta across every
    matching trade, so its magnitude reflects total traded size (seen
    against real data: values in the hundreds, not a single option's
    [-1, 1] range) — a fixed absolute threshold like 0.9 would be crossed
    almost immediately near the start of the curve, not at any deep-ITM
    point. Using the curve's own endpoint as 100% self-scales regardless of
    how much volume traded.

    Curves here are monotonic (see delta.py's sign convention: each leg's
    aggregate delta only ever moves in one direction across price), so the
    `stop_at` edge value is genuinely the curve's extreme in this window,
    and its sign is inherited automatically — long_calls/short_puts have a
    positive extreme, long_puts/short_calls a negative one, with no need to
    pass sign separately. Falls back to the STs edge on `stop_at` if the
    extreme itself is 0 (e.g. no trades — nothing to take a fraction of) or
    the curve never reaches that fraction within this window. Shared by
    dankbit.bands (delta_band) and the /<instrument>/lp,lc,sp,sc
    single-leg routes (green marker line), so the two can never disagree on
    where this point is."""
    curve = np.asarray(delta_lib.portfolio_delta(STs, trades), dtype=float)
    extreme = curve[-1] if stop_at == "max" else curve[0]
    if extreme == 0:
        return float(STs[-1]) if stop_at == "max" else float(STs[0])
    threshold = fraction * extreme
    crossings = find_zero_crossings(STs, curve - threshold)
    if crossings:
        return max(crossings) if stop_at == "max" else min(crossings)
    return float(STs[-1]) if stop_at == "max" else float(STs[0])


# Which numpy extremum-finder each leg uses per Greek — long positions carry
# positive gamma/vega (peak, argmax) and negative theta (decay cost, trough,
# argmin); short positions carry negative gamma/vega (trough, argmin) and
# positive theta (decay gain, peak, argmax). Delta has no argmax/argmin
# split — delta_saturation_price() finds an interpolated price instead, at
# the 'max' (high-S) edge for calls, 'min' (low-S) edge for puts, independent
# of long/short (see that function's own docstring).
_GAMMA_VEGA_ARGFN = {"long_call": np.argmax, "long_put": np.argmax, "short_call": np.argmin, "short_put": np.argmin}
_THETA_ARGFN = {"long_call": np.argmin, "long_put": np.argmin, "short_call": np.argmax, "short_put": np.argmax}
_DELTA_STOP_AT = {"long_call": "max", "long_put": "min", "short_call": "max", "short_put": "min"}


def per_leg_greeks(STs, trades, r=0.0):
    """For each of the 4 legs filtered from `trades` (long_call, long_put,
    short_call, short_put — buy/sell x call/put), finds where that leg's own
    portfolio gamma/theta/vega curve peaks or bottoms out, and the price
    where its portfolio delta first saturates to DELTA_SATURATION_FRACTION
    of its own extreme (see delta_saturation_price). Returns
    {leg_name: {"trades": recordset, "gamma_price", "gamma_value",
    "delta_price", "delta_value", "theta_price", "theta_value",
    "vega_price", "vega_value"}} — raw (unscaled) prices and values; each
    caller applies its own display scaling (e.g. the /<instrument>/zones
    page's Value lines divide gamma/delta/theta/vega by 1e6/10/1e4/100,
    forecast.per_leg_greeks() does the same for its own *_abs fields).

    Single source of truth for this computation — shared by chart_png_zones
    (main.py), dankbit.bands's gamma_band/delta_band
    (models/bands.py), and forecast.per_leg_greeks()
    (controllers/forecast.py), so the three can never quietly compute
    different numbers for the same trades. `trades` should already be
    filtered to whichever expiry/time-window the caller cares about — this
    function only splits by direction/option_type, nothing else."""
    legs = {
        "long_call": trades.filtered(lambda t: t.direction == "buy" and t.option_type == "call"),
        "long_put": trades.filtered(lambda t: t.direction == "buy" and t.option_type == "put"),
        "short_call": trades.filtered(lambda t: t.direction == "sell" and t.option_type == "call"),
        "short_put": trades.filtered(lambda t: t.direction == "sell" and t.option_type == "put"),
    }

    result = {}
    for leg_name, leg_trades in legs.items():
        gamma_curve = gamma_lib.portfolio_gamma(STs, leg_trades, r=r)
        gamma_idx = int(_GAMMA_VEGA_ARGFN[leg_name](gamma_curve))
        gamma_price, gamma_value = float(STs[gamma_idx]), float(gamma_curve[gamma_idx])

        theta_curve = theta_lib.portfolio_theta(STs, leg_trades, r=r)
        theta_idx = int(_THETA_ARGFN[leg_name](theta_curve))
        theta_price, theta_value = float(STs[theta_idx]), float(theta_curve[theta_idx])

        vega_curve = vega_lib.portfolio_vega(STs, leg_trades, r=r)
        vega_idx = int(_GAMMA_VEGA_ARGFN[leg_name](vega_curve))
        vega_price, vega_value = float(STs[vega_idx]), float(vega_curve[vega_idx])

        delta_price = delta_saturation_price(STs, leg_trades, DELTA_SATURATION_FRACTION, _DELTA_STOP_AT[leg_name])
        delta_value = float(np.interp(delta_price, STs, delta_lib.portfolio_delta(STs, leg_trades, r=r)))

        result[leg_name] = {
            "trades": leg_trades,
            "gamma_price": gamma_price, "gamma_value": gamma_value,
            "delta_price": delta_price, "delta_value": delta_value,
            "theta_price": theta_price, "theta_value": theta_value,
            "vega_price": vega_price, "vega_value": vega_value,
        }
    return result


def zone_summary(STs, longs_curve, shorts_curve):
    """Same extrema/box-boundary definitions used by dankbit.bands
    and the TradingView zones boxes: Shorts curve peak ("seller_max_profit"),
    Longs curve bottom ("buyer_max_loss"), and each curve's own highest/
    lowest zero-crossing, giving a "high zone" (the two curves' highest
    crossings) and a "low zone" (their lowest).

    Current price is deliberately not a factor anywhere in this function —
    same principle as high_resistance/low_support below: a zone or
    intersection is a property of where the curves themselves cross zero
    (or each other), not of where the index price happens to sit relative
    to them. `high_zone`/`low_zone` (renamed from top_box/bottom_box) used
    to require a crossing on each curve on the correct side of index_price,
    and silently returned None otherwise — but "no crossing above/below
    current price" and "no crossing at all" are different situations; the
    former discarded real data. Now: any curve that has at least one
    crossing contributes its max (to high_zone) and min (to low_zone), so a
    zone can still form from a single curve's data alone (a degenerate
    (price, price) pair) if the other curve has none. Only truly empty
    (neither curve ever crosses zero) yields None.

    "middle_zone" is the zone bounded by seller_max_profit and
    buyer_max_loss (min/max of the two, since either can sit above the
    other depending on the trades) — unlike high_zone/low_zone, always
    defined, since seller_max_profit/buyer_max_loss are argmax/argmin
    over the full curve and don't depend on a zero-crossing existing.

    "high_resistance"/"low_support" (renamed from top_intersection/
    bottom_intersection to match Thales's own Resistance/Support
    terminology for this reference band) are the highest/lowest price
    where the Longs and Shorts curves cross each other (`longs_curve -
    shorts_curve` sign changes — the same crossings build_zone_curves()
    finds internally for its ±$2000 auto-zoom). Returns a dict; any of
    these is None if there's no crossing at all."""
    seller_max_profit = float(STs[int(np.argmax(shorts_curve))])
    buyer_max_loss = float(STs[int(np.argmin(longs_curve))])

    short_crossings = find_zero_crossings(STs, shorts_curve)
    long_crossings = find_zero_crossings(STs, longs_curve)

    top_prices = ([max(short_crossings)] if short_crossings else []) + ([max(long_crossings)] if long_crossings else [])
    bottom_prices = ([min(short_crossings)] if short_crossings else []) + ([min(long_crossings)] if long_crossings else [])

    diff = np.asarray(longs_curve, dtype=float) - np.asarray(shorts_curve, dtype=float)
    lvs_crossings = find_zero_crossings(STs, diff)

    return {
        "seller_max_profit": seller_max_profit,
        "buyer_max_loss": buyer_max_loss,
        "high_zone": (min(top_prices), max(top_prices)) if top_prices else None,
        "low_zone": (min(bottom_prices), max(bottom_prices)) if bottom_prices else None,
        "middle_zone": (min(seller_max_profit, buyer_max_loss), max(seller_max_profit, buyer_max_loss)),
        "high_resistance": max(lvs_crossings) if lvs_crossings else None,
        "low_support": min(lvs_crossings) if lvs_crossings else None,
    }


def build_zone_curves(instrument_name, index_price, trades, from_price, to_price, steps):
    """Build the Longs/Shorts OptionStrat curves from `trades` (any iterable of
    objects with .direction/.option_type/.strike/.price/.index_price — an Odoo
    recordset works directly), then re-center on the crossing-based zoom
    exactly as the live /<instrument>/zones PNG chart does: ±$2000 for BTC,
    ±$100 for ETH (ETH's much smaller price scale made the ±$2000 margin blow
    out the auto-zoom). Falls back to the wide [from_price, to_price] range
    if the curves never cross.

    Shared by the /<instrument>/zones route (controllers/main.py) and
    dankbit.bands's cron (models/bands.py) so the two can
    never compute different extrema for the same trades — one implementation,
    not two copies that could quietly drift apart."""
    def build(fp, tp, st):
        longs = OptionStrat(instrument_name, index_price, fp, tp, st)
        shorts = OptionStrat(instrument_name, index_price, fp, tp, st)
        for trade in trades:
            if trade.direction == "buy":
                if trade.option_type == "call":
                    longs.long_call(trade.strike, trade.price * trade.index_price)
                elif trade.option_type == "put":
                    longs.long_put(trade.strike, trade.price * trade.index_price)
            elif trade.direction == "sell":
                if trade.option_type == "call":
                    shorts.short_call(trade.strike, trade.price * trade.index_price)
                elif trade.option_type == "put":
                    shorts.short_put(trade.strike, trade.price * trade.index_price)
        return longs, shorts

    longs_obj, shorts_obj = build(from_price, to_price, steps)

    STs = longs_obj.STs
    diff = longs_obj.payoffs - shorts_obj.payoffs
    crossings = find_zero_crossings(STs, diff)

    if crossings:
        if instrument_name.startswith("ETH"):
            margin_below, margin_above = 100, 100
        else:
            margin_below, margin_above = 2000, 2000
        zoom_from = min(crossings) - margin_below
        zoom_to = max(crossings) + margin_above
        longs_obj, shorts_obj = build(zoom_from, zoom_to, steps)

    return longs_obj, shorts_obj
