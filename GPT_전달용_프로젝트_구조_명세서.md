# Binance Futures Trading Bot — 프로젝트 구조 명세서
## GPT 개선안 요청용 기술 문서 (2026-03-02)

---

## 1. 프로젝트 개요

바이낸스 선물 자동매매 봇으로, Python 기반 비동기(asyncio) 틱 엔진을 핵심으로 한다.
현재 v1.1 상태이며, GUI(PyQt 기반)와 결합하여 사용자에게 배포 중이다.

### 1.1 핵심 목적
- 바이낸스 USDT-M 선물 시장에서 다중 심볼 동시 트레이딩
- 모멘텀 + 변동성 + 멀티타임프레임(MTF) 복합 시그널 기반 진입
- ATR 기반 손절/익절 + 트레일링 스탑 청산
- 프리미엄 사용자에게 신경망(NeuralScorer) 기반 고도화된 진입 필터 제공

### 1.2 디렉토리 구조
```
AUTO BOT/
├── bot_gui.py                     # GUI 메인 (PyQt)
├── gui_config.json                # GUI 설정 파일
├── license_gate.py                # 라이선스 관리
├── requirements.txt
├── binance_futures_bot1_1/
│   ├── main.py                    # 엔진 시작점
│   └── binance_futures_bot/
│       ├── __init__.py
│       ├── tick_engine.py         # ★ 핵심 엔진 (4768 라인)
│       ├── config.py              # EngineConfig 데이터클래스
│       ├── auto_tuner.py          # ★ AutoTuner (891 라인) — 현재 비활성
│       ├── neural_scorer.py       # ★ NeuralScorer v3.0 (783 라인) — 현재 비활성
│       ├── ai_advisor.py          # AI Advisor (682 라인) — GUI 이벤트 번역/분석
│       ├── engine_helpers.py      # TP/SL/트레일링 스탑 계산 유틸
│       ├── exchange_utils.py      # 바이낸스 API 유틸
│       ├── risk_limits.py         # 파라미터 클램프/안전 범위
│       ├── position_snapshot.py   # 포지션 스냅샷 데이터
│       └── snapshot_manager.py    # 스냅샷 빌더
```

---

## 2. 현재 활성/비활성 기능 상태

| 모듈 | 현재 상태 | config 키 | 비활성화 사유 |
|------|----------|-----------|-------------|
| **AutoTuner** | ❌ 비활성 | `auto_tune_enabled = False` | [PATCH-16] 16샘플 미니배치로 노이즈 학습 문제 |
| **NeuralScorer v3** | ❌ 비활성 | `neural_scorer_enabled = False` | 프리미엄 전용, 라이선스 키 필요 |
| **Time Stop** | ❌ 비활성 | `enable_time_stop = False` | [PATCH-16] 이전 분석 6건 전부 손실 |
| **Signal Decay Exit** | ❌ 비활성 | `enable_signal_decay_exit = False` | [PATCH-16] 횡보 구간 수익 포지션 조기 청산 |
| **Progress Stop** | ❌ 비활성 | `enable_progress_stop = False` | [PATCH-16] 30분 타이머가 수익 포지션 조기 청산 |
| RSI Filter | ❌ 비활성 | `rsi_filter_enabled = False` | 하드 필터 대신 chop 소프트 스코어링으로 대체 |
| Composite Signal | ✅ 활성 | `composite_signal_enabled = True` | 핵심 진입 로직 |
| Take Profit (R-Multiple) | ✅ 활성 | `enable_take_profit = True` | TP1: 1.5R, TP2: 2.5R |
| ATR Trailing Stop | ✅ 활성 | `enable_atr_trailing_stop = True` | ATR 1.7배 |
| Kelly Sizing | ✅ 활성 | `kelly_sizing_enabled = True` | 실적 기반 동적 포지션 |
| Maker-First Entry | ✅ 활성 | `maker_first_enabled = True` | 수수료 절감 |
| Funding Filter | ✅ 활성 | `funding_filter_enabled = True` | 펀딩 역방향 페널티 |
| Spike Guard | ✅ 활성 | `spike_guard_enabled = True` | 급변동 진입 차단 |

---

## 3. AutoTuner 상세 명세 (auto_tuner.py — 892 라인)

### 3.1 아키텍처 개요
AutoTuner는 **실시간 시장 메트릭을 기반으로 엔진 파라미터를 자동 조정**하는 모듈이다.

