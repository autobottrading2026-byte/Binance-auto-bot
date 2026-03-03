"""
3-Party Consensus Scorer
────────────────────────
Rule Engine, Neural Scorer, AutoTuner 3자의 가중 합의를 통해
진입 신호의 최종 의사결정을 수행하는 프레임워크.

현재 상태: 프레임워크 구축 완료, 기본 비활성 (consensus_scoring_enabled=false)
활성화 시: feature_flags.json에서 consensus_scoring_enabled=true로 설정

가중치 기본값:
  - 기본:  Rule 40% / Neural 35% / Tuner 25%
  - Trend: Rule 30% / Neural 50% / Tuner 20%
  - Chop:  Rule 50% / Neural 25% / Tuner 25%
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .tick_engine import TickEngine, SymbolSnapshot

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class ConsensusResult:
    """3-party consensus 결과."""
    final_decision: Optional[str] = None       # "LONG" | "SHORT" | None (차단)
    final_confidence: float = 0.0              # 0~1

    rule_score: float = 0.0                    # 0~1
    neural_prob: float = 0.5                   # 0~1
    tuner_confidence: float = 0.0              # 0~1

    weighted_score: float = 0.0                # weighted avg
    block_reason: Optional[str] = None         # consensus 단계에서 차단 이유
    regime: str = "unknown"                    # 적용된 레짐
    weights_used: Dict[str, float] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════
# 추상 인터페이스
# ══════════════════════════════════════════════════════════════════

class ScorableSignal(ABC):
    """스코러 인터페이스. 각 시스템이 구현."""

    @abstractmethod
    def score_signal(
        self,
        snap: "SymbolSnapshot",
        direction: str,
    ) -> Tuple[float, Optional[str]]:
        """
        신호에 대한 점수 반환.
        Returns: (score: 0~1, block_reason: str 또는 None)
        """
        ...


# ══════════════════════════════════════════════════════════════════
# Rule Scorer — 기존 규칙 기반 필터를 점수로 변환
# ══════════════════════════════════════════════════════════════════

class RuleScorer(ScorableSignal):
    """기존 evaluate_signal()의 필터 로직을 0~1 점수로 변환."""

    def __init__(self, engine: "TickEngine"):
        self.engine = engine

    def score_signal(self, snap: "SymbolSnapshot", direction: str) -> Tuple[float, Optional[str]]:
        score = 1.0
        cfg = self.engine.config

        # 1. Volatility check
        vol_min = float(getattr(cfg, "volatility_min", 0.003))
        if snap.volatility < vol_min:
            return 0.0, f"volatility {snap.volatility:.4f} < min {vol_min:.4f}"

        # 2. ATR upper bound
        atr_max = vol_min * float(getattr(cfg, "atr_max_mult", 3.0))
        if snap.volatility > atr_max:
            score *= 0.6

        # 3. Momentum alignment
        if direction == "LONG":
            req = float(getattr(cfg, "momentum_min_long", 0.003))
            if snap.momentum_5m < req:
                score *= max(0.3, snap.momentum_5m / req if req > 0 else 0.3)
        else:
            req = float(getattr(cfg, "momentum_min_short", -0.004))
            if snap.momentum_5m > req:
                score *= max(0.3, req / snap.momentum_5m if snap.momentum_5m < 0 else 0.3)

        # 4. Spread penalty
        spread_bps = getattr(snap, "spread_bps", 0.0)
        if spread_bps > 15.0:
            score *= 0.7
        elif spread_bps > 10.0:
            score *= 0.85

        # 5. MTF EMA conflict
        _mtf = getattr(self.engine, "_mtf_ema_slopes", {})
        if snap.symbol in _mtf:
            slopes = _mtf[snap.symbol]
            if isinstance(slopes, (list, tuple)):
                avg_slope = sum(slopes) / len(slopes) if slopes else 0.0
            else:
                avg_slope = float(slopes)
            if (direction == "LONG" and avg_slope < -2.0) or \
               (direction == "SHORT" and avg_slope > 2.0):
                score *= 0.65

        return max(0.0, min(1.0, score)), None


# ══════════════════════════════════════════════════════════════════
# Neural v3 Adapter
# ══════════════════════════════════════════════════════════════════

class NeuralV3Adapter(ScorableSignal):
    """Neural Scorer v3의 P(win) 출력을 consensus 점수로 변환."""

    def __init__(self, engine: "TickEngine"):
        self.engine = engine

    def score_signal(self, snap: "SymbolSnapshot", direction: str) -> Tuple[float, Optional[str]]:
        scorer = getattr(self.engine, "neural_scorer", None)
        if scorer is None:
            return 0.5, None  # neutral

        # Neural scorer가 비활성이면 neutral
        status = scorer.status()
        if not status.get("active", False):
            return 0.5, None

        try:
            from .neural_scorer import build_feature_vector
            features = build_feature_vector(snap, self.engine)
            prob_win, expected_roi = scorer.predict(features)

            # Hard block threshold
            block_threshold = float(getattr(self.engine.config, "neural_block_threshold", 0.25))
            if prob_win < block_threshold:
                return 0.0, f"Neural v3 P(win)={prob_win:.2%} < {block_threshold:.2%}"

            return float(prob_win), None
        except Exception as e:
            logger.debug("NeuralV3Adapter error: %s", e)
            return 0.5, None  # neutral on error


# ══════════════════════════════════════════════════════════════════
# AutoTuner Adapter
# ══════════════════════════════════════════════════════════════════

class AutoTunerAdapter(ScorableSignal):
    """AutoTuner confidence를 consensus 점수로 변환."""

    def __init__(self, engine: "TickEngine"):
        self.engine = engine

    def score_signal(self, snap: "SymbolSnapshot", direction: str) -> Tuple[float, Optional[str]]:
        tuner = getattr(self.engine, "auto_tuner", None)
        if tuner is None or not hasattr(tuner, "state"):
            return 0.5, None  # neutral

        confidence = getattr(tuner.state, "confidence", 0.5)
        regime = "chop"
        hyst = getattr(tuner.state, "hysteresis", None)
        if hyst:
            regime = getattr(hyst, "current_regime", "chop")

        # 레짐-방향 일치도 보너스
        direction_bonus = 0.0
        if regime == "trend_up" and direction == "LONG":
            direction_bonus = 0.10
        elif regime == "trend_down" and direction == "SHORT":
            direction_bonus = 0.10
        elif regime == "chop":
            direction_bonus = -0.05  # chop에서는 약간 페널티

        # 최소 신뢰도 체크
        min_conf = 0.15 if regime in ("trend_up", "trend_down") else 0.10
        if confidence < min_conf:
            return 0.0, f"AutoTuner confidence {confidence:.2%} < min {min_conf:.2%} ({regime})"

        score = max(0.0, min(1.0, float(confidence) + direction_bonus))
        return score, None


# ══════════════════════════════════════════════════════════════════
# Neural v4 Adapter (스텁 — 향후 통합용)
# ══════════════════════════════════════════════════════════════════

class NeuralV4Adapter(ScorableSignal):
    """
    Neural v4 스코어러 어댑터 (향후 통합용).
    MC Dropout 불확실성을 활용한 점수 페널티 적용.
    """

    def __init__(self, engine: "TickEngine"):
        self.engine = engine
        self._v4_scorer = None

    def score_signal(self, snap: "SymbolSnapshot", direction: str) -> Tuple[float, Optional[str]]:
        # v4가 비활성이면 neutral 반환
        ff = getattr(self.engine, "feature_flags", None)
        if ff is None or not ff.is_enabled("neural_v4_enabled"):
            return 0.5, None

        # TODO: neural_scorer_v4.py 실제 통합 시 구현
        # 아래는 인터페이스 예시:
        #
        # if self._v4_scorer is None:
        #     from .neural_scorer_v4 import NeuralScorerV4
        #     path = os.path.join(...)
        #     self._v4_scorer = NeuralScorerV4(model_path=path)
        #
        # prob_win, uncertainty = self._v4_scorer.predict_mc(features, regime_weight)
        #
        # if uncertainty > 0.15:
        #     return 0.0, f"Neural v4 uncertainty {uncertainty:.2%} > 0.15"
        #
        # adj_score = prob_win * (1.0 - 0.3 * uncertainty)
        # return adj_score, None

        return 0.5, None  # 미구현 상태에서는 neutral


# ══════════════════════════════════════════════════════════════════
# Consensus Scorer — 3자 가중 합의
# ══════════════════════════════════════════════════════════════════

# 레짐별 가중치 프리셋
REGIME_WEIGHTS: Dict[str, Dict[str, float]] = {
    "default":    {"rule": 0.40, "neural": 0.35, "tuner": 0.25},
    "trend_up":   {"rule": 0.30, "neural": 0.50, "tuner": 0.20},
    "trend_down": {"rule": 0.30, "neural": 0.50, "tuner": 0.20},
    "chop":       {"rule": 0.50, "neural": 0.25, "tuner": 0.25},
}

# 최종 결정 임계값
CONSENSUS_THRESHOLD = 0.55


class ConsensusScorer:
    """
    3-party 가중 평균 합의.
    Rule Engine + Neural Scorer + AutoTuner의 점수를 레짐별 가중치로 결합.
    """

    def __init__(
        self,
        engine: "TickEngine",
        weights: Optional[Dict[str, float]] = None,
        threshold: float = CONSENSUS_THRESHOLD,
    ):
        self.engine = engine
        self.custom_weights = weights
        self.threshold = threshold

        self.rule_scorer = RuleScorer(engine)
        self.neural_scorer = NeuralV3Adapter(engine)
        self.tuner_scorer = AutoTunerAdapter(engine)
        self.v4_adapter = NeuralV4Adapter(engine)

        # 통계
        self._total_evaluated = 0
        self._total_passed = 0
        self._total_blocked = 0

    def compute_consensus(
        self,
        snap: "SymbolSnapshot",
        direction: str,
        regime: Optional[str] = None,
    ) -> ConsensusResult:
        """
        3개 스코러의 가중 합의.
        regime이 전달되면 레짐별 가중치 적용.
        """
        self._total_evaluated += 1

        # 각 스코러 평가
        rule_score, rule_block = self.rule_scorer.score_signal(snap, direction)
        neural_score, neural_block = self.neural_scorer.score_signal(snap, direction)
        tuner_score, tuner_block = self.tuner_scorer.score_signal(snap, direction)

        # v4 활성화 시 neural_score를 v4로 대체
        ff = getattr(self.engine, "feature_flags", None)
        if ff and ff.is_enabled("neural_v4_enabled"):
            v4_score, v4_block = self.v4_adapter.score_signal(snap, direction)
            if v4_score != 0.5 or v4_block:  # v4가 실제 결과를 반환했으면
                neural_score = v4_score
                neural_block = v4_block

        # Hard block: 어떤 스코러든 score=0 + block_reason이면 즉시 차단
        for name, (score, block) in [
            ("rule", (rule_score, rule_block)),
            ("neural", (neural_score, neural_block)),
            ("tuner", (tuner_score, tuner_block)),
        ]:
            if score <= 0.0 and block:
                self._total_blocked += 1
                return ConsensusResult(
                    final_decision=None,
                    final_confidence=0.0,
                    rule_score=rule_score,
                    neural_prob=neural_score,
                    tuner_confidence=tuner_score,
                    weighted_score=0.0,
                    block_reason=f"[{name}] {block}",
                    regime=regime or "unknown",
                )

        # 레짐별 가중치 선택
        if self.custom_weights:
            weights = self.custom_weights
        elif regime and regime in REGIME_WEIGHTS:
            weights = REGIME_WEIGHTS[regime]
        else:
            weights = REGIME_WEIGHTS["default"]

        # 가중 평균 계산
        weighted = (
            weights["rule"] * rule_score +
            weights["neural"] * neural_score +
            weights["tuner"] * tuner_score
        )

        # 결정: threshold 이상이면 진입
        if weighted >= self.threshold:
            decision = direction
            self._total_passed += 1
        else:
            decision = None
            self._total_blocked += 1

        return ConsensusResult(
            final_decision=decision,
            final_confidence=weighted,
            rule_score=rule_score,
            neural_prob=neural_score,
            tuner_confidence=tuner_score,
            weighted_score=weighted,
            block_reason=None if decision else f"consensus {weighted:.2%} < threshold {self.threshold:.2%}",
            regime=regime or "unknown",
            weights_used=weights,
        )

    def status(self) -> Dict[str, Any]:
        """상태 요약 (GUI/로깅용)."""
        return {
            "total_evaluated": self._total_evaluated,
            "total_passed": self._total_passed,
            "total_blocked": self._total_blocked,
            "pass_rate": (self._total_passed / max(1, self._total_evaluated) * 100.0),
            "threshold": self.threshold,
        }
