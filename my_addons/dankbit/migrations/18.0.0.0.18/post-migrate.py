# -*- coding: utf-8 -*-

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Enable the five hourly/accuracy jobs required by the live dashboard.

    ir_cron.xml is noupdate=1, so changing its ``active`` values only fixes a
    clean installation.  Existing databases need this migration or their
    Bands, Smart Liquidity and Forecast histories remain frozen after the
    module upgrade.
    """
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    xmlids = (
        "dankbit.dankbit_compute_bands_cron",
        "dankbit.dankbit_compute_live_band_cron",
        "dankbit.dankbit_compute_forecast_snapshot_cron",
        "dankbit.dankbit_log_forecast_cron",
        "dankbit.dankbit_check_forecast_accuracy_cron",
    )
    for xmlid in xmlids:
        cron = env.ref(xmlid, raise_if_not_found=False)
        if not cron:
            _logger.warning("Required Dankbit cron not found during migration: %s", xmlid)
            continue
        cron.sudo().write({"active": True})

