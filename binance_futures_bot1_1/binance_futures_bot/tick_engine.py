"""Tick-based trading engine prototype for v1.1"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any, Set
import asyncio
import json
import logging
import math
import statistics
import os
import time
from collections import deque

from .auto_tuner import AutoTuner
from .config import EngineConfig
from .engine_helpers import compute_take_profit_levels, update_trailing_stop
from .exchange_utils import compliant_quantity, min_notional_from_filters
from .position_snapshot import PositionSnapshot
from .snapshot_manager import build_snapshot, update_snapshot
from binance import AsyncClient
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT
from binance.exceptions import BinanceAPIException
from .neural_scorer import NeuralScorer, build_feature_vector
from .feature_flags import FeatureFlagManager
from .kpi_tracker import KPITracker
from .consensus_scorer import ConsensusScorer
from .execution_quality import ExecutionQualityEngine

logger = logging.getLogger(__name__)


@dataclass
class SymbolSnapshot:
    symbol: str
    volume_24h: float
    notional_24h: float
    volatility: float
    momentum_pct: float
    atr: float
    price: float
    mark_price: float
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    spread_bps: float = 0.0
    tradable: bool = True
    high_24h: float = 0.0   # 24h 고가 (ATR 정밀 계산용)
    low_24h: float = 0.0    # 24h 저가
    momentum_5m: float = 0.0  # 5분 단기 return (진입 방향 결정 기준)


@dataclass
class SignalDecision:
    symbol: str
    direction: str  # "LONG" or "SHORT"
    strength: float
    reason: str


class TickEngine:
    EXIT_REASON_STOP_LOSS = "STOP_LOSS"
    EXIT_REASON_SPIKE_GUARD = "SPIKE_GUARD"
    EXIT_REASON_TAKE_PROFIT = "TAKE_PROFIT"
    EXIT_REASON_TRAILING = "TRAILING_STOP"
    EXIT_REASON_TIME_STOP = "TIME_STOP"
    EXIT_REASON_SIGNAL_DECAY = "SIGNAL_DECAY"
    EXIT_REASON_MANUAL = "MANUAL"
    EXIT_REASON_SESSION_DD = "SESSION_DD"

    STRATEGY_FAILURE_REASONS = {
        "STRATEGY_REJECT",
        "ACCOUNT_RISK",
        "INSUFFICIENT_BALANCE",
        "MARGIN_INSUFFICIENT",
    }
    TECH_FAILURE_REASONS = {
        "RATE_LIMIT",
        "MIN_NOTIONAL",
        "PRECISION",
        "SYMBOL_CLOSED",
        "UNKNOWN",
    }
    # [PATCH-17] 상관성 높은 메이저 심볼 — 동시 동방향 진입 제한 대상
    MAJOR_SYMBOLS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"}

    HARD_CAPS = {
        # [PATCH-13c] config.py 기본값 기준 ±50% 범위로 엄격 제한
        "position_pct": (0.03, 0.08),        # 3~8% (config 기본 6%, 최대 8%)
        "leverage_min": (1, 5),              # 1~5x
        "leverage_max": (2, 12),             # 2~12x (config 기본 10x)
        "max_loss_per_position": (0.5, 2.2), # 0.5~2.2% (config 기본 1.8%, 최대 2.2%)
        "watch_limit": (1, 30),
        "max_open_symbols": (1, 20),
    }
    # D: non-expert hard cap for leverage_max
    LEVERAGE_MAX_NON_EXPERT: int = 50
    ROC_LIMITS = {
        # [PATCH-13] ROC 제한 강화 (급격한 변경 방지)
        "position_pct": 0.005,  # max ±0.5%p per update (기존 1%)
        "leverage_min": 2,      # max ±2x per update (기존 5)
        "leverage_max": 3,      # max ±3x per update (기존 10)
        "max_loss_per_position": 1.0,
    }
    TUNABLE_PARAM_KEYS = (
        "position_pct",
        "total_risk_budget",
        "leverage_min",
        "leverage_max",
        "max_loss_per_position",
        "watch_limit",
        "max_open_symbols",
        "auto_tune_mode",
    )

    def __init__(self, client: AsyncClient, config: EngineConfig, *, testnet: bool, notification_path: str = "notifications.log"):
        self.client = client
        self.config = config
        self.testnet = testnet
        self.notification_path = notification_path
        self._last_param_values: Dict[str, float] = {}
        self._apply_hard_caps()
        self._stable_param_snapshot: Dict[str, float] = {}
        self._pending_param_snapshot: Optional[Dict[str, float]] = None
        self._pending_param_applied_ts: float = 0.0
        self._pending_grace_metrics: Optional[dict] = None  # auto-tune grace period state
        self._capture_initial_param_snapshot()
        self.running = False
        self._exchange_info = None
        self._balance_cache = {"ts": 0.0, "available": 0.0}
        self._last_cycle_symbols: List[str] = []
        self._cycle_index = 0
        self._skip_counts: dict[str, int] = {}
        self._symbol_blocked: set[str] = set()
        self._open_symbols: set[str] = set()
        self._symbol_last_exit_ts: dict[str, float] = {}  # [PATCH-11] 재진입 쿨다운용
        self._closing_symbols: set[str] = set()
        self._pending_orders: set[str] = set()
        self._pending_closes: set[str] = set()
        self._price_history: dict[str, deque] = {}
        self._symbol_leverage: dict[str, int] = {}
        self._leverage_limits_cache: dict[str, Tuple[int, int, float]] = {}
        self._spike_guard_last_check = 0.0
        self._spike_blocked_until: Dict[str, float] = {}
        self._spike_reentry_until: Dict[str, float] = {}
        self._global_spike_reason = ""
        self._stat_window = {
            "evaluated": deque(),
            "passed": deque(),
            "orders": deque(),
            "fills": deque(),
            "signals_evaluated": deque(),
            "signals_passed": deque(),
            "entry_blocked_ratelimit": deque(),
            "entry_blocked_cooldown": deque(),
            "entry_blocked_spike_guard": deque(),
            "entry_blocked_portfolio_cap": deque(),
            "entry_blocked_mark_gap": deque(),
            "entry_blocked_edge": deque(),
            "entry_blocked_busy": deque(),
            "exit_busy": deque(),
            "exit_stop_loss": deque(),
            "exit_take_profit": deque(),
            "exit_trailing": deque(),
            "exit_time_stop": deque(),
            "exit_signal_decay": deque(),
            "exit_spike_guard": deque(),
            # ── Execution Quality Tracking ──
            "maker_fills": deque(),
            "taker_fills": deque(),
            "fill_latencies_ms": deque(),
        }
        self._hold_window: deque[tuple[float, float]] = deque()
        self._pnl_outcomes: deque[tuple[float, float]] = deque()
        self._init_flow_stats()
        self._order_failures = deque()
        self._entry_cooldown_until = 0.0
        self._rate_limit_until = 0.0
        self._global_spike_cooldown_until = 0.0
        self._last_known_regime: str = "chop"  # [PATCH-17] SL 레짐 판별용 캐시
        self._income_cache = {"ts": 0.0, "value": 0.0}
        self._income_breakdown_cache = {"ts": 0.0, "data": {}}
        self._pnl_fast_window_sec = int(max(60, getattr(self.config, "pnl_fast_window_sec", 1800)))
        self._pnl_fast_window: deque[tuple[float, float]] = deque()
        self._pnl_fast_sum = 0.0
        self._pnl_fast_cache = {"ts": 0.0, "value": 0.0, "window_sec": self._pnl_fast_window_sec, "source": "api"}
        self._pnl_fast_seen_trades: deque[tuple[float, str]] = deque()
        self._pnl_fast_seen_keys: set[str] = set()
        self._metrics_window_sec = 1800
        self._returns_window_sec = 1800
        self._rv_short_window = 300
        self._rv_long_window = 900
        base_budget = float(getattr(self.config, "total_risk_budget", getattr(self.config, "position_pct", 0.06)))  # [PATCH-14] 0.10→0.06 config 정렬
        self.total_risk_budget = max(base_budget, 0.01)
        self.config.total_risk_budget = self.total_risk_budget
        self.session_loss_limit_pct = max(0.0, float(getattr(self.config, "session_loss_limit_pct", 0.0)))
        self.session_loss_window_minutes = max(60, int(getattr(self.config, "session_loss_window_minutes", 1440)))
        self.kill_switch_cooldown_min = max(1, int(getattr(self.config, "kill_switch_cooldown_min", 120)))
        self.global_spike_cooldown_min = max(1, int(getattr(self.config, "global_spike_cooldown_min", 5)))
        self.spark_reentry_candles = max(1, int(getattr(self.config, "spark_reentry_candles", 3)))
        self.spark_reentry_seconds = self.spark_reentry_candles * 60
        self.session_start_ts = time.time()
        self.session_start_balance: Optional[float] = None
        self.kill_switch_triggered = False
        self.kill_switch_release_ts = 0.0
        self.kill_switch_reason = ""
        self.position_snapshots: Dict[str, PositionSnapshot] = {}
        self._snapshot_seeds: Dict[str, Dict[str, Any]] = {}
        self._isolated_symbols: Set[str] = set()
        self._last_atr_estimate: float = 0.0
        base_dir = os.path.dirname(self.notification_path)
        self.metrics_path = os.path.join(base_dir, "metrics.jsonl")
        self.auto_tuner_state_path = os.path.join(base_dir, "auto_tuner_state.json")
        # 사용자 원본 설정값 보존 (auto-tune이 덮어써도 floor로 사용)
        self._user_watch_limit = int(getattr(config, "watch_limit", 10))
        self._user_max_open_symbols = int(getattr(config, "max_open_symbols", 5))
        self.trade_log_path = os.path.join(base_dir, "trade_history.jsonl")
        self._ensure_extended_config()
        self.auto_boost_position_pct = bool(getattr(self.config, "auto_boost_position_pct", False))
        self.auto_tuner = self._init_auto_tuner()
        self.risk_per_position = self._compute_risk_per_position()
        # --- 신규 캐시 (개선 기능) ---
        self._notional_history: dict[str, deque] = {}   # 거래량 이력 (복합 신호용)
        self._funding_cache: dict[str, dict] = {}       # 펀딩 레이트 캐시
        # ── 온라인 학습 신경망 스코어러 ─────────────────────────────────────
        _scorer_path = os.path.join(base_dir, "neural_scorer.json")
        self.neural_scorer = NeuralScorer(model_path=_scorer_path)
        # ── Feature Flags (런타임 기능 토글) ──────────────────────────
        _flags_path = os.path.join(base_dir, "feature_flags.json")
        self.feature_flags = FeatureFlagManager(_flags_path)
        _n_overrides = self.feature_flags.apply_to_config(self.config)
        if _n_overrides:
            logger.info("[INIT] Feature flags: %d config overrides applied", _n_overrides)
        # ── KPI Tracker (6개 핵심 지표 추적) ─────────────────────────
        self.kpi_tracker: Optional[KPITracker] = (
            KPITracker(self) if self.feature_flags.is_enabled("kpi_tracker_enabled") else None
        )
        # ── Execution Quality Engine (심볼별 maker 계측 + 자동 조정) ──
        self.exec_quality: Optional[ExecutionQualityEngine] = (
            ExecutionQualityEngine(self.config)
            if self.feature_flags.is_enabled("execution_quality_tracking") else None
        )
        # ── 3-Party Consensus (Rule/Neural/Tuner 합의) ────────────
        self.consensus_scorer: Optional[ConsensusScorer] = (
            ConsensusScorer(self) if self.feature_flags.is_enabled("consensus_scoring_enabled") else None
        )
        self.stream = None                              # WebSocket stream (externally injected)
        # ── Commercial safety state ────────────────────────────────────
        self._engine_boot_time: float = 0.0          # set in run() for startup grace
        # B: log masking — populated when engine starts
        self._log_mask_patterns: list = []
        # C1: API weight / call-rate self-throttling
        self._api_call_times: "deque[float]" = deque()   # rolling 1-minute window
        self._api_calls_per_min_limit: int = int(getattr(self.config, "api_calls_per_min_limit", 1200))
        self._api_weight_used: int = 0                     # last known X-MBX-USED-WEIGHT-1M
        self._api_weight_warned: bool = False              # suppress repeated warn
        self._consecutive_rollbacks: int = 0          # E: auto-tune rollback counter
        self._auto_tune_force_disabled: bool = False  # E: set True after max rollbacks
        # v2: 봇 시작 시 config.auto_tune_enabled=True면 force_disabled 해제
        # 이전 세션에서 연속 롤백으로 비활성화되었더라도 재시작 시 리셋
        if bool(getattr(self.config, "auto_tune_enabled", True)):
            self._auto_tune_force_disabled = False

    async def run(self):
        self.running = True
        self._engine_boot_time = time.time()          # C2: startup grace reference
        self._last_time_sync = time.time()             # periodic server time sync
        self._init_log_masks()                         # B: register sensitive strings
        await self._hydrate_positions()
        await self._sync_open_orders()                 # C2+C3: reconcile open orders on boot
        while self.running:
            try:
                # periodic time re-sync every 30 min to prevent -1021 drift
                if time.time() - self._last_time_sync > 1800:
                    await self._resync_server_time()
                    self._last_time_sync = time.time()
                await self.tick()
            except Exception as exc:
                logger.exception("Tick error: %s", exc)
            await asyncio.sleep(5)

    async def tick(self):
        positions = await self._fetch_active_positions()
        self._record_api_call()  # C1: track API usage
        snapshots = await self.fetch_symbol_snapshots()
        self._record_api_call()  # C1: track API usage
        self._update_trailing_stops(snapshots)
        await self._enforce_stop_losses(positions, snapshots)
        await self._enforce_spike_guard(positions)
        await self._enforce_take_profit(snapshots, positions)
        await self._evaluate_profit_exit_layers(snapshots, positions)
        filtered = self.filter_symbols(snapshots)
        for snap in filtered:
            await self.try_enter_position(snap)
        await self._run_auto_tuner_cycle(snapshots)
        await self._enforce_time_and_signal_decay(positions, snapshots)
        # auto_tune OFF여도 세션 손실 한도는 항상 체크
        if not bool(getattr(self.config, "auto_tune_enabled", True)):
            _pnl = await self._get_fast_trade_pnl()
            self._check_session_loss_limit(_pnl)
        self._emit_metric_snapshot()
        # ── KPI Tracker: 매 틱 계산 + 배치 저장 ──
        if self.kpi_tracker:
            try:
                self.kpi_tracker.compute_snapshot()
                self.kpi_tracker.maybe_flush_batch()
            except Exception as _kpi_err:
                logger.debug("KPI tracker error: %s", _kpi_err)
        # 5분마다 neural scorer 저장
        if int(time.time()) % 300 < 5:
            try:
                self.neural_scorer.save()
            except Exception:
                pass

    
    # ------------------------------ Cost / MTF helpers ------------------------------
    def _median(self, values: List[float]) -> float:
        vals = [float(v) for v in values if v is not None and not math.isnan(float(v))]
        if not vals:
            return 0.0
        try:
            return float(statistics.median(vals))
        except Exception:
            vals.sort()
            mid = len(vals) // 2
            return float(vals[mid])

    def _percentile(self, values: List[float], p: float) -> float:
        vals = [float(v) for v in values if v is not None and not math.isnan(float(v))]
        if not vals:
            return 0.0
        vals.sort()
        k = max(0, min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1)))))
        return float(vals[k])

    def _compute_spread_stats(self, snapshots: List[SymbolSnapshot]) -> Dict[str, float]:
        spreads = [float(s.spread_bps) for s in snapshots if getattr(s, "tradable", True) and float(getattr(s, "spread_bps", 0.0)) > 0]
        return {
            "spread_bps_med": self._median(spreads),
            "spread_bps_p90": self._percentile(spreads, 90.0),
            "spread_samples": float(len(spreads)),
        }

    def _compute_tca_metrics(self, window_sec: int) -> Dict[str, float]:
        """Best-effort transaction-cost metrics from recent trade_history.jsonl."""
        now = time.time()
        path = self.trade_log_path
        if not path or not os.path.exists(path):
            return {"slippage_bps_med": 0.0, "slippage_bps_p90": 0.0, "tca_samples": 0.0}
        slips: List[float] = []
        spreads: List[float] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    ts = float(ev.get("ts", 0.0) or 0.0)
                    if ts <= 0 or ts < now - window_sec:
                        continue
                    sb = ev.get("slippage_bps")
                    sp = ev.get("spread_bps")
                    if sb is not None:
                        try:
                            slips.append(float(sb))
                        except Exception:
                            pass
                    if sp is not None:
                        try:
                            spreads.append(float(sp))
                        except Exception:
                            pass
        except Exception:
            return {"slippage_bps_med": 0.0, "slippage_bps_p90": 0.0, "tca_samples": 0.0}
        return {
            "slippage_bps_med": self._median(slips),
            "slippage_bps_p90": self._percentile(slips, 90.0),
            "tca_spread_bps_med": self._median(spreads),
            "tca_spread_bps_p90": self._percentile(spreads, 90.0),
            "tca_samples": float(max(len(slips), len(spreads))),
        }

    def _mtf_ema_slope_bps(self, symbol: str, timeframe_sec: int, ema_period: int, lookback_bars: int = 3) -> float:
        """EMA slope in bps over last 'lookback_bars' bars for a given bucket timeframe."""
        dq = self._price_history.get(symbol)
        if not dq:
            return float("nan")  # no data
        now = time.time()
        cutoff = now - max(self._returns_window_sec, timeframe_sec * max(ema_period + lookback_bars + 5, 60))
        points = [(ts, px) for ts, px in dq if ts >= cutoff and px and px > 0]
        if len(points) < ema_period + lookback_bars + 2:
            return float("nan")  # data insufficient
        # bucketize: use last price per bucket
        buckets: Dict[int, float] = {}
        for ts, px in points:
            b = int(ts // timeframe_sec)
            buckets[b] = float(px)
        keys = sorted(buckets.keys())
        closes = [buckets[k] for k in keys]
        if len(closes) < ema_period + lookback_bars + 2:
            return float("nan")  # data insufficient
        alpha = 2.0 / (ema_period + 1.0)
        ema = closes[0]
        emas: List[float] = [ema]
        for c in closes[1:]:
            ema = alpha * c + (1 - alpha) * ema
            emas.append(ema)
        if len(emas) < lookback_bars + 2:
            return float("nan")  # data insufficient
        e0 = emas[-(lookback_bars + 1)]
        e1 = emas[-1]
        if not e0 or e0 <= 0:
            return 0.0
        return float((e1 - e0) / e0 * 10000.0)

    def _mtf_confirm_ok(self, symbol: str, direction: str) -> bool:
        if not bool(getattr(self.config, "enable_mtf_ema_confirm", True)):
            return True
        tfs = getattr(self.config, "mtf_timeframes_sec", [60, 300]) or [60, 300]
        ema_period = int(getattr(self.config, "mtf_ema_period", 21))
        min_slope = float(getattr(self.config, "mtf_min_slope_bps", 2.0))
        direction = str(direction).upper()
        want_up = direction == "LONG"
        valid_count = 0
        against_count = 0
        for tf in tfs:
            try:
                slope = self._mtf_ema_slope_bps(symbol, int(tf), ema_period)
            except Exception:
                slope = float("nan")
            if slope != slope:  # NaN = 데이터 부족 → 이 TF 스킵 (0.0 처리 금지)
                continue
            valid_count += 1
            if want_up and slope < min_slope:
                against_count += 1
            elif not want_up and slope > -min_slope:
                against_count += 1
        # 유효 TF 없으면 통과 (데이터 부족으로 알트코인 차단 방지)
        if valid_count == 0:
            return True
        # 과반수 이상 반대 방향이면 차단
        return against_count < valid_count

# ------------------------------------------------------------------
    def _ensure_extended_config(self):
        base_momentum = getattr(self.config, "momentum_min", 0.001)
        if not hasattr(self.config, "momentum_min_long"):
            self.config.momentum_min_long = max(base_momentum, 0.001)
        if not hasattr(self.config, "momentum_min_short"):
            self.config.momentum_min_short = -abs(base_momentum)
        if not hasattr(self.config, "momentum_min"):
            self.config.momentum_min = self.config.momentum_min_long
        if not hasattr(self.config, "auto_tune_enabled"):
            self.config.auto_tune_enabled = True

    async def _ensure_isolated_margin(self, symbol: str) -> bool:
        if not symbol:
            return False
        if symbol in self._isolated_symbols:
            return True
        try:
            await self.client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
        except BinanceAPIException as exc:
            if exc.code == -4046:
                pass
            else:
                logger.warning("Failed to set isolated margin for %s: %s", symbol, exc)
                return False
        except Exception as exc:
            logger.warning("Failed to set isolated margin for %s: %s", symbol, exc)
            return False
        self._isolated_symbols.add(symbol)
        return True

    def _init_auto_tuner(self) -> AutoTuner:
        shadow_mode = getattr(self.config, "auto_tune_shadow_mode", True)
        shadow_cycles_cfg = getattr(self.config, "auto_tune_shadow_cycles", 3)
        shadow_min = max(5, int(getattr(self.config, "auto_tune_shadow_min_cycles", 5)))
        shadow_cycles = max(shadow_min, int(shadow_cycles_cfg))  # E: floor at shadow_min_cycles
        cooldown_min = getattr(self.config, "auto_tune_cooldown_min", 10)
        max_tunes = getattr(self.config, "auto_tune_max_per_day", 6)
        tuner = AutoTuner(
            config=self.config,
            notifier=self._notify,
            shadow_mode=shadow_mode,
            shadow_cycles=shadow_cycles,
            cooldown_min=cooldown_min,
            max_tunes_per_day=max_tunes,
        )
        self._load_auto_tuner_state(tuner)
        return tuner

    def _compute_risk_per_position(self) -> float:
        # ── Fix: max_open_symbols로 나누지 않음 ─────────────────────────────
        # 과거에 total_risk_budget / max_open = 0.055/5 = 0.011(1.1%)로 줄여
        # auto-tune이 추가로 낮추면 0.002 수준까지 내려가 진입이 모두 차단됐음.
        # position_pct는 계좌 대비 포지션 비율로 사용 (분할 없음).
        # 동시 진입 제한은 max_open_symbols 필터가 별도로 담당.
        risk = max(self.total_risk_budget, 0.01)  # 최소 1%
        self.config.position_pct = risk
        return risk

    async def _fetch_active_positions(self) -> List[dict]:
        try:
            positions = await self.client.futures_position_information()
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, "futures_position_information")
            return []
        except Exception as exc:
            logger.warning("Failed to fetch positions for guards: %s", exc)
            return []
        return positions or []


    def _ko(self, ko_text: str, en_text: str) -> str:
        """Return ko_text or en_text based on ui_language config."""
        return ko_text if getattr(self.config, "ui_language", "ko") == "ko" else en_text

    def _init_log_masks(self):
        """B: collect API key/secret strings to redact from all log output."""
        import os
        raw = []
        for env in ("TESTNET_API_KEY", "TESTNET_API_SECRET", "BINANCE_API_KEY", "BINANCE_API_SECRET"):
            val = os.environ.get(env, "")
            if val and len(val) >= 8:
                raw.append(val)
        self._log_mask_patterns = raw

    def _mask(self, text: str) -> str:
        """B: replace any known secret substrings with ****."""
        if not self._log_mask_patterns:
            return text
        for secret in self._log_mask_patterns:
            if secret in text:
                text = text.replace(secret, "****")
        return text


    def _extract_fill_price(self, order_response: dict, fallback: float = 0.0) -> float:
        """주문 응답에서 실제 체결가 추출. avgPrice → price → fallback 순으로 시도."""
        if not order_response:
            return fallback
        for key in ("avgPrice", "averagePrice", "price", "stopPrice"):
            val = order_response.get(key)
            if val:
                try:
                    px = float(val)
                    if px > 0:
                        return px
                except (TypeError, ValueError):
                    pass
        # fills 배열에서 가중평균
        fills = order_response.get("fills", [])
        if fills:
            total_qty = sum(float(f.get("qty", 0)) for f in fills)
            if total_qty > 0:
                wavg = sum(float(f.get("price", 0)) * float(f.get("qty", 0)) for f in fills) / total_qty
                if wavg > 0:
                    return wavg
        return fallback

    def _record_api_call(self):
        """C1: call-rate tracker — prune old entries and count calls in last 60s."""
        now = time.time()
        self._api_call_times.append(now)
        cutoff = now - 60.0
        while self._api_call_times and self._api_call_times[0] < cutoff:
            self._api_call_times.popleft()
        # weight-based warn (from last error header) — fire once per crossing
        weight_pct = (self._api_weight_used / max(1, self._api_calls_per_min_limit)) * 100
        if weight_pct >= 80 and not self._api_weight_warned:
            self._api_weight_warned = True
            self._notify("WARN", self._ko(
                f"[API_WEIGHT] 사용량 {self._api_weight_used}/{self._api_calls_per_min_limit} "
                f"({weight_pct:.0f}%) — 진입 감속 권고",
                f"[API_WEIGHT] usage {self._api_weight_used}/{self._api_calls_per_min_limit} "
                f"({weight_pct:.0f}%) — entry throttling advised"
            ))
        elif weight_pct < 60:
            self._api_weight_warned = False

    def _api_rate_ok(self) -> bool:
        """C1: True if safe to make more API calls; False if call-rate limit near."""
        now = time.time()
        cutoff = now - 60.0
        while self._api_call_times and self._api_call_times[0] < cutoff:
            self._api_call_times.popleft()
        # block entries above 80% of per-minute call budget
        call_limit = int(self._api_calls_per_min_limit * 0.80)
        if len(self._api_call_times) >= call_limit:
            return False
        # C1: also block if last-known weight usage is >= 90% of limit
        weight_pct = (self._api_weight_used / max(1, self._api_calls_per_min_limit)) * 100
        if weight_pct >= 90:
            return False
        return True


    def _check_session_loss_limit(self, pnl_fast: float) -> None:
        """D: session_loss_limit_pct 초과 시 Kill Switch 자동 발동."""
        if self.kill_switch_triggered:
            return
        limit_pct = self.session_loss_limit_pct
        if limit_pct <= 0:
            return
        # 잔고 기준 누적 손실 비율 계산
        balance = getattr(self, "_last_known_balance", 0.0)
        if balance <= 0:
            # _last_known_balance 미설정 시 캐시 잔고로 fallback
            balance = float(self._balance_cache.get("available", 0.0))
        if balance <= 0:
            return
        loss_threshold = balance * (limit_pct / 100.0)
        # pnl_fast는 세션 내 빠른 PnL 지표 — 음수면 손실
        if pnl_fast <= -loss_threshold:
            now = time.time()
            cooldown_sec = self.kill_switch_cooldown_min * 60
            self.kill_switch_triggered = True
            self.kill_switch_release_ts = now + cooldown_sec
            self.kill_switch_reason = self._ko(
                f"세션 손실 한도 초과 ({pnl_fast:.4f} USDT / 한도 -{loss_threshold:.4f} USDT)",
                f"Session loss limit exceeded ({pnl_fast:.4f} USDT / limit -{loss_threshold:.4f} USDT)"
            )
            self._notify("ALERT", self._ko(
                f"[KILL_SWITCH] 세션 손실 한도 초과 → 진입 차단 {self.kill_switch_cooldown_min}분",
                f"[KILL_SWITCH] Session loss limit hit → entries blocked for {self.kill_switch_cooldown_min}m"
            ))
            logger.warning("[KILL_SWITCH] Session loss %.4f exceeded limit -%.4f USDT", pnl_fast, loss_threshold)

    def _apply_hard_caps(self):
        cfg = self.config
        lo, hi = self.HARD_CAPS["position_pct"]
        cfg.position_pct = max(0.03, lo, min(hi, float(getattr(cfg, "position_pct", lo))))  # 3% floor
        min_lo, min_hi = self.HARD_CAPS["leverage_min"]
        max_lo, max_hi = self.HARD_CAPS["leverage_max"]
        # D: non-expert mode caps leverage_max at 50x
        if not getattr(self.config, "expert_mode_enabled", False):
            max_hi = min(max_hi, self.LEVERAGE_MAX_NON_EXPERT)
        cfg.leverage_min = int(max(min_lo, min(min_hi, getattr(cfg, "leverage_min", min_lo))))
        cfg.leverage_max = int(max(cfg.leverage_min + 1, min(max_hi, getattr(cfg, "leverage_max", max_hi))))
        sl_lo, sl_hi = self.HARD_CAPS["max_loss_per_position"]
        cfg.max_loss_per_position = max(sl_lo, min(sl_hi, float(getattr(cfg, "max_loss_per_position", sl_lo))))
        w_lo, w_hi = self.HARD_CAPS["watch_limit"]
        cfg.watch_limit = int(max(w_lo, min(w_hi, getattr(cfg, "watch_limit", w_hi))))
        mo_lo, mo_hi = self.HARD_CAPS["max_open_symbols"]
        cfg.max_open_symbols = int(max(mo_lo, min(mo_hi, getattr(cfg, "max_open_symbols", mo_hi))))
        self._snapshot_rate_guard_state()

    def _snapshot_rate_guard_state(self):
        for key in self.ROC_LIMITS:
            value = getattr(self.config, key, None)
            if value is None:
                continue
            try:
                self._last_param_values[key] = float(value)
            except (TypeError, ValueError):
                continue

    def _rate_limited_value(self, key: str, new_value: float) -> float:
        limit = self.ROC_LIMITS.get(key)
        if not limit:
            return new_value
        old_value = self._last_param_values.get(key, new_value)
        delta = new_value - old_value
        if abs(delta) > limit:
            new_value = old_value + math.copysign(limit, delta)
        self._last_param_values[key] = float(new_value)
        return new_value

    def _assign_rate_limited(self, key: str, new_value: float, *, cast=float) -> float:
        limited = self._rate_limited_value(key, float(new_value))
        if cast is int:
            limited = int(round(limited))
        setattr(self.config, key, limited)
        return limited

    def _snapshot_current_params(self) -> Dict[str, float]:
        snapshot: Dict[str, float] = {}
        for key in self.TUNABLE_PARAM_KEYS:
            value = getattr(self.config, key, None)
            if value is None:
                continue
            if isinstance(value, (int, float)):
                snapshot[key] = float(value)
            else:
                snapshot[key] = value
        return snapshot

    def _capture_initial_param_snapshot(self):
        if not self._stable_param_snapshot:
            self._stable_param_snapshot = self._snapshot_current_params()

    def _restore_param_snapshot(self, snapshot: Optional[Dict[str, float]]):
        if not snapshot:
            return
        for key, value in snapshot.items():
            setattr(self.config, key, value)
            if key == "position_pct":
                self.total_risk_budget = float(value)
                self.config.total_risk_budget = float(value)
        self._apply_hard_caps()
        self._snapshot_rate_guard_state()
        self.risk_per_position = self._compute_risk_per_position()

    def _mark_pending_params(self):
        self._pending_param_snapshot = self._snapshot_current_params()
        self._pending_param_applied_ts = time.time()
        self._pending_grace_metrics = {
            "orders_start": self._flow_stats.get("order_sent", 0),
            "orders": 0,
            "pnl_fast": 0.0,
            "execution_pass_rate_min": 1.0,
            "fill_rate_min": 1.0,
        }
        self._notify("INFO", self._ko("AUTOTUNE_GRACE_START 새 파라미터 검증 시작", "AUTOTUNE_GRACE_START new parameter validation started"))

    def _pending_snapshot_active(self) -> bool:
        return bool(self._pending_param_snapshot and self._pending_param_applied_ts > 0)

    def _update_grace_metrics(self, pnl_fast: float, execution_pass_rate: float, fill_rate: float):
        if not self._pending_snapshot_active():
            return
        metrics = self._pending_grace_metrics
        total_orders = max(0, self._flow_stats.get("order_sent", 0) - metrics.get("orders_start", 0))
        metrics["orders"] = total_orders
        metrics["pnl_fast"] = pnl_fast
        metrics["execution_pass_rate_min"] = min(metrics["execution_pass_rate_min"], execution_pass_rate)
        metrics["fill_rate_min"] = min(metrics["fill_rate_min"], fill_rate)

    def _maybe_finalize_pending_params(self):
        if not self._pending_snapshot_active():
            return
        grace_sec = max(60, int(getattr(self.config, "auto_tune_grace_minutes", 10)) * 60)
        min_orders = max(1, int(getattr(self.config, "auto_tune_grace_min_orders", 3)))
        exec_threshold = float(getattr(self.config, "auto_tune_grace_execution_min", 0.2))
        fill_threshold = float(getattr(self.config, "auto_tune_grace_fill_min", 0.2))
        metrics = self._pending_grace_metrics
        elapsed = time.time() - self._pending_param_applied_ts
        if elapsed >= grace_sec and metrics["orders"] >= min_orders:
            if metrics["execution_pass_rate_min"] >= exec_threshold and metrics["fill_rate_min"] >= fill_threshold:
                self._stable_param_snapshot = self._pending_param_snapshot or self._stable_param_snapshot
                self._pending_param_snapshot = None
                self._pending_param_applied_ts = 0.0
                self._pending_grace_metrics = None
                self._notify("INFO", self._ko("AUTOTUNE_GRACE_PASS 파라미터 확정", "AUTOTUNE_GRACE_PASS parameters confirmed"))
                self._consecutive_rollbacks = 0  # E: reset on successful apply
            else:
                self._restore_param_snapshot(self._stable_param_snapshot)
                self._pending_param_snapshot = None
                self._pending_param_applied_ts = 0.0
                self._pending_grace_metrics = None
                self._notify(
                    "WARN",
                    self._ko("AUTOTUNE_GRACE_FAIL execution/fill 부족", "AUTOTUNE_GRACE_FAIL execution/fill insufficient"),
                )

    def _maybe_trigger_auto_tune_rollback(self, pnl_30m: float, order_failures: int):
        if not self._pending_snapshot_active():
            return
        # 진입이 0건이면 실행률/체결률이 0이 되는 건 당연 — 데이터 없음이므로 rollback 안 함
        metrics = self._pending_grace_metrics or {}
        if metrics.get("orders", 0) == 0:
            return
        reasons: List[str] = []
        loss_limit = abs(float(getattr(self.config, "auto_tune_rollback_loss_usdt", 0.0)))
        failure_limit = max(0, int(getattr(self.config, "auto_tune_rollback_failures", 0)))
        exec_floor = float(getattr(self.config, "auto_tune_grace_execution_min", 0.2))
        fill_floor = float(getattr(self.config, "auto_tune_grace_fill_min", 0.2))
        metrics = self._pending_grace_metrics or {}
        exec_min = metrics.get("execution_pass_rate_min", 1.0)
        fill_min = metrics.get("fill_rate_min", 1.0)
        if loss_limit > 0 and pnl_30m <= -loss_limit:
            reasons.append(self._ko(f"최근 손익 {pnl_30m:.2f} USDT", f"recent PnL {pnl_30m:.2f} USDT"))
        if failure_limit > 0 and order_failures >= failure_limit:
            reasons.append(self._ko(f"주문 실패 {order_failures}건", f"order failures {order_failures}"))
        if exec_min < exec_floor:
            reasons.append(self._ko(f"실행률 저하 {exec_min:.2f} < {exec_floor:.2f}", f"execution rate low {exec_min:.2f} < {exec_floor:.2f}"))
        if fill_min < fill_floor:
            reasons.append(self._ko(f"체결률 저하 {fill_min:.2f} < {fill_floor:.2f}", f"fill rate low {fill_min:.2f} < {fill_floor:.2f}"))
        if not reasons:
            return
        self._restore_param_snapshot(self._stable_param_snapshot)
        self._pending_param_snapshot = None
        self._pending_param_applied_ts = 0.0
        self._pending_grace_metrics = None
        self._notify("WARN", self._ko("AUTOTUNE_ROLLBACK 즉시 롤백: ", "AUTOTUNE_ROLLBACK immediate rollback: ") + ", ".join(reasons))
        # E: consecutive rollback counter → auto-disable auto-tune
        self._consecutive_rollbacks += 1
        max_rb = int(getattr(self.config, "max_consecutive_rollbacks", 5))
        if max_rb > 0 and self._consecutive_rollbacks >= max_rb and not self._auto_tune_force_disabled:
            self._auto_tune_force_disabled = True
            self.config.auto_tune_enabled = False
            self._notify("WARN", self._ko(
                f"[AUTO_TUNE_DISABLED] 연속 롤백 {self._consecutive_rollbacks}회 — Auto-tune 자동 비활성화됨",
                f"[AUTO_TUNE_DISABLED] {self._consecutive_rollbacks} consecutive rollbacks — auto-tune disabled"
            ))
            logger.warning("[AUTO_TUNE] Disabled after %d consecutive rollbacks", self._consecutive_rollbacks)

    def _set_auto_tune_mode(self, mode: str, cooldown_minutes: int = 20):
        if not self.auto_tuner:
            return
        mode_value = str(mode).lower()
        if mode_value not in getattr(self.auto_tuner, "mode_profiles", {}):
            mode_value = "balanced"
        if self.auto_tuner.current_mode == mode_value:
            return
        self.auto_tuner.current_mode = mode_value
        self.auto_tuner._apply_mode_profile()
        self.config.auto_tune_mode = mode_value
        self.auto_tuner.state.cooldown_until = time.time() + max(cooldown_minutes * 60, 60)
        self._persist_auto_tuner_state()

    async def _init_session_balance(self):
        if self.session_start_balance is not None:
            return
        try:
            account = await self.client.futures_account()
            self.session_start_balance = float(account.get("totalWalletBalance", 0.0))
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, "init_session_balance")
        except Exception as exc:
            logger.warning("Failed to initialize session balance: %s", exc)

    def _load_auto_tuner_state(self, tuner: AutoTuner):
        if not self.auto_tuner_state_path or not os.path.exists(self.auto_tuner_state_path):
            return
        try:
            with open(self.auto_tuner_state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            logger.warning("Failed to load auto tuner state: %s", exc)
            return
        lifecycle = data.get("lifecycle")
        if isinstance(lifecycle, dict):
            for stage in ("active", "staged", "proposed"):
                snapshot = lifecycle.get(stage)
                if snapshot and "params" in snapshot:
                    lifecycle[stage] = snapshot
            tuner.lifecycle.update({k: lifecycle.get(k) for k in tuner.lifecycle})
            active_snapshot = tuner.lifecycle.get("active") or {}
            active_params = active_snapshot.get("params")
            if isinstance(active_params, dict):
                loaded = dict(active_params)
                # [PATCH-15] watch_limit/max_open_symbols: auto-tune 대상 아님
                # 항상 config 기본값 이상으로 강제 (state에 잔존하는 낮은 값 무시)
                _wl_config = int(getattr(self.config, "watch_limit", 10))
                _mo_config = int(getattr(self.config, "max_open_symbols", 10))
                _wl_user = getattr(self, "_user_watch_limit", 0)
                _mo_user = getattr(self, "_user_max_open_symbols", 0)
                _wl_floor = max(_wl_config, _wl_user)
                _mo_floor = max(_mo_config, _mo_user)
                # state에 있는 값이 floor보다 낮으면 강제 교체
                loaded["watch_limit"] = max(int(loaded.get("watch_limit", _wl_floor)), _wl_floor)
                loaded["max_open_symbols"] = max(int(loaded.get("max_open_symbols", _mo_floor)), _mo_floor)
                # [PATCH-13c] 로드된 파라미터를 config 기본값 범위로 클램핑
                # Auto-Tuner state에 저장된 과거 잘못된 값이 config 변경을 무시하지 않도록
                _cfg_caps = {
                    "position_pct": (0.03, self.config.position_pct * 1.5),  # config 기본값의 150%까지만
                    "max_loss_per_position": (0.5, self.config.max_loss_per_position * 1.2),  # config 기본값의 120%까지만
                }
                for _cap_key, (_cap_lo, _cap_hi) in _cfg_caps.items():
                    if _cap_key in loaded:
                        _old_val = loaded[_cap_key]
                        loaded[_cap_key] = max(_cap_lo, min(_cap_hi, float(loaded[_cap_key])))
                        if _old_val != loaded[_cap_key]:
                            logger.info("[LOAD_CLAMP] %s: %.4f → %.4f (config cap)", _cap_key, _old_val, loaded[_cap_key])
                tuner.current = loaded
                # ── [P0-C1] config baseline 대비 state drift 감지 → 강제 리셋 ──
                # config가 변경되었으나 state에 이전 높은 값이 남아있으면 즉시 baseline으로 리셋
                _reset_thresholds = {
                    "volatility_min": 0.0010,
                    "momentum_min_long": 0.0010,
                    "momentum_min_short": 0.0010,
                    "position_pct": 0.005,
                }
                for _rk, _rt in _reset_thresholds.items():
                    _state_val = float(tuner.current.get(_rk, 0))
                    _baseline_val = float(tuner.baseline.get(_rk, _state_val))
                    if abs(_state_val - _baseline_val) > _rt:
                        logger.info("[BOOT_RESET] %s: state=%.5f → baseline=%.5f (drift=%.5f > threshold=%.4f)",
                                    _rk, _state_val, _baseline_val, abs(_state_val - _baseline_val), _rt)
                        tuner.current[_rk] = _baseline_val
        else:
            current = data.get("current") or {}
            if isinstance(current, dict):
                snapshot = tuner._make_snapshot(current, regime=data.get("hysteresis", {}).get("current_regime", ""), metrics=None, rationale="legacy_load")
                tuner.lifecycle["active"] = snapshot
                loaded_legacy = dict(snapshot["params"])
                for _k in ("watch_limit", "max_open_symbols"):
                    user_val = getattr(self.config, _k, None)
                    if user_val is not None:
                        loaded_legacy[_k] = user_val
                tuner.current = loaded_legacy
        meta = data.get("meta") or {}
        if isinstance(meta, dict):
            tuner.lifecycle_meta.update(meta)
        shadow_active = data.get("shadow_active")
        if shadow_active is not None:
            tuner.state.shadow.active = bool(shadow_active)
        cooldown_until = data.get("cooldown_until")
        if isinstance(cooldown_until, (int, float)):
            tuner.state.cooldown_until = float(cooldown_until)
        hyst = data.get("hysteresis") or {}
        tuner.state.hysteresis.current_regime = hyst.get("current_regime", tuner.state.hysteresis.current_regime)
        tuner.state.hysteresis.up_hits = hyst.get("up_hits", tuner.state.hysteresis.up_hits)
        tuner.state.hysteresis.down_hits = hyst.get("down_hits", tuner.state.hysteresis.down_hits)
        tuner.state.hysteresis.chop_hits = hyst.get("chop_hits", tuner.state.hysteresis.chop_hits)
        mode = data.get("mode")
        if mode and hasattr(tuner, "mode_profiles"):
            mode_value = str(mode).lower()
            if mode_value in tuner.mode_profiles:
                tuner.current_mode = mode_value
                tuner._apply_mode_profile()

        # ── v2 신규 필드 복원 ──
        last_apply_ts = data.get("last_apply_ts")
        if isinstance(last_apply_ts, (int, float)):
            tuner.state.last_apply_ts = float(last_apply_ts)

        regime_entered_ts = data.get("regime_entered_ts")
        if isinstance(regime_entered_ts, (int, float)):
            tuner.state.regime_entered_ts = float(regime_entered_ts)

        regime_switch_ts = data.get("regime_switch_timestamps")
        if isinstance(regime_switch_ts, list):
            tuner.state.regime_switch_timestamps = [float(t) for t in regime_switch_ts if isinstance(t, (int, float))]

        regime_locked = data.get("regime_locked_until")
        if isinstance(regime_locked, (int, float)):
            tuner.state.regime_locked_until = float(regime_locked)

        risk_streak = data.get("risk_bias_confirm_streak")
        if isinstance(risk_streak, int):
            tuner.state.risk_bias_confirm_streak = risk_streak

        shadow_deferred = data.get("shadow_lite_deferred")
        if isinstance(shadow_deferred, bool):
            tuner.state.shadow_lite_deferred = shadow_deferred

        ema_m = data.get("ema_metrics") or {}
        if isinstance(ema_m, dict):
            tuner.state.ema_tca_bps = float(ema_m.get("tca_bps", 0.0))
            tuner.state.ema_failures = float(ema_m.get("failures", 0.0))
            tuner.state.ema_fill_rate = float(ema_m.get("fill_rate", 1.0))
            tuner.state.ema_trend_score = float(ema_m.get("trend_score", 0.0))
            tuner.state.ema_noise_index = float(ema_m.get("noise_index", 0.0))
            tuner.state.ema_pass_rate = float(ema_m.get("pass_rate", 1.0))
            tuner.state.ema_pnl = float(ema_m.get("pnl", 0.0))

        targets = data.get("targets")
        if isinstance(targets, dict):
            tuner.state.targets = {k: float(v) for k, v in targets.items() if isinstance(v, (int, float))}

        best_targets = data.get("best_targets")
        if isinstance(best_targets, dict):
            tuner.state.best_targets = {k: float(v) for k, v in best_targets.items() if isinstance(v, (int, float))}

        best_score = data.get("best_targets_score")
        if isinstance(best_score, (int, float)):
            tuner.state.best_targets_score = float(best_score)

        # ── [P0-C1] targets/best_targets도 baseline drift 리셋 ──
        for _tgt_dict_name in ("targets", "best_targets"):
            _tgt_dict = getattr(tuner.state, _tgt_dict_name, {})
            if not isinstance(_tgt_dict, dict):
                continue
            for _rk, _rt in {
                "volatility_min": 0.0015,
                "momentum_min_long": 0.0015,
                "momentum_min_short": 0.0015,
            }.items():
                _tv = float(_tgt_dict.get(_rk, 0))
                _bv = float(tuner.baseline.get(_rk, _tv))
                if abs(_tv - _bv) > _rt:
                    _tgt_dict[_rk] = _bv
                    logger.info("[BOOT_RESET] %s.%s: %.5f → %.5f", _tgt_dict_name, _rk, _tv, _bv)

        # ── [P0-C1] 부팅 시 쿨다운 해제 (config 변경 후 바로 적용 가능하도록) ──
        if tuner.state.cooldown_until > 0:
            logger.info("[BOOT_RESET] cooldown cleared (was until %.0f)", tuner.state.cooldown_until)
            tuner.state.cooldown_until = 0.0

    @staticmethod
    def _sanitize_lifecycle(lifecycle: dict) -> dict:
        """[PATCH-15] lifecycle 저장 시 watch_limit/max_open_symbols 제거.
        이 값들은 auto-tune 대상이 아니며, config 기본값을 사용해야 함.
        lifecycle에 잔존하면 재시작 시 오래된 값(5, 3 등)이 복원됨."""
        _remove_keys = ("watch_limit", "max_open_symbols")
        sanitized = {}
        for stage_name, stage_data in lifecycle.items():
            if stage_data is None:
                sanitized[stage_name] = None
                continue
            stage_copy = dict(stage_data)
            if "params" in stage_copy and isinstance(stage_copy["params"], dict):
                stage_copy["params"] = {
                    k: v for k, v in stage_copy["params"].items()
                    if k not in _remove_keys
                }
            sanitized[stage_name] = stage_copy
        return sanitized

    def _persist_auto_tuner_state(self):
        if not self.auto_tuner:
            return
        payload = {
            "version": self.auto_tuner.lifecycle_meta.get("version", 1),
            "updated_at": self.auto_tuner.lifecycle_meta.get("updated_at", time.time()),
            # [PATCH-15] lifecycle 내 watch_limit/max_open_symbols 잔존값 제거
            "lifecycle": self._sanitize_lifecycle(self.auto_tuner.lifecycle),
            "meta": self.auto_tuner.lifecycle_meta,
            "shadow_active": self.auto_tuner.state.shadow.active,
            "cooldown_until": self.auto_tuner.state.cooldown_until,
            "mode": getattr(self.auto_tuner, "current_mode", getattr(self.config, "auto_tune_mode", "balanced")),
            "hysteresis": {
                "current_regime": self.auto_tuner.state.hysteresis.current_regime,
                "up_hits": self.auto_tuner.state.hysteresis.up_hits,
                "down_hits": self.auto_tuner.state.hysteresis.down_hits,
                "chop_hits": self.auto_tuner.state.hysteresis.chop_hits,
            },
            "current": {k: v for k, v in self.auto_tuner.current.items()
                         if k not in ("watch_limit", "max_open_symbols")},
            # ── v2 신규 필드 ──
            "last_apply_ts": self.auto_tuner.state.last_apply_ts,
            "regime_entered_ts": self.auto_tuner.state.regime_entered_ts,
            "regime_switch_timestamps": list(self.auto_tuner.state.regime_switch_timestamps),
            "regime_locked_until": self.auto_tuner.state.regime_locked_until,
            "risk_bias_confirm_streak": self.auto_tuner.state.risk_bias_confirm_streak,
            "shadow_lite_deferred": self.auto_tuner.state.shadow_lite_deferred,
            "ema_metrics": {
                "tca_bps": self.auto_tuner.state.ema_tca_bps,
                "failures": self.auto_tuner.state.ema_failures,
                "fill_rate": self.auto_tuner.state.ema_fill_rate,
                "trend_score": self.auto_tuner.state.ema_trend_score,
                "noise_index": self.auto_tuner.state.ema_noise_index,
                "pass_rate": self.auto_tuner.state.ema_pass_rate,
                "pnl": self.auto_tuner.state.ema_pnl,
            },
            "targets": dict(self.auto_tuner.state.targets) if self.auto_tuner.state.targets else {},
            "best_targets": dict(self.auto_tuner.state.best_targets) if self.auto_tuner.state.best_targets else {},
            "best_targets_score": self.auto_tuner.state.best_targets_score,
        }
        try:
            os.makedirs(os.path.dirname(self.auto_tuner_state_path), exist_ok=True)
            with open(self.auto_tuner_state_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("Failed to persist auto tuner state: %s", exc)

    # ------------------------------------------------------------------
    def _record_price(self, symbol: str, price: float):
        dq = self._price_history.setdefault(symbol, deque())
        now = time.time()
        dq.append((now, price))
        cutoff = now - self._returns_window_sec
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _record_notional(self, symbol: str, notional: float):
        """24h 거래대금 이력 기록. maxlen=60 (5분치 틱)."""
        dq = self._notional_history.setdefault(symbol, deque(maxlen=60))
        dq.append(notional)

    def _recent_avg_notional(self, symbol: str, window: int = 20) -> float:
        """최근 N tick 평균 거래대금. 데이터 없으면 0 반환."""
        dq = self._notional_history.get(symbol)
        if not dq:
            return 0.0
        samples = list(dq)[-window:]
        return sum(samples) / len(samples) if samples else 0.0

    def _volume_surge_score(self, symbol: str) -> float:
        """단기 거래량 서지 탐지: 최근 4 tick(~20초) vs 직전 16 tick(~80초) 비교.
        24h notional은 단기 변화가 없으므로 틱 간 변화량(delta)을 사용.
        Returns 0.0~2.0 (1.0 = 평균 수준, >1.5 = 서지)."""
        dq = self._notional_history.get(symbol)
        if not dq or len(dq) < 8:
            return 1.0  # 데이터 부족 → 중립
        samples = list(dq)
        # 틱 간 notional 변화량(delta) 계산 — 24h 누적값 차이가 실제 해당 구간 거래량
        deltas = [max(0.0, samples[i] - samples[i-1]) for i in range(1, len(samples))]
        if not deltas or max(deltas) == 0:
            return 1.0
        recent = deltas[-4:]    # 최근 4 delta (~20초)
        baseline = deltas[-20:-4] if len(deltas) >= 20 else deltas[:-4]
        if not baseline:
            return 1.0
        avg_recent = sum(recent) / len(recent)
        avg_base = sum(baseline) / len(baseline)
        if avg_base <= 0:
            return 1.0
        ratio = avg_recent / avg_base
        return min(ratio, 3.0)  # 최대 3.0 클램프

    def _recent_return_pct(self, symbol: str, window_sec: int) -> float:
        dq = self._price_history.get(symbol)
        if not dq:
            return 0.0
        now = time.time()
        latest_price = None
        reference_price = None
        for ts, price in reversed(dq):
            if latest_price is None:
                latest_price = price
            if now - ts >= window_sec:
                reference_price = price
                break
        if reference_price is None and dq:
            reference_price = dq[0][1]
        if latest_price and reference_price and reference_price > 0:
            return (latest_price - reference_price) / reference_price
        return 0.0

    def _collect_returns(self, lookback_sec: int) -> List[float]:
        now = time.time()
        returns: List[float] = []
        for dq in self._price_history.values():
            past_price = None
            recent_price = None
            for ts, price in dq:
                if ts <= now - lookback_sec:
                    past_price = price
                recent_price = price
            if past_price and recent_price and past_price > 0 and recent_price > 0:
                returns.append(math.log(recent_price / past_price))
        return returns or [0.0]

    def _compute_rv30(self) -> float:
        now = time.time()
        rv = 0.0
        for dq in self._price_history.values():
            prev_ts = None
            prev_price = None
            for ts, price in dq:
                if ts < now - self._returns_window_sec:
                    continue
                if prev_price and prev_price > 0 and price > 0:
                    rv += math.log(price / prev_price) ** 2
                prev_price = price
                prev_ts = ts
        return rv

    def _estimate_atr30(self, snapshots: List[SymbolSnapshot]) -> float:
        if not snapshots:
            return 0.0
        sample = snapshots[: max(1, len(snapshots) // 2)]
        return sum(s.volatility for s in sample) / len(sample)

    def _record_stat(self, key: str, count: int):
        dq = self._stat_window.setdefault(key, deque())
        dq.append((time.time(), count))
        self._prune_deque(dq)

    def _record_metric(self, key: str, value: float, window_sec: Optional[int] = None):
        if window_sec is None:
            window_sec = self._metrics_window_sec
        dq = self._stat_window.setdefault(key, deque())
        dq.append((time.time(), float(value)))
        self._prune_deque_custom(dq, window_sec)

    def _record_exit_event(self, symbol: str, reason: str, roi_percent: float, pnl_value: float = 0.0, trigger: str = ""):
        now = time.time()
        snapshot = self.position_snapshots.get(symbol)
        entry_ts = getattr(snapshot, "opened_at", None)
        if entry_ts:
            duration = max(0.0, now - float(entry_ts))
            self._hold_window.append((now, duration))
            self._prune_deque_custom(self._hold_window, self._metrics_window_sec)
        # 부분청산(PARTIAL_TP)은 포지션 종료가 아니므로 win/loss 집계 제외
        is_partial = str(trigger).upper().startswith("PARTIAL")
        if not is_partial:
            # 수수료 포함 USDT 손익으로 기록 (없으면 roi_percent 폴백)
            outcome = float(pnl_value) if abs(float(pnl_value)) > 1e-9 else float(roi_percent)
            self._pnl_outcomes.append((now, outcome))
            self._prune_deque_custom(self._pnl_outcomes, self._metrics_window_sec)
        key = f"exit_{reason.lower()}"
        self._record_stat(key, 1)
        # ── AI 어시스턴트 이벤트: 청산 ──
        _partial_tag = "PARTIAL" if is_partial else "FULL"
        self._ai_event("EXIT_ACTION",
                       f"EXIT_{_partial_tag} {symbol} reason={reason} roi={roi_percent:+.2f}% pnl={pnl_value:+.4f}")
        # ── 온라인 학습: 거래 완료 → 신경망 학습 ─────────────────────────
        if not is_partial:
            try:
                # pnl_value(수수료 포함 실손익)가 있으면 그것을 기준으로 승패 판단
                # roi_percent는 여전히 학습 피처로 활용
                self.neural_scorer.learn_from_outcome(symbol, roi_percent, net_pnl=pnl_value)
                _ns = self.neural_scorer.status()
                if _ns["n_trained"] % 10 == 0 and _ns["n_trained"] > 0:
                    self._ai_event("NEURAL",
                                   f"학습 {_ns['n_trained']}건 완료 acc={_ns['accuracy']}% lr={_ns.get('lr',0):.5f}")
            except Exception as _ne:
                logger.debug("NeuralScorer learn error: %s", _ne)

    def _log_trade_event(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        exit_price: float,
        pnl_value: float,
        roi_percent: float,
        trigger: str,
        leverage: float = 1.0,
        order_id: Optional[Any] = None,
        expected_mid: float | None = None,
        spread_bps: float | None = None,
        slippage_bps: float | None = None,
        fee_type: str = "taker",        # "maker" | "taker"
        fee_amount: float = 0.0,        # 실제 청산 수수료 USDT
        fee_rate: float = 0.0,          # 적용된 수수료율 (e.g. 0.0005)
    ):
        lev = max(1.0, float(leverage))
        event = {
            "ts": time.time(),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl_value,           # 실제 손익 USDT (수수료 포함, leverage 반영)
            "roi_pct": roi_percent,
            "leverage": lev,
            "trigger": trigger,
            "order_id": order_id,
            "mode": "auto",
            "env": "testnet" if self.testnet else "live",
            "expected_mid": None if expected_mid is None else float(expected_mid),
            "spread_bps": None if spread_bps is None else float(spread_bps),
            "slippage_bps": None if slippage_bps is None else float(slippage_bps),
            "fee_type": fee_type,       # "maker" | "taker"
            "fee_amount": round(float(fee_amount), 6),   # 청산 수수료 USDT
            "fee_rate": round(float(fee_rate), 6),       # 적용 수수료율
        }
        try:
            os.makedirs(os.path.dirname(self.trade_log_path), exist_ok=True)
            with open(self.trade_log_path, "a", encoding="utf-8") as fh:
                json.dump(event, fh, ensure_ascii=False)
                fh.write("\n")
        except Exception:
            logger.debug("Failed to log trade event", exc_info=True)

    def _compute_realized_pnl(
        self,
        entry_price: Optional[float],
        exit_price: Optional[float],
        position_amt: float,
        fees_model: Optional[Dict[str, Any]] = None,
    ) -> float:
        if entry_price is None or exit_price is None:
            return 0.0
        qty = abs(position_amt)
        if qty <= 0:
            return 0.0
        direction = 1.0 if position_amt > 0 else -1.0
        pnl = direction * (float(exit_price) - float(entry_price)) * qty
        fee_rate = None
        if fees_model and isinstance(fees_model, dict):
            fee_rate = fees_model.get("taker") or fees_model.get("taker_fee")
        if fee_rate is None:
            fee_rate = getattr(self.config, "taker_fee_pct", 0.0005)
        try:
            fee_rate = float(fee_rate)
        except (TypeError, ValueError):
            fee_rate = 0.0
        if fee_rate and fee_rate > 0:
            fees = (abs(entry_price) + abs(exit_price)) * qty * fee_rate
            pnl -= fees
        return pnl

    def _prune_deque_custom(self, dq: deque, window_sec: int):
        cutoff = time.time() - max(1, window_sec)
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _compute_avg_hold_seconds(self, window_sec: Optional[int] = None) -> float:
        # deque를 직접 수정하지 않고 사본으로 계산
        now = time.time()
        cutoff = (now - max(1, window_sec)) if window_sec is not None else 0.0
        durations = [d for ts, d in self._hold_window if ts >= cutoff]
        if not durations:
            return 0.0
        return sum(durations) / len(durations)

    def _compute_win_rate(self, window_sec: Optional[int] = None) -> float:
        # deque를 직접 수정하지 않고 사본으로 계산 (다른 메서드와 공유하므로)
        now = time.time()
        cutoff = (now - max(1, window_sec)) if window_sec is not None else 0.0
        outcomes = [outcome for ts, outcome in self._pnl_outcomes if ts >= cutoff]
        if not outcomes:
            return 0.0
        # roi=0.0 인 레코드는 청산 직후 포지션 데이터 소멸로 계산 불가한 케이스 → 제외
        valid = [o for o in outcomes if abs(o) > 1e-6]
        if not valid:
            return 0.0
        return sum(1 for o in valid if o > 0) / len(valid)

    def _compute_expectancy(self, window_sec: Optional[int] = None) -> float:
        # deque를 직접 수정하지 않고 사본으로 계산
        now = time.time()
        cutoff = (now - max(1, window_sec)) if window_sec is not None else 0.0
        outcomes = [outcome for ts, outcome in self._pnl_outcomes if ts >= cutoff]
        if not outcomes:
            return 0.0
        wins = [o for o in outcomes if o > 0]
        losses = [abs(o) for o in outcomes if o < 0]
        win_rate = len(wins) / len(outcomes)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        return (avg_win * win_rate) - (avg_loss * (1.0 - win_rate))

    def _init_flow_stats(self):
        self._flow_stats = {
            "evaluated_total": 0,
            "passed_signal": 0,
            "blocked_ratelimit": 0,
            "blocked_cooldown": 0,
            "blocked_spike_guard": 0,
            "blocked_portfolio_cap": 0,
            "blocked_mark_gap": 0,
            "blocked_edge": 0,    # edge-vs-cost gate / TCA spread 차단
            "blocked_busy": 0,    # symbol busy / closing-in-progress 차단
            "order_sent": 0,
            "fill_ok": 0,
        }

    def _increment_flow(self, key: str, amount: int = 1):
        if key not in self._flow_stats:
            self._flow_stats[key] = 0
        self._flow_stats[key] += amount

    def _flow_ratio(self, numerator_key: str, denominator_key: str) -> float:
        numerator = float(self._flow_stats.get(numerator_key, 0))
        denominator = float(self._flow_stats.get(denominator_key, 0))
        if denominator <= 0:
            return 0.0
        return numerator / denominator

    def _rv_ratio(self) -> float:
        short_returns = self._collect_returns(lookback_sec=self._rv_short_window)
        long_returns = self._collect_returns(lookback_sec=self._rv_long_window)
        short_vol = math.sqrt(sum(r ** 2 for r in short_returns) / max(len(short_returns), 1))
        long_vol = math.sqrt(sum(r ** 2 for r in long_returns) / max(len(long_returns), 1))
        if long_vol <= 0:
            return 1.0
        return min(5.0, max(0.1, short_vol / long_vol))

    def _leverage_cap(self) -> float:
        rv_ratio = self._rv_ratio()
        base_cap = float(getattr(self.config, "leverage_max", 10.0) or 10.0)  # [PATCH-14] 50→10 config 정렬
        if rv_ratio > 1.5:
            cap = base_cap / rv_ratio
        else:
            cap = base_cap
        return max(1.0, min(12.0, cap))  # [PATCH-14] HARD_CAPS 범위(1~12) 정렬

    def _stat_ratio(self, numerator_key: str, denominator_key: str) -> float:
        numerator = self._stat_sum(self._stat_window.get(numerator_key, deque()))
        denominator = self._stat_sum(self._stat_window.get(denominator_key, deque()))
        if denominator == 0:
            return 0.0
        return numerator / denominator

    def _stat_sum(self, dq: deque) -> float:
        self._prune_deque(dq)
        return sum(value for _, value in dq)

    def _emit_metric_snapshot(self):
        window_sec = self._metrics_window_sec
        win_rate = self._compute_win_rate(window_sec)
        expectancy = self._compute_expectancy(window_sec)
        avg_hold = self._compute_avg_hold_seconds(window_sec)
        entry_blocks = {
            "rate": self._stat_sum(self._stat_window.get("entry_blocked_ratelimit", deque())),
            "cooldown": self._stat_sum(self._stat_window.get("entry_blocked_cooldown", deque())),
            "spike": self._stat_sum(self._stat_window.get("entry_blocked_spike_guard", deque())),
            "portfolio": self._stat_sum(self._stat_window.get("entry_blocked_portfolio_cap", deque())),
            "mark": self._stat_sum(self._stat_window.get("entry_blocked_mark_gap", deque())),
            "busy": self._stat_sum(self._stat_window.get("entry_blocked_busy", deque())),
            "edge": self._stat_sum(self._stat_window.get("entry_blocked_edge", deque())),
        }
        exits = {
            "stop": self._stat_sum(self._stat_window.get("exit_stop_loss", deque())),
            "tp": self._stat_sum(self._stat_window.get("exit_take_profit", deque())),
            "trail": self._stat_sum(self._stat_window.get("exit_trailing", deque())),
            "time": self._stat_sum(self._stat_window.get("exit_time_stop", deque())),
            "decay": self._stat_sum(self._stat_window.get("exit_signal_decay", deque())),
            "spike": self._stat_sum(self._stat_window.get("exit_spike_guard", deque())),
            "busy": self._stat_sum(self._stat_window.get("exit_busy", deque())),
        }
        flow_snapshot = {
            "evaluated": float(self._flow_stats.get("evaluated_total", 0)),
            "passed": float(self._flow_stats.get("passed_signal", 0)),
            "orders": float(self._flow_stats.get("order_sent", 0)),
            "fills": float(self._flow_stats.get("fill_ok", 0)),
            "blocked_cooldown": float(self._flow_stats.get("blocked_cooldown", 0)),
            "blocked_spike_guard": float(self._flow_stats.get("blocked_spike_guard", 0)),
            "blocked_mark_gap": float(self._flow_stats.get("blocked_mark_gap", 0)),
            "blocked_portfolio_cap": float(self._flow_stats.get("blocked_portfolio_cap", 0)),
            "blocked_edge": float(self._flow_stats.get("blocked_edge", 0)),
            "blocked_busy": float(self._flow_stats.get("blocked_busy", 0)),
            "pass_rate": self._flow_ratio("passed_signal", "evaluated_total"),
            "fill_rate": self._flow_ratio("fill_ok", "order_sent"),
        }
        pnl_fast = self._current_pnl_fast()
        kill_switch_state = {
            "active": self.kill_switch_triggered,
            "reason": self.kill_switch_reason,
            "release_ts": self.kill_switch_release_ts if self.kill_switch_triggered else 0.0,
        }
        auto_mode = getattr(self.auto_tuner, "current_mode", getattr(self.config, "auto_tune_mode", "balanced")) if self.auto_tuner else getattr(self.config, "auto_tune_mode", "balanced")
        cooldown_remaining = 0.0
        shadow_active = False
        if self.auto_tuner:
            cooldown_remaining = max(0.0, getattr(self.auto_tuner.state, "cooldown_until", 0.0) - time.time())
            shadow_active = bool(getattr(self.auto_tuner.state, "shadow", None) and getattr(self.auto_tuner.state.shadow, "active", False))
        # ── v2: GUI에 풍부한 AutoTuner 상태 전달 ──
        _force_disabled = getattr(self, "_auto_tune_force_disabled", False)
        _regime = "unknown"
        _confidence = 0.0
        _ema_trend = 0.0
        _ema_noise = 0.0
        _ema_pnl = 0.0
        _ema_tca = 0.0
        _last_apply_ts = 0.0
        _tune_count_today = 0
        _risk_bias_streak = 0
        if self.auto_tuner:
            _regime = getattr(self.auto_tuner.state.hysteresis, "current_regime", "unknown")
            _confidence = getattr(self.auto_tuner.state, "confidence", 0.0)
            _ema_trend = getattr(self.auto_tuner.state, "ema_trend_score", 0.0)
            _ema_noise = getattr(self.auto_tuner.state, "ema_noise_index", 0.0)
            _ema_pnl = getattr(self.auto_tuner.state, "ema_pnl", 0.0)
            _ema_tca = getattr(self.auto_tuner.state, "ema_tca_bps", 0.0)
            _last_apply_ts = getattr(self.auto_tuner.state, "last_apply_ts", 0.0)
            _tune_count_today = getattr(self.auto_tuner.state, "tune_count_today", 0)
            _risk_bias_streak = getattr(self.auto_tuner.state, "risk_bias_confirm_streak", 0)
        auto_tune_state = {
            "enabled": bool(getattr(self.config, "auto_tune_enabled", True)) and not _force_disabled,
            "config_enabled": bool(getattr(self.config, "auto_tune_enabled", True)),
            "force_disabled": _force_disabled,
            "mode": auto_mode,
            "shadow_active": shadow_active,
            "cooldown_remaining_s": cooldown_remaining,
            "regime": _regime,
            "confidence": round(_confidence, 4),
            "ema_trend": round(_ema_trend, 4),
            "ema_noise": round(_ema_noise, 6),
            "ema_pnl": round(_ema_pnl, 6),
            "ema_tca_bps": round(_ema_tca, 2),
            "last_apply_ts": _last_apply_ts,
            "tune_count_today": _tune_count_today,
            "risk_bias_streak": _risk_bias_streak,
        }
        profit_exit_state = {
            "layer": bool(getattr(self.config, "enable_profit_exit_layer", False)),
            "partial": bool(getattr(self.config, "enable_partial_take_profit", False)),
            "trail": bool(getattr(self.config, "enable_atr_trailing_stop", False)),
            "progress": bool(getattr(self.config, "enable_progress_stop", False)),
        }
        # ── KPI + Execution Quality 요약 ──
        _kpi_summary = {}
        if self.kpi_tracker:
            _kpi_snap = self.kpi_tracker.latest()
            if _kpi_snap:
                _kpi_summary = {
                    "tca_bps": _kpi_snap.tca_bps,
                    "maker_fill_rate": _kpi_snap.maker_fill_rate,
                    "pipeline_pass_rate": _kpi_snap.pipeline_pass_rate,
                    "regime_switch_rate": _kpi_snap.regime_switch_rate,
                    "ror_proxy": _kpi_snap.ror_proxy,
                    "edge_after_fee": _kpi_snap.edge_after_fee_pct,
                }
        _eq_summary = {}
        if self.exec_quality:
            _eq_summary = self.exec_quality.global_summary()
        payload = {
            "ts": time.time(),
            "win_rate": win_rate,
            "expectancy": expectancy,
            "avg_hold": avg_hold,
            "entry_blocks": entry_blocks,
            "exits": exits,
            "flow": flow_snapshot,
            "open_positions": len(self._open_symbols),
            "pending_orders": len(self._pending_orders),
            "pending_closes": len(self._pending_closes),
            "pnl_fast": pnl_fast,
            "kill_switch": kill_switch_state,
            "auto_tune": auto_tune_state,
            "profit_exit": profit_exit_state,
            "kpi": _kpi_summary,
            "exec_quality": _eq_summary,
        }
        logger.info(
            "[METRIC] win_rate=%.2f expectancy=%.4f avg_hold=%.1fs entry_blocks=%s exits=%s",
            win_rate,
            expectancy,
            avg_hold,
            entry_blocks,
            exits,
        )
        self._append_metric_json(payload)

    def _append_metric_json(self, payload: Dict[str, Any]):
        try:
            metrics_path = getattr(self, "metrics_path", os.path.join("logs", "metrics.jsonl"))
            os.makedirs(os.path.dirname(metrics_path), exist_ok=True)
            with open(metrics_path, "a", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
                fh.write("\n")
        except Exception as exc:
            logger.debug("Failed to append metrics json: %s", exc)

    def _prune_deque(self, dq: deque):
        cutoff = time.time() - self._metrics_window_sec
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _recent_order_failures(self, include_technical: bool = False) -> int:
        cutoff = time.time() - self._metrics_window_sec
        while self._order_failures and self._order_failures[0][0] < cutoff:
            self._order_failures.popleft()
        if include_technical:
            return len(self._order_failures)
        return sum(1 for _, reason in self._order_failures if reason in self.STRATEGY_FAILURE_REASONS)

    def _prune_seen_trades(self, cutoff: float):
        while self._pnl_fast_seen_trades and self._pnl_fast_seen_trades[0][0] < cutoff:
            _, key = self._pnl_fast_seen_trades.popleft()
            self._pnl_fast_seen_keys.discard(key)

    def _prune_pnl_fast_window(self, window_sec: Optional[int] = None):
        if window_sec is None:
            window_sec = self._pnl_fast_window_sec
        cutoff = time.time() - max(60, window_sec)
        self._prune_seen_trades(cutoff)
        while self._pnl_fast_window and self._pnl_fast_window[0][0] < cutoff:
            _, value = self._pnl_fast_window.popleft()
            self._pnl_fast_sum -= value
        if abs(self._pnl_fast_sum) < 1e-9:
            self._pnl_fast_sum = 0.0

    def _record_realized_pnl(
        self,
        amount: float,
        *,
        symbol: Optional[str] = None,
        trigger: Optional[str] = None,
        timestamp: Optional[float] = None,
    ):
        if not isinstance(amount, (int, float)):
            return
        if math.isinf(amount) or math.isnan(amount) or amount == 0.0:
            return
        ts = float(timestamp or time.time())
        self._pnl_fast_window.append((ts, float(amount)))
        self._pnl_fast_sum += float(amount)
        self._prune_pnl_fast_window()
        self._pnl_fast_cache.update({
            "ts": ts,
            "value": self._pnl_fast_sum,
            "window_sec": self._pnl_fast_window_sec,
            "source": "local",
        })
        if symbol and trigger:
            self._notify("WATCH", f"PNL_FAST_EVENT {symbol} trigger={trigger} pnl={amount:.4f}")

    def _current_pnl_fast(self) -> float:
        self._prune_pnl_fast_window()
        self._pnl_fast_cache.update({
            "ts": time.time(),
            "value": self._pnl_fast_sum,
            "window_sec": self._pnl_fast_window_sec,
            "source": "local",
        })
        return self._pnl_fast_sum

    async def _get_recent_income(self) -> float:
        breakdown = await self._get_income_breakdown()
        return breakdown.get("REALIZED_PNL", 0.0)

    def _symbols_for_pnl_scan(self) -> List[str]:
        ordered: List[str] = []
        if self._open_symbols:
            ordered.extend(list(self._open_symbols))
        if self._last_cycle_symbols:
            for symbol in self._last_cycle_symbols:
                if symbol not in ordered:
                    ordered.append(symbol)
        if not ordered:
            ordered.append(getattr(self.config, "pnl_fast_fallback_symbol", "BTCUSDT"))
        max_symbols = max(1, int(getattr(self.config, "pnl_fast_symbol_limit", 8)))
        return ordered[:max_symbols]

    async def _get_fast_trade_pnl(self, window_sec: int = 1800) -> float:
        now = time.time()
        cache = self._pnl_fast_cache
        if now - cache.get("ts", 0.0) < 10 and cache.get("window_sec") == window_sec:
            return float(cache.get("value", 0.0))
        start_ms = int((now - window_sec) * 1000)
        symbols = self._symbols_for_pnl_scan()
        for symbol in symbols:
            try:
                trades = await self.client.futures_account_trades(symbol=symbol, startTime=start_ms, limit=500)
            except BinanceAPIException as exc:
                self._handle_api_exception(exc, "user_trades")
                continue
            except Exception as exc:
                logger.warning("user_trades failed for %s: %s", symbol, exc)
                continue
            for trade in trades:
                trade_time = float(trade.get("time") or trade.get("T") or 0.0) / 1000.0
                if trade_time <= 0:
                    trade_time = now
                try:
                    pnl_value = float(trade.get("realizedPnl", 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue
                trade_id = trade.get("id")
                trade_key = f"{symbol}:{trade_id}" if trade_id is not None else f"{symbol}:{trade.get('orderId')}:{trade_time}"
                self._prune_seen_trades(time.time() - self._pnl_fast_window_sec)
                if trade_key in self._pnl_fast_seen_keys:
                    continue
                self._pnl_fast_seen_keys.add(trade_key)
                self._pnl_fast_seen_trades.append((trade_time, trade_key))
                if pnl_value != 0.0 and trade_time >= (now - window_sec):
                    self._record_realized_pnl(pnl_value, symbol=symbol, trigger="TRADE", timestamp=trade_time)
        value = self._current_pnl_fast()
        self._pnl_fast_cache = {
            "ts": time.time(),
            "value": value,
            "window_sec": window_sec,
            "symbols": symbols,
        }
        return value

    async def _get_income_breakdown(self) -> Dict[str, float]:
        now = time.time()
        cached = self._income_breakdown_cache
        if now - cached.get("ts", 0.0) < 60:
            return dict(cached.get("data", {}))
        start_ms = int((now - 1800) * 1000)
        try:
            history = await self.client.futures_income_history(startTime=start_ms, limit=500)
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, "income_history")
            return {}
        components: Dict[str, float] = {
            "REALIZED_PNL": 0.0,
            "FUNDING_FEE": 0.0,
            "COMMISSION": 0.0,
            "OTHER": 0.0,
        }
        for row in history:
            try:
                value = float(row.get("income", 0.0))
            except (TypeError, ValueError):
                continue
            income_type = (row.get("incomeType") or row.get("type") or "OTHER").upper()
            if income_type not in components:
                components["OTHER"] += value
            else:
                components[income_type] += value
        self._income_breakdown_cache = {"ts": now, "data": dict(components)}
        # Preserve legacy cache for callers still using scalar pnl_30m
        self._income_cache = {"ts": now, "value": components.get("REALIZED_PNL", 0.0)}
        return dict(components)

    def _can_enter_market(self) -> bool:
        now = time.time()
        resume = max(self._entry_cooldown_until, self._rate_limit_until, self._global_spike_cooldown_until)
        if now < resume:
            return False
        if not self._api_rate_ok():  # C1: call-rate self-throttle
            return False
        # Kill Switch 쿨다운 경과 후 자동 해제
        if self.kill_switch_triggered and self.kill_switch_release_ts > 0:
            if now >= self.kill_switch_release_ts:
                self.kill_switch_triggered = False
                self.kill_switch_release_ts = 0.0
                self.kill_switch_reason = ""
                logger.info("[KILL_SWITCH] 쿨다운 경과 → 자동 해제")
                self._notify("INFO", self._ko(
                    "[KILL_SWITCH] 쿨다운 완료 → 진입 재개",
                    "[KILL_SWITCH] Cooldown expired → entries resumed"
                ))
        return not self.kill_switch_triggered

    def _entry_block_reason(self) -> Optional[str]:
        now = time.time()
        if self.kill_switch_triggered:
            remaining = max(0.0, self.kill_switch_release_ts - now)
            return self._ko(f"Kill switch 활성화 (남은 {remaining/60:.1f}분)", f"Kill switch active (remaining {remaining/60:.1f}m)")
        if now < self._global_spike_cooldown_until:
            remaining = self._global_spike_cooldown_until - now
            return self._global_spike_reason or self._ko(f"스파크 쿨다운 진행 중 ({remaining/60:.1f}분)", f"Spike cooldown active ({remaining/60:.1f}m)")
        if now < self._entry_cooldown_until:
            return self._ko(f"엔트리 쿨다운 {max(0.0, self._entry_cooldown_until - now):.0f}s", f"Entry cooldown {max(0.0, self._entry_cooldown_until - now):.0f}s")
        if now < self._rate_limit_until:
            return self._ko(f"레이트리밋 대기 {max(0.0, self._rate_limit_until - now):.0f}s", f"Rate limit wait {max(0.0, self._rate_limit_until - now):.0f}s")
        return None

    def _classify_block_reason(self, reason: Optional[str]) -> Optional[str]:
        if not reason:
            return None
        lower = reason.lower()
        if "rate" in lower or "레이트" in lower:
            return "blocked_ratelimit"
        if "cooldown" in lower or "kill switch" in lower or "쿨다운" in lower:
            return "blocked_cooldown"
        if "spike" in lower or "스파크" in lower:
            return "blocked_spike_guard"
        if "portfolio" in lower or "max_open" in lower:
            return "blocked_portfolio_cap"
        if "mark" in lower:
            return "blocked_mark_gap"
        return None

    def _record_entry_block(self, category: Optional[str]):
        if not category:
            return
        key = f"entry_{category}"
        self._record_stat(key, 1)

    def _symbol_busy(self, symbol: str) -> bool:
        return symbol in self._pending_orders or symbol in self._pending_closes

    def _estimate_entry_atr(self, snap: Optional[SymbolSnapshot], price: float) -> float:
        """ATR 추정: high/low 기반 실제 범위 우선, fallback = volatility 추정."""
        if snap and getattr(snap, "high_24h", 0.0) > getattr(snap, "low_24h", 0.0):
            true_range = snap.high_24h - snap.low_24h
            # 24h 범위의 1/4을 인트라데이 ATR 근사치로 사용
            atr = true_range / 4.0
            if atr > 0:
                return atr
        atr = max(self._last_atr_estimate, 0.0)
        if snap:
            atr = max(atr, abs(snap.volatility) * max(price, 1.0))
        if atr <= 0:
            atr = max(price * 0.001, 0.1)
        return atr

    def _current_fees_model(self) -> Dict[str, float]:
        return {
            "maker": float(getattr(self.config, "maker_fee_pct", 0.0) or 0.0),
            "taker": float(getattr(self.config, "taker_fee_pct", 0.0) or 0.0),
        }

    # ── RSI ─────────────────────────────────────────────────────────────────
    def _compute_rsi(self, symbol: str, period: int = 14) -> float:
        """_price_history 틱 데이터로 RSI 계산. 데이터 부족 시 50(중립) 반환."""
        dq = self._price_history.get(symbol)
        if not dq or len(dq) < period + 2:
            return 50.0
        prices = [px for _, px in list(dq)[-(period + 1):]]
        gains, losses = [], []
        for i in range(1, len(prices)):
            delta = prices[i] - prices[i - 1]
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))
        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 0.0
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    # ── MTF float 점수 (0.0 ~ 1.0) ──────────────────────────────────────
    def _mtf_alignment_score(self, symbol: str, direction: str) -> float:
        """각 타임프레임 EMA slope 통과 여부를 float 점수로 반환."""
        if not bool(getattr(self.config, "enable_mtf_ema_confirm", True)):
            return 1.0
        tfs = getattr(self.config, "mtf_timeframes_sec", [60, 300]) or [60, 300]
        ema_period = int(getattr(self.config, "mtf_ema_period", 21))
        min_slope = float(getattr(self.config, "mtf_min_slope_bps", 2.0))
        want_up = str(direction).upper() == "LONG"
        passed = 0
        valid = 0
        for tf in tfs:
            try:
                slope = self._mtf_ema_slope_bps(symbol, int(tf), ema_period)
            except Exception:
                slope = float("nan")
            if slope is None or (slope != slope):  # None or NaN → 데이터 부족, 스킵
                continue
            valid += 1
            if want_up and slope >= min_slope:
                passed += 1
            elif not want_up and slope <= -min_slope:
                passed += 1
        # 유효 TF 없으면 1.0 (데이터 부족 패널티 없음)
        if valid == 0:
            return 1.0
        return passed / valid

    # ── Kelly 사이징 ─────────────────────────────────────────────────────
    def _kelly_position_pct(self) -> float:
        """[PATCH-17] 3단계 Kelly: 동결→혼합→Full Quarter-Kelly + 드로다운 보호."""
        base_pct = self.config.position_pct
        if not bool(getattr(self.config, "kelly_sizing_enabled", True)):
            return base_pct

        total_trades = len(self._pnl_outcomes)
        freeze = int(getattr(self.config, "kelly_freeze_threshold", 200))
        blend = int(getattr(self.config, "kelly_blend_threshold", 500))

        # Phase 1: 고정분할 (데이터 부족 — 추정오차 방지)
        if total_trades < freeze:
            return self._apply_drawdown_guard(base_pct)

        # Kelly 계산
        dq = self._pnl_outcomes
        wins = [roi for _, roi in dq if roi > 0]
        losses = [abs(roi) for _, roi in dq if roi < 0]
        if not wins or not losses:
            return self._apply_drawdown_guard(base_pct)
        W = len(wins) / len(dq)
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        if avg_loss == 0:
            return self._apply_drawdown_guard(base_pct)
        R = avg_win / avg_loss
        kelly = W - (1.0 - W) / R
        if kelly <= 0:
            return max(0.005, base_pct * 0.3)
        fraction = float(getattr(self.config, "kelly_fraction", 0.25))
        kelly_pct = max(0.005, min(kelly * fraction, base_pct))

        # Phase 2: 혼합 (고정 70% + Kelly 30%)
        if total_trades < blend:
            blended = base_pct * 0.7 + kelly_pct * 0.3
            return self._apply_drawdown_guard(blended)

        # Phase 3: Full Quarter-Kelly
        return self._apply_drawdown_guard(kelly_pct)

    def _apply_drawdown_guard(self, pct: float) -> float:
        """[PATCH-13] 연속 손실 시 포지션 크기 점진적 축소."""
        dq = self._pnl_outcomes
        if not dq:
            return pct
        # 최근 거래에서 연속 손실 횟수 계산
        consecutive_losses = 0
        for _, roi in reversed(dq):
            if roi < 0:
                consecutive_losses += 1
            else:
                break
        # 연속 3회 이상 손실 시 점진적 축소 (3→70%, 4→50%, 5+→35%)
        if consecutive_losses >= 5:
            return max(0.005, pct * 0.35)
        elif consecutive_losses >= 4:
            return max(0.005, pct * 0.50)
        elif consecutive_losses >= 3:
            return max(0.005, pct * 0.70)
        return pct

    # ── 펀딩 레이트 캐시 ────────────────────────────────────────────────
    async def _get_funding_rate(self, symbol: str) -> float:
        """최근 펀딩 레이트 (설정 캐시 시간 기준, 기본 15분)."""
        if not bool(getattr(self.config, "funding_filter_enabled", True)):
            return 0.0
        cache_sec = int(getattr(self.config, "funding_rate_cache_sec", 900))
        cached = self._funding_cache.get(symbol, {})
        if time.time() - cached.get("ts", 0.0) < cache_sec:
            return float(cached.get("rate", 0.0))
        try:
            data = await self.client.futures_funding_rate(symbol=symbol, limit=1)
            rate = float(data[0].get("fundingRate", 0.0)) if data else 0.0
        except Exception:
            rate = 0.0
        self._funding_cache[symbol] = {"ts": time.time(), "rate": rate}
        return rate

    def _quality_score(self, snap: SymbolSnapshot, rv_ratio: float) -> float:
        mark_gap = self._mark_gap_ratio(snap)
        gap_cap = float(getattr(self.config, "quality_mark_gap_cap", 0.01) or 0.01)
        rv_cap = float(getattr(self.config, "quality_rv_cap", 3.0) or 3.0)
        gap_weight = float(getattr(self.config, "quality_mark_gap_weight", 0.5) or 0.0)
        rv_weight = float(getattr(self.config, "quality_rv_weight", 0.3) or 0.0)
        mom_weight = float(getattr(self.config, "quality_momentum_weight", 0.2) or 0.0)
        base = snap.volatility / max(self.config.volatility_min, 1e-9)
        gap_penalty = min(mark_gap, gap_cap) * gap_weight
        rv_penalty = min(rv_ratio, rv_cap) * rv_weight
        momentum_score = abs(snap.momentum_pct) * mom_weight
        return base + momentum_score - gap_penalty - rv_penalty

    def _expected_edge_pct(self, decision: SignalDecision, snap: SymbolSnapshot) -> float:
        price = snap.price or snap.mark_price or 0.0
        atr_value = self._estimate_entry_atr(snap, price)
        atr_ratio = atr_value / max(price, 1e-9) if price > 0 else 0.0
        momentum = abs(snap.momentum_pct)
        strength = max(getattr(decision, "strength", 1.0), 0.1)
        return (momentum * 0.6 + atr_ratio * 0.4) * min(strength, 5.0)

    def _total_cost_pct(self, snap: SymbolSnapshot) -> float:
        taker = float(getattr(self.config, "taker_fee_pct", 0.0) or 0.0)
        maker = float(getattr(self.config, "maker_fee_pct", 0.0) or 0.0)
        mark_gap = self._mark_gap_ratio(snap)
        rv_penalty = max(self._rv_ratio() - 1.0, 0.0) * 0.05
        # Execution cost proxy (TCA): spread + slippage estimates.
        # We don't always have L1 bid/ask here, so we use conservative bps proxies.
        spread_bps = float(getattr(self.config, "tca_spread_estimate_bps", 0.0) or 0.0)
        slip_bps = float(getattr(self.config, "tca_slippage_estimate_bps", 0.0) or 0.0)
        tca = (spread_bps + slip_bps) / 10000.0

        # [PATCH-17] 진입=maker(PATCH-17 활성화), 청산=maker → 비용 현실화
        # TCA는 편도 1회만 적용 (기존 ×2는 과도하게 보수적이었음)
        fee_cost = taker + maker
        return fee_cost + tca + mark_gap + rv_penalty

    def _edge_covers_cost(self, decision: SignalDecision, snap: SymbolSnapshot) -> bool:
        min_edge = float(getattr(self.config, "min_edge_over_fee_pct", 0.0) or 0.0)
        expected_move = self._expected_edge_pct(decision, snap)
        cost = self._total_cost_pct(snap)
        return (expected_move - cost) >= min_edge

    # ── [PATCH-17] 방향 집중도 체크 ─────────────────────────────────────
    def _check_direction_concentration(self, symbol: str, direction: str) -> bool:
        """같은 방향 동시진입 집중도 체크 (메이저 상관성 + 전체 동방향 제한)."""
        major_cap = int(getattr(self.config, "same_direction_major_cap", 2))
        total_cap = int(getattr(self.config, "max_same_direction_total", 6))
        if major_cap <= 0 and total_cap <= 0:
            return True  # 모두 0이면 비활성화

        same_dir_count = 0
        same_dir_major = 0
        for sym, snap in self.position_snapshots.items():
            if getattr(snap, "side", "").upper() == direction.upper():
                same_dir_count += 1
                if sym in self.MAJOR_SYMBOLS:
                    same_dir_major += 1

        # 메이저 심볼 동방향 제한
        if major_cap > 0 and symbol in self.MAJOR_SYMBOLS and same_dir_major >= major_cap:
            return False
        # 전체 동방향 제한
        if total_cap > 0 and same_dir_count >= total_cap:
            return False
        return True

    def _compute_stop_loss_price(self, entry_price: float, direction: str, atr: float) -> float:
        # [PATCH-17] 레짐별 SL 멀티플라이어: chop=1.4, trend=2.0 (크립토 노이즈 대응)
        _base_mult = float(getattr(self.config, "sl_atr_mult", 2.0))
        # auto_tune 활성 시 auto_tuner 레짐, 비활성 시 캐시된 레짐 사용
        if self.auto_tuner and bool(getattr(self.config, "auto_tune_enabled", True)):
            _regime = getattr(self.auto_tuner.state.hysteresis, "current_regime", "chop")
        else:
            _regime = getattr(self, "_last_known_regime", "chop")
        if _regime in ("trend_up", "trend_down"):
            mult = float(getattr(self.config, "sl_atr_mult_trend", _base_mult))
        else:
            mult = float(getattr(self.config, "sl_atr_mult_chop", _base_mult))
        offset = max(atr, entry_price * 0.001) * max(mult, 0.1)
        if direction.upper() == "LONG":
            return max(entry_price - offset, 0.0001)
        return entry_price + offset

    def _compute_take_profit_targets(self, entry_price: float, stop_price: float, direction: str) -> List[float]:
        if stop_price is None or stop_price <= 0:
            return []
        tp1 = float(getattr(self.config, "tp_r_multiple_1", 0.0))
        tp2 = float(getattr(self.config, "tp_r_multiple_2", 0.0))
        multiples = [tp for tp in (tp1, tp2) if tp and tp > 0]
        if not multiples:
            return []
        raw_levels = compute_take_profit_levels(entry_price, stop_price, multiples, direction.upper())
        min_roi_pct = max(float(getattr(self.config, "tp_min_roi_pct", 0.0)), 0.0)
        filtered = []
        for level in raw_levels:
            if direction.upper() == "LONG":
                roi = (level - entry_price) / entry_price if entry_price > 0 else 0.0
            else:
                roi = (entry_price - level) / entry_price if entry_price > 0 else 0.0
            if roi >= min_roi_pct:
                filtered.append(level)
        return filtered

    def _initialize_snapshot_targets(self, snapshot: PositionSnapshot) -> PositionSnapshot:
        direction = snapshot.side
        stop_px = snapshot.stop_loss_px
        if not stop_px:
            stop_px = self._compute_stop_loss_price(snapshot.entry_price, direction, snapshot.atr_at_entry)
        snapshot = update_snapshot(snapshot, stop_loss_px=stop_px)
        if getattr(self.config, "enable_take_profit", False):
            tp_levels = snapshot.take_profit_levels or self._compute_take_profit_targets(snapshot.entry_price, stop_px, direction)
            snapshot = update_snapshot(snapshot, take_profit_levels=tp_levels)
        return snapshot

    def _queue_snapshot_seed(
        self,
        symbol: str,
        decision: SignalDecision,
        snap: SymbolSnapshot,
        quantity: float,
        leverage: float,
    ):
        atr_est = self._estimate_entry_atr(snap, snap.price)
        # Always seed a stop-loss price (even if TP layer is off). This is required for
        # coherent risk sizing, stop-loss enforcement, and ROI accounting.
        stop_loss_px = self._compute_stop_loss_price(snap.price, decision.direction, atr_est)
        take_profit_levels: List[float] = []
        if getattr(self.config, "enable_take_profit", False):
            take_profit_levels = self._compute_take_profit_targets(snap.price, stop_loss_px, decision.direction)
        self._snapshot_seeds[symbol] = {
            "decision": decision,
            "entry_price": snap.price,
            "side": decision.direction,
            "qty": quantity,
            "leverage": leverage,
            "atr": atr_est,
            "momentum": snap.momentum_pct,
            "stop_loss_px": stop_loss_px,
            "take_profit_levels": take_profit_levels,
        }

    def _build_snapshot_from_seed(self, symbol: str, seed: Dict[str, Any]) -> PositionSnapshot:
        params = self._snapshot_current_params()
        fees_model = self._current_fees_model()
        snapshot = build_snapshot(
            symbol=symbol,
            params=params,
            entry_price=seed.get("entry_price", 0.0),
            side=seed.get("side", "LONG"),
            quantity=seed.get("qty", 0.0),
            leverage=seed.get("leverage", 1.0),
            atr_at_entry=seed.get("atr", 0.0),
            momentum_at_entry=seed.get("momentum", 0.0),
            decision=seed.get("decision"),
            fees_model=fees_model,
            stop_loss_px=seed.get("stop_loss_px"),
            take_profit_levels=seed.get("take_profit_levels"),
        )
        return self._initialize_snapshot_targets(snapshot)

    def _ensure_snapshot_for_position(self, pos: dict):
        symbol = pos.get("symbol")
        if not symbol or symbol in self.position_snapshots:
            return
        try:
            entry_price = float(pos.get("entryPrice", 0.0))
            position_amt = float(pos.get("positionAmt", 0.0))
            leverage = float(pos.get("leverage", 1.0) or 1.0)
        except (TypeError, ValueError):
            return
        if position_amt == 0 or entry_price <= 0:
            return
        side = "LONG" if position_amt > 0 else "SHORT"
        qty = abs(position_amt)
        atr_guess = max(entry_price * 0.01, 0.1)
        stop_loss_px = self._compute_stop_loss_price(entry_price, side, atr_guess)
        tp_levels: List[float] = []
        if getattr(self.config, "enable_take_profit", False):
            tp_levels = self._compute_take_profit_targets(entry_price, stop_loss_px, side)
        seed = {
            "entry_price": entry_price,
            "side": side,
            "qty": qty,
            "leverage": leverage,
            "atr": atr_guess,
            "momentum": 0.0,
            "decision": None,
            "stop_loss_px": stop_loss_px,
            "take_profit_levels": tp_levels,
        }
        snapshot = self._build_snapshot_from_seed(symbol, seed)
        snapshot = update_snapshot(snapshot, opened_at=time.time())
        self.position_snapshots[symbol] = snapshot
        self._snapshot_seeds.pop(symbol, None)

    def _classify_order_failure(self, exc: Exception, message: str = "") -> str:
        reason = "UNKNOWN"
        msg = (message or "").lower()
        code = None
        if isinstance(exc, BinanceAPIException):
            code = getattr(exc, "code", None)
            if code in (-1003, -1015) or "rate limit" in msg or "too many" in msg:
                return "RATE_LIMIT"
            if code in (-2010, -2019) and ("insufficient" in msg or "not enough" in msg):
                return "INSUFFICIENT_BALANCE"
            if code in (-4003, -4110) or "precision" in msg:
                return "PRECISION"
            if code in (-4164, -4165) or "min notional" in msg:
                return "MIN_NOTIONAL"
            if code in (-4047, -4082) or "trading is not allowed" in msg or "no such symbol" in msg:
                return "SYMBOL_CLOSED"
            if code == -4411 or "tradfi" in msg or "agreement" in msg:
                return "TRADFI_AGREEMENT"
            if code in (-2011, -2021) and "reduceonly" in msg:
                return "STRATEGY_REJECT"
            if code in (-4061, -4062) or "position not adequate" in msg or "insufficient margin" in msg:
                return "MARGIN_INSUFFICIENT"
        if "insufficient" in msg or "margin" in msg:
            reason = "MARGIN_INSUFFICIENT"
        elif "rate limit" in msg:
            reason = "RATE_LIMIT"
        elif "min notional" in msg:
            reason = "MIN_NOTIONAL"
        elif "precision" in msg:
            reason = "PRECISION"
        elif "symbol" in msg or "trading" in msg:
            reason = "SYMBOL_CLOSED"
        elif "reject" in msg or "risk" in msg:
            reason = "STRATEGY_REJECT"
        return reason

    def _handle_api_exception(self, exc: BinanceAPIException, context: str):
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        headers = getattr(exc, "headers", {}) or {}
        # C1: parse X-MBX-USED-WEIGHT-1M from error response headers
        for hk in ("x-mbx-used-weight-1m", "X-MBX-USED-WEIGHT-1M", "x-mbx-used-weight"):
            raw_w = headers.get(hk)
            if raw_w is not None:
                try:
                    self._api_weight_used = int(raw_w)
                except (ValueError, TypeError):
                    pass
                break
        if code == -4411:
            # TradFi-Perps agreement not signed → permanently block symbol
            _sym = context.split()[-1] if context else ""
            # Extract symbol from context string if present
            for _part in (context or "").replace(":", " ").split():
                if _part.endswith("USDT") or _part.endswith("BUSD"):
                    _sym = _part
                    break
            if _sym:
                self._symbol_blocked.add(_sym)
                if not hasattr(self, "_tradfi_blocked"):
                    self._tradfi_blocked: set = set()
                self._tradfi_blocked.add(_sym)
            self._notify("WARN", self._ko(
                f"TradFi 계약 미서명: {_sym or context} — 바이낸스에서 TradFi-Perps 계약에 서명해야 거래 가능합니다. 해당 심볼을 자동 제외합니다.",
                f"TradFi agreement not signed: {_sym or context} — Sign TradFi-Perps contract on Binance to trade this symbol. Auto-blocked.",
            ))
            logger.warning("[TRADFI_BLOCK] %s blocked — agreement not signed (code=-4411)", _sym or context)
        elif code == -1021:
            # Timestamp ahead/behind server → re-sync
            asyncio.ensure_future(self._resync_server_time())
            self._notify("WARN", f"Timestamp error({context}), re-syncing server time")
        elif status == 429:
            retry_after = float(headers.get("Retry-After", 60))
            self._rate_limit_until = time.time() + retry_after
            self._entry_cooldown_until = max(self._entry_cooldown_until, self._rate_limit_until)
            self._notify("WARN", f"Rate limit({context}) backoff {retry_after:.0f}s")
        elif status == 418:
            self._entry_cooldown_until = time.time() + 30 * 60
            self._rate_limit_until = self._entry_cooldown_until
            self._notify("WARN", f"Binance 418 ban on {context}, pausing 30m")
        else:
            self._notify("WARN", f"API error {status} on {context}: {exc}")

    async def _resync_server_time(self):
        """Re-sync timestamp_offset with Binance server."""
        try:
            server_time = await self.client.futures_time()
            server_ts = int(server_time["serverTime"])
            local_ts = int(time.time() * 1000)
            self.client.timestamp_offset = server_ts - local_ts
            logger.info(f"[TIME_SYNC] re-synced offset={self.client.timestamp_offset}ms")
            self._notify("INFO", f"Time re-synced: offset={self.client.timestamp_offset}ms")
        except Exception as e:
            logger.warning(f"[TIME_SYNC] re-sync failed: {e}")

    def _check_failure_circuit(self):
        cutoff = time.time() - 600
        while self._order_failures and self._order_failures[0][0] < cutoff:
            self._order_failures.popleft()
        if len(self._order_failures) >= 3:
            self._entry_cooldown_until = time.time() + 600
            self._notify("WARN", "Order failure circuit triggered: pausing entries 10m")

    async def _run_auto_tuner_cycle(self, snapshots: List[SymbolSnapshot]):
        if not self.auto_tuner or not getattr(self.config, "auto_tune_enabled", True):  # [v2] 기본값 True
            return
        returns = self._collect_returns(lookback_sec=300)
        rv30 = self._compute_rv30()
        atr30 = self._estimate_atr30(snapshots)
        self._last_atr_estimate = atr30
        pass_rate = self._stat_ratio("passed", "evaluated")
        entry_rate = self._stat_ratio("orders", "passed")
        fill_rate = self._stat_ratio("fills", "orders")
        signal_pass_rate = self._flow_ratio("passed_signal", "evaluated_total")
        execution_pass_rate = self._flow_ratio("order_sent", "passed_signal")
        pure_fill_rate = self._flow_ratio("fill_ok", "order_sent")
        blocked_ratelimit = self._flow_ratio("blocked_ratelimit", "evaluated_total")
        blocked_cooldown = self._flow_ratio("blocked_cooldown", "evaluated_total")
        blocked_spike_guard = self._flow_ratio("blocked_spike_guard", "evaluated_total")
        blocked_portfolio_cap = self._flow_ratio("blocked_portfolio_cap", "evaluated_total")
        order_failures = self._recent_order_failures()
        pnl_fast = await self._get_fast_trade_pnl()
        income_breakdown = await self._get_income_breakdown()
        pnl_slow_realized = income_breakdown.get("REALIZED_PNL", 0.0)
        pnl_slow_funding = income_breakdown.get("FUNDING_FEE", 0.0)
        pnl_slow_fee = income_breakdown.get("COMMISSION", 0.0)
        pnl_slow_other = income_breakdown.get("OTHER", 0.0)
        self._update_grace_metrics(
            pnl_fast=pnl_fast,
            execution_pass_rate=execution_pass_rate,
            fill_rate=pure_fill_rate,
        )
        spread_stats = self._compute_spread_stats(snapshots)
        tca_stats = self._compute_tca_metrics(int(getattr(self.config, 'tca_window_sec', 1800) or 1800))
        tuner_kwargs = {
            "returns": returns,
            "rv30": rv30,
            "atr30": atr30,
            "pass_rate": pass_rate,
            "entry_rate": entry_rate,
            "fill_rate": fill_rate,
            "signal_pass_rate": signal_pass_rate,
            "execution_pass_rate": execution_pass_rate,
            "pure_fill_rate": pure_fill_rate,
            "blocked_ratelimit": blocked_ratelimit,
            "blocked_cooldown": blocked_cooldown,
            "blocked_spike_guard": blocked_spike_guard,
            "blocked_portfolio_cap": blocked_portfolio_cap,
            "pnl_30m": pnl_slow_realized,
            "order_failures": order_failures,
            "pnl_fast": pnl_fast,
            "pnl_slow_realized": pnl_slow_realized,
            "pnl_slow_funding": pnl_slow_funding,
            "pnl_slow_fee": pnl_slow_fee,
            "pnl_slow_other": pnl_slow_other,
            "spread_bps_med": spread_stats.get("spread_bps_med", 0.0),
            "spread_bps_p90": spread_stats.get("spread_bps_p90", 0.0),
            "slippage_bps_med": tca_stats.get("slippage_bps_med", 0.0),
            "slippage_bps_p90": tca_stats.get("slippage_bps_p90", 0.0),
            "tca_spread_bps_med": tca_stats.get("tca_spread_bps_med", 0.0),
            "tca_spread_bps_p90": tca_stats.get("tca_spread_bps_p90", 0.0),
            "tca_samples": tca_stats.get("tca_samples", 0.0),
        }
        try:
            params = self.auto_tuner.run_cycle(**tuner_kwargs)
        except TypeError:
            logger.debug("AutoTuner.run_cycle legacy signature detected; retrying without extended metrics")
            legacy_kwargs = {k: tuner_kwargs[k] for k in (
                "returns",
                "rv30",
                "atr30",
                "pass_rate",
                "entry_rate",
                "fill_rate",
                "signal_pass_rate",
                "execution_pass_rate",
                "pure_fill_rate",
                "blocked_ratelimit",
                "blocked_cooldown",
                "blocked_spike_guard",
                "blocked_portfolio_cap",
                "pnl_30m",
                "order_failures",
            )}
            params = self.auto_tuner.run_cycle(**legacy_kwargs)
        self._apply_auto_tune_params(params)
        self._check_session_loss_limit(pnl_fast)  # D: session loss kill switch
        self._maybe_trigger_auto_tune_rollback(pnl_slow_realized, order_failures)
        self._maybe_finalize_pending_params()
        self._persist_auto_tuner_state()
        # ── EQ: 심볼별 maker 파라미터 자동 미세조정 (5분마다) ──
        if self.exec_quality:
            try:
                self.exec_quality.auto_adjust_all()
            except Exception as _eq_err:
                logger.debug("EQ auto_adjust error: %s", _eq_err)

    def _apply_auto_tune_params(self, params: Dict[str, float]):
        if not params:
            return
        before_snapshot = self._snapshot_current_params()
        for key in ("momentum_min_long", "momentum_min_short", "volatility_min"):
            if key in params:
                val = float(params[key])
                if key == "momentum_min_short":
                    val = min(val, -0.001)   # 하한 -0.001 (너무 좁은 short 차단)
                    val = max(val, -0.01)    # 상한 -0.01 (너무 가파른 요구 차단)
                if key == "momentum_min_long":
                    val = max(val, 0.001)    # 하한 0.001 (0 이하 방지)
                    val = min(val, 0.006)    # ← 상한 0.006 추가: 0.007 이상은 모든 심볼 차단
                setattr(self.config, key, val)
        # watch_limit / max_open_symbols는 auto-tune 적용 무시 → 사용자 설정값 유지
        # (심볼 수 감소는 진입 기회만 줄이고 리스크 감소 효과 없음)
        if "position_pct" in params:
            raw_pct = max(0.03, min(0.08, float(params["position_pct"])))  # [PATCH-13c] 상한 8%: config 기본 6%
            position_pct = self._assign_rate_limited("position_pct", raw_pct)
            self.total_risk_budget = position_pct
            self.config.total_risk_budget = position_pct
        if "leverage_min" in params:
            # [PATCH-8] 상한 10x: auto-tune이 레버리지를 과도하게 올리지 못하게
            _lev_min_safe = max(1.0, min(10.0, float(params["leverage_min"])))
            self._assign_rate_limited("leverage_min", _lev_min_safe)
        if "leverage_max" in params:
            # [PATCH-8] 상한 10x: 분석 결과 1x에서만 안정 수익, 5x+ 에서 손실
            _lev_max_safe = max(1.0, min(10.0, float(params["leverage_max"])))
            self._assign_rate_limited("leverage_max", _lev_max_safe)
        if self.config.leverage_min > self.config.leverage_max - 1:
            self.config.leverage_min = max(1.0, self.config.leverage_max - 1.0)
        if "max_loss_per_position" in params:
            # [PATCH-8] 손절 상한 5.0→2.5%: auto-tuner가 손절폭을 넓히지 못하게
            raw_sl = max(0.5, min(2.2, float(params["max_loss_per_position"])))  # [PATCH-14] 2.5→2.2 HARD_CAPS 정렬
            self._assign_rate_limited("max_loss_per_position", raw_sl)
        # [PATCH-15] watch_limit/max_open_symbols: auto-tune 대상 아님, 항상 config 이상 유지
        _wl_floor = max(int(getattr(self.config, "watch_limit", 10)), getattr(self, "_user_watch_limit", 0))
        _mo_floor = max(int(getattr(self.config, "max_open_symbols", 10)), getattr(self, "_user_max_open_symbols", 0))
        self.config.watch_limit = max(_wl_floor, self.config.watch_limit)
        self.config.max_open_symbols = max(_mo_floor, self.config.max_open_symbols)
        # [PATCH-14] HARD_CAPS 참조로 변경 (하드코딩 제거)
        _hc_lev_min = self.HARD_CAPS.get("leverage_min", (1, 5))
        _hc_lev_max = self.HARD_CAPS.get("leverage_max", (2, 12))
        self.config.leverage_min = max(float(_hc_lev_min[0]), min(self.config.leverage_min, float(_hc_lev_min[1])))
        self.config.leverage_max = max(self.config.leverage_min + 1.0, min(self.config.leverage_max, float(_hc_lev_max[1])))
        if "auto_tune_mode" in params:
            self.config.auto_tune_mode = str(params.get("auto_tune_mode", self.config.auto_tune_mode))
        self._apply_hard_caps()
        self.config.momentum_min = self.config.momentum_min_long
        self.risk_per_position = self._compute_risk_per_position()
        after_snapshot = self._snapshot_current_params()
        if before_snapshot != after_snapshot:
            self._mark_pending_params()
            win = self._compute_win_rate(self._metrics_window_sec)
            exp = self._compute_expectancy(self._metrics_window_sec)
            self._notify(
                "INFO",
                f"Auto-Tune param staging | win_rate={win:.2f} expectancy={exp:.4f}"
            )

    async def _ensure_exchange_info(self):
        if not self._exchange_info:
            try:
                self._exchange_info = await self.client.futures_exchange_info()
            except BinanceAPIException as exc:
                self._handle_api_exception(exc, "futures_exchange_info")
                raise
        return self._exchange_info

    async def _filters_for_symbol(self, symbol: str):
        info = await self._ensure_exchange_info()
        entry = next((s for s in info.get("symbols", []) if s.get("symbol") == symbol), None)
        return entry.get("filters", []) if entry else []

    def _compute_target_leverage(self, strength_ratio: float,
                                  neural_prob: float = 0.5,
                                  volatility: float = 0.0) -> int:
        """동적 레버리지 계산 — 신호강도 + 신경망 신뢰도 + 변동성 반영.

        strength_ratio: 0.0~1.0 (composite signal strength / 5.0)
        neural_prob:    0.0~1.0 (v3 neural scorer 승률 예측, 없으면 0.5)
        volatility:     최근 변동성 (높으면 레버리지 낮춤)
        """
        ratio = max(0.0, min(1.0, float(strength_ratio)))
        min_lev = max(1.0, float(getattr(self.config, "leverage_min", 1.0) or 1.0))
        max_lev = max(min_lev + 1.0, float(getattr(self.config, "leverage_max", min_lev + 1.0) or (min_lev + 1.0)))

        # ── 1) 신경망 신뢰도 반영: 승률 50%+ → 배율 증가, 50%- → 감소 ──
        neural_mult = 1.0
        if neural_prob > 0.0:
            # prob=0.25 → mult=0.5, prob=0.5 → mult=1.0, prob=0.75 → mult=1.5
            neural_mult = max(0.3, min(1.5, neural_prob * 2.0))

        # ── 2) 변동성 반영: 고변동성 → 레버리지 감소 ──
        vol_mult = 1.0
        if volatility > 0:
            # vol=0.01(1%) → mult=1.0, vol=0.03(3%) → mult=0.7, vol=0.05(5%+) → mult=0.5
            vol_mult = max(0.3, min(1.0, 1.0 - (volatility - 0.01) * 10.0))

        # ── 3) 종합 레버리지 = base * neural * volatility ──
        base_target = min_lev + (max_lev - min_lev) * ratio
        target = base_target * neural_mult * vol_mult

        # 범위 제한
        cap = self._leverage_cap()
        target = min(target, cap)
        target = max(min_lev, min(max_lev, target))
        hard_max = min(12, int(getattr(self.config, "leverage_max", 10)))  # [PATCH-14] 150→12 HARD_CAPS 정렬
        result = max(1, min(hard_max, int(round(target))))
        logger.info("Leverage calc: ratio=%.2f neural=%.2f vol=%.3f → base=%.1f mult=%.2f → %dx",
                     ratio, neural_prob, volatility, base_target, neural_mult * vol_mult, result)
        return result

    async def _ensure_symbol_leverage(self, symbol: str, leverage: int) -> bool:
        """레버리지 설정. 성공 시 True, 실패 시 False 반환."""
        leverage = max(1, min(12, int(leverage)))  # [PATCH-14] 125→12 HARD_CAPS 정렬
        if leverage <= 0:
            return False
        min_allowed, max_allowed, step = await self._symbol_leverage_limits(symbol)
        leverage = max(min_allowed, min(max_allowed, leverage))
        if step > 1:
            leverage = int(max(min_allowed, min(max_allowed, math.floor(leverage / step) * step)))
        if self._symbol_leverage.get(symbol) == leverage:
            return True
        try:
            await self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            self._symbol_leverage[symbol] = leverage
            logger.info("Adjusted leverage for %s to %dx", symbol, leverage)
            return True
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, "change_leverage")
            logger.warning(
                "Failed to set leverage for %s to %dx (allowed %d-%dx): %s",
                symbol,
                leverage,
                min_allowed,
                max_allowed,
                exc,
            )
            return False
        except Exception as exc:
            logger.warning("Failed to set leverage for %s to %dx: %s", symbol, leverage, exc)
            return False

    async def _compute_minimum_quantity(self, symbol: str, price: float, desired_notional: Optional[float] = None) -> float:
        filters = await self._filters_for_symbol(symbol)
        return compliant_quantity(price if price > 0 else 1.0, filters, desired_notional)

    async def _symbol_min_notional(self, symbol: str) -> float:
        filters = await self._filters_for_symbol(symbol)
        return min_notional_from_filters(filters)

    async def _symbol_leverage_limits(self, symbol: str) -> Tuple[int, int, float]:
        cached = self._leverage_limits_cache.get(symbol)
        if cached:
            return cached
        filters = await self._filters_for_symbol(symbol)
        min_lev = 1.0
        max_lev = 125.0
        step = 1.0
        for filt in filters:
            if filt.get("filterType") == "LEVERAGE":
                try:
                    min_lev = float(filt.get("minLeverage", min_lev))
                    max_lev = float(filt.get("maxLeverage", max_lev))
                    step = float(filt.get("leverageStep", step) or step)
                except (TypeError, ValueError):
                    pass
                break
        limits = (int(max(1, min_lev)), int(max(1, max_lev)), max(step, 1.0))
        self._leverage_limits_cache[symbol] = limits
        return limits

    async def _get_margin_summary(self, target_symbol: Optional[str] = None):
        try:
            positions = await self.client.futures_position_information()
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, "margin_summary")
            return 0.0, 0.0, 0.0, 0.0
        except Exception as exc:
            logger.warning("Failed to fetch margin summary: %s", exc)
            return 0.0, 0.0, 0.0, 0.0
        total_margin = 0.0
        total_notional = 0.0
        symbol_margin = 0.0
        symbol_notional = 0.0
        for pos in positions:
            try:
                amt = abs(float(pos.get("positionAmt", 0.0)))
                if amt == 0:
                    continue
                mark_price = float(pos.get("markPrice", 0.0))
                leverage = float(pos.get("leverage", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            notional = max(amt * mark_price, 0.0)
            margin = max(notional / leverage if leverage > 0 else notional, 0.0)
            total_notional += notional
            total_margin += margin
            if target_symbol and pos.get("symbol") == target_symbol:
                symbol_notional = notional
                symbol_margin = margin
        return total_margin, total_notional, symbol_margin, symbol_notional

    def _position_roi_percent(self, position: dict) -> float:
        try:
            unrealized = float(position.get("unRealizedProfit", 0.0))
            amt = abs(float(position.get("positionAmt", 0.0)))
            entry_price = float(position.get("entryPrice", 0.0))
            initial_margin = float(position.get("positionInitialMargin", 0.0))
            leverage = float(position.get("leverage", 0.0) or 0.0)
            mark_price = float(position.get("markPrice", entry_price) or entry_price)
        except (TypeError, ValueError):
            return 0.0
        if initial_margin <= 0:
            notional = amt * entry_price
            if notional <= 0:
                return 0.0
            if leverage > 0:
                initial_margin = notional / leverage
            else:
                initial_margin = notional
        if initial_margin <= 0:
            return 0.0
        taker_fee_pct = max(0.0, float(getattr(self.config, "taker_fee_pct", 0.0) or 0.0))
        maker_fee_pct = max(0.0, float(getattr(self.config, "maker_fee_pct", 0.0) or 0.0))
        # 진입 수수료 (진입가 기준, taker 또는 maker)
        entry_fee = 0.0
        if entry_price > 0 and amt > 0:
            entry_fee = amt * entry_price * taker_fee_pct   # 진입은 보통 taker
        # 청산 수수료 (현재가 기준, taker 기준으로 보수적 계산)
        close_fee = 0.0
        if taker_fee_pct > 0 and amt > 0 and mark_price > 0:
            close_fee = amt * mark_price * taker_fee_pct
        # [PATCH-8] 실제 수익 = unrealized - 진입수수료 - 청산수수료
        # Binance unrealizedProfit은 순수 가격차이(수수료 미포함).
        # 진입수수료는 Binance가 잔고에서 별도 차감했으므로 PnL에도 반영해야
        # ROI가 실제 잔고 변화를 정확히 반영함.
        adjusted_unrealized = unrealized - entry_fee - close_fee
        return (adjusted_unrealized / initial_margin) * 100.0

    def _increment_skip(self, symbol: str, reason: Optional[str]):
        count = self._skip_counts.get(symbol, 0) + 1
        self._skip_counts[symbol] = count
        logger.info("Skip count %s -> %d (%s)", symbol, count, reason)
        if count >= 5:
            # 영구 차단 대신 30분 TTL 차단 (신호 약화는 일시적일 수 있음)
            if not hasattr(self, "_timed_skip_block"):
                self._timed_skip_block: dict = {}
            self._timed_skip_block[symbol] = time.time() + 1800  # 30분 후 자동 해제
            self._skip_counts[symbol] = 0  # 카운터 리셋
            self._notify("WATCH", self._ko(
                f"심볼 일시 제외: {symbol} 30분 차단 (반복 필터 실패)",
                f"Symbol temp-blocked: {symbol} 30min block (repeated filter fails)"
            ))

    async def _sync_open_orders(self):
        """C2+C3: Cancel any orphaned open orders on boot — prevents ghost fills after restart.

        On restart the engine's _pending_orders set is empty, so any open orders
        placed in a previous session are "orphans".  We cancel them so they don't
        ghost-fill and create unexpected positions.
        """
        try:
            open_orders = await self.client.futures_get_open_orders()
            self._record_api_call()  # C1
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, "sync_open_orders")
            return
        except Exception as exc:
            logger.warning("[SYNC_ORDERS] Failed to fetch open orders: %s", exc)
            return
        if not open_orders:
            logger.info("[SYNC_ORDERS] No open orders found on boot")
            return
        cancelled = 0
        errors = 0
        for order in open_orders:
            symbol = order.get("symbol", "")
            order_id = order.get("orderId")
            status = order.get("status", "")
            if status not in ("NEW", "PARTIALLY_FILLED"):
                continue
            if symbol in self._pending_orders:
                logger.info("[SYNC_ORDERS] Keeping order %s %s (tracked)", symbol, order_id)
                continue
            # Orphan order — cancel it
            try:
                await self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
                self._record_api_call()
                cancelled += 1
                logger.info("[SYNC_ORDERS] Cancelled orphan order %s id=%s", symbol, order_id)
            except BinanceAPIException as exc:
                if getattr(exc, "code", None) == -2011:
                    pass  # already cancelled / filled — fine
                else:
                    self._handle_api_exception(exc, f"cancel_orphan_order_{symbol}")
                    errors += 1
            except Exception as exc:
                logger.warning("[SYNC_ORDERS] Cancel failed %s id=%s: %s", symbol, order_id, exc)
                errors += 1
        if cancelled or errors:
            self._notify("WARN", self._ko(
                f"[부팅 주문정리] 고아 주문 {cancelled}개 취소, 실패 {errors}개",
                f"[BOOT_SYNC] Cancelled {cancelled} orphan orders, {errors} errors"
            ))
        else:
            logger.info("[SYNC_ORDERS] %d open orders verified — all tracked", len(open_orders))

    async def _hydrate_positions(self):
        try:
            positions = await self.client.futures_position_information()
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, "hydrate_positions")
            return
        except Exception as exc:
            logger.warning("Failed to hydrate positions: %s", exc)
            return
        loss_threshold_pct = abs(self.config.max_loss_per_position or 10.0)
        for pos in positions:
            symbol = pos.get("symbol")
            try:
                entry_price = float(pos.get("entryPrice", 0.0))
                position_amt = float(pos.get("positionAmt", 0.0))
                leverage = float(pos.get("leverage", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if not symbol or position_amt == 0:
                continue
            if leverage > 0:
                self._symbol_leverage[symbol] = int(leverage)
            unrealized = float(pos.get("unRealizedProfit", 0.0))
            roi_percent = self._position_roi_percent(pos)
            self._open_symbols.add(symbol)
            self._ensure_snapshot_for_position(pos)
            _snap_sl = self.position_snapshots.get(symbol)
            if _snap_sl:
                _snap_sl.unrealized_pnl = unrealized
            # 청산 직전 PnL 계산용으로 unrealized_pnl 캐시 업데이트
            snap = self.position_snapshots.get(symbol)
            if snap:
                snap.unrealized_pnl = unrealized
            logger.info(
                "Hydrated position %s qty=%s entry=%s unrealized=%s roi=%.2f%%",
                symbol,
                position_amt,
                entry_price,
                unrealized,
                roi_percent,
            )
            if roi_percent <= -loss_threshold_pct:
                # C2: startup grace — skip forced close for N seconds after boot
                grace_sec = int(getattr(self.config, "startup_grace_sec", 60))
                elapsed = time.time() - self._engine_boot_time
                if elapsed < grace_sec:
                    logger.warning(
                        "[STARTUP_GRACE] Skipping forced close for %s (roi=%.2f%%, grace=%ds, elapsed=%.0fs)",
                        symbol, roi_percent, grace_sec, elapsed,
                    )
                    self._notify("WARN", self._ko(
                        f"[시작 보호] {symbol} 강제청산 보류 (roi={roi_percent:.2f}%, grace={grace_sec}s)",
                        f"[STARTUP_GRACE] {symbol} forced close deferred (roi={roi_percent:.2f}%, grace={grace_sec}s)"
                    ))
                else:
                    await self._force_close_position(
                        symbol,
                        position_amt,
                        roi_percent,
                        trigger=self._ko("손절", "stop_loss"),
                        exit_reason=self.EXIT_REASON_STOP_LOSS,
                    )

    async def _force_close_position(
        self,
        symbol: str,
        position_amt: float,
        roi_percent: float,
        trigger: str = "stop_loss",
        exit_reason: Optional[str] = None,
        note: Optional[str] = None,
        urgent: bool = False,
    ):
        """urgent=True  → 시장가(MARKET) 즉시 청산 (스파크, 킬스위치 등 긴급 상황)
           urgent=False → 지정가 post-only 시도 후 미체결 시 시장가 fallback
        """
        if not symbol or position_amt == 0:
            return False
        if symbol in self._closing_symbols:
            return False
        quantity = abs(position_amt)
        if quantity <= 0:
            return False
        side = SIDE_SELL if position_amt > 0 else SIDE_BUY
        if self._symbol_busy(symbol):
            logger.info("[EXIT] BLOCKED %s reason=BUSY", symbol)
            self._record_stat("exit_busy", 1)
            return False
        self._closing_symbols.add(symbol)
        self._pending_closes.add(symbol)
        try:
            order_side_text = "SELL" if side == SIDE_SELL else "BUY"
            order_side_local = self._ko("롱", "long") if side == SIDE_SELL else self._ko("숏", "short")
            # TCA snapshot (best-effort)
            expected_mid = None
            spread_bps = None
            if hasattr(self, "stream") and self.stream:
                try:
                    # mid-price at decision time
                    expected_mid = float(self.stream.get_mid_price(symbol)) if (self.stream is not None) else 0.0
                    getter = getattr(self.stream, "get_bid_ask", None) or getattr(self.stream, "get_best_bid_ask", None)
                    if getter:
                        bid, ask = getter(symbol)
                        bid = float(bid or 0.0)
                        ask = float(ask or 0.0)
                        if bid > 0 and ask > 0 and ask >= bid and expected_mid and expected_mid > 0:
                            spread_bps = float((ask - bid) / expected_mid * 10000.0)
                except Exception:
                    expected_mid = expected_mid
            # Snap quantity to exchange LOT_SIZE step to avoid -1111 precision error
            try:
                filters = await self._filters_for_symbol(symbol)
                step_size = 1.0
                for f in filters:
                    if f.get("filterType") == "LOT_SIZE":
                        try:
                            step_size = float(f.get("stepSize", 1.0) or 1.0)
                        except (TypeError, ValueError):
                            pass
                        break
                if step_size > 0:
                    precision = len(str(step_size).rstrip('0').split('.')[-1]) if '.' in str(step_size) else 0
                    quantity = round(math.floor(quantity / step_size) * step_size, precision)
            except Exception:
                pass
            if quantity <= 0:
                return False
            # 긴급 청산(urgent=True): 즉시 시장가
            # 일반 청산(urgent=False): 지정가 post-only 시도 후 미체결이면 시장가 fallback
            order = None
            _exit_fee_type = "taker"   # 기본: 시장가(테이커), LIMIT 체결 시 "maker"로 교체
            if not urgent:
                try:
                    # 지정가 최대 3회 시도 (매 시도마다 현재 호가 재조회)
                    _limit_attempts = int(getattr(self.config, "limit_exit_max_attempts", 3))
                    _wait_per_attempt = float(getattr(self.config, "limit_exit_wait_sec", 2.0))
                    _offset = float(getattr(self.config, "limit_exit_offset_bps", 2.0) or 2.0)
                    for _attempt in range(1, _limit_attempts + 1):
                        if order is not None:
                            break
                        _mid = self.stream.get_mid_price(symbol) if self.stream else 0.0
                        if not _mid or _mid <= 0:
                            break
                        # 롱 청산(SELL)→ bid 근처, 숏 청산(BUY)→ ask 근처
                        if side == SIDE_SELL:
                            _limit_px = _mid * (1.0 - _offset / 10000.0)
                        else:
                            _limit_px = _mid * (1.0 + _offset / 10000.0)
                        _limit_px = max(_limit_px, 0.0001)
                        try:
                            _limit_order = await self.client.futures_create_order(
                                symbol=symbol,
                                side=side,
                                type=ORDER_TYPE_LIMIT,
                                timeInForce="GTC",
                                quantity=quantity,
                                price=f"{_limit_px:.8f}",
                                reduceOnly=True,
                            )
                        except Exception as _oe:
                            logger.debug("[EXIT] LIMIT order attempt %d failed: %s", _attempt, _oe)
                            break
                        _deadline = time.time() + _wait_per_attempt
                        _oid = (_limit_order or {}).get("orderId")
                        _filled = False
                        while _oid and time.time() < _deadline:
                            await asyncio.sleep(0.2)
                            try:
                                _chk = await self.client.futures_get_order(symbol=symbol, orderId=_oid)
                                if (_chk or {}).get("status") == "FILLED":
                                    order = _chk
                                    _exit_fee_type = "maker"
                                    _filled = True
                                    logger.info("[EXIT] LIMIT filled %s attempt=%d oid=%s (maker)", symbol, _attempt, _oid)
                                    break
                            except Exception:
                                break
                        if not _filled and _oid:
                            # 미체결 → 취소 후 다음 시도 or 시장가 fallback
                            try:
                                await self.client.futures_cancel_order(symbol=symbol, orderId=_oid)
                                logger.info("[EXIT] LIMIT timeout attempt=%d → %s",
                                    _attempt, "retry" if _attempt < _limit_attempts else "MARKET fallback")
                            except Exception:
                                pass
                except Exception as _le:
                    logger.debug("[EXIT] LIMIT attempt failed, fallback MARKET: %s", _le)

            if order is None:
                order = await self.client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type=ORDER_TYPE_MARKET,
                    quantity=quantity,
                    reduceOnly=True,
                )
            if note:
                order["note"] = note
            fallback_price = self.stream.get_mid_price(symbol) if (self.stream is not None) else None
            exit_price = self._extract_fill_price(order, fallback=fallback_price or 0.0)
            # Binance 시장가 청산 응답에서 avgPrice="0" 케이스 대응 — 즉시 재조회
            if not exit_price or float(exit_price) <= 0:
                _oid = (order or {}).get("orderId")
                if _oid:
                    try:
                        await asyncio.sleep(0.3)  # 체결 확정 대기
                        _filled = await self.client.futures_get_order(symbol=symbol, orderId=_oid)
                        _refill = self._extract_fill_price(_filled, fallback=fallback_price or 0.0)
                        if _refill and float(_refill) > 0:
                            exit_price = _refill
                    except Exception as _e:
                        logger.debug("[EXIT] avgPrice re-query failed %s: %s", symbol, _e)
            # 여전히 0이면 스트림 현재가로 최후 fallback
            if not exit_price or float(exit_price) <= 0:
                if fallback_price and float(fallback_price) > 0:
                    exit_price = fallback_price
                    logger.warning("[EXIT] exit_price fallback to stream mid: %s = %.4f", symbol, exit_price)
            slippage_bps = None
            if expected_mid and expected_mid > 0 and exit_price and float(exit_price) > 0:
                try:
                    slippage_bps = float((float(exit_price) - float(expected_mid)) / float(expected_mid) * 10000.0)
                except Exception:
                    slippage_bps = None
            message = self._ko(
                f"주문 청산: {order_side_local} ({order_side_text}) {symbol} ROI {roi_percent:.2f}% (exit {exit_price:.4f})",
                f"Order close: {order_side_local} ({order_side_text}) {symbol} ROI {roi_percent:.2f}% (exit {exit_price:.4f})")
            self._notify("ALERT", message)
            self._open_symbols.discard(symbol)
            # [PATCH-11] 재진입 쿨다운 타임스탬프 기록
            if not hasattr(self, "_symbol_last_exit_ts"):
                self._symbol_last_exit_ts = {}
            self._symbol_last_exit_ts[symbol] = time.time()
            logger.warning("Closed %s via %s at ROI %.2f%%", symbol, trigger, roi_percent)
            if fallback_price:
                slippage = (exit_price - fallback_price) if exit_price and fallback_price else 0.0
                self._notify("WATCH", self._ko(f"체결 슬리피지 {symbol}: 예상 {fallback_price:.4f} → 실제 {exit_price:.4f} (Δ={slippage:.4f})", f"Fill slippage {symbol}: expected {fallback_price:.4f} → actual {exit_price:.4f} (Δ={slippage:.4f})"))
            reason = exit_reason or self.EXIT_REASON_MANUAL
            snapshot = self.position_snapshots.get(symbol)
            entry_price = None
            fees_model = None
            if snapshot:
                entry_price = getattr(snapshot, "entry_price", None)
                fees_model = getattr(snapshot, "fees_model", None)
            # ── PnL 계산: snapshot.unrealized_pnl(Binance 직접 계산값) 우선 사용 ──
            # unrealized_pnl = Binance API의 unRealizedProfit (수수료 미포함 레버리지 반영)
            # 수수료를 별도 계산해서 차감하면 실제 계좌 변화액과 일치
            unrealized_snap = getattr(snapshot, "unrealized_pnl", None) if snapshot else None
            # 실제 체결 방식에 따른 수수료율 결정
            _maker_fee = float(getattr(self.config, "maker_fee_pct", 0.0002) or 0.0002)
            _taker_fee = float(getattr(self.config, "taker_fee_pct", 0.0005) or 0.0005)
            # [PATCH-8] 항상 taker 기준 보수적 수수료 적용 (maker 체결이어도 taker로 계산)
            _actual_fee_rate = _taker_fee
            # 진입 수수료율: snapshot fees_model에 기록된 값 사용, 없으면 taker로 fallback
            _entry_fee_rate = float((fees_model or {}).get("taker", _taker_fee))
            _ep_for_fee = float(entry_price or exit_price or 0.0)
            _exit_px    = float(exit_price or 0.0)
            _qty_abs    = abs(position_amt)
            # 진입 수수료: 진입가 × 수량 × 진입 수수료율
            _entry_fee_amount = _ep_for_fee * _qty_abs * _entry_fee_rate if _entry_fee_rate > 0 else 0.0
            # 청산 수수료: 청산가 × 수량 × 청산 수수료율
            _exit_fee_amount  = _exit_px   * _qty_abs * _actual_fee_rate if _actual_fee_rate > 0 else 0.0
            # 전체 수수료 = 진입 + 청산
            _total_fee_amount = _entry_fee_amount + _exit_fee_amount
            if unrealized_snap is not None:
                # [PATCH-8] Binance unrealizedProfit은 수수료 미포함 순수 가격차이.
                # 실제 잔고 변화 = unrealized - 진입수수료 - 청산수수료
                # (진입수수료는 Binance가 잔고에서 이미 차감했으므로 PnL에도 반영해야
                #  기록된 PnL 합계 = 실제 잔고 변화와 일치함)
                #
                # 부분 청산(PARTIAL_TP) 보정:
                # unrealized_snap은 전체 잔여 포지션의 미실현 손익이므로
                # 부분 청산 시에는 (청산 수량 / 전체 수량) 비율로 안분해야 함.
                _snap_qty = abs(float(getattr(snapshot, "quantity", 0.0) or 0.0)) if snapshot else 0.0
                if _snap_qty > 0 and _qty_abs < _snap_qty * 0.99:
                    # 부분 청산: unrealized를 비율로 안분
                    _partial_ratio = _qty_abs / _snap_qty
                    _prorated_unrealized = float(unrealized_snap) * _partial_ratio
                else:
                    # 전량 청산
                    _prorated_unrealized = float(unrealized_snap)
                pnl_value = _prorated_unrealized - _entry_fee_amount - _exit_fee_amount
            else:
                # fallback: (exit-entry)*qty - 전체수수료 직접 계산
                pnl_value = self._compute_realized_pnl(entry_price, exit_price, position_amt, fees_model)
            self._record_exit_event(symbol, reason, roi_percent, pnl_value=pnl_value, trigger=trigger)
            # pnl_fast 로컬 반영 — Kill Switch가 API 호출 전에도 작동하도록
            if pnl_value != 0.0:
                self._record_realized_pnl(pnl_value, symbol=symbol, trigger=reason)
            if entry_price is not None and exit_price is not None:
                _lev = float(getattr(snapshot, "leverage", None) or
                             self._symbol_leverage.get(symbol, 1) or 1.0)
                self._log_trade_event(
                    symbol=symbol,
                    side="LONG" if position_amt > 0 else "SHORT",
                    quantity=abs(position_amt),
                    entry_price=float(entry_price),
                    exit_price=float(exit_price),
                    pnl_value=float(pnl_value),
                    roi_percent=float(roi_percent),
                    trigger=trigger,
                    leverage=_lev,
                    order_id=order.get("orderId"),
                    expected_mid=expected_mid,
                    spread_bps=spread_bps,
                    slippage_bps=slippage_bps,
                    fee_type=_exit_fee_type,
                    fee_amount=_total_fee_amount,   # 진입+청산 수수료 합산
                    fee_rate=_actual_fee_rate,
                )
            self.position_snapshots.pop(symbol, None)
            return True
        except BinanceAPIException as exc:
            # -1007: Binance backend timeout — execution status unknown.
            # The order may have been filled. Verify by re-fetching position.
            if exc.code == -1007:
                if self.testnet:
                    logger.warning(
                        "[TESTNET] Timeout (-1007) closing %s via %s — skipping verification on testnet",
                        symbol, trigger,
                    )
                    return False
                logger.warning(
                    "Timeout (-1007) closing %s via %s — verifying actual position status...",
                    symbol, trigger,
                )
                await asyncio.sleep(2)
                try:
                    positions = await self.client.futures_position_information(symbol=symbol)
                    actual_amt = 0.0
                    for p in positions:
                        try:
                            actual_amt = float(p.get("positionAmt", 0.0))
                        except (TypeError, ValueError):
                            pass
                    if actual_amt == 0.0:
                        # Position is gone — order was actually filled
                        logger.warning(
                            "[TIMEOUT_VERIFY] %s position is 0 after -1007 -> treating as closed", symbol
                        )
                        self._notify("ALERT", self._ko(f"[타임아웃 체결확인] {symbol} 포지션 소멸 확인 -> 청산 성공 처리", f"[TIMEOUT FILL CONFIRMED] {symbol} position closed -> success"))
                        self._open_symbols.discard(symbol)
                        # [PATCH-11] 재진입 쿨다운 타임스탬프 기록
                        if not hasattr(self, "_symbol_last_exit_ts"):
                            self._symbol_last_exit_ts = {}
                        self._symbol_last_exit_ts[symbol] = time.time()
                        self.position_snapshots.pop(symbol, None)
                        self._record_exit_event(symbol, exit_reason or self.EXIT_REASON_MANUAL, roi_percent)
                        return True
                    else:
                        # Position still open — order did not fill
                        logger.warning(
                            "[TIMEOUT_VERIFY] %s positionAmt=%.6f after -1007 -> order did NOT fill",
                            symbol, actual_amt,
                        )
                        self._notify("ALERT", self._ko(f"[타임아웃 미체결] {symbol} 포지션 잔존 확인 -> 다음 틱 재시도", f"[TIMEOUT UNFILLED] {symbol} position still open -> retrying next tick"))
                except Exception as verify_exc:
                    logger.warning(
                        "[TIMEOUT_VERIFY] Failed to verify %s position after -1007: %s",
                        symbol, verify_exc,
                    )
            else:
                self._handle_api_exception(exc, "force_close")
            logger.warning("Failed to close %s via %s: %s", symbol, trigger, exc)
            return False
        except Exception as exc:
            logger.warning("Failed to close %s via %s: %s", symbol, trigger, exc)
            return False
        finally:
            self._closing_symbols.discard(symbol)
            self._pending_closes.discard(symbol)

    async def _enforce_stop_losses(self, positions: Optional[List[dict]] = None, symbol_snaps: Optional[List[SymbolSnapshot]] = None):
        loss_threshold_pct = abs(self.config.max_loss_per_position or 0.0)
        if positions is None:
            positions = await self._fetch_active_positions()
        snap_map = {snap.symbol: snap for snap in (symbol_snaps or [])}
        active_symbols: set[str] = set()
        for pos in positions:
            try:
                position_amt = float(pos.get("positionAmt", 0.0))
            except (TypeError, ValueError):
                continue
            if position_amt == 0:
                continue
            symbol = pos.get("symbol")
            if not symbol:
                continue
            active_symbols.add(symbol)
            snapshot = self.position_snapshots.get(symbol)
            price_stop_hit = False
            if snapshot and snapshot.stop_loss_px:
                snap_info = snap_map.get(symbol)
                mark = 0.0
                if snap_info:
                    mark = float(snap_info.mark_price or snap_info.price or 0.0)
                if mark > 0 and self._price_stop_hit(snapshot.side, mark, snapshot.stop_loss_px):
                    if self._minimum_hold_elapsed(snapshot):
                        roi_percent = self._position_roi_percent(pos)
                        await self._force_close_position(
                            symbol,
                            position_amt,
                            roi_percent,
                            trigger=self._ko("가격 SL", "price_SL"),
                            exit_reason=self.EXIT_REASON_STOP_LOSS,
                        )
                        price_stop_hit = True
            if price_stop_hit:
                continue
            # [PATCH-13] 단일 거래 손실 하드캡 (min_hold 고려 + 2단계 캡)
            _hard_loss_cap = float(getattr(self.config, "max_single_trade_loss_pct", 0.0) or 0.0)
            if _hard_loss_cap > 0:
                roi_percent = self._position_roi_percent(pos)
                _critical_cap = _hard_loss_cap * 1.5  # 극단 손실 (캡의 150%)
                if roi_percent <= -_critical_cap:
                    # 극단 손실: min_hold 무시, 즉시 강제 청산
                    logger.warning("[HARD_LOSS_CAP_CRITICAL] %s ROI=%.2f%% < -%.2f%% → forced close (min_hold bypass)", symbol, roi_percent, _critical_cap)
                    await self._force_close_position(
                        symbol, position_amt, roi_percent,
                        trigger=self._ko(f"극단 손실캡 -{_critical_cap:.1f}%", f"critical_loss_cap_{_critical_cap:.1f}%"),
                        exit_reason=self.EXIT_REASON_STOP_LOSS, urgent=True,
                    )
                    continue
                elif roi_percent <= -_hard_loss_cap:
                    # 일반 하드캡: min_hold 경과 후에만 청산 (노이즈 손절 방지)
                    if snapshot and self._minimum_hold_elapsed(snapshot):
                        logger.warning("[HARD_LOSS_CAP] %s ROI=%.2f%% < -%.2f%% → forced close", symbol, roi_percent, _hard_loss_cap)
                        await self._force_close_position(
                            symbol, position_amt, roi_percent,
                            trigger=self._ko(f"손실캡 -{_hard_loss_cap}%", f"hard_loss_cap_{_hard_loss_cap}%"),
                            exit_reason=self.EXIT_REASON_STOP_LOSS, urgent=True,
                        )
                        continue
            if loss_threshold_pct > 0:
                roi_percent = self._position_roi_percent(pos)
                if roi_percent <= -loss_threshold_pct and self._minimum_hold_elapsed(snapshot):
                    await self._force_close_position(
                        symbol,
                        position_amt,
                        roi_percent,
                        trigger=self._ko("손절", "stop_loss"),
                        exit_reason=self.EXIT_REASON_STOP_LOSS,
                    )
        if active_symbols:
            self._open_symbols = active_symbols
        else:
            self._open_symbols.clear()

    async def _enforce_spike_guard(self, positions: Optional[List[dict]] = None):
        threshold = float(getattr(self.config, "spike_guard_return_pct", 0.0) or 0.0)
        if not getattr(self.config, "spike_guard_enabled", True):
            threshold = 0.0
        if threshold <= 0:
            return
        now = time.time()
        if now < self._global_spike_cooldown_until:
            return
        interval = max(1.0, float(getattr(self.config, "spike_guard_check_interval_s", 2)))
        if now - self._spike_guard_last_check < interval:
            return
        self._spike_guard_last_check = now
        window = max(1, int(getattr(self.config, "spike_guard_window", 8)))
        if positions is None:
            positions = await self._fetch_active_positions()
        triggered: List[Tuple[str, float, float, dict]] = []
        for pos in positions:
            try:
                position_amt = float(pos.get("positionAmt", 0.0))
            except (TypeError, ValueError):
                continue
            if position_amt == 0:
                continue
            symbol = pos.get("symbol")
            if not symbol:
                continue
            if symbol in self._spike_reentry_until and now < self._spike_reentry_until[symbol]:
                continue
            recent_move = self._recent_return_pct(symbol, window)
            if recent_move == 0.0:
                continue
            direction = 1.0 if position_amt > 0 else -1.0
            adjusted_move = direction * recent_move
            if adjusted_move <= -threshold:
                triggered.append((symbol, position_amt, recent_move, pos))
        if triggered:
            # [PATCH-9] 심볼별 쿨다운 옵션: 글로벌 쿨다운 대신 해당 심볼만 차단
            _per_symbol = bool(getattr(self.config, "spike_guard_per_symbol_cooldown", False))
            if not _per_symbol:
                self._global_spike_cooldown_until = now + self.global_spike_cooldown_min * 60
            self._global_spike_reason = (
                self._ko(
                    f"스파크 방어 발동 {len(triggered)}건 → {'심볼별' if _per_symbol else '글로벌'} 쿨다운 {self.global_spike_cooldown_min}분",
                    f"Spike guard triggered {len(triggered)} positions → {'per-symbol' if _per_symbol else 'global'} cooldown {self.global_spike_cooldown_min}m")
            )
            self._notify("WARN", self._global_spike_reason)
        for symbol, position_amt, move, raw in triggered:
            roi_percent = self._position_roi_percent(raw)
            await self._force_close_position(
                symbol,
                position_amt,
                roi_percent,
                trigger=self._ko("스파크 방어", "spike_guard"),
                exit_reason=self.EXIT_REASON_SPIKE_GUARD,
                urgent=True,
            )
            cooldown = max(window * 2, 10)
            self._spike_blocked_until[symbol] = time.time() + cooldown
            self._spike_reentry_until[symbol] = time.time() + self.spark_reentry_seconds
            self._notify(
                "ALERT",
                self._ko(f"스파크 방어 발동: {symbol} 최근 {window}s 변화 {move * 100:.2f}% (임계 {threshold * 100:.2f}%)", f"Spike guard: {symbol} {window}s move {move * 100:.2f}% (threshold {threshold * 100:.2f}%)"),
            )

    async def _enforce_take_profit(self, symbol_snaps: List[SymbolSnapshot], positions: Optional[List[dict]] = None):
        if getattr(self.config, "enable_profit_exit_layer", False):
            return
        if not getattr(self.config, "enable_take_profit", False):
            return
        if positions is None:
            positions = await self._fetch_active_positions()
        pos_map: Dict[str, dict] = {}
        for pos in positions:
            try:
                amt = float(pos.get("positionAmt", 0.0))
            except (TypeError, ValueError):
                continue
            symbol = pos.get("symbol")
            if not symbol or amt == 0:
                continue
            pos_map[symbol] = pos
        if not pos_map:
            return
        snap_map = {snap.symbol: snap for snap in symbol_snaps}
        partial_ratio = float(getattr(self.config, "partial_tp_ratio", 0.5))
        tp_cooldown = max(int(getattr(self.config, "tp_cooldown_s", 30)), 0)
        now = time.time()
        for symbol, snapshot in list(self.position_snapshots.items()):
            if symbol not in pos_map or symbol not in snap_map:
                continue
            if not self._minimum_hold_elapsed(snapshot):
                continue
            tp_levels = snapshot.take_profit_levels or []
            if not tp_levels:
                continue
            pos = pos_map[symbol]
            try:
                position_amt = float(pos.get("positionAmt", 0.0))
            except (TypeError, ValueError):
                continue
            if position_amt == 0:
                continue
            direction = "LONG" if position_amt > 0 else "SHORT"
            price = float(snap_map[symbol].mark_price or snap_map[symbol].price or 0.0)
            if price <= 0:
                continue
            roi_percent = self._position_roi_percent(pos)
            last_tp = snapshot.last_tp_ts or 0.0
            if tp_cooldown and (now - last_tp) < tp_cooldown:
                continue
            tp1 = tp_levels[0] if len(tp_levels) >= 1 else None
            tp2 = tp_levels[1] if len(tp_levels) >= 2 else None
            triggered = None
            tp_stage = None
            remaining_qty = abs(position_amt)
            if snapshot.partial_tp_done:
                tp1 = None
            if tp1 and self._tp_hit(direction, price, tp1):
                if 0 < partial_ratio < 1:
                    qty_to_close = remaining_qty * partial_ratio
                else:
                    qty_to_close = remaining_qty
                triggered = qty_to_close
                tp_stage = "TP1"
            elif tp2 and self._tp_hit(direction, price, tp2):
                triggered = remaining_qty
                tp_stage = "TP2"
            if not triggered:
                continue
            signed_amt = math.copysign(triggered, position_amt)
            if tp_stage == "TP1" and self._symbol_busy(symbol):
                continue
            result = await self._force_close_position(
                symbol,
                signed_amt,
                roi_percent,
                trigger=tp_stage,
                exit_reason=self.EXIT_REASON_TAKE_PROFIT,
            )
            if not result:
                continue
            if tp_stage == "TP1" and 0 < partial_ratio < 1:
                new_stop = snapshot.stop_loss_px
                if getattr(self.config, "break_even_after_partial", True):
                    epsilon = snapshot.entry_price * max(self.config.tp_min_roi_pct, 0.0)
                    if direction == "LONG":
                        new_stop = max(snapshot.entry_price + epsilon, snapshot.stop_loss_px or 0.0)
                    else:
                        new_stop = min(snapshot.entry_price - epsilon, snapshot.stop_loss_px or snapshot.entry_price)
                snapshot = update_snapshot(
                    snapshot,
                    partial_tp_done=True,
                    stop_loss_px=new_stop,
                    last_tp_ts=now,
                    trail_active=True,
                    trail_ref_px=price,
                    trail_offset_px=abs(price - new_stop),
                )
                self.position_snapshots[symbol] = snapshot
            else:
                self.position_snapshots.pop(symbol, None)

    def _tp_hit(self, direction: str, price: float, target: float) -> bool:
        if direction.upper() == "LONG":
            return price >= target
        return price <= target

    def _price_stop_hit(self, direction: str, price: float, stop_price: float) -> bool:
        if direction.upper() == "LONG":
            return price <= stop_price
        return price >= stop_price

    def _minimum_hold_elapsed(self, snapshot: Optional[PositionSnapshot]) -> bool:
        if snapshot is None:
            return True
        min_hold = max(int(getattr(self.config, "min_hold_seconds", 0)), 0)
        if min_hold == 0:
            return True
        opened = getattr(snapshot, "opened_at", None)
        if not opened:
            return True
        return (time.time() - float(opened)) >= min_hold

    async def _enforce_time_and_signal_decay(self, positions: Optional[List[dict]] = None, symbol_snaps: Optional[List[SymbolSnapshot]] = None):
        # [PATCH-16] 개별 enable 플래그 우선 체크
        enable_time = bool(getattr(self.config, "enable_time_stop", False)) and bool(getattr(self.config, "time_stop_seconds", 0))
        enable_decay = bool(getattr(self.config, "enable_signal_decay_exit", False)) and bool(getattr(self.config, "signal_decay_threshold", 0))
        if not enable_time and not enable_decay:
            return
        if positions is None:
            positions = await self._fetch_active_positions()
        snap_map = {snap.symbol: snap for snap in (symbol_snaps or [])}
        max_hold = max(int(getattr(self.config, "time_stop_seconds", 0)), 0)
        decay_threshold = float(getattr(self.config, "signal_decay_threshold", 0.0) or 0.0)
        decay_min_profit = float(getattr(self.config, "signal_decay_min_profit", 0.0) or 0.0)
        now = time.time()
        for pos in positions:
            try:
                position_amt = float(pos.get("positionAmt", 0.0))
            except (TypeError, ValueError):
                continue
            if position_amt == 0:
                continue
            symbol = pos.get("symbol")
            if not symbol or symbol not in self.position_snapshots:
                continue
            snapshot = self.position_snapshots[symbol]
            # unrealized_pnl 최신화 (청산 시 정확한 PnL 계산용)
            _unreal = pos.get("unRealizedProfit")
            if _unreal is not None:
                snapshot.unrealized_pnl = float(_unreal)
            if not self._minimum_hold_elapsed(snapshot):
                continue
            reason = None
            roi_percent = self._position_roi_percent(pos)
            if enable_time and max_hold > 0:
                opened = getattr(snapshot, "opened_at", None)
                if opened:
                    # [PATCH-2+9] ATR 기반 적응형 시간 손절 + 레짐별 조정
                    _adaptive = bool(getattr(self.config, "time_stop_adaptive", False))
                    _effective_hold = max_hold
                    if _adaptive and symbol in snap_map:
                        _cur_atr = float(getattr(snap_map[symbol], "atr_value", 0.0) or 0.0)
                        _ref_atr = float(getattr(self.config, "time_stop_atr_ref", 0.005) or 0.005)
                        _min_s = int(getattr(self.config, "time_stop_min_seconds", 600))
                        _max_s = int(getattr(self.config, "time_stop_max_seconds", 7200))  # [PATCH-9] 3600→7200
                        if _cur_atr > 0 and _ref_atr > 0:
                            _ratio = _ref_atr / _cur_atr
                            _effective_hold = max(_min_s, min(int(max_hold * _ratio), _max_s))
                    # [PATCH-9] trend 레짐이면 time-stop 연장 (추세 수익 보호)
                    _ts_regime = "chop"
                    if self.auto_tuner and bool(getattr(self.config, "auto_tune_enabled", True)):
                        _ts_regime = getattr(self.auto_tuner.state.hysteresis, "current_regime", "chop")
                    if _ts_regime in ("trend_up", "trend_down") and roi_percent > 0:
                        # 추세 + 수익 중이면 최대 2시간까지 연장
                        _effective_hold = max(_effective_hold, int(getattr(self.config, "time_stop_max_seconds", 7200)))
                    # [PATCH-10] 펀딩 타임 근처 time-stop 축소 (펀딩 비용 방어)
                    if bool(getattr(self.config, "funding_time_stop_enabled", False)):
                        import datetime
                        _utc_now = datetime.datetime.utcnow()
                        _funding_hours = [0, 8, 16]  # 바이낸스 펀딩 정산 시각 (UTC)
                        _fund_window_min = int(getattr(self.config, "funding_time_stop_window_min", 30))
                        _fund_mult = float(getattr(self.config, "funding_time_stop_mult", 0.5))
                        for _fh in _funding_hours:
                            _fund_time = _utc_now.replace(hour=_fh, minute=0, second=0, microsecond=0)
                            _diff_min = abs((_utc_now - _fund_time).total_seconds()) / 60.0
                            if _diff_min > 720:  # 12시간 넘으면 다음 날 기준
                                _diff_min = 1440 - _diff_min
                            if _diff_min <= _fund_window_min:
                                _effective_hold = max(600, int(_effective_hold * _fund_mult))
                                break
                    if (now - opened) >= _effective_hold:
                        reason = self.EXIT_REASON_TIME_STOP
            if reason is None and enable_decay and decay_threshold > 0 and symbol in snap_map:
                # signal decay 청산: 진입+청산 수수료를 커버하는 ROI를 넘어야 발동
                # (수수료 미충족 구간에서 decay 청산 → 수수료 손실 확정 방지)
                _fee_min_roi = self._fee_break_even_roi_pct(
                    snapshot.entry_price, snapshot.quantity, snapshot.leverage)
                # 수수료 110% 이상 + config decay_min_profit 중 큰 값을 최소 기준으로 사용
                _eff_decay_min = max(decay_min_profit, _fee_min_roi * 1.1)
                entry_strength = abs(snapshot.momentum_at_entry or 0.0)
                if entry_strength > 0 and roi_percent >= _eff_decay_min:
                    # 단기 EMA slope 기반 현재 강도 보완 (24h momentum_pct만으론 단기 반전 감지 불가)
                    _short_m = float(getattr(snap_map[symbol], "momentum_5m", 0.0) or 0.0)
                    current_strength = abs(_short_m) if abs(_short_m) > 1e-6 else abs(snap_map[symbol].momentum_pct)
                    _ema_p = int(getattr(self.config, "mtf_ema_period", 21))
                    _slope_sum = 0.0
                    _slope_n = 0
                    for _tf in [60, 300]:
                        try:
                            _s = self._mtf_ema_slope_bps(symbol, _tf, _ema_p)
                            if _s == _s:  # not NaN
                                _slope_sum += abs(_s)
                                _slope_n += 1
                        except Exception:
                            pass
                    if _slope_n > 0:
                        _ema_strength = (_slope_sum / _slope_n) / 10000.0
                        current_strength = max(current_strength, _ema_strength)
                    if (current_strength / entry_strength) < decay_threshold:
                        reason = self.EXIT_REASON_SIGNAL_DECAY
            if reason:
                await self._force_close_position(
                    symbol,
                    position_amt,
                    roi_percent,
                    trigger=reason,
                    exit_reason=reason,
                )

    def _update_trailing_stops(self, symbol_snaps: List[SymbolSnapshot]):
        # profit_exit_layer가 켜져 있어도 trailing stop 업데이트는 계속 진행
        # (_evaluate_profit_exit_layers가 trail_stop_price를 읽기 때문)
        if not getattr(self.config, "enable_take_profit", False) and not getattr(self.config, "enable_profit_exit_layer", False):
            return
        snap_map = {snap.symbol: snap for snap in symbol_snaps}
        trail_mult = max(float(getattr(self.config, "trail_atr_mult", 1.7)), 0.1)  # [PATCH-14] 1.0→1.7 config 정렬
        trail_step = max(float(getattr(self.config, "trail_min_step_pct", 0.001)), 0.0)
        for symbol, snapshot in list(self.position_snapshots.items()):
            if not snapshot.trail_active:
                continue
            snap_info = snap_map.get(symbol)
            if not snap_info:
                continue
            price = float(snap_info.mark_price or snap_info.price or 0.0)
            if price <= 0:
                continue
            current_stop = snapshot.stop_loss_px or snapshot.entry_price
            trail_ref = snapshot.trail_ref_px or snapshot.entry_price
            atr_value = snapshot.atr_at_entry or self._last_atr_estimate or abs(snapshot.entry_price) * 0.001
            new_stop, new_ref = update_trailing_stop(
                current_stop,
                trail_ref,
                price,
                atr_value,
                snapshot.side,
                trail_mult,
                trail_step,
            )
            if new_stop == current_stop and new_ref == trail_ref:
                continue
            snapshot = update_snapshot(snapshot, stop_loss_px=new_stop, trail_ref_px=new_ref, trail_offset_px=abs(new_ref - new_stop))
            self.position_snapshots[symbol] = snapshot

    async def _get_available_balance(self) -> float:
        now = time.time()
        if now - self._balance_cache["ts"] > 10:
            try:
                account = await self.client.futures_account()
            except BinanceAPIException as exc:
                self._handle_api_exception(exc, "futures_account")
                return 0.0
            available = float(account.get("availableBalance", 0.0))
            self._balance_cache = {"ts": now, "available": available}
            # Kill Switch용 잔고 기준선 갱신 (세션 최초 1회 이후 고정)
            if not getattr(self, "_last_known_balance", 0.0):
                self._last_known_balance = available
        return max(0.0, self._balance_cache.get("available", 0.0))

    async def _get_available_notional(self) -> float:
        available = await self._get_available_balance()
        return max(0.0, available * max(self.config.position_pct, 0.0))

    async def _get_account_equity(self) -> float:
        try:
            account = await self.client.futures_account()
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, "account_equity")
            return 0.0
        except Exception as exc:
            logger.warning("Failed to fetch account equity: %s", exc)
            return 0.0
        return float(account.get("totalWalletBalance", 0.0))


    def _append_log(self, message: str, level: str = "INFO"):
        """Internal lightweight logger wrapper used by UI-aligned features.

        This engine uses module-level `logger`; this method provides a stable hook
        for patches that were originally developed against the GUI logger.
        """
        if message is None:
            return
        msg = str(message)

        # Auto-detect level from common tags if caller didn't specify.
        lvl = (level or "INFO").upper()
        tag = msg.strip().upper()
        if tag.startswith("[ERROR]") or tag.startswith("ERROR"):
            lvl = "ERROR"
        elif tag.startswith("[WARN]") or tag.startswith("WARN"):
            lvl = "WARNING"
        elif tag.startswith("[DEBUG]") or tag.startswith("DEBUG"):
            lvl = "DEBUG"
        elif tag.startswith("[INFO]") or tag.startswith("INFO"):
            lvl = "INFO"

        if lvl == "DEBUG":
            logger.debug(msg)
        elif lvl in ("WARN", "WARNING"):
            logger.warning(msg)
        elif lvl == "ERROR":
            logger.error(msg)
        else:
            logger.info(msg)

    def _trim_notification_log(self, max_bytes: int = 200_000):
        try:
            if os.path.exists(self.notification_path):
                size = os.path.getsize(self.notification_path)
                if size > max_bytes:
                    keep = max_bytes // 2
                    with open(self.notification_path, "r", encoding="utf-8", errors="ignore") as fh:
                        fh.seek(max(size - keep, 0))
                        if fh.tell() > 0:
                            fh.readline()
                        data = fh.read()
                    with open(self.notification_path, "w", encoding="utf-8") as fh:
                        fh.write(data)
        except OSError:
            logger.exception("Failed to trim notification log")

    def _normalize_pct_value(self, value: float) -> float:
        try:
            val = float(value)
        except (TypeError, ValueError):
            return 0.0
        if abs(val) <= 1.0:
            return val * 100.0
        return val

    def _normalize_ratio_value(self, value: float, clamp_max: float = 0.99) -> float:
        """ratio 정규화: >1.0이면 /100 (단위 오류 자동 보정). 결과는 0~clamp_max."""
        try:
            val = float(value)
        except (TypeError, ValueError):
            return 0.0
        if val > 1.0:
            val = val / 100.0
        return max(0.0, min(val, clamp_max))

    def _update_profit_tracking(self, snapshot: PositionSnapshot, direction: str, price: float, roi_percent: float):
        if snapshot.highest_price_since_entry is None:
            snapshot.highest_price_since_entry = snapshot.entry_price
        if snapshot.lowest_price_since_entry is None:
            snapshot.lowest_price_since_entry = snapshot.entry_price
        direction = direction.upper()
        if direction == "LONG":
            if price > snapshot.highest_price_since_entry:
                snapshot.highest_price_since_entry = price
        else:
            if price < snapshot.lowest_price_since_entry:
                snapshot.lowest_price_since_entry = price
        now = time.time()
        if roi_percent > (snapshot.mfe_pnl_pct or 0.0):
            snapshot.mfe_pnl_pct = roi_percent
            snapshot.mfe_last_update_ts = now
        elif snapshot.mfe_last_update_ts is None:
            snapshot.mfe_last_update_ts = now

    async def _maybe_execute_partial_tp(self, symbol: str, snapshot: PositionSnapshot, pos: dict, roi_percent: float) -> bool:
        # 청산 직전 unrealized_pnl 최신화 (partial TP, trailing stop 시 정확한 PnL 계산용)
        if pos and isinstance(pos, dict):
            _unrealized = pos.get("unRealizedProfit")
            if _unrealized is not None:
                snapshot.unrealized_pnl = float(_unrealized)
        if not getattr(self.config, "enable_partial_take_profit", False):
            return False
        levels = list(getattr(self.config, "partial_tp_levels", []))
        if not levels:
            return False
        try:
            position_amt = float(pos.get("positionAmt", 0.0))
        except (TypeError, ValueError):
            return False
        if position_amt == 0.0:
            return False
        fired = snapshot.partial_tp_fired_levels or set()
        remaining = abs(position_amt)
        now = time.time()
        cumulative_closed = 0.0  # 같은 tick 내 누적 청산량 추적
        for idx, level in enumerate(levels):
            # pnl_pct가 명시적으로 있으면 그것을 사용 (% 단위)
            # r 키는 risk-reward ratio로 해석: 실제 ROI = r × (stop_distance × leverage × 100%)
            # 예: r=0.7, stop=0.75%, leverage=40x → ROI target = 0.7 × 0.75% × 40 × 100% = 21%
            if "pnl_pct" in level:
                level_pct = self._normalize_pct_value(float(level["pnl_pct"]))
            elif "r" in level:
                r_val = float(level["r"])
                # snapshot에서 진입가·스탑가·레버 조회 → R-multiple 기반 실제 ROI 계산
                _ep = float(getattr(snapshot, "entry_price", 0.0) or 0.0)
                _sp = float(getattr(snapshot, "stop_loss_px", 0.0) or 0.0)
                _lev = float(getattr(snapshot, "leverage", 1.0) or 1.0)
                if _ep > 0 and _sp > 0 and _lev > 0:
                    _stop_dist_pct = abs(_ep - _sp) / _ep   # 상대 stop distance
                    level_pct = r_val * _stop_dist_pct * _lev * 100.0
                else:
                    # 데이터 없으면 기존 방식 fallback (r을 ROI%로)
                    level_pct = self._normalize_pct_value(r_val)
            else:
                level_pct = 0.0
            # 수수료 손익분기 ROI 계산 후 level_pct와 합산해 최소 기준 결정
            _fee_min = self._fee_break_even_roi_pct(
                snapshot.entry_price, snapshot.quantity, snapshot.leverage)
            _effective_level = max(level_pct, _fee_min)  # 수수료를 최소한 커버해야 부분청산
            if roi_percent < _effective_level:
                break
            if idx in fired:
                continue
            close_frac = float(level.get("close_frac", 1.0) or 0.0)
            close_frac = min(max(close_frac, 0.0), 1.0)
            if close_frac <= 0.0:
                continue
            # 이미 이번 tick에 청산된 수량 제외한 실제 잔여량으로 계산
            effective_remaining = max(0.0, remaining - cumulative_closed)
            if effective_remaining <= 0.0:
                break
            qty_to_close = effective_remaining * close_frac
            if qty_to_close <= 0.0:
                continue
            signed_qty = math.copysign(qty_to_close, position_amt)
            result = await self._force_close_position(
                symbol,
                signed_qty,
                roi_percent,
                trigger=f"PARTIAL_TP_{idx + 1}",
                exit_reason=self.EXIT_REASON_TAKE_PROFIT,
                note="partial_tp",
            )
            if not result:
                return False
            fired.add(idx)
            snapshot.partial_tp_fired_levels = fired
            snapshot.last_tp_ts = now
            cumulative_closed += qty_to_close  # 이번 tick 누적 청산량 갱신
            remaining_after = max(0.0, remaining - cumulative_closed)
            snapshot.quantity = remaining_after
            self._append_log(f"[PARTIAL_TP] {symbol} level={idx + 1} roi={roi_percent:.2f}% close={close_frac * 100:.1f}%")

            # ── Breakeven Stop: 첫 TP 발동 시 스탑을 손익분기점으로 이동 ──
            if idx == 0 and bool(getattr(self.config, "breakeven_stop_enabled", True)):
                entry_price = snapshot.entry_price
                # [PATCH-10] 브레이크이븐 수수료: 진입(taker) + 청산(maker or taker) 실비용 반영
                _entry_fee = float(getattr(self.config, "taker_fee_pct", 0.0005))  # 진입은 taker
                _exit_fee = float(getattr(self.config, "maker_fee_pct", 0.0002))   # 청산은 maker 우선
                buffer = float(getattr(self.config, "breakeven_buffer_pct", 0.001))
                total_cost_rate = _entry_fee + _exit_fee + buffer
                direction_pos = "LONG" if position_amt > 0 else "SHORT"
                if direction_pos == "LONG":
                    be_stop = entry_price * (1.0 + total_cost_rate)
                    current_stop = snapshot.stop_loss_px or 0.0
                    if be_stop > current_stop:
                        snapshot = update_snapshot(snapshot, stop_loss_px=be_stop)
                        self._append_log(
                            self._ko(f"[BE_STOP] {symbol} 스탑 손익분기 이동: {current_stop:.4f} → {be_stop:.4f}", f"[BE_STOP] {symbol} stop moved to breakeven: {current_stop:.4f} → {be_stop:.4f}")
                        )
                else:
                    be_stop = entry_price * (1.0 - total_cost_rate)
                    current_stop = snapshot.stop_loss_px or float("inf")
                    if be_stop < current_stop:
                        snapshot = update_snapshot(snapshot, stop_loss_px=be_stop)
                        self._append_log(
                            self._ko(f"[BE_STOP] {symbol} 스탑 손익분기 이동: {current_stop:.4f} → {be_stop:.4f}", f"[BE_STOP] {symbol} stop moved to breakeven: {current_stop:.4f} → {be_stop:.4f}")
                        )

            if remaining_after <= 0.0 or close_frac >= 1.0:
                self.position_snapshots.pop(symbol, None)
            else:
                self.position_snapshots[symbol] = snapshot
            return True
        return False

    def _fee_break_even_roi_pct(self, entry_price: float, qty: float, leverage: float) -> float:
        """진입+청산 수수료 + 슬리피지 + 펀딩 추정을 커버하는 최소 ROI % (증거금 기준).
        [PATCH-18] taker×2만 → 슬리피지/펀딩 추정 포함 현실적 BEP."""
        maker = float(getattr(self.config, "maker_fee_pct", 0.0002) or 0.0002)
        taker = float(getattr(self.config, "taker_fee_pct", 0.0005) or 0.0005)
        notional = entry_price * qty if entry_price > 0 and qty > 0 else 0.0
        if notional <= 0 or leverage <= 0:
            return 0.0
        margin = notional / leverage
        # 진입: maker-first 활성 시 maker, 아니면 taker
        _use_maker = bool(getattr(self.config, "maker_first_enabled", False)) and \
                     not bool(getattr(self.config, "maker_entry_use_taker", True))
        entry_fee = notional * (maker if _use_maker else taker)
        exit_fee = notional * taker  # 청산은 보수적으로 taker 가정
        # 슬리피지 추정: 편도 ~5bps
        slippage_est = notional * 0.0005
        # 펀딩 추정: 8시간 기본율 0.01%, 평균 보유 ~2시간 가정
        funding_est = notional * 0.0001 * 0.25
        total_cost = entry_fee + exit_fee + slippage_est + funding_est
        return (total_cost / margin) * 100.0   # % 단위

    async def _maybe_execute_trailing_exit(self, symbol: str, snapshot: PositionSnapshot, pos: dict, roi_percent: float, price: float, direction: str) -> bool:
        if not getattr(self.config, "enable_atr_trailing_stop", False):
            return False
        # [PATCH-18] ratio 기반으로 통일: 0.008→0.8%, 0.8→자동보정→0.8%
        _raw_activate = float(getattr(self.config, "trail_activate_pnl_pct", 0.008) or 0.008)
        activate_ratio = self._normalize_ratio_value(_raw_activate, clamp_max=0.10)  # max 10%
        activate_pct = activate_ratio * 100.0  # ratio → percent
        # 수수료 손익분기 이하이면 trailing 활성화 자체를 막음
        _fee_min = self._fee_break_even_roi_pct(
            snapshot.entry_price, snapshot.quantity, snapshot.leverage)
        _effective_activate = max(activate_pct, _fee_min * 1.2)  # 수수료의 120% 이상일 때만 활성화
        if roi_percent < _effective_activate:
            return False
        trail_mult = max(float(getattr(self.config, "trail_atr_mult", 1.7)), 0.1)  # [PATCH-14] 3.0→1.7 config 정렬
        interval = max(int(getattr(self.config, "trail_recalc_interval_sec", 5)), 1)
        now = time.time()
        last_calc = snapshot.trail_last_update_ts or 0.0
        if snapshot.trail_stop_price is None or (now - last_calc) >= interval:
            # 실시간 ATR 재계산: price_history 기반 > atr_at_entry > fallback 순
            _ph = self._price_history.get(snapshot.symbol)
            _period = int(getattr(self.config, "trail_atr_period", 22))
            _live_atr = 0.0
            if _ph and len(_ph) >= _period + 2:
                _prices = [px for _, px in list(_ph)[-(_period + 2):]]
                _ranges = [abs(_prices[i] - _prices[i-1]) for i in range(1, len(_prices))]
                _live_atr = sum(_ranges) / len(_ranges) if _ranges else 0.0
            atr_value = _live_atr if _live_atr > 0 else (snapshot.atr_at_entry or (abs(price) * 0.005) or 0.001)
            if atr_value <= 0:
                atr_value = max(abs(price) * 0.005, 0.001)
            direction = direction.upper()
            updated = False
            if direction == "LONG":
                ref = snapshot.highest_price_since_entry or price
                stop = ref - atr_value * trail_mult
                if snapshot.trail_stop_price is None or stop > snapshot.trail_stop_price:
                    snapshot.trail_stop_price = stop
                    snapshot.trail_ref_px = ref
                    updated = True
            else:
                ref = snapshot.lowest_price_since_entry or price
                stop = ref + atr_value * trail_mult
                if snapshot.trail_stop_price is None or stop < snapshot.trail_stop_price:
                    snapshot.trail_stop_price = stop
                    snapshot.trail_ref_px = ref
                    updated = True
            if updated:
                snapshot.trail_active = True
                snapshot.trail_last_update_ts = now
                self._append_log(f"[TRAIL_UPDATE] {symbol} stop={snapshot.trail_stop_price:.4f}")
        if snapshot.trail_stop_price is None:
            return False
        hit = False
        if direction.upper() == "LONG" and price <= snapshot.trail_stop_price:
            hit = True
        elif direction.upper() == "SHORT" and price >= snapshot.trail_stop_price:
            hit = True
        if hit:
            await self._force_close_position(
                symbol,
                float(pos.get("positionAmt", 0.0)),
                roi_percent,
                trigger="TRAIL_EXIT",
                exit_reason=self.EXIT_REASON_TAKE_PROFIT,
                note="trail_exit",
            )
            self._append_log(f"[TRAIL_EXIT] {symbol} roi={roi_percent:.2f}%")
            self.position_snapshots.pop(symbol, None)
            return True
        return False

    async def _maybe_execute_progress_stop(self, symbol: str, snapshot: PositionSnapshot, pos: dict, roi_percent: float) -> bool:
        if not getattr(self.config, "enable_progress_stop", False):
            return False
        min_pnl = self._normalize_pct_value(getattr(self.config, "progress_stop_min_pnl_pct", 0.0))
        if roi_percent < min_pnl:
            return False
        try:
            position_amt = float(pos.get("positionAmt", 0.0))
        except (TypeError, ValueError):
            return False
        if position_amt == 0.0:
            return False
        last_update = snapshot.mfe_last_update_ts or snapshot.opened_at or time.time()
        stale_sec = max(int(getattr(self.config, "progress_stop_no_new_high_sec", 1800)), 0)
        now = time.time()
        if (now - last_update) < stale_sec:
            return False
        drawdown = self._normalize_ratio_value(getattr(self.config, "progress_stop_drawdown_from_mfe", 0.15))
        if snapshot.mfe_pnl_pct <= 0.0:
            return False
        threshold = snapshot.mfe_pnl_pct * (1 - drawdown)
        if roi_percent > threshold:
            return False
        action = str(getattr(self.config, "progress_stop_action", "partial_or_full")).lower()
        close_qty = abs(position_amt)
        partial = False
        if action != "full":
            close_qty *= 0.5
            partial = True
        if close_qty <= 0.0:
            return False
        signed_qty = math.copysign(close_qty, position_amt)
        await self._force_close_position(
            symbol,
            signed_qty,
            roi_percent,
            trigger="PROGRESS_STOP",
            exit_reason=self.EXIT_REASON_SIGNAL_DECAY,
            note="progress_stop",
        )
        self._append_log(f"[PROGRESS_STOP] {symbol} roi={roi_percent:.2f}% mfe={snapshot.mfe_pnl_pct:.2f}% action={'partial' if partial else 'full'}")
        snapshot.mfe_last_update_ts = now
        if partial:
            snapshot.quantity = max(0.0, abs(position_amt) - close_qty)
            if snapshot.quantity <= 0.0:
                self.position_snapshots.pop(symbol, None)
            else:
                self.position_snapshots[symbol] = snapshot
        else:
            self.position_snapshots.pop(symbol, None)
        return True

    async def _evaluate_profit_exit_layers(self, symbol_snaps: List[SymbolSnapshot], positions: List[dict]):
        if not getattr(self.config, "enable_profit_exit_layer", False):
            return
        if not positions:
            return
        snap_map = {snap.symbol: snap for snap in symbol_snaps}
        pos_map: Dict[str, dict] = {}
        for pos in positions:
            symbol = pos.get("symbol")
            if not symbol:
                continue
            try:
                amt = float(pos.get("positionAmt", 0.0))
            except (TypeError, ValueError):
                continue
            if amt == 0.0:
                continue
            pos_map[symbol] = pos
        for symbol, snapshot in list(self.position_snapshots.items()):
            pos = pos_map.get(symbol)
            snap_info = snap_map.get(symbol)
            if not pos or not snap_info:
                continue
            try:
                position_amt = float(pos.get("positionAmt", 0.0))
            except (TypeError, ValueError):
                continue
            if position_amt == 0.0:
                continue
            direction = "LONG" if position_amt > 0 else "SHORT"
            price = float(snap_info.mark_price or snap_info.price or 0.0)
            if price <= 0:
                continue
            roi_percent = self._position_roi_percent(pos)
            self._update_profit_tracking(snapshot, direction, price, roi_percent)
            self.position_snapshots[symbol] = snapshot
            triggered = await self._maybe_execute_partial_tp(symbol, snapshot, pos, roi_percent)
            if triggered:
                continue
            triggered = await self._maybe_execute_trailing_exit(symbol, snapshot, pos, roi_percent, price, direction)
            if triggered:
                continue
            await self._maybe_execute_progress_stop(symbol, snapshot, pos, roi_percent)

    def _notify(self, level: str, message: str):
        message = self._mask(message)  # B: redact sensitive strings
        self._trim_notification_log()
        try:
            with open(self.notification_path, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}|{level}|{message}\n")
        except Exception:
            logger.exception("Failed to write notification")

    def _ai_event(self, category: str, message: str):
        """AI 어시스턴트용 구조화 이벤트 발행. [CATEGORY] 접두사로 분류 용이."""
        self._notify("WATCH", f"[{category}] {message}")

    def _mark_gap_ratio(self, snap: SymbolSnapshot) -> float:
        mark = float(snap.mark_price or snap.price or 0.0)
        last = float(snap.price or snap.mark_price or 0.0)
        if mark <= 0 or last <= 0:
            return 0.0
        return abs(mark - last) / max(mark, last)

    async def fetch_symbol_snapshots(self) -> List[SymbolSnapshot]:
        try:
            tickers = await self.client.futures_ticker()
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, "futures_ticker")
            return []
        mark_prices: Dict[str, float] = {}
        try:
            marks = await self.client.futures_mark_price()
            if isinstance(marks, list):
                for item in marks:
                    try:
                        mark_prices[item.get("symbol")] = float(item.get("markPrice", 0.0))
                    except (TypeError, ValueError):
                        continue
            elif isinstance(marks, dict) and marks.get("symbol"):
                try:
                    mark_prices[marks.get("symbol")] = float(marks.get("markPrice", 0.0))
                except (TypeError, ValueError):
                    pass
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, "futures_mark_price")
        tradable_map: Dict[str, bool] = {}
        try:
            info = await self._ensure_exchange_info()
            entries = info.get("symbols", []) if isinstance(info, dict) else []
            tradable_map = {
                entry.get("symbol"): str(entry.get("status", "")).upper() == "TRADING"
                for entry in entries
                if entry.get("symbol")
            }
        except Exception as exc:
            logger.debug("Failed to build tradable map: %s", exc)
        snapshots: List[SymbolSnapshot] = []
        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            try:
                last_price = float(ticker.get("lastPrice", 0))
                volume = float(ticker.get("volume", 0))
                quote_volume = float(ticker.get("quoteVolume", 0))
                price_change_pct = float(ticker.get("priceChangePercent", 0)) / 100.0
                volatility = abs(price_change_pct)
                high_24h = float(ticker.get("highPrice", last_price))
                low_24h = float(ticker.get("lowPrice", last_price))
            except (TypeError, ValueError):
                continue
            mark_price = mark_prices.get(symbol, last_price)
            bid = ask = 0.0
            mid = float(last_price) if last_price else 0.0
            spread_bps = 0.0
            if hasattr(self, "stream") and self.stream:
                try:
                    getter = getattr(self.stream, "get_bid_ask", None) or getattr(self.stream, "get_best_bid_ask", None)
                    if getter:
                        bid, ask = getter(symbol)  # expected (bid, ask)
                    else:
                        bid = float(getattr(self.stream, "get_bid_price", lambda _s: 0.0)(symbol))
                        ask = float(getattr(self.stream, "get_ask_price", lambda _s: 0.0)(symbol))
                except Exception:
                    bid = ask = 0.0
            if bid and ask and bid > 0 and ask > 0 and ask >= bid:
                mid = (bid + ask) / 2.0
                spread_bps = (ask - bid) / mid * 10000.0 if mid else 0.0
            self._record_price(symbol, last_price)
            self._record_notional(symbol, quote_volume)
            snapshots.append(
                SymbolSnapshot(
                    symbol=symbol,
                    volume_24h=volume,
                    notional_24h=quote_volume,
                    volatility=volatility,
                    momentum_pct=price_change_pct,
                    atr=0.0,
                    price=last_price,
                    mark_price=mark_price,
                    bid=bid,
                    ask=ask,
                    mid=mid,
                    spread_bps=spread_bps,
                    tradable=tradable_map.get(symbol, True),
                    high_24h=high_24h,
                    low_24h=low_24h,
                    momentum_5m=self._recent_return_pct(symbol, 300),  # 5분 단기 return
                )
            )
        return snapshots

    def filter_symbols(self, snapshots: List[SymbolSnapshot]) -> List[SymbolSnapshot]:
        top_n = max(1, int(getattr(self.config, "top_n", self.config.watch_limit or 1)))
        snaps = sorted(snapshots, key=lambda s: s.notional_24h, reverse=True)[:top_n]
        stats: Dict[str, int] = {
            "input_total": len(snapshots),
            "top_n": len(snaps),
            "passed": 0,
        }
        filtered: List[SymbolSnapshot] = []
        now = time.time()
        rv_ratio = self._rv_ratio()
        quality_min = float(getattr(self.config, "quality_min_score", 0.0) or 0.0)
        for snap in snaps:
            self._increment_flow("evaluated_total")
            reason = None
            if not snap.tradable:
                reason = "FILTER_REJECT_STATUS"
            elif snap.symbol in self._symbol_blocked:
                reason = "FILTER_REJECT_BLOCKED"
            elif snap.symbol in self._spike_blocked_until and self._spike_blocked_until[snap.symbol] > now:
                reason = "FILTER_REJECT_SPIKE"
            elif float(getattr(snap, 'spread_bps', 0.0) or 0.0) > float(getattr(self.config, 'max_spread_bps', 1e9) or 1e9):
                reason = "FILTER_REJECT_SPREAD"
            elif self._mark_gap_ratio(snap) * 10000 > float(getattr(self.config, 'max_mark_gap_bps', 1e9) or 1e9):
                reason = "FILTER_REJECT_MARK_GAP"
            elif snap.volatility < self.config.volatility_min:
                reason = "FILTER_REJECT_VOL"
            else:
                quality_score = self._quality_score(snap, rv_ratio)
                if quality_score < quality_min:
                    reason = "FILTER_REJECT_QUALITY"
            if reason:
                stats[reason] = stats.get(reason, 0) + 1
                logger.info(
                    "%s %s vol=%.4f mom_pct=%.4f thresh_vol=%.4f",
                    reason,
                    snap.symbol,
                    snap.volatility,
                    snap.momentum_pct,
                    self.config.volatility_min,
                )
                self._notify(
                    "WATCH",
                    f"FILTER_REJECT {snap.symbol} {reason} "
                    f"vol={snap.volatility:.4f} mom={snap.momentum_pct:.4f} "
                    f"thresh_vol={self.config.volatility_min:.4f}",
                )
                continue
            filtered.append(snap)
        stats["passed"] = len(filtered)
        # [PATCH-9] 필터 파이프라인 상세 계측 로그
        _fs_msg = (
            f"Filter summary: input={stats['input_total']} topN={stats['top_n']} "
            f"passed={stats['passed']} status={stats.get('FILTER_REJECT_STATUS',0)} "
            f"blocked={stats.get('FILTER_REJECT_BLOCKED',0)} spike={stats.get('FILTER_REJECT_SPIKE',0)} "
            f"spread={stats.get('FILTER_REJECT_SPREAD',0)} mark_gap={stats.get('FILTER_REJECT_MARK_GAP',0)} "
            f"vol_fail={stats.get('FILTER_REJECT_VOL',0)} quality={stats.get('FILTER_REJECT_QUALITY',0)}"
        )
        # 파이프라인 통과율 추적 (진단 모드)
        _pass_rate = (stats['passed'] / max(stats['top_n'], 1)) * 100
        if hasattr(self, '_filter_pipeline_stats'):
            self._filter_pipeline_stats.append({
                'ts': time.time(), 'input': stats['input_total'], 'top_n': stats['top_n'],
                'passed': stats['passed'], 'pass_rate': _pass_rate,
                **{k: v for k, v in stats.items() if k.startswith('FILTER_')}
            })
            # 최근 100개만 유지
            if len(self._filter_pipeline_stats) > 100:
                self._filter_pipeline_stats = self._filter_pipeline_stats[-100:]
        else:
            self._filter_pipeline_stats = []
        logger.info(_fs_msg)
        self._notify("WATCH", _fs_msg)
        if stats["passed"] == 0:
            logger.warning(
                "No tradable symbols after filters (vol>=%.4f)",
                self.config.volatility_min,
            )
        self._record_stat("evaluated", stats["top_n"] or len(snaps) or 1)
        self._record_stat("passed", stats["passed"])
        if not filtered:
            return []
        current_symbols = [snap.symbol for snap in filtered]
        if current_symbols != self._last_cycle_symbols:
            self._last_cycle_symbols = current_symbols
            self._cycle_index = 0
            self._skip_counts = {symbol: 0 for symbol in current_symbols}
            # 새 심볼만 skip count 초기화, 기존 block은 유지 (MIN_NOTIONAL, 정밀도 차단 등)
            # 단, 신규 등장한 심볼만 블락에서 제외
            new_symbols = set(current_symbols) - set(self._symbol_blocked)
            removed = self._symbol_blocked - set(current_symbols)
            self._symbol_blocked -= removed  # watchlist에서 사라진 심볼 block만 제거
        # timed_skip_block: 30분 TTL 차단 (영구 차단 대체)
        _now = time.time()
        if hasattr(self, "_timed_skip_block"):
            # 만료된 차단 해제
            self._timed_skip_block = {sym: exp for sym, exp in self._timed_skip_block.items() if exp > _now}
        _temp_blocked = getattr(self, "_timed_skip_block", {})
        active_snaps = [snap for snap in filtered if snap.symbol not in self._symbol_blocked and snap.symbol not in _temp_blocked]
        if not active_snaps:
            active_snaps = filtered
        rotate = self._cycle_index % len(active_snaps)
        ordered = active_snaps[rotate:] + active_snaps[:rotate]
        self._cycle_index = (self._cycle_index + 1) % len(active_snaps)
        return self._apply_watchlist_diversity(ordered)

    def _apply_watchlist_diversity(self, snaps: List[SymbolSnapshot]) -> List[SymbolSnapshot]:
        limit = int(max(0, getattr(self.config, "watch_limit", 10)))
        if limit == 0 or not snaps:
            return []
        diversify = bool(getattr(self.config, "diversify_watchlist", False))
        if not diversify or len(snaps) <= 2:
            return snaps[:limit]
        sorted_vols = sorted(s.volatility for s in snaps)
        if not sorted_vols:
            return snaps[:limit]
        def _quantile(vols: List[float], q: float) -> float:
            if not vols:
                return 0.0
            idx = max(0, min(len(vols) - 1, int(math.floor(q * (len(vols) - 1)))))
            return vols[idx]
        low_cut = _quantile(sorted_vols, 0.33)
        high_cut = _quantile(sorted_vols, 0.66)
        buckets = {0: [], 1: [], 2: []}
        for snap in snaps:
            if snap.volatility <= low_cut:
                buckets[0].append(snap)
            elif snap.volatility <= high_cut:
                buckets[1].append(snap)
            else:
                buckets[2].append(snap)
        diversified: List[SymbolSnapshot] = []
        while len(diversified) < limit and any(buckets.values()):
            for idx in (0, 1, 2):
                if buckets[idx]:
                    diversified.append(buckets[idx].pop(0))
                    if len(diversified) >= limit:
                        break
        if len(diversified) < limit:
            for snap in snaps:
                if snap not in diversified:
                    diversified.append(snap)
                if len(diversified) >= limit:
                    break
        logger.info(
            "Diversified watchlist (limit=%d, diversify=%s, low=%d mid=%d high=%d)",
            limit,
            diversify,
            len([s for s in diversified if s.volatility <= low_cut]),
            len([s for s in diversified if low_cut < s.volatility <= high_cut]),
            len([s for s in diversified if s.volatility > high_cut]),
        )
        return diversified[:limit]

    def _momentum_threshold(self, value: float) -> float:
        if value >= 0:
            return max(abs(self.config.momentum_min_long), 0.0001)
        return max(abs(self.config.momentum_min_short), 0.0001)

    def _momentum_pass(self, value: float) -> bool:
        if value >= 0:
            return value >= self.config.momentum_min_long
        return value <= self.config.momentum_min_short

    def evaluate_signal(self, snap: SymbolSnapshot) -> Tuple[Optional[SignalDecision], Optional[str]]:
        self._record_stat("signals_evaluated", 1)
        # [PATCH-9] 파이프라인 계측: 각 단계 통과/차단 카운트
        self._increment_flow("signal_eval_total")

        # ── 변동성 필터 (하한) ──────────────────────────────────────────────────
        if snap.volatility < self.config.volatility_min:
            return None, self._ko(f"변동성이 낮습니다 ({snap.volatility:.4f} < {self.config.volatility_min:.4f})", f"Volatility too low ({snap.volatility:.4f} < {self.config.volatility_min:.4f})")

        # ═══════════════════════════════════════════════════════════
        # 🆕 v3.3: ATR 상한 필터 (과도한 변동성 차단)
        # ═══════════════════════════════════════════════════════════
        # 변동성이 너무 높으면 손절 폭이 커져서 큰 손실 발생
        # ATR 상한 = volatility_min × atr_max_mult
        atr_max_mult = getattr(self.config, "atr_max_mult", 3.0)
        atr_max = self.config.volatility_min * atr_max_mult
        
        if snap.atr > atr_max:
            return None, self._ko(
                f"변동성이 과도합니다 (ATR {snap.atr:.4f} > {atr_max:.4f})",
                f"Volatility too high (ATR {snap.atr:.4f} > {atr_max:.4f})"
            )

        # ── 기본 모멘텀 필터 ────────────────────────────────────────────
        momentum_threshold = self._momentum_threshold(snap.momentum_pct)
        if not self._momentum_pass(snap.momentum_pct):
            return None, self._ko(f"모멘텀이 약합니다 ({snap.momentum_pct:.4f} < {momentum_threshold:.4f})", f"Momentum too weak ({snap.momentum_pct:.4f} < {momentum_threshold:.4f})")

        # 방향 결정: 5분 단기 return 우선 (24h 기반은 단기 역방향 진입 허용하는 문제)
        # 5분 데이터가 없으면 24h 기반 fallback
        _short_mom = float(getattr(snap, "momentum_5m", 0.0) or 0.0)
        if abs(_short_mom) > 1e-6:
            direction = "LONG" if _short_mom > 0 else "SHORT"
            # 방향 결정에 사용한 모멘텀 값으로 필터도 재검사
            # (24h pass + 5m 역방향 = threshold는 통과했지만 실제 방향 상반)
            _dir_momentum = _short_mom
        else:
            direction = "LONG" if snap.momentum_pct > 0 else "SHORT"
            _dir_momentum = snap.momentum_pct
        # 방향 기반 모멘텀 재필터: 사용할 방향의 모멘텀 크기가 충분한지 재확인
        if direction == "LONG" and _dir_momentum < self.config.momentum_min_long:
            return None, self._ko(
                f"5m 방향 기반 모멘텀 부족 (long: {_dir_momentum:.4f} < {self.config.momentum_min_long:.4f})",
                f"5m direction momentum insufficient (long: {_dir_momentum:.4f} < {self.config.momentum_min_long:.4f})",
            )
        if direction == "SHORT" and _dir_momentum > self.config.momentum_min_short:
            return None, self._ko(
                f"5m 방향 기반 모멘텀 부족 (short: {_dir_momentum:.4f} > {self.config.momentum_min_short:.4f})",
                f"5m direction momentum insufficient (short: {_dir_momentum:.4f} > {self.config.momentum_min_short:.4f})",
            )

        # ── 단기 EMA 방향 충돌 필터 ─────────────────────────────────────────────
        # [PATCH-9] chop에서는 완전 차단 유지, trend_down에서는 점수 페널티로 변경
        _ema_conflict_penalty = 0.0
        if bool(getattr(self.config, "short_ema_conflict_filter", True)):
            _conflict_tfs = [60, 300]  # 1분, 5분
            _ema_p = int(getattr(self.config, "mtf_ema_period", 21))
            _min_slope = float(getattr(self.config, "mtf_min_slope_bps", 2.0))
            _against_count = 0
            for _tf in _conflict_tfs:
                try:
                    _slope = self._mtf_ema_slope_bps(snap.symbol, _tf, _ema_p)
                except Exception:
                    _slope = float("nan")
                if _slope != _slope:  # NaN = 데이터 부족 → 통과
                    continue
                if direction == "LONG" and _slope < -_min_slope:
                    _against_count += 1
                elif direction == "SHORT" and _slope > _min_slope:
                    _against_count += 1
            if _against_count >= len(_conflict_tfs):
                # [PATCH-9] 레짐 확인: trend 레짐에서는 차단 대신 점수 페널티
                _cur_regime = "chop"
                if self.auto_tuner and bool(getattr(self.config, "auto_tune_enabled", True)):
                    _cur_regime = getattr(self.auto_tuner.state.hysteresis, "current_regime", "chop")
                if _cur_regime in ("trend_up", "trend_down"):
                    # trend 레짐: 완전 차단 대신 -0.20 점수 페널티 (composite에서 감산)
                    _ema_conflict_penalty = 0.20
                    logger.info("EMA conflict in %s regime → penalty %.2f (not blocked)", _cur_regime, _ema_conflict_penalty)
                else:
                    # chop 레짐: 기존대로 완전 차단
                    return None, self._ko(
                        f"단기 EMA 방향 충돌: 24h momentum={direction} 이지만 단기 EMA slope 역방향",
                        f"Short-term EMA conflict: 24h momentum={direction} but short-term EMA is against it",
                    )

        # ── 모멘텀 과열 필터 (volatility 기반 절대 상한) ────────────────────────────
        # momentum_threshold 상대 배수 방식은 AutoTune이 threshold를 낮출 때 오히려
        # 과열 임계값도 낮아져 정상 신호를 모두 차단하는 역효과가 발생.
        # 대신 volatility의 N배를 절대 상한선으로 사용.
        if bool(getattr(self.config, "rsi_filter_enabled", False)):
            overheat_cap = snap.volatility * float(getattr(self.config, "overheat_volatility_mult", 1.5))
            if abs(snap.momentum_pct) > overheat_cap:
                return None, (
                    self._ko(
                        f"모멘텀 과열 ({abs(snap.momentum_pct):.4f} > volatility×1.5={overheat_cap:.4f}) — 추세 끝자락 가능성",
                        f"Momentum overheated ({abs(snap.momentum_pct):.4f} > volatility×1.5={overheat_cap:.4f}) — possible trend exhaustion"
                    )
                )
            # RSI overbought/oversold 차단 (구현됐지만 미호출 → 연결)
            rsi_val = self._compute_rsi(snap.symbol, int(getattr(self.config, "rsi_period", 14)))
            _ob = float(getattr(self.config, "rsi_overbought", 75.0))
            _os = float(getattr(self.config, "rsi_oversold", 25.0))
            if direction == "LONG" and rsi_val >= _ob:
                return None, self._ko(
                    f"RSI 과매수 차단 (rsi={rsi_val:.1f} >= {_ob})",
                    f"RSI overbought block (rsi={rsi_val:.1f} >= {_ob})",
                )
            if direction == "SHORT" and rsi_val <= _os:
                return None, self._ko(
                    f"RSI 과매도 차단 (rsi={rsi_val:.1f} <= {_os})",
                    f"RSI oversold block (rsi={rsi_val:.1f} <= {_os})",
                )

        # ── 레짐별 전략 분리 ────────────────────────────────────────────
        regime = "chop"
        if self.auto_tuner and bool(getattr(self.config, "auto_tune_enabled", True)):
            regime = getattr(self.auto_tuner.state.hysteresis, "current_regime", "chop")
        else:
            # auto_tune 미사용 시 단기 EMA slope로 직접 레짐 추정
            _ema_p = int(getattr(self.config, "mtf_ema_period", 21))
            _slope_sum = 0.0
            _n = 0
            for _tf in [60, 300, 900]:  # 1m, 5m, 15m
                try:
                    _s = self._mtf_ema_slope_bps(snap.symbol, _tf, _ema_p)
                    if _s == _s:  # not NaN
                        _slope_sum += _s
                        _n += 1
                except Exception:
                    pass
            if _n >= 2:
                _avg_slope = _slope_sum / _n
                # slope_thresh = min_slope (2.0 bps 기본) 그대로 사용
                # 기존 *2.0 배수는 너무 높아 거의 항상 CHOP으로 판정됨
                _slope_thresh = float(getattr(self.config, "mtf_min_slope_bps", 2.0))
                if _avg_slope > _slope_thresh:
                    regime = "trend_up"
                elif _avg_slope < -_slope_thresh:
                    regime = "trend_down"

        self._last_known_regime = regime  # [PATCH-17] SL 등에서 재활용

        if regime == "chop":
            # CHOP 구간: 임계값 배수 강화로 허수 신호 차단 (config로 조정 가능)
            chop_mult = float(getattr(self.config, "chop_momentum_multiplier", 1.3))
            if abs(snap.momentum_pct) < momentum_threshold * chop_mult:
                return None, self._ko(f"CHOP 레짐 강화 필터 ({abs(snap.momentum_pct):.4f} < {momentum_threshold * chop_mult:.4f})", f"CHOP regime strict filter ({abs(snap.momentum_pct):.4f} < {momentum_threshold * chop_mult:.4f})")
            # CHOP 구간에서 단기 EMA가 24h 방향과 반대이면 단기 방향을 우선
            if bool(getattr(self.config, "chop_use_short_ema_direction", True)):
                _ema_p = int(getattr(self.config, "mtf_ema_period", 21))
                _slope_sum = 0.0
                _slope_valid = 0
                for _tf in [60, 300]:
                    try:
                        _s = self._mtf_ema_slope_bps(snap.symbol, _tf, _ema_p)
                        if _s == _s:  # not NaN
                            _slope_sum += _s
                            _slope_valid += 1
                    except Exception:
                        pass
                if _slope_valid > 0:
                    _short_ema_dir = "LONG" if _slope_sum > 0 else "SHORT"
                    # 단기EMA가 방향과 반대이고 기울기가 유의미한 경우에만 차단
                    # (|slope_sum| > min_slope * valid_count: 약한 역방향은 허용)
                    _sig_threshold = float(getattr(self.config, "mtf_min_slope_bps", 2.0)) * _slope_valid
                    if _short_ema_dir != direction and abs(_slope_sum) > _sig_threshold:
                        return None, self._ko(
                            f"CHOP 레짐 단기EMA 방향 우선: 24h={direction} vs 단기EMA={_short_ema_dir} (slope={_slope_sum:.1f}bps) → 차단",
                            f"CHOP regime short-EMA direction override: 24h={direction} vs short-EMA={_short_ema_dir} (slope={_slope_sum:.1f}bps) → blocked",
                        )
        elif regime == "trend_up" and direction == "SHORT":
            return None, self._ko("trend_up 레짐에서 SHORT 진입 차단", "SHORT entry blocked in trend_up regime")
        elif regime == "trend_down" and direction == "LONG":
            return None, self._ko("trend_down 레짐에서 LONG 진입 차단", "LONG entry blocked in trend_down regime")

        # ── 복합 신호 스코어 ─────────────────────────────────────────────
        if bool(getattr(self.config, "composite_signal_enabled", True)):
            # 모멘텀 점수
            momentum_score = min(abs(snap.momentum_pct) / max(momentum_threshold, 1e-9), 3.0)
            # 거래량 서지 점수 (단기 틱 속도 기반)
            volume_score = self._volume_surge_score(snap.symbol)
            volume_score = min(volume_score / 1.5, 2.0)  # 1.5배 서지면 만점
            # [PATCH-13] 볼륨 플로어 0.5 제거 → 0.0 (거래량 없는 심볼에 허위 점수 부여 방지)
            # 거래량이 실제로 없으면 composite 점수가 자연스럽게 낮아져 진입 차단됨
            # MTF 정렬 점수 (0.0~1.0)
            mtf_score = self._mtf_alignment_score(snap.symbol, direction)
            # 가중 합산
            w_m = float(getattr(self.config, "composite_weights_momentum", 0.50))
            w_v = float(getattr(self.config, "composite_weights_volume", 0.30))
            w_t = float(getattr(self.config, "composite_weights_mtf", 0.20))
            composite = momentum_score * w_m + volume_score * w_v + mtf_score * w_t
            # [PATCH-9] EMA 충돌 페널티 적용 (trend 레짐에서 완전차단 대신 감점)
            composite -= _ema_conflict_penalty
            # [PATCH-10] chop 레짐 RSI 소프트 스코어링 (mean-reversion 보조)
            if regime == "chop" and bool(getattr(self.config, "rsi_chop_soft_scoring", False)):
                _rsi_val = self._compute_rsi(snap.symbol, int(getattr(self.config, "rsi_period", 14)))
                _rsi_bonus = float(getattr(self.config, "rsi_chop_bonus", 0.10))
                _ob = float(getattr(self.config, "rsi_overbought", 75.0))
                _os = float(getattr(self.config, "rsi_oversold", 25.0))
                if direction == "LONG" and _rsi_val <= _os:
                    composite += _rsi_bonus  # 과매도에서 롱 → 보너스
                elif direction == "SHORT" and _rsi_val >= _ob:
                    composite += _rsi_bonus  # 과매수에서 숏 → 보너스
            # [PATCH-11] 레짐 방향 바이어스 — 추세 방향과 일치하는 진입에 보너스/페널티
            if bool(getattr(self.config, "regime_direction_bias_enabled", False)):
                if regime == "trend_up":
                    if direction == "LONG":
                        composite += float(getattr(self.config, "regime_long_bonus_trend_up", 0.15))
                    else:
                        composite += float(getattr(self.config, "regime_short_penalty_trend_up", -0.10))
                elif regime == "trend_down":
                    if direction == "SHORT":
                        composite += float(getattr(self.config, "regime_short_bonus_trend_down", 0.15))
                    else:
                        composite += float(getattr(self.config, "regime_long_penalty_trend_down", -0.10))
            min_composite = float(getattr(self.config, "composite_min_score", 0.80))  # [PATCH-14] 0.72→0.80 config 정렬
            # [PATCH-16] auto_tune 꺼져있으면 regime 기반 조정 무시 (기본값 chop 고정이므로)
            if regime == "chop" and getattr(self.config, "auto_tune_enabled", True):
                min_composite = float(getattr(self.config, "chop_composite_min_score", 0.85))
            if composite < min_composite:
                return None, self._ko(f"복합 스코어 부족 ({composite:.2f} < {min_composite})", f"Composite score too low ({composite:.2f} < {min_composite})")
            strength = min(composite, 5.0)
        else:
            strength = min(abs(snap.momentum_pct) / momentum_threshold, 5.0)

        # ── 신경망 스코어 반영 [프리미엄 v3] ──────────────────────────────────
        _neural_enabled = bool(getattr(self.config, "neural_scorer_enabled", False))
        _mtf_1m = 0.0
        _mtf_5m = 0.0
        try:
            _ema_p = int(getattr(self.config, "mtf_ema_period", 21))
            _s1 = self._mtf_ema_slope_bps(snap.symbol, 60, _ema_p)
            _mtf_1m = _s1 if _s1 == _s1 else 0.0
            _s5 = self._mtf_ema_slope_bps(snap.symbol, 300, _ema_p)
            _mtf_5m = _s5 if _s5 == _s5 else 0.0
        except Exception:
            pass
        _funding = float(self._funding_cache.get(snap.symbol, {}).get("rate", 0.0))
        _vsurge = self._volume_surge_score(snap.symbol)
        try:
            _mtf_align = self._mtf_alignment_score(snap.symbol, direction)
        except Exception:
            _mtf_align = 0.5

        # ── spread_bps NULL 수정: 0이면 추정값 대체 ──
        _spread = float(getattr(snap, "spread_bps", 0.0) or 0.0)
        if _spread <= 0.0:
            _spread = float(getattr(self.config, "tca_spread_estimate_bps", 5.0))

        # ── v3 신규 피처 수집 ──
        _price = float(getattr(snap, "price", 0.0) or 0.0)
        _high_24h = float(getattr(snap, "high_24h", 0.0) or 0.0)
        _low_24h = float(getattr(snap, "low_24h", 0.0) or 0.0)
        _rsi_14 = self._compute_rsi(snap.symbol, 14)
        _entry_atr = self._estimate_entry_atr(snap, _price)
        # 변동성 레짐: 단기/장기 변동성 비율
        _vol_short = float(getattr(snap, "volatility", 0.0) or 0.0)
        _vol_long = max(float(getattr(self, "_last_atr_estimate", _vol_short)), 1e-8)
        _vol_regime_ratio = _vol_short / _vol_long if _vol_long > 1e-8 else 1.0
        _open_pos_count = len(self._open_symbols)

        _feat = build_feature_vector(
            momentum_5m=float(getattr(snap, "momentum_5m", 0.0) or 0.0),
            volatility=snap.volatility,
            volume_surge=_vsurge,
            mtf_slope_1m=_mtf_1m,
            mtf_slope_5m=_mtf_5m,
            mtf_alignment=_mtf_align,
            spread_bps=_spread,
            funding_rate=_funding,
            regime=regime,
            direction=direction,
            entry_ts=time.time(),
            # v3 신규 피처
            price=_price,
            high_24h=_high_24h,
            low_24h=_low_24h,
            rsi_14=_rsi_14,
            atr=_entry_atr,
            vol_regime_ratio=_vol_regime_ratio,
            open_pos_count=_open_pos_count,
        )
        _win_prob = 0.5
        _expected_roi = 0.0
        _neural_status = self.neural_scorer.status()
        _block_thresh = float(getattr(self.config, "neural_block_threshold", 0.25))
        if _neural_enabled:
            _win_prob, _expected_roi = self.neural_scorer.predict(_feat)
            self._last_neural_win_prob = _win_prob  # 레버리지 동적 계산용 캐시
            if _neural_status["ready"]:
                # v3 신뢰도 기반 하드 블록
                if _win_prob < _block_thresh:
                    self.neural_scorer.record_entry(snap.symbol, _feat)
                    return None, self._ko(
                        f"NEURAL BLOCK: 승률 {_win_prob:.1%} < {_block_thresh:.0%} (E[ROI]={_expected_roi:+.2f}%)",
                        f"NEURAL BLOCK: win_prob {_win_prob:.1%} < {_block_thresh:.0%} (E[ROI]={_expected_roi:+.2f}%)"
                    )
                # v3 강도 배율: 0.0 ~ 2.0 (기존 0.4~1.3 → 확대)
                # expected_roi 보정: 양수면 boost, 음수면 패널티
                _roi_adj = 1.0 + max(-0.5, min(0.5, _expected_roi / 10.0))
                _neural_mult = max(0.0, min(2.0, _win_prob * 2.0 * _roi_adj))
                strength = min(strength * _neural_mult, 5.0)
                logger.info("NEURAL_v3 %s prob=%.3f E[roi]=%.2f%% mult=%.2f str→%.2f (n=%d acc=%.1f%% lr=%.5f)",
                           snap.symbol, _win_prob, _expected_roi, _neural_mult, strength,
                           _neural_status["n_trained"], _neural_status.get("accuracy", 0.0),
                           _neural_status.get("lr", 0.0))
            self.neural_scorer.record_entry(snap.symbol, _feat)
        else:
            self.neural_scorer.record_entry(snap.symbol, _feat)

        bias = self._ko("강한 상승", "strong uptrend") if direction == "LONG" else self._ko("강한 하락", "strong downtrend")
        _trend_prefix = self._ko(
            f"{bias} 추세 감지 (dir={direction}, regime={regime}, ",
            f"{bias} trend detected (dir={direction}, regime={regime}, "
        )
        _neural_info = (
            f", neural_prob={_win_prob:.3f},E[roi]={_expected_roi:+.2f}%(n={_neural_status['n_trained']},acc={_neural_status.get('accuracy',0):.1f}%)"
            if (_neural_enabled and _neural_status.get("ready")) else ""
        )
        reason = _trend_prefix + f"momentum_pct={snap.momentum_pct:.4f}, volatility={snap.volatility:.4f}{_neural_info})"
        # ── 3-Party Consensus (활성화 시) ───────────────────────────
        if self.consensus_scorer:
            try:
                _consensus = self.consensus_scorer.compute_consensus(snap, direction, regime=regime)
                if _consensus.final_decision is None:
                    self._increment_flow("blocked_consensus")
                    return None, f"[CONSENSUS] {_consensus.block_reason}"
                # consensus 통과 시 strength를 가중 점수로 조정
                strength = max(strength * _consensus.weighted_score, 0.01)
                logger.debug(
                    "[CONSENSUS] %s %s PASS: rule=%.2f neural=%.2f tuner=%.2f → %.2f",
                    snap.symbol, direction,
                    _consensus.rule_score, _consensus.neural_prob,
                    _consensus.tuner_confidence, _consensus.weighted_score
                )
            except Exception as _ce:
                logger.debug("Consensus scorer error: %s", _ce)

        self._increment_flow("passed_signal")
        self._record_stat("signals_passed", 1)
        return SignalDecision(symbol=snap.symbol, direction=direction, strength=strength, reason=reason), None

    async def try_enter_position(self, snap: SymbolSnapshot):
        # A3: engine-level Fail-Closed — block all entries if consent not verified
        if not getattr(self.config, "consent_verified", False):
            logger.info("ENTRY_BLOCKED %s reason=CONSENT_NOT_VERIFIED", snap.symbol)
            return
        if self._symbol_busy(snap.symbol):
            logger.info("[ENTRY] BLOCKED %s reason=BUSY", snap.symbol)
            self._increment_flow("blocked_busy")
            self._record_entry_block("blocked_busy")
            return
        if not self._can_enter_market():
            reason = self._entry_block_reason() or "Entry paused"
            logger.info("ENTRY_BLOCKED_GLOBAL %s reason=%s", snap.symbol, reason)
            key = self._classify_block_reason(reason)
            if key:
                self._increment_flow(key)
                self._record_entry_block(key)
            self._notify("WATCH", f"ENTRY_BLOCKED_GLOBAL {snap.symbol}: {reason}")
            return
        if snap.symbol in self._open_symbols:
            logger.info("ENTRY_BLOCKED_GLOBAL %s reason=ALREADY_OPEN", snap.symbol)
            return
        now = time.time()
        block_until = self._spike_blocked_until.get(snap.symbol)
        if block_until and block_until > now:
            remaining = block_until - now
            logger.info("ENTRY_BLOCKED_SPIKE %s remaining=%.1fs", snap.symbol, remaining)
            self._increment_flow("blocked_spike_guard")
            self._record_entry_block("blocked_spike_guard")
            self._notify("WATCH", f"ENTRY_BLOCKED_SPIKE {snap.symbol} {remaining:.1f}s")
            return
        if block_until and block_until <= now:
            self._spike_blocked_until.pop(snap.symbol, None)
        reentry_until = self._spike_reentry_until.get(snap.symbol)
        if reentry_until and reentry_until > now:
            remaining = reentry_until - now
            logger.info("ENTRY_BLOCKED_SPIKE %s reentry=%.1fs", snap.symbol, remaining)
            self._increment_flow("blocked_spike_guard")
            self._record_entry_block("blocked_spike_guard")
            return
        if reentry_until and reentry_until <= now:
            self._spike_reentry_until.pop(snap.symbol, None)
        gap_threshold = float(getattr(self.config, "mark_gap_threshold", 0.0) or 0.0)
        if gap_threshold > 0:
            gap_ratio = self._mark_gap_ratio(snap)
            if gap_ratio >= gap_threshold:
                self._increment_flow("blocked_mark_gap")
                self._record_entry_block("blocked_mark_gap")
                logger.info("ENTRY_BLOCKED_GLOBAL %s reason=MARK_GAP %.4f", snap.symbol, gap_ratio)
                self._notify(
                    "WATCH",
                    f"ENTRY_BLOCKED_MARK_GAP {snap.symbol} gap={gap_ratio:.4f} thresh={gap_threshold:.4f}",
                )
                return
        logger.info("Watching %s volatility=%.4f momentum_pct=%.4f", snap.symbol, snap.volatility, snap.momentum_pct)
        self._notify(
            "WATCH",
            self._ko(f"체크 중: {snap.symbol} 변동성 {snap.volatility:.4f} 모멘텀% {snap.momentum_pct:.4f}", f"Checking: {snap.symbol} volatility={snap.volatility:.4f} momentum%={snap.momentum_pct:.4f}"),
        )
        decision, skip_reason = self.evaluate_signal(snap)
        if not decision:
            logger.info("SIGNAL_REJECT %s reason=%s", snap.symbol, skip_reason)
            self._notify("WATCH", f"SIGNAL_REJECT {snap.symbol} {skip_reason}")
            self._increment_skip(symbol=snap.symbol, reason=skip_reason)
            return

        # [PATCH-17] MTF 하드 게이트 제거 — EMA conflict filter (evaluate_signal 내)와 중복
        # composite scoring의 MTF 가중치(20%)가 이미 방향 확인을 수행함.
        # 별도 하드 게이트는 좋은 진입 기회를 불필요하게 차단하므로 비활성화.

        # [PATCH-11] 동일 심볼 재진입 쿨다운 — 청산 후 일정 시간 대기
        _reentry_cd = int(getattr(self.config, "symbol_reentry_cooldown_sec", 0) or 0)
        if _reentry_cd > 0:
            _last_exit_ts = getattr(self, "_symbol_last_exit_ts", {}).get(snap.symbol, 0.0)
            if _last_exit_ts > 0 and (time.time() - _last_exit_ts) < _reentry_cd:
                _remaining = int(_reentry_cd - (time.time() - _last_exit_ts))
                logger.info("ENTRY_BLOCKED_REENTRY_CD %s cooldown=%ds remaining=%ds", snap.symbol, _reentry_cd, _remaining)
                self._increment_flow("blocked_reentry_cooldown")
                return

        # [PATCH-11] chop 레짐에서 동시 포지션 제한
        # [PATCH-16] auto_tune 꺼져있으면 chop 제한 무시 (regime 항상 chop 고정이므로)
        _chop_max = int(getattr(self.config, "chop_max_open_symbols", 0) or 0)
        if _chop_max > 0 and getattr(self.config, "auto_tune_enabled", True):
            _cur_regime = ""
            if hasattr(self, "auto_tuner") and self.auto_tuner:
                _cur_regime = getattr(self.auto_tuner, "current_regime", "") or ""
            if _cur_regime == "chop" and len(self._open_symbols) >= _chop_max:
                logger.info("ENTRY_BLOCKED_CHOP_MAX %s open=%d chop_max=%d", snap.symbol, len(self._open_symbols), _chop_max)
                self._increment_flow("blocked_chop_max")
                return

        if len(self._open_symbols) >= self.config.max_open_symbols:
            logger.info(
                "ENTRY_BLOCKED_GLOBAL %s reason=MAX_OPEN (%d)",
                snap.symbol,
                self.config.max_open_symbols,
            )
            self._increment_flow("blocked_portfolio_cap")
            self._record_entry_block("blocked_portfolio_cap")
            return

        # [PATCH-17] 방향 집중도 체크 — 메이저 심볼 동방향 제한 + 전체 동방향 제한
        if not self._check_direction_concentration(snap.symbol, decision.direction):
            logger.info(
                "ENTRY_BLOCKED_CONCENTRATION %s direction=%s majors_same_dir",
                snap.symbol, decision.direction,
            )
            self._increment_flow("blocked_concentration")
            self._notify("WATCH", f"ENTRY_BLOCKED_CONCENTRATION {snap.symbol} {decision.direction}")
            return

        available_balance = await self._get_available_balance()
        # Kelly 사이징: 실적 기반 동적 포지션 비율 (데이터 부족 시 config 값 사용)
        position_pct = max(0.0, self._kelly_position_pct())
        # [PATCH-12] chop 레짐에서 포지션 사이즈 축소
        # [PATCH-16] auto_tune 꺼져있으면 chop 사이즈 축소 무시
        _cur_regime_pos = ""
        if getattr(self.config, "auto_tune_enabled", True) and hasattr(self, "auto_tuner") and self.auto_tuner:
            _cur_regime_pos = getattr(self.auto_tuner, "current_regime", "") or ""
        if _cur_regime_pos == "chop":
            _chop_mult = float(getattr(self.config, "chop_position_pct_mult", 0.5))
            position_pct *= _chop_mult
            logger.info("CHOP_POSITION_REDUCE %s pct=%.4f mult=%.2f", snap.symbol, position_pct, _chop_mult)
        available_notional = available_balance * position_pct
        if position_pct <= 0 or available_balance <= 0 or available_notional <= 0:
            detail = f"balance={available_balance:.2f} pct={position_pct:.4f}"
            logger.warning("ENTRY_REJECT_BALANCE %s %s", snap.symbol, detail)
            self._notify("WARN", f"ENTRY_REJECT_BALANCE {snap.symbol} {detail}")
            return

        symbol_min_notional = await self._symbol_min_notional(snap.symbol)
        fallback_min = 5.0 if self.testnet else 10.0
        min_notional = max(symbol_min_notional, fallback_min)
        strength_ratio = min(decision.strength / 5.0, 1.0)

        # ── 동적 레버리지: neural_prob + volatility 반영 ──
        _lev_neural_prob = 0.5  # 기본값
        try:
            if (getattr(self.config, "neural_scorer_enabled", False)
                    and hasattr(self, "neural_scorer")
                    and self.neural_scorer.status().get("ready")):
                # 가장 최근 예측 결과를 재사용 (evaluate_signal에서 이미 계산됨)
                _last = getattr(self, "_last_neural_win_prob", None)
                if _last is not None:
                    _lev_neural_prob = float(_last)
        except Exception:
            pass
        _lev_volatility = float(getattr(snap, "volatility", 0.0) or 0.0)
        target_leverage = self._compute_target_leverage(
            strength_ratio,
            neural_prob=_lev_neural_prob,
            volatility=_lev_volatility,
        )
        # [PATCH-7] 레버리지 설정 실패 시 진입 차단 (1x 진입 방지)
        if not await self._ensure_symbol_leverage(snap.symbol, target_leverage):
            logger.warning("ENTRY_BLOCKED_LEVERAGE %s target=%dx", snap.symbol, target_leverage)
            self._notify("WARN", self._ko(
                f"진입 차단 {snap.symbol}: 레버리지 {target_leverage}x 설정 실패",
                f"Entry blocked {snap.symbol}: leverage {target_leverage}x setup failed",
            ))
            return
        gross_notional_budget = available_notional * max(target_leverage, 1)
        desired_notional = max(min_notional, gross_notional_budget * strength_ratio)
        # [PATCH-6a] gross_notional_budget 상한 적용 후 min_notional 재검증
        desired_notional = min(desired_notional, gross_notional_budget)
        if desired_notional < min_notional:
            if self.auto_boost_position_pct and available_balance >= min_notional:
                desired_notional = min(available_balance, min_notional * 1.05)
                logger.info(
                    "ENTRY_AUTO_BOOST %s target=%.2f min=%.2f balance=%.2f",
                    snap.symbol,
                    desired_notional,
                    min_notional,
                    available_balance,
                )
                self._notify(
                    "INFO",
                    f"ENTRY_AUTO_BOOST {snap.symbol} target={desired_notional:.2f} min={min_notional:.2f}",
                )
            else:
                detail = f"avail_notional={available_notional:.2f} min={min_notional:.2f}"
                logger.warning("ENTRY_REJECT_MIN_NOTIONAL %s %s", snap.symbol, detail)
                self._notify("WARN", f"ENTRY_REJECT_MIN_NOTIONAL {snap.symbol} {detail}")
                self._symbol_blocked.add(snap.symbol)
                return
        if desired_notional <= 0:
            self._notify("WARN", self._ko(f"ENTRY_REJECT_BALANCE {snap.symbol} 진입 불가", f"ENTRY_REJECT_BALANCE {snap.symbol} insufficient balance"))
            return
        # ── 펀딩 레이트 편향 필터 ──────────────────────────────────────────
        funding_rate = await self._get_funding_rate(snap.symbol)
        funding_threshold = float(getattr(self.config, "funding_bias_threshold", 0.001))
        funding_penalty = float(getattr(self.config, "funding_bias_penalty", 0.30))
        if decision.direction == "LONG" and funding_rate > funding_threshold:
            decision = SignalDecision(
                symbol=decision.symbol,
                direction=decision.direction,
                strength=decision.strength * (1.0 - funding_penalty),
                reason=decision.reason + f" [funding_penalty: rate={funding_rate:.5f}]",
            )
            logger.info("FUNDING_PENALTY LONG %s rate=%.5f strength→%.2f", snap.symbol, funding_rate, decision.strength)
        elif decision.direction == "SHORT" and funding_rate < -funding_threshold:
            decision = SignalDecision(
                symbol=decision.symbol,
                direction=decision.direction,
                strength=decision.strength * (1.0 - funding_penalty),
                reason=decision.reason + f" [funding_penalty: rate={funding_rate:.5f}]",
            )
            logger.info("FUNDING_PENALTY SHORT %s rate=%.5f strength→%.2f", snap.symbol, funding_rate, decision.strength)

        # TCA 가드: 실시간 스프레드/슬리피지가 임계치 초과 시 차단
        _tca_max_spread = float(getattr(self.config, "tca_max_spread_bps_med", 0.0) or 0.0)
        _tca_max_slip   = float(getattr(self.config, "tca_max_slippage_bps_med", 0.0) or 0.0)
        if _tca_max_spread > 0 and float(getattr(snap, "spread_bps", 0.0) or 0.0) > _tca_max_spread:
            logger.info("ENTRY_BLOCKED_TCA_SPREAD %s spread=%.1fbps > %.1fbps", snap.symbol, snap.spread_bps, _tca_max_spread)
            self._notify("WATCH", f"ENTRY_BLOCKED_TCA_SPREAD {snap.symbol} spread={snap.spread_bps:.1f}bps")
            self._increment_flow("blocked_edge")   # TCA는 비용 관련 — blocked_edge로 분류
            self._record_entry_block("blocked_edge")
            return
        if not self._edge_covers_cost(decision, snap):
            logger.info("ENTRY_BLOCKED_GLOBAL %s reason=COST_NOT_COVERED", snap.symbol)
            self._increment_flow("blocked_edge")
            self._record_entry_block("blocked_edge")
            return
        # ATR risk-based sizing: cap notional so that a stop-loss hit ~= entry_risk_pct of balance.
        if bool(getattr(self.config, "atr_risk_sizing_enabled", False)):
            risk_pct = float(getattr(self.config, "entry_risk_pct", 0.0) or 0.0)
            if risk_pct > 0 and available_balance > 0 and snap.price > 0:
                atr_est = self._estimate_entry_atr(snap, snap.price)
                stop_px = self._compute_stop_loss_price(snap.price, decision.direction, atr_est)
                risk_per_unit = abs(snap.price - stop_px)
                if risk_per_unit > 0:
                    risk_budget = available_balance * risk_pct
                    qty_by_risk = max(risk_budget / risk_per_unit, 0.0)
                    notional_by_risk = qty_by_risk * snap.price
                    if notional_by_risk > 0:
                        desired_notional = min(desired_notional, notional_by_risk)
                        # [PATCH-13] min_notional이 ATR 사이징을 무효화하지 않도록
                        # ATR 사이징 결과가 min_notional 미만이면 진입 차단 (리스크 초과 방지)
                        if notional_by_risk < min_notional:
                            logger.info("ENTRY_BLOCKED_ATR_SIZE %s atr_notional=%.2f < min_notional=%.2f → skip (risk too large)", snap.symbol, notional_by_risk, min_notional)
                            self._increment_flow("blocked_size")
                            self._record_entry_block("blocked_size")
                            return

        # [PATCH-13] min_notional 보장 (ATR 사이징 미사용 시만)
        if not bool(getattr(self.config, "atr_risk_sizing_enabled", False)):
            desired_notional = max(desired_notional, min_notional)

        # 증거금 최소값 검사: desired_notional / leverage < min_margin_usdt 면 차단
        _min_margin = float(getattr(self.config, "min_margin_usdt", 1.0))
        _margin_estimate = desired_notional / max(1.0, float(target_leverage))
        if _margin_estimate < _min_margin:
            # auto_boost: min_margin 부족 시 notional 상향하여 재시도
            if self.auto_boost_position_pct:
                _boosted_notional = _min_margin * max(1.0, float(target_leverage))
                if _boosted_notional <= available_balance * max(1.0, float(target_leverage)):
                    logger.info(
                        "ENTRY_MARGIN_BOOST %s notional %.2f→%.2f (margin %.4f→%.4f USDT)",
                        snap.symbol, desired_notional, _boosted_notional,
                        _margin_estimate, _min_margin,
                    )
                    desired_notional = _boosted_notional
                    _margin_estimate = desired_notional / max(1.0, float(target_leverage))
                else:
                    logger.warning(
                        "ENTRY_REJECT_MIN_MARGIN %s margin_est=%.4f < min=%.2f USDT (boost불가 잔고부족)",
                        snap.symbol, _margin_estimate, _min_margin,
                    )
                    self._notify("WARN", self._ko(
                        f"진입 차단 {snap.symbol}: 잔고 부족으로 최소 증거금 미달",
                        f"Entry blocked {snap.symbol}: insufficient balance for min margin",
                    ))
                    return
            else:
                logger.warning(
                    "ENTRY_REJECT_MIN_MARGIN %s margin_est=%.4f < min=%.2f USDT",
                    snap.symbol, _margin_estimate, _min_margin,
                )
                self._notify("WARN", self._ko(
                    f"진입 차단 {snap.symbol}: 증거금 {_margin_estimate:.4f} USDT < 최소 {_min_margin:.2f} USDT",
                    f"Entry blocked {snap.symbol}: margin {_margin_estimate:.4f} USDT < min {_min_margin:.2f} USDT",
                ))
                return
        quantity = await self._compute_minimum_quantity(snap.symbol, snap.price, desired_notional)
        applied_leverage = float(self._symbol_leverage.get(snap.symbol, target_leverage))

        # [PATCH-6c] 최종 마진 검증: quantity 계산 후 실제 notional/margin 재확인
        _final_notional = quantity * snap.price if (quantity > 0 and snap.price > 0) else 0.0
        _final_margin = _final_notional / max(1.0, float(applied_leverage))
        if _final_margin < _min_margin:
            logger.warning(
                "ENTRY_REJECT_FINAL_MARGIN %s qty=%.6f price=%.4f notional=%.4f margin=%.4f < min=%.2f",
                snap.symbol, quantity, snap.price, _final_notional, _final_margin, _min_margin,
            )
            self._notify("WARN", self._ko(
                f"진입 최종 차단 {snap.symbol}: 실제 증거금 {_final_margin:.4f} USDT < 최소 {_min_margin:.2f} USDT",
                f"Entry final block {snap.symbol}: actual margin {_final_margin:.4f} USDT < min {_min_margin:.2f} USDT",
            ))
            return

        self._queue_snapshot_seed(snap.symbol, decision, snap, quantity, applied_leverage)
        logger.info(
            "Signal %s %s qty=%.6f price=%.4f reason=%s target_notional=%.2f margin=%.4f",
            decision.direction,
            snap.symbol,
            quantity,
            snap.price,
            decision.reason,
            _final_notional,
            _final_margin,
        )
        self._record_stat("orders", 1)
        if self._symbol_busy(decision.symbol):
            logger.info("[ENTRY] BLOCKED %s reason=CLOSING_IN_PROGRESS", decision.symbol)
            self._increment_flow("blocked_busy")
            self._record_entry_block("blocked_busy")
            return
        success = await self._execute_order(decision, quantity, snap.price)
        if success:
            self._open_symbols.add(decision.symbol)
        else:
            self._open_symbols.discard(decision.symbol)
            self._increment_skip(decision.symbol, "ORDER_FAILED")

    async def _execute_order(self, decision: SignalDecision, quantity: float, price: float):
        side = SIDE_BUY if decision.direction == "LONG" else SIDE_SELL
        symbol = decision.symbol
        if symbol in self._pending_orders:
            logger.info("[ENTRY] BLOCKED %s reason=PENDING_ORDER", symbol)
            return False
        if not await self._ensure_isolated_margin(symbol):
            logger.warning("[ENTRY] ABORT %s reason=ISOLATED_ENFORCE_FAILED", symbol)
            return False
        self._pending_orders.add(symbol)
        try:
            self._increment_flow("order_sent")

            # Maker-first entry: place post-only LIMIT (GTX) around mid, then fall back to MARKET.
            response = None
            entry_px = float(price or 0.0)
            maker_first = bool(getattr(self.config, "maker_first_enabled", False))
            # [PATCH-9] 진입은 taker(타이밍), 청산은 maker(비용) 역할 분리
            _entry_use_taker = bool(getattr(self.config, "maker_entry_use_taker", False))
            if maker_first and not _entry_use_taker and entry_px > 0 and quantity > 0:
                response, entry_px = await self._execute_maker_first_entry(symbol, side, quantity, entry_px)
            if response is None:
                response = await self.client.futures_create_order(
                    symbol=decision.symbol,
                    side=side,
                    type=ORDER_TYPE_MARKET,
                    quantity=quantity,
                )
                # Best-effort fill price extraction
                fill_px = self._extract_fill_price(response, fallback=entry_px)
                if fill_px:
                    entry_px = float(fill_px)
                # C3: slippage cap — close position immediately if fill is too far from mid
                slippage_cap_bps = float(getattr(self.config, "entry_slippage_cap_bps", 0.0))
                if slippage_cap_bps > 0 and entry_px > 0 and price > 0:
                    actual_slippage_bps = abs(entry_px - price) / price * 10000.0
                    if actual_slippage_bps > slippage_cap_bps:
                        self._notify("WARN", self._ko(
                            f"[SLIPPAGE_CAP] {symbol} 슬리피지 {actual_slippage_bps:.1f}bps > 캡 {slippage_cap_bps:.0f}bps — 진입 취소",
                            f"[SLIPPAGE_CAP] {symbol} slippage {actual_slippage_bps:.1f}bps > cap {slippage_cap_bps:.0f}bps — reversing entry"
                        ))
                        logger.warning("[SLIPPAGE_CAP] %s actual=%.1fbps cap=%.0fbps qty=%.4f — reversing", symbol, actual_slippage_bps, slippage_cap_bps, quantity)
                        order_id = response.get("orderId")
                        qty_to_close = abs(quantity)
                        close_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
                        try:
                            await self.client.futures_create_order(
                                symbol=symbol, side=close_side, type=ORDER_TYPE_MARKET, quantity=qty_to_close, reduceOnly=True
                            )
                        except Exception as _ce:
                            logger.warning("[SLIPPAGE_CAP] Close-on-slippage failed for %s: %s", symbol, _ce)
                        return False
            logger.info(
                "ORDER_SUBMITTED id=%s side=%s symbol=%s qty=%.6f price=%.4f",
                response.get("orderId"),
                decision.direction,
                decision.symbol,
                quantity,
                price,
            )
            order_notional = max(quantity * entry_px, 0.0)
            total_margin = total_notional = symbol_margin = symbol_notional = 0.0
            try:
                (total_margin, total_notional, symbol_margin, symbol_notional) = await self._get_margin_summary(decision.symbol)
                if symbol_notional == 0.0:
                    symbol_notional = order_notional
            except Exception as exc:
                logger.warning("Margin summary failed: %s", exc)
            direction_label = str(decision.direction).upper()
            side_label = self._ko("롱", "long") if direction_label == "LONG" else self._ko("숏", "short")
            message_lines = [
                self._ko(f"주문 진입: {side_label} ({direction_label}) {decision.symbol} 수량 {quantity:.4f} (ID {response.get('orderId')})", f"Order entry: {side_label} ({direction_label}) {decision.symbol} qty={quantity:.4f} (ID {response.get('orderId')})")
            ]
            if symbol_notional or symbol_margin:
                message_lines.append(
                    self._ko(f"명목금 {symbol_notional:.2f} USDT / 증거금 {symbol_margin:.2f} USDT", f"notional={symbol_notional:.2f} USDT / margin={symbol_margin:.2f} USDT")
                )
            if total_notional or total_margin:
                message_lines.append(
                    self._ko(f"총 명목금 {total_notional:.2f} USDT / 총 증거금 {total_margin:.2f} USDT", f"total notional={total_notional:.2f} USDT / total margin={total_margin:.2f} USDT")
                )
            self._notify("ALERT", "\n".join(message_lines))
            self._record_stat("fills", 1)
            self._increment_flow("fill_ok")
            # ── Execution Quality: maker/taker fill 추적 ──
            if self.feature_flags.is_enabled("execution_quality_tracking"):
                _is_maker = (response is not None and
                             isinstance(response, dict) and
                             response.get("timeInForce") == "GTX")
                if _is_maker:
                    self._record_stat("maker_fills", 1)
                else:
                    self._record_stat("taker_fills", 1)
            self._ai_event("ENTRY_CHECK",
                           f"ENTRY_OK {symbol} {decision.direction} qty={quantity:.6f} price={entry_px:.4f} "
                           f"leverage={self._symbol_leverage.get(symbol, 1):.0f}x strength={decision.strength:.2f}")
            seed = self._snapshot_seeds.get(symbol)
            if not seed:
                seed = {
                    "decision": decision,
                    "entry_price": entry_px,
                    "side": decision.direction,
                    "qty": quantity,
                    "leverage": self._symbol_leverage.get(symbol, 1.0),
                    "atr": max(entry_px * 0.01, 0.1),
                    "momentum": 0.0,
                }
            else:
                # Ensure snapshot entry price reflects actual fills (important for ROI/SL/TP logic).
                seed = dict(seed)
                seed["entry_price"] = entry_px
            snapshot = self._build_snapshot_from_seed(symbol, seed)
            self.position_snapshots[symbol] = snapshot
            self._notify(
                "WATCH",
                f"ORDER_SUCCESS {decision.symbol} dir={decision.direction} qty={quantity:.4f} notional={order_notional:.2f}",
            )
            return True
        except BinanceAPIException as exc:
            self._handle_api_exception(exc, f"execute_order {decision.symbol}")
            message = exc.message or str(exc)
            reason = self._classify_order_failure(exc, message)
            self._order_failures.append((time.time(), reason))
            self._check_failure_circuit()
            logger.warning(
                "ORDER_FAILED %s %s qty=%.6f reason=%s (%s)",
                decision.direction,
                decision.symbol,
                quantity,
                message,
                reason,
            )
            self._notify(
                "WARN",
                f"ORDER_FAILED {decision.symbol} dir={decision.direction} qty={quantity:.4f} reason={message} ({reason})",
            )
            if exc.code in (-4140, -4411):
                self._symbol_blocked.add(decision.symbol)
                if exc.code == -4411:
                    if not hasattr(self, "_tradfi_blocked"):
                        self._tradfi_blocked: set = set()
                    self._tradfi_blocked.add(decision.symbol)
            return False
        except Exception as exc:
            message = str(exc)
            reason = self._classify_order_failure(exc, message)
            self._order_failures.append((time.time(), reason))
            self._check_failure_circuit()
            logger.exception(
                "ORDER_FAILED %s %s qty=%.6f reason=%s (%s)",
                decision.direction,
                decision.symbol,
                quantity,
                exc,
                reason,
            )
            self._notify(
                "WARN",
                f"ORDER_FAILED {decision.symbol} dir={decision.direction} qty={quantity:.4f} exception={exc} ({reason})",
            )
            return False
        finally:
            self._pending_orders.discard(symbol)

    async def _execute_maker_first_entry(self, symbol: str, side: str, quantity: float, mid_price: float):
        """Best-effort maker entry using post-only LIMIT (timeInForce=GTX).

        Returns (response_or_none, executed_entry_price).
        - If post-only order is rejected or not filled within timeout, returns (None, mid_price)
          so caller can fall back to MARKET.
        """
        offset_bps = float(getattr(self.config, "maker_first_offset_bps", 0.0) or 0.0)
        timeout_ms = int(getattr(self.config, "maker_first_timeout_ms", 0) or 0)
        # ── EQ: 심볼별 오버라이드 적용 ──
        if self.exec_quality:
            _eq_offset, _eq_timeout = self.exec_quality.get_params(symbol)
            offset_bps = _eq_offset
            timeout_ms = _eq_timeout
            self.exec_quality.record_maker_attempt(symbol)
        _maker_attempt_ts = time.time()  # 체결 시간 측정용
        if mid_price <= 0 or quantity <= 0 or timeout_ms <= 0:
            return None, mid_price

        # [PATCH-3] 적응형 메이커 파라미터: 변동성/스프레드 연동
        _adaptive_timeout = bool(getattr(self.config, "maker_adaptive_timeout", False))
        _adaptive_offset = bool(getattr(self.config, "maker_offset_adaptive", False))
        if _adaptive_timeout or _adaptive_offset:
            _snap = self._symbol_snapshots.get(symbol) if hasattr(self, "_symbol_snapshots") else None
            if _snap is None:
                _snap = self.symbol_snapshots.get(symbol) if hasattr(self, "symbol_snapshots") else None
            if _snap is not None:
                # 타임아웃 적응: ATR 높으면 짧게
                if _adaptive_timeout:
                    _cur_atr = float(getattr(_snap, "atr_value", 0.0) or 0.0)
                    _ref_atr = float(getattr(self.config, "time_stop_atr_ref", 0.005) or 0.005)
                    if _cur_atr > 0 and _ref_atr > 0:
                        _t_ratio = _ref_atr / _cur_atr
                        _t_min = int(getattr(self.config, "maker_timeout_min_ms", 1000))
                        _t_max = int(getattr(self.config, "maker_timeout_max_ms", 5000))
                        timeout_ms = max(_t_min, min(int(timeout_ms * _t_ratio), _t_max))
                # 오프셋 적응: 스프레드 넓으면 크게
                if _adaptive_offset:
                    _spread_bps = float(getattr(_snap, "spread_bps", 0.0) or 0.0)
                    if _spread_bps > 0:
                        _o_min = float(getattr(self.config, "maker_offset_min_bps", 0.5))
                        _o_max = float(getattr(self.config, "maker_offset_max_bps", 3.0))
                        _s_mult = float(getattr(self.config, "maker_spread_mult", 0.5))
                        offset_bps = max(_o_min, min(offset_bps + _spread_bps * _s_mult, _o_max))

        # Maker intent: BUY below mid, SELL above mid.
        if side == SIDE_BUY:
            limit_price = mid_price * (1.0 - offset_bps / 10000.0)
        else:
            limit_price = mid_price * (1.0 + offset_bps / 10000.0)
        limit_price = max(float(limit_price), 0.0001)

        # [PATCH-18] GTX requote: -5022 거절 시 최대 2회 재시도 (오프셋 확대)
        _max_requotes = 2
        order = None
        for _attempt in range(_max_requotes + 1):
            try:
                order = await self.client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce="GTX",  # post-only (maker) for USDT-margined futures
                    quantity=quantity,
                    price=f"{limit_price:.8f}",
                    newOrderRespType="RESULT",
                )
                break  # 주문 성공
            except BinanceAPIException as exc:
                _code = getattr(exc, "code", None)
                if _code == -4411:
                    # TradFi agreement not signed → block symbol, no retry
                    self._symbol_blocked.add(symbol)
                    if not hasattr(self, "_tradfi_blocked"):
                        self._tradfi_blocked: set = set()
                    self._tradfi_blocked.add(symbol)
                    self._handle_api_exception(exc, f"maker_first_entry {symbol}")
                    return None, mid_price
                if _code == -5022 and _attempt < _max_requotes:
                    # Post-only rejected → 오프셋 확대 후 재시도
                    if self.exec_quality:
                        self.exec_quality.record_gtx_rejection(symbol)
                    _widen = 1.0 + (_attempt + 1) * 0.5  # 1.5x, 2.0x
                    if side == SIDE_BUY:
                        limit_price = mid_price * (1.0 - offset_bps * _widen / 10000.0)
                    else:
                        limit_price = mid_price * (1.0 + offset_bps * _widen / 10000.0)
                    limit_price = max(float(limit_price), 0.0001)
                    await asyncio.sleep(0.1)
                    continue
                self._handle_api_exception(exc, "maker_first_entry")
                return None, mid_price
            except Exception:
                return None, mid_price
        if order is None:
            return None, mid_price

        order_id = None
        try:
            order_id = order.get("orderId") if isinstance(order, dict) else None
        except Exception:
            order_id = None

        # Poll status until filled or timeout.
        deadline = time.time() + (timeout_ms / 1000.0)
        last = order
        while time.time() < deadline:
            try:
                if hasattr(self.client, "futures_get_order") and order_id:
                    last = await self.client.futures_get_order(symbol=symbol, orderId=order_id)
                status = (last or {}).get("status")
                if status == "FILLED":
                    fill_px = self._extract_fill_price(last, fallback=limit_price)
                    # ── EQ: maker 체결 성공 기록 ──
                    if self.exec_quality:
                        _fill_ms = (time.time() - _maker_attempt_ts) * 1000.0
                        _slip = abs(float(fill_px or limit_price) - mid_price) / mid_price * 10000.0 if mid_price > 0 else 0.0
                        self.exec_quality.record_fill(symbol, is_maker=True, fill_time_ms=_fill_ms, slippage_bps=_slip)
                    return last, float(fill_px or limit_price)
            except BinanceAPIException:
                break
            except Exception:
                break
            await asyncio.sleep(0.15)

        # Not filled → cancel and fall back to market.
        # ── EQ: taker 전환 기록 ──
        if self.exec_quality:
            self.exec_quality.record_fill(symbol, is_maker=False, fill_time_ms=timeout_ms)
        try:
            if hasattr(self.client, "futures_cancel_order") and order_id:
                await self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
        except Exception:
            pass
        return None, mid_price