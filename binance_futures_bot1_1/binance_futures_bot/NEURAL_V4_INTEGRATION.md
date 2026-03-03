# Neural Scorer v4 — tick_engine 통합 가이드

## 활성화 방법

### 1. config.py 변경 (1줄)
```python
neural_scorer_version: str = "v4"   # "v3" → "v4"
neural_scorer_enabled: bool = True  # False → True (데이터 충분할 때)
```

### 2. tick_engine.py 수정 (3곳)

#### A. import 변경 (line 23)
```python
# 기존
from .neural_scorer import NeuralScorer, build_feature_vector

# 변경 (config 기반 조건부)
_ns_version = getattr(config, 'neural_scorer_version', 'v3') if 'config' in dir() else 'v3'
if _ns_version == 'v4':
    from .neural_scorer_v4 import NeuralScorerV4 as NeuralScorer, build_feature_vector_v4 as build_feature_vector, regime_to_weight
else:
    from .neural_scorer import NeuralScorer, build_feature_vector
```

#### B. 초기화 변경 (line 217-218)
```python
# 기존
_scorer_path = os.path.join(base_dir, "neural_scorer.json")
self.neural_scorer = NeuralScorer(model_path=_scorer_path)

# 변경
if getattr(self.config, 'neural_scorer_version', 'v3') == 'v4':
    _scorer_path = os.path.join(base_dir, "neural_scorer_v4.json")
else:
    _scorer_path = os.path.join(base_dir, "neural_scorer.json")
self.neural_scorer = NeuralScorer(model_path=_scorer_path)
```

#### C. evaluate_signal에서 v4 호출 (line ~4266)
```python
# 기존 v3 호출
_feat = build_feature_vector(...)
_win_prob, _expected_roi = self.neural_scorer.predict(_feat)

# 변경 v4 호출
if getattr(self.config, 'neural_scorer_version', 'v3') == 'v4':
    _feat = build_feature_vector(
        ...,  # 기존 v3 인자들 그대로
        # v4 신규 인자 추가
        recent_win_rate=self._get_recent_win_rate(),
        recent_avg_roi=self._get_recent_avg_roi(),
        drawdown_pct=self._session_drawdown_pct,
        time_since_last_trade_min=self._time_since_last_trade_min(),
        regime_duration_min=self._get_regime_duration_min(),
        tuner_confidence=self._get_tuner_confidence(),
    )
    _win_prob, _expected_roi, _uncertainty = self.neural_scorer.predict(_feat, regime=regime)

    # v4: 불확실성 기반 추가 필터
    if _uncertainty > getattr(self.config, 'neural_v4_uncertainty_block', 0.15):
        _win_prob = 0.5  # 불확실하면 중립으로 (block 안 함, boost도 안 함)

    # v4: 레짐별 동적 block threshold
    _block_thresh = self.neural_scorer.get_dynamic_block_threshold(regime)
else:
    _feat = build_feature_vector(...)
    _win_prob, _expected_roi = self.neural_scorer.predict(_feat)
    _uncertainty = 0.0
```

#### D. record_entry에서 regime 전달
```python
# 기존
self.neural_scorer.record_entry(snap.symbol, _feat)

# 변경
self.neural_scorer.record_entry(snap.symbol, _feat, regime=regime)
```

#### E. learn_from_outcome에서 regime 전달
```python
# 기존
self.neural_scorer.learn_from_outcome(symbol, roi_percent, net_pnl=pnl_value)

# 변경
self.neural_scorer.learn_from_outcome(
    symbol, roi_percent, net_pnl=pnl_value,
    regime=self._current_regime or "chop"
)
```

### 3. 헬퍼 메서드 추가 (tick_engine.py에 추가 필요)

```python
def _get_recent_win_rate(self, window=10):
    """최근 N거래 승률."""
    # trade_history에서 최근 N건 읽기
    return self._cached_recent_win_rate  # 기존 메트릭에서 가져옴

def _get_recent_avg_roi(self, window=10):
    """최근 N거래 평균 ROI."""
    return self._cached_recent_avg_roi

def _time_since_last_trade_min(self):
    """마지막 거래 이후 경과 시간(분)."""
    if not hasattr(self, '_last_trade_ts') or self._last_trade_ts == 0:
        return 60.0
    return (time.time() - self._last_trade_ts) / 60.0

def _get_regime_duration_min(self):
    """현재 레짐 유지 시간(분)."""
    if hasattr(self, 'auto_tuner') and hasattr(self.auto_tuner, 'state'):
        entered = self.auto_tuner.state.regime_entered_ts
        if entered > 0:
            return (time.time() - entered) / 60.0
    return 5.0

def _get_tuner_confidence(self):
    """AutoTuner confidence."""
    if hasattr(self, 'auto_tuner') and hasattr(self.auto_tuner, 'state'):
        return self.auto_tuner.state.confidence
    return 0.2
```

## v3 → v4 자동 마이그레이션

v4를 처음 활성화하면 자동으로:
1. `neural_scorer.json` (v3) → `neural_scorer_v4.json` (v4) 변환
2. 20개 피처 → 26개 피처 zero-padding
3. 단일 분류 헤드 → Trend/Chop 분리 헤드 초기화
4. Replay 버퍼의 피처도 자동 확장

## 성능 비교 예상

| 항목 | v3 | v4 |
|------|----|----|
| 피처 수 | 20 | 26 |
| 아키텍처 | 48→24→1 | 64→32→(16→1)×3 |
| 레짐 인식 | 피처 값으로만 | 분리 헤드 + 가중합 |
| 불확실성 | 없음 | MC Dropout |
| Block threshold | 고정 0.25 | 레짐별 적응형 |
| Cold start | 50거래 | 30거래 |
| LR 스케줄 | 정확도 기반 | Cosine Annealing |
| 그래디언트 | 클리핑 없음 | L2 norm 클리핑 |
