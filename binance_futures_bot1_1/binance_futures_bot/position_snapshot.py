"""Helpers for recording per-position parameter snapshots."""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Dict, Any, Optional, List, Set


def hash_params(params: Dict[str, Any]) -> str:
    """Small deterministic hash for parameter dict."""
    items = sorted((k, round(float(v), 8) if isinstance(v, (int, float)) else v) for k, v in params.items())
    return "|".join(f"{k}:{value}" for k, value in items)


def strategy_from_decision(decision) -> str:
    if not decision:
        return "default"
    label = getattr(decision, "strategy", None) or getattr(decision, "reason", None)
    if isinstance(label, str) and label:
        return label
    return getattr(decision, "symbol", "default")


@dataclass
class PositionSnapshot:
    symbol: str
    params: Dict[str, Any]
    params_hash: str
    opened_at: float
    strategy_tag: str
    entry_price: float
    side: str
    quantity: float
    leverage: float
    atr_at_entry: float
    momentum_at_entry: float
    fees_model: Dict[str, float]
    entry_expected_mid: Optional[float] = None
    entry_spread_bps: Optional[float] = None
    entry_slippage_bps: Optional[float] = None
    stop_loss_px: Optional[float] = None
    take_profit_levels: Optional[List[float]] = None
    trail_active: bool = False
    trail_ref_px: Optional[float] = None
    trail_offset_px: Optional[float] = None
    partial_tp_done: bool = False
    initial_tp: Optional[float] = None
    initial_sl: Optional[float] = None
    last_tp_ts: Optional[float] = None

    unrealized_pnl: Optional[float] = None       # 청산 직전 Binance unRealizedProfit
    highest_price_since_entry: Optional[float] = None
    lowest_price_since_entry: Optional[float] = None
    mfe_pnl_pct: float = 0.0
    mfe_last_update_ts: Optional[float] = None
    partial_tp_fired_levels: Set[int] = field(default_factory=set)
    trail_stop_price: Optional[float] = None
    trail_last_update_ts: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['partial_tp_fired_levels'] = list(self.partial_tp_fired_levels)
        return data
