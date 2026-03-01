from dataclasses import dataclass, field


@dataclass
class EngineConfig:
    """
    ═══════════════════════════════════════════════════════════════════
    🚨 Auto-Tuner 수정 버전 (v3.4) - 2026-02-28
    ═══════════════════════════════════════════════════════════════════
    Auto-Tuner 범위 수정:
    - watch_limit 최소: 5 → 10
    - max_open_symbols 최소: 2 → 5
    - balanced 모드: (10-20개, 5-12개)
    - aggressive 모드: (12-20개, 6-14개)
    - conservative 모드: (8-15개, 4-10개)
    
    추가 수정사항 (v2.0 기반):
    1. 손절: 2.5 → 1.0 (60% 감소)
    2. Take Profit 활성화
    3. 진입 조건 강화
    4. Auto-Tuner: 활성화 (수정본 적용)
    
    🆕 v3.3 추가 (올바른 접근):
    5. ATR 상한 필터 추가 (블랙리스트 대신)
       - 거래 분석: POWERUSDT에서 큰 손실 발생
       - 원인: 변동성이 너무 높아서 손절 폭 거대
       - 해결: ATR 상한선으로 과도한 변동성 심볼 차단
       - 효과: 나쁜 진입만 걸러내고, 좋은 진입은 유지
       - 블랙리스트 방식의 문제점 해결 (기회 손실 방지)
    
    🆕 v3.4 추가 (청산 최적화):
    6. Take Profit 레벨 조정 (0.8→0.6, 1.2→1.0)
       - 거래 분석: TP 1건만 작동 (2.6%)
       - 문제: SIGNAL_DECAY가 TP 전에 발동 (73.7%)
       - 해결: TP 레벨을 더 가깝게 조정
       - 효과: TP 도달 가능성 증가, 평균 수익 +2.18% → +3.5%
    7. 보유 시간 연장 (120→180초, TIME_STOP 1200→1800초)
    8. Trailing Stop 강화 (0.001→0.0015)
    ═══════════════════════════════════════════════════════════════════
    """
    
    top_n: int = 20
    
    # ═══════════════════════════════════════════════════════════
    # 💰 포지션 크기 (마진 비율)
    # ═══════════════════════════════════════════════════════════
    # 자산의 몇 %를 마진으로 사용할지 설정
    # 예시 (자산 1 USDT 기준):
    # - 5%: 0.05 USDT 마진 → 0.55 USDT 포지션 (11배)
    # - 15%: 0.15 USDT 마진 → 1.65 USDT 포지션 (11배)
    # 
    # 권장 설정:
    # - 자산 < 5 USDT: 15-20% (수익 확보 위해)
    # - 자산 5-20 USDT: 10-15%
    # - 자산 > 20 USDT: 5-10%
    position_pct: float = 0.06  # [PATCH-13] 12%→6% — 리스크 축소 + Kelly 드로다운 가드와 연동
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 수정 #1: 레버리지 대폭 하향 (40x → 10-15x)
    # ═══════════════════════════════════════════════════════════
    leverage_min: int = 1       # 변경: 10 → 1 (저변동성 종목은 1x로 안전하게)
    leverage_max: int = 10      # 변경: 15 → 10 (과도한 레버리지 방지)
    
    # ═══════════════════════════════════════════════════════════
    # 🚨 긴급: 진입 조건 강화 (품질 향상)
    # ═══════════════════════════════════════════════════════════
    volatility_min: float = 0.003           # 0.002 → 0.003 (50% 증가)
    momentum_min_long: float = 0.005        # 0.003 → 0.005 (67% 증가)
    momentum_min_short: float = -0.0055     # [PATCH-9] -0.008 → -0.0055 (숏 비대칭 완화, 방향 분산)
    momentum_min: float = 0.0
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 수정 #2: Auto-Tune 설정
    # ═══════════════════════════════════════════════════════════
    auto_tune_enabled: bool = False  # [PATCH-16] 비활성화 — 16샘플 미니배치로 노이즈 학습
    auto_tune_mode: str = "balanced"  # "conservative" | "balanced" | "aggressive"
    
    total_risk_budget: float = 0.10
    watch_limit: int = 10
    max_open_symbols: int = 10
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 수정 #3: 손절 여유 확대
    # ═══════════════════════════════════════════════════════════
    max_loss_per_position: float = 1.8  # [PATCH-11] 2.5→1.8: 손실 한도 추가 축소
    
    diversify_watchlist: bool = False
    
    # ═══════════════════════════════════════════════════════════
    # 🆕 v3.3: ATR 상한 필터 추가 (변동성 과도한 심볼 제외)
    # ═══════════════════════════════════════════════════════════
    # 거래 분석 결과: POWERUSDT에서 큰 손실 발생
    # 원인: ATR이 높아서 손절 폭이 너무 커짐
    # 해결: ATR 상한선 설정으로 과도한 변동성 심볼 제외
    #
    # ATR 상한 계산 예시:
    # - volatility_min = 0.003 (0.3%)
    # - atr_max_mult = 3.0
    # - ATR 상한 = 0.003 × 3.0 = 0.009 (0.9%)
    #
    # 효과:
    # - ATR > 0.9%인 심볼 진입 차단
    # - 과도한 변동성으로 인한 큰 손절 방지
    # - 정상 변동성 심볼은 계속 거래
    atr_max_mult: float = 3.0  # volatility_min의 3배까지 허용
    
    mark_gap_threshold: float = 0.003
    spike_guard_enabled: bool = True
    spike_guard_return_pct: float = 0.07     # [PATCH-9] 0.05→0.07: 알트코인 5% 빈번 → 완화
    spike_guard_window: int = 8
    spike_guard_check_interval_s: int = 2
    global_spike_cooldown_min: int = 5        # [PATCH-9] 10→5: 글로벌 쿨다운 축소
    spike_guard_per_symbol_cooldown: bool = True  # [PATCH-9] 심볼별 쿨다운 (글로벌 대신)
    spark_reentry_candles: int = 3
    session_loss_limit_pct: float = 3.0
    session_loss_window_minutes: int = 1440
    kill_switch_cooldown_min: int = 30
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 수정 #4: Auto-Tune 롤백 조건 강화
    # ═══════════════════════════════════════════════════════════
    auto_tune_rollback_loss_usdt: float = 10.0   # 변경: 50.0 → 10.0
    auto_tune_rollback_failures: int = 3         # 변경: 5 → 3
    auto_tune_grace_minutes: int = 10
    auto_tune_cooldown_min: int = 10
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 수정 #5: 보유 시간 연장 (TP 도달 시간 확보)
    # ═══════════════════════════════════════════════════════════
    # 거래 분석: SIGNAL_DECAY가 TP 전에 발동
    # → 보유 시간 연장으로 TP 도달 시간 확보
    min_hold_seconds: int = 180   # 120 → 180초 (3분)
    # [PATCH-2] 적응형 시간 손절 파라미터
    time_stop_adaptive: bool = True           # ATR 기반 적응형 시간 손절 활성화
    time_stop_atr_ref: float = 0.005          # 기준 ATR (0.5%) — 이 값 기준으로 비율 산출
    time_stop_min_seconds: int = 600          # 최소 시간 손절 (10분, 고변동성)
    time_stop_max_seconds: int = 7200         # [PATCH-9] 3600→7200: trend 레짐 시 2시간까지 허용 (큰 수익 구간 보호)
    
    auto_tune_include_order_failures: bool = False
    auto_boost_position_pct: bool = False
    maker_fee_pct: float = 0.0002
    taker_fee_pct: float = 0.0005
    
    # ═══════════════════════════════════════════════════════════
    # 🚨 긴급: Take Profit 활성화 & 최적화 (v3.4)
    # ═══════════════════════════════════════════════════════════
    # 거래 분석 결과:
    # - SIGNAL_DECAY: 28건 (73.7%), 평균 +2.18% (작음!)
    # - TP: 1건만 작동 (2.6%) ← 문제!
    # - 소액 수익 (0~2%): 16건 ← TP 전에 청산됨
    #
    # 개선:
    # - TP 레벨을 더 가깝게 조정 (0.8→0.6, 1.2→1.0)
    # - TP 도달 가능성 증가
    # - 평균 수익 +2.18% → +3.5% 예상
    enable_take_profit: bool = True         # False → True
    tp_r_multiple_1: float = 1.5            # [PATCH-13] 0.6→1.5 (손절의 150%에서 50% 익절, 손익비 최소 1.5:1)
    tp_r_multiple_2: float = 2.5            # [PATCH-13] 1.0→2.5 (손절의 250%에서 전량 익절, 손익비 2.5:1)
    partial_tp_ratio: float = 0.5
    break_even_after_partial: bool = True
    tp_min_roi_pct: float = 0.003           # 최소 ROI 0.3%
    tp_cooldown_s: int = 30
    tp_working_type: str = "MARK_PRICE"
    
    # ═══════════════════════════════════════════════════════════
    # 🚨 긴급: 손절 대폭 타이트하게 (평균 손실 감소)
    # ═══════════════════════════════════════════════════════════
    sl_atr_mult: float = 1.0         # [PATCH-9] 0.5→1.0: 손절 현실화 (노이즈 털림 방지, ATR sizing이 리스크 보정)
    sl_atr_mult_chop: float = 0.8    # [PATCH-11] 1.0→0.8 chop 레짐 SL 타이트닝
    sl_atr_mult_trend: float = 1.0   # [PATCH-11] 1.2→1.0 trend 레짐 SL 타이트닝
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 Trailing Stop 강화 (v3.4)
    # ═══════════════════════════════════════════════════════════
    # 수익 보호 강화: 최소 단계 증가
    trail_min_step_pct: float = 0.0015  # 0.001 → 0.0015
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 수정 #7: 타임스탑 연장 (v3.4)
    # ═══════════════════════════════════════════════════════════
    # 거래 분석: TIME_STOP 2건 (1승 1패)
    # → 더 연장하여 TP 도달 시간 확보
    enable_time_stop: bool = False         # [PATCH-16] 비활성화 — 이전 분석 6건 전부 손실
    time_stop_seconds: int = 1800

    enable_signal_decay_exit: bool = False  # [PATCH-16] 비활성화 — 횡보 구간에서 수익 포지션 조기 청산
    signal_decay_window: int = 300
    signal_decay_threshold: float = 0.25  # [PATCH-7] 0.4→0.25: 신호 감쇠 허용 범위 확대 (조기 청산 방지)
    signal_decay_min_profit: float = 3.5  # [PATCH-8] 2.0→3.5: ROI 3.5% 미만에서 signal decay 청산 차단 (조기 청산 방지)
    quality_min_score: float = 0.0
    quality_mark_gap_weight: float = 0.5
    quality_rv_weight: float = 0.3
    quality_momentum_weight: float = 0.2
    quality_mark_gap_cap: float = 0.01
    quality_rv_cap: float = 3.0
    
    # ═══════════════════════════════════════════════════════════
    # 🚨 긴급: 진입 엣지 요구사항 강화
    # ═══════════════════════════════════════════════════════════
    min_edge_over_fee_pct: float = 0.0015  # [PATCH-9] 0.005→0.0015: 엣지 기준 현실화 (B단계에서 비용실측 후 재조정)
    
    min_margin_usdt: float = 1.0

    # --- Execution cost / liquidity filters ---
    max_spread_bps: float = 15.0
    max_mark_gap_bps: float = 30.0

    # --- Multi-timeframe (MTF) confirmation (EMA slope) ---
    enable_mtf_ema_confirm: bool = True
    mtf_timeframes_sec: list = field(default_factory=lambda: [60, 300])
    mtf_ema_period: int = 21
    mtf_min_slope_bps: float = 2.0

    # --- 단기 EMA 방향 충돌 필터 ---
    short_ema_conflict_filter: bool = True
    chop_use_short_ema_direction: bool = True

    # Conservative fallback estimates
    tca_spread_estimate_bps: float = 10.0
    tca_slippage_estimate_bps: float = 6.0

    # --- TCA-aware guardrails ---
    tca_window_sec: int = 1800
    tca_max_slippage_bps_med: float = 8.0
    tca_max_spread_bps_med: float = 12.0

    enable_profit_exit_layer: bool = True
    enable_partial_take_profit: bool = True
    enable_atr_trailing_stop: bool = True
    enable_progress_stop: bool = False  # [PATCH-16] 비활성화 — 30분 타이머가 수익 포지션 조기 청산

    partial_tp_levels: list = field(
        default_factory=lambda: [
            {"r": 1.0, "close_frac": 0.30},   # [PATCH-13] TP1: 1.0R에서 30% (1:1 손익비 확보)
            {"r": 1.5, "close_frac": 0.30},   # [PATCH-13] TP2: 1.5R에서 30% (손익비 1.5:1)
            {"r": 2.5, "close_frac": 0.40},   # [PATCH-13] TP3: 2.5R에서 40% (나머지 트레일링)
        ]
    )
    maker_entry_use_taker: bool = True   # [PATCH-9] 진입은 taker(타이밍), 청산은 maker(비용) 분리
    trail_atr_period: int = 22
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 수정 #9: 트레일링 스탑 보수적 조정
    # ═══════════════════════════════════════════════════════════
    trail_atr_mult: float = 1.7   # [PATCH-9] 2.0→1.7: 수익 보호 강화 (리스크 대비 촘촘하게)
    trail_activate_pnl_pct: float = 0.008  # [PATCH-10] 0.01→0.008: ROI 0.8%에서 트레일링 활성화 (trend 시)
    
    trail_use_highest_since_entry: bool = True
    trail_recalc_interval_sec: int = 5

    progress_stop_lookback_sec: int = 600
    progress_stop_no_new_high_sec: int = 300
    progress_stop_drawdown_from_mfe: float = 0.07
    progress_stop_min_pnl_pct: float = 0.05
    progress_stop_action: str = "partial_or_full"

    # --- ATR risk-based position sizing ---
    atr_risk_sizing_enabled: bool = True
    entry_risk_pct: float = 0.01

    # --- RSI filter ---
    rsi_filter_enabled: bool = False
    rsi_period: int = 14
    rsi_overbought: float = 75.0
    rsi_oversold: float = 25.0

    # --- Composite signal scoring ---
    composite_signal_enabled: bool = True
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 수정 #10: 복합 시그널 최소 스코어 상향 (진입 조건 강화)
    # ═══════════════════════════════════════════════════════════
    composite_min_score: float = 0.80       # [PATCH-13] 0.72→0.80: 진입 품질 강화 (볼륨 플로어 제거와 연동)
    
    chop_momentum_multiplier: float = 1.2
    overheat_min_multiplier: float = 2.0
    overheat_volatility_mult: float = 1.5
    regime_noise_threshold: float = 0.012
    regime_trend_up_threshold: float = 0.8
    regime_trend_dn_threshold: float = -0.8
    composite_volume_window: int = 20
    composite_weights_momentum: float = 0.50
    composite_weights_volume: float = 0.30
    composite_weights_mtf: float = 0.20

    # --- Breakeven stop ---
    breakeven_stop_enabled: bool = True
    breakeven_buffer_pct: float = 0.001

    # --- Kelly sizing ---
    kelly_sizing_enabled: bool = True
    kelly_fraction: float = 0.25
    kelly_min_samples: int = 10

    # --- Maker-first entry ---
    # ═══════════════════════════════════════════════════════════
    # 🔧 수정 #11: Maker 주문 타임아웃 연장 (수수료 절감)
    # ═══════════════════════════════════════════════════════════
    maker_first_enabled: bool = True
    maker_first_offset_bps: float = 1.0
    maker_first_timeout_ms: int = 1000       # [PATCH-9] 3000→1000ms: 진입 지연 축소 (타이밍 알파 보존)
    # [PATCH-3] 적응형 메이커 파라미터
    maker_adaptive_timeout: bool = True       # 변동성 연동 타임아웃
    maker_timeout_min_ms: int = 1000          # 최소 1초 (고변동성)
    maker_timeout_max_ms: int = 5000          # 최대 5초 (저변동성)
    maker_offset_adaptive: bool = True        # 스프레드 연동 오프셋
    maker_offset_min_bps: float = 0.5         # 최소 오프셋
    maker_offset_max_bps: float = 3.0         # 최대 오프셋
    maker_spread_mult: float = 0.5            # 스프레드의 50%를 오프셋에 추가

    # --- Auto-tune shadow evaluation ---
    auto_tune_shadow_min_cycles: int = 5

    # --- Expert mode / safety caps ---
    expert_mode_enabled: bool = False

    # --- UI / log language ---
    ui_language: str = "ko"

    # ── API call-rate self-throttling ──
    api_calls_per_min_limit: int = 1200

    # ── Commercial deployment safety ──
    consent_verified: bool = False
    startup_grace_sec: int = 60
    entry_slippage_cap_bps: float = 12.0    # [PATCH-10] 20→12: 슬리피지 캡 강화 (체결 질 보정)
    limit_exit_offset_bps: float = 5.0   # [PATCH-11] 2.0→5.0 maker 체결률 향상
    limit_exit_max_attempts: int = 4     # [PATCH-11] 3→4 재시도 증가
    limit_exit_wait_sec: float = 4.0     # [PATCH-11] 2.0→4.0 대기시간 증가
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 수정 #12: Auto-Tune 연속 롤백 제한 강화
    # ═══════════════════════════════════════════════════════════
    max_consecutive_rollbacks: int = 3     # 변경: 5 → 3

    # --- 온라인 학습 신경망 [프리미엄 v3] ---
    neural_scorer_enabled: bool = False
    neural_license_key: str = ""
    neural_block_threshold: float = 0.25  # v3: win_prob < 이 값이면 진입 거부
    tca_spread_estimate_bps: float = 5.0  # spread_bps가 0일 때 추정 대체값

    # --- Funding rate filter ---
    funding_filter_enabled: bool = True
    funding_rate_cache_sec: int = 900
    funding_bias_threshold: float = 0.001
    funding_bias_penalty: float = 0.30
    funding_time_stop_enabled: bool = True   # [PATCH-10] 펀딩 타임 근처 time-stop 축소
    funding_time_stop_window_min: int = 30   # [PATCH-10] 펀딩 정산 전후 30분 이내
    funding_time_stop_mult: float = 0.5      # [PATCH-10] 해당 구간 time-stop을 50%로 축소

    # ── [PATCH-10] 단일 거래 손실 하드캡 ──
    max_single_trade_loss_pct: float = 1.8   # [PATCH-11] 2.2→1.8 ROI 초과 시 시스템 강제 청산

    # ── [PATCH-10] chop 레짐 RSI 소프트 스코어링 ──
    rsi_chop_soft_scoring: bool = True       # chop에서 RSI를 composite 보조 점수로 사용
    rsi_chop_bonus: float = 0.10             # RSI 과매도(LONG)/과매수(SHORT) 시 +0.10 보너스

    # ── [PATCH-11] 레짐 방향 바이어스 ──
    regime_direction_bias_enabled: bool = True
    regime_long_bonus_trend_up: float = 0.15     # trend_up에서 LONG +0.15
    regime_short_penalty_trend_up: float = -0.10 # trend_up에서 SHORT -0.10
    regime_short_bonus_trend_down: float = 0.15  # trend_down에서 SHORT +0.15
    regime_long_penalty_trend_down: float = -0.10 # trend_down에서 LONG -0.10

    # ── [PATCH-11] 동일 심볼 재진입 쿨다운 ──
    symbol_reentry_cooldown_sec: int = 120       # 같은 심볼 청산 후 120초 대기

    # ── [PATCH-11] chop 레짐 동시 포지션 제한 ──
    chop_max_open_symbols: int = 5               # [PATCH-13c] 2→5: chop에서도 분산 투자 허용

    # ── [PATCH-12] chop 레짐 진입 강화 ──
    chop_composite_min_score: float = 0.85       # chop에서 진입 임계값 상향 (일반 0.72 → 0.85)
    chop_position_pct_mult: float = 0.5          # chop에서 포지션 사이즈 50%로 축소


