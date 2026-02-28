from __future__ import annotations

import math
from typing import Dict, List, Optional


def _lot_size_params(filters: List[Dict]) -> tuple[float, float]:
    min_qty = 0.0
    step = 0.0
    for f in filters:
        if f.get("filterType") == "LOT_SIZE":
            min_qty = float(f.get("minQty", min_qty))
            step = float(f.get("stepSize", step))
            break
    return min_qty, step if step else 0.0


def min_notional_from_filters(filters: List[Dict]) -> float:
    for f in filters:
        if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
            return float(f.get("notional") or f.get("minNotional") or 0.0)
    return 0.0


def quantize(value: float, step: float) -> float:
    if step <= 0:
        return value
    steps = math.ceil(value / step)
    return round(steps * step, 8)


def compliant_quantity(price: float, filters: List[Dict], desired_notional: Optional[float] = None) -> float:
    min_qty, step = _lot_size_params(filters)
    step = step if step > 0 else 0.0
    qty = min_qty if min_qty > 0 else 0.0
    if desired_notional and price > 0:
        target_qty = desired_notional / price
        qty = max(qty, target_qty)
    if step > 0 and qty > 0:
        qty = quantize(qty, step)
    min_notional = min_notional_from_filters(filters)
    if min_notional > 0 and price > 0:
        needed = min_notional / price
        qty = max(qty, needed)
        if step > 0:
            qty = quantize(qty, step)
    return round(qty, 6)
