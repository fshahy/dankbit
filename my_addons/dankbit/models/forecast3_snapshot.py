# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta, timezone

from odoo import fields, models

_logger = logging.getLogger(__name__)

# Bucket size for the snapshot history (independent of the forecast's own
# horizon, see forecast3.simulate_forecast3's hours_ahead) — a snapshot is
# taken once per bucket of this size, giving the Gamma-Band Consensus
# engine (see forecast3.py) a real time-ordered history to compute slopes
# from. 4h matches the candle spacing both the source Pine script and
# Dankbit's own forecast steps use.
BUCKET_HOURS = 4


class Forecast3Snapshot(models.Model):
    _name = "dankbit.forecast3.snapshot"
    _order = "bucket_start"

    # One row per (asset, bucket_start) — continuously refined while its
    # bucket is "current", then frozen once time moves past it, exactly the
    # same "continuously-refined-then-frozen" pattern dankbit.bands
    # uses per-instrument (see _persist_extrema there), just keyed by a
    # rolling 4h time bucket instead of an expiry instrument. This is the
    # real historical time series Thales's manually-dated CSV rows provide
    # in the source Pine script — Dankbit has no human typing in daily
    # snapshots, so this model exists purely to give the Gamma-Band
    # Consensus engine (forecast3.gamma_band_consensus) the 3 real
    # historical points it needs to compute a top/low/gamma slope over
    # actual elapsed time.
    asset = fields.Char(required=True, index=True)
    bucket_start = fields.Datetime(required=True, index=True)
    index_price = fields.Float(digits=(16, 4))

    # Band edges — same values as dankbit.bands's high_zone_max/
    # low_zone_min (each curve's own highest/lowest zero-crossing, same
    # skip-absent-sides rule options.zone_summary() uses), read straight
    # off that model's _compute_asset() rather than re-derived here,
    # reused as Thales's manually-drawn Resistance/Support reference band.
    top = fields.Float(digits=(16, 4))
    low = fields.Float(digits=(16, 4))

    # Per-leg gamma extrema (price where that leg's dollar-gamma curve
    # peaks/bottoms) plus the peak/bottom value itself, abs/1_000_000 (same
    # scale the /<instrument>/zones page's Gamma Peak Value lines use) —
    # Thales's BCG/BPG/SCG/SPG + BCGAbs/BPGAbs/SCGAbs/SPGAbs.
    bcg_price = fields.Float(digits=(16, 4))
    bpg_price = fields.Float(digits=(16, 4))
    scg_price = fields.Float(digits=(16, 4))
    spg_price = fields.Float(digits=(16, 4))
    bcg_abs = fields.Float(digits=(16, 4))
    bpg_abs = fields.Float(digits=(16, 4))
    scg_abs = fields.Float(digits=(16, 4))
    spg_abs = fields.Float(digits=(16, 4))

    # Per-leg delta-saturation prices/values, abs/10 — Thales's
    # BCD/BPD/SCD/SPD + BCDAbs/BPDAbs/SCDAbs/SPDAbs.
    bcd_price = fields.Float(digits=(16, 4))
    bpd_price = fields.Float(digits=(16, 4))
    scd_price = fields.Float(digits=(16, 4))
    spd_price = fields.Float(digits=(16, 4))
    bcd_abs = fields.Float(digits=(16, 4))
    bpd_abs = fields.Float(digits=(16, 4))
    scd_abs = fields.Float(digits=(16, 4))
    spd_abs = fields.Float(digits=(16, 4))

    # Per-leg theta extrema prices/values, abs/10_000 — Thales's
    # BCT/BPT/SCT/SPT + BCTAbs/BPTAbs/SCTAbs/SPTAbs.
    bct_price = fields.Float(digits=(16, 4))
    bpt_price = fields.Float(digits=(16, 4))
    sct_price = fields.Float(digits=(16, 4))
    spt_price = fields.Float(digits=(16, 4))
    bct_abs = fields.Float(digits=(16, 4))
    bpt_abs = fields.Float(digits=(16, 4))
    sct_abs = fields.Float(digits=(16, 4))
    spt_abs = fields.Float(digits=(16, 4))

    # Per-leg vega extrema prices/values, abs/100 — Thales's
    # BCV/BPV/SCV/SPV + BCVAbs/BPVAbs/SCVAbs/SPVAbs.
    bcv_price = fields.Float(digits=(16, 4))
    bpv_price = fields.Float(digits=(16, 4))
    scv_price = fields.Float(digits=(16, 4))
    spv_price = fields.Float(digits=(16, 4))
    bcv_abs = fields.Float(digits=(16, 4))
    bpv_abs = fields.Float(digits=(16, 4))
    scv_abs = fields.Float(digits=(16, 4))
    spv_abs = fields.Float(digits=(16, 4))

    # Curve extremes — Thales's BML (Buyer Max Loss = Longs curve bottom)
    # and SMP (Seller Max Profit = Shorts curve peak); same values
    # dankbit.bands's buyer_max_loss/seller_max_profit already
    # track, read from that model's _compute_asset() (see
    # compute_and_persist) but kept as this model's own columns rather
    # than a relational reference, since this snapshot's whole point is to
    # freeze one moment's values into its own row.
    bml = fields.Float(digits=(16, 4))
    smp = fields.Float(digits=(16, 4))

    _sql_constraints = [
        ("asset_bucket_uniq", "unique (asset, bucket_start)",
         "Only one Forecast 3 snapshot is kept per asset per time bucket."),
    ]

    def _bucket_start_for(self, as_of):
        epoch_hours = int(as_of.timestamp() // 3600)
        bucket_epoch_hours = (epoch_hours // BUCKET_HOURS) * BUCKET_HOURS
        return datetime.fromtimestamp(bucket_epoch_hours * 3600, tz=timezone.utc).replace(tzinfo=None)

    def compute_and_persist(self, asset):
        """Compute this moment's per-leg Greeks/band data for `asset` (nearest
        active expiry, trades since 00:00 UTC) and upsert it into the current
        4h bucket's row, refining it in place until the bucket rolls over
        (see BUCKET_HOURS). Returns the upserted record, or None if there's
        nothing computable yet (no index price, no active expiry, or no
        trades in the window — a real gap, not written as a row of zeroes).

        Everything below is read straight off
        dankbit.bands._compute_asset(asset, expiry_index=0) — the
        exact same computation that feeds the TradingView chart's yellow
        zones box, and which already returns high_zone_max/low_zone_min
        (identical to this row's own top/low: each curve's own highest/
        lowest zero-crossing, same skip-absent-sides rule) and all 32
        per-leg gamma/delta/theta/vega price+Abs fields (via its own call
        into forecast3_lib.per_leg_greeks()) since that model started
        persisting them too. This method used to re-fetch `asset`'s trades
        and call build_zone_curves()/per_leg_greeks() a second time just to
        get numbers _compute_asset() had already computed a moment
        earlier — removed as pure duplicate work now that both models
        agree on field names, so Thales Forecast's band and per-leg
        Greeks can never quietly drift from what's actually drawn
        elsewhere in the app, and never do the same query/curve-build
        twice for one asset."""
        bands_data = self.env["dankbit.bands"]._compute_asset(asset, expiry_index=0)
        if not bands_data:
            _logger.warning("forecast3.compute_and_persist: dankbit.bands has nothing computable for %s, skipping", asset)
            return None

        top = bands_data["high_zone_max"]
        low = bands_data["low_zone_min"]
        if not top or not low:
            _logger.warning("forecast3.compute_and_persist: no band for %s, skipping", asset)
            return None

        as_of = bands_data["computed_at"]

        vals = {
            "asset": asset,
            "bucket_start": self._bucket_start_for(as_of),
            "index_price": bands_data["index_price"],
            "top": top,
            "low": low,
            "bml": bands_data["buyer_max_loss"],
            "smp": bands_data["seller_max_profit"],
        }
        vals.update({f: bands_data[f] for f in self.env["dankbit.bands"]._PER_LEG_GREEK_FIELDS})

        record = self.sudo().search([
            ("asset", "=", asset),
            ("bucket_start", "=", vals["bucket_start"]),
        ], limit=1)
        if record:
            record.write(vals)
            return record
        return self.sudo().create(vals)

    def recent_history(self, asset, limit=3):
        """The `limit` most recent snapshot rows for `asset`, newest first —
        feeds forecast3.gamma_band_consensus's 3-point slope calculation.
        History only ever accumulates as a side effect of
        compute_and_persist() being called live from /api/forecast3/<asset>
        (i.e. while the "Thales Forecast" checkbox is open somewhere) — no
        cron fallback, since a gap here just widens the Gamma-Band
        Consensus engine's slope window rather than breaking anything (see
        gamma_band_consensus()'s real-elapsed-hours normalization)."""
        return self.sudo().search(
            [("asset", "=", asset)], order="bucket_start desc", limit=limit
        )
