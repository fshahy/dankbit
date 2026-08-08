# -*- coding: utf-8 -*-
"""Auditable Thales signal bot.

4H proposes the setup, 1H (including all three expiry anchors) confirms and
times it, and 1D is a risk filter.  Every decision is append-only so later
forecast revisions cannot rewrite the forward test.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class DankbitSignal(models.Model):
    _name = "dankbit.signal"
    _description = "Thales Signal Bot"
    _order = "evaluated_at desc, id desc"

    asset = fields.Char(required=True, index=True)
    evaluated_at = fields.Datetime(required=True, index=True)
    utc_day = fields.Date(required=True, index=True)
    kind = fields.Selection([
        ("official", "Official"), ("shadow", "Shadow"),
        ("no_trade", "No Trade"),
    ], required=True, index=True)
    state = fields.Selection([
        ("watch", "Watch"), ("active", "Active"),
        ("tp", "Target Hit"), ("sl", "Stop Hit"),
        ("expired", "Expired"), ("cancelled", "Cancelled"),
        ("ambiguous", "Ambiguous"), ("rejected", "Rejected"),
    ], required=True, default="watch", index=True)
    direction = fields.Selection([
        ("long", "Long"), ("short", "Short"), ("neutral", "Neutral"),
    ], required=True, default="neutral", index=True)

    forecast_score_4h = fields.Float(digits=(16, 4))
    confirmation_score_1h = fields.Float(digits=(16, 4))
    daily_filter_score = fields.Float(digits=(16, 4))
    quality_score = fields.Float(digits=(16, 4))
    final_score = fields.Float(digits=(16, 4))
    anchor_coverage_4h = fields.Integer()
    anchor_coverage_1h = fields.Integer()
    is_weekend = fields.Boolean()
    activity_regime = fields.Char()

    entry = fields.Float(digits=(16, 4))
    stop_loss = fields.Float(digits=(16, 4))
    target = fields.Float(digits=(16, 4))
    risk_reward = fields.Float(digits=(16, 4))
    expires_at = fields.Datetime(index=True)
    closed_at = fields.Datetime(index=True)
    exit_price = fields.Float(digits=(16, 4))
    result_r = fields.Float(digits=(16, 4))
    reason = fields.Text()
    snapshot_json = fields.Text()

    @api.model
    def _latest_anchors(self, asset, timeframe, now_naive):
        """Latest revision for each E1/E2/E3, never a future-generated row."""
        rows = self.env["dankbit.forecast.next_candle"].sudo().search([
            ("asset", "=", asset), ("timeframe", "=", timeframe),
            ("generated_at", "<=", now_naive),
            ("generated_at", ">=", now_naive - timedelta(hours=8)),
        ], order="generated_at desc, revision desc")
        found = {}
        for row in rows:
            if row.expiry_index not in found and row.expiry_index in (0, 1, 2):
                found[row.expiry_index] = row
            if len(found) == 3:
                break
        return found

    @staticmethod
    def _anchor_score(rows, index_price):
        """Direction from anchor candle bodies; E1 receives highest weight."""
        weights = {0: 0.45, 1: 0.35, 2: 0.20}
        score = 0.0
        used = 0.0
        for idx, row in rows.items():
            if not row.forecast_open or not index_price:
                continue
            # 1% anchor body maps to 100 points, then clamps. This measures
            # shape/direction, not the unreliable unanchored post-E3 tail.
            body_pct = (row.forecast_close - row.forecast_open) / index_price * 100.0
            component = max(min(body_pct * 100.0, 100.0), -100.0)
            score += component * weights[idx]
            used += weights[idx]
        return (score / used if used else 0.0), len(rows)

    @staticmethod
    def _alignment(primary, confirmer):
        if abs(primary) < 20.0 or abs(confirmer) < 15.0:
            return 0.70
        return 1.0 if primary * confirmer > 0 else 0.35

    @api.model
    def _latest_snapshots(self, asset, now_naive):
        rows = self.env["dankbit.forecast.snapshot"].sudo().search([
            ("asset", "=", asset), ("bucket_start", "<=", now_naive),
            ("bucket_start", ">=", now_naive - timedelta(hours=3)),
        ], order="bucket_start desc")
        found = {}
        for row in rows:
            idx = int(row.expiry_index or 0)
            if idx in (0, 1, 2) and idx not in found:
                found[idx] = row
            if len(found) == 3:
                break
        return found

    @staticmethod
    def _quality(rows4, rows1, snapshots, is_weekend):
        coverage = min(len(rows4), len(rows1), len(snapshots)) / 3.0
        confidences = [max(min(float(r.confidence or 0.0), 100.0), 0.0)
                       for r in list(rows4.values()) + list(rows1.values())]
        confidence = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0
        # Coverage cannot rescue weak confidence: with complete anchors but
        # C50 the base quality is 60%, so a thin/neutral day cannot emit an
        # official signal merely because all three rows exist.
        quality = 100.0 * coverage * (0.20 + 0.80 * confidence)
        regimes = [str(getattr(r, "activity_regime", "") or "").lower()
                   for r in rows4.values()]
        if any("low" in r for r in regimes):
            quality *= 0.88
        if is_weekend:
            quality *= 0.90
        return max(min(quality, 100.0), 0.0)

    @staticmethod
    def _levels(direction, entry, snapshots, anchors):
        """Choose only structure-backed stop/target; never use post-E3 tail."""
        candidates = []
        for row in snapshots.values():
            candidates.extend([row.top, row.low, row.bml, row.smp])
        for row in anchors.values():
            candidates.extend([row.forecast_high, row.forecast_low, row.forecast_close])
        candidates = sorted({float(v) for v in candidates if v and float(v) > 0})
        if direction == "long":
            below = [v for v in candidates if v < entry]
            above = [v for v in candidates if v > entry]
            stop = max(below) if below else 0.0
            valid_targets = [v for v in above if stop and (v - entry) >= 2.0 * (entry - stop)]
            target = min(valid_targets) if valid_targets else 0.0
        else:
            above = [v for v in candidates if v > entry]
            below = [v for v in candidates if v < entry]
            stop = min(above) if above else 0.0
            valid_targets = [v for v in below if stop and (entry - v) >= 2.0 * (stop - entry)]
            target = max(valid_targets) if valid_targets else 0.0
        risk = abs(entry - stop) if stop else 0.0
        rr = abs(target - entry) / risk if risk and target else 0.0
        return stop, target, rr

    @api.model
    def evaluate(self):
        """Hourly cron: append one auditable decision per asset and hour."""
        now = datetime.now(timezone.utc)
        now_naive = now.replace(tzinfo=None, minute=0, second=0, microsecond=0)
        is_weekend = now.weekday() >= 5
        for asset in ("BTC", "ETH"):
            if self.sudo().search_count([("asset", "=", asset), ("evaluated_at", "=", now_naive)]):
                continue
            try:
                with self.env.cr.savepoint():
                    rows4 = self._latest_anchors(asset, "4h", now_naive)
                    rows1 = self._latest_anchors(asset, "1h", now_naive)
                    rowsd = self._latest_anchors(asset, "1d", now_naive)
                    snapshots = self._latest_snapshots(asset, now_naive)
                    entry = float(self.env["dankbit.trade"].get_index_price(asset) or 0.0)
                    score4, coverage4 = self._anchor_score(rows4, entry)
                    score1, coverage1 = self._anchor_score(rows1, entry)
                    scored, _ = self._anchor_score(rowsd, entry)
                    quality = self._quality(rows4, rows1, snapshots, is_weekend)
                    direction = "long" if score4 >= 25.0 else "short" if score4 <= -25.0 else "neutral"
                    alignment = self._alignment(score4, score1)
                    daily_mult = 1.0 if abs(scored) < 20 or score4 * scored > 0 else 0.55
                    final = score4 * (quality / 100.0) * alignment * daily_mult
                    stop, target, rr = self._levels(direction, entry, snapshots, rows4) if direction != "neutral" else (0, 0, 0)

                    min_quality = 72.0 if is_weekend else 65.0
                    reasons = []
                    if coverage4 < 3 or coverage1 < 3:
                        reasons.append("Incomplete E1/E2/E3 coverage")
                    if quality < min_quality:
                        reasons.append("Quality %.1f below %.1f threshold" % (quality, min_quality))
                    if direction == "neutral" or abs(final) < 35.0:
                        reasons.append("4H/confirmed score is not directional enough")
                    if alignment < 0.5:
                        reasons.append("1H three-anchor forecast conflicts with 4H")
                    if rr < 2.0:
                        reasons.append("No structure-backed target with R:R >= 2.0")
                    eligible = not reasons and entry > 0

                    # Serialize the daily decision even if an administrator
                    # manually triggers the cron while its scheduled run is
                    # active. No duplicate official can pass the check.
                    self.env.cr.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        ["dankbit-signal-%s-%s" % (asset, now.date().isoformat())],
                    )
                    official_exists = self.sudo().search_count([
                        ("asset", "=", asset), ("utc_day", "=", now.date()),
                        ("kind", "=", "official"),
                    ])
                    kind = "official" if eligible and not official_exists else "shadow" if eligible else "no_trade"
                    state = "active" if kind == "official" else "watch" if kind == "shadow" else "rejected"
                    if eligible and official_exists:
                        reasons.append("Daily official-signal limit reached; stored as shadow")

                    payload = {
                        "4h": {str(k): {"id": v.id, "open": v.forecast_open, "close": v.forecast_close,
                                         "confidence": v.confidence} for k, v in rows4.items()},
                        "1h": {str(k): {"id": v.id, "open": v.forecast_open, "close": v.forecast_close,
                                         "confidence": v.confidence} for k, v in rows1.items()},
                        "1d": {str(k): {"id": v.id, "open": v.forecast_open, "close": v.forecast_close,
                                         "confidence": v.confidence} for k, v in rowsd.items()},
                        "snapshot_ids": [r.id for r in snapshots.values()],
                    }
                    regime = ", ".join(sorted({r.activity_regime for r in rows4.values() if r.activity_regime}))
                    self.sudo().create({
                        "asset": asset, "evaluated_at": now_naive, "utc_day": now.date(),
                        "kind": kind, "state": state, "direction": direction,
                        "forecast_score_4h": score4, "confirmation_score_1h": score1,
                        "daily_filter_score": scored, "quality_score": quality,
                        "final_score": final, "anchor_coverage_4h": coverage4,
                        "anchor_coverage_1h": coverage1, "is_weekend": is_weekend,
                        "activity_regime": regime, "entry": entry, "stop_loss": stop,
                        "target": target, "risk_reward": rr,
                        "expires_at": now_naive + timedelta(hours=24),
                        "reason": "; ".join(reasons) if reasons else "All signal gates passed",
                        "snapshot_json": json.dumps(payload, sort_keys=True),
                    })
            except Exception:
                _logger.exception("signal bot: isolated evaluation failure for %s", asset)

    @api.model
    def check_outcomes(self):
        """Forward-test official/shadow signals against subsequent real 1H bars."""
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        open_signals = self.sudo().search([
            ("kind", "in", ("official", "shadow")),
            ("state", "in", ("active", "watch")),
        ])
        Trade = self.env["dankbit.trade"]
        for signal in open_signals:
            candles = Trade.get_candles(signal.asset, interval="1h", limit=72) or []
            relevant = []
            start_epoch = signal.evaluated_at.replace(tzinfo=timezone.utc).timestamp() * 1000
            for candle in candles:
                ts = candle.get("t", candle.get("time", candle.get("open_time", 0))) if isinstance(candle, dict) else 0
                if ts and ts < 10 ** 12:
                    ts *= 1000
                if ts >= start_epoch:
                    relevant.append(candle)
            outcome = None
            for candle in relevant:
                high = float(candle.get("h", candle.get("high", 0)))
                low = float(candle.get("l", candle.get("low", 0)))
                tp_hit = high >= signal.target if signal.direction == "long" else low <= signal.target
                sl_hit = low <= signal.stop_loss if signal.direction == "long" else high >= signal.stop_loss
                if tp_hit and sl_hit:
                    outcome = ("ambiguous", signal.entry, 0.0)
                elif tp_hit:
                    outcome = ("tp", signal.target, signal.risk_reward)
                elif sl_hit:
                    outcome = ("sl", signal.stop_loss, -1.0)
                if outcome:
                    break
            if outcome:
                signal.write({"state": outcome[0], "exit_price": outcome[1],
                              "result_r": outcome[2], "closed_at": now_naive})
            elif signal.expires_at and signal.expires_at <= now_naive:
                price = float(Trade.get_index_price(signal.asset) or signal.entry)
                risk = abs(signal.entry - signal.stop_loss) or 1.0
                pnl = (price - signal.entry) if signal.direction == "long" else (signal.entry - price)
                signal.write({"state": "expired", "exit_price": price,
                              "result_r": pnl / risk, "closed_at": now_naive})
