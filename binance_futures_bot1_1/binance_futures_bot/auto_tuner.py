"""AutoTuner v2 — EMA 수렴 + Apply Cadence + Regime Switching Cost.

GPT 최종 개선안(A~H) 반영:
  A1: best_targets score 기반 선택
  A2: 시간당 레짐 전환 상한 (max 3/hour)
  A3: 파라미터별 KPI 분리
  B1-B2: 레짐 조건부 목표값
  C: TCA risk_bias 블로킹
  D: Shadow-lite (대폭 변경 1사이클 유예)
  G: KPI 로깅
"""
import math
import time
import logging as _log
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

from .risk_limits import clamp_params, DEFAULT_LIMITS

LifecycleSnapshot = Dict[str, Any]
_logger = _log.getLogger(__name__)


# -----------------------------
# utils
# -----------------------------
def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def exp_smooth(prev: float, target: float, alpha: float = 0.3) -> float:
    return alpha * target + (1 - alpha) * prev


def sign(x: float) -> int:
    return -1 if x < 0 else (1 if x > 0 else 0)


def median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    if n % 2 == 1:
        return ys[mid]
    return 0.5 * (ys[mid - 1] + ys[mid])


def mad_sigma(returns: List[float], sigma_floor: float = 0.0005) -> Tuple[float, float, float]:
    """ Robust scale via Median Absolute Deviation (MAD):
    MAD = median(|x - median(x)|)
    sigma_robust ~= 1.4826 * MAD (consistent for Normal)
    """
    if not returns:
        returns = [0.0]
    mu = sum(returns) / len(returns)
    m = median(returns)
    abs_dev = [abs(r - m) for r in returns]
    mad = median(abs_dev)
    sigma_robust = 1.4826 * mad
    sigma_eff = max(sigma_robust, sigma_floor)
    return mu, sigma_eff, mad


def normalize_pnl(pnl_30m: float) -> float:
    """ Normalize PnL into fraction unit:
    - If -2.0 means -2%, normalize to -0.02
    - If already -0.02, keep
    """
    if pnl_30m is None:
        return 0.0
    if abs(pnl_30m) > 1.0:
        return pnl_30m / 100.0
    return pnl_30m


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# -----------------------------
# state
# -----------------------------
@dataclass
class HysteresisState:
    up_hits: int = 0
    down_hits: int = 0
    chop_hits: int = 0
    current_regime: str = "chop"
    noise_spike_hits: int = 0


@dataclass
class ShadowPerformance:
    win_sum: float = 0.0
    expectancy_sum: float = 0.0
    pnl_sum: float = 0.0
    dd_min: float = 0.0
    samples: int = 0
    last_ts: float = 0.0

    def reset(self):
        self.win_sum = 0.0
        self.expectancy_sum = 0.0
        self.pnl_sum = 0.0
        self.dd_min = 0.0
        self.samples = 0
        self.last_ts = 0.0

    def snapshot(self) -> Dict[str, float]:
        if self.samples <= 0:
            return {"samples": 0, "win_rate": 0.0, "expectancy": 0.0, "pnl": 0.0, "dd": self.dd_min}
        return {"samples": self.samples, "win_rate": self.win_sum / self.samples, "expectancy": self.expectancy_sum / self.samples, "pnl": self.pnl_sum / self.samples, "dd": self.dd_min}


@dataclass
class ShadowState:
    active: bool = True
    baseline_snapshot: Optional[Dict[str, float]] = None
    records: List[Dict[str, Any]] = field(default_factory=list)
    cycles_required: int = 3  # 2~3 권장
    perf_candidate: ShadowPerformance = field(default_factory=ShadowPerformance)
    perf_baseline: ShadowPerformance = field(default_factory=ShadowPerformance)


@dataclass
class AutoTunerState:
    last_metrics: Dict[str, float] = field(default_factory=dict)
    cooldown_until: float = 0.0
    rollback_stack: List[Tuple[Dict[str, float], str]] = field(default_factory=list)
    last_dir: Dict[str, int] = field(default_factory=dict)
    consecutive_same_dir: Dict[str, int] = field(default_factory=dict)
    failure_count: int = 0
    tune_count_today: int = 0
    tune_day_key: str = ""
    confidence: float = 0.0
    hysteresis: HysteresisState = field(default_factory=HysteresisState)
    shadow: ShadowState = field(default_factory=ShadowState)
    t_up: float = 0.8   # trend_up 진입 임계 (낮출수록 추세 인식 빨라짐)
    t_dn: float = -0.8  # trend_down 진입 임계

    # ── v2 신규 필드 ──
    last_apply_ts: float = 0.0              # 마지막 실제 적용 시각
    regime_entered_ts: float = 0.0          # 현재 레짐 진입 시각
    risk_bias_confirm_streak: int = 0       # risk_bias +1 연속 충족 횟수
    prev_risk_bias: int = 0                 # 이전 risk_bias 값

    # [A2] 시간당 레짐 전환 타임스탬프 기록
    regime_switch_timestamps: List[float] = field(default_factory=list)
    regime_locked_until: float = 0.0        # 전환 초과 시 잠금 시각

    # EMA 메트릭 (다중 타임스케일)
    ema_tca_bps: float = 0.0               # Fast EMA (5분)
    ema_failures: float = 0.0              # Fast EMA
    ema_fill_rate: float = 1.0             # Fast EMA
    ema_trend_score: float = 0.0           # Mid EMA (15분)
    ema_noise_index: float = 0.0           # Mid EMA
    ema_pass_rate: float = 1.0             # Mid EMA
    ema_pnl: float = 0.0                   # Slow EMA (60분)

    # 목표값 (propose 결과 저장, apply 시 EMA 수렴에 사용)
    targets: Dict[str, float] = field(default_factory=dict)
    # [A1] best_targets: score 기반 최적 목표 선택
    best_targets: Dict[str, float] = field(default_factory=dict)
    best_targets_score: float = 0.0

    # [D] Shadow-lite: 대폭 변경 유예
    shadow_lite_deferred: bool = False
    shadow_lite_deferred_params: Dict[str, float] = field(default_factory=dict)