```
[tick_engine 4초 루프]
    │
    ▼
compute_metrics()  ← returns, rv30, atr30, pass_rate, fill_rate, pnl_30m, TCA 등
    │
    ▼
classify_regime()  → "trend_up" | "trend_down" | "chop"
    │
    ▼
propose_adjustment()  → 파라미터 변경 제안
    │
    ▼
safety_guard()  → 클램프 + ROC 제한 + 일일 튜닝 횟수 제한
    │
    ▼
apply_or_shadow()  → Shadow 모드(검증) 또는 즉시 적용
    │
    ▼
evaluate_and_rollback()  → pnl_30m <= -2% 시 롤백
```

### 3.2 핵심 메트릭 (compute_metrics)
입력 메트릭 목록:
- `returns`: 최근 5분 수익률 리스트
- `rv30`: 30분 실현 변동성
- `atr30`: 30분 ATR 추정
- `pass_rate`, `entry_rate`, `fill_rate`: 파이프라인 통과율
- `signal_pass_rate`, `execution_pass_rate`, `pure_fill_rate`: 3단계 흐름 비율
- `blocked_ratelimit/cooldown/spike_guard/portfolio_cap`: 차단 비율
- `pnl_30m`: 30분 실현 PnL
- `order_failures`: 주문 실패 건수
- `pnl_fast`: 빠른 PnL (미실현)
- `pnl_slow_realized/funding/fee/other`: Binance Income API 분해
- `spread_bps_med/p90`: 스프레드 통계
- `slippage_bps_med/p90`: 슬리피지 통계
- `tca_spread_bps_med/p90`, `tca_samples`: TCA 메트릭

계산되는 파생 지표:
- `mu`: 수익률 평균
- `sigma_eff`: MAD 기반 강건한 변동성 (Median Absolute Deviation × 1.4826)
- `trend_score`: mu / sigma_eff (방향 포함)
- `noise_index`: sigma_eff - |mu|
- `confidence`: 0.6×트렌드 + 0.3×품질 - 0.3×노이즈 (0~1)

### 3.3 레짐 분류 (classify_regime)
- **trend_up**: trend_score > 0.8 AND noise_index < 0.012
- **trend_down**: trend_score < -0.8 AND noise_index < 0.012
- **chop**: 그 외

히스테리시스(Hysteresis) 적용:
- 기본 2회 연속 같은 레짐이어야 전환
- |trend_score| >= 1.2인 강한 신호는 1회로 즉시 전환

### 3.4 파라미터 조정 (propose_adjustment)
**조정 대상 파라미터 및 현재 범위:**

| 파라미터 | 설명 | 기본값 | 클램프 범위 | 스텝 |
|---------|------|-------|-----------|------|
| `momentum_min_long` | 롱 진입 최소 모멘텀 | 0.005 | 0.001~0.006 | 0.0008 |
| `momentum_min_short` | 숏 진입 최소 모멘텀 | -0.0055 | -0.006~0.006 | 0.0008 |
| `volatility_min` | 최소 변동성 | 0.003 | 0.001~0.006 | 0.001 |
| `position_pct` | 포지션 비율 | 0.06 (6%) | 0.03~0.08 | 0.0015 |
| `leverage_min` | 최소 레버리지 | 1 | 1~5 | 0.5 |
| `leverage_max` | 최대 레버리지 | 10 | 3~12 | 1.0 |
| `max_loss_per_position` | 포지션당 최대 손실% | 1.8 | 0.5~2.2 | 0.3 |

**조정 로직:**
- **trend_up**: momentum_min_long ↑ (추세 추종 강화), momentum_min_short → baseline 회귀
- **trend_down**: momentum_min_short ↓ (숏 강화), momentum_min_long → baseline 회귀
- **chop**: 양쪽 모두 baseline으로 지수 평활(α=0.3~0.5)
- **고노이즈**: volatility_min ↑ (진입 기준 강화)
- **TCA 비용 높음(≥8bps)**: de-risk + momentum 요구 강화

**risk_bias 결정 (포지션/레버리지 조정):**
- `+1` (확대): confidence > 0.7 AND pnl ≥ 0 AND failures == 0
- `-1` (축소): confidence < 0.35 OR pnl ≤ -0.6% OR failures ≥ 2
- `0` (유지): 그 외 → baseline 방향 지수 평활

