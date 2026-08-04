from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    from_price = fields.Float(
        string="From price",
        config_parameter="dankbit.from_price"
    )

    to_price = fields.Float(
        string="To price",
        config_parameter="dankbit.to_price"
    )

    eth_from_price = fields.Float(
        string="ETH From price",
        config_parameter="dankbit.eth_from_price"
    )

    eth_to_price = fields.Float(
        string="ETH To price",
        config_parameter="dankbit.eth_to_price"
    )

    steps = fields.Integer(
        string="Steps",
        config_parameter="dankbit.steps"
    )

    eth_steps = fields.Integer(
        string="ETH Steps",
        config_parameter="dankbit.eth_steps"
    )

    refresh_interval = fields.Integer(
        string="Refresh interval (s)",
        config_parameter="dankbit.refresh_interval"
    )

    zones_box_refresh_interval = fields.Integer(
        string="Zones box / Bands refresh interval (s)",
        config_parameter="dankbit.zones_box_refresh_interval",
        help="How often (in seconds) the TradingView chart re-fetches the yellow/teal "
             "zones boxes AND the Bands lines (High/Resistance, Low/Support, Gamma Band, "
             "Smart Liquidity) — both share this one (deliberately slow) interval rather "
             "than the general refresh_interval, since dankbit.bands rows only ever "
             "change once per hourly compute_snapshot() cron tick regardless of how "
             "often the browser polls, so polling faster than this just wastes requests. "
             "Defaults to 3600 (1 hour).",
    )

    zones_box_window_hours = fields.Integer(
        string="Zones Box Trailing Window (h)",
        config_parameter="dankbit.zones_box_window_hours",
        help="How many trailing hours of trades the yellow/teal zones boxes use when the chart's trade-window toggle is set to \"X hours ago\" instead of \"00:00 UTC\". Defaults to 8.",
    )

    deribit_timeout = fields.Float(
        string="Deribit API timeout (s)",
        config_parameter="dankbit.deribit_timeout",
        help="Timeout in seconds for calls to Deribit public APIs."
    )

    deribit_cache_ttl = fields.Float(
        string="Deribit cache TTL (s)",
        config_parameter="dankbit.deribit_cache_ttl",
        help="Time-to-live in seconds for cached Deribit responses (index/instruments)."
    )

    weekly_expiry = fields.Char(
        string="Weekly Expiry",
        config_parameter="dankbit.weekly_expiry",
    )

    monthly_expiry = fields.Char(
        string="Monthly Expiry",
        config_parameter="dankbit.monthly_expiry",
    )

    eth_weekly_expiry = fields.Char(
        string="ETH Weekly Expiry",
        config_parameter="dankbit.eth_weekly_expiry",
    )

    eth_monthly_expiry = fields.Char(
        string="ETH Monthly Expiry",
        config_parameter="dankbit.eth_monthly_expiry",
    )

    # Not using config_parameter= here: Odoo's generic config_parameter
    # handling for Boolean fields treats a False value the same as "delete
    # the parameter" (ir.config_parameter.set_param() special-cases Python
    # False as "unset"), so unchecking one of these and saving would silently
    # revert to the field's default=True on next read instead of persisting
    # False. get_values()/set_values() below store an explicit "True"/"False"
    # string instead, which set_param() writes normally (only a real Python
    # False/None triggers the delete-on-unset behavior, not the string).
    show_daily_lines = fields.Boolean(
        string="Show Daily Lines",
        default=True,
        help="Show the Daily (24H) / Daily+1 (24H) delta=0 lines on the TradingView chart.",
    )

    show_weekly_lines = fields.Boolean(
        string="Show Weekly Lines",
        default=True,
        help="Show the Weekly delta=0 line on the TradingView chart.",
    )

    show_monthly_lines = fields.Boolean(
        string="Show Monthly Lines",
        default=True,
        help="Show the Monthly delta=0 line on the TradingView chart.",
    )

    forecast_trade_weighted_greeks = fields.Boolean(
        string="Forecast: Trade-Weighted Per-Leg Greeks",
        default=False,
        help="Use the trade-weighted single-strike per-leg Greek extraction "
             "(forecast.trade_weighted_per_leg_greeks) requested directly by "
             "Thales's original indicator author, instead of the default "
             "Black-Scholes portfolio-curve extraction (per_leg_greeks) — "
             "affects the Thales Forecast candle engine only, never the "
             "/<instrument>/zones page or the persisted dankbit.bands "
             "numbers. Off by default: this changes the numeric scale of "
             "every BCG/BPG/.../SPV Abs field the forecast cascade reads, "
             "so it should be validated against real forecast accuracy "
             "(see the Forecast Log pivot view) before relying on it.",
    )

    # ============================================================
    # Thales Forecast — the top-level tunables
    # simulate_forecast() itself uses directly (see forecast.py's
    # module docstring and simulate_forecast's `cfg` parameter). Fields
    # left unset fall back to the engine's own hardcoded default (shown
    # in each field's help text) via icp.get_param(key, default=...) at
    # the call site (main.py's forecast_json) — same convention
    # from_price/steps/etc. above use, no field-level `default=`.
    # Constants private to nested helper functions (vega_regime,
    # market_maker_gamma_contest, cluster_*, smart_synthetic_liquidity's
    # internals, delta_shock_module, gamma_shock_module, etc.) are not
    # exposed here — see the "Top-level only" scoping decision in
    # CLAUDE.md's Forecast candles section. The center-weight fields
    # (forecast_gamma_center_weight/forecast_curve_center_weight/
    # forecast_theta_center_weight) and the 4 *_abs_normalizer fields
    # below are the exceptions: derive_levels()/vega_regime()/
    # market_maker_gamma_contest()/smart_synthetic_liquidity()/
    # greek_flow()/session_activity_score() are nested helpers, not
    # simulate_forecast's own body, but each takes its own `cfg`
    # parameter specifically so these particular constants can still be
    # tuned (the center weights per the Pine script author's own request
    # to weight gamma more heavily; the 4 normalizers per Thales dev
    # feedback that the forecast reads as BTC-optimized — see their own
    # help text and forecast.py's module-level comment on
    # GAMMA_ABS_NORMALIZER/etc.).
    #
    # BTC vs. ETH: every field in this section (this BTC-scoped block AND
    # its "ETH counterparts" mirror block further below) is genuinely
    # per-asset — dankbit.forecast.snapshot.get_forecast_cfg(asset) reads
    # the plain `forecast_*`/`session_*` keys for BTC and the
    # `eth_forecast_*`/`eth_session_*` keys for ETH, same
    # unprefixed-is-BTC/eth_-prefixed-is-ETH convention as
    # eth_from_price/eth_weekly_expiry/etc. Every eth_forecast_* field
    # defaults to the exact same value as its BTC counterpart, so
    # behavior for both assets is unchanged until an admin retunes ETH's
    # values independently.
    # ============================================================
    forecast_gamma_center_weight = fields.Float(
        string="Gamma Center Weight",
        config_parameter="dankbit.forecast_gamma_center_weight",
        default=0.70,
        digits=(16, 4),
        help="Weight of the gamma average in the blended gamma/curve/theta center price the forecast pulls toward. Default 0.70 (raised from Thales's own 0.55 default per the script author's request to weight gamma more heavily).",
    )

    forecast_curve_center_weight = fields.Float(
        string="Curve Center Weight",
        config_parameter="dankbit.forecast_curve_center_weight",
        default=0.20,
        digits=(16, 4),
        help="Weight of the BML/SMP curve average in the blended center price. Default 0.20 (lowered from Thales's own 0.30 default).",
    )

    forecast_theta_center_weight = fields.Float(
        string="Theta Center Weight",
        config_parameter="dankbit.forecast_theta_center_weight",
        default=0.10,
        digits=(16, 4),
        help="Weight of the theta average in the blended center price. Default 0.10 (lowered from Thales's own 0.15 default).",
    )

    forecast_pull_factor = fields.Float(
        string="Pull Factor",
        config_parameter="dankbit.forecast_pull_factor",
        default=0.55,
        digits=(16, 4),
        help="Weight of the pull toward the blended gamma/curve/theta center in the base impulse. Default 0.55.",
    )

    forecast_slope_factor = fields.Float(
        string="Slope Factor",
        config_parameter="dankbit.forecast_slope_factor",
        default=0.35,
        digits=(16, 4),
        help="Weight of the center's own recent slope (momentum) in the base impulse. Default 0.35.",
    )

    forecast_body_factor = fields.Float(
        string="Body Factor",
        config_parameter="dankbit.forecast_body_factor",
        default=0.42,
        digits=(16, 4),
        help="Weight of the most recent real candle's body in the base impulse. Default 0.42.",
    )

    forecast_curve_extreme_body_weight = fields.Float(
        string="Curve Extreme Body Weight",
        config_parameter="dankbit.forecast_curve_extreme_body_weight",
        default=0.26,
        digits=(16, 4),
        help="Weight of the pull toward BML/SMP (curve extremes) in the base impulse. Default 0.26.",
    )

    forecast_wick_factor = fields.Float(
        string="Wick Factor",
        config_parameter="dankbit.forecast_wick_factor",
        default=0.35,
        digits=(16, 4),
        help="Share of the remaining room to the wick target that becomes wick length. Default 0.35.",
    )

    forecast_atr_factor = fields.Float(
        string="Atr Factor",
        config_parameter="dankbit.forecast_atr_factor",
        default=0.3,
        digits=(16, 4),
        help="How much of the session/weekend-adjusted ATR is added to each wick. Default 0.3.",
    )

    forecast_curve_wick_weight = fields.Float(
        string="Curve Wick Weight",
        config_parameter="dankbit.forecast_curve_wick_weight",
        default=0.42,
        digits=(16, 4),
        help="Weight of BML/SMP when computing the upper/lower wick target price. Default 0.42.",
    )

    forecast_greek_flow_impulse_weight = fields.Float(
        string="Greek Flow Impulse Weight",
        config_parameter="dankbit.forecast_greek_flow_impulse_weight",
        default=1.75,
        digits=(16, 4),
        help="Greek Flow Priority Hybrid: multiplier on the Greek Flow engine's own impulse "
             "before it's added alongside every other engine's impulse (Gamma-Band, Delta/"
             "Gamma Shock, Vega, Market-Maker, Liquidity, ...) in the base body/close estimate. "
             "Default 1.75 makes Greek Flow the dominant additive voice for body direction, "
             "per the design decision that it should drive Close while the structural engines "
             "(Bands, Smart Liquidity, Session Activity, Vega) keep constraining how far that "
             "move is allowed to travel rather than being removed.",
    )

    forecast_gb_opposite_wick_compression = fields.Float(
        string="Gb Opposite Wick Compression",
        config_parameter="dankbit.forecast_gb_opposite_wick_compression",
        default=0.18,
        digits=(16, 4),
        help="Max fraction the wick opposite a Gamma-Band consensus direction is compressed by. Default 0.18.",
    )

    forecast_gb_confirmed_target_boost = fields.Float(
        string="Gb Confirmed Target Boost",
        config_parameter="dankbit.forecast_gb_confirmed_target_boost",
        default=0.55,
        digits=(16, 4),
        help="Extra weight given to top/low as the wick target when Gamma-Band Consensus confirms that side. Default 0.55.",
    )

    forecast_gb_confidence_boost = fields.Float(
        string="Gb Confidence Boost",
        config_parameter="dankbit.forecast_gb_confidence_boost",
        default=0.2,
        digits=(16, 4),
        help="Body-confidence boost when Gamma-Band Consensus is directionally active. Default 0.2.",
    )

    forecast_gb_conflict_body_damping = fields.Float(
        string="Gb Conflict Body Damping",
        config_parameter="dankbit.forecast_gb_conflict_body_damping",
        default=0.3,
        digits=(16, 4),
        help="Body-confidence damping when Gamma-Band Consensus signals are in conflict. Default 0.3.",
    )

    forecast_gb_conflict_wick_expansion = fields.Float(
        string="Gb Conflict Wick Expansion",
        config_parameter="dankbit.forecast_gb_conflict_wick_expansion",
        default=0.25,
        digits=(16, 4),
        help="Wick expansion when Gamma-Band Consensus signals are in conflict. Default 0.25.",
    )

    forecast_gb_opposing_magnet_damping = fields.Float(
        string="Gb Opposing Magnet Damping",
        config_parameter="dankbit.forecast_gb_opposing_magnet_damping",
        default=0.6,
        digits=(16, 4),
        help="Damping applied to the gamma-gap pull when it opposes the Gamma-Band Consensus direction. Default 0.6.",
    )

    forecast_gb_trend_lock_strength = fields.Float(
        string="Gb Trend Lock Strength",
        config_parameter="dankbit.forecast_gb_trend_lock_strength",
        default=0.55,
        digits=(16, 4),
        help="Minimum Gamma-Band Consensus strength (with all 3 series aligned) to arm the Trend Lock. Default 0.55.",
    )

    forecast_gb_counter_body_damping = fields.Float(
        string="Gb Counter Body Damping",
        config_parameter="dankbit.forecast_gb_counter_body_damping",
        default=0.18,
        digits=(16, 4),
        help="Body-impulse damping applied to the current real candle while the Trend Lock is engaged. Default 0.18.",
    )

    forecast_gb_counter_max_opp_impulse = fields.Float(
        string="Gb Counter Max Opp Impulse",
        config_parameter="dankbit.forecast_gb_counter_max_opp_impulse",
        default=0.03,
        digits=(16, 4),
        help="Maximum opposite-direction forecast impulse allowed while the Trend Lock is engaged. Default 0.03.",
    )

    forecast_gb_counter_escape_atr = fields.Float(
        string="Gb Counter Escape Atr",
        config_parameter="dankbit.forecast_gb_counter_escape_atr",
        default=0.95,
        digits=(16, 4),
        help="Body-to-ATR ratio a real candle needs to escape the Trend Lock. Default 0.95.",
    )

    forecast_gb_term_slope_impulse_strength = fields.Float(
        string="Gb Term Slope Impulse Strength",
        config_parameter="dankbit.forecast_gb_term_slope_impulse_strength",
        default=0.16,
        digits=(16, 4),
        help="Weight of the forward slope between the nearest and next tracked expiry's own Gamma Band "
             "point (the chart's dashed line's forward-most segment) in the base impulse. Default 0.16.",
    )

    forecast_gb_term_slope_max_impulse = fields.Float(
        string="Gb Term Slope Max Impulse",
        config_parameter="dankbit.forecast_gb_term_slope_max_impulse",
        default=0.2,
        digits=(16, 4),
        help="Cap on the per-step impulse from the Gamma Band term-structure slope. Default 0.2.",
    )

    forecast_gamma_confirm_buffer_pct = fields.Float(
        string="Gamma Confirm Buffer Pct",
        config_parameter="dankbit.forecast_gamma_confirm_buffer_pct",
        default=0.06,
        digits=(16, 4),
        help="Buffer (as a fraction of band width) a close must clear the gamma reference by to confirm a break. Default 0.06.",
    )

    forecast_cluster_alignment_threshold = fields.Float(
        string="Cluster Alignment Threshold",
        config_parameter="dankbit.forecast_cluster_alignment_threshold",
        default=0.6,
        digits=(16, 4),
        help="Minimum directional alignment across top/low/gamma/BML/SMP to treat a cluster expansion as directional. Default 0.6.",
    )

    forecast_cluster_body_confidence_floor = fields.Float(
        string="Cluster Body Confidence Floor",
        config_parameter="dankbit.forecast_cluster_body_confidence_floor",
        default=0.58,
        digits=(16, 4),
        help="Body-confidence floor when the Option Cluster is expanding and aligned with the candle's own direction. Default 0.58.",
    )

    forecast_cluster_compressed_threshold = fields.Float(
        string="Cluster Compressed Threshold",
        config_parameter="dankbit.forecast_cluster_compressed_threshold",
        default=0.18,
        digits=(16, 4),
        help="Dispersion (in band-widths) below which the Option Cluster is considered compressed. Default 0.18.",
    )

    forecast_cluster_compression_body_damping = fields.Float(
        string="Cluster Compression Body Damping",
        config_parameter="dankbit.forecast_cluster_compression_body_damping",
        default=0.15,
        digits=(16, 4),
        help="Body-confidence damping applied while the Option Cluster is compressed. Default 0.15.",
    )

    forecast_cluster_compression_wick_compression = fields.Float(
        string="Cluster Compression Wick Compression",
        config_parameter="dankbit.forecast_cluster_compression_wick_compression",
        default=0.3,
        digits=(16, 4),
        help="Wick compression applied while the Option Cluster is compressed. Default 0.3.",
    )

    forecast_cluster_expansion_threshold = fields.Float(
        string="Cluster Expansion Threshold",
        config_parameter="dankbit.forecast_cluster_expansion_threshold",
        default=0.025,
        digits=(16, 4),
        help="Dispersion-change (in band-widths) needed for the Option Cluster to register as expanding. Default 0.025.",
    )

    forecast_liquidity_aligned_wick_compression = fields.Float(
        string="Liquidity Aligned Wick Compression",
        config_parameter="dankbit.forecast_liquidity_aligned_wick_compression",
        default=0.25,
        digits=(16, 4),
        help="Wick compression when the current candle's direction aligns with the dominant Smart Synthetic Liquidity side. Default 0.25.",
    )

    forecast_liquidity_body_confidence_floor = fields.Float(
        string="Liquidity Body Confidence Floor",
        config_parameter="dankbit.forecast_liquidity_body_confidence_floor",
        default=0.6,
        digits=(16, 4),
        help="Body-confidence floor when liquidity dominance/sweep-rejection is active. Default 0.6.",
    )

    forecast_liquidity_opposite_wick_compression = fields.Float(
        string="Liquidity Opposite Wick Compression",
        config_parameter="dankbit.forecast_liquidity_opposite_wick_compression",
        default=0.35,
        digits=(16, 4),
        help="Wick compression on the side opposite the dominant liquidity side. Default 0.35.",
    )

    forecast_liquidity_sweep_wick_compression = fields.Float(
        string="Liquidity Sweep Wick Compression",
        config_parameter="dankbit.forecast_liquidity_sweep_wick_compression",
        default=0.7,
        digits=(16, 4),
        help="Wick compression right after a liquidity level is swept and rejected. Default 0.7.",
    )

    forecast_momentum_body_confidence_floor = fields.Float(
        string="Momentum Body Confidence Floor",
        config_parameter="dankbit.forecast_momentum_body_confidence_floor",
        default=0.62,
        digits=(16, 4),
        help="Body-confidence floor while the Momentum/Liquidity-Sweep Override is active. Default 0.62.",
    )

    forecast_momentum_wick_compression = fields.Float(
        string="Momentum Wick Compression",
        config_parameter="dankbit.forecast_momentum_wick_compression",
        default=0.75,
        digits=(16, 4),
        help="Wick compression while the Momentum/Liquidity-Sweep Override is active. Default 0.75.",
    )

    forecast_near_gamma_body_damping = fields.Float(
        string="Near Gamma Body Damping",
        config_parameter="dankbit.forecast_near_gamma_body_damping",
        default=0.4,
        digits=(16, 4),
        help="Body-confidence damping near the Gamma Neutral/Hysteresis Zone. Default 0.4.",
    )

    forecast_near_gamma_wick_expansion = fields.Float(
        string="Near Gamma Wick Expansion",
        config_parameter="dankbit.forecast_near_gamma_wick_expansion",
        default=0.25,
        digits=(16, 4),
        help="Wick expansion near the Gamma Neutral/Hysteresis Zone. Default 0.25.",
    )

    forecast_high_vol_pull_factor = fields.Float(
        string="High Vol Pull Factor",
        config_parameter="dankbit.forecast_high_vol_pull_factor",
        default=1.05,
        digits=(16, 4),
        help="Pull-factor multiplier during the London/Overlap/NY high-volume regime. Default 1.05.",
    )

    forecast_low_vol_pull_factor = fields.Float(
        string="Low Vol Pull Factor",
        config_parameter="dankbit.forecast_low_vol_pull_factor",
        default=0.75,
        digits=(16, 4),
        help="Pull-factor multiplier during the Asia/Post-NY low-volume regime. Default 0.75.",
    )

    forecast_weekday_pull_factor = fields.Float(
        string="Weekday Pull Factor",
        config_parameter="dankbit.forecast_weekday_pull_factor",
        default=1.0,
        digits=(16, 4),
        help="Pull-factor multiplier outside the low/high-volume regimes. Default 1.0.",
    )

    forecast_high_vol_shock_factor = fields.Float(
        string="High Vol Shock Factor",
        config_parameter="dankbit.forecast_high_vol_shock_factor",
        default=1.1,
        digits=(16, 4),
        help="Shock-strength multiplier during the London/Overlap/NY high-volume regime. Default 1.1.",
    )

    forecast_low_vol_shock_factor = fields.Float(
        string="Low Vol Shock Factor",
        config_parameter="dankbit.forecast_low_vol_shock_factor",
        default=0.7,
        digits=(16, 4),
        help="Shock-strength multiplier during the Asia/Post-NY low-volume regime. Default 0.7.",
    )

    forecast_weekday_shock_factor = fields.Float(
        string="Weekday Shock Factor",
        config_parameter="dankbit.forecast_weekday_shock_factor",
        default=1.0,
        digits=(16, 4),
        help="Shock-strength multiplier outside the low/high-volume regimes. Default 1.0.",
    )

    forecast_weekend_atr_factor = fields.Float(
        string="Weekend Atr Factor",
        config_parameter="dankbit.forecast_weekend_atr_factor",
        default=0.75,
        digits=(16, 4),
        help="ATR (wick-size) multiplier on a UTC Saturday/Sunday. Default 0.75.",
    )

    forecast_weekend_body_factor = fields.Float(
        string="Weekend Body Factor",
        config_parameter="dankbit.forecast_weekend_body_factor",
        default=0.65,
        digits=(16, 4),
        help="Body-impulse multiplier on a UTC Saturday/Sunday. Default 0.65.",
    )

    forecast_weekend_shock_factor = fields.Float(
        string="Weekend Shock Factor",
        config_parameter="dankbit.forecast_weekend_shock_factor",
        default=0.75,
        digits=(16, 4),
        help="Shock-strength multiplier on a UTC Saturday/Sunday. Default 0.75.",
    )

    forecast_bucket_hours_fallback = fields.Float(
        string="Bucket Hours Fallback",
        config_parameter="dankbit.forecast_bucket_hours_fallback",
        default=4.0,
        digits=(16, 4),
        help="Assumed real-hours gap between snapshots when no real candles are available to anchor now to. Default 4.0.",
    )

    forecast_gamma_abs_normalizer = fields.Float(
        string="Gamma Abs Normalizer",
        config_parameter="dankbit.forecast_gamma_abs_normalizer",
        default=0.15,
        digits=(16, 4),
        help="Per-asset scale for how big a dollar-gamma leg (bcg_abs/bpg_abs/scg_abs/spg_abs) needs to be to "
             "count as strong conviction — market_maker_gamma_contest, smart_synthetic_liquidity, and "
             "session_activity_score all divide by this to turn a raw dollar-gamma magnitude into a clamped "
             "0-3ish activity score. Default 0.15 (BTC-calibrated).",
    )

    forecast_delta_abs_normalizer = fields.Float(
        string="Delta Abs Normalizer",
        config_parameter="dankbit.forecast_delta_abs_normalizer",
        default=600.0,
        digits=(16, 4),
        help="Per-asset scale for how big a dollar-delta leg (bcd_abs/bpd_abs/scd_abs/spd_abs) needs to be to "
             "count as strong conviction — feeds the Delta Shock Module's strength multiplier, "
             "smart_synthetic_liquidity, greek_flow's Delta Flow signal, and session_activity_score. Default "
             "600.0 (BTC-calibrated).",
    )

    forecast_theta_abs_normalizer = fields.Float(
        string="Theta Abs Normalizer",
        config_parameter="dankbit.forecast_theta_abs_normalizer",
        default=500.0,
        digits=(16, 4),
        help="Per-asset scale for how big a dollar-theta leg (bct_abs/bpt_abs/sct_abs/spt_abs) needs to be to "
             "count as strong conviction — feeds smart_synthetic_liquidity and session_activity_score. Default "
             "500.0 (BTC-calibrated).",
    )

    forecast_vega_abs_normalizer = fields.Float(
        string="Vega Abs Normalizer",
        config_parameter="dankbit.forecast_vega_abs_normalizer",
        default=2500.0,
        digits=(16, 4),
        help="Per-asset scale for how big a dollar-vega leg (bcv_abs/bpv_abs/scv_abs/spv_abs) needs to be to "
             "count as strong conviction — feeds vega_regime's dominance/activity gating, "
             "smart_synthetic_liquidity, greek_flow's Vega Flow signal, and session_activity_score. Default "
             "2500.0 (BTC-calibrated).",
    )

    forecast_session_body_asia = fields.Float(
        string="Session Body (Asia)",
        config_parameter="dankbit.forecast_session_body_asia",
        default=0.7,
        digits=(16, 4),
        help="Body-impulse multiplier for the Asia session. Default 0.7.",
    )

    forecast_session_body_london = fields.Float(
        string="Session Body (London)",
        config_parameter="dankbit.forecast_session_body_london",
        default=0.9,
        digits=(16, 4),
        help="Body-impulse multiplier for the London session. Default 0.9.",
    )

    forecast_session_body_overlap = fields.Float(
        string="Session Body (Overlap)",
        config_parameter="dankbit.forecast_session_body_overlap",
        default=1.05,
        digits=(16, 4),
        help="Body-impulse multiplier for the Overlap session. Default 1.05.",
    )

    forecast_session_body_ny = fields.Float(
        string="Session Body (NY)",
        config_parameter="dankbit.forecast_session_body_ny",
        default=0.95,
        digits=(16, 4),
        help="Body-impulse multiplier for the NY session. Default 0.95.",
    )

    forecast_session_body_postny = fields.Float(
        string="Session Body (PostNY)",
        config_parameter="dankbit.forecast_session_body_postny",
        default=0.7,
        digits=(16, 4),
        help="Body-impulse multiplier for the PostNY session. Default 0.7.",
    )

    forecast_session_atr_asia = fields.Float(
        string="Session Atr (Asia)",
        config_parameter="dankbit.forecast_session_atr_asia",
        default=0.75,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the Asia session. Default 0.75.",
    )

    forecast_session_atr_london = fields.Float(
        string="Session Atr (London)",
        config_parameter="dankbit.forecast_session_atr_london",
        default=0.95,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the London session. Default 0.95.",
    )

    forecast_session_atr_overlap = fields.Float(
        string="Session Atr (Overlap)",
        config_parameter="dankbit.forecast_session_atr_overlap",
        default=1.1,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the Overlap session. Default 1.1.",
    )

    forecast_session_atr_ny = fields.Float(
        string="Session Atr (NY)",
        config_parameter="dankbit.forecast_session_atr_ny",
        default=1.0,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the NY session. Default 1.0.",
    )

    forecast_session_atr_postny = fields.Float(
        string="Session Atr (PostNY)",
        config_parameter="dankbit.forecast_session_atr_postny",
        default=0.75,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the PostNY session. Default 0.75.",
    )

    forecast_session_shock_asia = fields.Float(
        string="Session Shock (Asia)",
        config_parameter="dankbit.forecast_session_shock_asia",
        default=0.65,
        digits=(16, 4),
        help="Shock-strength multiplier for the Asia session. Default 0.65.",
    )

    forecast_session_shock_london = fields.Float(
        string="Session Shock (London)",
        config_parameter="dankbit.forecast_session_shock_london",
        default=0.95,
        digits=(16, 4),
        help="Shock-strength multiplier for the London session. Default 0.95.",
    )

    forecast_session_shock_overlap = fields.Float(
        string="Session Shock (Overlap)",
        config_parameter="dankbit.forecast_session_shock_overlap",
        default=1.1,
        digits=(16, 4),
        help="Shock-strength multiplier for the Overlap session. Default 1.1.",
    )

    forecast_session_shock_ny = fields.Float(
        string="Session Shock (NY)",
        config_parameter="dankbit.forecast_session_shock_ny",
        default=1.0,
        digits=(16, 4),
        help="Shock-strength multiplier for the NY session. Default 1.0.",
    )

    forecast_session_shock_postny = fields.Float(
        string="Session Shock (PostNY)",
        config_parameter="dankbit.forecast_session_shock_postny",
        default=0.65,
        digits=(16, 4),
        help="Shock-strength multiplier for the PostNY session. Default 0.65.",
    )

    forecast_session_firstmove_asia = fields.Float(
        string="Session Firstmove (Asia)",
        config_parameter="dankbit.forecast_session_firstmove_asia",
        default=0.35,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the Asia session. Default 0.35.",
    )

    forecast_session_firstmove_london = fields.Float(
        string="Session Firstmove (London)",
        config_parameter="dankbit.forecast_session_firstmove_london",
        default=0.55,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the London session. Default 0.55.",
    )

    forecast_session_firstmove_overlap = fields.Float(
        string="Session Firstmove (Overlap)",
        config_parameter="dankbit.forecast_session_firstmove_overlap",
        default=0.75,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the Overlap session. Default 0.75.",
    )

    forecast_session_firstmove_ny = fields.Float(
        string="Session Firstmove (NY)",
        config_parameter="dankbit.forecast_session_firstmove_ny",
        default=0.6,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the NY session. Default 0.6.",
    )

    forecast_session_firstmove_postny = fields.Float(
        string="Session Firstmove (PostNY)",
        config_parameter="dankbit.forecast_session_firstmove_postny",
        default=0.35,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the PostNY session. Default 0.35.",
    )

    # FlowImbalance damping, Zone Brake, and the Breakout Gate — see
    # forecast.py's own module-level comment on flow_imbalance()/
    # _zone_brake_mult()/the Breakout Gate block inside simulate_forecast,
    # added per Thales dev feedback (a chat transcript reviewing the
    # 18-candle forecast against a live BTC-4aug26 example, 2026-08-03).
    forecast_flow_imbalance_neutral_threshold = fields.Float(
        string="Flow Imbalance Neutral Threshold",
        config_parameter="dankbit.forecast_flow_imbalance_neutral_threshold",
        default=0.05,
        digits=(16, 4),
        help="Below this |(Longs-Shorts)/(Longs+Shorts)| trade-count imbalance, the raw Long/Short split counts as near-neutral and the body gets damped (see Flow Imbalance Body Damping) regardless of what the Greek levels themselves suggest. Default 0.05.",
    )

    forecast_flow_imbalance_body_damping = fields.Float(
        string="Flow Imbalance Body Damping",
        config_parameter="dankbit.forecast_flow_imbalance_body_damping",
        default=0.20,
        digits=(16, 4),
        help="Fraction body_confidence is reduced by when the raw Long/Short trade count is near-neutral (see Flow Imbalance Neutral Threshold). Default 0.20 (~20%, per Thales dev's own stated first fix).",
    )

    forecast_zone_brake_atr_distance = fields.Float(
        string="Zone Brake ATR Distance",
        config_parameter="dankbit.forecast_zone_brake_atr_distance",
        default=0.5,
        digits=(16, 4),
        help="How many ATRs away from the Zone High/Low edge (top/low) the body brake starts kicking in as the forecast approaches it. Default 0.5.",
    )

    forecast_zone_brake_min_body_mult = fields.Float(
        string="Zone Brake Min Body Mult",
        config_parameter="dankbit.forecast_zone_brake_min_body_mult",
        default=0.40,
        digits=(16, 4),
        help="Floor the Zone Brake's body_confidence multiplier won't shrink below, even right up against the level. Default 0.40.",
    )

    forecast_breakout_gate_wick_bleed = fields.Float(
        string="Breakout Gate Wick Bleed",
        config_parameter="dankbit.forecast_breakout_gate_wick_bleed",
        default=0.6,
        digits=(16, 4),
        help="Fraction of a Breakout-Gate-blocked close (an attempt to cross top/low without structural confirmation) that bleeds into wick instead of being discarded, so a rejected level still shows as tested. Default 0.6.",
    )

    forecast_hours_ahead = fields.Integer(
        string="Hours Ahead",
        config_parameter="dankbit.forecast_hours_ahead",
        default=72,
        help="How many hours out the Thales Forecast path runs (candle count = this / Step Hours). Default 72.",
    )

    forecast_step_hours = fields.Integer(
        string="Step Hours",
        config_parameter="dankbit.forecast_step_hours",
        default=4,
        help="Hours per forecast candle. Default 4.",
    )

    forecast_start_offset_hours = fields.Integer(
        string="Start Offset Hours",
        config_parameter="dankbit.forecast_start_offset_hours",
        default=4,
        help="Hours from now to the first forecast candle. Default 4.",
    )

    # ============================================================
    # Thales Forecast — ETH counterparts. Same fields as the BTC-scoped
    # block above (config_parameter="dankbit.eth_forecast_*"), same
    # unprefixed-is-BTC/eth_-prefixed-is-ETH convention as
    # eth_from_price/eth_weekly_expiry/etc. Added per Thales dev feedback
    # that the forecast engine read as tuned for BTC specifically — every
    # field below defaults to the SAME value as its BTC counterpart until
    # retuned independently; dankbit.forecast.snapshot.get_forecast_cfg(asset)
    # picks the eth_-prefixed key when asset == "ETH".
    # ============================================================
    eth_forecast_gamma_center_weight = fields.Float(
        string="ETH Gamma Center Weight",
        config_parameter="dankbit.eth_forecast_gamma_center_weight",
        default=0.70,
        digits=(16, 4),
        help="Weight of the gamma average in the blended gamma/curve/theta center price the forecast pulls "
             "toward. Default 0.70 (raised from Thales's own 0.55 default per the script author's request to "
             "weight gamma more heavily).",
    )

    eth_forecast_curve_center_weight = fields.Float(
        string="ETH Curve Center Weight",
        config_parameter="dankbit.eth_forecast_curve_center_weight",
        default=0.20,
        digits=(16, 4),
        help="Weight of the BML/SMP curve average in the blended center price. Default 0.20 (lowered from "
             "Thales's own 0.30 default).",
    )

    eth_forecast_theta_center_weight = fields.Float(
        string="ETH Theta Center Weight",
        config_parameter="dankbit.eth_forecast_theta_center_weight",
        default=0.10,
        digits=(16, 4),
        help="Weight of the theta average in the blended center price. Default 0.10 (lowered from Thales's own "
             "0.15 default).",
    )

    eth_forecast_pull_factor = fields.Float(
        string="ETH Pull Factor",
        config_parameter="dankbit.eth_forecast_pull_factor",
        default=0.55,
        digits=(16, 4),
        help="Weight of the pull toward the blended gamma/curve/theta center in the base impulse. Default 0.55.",
    )

    eth_forecast_slope_factor = fields.Float(
        string="ETH Slope Factor",
        config_parameter="dankbit.eth_forecast_slope_factor",
        default=0.35,
        digits=(16, 4),
        help="Weight of the center's own recent slope (momentum) in the base impulse. Default 0.35.",
    )

    eth_forecast_body_factor = fields.Float(
        string="ETH Body Factor",
        config_parameter="dankbit.eth_forecast_body_factor",
        default=0.42,
        digits=(16, 4),
        help="Weight of the most recent real candle's body in the base impulse. Default 0.42.",
    )

    eth_forecast_curve_extreme_body_weight = fields.Float(
        string="ETH Curve Extreme Body Weight",
        config_parameter="dankbit.eth_forecast_curve_extreme_body_weight",
        default=0.26,
        digits=(16, 4),
        help="Weight of the pull toward BML/SMP (curve extremes) in the base impulse. Default 0.26.",
    )

    eth_forecast_wick_factor = fields.Float(
        string="ETH Wick Factor",
        config_parameter="dankbit.eth_forecast_wick_factor",
        default=0.35,
        digits=(16, 4),
        help="Share of the remaining room to the wick target that becomes wick length. Default 0.35.",
    )

    eth_forecast_atr_factor = fields.Float(
        string="ETH Atr Factor",
        config_parameter="dankbit.eth_forecast_atr_factor",
        default=0.3,
        digits=(16, 4),
        help="How much of the session/weekend-adjusted ATR is added to each wick. Default 0.3.",
    )

    eth_forecast_curve_wick_weight = fields.Float(
        string="ETH Curve Wick Weight",
        config_parameter="dankbit.eth_forecast_curve_wick_weight",
        default=0.42,
        digits=(16, 4),
        help="Weight of BML/SMP when computing the upper/lower wick target price. Default 0.42.",
    )

    eth_forecast_greek_flow_impulse_weight = fields.Float(
        string="ETH Greek Flow Impulse Weight",
        config_parameter="dankbit.eth_forecast_greek_flow_impulse_weight",
        default=1.75,
        digits=(16, 4),
        help="Greek Flow Priority Hybrid: multiplier on the Greek Flow engine's own impulse before it's added "
             "alongside every other engine's impulse (Gamma-Band, Delta/Gamma Shock, Vega, Market-Maker, "
             "Liquidity, ...) in the base body/close estimate. Default 1.75 makes Greek Flow the dominant "
             "additive voice for body direction, per the design decision that it should drive Close while the "
             "structural engines (Bands, Smart Liquidity, Session Activity, Vega) keep constraining how far "
             "that move is allowed to travel rather than being removed.",
    )

    eth_forecast_gb_opposite_wick_compression = fields.Float(
        string="ETH Gb Opposite Wick Compression",
        config_parameter="dankbit.eth_forecast_gb_opposite_wick_compression",
        default=0.18,
        digits=(16, 4),
        help="Max fraction the wick opposite a Gamma-Band consensus direction is compressed by. Default 0.18.",
    )

    eth_forecast_gb_confirmed_target_boost = fields.Float(
        string="ETH Gb Confirmed Target Boost",
        config_parameter="dankbit.eth_forecast_gb_confirmed_target_boost",
        default=0.55,
        digits=(16, 4),
        help="Extra weight given to top/low as the wick target when Gamma-Band Consensus confirms that side. "
             "Default 0.55.",
    )

    eth_forecast_gb_confidence_boost = fields.Float(
        string="ETH Gb Confidence Boost",
        config_parameter="dankbit.eth_forecast_gb_confidence_boost",
        default=0.2,
        digits=(16, 4),
        help="Body-confidence boost when Gamma-Band Consensus is directionally active. Default 0.2.",
    )

    eth_forecast_gb_conflict_body_damping = fields.Float(
        string="ETH Gb Conflict Body Damping",
        config_parameter="dankbit.eth_forecast_gb_conflict_body_damping",
        default=0.3,
        digits=(16, 4),
        help="Body-confidence damping when Gamma-Band Consensus signals are in conflict. Default 0.3.",
    )

    eth_forecast_gb_conflict_wick_expansion = fields.Float(
        string="ETH Gb Conflict Wick Expansion",
        config_parameter="dankbit.eth_forecast_gb_conflict_wick_expansion",
        default=0.25,
        digits=(16, 4),
        help="Wick expansion when Gamma-Band Consensus signals are in conflict. Default 0.25.",
    )

    eth_forecast_gb_opposing_magnet_damping = fields.Float(
        string="ETH Gb Opposing Magnet Damping",
        config_parameter="dankbit.eth_forecast_gb_opposing_magnet_damping",
        default=0.6,
        digits=(16, 4),
        help="Damping applied to the gamma-gap pull when it opposes the Gamma-Band Consensus direction. Default 0.6.",
    )

    eth_forecast_gb_trend_lock_strength = fields.Float(
        string="ETH Gb Trend Lock Strength",
        config_parameter="dankbit.eth_forecast_gb_trend_lock_strength",
        default=0.55,
        digits=(16, 4),
        help="Minimum Gamma-Band Consensus strength (with all 3 series aligned) to arm the Trend Lock. Default 0.55.",
    )

    eth_forecast_gb_counter_body_damping = fields.Float(
        string="ETH Gb Counter Body Damping",
        config_parameter="dankbit.eth_forecast_gb_counter_body_damping",
        default=0.18,
        digits=(16, 4),
        help="Body-impulse damping applied to the current real candle while the Trend Lock is engaged. Default 0.18.",
    )

    eth_forecast_gb_counter_max_opp_impulse = fields.Float(
        string="ETH Gb Counter Max Opp Impulse",
        config_parameter="dankbit.eth_forecast_gb_counter_max_opp_impulse",
        default=0.03,
        digits=(16, 4),
        help="Maximum opposite-direction forecast impulse allowed while the Trend Lock is engaged. Default 0.03.",
    )

    eth_forecast_gb_counter_escape_atr = fields.Float(
        string="ETH Gb Counter Escape Atr",
        config_parameter="dankbit.eth_forecast_gb_counter_escape_atr",
        default=0.95,
        digits=(16, 4),
        help="Body-to-ATR ratio a real candle needs to escape the Trend Lock. Default 0.95.",
    )

    eth_forecast_gb_term_slope_impulse_strength = fields.Float(
        string="ETH Gb Term Slope Impulse Strength",
        config_parameter="dankbit.eth_forecast_gb_term_slope_impulse_strength",
        default=0.16,
        digits=(16, 4),
        help="Weight of the forward slope between the nearest and next tracked expiry's own Gamma Band point "
             "(the chart's dashed line's forward-most segment) in the base impulse. Default 0.16.",
    )

    eth_forecast_gb_term_slope_max_impulse = fields.Float(
        string="ETH Gb Term Slope Max Impulse",
        config_parameter="dankbit.eth_forecast_gb_term_slope_max_impulse",
        default=0.2,
        digits=(16, 4),
        help="Cap on the per-step impulse from the Gamma Band term-structure slope. Default 0.2.",
    )

    eth_forecast_gamma_confirm_buffer_pct = fields.Float(
        string="ETH Gamma Confirm Buffer Pct",
        config_parameter="dankbit.eth_forecast_gamma_confirm_buffer_pct",
        default=0.06,
        digits=(16, 4),
        help="Buffer (as a fraction of band width) a close must clear the gamma reference by to confirm a "
             "break. Default 0.06.",
    )

    eth_forecast_cluster_alignment_threshold = fields.Float(
        string="ETH Cluster Alignment Threshold",
        config_parameter="dankbit.eth_forecast_cluster_alignment_threshold",
        default=0.6,
        digits=(16, 4),
        help="Minimum directional alignment across top/low/gamma/BML/SMP to treat a cluster expansion as "
             "directional. Default 0.6.",
    )

    eth_forecast_cluster_body_confidence_floor = fields.Float(
        string="ETH Cluster Body Confidence Floor",
        config_parameter="dankbit.eth_forecast_cluster_body_confidence_floor",
        default=0.58,
        digits=(16, 4),
        help="Body-confidence floor when the Option Cluster is expanding and aligned with the candle's own "
             "direction. Default 0.58.",
    )

    eth_forecast_cluster_compressed_threshold = fields.Float(
        string="ETH Cluster Compressed Threshold",
        config_parameter="dankbit.eth_forecast_cluster_compressed_threshold",
        default=0.18,
        digits=(16, 4),
        help="Dispersion (in band-widths) below which the Option Cluster is considered compressed. Default 0.18.",
    )

    eth_forecast_cluster_compression_body_damping = fields.Float(
        string="ETH Cluster Compression Body Damping",
        config_parameter="dankbit.eth_forecast_cluster_compression_body_damping",
        default=0.15,
        digits=(16, 4),
        help="Body-confidence damping applied while the Option Cluster is compressed. Default 0.15.",
    )

    eth_forecast_cluster_compression_wick_compression = fields.Float(
        string="ETH Cluster Compression Wick Compression",
        config_parameter="dankbit.eth_forecast_cluster_compression_wick_compression",
        default=0.3,
        digits=(16, 4),
        help="Wick compression applied while the Option Cluster is compressed. Default 0.3.",
    )

    eth_forecast_cluster_expansion_threshold = fields.Float(
        string="ETH Cluster Expansion Threshold",
        config_parameter="dankbit.eth_forecast_cluster_expansion_threshold",
        default=0.025,
        digits=(16, 4),
        help="Dispersion-change (in band-widths) needed for the Option Cluster to register as expanding. "
             "Default 0.025.",
    )

    eth_forecast_liquidity_aligned_wick_compression = fields.Float(
        string="ETH Liquidity Aligned Wick Compression",
        config_parameter="dankbit.eth_forecast_liquidity_aligned_wick_compression",
        default=0.25,
        digits=(16, 4),
        help="Wick compression when the current candle's direction aligns with the dominant Smart Synthetic "
             "Liquidity side. Default 0.25.",
    )

    eth_forecast_liquidity_body_confidence_floor = fields.Float(
        string="ETH Liquidity Body Confidence Floor",
        config_parameter="dankbit.eth_forecast_liquidity_body_confidence_floor",
        default=0.6,
        digits=(16, 4),
        help="Body-confidence floor when liquidity dominance/sweep-rejection is active. Default 0.6.",
    )

    eth_forecast_liquidity_opposite_wick_compression = fields.Float(
        string="ETH Liquidity Opposite Wick Compression",
        config_parameter="dankbit.eth_forecast_liquidity_opposite_wick_compression",
        default=0.35,
        digits=(16, 4),
        help="Wick compression on the side opposite the dominant liquidity side. Default 0.35.",
    )

    eth_forecast_liquidity_sweep_wick_compression = fields.Float(
        string="ETH Liquidity Sweep Wick Compression",
        config_parameter="dankbit.eth_forecast_liquidity_sweep_wick_compression",
        default=0.7,
        digits=(16, 4),
        help="Wick compression right after a liquidity level is swept and rejected. Default 0.7.",
    )

    eth_forecast_momentum_body_confidence_floor = fields.Float(
        string="ETH Momentum Body Confidence Floor",
        config_parameter="dankbit.eth_forecast_momentum_body_confidence_floor",
        default=0.62,
        digits=(16, 4),
        help="Body-confidence floor while the Momentum/Liquidity-Sweep Override is active. Default 0.62.",
    )

    eth_forecast_momentum_wick_compression = fields.Float(
        string="ETH Momentum Wick Compression",
        config_parameter="dankbit.eth_forecast_momentum_wick_compression",
        default=0.75,
        digits=(16, 4),
        help="Wick compression while the Momentum/Liquidity-Sweep Override is active. Default 0.75.",
    )

    eth_forecast_near_gamma_body_damping = fields.Float(
        string="ETH Near Gamma Body Damping",
        config_parameter="dankbit.eth_forecast_near_gamma_body_damping",
        default=0.4,
        digits=(16, 4),
        help="Body-confidence damping near the Gamma Neutral/Hysteresis Zone. Default 0.4.",
    )

    eth_forecast_near_gamma_wick_expansion = fields.Float(
        string="ETH Near Gamma Wick Expansion",
        config_parameter="dankbit.eth_forecast_near_gamma_wick_expansion",
        default=0.25,
        digits=(16, 4),
        help="Wick expansion near the Gamma Neutral/Hysteresis Zone. Default 0.25.",
    )

    eth_forecast_high_vol_pull_factor = fields.Float(
        string="ETH High Vol Pull Factor",
        config_parameter="dankbit.eth_forecast_high_vol_pull_factor",
        default=1.05,
        digits=(16, 4),
        help="Pull-factor multiplier during the London/Overlap/NY high-volume regime. Default 1.05.",
    )

    eth_forecast_low_vol_pull_factor = fields.Float(
        string="ETH Low Vol Pull Factor",
        config_parameter="dankbit.eth_forecast_low_vol_pull_factor",
        default=0.75,
        digits=(16, 4),
        help="Pull-factor multiplier during the Asia/Post-NY low-volume regime. Default 0.75.",
    )

    eth_forecast_weekday_pull_factor = fields.Float(
        string="ETH Weekday Pull Factor",
        config_parameter="dankbit.eth_forecast_weekday_pull_factor",
        default=1.0,
        digits=(16, 4),
        help="Pull-factor multiplier outside the low/high-volume regimes. Default 1.0.",
    )

    eth_forecast_high_vol_shock_factor = fields.Float(
        string="ETH High Vol Shock Factor",
        config_parameter="dankbit.eth_forecast_high_vol_shock_factor",
        default=1.1,
        digits=(16, 4),
        help="Shock-strength multiplier during the London/Overlap/NY high-volume regime. Default 1.1.",
    )

    eth_forecast_low_vol_shock_factor = fields.Float(
        string="ETH Low Vol Shock Factor",
        config_parameter="dankbit.eth_forecast_low_vol_shock_factor",
        default=0.7,
        digits=(16, 4),
        help="Shock-strength multiplier during the Asia/Post-NY low-volume regime. Default 0.7.",
    )

    eth_forecast_weekday_shock_factor = fields.Float(
        string="ETH Weekday Shock Factor",
        config_parameter="dankbit.eth_forecast_weekday_shock_factor",
        default=1.0,
        digits=(16, 4),
        help="Shock-strength multiplier outside the low/high-volume regimes. Default 1.0.",
    )

    eth_forecast_weekend_atr_factor = fields.Float(
        string="ETH Weekend Atr Factor",
        config_parameter="dankbit.eth_forecast_weekend_atr_factor",
        default=0.75,
        digits=(16, 4),
        help="ATR (wick-size) multiplier on a UTC Saturday/Sunday. Default 0.75.",
    )

    eth_forecast_weekend_body_factor = fields.Float(
        string="ETH Weekend Body Factor",
        config_parameter="dankbit.eth_forecast_weekend_body_factor",
        default=0.65,
        digits=(16, 4),
        help="Body-impulse multiplier on a UTC Saturday/Sunday. Default 0.65.",
    )

    eth_forecast_weekend_shock_factor = fields.Float(
        string="ETH Weekend Shock Factor",
        config_parameter="dankbit.eth_forecast_weekend_shock_factor",
        default=0.75,
        digits=(16, 4),
        help="Shock-strength multiplier on a UTC Saturday/Sunday. Default 0.75.",
    )

    eth_forecast_bucket_hours_fallback = fields.Float(
        string="ETH Bucket Hours Fallback",
        config_parameter="dankbit.eth_forecast_bucket_hours_fallback",
        default=4.0,
        digits=(16, 4),
        help="Assumed real-hours gap between snapshots when no real candles are available to anchor now to. "
             "Default 4.0.",
    )

    eth_forecast_session_body_asia = fields.Float(
        string="ETH Session Body (Asia)",
        config_parameter="dankbit.eth_forecast_session_body_asia",
        default=0.7,
        digits=(16, 4),
        help="Body-impulse multiplier for the Asia session. Default 0.7.",
    )

    eth_forecast_session_body_london = fields.Float(
        string="ETH Session Body (London)",
        config_parameter="dankbit.eth_forecast_session_body_london",
        default=0.9,
        digits=(16, 4),
        help="Body-impulse multiplier for the London session. Default 0.9.",
    )

    eth_forecast_session_body_overlap = fields.Float(
        string="ETH Session Body (Overlap)",
        config_parameter="dankbit.eth_forecast_session_body_overlap",
        default=1.05,
        digits=(16, 4),
        help="Body-impulse multiplier for the Overlap session. Default 1.05.",
    )

    eth_forecast_session_body_ny = fields.Float(
        string="ETH Session Body (NY)",
        config_parameter="dankbit.eth_forecast_session_body_ny",
        default=0.95,
        digits=(16, 4),
        help="Body-impulse multiplier for the NY session. Default 0.95.",
    )

    eth_forecast_session_body_postny = fields.Float(
        string="ETH Session Body (PostNY)",
        config_parameter="dankbit.eth_forecast_session_body_postny",
        default=0.7,
        digits=(16, 4),
        help="Body-impulse multiplier for the PostNY session. Default 0.7.",
    )

    eth_forecast_session_atr_asia = fields.Float(
        string="ETH Session Atr (Asia)",
        config_parameter="dankbit.eth_forecast_session_atr_asia",
        default=0.75,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the Asia session. Default 0.75.",
    )

    eth_forecast_session_atr_london = fields.Float(
        string="ETH Session Atr (London)",
        config_parameter="dankbit.eth_forecast_session_atr_london",
        default=0.95,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the London session. Default 0.95.",
    )

    eth_forecast_session_atr_overlap = fields.Float(
        string="ETH Session Atr (Overlap)",
        config_parameter="dankbit.eth_forecast_session_atr_overlap",
        default=1.1,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the Overlap session. Default 1.1.",
    )

    eth_forecast_session_atr_ny = fields.Float(
        string="ETH Session Atr (NY)",
        config_parameter="dankbit.eth_forecast_session_atr_ny",
        default=1.0,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the NY session. Default 1.0.",
    )

    eth_forecast_session_atr_postny = fields.Float(
        string="ETH Session Atr (PostNY)",
        config_parameter="dankbit.eth_forecast_session_atr_postny",
        default=0.75,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the PostNY session. Default 0.75.",
    )

    eth_forecast_session_shock_asia = fields.Float(
        string="ETH Session Shock (Asia)",
        config_parameter="dankbit.eth_forecast_session_shock_asia",
        default=0.65,
        digits=(16, 4),
        help="Shock-strength multiplier for the Asia session. Default 0.65.",
    )

    eth_forecast_session_shock_london = fields.Float(
        string="ETH Session Shock (London)",
        config_parameter="dankbit.eth_forecast_session_shock_london",
        default=0.95,
        digits=(16, 4),
        help="Shock-strength multiplier for the London session. Default 0.95.",
    )

    eth_forecast_session_shock_overlap = fields.Float(
        string="ETH Session Shock (Overlap)",
        config_parameter="dankbit.eth_forecast_session_shock_overlap",
        default=1.1,
        digits=(16, 4),
        help="Shock-strength multiplier for the Overlap session. Default 1.1.",
    )

    eth_forecast_session_shock_ny = fields.Float(
        string="ETH Session Shock (NY)",
        config_parameter="dankbit.eth_forecast_session_shock_ny",
        default=1.0,
        digits=(16, 4),
        help="Shock-strength multiplier for the NY session. Default 1.0.",
    )

    eth_forecast_session_shock_postny = fields.Float(
        string="ETH Session Shock (PostNY)",
        config_parameter="dankbit.eth_forecast_session_shock_postny",
        default=0.65,
        digits=(16, 4),
        help="Shock-strength multiplier for the PostNY session. Default 0.65.",
    )

    eth_forecast_session_firstmove_asia = fields.Float(
        string="ETH Session Firstmove (Asia)",
        config_parameter="dankbit.eth_forecast_session_firstmove_asia",
        default=0.35,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the Asia session. Default 0.35.",
    )

    eth_forecast_session_firstmove_london = fields.Float(
        string="ETH Session Firstmove (London)",
        config_parameter="dankbit.eth_forecast_session_firstmove_london",
        default=0.55,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the London session. Default 0.55.",
    )

    eth_forecast_session_firstmove_overlap = fields.Float(
        string="ETH Session Firstmove (Overlap)",
        config_parameter="dankbit.eth_forecast_session_firstmove_overlap",
        default=0.75,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the Overlap session. Default 0.75.",
    )

    eth_forecast_session_firstmove_ny = fields.Float(
        string="ETH Session Firstmove (NY)",
        config_parameter="dankbit.eth_forecast_session_firstmove_ny",
        default=0.6,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the NY session. Default 0.6.",
    )

    eth_forecast_session_firstmove_postny = fields.Float(
        string="ETH Session Firstmove (PostNY)",
        config_parameter="dankbit.eth_forecast_session_firstmove_postny",
        default=0.35,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the PostNY session. Default 0.35.",
    )

    eth_forecast_flow_imbalance_neutral_threshold = fields.Float(
        string="ETH Flow Imbalance Neutral Threshold",
        config_parameter="dankbit.eth_forecast_flow_imbalance_neutral_threshold",
        default=0.05,
        digits=(16, 4),
        help="Below this |(Longs-Shorts)/(Longs+Shorts)| trade-count imbalance, the raw Long/Short split counts as near-neutral and the body gets damped (see Flow Imbalance Body Damping) regardless of what the Greek levels themselves suggest. Default 0.05.",
    )

    eth_forecast_flow_imbalance_body_damping = fields.Float(
        string="ETH Flow Imbalance Body Damping",
        config_parameter="dankbit.eth_forecast_flow_imbalance_body_damping",
        default=0.20,
        digits=(16, 4),
        help="Fraction body_confidence is reduced by when the raw Long/Short trade count is near-neutral (see Flow Imbalance Neutral Threshold). Default 0.20 (~20%, per Thales dev's own stated first fix).",
    )

    eth_forecast_zone_brake_atr_distance = fields.Float(
        string="ETH Zone Brake ATR Distance",
        config_parameter="dankbit.eth_forecast_zone_brake_atr_distance",
        default=0.5,
        digits=(16, 4),
        help="How many ATRs away from the Zone High/Low edge (top/low) the body brake starts kicking in as the forecast approaches it. Default 0.5.",
    )

    eth_forecast_zone_brake_min_body_mult = fields.Float(
        string="ETH Zone Brake Min Body Mult",
        config_parameter="dankbit.eth_forecast_zone_brake_min_body_mult",
        default=0.40,
        digits=(16, 4),
        help="Floor the Zone Brake's body_confidence multiplier won't shrink below, even right up against the level. Default 0.40.",
    )

    eth_forecast_breakout_gate_wick_bleed = fields.Float(
        string="ETH Breakout Gate Wick Bleed",
        config_parameter="dankbit.eth_forecast_breakout_gate_wick_bleed",
        default=0.6,
        digits=(16, 4),
        help="Fraction of a Breakout-Gate-blocked close (an attempt to cross top/low without structural confirmation) that bleeds into wick instead of being discarded, so a rejected level still shows as tested. Default 0.6.",
    )

    eth_forecast_hours_ahead = fields.Integer(
        string="ETH Hours Ahead",
        config_parameter="dankbit.eth_forecast_hours_ahead",
        default=72,
        help="How many hours out the Thales Forecast path runs (candle count = this / Step Hours). Default 72.",
    )

    eth_forecast_step_hours = fields.Integer(
        string="ETH Step Hours",
        config_parameter="dankbit.eth_forecast_step_hours",
        default=4,
        help="Hours per forecast candle. Default 4.",
    )

    eth_forecast_start_offset_hours = fields.Integer(
        string="ETH Start Offset Hours",
        config_parameter="dankbit.eth_forecast_start_offset_hours",
        default=4,
        help="Hours from now to the first forecast candle. Default 4.",
    )

    eth_forecast_gamma_abs_normalizer = fields.Float(
        string="ETH Gamma Abs Normalizer",
        config_parameter="dankbit.eth_forecast_gamma_abs_normalizer",
        default=0.02,
        digits=(16, 4),
        help="Per-asset scale for how big a dollar-gamma leg (bcg_abs/bpg_abs/scg_abs/spg_abs) needs to be to "
             "count as strong conviction — market_maker_gamma_contest, smart_synthetic_liquidity, and "
             "session_activity_score all divide by this to turn a raw dollar-gamma magnitude into a clamped "
             "0-3ish activity score. Default 0.02 — ETH-calibrated: median dollar-gamma leg magnitude in "
             "dankbit.forecast.snapshot ran ~0.13x BTC's over the first ~2 days of history (BTC's own default "
             "is 0.15), not simply \"smaller across the board\" like every other Greek here — revisit via the "
             "Forecast Log once more accuracy history accumulates.",
    )

    eth_forecast_delta_abs_normalizer = fields.Float(
        string="ETH Delta Abs Normalizer",
        config_parameter="dankbit.eth_forecast_delta_abs_normalizer",
        default=2250.0,
        digits=(16, 4),
        help="Per-asset scale for how big a dollar-delta leg (bcd_abs/bpd_abs/scd_abs/spd_abs) needs to be to "
             "count as strong conviction — feeds the Delta Shock Module's strength multiplier, "
             "smart_synthetic_liquidity, greek_flow's Delta Flow signal, and session_activity_score. Default "
             "2250.0 — ETH-calibrated: median dollar-delta leg magnitude in dankbit.forecast.snapshot ran "
             "~3.8x BTC's over the first ~2 days of history (BTC's own default is 600.0) — the one Greek where "
             "ETH runs LARGER than BTC, not smaller, likely from bigger ETH contract-count position sizes — "
             "revisit via the Forecast Log once more accuracy history accumulates.",
    )

    eth_forecast_theta_abs_normalizer = fields.Float(
        string="ETH Theta Abs Normalizer",
        config_parameter="dankbit.eth_forecast_theta_abs_normalizer",
        default=110.0,
        digits=(16, 4),
        help="Per-asset scale for how big a dollar-theta leg (bct_abs/bpt_abs/sct_abs/spt_abs) needs to be to "
             "count as strong conviction — feeds smart_synthetic_liquidity and session_activity_score. Default "
             "110.0 — ETH-calibrated: median dollar-theta leg magnitude in dankbit.forecast.snapshot ran ~0.22x "
             "BTC's over the first ~2 days of history (BTC's own default is 500.0) — revisit via the Forecast "
             "Log once more accuracy history accumulates.",
    )

    eth_forecast_vega_abs_normalizer = fields.Float(
        string="ETH Vega Abs Normalizer",
        config_parameter="dankbit.eth_forecast_vega_abs_normalizer",
        default=250.0,
        digits=(16, 4),
        help="Per-asset scale for how big a dollar-vega leg (bcv_abs/bpv_abs/scv_abs/spv_abs) needs to be to "
             "count as strong conviction — feeds vega_regime's dominance/activity gating, "
             "smart_synthetic_liquidity, greek_flow's Vega Flow signal, and session_activity_score. Default "
             "250.0 — ETH-calibrated: median dollar-vega leg magnitude in dankbit.forecast.snapshot ran ~0.10x "
             "BTC's over the first ~2 days of history (BTC's own default is 2500.0) — revisit via the Forecast "
             "Log once more accuracy history accumulates.",
    )

    # Thales Forecast candle colors — rendering-only (the TradingView
    # chart's forecastSeries), not part of simulate_forecast()'s own
    # cfg dict, so these are read by _build_tv_chart_context() (main.py)
    # instead of _forecast_cfg(). Light red/green so the forecast
    # candles read as directional at a glance while staying visually
    # distinct from the real candle series' own teal/red (#26a69a/
    # #ef5350) — plain hex string fields, same as weekly_expiry/
    # monthly_expiry above; no color-picker widget, just a hex value
    # typed/pasted in.
    forecast_up_color = fields.Char(
        string="Forecast Up Color",
        config_parameter="dankbit.forecast_up_color",
        default="#a5d6a7",
        help="Body color for bullish (close >= open) Thales Forecast candles. Hex color, default #a5d6a7 (light green).",
    )

    forecast_down_color = fields.Char(
        string="Forecast Down Color",
        config_parameter="dankbit.forecast_down_color",
        default="#ef9a9a",
        help="Body color for bearish Thales Forecast candles. Hex color, default #ef9a9a (light red).",
    )

    forecast_wick_up_color = fields.Char(
        string="Forecast Wick Up Color",
        config_parameter="dankbit.forecast_wick_up_color",
        default="#66bb6a",
        help="Wick color for bullish Thales Forecast candles. Hex color, default #66bb6a (green).",
    )

    forecast_wick_down_color = fields.Char(
        string="Forecast Wick Down Color",
        config_parameter="dankbit.forecast_wick_down_color",
        default="#e57373",
        help="Wick color for bearish Thales Forecast candles. Hex color, default #e57373 (red).",
    )

    def get_values(self):
        res = super().get_values()
        icp = self.env["ir.config_parameter"].sudo()
        res.update(
            show_daily_lines=icp.get_param("dankbit.show_daily_lines", "True") == "True",
            show_weekly_lines=icp.get_param("dankbit.show_weekly_lines", "True") == "True",
            show_monthly_lines=icp.get_param("dankbit.show_monthly_lines", "True") == "True",
            forecast_trade_weighted_greeks=icp.get_param("dankbit.forecast_trade_weighted_greeks", "False") == "True",
        )
        return res

    def set_values(self):
        super().set_values()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("dankbit.show_daily_lines", str(self.show_daily_lines))
        icp.set_param("dankbit.show_weekly_lines", str(self.show_weekly_lines))
        icp.set_param("dankbit.show_monthly_lines", str(self.show_monthly_lines))
        icp.set_param("dankbit.forecast_trade_weighted_greeks", str(self.forecast_trade_weighted_greeks))


