# AutoTuner 재활성화 + NeuralScorer/Assistant 고도화 — 패치 설계서
## GPT 구체화 요청용 (2026-03-02)

> **목적**: 아래 내용을 GPT에게 전달하여 "코드 수준의 구체적 구현안"을 받아오기 위한 설계서.
> 각 섹션마다 [GPT에게 요청할 질문]을 명시했으므로 그대로 복사하여 활용 가능.

---

## 전제 조건

- 진입/청산 로직은 현재 상태 그대로 유지 (composite signal, ATR SL/TP, trailing stop 등)
- AutoTuner = 기본 기능(무료 포함)으로 고도화
- NeuralScorer + AI Assistant = 프리미엄 전용 차별화
- 기술 제약: Python 3.10+, numpy only (no GPU), 5초 틱 루프, JSON 파일 영속성

---

## PART 1. AutoTuner 재활성화 — 파일별 변경 명세

### 1.1 변경 대상 파일 및 역할

| 파일 | 변경 내용 |
|------|----------|
| `config.py` | 신규 설정 필드 추가 |
| `auto_tuner.py` | 핵심 로직 전면 리팩토링 |
| `tick_engine.py` | 호출 구조 변경 (propose/apply 분리) |
| `risk_limits.py` | ROC 값 하향 조정 |

---

### 1.2 config.py — 신규 설정 필드 추가

**현재 상태** (변경할 필드):
```python
auto_tune_enabled: bool = False  # → True로 변경
```

**신규 추가 필드**:
```python
# ═══════════════════════════════════════════════════════════
# AutoTuner v2 — 안정화 설정
# ═══════════════════════════════════════════════════════════

# 적용 주기: propose는 매 틱(5초), apply는 이 간격 이상일 때만
auto_tune_apply_interval_sec: int = 300       # 5분 — 핵심 진동 억제 장치

# 레짐 최소 유지시간: 이 시간 미만이면 전환 금지
regime_min_hold_sec: int = 180                # 3분

# 레짐 전환 비용: confidence 차이가 이 값 이상이어야 전환 허용
regime_switch_penalty: float = 0.08

# 롤백 쿨다운: 롤백 후 최소 대기시간
auto_tune_rollback_cooldown_sec: int = 600    # 10분

# EMA 계수 — 진입 임계치 (민감, 빠르게 반응)
tune_alpha_entry: float = 0.15

# EMA 계수 — 리스크 파라미터 (보수적, 느리게 반응)
tune_alpha_risk: float = 0.08

# risk_bias 확대(+1) 연속 충족 필요 횟수
risk_bias_confirm_count: int = 2

# confidence 가중치 (합=1.0)
conf_weight_trend: float = 0.55
conf_weight_quality: float = 0.20
conf_weight_noise: float = 0.20
conf_weight_pnl: float = 0.05
```

**위치**: `EngineConfig` 데이터클래스 내, 기존 `auto_tune_cooldown_min` 아래

---

### 1.3 auto_tuner.py — 핵심 리팩토링 상세

#### A. AutoTunerState에 추가할 필드

**현재 위치**: `auto_tuner.py` 라인 118~131, `AutoTunerState` 데이터클래스

```python
@dataclass
class AutoTunerState:
    # ... 기존 필드 유지 ...

    # ── v2 신규 필드 ──
    last_apply_ts: float = 0.0              # 마지막 실제 적용 시각
    regime_entered_ts: float = 0.0          # 현재 레짐 진입 시각
    risk_bias_confirm_streak: int = 0       # risk_bias +1 연속 충족 횟수
    prev_risk_bias: int = 0                 # 이전 risk_bias 값

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
```

#### B. compute_metrics() 변경 — EMA 메트릭 업데이트 추가

**현재 위치**: `auto_tuner.py` 라인 330~410

**변경 내용**: 메서드 끝에 EMA 업데이트 로직 추가