### 3.5 안전장치 (safety_guard)
- `clamp_params()`: risk_limits.py의 DEFAULT_LIMITS로 절대 범위 + ROC 제한
- 일일 최대 튜닝 횟수: 6회 (max_tunes_per_day)
- 같은 방향 연속 3회 이상 변경 시 쿨다운 (20분)
- leverage_min > leverage_max - 1 역전 방지

### 3.6 Shadow 모드 (검증 메커니즘)
- 새 파라미터를 즉시 적용하지 않고 **최소 5사이클(shadow_min_cycles) 동안 모니터링**
- 승격 조건:
  - confidence 유지 (기존 대비 -0.05 이내)
  - order_failures 증가하지 않음 (기존 대비 +0.5 이내)
  - fill_rate 유지 (기존 대비 -0.05 이내)
  - noise_index 악화 안 됨 (기존 대비 +0.002 이내)
  - 후보 기대값(expectancy) ≥ 0
  - fill_rate ≥ 80%
- 실패 시: 기존 파라미터 유지, shadow 레코드 폐기

### 3.7 롤백 메커니즘
- `pnl_30m <= -2%` 시 이전 파라미터 스택에서 복원
- 스택 비어있으면 baseline으로 롤백
- 롤백 후 쿨다운 적용 (20분)

### 3.8 모드 프로파일 (3종)
| 모드 | position_pct | step_scale | risk_bias_up | risk_bias_down |
|------|-------------|-----------|-------------|---------------|
| aggressive | 0.03~0.08 | 1.4 | 0.62 | 0.32 |
| balanced | 0.03~0.08 | 1.0 | 0.70 | 0.35 |
| conservative | 0.03~0.08 | 0.7 | 0.80 | 0.45 |

### 3.9 비활성화 사유 및 문제점
**[PATCH-16] 비활성화 이유:**
- 16샘플 미니배치로 노이즈가 많은 시장 데이터를 학습
- 파라미터 진동(oscillation) → 빈번한 변경이 오히려 성능 저하
- watch_limit, max_open_symbols를 줄여 진입 기회 자체를 차단하는 문제 (이후 조정 제외됨)
- 롤백 폭풍: 같은 PnL 이벤트로 매 4초마다 롤백 발생 (이후 cooldown guard 추가)

**현재까지의 패치 히스토리:**
- watch_limit/max_open_symbols를 tune 대상에서 완전 제외
- 최소 신뢰도 0.15 미만 시 적용 차단
- Shadow 재활성화 로직 (신뢰도 하락 시)
- 부트스트랩 보정 (데이터 부족 시 confidence 최소 0.20)

---

## 4. NeuralScorer v3.0 상세 명세 (neural_scorer.py — 784 라인)

### 4.1 아키텍처
**듀얼헤드 경량 신경망 — GPU/PyTorch 없이 numpy만 사용**

```
Input(20 features)
    │
    ▼
Dense(48 neurons, LeakyReLU)
    │
    ▼
Dense(24 neurons, LeakyReLU)
    │
    ├──→ Head1: Dense(1, Sigmoid) → P(win)     [분류: 승률 예측]
    └──→ Head2: Dense(1, Linear)  → E[ROI%]    [회귀: 기대수익 예측]
```

### 4.2 피처 벡터 (20개, z-score 정규화)

| # | 피처 | 설명 | 출처 |
|---|------|------|------|
| 0 | rel_momentum | momentum_5m / volatility | tick data |
| 1 | volatility | 변동성 절댓값 | tick data |
| 2 | volume_surge | 거래량 서지 점수 | 거래량 이동평균 대비 |
| 3 | slope_1m | 1분 EMA slope (z-score) | MTF EMA |
| 4 | slope_5m | 5분 EMA slope (z-score) | MTF EMA |
| 5 | mtf_alignment | MTF 정렬도 (0~1) | MTF 모듈 |
| 6 | spread_bps | bid-ask 스프레드 | 오더북 |
| 7 | funding_sign | 펀딩레이트 방향 (-1/0/+1) | Binance API |
| 8 | regime_num | 레짐 인코딩 (-1/0/+1) | AutoTuner/캐시 |
| 9 | hour_sin | 진입 시각 sin | 시장 세션 패턴 |
| 10 | hour_cos | 진입 시각 cos | 시계열 주기 |
| 11 | direction_match | 방향과 EMA 방향 일치 (0/1) | MTF |
| 12 | price_position | 24h 범위 내 위치 (0~1) | **v3 신규** |
| 13 | rsi_14 | RSI 14 (0~100) | **v3 신규** |
| 14 | atr_ratio | ATR / price | **v3 신규** |
| 15 | slope_divergence | slope_1m - slope_5m | **v3 신규** |
| 16 | funding_magnitude | abs(funding_rate) | **v3 신규** |
| 17 | vol_regime_ratio | 단기변동성/장기변동성 | **v3 신규** |
| 18 | open_pos_count | 현재 보유 포지션 수 | **v3 신규** |
| 19 | day_of_week_sin | 요일 주기 패턴 | **v3 신규** |

