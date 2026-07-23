# -*- coding: utf-8 -*-

import logging
from datetime import timedelta

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ForecastLog(models.Model):
    _name = "dankbit.forecast.log"
    _order = "generated_at desc, hours_ahead"

    # One row per forecasted candle per cron tick (not one row per tick) —
    # 18 rows per (asset, generated_at), one per forecast.simulate_forecast()
    # step — so accuracy stats can be grouped/aggregated directly in Odoo's
    # own list/pivot views (e.g. "average error by hours_ahead" or by
    # `mode`) without unpacking a JSON blob first. Exists purely for
    # testing/improving the Thales Forecast engine (see
    # controllers/forecast.py); nothing else in this addon reads this
    # model. `index_price`/`sigma_annual` are the anchor values
    # dankbit.forecast.snapshot.get_forecast_points() used to generate the
    # whole run, denormalized onto every one of that run's rows for
    # convenience (same value repeated per run, not a relational lookup).
    #
    # There is deliberately no separate "actual observed price" table or
    # column: this model doubles as its own ground-truth price history,
    # since every cron tick's own index_price is a real observed price at
    # that tick's own generated_at. To check a candle's accuracy once its
    # target_time has passed, look up whichever later row's own
    # generated_at sits closest to that target_time and compare its
    # index_price against this row's close — a self-join on this same
    # table, e.g.:
    #   SELECT a.asset, a.hours_ahead, a.close AS predicted,
    #          b.index_price AS actual, a.close - b.index_price AS error
    #   FROM dankbit_forecast_log a
    #   JOIN LATERAL (
    #       SELECT index_price FROM dankbit_forecast_log b
    #       WHERE b.asset = a.asset AND b.generated_at >= a.target_time
    #       ORDER BY b.generated_at ASC LIMIT 1
    #   ) b ON true
    #   WHERE a.target_time <= NOW();
    asset = fields.Char(required=True, index=True)
    generated_at = fields.Datetime(required=True, index=True)
    index_price = fields.Float(digits=(16, 4))
    sigma_annual = fields.Float(digits=(16, 6))
    hours_ahead = fields.Integer(required=True)
    target_time = fields.Datetime(required=True, index=True)
    open = fields.Float(digits=(16, 4))
    high = fields.Float(digits=(16, 4))
    low = fields.Float(digits=(16, 4))
    close = fields.Float(digits=(16, 4))
    mode = fields.Char()

    # Backfilled by check_accuracy() once target_time has passed — see
    # that method's own docstring for how actual_price is sourced.
    actual_price = fields.Float(digits=(16, 4))
    error = fields.Float(digits=(16, 4))
    error_pct = fields.Float(digits=(16, 4))
    checked_at = fields.Datetime()

    def log_forecast(self):
        """Cron entry point (every 4 hours — see data/ir_cron.xml, matching
        forecast.simulate_forecast's own step_hours default so each tick's
        candles land close to a prior tick's own target times). For each of
        BTC/ETH, calls dankbit.forecast.snapshot.get_forecast_points() (the
        exact same computation /api/forecast/<asset> serves) and persists
        one row per returned candle. A run with nothing computable yet
        (see get_forecast_points) writes nothing for that asset, rather
        than a row of zeroes."""
        Snapshot = self.env["dankbit.forecast.snapshot"]
        for asset in ("BTC", "ETH"):
            result = Snapshot.get_forecast_points(asset)
            if not result["points"]:
                _logger.info("forecast.log: nothing computable yet for %s, skipping", asset)
                continue
            generated_at = result["generated_at"].replace(tzinfo=None)
            vals_list = [{
                "asset": asset,
                "generated_at": generated_at,
                "index_price": result["index_price"],
                "sigma_annual": result["sigma_annual"],
                "hours_ahead": p["hours"],
                "target_time": generated_at + timedelta(hours=p["hours"]),
                "open": p["open"], "high": p["high"], "low": p["low"], "close": p["close"],
                "mode": p["mode"],
            } for p in result["points"]]
            self.sudo().create(vals_list)

    def check_accuracy(self):
        """Cron entry point (hourly — dankbit_check_forecast_accuracy_cron).
        Backfills actual_price/error/error_pct/checked_at on every row whose
        target_time has passed and hasn't been checked yet, via the self-join
        documented above: for each such row, finds the same-asset row with
        the earliest generated_at at/after that target_time (DISTINCT ON,
        ordered by that later row's generated_at) and takes its index_price
        as the actual observed price. Raw SQL (bypasses the ORM, like every
        other bulk/aggregate lookup in this addon) since this is a
        set-based backfill across potentially many rows, not a per-record
        write. Rows with no later observation yet (recent history) are
        simply left unchecked and retried on the next tick, same
        nothing-computable-yet-skip convention every other cron here
        follows rather than writing a placeholder."""
        self.env.cr.execute("""
            UPDATE dankbit_forecast_log a
            SET actual_price = m.actual_price,
                error = a.close - m.actual_price,
                error_pct = CASE WHEN m.actual_price != 0
                            THEN (a.close - m.actual_price) / m.actual_price * 100
                            ELSE NULL END,
                checked_at = NOW()
            FROM (
                SELECT DISTINCT ON (a2.id) a2.id AS log_id, b2.index_price AS actual_price
                FROM dankbit_forecast_log a2
                JOIN dankbit_forecast_log b2
                  ON b2.asset = a2.asset AND b2.generated_at >= a2.target_time
                WHERE a2.target_time <= NOW() AND a2.checked_at IS NULL
                ORDER BY a2.id, b2.generated_at ASC
            ) m
            WHERE a.id = m.log_id
        """)
        _logger.info("forecast.log: checked accuracy for %s rows", self.env.cr.rowcount)