```python
def compute_metrics(self, ...) -> Dict[str, float]:
    # ... 기존 로직 유지 ...

    # ── v2: 다중 타임스케일 EMA 업데이트 ──
    # Fast (α ≈ 2/(60/5+1) ≈ 0.15) — 5분 상당
    alpha_fast = 0.15
    self.state.ema_tca_bps = exp_smooth(self.state.ema_tca_bps, cost_bps, alpha_fast)
    self.state.ema_failures = exp_smooth(self.state.ema_failures, float(order_failures), alpha_fast)
    self.state.ema_fill_rate = exp_smooth(self.state.ema_fill_rate, pure_fill_rate, alpha_fast)

    # Mid (α ≈ 2/(180/5+1) ≈ 0.05) — 15분 상당
    alpha_mid = 0.05
    self.state.ema_trend_score = exp_smooth(self.state.ema_trend_score, trend_score, alpha_mid)
    self.state.ema_noise_index = exp_smooth(self.state.ema_noise_index, noise_index, alpha_mid)
    self.state.ema_pass_rate = exp_smooth(self.state.ema_pass_rate, signal_pass_rate, alpha_mid)

    # Slow (α ≈ 2/(720/5+1) ≈ 0.014) — 60분 상당
    alpha_slow = 0.014
    self.state.ema_pnl = exp_smooth(self.state.ema_pnl, pnl_frac, alpha_slow)

    # ── v2: confidence 재계산 (다중 타임스케일 기반) ──
    wT = getattr(self.config, 'conf_weight_trend', 0.55)
    wQ = getattr(self.config, 'conf_weight_quality', 0.20)
    wN = getattr(self.config, 'conf_weight_noise', 0.20)
    wP = getattr(self.config, 'conf_weight_pnl', 0.05)

    trend_comp = clamp(abs(self.state.ema_trend_score) / 2.0, 0.0, 1.0)
    quality_comp = clamp((self.state.ema_pass_rate + self.state.ema_fill_rate) / 2.0, 0.0, 1.0)
    noise_comp = clamp(self.state.ema_noise_index / 0.01, 0.0, 1.0)
    pnl_comp = clamp((self.state.ema_pnl + 0.01) / 0.02, 0.0, 1.0)  # -1%~+1% → 0~1

    confidence = clamp(wT * trend_comp + wQ * quality_comp - wN * noise_comp + wP * pnl_comp, 0.0, 1.0)

    # 부트스트랩 보정 유지
    if len(returns) < 10:
        confidence = max(confidence, 0.20)

    metrics["confidence"] = confidence
    self.state.confidence = confidence
    return metrics
```

#### C. classify_regime() 변경 — 전환 비용 + 최소 유지시간

**현재 위치**: `auto_tuner.py` 라인 412~449

**변경 후 로직**:
```python
def classify_regime(self, metrics: Dict[str, float]) -> str:
    # ... 기존 score/noise/cand 계산 유지 ...
    # ... 기존 히스테리시스 hit 카운팅 유지 ...

    # ── v2: 전환 비용 + 최소 유지시간 ──
    now = time.time()
    current = h.current_regime
    min_hold = float(getattr(self.config, 'regime_min_hold_sec', 180))
    switch_penalty = float(getattr(self.config, 'regime_switch_penalty', 0.08))

    # 히스테리시스 통과한 후보 레짐 결정 (기존 로직)
    candidate = current  # 기본은 유지
    if h.up_hits >= required:
        candidate = "trend_up"
    elif h.down_hits >= required:
        candidate = "trend_down"
    elif h.chop_hits >= required:
        candidate = "chop"

    # 전환 시도 시 추가 검증
    if candidate != current:
        time_in_regime = now - self.state.regime_entered_ts

        # 조건1: 최소 유지시간 충족
        if time_in_regime < min_hold:
            return current  # 전환 거부

        # 조건2: confidence 차이가 switch_penalty 이상
        # (현재 confidence는 전환 후보 레짐 맥락에서의 값)
        if metrics["confidence"] < self.state.confidence + switch_penalty:
            return current  # 전환 거부 (확신 부족)

        # 전환 승인
        h.current_regime = candidate
        self.state.regime_entered_ts = now

    return h.current_regime
```