### 4.3 학습 메커니즘
- **온라인 학습**: 거래 결과가 나올 때마다 학습 (비실시간, 포지션 청산 후)
- **Replay Buffer**: Win/Loss 균형 샘플링 (각 1000건, 총 2000건)
- **미니배치 학습**: 거래 1건당 3회 반복 (batch=16)
- **듀얼헤드 손실**: 0.7×BCE(분류) + 0.3×Huber(회귀)
- **Adam 옵티마이저**: β1=0.9, β2=0.999, 초기 lr=0.003
- **적응형 학습률**: 50건마다 정확도 체크 → acc<52%이면 lr×0.9, acc>56%이면 lr×1.05
- **z-score 정규화**: Welford 알고리즘으로 온라인 평균/분산 추적

### 4.4 예측 및 진입 게이팅
- **냉각 기간**: 최소 50건(MIN_SAMPLES_TO_PREDICT) 학습 전까지 예측 비활성
- **정확도 최소**: 52%(ACCURACY_MIN) 미만이면 자동 비활성화 (최근 50건 기준)
- **하드 블록**: P(win) < 25%(BLOCK_THRESHOLD)이면 진입 거부
- **강도 배율**: `strength *= win_prob × 2.0 × roi_adjustment` (0.0~2.0)
- **모델 영속성**: logs/neural_scorer.json에 20건마다 자동 저장, 재시작 시 복원
- **v2→v3 마이그레이션**: 12→20 피처 zero-padding, 32→48/16→24 히든 확장

### 4.5 tick_engine 통합 흐름
```python
# 진입 시그널 평가 단계 (evaluate_signal)
feat = build_feature_vector(...)          # 20개 원시 피처 구성
win_prob, expected_roi = scorer.predict(feat)  # 예측
if neural_enabled and win_prob < 0.25:
    return BLOCK                          # 하드 블록
strength *= win_prob * 2.0 * roi_adj      # 강도 배율 적용
scorer.record_entry(symbol, feat)          # 피처 임시 저장

# 포지션 청산 후 (close_position)
scorer.learn_from_outcome(symbol, roi_pct, net_pnl)  # 학습
```

### 4.6 프리미엄 게이트
- `neural_scorer_enabled: bool = False` (config 기본값)
- `neural_license_key: str = ""` (라이선스 키 필요)
- GUI에서 라이선스 키 입력 → 검증 후 활성화

### 4.7 현재 한계점
1. **데이터 부족 문제**: 50건 학습 전까지 완전 비활성 → 초기 구간 활용 불가
2. **단순 아키텍처**: 2-layer MLP는 시계열 패턴 학습에 한계
3. **피처 부족**: 오더북 깊이, OI(미결제약정) 변화, 청산 맵 등 미반영
4. **오프라인 학습 없음**: 과거 데이터 배치 학습(pre-training) 기능 없음
5. **모델 선택 없음**: 앙상블, 교차 검증 등 모델 평가 체계 부재
6. **과적합 방지 미흡**: Weight decay만 사용, dropout/early stopping 없음

---

## 5. 현재 활성 진입 로직 상세

