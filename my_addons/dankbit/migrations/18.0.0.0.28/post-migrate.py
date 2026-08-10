# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Restore the pre-damping Forecast body defaults for existing DBs."""
    values = {
        "dankbit.forecast_flow_imbalance_body_damping": "0.0",
        "dankbit.forecast_zone_brake_min_body_mult": "1.0",
        "dankbit.eth_forecast_flow_imbalance_body_damping": "0.0",
        "dankbit.eth_forecast_zone_brake_min_body_mult": "1.0",
    }
    for key, value in values.items():
        cr.execute(
            """
            INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
            VALUES (%s, %s, 1, NOW(), 1, NOW())
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, write_uid = 1, write_date = NOW()
            """,
            (key, value),
        )
