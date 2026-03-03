"""
neural_scorer_v4.py  v4.0 — Regime-Aware Attention Neural Signal Scorer
────────────────────────────────────────────────────────────────────────────
[프리미엄 Phase 2] v3 대비 핵심 개선:
  1. Feature Attention: 피처 간 상호작용 + 중요도 자동 학습
  2. Regime-Conditioned Heads: trend/chop별 분리 예측 (가중 합산)
  3. MC Dropout: 순전파 시 드롭아웃 → 불확실성 추정
  4. 26개 피처: v3(20) + trade context(6)
  5. Dynamic Block Threshold: 레짐·정확도 기반 적응형
  6. Gradient Clipping + Cosine Annealing LR
  7. v3 모델 자동 마이그레이션 (20→26 피처 zero-pad)

아키텍처:
  Input(26) → FeatureAttention(26→26) → Dropout(0.15)
    → Dense(64, LeakyReLU) → Dropout(0.10)
    → Dense(32, LeakyReLU) → Dropout(0.10)
    ├→ TrendHead:  Dense(16,ReLU) → Dense(1,Sigmoid) → P(win|trend)
    ├→ ChopHead:   Dense(16,ReLU) → Dense(1,Sigmoid) → P(win|chop)
    ├→ ROI_Head:   Dense(16,ReLU) → Dense(1,Linear)  → E[ROI%]
    └→ Gate:       regime_weight * trend + (1-w) * chop → P(win)

피처 벡터 (z-score 정규화):
  [ 0-19] v3 기존 20개 (하위 호환)
  ── v4 신규 ──
  [20] recent_win_rate     최근 10거래 승률
  [21] recent_avg_roi      최근 10거래 평균 ROI
  [22] drawdown_pct        현재 세션 드로다운 %
  [23] time_since_last      마지막 거래 이후 경과(분, 정규화)
  [24] regime_duration_min  현재 레짐 유지 시간(분)
  [25] tuner_confidence     AutoTuner confidence 값
"""

from __future__ import annotations

import os
import json
import math
import time
import logging
from collections import deque
from typing import Dict, List, Optional, Tuple, Any

import numpy as np

logger = logging.getLogger(__name__)

# ── v4 상수 ────────────────────────────────────────────────────────────────
N_FEATURES_V4 = 26              # v4: 26개 피처
N_FEATURES_V3 = 20              # v3 호환용
N_FEATURES_V2 = 12              # v2 호환용

HIDDEN1 = 64                    # v4: 48→64
HIDDEN2 = 32                    # v4: 24→32
HEAD_DIM = 16                   # 레짐별 헤드 히든
REPLAY_CAPACITY = 3000          # v4: 2000→3000
MIN_REPLAY_TO_TRAIN = 20        # v4: 30→20 (cold start 개선)
MINI_BATCH_SIZE = 24            # v4: 16→24
TRAIN_ITERS_PER_STEP = 4        # v4: 3→4
MIN_SAMPLES_TO_PREDICT = 30     # v4: 50→30 (cold start 개선)
ACCURACY_WINDOW = 50
ACCURACY_MIN = 0.51             # v4: 0.52→0.51 (허용 범위 확대)
BLOCK_THRESHOLD_BASE = 0.25
BLOCK_THRESHOLD_CHOP = 0.30     # chop에서는 더 보수적 (v4 신규)
BLOCK_THRESHOLD_TREND = 0.20    # trend에서는 더 공격적 (v4 신규)
LR_MIN = 0.0003
LR_MAX = 0.006
LR_INIT = 0.002
ROI_LOSS_WEIGHT = 0.25          # 0.75*BCE + 0.25*Huber
DROPOUT_TRAIN = 0.15            # 학습 시 드롭아웃 비율
DROPOUT_HIDDEN = 0.10           # 히든 레이어 드롭아웃
MC_SAMPLES = 5                  # MC Dropout 샘플 수
GRAD_CLIP_NORM = 5.0            # 그래디언트 클리핑 L2 norm
COSINE_T_MAX = 200              # Cosine Annealing 주기 (거래 수)


# ─────────────────────────────────────────────────────────────────────────────
# 수치 유틸
# ─────────────────────────────────────────────────────────────────────────────
def leaky_relu(x: np.ndarray, alpha: float = 0.01) -> np.ndarray:
    return np.where(x > 0, x, alpha * x)

def leaky_relu_grad(x: np.ndarray, alpha: float = 0.01) -> np.ndarray:
    return np.where(x > 0, 1.0, alpha)

def relu(x: np.ndarray) -> np.ndarray:
    return np.maximum(0, x)

def relu_grad(x: np.ndarray) -> np.ndarray:
    return (x > 0).astype(float)

def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -20.0, 20.0)))

def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x, axis=-1, keepdims=True))
    return e / (e.sum(axis=-1, keepdims=True) + 1e-8)

def safe_float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if (f != f or math.isinf(f)) else f
    except Exception:
        return default

def _huber_grad(pred: np.ndarray, target: np.ndarray, delta: float = 1.0) -> np.ndarray:
    diff = pred - target
    return np.where(np.abs(diff) <= delta, diff, delta * np.sign(diff))

def _clip_grad_norm(grads: list, max_norm: float = GRAD_CLIP_NORM) -> list:
    """L2 norm 기반 그래디언트 클리핑."""
    total_norm = 0.0
    for g in grads:
        total_norm += np.sum(g ** 2)
    total_norm = math.sqrt(total_norm)
    if total_norm > max_norm:
        scale = max_norm / (total_norm + 1e-8)
        return [g * scale for g in grads]
    return grads

def cosine_lr(base_lr: float, step: int, t_max: int = COSINE_T_MAX) -> float:
    """Cosine Annealing with Warm Restart."""
    return LR_MIN + 0.5 * (base_lr - LR_MIN) * (1 + math.cos(math.pi * (step % t_max) / t_max))