#### D. propose_adjustment() 변경 — 스텝 점프 → 목표값(target) 저장

**현재 위치**: `auto_tuner.py` 라인 467~574

**핵심 변경**: 직접 `current + step` 하는 대신, `target` 딕셔너리에 목표값을 저장

```python
def propose_adjustment(self, regime: str, metrics: Dict[str, float]) -> Dict[str, float]:
    targets = dict(self.baseline)  # baseline에서 시작

    # ── 레짐별 목표값 설정 ──
    if regime == "trend_up":
        targets["momentum_min_long"] = self.baseline["momentum_min_long"] + 0.0006
        targets["momentum_min_short"] = self.baseline["momentum_min_short"]  # 회귀
    elif regime == "trend_down":
        targets["momentum_min_short"] = self.baseline["momentum_min_short"] - 0.0006
        targets["momentum_min_long"] = self.baseline["momentum_min_long"]   # 회귀
    else:  # chop
        targets["momentum_min_long"] = self.baseline["momentum_min_long"]
        targets["momentum_min_short"] = self.baseline["momentum_min_short"]
        targets["volatility_min"] = self.baseline["volatility_min"] + 0.0006
        targets["position_pct"] = self.baseline["position_pct"] - 0.0010

    # ── 고노이즈 오버라이드 ──
    if self.state.ema_noise_index > 0.014:
        targets["volatility_min"] = self.baseline["volatility_min"] + 0.0010
        targets["position_pct"] = self.baseline["position_pct"] - 0.0010

    # ── TCA 비용 높음 오버라이드 ──
    if self.state.ema_tca_bps >= 8.0:
        targets["position_pct"] = self.baseline["position_pct"] - 0.0015
        targets["momentum_min_long"] = targets.get("momentum_min_long", self.baseline["momentum_min_long"]) + 0.0004
        targets["momentum_min_short"] = targets.get("momentum_min_short", self.baseline["momentum_min_short"]) - 0.0004

    # risk_bias는 그대로 유지 (별도 메서드로 분리)
    risk_bias = self._compute_risk_bias(metrics)
    if risk_bias > 0:
        targets["position_pct"] = self.baseline["position_pct"] + 0.0010
        targets["leverage_max"] = self.baseline["leverage_max"] + 1.0
    elif risk_bias < 0:
        targets["position_pct"] = self.baseline["position_pct"] - 0.0010
        targets["leverage_max"] = max(5.0, self.baseline["leverage_max"] - 1.0)

    # watch_limit/max_open_symbols 제외 (기존 유지)
    targets.pop("watch_limit", None)
    targets.pop("max_open_symbols", None)

    self.state.targets = targets
    return targets
```

#### E. 신규 메서드: _compute_risk_bias() — 연속 확인 로직 포함

```python
def _compute_risk_bias(self, metrics: Dict[str, float]) -> int:
    """v2: risk_bias 확대(+1)는 연속 2회 충족 필요."""
    confidence = metrics["confidence"]
    pnl = self.state.ema_pnl
    failures = self.state.ema_failures

    confirm_needed = int(getattr(self.config, 'risk_bias_confirm_count', 2))

    if confidence < 0.40 or pnl <= -0.004 or failures >= 1.2:
        self.state.risk_bias_confirm_streak = 0
        return -1

    if confidence > 0.75 and pnl >= 0 and failures < 0.5:
        self.state.risk_bias_confirm_streak += 1
        if self.state.risk_bias_confirm_streak >= confirm_needed:
            return 1
        return 0  # 아직 확인 부족 → 유지

    self.state.risk_bias_confirm_streak = 0
    return 0
```

#### F. 신규 메서드: apply_targets() — EMA 수렴 + ROC 캡

**핵심 변경점**: 기존 `apply_or_shadow()`를 대체하는 새로운 적용 메서드

