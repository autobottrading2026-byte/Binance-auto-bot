import asyncio
import math
import os
import atexit
import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List

from binance import AsyncClient
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT
from binance.exceptions import BinanceAPIException

from .binance_futures_bot.tick_engine import TickEngine
from .binance_futures_bot.config import EngineConfig
from .binance_futures_bot.exchange_utils import compliant_quantity

current_engine: Optional[TickEngine] = None
current_task: Optional[asyncio.Task] = None
current_client: Optional[AsyncClient] = None
client_lock: Optional[asyncio.Lock] = None
client_context: Dict[str, Optional[str]] = {
    "api_key": None,
    "api_secret": None,
    "testnet": True,
}
exchange_info_cache: Optional[dict] = None
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
notification_path_default = os.path.join(LOG_DIR, "notifications.log")


async def _sync_server_time(client: AsyncClient) -> None:
    """Sync local timestamp offset with Binance server to prevent -1021 errors."""
    try:
        server_time = await client.futures_time()
        server_ts = int(server_time["serverTime"])
        local_ts = int(time.time() * 1000)
        client.timestamp_offset = server_ts - local_ts
        logging.info(f"[TIME_SYNC] offset={client.timestamp_offset}ms (server={server_ts}, local={local_ts})")
    except Exception as exc:
        logging.warning(f"[TIME_SYNC] Failed to sync server time: {exc}")
        client.timestamp_offset = 0


async def _ensure_client(api_key: str, api_secret: str, testnet: bool) -> AsyncClient:
    global current_client, client_context, client_lock
    if client_lock is None:
        client_lock = asyncio.Lock()
    async with client_lock:
        if (
            current_client
            and client_context["api_key"] == api_key
            and client_context["api_secret"] == api_secret
            and client_context["testnet"] == testnet
        ):
            return current_client
        if current_client:
            await current_client.close_connection()
            current_client = None
        current_client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None, testnet=testnet)
        await _sync_server_time(current_client)
        client_context = {"api_key": api_key, "api_secret": api_secret, "testnet": testnet}
        return current_client


async def start_engine(config: EngineConfig, creds: Dict[str, str], notification_path: str = notification_path_default):
    global current_engine, current_task
    if current_engine:
        return
    client = await _ensure_client(creds.get("api_key", ""), creds.get("api_secret", ""), creds.get("testnet", True))
    os.makedirs(os.path.dirname(notification_path), exist_ok=True)
    current_engine = TickEngine(
        client,
        config,
        testnet=creds.get("testnet", True),
        notification_path=notification_path,
    )
    loop = asyncio.get_running_loop()
    current_task = loop.create_task(current_engine.run())


async def close_client_session():
    global current_client, client_context, exchange_info_cache
    if current_client:
        try:
            session = getattr(current_client, "session", None)
            await current_client.close_connection()
            if session and not session.closed:
                await session.close()
                await asyncio.sleep(0)
        finally:
            current_client = None
            client_context = {"api_key": None, "api_secret": None, "testnet": True}
            exchange_info_cache = None


def _close_client_session_sync():
    if current_client:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(close_client_session())
        finally:
            loop.close()


atexit.register(_close_client_session_sync)


async def stop_engine():
    global current_engine, current_task
    if current_engine:
        current_engine.running = False
    task = current_task
    current_engine = None
    current_task = None
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # swallow other errors on shutdown to avoid blocking UI
            pass
    await close_client_session()


async def _fetch_exchange_info(client: AsyncClient):
    global exchange_info_cache
    if exchange_info_cache is None:
        exchange_info_cache = await client.futures_exchange_info()
    return exchange_info_cache


def _filters_for_symbol_from_info(symbol: str, info: dict) -> List[dict]:
    entry = next((s for s in info.get("symbols", []) if s.get("symbol") == symbol), None)
    return entry.get("filters", []) if entry else []


async def _filters_for_symbol(client: AsyncClient, symbol: str) -> List[dict]:
    info = await _fetch_exchange_info(client)
    return _filters_for_symbol_from_info(symbol, info)


def _quantize_reduce_only_quantity(quantity: float, filters: List[dict]) -> float:
    if quantity is None:
        return None
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        return quantity
    if qty <= 0:
        return qty
    original = qty
    step = 0.0
    for f in filters or []:
        if f.get("filterType") == "LOT_SIZE":
            try:
                step = float(f.get("stepSize", step))
            except (TypeError, ValueError):
                step = 0.0
            break
    if step > 0:
        try:
            step_dec = Decimal(str(step))
            qty = Decimal(str(qty)).quantize(step_dec, rounding=ROUND_DOWN)
            qty = float(qty)
        except Exception:
            factor = 10 ** max(0, -int(math.log10(step)) if step > 0 else 0)
            qty = math.floor(qty * factor) / factor
    if qty <= 0:
        qty = min(original, step if step > 0 else original)
    return max(0.0, round(qty, 6))