# ─────────────────────────────────────────────────────────────────────────────
# 온라인 z-score 정규화 (Welford 알고리즘) — v3 호환
# ─────────────────────────────────────────────────────────────────────────────
class RunningNorm:
    def __init__(self, n_features: int):
        self.n = 0
        self.n_features = n_features
        self.mean = np.zeros(n_features)
        self.M2 = np.ones(n_features)

    @property
    def std(self) -> np.ndarray:
        var = self.M2 / max(self.n, 1)
        return np.sqrt(np.maximum(var, 1e-8))

    def update(self, x: np.ndarray):
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        delta2 = x - self.mean
        self.M2 += delta * delta2

    def normalize(self, x: np.ndarray) -> np.ndarray:
        z = (x - self.mean) / self.std
        return np.clip(z, -4.0, 4.0)

    def expand(self, new_n: int):
        if new_n <= self.n_features:
            return
        old_n = self.n_features
        self.mean = np.concatenate([self.mean, np.zeros(new_n - old_n)])
        self.M2 = np.concatenate([self.M2, np.ones(new_n - old_n)])
        self.n_features = new_n

    def to_dict(self) -> dict:
        return {"n": self.n, "mean": self.mean.tolist(), "M2": self.M2.tolist()}

    def from_dict(self, d: dict):
        self.n = int(d.get("n", 0))
        self.mean = np.array(d.get("mean", self.mean.tolist()))
        self.M2 = np.array(d.get("M2", self.M2.tolist()))
        self.n_features = len(self.mean)


