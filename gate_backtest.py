#!/usr/bin/env python3
"""Read-only Gate USDT futures backtester; uses only the Python standard library."""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://api.gateio.ws/api/v4"
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
STRATEGIES = ("trend", "breakout", "mean_reversion", "trend_pullback")
CACHE_DIR = Path(".cache")
RESULTS_DIR = Path("results")


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0


@dataclass(frozen=True)
class FundingPoint:
    timestamp: int
    rate: float


@dataclass(frozen=True)
class Signal:
    side: int  # 1 = long, -1 = short
    stop_distance: float
    stop_price: float | None = None


@dataclass
class Position:
    side: int
    entry_timestamp: int
    entry_price: float
    stop_price: float
    target_price: float
    quantity: float
    entry_fee: float
    funding_pnl: float = 0.0
    risk_budget: float = 0.0
    stop_risk_estimate: float = 0.0


@dataclass(frozen=True)
class Trade:
    contract: str
    strategy: str
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: float
    gross_pnl: float
    fees: float
    funding_pnl: float
    net_pnl: float
    exit_reason: str
    stop_price: float = 0.0
    target_price: float = 0.0
    planned_reward_risk: float = 2.0
    risk_budget: float = 0.0
    stop_risk_estimate: float = 0.0


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 10_000.0
    risk_fraction: float = 0.005
    leverage: float = 1.0
    fee_rate: float = 0.00075
    slippage: float = 0.0002
    reward_risk: float = 2.0
    stop_buffer_atr: float = 0.25
    atr_period: int = 14
    cost_buffer: float = 3.0
    contract_multiplier: float = 1.0
    quantity_step: float = 0.0
    minimum_quantity: float = 0.0


