"""
neural_scorer.py  v3.0 — Dual-Head Adaptive Online Learning Signal Scorer
────────────────────────────────────────────────────────────────────────────
[프리미엄 기능] 거래 결과를 누적 학습해 다음 진입 확률 + 기대 ROI를 예측한다.
GPU/PyTorch 없이 numpy만 사용. 재시작 후에도 학습이 이어진다.

v2 → v3 주요 변경:
  1. 피처 12개 → 20개 확장 (price_position, RSI, ATR ratio, slope divergence 등)
  2. 듀얼헤드 출력: P(win) + E[ROI%] 회귀 → 수익 크기까지 예측
  3. 히든 레이어 확장: 32→48, 16→24 (피처 증가분 반영)
  4. 신뢰도 기반 진입 게이팅: P(win) < 0.25 → 하드 블록
  5. 적응형 학습률: 정확도에 따라 lr 자동 조절
  6. v2 모델 자동 마이그레이션: 12→20 피처 zero-padding
  7. Replay 버퍼에 ROI 저장 → 회귀 헤드 학습

아키텍처:
  Input(20) → Dense(48, LeakyReLU) → Dense(24, LeakyReLU)
    ├→ Head1: Dense(1, Sigmoid) → P(win)          [분류]
    └→ Head2: Dense(1, Linear)  → E[ROI%]         [회귀]

피처 벡터 (z-score 정규화):
  [ 0] rel_momentum      상대 단기 모멘텀 (momentum_5m / vol)
  [ 1] volatility        변동성 절댓값
  [ 2] volume_surge      거래량 서지 점수
  [ 3] slope_1m          1분 EMA slope (z-score)
  [ 4] slope_5m          5분 EMA slope (z-score)
  [ 5] mtf_alignment     MTF 정렬도 (0~1)
  [ 6] spread_bps        bid-ask 스프레드
  [ 7] funding_sign      펀딩레이트 방향 (-1/0/+1)
  [ 8] regime_num        레짐 인코딩 (-1/0/+1)
  [ 9] hour_sin          진입 시각 sin (시장 세션 패턴)
  [10] hour_cos          진입 시각 cos
  [11] direction_match   방향과 단기 EMA 방향 일치 여부 (0/1)
  ── v3 신규 ──
  [12] price_position    24h 범위 내 위치 (0~1)
  [13] rsi_14            RSI 14 (0~100)
  [14] atr_ratio         ATR / price
  [15] slope_divergence  slope_1m - slope_5m (모멘텀 괴리)
  [16] funding_magnitude abs(funding_rate)
  [17] vol_regime_ratio  단기변동성 / 장기변동성
  [18] open_pos_count    현재 보유 포지션 수
  [19] day_of_week_sin   요일 주기 패턴
"""

from __future__ import annotations

import os
import json
import math
import time
import logging
from collections import deque
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── v3 상수 ────────────────────────────────────────────────────────────────
N_FEATURES = 20                 # v3: 20개 피처
N_FEATURES_V2 = 12              # v2 호환용
HIDDEN1 = 48                    # v3: 32→48
HIDDEN2 = 24                    # v3: 16→24
REPLAY_CAPACITY = 2000          # 최대 저장 거래 수
MIN_REPLAY_TO_TRAIN = 30        # 미니배치 학습 시작 최소 샘플 수
MINI_BATCH_SIZE = 16            # 미니배치 크기
TRAIN_ITERS_PER_STEP = 3        # 거래 1건당 replay 학습 반복 수
MIN_SAMPLES_TO_PREDICT = 50     # 이 수 이상 학습해야 예측 반영 (냉각 기간)
ACCURACY_WINDOW = 50            # 최근 N건 정확도로 모델 유효성 판단
ACCURACY_MIN = 0.52             # 이 미만이면 예측 비활성화
BLOCK_THRESHOLD = 0.25          # P(win) < 이 값이면 진입 하드 블록
LR_MIN = 0.0005
LR_MAX = 0.008
ROI_LOSS_WEIGHT = 0.3           # 듀얼헤드 손실 가중치: 0.7*BCE + 0.3*Huber


# ─────────────────────────────────────────────────────────────────────────────
# 수치 유틸
# ─────────────────────────────────────────────────────────────────────────────
def leaky_relu(x: np.ndarray, alpha: float = 0.01) -> np.ndarray:
    return np.where(x > 0, x, alpha * x)

