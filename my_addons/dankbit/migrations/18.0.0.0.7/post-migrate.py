# -*- coding: utf-8 -*-
"""Activate the unified multi-timeframe Forecast jobs on module upgrade."""


def migrate(cr, version):
    xmlids = (
        "dankbit_compute_next_candle_forecast_1h_cron",
        "dankbit_compute_next_candle_forecast_4h_cron",
        "dankbit_compute_next_candle_forecast_1d_cron",
        "dankbit_check_next_candle_accuracy_cron",
    )
    cr.execute(
        """
        UPDATE ir_cron
           SET active = TRUE
         WHERE id IN (
             SELECT res_id
               FROM ir_model_data
              WHERE module = 'dankbit'
                AND model = 'ir.cron'
                AND name IN %s
         )
        """,
        (xmlids,),
    )