```python
def apply_targets(self, metrics: Dict[str, float], regime: str) -> Tuple[Dict[str, float], bool, str]:
    """v2: 목표값(targets)에 EMA 수렴으로 파라미터 적용.
    apply_interval을 충족해야만 실제 적용."""

    now = time.time()
    apply_interval = float(getattr(self.config, 'auto_tune_apply_interval_sec', 300))

    # 적용 주기 미충족 → propose만 저장
    if (now - self.state.last_apply_ts) < apply_interval:
        return dict(self.current), False, "waiting_apply_interval"

    # 최소 신뢰도 체크 (기존 유지)
    confidence = metrics.get("confidence", 0.0)
    if confidence < 0.15:
        return dict(self.current), False, f"low_confidence({confidence:.2f})"

    # 일일 튜닝 횟수 체크 (기존 유지)
    dk = self._day_key()
    if self.state.tune_day_key != dk:
        self.state.tune_day_key = dk
        self.state.tune_count_today = 0
    if self.state.tune_count_today >= self.max_tunes_per_day:
        return dict(self.current), False, "daily_limit"

    # ── EMA 수렴 적용 ──
    targets = self.state.targets
    if not targets:
        return dict(self.current), False, "no_targets"

    alpha_entry = float(getattr(self.config, 'tune_alpha_entry', 0.15))
    alpha_risk = float(getattr(self.config, 'tune_alpha_risk', 0.08))

    # 파라미터별 EMA 계수 매핑
    alpha_map = {
        "momentum_min_long": alpha_entry,
        "momentum_min_short": alpha_entry,
        "volatility_min": alpha_entry,
        "position_pct": alpha_risk,
        "leverage_min": alpha_risk,
        "leverage_max": alpha_risk,
        "max_loss_per_position": alpha_risk,
    }

    # ROC 캡 (기존 max_step보다 작게)
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

    for key, target_val in targets.items():
        if key not in alpha_map:
            continue
        cur_val = self.current.get(key)
        if cur_val is None:
            continue

        alpha = alpha_map[key]
        smoothed = exp_smooth(float(cur_val), float(target_val), alpha)

        # ROC 캡 적용
        cap = roc_caps.get(key, 999)
        delta = smoothed - float(cur_val)
        if abs(delta) > cap:
            smoothed = float(cur_val) + math.copysign(cap, delta)

        if abs(smoothed - float(cur_val)) > 1e-7:
            changed = True
        new_params[key] = smoothed

    if not changed:
        return dict(self.current), False, "no_change"

    # clamp_params 적용 (기존 safety_guard의 절대 범위)
    new_params = clamp_params(new_params, self.current, DEFAULT_LIMITS)

    # leverage 역전 방지
    if "leverage_min" in new_params and "leverage_max" in new_params:
        if new_params["leverage_min"] > new_params["leverage_max"] - 1:
            new_params["leverage_min"] = max(1.0, new_params["leverage_max"] - 1)

    # 롤백 스택 저장 & 적용
    self.state.rollback_stack.append((dict(self.current), f"apply@{now:.0f}"))
    self._log_tune_rationale(new_params, metrics, regime)
    self.current = dict(new_params)
    self.state.last_apply_ts = now
    self._bump_daily_counter()

    # lifecycle 업데이트
    self._set_lifecycle_stage("active", new_params, regime, metrics, "applied_v2")
    self._clear_stage("staged")
    self._clear_stage("proposed")

    return dict(self.current), True, "applied"
```

#### G. run_cycle() 변경 — propose/apply 분리

**현재 위치**: `auto_tuner.py` 라인 817~891

```python
def run_cycle(self, ...) -> Dict[str, float]:
    metrics = self.compute_metrics(...)          # 매 틱: EMA 메트릭 업데이트 포함
    regime = self.classify_regime(metrics)        # 매 틱: 전환 비용 포함
    targets = self.propose_adjustment(regime, metrics)  # 매 틱: 목표값만 저장

    # apply는 간격 충족 시에만 실행 (내부에서 체크)
    params, applied, reason = self.apply_targets(metrics, regime)

    self.evaluate_and_rollback(metrics)  # 기존 롤백 유지

    self.notifier("WATCH", f"AutoTune v2 regime={regime} conf={metrics['confidence']:.2f} "
                  f"applied={applied} reason={reason}")
    return dict(self.current)
```