# ─────────────────────────────────────────────────────────────────────────────
# Feature Attention Layer
# ─────────────────────────────────────────────────────────────────────────────
class FeatureAttention:
    """
    피처별 중요도 가중치를 학습하는 self-attention 레이어.
    score_i = softmax(W_attn @ x)  →  x_attended = x * score
    """
    def __init__(self, n_features: int, lr: float = 0.001):
        self.n = n_features
        self.W_attn = np.random.default_rng(42).normal(0, 0.1, (n_features,))
        self.lr = lr
        # Adam state
        self._m = np.zeros(n_features)
        self._v = np.zeros(n_features)
        self._t = 0

    def forward(self, x: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """x: (batch, n) → attended_x: (batch, n), attention_weights: (batch, n)"""
        if x.ndim == 1:
            x = x[None, :]
        scores = x * self.W_attn[None, :]  # element-wise
        weights = softmax(scores)  # (batch, n)
        attended = x * (1.0 + weights)  # residual attention: 원본 + 가중
        return attended, weights

    def backward(self, x: np.ndarray, grad_out: np.ndarray, weights: np.ndarray):
        """그래디언트를 W_attn으로 전파."""
        # Simplified gradient: dL/dW ≈ mean(grad_out * x * d_softmax/d_score)
        # d_softmax approximation: w * (1 - w) for each element
        if x.ndim == 1:
            x = x[None, :]
        dsoftmax = weights * (1.0 - weights)
        dW = np.mean(grad_out * x * dsoftmax, axis=0)

        # Adam update
        self._t += 1
        self._m = 0.9 * self._m + 0.1 * dW
        self._v = 0.999 * self._v + 0.001 * dW ** 2
        m_hat = self._m / (1 - 0.9 ** self._t)
        v_hat = self._v / (1 - 0.999 ** self._t)
        self.W_attn -= self.lr * m_hat / (np.sqrt(v_hat) + 1e-8)
        np.clip(self.W_attn, -3.0, 3.0, out=self.W_attn)

    def to_dict(self) -> dict:
        return {"W_attn": self.W_attn.tolist(), "t": self._t,
                "m": self._m.tolist(), "v": self._v.tolist()}

    def from_dict(self, d: dict):
        if "W_attn" in d:
            self.W_attn = np.array(d["W_attn"])
            self._t = int(d.get("t", 0))
            self._m = np.array(d.get("m", np.zeros_like(self.W_attn)))
            self._v = np.array(d.get("v", np.zeros_like(self.W_attn)))
            self.n = len(self.W_attn)

    def expand(self, new_n: int):
        if new_n <= self.n:
            return
        pad = new_n - self.n
        self.W_attn = np.concatenate([self.W_attn, np.zeros(pad)])
        self._m = np.concatenate([self._m, np.zeros(pad)])
        self._v = np.concatenate([self._v, np.zeros(pad)])
        self.n = new_n


# ─────────────────────────────────────────────────────────────────────────────
# Regime-Aware Dual-Path Network
# ─────────────────────────────────────────────────────────────────────────────
class RegimeAwareNet:
    """
    Shared backbone + Regime-conditioned heads.

    Input(26) → Attention(26) → Dense(64) → Dense(32)
      ├→ TrendHead(16→1, Sigmoid)   P(win|trend)
      ├→ ChopHead(16→1, Sigmoid)    P(win|chop)
      ├→ ROIHead(16→1, Linear)      E[ROI%]
      └→ Gate(regime_feature)        weighted combination
    """

    def __init__(self, n_in: int = N_FEATURES_V4, h1: int = HIDDEN1, h2: int = HIDDEN2,
                 head_dim: int = HEAD_DIM, lr: float = LR_INIT, wd: float = 1e-4):
        self.n_in, self.h1, self.h2, self.head_dim = n_in, h1, h2, head_dim
        self.lr, self.base_lr, self.wd = lr, lr, wd
        self.t = 0

        rng = np.random.default_rng(42)

        # Shared backbone
        self.W1 = rng.normal(0, math.sqrt(2.0 / n_in), (h1, n_in))
        self.b1 = np.zeros(h1)
        self.W2 = rng.normal(0, math.sqrt(2.0 / h1), (h2, h1))
        self.b2 = np.zeros(h2)

        # Trend head: h2 → head_dim → 1 (sigmoid)
        self.Wt1 = rng.normal(0, math.sqrt(2.0 / h2), (head_dim, h2))
        self.bt1 = np.zeros(head_dim)
        self.Wt2 = rng.normal(0, math.sqrt(2.0 / head_dim), (1, head_dim))
        self.bt2 = np.zeros(1)

        # Chop head: h2 → head_dim → 1 (sigmoid)
        self.Wc1 = rng.normal(0, math.sqrt(2.0 / h2), (head_dim, h2))
        self.bc1 = np.zeros(head_dim)
        self.Wc2 = rng.normal(0, math.sqrt(2.0 / head_dim), (1, head_dim))
        self.bc2 = np.zeros(1)

        # ROI head: h2 → head_dim → 1 (linear)
        self.Wr1 = rng.normal(0, math.sqrt(2.0 / h2), (head_dim, h2))
        self.br1 = np.zeros(head_dim)
        self.Wr2 = rng.normal(0, math.sqrt(2.0 / head_dim), (1, head_dim))
        self.br2 = np.zeros(1)

        self._params = [
            self.W1, self.b1, self.W2, self.b2,         # shared
            self.Wt1, self.bt1, self.Wt2, self.bt2,     # trend head
            self.Wc1, self.bc1, self.Wc2, self.bc2,     # chop head
            self.Wr1, self.br1, self.Wr2, self.br2,     # roi head
        ]
        self._m = [np.zeros_like(p) for p in self._params]
        self._v = [np.zeros_like(p) for p in self._params]

        # Attention layer
        self.attention = FeatureAttention(n_in)

    def _dropout_mask(self, shape, rate: float, training: bool) -> np.ndarray:
        if not training or rate <= 0:
            return np.ones(shape)
        mask = (np.random.random(shape) > rate).astype(float) / (1.0 - rate)
        return mask

    def _forward(self, X: np.ndarray, regime_weights: np.ndarray = None,
                 training: bool = False) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
        """
        X: (batch, n_in)
        regime_weights: (batch,) — 1.0=pure trend, 0.0=pure chop, 0.5=mixed

        Returns: prob, roi_pred, trend_prob, chop_prob, cache
        """
        batch = X.shape[0] if X.ndim > 1 else 1
        if X.ndim == 1:
            X = X[None, :]
        if regime_weights is None:
            regime_weights = np.full(batch, 0.5)

        # Feature attention
        X_att, attn_w = self.attention.forward(X)

        # Dropout on input
        drop_in = self._dropout_mask(X_att.shape, DROPOUT_TRAIN, training)
        X_att = X_att * drop_in

        # Shared backbone
        z1 = X_att @ self.W1.T + self.b1
        a1 = leaky_relu(z1)
        drop1 = self._dropout_mask(a1.shape, DROPOUT_HIDDEN, training)
        a1 = a1 * drop1

        z2 = a1 @ self.W2.T + self.b2
        a2 = leaky_relu(z2)
        drop2 = self._dropout_mask(a2.shape, DROPOUT_HIDDEN, training)
        a2 = a2 * drop2

        # Trend head
        zt1 = a2 @ self.Wt1.T + self.bt1
        at1 = relu(zt1)
        zt2 = at1 @ self.Wt2.T + self.bt2
        trend_prob = sigmoid(zt2).ravel()

        # Chop head
        zc1 = a2 @ self.Wc1.T + self.bc1
        ac1 = relu(zc1)
        zc2 = ac1 @ self.Wc2.T + self.bc2
        chop_prob = sigmoid(zc2).ravel()

        # ROI head
        zr1 = a2 @ self.Wr1.T + self.br1
        ar1 = relu(zr1)
        zr2 = ar1 @ self.Wr2.T + self.br2
        roi_pred = zr2.ravel()

        # Gated combination: regime_weight blends trend vs chop
        prob = regime_weights * trend_prob + (1.0 - regime_weights) * chop_prob

        cache = {
            "X": X, "X_att": X_att, "attn_w": attn_w,
            "z1": z1, "a1": a1, "z2": z2, "a2": a2,
            "zt1": zt1, "at1": at1, "zt2": zt2,
            "zc1": zc1, "ac1": ac1, "zc2": zc2,
            "zr1": zr1, "ar1": ar1, "zr2": zr2,
            "trend_prob": trend_prob, "chop_prob": chop_prob,
            "regime_weights": regime_weights,
            "drop_in": drop_in, "drop1": drop1, "drop2": drop2,
        }
        return prob, roi_pred, trend_prob, chop_prob, cache

    def _backward(self, cache: dict, y: np.ndarray, roi_targets: np.ndarray,
                  weights: np.ndarray, roi_weight: float = ROI_LOSS_WEIGHT):
        X = cache["X"]
        X_att = cache["X_att"]
        a1, a2 = cache["a1"], cache["a2"]
        z1, z2 = cache["z1"], cache["z2"]
        at1, ac1, ar1 = cache["at1"], cache["ac1"], cache["ar1"]
        zt1, zc1, zr1 = cache["zt1"], cache["zc1"], cache["zr1"]
        zt2, zc2, zr2 = cache["zt2"], cache["zc2"], cache["zr2"]
        rw = cache["regime_weights"]
        batch = max(len(y), 1)
        cls_w = 1.0 - roi_weight

        # ── Trend head BCE gradient ──
        tp = sigmoid(zt2.reshape(-1, 1))
        dzt2 = cls_w * rw[:, None] * (weights[:, None] * (tp - y[:, None])) / batch
        dWt2 = dzt2.T @ at1 + self.wd * self.Wt2
        dbt2 = dzt2.sum(axis=0)
        dat1 = dzt2 @ self.Wt2
        dzt1 = dat1 * relu_grad(zt1)
        dWt1 = dzt1.T @ a2 + self.wd * self.Wt1
        dbt1 = dzt1.sum(axis=0)

        # ── Chop head BCE gradient ──
        cp = sigmoid(zc2.reshape(-1, 1))
        dzc2 = cls_w * (1.0 - rw[:, None]) * (weights[:, None] * (cp - y[:, None])) / batch
        dWc2 = dzc2.T @ ac1 + self.wd * self.Wc2
        dbc2 = dzc2.sum(axis=0)
        dac1 = dzc2 @ self.Wc2
        dzc1 = dac1 * relu_grad(zc1)
        dWc1 = dzc1.T @ a2 + self.wd * self.Wc1
        dbc1 = dzc1.sum(axis=0)

        # ── ROI head Huber gradient ──
        roi_pred = zr2.ravel()
        dzr2_raw = _huber_grad(roi_pred, roi_targets)
        dzr2 = roi_weight * (weights[:, None] * dzr2_raw[:, None]) / batch
        dWr2 = dzr2.T @ ar1 + self.wd * self.Wr2
        dbr2 = dzr2.sum(axis=0)
        dar1 = dzr2 @ self.Wr2
        dzr1 = dar1 * relu_grad(zr1)
        dWr1 = dzr1.T @ a2 + self.wd * self.Wr1
        dbr1 = dzr1.sum(axis=0)

        # ── Shared backbone gradient (sum from all heads) ──
        da2 = dzt1 @ self.Wt1 + dzc1 @ self.Wc1 + dzr1 @ self.Wr1
        da2 = da2 * cache["drop2"]
        dz2 = da2 * leaky_relu_grad(z2)
        dW2 = dz2.T @ a1 + self.wd * self.W2
        db2 = dz2.sum(axis=0)

        da1 = dz2 @ self.W2
        da1 = da1 * cache["drop1"]
        dz1 = da1 * leaky_relu_grad(z1)
        dW1 = dz1.T @ X_att + self.wd * self.W1
        db1 = dz1.sum(axis=0)

        grads = [dW1, db1, dW2, db2,
                 dWt1, dbt1, dWt2, dbt2,
                 dWc1, dbc1, dWc2, dbc2,
                 dWr1, dbr1, dWr2, dbr2]

        # Gradient clipping
        grads = _clip_grad_norm(grads)

        # Attention backward (backbone → attention)
        dX_att = dz1 @ self.W1
        self.attention.backward(X, dX_att, cache["attn_w"])

        # Adam update
        self.t += 1
        bc1_adam = 1.0 - 0.9 ** self.t
        bc2_adam = 1.0 - 0.999 ** self.t
        for i, (p, g) in enumerate(zip(self._params, grads)):
            self._m[i] = 0.9 * self._m[i] + 0.1 * g
            self._v[i] = 0.999 * self._v[i] + 0.001 * g ** 2
            p -= self.lr * (self._m[i] / bc1_adam) / (np.sqrt(self._v[i] / bc2_adam) + 1e-8)
            np.clip(p, -10.0, 10.0, out=p)

    def predict_one(self, x: np.ndarray, regime_weight: float = 0.5) -> Tuple[float, float, float]:
        """Returns (win_prob, expected_roi%, uncertainty)"""
        rw = np.array([regime_weight])
        prob, roi, tp, cp, _ = self._forward(x[None, :], rw, training=False)
        return float(prob[0]), float(roi[0]), 0.0

    def predict_mc(self, x: np.ndarray, regime_weight: float = 0.5,
                   n_samples: int = MC_SAMPLES) -> Tuple[float, float, float]:
        """MC Dropout: n_samples회 forward → 평균/분산으로 불확실성 추정."""
        probs, rois = [], []
        rw = np.array([regime_weight])
        for _ in range(n_samples):
            p, r, _, _, _ = self._forward(x[None, :], rw, training=True)
            probs.append(float(p[0]))
            rois.append(float(r[0]))
        mean_prob = np.mean(probs)
        mean_roi = np.mean(rois)
        uncertainty = np.std(probs)  # 확률 분산 = 불확실성
        return float(np.clip(mean_prob, 0.0, 1.0)), float(np.clip(mean_roi, -20, 20)), float(uncertainty)

    def fit_batch(self, X: np.ndarray, y: np.ndarray, roi_targets: np.ndarray,
                  weights: np.ndarray, regime_weights: np.ndarray):
        prob, roi, _, _, cache = self._forward(X, regime_weights, training=True)
        self._backward(cache, y, roi_targets, weights)

    # ── 직렬화 ───────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "t": int(self.t), "n_in": self.n_in, "h1": self.h1, "h2": self.h2,
            "head_dim": self.head_dim, "base_lr": self.base_lr,
            "W1": self.W1.tolist(), "b1": self.b1.tolist(),
            "W2": self.W2.tolist(), "b2": self.b2.tolist(),
            "Wt1": self.Wt1.tolist(), "bt1": self.bt1.tolist(),
            "Wt2": self.Wt2.tolist(), "bt2": self.bt2.tolist(),
            "Wc1": self.Wc1.tolist(), "bc1": self.bc1.tolist(),
            "Wc2": self.Wc2.tolist(), "bc2": self.bc2.tolist(),
            "Wr1": self.Wr1.tolist(), "br1": self.br1.tolist(),
            "Wr2": self.Wr2.tolist(), "br2": self.br2.tolist(),
            "attention": self.attention.to_dict(),
            "m": [a.tolist() for a in self._m],
            "v": [a.tolist() for a in self._v],
        }

    def from_dict(self, d: dict, target_n_in: int = N_FEATURES_V4):
        """v3 모델 자동 마이그레이션 지원."""
        self.t = int(d.get("t", 0))
        self.base_lr = float(d.get("base_lr", LR_INIT))

        # ── v3→v4 backbone 마이그레이션 ──
        loaded_W1 = np.array(d.get("W1", self.W1))
        loaded_n_in = loaded_W1.shape[1] if loaded_W1.ndim == 2 else N_FEATURES_V3
        is_migration = loaded_n_in < target_n_in or loaded_W1.shape[0] < HIDDEN1

        if loaded_n_in < target_n_in:
            pad = np.zeros((loaded_W1.shape[0], target_n_in - loaded_n_in))
            loaded_W1 = np.hstack([loaded_W1, pad])
            logger.info("NeuralScorer v4 migration: W1 features %d→%d", loaded_n_in, target_n_in)

        if loaded_W1.shape[0] < HIDDEN1:
            rng = np.random.default_rng(99)
            extra = rng.normal(0, 0.01, (HIDDEN1 - loaded_W1.shape[0], target_n_in))
            loaded_W1 = np.vstack([loaded_W1, extra])

        self.W1 = loaded_W1
        self.b1 = np.array(d.get("b1", self.b1))
        if len(self.b1) < HIDDEN1:
            self.b1 = np.concatenate([self.b1, np.zeros(HIDDEN1 - len(self.b1))])

        # W2
        loaded_W2 = np.array(d.get("W2", self.W2))
        if loaded_W2.shape[1] < HIDDEN1:
            loaded_W2 = np.hstack([loaded_W2, np.zeros((loaded_W2.shape[0], HIDDEN1 - loaded_W2.shape[1]))])
        if loaded_W2.shape[0] < HIDDEN2:
            rng = np.random.default_rng(77)
            extra = rng.normal(0, 0.01, (HIDDEN2 - loaded_W2.shape[0], HIDDEN1))
            loaded_W2 = np.vstack([loaded_W2, extra])
        self.W2 = loaded_W2
        self.b2 = np.array(d.get("b2", self.b2))
        if len(self.b2) < HIDDEN2:
            self.b2 = np.concatenate([self.b2, np.zeros(HIDDEN2 - len(self.b2))])

        # v3→v4: 레짐 헤드 마이그레이션
        if "Wt1" in d:
            # v4 native format
            self.Wt1 = np.array(d["Wt1"]); self.bt1 = np.array(d["bt1"])
            self.Wt2 = np.array(d["Wt2"]); self.bt2 = np.array(d["bt2"])
            self.Wc1 = np.array(d["Wc1"]); self.bc1 = np.array(d["bc1"])
            self.Wc2 = np.array(d["Wc2"]); self.bc2 = np.array(d["bc2"])
            self.Wr1 = np.array(d["Wr1"]); self.br1 = np.array(d["br1"])
            self.Wr2 = np.array(d["Wr2"]); self.br2 = np.array(d["br2"])
        elif "W3" in d:
            # v3 format: single classification head → migrate to trend head
            loaded_W3 = np.array(d["W3"])
            if loaded_W3.shape[1] < HIDDEN2:
                loaded_W3 = np.hstack([loaded_W3, np.zeros((1, HIDDEN2 - loaded_W3.shape[1]))])
            # Expand v3 single head to v4 trend head intermediate layer
            rng = np.random.default_rng(44)
            self.Wt1 = rng.normal(0, 0.01, (HEAD_DIM, HIDDEN2))
            self.bt1 = np.zeros(HEAD_DIM)
            self.Wt2 = rng.normal(0, 0.01, (1, HEAD_DIM))
            self.bt2 = np.array(d.get("b3", np.zeros(1)))
            # Copy trend → chop (start identical, diverge through learning)
            self.Wc1 = self.Wt1.copy()
            self.bc1 = self.bt1.copy()
            self.Wc2 = self.Wt2.copy()
            self.bc2 = self.bt2.copy()
            # ROI head from v3
            if "W3r" in d:
                loaded_W3r = np.array(d["W3r"])
                if loaded_W3r.shape[1] < HIDDEN2:
                    loaded_W3r = np.hstack([loaded_W3r, np.zeros((1, HIDDEN2 - loaded_W3r.shape[1]))])
                self.Wr1 = rng.normal(0, 0.01, (HEAD_DIM, HIDDEN2))
                self.br1 = np.zeros(HEAD_DIM)
                self.Wr2 = rng.normal(0, 0.01, (1, HEAD_DIM))
                self.br2 = np.array(d.get("b3r", np.zeros(1)))
            logger.info("NeuralScorer v4 migration: v3 single head → regime-aware heads")

        # Attention
        if "attention" in d:
            self.attention.from_dict(d["attention"])
        if self.attention.n < target_n_in:
            self.attention.expand(target_n_in)

        self.n_in = target_n_in
        self.h1 = self.W1.shape[0]
        self.h2 = self.W2.shape[0]
        self._params = [
            self.W1, self.b1, self.W2, self.b2,
            self.Wt1, self.bt1, self.Wt2, self.bt2,
            self.Wc1, self.bc1, self.Wc2, self.bc2,
            self.Wr1, self.br1, self.Wr2, self.br2,
        ]

        # Adam state
        if "m" in d and "v" in d and not is_migration:
            loaded_m = [np.array(a) for a in d["m"]]
            loaded_v = [np.array(a) for a in d["v"]]
            self._m, self._v = [], []
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
# 피처 빌더 v4
# ─────────────────────────────────────────────────────────────────────────────
def build_feature_vector_v4(
    # ── v3 기존 파라미터 (하위 호환) ──
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
    price: float = 0.0,
    high_24h: float = 0.0,
    low_24h: float = 0.0,
    rsi_14: float = 50.0,
    atr: float = 0.0,
    vol_regime_ratio: float = 1.0,
    open_pos_count: int = 0,
    # ── v4 신규 피처 ──
    recent_win_rate: float = 0.5,
    recent_avg_roi: float = 0.0,
    drawdown_pct: float = 0.0,
    time_since_last_trade_min: float = 60.0,
    regime_duration_min: float = 5.0,
    tuner_confidence: float = 0.2,
) -> np.ndarray:
    """26개 피처 벡터 (정규화 전 원시값)."""
    vol = max(safe_float(volatility), 1e-6)
    ts = entry_ts or time.time()
    hour = (ts % 86400) / 3600.0
    hour_rad = hour * 2 * math.pi / 24.0
    regime_map = {"trend_up": 1.0, "trend_down": -1.0, "chop": 0.0}
    dir_slope = (safe_float(mtf_slope_1m) + safe_float(mtf_slope_5m)) / 2.0
    dir_up = dir_slope > 0
    dir_match = 1.0 if (direction == "LONG") == dir_up else 0.0

    _range = safe_float(high_24h) - safe_float(low_24h)
    price_pos = (safe_float(price) - safe_float(low_24h)) / max(_range, 1e-8) if _range > 0 else 0.5
    price_pos = max(0.0, min(1.0, price_pos))
    atr_ratio = safe_float(atr) / max(safe_float(price), 1e-8) if price > 0 else 0.0
    slope_div = safe_float(mtf_slope_1m) - safe_float(mtf_slope_5m)
    funding_mag = abs(safe_float(funding_rate))
    dow_rad = ((ts % 604800) / 604800.0) * 2 * math.pi

    return np.array([
        # ── v3 기존 20개 ──
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
        price_pos,                                              # 12 price_position
        safe_float(rsi_14),                                     # 13 rsi_14
        atr_ratio,                                              # 14 atr_ratio
        slope_div,                                              # 15 slope_divergence
        funding_mag,                                            # 16 funding_magnitude
        safe_float(vol_regime_ratio),                           # 17 vol_regime_ratio
        float(open_pos_count),                                  # 18 open_pos_count
        math.sin(dow_rad),                                      # 19 day_of_week_sin
        # ── v4 신규 6개 ──
        safe_float(recent_win_rate),                            # 20 recent_win_rate
        safe_float(recent_avg_roi),                             # 21 recent_avg_roi
        safe_float(drawdown_pct),                               # 22 drawdown_pct
        min(safe_float(time_since_last_trade_min), 300.0) / 60.0,  # 23 time_since_last (정규화)
        min(safe_float(regime_duration_min), 120.0) / 30.0,    # 24 regime_duration (정규화)
        safe_float(tuner_confidence),                           # 25 tuner_confidence
    ], dtype=float)