def api_get(path: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    url = f"{API_ROOT}{path}?{urlencode(params)}"
    request_headers = {"User-Agent": "gate-backtest-prototype/0.1"}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gate API returned HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Gate API: {exc.reason}") from exc


def api_signature(method: str, path: str, query: str, body: bytes, timestamp: str, secret: str) -> str:
    payload_hash = hashlib.sha512(body).hexdigest()
    message = f"{method}\n{path}\n{query}\n{payload_hash}\n{timestamp}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha512).hexdigest()


def signed_api_get(path: str, params: dict[str, Any], key: str, secret: str, api_root: str = API_ROOT) -> Any:
    query = urlencode(params)
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    signed_path = f"/api/v4{path}"
    signature = api_signature("GET", signed_path, query, b"", timestamp, secret)
    url = f"{api_root}{path}{'?' + query if query else ''}"
    request = Request(
        url,
        headers={"Accept": "application/json", "KEY": key, "Timestamp": timestamp, "SIGN": signature},
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gate 拒绝连接（HTTP {exc.code}）：{detail[:300]}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接 Gate：{exc.reason}") from exc


def fetch_contract_spec(contract: str) -> dict[str, Any]:
    row = api_get(
        f"/futures/usdt/contracts/{contract}", {}, {"X-Gate-Size-Decimal": "1"}
    )
    if not isinstance(row, dict):
        raise RuntimeError(f"Unexpected contract response for {contract}: {row!r}")
    multiplier = float(row.get("quanto_multiplier", 0) or 0)
    if multiplier <= 0:
        raise RuntimeError(f"Gate returned an invalid contract multiplier for {contract}")
    minimum_contract_text = str(row.get("order_size_min", 0) or 0)
    minimum_contracts = float(minimum_contract_text)
    decimal_places = len(minimum_contract_text.partition(".")[2].rstrip("0"))
    decimal_supported = bool(row.get("enable_decimal", False))
    return {
        "quanto_multiplier": multiplier,
        "quantity_step": 10 ** -decimal_places if decimal_supported and decimal_places else 1.0,
        "minimum_quantity": minimum_contracts,
        "enable_decimal": decimal_supported,
        "price_tick": float(row.get("order_price_round", 0) or 0),
        "maker_fee_rate": float(row.get("maker_fee_rate", 0) or 0),
        "taker_fee_rate": float(row.get("taker_fee_rate", 0) or 0),
        "leverage_max": float(row.get("leverage_max", 0) or 0),
        "status": row.get("status", ""),
    }


def parse_utc_date(value: str) -> int:
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc
    return int(datetime.combine(parsed_date, time.min, tzinfo=timezone.utc).timestamp())


def _candle_from_api(row: dict[str, Any]) -> Candle:
    return Candle(
        timestamp=int(row["t"]),
        open=float(row["o"]),
        high=float(row["h"]),
        low=float(row["l"]),
        close=float(row["c"]),
        volume=float(row.get("v", 0) or 0),
        turnover=float(row.get("sum", 0) or 0),
    )


def fetch_candles(contract: str, interval: str, start: int, end: int) -> list[Candle]:
    step = INTERVAL_SECONDS[interval]
    page_size = 1000
    cursor = start
    collected: dict[int, Candle] = {}
    while cursor < end:
        page_end = min(end - 1, cursor + step * (page_size - 1))
        rows = api_get(
            "/futures/usdt/candlesticks",
            {"contract": contract, "interval": interval, "from": cursor, "to": page_end},
        )
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected candle response for {contract}: {rows!r}")
        page = [_candle_from_api(row) for row in rows]
        for candle in page:
            if start <= candle.timestamp < end:
                collected[candle.timestamp] = candle
        if page:
            next_cursor = max(c.timestamp for c in page) + step
            if next_cursor <= cursor:
                raise RuntimeError("Gate candle pagination did not advance")
            cursor = next_cursor
        else:
            cursor = page_end + 1
    return [collected[key] for key in sorted(collected)]


def fetch_funding(contract: str, start: int, end: int) -> list[FundingPoint]:
    # Gate defaults to 100 rows; request enough for a 90-day, 8-hour funding history.
    chunk_seconds = 90 * 24 * 60 * 60
    collected: dict[int, FundingPoint] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(end - 1, cursor + chunk_seconds)
        rows = api_get(
            "/futures/usdt/funding_rate",
            {"contract": contract, "from": cursor, "to": chunk_end, "limit": 1000},
        )
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected funding response for {contract}: {rows!r}")
        for row in rows:
            point = FundingPoint(timestamp=int(row["t"]), rate=float(row["r"]))
            if start <= point.timestamp < end:
                collected[point.timestamp] = point
        cursor = chunk_end + 1
    return [collected[key] for key in sorted(collected)]


def _cache_stem(contract: str, interval: str, start: int, end: int) -> str:
    return f"{contract}_{interval}_{start}_{end}"


def _load_candles_csv(path: Path) -> list[Candle]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            Candle(
                timestamp=int(row["timestamp"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                turnover=float(row.get("turnover", 0) or 0),
            )
            for row in csv.DictReader(handle)
        ]


def _save_candles_csv(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=Candle.__dataclass_fields__.keys())
        writer.writeheader()
        writer.writerows(asdict(candle) for candle in candles)


def get_candles(contract: str, interval: str, start: int, end: int) -> list[Candle]:
    path = CACHE_DIR / f"{_cache_stem(contract, interval, start, end)}_candles.csv"
    if path.exists():
        return _load_candles_csv(path)
    candles = fetch_candles(contract, interval, start, end)
    _save_candles_csv(path, candles)
    return candles


def get_funding(contract: str, interval: str, start: int, end: int) -> list[FundingPoint]:
    path = CACHE_DIR / f"{_cache_stem(contract, interval, start, end)}_funding.csv"
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            return [FundingPoint(int(row["timestamp"]), float(row["rate"])) for row in csv.DictReader(handle)]
    funding = fetch_funding(contract, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("timestamp", "rate"))
        writer.writeheader()
        writer.writerows(asdict(point) for point in funding)
    return funding


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    result: list[float] = []
    current = values[0] if values else 0.0
    for value in values:
        current = alpha * value + (1 - alpha) * current
        result.append(current)
    return result


def _atr(candles: list[Candle], period: int) -> list[float | None]:
    true_ranges: list[float] = []
    for index, candle in enumerate(candles):
        previous_close = candles[index - 1].close if index else candle.close
        true_ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
    result: list[float | None] = [None] * len(candles)
    for index in range(period - 1, len(candles)):
        result[index] = statistics.fmean(true_ranges[index - period + 1 : index + 1])
    return result


def _hourly_candles(candles: list[Candle], interval_seconds: int) -> list[Candle]:
    if interval_seconds <= 0 or 3600 % interval_seconds:
        raise ValueError("trend_pullback needs candles that divide evenly into one hour")
    bars_per_hour = 3600 // interval_seconds
    by_time = {candle.timestamp: candle for candle in candles}
    hourly: list[Candle] = []
    buckets = sorted({candle.timestamp // 3600 * 3600 for candle in candles})
    for bucket in buckets:
        expected = [bucket + offset * interval_seconds for offset in range(bars_per_hour)]
        rows = [by_time.get(timestamp) for timestamp in expected]
        if any(candle is None for candle in rows):
            continue
        complete = [candle for candle in rows if candle is not None]
        hourly.append(
            Candle(
                timestamp=bucket,
                open=complete[0].open,
                high=max(candle.high for candle in complete),
                low=min(candle.low for candle in complete),
                close=complete[-1].close,
                volume=sum(candle.volume for candle in complete),
                turnover=sum(candle.turnover for candle in complete),
            )
        )
    return hourly


def _signal_at_stop(side: int, stop_price: float, reference_price: float) -> Signal | None:
    stop_distance = side * (reference_price - stop_price)
    if stop_distance <= 0:
        return None
    return Signal(side, stop_distance, stop_price)


def generate_signals(
    strategy: str,
    candles: list[Candle],
    atr_period: int = 14,
    interval_seconds: int | None = None,
    stop_buffer_atr: float = 0.25,
) -> list[Signal | None]:
    signals: list[Signal | None] = [None] * len(candles)
    atr_values = _atr(candles, atr_period)
    closes = [candle.close for candle in candles]

    if strategy == "trend":
        fast = _ema(closes, 20)
        slow = _ema(closes, 50)
        for index in range(50, len(candles)):
            if candles[index].volume <= 0 or not atr_values[index]:
                continue
            side = 0
            if fast[index - 1] <= slow[index - 1] and fast[index] > slow[index]:
                side = 1
            elif fast[index - 1] >= slow[index - 1] and fast[index] < slow[index]:
                side = -1
            if side:
                swing = candles[index - 19 : index + 1]
                extreme = min(c.low for c in swing) if side == 1 else max(c.high for c in swing)
                stop = extreme - side * atr_values[index] * stop_buffer_atr
                signals[index] = _signal_at_stop(side, stop, closes[index])

    elif strategy == "breakout":
        lookback = 20
        for index in range(lookback + 1, len(candles)):
            if candles[index].volume <= 0 or not atr_values[index]:
                continue
            upper = max(c.high for c in candles[index - lookback : index])
            lower = min(c.low for c in candles[index - lookback : index])
            previous_upper = max(c.high for c in candles[index - lookback - 1 : index - 1])
            previous_lower = min(c.low for c in candles[index - lookback - 1 : index - 1])
            side = 1 if closes[index] > upper and closes[index - 1] <= previous_upper else 0
            if closes[index] < lower and closes[index - 1] >= previous_lower:
                side = -1
            if side:
                stop = (upper if side == 1 else lower) - side * atr_values[index] * stop_buffer_atr
                signals[index] = _signal_at_stop(side, stop, closes[index])

    elif strategy == "mean_reversion":
        period = 20
        for index in range(period, len(candles)):
            if candles[index].volume <= 0 or not atr_values[index]:
                continue
            previous_window = closes[index - period : index]
            current_window = closes[index - period + 1 : index + 1]
            previous_mean = statistics.fmean(previous_window)
            current_mean = statistics.fmean(current_window)
            previous_deviation = statistics.pstdev(previous_window)
            current_deviation = statistics.pstdev(current_window)
            previous_lower = previous_mean - 2 * previous_deviation
            previous_upper = previous_mean + 2 * previous_deviation
            current_lower = current_mean - 2 * current_deviation
            current_upper = current_mean + 2 * current_deviation
            side = 1 if closes[index - 1] < previous_lower and closes[index] >= current_lower else 0
            if closes[index - 1] > previous_upper and closes[index] <= current_upper:
                side = -1
            if side:
                setup = candles[index - 1 : index + 1]
                extreme = min(c.low for c in setup) if side == 1 else max(c.high for c in setup)
                stop = extreme - side * atr_values[index] * stop_buffer_atr
                signals[index] = _signal_at_stop(side, stop, closes[index])

    elif strategy == "trend_pullback":
        if interval_seconds is None:
            deltas = [b.timestamp - a.timestamp for a, b in zip(candles, candles[1:]) if b.timestamp > a.timestamp]
            interval_seconds = min(deltas) if deltas else 900
        if interval_seconds >= 3600:
            hourly = candles
            trend_bar_seconds = interval_seconds
        else:
            hourly = _hourly_candles(candles, interval_seconds)
            trend_bar_seconds = 3600
        if len(hourly) < 201:
            return signals
        hourly_closes = [candle.close for candle in hourly]
        hourly_fast = _ema(hourly_closes, 50)
        hourly_slow = _ema(hourly_closes, 200)
        base_ema = _ema(closes, 20)
        high_index = -1
        for index in range(20, len(candles)):
            available_at = candles[index].timestamp + interval_seconds
            while high_index + 1 < len(hourly) and hourly[high_index + 1].timestamp + trend_bar_seconds <= available_at:
                high_index += 1
            if candles[index].volume <= 0 or not atr_values[index] or high_index < 200:
                continue
            side = 0
            if hourly_fast[high_index] > hourly_slow[high_index]:
                if closes[index - 1] <= base_ema[index - 1] and closes[index] > base_ema[index]:
                    side = 1
            elif hourly_fast[high_index] < hourly_slow[high_index]:
                if closes[index - 1] >= base_ema[index - 1] and closes[index] < base_ema[index]:
                    side = -1
            if side:
                start = index - 1
                while start > 0 and (
                    closes[start - 1] <= base_ema[start - 1]
                    if side == 1
                    else closes[start - 1] >= base_ema[start - 1]
                ):
                    start -= 1
                pullback = candles[start : index + 1]
                extreme = min(c.low for c in pullback) if side == 1 else max(c.high for c in pullback)
                stop = extreme - side * atr_values[index] * stop_buffer_atr
                signals[index] = _signal_at_stop(side, stop, closes[index])
    else:
        raise ValueError(f"unknown strategy: {strategy}")

    return signals


def open_position(signal: Signal, candle: Candle, equity: float, config: BacktestConfig) -> Position | None:
    if equity <= 0 or signal.stop_distance <= 0 or candle.open <= 0 or config.contract_multiplier <= 0:
        return None
    entry = candle.open * (1 + signal.side * config.slippage)
    stop = signal.stop_price if signal.stop_price is not None else entry - signal.side * signal.stop_distance
    stop_distance = signal.side * (entry - stop)
    if stop_distance <= 0:
        return None
    if stop <= 0:
        return None
    stop_exit = stop * (1 - signal.side * config.slippage)
    multiplier = config.contract_multiplier
    loss_per_contract_at_stop = multiplier * (
        -signal.side * (stop_exit - entry) + config.fee_rate * (entry + stop_exit)
    )
    if loss_per_contract_at_stop <= 0:
        return None
    risk_budget = equity * config.risk_fraction
    quantity_for_risk = risk_budget / loss_per_contract_at_stop
    quantity_for_leverage = equity * config.leverage / (entry * multiplier)
    quantity = min(quantity_for_risk, quantity_for_leverage)
    if config.quantity_step > 0:
        quantity = math.floor(quantity / config.quantity_step + 1e-12) * config.quantity_step
    if quantity < config.minimum_quantity:
        return None
    if quantity <= 0:
        return None
    target = entry + signal.side * stop_distance * config.reward_risk
    return Position(
        side=signal.side,
        entry_timestamp=candle.timestamp,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=quantity,
        entry_fee=quantity * multiplier * entry * config.fee_rate,
        risk_budget=risk_budget,
        stop_risk_estimate=quantity * loss_per_contract_at_stop,
    )


def opening_exit_reference(position: Position, price: float) -> tuple[float, str] | None:
    if position.side == 1:
        if price <= position.stop_price:
            return price, "stop_gap"
        if price >= position.target_price:
            return price, "target_gap"
    else:
        if price >= position.stop_price:
            return price, "stop_gap"
        if price <= position.target_price:
            return price, "target_gap"
    return None


def intrabar_exit_reference(position: Position, candle: Candle) -> tuple[float, str] | None:
    stop_hit = candle.low <= position.stop_price if position.side == 1 else candle.high >= position.stop_price
    target_hit = candle.high >= position.target_price if position.side == 1 else candle.low <= position.target_price
    # If both levels trade inside one candle, assume the stop was hit first.
    if stop_hit:
        return position.stop_price, "stop"
    if target_hit:
        return position.target_price, "target"
    return None


def close_position(
    contract: str,
    strategy: str,
    position: Position,
    reference_price: float,
    timestamp: int,
    reason: str,
    config: BacktestConfig,
) -> tuple[Trade, float]:
    exit_price = reference_price * (1 - position.side * config.slippage)
    multiplier = config.contract_multiplier
    exit_fee = position.quantity * multiplier * exit_price * config.fee_rate
    gross_pnl = position.quantity * multiplier * position.side * (exit_price - position.entry_price)
    total_fees = position.entry_fee + exit_fee
    net_pnl = gross_pnl - total_fees + position.funding_pnl
    trade = Trade(
        contract=contract,
        strategy=strategy,
        side="long" if position.side == 1 else "short",
        entry_time=datetime.fromtimestamp(position.entry_timestamp, timezone.utc).isoformat(),
        exit_time=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        entry_price=position.entry_price,
        exit_price=exit_price,
        quantity=position.quantity,
        gross_pnl=gross_pnl,
        fees=total_fees,
        funding_pnl=position.funding_pnl,
        net_pnl=net_pnl,
        exit_reason=reason,
        stop_price=position.stop_price,
        target_price=position.target_price,
        planned_reward_risk=config.reward_risk,
        risk_budget=position.risk_budget,
        stop_risk_estimate=position.stop_risk_estimate,
    )
    # Entry fees and funding have already been applied to account equity.
    return trade, gross_pnl - exit_fee


def calculate_metrics(trades: list[Trade], equity_curve: list[float], initial_capital: float) -> dict[str, Any]:
    winners = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    losers = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
    gross_wins = sum(winners)
    gross_losses = abs(sum(losers))
    peak = initial_capital
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    avg_win = statistics.fmean(winners) if winners else None
    avg_loss = abs(statistics.fmean(losers)) if losers else None
    return {
        "trades": len(trades),
        "win_rate": len(winners) / len(trades) if trades else None,
        "average_win_net": avg_win,
        "average_loss_net": avg_loss,
        "average_win_loss_ratio": avg_win / avg_loss if avg_win is not None and avg_loss else None,
        "profit_factor": gross_wins / gross_losses if gross_losses else None,
        "net_pnl": equity_curve[-1] - initial_capital if equity_curve else 0.0,
        "ending_equity": equity_curve[-1] if equity_curve else initial_capital,
        "max_drawdown": max_drawdown,
    }


def run_backtest(
    contract: str,
    strategy: str,
    candles: list[Candle],
    funding: list[FundingPoint],
    config: BacktestConfig,
    interval_seconds: int | None = None,
) -> tuple[list[Trade], dict[str, Any]]:
    if len(candles) < 60:
        raise ValueError(f"至少需要 60 根 K 线才能回测；当前只有 {len(candles)} 根")
    # The candle interval is inferred from adjacent timestamps so cached data remains self-describing.
    deltas = [b.timestamp - a.timestamp for a, b in zip(candles, candles[1:]) if b.timestamp > a.timestamp]
    bar_seconds = min(deltas) if deltas else 300
    signals = generate_signals(
        strategy, candles, config.atr_period, interval_seconds or bar_seconds, config.stop_buffer_atr
    )
    equity = config.initial_capital
    equity_curve: list[float] = [equity]
    trades: list[Trade] = []
    position: Position | None = None
    pending: Signal | None = None
    funding_index = 0
    for index, candle in enumerate(candles):
        had_position_at_open = position is not None
        if position is not None:
            gap_exit = opening_exit_reference(position, candle.open)
            if gap_exit:
                trade, cash_change = close_position(contract, strategy, position, gap_exit[0], candle.timestamp, gap_exit[1], config)
                trades.append(trade)
                equity += cash_change
                position = None

        entered_this_bar = False
        if position is None and pending is not None and not had_position_at_open and candle.volume > 0:
            planned_entry = candle.open * (1 + pending.side * config.slippage)
            stop_distance = (
                pending.side * (planned_entry - pending.stop_price)
                if pending.stop_price is not None
                else pending.stop_distance
            )
            round_trip_cost = candle.open * 2 * (config.fee_rate + config.slippage)
            target_distance = stop_distance * config.reward_risk
            if stop_distance <= 0 or target_distance <= round_trip_cost * config.cost_buffer:
                pending = None
                position = None
            else:
                position = open_position(pending, candle, equity, config)
            pending = None
            entered_this_bar = position is not None
            if position is not None:
                equity -= position.entry_fee

        # Apply historical funding events in this bar to positions that existed at the event time.
        bar_end = candle.timestamp + bar_seconds
        while funding_index < len(funding) and funding[funding_index].timestamp < bar_end:
            point = funding[funding_index]
            if position is not None and point.timestamp > position.entry_timestamp:
                cashflow = (
                    -position.side * position.quantity * config.contract_multiplier * candle.open * point.rate
                )
                equity += cashflow
                position.funding_pnl += cashflow
            funding_index += 1

        if position is not None:
            exit_point = intrabar_exit_reference(position, candle)
            if exit_point:
                trade, cash_change = close_position(contract, strategy, position, exit_point[0], candle.timestamp + bar_seconds, exit_point[1], config)
                trades.append(trade)
                equity += cash_change
                position = None

        if position is None and not entered_this_bar and candle.volume > 0:
            pending = signals[index]
        else:
            pending = None

        marked_equity = equity
        if position is not None:
            marked_equity += (
                position.side * position.quantity * config.contract_multiplier * (candle.close - position.entry_price)
            )
        equity_curve.append(marked_equity)

    if position is not None:
        last = candles[-1]
        trade, cash_change = close_position(
            contract, strategy, position, last.close, last.timestamp + bar_seconds, "end_of_data", config
        )
        trades.append(trade)
        equity += cash_change
        equity_curve.append(equity)

    return trades, calculate_metrics(trades, equity_curve, config.initial_capital)


def assess_validation(full: dict[str, Any], first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if full["trades"] < 50:
        reasons.append("总样本少于 50 笔")
    if full["profit_factor"] is None or full["profit_factor"] <= 1.1:
        reasons.append("整体利润因子未超过 1.10")
    if full["max_drawdown"] > 0.10:
        reasons.append("最大回撤超过 10%")
    if first["net_pnl"] <= 0 or second["net_pnl"] <= 0:
        reasons.append("前后两个验证区间没有同时盈利")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "first_net_pnl": first["net_pnl"],
        "second_net_pnl": second["net_pnl"],
    }


def _write_results(contract: str, interval: str, strategy: str, start: int, end: int, trades: list[Trade], metrics: dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{contract}_{interval}_{strategy}_{start}_{end}"
    trade_path = RESULTS_DIR / f"{stem}_trades.csv"
    with trade_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=Trade.__dataclass_fields__.keys())
        writer.writeheader()
        writer.writerows(asdict(trade) for trade in trades)
    summary_path = RESULTS_DIR / f"{stem}_summary.json"
    summary_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary_path


def _format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest simple strategies on Gate USDT-settled futures.")
    parser.add_argument("--contracts", required=True, help="comma-separated Gate contracts, e.g. BTC_USDT,TSLA_USDT")
    parser.add_argument("--interval", choices=INTERVAL_SECONDS, default="15m")
    parser.add_argument("--start", required=True, type=parse_utc_date, help="inclusive UTC date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=parse_utc_date, help="exclusive UTC date, YYYY-MM-DD")
    parser.add_argument("--strategy", choices=(*STRATEGIES, "all"), default="all")
    parser.add_argument("--capital", type=float, default=10_000.0, help="starting USDT equity")
    parser.add_argument("--risk-fraction", type=float, default=0.005, help="maximum planned equity risk per trade; default 0.5%%")
    parser.add_argument("--leverage", type=float, default=1.0, help="notional cap as a multiple of equity")
    parser.add_argument("--fee-rate", type=float, default=0.00075, help="fee per side; default 7.5 bps (taker assumption)")
    parser.add_argument("--slippage-bps", type=float, default=2.0, help="slippage per side in basis points")
    args = parser.parse_args(argv)

    if args.start >= args.end:
        parser.error("--start must be earlier than --end")
    if args.capital <= 0 or args.risk_fraction <= 0 or args.risk_fraction >= 1 or args.leverage <= 0:
        parser.error("capital/leverage must be positive and risk-fraction must be between 0 and 1")
    if args.fee_rate < 0 or args.slippage_bps < 0:
        parser.error("fees and slippage cannot be negative")

    strategies = STRATEGIES if args.strategy == "all" else (args.strategy,)
    config = BacktestConfig(
        initial_capital=args.capital,
        risk_fraction=args.risk_fraction,
        leverage=args.leverage,
        fee_rate=args.fee_rate,
        slippage=args.slippage_bps / 10_000,
    )
    contracts = [value.strip().upper() for value in args.contracts.split(",") if value.strip()]
    if not contracts:
        parser.error("provide at least one contract")

    try:
        for contract in contracts:
            spec = fetch_contract_spec(contract)
            contract_config = replace(
                config,
                contract_multiplier=spec["quanto_multiplier"],
                quantity_step=spec["quantity_step"],
                minimum_quantity=spec["minimum_quantity"],
                leverage=min(config.leverage, spec["leverage_max"])
                if spec["leverage_max"] > 0
                else config.leverage,
            )
            candles = get_candles(contract, args.interval, args.start, args.end)
            if not candles:
                print(f"{contract}: no candles in requested range", file=sys.stderr)
                continue
            funding = get_funding(contract, args.interval, args.start, args.end)
            print(f"{contract}: loaded {len(candles)} candles and {len(funding)} funding events")
            for strategy in strategies:
                trades, metrics = run_backtest(
                    contract, strategy, candles, funding, contract_config, INTERVAL_SECONDS[args.interval]
                )
                path = _write_results(contract, args.interval, strategy, args.start, args.end, trades, metrics)
                ratio = metrics["average_win_loss_ratio"]
                ratio_text = "n/a" if ratio is None else f"{ratio:.2f}:1"
                print(
                    f"  {strategy:15} trades={metrics['trades']:4} "
                    f"net={metrics['net_pnl']:10.2f} USDT "
                    f"win={_format_percent(metrics['win_rate'])} "
                    f"avg-win/loss={ratio_text} "
                    f"max-DD={_format_percent(metrics['max_drawdown'])} "
                    f"report={path}"
                )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