---

### 1.4 tick_engine.py — 호출 구조 변경

#### A. `_run_auto_tuner_cycle()` (라인 1991~2077)

**변경 사항**: 기존 로직 대부분 유지하되, `auto_tune_enabled` 기본값 변경 인식

```python
async def _run_auto_tuner_cycle(self, snapshots):
    if not self.auto_tuner or not getattr(self.config, "auto_tune_enabled", False):
        return
    # ... 기존 메트릭 수집 로직 전부 유지 ...
    # ... tuner_kwargs 구성 유지 ...

    try:
        params = self.auto_tuner.run_cycle(**tuner_kwargs)
    except TypeError:
        # legacy fallback 유지
        params = self.auto_tuner.run_cycle(**legacy_kwargs)

    self._apply_auto_tune_params(params)
    self._check_session_loss_limit(pnl_fast)
    self._maybe_trigger_auto_tune_rollback(pnl_slow_realized, order_failures)
    self._persist_auto_tuner_state()
```

**핵심**: tick_engine 쪽은 거의 변경 없음. AutoTuner 내부에서 propose/apply 분리를 처리하므로.

#### B. `_persist_auto_tuner_state()` (라인 886~) — 신규 필드 저장 추가

```python
def _persist_auto_tuner_state(self):
    if not self.auto_tuner:
        return
    payload = {
        # ... 기존 필드 유지 ...

        # ── v2 신규 필드 ──
        "last_apply_ts": self.auto_tuner.state.last_apply_ts,
        "regime_entered_ts": self.auto_tuner.state.regime_entered_ts,
        "ema_metrics": {
            "tca_bps": self.auto_tuner.state.ema_tca_bps,
            "failures": self.auto_tuner.state.ema_failures,
            "fill_rate": self.auto_tuner.state.ema_fill_rate,
            "trend_score": self.auto_tuner.state.ema_trend_score,
            "noise_index": self.auto_tuner.state.ema_noise_index,
            "pnl": self.auto_tuner.state.ema_pnl,
        },
        "targets": self.auto_tuner.state.targets,
    }
    # ... 기존 저장 로직 ...
```

#### C. `_load_auto_tuner_state()` (라인 790~) — 신규 필드 복원 추가

```python
# 기존 로직 끝에 추가:
last_apply_ts = data.get("last_apply_ts")
if isinstance(last_apply_ts, (int, float)):
    tuner.state.last_apply_ts = float(last_apply_ts)

regime_entered_ts = data.get("regime_entered_ts")
if isinstance(regime_entered_ts, (int, float)):
    tuner.state.regime_entered_ts = float(regime_entered_ts)

ema_m = data.get("ema_metrics") or {}
if isinstance(ema_m, dict):
    tuner.state.ema_tca_bps = float(ema_m.get("tca_bps", 0.0))
    tuner.state.ema_failures = float(ema_m.get("failures", 0.0))
    tuner.state.ema_fill_rate = float(ema_m.get("fill_rate", 1.0))
    tuner.state.ema_trend_score = float(ema_m.get("trend_score", 0.0))
    tuner.state.ema_noise_index = float(ema_m.get("noise_index", 0.0))
    tuner.state.ema_pnl = float(ema_m.get("pnl", 0.0))

targets = data.get("targets")
if isinstance(targets, dict):
    tuner.state.targets = {k: float(v) for k, v in targets.items()}
```

---

### 1.5 risk_limits.py — ROC 값 하향 조정

**현재 위치**: `risk_limits.py` 라인 19~32

