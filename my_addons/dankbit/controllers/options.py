# -*- coding: utf-8 -*-
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator
import matplotlib.image as mpimg
from matplotlib.offsetbox import OffsetImage, AnnotationBbox
import matplotlib.patheffects as path_effects
from odoo.http import request as _odoo_request

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
    # BASELINE PLOT — SINGLE ZERO AXIS (CORRECT)
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
        fig, ax = plt.subplots(figsize=(width, height))
        plt.xticks(rotation=90)

        if self.name.startswith("BTC"):
            ax.xaxis.set_major_locator(MultipleLocator(1000))
        elif self.name.startswith("ETH"):
            ax.xaxis.set_major_locator(MultipleLocator(50))

        ax.grid(True)

        md = np.asarray(market_delta, dtype=float) if market_delta is not None else np.zeros_like(self.STs)
        mg = np.asarray(market_gammas, dtype=float) if market_gammas is not None else np.zeros_like(self.STs)

        if md.size != self.STs.size:
            md = np.interp(self.STs, np.linspace(self.STs.min(), self.STs.max(), md.size), md)
        if mg.size != self.STs.size:
            mg = np.interp(self.STs, np.linspace(self.STs.min(), self.STs.max(), mg.size), mg)

        if view_type == "mm":
            if show_red_line:
                ax.plot(self.STs, -self.payoffs, color="red", label="MM P&L")
            delta_curve = -md
            gamma_curve = -mg
        else:
            if show_red_line:
                ax.plot(self.STs, self.payoffs, color="red", label="Taker P&L")
            delta_curve = md
            gamma_curve = mg

        ax.plot(self.STs, delta_curve, color="green", label="Delta")

        # ---- FORCE DELTA AXIS TO BE SYMMETRIC (KEY FIX) ----
        if np.any(np.isfinite(delta_curve)):
            dmax = float(np.max(np.abs(delta_curve)))
            if dmax > 0:
                ax.set_ylim(-dmax, dmax)

        # ---- SINGLE, TRUE ZERO LINE ----
        ax.axhline(0, color="black", linewidth=1)
        ax.axvline(x=index_price, color="blue")

        # =====================================================
        # SECONDARY AXIS — Gamma (NO ZERO LINE HERE)
        # =====================================================
        axg = ax.twinx()
        axg.plot(
            self.STs,
            gamma_curve,
            color="violet",
            linewidth=2.0,
            alpha=0.9,
            label="Gamma (raw)",
        )
        axg.set_ylabel("Gamma exposure (raw)", color="violet")
        axg.tick_params(axis="y", labelcolor="violet")

        if np.any(np.isfinite(gamma_curve)):
            gmax = float(np.max(np.abs(gamma_curve)))
            if gmax > 0:
                axg.set_ylim(-gmax, gmax)

        axg.fill_between(
            self.STs,
            gamma_curve,
            0,
            where=(gamma_curve > 0),
            color="violet",
            alpha=0.25,
            interpolate=True,
        )

        now = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M")
        ax.set_title(f"{self.name} | {now} UTC | {plot_title}")
        ax.set_xlabel(f"${self.S0:,.0f}", fontsize=10, color="blue")

        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = axg.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, loc="upper right", framealpha=0.85)

        self.add_dankbit_signature(ax)
        return fig, ax

    # =====================================================
    # OI PLOT (UNCHANGED)
    # =====================================================
    def plot_oi(self, index_price, oi_data):
        fig, ax = plt.subplots(figsize=(18, 8))
        ax.grid(True)
        ax.axhline(0, color="black", linewidth=1)
        ax.axvline(x=index_price, color="blue")
        self.add_dankbit_signature(ax)
        return fig

    # =====================================================
    # Signature (UNCHANGED)
    # =====================================================
    def add_dankbit_signature(self, ax, logo_path=None, alpha=0.5, fontsize=16, trade_count=None):
        fig = ax.figure
        t = ax.text(
            0.98,
            0.98,
            "Dankbit™",
            transform=ax.transAxes,
            fontsize=fontsize,
            color="#6c2bd9",
            alpha=alpha,
            ha="right",
            va="top",
            fontweight="bold",
            family="monospace",
        )
        t.set_path_effects([
            path_effects.withStroke(linewidth=3, alpha=0.3, foreground="white")
        ])
