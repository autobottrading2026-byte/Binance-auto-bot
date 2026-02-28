from dataclasses import dataclass, field


@dataclass
class EngineConfig:
    top_n: int = 20
    position_pct: float = 0.05
    leverage_min: int = 5
    leverage_max: int = 20
    volatility_min: float = 0.002
    momentum_min_long: float = 0.003  # percent units — 기본 0.3%, AutoTune이 0.001~0.006 범위로 조정
    momentum_min_short: float = -0.005  # percent units — 반드시 음수 유지
    momentum_min: float = 0.0  # legacy compatibility for code paths that still read momentum_min
    auto_tune_enabled: bool = True
    auto_tune_mode: str = "balanced"
    total_risk_budget: float = 0.10
    watch_limit: int = 10
    max_open_symbols: int = 10
    max_loss_per_position: float = 18.0  # interpreted as max allowed PnL drawdown (%)
    diversify_watchlist: bool = False
    mark_gap_threshold: float = 0.003
    spike_guard_enabled: bool = True
    spike_guard_return_pct: float = 0.05  # 8초 내 포지션 역방향 5% 급변 시 즉시 시장가 청산 (0이면 비활성)
    spike_guard_window: int = 8              # 급변동 감지 시간 창 (초)
    spike_guard_check_interval_s: int = 2
    global_spike_cooldown_min: int = 5       # 스파이크 발동 후 재진입 차단 시간(분)
    spark_reentry_candles: int = 3
    session_loss_limit_pct: float = 3.0   # 세션 손실 한도 (5%→3%: 더 빠른 보호)
    session_loss_window_minutes: int = 1440
    kill_switch_cooldown_min: int = 30   # Kill switch 쿨다운 30분 (60→30: 짧은 손실 후 조기 재진입 허용)
    auto_tune_rollback_loss_usdt: float = 50.0
    auto_tune_rollback_failures: int = 5
    auto_tune_grace_minutes: int = 10
    auto_tune_cooldown_min: int = 10
    min_hold_seconds: int = 20    # 최소 보유 시간 (45→20초: 고레버 빠른 손절 허용)
    auto_tune_include_order_failures: bool = False
    auto_boost_position_pct: bool = False
    maker_fee_pct: float = 0.0002
    taker_fee_pct: float = 0.0005
    enable_take_profit: bool = False
    tp_r_multiple_1: float = 1.0
    tp_r_multiple_2: float = 2.0
    partial_tp_ratio: float = 0.5
    break_even_after_partial: bool = True
    tp_min_roi_pct: float = 0.0025
    tp_cooldown_s: int = 30
    tp_working_type: str = "MARK_PRICE"
    sl_atr_mult: float = 1.5        # 스탑로스 ATR 배수 (구 stop_loss_atr_mult 통합)
    trail_min_step_pct: float = 0.001
    time_stop_seconds: int = 600    # 10분 타임스탑 (고레버 환경: 30분→10분)
    signal_decay_window: int = 300
    signal_decay_threshold: float = 0.4  # 신호 강도 40% 이하로 감소 시 청산 (0.3→0.4: 더 빠른 반응)
    signal_decay_min_profit: float = 0.0   # tick_engine에서 수수료 110%로 동적 결정
    quality_min_score: float = 0.0
    quality_mark_gap_weight: float = 0.5
    quality_rv_weight: float = 0.3
    quality_momentum_weight: float = 0.2
    quality_mark_gap_cap: float = 0.01
    quality_rv_cap: float = 3.0
    min_edge_over_fee_pct: float = 0.0015
    min_margin_usdt: float = 1.0  # 포지션 진입 최소 증거금 (USDT). 이 미만이면 진입 차단

    # --- Execution cost / liquidity filters ---
    max_spread_bps: float = 15.0  # skip entry if spread wider than this (bps)
    max_mark_gap_bps: float = 30.0  # skip entry if mark vs mid gap exceeds this (bps)

    # --- Multi-timeframe (MTF) confirmation (EMA slope) ---
    enable_mtf_ema_confirm: bool = True
    mtf_timeframes_sec: list = field(default_factory=lambda: [60, 300])  # 1m, 5m buckets from tick history
    mtf_ema_period: int = 21
    mtf_min_slope_bps: float = 2.0  # require EMA slope >= this (bps) in direction of trade

    # --- 단기 EMA 방향 충돌 필터 (세션 55 추가) ---
    short_ema_conflict_filter: bool = True   # 1m·5m EMA 모두 24h 방향과 반대이면 차단
    chop_use_short_ema_direction: bool = True  # CHOP 레짐에서 단기EMA 방향 우선

    # Conservative fallback estimates used in edge-vs-cost gate when no recent trade metrics exist.
    tca_spread_estimate_bps: float = 10.0
    tca_slippage_estimate_bps: float = 6.0

    # --- TCA-aware guardrails ---
    tca_window_sec: int = 1800
    tca_max_slippage_bps_med: float = 8.0
    tca_max_spread_bps_med: float = 12.0

    enable_profit_exit_layer: bool = True
    enable_partial_take_profit: bool = True
    enable_atr_trailing_stop: bool = True
    enable_progress_stop: bool = True

    partial_tp_levels: list = field(
        default_factory=lambda: [
            {"r": 0.7, "close_frac": 0.35},   # R0.7 → 35% 청산 (빠른 수익 실현)
            {"r": 1.2, "close_frac": 0.35},   # R1.2 → 추가 35% 청산
            {"r": 2.0, "close_frac": 1.00},   # R2.0 → 잔량 전부 청산
        ]
    )
    trail_atr_period: int = 22
    trail_atr_mult: float = 1.5   # 트레일링 스탑 ATR 배수 (3.0→1.5: 수익 되돌림 최소화)
    trail_activate_pnl_pct: float = 0.10   # 트레일링 활성화 ROI (20%→10%: 더 빨리 수익 보호)
    trail_use_highest_since_entry: bool = True
    trail_recalc_interval_sec: int = 5

    progress_stop_lookback_sec: int = 600   # 진행 중단 감지 lookback (30분→10분)
    progress_stop_no_new_high_sec: int = 300  # 5분 신고점 없으면 청산 시작 (30분→5분)
    progress_stop_drawdown_from_mfe: float = 0.07  # MFE 대비 되돌림 허용 (15%→7%: 고레버 환경)
    progress_stop_min_pnl_pct: float = 0.05  # 이 ROI 이상에서만 progress stop 작동 (25%→5%)
    progress_stop_action: str = "partial_or_full"

    # --- ATR risk-based position sizing ---
    atr_risk_sizing_enabled: bool = True    # 포지션당 리스크 예산으로 수량 캡
    entry_risk_pct: float = 0.01            # 포지션당 계좌의 최대 1% 손실 허용

    # --- RSI filter ---
    rsi_filter_enabled: bool = False  # True 시 volatility×overheat_volatility_mult 초과 모멘텀 차단
    rsi_period: int = 14
    rsi_overbought: float = 75.0            # LONG 진입 차단 임계
    rsi_oversold: float = 25.0             # SHORT 진입 차단 임계

    # --- Composite signal scoring ---
    composite_signal_enabled: bool = True
    composite_min_score: float = 0.75       # 복합 스코어 최소값 (NaN 패널티 제거 후 0.9→0.75)
    chop_momentum_multiplier: float = 1.2   # CHOP 레짐 모멘텀 강화 배수 (1.8→1.2: 알트코인 진입 허용)
    overheat_min_multiplier: float = 2.0    # (레거시, rsi_filter_enabled=False면 미사용)
    overheat_volatility_mult: float = 1.5   # 과열 상한 = volatility × 이 값 (rsi_filter 활성 시)
    regime_noise_threshold: float = 0.012   # 레짐 판정 noise 상한 (0.005→0.012 완화)
    regime_trend_up_threshold: float = 0.8   # trend_up 판정 trend_score 하한 (1.2→0.8 완화)
    regime_trend_dn_threshold: float = -0.8  # trend_down 판정 trend_score 상한 (-1.2→-0.8 완화)
    composite_volume_window: int = 20       # (미사용) _volume_surge_score로 대체됨
    composite_weights_momentum: float = 0.50
    composite_weights_volume: float = 0.30
    composite_weights_mtf: float = 0.20

    # --- Breakeven stop ---
    breakeven_stop_enabled: bool = True
    breakeven_buffer_pct: float = 0.001     # 진입가 위/아래 0.1% 버퍼

    # --- Kelly sizing ---
    kelly_sizing_enabled: bool = True
    kelly_fraction: float = 0.25            # Quarter-Kelly (안전)
    kelly_min_samples: int = 10             # 최소 표본 수

    # --- Maker-first entry ---
    maker_first_enabled: bool = True         # GTX Post-Only Limit 먼저 시도, 실패시 Market fallback
    maker_first_offset_bps: float = 1.0      # 진입 지정가 mid 대비 offset (bps)
    maker_first_timeout_ms: int = 1500       # post-only 체결 대기 시간 (ms), 0=비활성

    # --- Auto-tune shadow evaluation ---
    auto_tune_shadow_min_cycles: int = 5   # E: minimum shadow cycles before promotion (≥5 recommended)

    # --- Expert mode / safety caps ---
    expert_mode_enabled: bool = False   # True = Aggressive + leverage up to 150x

    # --- UI / log language ---
    ui_language: str = "ko"   # "ko" or "en" — controls _notify() message language


    # ── C1: API call-rate self-throttling ─────────────────────────────
    api_calls_per_min_limit: int = 1200    # Binance default 1200 weight/min; reduce if needed

    # ── Commercial deployment safety ──────────────────────────────────
    # A3: engine-level consent gate (set to True by GUI only after user acks)
    consent_verified: bool = False

    # C2: startup grace period — suppress forced-close triggers for N seconds after boot
    startup_grace_sec: int = 60

    # C3: slippage cap on entry orders — cancel GTX and skip market if slippage > cap
    entry_slippage_cap_bps: float = 20.0   # 0 = disabled
    limit_exit_offset_bps: float = 2.0    # 지정가 청산 offset (bps), 0 = 미사용
    limit_exit_max_attempts: int = 3       # 지정가 청산 최대 시도 횟수 (실패시 시장가 fallback)
    limit_exit_wait_sec: float = 2.0       # 지정가 청산 회당 대기 시간(초)

    # E: auto-tune consecutive rollback → auto-disable
    max_consecutive_rollbacks: int = 5     # 0 = disabled
    # --- 온라인 학습 신경망 [프리미엄] ---
    neural_scorer_enabled: bool = False    # 프리미엄 라이선스 필요
    neural_license_key: str = ""           # gui_config.json에서 읽어옴

    # --- Funding rate filter ---
    funding_filter_enabled: bool = True
    funding_rate_cache_sec: int = 900       # 15분 캐시
    funding_bias_threshold: float = 0.001   # 0.1% 이상 펀딩 → 신호 강도 축소
    funding_bias_penalty: float = 0.30      # 강도 30% 감소