def leaky_relu_grad(x: np.ndarray, alpha: float = 0.01) -> np.ndarray:
    return np.where(x > 0, 1.0, alpha)

def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -20.0, 20.0)))

def safe_float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if (f != f or math.isinf(f)) else f
    except Exception:
        return default

def _huber_grad(pred: np.ndarray, target: np.ndarray, delta: float = 1.0) -> np.ndarray:
    """Huber loss gradient (robust to outliers)."""
    diff = pred - target
    return np.where(np.abs(diff) <= delta, diff, delta * np.sign(diff))


# ─────────────────────────────────────────────────────────────────────────────
# 온라인 z-score 정규화 (Welford 알고리즘)
# ─────────────────────────────────────────────────────────────────────────────
class RunningNorm:
    """피처별 온라인 평균·분산 추적 → z-score 정규화."""
    def __init__(self, n_features: int):
        self.n = 0
        self.n_features = n_features
        self.mean = np.zeros(n_features)
        self.M2   = np.ones(n_features)   # 분산 누적 (초기 1 → 초기 std=1)

    @property
    def std(self) -> np.ndarray:
        var = self.M2 / max(self.n, 1)
        return np.sqrt(np.maximum(var, 1e-8))

    def update(self, x: np.ndarray):
        self.n += 1
        delta  = x - self.mean
        self.mean += delta / self.n
        delta2 = x - self.mean
        self.M2 += delta * delta2

    def normalize(self, x: np.ndarray) -> np.ndarray:
        z = (x - self.mean) / self.std
        return np.clip(z, -4.0, 4.0)

    def expand(self, new_n: int):
        """v2(12) → v3(20) 마이그레이션: 신규 피처는 mean=0, M2=1로 초기화."""
        if new_n <= self.n_features:
            return
        old_n = self.n_features
        self.mean = np.concatenate([self.mean, np.zeros(new_n - old_n)])
        self.M2   = np.concatenate([self.M2,   np.ones(new_n - old_n)])
        self.n_features = new_n

    def to_dict(self) -> dict:
        return {"n": self.n, "mean": self.mean.tolist(), "M2": self.M2.tolist()}

    def from_dict(self, d: dict):
        self.n    = int(d.get("n", 0))
        self.mean = np.array(d.get("mean", self.mean.tolist()))
        self.M2   = np.array(d.get("M2",   self.M2.tolist()))
        self.n_features = len(self.mean)


