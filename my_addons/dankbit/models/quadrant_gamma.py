# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timezone

from odoo import fields, models

from ..controllers import gamma as gamma_lib

_logger = logging.getLogger(__name__)


class _QGAggTrade:
    """SQL-aggregated trade row — duck-typed for gamma.portfolio_gamma."""
    __slots__ = ("strike", "option_type", "direction", "amount", "iv", "_expiration", "_as_of")

    def __init__(self, strike, option_type, direction, expiration, amount, iv, as_of=None):
        self.strike = strike
        self.option_type = option_type
        self.direction = direction
        self.amount = amount
        self.iv = iv
        self._expiration = expiration
        self._as_of = as_of

    def get_hours_to_expiry(self):
        if not self._expiration:
            return 0.0
        now = self._as_of if self._as_of else datetime.now(timezone.utc)
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        exp = self._expiration if self._expiration.tzinfo else self._expiration.replace(tzinfo=timezone.utc)
        return max((exp - now).total_seconds() / 3600.0, 0.0)


class QuadrantGamma(models.Model):
    _name = "dankbit.quadrant.gamma"
    _order = "computed_at desc"

    asset = fields.Char(required=True, index=True)
    computed_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    index_price = fields.Float(digits=(16, 4))
    buyer_call_gamma = fields.Float(digits=(16, 4))
    buyer_put_gamma = fields.Float(digits=(16, 4))
    seller_call_gamma = fields.Float(digits=(16, 4))
    seller_put_gamma = fields.Float(digits=(16, 4))

    def compute_snapshot(self):
        for asset in ("BTC", "ETH"):
            index_price = self.env["dankbit.trade"].get_index_price(asset)
            if not index_price:
                _logger.warning("compute_snapshot: no index price for %s, skipping snapshot", asset)
                continue
            self._snapshot_asset(asset, fields.Datetime.now(), index_price)

    def _snapshot_asset(self, asset, as_of, index_price):
        """Create one quadrant-gamma row for `asset` as of `as_of`, using the trailing
        24h of trades (relative to `as_of`) still active at that time, priced with
        `index_price`. Shared by the live hourly cron and the historical backfill."""
        cr = self.env.cr
        cr.execute("""
            SELECT strike, option_type, direction, expiration,
                   SUM(amount)                                AS total_amount,
                   SUM(iv * amount) / NULLIF(SUM(amount), 0) AS weighted_iv
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= %s
              AND active = TRUE
              AND deribit_ts >= %s - INTERVAL '24 hours'
              AND deribit_ts <= %s
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{asset}%', as_of, as_of, as_of))
        rows = cr.fetchall()

        buy_call, buy_put, sell_call, sell_put = [], [], [], []
        for strike, option_type, direction, expiration, total_amount, weighted_iv in rows:
            trd = _QGAggTrade(
                strike=strike,
                option_type=option_type,
                direction=direction,
                expiration=expiration,
                amount=float(total_amount),
                iv=float(weighted_iv or 0.01),
                as_of=as_of,
            )
            if option_type == "call" and direction == "buy":
                buy_call.append(trd)
            elif option_type == "put" and direction == "buy":
                buy_put.append(trd)
            elif option_type == "call" and direction == "sell":
                sell_call.append(trd)
            elif option_type == "put" and direction == "sell":
                sell_put.append(trd)

        S = [index_price]
        computed_at = as_of.replace(tzinfo=None) if as_of.tzinfo else as_of
        self.create({
            "asset": asset,
            "computed_at": computed_at,
            "index_price": index_price,
            "buyer_call_gamma": float(gamma_lib.portfolio_gamma(S, buy_call, 0.05)[0]),
            "buyer_put_gamma": float(gamma_lib.portfolio_gamma(S, buy_put, 0.05)[0]),
            "seller_call_gamma": float(gamma_lib.portfolio_gamma(S, sell_call, 0.05)[0]),
            "seller_put_gamma": float(gamma_lib.portfolio_gamma(S, sell_put, 0.05)[0]),
        })