async def _ensure_isolated_margin(client: AsyncClient, symbol: str) -> bool:
    if not symbol:
        return False
    try:
        await client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
        return True
    except BinanceAPIException as exc:
        if exc.code == -4046:
            return True
        logging.warning("[WARN] margin mode change failed (%s): %s", symbol, exc)
        return False
    except Exception as exc:
        logging.warning("[WARN] margin mode change exception (%s): %s", symbol, exc)
        return False


async def get_symbol_filters(creds: Dict[str, str], symbol: str) -> List[dict]:
    client = await _ensure_client(creds.get("api_key", ""), creds.get("api_secret", ""), creds.get("testnet", True))
    return await _filters_for_symbol(client, symbol)


async def place_test_order(creds: Dict[str, str], symbol: str = "BTCUSDT"):
    client = await _ensure_client(creds.get("api_key", ""), creds.get("api_secret", ""), creds.get("testnet", True))
    info = await _fetch_exchange_info(client)
    symbol_entry = next((s for s in info.get("symbols", []) if s.get("symbol") == symbol), None)
    filters = symbol_entry.get("filters", []) if symbol_entry else []
    ticker = await client.futures_symbol_ticker(symbol=symbol)
    price = float(ticker.get("price", 0))
    # [PATCH-6d] 최소 노셔널 보장: desired_notional을 명시적으로 전달
    _min_notional = max(min_notional_from_filters(filters), 10.0)
    quantity = compliant_quantity(price if price > 0 else 1.0, filters, _min_notional)
    # [PATCH-6d] 최소 마진 검증 (1.0 USDT)
    _notional = quantity * price if (quantity > 0 and price > 0) else 0.0
    if _notional < _min_notional:
        raise ValueError(f"테스트 주문 거부: notional={_notional:.4f} < min={_min_notional:.2f}")
    response = await client.futures_create_order(
        symbol=symbol,
        side=SIDE_BUY,
        type=ORDER_TYPE_MARKET,
        quantity=quantity,
    )
    return {
        "symbol": symbol,
        "quantity": quantity,
        "orderId": response.get("orderId"),
        "price": price,
        "notional": quantity * price if price else None,
    }


