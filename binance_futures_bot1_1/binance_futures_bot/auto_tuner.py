import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

from .risk_limits import clamp_params, DEFAULT_LIMITS

LifecycleSnapshot = Dict[str, Any]


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


# -----------------------------
# main class
# -----------------------------
class AutoTuner:
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
        self.config = config  # EngineConfig 참조 보관 (regime 파라미터 조회용)
        self.baseline = {
            # baseline 상한 0.005: 오염된 config값(0.007 등)이 baseline 되는 것 방지
            # baseline이 높으면 clamp 상한(baseline+0.003)도 올라가서 고값 허용
            "momentum_min_long": max(0.001, min(0.005, float(getattr(config, "momentum_min_long", 0.003)))),
            "momentum_min_short": min(-0.003, float(getattr(config, "momentum_min_short", -0.005))),  # 최소 0.3% 하락 요구
            "volatility_min": max(0.001, min(0.005, float(getattr(config, "volatility_min", 0.002)))),
            "watch_limit": getattr(config, "watch_limit", 10),
            "max_open_symbols": getattr(config, "max_open_symbols", 10),
            "position_pct": max(float(getattr(config, "position_pct", 0.06)), 0.005),  # [PATCH-14] 0.05→0.06
            "leverage_min": float(getattr(config, "leverage_min", 1)),      # [PATCH-14] 5→1
            "leverage_max": float(getattr(config, "leverage_max", 10)),      # [PATCH-14] 25→10
            "max_loss_per_position": float(getattr(config, "max_loss_per_position", 1.8)),  # [PATCH-14] 55.0→1.8
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
        # ═══════════════════════════════════════════════════════════
        # 🚀 v2.0 최소 패치: Hard Floor 적용
        # - watch_limit 최소: 5 → 10
        # - max_open_symbols 최소: 2 → 5
        # ═══════════════════════════════════════════════════════════
        self.base_clamps = {
            # clamp 상한: baseline(≤0.005) + 0.001 = 최대 0.006
            # 0.007 이상이면 대부분 심볼 진입 차단되므로 엄격하게 제한
            "momentum_min_long": (max(0.001, self.baseline["momentum_min_long"] - 0.002), min(0.006, self.baseline["momentum_min_long"] + 0.001)),
            "momentum_min_short": (self.baseline["momentum_min_short"] - 0.003, min(-0.003, self.baseline["momentum_min_short"] + 0.003)),  # 최소 0.3% 요구
            "volatility_min": (0.0010, 0.0060),  # 상한 0.012→0.006 (너무 높으면 진입 불가)
            "watch_limit": (10, 20),  # 최소 10개 심볼 감시 (5→10 상향!)
            "max_open_symbols": (5, 12),  # 최소 5개 포지션 (2→5 상향!)
            "position_pct": (0.03, 0.08),   # [PATCH-14] 0.12→0.08 risk_limits 정렬
            "leverage_min": (1, 5),         # [PATCH-14] 60→5 risk_limits 정렬
            "leverage_max": (3, 12),        # [PATCH-14] 120→12 risk_limits 정렬
            "max_loss_per_position": (0.5, 2.2),  # [PATCH-14] 60→2.2 risk_limits 정렬
        }
        self.clamps = dict(self.base_clamps)
        # per-cycle step limits
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
                "position_pct": (0.03, 0.08),       # [PATCH-14] 0.18→0.08 risk_limits 정렬
                "watch_limit": (12, 20),
                "max_open_symbols": (6, 14),
                "leverage_min": (1, 5),              # [PATCH-14] 90→5 risk_limits 정렬
                "leverage_max": (3, 12),             # [PATCH-14] 100→12 risk_limits 정렬
                "max_loss_per_position": (0.5, 2.2), # [PATCH-14] 60→2.2 risk_limits 정렬
                "step_scale": 1.4,
                "risk_bias_up": 0.62,
                "risk_bias_down": 0.32,
                "pnl_gain_floor": -0.001,
                "pnl_loss_floor": -0.008,
            },
            "balanced": {
                "position_pct": (0.03, 0.08),        # [PATCH-14] 0.12→0.08 risk_limits 정렬
                "watch_limit": (10, 20),
                "max_open_symbols": (5, 12),
                "leverage_min": (1, 5),               # [PATCH-14] 80→5 risk_limits 정렬
                "leverage_max": (3, 12),              # [PATCH-14] 100→12 risk_limits 정렬
                "max_loss_per_position": (0.5, 2.2),  # [PATCH-14] 55→2.2 risk_limits 정렬
                "step_scale": 1.0,
                "risk_bias_up": 0.70,
                "risk_bias_down": 0.35,
                "pnl_gain_floor": 0.0,
                "pnl_loss_floor": -0.006,
            },
            "conservative": {
                "position_pct": (0.03, 0.08),        # [PATCH-14] 0.02→0.03 risk_limits 정렬
                "watch_limit": (8, 15),
                "max_open_symbols": (4, 10),
                "leverage_min": (1, 5),               # [PATCH-14] 60→5 risk_limits 정렬
                "leverage_max": (3, 12),              # [PATCH-14] 110→12 risk_limits 정렬
                "max_loss_per_position": (0.5, 2.2),  # [PATCH-14] 40→2.2 risk_limits 정렬
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
            "version": 1,
            "updated_at": time.time(),
            "last_apply_at": 0.0,
            "last_rollback_at": 0.0,
            "rollback_reason": "",
        }
        # hysteresis/debounce
        self.regime_hits_required = 2
        # [PATCH-1] 적응형 히스테리시스: 강한 신호는 빠른 전환
        self.regime_hits_required_base = 2       # 기본값 유지 (약한 신호)
        self.regime_fast_threshold = 1.2         # 강한 신호 임계값 (|score| >= 1.2)
        self.regime_hits_fast = 1                # 강한 신호 시 1회 확인으로 즉시 전환
        self.noise_spike_ratio = 1.30
        self.noise_spike_hits_required = 2

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
        for key in ("position_pct", "leverage_min", "leverage_max", "max_loss_per_position"):  # watch_limit/max_open_symbols은 auto-tune 제외
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
        trend_component = clamp(abs(trend_score) / 2.0, 0.0, 1.0)
        quality_component = clamp((signal_pass_rate + execution_pass_rate + pure_fill_rate) / 3.0, 0.0, 1.0)
        noise_penalty = clamp(noise_index / 0.01, 0.0, 1.0)
        _raw_conf = 0.6 * trend_component + 0.3 * quality_component - 0.3 * noise_penalty
        # ── 부트스트랩 보정: 시작 직후 데이터가 없을 때 conf=0이 되어 파라미터 교체
        # 불가능한 데드락 방지 — returns가 10개 미만이면 최소 0.20 보장
        if len(returns) < 10:
            _raw_conf = max(_raw_conf, 0.20)
        confidence = clamp(_raw_conf, 0.0, 1.0)
        pnl_frac = normalize_pnl(pnl_30m)
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
        }
        self.state.last_metrics = metrics
        self.state.confidence = confidence
        return metrics

    def classify_regime(self, metrics: Dict[str, float]) -> str:
        score = metrics["trend_score"]
        noise = metrics["noise_index"]
        noise_threshold = float(getattr(self.config, "regime_noise_threshold", 0.012))
        # config로 t_up/t_dn 재정의 가능 (기본값은 HysteresisState)
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
        # [PATCH-1] 적응형 히스테리시스: 강한 신호는 빠른 전환
        abs_score = abs(score)
        required = self.regime_hits_required_base  # 기본 2회
        if abs_score >= self.regime_fast_threshold:
            required = self.regime_hits_fast  # 강한 신호 시 1회
        if h.up_hits >= required:
            h.current_regime = "trend_up"
        elif h.down_hits >= required:
            h.current_regime = "trend_down"
        elif h.chop_hits >= required:
            h.current_regime = "chop"
        return h.current_regime

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

    def propose_adjustment(self, regime: str, metrics: Dict[str, float]) -> Dict[str, float]:
        proposal = dict(self.current)
        if getattr(self, "mode_profiles", None) and self.current_mode in self.mode_profiles:
            proposal["auto_tune_mode"] = self.current_mode
        # momentum rules (long/short separated)
        if regime == "trend_up":
            proposal["momentum_min_long"] = self.current["momentum_min_long"] + self.max_step["momentum_min_long"]
            proposal["momentum_min_short"] = exp_smooth(
                self.current["momentum_min_short"], self.baseline["momentum_min_short"], alpha=0.3
            )
        elif regime == "trend_down":
            # strengthens short if your short rule is (momentum <= momentum_min_short)
            proposal["momentum_min_short"] = self.current["momentum_min_short"] - self.max_step["momentum_min_short"]
            proposal["momentum_min_long"] = exp_smooth(
                self.current["momentum_min_long"], self.baseline["momentum_min_long"], alpha=0.3
            )
        else:
            # chop/range: baseline 방향으로 회귀
            # momentum이 과도하게 높으면 alpha 강화하여 빠르게 낮춤
            _mom_long_cur = self.current["momentum_min_long"]
            _mom_alpha = 0.5 if _mom_long_cur >= 0.006 else 0.3  # 0.006 이상은 빠른 수렴
            proposal["momentum_min_long"] = exp_smooth(
                _mom_long_cur, self.baseline["momentum_min_long"], alpha=_mom_alpha
            )
            proposal["momentum_min_short"] = exp_smooth(
                self.current["momentum_min_short"], self.baseline["momentum_min_short"], alpha=0.3
            )
        # noise debounce via sigma ratio
        prev_sigma = self.state.last_metrics.get("sigma_eff", metrics["sigma_eff"])
        sigma_ratio = (metrics["sigma_eff"] / prev_sigma) if prev_sigma > 0 else 1.0
        h = self.state.hysteresis
        if sigma_ratio >= self.noise_spike_ratio:
            h.noise_spike_hits += 1
        else:
            h.noise_spike_hits = 0
        high_noise = (metrics["noise_index"] > 0.004) or (h.noise_spike_hits >= self.noise_spike_hits_required)
        if high_noise:
            proposal["volatility_min"] = self.current["volatility_min"] + self.max_step["volatility_min"]
        else:
            proposal["volatility_min"] = exp_smooth(
                self.current["volatility_min"], self.baseline["volatility_min"], alpha=0.3
            )
        # watch_limit / max_open_symbols는 auto-tune 조정 대상 완전 제외
        # 심볼 감시 수는 진입 필터(MTF, composite 등)가 이미 담당하므로
        # 줄여봐야 기회 손실만 발생하고 리스크 감소 효과 없음
        # proposal에서 제거하여 state 파일에도 저장되지 않도록 함
        proposal.pop("watch_limit", None)
        proposal.pop("max_open_symbols", None)

        # risk controls: position size, leverage range, stop-loss tolerance
        pnl_bias = metrics["pnl_30m"]
        confidence = metrics["confidence"]
        failures = metrics["order_failures"]
        risk_bias = 0
        if confidence > self.risk_bias_up and pnl_bias >= self.pnl_gain_floor and failures == 0:
            risk_bias = 1
        elif confidence < self.risk_bias_down or pnl_bias <= self.pnl_loss_floor or failures >= 2:
            risk_bias = -1

        # Cost-aware override (TCA / liquidity): if costs are elevated, de-risk and demand more edge.
        tca_n = float(metrics.get("tca_samples", 0.0) or 0.0)
        cost_bps = max(
            float(metrics.get("slippage_bps_med", 0.0) or 0.0),
            float(metrics.get("tca_spread_bps_med", 0.0) or 0.0),
            float(metrics.get("spread_bps_med", 0.0) or 0.0),
        )
        # Soft/hard bands (bps) tuned for perp taker-heavy execution; you can tweak later.
        soft_cost = 8.0
        hard_cost = 12.0
        if tca_n >= 3 and cost_bps >= hard_cost:
            risk_bias = -1
            # TCA 비용 높을 때: watch_limit 감소 제거 → volatility_min 높여서 저유동성 심볼만 걸러냄
            proposal["volatility_min"] = self.current["volatility_min"] + self.max_step["volatility_min"]
        elif tca_n >= 3 and cost_bps >= soft_cost and pnl_bias < self.pnl_gain_floor:
            risk_bias = -1
            proposal["volatility_min"] = self.current["volatility_min"] + self.max_step["volatility_min"]

        if tca_n >= 3 and cost_bps >= soft_cost:
            # demand more momentum edge to compensate for costs
            proposal["momentum_min_long"] = self.current["momentum_min_long"] + self.max_step["momentum_min_long"]
            proposal["momentum_min_short"] = self.current["momentum_min_short"] - self.max_step["momentum_min_short"]
        if risk_bias > 0:
            proposal["position_pct"] = self.current["position_pct"] + self.max_step["position_pct"]
            proposal["leverage_max"] = self.current["leverage_max"] + self.max_step["leverage_max"]
            proposal["leverage_min"] = max(1.0, self.current["leverage_min"] - self.max_step["leverage_min"])
            proposal["max_loss_per_position"] = self.current["max_loss_per_position"] + self.max_step["max_loss_per_position"]
        elif risk_bias < 0:
            proposal["position_pct"] = self.current["position_pct"] - self.max_step["position_pct"]
            proposal["leverage_max"] = max(5.0, self.current["leverage_max"] - self.max_step["leverage_max"])
            proposal["leverage_min"] = self.current["leverage_min"] + self.max_step["leverage_min"]
            proposal["max_loss_per_position"] = self.current["max_loss_per_position"] - self.max_step["max_loss_per_position"]
        else:
            proposal["position_pct"] = exp_smooth(
                self.current["position_pct"], self.baseline["position_pct"], alpha=0.3
            )
            proposal["leverage_min"] = exp_smooth(
                self.current["leverage_min"], self.baseline["leverage_min"], alpha=0.3
            )
            proposal["leverage_max"] = exp_smooth(
                self.current["leverage_max"], self.baseline["leverage_max"], alpha=0.3
            )
            proposal["max_loss_per_position"] = exp_smooth(
                self.current["max_loss_per_position"], self.baseline["max_loss_per_position"], alpha=0.3
            )

        if proposal["leverage_min"] > proposal["leverage_max"] - 1:
            proposal["leverage_min"] = max(1.0, proposal["leverage_max"] - 1)
        return proposal

    def safety_guard(self, proposal: Dict[str, float]) -> Dict[str, float]:
        if self._cooldown_active():
            return dict(self.current)
        # daily tune cap
        dk = self._day_key()
        if self.state.tune_day_key != dk:
            self.state.tune_day_key = dk
            self.state.tune_count_today = 0
        if self.state.tune_count_today >= self.max_tunes_per_day:
            self._bump_cooldown()
            return dict(self.current)
        # clamp + ROC
        proposal = clamp_params(proposal, self.current, DEFAULT_LIMITS)
        # step-limit (legacy)
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
        # consecutive same-direction guard
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
        # ── Fix: leverage 역전 방지 (step-limit 이후 재발 가능) ──────────
        # clamp_params 이후 step-limit이 leverage_max를 추가로 낮출 수 있어
        # leverage_min > leverage_max 역전이 재발생하는 경우를 최종 보정
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

    # shadow validation gates
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
        # [PATCH-10] EV 기반 하드 조건: 후보 기대값이 0 이상이어야 승격
        cand_snap = sh.perf_candidate.snapshot() if hasattr(sh, 'perf_candidate') else {}
        _cand_exp = cand_snap.get("expectancy", 0.0) if cand_snap else 0.0
        if _cand_exp < 0:
            return False
        # [PATCH-10] fill_rate 하드 조건: 체결률 80% 미만이면 폐기
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

    def apply_or_shadow(self, proposal: Dict[str, float], metrics: Dict[str, float], regime: str) -> Tuple[Dict[str, float], bool, str]:
        sh = self.state.shadow
        confidence = float(metrics.get("confidence", 0.0))

        # ── Fix: 최소 신뢰도 미달 시 적용 차단 ─────────────────────────────
        # conf < 0.25 이면 파라미터 변경을 적용하지 않음
        # (risk_bias_down=0.35 보다 낮은 완충 임계값)
        # chop 레짐에서도 어느 정도 conf 있으면 파라미터 개선 허용
        # 0.25 → 0.15: 너무 높으면 항상 low_confidence로 막혀 데드락
        MIN_CONF_TO_APPLY = 0.15
        if confidence < MIN_CONF_TO_APPLY:
            self.lifecycle_meta["last_reason"] = f"low_confidence({confidence:.2f}<{MIN_CONF_TO_APPLY})"
            return dict(self.current), False, f"low_confidence({confidence:.2f}<{MIN_CONF_TO_APPLY})"

        # ── Fix: Shadow 비활성 상태에서 낮은 신뢰도면 재활성화 ───────────────
        # Shadow가 이전 사이클에서 통과 후 영구 비활성 되는 문제 보정:
        # 신뢰도가 risk_bias_down 임계값 아래면 Shadow 재활성화하여 재검증
        SHADOW_REARM_CONF = getattr(self, "risk_bias_down", 0.35)
        if not sh.active and confidence < SHADOW_REARM_CONF:
            sh.active = True
            sh.baseline_snapshot = None
            sh.records.clear()
            self._reset_candidate_performance()

        self._set_lifecycle_stage("staged", proposal, regime, metrics, "shadow")
        if sh.active and sh.baseline_snapshot is None:
            sh.baseline_snapshot = dict(metrics)
            self._reset_candidate_performance()
        if sh.active:
            sh.records.append({"ts": time.time(), "regime": regime, "proposal": dict(proposal), "metrics": dict(metrics)})
            if self._shadow_should_apply():
                self._shadow_log_perf(prefix="Shadow candidate ready")
                sh.active = False
            else:
                self.lifecycle_meta["last_reason"] = "shadow_validate"
                return dict(self.current), False, "shadow_validate"
        self.state.rollback_stack.append((dict(self.current), f"apply@{time.time():.0f}"))
        # E: why-changed 1-line summary log
        self._log_tune_rationale(proposal, metrics, regime)
        snapshot = self._set_lifecycle_stage("active", proposal, regime, metrics, "applied")
        self._clear_stage("staged")
        self._clear_stage("proposed")
        self._bump_daily_counter()
        sh.records.clear()
        sh.baseline_snapshot = None
        self._shadow_log_perf(prefix="Shadow promote")
        self._reset_candidate_performance()
        self.lifecycle_meta["last_reason"] = "applied"
        return dict(snapshot["params"]), True, "applied"

    # rollback/brake: PnL only
    def _log_tune_rationale(self, proposal: Dict[str, float], metrics: Dict[str, float], regime: str):
        """E: 파라미터 변경 시 이유를 1줄 요약 로그로 남긴다."""
        import logging as _log
        _logger = _log.getLogger(__name__)
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
                  "momentum_min_long", "max_loss_per_position", "watch_limit"):
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

    def evaluate_and_rollback(self, metrics: Dict[str, float]):
        # ── cooldown guard: 이미 롤백된 직후엔 재롤백 금지 ─────────────────
        # 같은 음수 pnl 이벤트로 매 4초마다 rollback 폭풍이 발생하는 것을 방지
        # _bump_cooldown()이 cooldown_until을 설정하므로 이를 재활용
        if time.time() < self.state.cooldown_until:
            return
        if metrics["pnl_30m"] <= -0.02:
            now = time.time()
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
            else:
                self.current = dict(self.baseline)
                if self.lifecycle.get("active"):
                    self.lifecycle["active"]["params"] = dict(self.current)
                    self.lifecycle["active"]["updated_at"] = now
                self.lifecycle_meta["last_rollback_at"] = now
                self.lifecycle_meta["rollback_reason"] = "baseline"
                self._bump_cooldown()
                self.notifier("WARN", f"AutoTune rollback: pnl_30m={metrics['pnl_30m']:.4f} (baseline)")

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
        self._shadow_record_metrics(metrics, candidate=self.state.shadow.active)
        regime = self.classify_regime(metrics)
        proposal = self.propose_adjustment(regime, metrics)
        proposal = self.safety_guard(proposal)
        self._set_lifecycle_stage("proposed", proposal, regime, metrics, "safety_guard")
        params, applied, reason = self.apply_or_shadow(proposal, metrics, regime)
        self.evaluate_and_rollback(metrics)
        self.notifier(
            "WATCH",
            (
                f"AutoTune v1.0.1 regime={regime} conf={metrics['confidence']:.2f} "
                f"shadow={self.state.shadow.active} applied={applied} reason={reason} "
                f"params={params} pnl_30m={metrics['pnl_30m']:.4f} fails={metrics['order_failures']}"
            ),
        )
        return dict(self.current)
