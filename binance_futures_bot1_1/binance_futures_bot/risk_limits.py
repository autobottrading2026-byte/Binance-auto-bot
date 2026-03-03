"""Risk guard/clamp helpers for Binance Futures bot."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class ParamLimit:
    key: str
    bounds: Tuple[float, float]
    roc: float  # max absolute change per update (same unit as value)


DEFAULT_LIMITS = {
    # [v2] ROC 하향 — EMA 수렴과 함께 진동 억제
    "position_pct": ParamLimit("position_pct", (0.03, 0.08), 0.0010),      # 0.003→0.0010
    "total_risk_budget": ParamLimit("total_risk_budget", (0.03, 0.08), 0.0010),
    "leverage_min": ParamLimit("leverage_min", (1.0, 5.0), 0.5),
    "leverage_max": ParamLimit("leverage_max", (3.0, 12.0), 1.0),
    "max_loss_per_position": ParamLimit("max_loss_per_position", (0.5, 2.2), 0.10),
    "watch_limit": ParamLimit("watch_limit", (3, 20), 1.0),               # tune 대상 아님
    "max_open_symbols": ParamLimit("max_open_symbols", (2, 12), 0.5),     # tune 대상 아님
    "momentum_min_long": ParamLimit("momentum_min_long", (-0.006, 0.006), 0.0006),   # 0.0008→0.0006
    "momentum_min_short": ParamLimit("momentum_min_short", (-0.006, 0.006), 0.0006), # 0.0008→0.0006
    "volatility_min": ParamLimit("volatility_min", (0.001, 0.015), 0.0006),          # 0.001→0.0006
}


def clamp_params(
    proposal: Dict[str, float],
    previous: Dict[str, float],
    limits: Dict[str, ParamLimit] | None = None,
) -> Dict[str, float]:
    """Apply bounds + rate-of-change limits to parameter dict."""
    limits = limits or DEFAULT_LIMITS
    result: Dict[str, float] = dict(proposal)
    for key, limit in limits.items():
        if key not in result:
            continue
        value = result[key]
        lo, hi = limit.bounds
        value = clamp(float(value), lo, hi)
        prev = float(previous.get(key, value))
        delta = value - prev
        if abs(delta) > limit.roc:
            value = prev + (limit.roc if delta > 0 else -limit.roc)
        result[key] = value
    # ensure leverage_min <= leverage_max-1
    if "leverage_min" in result and "leverage_max" in result:
        if result["leverage_min"] > result["leverage_max"] - 1:
            result["leverage_min"] = max(1.0, result["leverage_max"] - 1)
    return result