### 5.1 진입 파이프라인 (evaluate_signal → place_entry_order)
```
[5초 틱 루프]
    │
    ▼
① 전처리 필터
   - 스프레드 ≤ 15bps
   - mark-last gap ≤ 30bps
   - 스파이크 가드 (7% / 8캔들)
   - ATR 상한 (volatility_min × 3.0)
   - 심볼 재진입 쿨다운 (120초)
    │
    ▼
② 방향 결정 (LONG/SHORT)
   - 모멘텀 기반: momentum_pct ≥ momentum_min_long(0.005) → LONG
   - 모멘텀 기반: momentum_pct ≤ momentum_min_short(-0.0055) → SHORT
   - chop 레짐: 단기 EMA 방향 우선
    │
    ▼
③ MTF EMA 확인
   - 1분/5분 EMA slope ≥ 2.0bps (방향 일치)
   - 단기 EMA 방향 충돌 필터
    │
    ▼
④ Composite Signal 스코어링
   - 가중치: momentum(0.50) + volume(0.30) + mtf(0.20)
   - 최소 스코어: 0.80 (chop: 0.85)
   - 레짐 방향 바이어스: trend_up에서 LONG +0.15, SHORT -0.10
   - chop RSI 보너스: 과매도 LONG +0.10, 과매수 SHORT +0.10
    │
    ▼
⑤ 실행 비용 필터
   - min_edge_over_fee ≥ 0.3%
   - 펀딩레이트 역방향 페널티 (0.30)
    │
    ▼
⑥ [비활성] NeuralScorer 게이팅
   - P(win) < 25% → 하드 블록
   - 강도 배율 적용
    │
    ▼
⑦ 포지션 제한 체크
   - 전체 max_open_symbols(10)
   - chop 레짐 제한(5)
   - 같은 방향 메이저 심볼 제한(2)
   - 전체 같은 방향 제한(6)
    │
    ▼
⑧ 사이즈 & 레버리지 계산
   - Kelly 사이징 (≥200거래 후 블렌딩)
   - ATR risk-based sizing
   - 동적 레버리지 (strength × neural_prob × volatility)
   - 레버리지 범위: 1~10x
    │
    ▼
⑨ 주문 실행
   - Maker-first (2초 타임아웃, 적응형)
   - 실패 시 Taker fallback
   - 슬리피지 캡: 12bps
```

### 5.2 포지션 사이즈 결정
```
1. Kelly 기본 비율 계산 (win_rate × avg_win/avg_loss - (1-win_rate))
2. kelly_fraction(0.25) 적용 → 보수적 비율
3. chop 레짐이면 × 0.5
4. position_pct × available_balance = notional
5. ATR risk sizing: entry_risk_pct(1%) / (SL_distance / price)
6. 둘 중 작은 값 선택
```

---

## 6. 현재 활성 청산 로직 상세

### 6.1 청산 레이어 (우선순위 순)

```
[매 틱마다 모든 포지션 체크]
    │
    ▼
① 하드 손절 (max_single_trade_loss_pct = 1.8%)
   - ROI ≤ -1.8%이면 즉시 시장가 청산
    │
    ▼
② ATR 기반 SL
   - SL 거리 = ATR × sl_atr_mult
   - trend: 2.0 × ATR
   - chop: 1.4 × ATR
   - max_loss_per_position(1.8%) 캡 적용
    │
    ▼
③ 부분 익절 (R-Multiple TP)
   - TP1: 1.0R에서 30% 청산
   - TP2: 1.5R에서 30% 청산
   - TP3: 2.5R에서 40% 청산
   - 부분 TP 후 손익분기 스탑 활성화
    │
    ▼
④ ATR 트레일링 스탑
   - 활성화: ROI ≥ 0.8%
   - 간격: ATR × 1.7
   - 최소 스텝: 0.15%
   - 5초마다 재계산
    │
    ▼
⑤ 손익분기 스탑
   - 부분 TP 후 활성화
   - 버퍼: 0.1%
    │
    ▼
⑥ [비활성] Time Stop, Signal Decay, Progress Stop
```

---

## 7. AI Advisor 모듈 (ai_advisor.py)

### 7.1 역할
- notifications.log 실시간 스트리밍 → 이벤트 분류 & 자연어 번역 (한/영)
- trade_history.jsonl 분석 → 패턴 인식 (방향별/트리거별/시간대별 승률)
- 약점 탐지 & 개선 제안 자동 생성
- NeuralScorer 상태 모니터링 (GUI 표시용)

### 7.2 현재 한계
- 단순 패턴 매칭 기반 (통계적 분석만)
- 실시간 개입 능력 없음 (GUI 표시용 정보만 제공)
- LLM 연동 없음 (이름만 "AI" Advisor)

---

## 8. 개선 요청 사항

### 8.1 AutoTuner 재활성화 및 고도화

**목표:** AutoTuner가 기본적으로 작동하면서 적절한 수치를 제공

**현재 문제점:**
1. 노이즈 학습: 16샘플 미니배치가 시장 노이즈에 과반응
2. 파라미터 진동: 빈번한 변경으로 일관성 없는 전략 실행
3. 레짐 오분류: 짧은 윈도우(5분)로 레짐 판단 → 잦은 전환
4. 비활성 시 레짐 항상 "chop" 고정 → chop 관련 로직 미작동

