"""
Feature Flags 관리 모듈
─────────────────────────
feature_flags.json을 로드하여 EngineConfig의 bool 필드를 런타임 오버라이드.
기능별 on/off를 config 재시작 없이 제어 가능.

사용법:
    fm = FeatureFlagManager("feature_flags.json")
    fm.apply_to_config(config)        # EngineConfig에 플래그 적용
    fm.is_enabled("kpi_tracker_enabled")  # 개별 플래그 조회
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── 알려진 플래그 정의 ──────────────────────────────────────────
KNOWN_FLAGS: Dict[str, Dict[str, Any]] = {
    # ─── 코어 기능 ───
    "auto_tune_enabled":          {"type": "bool", "default": True,  "desc": "AutoTuner v2 활성화"},
    "enable_take_profit":         {"type": "bool", "default": True,  "desc": "TP 레이어 활성화"},
    "enable_partial_take_profit": {"type": "bool", "default": True,  "desc": "부분 TP (30/30/40) 활성화"},
    "enable_atr_trailing_stop":   {"type": "bool", "default": True,  "desc": "ATR 트레일링 스톱 활성화"},
    "spike_guard_enabled":        {"type": "bool", "default": True,  "desc": "스파이크 가드 활성화"},
    "maker_first_enabled":        {"type": "bool", "default": True,  "desc": "메이커 우선 진입 활성화"},
    "enable_mtf_ema_confirm":     {"type": "bool", "default": True,  "desc": "MTF EMA 확인 활성화"},
    "composite_signal_enabled":   {"type": "bool", "default": True,  "desc": "복합 신호 스코어링 활성화"},
    "kelly_sizing_enabled":       {"type": "bool", "default": True,  "desc": "Kelly 사이징 활성화"},
    "funding_filter_enabled":     {"type": "bool", "default": True,  "desc": "펀딩 레이트 필터 활성화"},
    "enable_signal_decay_exit":   {"type": "bool", "default": False, "desc": "Signal Decay 청산"},
    "enable_time_stop":           {"type": "bool", "default": False, "desc": "시간 기반 청산"},
    "enable_progress_stop":       {"type": "bool", "default": False, "desc": "진행도 기반 청산"},
    "rsi_filter_enabled":         {"type": "bool", "default": False, "desc": "RSI 필터 활성화"},
    "neural_scorer_enabled":      {"type": "bool", "default": False, "desc": "Neural Scorer 활성화"},

    # ─── 실험적 기능 (Phase 2+) ───
    "neural_v4_enabled":            {"type": "bool", "default": False, "desc": "Neural v4 스코어러 활성화 (30+ 거래 후)"},
    "consensus_scoring_enabled":    {"type": "bool", "default": False, "desc": "3-party consensus (Rule/Neural/Tuner)"},
    "regime_5stage_enabled":        {"type": "bool", "default": False, "desc": "5-stage regime expansion (추후)"},

    # ─── 품질 제어 ───
    "kpi_tracker_enabled":          {"type": "bool", "default": True,  "desc": "KPI 대시보드 추적"},
    "execution_quality_tracking":   {"type": "bool", "default": True,  "desc": "Maker fill rate + TCA 상세 추적"},
}


class FeatureFlagManager:
    """런타임 피처 플래그 로더/매니저."""

    def __init__(self, config_path: str = "feature_flags.json"):
        self.config_path = config_path
        self.flags: Dict[str, Any] = {}
        self.applied_ts: float = 0.0
        self._load()

    # ── 로드 ──────────────────────────────────────────────────
    def _load(self) -> None:
        """feature_flags.json 로드. 없으면 빈 dict 사용 (KNOWN_FLAGS default 활용)."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    self.flags = raw
                    logger.info("[FLAG] Loaded %d flags from %s", len(self.flags), self.config_path)
                else:
                    logger.warning("[FLAG] Invalid format in %s, using defaults", self.config_path)
                    self.flags = {}
            except Exception as e:
                logger.warning("[FLAG] Failed to load %s: %s — using defaults", self.config_path, e)
                self.flags = {}
        else:
            logger.info("[FLAG] %s not found — using KNOWN_FLAGS defaults", self.config_path)
            self.flags = {}

    def reload(self) -> None:
        """런타임 중 JSON 재로드."""
        self._load()
        self.applied_ts = 0.0  # re-apply 필요

    # ── 조회 ──────────────────────────────────────────────────
    def is_enabled(self, flag_name: str) -> bool:
        """bool 플래그 조회. JSON에 없으면 KNOWN_FLAGS default 사용."""
        val = self.flags.get(flag_name)
        if val is not None:
            return bool(val)
        meta = KNOWN_FLAGS.get(flag_name)
        if meta:
            return bool(meta.get("default", False))
        return False

    def get_value(self, flag_name: str, fallback: Any = None) -> Any:
        """임의 타입 플래그 조회."""
        val = self.flags.get(flag_name)
        if val is not None:
            return val
        meta = KNOWN_FLAGS.get(flag_name)
        if meta and "default" in meta:
            return meta["default"]
        return fallback

    # ── EngineConfig 적용 ──────────────────────────────────────
    def apply_to_config(self, config: object) -> int:
        """
        config의 bool 필드를 feature_flags로 오버라이드.
        Returns: 오버라이드된 필드 수.
        """
        applied = 0
        for flag_name, value in self.flags.items():
            if not hasattr(config, flag_name):
                continue
            current = getattr(config, flag_name)
            if isinstance(current, bool) and isinstance(value, bool):
                if current != value:
                    setattr(config, flag_name, value)
                    logger.info("[FLAG] Override %s: %s → %s", flag_name, current, value)
                    applied += 1
        self.applied_ts = time.time()
        return applied

    # ── 저장 ──────────────────────────────────────────────────
    def save(self) -> None:
        """현재 flags를 JSON으로 저장."""
        try:
            os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.flags, f, indent=2, ensure_ascii=False)
            logger.info("[FLAG] Saved %d flags to %s", len(self.flags), self.config_path)
        except Exception as e:
            logger.warning("[FLAG] Save failed: %s", e)

    # ── 유틸 ──────────────────────────────────────────────────
    def generate_default_json(self) -> Dict[str, Any]:
        """KNOWN_FLAGS 기반 기본 feature_flags.json 생성용 dict 반환."""
        return {k: v["default"] for k, v in KNOWN_FLAGS.items()}

    def summary(self) -> Dict[str, Any]:
        """현재 플래그 요약 (디버깅/GUI용)."""
        result = {}
        for flag_name, meta in KNOWN_FLAGS.items():
            result[flag_name] = {
                "value": self.is_enabled(flag_name),
                "source": "json" if flag_name in self.flags else "default",
                "desc": meta.get("desc", ""),
            }
        return result