```python
DEFAULT_LIMITS = {
    "position_pct": ParamLimit("position_pct", (0.03, 0.08), 0.0010),        # 0.003 → 0.0010
    "leverage_min": ParamLimit("leverage_min", (1.0, 5.0), 0.5),             # 유지
    "leverage_max": ParamLimit("leverage_max", (3.0, 12.0), 1.0),            # 유지
    "max_loss_per_position": ParamLimit("max_loss_per_position", (0.5, 2.2), 0.10),  # 0.1 유지
    "watch_limit": ParamLimit("watch_limit", (3, 20), 1.0),                  # 유지 (tune 대상 아님)
    "max_open_symbols": ParamLimit("max_open_symbols", (2, 12), 0.5),        # 유지 (tune 대상 아님)
    "momentum_min_long": ParamLimit("momentum_min_long", (-0.006, 0.006), 0.0006),   # 0.0008 → 0.0006
    "momentum_min_short": ParamLimit("momentum_min_short", (-0.006, 0.006), 0.0006), # 0.0008 → 0.0006
    "volatility_min": ParamLimit("volatility_min", (0.001, 0.015), 0.0006),  # 0.001 → 0.0006
}
```

---

## PART 2. NeuralScorer 프리미엄 고도화 설계

### 2.1 권한 분리 원칙

```
┌─────────────────────────────────────────────────┐
│              tick_engine.py (5초 루프)             │
│                                                   │
│  ① AutoTuner.run_cycle()                         │
│     → regime, params, confidence 결정             │
│     → config 파라미터 적용                         │
│                                                   │
│  ② evaluate_signal()                             │
│     └─ [프리미엄] NeuralScorer.predict()          │
│        → 진입 게이팅만 (파라미터 변경 금지)         │
│                                                   │
│  AutoTuner 출력이 Neural 입력에 영향:             │
│  - regime → 피처[8] regime_num                    │
│  - confidence → 직접 전달하지 않음 (독립성 유지)    │
│                                                   │
│  Neural 출력이 AutoTuner에 영향하지 않음:          │
│  - p_win, roi_hat → 진입 게이팅 + 레버리지만       │
│  - AutoTuner의 position_pct/leverage 조정 금지     │
└─────────────────────────────────────────────────┘
```

### 2.2 NeuralScorer 개선 방향

#### A. 즉시 적용 가능한 개선 (현재 아키텍처 유지)

| 항목 | 현재 | 개선안 |
|------|------|-------|
| 블록 임계값 | `p_win < 0.25` | `p_win < 0.35` AND `roi_hat < 0.0` (듀얼 조건) |
| 냉각 기간 | 50건 | 30건으로 단축 (v3 피처 충분) |
| 강도 배율 | `p_win * 2.0 * roi_adj` | `0.5 + p_win * 1.0 * roi_adj` (최소 0.5 보장) |
| Replay 샘플링 | 균등 win/loss | 최근 데이터 가중치 (time-weighted) |
| 학습률 | 고정 0.003 시작 | 워밍업: 처음 100건 lr=0.005 → 이후 적응형 |

#### B. 중기 개선 (아키텍처 확장, numpy 범위 내)

1. **피처 확장 (20 → 25~28개)**
   - `oi_change_pct`: OI 변화율 (Binance /fapi/v1/openInterest)
   - `orderbook_imbalance`: 오더북 bid/ask 불균형
   - `btc_correlation`: BTC 5분 수익률과의 상관계수
   - `regime_duration`: 현재 레짐 유지 시간 (AutoTuner에서 전달)
   - `autotuner_confidence`: AutoTuner confidence (간접 연동)

2. **Dropout 구현 (numpy)**
   ```python
   def _forward_with_dropout(self, X, dropout_rate=0.1, training=False):
       # training 시에만 dropout 적용
       if training:
           mask1 = (np.random.random(a1.shape) > dropout_rate) / (1 - dropout_rate)
           a1 = a1 * mask1
       # ...
   ```

3. **앙상블 (MLP × 3)**
   - 동일 아키텍처 3개를 다른 시드로 초기화
   - 예측 시 3개 출력의 중앙값 사용
   - 학습 시 각각 독립적으로 학습
   - 3개 중 2개 이상 block이면 진입 거부

#### C. 장기 개선 (별도 프로세스, 선택적)

- 과거 trade_history.jsonl 기반 오프라인 pre-training
- 모델 체크포인트 비교 & 자동 폴백
- A/B 테스트 프레임워크 (50% 확률로 neural ON/OFF → 성과 비교)

### 2.3 tick_engine.py 내 Neural 통합 변경점

