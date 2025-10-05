# -*- coding: utf-8 -*-
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator
from matplotlib.offsetbox import OffsetImage, AnnotationBbox
import matplotlib.image as mpimg


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

    def plot(self, index_price, market_delta, market_gammas, veiw_type, hours_ago):
        fig, ax = plt.subplots(figsize=(8, 4))
        # ax.xaxis.set_major_locator(MultipleLocator(1000))  # Tick every 1000
        # plt.xticks(rotation=90) 
        ax.grid(True)
        
        berlin_time = datetime.now(ZoneInfo("Europe/Berlin"))
        now = berlin_time.strftime("%Y-%m-%d %H:%M")

        if veiw_type == "mm": # for market maker
            # ax.plot(self.STs, -self.payoffs, color="red")
            ax.plot(self.STs, -market_delta, color="green")
            ax.plot(self.STs, -market_gammas, color="violet")
        elif veiw_type == "taker":
            # ax.plot(self.STs, self.payoffs, color="red")
            ax.plot(self.STs, market_delta, color="green")
            ax.plot(self.STs, market_gammas, color="violet")

        ax.set_title(f"at ${self.S0:,.0f} | {self.name} | {now} | {veiw_type} | {hours_ago}H")
        ax.axhline(0, color='black', linewidth=1, linestyle='-')
        ax.axvline(x=index_price, linestyle="--", color="blue")
        # ax.set_ylabel('Profit $')
        plt.show()
    
        return fig