def regime_to_weight(regime: str) -> float:
    """레짐을 trend/chop 가중치로 변환 (1.0=pure trend, 0.0=pure chop)."""
    if regime == "trend_up":
        return 0.9
    elif regime == "trend_down":
        return 0.85  # 하락 추세는 약간 덜 확신
    else:
        return 0.15  # chop


# ─────────────────────────────────────────────────────────────────────────────
# Replay Buffer v4 (레짐 정보 추가 저장)
# ─────────────────────────────────────────────────────────────────────────────
class ReplayBufferV4:
    """Win/Loss + 레짐 가중치 저장."""
    def __init__(self, capacity: int = REPLAY_CAPACITY):
        self.cap = capacity // 2
        self.wins:   deque[Tuple[np.ndarray, float, float]] = deque(maxlen=self.cap)  # (feat, roi, regime_w)
        self.losses: deque[Tuple[np.ndarray, float, float]] = deque(maxlen=self.cap)

    def push(self, x: np.ndarray, label: float, roi: float, regime_weight: float):
        if label == 1.0:
            self.wins.append((x.copy(), float(roi), float(regime_weight)))
        else:
            self.losses.append((x.copy(), float(roi), float(regime_weight)))

    def sample(self, batch_size: int) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
        half = batch_size // 2
        if len(self.wins) < max(4, half) or len(self.losses) < max(4, half):
            return None
        rng = np.random.default_rng()
        w_idx = rng.integers(0, len(self.wins), size=half)
        l_idx = rng.integers(0, len(self.losses), size=half)
        w_list = list(self.wins)
        l_list = list(self.losses)
        X = np.array([w_list[i][0] for i in w_idx] + [l_list[i][0] for i in l_idx])
        y = np.array([1.0] * half + [0.0] * half)
        roi = np.array([w_list[i][1] for i in w_idx] + [l_list[i][1] for i in l_idx])
        rw = np.array([w_list[i][2] for i in w_idx] + [l_list[i][2] for i in l_idx])
        W = np.ones(batch_size)
        return X, y, roi, W, rw

    @property
    def total(self) -> int:
        return len(self.wins) + len(self.losses)

    def expand_features(self, old_n: int, new_n: int):
        if new_n <= old_n:
            return
        pad_size = new_n - old_n
        new_wins = deque(maxlen=self.cap)
        for item in self.wins:
            x = item[0]
            if len(x) < new_n:
                x = np.concatenate([x, np.zeros(pad_size)])
            new_wins.append((x, item[1], item[2] if len(item) > 2 else 0.5))
        self.wins = new_wins
        new_losses = deque(maxlen=self.cap)
        for item in self.losses:
            x = item[0]
            if len(x) < new_n:
                x = np.concatenate([x, np.zeros(pad_size)])
            new_losses.append((x, item[1], item[2] if len(item) > 2 else 0.5))
        self.losses = new_losses

    def to_dict(self) -> dict:
        return {
            "wins": [(x.tolist(), r, rw) for x, r, rw in self.wins],
            "losses": [(x.tolist(), r, rw) for x, r, rw in self.losses],
        }

    def from_dict(self, d: dict):
        for item in d.get("wins", []):
            if len(item) >= 3:
                self.wins.append((np.array(item[0]), float(item[1]), float(item[2])))
            elif len(item) == 2:
                self.wins.append((np.array(item[0]), float(item[1]), 0.5))
        for item in d.get("losses", []):
            if len(item) >= 3:
                self.losses.append((np.array(item[0]), float(item[1]), float(item[2])))
            elif len(item) == 2:
                self.losses.append((np.array(item[0]), float(item[1]), 0.5))