# ─────────────────────────────────────────────────────────────────────────────
# 듀얼헤드 경량 신경망
# Input(20) → Dense(48) → Dense(24) → [Sigmoid(1), Linear(1)]
# ─────────────────────────────────────────────────────────────────────────────
class MiniNet:
    def __init__(self, n_in: int = N_FEATURES, h1: int = HIDDEN1, h2: int = HIDDEN2,
                 lr: float = 0.003, wd: float = 1e-4,
                 beta1: float = 0.9, beta2: float = 0.999, eps: float = 1e-8):
        self.n_in, self.h1, self.h2 = n_in, h1, h2
        self.lr, self.wd = lr, wd
        self.beta1, self.beta2, self.eps = beta1, beta2, eps
        self.t = 0

        rng = np.random.default_rng(42)
        # He 초기화
        self.W1 = rng.normal(0, math.sqrt(2.0 / n_in), (h1, n_in))
        self.b1 = np.zeros(h1)
        self.W2 = rng.normal(0, math.sqrt(2.0 / h1),   (h2, h1))
        self.b2 = np.zeros(h2)
        # Head 1: 분류 (P(win))
        self.W3 = rng.normal(0, math.sqrt(2.0 / h2),   (1, h2))
        self.b3 = np.zeros(1)
        # Head 2: 회귀 (E[ROI%])
        self.W3r = rng.normal(0, math.sqrt(2.0 / h2),  (1, h2))
        self.b3r = np.zeros(1)

        self._params = [self.W1, self.b1, self.W2, self.b2,
                        self.W3, self.b3, self.W3r, self.b3r]
        self._m = [np.zeros_like(p) for p in self._params]
        self._v = [np.zeros_like(p) for p in self._params]

    # ── 순전파 ──────────────────────────────────────────────────────────────
    def _forward(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray, dict]:
        """X: (batch, n_in) → prob: (batch,), roi_pred: (batch,)"""
        z1 = X @ self.W1.T + self.b1
        a1 = leaky_relu(z1)
        z2 = a1 @ self.W2.T + self.b2
        a2 = leaky_relu(z2)
        # Head 1: 분류
        z3 = a2 @ self.W3.T + self.b3
        prob = sigmoid(z3).ravel()
        # Head 2: 회귀
        z3r = a2 @ self.W3r.T + self.b3r
        roi_pred = z3r.ravel()
        cache = {"X": X, "z1": z1, "a1": a1, "z2": z2, "a2": a2, "z3": z3, "z3r": z3r}
        return prob, roi_pred, cache

    # ── 역전파 (듀얼헤드) ─────────────────────────────────────────────────
    def _backward(self, cache: dict, y: np.ndarray, roi_targets: np.ndarray,
                  weights: np.ndarray, roi_weight: float = ROI_LOSS_WEIGHT):
        """
        y: (batch,) 0/1 labels
        roi_targets: (batch,) actual ROI%
        weights: (batch,) sample weights
        """
        X, a1, a2 = cache["X"], cache["a1"], cache["a2"]
        z1, z2, z3, z3r = cache["z1"], cache["z2"], cache["z3"], cache["z3r"]
        batch = max(len(y), 1)
        cls_w = 1.0 - roi_weight  # 분류 손실 가중치

        # ── Head 1: BCE gradient ──
        prob_ = sigmoid(np.array(z3))
        dz3 = cls_w * (weights[:, None] * (prob_.reshape(-1, 1) - y[:, None])) / batch

        dW3 = dz3.T @ a2 + self.wd * self.W3
        db3 = dz3.sum(axis=0)

        # ── Head 2: Huber gradient ──
        roi_pred = z3r.ravel()
        dz3r_raw = _huber_grad(roi_pred, roi_targets)
        dz3r = roi_weight * (weights[:, None] * dz3r_raw[:, None]) / batch

        dW3r = dz3r.T @ a2 + self.wd * self.W3r
        db3r = dz3r.sum(axis=0)

        # ── Shared backbone gradient ──
        da2 = dz3 @ self.W3 + dz3r @ self.W3r
        dz2 = da2 * leaky_relu_grad(z2)
        dW2 = dz2.T @ a1 + self.wd * self.W2
        db2 = dz2.sum(axis=0)

        da1 = dz2 @ self.W2
        dz1 = da1 * leaky_relu_grad(z1)
        dW1 = dz1.T @ X + self.wd * self.W1
        db1 = dz1.sum(axis=0)

        grads = [dW1, db1, dW2, db2, dW3, db3, dW3r, db3r]

        # ── Adam update ──
        self.t += 1
        bc1 = 1.0 - self.beta1 ** self.t
        bc2 = 1.0 - self.beta2 ** self.t
        for i, (p, g) in enumerate(zip(self._params, grads)):
            self._m[i] = self.beta1 * self._m[i] + (1 - self.beta1) * g
            self._v[i] = self.beta2 * self._v[i] + (1 - self.beta2) * g ** 2
            p -= self.lr * (self._m[i] / bc1) / (np.sqrt(self._v[i] / bc2) + self.eps)
            np.clip(p, -10.0, 10.0, out=p)

    # ── 예측 ──────────────────────────────────────────────────────────────
    def predict_one(self, x: np.ndarray) -> Tuple[float, float]:
        """Returns (win_prob, expected_roi%)"""
        prob, roi, _ = self._forward(x[None, :])
        return float(prob[0]), float(roi[0])

    # ── 미니배치 학습 ────────────────────────────────────────────────────
    def fit_batch(self, X: np.ndarray, y: np.ndarray,
                  roi_targets: np.ndarray, weights: np.ndarray):
        prob, roi_pred, cache = self._forward(X)
        self._backward(cache, y, roi_targets, weights)

    # ── 직렬화 ───────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "t": int(self.t), "n_in": self.n_in, "h1": self.h1, "h2": self.h2,
            "W1": self.W1.tolist(), "b1": self.b1.tolist(),
            "W2": self.W2.tolist(), "b2": self.b2.tolist(),
            "W3": self.W3.tolist(), "b3": self.b3.tolist(),
            "W3r": self.W3r.tolist(), "b3r": self.b3r.tolist(),
            "m": [a.tolist() for a in self._m],
            "v": [a.tolist() for a in self._v],
        }

    def from_dict(self, d: dict, target_n_in: int = N_FEATURES):
        self.t = int(d.get("t", 0))
        loaded_W1 = np.array(d["W1"])
        loaded_n_in = loaded_W1.shape[1] if loaded_W1.ndim == 2 else N_FEATURES_V2

        # ── v2→v3 마이그레이션: W1 zero-padding ──
        if loaded_n_in < target_n_in:
            h1_loaded = loaded_W1.shape[0]
            pad = np.zeros((h1_loaded, target_n_in - loaded_n_in))
            loaded_W1 = np.hstack([loaded_W1, pad])
            logger.info("NeuralScorer migration: W1 %d→%d features (zero-padded)",
                        loaded_n_in, target_n_in)

        # v2(h1=32)→v3(h1=48): W1 행 확장
        if loaded_W1.shape[0] < HIDDEN1:
            rng = np.random.default_rng(99)
            extra_rows = rng.normal(0, 0.01, (HIDDEN1 - loaded_W1.shape[0], target_n_in))
            loaded_W1 = np.vstack([loaded_W1, extra_rows])
            logger.info("NeuralScorer migration: W1 rows %d→%d", loaded_W1.shape[0] - extra_rows.shape[0], HIDDEN1)

        self.W1 = loaded_W1
        self.b1 = np.array(d["b1"])
        if len(self.b1) < HIDDEN1:
            self.b1 = np.concatenate([self.b1, np.zeros(HIDDEN1 - len(self.b1))])

        # W2: (h2, h1) — v2(16,32) → v3(24,48)
        loaded_W2 = np.array(d["W2"])
        if loaded_W2.shape[1] < HIDDEN1:
            pad_cols = np.zeros((loaded_W2.shape[0], HIDDEN1 - loaded_W2.shape[1]))
            loaded_W2 = np.hstack([loaded_W2, pad_cols])
        if loaded_W2.shape[0] < HIDDEN2:
            rng = np.random.default_rng(77)
            extra = rng.normal(0, 0.01, (HIDDEN2 - loaded_W2.shape[0], HIDDEN1))
            loaded_W2 = np.vstack([loaded_W2, extra])
        self.W2 = loaded_W2
        self.b2 = np.array(d["b2"])
        if len(self.b2) < HIDDEN2:
            self.b2 = np.concatenate([self.b2, np.zeros(HIDDEN2 - len(self.b2))])

        # W3 (classification head): (1, h2)
        loaded_W3 = np.array(d["W3"])
        if loaded_W3.shape[1] < HIDDEN2:
            pad = np.zeros((1, HIDDEN2 - loaded_W3.shape[1]))
            loaded_W3 = np.hstack([loaded_W3, pad])
        self.W3 = loaded_W3
        self.b3 = np.array(d["b3"])

        # W3r (regression head): v2에는 없음 → 새로 초기화
        if "W3r" in d:
            loaded_W3r = np.array(d["W3r"])
            if loaded_W3r.shape[1] < HIDDEN2:
                pad = np.zeros((1, HIDDEN2 - loaded_W3r.shape[1]))
                loaded_W3r = np.hstack([loaded_W3r, pad])
            self.W3r = loaded_W3r
            self.b3r = np.array(d["b3r"])
        else:
            rng = np.random.default_rng(55)
            self.W3r = rng.normal(0, math.sqrt(2.0 / HIDDEN2), (1, HIDDEN2))
            self.b3r = np.zeros(1)
            logger.info("NeuralScorer migration: regression head initialized (new in v3)")

        self.n_in = target_n_in
        self.h1 = self.W1.shape[0]
        self.h2 = self.W2.shape[0]
        self._params = [self.W1, self.b1, self.W2, self.b2,
                        self.W3, self.b3, self.W3r, self.b3r]

        # Adam 모멘텀 마이그레이션
        if "m" in d and "v" in d:
            loaded_m = [np.array(a) for a in d["m"]]
            loaded_v = [np.array(a) for a in d["v"]]
            # 파라미터 수가 다르면(v2=6, v3=8) 새 헤드용 모멘텀 추가
            while len(loaded_m) < len(self._params):
                loaded_m.append(np.zeros_like(self._params[len(loaded_m)]))
                loaded_v.append(np.zeros_like(self._params[len(loaded_v)]))
            # 각 파라미터 shape 맞추기
            self._m = []
            self._v = []
            for i, p in enumerate(self._params):
                if i < len(loaded_m) and loaded_m[i].shape == p.shape:
                    self._m.append(loaded_m[i])
                    self._v.append(loaded_v[i])
                else:
                    self._m.append(np.zeros_like(p))
                    self._v.append(np.zeros_like(p))
        else:
            self._m = [np.zeros_like(p) for p in self._params]
            self._v = [np.zeros_like(p) for p in self._params]