**현재 위치**: `tick_engine.py` 라인 4074~4157

**변경 사항** (evaluate_signal 내부):
```python
# 현재:
if _win_prob < _block_thresh:
    return None, "NEURAL BLOCK: ..."

# 개선:
_block_prob = float(getattr(self.config, "neural_block_threshold", 0.35))  # 0.25→0.35
if _neural_status["ready"]:
    # 듀얼 조건 블록: 승률 AND 기대ROI 모두 나빠야 블록
    if _win_prob < _block_prob and _expected_roi < 0.0:
        return None, f"NEURAL BLOCK: P(win)={_win_prob:.1%} < {_block_prob:.0%} AND E[ROI]={_expected_roi:+.2f}%"

    # 강도 배율: 최소 0.5 보장 (Neural이 약해도 완전 차단하지 않음)
    _roi_adj = 1.0 + max(-0.3, min(0.3, _expected_roi / 10.0))
    _neural_mult = max(0.5, min(1.8, 0.5 + _win_prob * 1.0 * _roi_adj))
    strength = min(strength * _neural_mult, 5.0)
```

---

## PART 3. AI Assistant 프리미엄 설계

### 3.1 권한 모델

```
┌──────────────────────────────────────┐
│         AI Assistant (프리미엄)         │
│                                        │
│  READ (자동):                          │
│  - AutoTuner: regime, confidence,      │
│    params history, rollback history    │
│  - NeuralScorer: accuracy, p_win,      │
│    roi_hat, n_trained, feature stats   │
│  - Engine: pass_rate, fill_rate,       │
│    pnl, open positions, trade log     │
│                                        │
│  WRITE (사용자 확인 필요):              │
│  - "추천안" 생성만 (GUI에 표시)          │
│  - 자동 적용 절대 금지                   │
│                                        │
│  호출 빈도:                             │
│  - 이벤트 기반 (사용자 클릭)             │
│  - 자동 요약: 30분에 1회                 │
└──────────────────────────────────────┘
```

### 3.2 AI Advisor 확장 (ai_advisor.py)

**현재 `TradeAnalyzer.get_improvement_suggestions()`를 확장하여 프리미엄 인사이트 추가**

#### 신규 메서드 추가:

```python
class TradeAnalyzer:
    # ... 기존 유지 ...

    def get_premium_insights(self, engine_state: dict) -> List[Dict[str, Any]]:
        """[프리미엄] AutoTuner + Neural 상태를 종합한 심층 인사이트."""
        insights = []

        # 1. 진입 미발생 원인 분석
        regime = engine_state.get("regime", "unknown")
        conf = engine_state.get("confidence", 0.0)
        noise = engine_state.get("noise_index", 0.0)
        neural_acc = engine_state.get("neural_accuracy", 0.0)
        pass_rate = engine_state.get("pass_rate", 0.0)

        if pass_rate < 0.1:
            # 왜 진입 안 하는지 진단
            reasons = []
            if noise > 0.012:
                reasons.append("noise_index 높음 → volatility_min 상향 중")
            if conf < 0.4:
                reasons.append(f"confidence {conf:.0%} 낮음 → risk_bias -1")
            # ... 등
            insights.append({
                "type": "entry_diagnosis",
                "priority": "high",
                "msg_ko": f"진입 미발생 원인: {', '.join(reasons)}",
            })

        # 2. 1시간 성과 리포트
        # 3. 사용자 액션 추천
        # ...
        return insights
```

#### engine_state 전달 지점 (tick_engine.py → ai_advisor):

```python
# tick_engine.py에서 AI Advisor에 상태 전달
def _get_advisor_engine_state(self) -> dict:
    """AI Advisor에 전달할 엔진 상태 스냅샷."""
    return {
        "regime": getattr(self.auto_tuner.state.hysteresis, "current_regime", "chop") if self.auto_tuner else "chop",
        "confidence": self.auto_tuner.state.confidence if self.auto_tuner else 0.0,
        "noise_index": self.auto_tuner.state.ema_noise_index if self.auto_tuner else 0.0,
        "pass_rate": self._flow_ratio("passed_signal", "evaluated_total"),
        "fill_rate": self._flow_ratio("fill_ok", "order_sent"),
        "neural_accuracy": self.neural_scorer.status().get("accuracy", 0.0),
        "neural_n_trained": self.neural_scorer.status().get("n_trained", 0),
        "open_positions": len(self._open_symbols),
        "auto_tune_mode": getattr(self.auto_tuner, "current_mode", "balanced") if self.auto_tuner else "balanced",
    }
```