# ─────────────────────────────────────────────────────────────────────────────
# Performance Tracker (v3 호환)
# ─────────────────────────────────────────────────────────────────────────────
class PerformanceTracker:
    def __init__(self, window: int = ACCURACY_WINDOW):
        self.window = window
        self._records: deque[int] = deque(maxlen=window)
        self._roi_records: deque[float] = deque(maxlen=window)
        self._regime_records: deque[str] = deque(maxlen=window)

    def record(self, predicted_prob: float, actual_label: float, roi: float, regime: str = ""):
        predicted_win = predicted_prob >= 0.5
        actual_win = actual_label >= 0.5
        self._records.append(1 if predicted_win == actual_win else 0)
        self._roi_records.append(roi)
        self._regime_records.append(regime)

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

    def accuracy_by_regime(self, regime: str) -> float:
        """특정 레짐의 정확도 (v4 신규)."""
        hits, total = 0, 0
        for rec, reg in zip(self._records, self._regime_records):
            if reg == regime:
                hits += rec
                total += 1
        return hits / max(total, 1)

    def is_valid(self) -> bool:
        if self.n_evaluated < 15:
            return True
        return self.accuracy >= ACCURACY_MIN

    def to_dict(self) -> dict:
        return {
            "records": list(self._records),
            "roi_records": list(self._roi_records),
            "regime_records": list(self._regime_records),
        }

    def from_dict(self, d: dict):
        for v in d.get("records", []):
            self._records.append(int(v))
        for v in d.get("roi_records", []):
            self._roi_records.append(float(v))
        for v in d.get("regime_records", []):
            self._regime_records.append(str(v))