# ─────────────────────────────────────────────────────────────────────────────
# 피처 빌더
# ─────────────────────────────────────────────────────────────────────────────
def build_feature_vector(
    momentum_5m: float,
    volatility: float,
    volume_surge: float,
    mtf_slope_1m: float,
    mtf_slope_5m: float,
    mtf_alignment: float,
    spread_bps: float,
    funding_rate: float,
    regime: str,
    direction: str,
    entry_ts: Optional[float] = None,
    # ── v3 신규 피처 (기본값으로 하위 호환) ──
    price: float = 0.0,
    high_24h: float = 0.0,
    low_24h: float = 0.0,
    rsi_14: float = 50.0,
    atr: float = 0.0,
    vol_regime_ratio: float = 1.0,
    open_pos_count: int = 0,
) -> np.ndarray:
    """
    20개 피처 벡터 구성 (정규화 전 원시값).
    v2 호출 호환: 신규 파라미터 미전달 시 기본값 사용.
    """
    vol = max(safe_float(volatility), 1e-6)
    ts = entry_ts or time.time()
    hour = (ts % 86400) / 3600.0
    hour_rad = hour * 2 * math.pi / 24.0
    regime_map = {"trend_up": 1.0, "trend_down": -1.0, "chop": 0.0}
    dir_slope = (safe_float(mtf_slope_1m) + safe_float(mtf_slope_5m)) / 2.0
    dir_up = dir_slope > 0
    dir_match = 1.0 if (direction == "LONG") == dir_up else 0.0

    # v3 신규 피처 계산
    _range = safe_float(high_24h) - safe_float(low_24h)
    price_pos = (safe_float(price) - safe_float(low_24h)) / max(_range, 1e-8) if _range > 0 else 0.5
    price_pos = max(0.0, min(1.0, price_pos))
    atr_ratio = safe_float(atr) / max(safe_float(price), 1e-8) if price > 0 else 0.0
    slope_div = safe_float(mtf_slope_1m) - safe_float(mtf_slope_5m)
    funding_mag = abs(safe_float(funding_rate))
    dow_rad = ((ts % 604800) / 604800.0) * 2 * math.pi  # 주간 주기

    return np.array([
        # ── v2 기존 12개 ──
        safe_float(momentum_5m) / vol,                          #  0 rel_momentum
        safe_float(volatility),                                 #  1 volatility
        safe_float(volume_surge),                               #  2 volume_surge
        safe_float(mtf_slope_1m),                               #  3 slope_1m
        safe_float(mtf_slope_5m),                               #  4 slope_5m
        safe_float(mtf_alignment),                              #  5 mtf_alignment
        safe_float(spread_bps),                                 #  6 spread_bps
        math.copysign(1.0, safe_float(funding_rate))
            if abs(safe_float(funding_rate)) > 1e-5 else 0.0,  #  7 funding_sign
        regime_map.get(regime, 0.0),                            #  8 regime_num
        math.sin(hour_rad),                                     #  9 hour_sin
        math.cos(hour_rad),                                     # 10 hour_cos
        dir_match,                                              # 11 direction_match
        # ── v3 신규 8개 ──
        price_pos,                                              # 12 price_position
        safe_float(rsi_14),                                     # 13 rsi_14
        atr_ratio,                                              # 14 atr_ratio
        slope_div,                                              # 15 slope_divergence
        funding_mag,                                            # 16 funding_magnitude
        safe_float(vol_regime_ratio),                           # 17 vol_regime_ratio
        float(open_pos_count),                                  # 18 open_pos_count
        math.sin(dow_rad),                                      # 19 day_of_week_sin
    ], dtype=float)


