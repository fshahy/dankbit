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
        self.STs = np.arange(from_price, to_price, step)
        self.payoffs = np.zeros_like(self.STs)
        self.longs = np.zeros_like(self.STs)
        self.shorts = np.zeros_like(self.STs)
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

    # --------------------------------------------------------------
    # longs
    def add_call_to_longs(self, K, C, Q=1):
        longs = (np.maximum(self.STs-K, 0) - C) * Q
        self.longs += longs
        self._add_to_self('call', K, C, 1, Q)

    def add_put_to_longs(self, K, P, Q=1):
        longs = (np.maximum(K-self.STs, 0) - P) * Q
        self.longs += longs
        self._add_to_self('put', K, P, 1, Q)
    # shorts
    def add_call_to_shorts(self, K, C, Q=1):
        shorts = ((-1)*np.maximum(self.STs-K, 0) + C) * Q
        self.shorts += shorts
        self._add_to_self('call', K, C, -1, Q)

    def add_put_to_shorts(self, K, P, Q=1):
        shorts = ((-1)*np.maximum(K-self.STs, 0) + P) * Q
        self.shorts += shorts
        self._add_to_self('put', K, P, -1, Q)
    # --------------------------------------------------------------
    def _add_to_self(self, type_, K, price, direction, Q):
        o = Option(type_, K, price, direction)
        for _ in range(Q):
            self.instruments.append(o)

    def plot(self, index_price, market_delta, market_gammas, view_type, show_red_line, strike=None, width=18, height=8):
        fig, ax = plt.subplots(figsize=(width, height))
        ax.xaxis.set_major_locator(MultipleLocator(1000))  # Tick every 1000
        plt.xticks(rotation=90) 
        plt.yticks(list(range(-6000, 6001, 200))) 
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
        elif view_type == "taker":
            if show_red_line:
                ax.plot(self.STs, payoff_scaled, color="red", label="P&L")
            ax.plot(self.STs, md_plot, color="green", label="Delta")
            ax.plot(self.STs, mg_plot, color="violet", label="Gamma")
        elif view_type == "be_taker":
            if show_red_line:
                ax.plot(self.STs, payoff_scaled, color="red", label="P&L")
            ax.plot(self.STs, md_plot, color="green", label="Delta")
            ax.plot(self.STs, mg_plot, color="violet", label="Gamma")
        elif view_type == "be_mm":
            if show_red_line:
                ax.plot(self.STs, payoff_scaled, color="red", label="Taker P&L")
            ax.plot(self.STs, -md_plot, color="green", label="Delta")
            ax.plot(self.STs, -mg_plot, color="violet", label="Gamma")

        if strike is not None and isinstance(strike, str):
            view_type = strike
        if strike is not None and isinstance(strike, (int, float)):
            ax.axvline(x=strike, color="orange")
            view_type = f"MM Strike {strike}"

        ax.set_title(f"{self.name} | {now} | {view_type}")

        ymax = np.max(np.abs(plt.ylim()))
        plt.ylim(-ymax, ymax)

        # --- Highlight market maker zone where gamma is negative
        if view_type in ["mm", "be_mm"] or (isinstance(strike, str) and "mm" in strike):
            ax.axhspan(-6000, 0, color="red", alpha=0.20)
            # --- Highlight weak-delta band between 0 and +50 ---
            ax.axhspan(0, 50, color="yellow", alpha=0.20)
            
        ax.axhline(0, color='black', linewidth=1, linestyle='-')
        ax.axvline(x=index_price, color="blue")

        ax.set_xlabel(f"${self.S0:,.0f}", fontsize=10, color="blue")
        # Draw legend first so we can place the Dankbit signature beside it
        legend = ax.legend()
        # add signature beside legend (or fallback to quiet corner)
        self.add_dankbit_signature(ax)
        plt.show()

        return fig

    def plot_zones(self, index_price):
        fig, ax = plt.subplots(figsize=(18, 8))
        ax.xaxis.set_major_locator(MultipleLocator(500))  # Tick every 500
        plt.xticks(rotation=90) 
        ax.grid(True)

        berlin_time = datetime.now(ZoneInfo("Europe/Berlin"))
        now = berlin_time.strftime("%Y-%m-%d %H:%M")

        ax.plot(self.STs, self.longs, color="green", label="Longs")
        ax.plot(self.STs, self.shorts, color="red", label="Shorts")

        ax.set_title(f"{self.name} | {now} | Zones")
        ax.axhline(0, color='black', linewidth=1, linestyle='-')
        ax.axvline(x=index_price, color="blue")
        ax.set_xlabel(f"${self.S0:,.0f}", fontsize=10, color="blue")
        legend = plt.legend()
        self.add_dankbit_signature(ax)
        plt.show()

        return fig
    
    def plot_oi(self, index_price, oi_data, plot_title):
        fig, ax = plt.subplots(figsize=(18, 8))
        ax.xaxis.set_major_locator(MultipleLocator(1000))  # Tick every 1000
        plt.xticks(rotation=90) 
        ax.grid(True)

        # place signature after plotting bars so it can choose a clean area
        
        berlin_time = datetime.now(ZoneInfo("Europe/Berlin"))
        now = berlin_time.strftime("%Y-%m-%d %H:%M")

        for oi in oi_data:
            plt.bar(float(oi[0]) - 400/2, float(oi[1]), width=400, color='green')
            plt.bar(float(oi[0]) + 400/2, float(oi[2]), width=400, color='red')


        ax.set_title(f"{self.name} | {now} | {plot_title}")
        ax.axhline(0, color='black', linewidth=1, linestyle='-')
        ax.axvline(x=index_price, color="blue")
        ax.set_xlabel(f"${self.S0:,.0f}", fontsize=10, color="blue")
        # no legend here by default, but keep signature placement logic
        self.add_dankbit_signature(ax)
        plt.show()

        return fig
                
    def add_dankbit_signature(sellf, ax, logo_path=None, alpha=0.5, fontsize=18):
        """
        Adds a Dankbit™ watermark or logo in a clean corner (axes-relative, never outside frame).
        Chooses the quietest corner by measuring data density.
        """
        # If a legend exists, place the signature beside it so they stay together
        try:
            fig = ax.figure
            # force a draw to compute legend bbox correctly
            fig.canvas.draw()
            legend = ax.get_legend()
        except Exception:
            legend = None

        if legend is not None:
            try:
                renderer = fig.canvas.get_renderer()
                lbbox = legend.get_window_extent(renderer)
                # convert to axes fraction coords
                lbbox_axes = ax.transAxes.inverted().transform_bbox(lbbox)
                # approximate signature size in axes fraction
                sig_w = 0.14
                sig_h = 0.06
                # prefer to place signature to the right of legend if it fits
                if lbbox_axes.x1 + sig_w + 0.01 < 1.0:
                    x = lbbox_axes.x1 + 0.01
                    ha = 'left'
                else:
                    x = lbbox_axes.x0 - 0.01
                    ha = 'right'
                # align vertically with top of legend
                y = min(max(lbbox_axes.y1 - 0.01, 0.05), 0.95)
                va = 'top'

                if logo_path:
                    try:
                        img = mpimg.imread(logo_path)
                        imagebox = OffsetImage(img, zoom=0.07, alpha=alpha)
                        ab = AnnotationBbox(
                            imagebox, (x, y),
                            xycoords="axes fraction",
                            frameon=False,
                            box_alignment=(1 if ha == "right" else 0, 1 if va == "top" else 0),
                        )
                        ax.add_artist(ab)
                        return
                    except Exception:
                        # fallback to text if logo fails
                        pass

                color = "#6c2bd9"
                t = ax.text(x, y, "Dankbit™",
                            transform=ax.transAxes,
                            fontsize=fontsize,
                            color=color,
                            alpha=alpha,
                            ha=ha,
                            va=va,
                            fontweight="bold",
                            family="monospace")
                t.set_path_effects([
                    path_effects.withStroke(linewidth=3, alpha=0.3, foreground="white")
                ])
                return
            except Exception:
                # fall through to density-based corner placement on any error
                pass

        # --- get plotted data ---
        lines = [l for l in ax.lines if l.get_visible()]
        if not lines:
            all_x = np.array([0, 1])
            all_y = np.array([0, 1])
        else:
            all_x = np.concatenate([l.get_xdata() for l in lines if len(l.get_xdata())])
            all_y = np.concatenate([l.get_ydata() for l in lines if len(l.get_ydata())])

        # normalize to [0,1] (axes coords) to compare regions
        x_norm = (all_x - np.min(all_x)) / (np.ptp(all_x) + 1e-9)
        y_norm = (all_y - np.min(all_y)) / (np.ptp(all_y) + 1e-9)

        # estimate density in each corner region
        def corner_density(x0, x1, y0, y1):
            mask = (x_norm >= x0) & (x_norm <= x1) & (y_norm >= y0) & (y_norm <= y1)
            return np.count_nonzero(mask)

        corners = {
            "bottom left":  (0.00, 0.25, 0.00, 0.25),
            "bottom right": (0.75, 1.00, 0.00, 0.25),
            "top left":     (0.00, 0.25, 0.75, 1.00),
            "top right":    (0.75, 1.00, 0.75, 1.00),
        }
        densities = {k: corner_density(*v) for k, v in corners.items()}
        corner = min(densities, key=densities.get)

        # assign position in axes coordinates
        x, y, ha, va = {
            "bottom left":  (0.03, 0.03, "left", "bottom"),
            "bottom right": (0.97, 0.03, "right", "bottom"),
            "top left":     (0.03, 0.97, "left", "top"),
            "top right":    (0.97, 0.97, "right", "top"),
        }[corner]

        # --- draw logo or text ---
        if logo_path:
            try:
                img = mpimg.imread(logo_path)
                imagebox = OffsetImage(img, zoom=0.07, alpha=alpha)
                ab = AnnotationBbox(
                    imagebox, (x, y),
                    xycoords="axes fraction",
                    frameon=False,
                    box_alignment=(1 if ha == "right" else 0, 1 if va == "top" else 0),
                )
                ax.add_artist(ab)
            except Exception as e:
                ax.text(0.5, 0.02, f"Dankbit™ (logo missing: {e})",
                        transform=ax.transAxes, ha="center", va="bottom", fontsize=8, color="gray", alpha=0.5)
        else:
            color = "#6c2bd9"
            t = ax.text(x, y, "Dankbit™",
                        transform=ax.transAxes,
                        fontsize=fontsize,
                        color=color,
                        alpha=alpha,
                        ha=ha,
                        va=va,
                        fontweight="bold",
                        family="monospace")
            t.set_path_effects([
                path_effects.withStroke(linewidth=3, alpha=0.3, foreground="white")
            ])
            