async def get_top_symbols(creds: Dict[str, str], limit: int = 10) -> List[dict]:
    client = await _ensure_client(creds.get("api_key", ""), creds.get("api_secret", ""), creds.get("testnet", True))
    tickers = await client.futures_ticker()
    entries = []
    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        try:
            price = float(ticker.get("lastPrice", 0))
            volume = float(ticker.get("volume", 0))
            quote_volume = float(ticker.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
        entries.append({"symbol": symbol, "price": price, "volume": volume, "quoteVolume": quote_volume})
    entries.sort(key=lambda x: x["quoteVolume"], reverse=True)
    return entries[:limit]


async def place_manual_order(
    creds: Dict[str, str],
    symbol: str,
    side: str,
    order_type: str,
    quantity: Optional[float] = None,
    price: Optional[float] = None,
    reduce_only: bool = False,
    notional_percent: Optional[float] = None,
):
    client = await _ensure_client(creds.get("api_key", ""), creds.get("api_secret", ""), creds.get("testnet", True))
    order_type = order_type.upper()
    side = side.upper()
    if order_type not in ("MARKET", "LIMIT"):
        raise ValueError("order_type must be MARKET or LIMIT")

    ref_price = price if price and price > 0 else None
    if notional_percent is not None:
        account = await client.futures_account()
        available = float(account.get("availableBalance", 0.0))
        if ref_price is None:
            ticker = await client.futures_symbol_ticker(symbol=symbol)
            ref_price = float(ticker.get("price", 0))
        if not ref_price or ref_price <= 0:
            raise ValueError("Invalid price reference")
        computed_qty = (available * max(notional_percent, 0) / 100) / ref_price
        quantity = max(quantity or 0.0, computed_qty)

    if ref_price is None:
        ticker = await client.futures_symbol_ticker(symbol=symbol)
        ref_price = float(ticker.get("price", 0))

    if quantity is None or quantity <= 0:
        raise ValueError("Quantity must be positive")

    if not await _ensure_isolated_margin(client, symbol):
        raise ValueError("해당 심볼의 마진 모드를 ISOLATED로 설정할 수 없습니다. 포지션을 정리한 뒤 다시 시도하세요.")

    filters = await _filters_for_symbol(client, symbol)
    if not filters:
        raise ValueError("심볼 필터를 불러오지 못했습니다")
    if not reduce_only:
        desired_notional = (quantity * ref_price) if (ref_price and quantity) else None
        # [PATCH-6e] 최소 노셔널 보장
        _min_notional = max(min_notional_from_filters(filters), 5.0)
        if desired_notional is not None and desired_notional < _min_notional:
            desired_notional = _min_notional
        quantity = compliant_quantity(ref_price or 1.0, filters, desired_notional)
        if quantity <= 0:
            raise ValueError("Unable to derive compliant quantity")
        # [PATCH-6e] 최소 마진 검증 (1.0 USDT)
        _actual_notional = quantity * ref_price if (quantity > 0 and ref_price > 0) else 0.0
        _min_margin_usdt = 1.0
        # 마진 계산을 위해 레버리지 추정 (isolated 모드 기본 레버리지)
        if _actual_notional > 0 and _actual_notional < _min_margin_usdt:
            raise ValueError(
                f"수동 주문 거부: notional={_actual_notional:.4f} USDT < 최소 마진 {_min_margin_usdt:.2f} USDT"
            )
    else:
        quantity = _quantize_reduce_only_quantity(quantity, filters)
        if quantity <= 0:
            raise ValueError("Unable to derive compliant quantity (reduce-only)")

    params = {
        "symbol": symbol,
        "side": SIDE_BUY if side == "BUY" else SIDE_SELL,
        "type": ORDER_TYPE_MARKET if order_type == "MARKET" else ORDER_TYPE_LIMIT,
        "quantity": quantity,
        "reduceOnly": reduce_only,
    }
    if params["type"] == ORDER_TYPE_LIMIT:
        if price is None or price <= 0:
            raise ValueError("Limit order requires price")
        params["price"] = price
        params["timeInForce"] = "GTC"
    return await client.futures_create_order(**params)


async def fetch_open_positions(creds: Dict[str, str]) -> List[dict]:
    client = await _ensure_client(creds.get("api_key", ""), creds.get("api_secret", ""), creds.get("testnet", True))
    info = await client.futures_position_information()
    results = []
    for pos in info:
        try:
            amt = float(pos.get("positionAmt", 0))
        except (TypeError, ValueError):
            continue
        if amt == 0:
            continue
        entry_price = float(pos.get("entryPrice", 0))
        mark_price = float(pos.get("markPrice", 0))
        break_even = float(pos.get("breakEvenPrice", entry_price))
        liq_price = float(pos.get("liquidationPrice", 0))
        unrealized = float(pos.get("unRealizedProfit", 0))
        try:
            leverage = float(pos.get("leverage", 0) or 0)
        except (TypeError, ValueError):
            leverage = 0.0
        margin_type = (pos.get("marginType", "cross") or "cross").upper()
        isolated_margin = float(pos.get("isolatedMargin", 0.0))
        position_initial_margin = float(pos.get("positionInitialMargin", 0.0))
        maint_margin = float(pos.get("maintMargin", 0.0))
        nominal = abs(amt) * mark_price
        margin_value = isolated_margin if margin_type == "ISOLATED" and isolated_margin > 0 else position_initial_margin
        if margin_value <= 0 and leverage > 0:
            margin_value = nominal / leverage
        margin_value = max(margin_value, 0.0001)
        wallet = isolated_margin if isolated_margin > 0 else margin_value
        margin_ratio = 0.0
        if wallet > 0:
            margin_ratio = (maint_margin / wallet) * 100 if maint_margin > 0 else (margin_value / wallet) * 100
        roi_percent = (unrealized / margin_value * 100) if margin_value else 0.0
        entry = {
            "symbol": pos.get("symbol"),
            "amount": amt,
            "side": "LONG" if amt > 0 else "SHORT",
            "entryPrice": entry_price,
            "breakEvenPrice": break_even,
            "markPrice": mark_price,
            "liqPrice": liq_price,
            "marginRatio": margin_ratio,
            "marginValue": margin_value,
            "marginType": margin_type.title(),
            "unRealizedProfit": unrealized,
            "roiPercent": roi_percent,
            "leverage": leverage,
        }
        results.append(entry)
    return results


async def fetch_account_balance(creds: Dict[str, str]) -> Dict[str, float]:
    client = await _ensure_client(creds.get("api_key", ""), creds.get("api_secret", ""), creds.get("testnet", True))
    account = await client.futures_account()
    return {
        "totalWalletBalance": float(account.get("totalWalletBalance", 0.0)),
        "availableBalance": float(account.get("availableBalance", 0.0)),
    }


async def fetch_income_history(creds: Dict[str, str], start_time_ms: int) -> List[dict]:
    client = await _ensure_client(creds.get("api_key", ""), creds.get("api_secret", ""), creds.get("testnet", True))
    params = {"limit": 1000}  # REALIZED_PNL + COMMISSION 모두 가져와 순손익 계산
    if start_time_ms:
        params["startTime"] = int(start_time_ms)
    history = await client.futures_income_history(**params)
    return history


async def main():
    creds = {"api_key": "", "api_secret": "", "testnet": True}
    config = EngineConfig()
    await start_engine(config, creds)


if __name__ == "__main__":
    asyncio.run(main())
