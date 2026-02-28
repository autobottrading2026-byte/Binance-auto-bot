from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .position_snapshot import PositionSnapshot, hash_params, strategy_from_decision


def build_snapshot(
    symbol: str,
    params: Dict[str, Any],
    entry_price: float,
    side: str,
    quantity: float,
    leverage: float,
    atr_at_entry: float,
    momentum_at_entry: float,
    fees_model: Dict[str, float],
    entry_expected_mid: float | None = None,
    entry_spread_bps: float | None = None,
    entry_slippage_bps: float | None = None,
    decision: Any = None,
    stop_loss_px: Optional[float] = None,
    take_profit_levels: Optional[list[float]] = None,
) -> PositionSnapshot:
    now = time.time()
    strat = strategy_from_decision(decision)
    return PositionSnapshot(
        symbol=symbol,
        params=dict(params),
        params_hash=hash_params(params),
        opened_at=now,
        strategy_tag=strat,
        entry_price=float(entry_price),
        side=str(side).upper(),
        quantity=float(quantity),
        leverage=float(leverage),
        atr_at_entry=float(atr_at_entry),
        momentum_at_entry=float(momentum_at_entry),
        fees_model=dict(fees_model or {}),
        entry_expected_mid=None if entry_expected_mid is None else float(entry_expected_mid),
        entry_spread_bps=None if entry_spread_bps is None else float(entry_spread_bps),
        entry_slippage_bps=None if entry_slippage_bps is None else float(entry_slippage_bps),
        stop_loss_px=stop_loss_px,
        take_profit_levels=list(take_profit_levels or []),
        trail_active=False,
        trail_ref_px=None,
        trail_offset_px=None,
        partial_tp_done=False,
        initial_tp=take_profit_levels[0] if take_profit_levels else None,
        initial_sl=stop_loss_px,
        highest_price_since_entry=float(entry_price),
        lowest_price_since_entry=float(entry_price),
        mfe_pnl_pct=0.0,
        mfe_last_update_ts=now,
        partial_tp_fired_levels=set(),
        trail_stop_price=None,
        trail_last_update_ts=None,
    )


def update_snapshot(snapshot: PositionSnapshot, **updates: Any) -> PositionSnapshot:
    data = snapshot.to_dict()
    data.update(updates)
    fired = data.get('partial_tp_fired_levels')
    if isinstance(fired, list):
        data['partial_tp_fired_levels'] = set(fired)
    return PositionSnapshot(**data)
