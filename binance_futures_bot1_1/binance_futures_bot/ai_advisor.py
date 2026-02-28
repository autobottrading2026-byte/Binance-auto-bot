"""
AI Trading Advisor v1.0
───────────────────────
실시간 엔진 이벤트를 분석하여 자연어 인사이트를 생성하고,
거래 패턴 기반 자가 개선 제안을 제공하는 모듈.

핵심 역할:
  1) notifications.log 스트리밍 → 이벤트 분류 & 번역
  2) trade_history.jsonl 분석 → 패턴 인식 & 약점 탐지
  3) 개선 제안 생성 (파라미터 조정, 진입 조건 강화 등)
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 상수 ──────────────────────────────────────────────────────────────────────
MAX_EVENTS = 500          # 메모리에 보관할 최대 이벤트 수
MAX_TRADES = 100          # 최근 거래 캐시
PATTERN_TRADES = 20       # 패턴 분석 대상 거래 수
WEAK_WIN_RATE = 0.45      # 약점 판정 임계값
CACHE_TTL_SEC = 3.0       # 분석 캐시 TTL
TRADE_CACHE_TTL = 10.0    # 거래 캐시 TTL
NEURAL_CACHE_TTL = 30.0   # 신경망 캐시 TTL
SUGGESTION_INTERVAL = 30  # 개선 제안 갱신 간격(초)

# ── 카테고리 정의 ─────────────────────────────────────────────────────────────
CATEGORY_MARKET   = "MARKET_INSIGHT"
CATEGORY_ENTRY    = "ENTRY_CHECK"
CATEGORY_EXIT     = "EXIT_ACTION"
CATEGORY_TUNE     = "AUTOTUNE"
CATEGORY_KILL     = "KILL_SWITCH"
CATEGORY_NEURAL   = "NEURAL"
CATEGORY_IMPROVE  = "IMPROVEMENT"
CATEGORY_SYSTEM   = "SYSTEM"

CATEGORY_ICONS = {
    CATEGORY_MARKET:  ("📊", "#4A90D9"),
    CATEGORY_ENTRY:   ("📍", "#2ECC71"),
    CATEGORY_EXIT:    ("🚪", "#F0B90B"),
    CATEGORY_TUNE:    ("⚙",  "#F0B90B"),
    CATEGORY_KILL:    ("🛑", "#FF6B6B"),
    CATEGORY_NEURAL:  ("🧠", "#4A90D9"),
    CATEGORY_IMPROVE: ("💡", "#F0B90B"),
    CATEGORY_SYSTEM:  ("ℹ",  "#8e96b8"),
}

# ── 이벤트 분류 패턴 (기존 _notify 메시지에서 카테고리 추론) ─────────────────
_CLASSIFY_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (CATEGORY_KILL,    re.compile(r"KILL_SWITCH|킬\s*스위치|kill.?switch", re.I)),
    (CATEGORY_TUNE,    re.compile(r"AUTOTUNE|AutoTune|auto.?tune|오토튜닝", re.I)),
    (CATEGORY_ENTRY,   re.compile(r"ENTRY_|진입|entry|SIGNAL_PASS|NEURAL_v3|NEURAL_SCORE|NEURAL BLOCK", re.I)),
    (CATEGORY_EXIT,    re.compile(r"EXIT_|청산|close|TRAIL_EXIT|PARTIAL_TP|손절|STOP_LOSS|TAKE_PROFIT", re.I)),
    (CATEGORY_NEURAL,  re.compile(r"NeuralScorer|neural|신경망|학습|learn", re.I)),
    (CATEGORY_MARKET,  re.compile(r"regime|레짐|volatility|변동성|funding|spike", re.I)),
]


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default  # NaN 체크
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════════
#  Event 파싱
# ══════════════════════════════════════════════════════════════════════════════

class ParsedEvent:
    """notifications.log 한 줄 파싱 결과."""
    __slots__ = ("ts_str", "ts_epoch", "level", "raw_msg", "category", "friendly_msg")

    def __init__(self, ts_str: str, level: str, raw_msg: str):
        self.ts_str = ts_str
        self.level = level.upper()
        self.raw_msg = raw_msg
        self.category = CATEGORY_SYSTEM
        self.friendly_msg = raw_msg
        self.ts_epoch = 0.0
        # timestamp 파싱
        try:
            t = time.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            self.ts_epoch = time.mktime(t)
        except Exception:
            self.ts_epoch = time.time()
        # [CATEGORY] 접두사 확인
        m = re.match(r"^\[([A-Z_]+)\]\s*(.+)$", raw_msg)
        if m:
            self.category = m.group(1)
            self.raw_msg = m.group(2)
        else:
            # 기존 메시지에서 카테고리 추론
            for cat, pat in _CLASSIFY_PATTERNS:
                if pat.search(raw_msg):
                    self.category = cat
                    break

    def to_dict(self) -> dict:
        icon, color = CATEGORY_ICONS.get(self.category, ("ℹ", "#8e96b8"))
        return {
            "ts_str": self.ts_str,
            "ts_epoch": self.ts_epoch,
            "level": self.level,
            "category": self.category,
            "icon": icon,
            "color": color,
            "raw_msg": self.raw_msg,
            "friendly_msg": self.friendly_msg,
        }


def _parse_notification_line(line: str) -> Optional[ParsedEvent]:
    """TIMESTAMP|LEVEL|MESSAGE 형식 파싱."""
    line = line.strip()
    if not line:
        return None
    parts = line.split("|", 2)
    if len(parts) < 3:
        return None
    return ParsedEvent(parts[0].strip(), parts[1].strip(), parts[2].strip())


# ══════════════════════════════════════════════════════════════════════════════
#  이벤트 번역 (자연어 변환)
# ══════════════════════════════════════════════════════════════════════════════

class EventTranslator:
    """엔진 이벤트를 사용자 친화적 자연어로 변환."""

    def __init__(self, language: str = "ko"):
        self.language = language

    @property
    def _ko(self) -> bool:
        return self.language == "ko"

    def translate(self, event: ParsedEvent) -> str:
        """이벤트를 카테고리에 맞는 자연어로 변환."""
        msg = event.raw_msg
        cat = event.category

        if cat == CATEGORY_KILL:
            return self._translate_kill(msg)
        elif cat == CATEGORY_TUNE:
            return self._translate_tune(msg)
        elif cat == CATEGORY_ENTRY:
            return self._translate_entry(msg)
        elif cat == CATEGORY_EXIT:
            return self._translate_exit(msg)
        elif cat == CATEGORY_NEURAL:
            return self._translate_neural(msg)
        elif cat == CATEGORY_MARKET:
            return self._translate_market(msg)
        # 기본: 원문 그대로
        return msg

    def _translate_kill(self, msg: str) -> str:
        if "쿨다운 완료" in msg or "Cooldown expired" in msg:
            return "킬스위치 쿨다운 완료 → 진입 재개됩니다." if self._ko else "Kill switch cooldown expired → entries resumed."
        m = re.search(r"([\-0-9.]+)\s*USDT.*한도\s*([\-0-9.]+)|limit\s*([\-0-9.]+)", msg)
        if m:
            return (f"세션 손실이 한도에 도달했습니다. 쿨다운 모드 진입." if self._ko
                    else f"Session loss limit hit. Entering cooldown mode.")
        return ("킬스위치가 작동했습니다." if self._ko else "Kill switch activated.") + f" {msg}"

    def _translate_tune(self, msg: str) -> str:
        if "rollback" in msg.lower() or "롤백" in msg:
            return ("오토튜닝 롤백: 성과 미달로 이전 설정으로 복원합니다." if self._ko
                    else "Auto-tune rollback: reverting to previous settings due to underperformance.")
        if "APPLY" in msg or "적용" in msg:
            return ("오토튜닝 파라미터가 업데이트되었습니다." if self._ko
                    else "Auto-tune parameters updated.") + f"\n  {msg[:120]}"
        return ("오토튜닝 실행 중..." if self._ko else "Auto-tuning in progress...") + f"\n  {msg[:120]}"

    def _translate_entry(self, msg: str) -> str:
        # NEURAL BLOCK
        if "NEURAL BLOCK" in msg or "NEURAL_BLOCK" in msg:
            m = re.search(r"win_prob\s*([\d.]+%)", msg)
            prob_str = m.group(1) if m else ""
            return (f"신경망이 진입을 차단했습니다 (승률 {prob_str} — 기준 미달)." if self._ko
                    else f"Neural network blocked entry (win prob {prob_str} — below threshold).")
        # ENTRY_BLOCKED
        if "BLOCKED" in msg or "차단" in msg:
            sym = re.search(r"([A-Z]+USDT)", msg)
            sym_str = sym.group(1) if sym else ""
            return (f"{sym_str} 진입이 차단되었습니다." if self._ko
                    else f"{sym_str} entry blocked.") + f" ({msg[:80]})"
        # NEURAL_v3 score
        m_neural = re.search(r"NEURAL_v3\s+(\w+)\s+prob=([\d.]+)\s+E\[roi\]=([\-+\d.]+)", msg)
        if m_neural:
            sym, prob, roi = m_neural.group(1), m_neural.group(2), m_neural.group(3)
            return (f"{sym} 진입 분석: 신경망 승률 {float(prob)*100:.0f}%, 기대수익 {roi}%"
                    if self._ko else
                    f"{sym} entry analysis: Neural win prob {float(prob)*100:.0f}%, expected ROI {roi}%")
        # SIGNAL_PASS
        if "SIGNAL_PASS" in msg or "signals_passed" in msg:
            sym = re.search(r"([A-Z]+USDT)", msg)
            sym_str = sym.group(1) if sym else ""
            return (f"{sym_str} 시그널 통과 → 진입 검토 중..." if self._ko
                    else f"{sym_str} signal passed → checking entry...")
        return msg[:150]

    def _translate_exit(self, msg: str) -> str:
        sym = re.search(r"([A-Z]+USDT)", msg)
        sym_str = sym.group(1) if sym else ""
        roi_m = re.search(r"roi[_=]([\-+\d.]+)", msg, re.I)
        roi_str = f" (ROI {roi_m.group(1)}%)" if roi_m else ""
        if "PARTIAL_TP" in msg or "부분" in msg:
            return (f"{sym_str} 부분 익절 실행{roi_str}" if self._ko
                    else f"{sym_str} partial take-profit{roi_str}")
        if "TRAIL" in msg or "트레일" in msg:
            return (f"{sym_str} 트레일링 스탑 청산{roi_str}" if self._ko
                    else f"{sym_str} trailing stop exit{roi_str}")
        if "STOP_LOSS" in msg or "손절" in msg:
            return (f"{sym_str} 손절 청산{roi_str}" if self._ko
                    else f"{sym_str} stop-loss exit{roi_str}")
        return (f"{sym_str} 포지션 청산{roi_str}" if self._ko
                else f"{sym_str} position closed{roi_str}")

    def _translate_neural(self, msg: str) -> str:
        m = re.search(r"n=(\d+).*acc=([\d.]+)", msg)
        if m:
            n, acc = m.group(1), m.group(2)
            return (f"신경망 학습 {n}건 완료, 정확도 {acc}%"
                    if self._ko else f"Neural learned {n} trades, accuracy {acc}%")
        return ("신경망 업데이트 중..." if self._ko else "Neural network updating...")

    def _translate_market(self, msg: str) -> str:
        if "regime" in msg.lower() or "레짐" in msg:
            regime = "알 수 없음"
            if "trend_up" in msg:
                regime = "트렌드 상승" if self._ko else "Trend Up"
            elif "trend_down" in msg or "trend_dn" in msg:
                regime = "트렌드 하락" if self._ko else "Trend Down"
            elif "chop" in msg:
                regime = "횡보" if self._ko else "Chop/Range"
            return (f"시장 레짐: {regime}" if self._ko else f"Market regime: {regime}")
        if "spike" in msg.lower():
            return ("가격 급변동 감지 — 스파이크 가드 작동" if self._ko
                    else "Price spike detected — spike guard activated")
        return msg[:150]


# ══════════════════════════════════════════════════════════════════════════════
#  거래 패턴 분석 & 자가 개선
# ══════════════════════════════════════════════════════════════════════════════

class TradeAnalyzer:
    """trade_history.jsonl 분석으로 패턴 인식 및 개선 제안 생성."""

    def __init__(self, trade_log_path: str, language: str = "ko"):
        self.trade_log_path = trade_log_path
        self.language = language
        self._trades_cache: List[dict] = []
        self._cache_ts: float = 0.0
        self._suggestions_cache: List[dict] = []
        self._suggestions_ts: float = 0.0

    @property
    def _ko(self) -> bool:
        return self.language == "ko"

    def _load_recent_trades(self, n: int = MAX_TRADES) -> List[dict]:
        """최근 N건 거래 로드 (캐시 적용)."""
        now = time.time()
        if self._trades_cache and (now - self._cache_ts) < TRADE_CACHE_TTL:
            return self._trades_cache[-n:]
        try:
            if not os.path.exists(self.trade_log_path):
                return []
            trades = []
            with open(self.trade_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            self._trades_cache = trades[-MAX_TRADES:]
            self._cache_ts = now
            return self._trades_cache[-n:]
        except Exception:
            return []

    def get_trade_patterns(self, n: int = PATTERN_TRADES) -> Dict[str, Any]:
        """최근 N건 거래 패턴 분석."""
        trades = self._load_recent_trades(n)
        if not trades:
            return {"total": 0, "patterns": [], "weaknesses": []}

        # 방향별 승률
        dir_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
        # 트리거별 승률
        trigger_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
        # 시간대별 승률
        hour_stats: Dict[int, Dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
        # 전체
        total_roi = 0.0
        total_pnl = 0.0
        consecutive_losses = 0
        max_consecutive_losses = 0

        for t in trades:
            side = t.get("side", "UNKNOWN")
            roi = _safe_float(t.get("roi_pct", t.get("roi_percent", 0)))
            pnl = _safe_float(t.get("pnl", t.get("pnl_value", 0)))
            trigger = t.get("trigger", t.get("exit_reason", "UNKNOWN"))
            ts = _safe_float(t.get("ts", 0))

            is_win = roi > 0
            total_roi += roi
            total_pnl += pnl

            # 방향별
            dir_stats[side]["total"] += 1
            dir_stats[side]["wins" if is_win else "losses"] += 1

            # 트리거별
            trigger_stats[trigger]["total"] += 1
            trigger_stats[trigger]["wins" if is_win else "losses"] += 1

            # 시간대별
            if ts > 0:
                hour = int((ts % 86400) / 3600)
                hour_stats[hour]["total"] += 1
                hour_stats[hour]["wins" if is_win else "losses"] += 1

            # 연속 손실
            if not is_win:
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
            else:
                consecutive_losses = 0

        # 약점 탐지
        weaknesses = []
        for side, stats in dir_stats.items():
            if stats["total"] >= 5:
                wr = stats["wins"] / stats["total"]
                if wr < WEAK_WIN_RATE:
                    weaknesses.append({
                        "type": "direction",
                        "key": side,
                        "win_rate": round(wr * 100, 1),
                        "sample_size": stats["total"],
                        "msg_ko": f"{side} 포지션 승률이 {wr*100:.0f}%로 낮습니다 ({stats['total']}건 중 {stats['wins']}건 승리).",
                        "msg_en": f"{side} position win rate is {wr*100:.0f}% ({stats['wins']}/{stats['total']} wins).",
                    })

        for trigger, stats in trigger_stats.items():
            if stats["total"] >= 3 and stats["losses"] > stats["wins"]:
                wr = stats["wins"] / stats["total"]
                weaknesses.append({
                    "type": "trigger",
                    "key": trigger,
                    "win_rate": round(wr * 100, 1),
                    "sample_size": stats["total"],
                    "msg_ko": f"'{trigger}' 청산 트리거의 수익률이 좋지 않습니다 (승률 {wr*100:.0f}%).",
                    "msg_en": f"'{trigger}' exit trigger has poor performance (win rate {wr*100:.0f}%).",
                })

        return {
            "total": len(trades),
            "avg_roi": round(total_roi / max(len(trades), 1), 2),
            "total_pnl": round(total_pnl, 4),
            "max_consecutive_losses": max_consecutive_losses,
            "direction_stats": {k: dict(v) for k, v in dir_stats.items()},
            "weaknesses": weaknesses,
        }

    def get_improvement_suggestions(self) -> List[Dict[str, Any]]:
        """자가 개선 제안 생성."""
        now = time.time()
        if self._suggestions_cache and (now - self._suggestions_ts) < SUGGESTION_INTERVAL:
            return self._suggestions_cache

        suggestions = []
        patterns = self.get_trade_patterns(PATTERN_TRADES)

        if patterns["total"] < 5:
            suggestions.append({
                "priority": "low",
                "icon": "ℹ",
                "msg_ko": f"아직 거래 데이터가 부족합니다 ({patterns['total']}건). 최소 10건 이상 필요합니다.",
                "msg_en": f"Not enough trade data yet ({patterns['total']} trades). Need at least 10.",
            })
            self._suggestions_cache = suggestions
            self._suggestions_ts = now
            return suggestions

        # 1) 방향별 약점 → 진입 기준 강화 제안
        for w in patterns.get("weaknesses", []):
            if w["type"] == "direction":
                side = w["key"]
                wr = w["win_rate"]
                suggestions.append({
                    "priority": "high" if wr < 30 else "medium",
                    "icon": "💡",
                    "msg_ko": (f"{side} 포지션 승률이 {wr}%입니다.\n"
                              f"→ {side} 진입 조건 강화를 권장합니다 (momentum 임계값 ↑ 또는 composite 기준 ↑)."),
                    "msg_en": (f"{side} win rate is {wr}%.\n"
                              f"→ Consider tightening {side} entry (raise momentum threshold or composite min)."),
                })

        # 2) 연속 손실 → 포지션 축소 제안
        max_cl = patterns["max_consecutive_losses"]
        if max_cl >= 4:
            suggestions.append({
                "priority": "high",
                "icon": "⚠",
                "msg_ko": f"최근 {max_cl}연속 손실이 발생했습니다.\n→ 포지션 크기 축소 또는 쿨다운 연장을 검토하세요.",
                "msg_en": f"{max_cl} consecutive losses detected.\n→ Consider reducing position size or extending cooldown.",
            })

        # 3) 평균 ROI 추세
        avg_roi = patterns["avg_roi"]
        if avg_roi < -0.5:
            suggestions.append({
                "priority": "high",
                "icon": "📉",
                "msg_ko": f"최근 {patterns['total']}건 평균 ROI가 {avg_roi:+.2f}%입니다.\n→ 리스크 관리 강화가 필요합니다.",
                "msg_en": f"Recent {patterns['total']} trades avg ROI is {avg_roi:+.2f}%.\n→ Risk management needs attention.",
            })
        elif avg_roi > 0.5:
            suggestions.append({
                "priority": "low",
                "icon": "📈",
                "msg_ko": f"최근 {patterns['total']}건 평균 ROI가 {avg_roi:+.2f}%로 양호합니다. 현재 전략을 유지하세요.",
                "msg_en": f"Recent {patterns['total']} trades avg ROI is {avg_roi:+.2f}%. Strategy is performing well.",
            })

        # 4) 특정 청산 트리거 약점
        for w in patterns.get("weaknesses", []):
            if w["type"] == "trigger" and w["sample_size"] >= 5:
                suggestions.append({
                    "priority": "medium",
                    "icon": "🔧",
                    "msg_ko": f"'{w['key']}' 청산의 승률이 {w['win_rate']}%입니다.\n→ 해당 청산 조건의 파라미터 조정을 검토하세요.",
                    "msg_en": f"'{w['key']}' exit trigger has {w['win_rate']}% win rate.\n→ Consider adjusting this exit parameter.",
                })

        # 정렬: priority high → medium → low
        priority_order = {"high": 0, "medium": 1, "low": 2}
        suggestions.sort(key=lambda s: priority_order.get(s["priority"], 99))

        self._suggestions_cache = suggestions[:5]  # 최대 5개
        self._suggestions_ts = now
        return self._suggestions_cache


# ══════════════════════════════════════════════════════════════════════════════
#  메인 AIAdvisor 클래스
# ══════════════════════════════════════════════════════════════════════════════

class AIAdvisor:
    """
    AI Trading Advisor — 엔진 이벤트 스트리밍, 자연어 변환, 자가 개선 제안.
    GUI에서 인스턴스화하여 주기적으로 poll/translate/suggest 호출.
    """

    def __init__(self, logs_dir: str, language: str = "ko"):
        self.logs_dir = logs_dir
        self.language = language
        self.translator = EventTranslator(language)
        self.trade_analyzer: Optional[TradeAnalyzer] = None

        # 파일 경로
        self.notification_path = os.path.join(logs_dir, "notifications.log")
        self._file_pointer: int = 0
        # 가능한 trade log 경로들
        for tp in [
            os.path.join(logs_dir, "trade_history.jsonl"),
            os.path.join(logs_dir, "..", "logs", "trade_history.jsonl"),
            os.path.join(logs_dir, "binance_futures_bot1_1", "logs", "trade_history.jsonl"),
        ]:
            if os.path.exists(tp):
                self.trade_analyzer = TradeAnalyzer(tp, language)
                break
        if self.trade_analyzer is None:
            # fallback: 첫 번째 경로로 초기화 (파일 없어도)
            self.trade_analyzer = TradeAnalyzer(
                os.path.join(logs_dir, "binance_futures_bot1_1", "logs", "trade_history.jsonl"),
                language,
            )

        # neural_scorer 경로
        self._neural_path: Optional[str] = None
        for np_ in [
            os.path.join(logs_dir, "binance_futures_bot1_1", "logs", "neural_scorer.json"),
            os.path.join(logs_dir, "logs", "neural_scorer.json"),
        ]:
            if os.path.exists(np_):
                self._neural_path = np_
                break

        # 캐시
        self._neural_cache: Optional[dict] = None
        self._neural_cache_ts: float = 0.0

        # 이벤트 버퍼
        self._events: deque = deque(maxlen=MAX_EVENTS)

        # 파일 포인터 초기화 (끝부터 읽기)
        try:
            if os.path.exists(self.notification_path):
                self._file_pointer = os.path.getsize(self.notification_path)
        except Exception:
            pass

    def set_language(self, lang: str):
        self.language = lang
        self.translator.language = lang
        if self.trade_analyzer:
            self.trade_analyzer.language = lang

    # ── 이벤트 폴링 ─────────────────────────────────────────────────────────
    def poll_new_events(self) -> List[dict]:
        """notifications.log에서 새 줄 읽기 → ParsedEvent 리스트 반환."""
        new_events = []
        try:
            if not os.path.exists(self.notification_path):
                return []
            size = os.path.getsize(self.notification_path)
            if size < self._file_pointer:
                self._file_pointer = 0  # 파일 truncated

            if size <= self._file_pointer:
                return []

            with open(self.notification_path, "r", encoding="utf-8") as f:
                f.seek(self._file_pointer)
                new_data = f.read()
                self._file_pointer = f.tell()

            for line in new_data.split("\n"):
                ev = _parse_notification_line(line)
                if ev:
                    ev.friendly_msg = self.translator.translate(ev)
                    d = ev.to_dict()
                    self._events.append(d)
                    new_events.append(d)
        except Exception as e:
            logger.debug("AIAdvisor poll error: %s", e)

        return new_events

    def get_recent_events(self, limit: int = 50) -> List[dict]:
        """최근 이벤트 반환 (메모리 캐시)."""
        events = list(self._events)
        return events[-limit:]

    # ── 시장 요약 ────────────────────────────────────────────────────────────
    def get_market_summary(self) -> Dict[str, Any]:
        """현재 시장 상태 요약."""
        summary: Dict[str, Any] = {
            "regime": "unknown",
            "kill_switch": False,
            "neural_status": "unknown",
            "neural_accuracy": 0.0,
            "neural_n_trained": 0,
        }

        # 최근 이벤트에서 레짐 추론
        for ev in reversed(list(self._events)):
            if ev["category"] == CATEGORY_MARKET or "regime" in ev["raw_msg"].lower():
                if "trend_up" in ev["raw_msg"]:
                    summary["regime"] = "trend_up"
                elif "trend_down" in ev["raw_msg"] or "trend_dn" in ev["raw_msg"]:
                    summary["regime"] = "trend_down"
                elif "chop" in ev["raw_msg"]:
                    summary["regime"] = "chop"
                break
            if ev["category"] == CATEGORY_TUNE and "regime=" in ev["raw_msg"]:
                m = re.search(r"regime=(\w+)", ev["raw_msg"])
                if m:
                    summary["regime"] = m.group(1)
                break

        # 킬스위치 상태
        for ev in reversed(list(self._events)):
            if ev["category"] == CATEGORY_KILL:
                if "재개" in ev["raw_msg"] or "resumed" in ev["raw_msg"].lower():
                    summary["kill_switch"] = False
                else:
                    summary["kill_switch"] = True
                break

        # 신경망 상태
        neural = self._get_neural_status()
        if neural:
            summary["neural_status"] = "active" if neural.get("active") else "inactive"
            summary["neural_accuracy"] = neural.get("accuracy", 0.0)
            summary["neural_n_trained"] = neural.get("n_trained", 0)

        return summary

    def get_market_summary_text(self) -> str:
        """시장 요약을 자연어로."""
        s = self.get_market_summary()
        ko = self.language == "ko"

        regime_map_ko = {"trend_up": "📈 트렌드 상승", "trend_down": "📉 트렌드 하락",
                         "chop": "↔ 횡보", "unknown": "❓ 분석 중"}
        regime_map_en = {"trend_up": "📈 Trend Up", "trend_down": "📉 Trend Down",
                         "chop": "↔ Chop/Range", "unknown": "❓ Analyzing"}

        regime_txt = (regime_map_ko if ko else regime_map_en).get(s["regime"], s["regime"])
        kill_txt = ("🛑 킬스위치 활성" if ko else "🛑 Kill Switch ON") if s["kill_switch"] else ""
        neural_txt = ""
        if s["neural_n_trained"] > 0:
            neural_txt = (f"🧠 {s['neural_n_trained']}건 학습 / 정확도 {s['neural_accuracy']}%"
                         if ko else
                         f"🧠 {s['neural_n_trained']} trained / Acc {s['neural_accuracy']}%")

        parts = [regime_txt]
        if kill_txt:
            parts.append(kill_txt)
        if neural_txt:
            parts.append(neural_txt)
        return " | ".join(parts)

    def _get_neural_status(self) -> Optional[dict]:
        """neural_scorer.json 캐시 읽기."""
        now = time.time()
        if self._neural_cache and (now - self._neural_cache_ts) < NEURAL_CACHE_TTL:
            return self._neural_cache

        # 실행 중 엔진에서 직접 조회 시도
        try:
            from binance_futures_bot1_1 import main as _eng_main
            _ce = getattr(_eng_main, "current_engine", None)
            if _ce:
                ns = getattr(_ce, "neural_scorer", None)
                if ns:
                    self._neural_cache = ns.status()
                    self._neural_cache_ts = now
                    return self._neural_cache
        except Exception:
            pass

        # 파일 fallback
        if self._neural_path and os.path.exists(self._neural_path):
            try:
                with open(self._neural_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                tr = d.get("tracker", {})
                recs = tr.get("records", [])
                self._neural_cache = {
                    "n_trained": d.get("n_trained", 0),
                    "accuracy": round(sum(recs) / len(recs) * 100, 1) if recs else 0.0,
                    "active": d.get("active", False),
                    "ready": d.get("n_trained", 0) >= 50,
                }
                self._neural_cache_ts = now
                return self._neural_cache
            except Exception:
                pass
        return None

    # ── 거래 분석 & 개선 제안 ────────────────────────────────────────────────
    def get_trade_patterns(self) -> Dict[str, Any]:
        if self.trade_analyzer:
            return self.trade_analyzer.get_trade_patterns()
        return {"total": 0, "patterns": [], "weaknesses": []}

    def get_improvement_suggestions(self) -> List[Dict[str, Any]]:
        if self.trade_analyzer:
            return self.trade_analyzer.get_improvement_suggestions()
        return []
