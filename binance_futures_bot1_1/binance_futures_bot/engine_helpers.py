import math
from dataclasses import dataclass
from typing import Dict, List, Tuple


def compute_take_profit_levels(
    entry_price: float,
    stop_price: float,
    r_multiples: List[float],
    direction: str,
) -> List[float]:
    if entry_price <= 0 or stop_price <= 0:
        raise ValueError("Prices must be positive")
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    risk = abs(entry_price - stop_price)
    if risk == 0:
        risk = entry_price * 0.0001
    sign = 1 if direction == "LONG" else -1
    levels = []
    for multiple in r_multiples:
        if multiple <= 0:
            continue
        level = entry_price + sign * risk * multiple
        levels.append(level)
    return levels


def compute_adaptive_time_stop(
    base_seconds: int,
    current_atr: float,
    ref_atr: float,
    min_seconds: int = 600,
    max_seconds: int = 3600,
) -> int:
    """[PATCH-2] ATR 기반 적응형 시간 손절 계산.
    ATR이 기준(ref_atr)보다 높으면(고변동성) 시간 단축,
    ATR이 기준보다 낮으면(저변동성) 시간 연장.
    """
    if ref_atr <= 0 or current_atr <= 0:
        return base_seconds
    ratio = ref_atr / current_atr  # 고변동성이면 ratio < 1 → 시간 단축
    adjusted = int(base_seconds * ratio)
    return max(min_seconds, min(adjusted, max_seconds))


def should_trigger_time_stop(
    entry_ts: float,
    now_ts: float,
    min_hold_seconds: float,
    time_stop_seconds: float,
    pnl_after_fee: float,
    pnl_tolerance: float = 0.0,
    current_atr: float = 0.0,
    ref_atr: float = 0.0,
    adaptive: bool = False,
    min_seconds: int = 600,
    max_seconds: int = 3600,
) -> bool:
    if entry_ts <= 0 or now_ts <= 0 or now_ts <= entry_ts:
        return False
    elapsed = now_ts - entry_ts
    if elapsed < max(min_hold_seconds, 0):
        return False
    # [PATCH-2] 적응형 시간 손절
    effective_time_stop = time_stop_seconds
    if adaptive and current_atr > 0 and ref_atr > 0:
        effective_time_stop = compute_adaptive_time_stop(
            time_stop_seconds, current_atr, ref_atr,
            min_seconds, max_seconds
        )
    if elapsed < max(effective_time_stop, 0):
        return False
    return abs(pnl_after_fee) <= abs(pnl_tolerance)


def should_trigger_signal_decay(
    entry_strength: float,
    current_strength: float,
    decay_threshold: float,
    pnl_after_fee: float,
    min_profit: float = 0.0,
) -> bool:
    if entry_strength <= 0:
        return False
    ratio = abs(current_strength) / entry_strength
    if ratio >= decay_threshold:
        return False
    return pnl_after_fee >= min_profit


def update_trailing_stop(
    current_stop: float,
    trail_reference: float,
    current_price: float,
    atr_value: float,
    direction: str,
    trail_atr_mult: float,
    trail_min_step_pct: float,
) -> Tuple[float, float]:
    if atr_value <= 0 or trail_atr_mult <= 0:
        return current_stop, trail_reference
    direction = direction.upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    sign = 1 if direction == "LONG" else -1
    new_ref = trail_reference
    improved = False
    if direction == "LONG" and current_price > trail_reference:
        new_ref = current_price
        improved = True
    elif direction == "SHORT" and current_price < trail_reference:
        new_ref = current_price
        improved = True
    if not improved:
        return current_stop, new_ref
    move_pct = abs((new_ref - trail_reference) / max(trail_reference, 1e-9))
    if move_pct < max(trail_min_step_pct, 0):
        return current_stop, new_ref
    offset = atr_value * trail_atr_mult
    if direction == "LONG":
        new_stop = max(current_stop, new_ref - offset)
    else:
        new_stop = min(current_stop, new_ref + offset)
    return new_stop, new_ref