# ─────────────────────────────────────────────────────────────────────────────
# Experience Replay Buffer (균형 샘플링 + ROI 저장)
# ─────────────────────────────────────────────────────────────────────────────
class ReplayBuffer:
    """
    Win / Loss 샘플을 별도 버퍼에 저장.
    각 샘플: (features, roi_percent)
    미니배치 샘플링 시 win:loss = 1:1 균형 유지.
    """
    def __init__(self, capacity: int = REPLAY_CAPACITY):
        self.cap = capacity // 2
        self.wins:   deque[Tuple[np.ndarray, float]] = deque(maxlen=self.cap)
        self.losses: deque[Tuple[np.ndarray, float]] = deque(maxlen=self.cap)

    def push(self, x: np.ndarray, label: float, roi: float):
        if label == 1.0:
            self.wins.append((x.copy(), float(roi)))
        else:
            self.losses.append((x.copy(), float(roi)))

    def sample(self, batch_size: int) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
        """균형 미니배치 반환: (X, y_labels, y_roi, weights). 데이터 부족 시 None."""
        half = batch_size // 2
        if len(self.wins) < max(4, half) or len(self.losses) < max(4, half):
            return None
        rng = np.random.default_rng()
        w_idx = rng.integers(0, len(self.wins),   size=half)
        l_idx = rng.integers(0, len(self.losses), size=half)
        w_list = list(self.wins)
        l_list = list(self.losses)
        X = np.array([w_list[i][0] for i in w_idx] + [l_list[i][0] for i in l_idx])
        y = np.array([1.0] * half + [0.0] * half)
        roi = np.array([w_list[i][1] for i in w_idx] + [l_list[i][1] for i in l_idx])
        W = np.ones(batch_size)
        return X, y, roi, W

    @property
    def total(self) -> int:
        return len(self.wins) + len(self.losses)

    def expand_features(self, old_n: int, new_n: int):
        """Replay 버퍼 내 피처를 zero-pad로 확장."""
        if new_n <= old_n:
            return
        pad_size = new_n - old_n
        new_wins = deque(maxlen=self.cap)
        for x, r in self.wins:
            if len(x) < new_n:
                x = np.concatenate([x, np.zeros(pad_size)])
            new_wins.append((x, r))
        self.wins = new_wins
        new_losses = deque(maxlen=self.cap)
        for x, r in self.losses:
            if len(x) < new_n:
                x = np.concatenate([x, np.zeros(pad_size)])
            new_losses.append((x, r))
        self.losses = new_losses
        logger.info("ReplayBuffer: features expanded %d→%d (%d samples)",
                    old_n, new_n, self.total)

    def to_dict(self) -> dict:
        return {
            "wins":   [(x.tolist(), r) for x, r in self.wins],
            "losses": [(x.tolist(), r) for x, r in self.losses],
        }

    def from_dict(self, d: dict):
        for x_list, r in d.get("wins", []):
            self.wins.append((np.array(x_list), float(r)))
        for x_list, r in d.get("losses", []):
            self.losses.append((np.array(x_list), float(r)))


