"""
Execution Quality Engine (Lite)
────────────────────────────────
심볼별 maker-first 실행 품질을 추적하고,
체결률이 낮은 심볼의 offset_bps / timeout_ms를 자동 미세조정.

GPT 제안 "Sprint 2: Execution Quality Lite" 구현:
  - 심볼별 maker 시도/체결/taker 전환 횟수 기록
  - time_to_fill_ms p50/p90 기록
  - 체결 기준 TCA_bps (간이)
  - maker 체결률 낮은 심볼 → offset/timeout 자동 소폭 조정
"""

import logging
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import EngineConfig

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# 심볼별 실행 통계
# ══════════════════════════════════════════════════════════════════

@dataclass
class SymbolExecStats:
    """심볼 1개의 maker-first 실행 통계."""
    maker_attempts: int = 0        # maker 시도 횟수
    maker_fills: int = 0           # maker 체결 성공
    taker_fallbacks: int = 0       # taker 전환 횟수
    gtx_rejections: int = 0        # -5022 거절 횟수
    fill_times_ms: deque = field(default_factory=lambda: deque(maxlen=50))
    slippage_bps_list: deque = field(default_factory=lambda: deque(maxlen=50))
    last_updated: float = 0.0

    @property
    def total_fills(self) -> int:
        return self.maker_fills + self.taker_fallbacks

    @property
    def maker_fill_rate(self) -> float:
        """maker 체결률 (%). 시도 대비."""
        if self.maker_attempts <= 0:
            return 0.0
        return self.maker_fills / self.maker_attempts * 100.0

    @property
    def taker_fallback_rate(self) -> float:
        """taker 전환율 (%). 시도 대비."""
        if self.maker_attempts <= 0:
            return 100.0
        return self.taker_fallbacks / self.maker_attempts * 100.0

    @property
    def fill_time_p50(self) -> float:
        """체결 시간 중앙값 (ms)."""
        if not self.fill_times_ms:
            return 0.0
        sorted_times = sorted(self.fill_times_ms)
        return sorted_times[len(sorted_times) // 2]

    @property
    def fill_time_p90(self) -> float:
        """체결 시간 p90 (ms)."""
        if not self.fill_times_ms:
            return 0.0
        sorted_times = sorted(self.fill_times_ms)
        idx = int(len(sorted_times) * 0.9)
        return sorted_times[min(idx, len(sorted_times) - 1)]

    @property
    def slippage_bps_med(self) -> float:
        """슬리피지 중앙값 (bps)."""
        if not self.slippage_bps_list:
            return 0.0
        return statistics.median(self.slippage_bps_list)

    def to_dict(self) -> dict:
        return {
            "maker_attempts": self.maker_attempts,
            "maker_fills": self.maker_fills,
            "taker_fallbacks": self.taker_fallbacks,
            "gtx_rejections": self.gtx_rejections,
            "maker_fill_rate": round(self.maker_fill_rate, 1),
            "taker_fallback_rate": round(self.taker_fallback_rate, 1),
            "fill_time_p50_ms": round(self.fill_time_p50, 1),
            "fill_time_p90_ms": round(self.fill_time_p90, 1),
            "slippage_bps_med": round(self.slippage_bps_med, 2),
            "total_fills": self.total_fills,
        }


# ══════════════════════════════════════════════════════════════════
# 심볼별 파라미터 오버라이드
# ══════════════════════════════════════════════════════════════════

@dataclass
class SymbolMakerOverride:
    """심볼별 maker-first 파라미터 오버라이드."""
    offset_bps: Optional[float] = None     # None이면 config 기본값 사용
    timeout_ms: Optional[int] = None       # None이면 config 기본값 사용
    reason: str = ""                       # 조정 사유
    adjusted_at: float = 0.0               # 마지막 조정 시각


# ══════════════════════════════════════════════════════════════════
# 자동 조정 상수
# ══════════════════════════════════════════════════════════════════

# maker 체결률이 이 값 미만이면 파라미터 조정 대상
LOW_FILL_RATE_THRESHOLD = 40.0     # %

# 최소 샘플 수 (조정 전 최소 시도 횟수)
MIN_SAMPLES_FOR_ADJUST = 5

# offset_bps 조정 범위
OFFSET_BPS_MIN = 0.5
OFFSET_BPS_MAX = 5.0
OFFSET_BPS_STEP = 0.3             # 1회 조정량

# timeout_ms 조정 범위
TIMEOUT_MS_MIN = 1000
TIMEOUT_MS_MAX = 8000
TIMEOUT_MS_STEP = 500             # 1회 조정량 (ms)

# 조정 쿨다운 (같은 심볼 재조정 방지)
ADJUST_COOLDOWN_SEC = 600         # 10분


class ExecutionQualityEngine:
    """
    심볼별 maker-first 실행 품질 추적 + 자동 미세조정.

    사용법:
        eq = ExecutionQualityEngine(config)

        # 진입 시도 전
        offset, timeout = eq.get_params(symbol)

        # maker 시도 기록
        eq.record_maker_attempt(symbol)

        # 체결 결과 기록
        eq.record_fill(symbol, is_maker=True, fill_time_ms=150, slippage_bps=1.2)

        # GTX 거절 기록
        eq.record_gtx_rejection(symbol)

        # 주기적 자동 조정 (auto_tuner 사이클에서)
        eq.auto_adjust_all()
    """

    def __init__(self, config: "EngineConfig"):
        self.config = config
        self._stats: Dict[str, SymbolExecStats] = defaultdict(SymbolExecStats)
        self._overrides: Dict[str, SymbolMakerOverride] = {}
        self._last_auto_adjust_ts: float = 0.0
        self._auto_adjust_interval: float = 300.0  # 5분마다 체크

    # ══════════════════════════════════════════════════════════════
    # 기록 API
    # ══════════════════════════════════════════════════════════════

    def record_maker_attempt(self, symbol: str) -> None:
        """maker-first 진입 시도 기록."""
        self._stats[symbol].maker_attempts += 1
        self._stats[symbol].last_updated = time.time()

    def record_fill(
        self,
        symbol: str,
        is_maker: bool,
        fill_time_ms: float = 0.0,
        slippage_bps: float = 0.0,
    ) -> None:
        """체결 결과 기록."""
        stats = self._stats[symbol]
        if is_maker:
            stats.maker_fills += 1
        else:
            stats.taker_fallbacks += 1
        if fill_time_ms > 0:
            stats.fill_times_ms.append(fill_time_ms)
        if slippage_bps != 0.0:
            stats.slippage_bps_list.append(abs(slippage_bps))
        stats.last_updated = time.time()

    def record_gtx_rejection(self, symbol: str) -> None:
        """GTX -5022 거절 기록."""
        self._stats[symbol].gtx_rejections += 1
        self._stats[symbol].last_updated = time.time()

    # ══════════════════════════════════════════════════════════════
    # 파라미터 조회
    # ══════════════════════════════════════════════════════════════

    def get_params(self, symbol: str) -> Tuple[float, int]:
        """
        심볼의 maker-first 파라미터 반환.
        오버라이드가 있으면 사용, 없으면 config 기본값.

        Returns: (offset_bps, timeout_ms)
        """
        override = self._overrides.get(symbol)

        base_offset = float(getattr(self.config, "maker_first_offset_bps", 1.0))
        base_timeout = int(getattr(self.config, "maker_first_timeout_ms", 3000))

        offset = override.offset_bps if (override and override.offset_bps is not None) else base_offset
        timeout = override.timeout_ms if (override and override.timeout_ms is not None) else base_timeout

        return offset, timeout

    # ══════════════════════════════════════════════════════════════
    # 자동 조정
    # ══════════════════════════════════════════════════════════════

    def auto_adjust_all(self) -> int:
        """
        모든 심볼의 maker 파라미터를 자동 조정.
        Returns: 조정된 심볼 수.
        """
        now = time.time()
        if now - self._last_auto_adjust_ts < self._auto_adjust_interval:
            return 0
        self._last_auto_adjust_ts = now

        adjusted = 0
        for symbol, stats in self._stats.items():
            if self._maybe_adjust_symbol(symbol, stats, now):
                adjusted += 1

        if adjusted > 0:
            logger.info("[EQ] Auto-adjusted %d symbols' maker params", adjusted)
        return adjusted

    def _maybe_adjust_symbol(self, symbol: str, stats: SymbolExecStats, now: float) -> bool:
        """심볼 1개의 파라미터 자동 조정. 조정했으면 True."""
        # 최소 샘플 체크
        if stats.maker_attempts < MIN_SAMPLES_FOR_ADJUST:
            return False

        # 쿨다운 체크
        override = self._overrides.get(symbol)
        if override and (now - override.adjusted_at) < ADJUST_COOLDOWN_SEC:
            return False

        fill_rate = stats.maker_fill_rate
        base_offset = float(getattr(self.config, "maker_first_offset_bps", 1.0))
        base_timeout = int(getattr(self.config, "maker_first_timeout_ms", 3000))

        current_offset = (override.offset_bps if override and override.offset_bps else base_offset)
        current_timeout = (override.timeout_ms if override and override.timeout_ms else base_timeout)

        new_offset = current_offset
        new_timeout = current_timeout
        reasons = []

        if fill_rate < LOW_FILL_RATE_THRESHOLD:
            # maker 체결률 낮음 → offset 확대 + timeout 연장
            new_offset = min(current_offset + OFFSET_BPS_STEP, OFFSET_BPS_MAX)
            new_timeout = min(current_timeout + TIMEOUT_MS_STEP, TIMEOUT_MS_MAX)
            reasons.append(f"low_fill_rate({fill_rate:.0f}%)")

        elif fill_rate > 80.0 and stats.maker_attempts >= 10:
            # maker 체결률 높음 → offset 축소 (비용 절약)
            new_offset = max(current_offset - OFFSET_BPS_STEP * 0.5, OFFSET_BPS_MIN)
            reasons.append(f"high_fill_rate({fill_rate:.0f}%)")

        # GTX 거절 비율이 높으면 offset 추가 확대
        if stats.maker_attempts > 0:
            rejection_rate = stats.gtx_rejections / stats.maker_attempts * 100.0
            if rejection_rate > 30.0:
                new_offset = min(new_offset + OFFSET_BPS_STEP, OFFSET_BPS_MAX)
                reasons.append(f"high_rejection({rejection_rate:.0f}%)")

        # 변경이 없으면 건너뜀
        if abs(new_offset - current_offset) < 0.01 and abs(new_timeout - current_timeout) < 1:
            return False

        # 오버라이드 적용
        self._overrides[symbol] = SymbolMakerOverride(
            offset_bps=round(new_offset, 2),
            timeout_ms=int(new_timeout),
            reason=", ".join(reasons),
            adjusted_at=now,
        )

        logger.info(
            "[EQ] %s adjusted: offset %.1f→%.1f bps, timeout %d→%d ms (%s)",
            symbol, current_offset, new_offset,
            current_timeout, new_timeout,
            ", ".join(reasons),
        )
        return True

    # ══════════════════════════════════════════════════════════════
    # 리포트
    # ══════════════════════════════════════════════════════════════

    def summary(self) -> Dict[str, dict]:
        """전체 심볼 실행 품질 요약."""
        result = {}
        for symbol, stats in self._stats.items():
            info = stats.to_dict()
            override = self._overrides.get(symbol)
            if override:
                info["override_offset_bps"] = override.offset_bps
                info["override_timeout_ms"] = override.timeout_ms
                info["override_reason"] = override.reason
            result[symbol] = info
        return result

    def global_summary(self) -> dict:
        """전체 합산 요약 (GUI 표시용)."""
        total_attempts = sum(s.maker_attempts for s in self._stats.values())
        total_maker = sum(s.maker_fills for s in self._stats.values())
        total_taker = sum(s.taker_fallbacks for s in self._stats.values())
        total_rejections = sum(s.gtx_rejections for s in self._stats.values())

        all_fill_times = []
        all_slippages = []
        for s in self._stats.values():
            all_fill_times.extend(s.fill_times_ms)
            all_slippages.extend(s.slippage_bps_list)

        fill_p50 = 0.0
        fill_p90 = 0.0
        if all_fill_times:
            sorted_t = sorted(all_fill_times)
            fill_p50 = sorted_t[len(sorted_t) // 2]
            fill_p90 = sorted_t[int(len(sorted_t) * 0.9)]

        slip_med = statistics.median(all_slippages) if all_slippages else 0.0

        return {
            "total_attempts": total_attempts,
            "total_maker_fills": total_maker,
            "total_taker_fallbacks": total_taker,
            "total_gtx_rejections": total_rejections,
            "maker_fill_rate": round(total_maker / max(1, total_attempts) * 100.0, 1),
            "taker_fallback_rate": round(total_taker / max(1, total_attempts) * 100.0, 1),
            "fill_time_p50_ms": round(fill_p50, 1),
            "fill_time_p90_ms": round(fill_p90, 1),
            "slippage_bps_med": round(slip_med, 2),
            "symbols_tracked": len(self._stats),
            "symbols_adjusted": len(self._overrides),
        }