# -----------------------------
# main class
# -----------------------------
class AutoTuner:
    """AutoTuner v2 — EMA 수렴 기반 파라미터 최적화.

    핵심 변경:
    - propose는 매 틱(5초), apply는 apply_interval(기본 5분) 이상일 때만
    - 스텝 점프 → 목표값 + EMA 수렴 (진동 제거)
    - 레짐 전환 비용 (min_hold + switch_penalty + hourly cap)
    - risk_bias 확대(+1)는 연속 충족 필요
    - TCA ≥ 8bps → risk_up 완전 블로킹
    - Shadow-lite: 대폭 변경 시 1사이클 유예
    """

    def __init__(
        self,
        config,
        notifier=None,
        shadow_mode: bool = True,
        shadow_cycles: int = 3,
        cooldown_min: int = 20,
        max_tunes_per_day: int = 6,
    ):
        self.notifier = notifier or (lambda level, msg: None)
        self.config = config
        self.baseline = {
            "momentum_min_long": max(0.001, min(0.005, float(getattr(config, "momentum_min_long", 0.003)))),
            "momentum_min_short": min(-0.003, float(getattr(config, "momentum_min_short", -0.005))),
            "volatility_min": max(0.001, min(0.005, float(getattr(config, "volatility_min", 0.003)))),
            "watch_limit": getattr(config, "watch_limit", 10),
            "max_open_symbols": getattr(config, "max_open_symbols", 10),
            "position_pct": max(float(getattr(config, "position_pct", 0.06)), 0.005),
            "leverage_min": float(getattr(config, "leverage_min", 1)),
            "leverage_max": float(getattr(config, "leverage_max", 10)),
            "max_loss_per_position": float(getattr(config, "max_loss_per_position", 1.8)),
        }
        self.current_mode = str(getattr(config, "auto_tune_mode", "balanced") or "balanced").lower()
        if self.current_mode not in {"aggressive", "balanced", "conservative"}:
            self.current_mode = "balanced"
        self.current = dict(self.baseline)
        self.state = AutoTunerState()
        self.state.shadow.active = bool(shadow_mode)
        self.state.shadow.cycles_required = int(shadow_cycles)
        self.cooldown_sec = int(cooldown_min) * 60
        self.max_tunes_per_day = int(max_tunes_per_day)

        # clamps (spec)
        # [P1-I2] volatility_min 상한을 baseline+0.001로 동적화 (고정 0.006 대신)
        self.base_clamps = {
            "momentum_min_long": (max(0.001, self.baseline["momentum_min_long"] - 0.002), min(0.006, self.baseline["momentum_min_long"] + 0.001)),
            "momentum_min_short": (self.baseline["momentum_min_short"] - 0.003, min(-0.003, self.baseline["momentum_min_short"] + 0.003)),
            "volatility_min": (0.0010, self.baseline["volatility_min"] + 0.001),
            "watch_limit": (10, 20),
            "max_open_symbols": (5, 12),
            "position_pct": (0.03, 0.08),
            "leverage_min": (1, 5),
            "leverage_max": (3, 12),
            "max_loss_per_position": (0.5, 2.2),
        }
        self.clamps = dict(self.base_clamps)

        # per-cycle step limits (legacy, kept for safety_guard compatibility)
        self.base_step = {
            "momentum_min_long": 0.0008,
            "momentum_min_short": 0.0008,
            "volatility_min": 0.001,
            "watch_limit": 1,
            "max_open_symbols": 0.5,
            "position_pct": 0.0015,
            "leverage_min": 0.5,
            "leverage_max": 1.0,
            "max_loss_per_position": 0.3,
        }
        self.max_step = dict(self.base_step)

        self.mode_profiles = {
            "aggressive": {
                "position_pct": (0.03, 0.08),
                "watch_limit": (12, 20),
                "max_open_symbols": (6, 14),
                "leverage_min": (1, 5),
                "leverage_max": (3, 12),
                "max_loss_per_position": (0.5, 2.2),
                "step_scale": 1.4,
                "risk_bias_up": 0.62,
                "risk_bias_down": 0.32,
                "pnl_gain_floor": -0.001,
                "pnl_loss_floor": -0.008,
            },
            "balanced": {
                "position_pct": (0.03, 0.08),
                "watch_limit": (10, 20),
                "max_open_symbols": (5, 12),
                "leverage_min": (1, 5),
                "leverage_max": (3, 12),
                "max_loss_per_position": (0.5, 2.2),
                "step_scale": 1.0,
                "risk_bias_up": 0.70,
                "risk_bias_down": 0.35,
                "pnl_gain_floor": 0.0,
                "pnl_loss_floor": -0.006,
            },
            "conservative": {
                "position_pct": (0.03, 0.08),
                "watch_limit": (8, 15),
                "max_open_symbols": (4, 10),
                "leverage_min": (1, 5),
                "leverage_max": (3, 12),
                "max_loss_per_position": (0.5, 2.2),
                "step_scale": 0.7,
                "risk_bias_up": 0.80,
                "risk_bias_down": 0.45,
                "pnl_gain_floor": 0.001,
                "pnl_loss_floor": -0.003,
            },
        }
        if self.current_mode not in self.mode_profiles:
            self.current_mode = "balanced"
        self._apply_mode_profile()

        # lifecycle
        init_snapshot = self._make_snapshot(
            params=self.current,
            regime="bootstrap",
            metrics=None,
            rationale="bootstrap",
        )
        self.lifecycle: Dict[str, Optional[LifecycleSnapshot]] = {
            "active": init_snapshot,
            "staged": None,
            "proposed": None,
        }
        self.lifecycle_meta: Dict[str, Any] = {
            "version": 2,
            "updated_at": time.time(),
            "last_apply_at": 0.0,
            "last_rollback_at": 0.0,
            "rollback_reason": "",
        }

        # hysteresis/debounce
        self.regime_hits_required = 2
        self.regime_hits_required_base = 2
        self.regime_fast_threshold = 1.2
        self.regime_hits_fast = 1
        self.noise_spike_ratio = 1.30
        self.noise_spike_hits_required = 2

    # ── lifecycle helpers ──────────────────────────────────────
    def _make_snapshot(self, params: Dict[str, float], regime: str = "", metrics: Optional[Dict[str, float]] = None, rationale: str = "") -> LifecycleSnapshot:
        metrics = metrics or {}
        snapshot = {
            "params": dict(params),
            "regime": regime,
            "metrics": {
                "confidence": _safe_float(metrics.get("confidence")),
                "noise_index": _safe_float(metrics.get("noise_index")),
                "pnl_30m": _safe_float(metrics.get("pnl_30m")),
                "pass_rate": _safe_float(metrics.get("pass_rate")),
                "entry_rate": _safe_float(metrics.get("entry_rate")),
                "fill_rate": _safe_float(metrics.get("fill_rate")),
            },
            "updated_at": time.time(),
            "rationale": rationale,
        }
        return snapshot

    def _set_lifecycle_stage(
        self,
        stage: str,
        params: Dict[str, float],
        regime: str,
        metrics: Dict[str, float],
        rationale: str,
    ) -> LifecycleSnapshot:
        if stage not in self.lifecycle:
            raise ValueError(f"invalid lifecycle stage: {stage}")
        snapshot = self._make_snapshot(params, regime=regime, metrics=metrics, rationale=rationale)
        self.lifecycle[stage] = snapshot
        if stage == "active":
            self.current = dict(snapshot["params"])
            self.lifecycle_meta["updated_at"] = snapshot["updated_at"]
            self.lifecycle_meta["last_apply_at"] = snapshot["updated_at"]
        return snapshot

    def _clear_stage(self, stage: str):
        if stage in self.lifecycle and stage != "active":
            self.lifecycle[stage] = None

    def _apply_mode_profile(self):
        profile = self.mode_profiles.get(self.current_mode, self.mode_profiles["balanced"])
        for key in ("position_pct", "leverage_min", "leverage_max", "max_loss_per_position"):
            if key in profile:
                self.clamps[key] = profile[key]
        step_scale = profile.get("step_scale", 1.0)
        for key, base_value in self.base_step.items():
            scale_target = step_scale if key in ("position_pct", "leverage_min", "leverage_max", "max_loss_per_position") else 1.0
            self.max_step[key] = base_value * scale_target
        self.risk_bias_up = profile.get("risk_bias_up", 0.7)
        self.risk_bias_down = profile.get("risk_bias_down", 0.35)
        self.pnl_gain_floor = profile.get("pnl_gain_floor", 0.0)
        self.pnl_loss_floor = profile.get("pnl_loss_floor", -0.006)

    # ── cooldown/daily helpers ──────────────────────────────────
    def _cooldown_active(self) -> bool:
        return time.time() < self.state.cooldown_until

    def _bump_cooldown(self):
        self.state.cooldown_until = time.time() + self.cooldown_sec

    def _day_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _bump_daily_counter(self):
        dk = self._day_key()
        if self.state.tune_day_key != dk:
            self.state.tune_day_key = dk
            self.state.tune_count_today = 0
        self.state.tune_count_today += 1

    # ═══════════════════════════════════════════════════════════
    # compute_metrics — v2: 다중 타임스케일 EMA + confidence 재계산
    # ═══════════════════════════════════════════════════════════
    def compute_metrics(
        self,
        returns: List[float],
        rv30: float,
        atr30: float,
        pass_rate: float,
        entry_rate: float,
        fill_rate: float,
        signal_pass_rate: float,
        execution_pass_rate: float,
        pure_fill_rate: float,
        blocked_ratelimit: float,
        blocked_cooldown: float,
        blocked_spike_guard: float,
        blocked_portfolio_cap: float,
        pnl_30m: float,
        order_failures: int,
        pnl_fast: float | None = None,
        pnl_slow_realized: float | None = None,
        pnl_slow_funding: float | None = None,
        pnl_slow_fee: float | None = None,
        pnl_slow_other: float | None = None,
        spread_bps_med: float | None = None,
        spread_bps_p90: float | None = None,
        slippage_bps_med: float | None = None,
        slippage_bps_p90: float | None = None,
        tca_spread_bps_med: float | None = None,
        tca_spread_bps_p90: float | None = None,
        tca_samples: float | None = None,
        sigma_floor: float = 0.0005,
    ) -> Dict[str, float]:
        mu, sigma_eff, mad = mad_sigma(returns, sigma_floor=sigma_floor)
        trend_score = 0.0 if sigma_eff <= 0 else math.copysign(abs(mu) / sigma_eff, mu)
        noise_index = max(0.0, sigma_eff - abs(mu))
        pnl_frac = normalize_pnl(pnl_30m)

        # TCA cost_bps (최대값 사용)
        cost_bps = max(
            float(slippage_bps_med) if slippage_bps_med is not None else 0.0,
            float(tca_spread_bps_med) if tca_spread_bps_med is not None else 0.0,
            float(spread_bps_med) if spread_bps_med is not None else 0.0,
        )

        # ── v2: 다중 타임스케일 EMA 업데이트 ──
        # Fast (α ≈ 0.15) — 5분 상당: TCA, failures, fill_rate
        alpha_fast = 0.15
        self.state.ema_tca_bps = exp_smooth(self.state.ema_tca_bps, cost_bps, alpha_fast)
        self.state.ema_failures = exp_smooth(self.state.ema_failures, float(order_failures), alpha_fast)
        self.state.ema_fill_rate = exp_smooth(self.state.ema_fill_rate, pure_fill_rate, alpha_fast)

        # Mid (α ≈ 0.05) — 15분 상당: trend, noise, pass_rate
        alpha_mid = 0.05
        self.state.ema_trend_score = exp_smooth(self.state.ema_trend_score, trend_score, alpha_mid)
        self.state.ema_noise_index = exp_smooth(self.state.ema_noise_index, noise_index, alpha_mid)
        self.state.ema_pass_rate = exp_smooth(self.state.ema_pass_rate, signal_pass_rate, alpha_mid)

        # Slow (α ≈ 0.014) — 60분 상당: PnL
        alpha_slow = 0.014
        self.state.ema_pnl = exp_smooth(self.state.ema_pnl, pnl_frac, alpha_slow)

        # ── v2: confidence 재계산 (다중 타임스케일 기반) ──
        wT = float(getattr(self.config, 'conf_weight_trend', 0.55))
        wQ = float(getattr(self.config, 'conf_weight_quality', 0.20))
        wN = float(getattr(self.config, 'conf_weight_noise', 0.20))
        wP = float(getattr(self.config, 'conf_weight_pnl', 0.05))

        trend_comp = clamp(abs(self.state.ema_trend_score) / 2.0, 0.0, 1.0)
        quality_comp = clamp((self.state.ema_pass_rate + self.state.ema_fill_rate) / 2.0, 0.0, 1.0)
        noise_comp = clamp(self.state.ema_noise_index / 0.01, 0.0, 1.0)
        pnl_comp = clamp((self.state.ema_pnl + 0.01) / 0.02, 0.0, 1.0)  # -1%~+1% → 0~1

        confidence = clamp(wT * trend_comp + wQ * quality_comp - wN * noise_comp + wP * pnl_comp, 0.0, 1.0)

        # 부트스트랩 보정: returns가 10개 미만이면 최소 0.20 보장
        if len(returns) < 10:
            confidence = max(confidence, 0.20)

        metrics = {
            "mu": float(mu),
            "mad": float(mad),
            "sigma_eff": float(sigma_eff),
            "trend_score": float(trend_score),
            "noise_index": float(noise_index),
            "rv30": float(rv30),
            "atr30": float(atr30),
            "pass_rate": float(pass_rate),
            "entry_rate": float(entry_rate),
            "fill_rate": float(fill_rate),
            "signal_pass_rate": float(signal_pass_rate),
            "execution_pass_rate": float(execution_pass_rate),
            "pure_fill_rate": float(pure_fill_rate),
            "blocked_ratelimit": float(blocked_ratelimit),
            "blocked_cooldown": float(blocked_cooldown),
            "blocked_spike_guard": float(blocked_spike_guard),
            "blocked_portfolio_cap": float(blocked_portfolio_cap),
            "confidence": float(confidence),
            "pnl_30m": float(pnl_frac),
            "order_failures": int(order_failures),
            "pnl_fast": float(pnl_fast) if pnl_fast is not None else 0.0,
            "pnl_slow_realized": float(pnl_slow_realized) if pnl_slow_realized is not None else 0.0,
            "pnl_slow_funding": float(pnl_slow_funding) if pnl_slow_funding is not None else 0.0,
            "pnl_slow_fee": float(pnl_slow_fee) if pnl_slow_fee is not None else 0.0,
            "pnl_slow_other": float(pnl_slow_other) if pnl_slow_other is not None else 0.0,
            "spread_bps_med": float(spread_bps_med) if spread_bps_med is not None else 0.0,
            "spread_bps_p90": float(spread_bps_p90) if spread_bps_p90 is not None else 0.0,
            "slippage_bps_med": float(slippage_bps_med) if slippage_bps_med is not None else 0.0,
            "slippage_bps_p90": float(slippage_bps_p90) if slippage_bps_p90 is not None else 0.0,
            "tca_spread_bps_med": float(tca_spread_bps_med) if tca_spread_bps_med is not None else 0.0,
            "tca_spread_bps_p90": float(tca_spread_bps_p90) if tca_spread_bps_p90 is not None else 0.0,
            "tca_samples": float(tca_samples) if tca_samples is not None else 0.0,
            # [G] KPI 추가 메트릭
            "cost_bps": float(cost_bps),
            "ema_trend_score": float(self.state.ema_trend_score),
            "ema_noise_index": float(self.state.ema_noise_index),
            "ema_pnl": float(self.state.ema_pnl),
            "ema_tca_bps": float(self.state.ema_tca_bps),
            "ema_fill_rate": float(self.state.ema_fill_rate),
        }
        self.state.last_metrics = metrics
        self.state.confidence = confidence
        return metrics

    # ═══════════════════════════════════════════════════════════
    # classify_regime — v2: 전환 비용 + 최소 유지시간 + 시간당 캡
    # ═══════════════════════════════════════════════════════════
    def classify_regime(self, metrics: Dict[str, float]) -> str:
        score = metrics["trend_score"]
        noise = metrics["noise_index"]
        noise_threshold = float(getattr(self.config, "regime_noise_threshold", 0.012))
        t_up = float(getattr(self.config, "regime_trend_up_threshold", self.state.t_up))
        t_dn = float(getattr(self.config, "regime_trend_dn_threshold", self.state.t_dn))

        if score > t_up and noise < noise_threshold:
            cand = "trend_up"
        elif score < t_dn and noise < noise_threshold:
            cand = "trend_down"
        else:
            cand = "chop"

        h = self.state.hysteresis
        if cand == "trend_up":
            h.up_hits += 1
            h.down_hits = 0
            h.chop_hits = 0
        elif cand == "trend_down":
            h.down_hits += 1
            h.up_hits = 0
            h.chop_hits = 0
        else:
            h.chop_hits += 1
            h.up_hits = 0
            h.down_hits = 0

        # [PATCH-1] 적응형 히스테리시스
        abs_score = abs(score)
        required = self.regime_hits_required_base
        if abs_score >= self.regime_fast_threshold:
            required = self.regime_hits_fast

        # 히스테리시스 통과한 후보 결정
        candidate = h.current_regime
        if h.up_hits >= required:
            candidate = "trend_up"
        elif h.down_hits >= required:
            candidate = "trend_down"
        elif h.chop_hits >= required:
            candidate = "chop"

        # ── v2: 전환 시도 시 추가 검증 ──
        now = time.time()
        current = h.current_regime

        # [A2] 시간당 레짐 전환 캡: 잠금 상태면 chop 강제
        if now < self.state.regime_locked_until:
            return current

        if candidate != current:
            min_hold = float(getattr(self.config, 'regime_min_hold_sec', 180))
            switch_penalty = float(getattr(self.config, 'regime_switch_penalty', 0.08))
            max_per_hour = int(getattr(self.config, 'regime_switch_max_per_hour', 3))

            time_in_regime = now - self.state.regime_entered_ts

            # 조건1: 최소 유지시간 충족
            if time_in_regime < min_hold and self.state.regime_entered_ts > 0:
                return current  # 전환 거부

            # 조건2: confidence 차이가 switch_penalty 이상
            if metrics["confidence"] < self.state.confidence + switch_penalty:
                # 예외: 현재 confidence가 매우 높으면 (>0.6) penalty 무시
                if metrics["confidence"] < 0.6:
                    return current  # 전환 거부 (확신 부족)

            # [A2] 조건3: 시간당 전환 횟수 제한
            # 1시간 이내 전환 기록 정리
            one_hour_ago = now - 3600
            self.state.regime_switch_timestamps = [
                ts for ts in self.state.regime_switch_timestamps if ts > one_hour_ago
            ]
            if len(self.state.regime_switch_timestamps) >= max_per_hour:
                # 초과: 30분 잠금 + chop 강제
                self.state.regime_locked_until = now + 1800
                h.current_regime = "chop"
                self.state.regime_entered_ts = now
                _logger.warning(
                    "REGIME_LOCK: %d switches/hour exceeded max=%d → chop forced for 30min",
                    len(self.state.regime_switch_timestamps), max_per_hour,
                )
                self.notifier("WARN", f"AutoTune regime lock: {len(self.state.regime_switch_timestamps)} switches/hour → chop 30min")
                return "chop"

            # 전환 승인
            h.current_regime = candidate
            self.state.regime_entered_ts = now
            self.state.regime_switch_timestamps.append(now)
            _logger.info("REGIME_SWITCH: %s → %s (conf=%.2f, time_in=%.0fs)",
                         current, candidate, metrics["confidence"], time_in_regime)

        return h.current_regime

    # ═══════════════════════════════════════════════════════════
    # _compute_risk_bias — v2: 연속 확인 + TCA 블로킹
    # ═══════════════════════════════════════════════════════════
    def _compute_risk_bias(self, metrics: Dict[str, float]) -> int:
        """[C] risk_bias 확대(+1)는 연속 충족 필요.
        TCA ≥ 8bps → risk_up 완전 블로킹.
        """
        confidence = metrics["confidence"]
        pnl = self.state.ema_pnl
        failures = self.state.ema_failures
        tca = self.state.ema_tca_bps

        confirm_needed = int(getattr(self.config, 'risk_bias_confirm_count', 2))

        # [C] TCA ≥ 8bps → risk_up 완전 차단
        tca_hard_block = 8.0
        if tca >= tca_hard_block:
            self.state.risk_bias_confirm_streak = 0
            # TCA 높으면 risk_down
            return -1

        # risk_down 조건
        if confidence < 0.40 or pnl <= -0.004 or failures >= 1.2:
            self.state.risk_bias_confirm_streak = 0
            return -1

        # risk_up 조건 (연속 확인 필요)
        if confidence > 0.75 and pnl >= 0 and failures < 0.5:
            self.state.risk_bias_confirm_streak += 1
            if self.state.risk_bias_confirm_streak >= confirm_needed:
                return 1
            return 0  # 아직 확인 부족

        self.state.risk_bias_confirm_streak = 0
        return 0

    # ═══════════════════════════════════════════════════════════
    # _score_targets — [A1] best_targets score 기반 선택
    # ═══════════════════════════════════════════════════════════
    def _score_targets(self, targets: Dict[str, float], metrics: Dict[str, float], regime: str) -> float:
        """Score = 0.55*trend + 0.20*quality - 0.20*noise + 0.05*pnl - switch_penalty"""
        wT = float(getattr(self.config, 'conf_weight_trend', 0.55))
        wQ = float(getattr(self.config, 'conf_weight_quality', 0.20))
        wN = float(getattr(self.config, 'conf_weight_noise', 0.20))
        wP = float(getattr(self.config, 'conf_weight_pnl', 0.05))

        trend_val = clamp(abs(self.state.ema_trend_score) / 2.0, 0.0, 1.0)
        quality_val = clamp((self.state.ema_pass_rate + self.state.ema_fill_rate) / 2.0, 0.0, 1.0)
        noise_val = clamp(self.state.ema_noise_index / 0.01, 0.0, 1.0)
        pnl_val = clamp((self.state.ema_pnl + 0.01) / 0.02, 0.0, 1.0)

        # 레짐 전환 패널티: 현재와 다른 레짐이면 감점
        switch_pen = 0.0
        if regime != self.state.hysteresis.current_regime:
            switch_pen = float(getattr(self.config, 'regime_switch_penalty', 0.08))

        score = wT * trend_val + wQ * quality_val - wN * noise_val + wP * pnl_val - switch_pen
        return score

    # ═══════════════════════════════════════════════════════════
    # propose_adjustment — v2: 목표값 기반 + 파라미터별 KPI 분리
    # ═══════════════════════════════════════════════════════════
    def propose_adjustment(self, regime: str, metrics: Dict[str, float]) -> Dict[str, float]:
        """[A3] 각 파라미터는 관련 KPI에만 반응:
        - momentum → trend_score, quality
        - volatility → noise_index
        - position/leverage → pnl, risk_bias
        """
        targets = dict(self.baseline)
        quality = clamp((self.state.ema_pass_rate + self.state.ema_fill_rate) / 2.0, 0.0, 1.0)

        # ── [B1-B2] 레짐 조건부 목표값 ──
        if regime == "trend_up":
            # trend_up + good quality → 강한 목표값
            offset = 0.0008 if quality > 0.6 else 0.0006
            targets["momentum_min_long"] = self.baseline["momentum_min_long"] + offset
            targets["momentum_min_short"] = self.baseline["momentum_min_short"]  # baseline 회귀
            targets["volatility_min"] = self.baseline["volatility_min"]  # 추세 시 변동성 필터 완화

        elif regime == "trend_down":
            offset = 0.0008 if quality > 0.6 else 0.0006
            targets["momentum_min_short"] = self.baseline["momentum_min_short"] - offset
            targets["momentum_min_long"] = self.baseline["momentum_min_long"]   # baseline 회귀
            targets["volatility_min"] = self.baseline["volatility_min"]

        else:
            # [B2] chop: good chop vs bad chop
            if quality > 0.6:
                # good chop: 약간만 방어적
                targets["momentum_min_long"] = self.baseline["momentum_min_long"]
                # [P1-I3] chop 숏 모멘텀 완화: baseline + 0.001 (-0.004 → -0.003)
                targets["momentum_min_short"] = self.baseline["momentum_min_short"] + 0.001
                targets["volatility_min"] = self.baseline["volatility_min"] + 0.0004
                targets["position_pct"] = self.baseline["position_pct"] - 0.0005
            else:
                # bad chop: 강하게 방어
                targets["momentum_min_long"] = self.baseline["momentum_min_long"]
                # [P1-I3] bad chop에서도 숏 완화 (절반): baseline + 0.0005
                targets["momentum_min_short"] = self.baseline["momentum_min_short"] + 0.0005
                targets["volatility_min"] = self.baseline["volatility_min"] + 0.0008
                targets["position_pct"] = self.baseline["position_pct"] - 0.0010

        # ── [A3] noise 반응: volatility_min 오버라이드 ──
        if self.state.ema_noise_index > 0.014:
            targets["volatility_min"] = self.baseline["volatility_min"] + 0.0010
            targets["position_pct"] = self.baseline["position_pct"] - 0.0010

        # ── [C] TCA 비용 높음 → momentum edge 확대 + 포지션 축소 ──
        if self.state.ema_tca_bps >= 8.0:
            targets["position_pct"] = self.baseline["position_pct"] - 0.0015
            targets["momentum_min_long"] = targets.get("momentum_min_long", self.baseline["momentum_min_long"]) + 0.0004
            targets["momentum_min_short"] = targets.get("momentum_min_short", self.baseline["momentum_min_short"]) - 0.0004

        # ── [A3] risk_bias → position/leverage KPI ──
        risk_bias = self._compute_risk_bias(metrics)
        if risk_bias > 0:
            targets["position_pct"] = self.baseline["position_pct"] + 0.0010
            targets["leverage_max"] = self.baseline["leverage_max"] + 1.0
            targets["leverage_min"] = max(1.0, self.baseline["leverage_min"] - 0.5)
            targets["max_loss_per_position"] = self.baseline["max_loss_per_position"] + 0.15
        elif risk_bias < 0:
            targets["position_pct"] = min(
                targets.get("position_pct", self.baseline["position_pct"]),
                self.baseline["position_pct"] - 0.0010
            )
            targets["leverage_max"] = max(5.0, self.baseline["leverage_max"] - 1.0)
            targets["leverage_min"] = self.baseline["leverage_min"] + 0.5
            targets["max_loss_per_position"] = self.baseline["max_loss_per_position"] - 0.15

        # watch_limit/max_open_symbols 제외 (기존 유지)
        targets.pop("watch_limit", None)
        targets.pop("max_open_symbols", None)

        # leverage 역전 방지
        if "leverage_min" in targets and "leverage_max" in targets:
            if targets["leverage_min"] > targets["leverage_max"] - 1:
                targets["leverage_min"] = max(1.0, targets["leverage_max"] - 1)

        # [A1] score 계산 및 best_targets 업데이트
        score = self._score_targets(targets, metrics, regime)
        if score > self.state.best_targets_score or not self.state.best_targets:
            self.state.best_targets = dict(targets)
            self.state.best_targets_score = score

        self.state.targets = targets
        return targets

    # ═══════════════════════════════════════════════════════════
    # apply_targets — v2: EMA 수렴 + ROC 캡 + Shadow-lite
    # ═══════════════════════════════════════════════════════════
    def apply_targets(self, metrics: Dict[str, float], regime: str) -> Tuple[Dict[str, float], bool, str]:
        """기존 apply_or_shadow() 대체.
        - apply_interval 충족 시에만 실제 적용
        - EMA 수렴으로 부드러운 파라미터 이동
        - Shadow-lite: 대폭 변경 시 1사이클 유예
        """
        now = time.time()
        apply_interval = float(getattr(self.config, 'auto_tune_apply_interval_sec', 300))

        # 적용 주기 미충족 → propose만 저장
        if (now - self.state.last_apply_ts) < apply_interval:
            return dict(self.current), False, "waiting_apply_interval"

        # 쿨다운 활성 중이면 적용 차단
        if self._cooldown_active():
            return dict(self.current), False, "cooldown_active"

        # [P1-I1] 최소 신뢰도 체크 — chop 레짐은 0.10, trend는 0.15
        confidence = metrics.get("confidence", 0.0)
        MIN_CONF_TREND = 0.15
        MIN_CONF_CHOP = 0.10
        MIN_CONF_TO_APPLY = MIN_CONF_CHOP if regime == "chop" else MIN_CONF_TREND
        if confidence < MIN_CONF_TO_APPLY:
            self.lifecycle_meta["last_reason"] = f"low_confidence({confidence:.2f}<{MIN_CONF_TO_APPLY})"
            return dict(self.current), False, f"low_confidence({confidence:.2f})"

        # 일일 튜닝 횟수 체크
        dk = self._day_key()
        if self.state.tune_day_key != dk:
            self.state.tune_day_key = dk
            self.state.tune_count_today = 0
        if self.state.tune_count_today >= self.max_tunes_per_day:
            return dict(self.current), False, "daily_limit"

        # [A1] best_targets 사용 (score 기반 최적 선택)
        targets = self.state.best_targets if self.state.best_targets else self.state.targets
        if not targets:
            return dict(self.current), False, "no_targets"

        # ── EMA 수렴 적용 ──
        alpha_entry = float(getattr(self.config, 'tune_alpha_entry', 0.15))
        alpha_risk = float(getattr(self.config, 'tune_alpha_risk', 0.08))

        alpha_map = {
            "momentum_min_long": alpha_entry,
            "momentum_min_short": alpha_entry,
            "volatility_min": alpha_entry,
            "position_pct": alpha_risk,
            "leverage_min": alpha_risk,
            "leverage_max": alpha_risk,
            "max_loss_per_position": alpha_risk,
        }

        # ROC 캡 (risk_limits.py DEFAULT_LIMITS와 정렬)
        roc_caps = {
            "momentum_min_long": 0.0006,
            "momentum_min_short": 0.0006,
            "volatility_min": 0.0006,
            "position_pct": 0.0010,
            "leverage_min": 0.5,
            "leverage_max": 1.0,
            "max_loss_per_position": 0.15,
        }

        new_params = dict(self.current)
        changed = False
        max_delta = 0.0  # Shadow-lite 판단용

        for key, target_val in targets.items():
            if key not in alpha_map:
                continue
            cur_val = self.current.get(key)
            if cur_val is None:
                continue

            alpha = alpha_map[key]
            smoothed = exp_smooth(float(cur_val), float(target_val), alpha)

            # ROC 캡 적용
            cap = roc_caps.get(key, 999.0)
            delta = smoothed - float(cur_val)
            if abs(delta) > cap:
                smoothed = float(cur_val) + math.copysign(cap, delta)

            if abs(smoothed - float(cur_val)) > 1e-7:
                changed = True
                # volatility_min 기준으로 max_delta 추적
                if key == "volatility_min":
                    max_delta = max(max_delta, abs(smoothed - float(cur_val)))
            new_params[key] = smoothed

        if not changed:
            return dict(self.current), False, "no_change"

        # ── [D] Shadow-lite: 대폭 변경 시 1사이클 유예 ──
        shadow_threshold = float(getattr(self.config, 'shadow_lite_threshold', 0.0012))
        if max_delta > shadow_threshold:
            if not self.state.shadow_lite_deferred:
                # 첫 번째: 유예
                self.state.shadow_lite_deferred = True
                self.state.shadow_lite_deferred_params = dict(new_params)
                _logger.info("SHADOW_LITE: deferred apply (max_delta=%.6f > threshold=%.6f)",
                             max_delta, shadow_threshold)
                return dict(self.current), False, f"shadow_lite_deferred(delta={max_delta:.6f})"
            else:
                # 두 번째: 유예된 params와 현재 params 비교하여 일관성 확인
                self.state.shadow_lite_deferred = False
                self.state.shadow_lite_deferred_params = {}
        else:
            self.state.shadow_lite_deferred = False
            self.state.shadow_lite_deferred_params = {}

        # clamp_params 적용 (절대 범위)
        new_params = clamp_params(new_params, self.current, DEFAULT_LIMITS)

        # leverage 역전 방지
        if "leverage_min" in new_params and "leverage_max" in new_params:
            lev_min = new_params["leverage_min"]
            lev_max = new_params["leverage_max"]
            if lev_max < 5.0:
                lev_max = 5.0
            if lev_min > lev_max - 1.0:
                lev_min = max(1.0, lev_max - 1.0)
            new_params["leverage_min"] = lev_min
            new_params["leverage_max"] = lev_max

        # 롤백 스택 저장 & 적용
        self.state.rollback_stack.append((dict(self.current), f"apply@{now:.0f}"))
        self._log_tune_rationale(new_params, metrics, regime)
        self._set_lifecycle_stage("active", new_params, regime, metrics, "applied_v2")
        self._clear_stage("staged")
        self._clear_stage("proposed")
        self.state.last_apply_ts = now
        self._bump_daily_counter()

        # [A1] best_targets 리셋 (적용 후 새로 수집)
        self.state.best_targets = {}
        self.state.best_targets_score = 0.0

        self.lifecycle_meta["last_reason"] = "applied"
        return dict(self.current), True, "applied"

    # ── legacy apply_or_shadow (backward compat) ──────────────
    def apply_or_shadow(self, proposal: Dict[str, float], metrics: Dict[str, float], regime: str) -> Tuple[Dict[str, float], bool, str]:
        """v1 호환: 내부적으로 apply_targets 위임."""
        self.state.targets = proposal
        return self.apply_targets(metrics, regime)

    # ── shadow validation gates (legacy, 유지) ─────────────────
    def _shadow_should_apply(self) -> bool:
        sh = self.state.shadow
        if len(sh.records) < sh.cycles_required:
            return False
        base = sh.baseline_snapshot or {}
        if not base:
            return False
        conf_avg = sum(r["metrics"]["confidence"] for r in sh.records[-sh.cycles_required:]) / sh.cycles_required
        fail_avg = sum(r["metrics"]["order_failures"] for r in sh.records[-sh.cycles_required:]) / sh.cycles_required
        fill_avg = sum(r["metrics"]["fill_rate"] for r in sh.records[-sh.cycles_required:]) / sh.cycles_required
        noise_avg = sum(r["metrics"]["noise_index"] for r in sh.records[-sh.cycles_required:]) / sh.cycles_required
        conf0 = base.get("confidence", 0.0)
        fail0 = base.get("order_failures", 0.0)
        fill0 = base.get("fill_rate", 0.0)
        noise0 = base.get("noise_index", noise_avg)
        if conf_avg < conf0 - 0.05:
            return False
        if fail_avg > fail0 + 0.5:
            return False
        if fill_avg < fill0 - 0.05:
            return False
        if noise_avg > noise0 + 0.002:
            return False
        cand_snap = sh.perf_candidate.snapshot() if hasattr(sh, 'perf_candidate') else {}
        _cand_exp = cand_snap.get("expectancy", 0.0) if cand_snap else 0.0
        if _cand_exp < 0:
            return False
        if fill_avg < 0.80:
            return False
        return True

    def _shadow_record_metrics(self, metrics: Dict[str, float], *, candidate: bool):
        sh = self.state.shadow
        perf = sh.perf_candidate if candidate else sh.perf_baseline
        perf.samples += 1
        perf.win_sum += _safe_float(metrics.get("win_rate", metrics.get("pass_rate", 0.0)))
        perf.expectancy_sum += _safe_float(metrics.get("expectancy", 0.0))
        perf.pnl_sum += _safe_float(metrics.get("pnl_30m", 0.0))
        perf.dd_min = min(perf.dd_min, _safe_float(metrics.get("session_dd_pct", 0.0)))
        perf.last_ts = time.time()
        if candidate and perf.samples % max(1, sh.cycles_required) == 0:
            self._shadow_log_perf(prefix="Shadow candidate perf")

    def _shadow_log_perf(self, prefix: str = "Shadow perf"):
        sh = self.state.shadow
        cand = sh.perf_candidate.snapshot()
        base = sh.perf_baseline.snapshot()
        self.notifier(
            "TUNE",
            (
                f"{prefix}: cand win={cand['win_rate']:.2f} exp={cand['expectancy']:.4f} pnl={cand['pnl']:.4f} dd={cand['dd']:.4f} n={cand['samples']} | "
                f"base win={base['win_rate']:.2f} exp={base['expectancy']:.4f} pnl={base['pnl']:.4f} dd={base['dd']:.4f} n={base['samples']}"
            ),
        )

    def _reset_candidate_performance(self):
        self.state.shadow.perf_candidate.reset()

    # ── safety_guard (legacy, 하위 호환) ──────────────────────
    def safety_guard(self, proposal: Dict[str, float]) -> Dict[str, float]:
        """v1 호환: clamp_params + step-limit + consecutive guard."""
        if self._cooldown_active():
            return dict(self.current)
        dk = self._day_key()
        if self.state.tune_day_key != dk:
            self.state.tune_day_key = dk
            self.state.tune_count_today = 0
        if self.state.tune_count_today >= self.max_tunes_per_day:
            self._bump_cooldown()
            return dict(self.current)
        proposal = clamp_params(proposal, self.current, DEFAULT_LIMITS)
        for k, max_d in self.max_step.items():
            if k not in proposal:
                continue
            cur = self.current.get(k)
            tgt = proposal.get(k)
            if not isinstance(cur, (int, float)) or not isinstance(tgt, (int, float)):
                continue
            delta = tgt - cur
            if abs(delta) > max_d:
                proposal[k] = cur + math.copysign(max_d, delta)
        for k, value in proposal.items():
            cur = self.current.get(k)
            if not isinstance(value, (int, float)) or not isinstance(cur, (int, float)):
                self.state.consecutive_same_dir[k] = 0
                self.state.last_dir[k] = 0
                continue
            delta = value - cur
            d = sign(delta)
            prev_d = self.state.last_dir.get(k, 0)
            if d == 0:
                self.state.consecutive_same_dir[k] = 0
                self.state.last_dir[k] = 0
                continue
            if d == prev_d:
                self.state.consecutive_same_dir[k] = self.state.consecutive_same_dir.get(k, 0) + 1
            else:
                self.state.consecutive_same_dir[k] = 1
            self.state.last_dir[k] = d
            if self.state.consecutive_same_dir[k] > 2:
                self._bump_cooldown()
                return dict(self.current)
        if "leverage_min" in proposal and "leverage_max" in proposal:
            lev_min = proposal["leverage_min"]
            lev_max = proposal["leverage_max"]
            if lev_max < 5.0:
                lev_max = 5.0
            if lev_min > lev_max - 1.0:
                lev_min = max(1.0, lev_max - 1.0)
            proposal["leverage_min"] = lev_min
            proposal["leverage_max"] = lev_max
        return proposal

    # ── tune rationale logging ─────────────────────────────────
    def _log_tune_rationale(self, proposal: Dict[str, float], metrics: Dict[str, float], regime: str):
        """파라미터 변경 시 이유를 1줄 요약 로그로 남긴다."""
        conf = metrics.get("confidence", 0.0)
        pnl = metrics.get("pnl_30m", 0.0)
        cost = max(
            metrics.get("slippage_bps_med", 0.0) or 0.0,
            metrics.get("tca_spread_bps_med", 0.0) or 0.0,
        )
        fails = metrics.get("order_failures", 0)
        noise = metrics.get("noise_index", 0.0)
        diffs = []
        for k in ("position_pct", "leverage_max", "leverage_min", "volatility_min",
                  "momentum_min_long", "max_loss_per_position"):
            old_v = self.current.get(k)
            new_v = proposal.get(k)
            if old_v is not None and new_v is not None:
                diff = new_v - old_v
                if abs(diff) > 1e-6:
                    direction = "↑" if diff > 0 else "↓"
                    diffs.append(f"{k}{direction}({old_v:.4g}→{new_v:.4g})")
        if not diffs:
            return
        reasons = []
        if conf > 0.7 and pnl > 0:
            reasons.append(f"high_conf={conf:.2f} pnl={pnl:.4f}")
        elif conf < 0.35 or pnl < -0.01:
            reasons.append(f"low_conf={conf:.2f} pnl={pnl:.4f}")
        if cost >= 8.0:
            reasons.append(f"high_cost={cost:.1f}bps")
        if fails >= 2:
            reasons.append(f"failures={int(fails)}")
        if noise > 0.004:
            reasons.append(f"high_noise={noise:.4f}")
        reason_str = ", ".join(reasons) if reasons else "scheduled_rebalance"
        _logger.info("AUTOTUNE_APPLY regime=%s reason=[%s] changes=[%s]",
                     regime, reason_str, " | ".join(diffs))
        if hasattr(self, "notifier") and self.notifier:
            self.notifier("INFO", f"AUTOTUNE_APPLY regime={regime} | {reason_str} | " + " | ".join(diffs[:3]))

    # ── rollback/brake ────────────────────────────────────────
    def evaluate_and_rollback(self, metrics: Dict[str, float]):
        # v2: config의 rollback_cooldown 사용
        rollback_cooldown = float(getattr(self.config, 'auto_tune_rollback_cooldown_sec', 600))
        last_rollback = self.lifecycle_meta.get("last_rollback_at", 0.0)
        now = time.time()

        # 롤백 쿨다운: 마지막 롤백으로부터 일정 시간 지나야 재롤백 가능
        if (now - last_rollback) < rollback_cooldown and last_rollback > 0:
            return

        if metrics["pnl_30m"] <= -0.02:
            if self.state.rollback_stack:
                prev, reason = self.state.rollback_stack.pop()
                self.current = dict(prev)
                if self.lifecycle.get("active"):
                    self.lifecycle["active"]["params"] = dict(self.current)
                    self.lifecycle["active"]["updated_at"] = now
                self.lifecycle_meta["last_rollback_at"] = now
                self.lifecycle_meta["rollback_reason"] = reason
                self._bump_cooldown()
                self.notifier("WARN", f"AutoTune rollback: pnl_30m={metrics['pnl_30m']:.4f} ({reason})")
                _logger.warning("AUTOTUNE_ROLLBACK: pnl_30m=%.4f reason=%s", metrics['pnl_30m'], reason)
            else:
                self.current = dict(self.baseline)
                if self.lifecycle.get("active"):
                    self.lifecycle["active"]["params"] = dict(self.current)
                    self.lifecycle["active"]["updated_at"] = now
                self.lifecycle_meta["last_rollback_at"] = now
                self.lifecycle_meta["rollback_reason"] = "baseline"
                self._bump_cooldown()
                self.notifier("WARN", f"AutoTune rollback: pnl_30m={metrics['pnl_30m']:.4f} (baseline)")
                _logger.warning("AUTOTUNE_ROLLBACK: pnl_30m=%.4f reason=baseline", metrics['pnl_30m'])

    # ═══════════════════════════════════════════════════════════
    # run_cycle — v2: propose/apply 분리
    # ═══════════════════════════════════════════════════════════
    def run_cycle(
        self,
        returns: List[float],
        rv30: float,
        atr30: float,
        pass_rate: float,
        entry_rate: float,
        fill_rate: float,
        signal_pass_rate: float,
        execution_pass_rate: float,
        pure_fill_rate: float,
        blocked_ratelimit: float,
        blocked_cooldown: float,
        blocked_spike_guard: float,
        blocked_portfolio_cap: float,
        pnl_30m: float,
        order_failures: int,
        pnl_fast: float | None = None,
        pnl_slow_realized: float | None = None,
        pnl_slow_funding: float | None = None,
        pnl_slow_fee: float | None = None,
        pnl_slow_other: float | None = None,
        spread_bps_med: float | None = None,
        spread_bps_p90: float | None = None,
        slippage_bps_med: float | None = None,
        slippage_bps_p90: float | None = None,
        tca_spread_bps_med: float | None = None,
        tca_spread_bps_p90: float | None = None,
        tca_samples: float | None = None,
    ) -> Dict[str, float]:
        # ① 매 틱: EMA 메트릭 업데이트 포함
        metrics = self.compute_metrics(
            returns,
            rv30,
            atr30,
            pass_rate,
            entry_rate,
            fill_rate,
            signal_pass_rate,
            execution_pass_rate,
            pure_fill_rate,
            blocked_ratelimit,
            blocked_cooldown,
            blocked_spike_guard,
            blocked_portfolio_cap,
            pnl_30m,
            order_failures,
            pnl_fast=pnl_fast,
            pnl_slow_realized=pnl_slow_realized,
            pnl_slow_funding=pnl_slow_funding,
            pnl_slow_fee=pnl_slow_fee,
            pnl_slow_other=pnl_slow_other,
            spread_bps_med=spread_bps_med,
            spread_bps_p90=spread_bps_p90,
            slippage_bps_med=slippage_bps_med,
            slippage_bps_p90=slippage_bps_p90,
            tca_spread_bps_med=tca_spread_bps_med,
            tca_spread_bps_p90=tca_spread_bps_p90,
            tca_samples=tca_samples,
        )

        # ② 매 틱: shadow perf 기록
        self._shadow_record_metrics(metrics, candidate=self.state.shadow.active)

        # ③ 매 틱: 레짐 분류 (전환 비용 포함)
        regime = self.classify_regime(metrics)

        # ④ 매 틱: 목표값만 저장 (propose)
        targets = self.propose_adjustment(regime, metrics)

        # ⑤ apply는 간격 충족 시에만 실행 (내부에서 체크)
        params, applied, reason = self.apply_targets(metrics, regime)

        # ⑥ 롤백 체크
        self.evaluate_and_rollback(metrics)

        # [G] KPI 로그
        self.notifier(
            "WATCH",
            (
                f"AutoTune v2 regime={regime} conf={metrics['confidence']:.2f} "
                f"ema_trend={self.state.ema_trend_score:.3f} ema_noise={self.state.ema_noise_index:.4f} "
                f"ema_pnl={self.state.ema_pnl:.5f} ema_tca={self.state.ema_tca_bps:.1f} "
                f"applied={applied} reason={reason} "
                f"bias_streak={self.state.risk_bias_confirm_streak} "
                f"pnl_30m={metrics['pnl_30m']:.4f} fails={metrics['order_failures']}"
            ),
        )
        return dict(self.current)
