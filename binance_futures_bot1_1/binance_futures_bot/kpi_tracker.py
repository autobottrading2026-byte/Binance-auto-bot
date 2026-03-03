"""
KPI Tracker 모듈
────────────────
6개 핵심 KPI를 매 틱마다 계산하고 60초 간격으로 metrics.jsonl에 배치 저장.

KPI 목록:
  1. tca_bps          — 평균 슬리피지 + 스프레드 (bps)
  2. maker_fill_rate  — maker 체결 / 전체 체결 (%)
  3. pipeline_pass_rate — 신호 통과율 (%)
  4. regime_switch_rate — 시간당 레짐 전환 수
  5. ror_proxy         — 세션 드로다운 % (Risk of Ruin 지표)
  6. edge_after_fee    — 실현 ROI - 수수료 (%)
"""

import json
import logging
import os
import statistics
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .tick_engine import TickEngine

logger = logging.getLogger(__name__)


@dataclass
class KPISnapshot:
    """6 핵심 KPI 스냅샷."""
    ts: float

    # 1. TCA (Transaction Cost Analysis)
    tca_bps: float = 0.0                    # 평균 슬리피지 + 스프레드 (bps)

    # 2. Execution Quality
    maker_fill_rate: float = 0.0            # maker fills / total fills (%)
    fill_latency_ms: float = 0.0            # 주문-체결 시간 (ms, 추정)

    # 3. Pipeline Quality
    pipeline_pass_rate: float = 0.0         # signals_passed / signals_evaluated (%)

    # 4. Regime Stability
    regime_switch_rate: float = 0.0         # per hour
    regime_current: str = "unknown"
    regime_confidence: float = 0.0          # 0~1

    # 5. Risk Management
    ror_proxy: float = 0.0                  # 세션 드로다운 % (risk of ruin indicator)

    # 6. Edge After Fee
    edge_after_fee_pct: float = 0.0         # realized_roi - total_fees (%)


