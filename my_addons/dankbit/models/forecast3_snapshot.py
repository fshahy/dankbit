# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta, timezone

from odoo import fields, models

from ..controllers import options as options_lib
from ..controllers import forecast3 as forecast3_lib

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
    # same "continuously-refined-then-frozen" pattern dankbit.zones.extrema
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

    # Band edges — same "top box"/"bottom box" degenerate-pair-safe
    # zero-crossing definition options.zone_summary() already uses (each
    # curve's own highest/lowest crossing), reused here as Thales's
    # manually-drawn Resistance/Support reference band. Computed from the
    # same longs/shorts curves per_leg_greeks() also consumes, so no extra
    # query is needed just for this.
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
    # dankbit.zones.extrema's short_max_price/long_min_price already track,
    # duplicated here (not looked up from that model) per the same
    # standalone-reimplementation precedent the Trial Points feature set:
    # a new/experimental feature should not share a live code path with the
    # existing persisted history other viewers' charts depend on.
    bml = fields.Float(digits=(16, 4))
    smp = fields.Float(digits=(16, 4))

    # Manually-entered CoinGlass liquidity, carried over from whatever
    # dankbit.liquidity.snapshot row was freshest as of this bucket's
    # computation (see get_latest() there) — feeds forecast3.py's
    # Liquidity Map Engine. 0.0 on all four means nothing had been entered
    # yet at the time this row was computed (this model's own "absent"
    # convention, same as dankbit.zones.extrema's 0.0-means-no-crossing
    # fields), not a real reading of zero liquidity.
    lower_liq_price = fields.Float(digits=(16, 4))
    lower_liq_m = fields.Float(digits=(16, 4))
    upper_liq_price = fields.Float(digits=(16, 4))
    upper_liq_m = fields.Float(digits=(16, 4))

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
        active expiry, trades since 00:00 UTC — same window
        dankbit.zones.extrema._compute_asset uses for expiry_index=0) and
        upsert it into the current 4h bucket's row, refining it in place
        until the bucket rolls over (see BUCKET_HOURS). Returns the upserted
        record, or None if there's nothing computable yet (no index price,
        no active expiry, or no trades in the window — a real gap, not
        written as a row of zeroes)."""
        icp = self.env["ir.config_parameter"].sudo()
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)

        if asset == "BTC":
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        else:
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        index_price = self.env["dankbit.trade"].get_index_price(asset)
        if not index_price:
            _logger.warning("forecast3.compute_and_persist: no index price for %s, skipping", asset)
            return None

        self.env.cr.execute(
            """
            SELECT DISTINCT expiration FROM dankbit_trade
            WHERE name ILIKE %s AND expiration >= %s
            ORDER BY expiration ASC
            LIMIT 1
            """,
            (f"{asset}-%", as_of),
        )
        row = self.env.cr.fetchone()
        if not row:
            _logger.warning("forecast3.compute_and_persist: no active expiry for %s, skipping", asset)
            return None
        target_expiration = row[0]

        midnight_utc = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        domain = [
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", "=", target_expiration),
            ("deribit_ts", ">=", midnight_utc),
            ("deribit_ts", "<=", as_of),
        ]
        trades = self.env["dankbit.trade"].search(domain=domain)
        if not trades:
            _logger.warning("forecast3.compute_and_persist: no trades for %s, skipping", asset)
            return None

        longs_obj, shorts_obj = options_lib.build_zone_curves(
            asset, index_price, trades, from_price, to_price, steps
        )
        summary = options_lib.zone_summary(longs_obj.STs, longs_obj.payoffs, shorts_obj.payoffs)
        if summary["top_box"] is None or summary["bottom_box"] is None:
            _logger.warning("forecast3.compute_and_persist: no band for %s, skipping", asset)
            return None
        top = summary["top_box"][1]
        low = summary["bottom_box"][0]

        legs = forecast3_lib.per_leg_greeks(longs_obj.STs, trades)

        # dankbit.liquidity.snapshot is base.group_user-only (see
        # ir.model.access.csv); this method runs from an auth="public"
        # route (forecast3_json), so it needs an explicit sudo() or a
        # cookie-less request would 403 here the same way
        # dankbit.zones.extrema._persist_extrema() would without its own
        # sudo() (see Odoo Gotchas in CLAUDE.md).
        liquidity = self.env["dankbit.liquidity.snapshot"].sudo().get_latest(asset, as_of=as_of)

        vals = {
            "asset": asset,
            "bucket_start": self._bucket_start_for(as_of),
            "index_price": index_price,
            "top": top,
            "low": low,
            "bml": summary["long_min_price"],
            "smp": summary["short_max_price"],
            "lower_liq_price": liquidity.lower_liq_price if liquidity else 0.0,
            "lower_liq_m": liquidity.lower_liq_m if liquidity else 0.0,
            "upper_liq_price": liquidity.upper_liq_price if liquidity else 0.0,
            "upper_liq_m": liquidity.upper_liq_m if liquidity else 0.0,
        }
        vals.update(legs)

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
