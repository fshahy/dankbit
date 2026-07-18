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
        string="Zones box refresh interval (s)",
        config_parameter="dankbit.zones_box_refresh_interval",
        help="How often (in seconds) the TradingView chart re-fetches the yellow/teal zones boxes. Defaults to 3600 (1 hour).",
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

    # ============================================================
    # Thales Forecast ("Forecast 3") — the top-level tunables
    # simulate_forecast3() itself uses directly (see forecast3.py's
    # module docstring and simulate_forecast3's `cfg` parameter). Fields
    # left unset fall back to the engine's own hardcoded default (shown
    # in each field's help text) via icp.get_param(key, default=...) at
    # the call site (main.py's forecast3_json) — same convention
    # from_price/steps/etc. above use, no field-level `default=`.
    # Constants private to nested helper functions (vega_regime,
    # market_maker_gamma_contest, cluster_*, smart_synthetic_liquidity's
    # internals, delta_shock_module, gamma_shock_module, etc.) are not
    # exposed here — see the "Top-level only" scoping decision in
    # CLAUDE.md's Forecast 3 candles section. The 3 center-weight fields
    # immediately below are the one exception: derive_levels() is a
    # nested helper, not simulate_forecast3's own body, but its own
    # `cfg` parameter was added specifically so these could be tuned —
    # the Pine script's original author asked for the forecast to weight
    # gamma more heavily so candles track the gamma reference line more
    # closely, which is exactly this blend.
    # ============================================================
    forecast3_gamma_center_weight = fields.Float(
        string="Gamma Center Weight",
        config_parameter="dankbit.forecast3_gamma_center_weight",
        default=0.70,
        digits=(16, 4),
        help="Weight of the gamma average in the blended gamma/curve/theta center price the forecast pulls toward. Default 0.70 (raised from Thales's own 0.55 default per the script author's request to weight gamma more heavily).",
    )

    forecast3_curve_center_weight = fields.Float(
        string="Curve Center Weight",
        config_parameter="dankbit.forecast3_curve_center_weight",
        default=0.20,
        digits=(16, 4),
        help="Weight of the BML/SMP curve average in the blended center price. Default 0.20 (lowered from Thales's own 0.30 default).",
    )

    forecast3_theta_center_weight = fields.Float(
        string="Theta Center Weight",
        config_parameter="dankbit.forecast3_theta_center_weight",
        default=0.10,
        digits=(16, 4),
        help="Weight of the theta average in the blended center price. Default 0.10 (lowered from Thales's own 0.15 default).",
    )

    forecast3_pull_factor = fields.Float(
        string="Pull Factor",
        config_parameter="dankbit.forecast3_pull_factor",
        default=0.55,
        digits=(16, 4),
        help="Weight of the pull toward the blended gamma/curve/theta center in the base impulse. Default 0.55.",
    )

    forecast3_slope_factor = fields.Float(
        string="Slope Factor",
        config_parameter="dankbit.forecast3_slope_factor",
        default=0.35,
        digits=(16, 4),
        help="Weight of the center's own recent slope (momentum) in the base impulse. Default 0.35.",
    )

    forecast3_body_factor = fields.Float(
        string="Body Factor",
        config_parameter="dankbit.forecast3_body_factor",
        default=0.3,
        digits=(16, 4),
        help="Weight of the most recent real candle's body in the base impulse. Default 0.3.",
    )

    forecast3_curve_extreme_body_weight = fields.Float(
        string="Curve Extreme Body Weight",
        config_parameter="dankbit.forecast3_curve_extreme_body_weight",
        default=0.26,
        digits=(16, 4),
        help="Weight of the pull toward BML/SMP (curve extremes) in the base impulse. Default 0.26.",
    )

    forecast3_wick_factor = fields.Float(
        string="Wick Factor",
        config_parameter="dankbit.forecast3_wick_factor",
        default=0.35,
        digits=(16, 4),
        help="Share of the remaining room to the wick target that becomes wick length. Default 0.35.",
    )

    forecast3_atr_factor = fields.Float(
        string="Atr Factor",
        config_parameter="dankbit.forecast3_atr_factor",
        default=0.3,
        digits=(16, 4),
        help="How much of the session/weekend-adjusted ATR is added to each wick. Default 0.3.",
    )

    forecast3_curve_wick_weight = fields.Float(
        string="Curve Wick Weight",
        config_parameter="dankbit.forecast3_curve_wick_weight",
        default=0.42,
        digits=(16, 4),
        help="Weight of BML/SMP when computing the upper/lower wick target price. Default 0.42.",
    )

    forecast3_gb_opposite_wick_compression = fields.Float(
        string="Gb Opposite Wick Compression",
        config_parameter="dankbit.forecast3_gb_opposite_wick_compression",
        default=0.18,
        digits=(16, 4),
        help="Max fraction the wick opposite a Gamma-Band consensus direction is compressed by. Default 0.18.",
    )

    forecast3_gb_confirmed_target_boost = fields.Float(
        string="Gb Confirmed Target Boost",
        config_parameter="dankbit.forecast3_gb_confirmed_target_boost",
        default=0.55,
        digits=(16, 4),
        help="Extra weight given to top/low as the wick target when Gamma-Band Consensus confirms that side. Default 0.55.",
    )

    forecast3_gb_confidence_boost = fields.Float(
        string="Gb Confidence Boost",
        config_parameter="dankbit.forecast3_gb_confidence_boost",
        default=0.2,
        digits=(16, 4),
        help="Body-confidence boost when Gamma-Band Consensus is directionally active. Default 0.2.",
    )

    forecast3_gb_conflict_body_damping = fields.Float(
        string="Gb Conflict Body Damping",
        config_parameter="dankbit.forecast3_gb_conflict_body_damping",
        default=0.3,
        digits=(16, 4),
        help="Body-confidence damping when Gamma-Band Consensus signals are in conflict. Default 0.3.",
    )

    forecast3_gb_conflict_wick_expansion = fields.Float(
        string="Gb Conflict Wick Expansion",
        config_parameter="dankbit.forecast3_gb_conflict_wick_expansion",
        default=0.25,
        digits=(16, 4),
        help="Wick expansion when Gamma-Band Consensus signals are in conflict. Default 0.25.",
    )

    forecast3_gb_opposing_magnet_damping = fields.Float(
        string="Gb Opposing Magnet Damping",
        config_parameter="dankbit.forecast3_gb_opposing_magnet_damping",
        default=0.6,
        digits=(16, 4),
        help="Damping applied to the gamma-gap pull when it opposes the Gamma-Band Consensus direction. Default 0.6.",
    )

    forecast3_gb_trend_lock_strength = fields.Float(
        string="Gb Trend Lock Strength",
        config_parameter="dankbit.forecast3_gb_trend_lock_strength",
        default=0.55,
        digits=(16, 4),
        help="Minimum Gamma-Band Consensus strength (with all 3 series aligned) to arm the Trend Lock. Default 0.55.",
    )

    forecast3_gb_counter_body_damping = fields.Float(
        string="Gb Counter Body Damping",
        config_parameter="dankbit.forecast3_gb_counter_body_damping",
        default=0.18,
        digits=(16, 4),
        help="Body-impulse damping applied to the current real candle while the Trend Lock is engaged. Default 0.18.",
    )

    forecast3_gb_counter_max_opp_impulse = fields.Float(
        string="Gb Counter Max Opp Impulse",
        config_parameter="dankbit.forecast3_gb_counter_max_opp_impulse",
        default=0.03,
        digits=(16, 4),
        help="Maximum opposite-direction forecast impulse allowed while the Trend Lock is engaged. Default 0.03.",
    )

    forecast3_gb_counter_escape_atr = fields.Float(
        string="Gb Counter Escape Atr",
        config_parameter="dankbit.forecast3_gb_counter_escape_atr",
        default=0.95,
        digits=(16, 4),
        help="Body-to-ATR ratio a real candle needs to escape the Trend Lock. Default 0.95.",
    )

    forecast3_gamma_confirm_buffer_pct = fields.Float(
        string="Gamma Confirm Buffer Pct",
        config_parameter="dankbit.forecast3_gamma_confirm_buffer_pct",
        default=0.06,
        digits=(16, 4),
        help="Buffer (as a fraction of band width) a close must clear the gamma reference by to confirm a break. Default 0.06.",
    )

    forecast3_cluster_alignment_threshold = fields.Float(
        string="Cluster Alignment Threshold",
        config_parameter="dankbit.forecast3_cluster_alignment_threshold",
        default=0.6,
        digits=(16, 4),
        help="Minimum directional alignment across top/low/gamma/BML/SMP to treat a cluster expansion as directional. Default 0.6.",
    )

    forecast3_cluster_body_confidence_floor = fields.Float(
        string="Cluster Body Confidence Floor",
        config_parameter="dankbit.forecast3_cluster_body_confidence_floor",
        default=0.58,
        digits=(16, 4),
        help="Body-confidence floor when the Option Cluster is expanding and aligned with the candle's own direction. Default 0.58.",
    )

    forecast3_cluster_compressed_threshold = fields.Float(
        string="Cluster Compressed Threshold",
        config_parameter="dankbit.forecast3_cluster_compressed_threshold",
        default=0.18,
        digits=(16, 4),
        help="Dispersion (in band-widths) below which the Option Cluster is considered compressed. Default 0.18.",
    )

    forecast3_cluster_compression_body_damping = fields.Float(
        string="Cluster Compression Body Damping",
        config_parameter="dankbit.forecast3_cluster_compression_body_damping",
        default=0.15,
        digits=(16, 4),
        help="Body-confidence damping applied while the Option Cluster is compressed. Default 0.15.",
    )

    forecast3_cluster_compression_wick_compression = fields.Float(
        string="Cluster Compression Wick Compression",
        config_parameter="dankbit.forecast3_cluster_compression_wick_compression",
        default=0.3,
        digits=(16, 4),
        help="Wick compression applied while the Option Cluster is compressed. Default 0.3.",
    )

    forecast3_cluster_expansion_threshold = fields.Float(
        string="Cluster Expansion Threshold",
        config_parameter="dankbit.forecast3_cluster_expansion_threshold",
        default=0.025,
        digits=(16, 4),
        help="Dispersion-change (in band-widths) needed for the Option Cluster to register as expanding. Default 0.025.",
    )

    forecast3_liquidity_aligned_wick_compression = fields.Float(
        string="Liquidity Aligned Wick Compression",
        config_parameter="dankbit.forecast3_liquidity_aligned_wick_compression",
        default=0.25,
        digits=(16, 4),
        help="Wick compression when the current candle's direction aligns with the dominant Smart Synthetic Liquidity side. Default 0.25.",
    )

    forecast3_liquidity_body_confidence_floor = fields.Float(
        string="Liquidity Body Confidence Floor",
        config_parameter="dankbit.forecast3_liquidity_body_confidence_floor",
        default=0.6,
        digits=(16, 4),
        help="Body-confidence floor when liquidity dominance/sweep-rejection is active. Default 0.6.",
    )

    forecast3_liquidity_opposite_wick_compression = fields.Float(
        string="Liquidity Opposite Wick Compression",
        config_parameter="dankbit.forecast3_liquidity_opposite_wick_compression",
        default=0.35,
        digits=(16, 4),
        help="Wick compression on the side opposite the dominant liquidity side. Default 0.35.",
    )

    forecast3_liquidity_sweep_wick_compression = fields.Float(
        string="Liquidity Sweep Wick Compression",
        config_parameter="dankbit.forecast3_liquidity_sweep_wick_compression",
        default=0.7,
        digits=(16, 4),
        help="Wick compression right after a liquidity level is swept and rejected. Default 0.7.",
    )

    forecast3_momentum_body_confidence_floor = fields.Float(
        string="Momentum Body Confidence Floor",
        config_parameter="dankbit.forecast3_momentum_body_confidence_floor",
        default=0.62,
        digits=(16, 4),
        help="Body-confidence floor while the Momentum/Liquidity-Sweep Override is active. Default 0.62.",
    )

    forecast3_momentum_wick_compression = fields.Float(
        string="Momentum Wick Compression",
        config_parameter="dankbit.forecast3_momentum_wick_compression",
        default=0.75,
        digits=(16, 4),
        help="Wick compression while the Momentum/Liquidity-Sweep Override is active. Default 0.75.",
    )

    forecast3_near_gamma_body_damping = fields.Float(
        string="Near Gamma Body Damping",
        config_parameter="dankbit.forecast3_near_gamma_body_damping",
        default=0.4,
        digits=(16, 4),
        help="Body-confidence damping near the Gamma Neutral/Hysteresis Zone. Default 0.4.",
    )

    forecast3_near_gamma_wick_expansion = fields.Float(
        string="Near Gamma Wick Expansion",
        config_parameter="dankbit.forecast3_near_gamma_wick_expansion",
        default=0.25,
        digits=(16, 4),
        help="Wick expansion near the Gamma Neutral/Hysteresis Zone. Default 0.25.",
    )

    forecast3_high_vol_pull_factor = fields.Float(
        string="High Vol Pull Factor",
        config_parameter="dankbit.forecast3_high_vol_pull_factor",
        default=1.05,
        digits=(16, 4),
        help="Pull-factor multiplier during the London/Overlap/NY high-volume regime. Default 1.05.",
    )

    forecast3_low_vol_pull_factor = fields.Float(
        string="Low Vol Pull Factor",
        config_parameter="dankbit.forecast3_low_vol_pull_factor",
        default=0.75,
        digits=(16, 4),
        help="Pull-factor multiplier during the Asia/Post-NY low-volume regime. Default 0.75.",
    )

    forecast3_weekday_pull_factor = fields.Float(
        string="Weekday Pull Factor",
        config_parameter="dankbit.forecast3_weekday_pull_factor",
        default=1.0,
        digits=(16, 4),
        help="Pull-factor multiplier outside the low/high-volume regimes. Default 1.0.",
    )

    forecast3_high_vol_shock_factor = fields.Float(
        string="High Vol Shock Factor",
        config_parameter="dankbit.forecast3_high_vol_shock_factor",
        default=1.1,
        digits=(16, 4),
        help="Shock-strength multiplier during the London/Overlap/NY high-volume regime. Default 1.1.",
    )

    forecast3_low_vol_shock_factor = fields.Float(
        string="Low Vol Shock Factor",
        config_parameter="dankbit.forecast3_low_vol_shock_factor",
        default=0.7,
        digits=(16, 4),
        help="Shock-strength multiplier during the Asia/Post-NY low-volume regime. Default 0.7.",
    )

    forecast3_weekday_shock_factor = fields.Float(
        string="Weekday Shock Factor",
        config_parameter="dankbit.forecast3_weekday_shock_factor",
        default=1.0,
        digits=(16, 4),
        help="Shock-strength multiplier outside the low/high-volume regimes. Default 1.0.",
    )

    forecast3_weekend_atr_factor = fields.Float(
        string="Weekend Atr Factor",
        config_parameter="dankbit.forecast3_weekend_atr_factor",
        default=0.75,
        digits=(16, 4),
        help="ATR (wick-size) multiplier on a UTC Saturday/Sunday. Default 0.75.",
    )

    forecast3_weekend_body_factor = fields.Float(
        string="Weekend Body Factor",
        config_parameter="dankbit.forecast3_weekend_body_factor",
        default=0.65,
        digits=(16, 4),
        help="Body-impulse multiplier on a UTC Saturday/Sunday. Default 0.65.",
    )

    forecast3_weekend_shock_factor = fields.Float(
        string="Weekend Shock Factor",
        config_parameter="dankbit.forecast3_weekend_shock_factor",
        default=0.75,
        digits=(16, 4),
        help="Shock-strength multiplier on a UTC Saturday/Sunday. Default 0.75.",
    )

    forecast3_bucket_hours_fallback = fields.Float(
        string="Bucket Hours Fallback",
        config_parameter="dankbit.forecast3_bucket_hours_fallback",
        default=4.0,
        digits=(16, 4),
        help="Assumed real-hours gap between snapshots when no real candles are available to anchor now to. Default 4.0.",
    )

    forecast3_session_body_asia = fields.Float(
        string="Session Body (Asia)",
        config_parameter="dankbit.forecast3_session_body_asia",
        default=0.7,
        digits=(16, 4),
        help="Body-impulse multiplier for the Asia session. Default 0.7.",
    )

    forecast3_session_body_london = fields.Float(
        string="Session Body (London)",
        config_parameter="dankbit.forecast3_session_body_london",
        default=0.9,
        digits=(16, 4),
        help="Body-impulse multiplier for the London session. Default 0.9.",
    )

    forecast3_session_body_overlap = fields.Float(
        string="Session Body (Overlap)",
        config_parameter="dankbit.forecast3_session_body_overlap",
        default=1.05,
        digits=(16, 4),
        help="Body-impulse multiplier for the Overlap session. Default 1.05.",
    )

    forecast3_session_body_ny = fields.Float(
        string="Session Body (NY)",
        config_parameter="dankbit.forecast3_session_body_ny",
        default=0.95,
        digits=(16, 4),
        help="Body-impulse multiplier for the NY session. Default 0.95.",
    )

    forecast3_session_body_postny = fields.Float(
        string="Session Body (PostNY)",
        config_parameter="dankbit.forecast3_session_body_postny",
        default=0.7,
        digits=(16, 4),
        help="Body-impulse multiplier for the PostNY session. Default 0.7.",
    )

    forecast3_session_atr_asia = fields.Float(
        string="Session Atr (Asia)",
        config_parameter="dankbit.forecast3_session_atr_asia",
        default=0.75,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the Asia session. Default 0.75.",
    )

    forecast3_session_atr_london = fields.Float(
        string="Session Atr (London)",
        config_parameter="dankbit.forecast3_session_atr_london",
        default=0.95,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the London session. Default 0.95.",
    )

    forecast3_session_atr_overlap = fields.Float(
        string="Session Atr (Overlap)",
        config_parameter="dankbit.forecast3_session_atr_overlap",
        default=1.1,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the Overlap session. Default 1.1.",
    )

    forecast3_session_atr_ny = fields.Float(
        string="Session Atr (NY)",
        config_parameter="dankbit.forecast3_session_atr_ny",
        default=1.0,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the NY session. Default 1.0.",
    )

    forecast3_session_atr_postny = fields.Float(
        string="Session Atr (PostNY)",
        config_parameter="dankbit.forecast3_session_atr_postny",
        default=0.75,
        digits=(16, 4),
        help="ATR (wick-size) multiplier for the PostNY session. Default 0.75.",
    )

    forecast3_session_shock_asia = fields.Float(
        string="Session Shock (Asia)",
        config_parameter="dankbit.forecast3_session_shock_asia",
        default=0.65,
        digits=(16, 4),
        help="Shock-strength multiplier for the Asia session. Default 0.65.",
    )

    forecast3_session_shock_london = fields.Float(
        string="Session Shock (London)",
        config_parameter="dankbit.forecast3_session_shock_london",
        default=0.95,
        digits=(16, 4),
        help="Shock-strength multiplier for the London session. Default 0.95.",
    )

    forecast3_session_shock_overlap = fields.Float(
        string="Session Shock (Overlap)",
        config_parameter="dankbit.forecast3_session_shock_overlap",
        default=1.1,
        digits=(16, 4),
        help="Shock-strength multiplier for the Overlap session. Default 1.1.",
    )

    forecast3_session_shock_ny = fields.Float(
        string="Session Shock (NY)",
        config_parameter="dankbit.forecast3_session_shock_ny",
        default=1.0,
        digits=(16, 4),
        help="Shock-strength multiplier for the NY session. Default 1.0.",
    )

    forecast3_session_shock_postny = fields.Float(
        string="Session Shock (PostNY)",
        config_parameter="dankbit.forecast3_session_shock_postny",
        default=0.65,
        digits=(16, 4),
        help="Shock-strength multiplier for the PostNY session. Default 0.65.",
    )

    forecast3_session_firstmove_asia = fields.Float(
        string="Session Firstmove (Asia)",
        config_parameter="dankbit.forecast3_session_firstmove_asia",
        default=0.35,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the Asia session. Default 0.35.",
    )

    forecast3_session_firstmove_london = fields.Float(
        string="Session Firstmove (London)",
        config_parameter="dankbit.forecast3_session_firstmove_london",
        default=0.55,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the London session. Default 0.55.",
    )

    forecast3_session_firstmove_overlap = fields.Float(
        string="Session Firstmove (Overlap)",
        config_parameter="dankbit.forecast3_session_firstmove_overlap",
        default=0.75,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the Overlap session. Default 0.75.",
    )

    forecast3_session_firstmove_ny = fields.Float(
        string="Session Firstmove (NY)",
        config_parameter="dankbit.forecast3_session_firstmove_ny",
        default=0.6,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the NY session. Default 0.6.",
    )

    forecast3_session_firstmove_postny = fields.Float(
        string="Session Firstmove (PostNY)",
        config_parameter="dankbit.forecast3_session_firstmove_postny",
        default=0.35,
        digits=(16, 4),
        help="Max first-candle move, in ATR units, for the PostNY session. Default 0.35.",
    )

    forecast3_hours_ahead = fields.Integer(
        string="Hours Ahead",
        config_parameter="dankbit.forecast3_hours_ahead",
        default=72,
        help="How many hours out the Thales Forecast path runs (candle count = this / Step Hours). Default 72.",
    )

    forecast3_step_hours = fields.Integer(
        string="Step Hours",
        config_parameter="dankbit.forecast3_step_hours",
        default=4,
        help="Hours per forecast candle. Default 4.",
    )

    forecast3_start_offset_hours = fields.Integer(
        string="Start Offset Hours",
        config_parameter="dankbit.forecast3_start_offset_hours",
        default=4,
        help="Hours from now to the first forecast candle. Default 4.",
    )

    # Thales Forecast candle colors — rendering-only (the TradingView
    # chart's forecast3Series), not part of simulate_forecast3()'s own
    # cfg dict, so these are read by _build_tv_chart_context() (main.py)
    # instead of _forecast3_cfg(). Light red/green so the forecast
    # candles read as directional at a glance while staying visually
    # distinct from the real candle series' own teal/red (#26a69a/
    # #ef5350) — plain hex string fields, same as weekly_expiry/
    # monthly_expiry above; no color-picker widget, just a hex value
    # typed/pasted in.
    forecast3_up_color = fields.Char(
        string="Forecast Up Color",
        config_parameter="dankbit.forecast3_up_color",
        default="#a5d6a7",
        help="Body color for bullish (close >= open) Thales Forecast candles. Hex color, default #a5d6a7 (light green).",
    )

    forecast3_down_color = fields.Char(
        string="Forecast Down Color",
        config_parameter="dankbit.forecast3_down_color",
        default="#ef9a9a",
        help="Body color for bearish Thales Forecast candles. Hex color, default #ef9a9a (light red).",
    )

    forecast3_wick_up_color = fields.Char(
        string="Forecast Wick Up Color",
        config_parameter="dankbit.forecast3_wick_up_color",
        default="#66bb6a",
        help="Wick color for bullish Thales Forecast candles. Hex color, default #66bb6a (green).",
    )

    forecast3_wick_down_color = fields.Char(
        string="Forecast Wick Down Color",
        config_parameter="dankbit.forecast3_wick_down_color",
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
        )
        return res

    def set_values(self):
        super().set_values()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("dankbit.show_daily_lines", str(self.show_daily_lines))
        icp.set_param("dankbit.show_weekly_lines", str(self.show_weekly_lines))
        icp.set_param("dankbit.show_monthly_lines", str(self.show_monthly_lines))


