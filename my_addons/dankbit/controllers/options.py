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
        direction = 'long' if self.direction == 1 else 'short'
        return f'Option(type={self.type},K={self.K}, price={self.price},direction={direction})'

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
        payoffs = (np.maximum(self.STs-K, 0) - C) * Q
        self.payoffs += payoffs
        self._add_to_self('call', K, C, 1, Q)
    
    def short_call(self, K, C, Q=1):
        payoffs = ((-1)*np.maximum(self.STs-K, 0) + C) * Q
        self.payoffs += payoffs
        self._add_to_self('call', K, C, -1, Q)
    
    def long_put(self, K, P, Q=1):
        payoffs = (np.maximum(K-self.STs, 0) - P) * Q
        self.payoffs += payoffs
        self._add_to_self('put', K, P, 1, Q)
      
    def short_put(self, K, P, Q=1):
        payoffs = ((-1)*np.maximum(K-self.STs, 0) + P) * Q
        self.payoffs += payoffs
        self._add_to_self('put', K, P, -1, Q)

    def _add_to_self(self, type_, K, price, direction, Q):
        o = Option(type_, K, price, direction)
        for _ in range(Q):
            self.instruments.append(o)

    def plot(self, index_price, market_delta, market_gammas, view_type, show_red_line, strike=None, width=18, height=8):
        fig, ax = plt.subplots(figsize=(width, height))
        plt.xticks(rotation=90) 
        if self.name.startswith("BTC"):
            ax.xaxis.set_major_locator(MultipleLocator(1000))  # Tick every 1000
            plt.yticks(list(range(-1000000, 1000001, 2000))) 
        elif self.name.startswith("ETH"):
            ax.xaxis.set_major_locator(MultipleLocator(50))  # Tick every 50
            plt.yticks(list(range(-1000000, 1000001, 10000)))
        ax.grid(True)

        # NOTE: signature is added after legend creation to allow placing it
        # next to the legend (see add_dankbit_signature implementation).
        
        berlin_time = datetime.now(ZoneInfo("Europe/Berlin"))
        now = berlin_time.strftime("%Y-%m-%d %H:%M")
        # compute plotting arrays for delta/gamma and scaled payoff
        try:
            md_arr = np.array(market_delta, dtype=float)
        except Exception:
            md_arr = np.array([0.0])
        try:
            mg_arr = np.array(market_gammas, dtype=float)
        except Exception:
            mg_arr = np.array([0.0])

        md_max = np.max(np.abs(md_arr)) if md_arr.size else 0.0
        mg_max = np.max(np.abs(mg_arr)) if mg_arr.size else 0.0

        # Config-driven gamma plotting magnification. If the config value
        # is missing or 0, fall back to automatic scaling derived from md/mg.
        gamma_scale = 1.0
        cfg_val = None
        try:
            icp = _odoo_request.env['ir.config_parameter'].sudo()
            cfg = icp.get_param('dankbit.gamma_plot_scale', default=None)
            if cfg is not None:
                try:
                    cfg_val = float(cfg)
                except Exception:
                    cfg_val = None
        except Exception:
            cfg_val = None

        if cfg_val and cfg_val > 0:
            gamma_scale = cfg_val
        else:
            if mg_max > 0:
                gamma_scale = max(md_max, 1.0) / mg_max
            else:
                gamma_scale = 1.0

        md_plot = md_arr.copy()
        mg_plot = mg_arr * gamma_scale

        max_signal = max(np.max(np.abs(md_plot)) if md_plot.size else 0.0,
                         np.max(np.abs(mg_plot)) if mg_plot.size else 0.0,
                         1.0)

        payoff_abs_max = np.max(np.abs(self.payoffs)) if self.payoffs.size else 0.0
        if payoff_abs_max > 0:
            payoff_scaled = self.payoffs * (max_signal / payoff_abs_max)
        else:
            payoff_scaled = self.payoffs

        if view_type == "mm": # for market maker
            if show_red_line:
                ax.plot(self.STs, payoff_scaled, color="red", label="Taker P&L")
            ax.plot(self.STs, -md_plot, color="green", label="Delta")
            ax.plot(self.STs, -mg_plot, color="violet", label="Gamma")

            # fill areas where mm gamma is positive
            pos_mask = -mg_plot > 0
            ax.fill_between(
                self.STs,
                -mg_plot,
                0,
                where=pos_mask,
                color="violet",
                alpha=0.3,
                interpolate=True
            )
        elif view_type == "taker":
            if show_red_line:
                ax.plot(self.STs, payoff_scaled, color="red", label="P&L")
            ax.plot(self.STs, md_plot, color="green", label="Delta")
            ax.plot(self.STs, mg_plot, color="violet", label="Gamma")

            # fill areas where mm gamma is positive
            pos_mask = mg_plot < 0
            ax.fill_between(
                self.STs,
                mg_plot,
                0,
                where=pos_mask,
                color="violet",
                alpha=0.3,
                interpolate=True
            )

        if strike is not None and isinstance(strike, str):
            view_type = strike
        if strike is not None and isinstance(strike, (int, float)):
            ax.axvline(x=strike, color="orange")
            view_type = f"MM Strike {strike}"

        ax.set_title(f"{self.name} | {now} | {view_type}")

        ymax = np.max(np.abs(plt.ylim()))
        plt.ylim(-ymax, ymax)
            
        ax.axhline(0, color='black', linewidth=1, linestyle='-')
        ax.axvline(x=index_price, color="blue")

        ax.set_xlabel(f"${self.S0:,.0f}", fontsize=10, color="blue")
        # Draw legend first so we can place the Dankbit signature beside it
        legend = ax.legend()
        # add signature beside legend (or fallback to quiet corner)
        self.add_dankbit_signature(ax)
        plt.show()

        return fig,ax

    def plot_oi(self, index_price, oi_data, plot_title):
        fig, ax = plt.subplots(figsize=(18, 8))
        # ax.xaxis.set_major_locator(MultipleLocator(1000))  # Tick every 1000

        if self.name.startswith("BTC"):
            ax.xaxis.set_major_locator(MultipleLocator(1000))  # Tick every 1000
            plt.yticks(list(range(-1000000, 1000001, 100))) 
        elif self.name.startswith("ETH"):
            ax.xaxis.set_major_locator(MultipleLocator(25))  # Tick every 25
            plt.yticks(list(range(-1000000, 1000001, 500)))

        plt.xticks(rotation=90) 
        ax.grid(True)

        # place signature after plotting bars so it can choose a clean area
        
        berlin_time = datetime.now(ZoneInfo("Europe/Berlin"))
        now = berlin_time.strftime("%Y-%m-%d %H:%M")

        if self.name.startswith("BTC"):
            for oi in oi_data:
                plt.bar(float(oi[0]) - 400/2, float(oi[1]), width=400, color='green')
                plt.bar(float(oi[0]) + 400/2, float(oi[2]), width=400, color='red')
        elif self.name.startswith("ETH"):
            for oi in oi_data:
                plt.bar(float(oi[0]) - 10/2, float(oi[1]), width=10, color='green')
                plt.bar(float(oi[0]) + 10/2, float(oi[2]), width=10, color='red')

        ax.set_title(f"{self.name} | {now} | {plot_title}")
        ax.axhline(0, color='black', linewidth=1, linestyle='-')
        ax.axvline(x=index_price, color="blue")
        ax.set_xlabel(f"${self.S0:,.0f}", fontsize=10, color="blue")
        # no legend here by default, but keep signature placement logic
        self.add_dankbit_signature(ax)
        plt.show()

        return fig
            
    def add_dankbit_signature(self, ax, logo_path=None, alpha=0.5, fontsize=16, trade_count=None):
        """
        Legend stays top-right.
        Dankbit™ signature sits immediately to the LEFT of the legend, with minimal spacing.
        Zero overlap, minimal distance.
        """
        fig = ax.figure

        # --- Force legend into top-right ---
        old_legend = ax.get_legend()
        if old_legend:
            handles, labels = old_legend.legendHandles, [t.get_text() for t in old_legend.texts]
            legend = ax.legend(handles, labels,
                            loc="upper right",
                            framealpha=0.85)
        else:
            legend = ax.legend(loc="upper right", framealpha=0.85)

        legend.get_frame().set_alpha(0.85)

        # draw now so we can measure bbox
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()

        # --- Legend bbox in axes coords ---
        lbbox = legend.get_window_extent(renderer)
        lbbox_axes = ax.transAxes.inverted().transform_bbox(lbbox)

        # legend right edge (axes fraction)
        legend_left_x  = lbbox_axes.x0
        legend_top_y   = lbbox_axes.y1

        # --- signature position: slightly left of legend ---
        pad = 0.015    # small space between signature + legend
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
            sig_x, sig_y,
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
        t.set_path_effects([
            path_effects.withStroke(linewidth=3, alpha=0.3, foreground="white")
        ])

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
