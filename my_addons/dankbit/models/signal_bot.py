# -*- coding: utf-8 -*-
"""Auditable Thales signal bot.

Closed Daily and 4H price trends form the hard direction gate. Forecast,
moving Greek concentration and option structure confirm the setup; closed 1H
price action times it. Every decision is append-only so later forecast
revisions cannot rewrite the forward test.
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
        ("watch", "Watch"), ("armed", "Armed"), ("active", "Active"),
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
    trend_daily = fields.Selection([
        ("up", "Up"), ("down", "Down"), ("neutral", "Neutral"),
    ], default="neutral", index=True)
    trend_4h = fields.Selection([
        ("up", "Up"), ("down", "Down"), ("neutral", "Neutral"),
    ], default="neutral", index=True)
    trend_1h = fields.Selection([
        ("up", "Up"), ("down", "Down"), ("neutral", "Neutral"),
    ], default="neutral", index=True)
    trend_score_daily = fields.Float(digits=(16, 4))
    trend_score_4h = fields.Float(digits=(16, 4))
    gamma_direction_score = fields.Float(digits=(16, 4))
    greek_direction_score = fields.Float(digits=(16, 4))
    setup_stage = fields.Selection([
        ("none", "No Setup"), ("setup", "Setup Detected"),
        ("armed", "Armed"), ("confirmed", "Entry Confirmed"),
    ], required=True, default="none", index=True)
    setup_path = fields.Selection([
        ("rtm_ftb", "RTM FTB"),
        ("breakout_retest", "Breakout-Retest"),
    ], index=True)
    plan_status = fields.Selection([
        ("watch", "Watch"), ("planned", "Planned"),
        ("armed", "Armed"), ("triggered", "Triggered"),
        ("cancelled", "Cancelled"),
    ], index=True)
    entry_zone_low = fields.Float(digits=(16, 4))
    entry_zone_high = fields.Float(digits=(16, 4))
    invalidation_level = fields.Float(digits=(16, 4))
    target_2 = fields.Float(digits=(16, 4))
    breakout_level = fields.Float(digits=(16, 4))
    breakout_at = fields.Datetime(index=True)
    plan_provisional = fields.Boolean()
    trigger_level = fields.Float(digits=(16, 4))
    trigger_basis = fields.Char()
    liquidity_swept = fields.Boolean()
    rtm_score = fields.Float(digits=(16, 4))
    rtm_zone_low = fields.Float(digits=(16, 4))
    rtm_zone_high = fields.Float(digits=(16, 4))
    rtm_zone_type = fields.Selection([
        ("demand", "Demand"), ("supply", "Supply"),
    ])
    rtm_structure = fields.Selection([
        ("rbr", "Rally-Base-Rally"), ("dbr", "Drop-Base-Rally"),
        ("dbd", "Drop-Base-Drop"), ("rbd", "Rally-Base-Drop"),
    ], index=True)
    rtm_opposing_structure = fields.Selection([
        ("rbr", "Rally-Base-Rally"), ("dbr", "Drop-Base-Rally"),
        ("dbd", "Drop-Base-Drop"), ("rbd", "Rally-Base-Drop"),
    ])
    rtm_opposing_level = fields.Float(digits=(16, 4))
    rtm_path_clear = fields.Boolean()
    rtm_touch_count = fields.Integer()
    rtm_fresh = fields.Boolean()
    rtm_base_candles = fields.Integer()
    rtm_departure_score = fields.Float(digits=(16, 4))
    rtm_compression = fields.Boolean()
    rtm_gap = fields.Boolean()
    rtm_nested = fields.Boolean()
    entry_model = fields.Selection([
        ("ftb_confirmed", "FTB Confirmed"),
        ("ftb_reclaim_shadow", "FTB Reclaim Shadow"),
        ("later_touch_shadow", "Later Touch Confirmed Shadow"),
        ("breakout_watch", "Breakout Watch"),
        ("breakout_retest_shadow", "Breakout Retest Shadow"),
        ("breakout_retest_confirmed", "Breakout Retest Confirmed"),
    ])

    entry = fields.Float(digits=(16, 4))
    stop_loss = fields.Float(digits=(16, 4))
    target = fields.Float(digits=(16, 4))
    risk_reward = fields.Float(digits=(16, 4))
    swing_level = fields.Float(digits=(16, 4))
    atr_4h = fields.Float(digits=(16, 4))
    stop_basis = fields.Char()
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
        seen_expiries = set()
        for row in rows:
            if row.expiry_index not in (0, 1, 2) or row.expiry_index in seen_expiries:
                continue
            seen_expiries.add(row.expiry_index)
            if not self._snapshot_complete(row.snapshot_id):
                continue
            found[row.expiry_index] = row
            if len(seen_expiries) == 3:
                break
        return found

    @staticmethod
    def _snapshot_complete(snapshot):
        if not snapshot:
            return False
        for leg in ("bc", "bp", "sc", "sp"):
            supported = any(
                getattr(snapshot, leg + greek + "_price", 0.0) > 0
                and getattr(snapshot, leg + greek + "_abs", 0.0) > 0
                for greek in ("g", "d", "t", "v")
            )
            if not supported:
                return False
        return True

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
        seen_expiries = set()
        for row in rows:
            idx = int(row.expiry_index or 0)
            if idx not in (0, 1, 2) or idx in seen_expiries:
                continue
            seen_expiries.add(idx)
            if not self._snapshot_complete(row):
                continue
            found[idx] = row
            if len(seen_expiries) == 3:
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
    def _atr(candles, period=14):
        if len(candles) < 2:
            return 0.0
        rows = candles[-(period + 1):]
        true_ranges = []
        for prev, current in zip(rows, rows[1:]):
            high = float(current.get("h", current.get("high", 0.0)))
            low = float(current.get("l", current.get("low", 0.0)))
            prev_close = float(prev.get("c", prev.get("close", 0.0)))
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return sum(true_ranges[-period:]) / len(true_ranges[-period:]) if true_ranges else 0.0

    @staticmethod
    def _ema(values, period):
        if not values:
            return 0.0
        alpha = 2.0 / (period + 1.0)
        value = float(values[0])
        for item in values[1:]:
            value = alpha * float(item) + (1.0 - alpha) * value
        return value

    @classmethod
    def _market_trend(cls, candles):
        """Closed-candle trend from EMA location/slope and confirmed swings."""
        closed = list(candles or [])[:-1]
        if len(closed) < 55:
            return "neutral", 0.0
        closes = [float(c.get("c", c.get("close", 0.0))) for c in closed]
        ema20 = cls._ema(closes[-55:], 20)
        ema50 = cls._ema(closes[-55:], 50)
        ema20_prev = cls._ema(closes[-55:-5], 20)
        score = 0.0
        score += 25.0 if closes[-1] > ema20 else -25.0
        score += 20.0 if ema20 > ema20_prev else -20.0
        score += 20.0 if ema20 > ema50 else -20.0

        swing_highs, swing_lows = [], []
        for i in range(2, len(closed) - 2):
            high = float(closed[i].get("h", closed[i].get("high", 0.0)))
            low = float(closed[i].get("l", closed[i].get("low", 0.0)))
            around = closed[i - 2:i] + closed[i + 1:i + 3]
            if all(high > float(c.get("h", c.get("high", 0.0))) for c in around):
                swing_highs.append(high)
            if all(low < float(c.get("l", c.get("low", 0.0))) for c in around):
                swing_lows.append(low)
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            if swing_highs[-1] > swing_highs[-2] and swing_lows[-1] > swing_lows[-2]:
                score += 35.0
            elif swing_highs[-1] < swing_highs[-2] and swing_lows[-1] < swing_lows[-2]:
                score -= 35.0
        score = max(min(score, 100.0), -100.0)
        return ("up" if score >= 40.0 else "down" if score <= -40.0 else "neutral"), score

    @api.model
    def _option_direction_scores(self, asset, now_naive):
        """Direction of Greek concentration centres across real hourly scans.

        This measures movement, not a one-scan absolute level. Gamma carries
        half the combined Greek score; Delta, Vega and Theta provide secondary
        confirmation. Only complete snapshots from the same expiry are paired.
        """
        rows = self.env["dankbit.forecast.snapshot"].sudo().search([
            ("asset", "=", asset), ("bucket_start", "<=", now_naive),
            ("bucket_start", ">=", now_naive - timedelta(hours=8)),
        ], order="bucket_start asc")
        grouped = {0: [], 1: [], 2: []}
        for row in rows:
            idx = int(row.expiry_index or 0)
            if idx in grouped and self._snapshot_complete(row):
                grouped[idx].append(row)

        def centre(row, greek):
            weighted = []
            for leg in ("bc", "bp", "sc", "sp"):
                price = float(getattr(row, leg + greek + "_price", 0.0) or 0.0)
                strength = float(getattr(row, leg + greek + "_abs", 0.0) or 0.0)
                if price > 0 and strength > 0:
                    weighted.append((price, strength))
            total = sum(w for _, w in weighted)
            return sum(p * w for p, w in weighted) / total if total else 0.0

        scores = {g: [] for g in ("g", "d", "v", "t")}
        for expiry_rows in grouped.values():
            if len(expiry_rows) < 2:
                continue
            first, last = expiry_rows[0], expiry_rows[-1]
            index_price = float(last.index_price or 0.0)
            if not index_price:
                continue
            for greek in scores:
                start, end = centre(first, greek), centre(last, greek)
                if start and end:
                    # A 0.5% centre shift maps to a full directional score.
                    scores[greek].append(max(min((end - start) / index_price * 20000.0, 100.0), -100.0))
        averaged = {g: (sum(values) / len(values) if values else 0.0) for g, values in scores.items()}
        combined = (0.50 * averaged["g"] + 0.25 * averaged["d"]
                    + 0.15 * averaged["v"] + 0.10 * averaged["t"])
        return averaged["g"], combined, averaged

    @classmethod
    def _structural_stop(cls, direction, entry, candles):
        """Stop beyond latest confirmed 4H swing, never inside market noise."""
        closed = list(candles or [])[:-1]  # Binance's last bar is forming.
        atr = cls._atr(closed)
        if len(closed) < 5 or not entry:
            return 0.0, 0.0, atr

        swing = 0.0
        for i in range(len(closed) - 3, 1, -1):
            high = float(closed[i].get("h", closed[i].get("high", 0.0)))
            low = float(closed[i].get("l", closed[i].get("low", 0.0)))
            before = closed[i - 2:i]
            after = closed[i + 1:i + 3]
            if direction == "short":
                confirmed = all(high > float(c.get("h", c.get("high", 0.0))) for c in before + after)
                if confirmed and high > entry:
                    swing = high
                    break
            else:
                confirmed = all(low < float(c.get("l", c.get("low", 0.0))) for c in before + after)
                if confirmed and low < entry:
                    swing = low
                    break
        if not swing:
            return 0.0, 0.0, atr

        buffer_distance = max(0.10 * atr, 0.001 * entry)
        minimum_risk = max(0.30 * atr, 0.0025 * entry)
        if direction == "short":
            stop = max(swing + buffer_distance, entry + minimum_risk)
        else:
            stop = min(swing - buffer_distance, entry - minimum_risk)
        return stop, swing, atr

    @staticmethod
    def _levels(direction, entry, stop, snapshots, anchors):
        """Choose the strongest reachable structure target; 1:2 is the floor."""
        candidates = []
        for row in snapshots.values():
            candidates.extend([row.top, row.low, row.bml, row.smp])
        for row in anchors.values():
            candidates.extend([row.forecast_high, row.forecast_low, row.forecast_close])
        candidates = sorted({float(v) for v in candidates if v and float(v) > 0})
        if direction == "long":
            above = [v for v in candidates if v > entry]
            valid_targets = [v for v in above if stop and (v - entry) >= 2.0 * (entry - stop)]
            preferred = [v for v in valid_targets if (v - entry) <= 4.0 * (entry - stop)]
            target = max(preferred) if preferred else min(valid_targets) if valid_targets else 0.0
        else:
            below = [v for v in candidates if v < entry]
            valid_targets = [v for v in below if stop and (entry - v) >= 2.0 * (stop - entry)]
            preferred = [v for v in valid_targets if (entry - v) <= 4.0 * (stop - entry)]
            target = min(preferred) if preferred else max(valid_targets) if valid_targets else 0.0
        risk = abs(entry - stop) if stop else 0.0
        rr = abs(target - entry) / risk if risk and target else 0.0
        return stop, target, rr

    @api.model
    def _entry_reference_levels(self, asset, direction, snapshots):
        """Confirmed Zone/Band and Smart-Liquidity levels for entry timing.

        Bands are matched to the exact expiry instruments already accepted by
        the complete snapshots. Candidate/unconfirmed zone fields are never
        used, so a noisy hourly recomputation cannot arm the bot by itself.
        """
        levels = []
        instruments = [r.expiry_instrument for r in snapshots.values() if r.expiry_instrument]
        bands = self.env["dankbit.bands"].sudo().search([
            ("asset", "=", asset), ("instrument", "in", instruments),
        ]) if instruments else self.env["dankbit.bands"]
        for row in bands:
            if direction == "long":
                values = [
                    (row.smart_liq_lower_price, "Smart Liquidity Lower"),
                    (row.low_zone_min, "Low Zone"),
                    (row.low_zone_max, "Low Zone"),
                    (row.low_support, "Red/Support Band"),
                ]
            else:
                values = [
                    (row.smart_liq_upper_price, "Smart Liquidity Upper"),
                    (row.high_zone_min, "High Zone"),
                    (row.high_zone_max, "High Zone"),
                    (row.high_resistance, "Green/Resistance Band"),
                ]
            levels.extend((float(value), label) for value, label in values if value and float(value) > 0)

        # Snapshot top/low is the frozen structural band fallback. It is only
        # used when an expiry's persisted band row is temporarily unavailable.
        if not levels:
            label = "Snapshot Support Band" if direction == "long" else "Snapshot Resistance Band"
            for row in snapshots.values():
                value = row.low if direction == "long" else row.top
                if value and float(value) > 0:
                    levels.append((float(value), label))
        return levels

    @classmethod
    def _entry_trigger(cls, direction, entry, candles, reference_levels, atr_4h=0.0):
        """Require a closed-1H sweep/reclaim followed by micro structure break.

        The last Binance bar is forming and is deliberately ignored. A level
        touch alone never confirms an entry: price must sweep the level, close
        back on the safe side, and a later closed candle must break the sweep
        candle's opposite extreme in the 4H direction.
        """
        closed = list(candles or [])[:-1]
        atr_1h = cls._atr(closed)
        if len(closed) < 6 or not entry or not reference_levels:
            return False, 0.0, "", False, atr_1h

        tolerance = max(0.12 * atr_1h, 0.0008 * entry)
        max_distance = max(1.25 * atr_4h, 3.0 * atr_1h, 0.006 * entry)
        if direction == "long":
            usable = [(v, name) for v, name in reference_levels
                      if v <= entry + tolerance and abs(entry - v) <= max_distance]
            usable.sort(key=lambda item: item[0], reverse=True)
        else:
            usable = [(v, name) for v, name in reference_levels
                      if v >= entry - tolerance and abs(entry - v) <= max_distance]
            usable.sort(key=lambda item: item[0])

        recent = closed[-6:]
        swept_candidate = None
        for level, basis in usable:
            # Leave at least one later closed candle for confirmation.
            for pos in range(len(recent) - 2, -1, -1):
                candle = recent[pos]
                low = float(candle.get("l", candle.get("low", 0.0)))
                high = float(candle.get("h", candle.get("high", 0.0)))
                close = float(candle.get("c", candle.get("close", 0.0)))
                later = recent[pos + 1:]
                if direction == "long":
                    swept = low <= level + tolerance and close > level
                    confirmed = any(float(c.get("c", c.get("close", 0.0))) > high for c in later)
                else:
                    swept = high >= level - tolerance and close < level
                    confirmed = any(float(c.get("c", c.get("close", 0.0))) < low for c in later)
                if swept and confirmed:
                    return True, level, basis, True, atr_1h
                if swept and swept_candidate is None:
                    swept_candidate = (level, basis)
        if swept_candidate:
            return False, swept_candidate[0], swept_candidate[1], True, atr_1h
        return False, (usable[0][0] if usable else 0.0), (usable[0][1] if usable else ""), False, atr_1h

    @classmethod
    def _breakout_retest_setup(cls, candles_4h, candles_1h, index_price):
        """Detect a closed-4H structural break and time its 1H retest.

        This is an independent entry path beside RTM FTB.  It never chases
        the breakout candle: WATCH exposes a provisional plan, ARMED means a
        1H retest/rejection exists, and CONFIRMED requires a later closed 1H
        body to break the rejection candle's opposite extreme.
        """
        closed4 = list(candles_4h or [])[:-1]
        closed1 = list(candles_1h or [])[:-1]
        atr4 = cls._atr(closed4)
        atr1 = cls._atr(closed1)
        if len(closed4) < 20 or len(closed1) < 12 or not atr4 or not atr1 or not index_price:
            return None

        def v(row, key):
            aliases = {"o": "open", "h": "high", "l": "low", "c": "close", "t": "time"}
            return float(row.get(key, row.get(aliases[key], 0.0)) or 0.0)

        def ts_ms(row):
            stamp = v(row, "t")
            return stamp * 1000.0 if stamp and stamp < 10 ** 12 else stamp

        breakout = None
        # A watch remains relevant for at most six completed 4H bars (24h).
        for idx in range(len(closed4) - 1, max(6, len(closed4) - 7), -1):
            candle = closed4[idx]
            prior = closed4[max(0, idx - 6):idx]
            if len(prior) < 4:
                continue
            open_, high, low, close = (v(candle, k) for k in ("o", "h", "l", "c"))
            candle_range = max(high - low, 1e-9)
            body = close - open_
            body_atr = abs(body) / atr4
            body_share = abs(body) / candle_range
            prior_high = max(v(c, "h") for c in prior)
            prior_low = min(v(c, "l") for c in prior)
            close_location = (close - low) / candle_range
            short_break = body < 0 and close < prior_low - 0.10 * atr4 and close_location <= 0.25
            long_break = body > 0 and close > prior_high + 0.10 * atr4 and close_location >= 0.75
            if body_atr < 0.80 or body_share < 0.55 or not (short_break or long_break):
                continue
            direction = "short" if short_break else "long"
            level = prior_low if short_break else prior_high
            breakout = {
                "direction": direction, "level": level, "at_ms": ts_ms(candle),
                "candle_high": high, "candle_low": low,
                "body_atr": body_atr, "body_share": body_share,
                "atr_4h": atr4, "atr_1h": atr1,
            }
            break
        if not breakout:
            return None

        level = breakout["level"]
        direction = breakout["direction"]
        tolerance = max(0.25 * atr1, 0.001 * index_price)
        # The 4H timestamp is its open time. Retest evidence may only start
        # after that breakout candle has fully closed, never from one of its
        # own constituent 1H bars.
        breakout_close_ms = breakout["at_ms"] + 4.0 * 3600.0 * 1000.0 if breakout["at_ms"] else 0.0
        after = [c for c in closed1 if not breakout_close_ms or ts_ms(c) >= breakout_close_ms]
        retest = confirmation = None
        for pos, candle in enumerate(after):
            high, low, close, open_ = (v(candle, k) for k in ("h", "l", "c", "o"))
            if direction == "short":
                rejected = high >= level - tolerance and close < level and close < open_
            else:
                rejected = low <= level + tolerance and close > level and close > open_
            if not rejected:
                continue
            retest = candle
            for later in after[pos + 1:]:
                later_close = v(later, "c")
                if (direction == "short" and later_close < low) or (direction == "long" and later_close > high):
                    confirmation = later
                    break
            break

        stage = "confirmed" if confirmation else "armed" if retest else "watch"
        entry_zone_low = level - tolerance
        entry_zone_high = level + tolerance
        if confirmation:
            plan_entry = v(confirmation, "c")
        elif retest:
            plan_entry = v(retest, "c")
        else:
            plan_entry = level

        recent_after = after[-8:] if after else []
        if direction == "short":
            local_high = (max(v(c, "h") for c in recent_after)
                          if retest and recent_after else breakout["candle_high"])
            invalidation = local_high + 0.15 * atr1
            risk = invalidation - plan_entry
            tp1 = plan_entry - 2.0 * risk if risk > 0 else 0.0
            tp2 = plan_entry - 3.0 * risk if risk > 0 else 0.0
        else:
            local_low = (min(v(c, "l") for c in recent_after)
                         if retest and recent_after else breakout["candle_low"])
            invalidation = local_low - 0.15 * atr1
            risk = plan_entry - invalidation
            tp1 = plan_entry + 2.0 * risk if risk > 0 else 0.0
            tp2 = plan_entry + 3.0 * risk if risk > 0 else 0.0
        if risk <= 0:
            return None
        breakout.update({
            "stage": stage, "entry": plan_entry,
            "entry_zone_low": entry_zone_low, "entry_zone_high": entry_zone_high,
            "stop": invalidation, "invalidation": invalidation,
            "target": tp1, "target_2": tp2, "risk_reward": 2.0,
            "retest_seen": bool(retest), "confirmed": bool(confirmation),
        })
        return breakout

    @classmethod
    def _rtm_zone_candidates(cls, direction, candles, reference_levels):
        """Detect auditable 1H RTM bases after the Thales setup exists.

        This detector is intentionally local to Signal Bot. It does not alter
        or replace Thales Zones: a price-action base is retained only when it
        overlaps a confirmed Band/Zone/Smart-Liquidity reference.
        """
        closed = list(candles or [])[:-1]
        atr = cls._atr(closed)
        if len(closed) < 25 or not atr or not reference_levels:
            return []

        def value(row, key):
            aliases = {"o": "open", "h": "high", "l": "low", "c": "close"}
            return float(row.get(key, row.get(aliases[key], 0.0)))

        candidates = []
        start_at = max(3, len(closed) - 70)
        # base_end leaves up to three completed candles for departure.
        for base_end in range(start_at, len(closed) - 2):
            for base_count in (1, 2, 3):
                base_start = base_end - base_count + 1
                if base_start < 2:
                    continue
                base = closed[base_start:base_end + 1]
                incoming = closed[max(0, base_start - 4):base_start]
                if len(incoming) < 2:
                    continue
                incoming_move = value(incoming[-1], "c") - value(incoming[0], "o")
                if abs(incoming_move) < 0.25 * atr:
                    continue
                zone_low = min(value(c, "l") for c in base)
                zone_high = max(value(c, "h") for c in base)
                width = zone_high - zone_low
                if width <= 0 or width > 1.60 * atr:
                    continue
                departure = closed[base_end + 1:min(base_end + 4, len(closed))]
                if not departure:
                    continue
                if direction == "long":
                    impulse = max(value(c, "c") - zone_high for c in departure)
                    bodies = [(value(c, "c") - value(c, "o")) / max(value(c, "h") - value(c, "l"), 1e-9)
                              for c in departure]
                    gap = value(departure[0], "l") > zone_high
                    structure = "rbr" if incoming_move > 0 else "dbr"
                else:
                    impulse = max(zone_low - value(c, "c") for c in departure)
                    bodies = [(value(c, "o") - value(c, "c")) / max(value(c, "h") - value(c, "l"), 1e-9)
                              for c in departure]
                    gap = value(departure[0], "h") < zone_low
                    structure = "dbd" if incoming_move < 0 else "rbd"
                best_body = max(bodies)
                if impulse < 0.35 * atr or best_body < 0.50:
                    continue

                buffer_distance = 0.20 * atr
                confluences = [(level, label) for level, label in reference_levels
                               if zone_low - buffer_distance <= level <= zone_high + buffer_distance]
                if not confluences:
                    continue

                after_departure = closed[min(base_end + 4, len(closed)):]
                overlaps = [value(c, "l") <= zone_high and value(c, "h") >= zone_low
                            for c in after_departure]
                touches = 0
                previous = False
                touch_positions = []
                for pos, overlap in enumerate(overlaps):
                    if overlap and not previous:
                        touches += 1
                        touch_positions.append(pos)
                    previous = overlap

                if direction == "long":
                    excursion = max([value(c, "h") - zone_high for c in after_departure] + [impulse])
                else:
                    excursion = max([zone_low - value(c, "l") for c in after_departure] + [impulse])
                distance_r = excursion / max(width, 0.20 * atr)

                compression = False
                if touch_positions:
                    absolute_touch = min(base_end + 4 + touch_positions[-1], len(closed) - 1)
                    approach = closed[max(base_end + 4, absolute_touch - 5):absolute_touch]
                    if len(approach) >= 3:
                        ranges = [value(c, "h") - value(c, "l") for c in approach]
                        contracting = sum(b <= a for a, b in zip(ranges, ranges[1:])) >= len(ranges) - 2
                        if direction == "long":
                            stepping = sum(value(b, "h") <= value(a, "h") for a, b in zip(approach, approach[1:])) >= len(approach) - 2
                        else:
                            stepping = sum(value(b, "l") >= value(a, "l") for a, b in zip(approach, approach[1:])) >= len(approach) - 2
                        compression = contracting and stepping

                source_groups = set()
                for _level, label in confluences:
                    lower = label.lower()
                    source_groups.add("liquidity" if "liquidity" in lower else "zone" if "zone" in lower else "band")
                nested = len(source_groups) >= 2
                departure_score = min(15.0, 7.5 * impulse / atr)
                base_score = 10.0 if base_count <= 2 and width <= atr else 6.0
                freshness_score = 15.0 if touches <= 1 else 5.0 if touches == 2 else 0.0
                score = (
                    20.0 + departure_score + base_score + freshness_score
                    + (15.0 if nested else 0.0)
                    + min(10.0, 10.0 * distance_r / 3.0)
                    + (10.0 if compression else 0.0)
                    + (5.0 if gap else 0.0)
                )
                candidates.append({
                    "low": zone_low, "high": zone_high,
                    "type": "demand" if direction == "long" else "supply",
                    "structure": structure,
                    "structure_priority": 2 if structure in ("dbr", "rbd") else 1,
                    "score": min(score, 100.0), "touch_count": touches,
                    "fresh": touches <= 1, "base_candles": base_count,
                    "departure_score": departure_score,
                    "compression": compression, "gap": gap, "nested": nested,
                    "distance_r": distance_r,
                    "confluences": sorted({label for _, label in confluences}),
                    "formed_at": int(closed[base_end].get("t", 0) or 0),
                })
        # Prefer quality, then the newest formation. Deduplicate overlapping
        # bases produced by the 1/2/3-candle search.
        candidates.sort(key=lambda z: (z["score"] + 5.0 * (z["structure_priority"] - 1), z["formed_at"]), reverse=True)
        unique = []
        for zone in candidates:
            if any(max(zone["low"], old["low"]) <= min(zone["high"], old["high"]) for old in unique):
                continue
            unique.append(zone)
        return unique

    @staticmethod
    def _select_rtm_zone(direction, entry, zones, atr_4h):
        max_distance = max(1.25 * atr_4h, 0.006 * entry)
        eligible = []
        for zone in zones:
            proximal = zone["high"] if direction == "long" else zone["low"]
            if abs(entry - proximal) <= max_distance and zone["touch_count"] >= 1:
                eligible.append(zone)
        # DBR for Long and RBD for Short receive a small preference because
        # they mark the end of the 1H pullback; quality can still overrule it.
        return max(eligible, key=lambda z: (z["score"] + 5.0 * (z["structure_priority"] - 1), z["formed_at"]), default=None)

    @staticmethod
    def _apply_rtm_opposing_zone(direction, entry, stop, target, zones):
        """Use the nearest credible opposite RTM zone as TP/path blocker."""
        credible = [z for z in zones if z["score"] >= 50.0 and z["touch_count"] <= 2]
        if direction == "long":
            ahead = [z for z in credible if z["low"] > entry]
            obstacle = min(ahead, key=lambda z: z["low"], default=None)
            obstacle_level = obstacle["low"] if obstacle else 0.0
        else:
            ahead = [z for z in credible if z["high"] < entry]
            obstacle = max(ahead, key=lambda z: z["high"], default=None)
            obstacle_level = obstacle["high"] if obstacle else 0.0
        if not obstacle:
            risk = abs(entry - stop) if stop else 0.0
            rr = abs(target - entry) / risk if risk and target else 0.0
            return target, rr, None, True
        risk = abs(entry - stop) if stop else 0.0
        obstacle_rr = abs(obstacle_level - entry) / risk if risk else 0.0
        if obstacle_rr < 2.0:
            return 0.0, 0.0, obstacle, False
        if direction == "long":
            target = min(target, obstacle_level) if target else obstacle_level
        else:
            target = max(target, obstacle_level) if target else obstacle_level
        rr = abs(target - entry) / risk if risk and target else 0.0
        return target, rr, obstacle, rr >= 2.0

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
                    Trade = self.env["dankbit.trade"]
                    entry = float(Trade.get_index_price(asset) or 0.0)
                    candles_daily = Trade.get_candles(asset, interval="1d", limit=90) or []
                    candles_4h = Trade.get_candles(asset, interval="4h", limit=100) or []
                    candles_1h = Trade.get_candles(asset, interval="1h", limit=80) or []
                    trend_daily, trend_score_daily = self._market_trend(candles_daily)
                    trend_4h, trend_score_4h = self._market_trend(candles_4h)
                    trend_1h, _trend_score_1h = self._market_trend(candles_1h)
                    score4, coverage4 = self._anchor_score(rows4, entry)
                    score1, coverage1 = self._anchor_score(rows1, entry)
                    scored, _ = self._anchor_score(rowsd, entry)
                    gamma_score, greek_score, greek_components = self._option_direction_scores(asset, now_naive)
                    quality = self._quality(rows4, rows1, snapshots, is_weekend)
                    standard_direction = ("long" if trend_daily == trend_4h == "up"
                                          else "short" if trend_daily == trend_4h == "down"
                                          else "neutral")
                    breakout = self._breakout_retest_setup(candles_4h, candles_1h, entry)
                    breakout_direction = breakout["direction"] if breakout else "neutral"
                    breakout_sign = 1.0 if breakout_direction == "long" else -1.0 if breakout_direction == "short" else 0.0
                    daily_not_opposed = bool(
                        breakout and (
                            trend_daily == "neutral"
                            or (breakout_direction == "long" and trend_daily == "up")
                            or (breakout_direction == "short" and trend_daily == "down")
                        )
                    )
                    breakout_options_aligned = bool(
                        breakout and breakout_sign * gamma_score >= 10.0
                        and breakout_sign * greek_score >= 10.0
                        and breakout_sign * score4 >= -35.0
                    )
                    breakout_candidate = bool(breakout and daily_not_opposed and breakout_options_aligned)
                    direction = standard_direction if standard_direction != "neutral" else (
                        breakout_direction if breakout_candidate else "neutral"
                    )
                    direction_sign = 1.0 if direction == "long" else -1.0 if direction == "short" else 0.0
                    # Trend owns direction. Forecast and moving Greek centres
                    # confirm/predict it; none may reverse the hard MTF gate.
                    raw_final = direction_sign * (
                        0.25 * trend_score_daily + 0.30 * trend_score_4h
                        + 0.20 * score4 + 0.15 * gamma_score + 0.10 * greek_score
                    )
                    final = raw_final * (quality / 100.0)
                    stop = target = rr = swing_level = 0.0
                    # Always expose ATR health in the panel, even while the
                    # hard trend gate is neutral and no stop is calculated.
                    atr_4h = self._atr(list(candles_4h or [])[:-1])
                    trigger_level = atr_1h = 0.0
                    trigger_basis = ""
                    liquidity_swept = entry_confirmed = False
                    rtm_zone = None
                    rtm_opposing_zone = None
                    rtm_path_clear = True
                    rtm_score = 0.0
                    entry_model = False
                    setup_path = False
                    plan_status = False
                    entry_zone_low = entry_zone_high = invalidation_level = target_2 = breakout_level = 0.0
                    breakout_at = False
                    plan_provisional = False
                    if direction != "neutral":
                        stop, swing_level, atr_4h = self._structural_stop(direction, entry, candles_4h)
                        stop, target, rr = self._levels(direction, entry, stop, snapshots, rows4)

                    min_quality = 72.0 if is_weekend else 65.0
                    reasons = []
                    if coverage4 < 3 or coverage1 < 3:
                        reasons.append("Incomplete EA1/EA2/EA3 coverage")
                    if quality < min_quality:
                        reasons.append("Quality %.1f below %.1f threshold" % (quality, min_quality))
                    if standard_direction == "neutral":
                        reasons.append("Daily and 4H real-price trends are not aligned")
                    elif final < 35.0:
                        reasons.append("Combined trend/Forecast/Greeks score is below 35")
                    if standard_direction != "neutral" and direction_sign * score4 < 15.0:
                        reasons.append("4H Forecast does not confirm the real-price trend")
                    if standard_direction != "neutral" and direction_sign * scored < -35.0:
                        reasons.append("Daily Forecast strongly conflicts with the real-price trend")
                    if standard_direction != "neutral" and direction_sign * gamma_score < -30.0:
                        reasons.append("Gamma concentration is moving strongly against the setup")
                    if standard_direction != "neutral" and direction_sign * greek_score < -30.0:
                        reasons.append("Combined Greek concentration is moving strongly against the setup")
                    # A counter-trend 1H market is a valid pullback. Only a
                    # strongly opposing *forward* 1H projection delays entry.
                    if standard_direction != "neutral" and direction_sign * score1 < -35.0:
                        reasons.append("Waiting for the 1H Forecast to turn out of the pullback")
                    if standard_direction != "neutral" and not swing_level:
                        reasons.append("No confirmed 4H swing available for structural stop")
                    if standard_direction != "neutral" and rr < 2.0:
                        reasons.append("No structure-backed target with R:R >= 2.0 after 4H swing stop")
                    thales_eligible = standard_direction != "neutral" and not reasons and entry > 0
                    if thales_eligible:
                        reference_levels = self._entry_reference_levels(asset, direction, snapshots)
                        if not reference_levels:
                            reasons.append("Setup detected, but no confirmed Zone/Band or Smart Liquidity entry level is available")
                        else:
                            opposite_direction = "short" if direction == "long" else "long"
                            opposing_references = self._entry_reference_levels(asset, opposite_direction, snapshots)
                            opposing_zones = self._rtm_zone_candidates(
                                opposite_direction, candles_1h, opposing_references,
                            ) if opposing_references else []
                            target, rr, rtm_opposing_zone, rtm_path_clear = self._apply_rtm_opposing_zone(
                                direction, entry, stop, target, opposing_zones,
                            )
                            if not rtm_path_clear:
                                reasons.append("Opposing RTM zone blocks the path before minimum 2R")
                            rtm_zones = self._rtm_zone_candidates(direction, candles_1h, reference_levels)
                            rtm_zone = self._select_rtm_zone(direction, entry, rtm_zones, atr_4h)
                            if not rtm_zone:
                                reasons.append("Thales setup armed: waiting for a confluent 1H RTM zone return")
                            else:
                                rtm_score = float(rtm_zone["score"])
                                proximal = rtm_zone["high"] if direction == "long" else rtm_zone["low"]
                                trigger_label = "RTM %s FTB" % rtm_zone["type"].title()
                                entry_confirmed, trigger_level, trigger_basis, liquidity_swept, atr_1h = self._entry_trigger(
                                    direction, entry, candles_1h, [(proximal, trigger_label)], atr_4h,
                                )
                                if rtm_score < 65.0:
                                    reasons.append("RTM Zone Score %.1f is below 65" % rtm_score)
                                if rtm_zone["touch_count"] > 1:
                                    reasons.append("RTM zone is no longer FTB; later-touch setup is Shadow only")
                                if not liquidity_swept:
                                    reasons.append("RTM zone armed: waiting for closed 1H sweep and reclaim")
                                elif not entry_confirmed:
                                    reasons.append("FTB reclaimed: waiting for closed 1H body Engulf/BOS")

                    rtm_official_eligible = bool(
                        thales_eligible and rtm_zone and rtm_score >= 65.0
                        and rtm_path_clear and rtm_zone["touch_count"] == 1 and entry_confirmed
                    )
                    reclaim_shadow = bool(
                        thales_eligible and rtm_zone and rtm_score >= 65.0
                        and rtm_path_clear and rtm_zone["touch_count"] == 1 and liquidity_swept and not entry_confirmed
                    )
                    later_touch_shadow = bool(
                        thales_eligible and rtm_zone and rtm_score >= 65.0
                        and rtm_path_clear and rtm_zone["touch_count"] >= 2 and entry_confirmed
                    )

                    breakout_official_eligible = False
                    breakout_shadow = False
                    breakout_watch = False
                    if not rtm_official_eligible and not reclaim_shadow and not later_touch_shadow and breakout_candidate:
                        direction = breakout["direction"]
                        direction_sign = 1.0 if direction == "long" else -1.0
                        directional_option_strength = max(
                            min((direction_sign * gamma_score + direction_sign * greek_score) / 100.0, 1.0), 0.0,
                        )
                        final = quality / 100.0 * min(
                            100.0, 55.0 * min(breakout["body_atr"] / 1.50, 1.0)
                            + 25.0 * directional_option_strength
                            + (20.0 if breakout["confirmed"] else 10.0 if breakout["retest_seen"] else 0.0),
                        )
                        entry = float(breakout["entry"])
                        stop = float(breakout["stop"])
                        target = float(breakout["target"])
                        target_2 = float(breakout["target_2"])
                        rr = float(breakout["risk_reward"])
                        atr_4h = float(breakout["atr_4h"])
                        swing_level = float(breakout["invalidation"])
                        entry_zone_low = float(breakout["entry_zone_low"])
                        entry_zone_high = float(breakout["entry_zone_high"])
                        invalidation_level = float(breakout["invalidation"])
                        breakout_level = float(breakout["level"])
                        breakout_at = datetime.fromtimestamp(breakout["at_ms"] / 1000.0, tz=timezone.utc).replace(tzinfo=None) if breakout["at_ms"] else False
                        setup_path = "breakout_retest"
                        setup_stage = "confirmed" if breakout["stage"] == "confirmed" else "armed" if breakout["stage"] == "armed" else "setup"
                        plan_status = "triggered" if breakout["confirmed"] else "armed" if breakout["retest_seen"] else "watch"
                        plan_provisional = not breakout["confirmed"]
                        trigger_level = breakout_level
                        trigger_basis = "Broken 4H structure retest"
                        liquidity_swept = bool(breakout["retest_seen"])
                        entry_confirmed = bool(breakout["confirmed"])
                        opposite_direction = "short" if direction == "long" else "long"
                        opposing_references = self._entry_reference_levels(asset, opposite_direction, snapshots)
                        opposing_zones = self._rtm_zone_candidates(
                            opposite_direction, candles_1h, opposing_references,
                        ) if opposing_references else []
                        target, rr, rtm_opposing_zone, rtm_path_clear = self._apply_rtm_opposing_zone(
                            direction, entry, stop, target, opposing_zones,
                        )
                        if rtm_path_clear and target_2:
                            # TP2 is optional and cannot project through the
                            # nearest credible opposing RTM zone.
                            if rtm_opposing_zone:
                                opposing_level = (rtm_opposing_zone["low"] if direction == "long"
                                                   else rtm_opposing_zone["high"])
                                target_2 = min(target_2, opposing_level) if direction == "long" else max(target_2, opposing_level)
                        elif not rtm_path_clear:
                            target = target_2 = rr = 0.0
                            plan_status = "cancelled"
                        entry_model = ("breakout_retest_confirmed" if entry_confirmed
                                       else "breakout_retest_shadow" if quality >= 50.0
                                       else "breakout_watch")
                        reasons = [
                            "Closed 4H %s breakout %.2f ATR; waiting for a non-chasing 1H retest entry"
                            % (direction, breakout["body_atr"])
                        ]
                        if quality < 50.0:
                            reasons.append("Option-data quality below 50: Watch only, numeric plan is provisional")
                        elif quality < min_quality:
                            reasons.append("Option-data quality 50-64: plan is Shadow only")
                        if not breakout["retest_seen"]:
                            reasons.append("Waiting for price to retest the broken 4H level")
                        elif not breakout["confirmed"]:
                            reasons.append("1H retest rejected: waiting for a later closed 1H BOS/Engulf confirmation")
                        else:
                            reasons.append("Closed 1H retest and micro-structure confirmation completed")
                        if not rtm_path_clear:
                            reasons.append("Opposing RTM zone blocks the Breakout-Retest path before minimum 2R")
                        breakout_official_eligible = bool(
                            entry_confirmed and quality >= min_quality and rr >= 2.0 and rtm_path_clear
                        )
                        breakout_shadow = bool(
                            quality >= 50.0 and rtm_path_clear and not breakout_official_eligible
                        )
                        breakout_watch = bool(quality < 50.0 and rtm_path_clear)

                    official_eligible = bool(rtm_official_eligible or breakout_official_eligible)

                    # Serialize the weekly decision even if an administrator
                    # manually triggers the cron while its scheduled run is
                    # active. No duplicate official can pass the check.
                    self.env.cr.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        ["dankbit-signal-week-%s-%s" % (asset, (now.date() - timedelta(days=now.weekday())).isoformat())],
                    )
                    week_start = now.date() - timedelta(days=now.weekday())
                    weekly_officials = self.sudo().search_count([
                        ("asset", "=", asset), ("utc_day", ">=", week_start),
                        ("kind", "=", "official"),
                    ])
                    active_official = self.sudo().search_count([
                        ("asset", "=", asset), ("kind", "=", "official"),
                        ("state", "=", "active"),
                    ])
                    cooldown = self.sudo().search_count([
                        ("asset", "=", asset), ("kind", "=", "official"),
                        ("closed_at", ">", now_naive - timedelta(hours=8)),
                    ])
                    capacity_available = weekly_officials < 3 and not active_official and not cooldown
                    shadow_candidate = official_eligible or reclaim_shadow or later_touch_shadow or breakout_shadow
                    kind = ("official" if official_eligible and capacity_available
                            else "shadow" if shadow_candidate else "no_trade")
                    if setup_path != "breakout_retest":
                        setup_path = "rtm_ftb" if thales_eligible or rtm_zone else False
                        entry_model = ("ftb_confirmed" if rtm_official_eligible
                                       else "ftb_reclaim_shadow" if reclaim_shadow
                                       else "later_touch_shadow" if later_touch_shadow else False)
                        setup_stage = "confirmed" if entry_confirmed else "armed" if thales_eligible else "setup" if direction != "neutral" else "none"
                        plan_status = "triggered" if entry_confirmed else "armed" if thales_eligible else "watch" if direction != "neutral" else False
                    state = ("active" if kind == "official" else "watch" if kind == "shadow" or breakout_watch
                             else "armed" if thales_eligible else "rejected")
                    if official_eligible and weekly_officials >= 3:
                        reasons.append("Weekly limit of three official signals reached; stored as shadow")
                    elif official_eligible and active_official:
                        reasons.append("Another official signal is active; stored as shadow")
                    elif official_eligible and cooldown:
                        reasons.append("Eight-hour post-trade cooldown is active; stored as shadow")

                    payload = {
                        "4h": {str(k): {"id": v.id, "open": v.forecast_open, "close": v.forecast_close,
                                         "confidence": v.confidence} for k, v in rows4.items()},
                        "1h": {str(k): {"id": v.id, "open": v.forecast_open, "close": v.forecast_close,
                                         "confidence": v.confidence} for k, v in rows1.items()},
                        "1d": {str(k): {"id": v.id, "open": v.forecast_open, "close": v.forecast_close,
                                         "confidence": v.confidence} for k, v in rowsd.items()},
                        "snapshot_ids": [r.id for r in snapshots.values()],
                        "market_trend": {
                            "daily": trend_daily, "daily_score": trend_score_daily,
                            "4h": trend_4h, "4h_score": trend_score_4h,
                            "1h": trend_1h,
                        },
                        "option_direction": {
                            "gamma": gamma_score, "combined_greeks": greek_score,
                            "components": greek_components,
                        },
                        "candidate_plan": {
                            "entry": entry, "stop": stop, "target": target, "risk_reward": rr,
                            "target_2": target_2, "entry_zone_low": entry_zone_low,
                            "entry_zone_high": entry_zone_high,
                            "invalidation_level": invalidation_level,
                            "swing_level": swing_level, "atr_4h": atr_4h,
                            "setup_path": setup_path, "plan_status": plan_status,
                            "provisional": plan_provisional,
                        },
                        "entry_trigger": {
                            "stage": setup_stage, "level": trigger_level,
                            "basis": trigger_basis, "liquidity_swept": liquidity_swept,
                            "entry_confirmed": entry_confirmed, "atr_1h": atr_1h,
                        },
                        "rtm": dict(rtm_zone, entry_model=entry_model) if rtm_zone else None,
                        "rtm_opposing_zone": rtm_opposing_zone,
                        "breakout": breakout,
                    }
                    regime = ", ".join(sorted({r.activity_regime for r in rows4.values() if r.activity_regime}))
                    visible_plan = kind in ("official", "shadow") or breakout_watch
                    self.sudo().create({
                        "asset": asset, "evaluated_at": now_naive, "utc_day": now.date(),
                        "kind": kind, "state": state, "direction": direction,
                        "forecast_score_4h": score4, "confirmation_score_1h": score1,
                        "daily_filter_score": scored, "quality_score": quality,
                        "final_score": final, "anchor_coverage_4h": coverage4,
                        "anchor_coverage_1h": coverage1, "is_weekend": is_weekend,
                        "activity_regime": regime,
                        "trend_daily": trend_daily,
                        "trend_4h": trend_4h,
                        "trend_1h": trend_1h,
                        "trend_score_daily": trend_score_daily,
                        "trend_score_4h": trend_score_4h,
                        "gamma_direction_score": gamma_score,
                        "greek_direction_score": greek_score,
                        "setup_stage": setup_stage,
                        "setup_path": setup_path,
                        "plan_status": plan_status,
                        "entry_zone_low": entry_zone_low,
                        "entry_zone_high": entry_zone_high,
                        "invalidation_level": invalidation_level,
                        "target_2": target_2,
                        "breakout_level": breakout_level,
                        "breakout_at": breakout_at,
                        "plan_provisional": plan_provisional,
                        "trigger_level": trigger_level,
                        "trigger_basis": trigger_basis,
                        "liquidity_swept": liquidity_swept,
                        "rtm_score": rtm_score,
                        "rtm_zone_low": rtm_zone["low"] if rtm_zone else 0.0,
                        "rtm_zone_high": rtm_zone["high"] if rtm_zone else 0.0,
                        "rtm_zone_type": rtm_zone["type"] if rtm_zone else False,
                        "rtm_structure": rtm_zone["structure"] if rtm_zone else False,
                        "rtm_opposing_structure": rtm_opposing_zone["structure"] if rtm_opposing_zone else False,
                        "rtm_opposing_level": ((rtm_opposing_zone["low"] if direction == "long" else rtm_opposing_zone["high"])
                                                 if rtm_opposing_zone else 0.0),
                        "rtm_path_clear": rtm_path_clear,
                        "rtm_touch_count": rtm_zone["touch_count"] if rtm_zone else 0,
                        "rtm_fresh": rtm_zone["fresh"] if rtm_zone else False,
                        "rtm_base_candles": rtm_zone["base_candles"] if rtm_zone else 0,
                        "rtm_departure_score": rtm_zone["departure_score"] if rtm_zone else 0.0,
                        "rtm_compression": rtm_zone["compression"] if rtm_zone else False,
                        "rtm_gap": rtm_zone["gap"] if rtm_zone else False,
                        "rtm_nested": rtm_zone["nested"] if rtm_zone else False,
                        "entry_model": entry_model,
                        "entry": entry if visible_plan else 0.0,
                        "stop_loss": stop if visible_plan else 0.0,
                        "target": target if visible_plan else 0.0,
                        "risk_reward": rr if visible_plan else 0.0,
                        "swing_level": swing_level,
                        "atr_4h": atr_4h,
                        "stop_basis": ("1H Retest Swing High + 0.15 ATR buffer" if setup_path == "breakout_retest" and direction == "short"
                                       else "1H Retest Swing Low - 0.15 ATR buffer" if setup_path == "breakout_retest" and direction == "long"
                                       else "4H Swing High + ATR buffer" if direction == "short"
                                       else "4H Swing Low - ATR buffer" if direction == "long" else ""),
                        "expires_at": (breakout_at + timedelta(hours=28)
                                       if setup_path == "breakout_retest" and breakout_at
                                       else now_naive + timedelta(hours=24)),
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
            # WATCH/ARMED breakout plans are displayed early but are not
            # hypothetical fills. Forward testing starts only after the 1H
            # retest has actually triggered the plan.
            if signal.setup_path == "breakout_retest" and signal.plan_status != "triggered":
                if signal.expires_at and signal.expires_at <= now_naive:
                    signal.write({"state": "cancelled", "closed_at": now_naive})
                continue
            candles = Trade.get_candles(signal.asset, interval="1h", limit=72) or []
            relevant = []
            start_epoch = signal.evaluated_at.replace(tzinfo=timezone.utc).timestamp() * 1000
            # Ignore the still-forming final 1H candle in forward-test results,
            # just as entry confirmation ignores it.
            for candle in list(candles)[:-1]:
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