# ═══════════════════════════════════════════════════════════════════
# 📋 전체 변경 사항 요약
# ═══════════════════════════════════════════════════════════════════
#
# 【레버리지 & 리스크】
# 1. 레버리지: 40-41x → 10-15x (75% 감소)
# 2. 손절 ATR 배수: 1.5 → 2.5 (67% 확대)
# 3. 손실 한도: 18% → 12% (레버리지 낮췄으므로 조정)
#
# 【거래 빈도 & 수수료】
# 4. 최소 보유 시간: 20초 → 120초 (6배 증가)
# 5. 타임스탑: 10분 → 20분 (2배 증가)
# 6. Maker 타임아웃: 1.5초 → 3.0초 (2배 증가)
# 7. 진입 엣지 요구: 0.15% → 0.3% (2배)
#
# 【진입 조건】
# 8. 복합 시그널 최소 스코어: 0.75 → 0.80 (6.7% 증가)
#
# 【수익 보호】
# 9. 트레일링 스탑: ATR 1.5배 → 2.0배 (33% 확대)
# 10. 트레일링 활성화: 10% → 15% ROI (50% 증가)
#
# 【Auto-Tune 안전장치】
# 11. 롤백 임계값: $50 → $10 (80% 감소)
# 12. 롤백 실패 횟수: 5회 → 3회 (40% 감소)
# 13. 연속 롤백 제한: 5회 → 3회 (40% 감소)
#
# ═══════════════════════════════════════════════════════════════════
# 🎯 예상 효과
# ═══════════════════════════════════════════════════════════════════
#
# • 승률: 33% → 45-50% 예상 (손절 여유 확대)
# • 수수료 부담: 87% → 40-50%로 감소 (거래 빈도 감소)
# • 기댓값: -0.7 → +0.3~+0.5 예상 (승률 개선)
# • 레버리지 낮춤으로 계좌 안정성 대폭 향상
# • Auto-Tune이 레버리지 40배까지 올리는 문제 해결
# • 손실 발생 시 빠른 롤백으로 손실 최소화
#
# ═══════════════════════════════════════════════════════════════════
# ⚠️ 중요 참고사항
# ═══════════════════════════════════════════════════════════════════
#
# 1. 테스트넷에서 최소 3일 테스트 후 실전 배포
# 2. 실전 배포 시 소액($100-500)으로 1주일 검증
# 3. auto_tuner_fixed.py와 함께 사용해야 함
# 4. 주간 리뷰로 승률, 기댓값, 수수료 비중 모니터링
#
# ═══════════════════════════════════════════════════════════════════
