# -*- coding: utf-8 -*-
"""Make the historical single-expiry uniqueness keys multi-expiry ready."""


def migrate(cr, version):
    cr.execute(
        "ALTER TABLE dankbit_forecast_snapshot "
        "DROP CONSTRAINT IF EXISTS dankbit_forecast_snapshot_asset_bucket_uniq"
    )
    cr.execute(
        "ALTER TABLE dankbit_forecast_next_candle "
        "DROP CONSTRAINT IF EXISTS "
        "dankbit_forecast_next_candle_asset_timeframe_target_revision_uniq"
    )