class KPITracker:
    """
    매 틱마다 6개 KPI 계산. 60초마다 metrics.jsonl에 배치 저장.
    """

    def __init__(self, engine: "TickEngine", batch_interval_sec: int = 60):
        self.engine = engine
        self.batch_interval_sec = batch_interval_sec
        self.last_batch_ts: float = time.time()
        self.snapshots_buffer: deque = deque(maxlen=120)  # 최대 2분 분량
        self._trade_cache_ts: float = 0.0
        self._trade_cache: list = []
        self._trade_cache_ttl: float = 30.0  # 30초 캐시

    # ══════════════════════════════════════════════════════════════
    # 메인 인터페이스
    # ══════════════════════════════════════════════════════════════

    def compute_snapshot(self) -> KPISnapshot:
        """한 틱의 KPI 스냅샷 계산."""
        now = time.time()

        # 1. TCA metrics (기존 함수 재활용)
        tca = self._safe_tca()
        tca_bps = tca.get("slippage_bps_med", 0.0) + tca.get("tca_spread_bps_med", 0.0)

        # 2. Maker fill rate
        maker_rate, latency = self._execution_quality()

        # 3. Pipeline pass rate
        pass_rate = self._pipeline_pass_rate()

        # 4. Regime info
        regime_info = self._regime_info()

        # 5. RoR proxy (세션 손실)
        ror = self._ror_proxy()

        # 6. Edge after fee
        edge = self._edge_after_fee()

        snap = KPISnapshot(
            ts=now,
            tca_bps=round(tca_bps, 2),
            maker_fill_rate=round(maker_rate, 2),
            fill_latency_ms=round(latency, 1),
            pipeline_pass_rate=round(pass_rate, 2),
            regime_switch_rate=round(regime_info["switch_rate"], 4),
            regime_current=regime_info["current"],
            regime_confidence=round(regime_info["confidence"], 4),
            ror_proxy=round(ror, 2),
            edge_after_fee_pct=round(edge, 3),
        )
        self.snapshots_buffer.append(snap)
        return snap

    def maybe_flush_batch(self) -> None:
        """batch_interval_sec마다 metrics.jsonl에 배치 저장."""
        now = time.time()
        if now - self.last_batch_ts < self.batch_interval_sec:
            return
        self._write_batch()
        self.last_batch_ts = now

    def latest(self) -> Optional[KPISnapshot]:
        """가장 최근 KPI 스냅샷 반환."""
        return self.snapshots_buffer[-1] if self.snapshots_buffer else None

    # ══════════════════════════════════════════════════════════════
    # KPI 계산 헬퍼
    # ══════════════════════════════════════════════════════════════

    def _safe_tca(self) -> dict:
        """_compute_tca_metrics 호출 래퍼."""
        try:
            return self.engine._compute_tca_metrics(1800)
        except Exception:
            return {"slippage_bps_med": 0.0, "tca_spread_bps_med": 0.0}

    def _execution_quality(self) -> tuple:
        """maker fill rate (%), fill latency (ms) 계산."""
        sw = self.engine._stat_window

        # maker fill rate
        maker_dq = sw.get("maker_fills", deque())
        taker_dq = sw.get("taker_fills", deque())
        fills_dq = sw.get("fills", deque())

        maker_cnt = self._stat_sum(maker_dq)
        taker_cnt = self._stat_sum(taker_dq)
        total = maker_cnt + taker_cnt
        if total <= 0:
            # fallback: fills deque로 추정
            total = self._stat_sum(fills_dq)
        maker_rate = (maker_cnt / total * 100.0) if total > 0 else 0.0

        # fill latency
        lat_dq = sw.get("fill_latencies_ms", deque())
        lats = [v for _, v in lat_dq]
        latency = statistics.mean(lats[-20:]) if lats else 0.0

        return maker_rate, latency

    def _pipeline_pass_rate(self) -> float:
        """신호 평가 → 통과 비율 (%)."""
        sw = self.engine._stat_window
        evaluated = self._stat_sum(sw.get("signals_evaluated", deque()))
        passed = self._stat_sum(sw.get("signals_passed", deque()))
        if evaluated <= 0:
            return 0.0
        return passed / evaluated * 100.0

    def _regime_info(self) -> dict:
        """AutoTuner 상태에서 레짐 정보 추출."""
        regime = "unknown"
        confidence = 0.0
        switch_rate = 0.0

        tuner = getattr(self.engine, "auto_tuner", None)
        if tuner and hasattr(tuner, "state"):
            hyst = getattr(tuner.state, "hysteresis", None)
            if hyst:
                regime = getattr(hyst, "current_regime", "chop")
            confidence = getattr(tuner.state, "confidence", 0.0)

            # switch rate: regime_switch_timestamps 기반
            switch_ts_list = getattr(tuner.state, "regime_switch_timestamps", [])
            if switch_ts_list:
                now = time.time()
                hour_ago = now - 3600
                recent_switches = sum(1 for ts in switch_ts_list if ts > hour_ago)
                switch_rate = float(recent_switches)
            else:
                # fallback: tune_count_today / uptime_hours
                tune_count = getattr(tuner.state, "tune_count_today", 0)
                uptime_h = max(0.01, (time.time() - self.engine.session_start_ts) / 3600.0)
                switch_rate = tune_count / uptime_h

        return {
            "current": regime,
            "confidence": confidence,
            "switch_rate": switch_rate,
        }

    def _ror_proxy(self) -> float:
        """세션 드로다운 % (RoR proxy). 미실현 손실 / 잔고."""
        try:
            snapshots = self.engine.position_snapshots
            if not snapshots:
                return 0.0
            losses = []
            for ps in snapshots.values():
                unrealized = getattr(ps, "unrealized_pnl", 0.0)
                if unrealized is not None and unrealized < 0:
                    losses.append(float(unrealized))
            if not losses:
                return 0.0
            total_loss = sum(losses)
            balance = self.engine._balance_cache.get("available", 1.0)
            if balance <= 0:
                return 0.0
            return abs(total_loss / balance * 100.0)
        except Exception:
            return 0.0

    def _edge_after_fee(self) -> float:
        """실현 ROI - 수수료 (%). 최근 거래 기반."""
        trades = self._load_recent_trades(window_sec=1800)
        if not trades:
            return 0.0

        rois = []
        fees = []
        for t in trades:
            roi = t.get("roi_pct", 0.0)
            fee_rate = t.get("fee_rate", 0.0)
            if roi is not None:
                rois.append(float(roi))
            if fee_rate is not None:
                # 진입+청산 양방향 수수료
                fees.append(float(fee_rate) * 2.0 * 100.0)

        if not rois:
            return 0.0

        avg_roi = statistics.mean(rois)
        avg_fee = statistics.mean(fees) if fees else 0.0
        return avg_roi - avg_fee

    # ══════════════════════════════════════════════════════════════
    # 유틸리티
    # ══════════════════════════════════════════════════════════════

    def _load_recent_trades(self, window_sec: int = 1800) -> list:
        """trade_history.jsonl에서 최근 거래 로드 (캐시)."""
        now = time.time()
        if now - self._trade_cache_ts < self._trade_cache_ttl and self._trade_cache:
            return self._trade_cache

        path = getattr(self.engine, "trade_log_path", "")
        if not path or not os.path.exists(path):
            return []

        cutoff = now - window_sec
        trades = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        ts = float(ev.get("ts", 0.0) or 0.0)
                        if ts > cutoff:
                            trades.append(ev)
                    except Exception:
                        continue
        except Exception:
            return []

        self._trade_cache = trades
        self._trade_cache_ts = now
        return trades

    def _stat_sum(self, dq: deque) -> float:
        """deque[(ts, count)] 에서 window 내 합계."""
        now = time.time()
        cutoff = now - self.engine._metrics_window_sec
        return sum(v for ts, v in dq if ts >= cutoff)

    # ══════════════════════════════════════════════════════════════
    # 배치 저장
    # ══════════════════════════════════════════════════════════════

    def _write_batch(self) -> None:
        """snapshots_buffer의 최근 KPI를 metrics.jsonl에 append."""
        if not self.snapshots_buffer:
            return

        # 마지막 스냅샷 1건만 저장 (과도한 I/O 방지)
        snap = self.snapshots_buffer[-1]
        try:
            path = getattr(self.engine, "metrics_path", "")
            if not path:
                return
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            payload = {
                "type": "kpi",
                "ts": snap.ts,
                "tca_bps": snap.tca_bps,
                "maker_fill_rate": snap.maker_fill_rate,
                "fill_latency_ms": snap.fill_latency_ms,
                "pipeline_pass_rate": snap.pipeline_pass_rate,
                "regime_switch_rate": snap.regime_switch_rate,
                "regime": snap.regime_current,
                "regime_confidence": snap.regime_confidence,
                "ror_proxy": snap.ror_proxy,
                "edge_after_fee_pct": snap.edge_after_fee_pct,
            }
            with open(path, "a", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.write("\n")
        except Exception as e:
            logger.warning("KPI batch write failed: %s", e)

    def to_dict(self) -> dict:
        """최근 KPI 스냅샷을 dict로 변환 (GUI 전달용)."""
        snap = self.latest()
        if snap is None:
            return {}
        return asdict(snap)