# ─────────────────────────────────────────────────────────────────────────────
# NeuralScorer v4.0 — 메인 클래스
# ─────────────────────────────────────────────────────────────────────────────
class NeuralScorerV4:
    """
    [프리미엄 Phase 2] Regime-Aware Attention Neural Signal Scorer.

    tick_engine 사용 흐름:
        feat = build_feature_vector_v4(...)                  # 26개 피처
        prob, roi, uncertainty = scorer.predict(feat, regime) # 예측 + 불확실성
        scorer.record_entry(symbol, feat, regime)             # 피처 저장

        scorer.learn_from_outcome(symbol, roi_pct, pnl, regime)  # 학습
    """
    VERSION = "4.0"

    def __init__(self, model_path: str = "logs/neural_scorer_v4.json"):
        self.model_path = model_path
        self.net = RegimeAwareNet(n_in=N_FEATURES_V4)
        self.norm = RunningNorm(N_FEATURES_V4)
        self.replay = ReplayBufferV4(REPLAY_CAPACITY)
        self.tracker = PerformanceTracker(ACCURACY_WINDOW)
        self.n_trained = 0
        self.n_wins = 0
        self.n_losses = 0
        self._pending: Dict[str, Tuple[np.ndarray, float, float, float, str]] = {}
        # symbol → (raw_feat, prob, roi_pred, uncertainty, regime)
        self._last_save = 0.0
        self._active = True
        self._feature_importance: Optional[np.ndarray] = None
        self.load()

    # ── 저장 / 복원 ──────────────────────────────────────────────────────────
    def save(self):
        try:
            os.makedirs(os.path.dirname(self.model_path) or ".", exist_ok=True)
            data = {
                "version": self.VERSION,
                "n_features": N_FEATURES_V4,
                "n_trained": self.n_trained,
                "n_wins": self.n_wins,
                "n_losses": self.n_losses,
                "active": self._active,
                "lr": self.net.lr,
                "base_lr": self.net.base_lr,
                "net": self.net.to_dict(),
                "norm": self.norm.to_dict(),
                "replay": self.replay.to_dict(),
                "tracker": self.tracker.to_dict(),
                "saved_at": time.time(),
            }
            tmp = self.model_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, self.model_path)
            self._last_save = time.time()
        except Exception as e:
            logger.warning("NeuralScorer v4 save failed: %s", e)

    def load(self):
        # v4 파일 먼저 시도
        if not os.path.exists(self.model_path):
            # v3 파일에서 마이그레이션 시도
            v3_path = self.model_path.replace("_v4", "")
            if os.path.exists(v3_path):
                self._migrate_from_v3(v3_path)
                return
            logger.info("NeuralScorer v%s: no saved model, starting fresh (n_features=%d)",
                        self.VERSION, N_FEATURES_V4)
            return
        try:
            with open(self.model_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.n_trained = int(data.get("n_trained", 0))
            self.n_wins = int(data.get("n_wins", 0))
            self.n_losses = int(data.get("n_losses", 0))
            self._active = bool(data.get("active", True))

            saved_n_features = int(data.get("n_features", N_FEATURES_V3))

            if "net" in data:
                self.net.from_dict(data["net"], target_n_in=N_FEATURES_V4)
            if "lr" in data:
                self.net.lr = float(data["lr"])
            if "base_lr" in data:
                self.net.base_lr = float(data["base_lr"])

            if "norm" in data:
                self.norm.from_dict(data["norm"])
                if self.norm.n_features < N_FEATURES_V4:
                    self.norm.expand(N_FEATURES_V4)

            if "replay" in data:
                self.replay.from_dict(data["replay"])
                if saved_n_features < N_FEATURES_V4:
                    self.replay.expand_features(saved_n_features, N_FEATURES_V4)

            if "tracker" in data:
                self.tracker.from_dict(data["tracker"])

            logger.info(
                "NeuralScorer v%s loaded: n=%d win=%d loss=%d acc=%.1f%% features=%d lr=%.5f",
                self.VERSION, self.n_trained, self.n_wins, self.n_losses,
                self.tracker.accuracy * 100, N_FEATURES_V4, self.net.lr
            )
        except Exception as e:
            logger.warning("NeuralScorer v4 load failed, starting fresh: %s", e)

    def _migrate_from_v3(self, v3_path: str):
        """v3 모델을 v4로 자동 마이그레이션."""
        try:
            with open(v3_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.n_trained = int(data.get("n_trained", 0))
            self.n_wins = int(data.get("n_wins", 0))
            self.n_losses = int(data.get("n_losses", 0))
            self._active = bool(data.get("active", True))

            if "net" in data:
                self.net.from_dict(data["net"], target_n_in=N_FEATURES_V4)
            if "norm" in data:
                self.norm.from_dict(data["norm"])
                self.norm.expand(N_FEATURES_V4)

            # v3 replay → v4 replay (regime_weight 기본 0.5 추가)
            if "replay" in data:
                v3_replay = data["replay"]
                for x_list, r in v3_replay.get("wins", []):
                    x = np.array(x_list)
                    if len(x) < N_FEATURES_V4:
                        x = np.concatenate([x, np.zeros(N_FEATURES_V4 - len(x))])
                    self.replay.wins.append((x, float(r), 0.5))
                for x_list, r in v3_replay.get("losses", []):
                    x = np.array(x_list)
                    if len(x) < N_FEATURES_V4:
                        x = np.concatenate([x, np.zeros(N_FEATURES_V4 - len(x))])
                    self.replay.losses.append((x, float(r), 0.5))

            if "tracker" in data:
                self.tracker.from_dict(data["tracker"])

            logger.info(
                "NeuralScorer v3→v4 migration complete: n=%d features=20→26",
                self.n_trained
            )
            self.save()  # v4 포맷으로 저장
        except Exception as e:
            logger.warning("v3→v4 migration failed: %s", e)

    # ── 진입 시 피처 기록 ────────────────────────────────────────────────────
    def record_entry(self, symbol: str, raw_features: np.ndarray, regime: str = "chop"):
        self.norm.update(raw_features)
        x_norm = self.norm.normalize(raw_features)
        rw = regime_to_weight(regime)
        if self.n_trained >= MIN_SAMPLES_TO_PREDICT and self._active:
            prob, roi, unc = self.net.predict_mc(x_norm, rw)
        else:
            prob, roi, unc = 0.5, 0.0, 1.0
        self._pending[symbol] = (raw_features.copy(), prob, roi, unc, regime)

    # ── 거래 완료 → 학습 ─────────────────────────────────────────────────────
    def learn_from_outcome(self, symbol: str, roi_percent: float,
                           net_pnl: float = None, regime: str = ""):
        entry = self._pending.pop(symbol, None)
        if entry is None:
            return
        raw_feat, predicted_prob, predicted_roi, predicted_unc, entry_regime = entry
        actual_regime = regime or entry_regime

        if net_pnl is not None:
            label = 1.0 if float(net_pnl) > 0.0 else 0.0
        else:
            label = 1.0 if roi_percent > 0.0 else 0.0

        # 1. 정규화 + replay 저장
        x_norm = self.norm.normalize(raw_feat)
        rw = regime_to_weight(actual_regime)
        self.replay.push(x_norm, label, roi_percent, rw)

        # 2. 성능 추적
        if self.n_trained >= MIN_SAMPLES_TO_PREDICT:
            self.tracker.record(predicted_prob, label, roi_percent, actual_regime)
            if not self.tracker.is_valid() and self._active:
                self._active = False
                logger.warning(
                    "NeuralScorer v4: accuracy %.1f%% < %.0f%% → disabled (n=%d)",
                    self.tracker.accuracy * 100, ACCURACY_MIN * 100, self.tracker.n_evaluated
                )
            elif self.tracker.is_valid() and not self._active and self.tracker.n_evaluated >= 15:
                self._active = True
                logger.info("NeuralScorer v4: accuracy %.1f%% recovered → re-enabled",
                            self.tracker.accuracy * 100)

        # 3. Cosine Annealing LR
        self.net.lr = cosine_lr(self.net.base_lr, self.n_trained)

        # 4. Replay 기반 학습
        for _ in range(TRAIN_ITERS_PER_STEP):
            batch = self.replay.sample(MINI_BATCH_SIZE)
            if batch is None:
                break
            X, y, roi_targets, W, regime_weights = batch
            self.net.fit_batch(X, y, roi_targets, W, regime_weights)

        self.n_trained += 1
        if label == 1.0:
            self.n_wins += 1
        else:
            self.n_losses += 1

        if self.n_trained % 15 == 0:
            self.save()

        win_rate = self.n_wins / max(self.n_trained, 1) * 100
        _pnl_str = f" net={net_pnl:.4f}U" if net_pnl is not None else ""
        logger.info(
            "NeuralScorer v4: %s roi=%.2f%%%s label=%d n=%d wr=%.1f%% "
            "acc=%.1f%% unc=%.3f regime=%s lr=%.5f",
            symbol, roi_percent, _pnl_str, int(label), self.n_trained,
            win_rate, self.tracker.accuracy * 100, predicted_unc,
            actual_regime, self.net.lr
        )

    # ── 예측 ─────────────────────────────────────────────────────────────────
    def predict(self, raw_features: np.ndarray,
                regime: str = "chop") -> Tuple[float, float, float]:
        """
        Returns (win_prob, expected_roi%, uncertainty).
        냉각 기간 또는 비활성 시 (0.5, 0.0, 1.0) 반환.
        uncertainty: 0.0=확실, >0.1=불확실 (MC dropout 기반)
        """
        if self.n_trained < MIN_SAMPLES_TO_PREDICT or not self._active:
            return 0.5, 0.0, 1.0
        try:
            x_norm = self.norm.normalize(raw_features)
            rw = regime_to_weight(regime)
            prob, roi, unc = self.net.predict_mc(x_norm, rw)
            return prob, roi, unc
        except Exception:
            return 0.5, 0.0, 1.0

    def get_dynamic_block_threshold(self, regime: str) -> float:
        """레짐 + 정확도 기반 적응형 block threshold."""
        base = BLOCK_THRESHOLD_CHOP if regime == "chop" else BLOCK_THRESHOLD_TREND
        # 정확도 높으면 threshold 낮춰서 더 많은 거래 허용
        if self.tracker.n_evaluated >= 30:
            acc = self.tracker.accuracy
            if acc > 0.60:
                base -= 0.03  # 정확도 높으면 공격적
            elif acc < 0.52:
                base += 0.05  # 정확도 낮으면 보수적
        return max(0.15, min(0.40, base))

    def get_feature_importance(self) -> Dict[str, float]:
        """Attention 가중치에서 피처 중요도 추출."""
        w = np.abs(self.net.attention.W_attn)
        w = w / (w.sum() + 1e-8)
        names = [
            "rel_momentum", "volatility", "volume_surge", "slope_1m", "slope_5m",
            "mtf_alignment", "spread_bps", "funding_sign", "regime_num",
            "hour_sin", "hour_cos", "direction_match",
            "price_position", "rsi_14", "atr_ratio", "slope_divergence",
            "funding_magnitude", "vol_regime_ratio", "open_pos_count", "day_of_week_sin",
            "recent_win_rate", "recent_avg_roi", "drawdown_pct",
            "time_since_last", "regime_duration", "tuner_confidence",
        ]
        return {name: float(w[i]) for i, name in enumerate(names) if i < len(w)}

    # ── 상태 조회 (GUI 표시용) ───────────────────────────────────────────────
    def status(self) -> dict:
        wr = self.n_wins / max(self.n_trained, 1) * 100
        acc = self.tracker.accuracy * 100
        return {
            "version": self.VERSION,
            "n_features": N_FEATURES_V4,
            "n_trained": self.n_trained,
            "n_wins": self.n_wins,
            "n_losses": self.n_losses,
            "win_rate": round(wr, 1),
            "accuracy": round(acc, 1),
            "avg_roi": round(self.tracker.avg_roi, 2),
            "replay_n": self.replay.total,
            "active": self._active,
            "ready": self.n_trained >= MIN_SAMPLES_TO_PREDICT and self._active,
            "lr": round(self.net.lr, 6),
            "block_threshold_trend": BLOCK_THRESHOLD_TREND,
            "block_threshold_chop": BLOCK_THRESHOLD_CHOP,
            "acc_trend": round(self.tracker.accuracy_by_regime("trend_up") * 100, 1),
            "acc_chop": round(self.tracker.accuracy_by_regime("chop") * 100, 1),
        }

    # ── 수동 리셋 ────────────────────────────────────────────────────────────
    def reset(self):
        self.__init__(self.model_path)
        if os.path.exists(self.model_path):
            os.remove(self.model_path)
        logger.info("NeuralScorer v%s: model reset", self.VERSION)


# ─────────────────────────────────────────────────────────────────────────────
# v3 하위 호환 인터페이스 (기존 tick_engine 코드 변경 최소화)
# ─────────────────────────────────────────────────────────────────────────────
# tick_engine에서 `from .neural_scorer_v4 import NeuralScorerV4 as NeuralScorer`
# 또는 config에서 neural_scorer_version="v4" 일 때만 사용
NeuralScorer = NeuralScorerV4
build_feature_vector = build_feature_vector_v4
