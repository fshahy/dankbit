# -*- coding: utf-8 -*-
"""Ensure legacy rows are assigned to the nearest-expiry chain."""


def migrate(cr, version):
    cr.execute(
        "UPDATE dankbit_forecast_snapshot SET expiry_index = 0 "
        "WHERE expiry_index IS NULL"
    )
    cr.execute(
        "UPDATE dankbit_forecast_next_candle SET expiry_index = 0 "
        "WHERE expiry_index IS NULL"
    )