# ─────────────────────────────────────────────────────────────────────────────
# 성능 추적기
# ─────────────────────────────────────────────────────────────────────────────
class PerformanceTracker:
    def __init__(self, window: int = ACCURACY_WINDOW):
        self.window = window
        self._records: deque[int] = deque(maxlen=window)
        self._roi_records: deque[float] = deque(maxlen=window)

    def record(self, predicted_prob: float, actual_label: float, roi: float):
        predicted_win = predicted_prob >= 0.5
        actual_win    = actual_label >= 0.5
        self._records.append(1 if predicted_win == actual_win else 0)
        self._roi_records.append(roi)

    @property
    def accuracy(self) -> float:
        if not self._records:
            return 0.5
        return sum(self._records) / len(self._records)

    @property
    def avg_roi(self) -> float:
        if not self._roi_records:
            return 0.0
        return sum(self._roi_records) / len(self._roi_records)

    @property
    def n_evaluated(self) -> int:
        return len(self._records)

    def is_valid(self) -> bool:
        if self.n_evaluated < 20:
            return True
        return self.accuracy >= ACCURACY_MIN

    def to_dict(self) -> dict:
        return {
            "records": list(self._records),
            "roi_records": list(self._roi_records),
        }

    def from_dict(self, d: dict):
        for v in d.get("records", []):
            self._records.append(int(v))
        for v in d.get("roi_records", []):
            self._roi_records.append(float(v))