**개선 방향 질문:**
- 메트릭 수집 윈도우를 어느 정도로 확대해야 하는가? (5분 → 15분/30분?)
- Shadow 검증 사이클을 몇 회로 설정해야 안정적인가?
- 파라미터 변경 속도(step size)를 어떻게 조절해야 하는가?
- 레짐 분류에 어떤 추가 지표가 필요한가?
- 베이지안 최적화, 밴딧 알고리즘 등 대안적 접근은?

### 8.2 NeuralScorer 프리미엄 고도화

**목표:** 프리미엄 사용자에게 더 정확한 알고리즘 학습 데이터 기반 수익 창출

**현재 한계:**
1. 2-layer MLP(48/24)는 시계열 패턴 학습 한계
2. 온라인 학습만 → 과거 데이터 활용 불가
3. 피처 20개로 시장 정보 부족
4. 과적합 방지 장치 미흡

**개선 방향 질문:**
- LSTM/GRU 또는 Transformer 경량 모델 도입 가능성?
- 과거 데이터 pre-training → online fine-tuning 파이프라인?
- 추가 피처 후보: OI 변화율, 청산 히트맵, 오더북 불균형, 크로스심볼 상관관계?
- 앙상블(MLP + LSTM + Rule-based) 접근?
- A/B 테스트 프레임워크 (기존 전략 vs 신경망 전략)?
- 모델 성능 모니터링 & 자동 폴백 메커니즘?

### 8.3 AutoTuner × NeuralScorer 연동

**현재:** 각각 독립적으로 동작 (AutoTuner는 파라미터, NeuralScorer는 진입 게이팅)

**질문:**
- NeuralScorer의 예측 정확도를 AutoTuner의 confidence 계산에 통합?
- 신경망 예측 기반 동적 파라미터 조정 (예: 승률 높을 때 position_pct 확대)?
- 공유 레짐 분류: AutoTuner의 통계적 레짐 + 신경망의 학습된 레짐?

### 8.4 프리미엄 티어 차별화

**현재 구조:**
- 무료: 기본 진입/청산 로직 (composite signal + ATR SL/TP)
- 프리미엄: NeuralScorer 활성화 (라이선스 키)

**질문:**
- 프리미엄 전용 추가 기능 제안? (예: 고급 리스크 관리, 커스텀 전략, 백테스트 등)
- AutoTuner를 무료/프리미엄 어떻게 분리?
- 데이터 수집 → 중앙 서버 학습 → 모델 배포 파이프라인?

---

## 9. 기술 제약 조건

- **Python 3.10+** 기반, asyncio 비동기
- **GPU 불가**: numpy만 사용 (PyTorch/TensorFlow 미설치 환경)
- **메모리 제한**: Replay Buffer 2000건, 모델 파라미터 ~5000개
- **네트워크**: Binance API rate limit (1200 calls/min 자체 제한)
- **실시간 성능**: 5초 틱 루프 내에서 모든 연산 완료 필요
- **배포 환경**: PyInstaller로 빌드된 단일 실행파일 (.exe)
- **상태 영속성**: JSON 파일로 저장/복원 (DB 없음)

---

## 10. 핵심 설정값 요약 (config.py 현재 값)

```python
# 진입
position_pct = 0.06          # 6% 마진
leverage_min = 1
leverage_max = 10
volatility_min = 0.003       # 0.3%
momentum_min_long = 0.005    # 0.5%
momentum_min_short = -0.0055
composite_min_score = 0.80
chop_composite_min_score = 0.85
min_edge_over_fee_pct = 0.003

# 청산
sl_atr_mult = 2.0
sl_atr_mult_chop = 1.4
sl_atr_mult_trend = 2.0
max_loss_per_position = 1.8
max_single_trade_loss_pct = 1.8
tp_r_multiple_1 = 1.5
tp_r_multiple_2 = 2.5
trail_atr_mult = 1.7
trail_activate_pnl_pct = 0.008

# 비활성 기능
auto_tune_enabled = False
neural_scorer_enabled = False
enable_time_stop = False
enable_signal_decay_exit = False
enable_progress_stop = False
```

---

*이 명세서를 GPT에게 전달하여 AutoTuner 재활성화 방안, NeuralScorer 고도화 전략, 프리미엄 차별화 방안에 대한 구체적 개선안을 요청하세요.*
