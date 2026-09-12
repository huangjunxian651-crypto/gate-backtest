#!/usr/bin/env python3
"""Local, read-only web interface for the Gate futures backtest prototype."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import gate_backtest as backtest


ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "dashboard.html"


class DashboardHandler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        if self.path in ("/", "/index.html"):
            data = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/api/defaults":
            end = datetime.now(timezone.utc).date()
            start = end - timedelta(days=30)
            self._json(200, {"start": start.isoformat(), "end": end.isoformat()})
            return
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        if self.path not in ("/api/backtest", "/api/gate-readonly-test"):
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 32_000:
                raise ValueError("request body is empty or too large")
            request = json.loads(self.rfile.read(length))
            response = (
                self._test_gate_connection(request)
                if self.path == "/api/gate-readonly-test"
                else self._run_backtest(request)
            )
            self._json(200, response)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(502, {"error": str(exc)})

    @staticmethod
    def _test_gate_connection(request: dict[str, Any]) -> dict[str, Any]:
        key = str(request.get("key", "")).strip()
        secret = str(request.get("secret", "")).strip()
        environment = request.get("environment", "testnet")
        if not key or not secret:
            raise ValueError("请填写 API Key 和 Secret")
        if len(key) > 256 or len(secret) > 256:
            raise ValueError("API Key 或 Secret 格式不正确")
        requested_contracts = request.get("contracts", ["BTC_USDT"])
        if not isinstance(requested_contracts, list) or len(requested_contracts) > 20:
            raise ValueError("合约列表格式不正确")
        requested_contracts = list(
            dict.fromkeys(str(value).strip().upper() for value in requested_contracts if str(value).strip())
        ) or ["BTC_USDT"]
        roots = {
            "testnet": "https://api-testnet.gateapi.io/api/v4",
            "live": backtest.API_ROOT,
        }
        if environment not in roots:
            raise ValueError("API 环境不正确")
        api_root = roots[environment]
        account = backtest.signed_api_get("/futures/usdt/accounts", {}, key, secret, api_root)
        fee_warning = None
        try:
            fee_data = backtest.signed_api_get("/futures/usdt/fee", {}, key, secret, api_root)
        except RuntimeError as exc:
            fee_data = {}
            fee_warning = str(exc)
        if isinstance(fee_data, dict) and "taker_fee" in fee_data:
            fee_map = {requested_contracts[0]: fee_data}
        else:
            fee_map = fee_data if isinstance(fee_data, dict) else {}
        contract_fees = {
            contract: {
                "taker": fee_map[contract].get("taker_fee"),
                "maker": fee_map[contract].get("maker_fee"),
            }
            for contract in requested_contracts
            if isinstance(fee_map.get(contract), dict) and fee_map[contract].get("taker_fee") is not None
        }
        fee_rates = {contract: fee["taker"] for contract, fee in contract_fees.items()}
        primary_fee = next(iter(contract_fees.values()), {})
        return {
            "connected": True,
            "environment": environment,
            "account": {
                "currency": account.get("currency", "USDT"),
                "available": account.get("available"),
                "total": account.get("total"),
                "unrealised_pnl": account.get("unrealised_pnl"),
                "position_mode": account.get("position_mode"),
            },
            "fee": primary_fee,
            "fee_rates": fee_rates,
            "fee_contracts": list(fee_rates),
            "fee_warning": fee_warning,
        }

    @staticmethod
    def _run_backtest(request: dict[str, Any]) -> dict[str, Any]:
        contracts = list(dict.fromkeys(value.strip().upper() for value in request["contracts"] if value.strip()))
        if not contracts:
            raise ValueError("至少选择一个合约")
        if len(contracts) > 20:
            raise ValueError("一次最多回测 20 个合约")

        interval = request["interval"]
        if interval not in backtest.INTERVAL_SECONDS:
            raise ValueError("周期只能选 5 分钟、15 分钟、1 小时或 4 小时")
        start = backtest.parse_utc_date(request["start"])
        end = backtest.parse_utc_date(request["end"])
        if start >= end:
            raise ValueError("结束日期必须晚于开始日期")

        config = backtest.BacktestConfig(
            initial_capital=float(request.get("capital", 10_000)),
            risk_fraction=float(request.get("risk_percent", 0.5)) / 100,
            leverage=float(request.get("leverage", 1)),
            fee_rate=float(request.get("fee_percent", 0.075)) / 100,
            slippage=float(request.get("slippage_bps", 2)) / 10_000,
        )
        if config.initial_capital <= 0 or not 0 < config.risk_fraction < 1 or config.leverage <= 0:
            raise ValueError("资金、风险比例或杠杆参数不正确")
        if config.fee_rate < 0 or config.slippage < 0:
            raise ValueError("手续费和滑点不能为负数")
        personal_fee_rates = request.get("fee_rates", {})
        if not isinstance(personal_fee_rates, dict):
            raise ValueError("个人费率列表格式不正确")

        results: list[dict[str, Any]] = []
        for contract in contracts:
            try:
                spec = backtest.fetch_contract_spec(contract)
                selected_fee = personal_fee_rates.get(contract)
                contract_fee_rate = config.fee_rate if selected_fee is None else float(selected_fee)
                if not 0 <= contract_fee_rate < 1:
                    raise ValueError(f"{contract} 的手续费率不正确")
                contract_config = replace(
                    config,
                    fee_rate=contract_fee_rate,
                    contract_multiplier=spec["quanto_multiplier"],
                    quantity_step=spec["quantity_step"],
                    minimum_quantity=spec["minimum_quantity"],
                    leverage=min(config.leverage, spec["leverage_max"])
                    if spec["leverage_max"] > 0
                    else config.leverage,
                )
                candles = backtest.get_candles(contract, interval, start, end)
                if not candles:
                    results.append({"contract": contract, "error": "这个日期范围没有 K 线"})
                    continue
                funding = backtest.get_funding(contract, interval, start, end)
                for strategy in backtest.STRATEGIES:
                    trades, metrics = backtest.run_backtest(
                        contract, strategy, candles, funding, contract_config, backtest.INTERVAL_SECONDS[interval]
                    )
                    midpoint = len(candles) // 2
                    if midpoint >= 60 and len(candles) - midpoint >= 60:
                        first_candles = candles[:midpoint]
                        second_candles = candles[midpoint:]
                        split_time = second_candles[0].timestamp
                        first_funding = [point for point in funding if point.timestamp < split_time]
                        second_funding = [point for point in funding if point.timestamp >= split_time]
                        _, first_metrics = backtest.run_backtest(
                            contract, strategy, first_candles, first_funding, contract_config,
                            backtest.INTERVAL_SECONDS[interval],
                        )
                        _, second_metrics = backtest.run_backtest(
                            contract, strategy, second_candles, second_funding, contract_config,
                            backtest.INTERVAL_SECONDS[interval],
                        )
                        validation = backtest.assess_validation(metrics, first_metrics, second_metrics)
                    else:
                        validation = {
                            "eligible": False,
                            "reasons": ["日期范围太短，无法拆成两个验证区间"],
                            "first_net_pnl": None,
                            "second_net_pnl": None,
                        }
                    backtest._write_results(contract, interval, strategy, start, end, trades, metrics)
                    results.append(
                        {
                            "contract": contract,
                            "strategy": strategy,
                            "candles": len(candles),
                            "funding_events": len(funding),
                            "contract_spec": spec,
                            "fee_rate": contract_fee_rate,
                            "fee_source": "Gate个人费率" if selected_fee is not None else "回测参数",
                            "metrics": metrics,
                            "validation": validation,
                            "trade_details": [asdict(trade) for trade in trades],
                        }
                    )
            except Exception as exc:
                results.append({"contract": contract, "error": str(exc)})
        return {
            "results": results,
            "start": request["start"],
            "end": request["end"],
            "interval": interval,
            "cost_buffer": config.cost_buffer,
        }

    def log_message(self, format: str, *args: Any) -> None:
        # Keep routine browser requests out of the terminal.
        return


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Start the local Gate backtest dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="local interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"回测面板已启动：http://{args.host}:{args.port}  （按 Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n回测面板已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