# ─────────────────────────────────────────────────────────────────────────────
# NeuralScorer v3.0 — 메인 클래스
# ─────────────────────────────────────────────────────────────────────────────
class NeuralScorer:
    """
    [프리미엄] 듀얼헤드 온라인 학습 신호 스코어러.

    tick_engine에서의 사용 흐름:
        # 진입 직전
        feat = build_feature_vector(...)          # 20개 피처
        prob, roi = scorer.predict(feat)          # 승률 + 기대 ROI 예측
        scorer.record_entry(symbol, feat)         # 피처 임시 저장

        # 포지션 청산 후
        scorer.learn_from_outcome(symbol, roi_percent, net_pnl=...)  # 학습
    """
    VERSION = "3.0"

    def __init__(self, model_path: str = "logs/neural_scorer.json"):
        self.model_path = model_path
        self.net        = MiniNet(n_in=N_FEATURES, h1=HIDDEN1, h2=HIDDEN2, lr=0.003, wd=1e-4)
        self.norm       = RunningNorm(N_FEATURES)
        self.replay     = ReplayBuffer(REPLAY_CAPACITY)
        self.tracker    = PerformanceTracker(ACCURACY_WINDOW)
        self.n_trained  = 0
        self.n_wins     = 0
        self.n_losses   = 0
        self._pending:  Dict[str, Tuple[np.ndarray, float, float]] = {}  # symbol → (raw_feat, prob, roi_pred)
        self._last_save = 0.0
        self._active    = True
        self.load()

    # ── 저장 / 복원 ──────────────────────────────────────────────────────────
    def save(self):
        try:
            os.makedirs(os.path.dirname(self.model_path) or ".", exist_ok=True)
            data = {
                "version":    self.VERSION,
                "n_features": N_FEATURES,
                "n_trained":  self.n_trained,
                "n_wins":     self.n_wins,
                "n_losses":   self.n_losses,
                "active":     self._active,
                "lr":         self.net.lr,
                "net":        self.net.to_dict(),
                "norm":       self.norm.to_dict(),
                "replay":     self.replay.to_dict(),
                "tracker":    self.tracker.to_dict(),
                "saved_at":   time.time(),
            }
            tmp = self.model_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, self.model_path)
            self._last_save = time.time()
        except Exception as e:
            logger.warning("NeuralScorer save failed: %s", e)

    def load(self):
        if not os.path.exists(self.model_path):
            logger.info("NeuralScorer v%s: no saved model, starting fresh (n_features=%d)",
                        self.VERSION, N_FEATURES)
            return
        try:
            with open(self.model_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.n_trained = int(data.get("n_trained", 0))
            self.n_wins    = int(data.get("n_wins", 0))
            self.n_losses  = int(data.get("n_losses", 0))
            self._active   = bool(data.get("active", True))

            saved_n_features = int(data.get("n_features", N_FEATURES_V2))
            saved_version = str(data.get("version", "2.0"))

            # 네트워크 로드 (자동 마이그레이션)
            if "net" in data:
                self.net.from_dict(data["net"], target_n_in=N_FEATURES)

            # 적응형 LR 복원
            if "lr" in data:
                self.net.lr = float(data["lr"])

            # 정규화 통계 로드 + 확장
            if "norm" in data:
                self.norm.from_dict(data["norm"])
                if self.norm.n_features < N_FEATURES:
                    self.norm.expand(N_FEATURES)

            # Replay 로드 + 피처 확장
            if "replay" in data:
                self.replay.from_dict(data["replay"])
                if saved_n_features < N_FEATURES:
                    self.replay.expand_features(saved_n_features, N_FEATURES)

            if "tracker" in data:
                self.tracker.from_dict(data["tracker"])

            logger.info(
                "NeuralScorer v%s loaded (from v%s): n=%d win=%d loss=%d "
                "acc=%.1f%% active=%s features=%d→%d lr=%.5f",
                self.VERSION, saved_version, self.n_trained, self.n_wins, self.n_losses,
                self.tracker.accuracy * 100, self._active,
                saved_n_features, N_FEATURES, self.net.lr
            )
        except Exception as e:
            logger.warning("NeuralScorer load failed, starting fresh: %s", e)

    # ── 진입 시 피처 기록 ────────────────────────────────────────────────────
    def record_entry(self, symbol: str, raw_features: np.ndarray):
        self.norm.update(raw_features)
        x_norm = self.norm.normalize(raw_features)
        if self.n_trained >= MIN_SAMPLES_TO_PREDICT:
            prob, roi_pred = self.net.predict_one(x_norm)
        else:
            prob, roi_pred = 0.5, 0.0
        self._pending[symbol] = (raw_features.copy(), prob, roi_pred)

    # ── 거래 완료 → 학습 ─────────────────────────────────────────────────────
    def learn_from_outcome(self, symbol: str, roi_percent: float, net_pnl: float = None):
        entry = self._pending.pop(symbol, None)
        if entry is None:
            return
        raw_feat, predicted_prob, predicted_roi = entry

        if net_pnl is not None:
            label = 1.0 if float(net_pnl) > 0.0 else 0.0
        else:
            label = 1.0 if roi_percent > 0.0 else 0.0

        # 1. 정규화 + replay 저장
        x_norm = self.norm.normalize(raw_feat)
        self.replay.push(x_norm, label, roi_percent)

        # 2. 성능 추적
        if self.n_trained >= MIN_SAMPLES_TO_PREDICT:
            self.tracker.record(predicted_prob, label, roi_percent)
            if not self.tracker.is_valid() and self._active:
                self._active = False
                logger.warning(
                    "NeuralScorer: accuracy %.1f%% < %.0f%% → prediction disabled (n=%d)",
                    self.tracker.accuracy * 100, ACCURACY_MIN * 100, self.tracker.n_evaluated
                )
            elif self.tracker.is_valid() and not self._active and self.tracker.n_evaluated >= 20:
                self._active = True
                logger.info("NeuralScorer: accuracy %.1f%% recovered → re-enabled",
                            self.tracker.accuracy * 100)

        # 3. 적응형 학습률
        if self.n_trained > 0 and self.n_trained % 50 == 0 and self.tracker.n_evaluated >= 30:
            acc = self.tracker.accuracy
            old_lr = self.net.lr
            if acc < 0.52:
                self.net.lr = max(LR_MIN, self.net.lr * 0.9)
            elif acc > 0.56:
                self.net.lr = min(LR_MAX, self.net.lr * 1.05)
            if abs(self.net.lr - old_lr) > 1e-6:
                logger.info("NeuralScorer AdaptiveLR: acc=%.1f%% lr=%.5f→%.5f",
                            acc * 100, old_lr, self.net.lr)

        # 4. Replay 기반 듀얼헤드 학습
        for _ in range(TRAIN_ITERS_PER_STEP):
            batch = self.replay.sample(MINI_BATCH_SIZE)
            if batch is None:
                break
            X, y, roi_targets, W = batch
            self.net.fit_batch(X, y, roi_targets, W)

        self.n_trained += 1
        if label == 1.0:
            self.n_wins += 1
        else:
            self.n_losses += 1

        if self.n_trained % 20 == 0:
            self.save()

        win_rate = self.n_wins / max(self.n_trained, 1) * 100
        _pnl_str = f" net={net_pnl:.4f}U" if net_pnl is not None else ""
        logger.info(
            "NeuralScorer: %s roi=%.2f%%%s label=%d n=%d wr=%.1f%% acc=%.1f%% "
            "active=%s lr=%.5f pred_roi=%.2f%%",
            symbol, roi_percent, _pnl_str, int(label), self.n_trained,
            win_rate, self.tracker.accuracy * 100, self._active,
            self.net.lr, predicted_roi
        )

    # ── 예측 ─────────────────────────────────────────────────────────────────
    def predict(self, raw_features: np.ndarray) -> Tuple[float, float]:
        """
        Returns (win_prob, expected_roi%).
        냉각 기간(<50건) 또는 성능 미달 시 (0.5, 0.0) 반환.
        """
        if self.n_trained < MIN_SAMPLES_TO_PREDICT:
            return 0.5, 0.0
        if not self._active:
            return 0.5, 0.0
        try:
            x_norm = self.norm.normalize(raw_features)
            prob, roi = self.net.predict_one(x_norm)
            return float(np.clip(prob, 0.0, 1.0)), float(np.clip(roi, -20.0, 20.0))
        except Exception:
            return 0.5, 0.0

    # ── 상태 조회 (GUI 표시용) ───────────────────────────────────────────────
    def status(self) -> dict:
        wr = self.n_wins / max(self.n_trained, 1) * 100
        acc = self.tracker.accuracy * 100
        return {
            "version":    self.VERSION,
            "n_features": N_FEATURES,
            "n_trained":  self.n_trained,
            "n_wins":     self.n_wins,
            "n_losses":   self.n_losses,
            "win_rate":   round(wr, 1),
            "accuracy":   round(acc, 1),
            "avg_roi":    round(self.tracker.avg_roi, 2),
            "replay_n":   self.replay.total,
            "active":     self._active,
            "ready":      self.n_trained >= MIN_SAMPLES_TO_PREDICT and self._active,
            "lr":         round(self.net.lr, 6),
            "block_threshold": BLOCK_THRESHOLD,
        }

    # ── 수동 리셋 ────────────────────────────────────────────────────────────
    def reset(self):
        self.__init__(self.model_path)
        if os.path.exists(self.model_path):
            os.remove(self.model_path)
        logger.info("NeuralScorer v%s: model reset", self.VERSION)
