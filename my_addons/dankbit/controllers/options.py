# -*- coding: utf-8 -*-
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

# ✅ Server-safe Matplotlib (NO pyplot!)
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from matplotlib.ticker import MultipleLocator
import matplotlib.image as mpimg
from matplotlib.offsetbox import OffsetImage, AnnotationBbox
import matplotlib.patheffects as path_effects


_logger = logging.getLogger(__name__)


class Option:
    def __init__(self, type_, K, price, direction):
        self.type = type_
        self.K = K
        self.price = price
        self.direction = direction

    def __repr__(self):
        direction = "long" if self.direction == 1 else "short"
        return f"Option(type={self.type},K={self.K}, price={self.price},direction={direction})"


class OptionStrat:
    def __init__(self, name, S0, from_price, to_price, step):
        self.name = name
        self.S0 = S0
        self.STs = np.arange(from_price, to_price, step, dtype=np.float64)
        self.payoffs = np.zeros_like(self.STs, dtype=np.float64)
        self.longs = np.zeros_like(self.STs, dtype=np.float64)
        self.shorts = np.zeros_like(self.STs, dtype=np.float64)
        self.instruments = []

    def long_call(self, K, C, Q=1):
        self.payoffs += (np.maximum(self.STs - K, 0) - C) * Q
        self._add_to_self("call", K, C, 1, Q)

    def short_call(self, K, C, Q=1):
        self.payoffs += (-np.maximum(self.STs - K, 0) + C) * Q
        self._add_to_self("call", K, C, -1, Q)

    def long_put(self, K, P, Q=1):
        self.payoffs += (np.maximum(K - self.STs, 0) - P) * Q
        self._add_to_self("put", K, P, 1, Q)

    def short_put(self, K, P, Q=1):
        self.payoffs += (-np.maximum(K - self.STs, 0) + P) * Q
        self._add_to_self("put", K, P, -1, Q)

    def _add_to_self(self, type_, K, price, direction, Q):
        o = Option(type_, K, price, direction)
        for _ in range(Q):
            self.instruments.append(o)

    # =========================================================
    # BASELINE PLOT — SERVER-SAFE (NO pyplot), EXPLICIT FIG/AXES
    # =========================================================
    def plot(
        self,
        index_price,
        market_delta,
        market_gammas,
        view_type,
        show_red_line,
        plot_title,
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
            ax.xaxis.set_major_locator(MultipleLocator(50))

        ax.grid(True)

        md = np.asarray(market_delta, dtype=float) if market_delta is not None else np.zeros_like(self.STs)
        mg = np.asarray(market_gammas, dtype=float) if market_gammas is not None else np.zeros_like(self.STs)

        # Resample if sizes mismatch
        if md.size != self.STs.size:
            md = np.interp(self.STs, np.linspace(self.STs.min(), self.STs.max(), md.size), md)
        if mg.size != self.STs.size:
            mg = np.interp(self.STs, np.linspace(self.STs.min(), self.STs.max(), mg.size), mg)

        if view_type == "mm":
            pnl_curve = -self.payoffs
            delta_curve = -md
            gamma_curve = -mg
            pnl_label = "MM P&L"
        elif view_type == "taker":
            pnl_curve = self.payoffs
            delta_curve = md
            gamma_curve = mg
            pnl_label = "Taker P&L"
        else:
            raise ValueError(f"Invalid view type: {view_type}")

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

        if view_type == "mm":
            axg.fill_between(
                self.STs,
                gamma_curve,
                0,
                where=(gamma_curve > 0),
                color="violet",
                alpha=0.25,
                interpolate=True,
            )
        elif view_type == "taker":
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

        now = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M")
        ax.set_title(f"{self.name} | {now} UTC | {plot_title}")
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

        ax.legend(h, l, loc="upper right", framealpha=0.85)

        self.add_dankbit_signature(ax)
        return fig, ax

    # =====================================================
    # OI PLOT — SERVER-SAFE (NO pyplot)
    # =====================================================
    def plot_oi(self, index_price, oi_data, plot_title):
        fig = Figure(figsize=(18, 8), dpi=120)
        FigureCanvas(fig)
        ax = fig.add_subplot(111)

        if self.name.startswith("BTC"):
            ax.xaxis.set_major_locator(MultipleLocator(1000))
            ax.set_yticks(list(range(-1000, 1001, 100)))
            bar_width = 400
            label_offset = 1
        elif self.name.startswith("ETH"):
            ax.xaxis.set_major_locator(MultipleLocator(25))
            ax.set_yticks(list(range(-10000, 10001, 500)))
            bar_width = 10
            label_offset = 2

        def _get_offeset_with_sign(oi):
            if oi > 0:
                return label_offset
            else:
                return -label_offset
            
        def _get_va(oi):
            if oi > 0:
                return "bottom"
            else:
                return "top"

        ax.tick_params(axis="x", labelrotation=45)
        ax.grid(True)

        berlin_time = datetime.now(ZoneInfo("Europe/Berlin"))
        now = berlin_time.strftime("%Y-%m-%d %H:%M")

        # ---- helper for readable OI labels ----
        def fmt_oi(v):
            # v = abs(v)
            if v >= 1_000_000:
                return f"{v/1_000_000:.1f}M"
            if v >= 1_000:
                return f"{v/1_000:.1f}k"
            return f"{int(v)}"

        # ---- bars + labels ----
        for oi in oi_data:
            strike = float(oi[0])
            calls = float(oi[1])
            puts = float(oi[2])

            # Calls
            ax.bar(strike - bar_width / 2, calls, width=bar_width, color="green")
            if calls != 0:
                ax.text(
                    strike - bar_width / 2,
                    calls + _get_offeset_with_sign(calls),
                    fmt_oi(calls),
                    ha="center",
                    va=_get_va(calls),
                    fontsize=12,
                    color="green",
                )

            # Puts
            ax.bar(strike + bar_width / 2, puts, width=bar_width, color="red")
            if puts != 0:
                ax.text(
                    strike + bar_width / 2,
                    puts + _get_offeset_with_sign(puts),
                    fmt_oi(puts),
                    ha="center",
                    va=_get_va(puts),
                    fontsize=12,
                    color="red",
                )

        ax.set_title(f"{self.name} | {now} | {plot_title}")
        ax.axhline(0, color="black", linewidth=1, linestyle="-")
        ax.axvline(x=index_price, color="blue")
        ax.set_xlabel(f"${self.S0:,.0f}", fontsize=10, color="blue")

        self.add_dankbit_signature(ax)
        return fig, ax

    # =====================================================
    # Signature (server-safe: ensure canvas exists)
    # =====================================================
    def add_dankbit_signature(self, ax, logo_path=None, alpha=0.5, fontsize=16, trade_count=None):
        """
        Legend stays top-right.
        Dankbit™ signature sits immediately to the LEFT of the legend, with minimal spacing.
        Zero overlap, minimal distance.
        """
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

        # --- Draw logo ---
        if logo_path:
            try:
                img = mpimg.imread(logo_path)
                imagebox = OffsetImage(img, zoom=0.07, alpha=alpha)
                ab = AnnotationBbox(
                    imagebox,
                    (sig_x, sig_y),
                    xycoords="axes fraction",
                    frameon=False,
                    box_alignment=(1, 1),
                )
                ax.add_artist(ab)
                return
            except Exception:
                pass

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

        # --- Trade count under signature ---
        if trade_count is not None:
            ax.text(
                sig_x,
                sig_y - 0.045,
                f"{trade_count} trades",
                transform=ax.transAxes,
                fontsize=fontsize * 0.55,
                color=color,
                alpha=alpha * 0.8,
                ha="right",
                va="top",
                family="monospace",
            )