---

## PART 4. 활성화 순서 및 테스트 체크리스트

### 4.1 Phase 1: AutoTuner 재활성화 (무료)

```
Step 1: config.py 수정
  - auto_tune_enabled = True
  - 신규 v2 설정 필드 추가

Step 2: auto_tuner.py 리팩토링
  - AutoTunerState 확장
  - compute_metrics() EMA 추가
  - classify_regime() 전환 비용 추가
  - propose_adjustment() → target 방식
  - apply_or_shadow() → apply_targets() 교체
  - run_cycle() 변경

Step 3: risk_limits.py ROC 하향

Step 4: tick_engine.py
  - _persist/_load에 신규 필드 추가

Step 5: 테스트넷 3일 검증
  - 파라미터 진동 없이 안정적 수렴 확인
  - 레짐 전환 빈도 < 시간당 3회
  - 일일 apply 횟수 ≤ 6회
```

### 4.2 Phase 2: NeuralScorer 고도화 (프리미엄)

```
Step 1: 블록 조건 듀얼화 + 강도 배율 조정
Step 2: Dropout 구현
Step 3: 피처 5개 추가 (OI, 오더북, BTC상관, 레짐시간, confidence)
Step 4: 앙상블 (×3) 구현
Step 5: 테스트넷 1주일 검증
```

### 4.3 Phase 3: AI Assistant (프리미엄)

```
Step 1: get_premium_insights() 구현
Step 2: GUI에 인사이트 패널 추가
Step 3: 30분 자동 요약 스케줄러
```

---

## PART 5. GPT에게 전달할 핵심 질문 목록

### AutoTuner 관련
1. `apply_interval=300초(5분)`이 적절한가? 시장 레짐 변화 속도 대비 너무 느리지 않은가?
2. EMA α값(entry=0.15, risk=0.08)의 이론적 근거와 최적 범위는?
3. `regime_switch_penalty=0.08`의 적정 수준은? confidence 스케일(0~1) 대비 8%p가 적절한가?
4. 베이지안 최적화(Bayesian Optimization)로 target을 결정하는 접근법은 5초 루프 + numpy only 제약에서 가능한가?
5. 레짐 분류에 Hidden Markov Model(HMM) 도입 가능성은? numpy로 구현 가능한 경량 버전이 있는가?

### NeuralScorer 관련
6. 2-layer MLP(48/24)에서 Dropout rate 최적값은? 온라인 학습에서 dropout 효과가 있는가?
7. 앙상블 3개 모델의 메모리/연산 비용은 5초 제약에 맞는가?
8. OI(미결제약정) 데이터를 피처로 추가할 때, Binance API 호출 빈도 영향은?
9. Time-weighted Replay 샘플링의 구체적 가중치 함수는? (지수 감쇠? 선형?)
10. 오프라인 pre-training 시 과적합 방지를 위한 train/validation 분할 비율은?

### 프리미엄 차별화 관련
11. AutoTuner를 무료/프리미엄으로 나눈다면, 어떤 기능을 프리미엄 전용으로 해야 가치가 있는가?
12. 중앙 서버에서 학습한 모델을 배포하는 Federated Learning 접근은 실용적인가?
13. A/B 테스트 프레임워크의 최소 샘플 수(통계적 유의성)는 거래 몇 건인가?

---

*이 설계서를 GPT에게 전달하면, 각 파일/메서드 단위로 구체적인 코드 구현안을 받아올 수 있습니다.*
*명세서(GPT_전달용_프로젝트_구조_명세서.md)와 함께 전달하세요.*
