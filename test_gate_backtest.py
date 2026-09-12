import unittest
from unittest.mock import patch

from app import DashboardHandler
from gate_backtest import (
    BacktestConfig,
    Candle,
    Position,
    Signal,
    Trade,
    api_signature,
    assess_validation,
    calculate_metrics,
    close_position,
    fetch_contract_spec,
    generate_signals,
    intrabar_exit_reference,
    open_position,
    run_backtest,
)


class BacktestTests(unittest.TestCase):
    def test_gate_signature_matches_known_value(self):
        signature = api_signature(
            "GET", "/api/v4/futures/usdt/accounts", "", b"", "1541993715", "secret"
        )
        self.assertEqual(
            signature,
            "1470000d29bd50aff57ae588e6df27e7386ae91173426f44652037edea24690750510f49c4a9bb4812812dbb4194fad1cecb75e75d64b5a8a4d5607eb8aafdfa",
        )

    def test_target_is_two_times_stop_distance(self):
        config = BacktestConfig(slippage=0, fee_rate=0, reward_risk=2, risk_fraction=0.01)
        position = open_position(Signal(side=1, stop_distance=10), Candle(0, 100, 100, 100, 100, 1), 10_000, config)
        self.assertIsNotNone(position)
        assert position is not None
        self.assertAlmostEqual(position.entry_price - position.stop_price, 10)
        self.assertAlmostEqual(position.target_price - position.entry_price, 20)

    def test_structural_stop_sets_target_and_quantity_from_actual_entry_distance(self):
        candle = Candle(0, 100, 100, 100, 100, 1)
        config = BacktestConfig(
            risk_fraction=0.01, leverage=100, fee_rate=0, slippage=0,
        )
        near_stop = open_position(
            Signal(side=1, stop_distance=1, stop_price=95), candle, 10_000, config
        )
        far_stop = open_position(
            Signal(side=1, stop_distance=1, stop_price=90), candle, 10_000, config
        )

        self.assertIsNotNone(near_stop)
        self.assertIsNotNone(far_stop)
        assert near_stop is not None and far_stop is not None
        self.assertEqual(near_stop.stop_price, 95)
        self.assertEqual(near_stop.target_price, 110)
        self.assertEqual(near_stop.quantity, 20)
        self.assertEqual(far_stop.quantity, 10)

    def test_structural_stop_breached_before_entry_skips_trade(self):
        position = open_position(
            Signal(side=1, stop_distance=5, stop_price=101),
            Candle(0, 100, 102, 99, 101, 1), 10_000,
            BacktestConfig(slippage=0, fee_rate=0),
        )
        self.assertIsNone(position)

    def test_position_sizing_keeps_estimated_stop_loss_within_budget(self):
        config = BacktestConfig(risk_fraction=0.005, fee_rate=0.00075, slippage=0.0002)
        equity = 10_000
        position = open_position(Signal(side=1, stop_distance=10), Candle(0, 100, 100, 100, 100, 1), equity, config)
        self.assertIsNotNone(position)
        assert position is not None

        trade, _ = close_position("BTC_USDT", "trend", position, position.stop_price, 1, "stop", config)

        self.assertLessEqual(position.stop_risk_estimate, equity * config.risk_fraction)
        self.assertAlmostEqual(position.target_price - position.entry_price, 2 * (position.entry_price - position.stop_price))
        self.assertAlmostEqual(abs(trade.net_pnl), position.stop_risk_estimate)
        self.assertLessEqual(position.quantity * position.entry_price, equity * config.leverage)

    def test_position_sizing_respects_contract_step_and_minimum(self):
        candle = Candle(0, 100, 100, 100, 100, 1)
        rounded = open_position(
            Signal(side=1, stop_distance=10), candle, 10_000,
            BacktestConfig(risk_fraction=0.01, fee_rate=0, slippage=0, quantity_step=3),
        )
        self.assertIsNotNone(rounded)
        assert rounded is not None
        self.assertEqual(rounded.quantity, 9)
        too_small = open_position(
            Signal(side=1, stop_distance=10), candle, 10_000,
            BacktestConfig(risk_fraction=0.01, fee_rate=0, slippage=0, minimum_quantity=11),
        )
        self.assertIsNone(too_small)

    def test_gate_contract_quantity_and_multiplier_are_distinct(self):
        with patch(
            "gate_backtest.api_get",
            return_value={
                "quanto_multiplier": "0.01",
                "order_size_min": "0.1",
                "enable_decimal": True,
                "order_price_round": "0.01",
                "taker_fee_rate": "0.00075",
                "maker_fee_rate": "0",
                "leverage_max": "100",
                "status": "trading",
            },
        ) as get:
            spec = fetch_contract_spec("ETH_USDT")

        self.assertEqual(spec["quanto_multiplier"], 0.01)
        self.assertEqual(spec["minimum_quantity"], 0.1)
        self.assertEqual(spec["quantity_step"], 0.1)
        get.assert_called_once_with(
            "/futures/usdt/contracts/ETH_USDT", {}, {"X-Gate-Size-Decimal": "1"}
        )

    def test_multiplier_scales_position_risk_and_usdt_profit(self):
        config = BacktestConfig(
            initial_capital=1_000,
            risk_fraction=0.01,
            leverage=1,
            fee_rate=0,
            slippage=0,
            contract_multiplier=0.01,
            quantity_step=0.1,
            minimum_quantity=0.1,
        )
        position = open_position(
            Signal(side=1, stop_distance=1), Candle(0, 100, 100, 100, 100, 1), 1_000, config
        )
        self.assertIsNotNone(position)
        assert position is not None
        self.assertEqual(position.quantity, 1_000)
        self.assertAlmostEqual(position.stop_risk_estimate, 10)

        trade, _ = close_position("ETH_USDT", "trend", position, 101, 1, "test", config)
        self.assertAlmostEqual(trade.gross_pnl, 10)

    def test_stop_wins_when_stop_and_target_touch_same_candle(self):
        position = Position(1, 0, 100, 95, 110, 1, 0)
        candle = Candle(300, 100, 112, 94, 105, 10)
        self.assertEqual(intrabar_exit_reference(position, candle), (95, "stop"))

    def test_realized_average_win_loss_ratio_uses_net_results(self):
        def trade(net_pnl):
            return Trade("BTC_USDT", "trend", "long", "a", "b", 100, 100, 1, net_pnl, 0, 0, net_pnl, "test")

        metrics = calculate_metrics([trade(20), trade(10), trade(-10)], [100, 120, 130, 120], 100)
        self.assertAlmostEqual(metrics["average_win_loss_ratio"], 1.5)

    def test_trend_pullback_waits_for_closed_hourly_trend_and_reentry(self):
        candles = []
        for index in range(210 * 4):
            close = 100 + index * 0.1
            if index == 205 * 4 + 2:
                close -= 15
            timestamp = index * 900
            candles.append(Candle(timestamp, close, close + 0.1, close - 0.1, close, 1))

        signals = generate_signals("trend_pullback", candles, interval_seconds=900)

        self.assertIsNotNone(signals[205 * 4 + 3])
        assert signals[205 * 4 + 3] is not None
        self.assertEqual(signals[205 * 4 + 3].side, 1)
        self.assertLess(signals[205 * 4 + 3].stop_price, candles[205 * 4 + 2].low)

    def test_breakout_stop_uses_the_broken_range_boundary(self):
        candles = [Candle(i * 3600, 100, 101, 99, 100, 1) for i in range(21)]
        candles.append(Candle(21 * 3600, 100, 103, 100, 102, 1))

        signal = generate_signals("breakout", candles)[21]

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, 1)
        self.assertGreater(signal.stop_price, 100)
        self.assertLess(signal.stop_price, 101)

    def test_mean_reversion_stop_uses_the_excursion_extreme(self):
        candles = [Candle(i * 3600, 100, 101, 99, 100, 1) for i in range(19)]
        candles.extend([
            Candle(19 * 3600, 85, 90, 79, 80, 1),
            Candle(20 * 3600, 80, 100, 78, 99, 1),
        ])

        signal = generate_signals("mean_reversion", candles)[20]

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, 1)
        self.assertLess(signal.stop_price, 78)

    def test_trend_stop_uses_recent_swing_extreme(self):
        candles = [Candle(i * 3600, 100, 101, 99, 100, 1) for i in range(51)]
        candles.append(Candle(51 * 3600, 100, 103, 100, 102, 1))

        signal = generate_signals("trend", candles)[51]

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, 1)
        self.assertLess(signal.stop_price, 99)

    def test_trend_pullback_ignores_incomplete_hourly_bars(self):
        candles = [
            Candle(index * 900, 100 + index * 0.1, 101 + index * 0.1, 99 + index * 0.1, 100 + index * 0.1, 1)
            for index in range(201 * 4)
        ]
        candles.pop(2)

        signals = generate_signals("trend_pullback", candles, interval_seconds=900)

        self.assertTrue(all(signal is None for signal in signals))

    def test_cost_filter_skips_signals_when_costs_exceed_target_space(self):
        candles = []
        for index in range(210 * 4):
            close = 100 + index * 0.1
            if index == 205 * 4 + 2:
                close -= 15
            candles.append(Candle(index * 900, close, close + 0.1, close - 0.1, close, 1))
        config = BacktestConfig(fee_rate=0.2, slippage=0.1)

        trades, _ = run_backtest("BTC_USDT", "trend_pullback", candles, [], config, 900)

        self.assertEqual(trades, [])

    def test_validation_requires_sample_profit_factor_drawdown_and_two_profitable_halves(self):
        passing = {"trades": 60, "profit_factor": 1.2, "max_drawdown": 0.08, "net_pnl": 100}
        self.assertTrue(assess_validation(passing, passing, passing)["eligible"])
        failing_second = dict(passing, net_pnl=-1)
        result = assess_validation(passing, passing, failing_second)
        self.assertFalse(result["eligible"])
        self.assertIn("前后两个验证区间没有同时盈利", result["reasons"])

    @patch("app.backtest.signed_api_get")
    def test_readonly_connection_returns_safe_account_summary_and_fee(self, signed_get):
        signed_get.side_effect = [
            {"currency": "USDT", "available": "123.45", "total": "150", "unrealised_pnl": "2"},
            {"BTC_USDT": {"taker_fee": "0.0004", "maker_fee": "-0.0001"}},
        ]
        result = DashboardHandler._test_gate_connection(
            {
                "environment": "testnet",
                "key": "example-key",
                "secret": "example-secret",
                "contracts": ["BTC_USDT"],
            }
        )
        self.assertTrue(result["connected"])
        self.assertEqual(result["account"]["available"], "123.45")
        self.assertEqual(result["fee"]["taker"], "0.0004")
        self.assertEqual(result["fee_rates"], {"BTC_USDT": "0.0004"})
        self.assertNotIn("key", result)
        self.assertNotIn("secret", result)

    @patch("app.backtest._write_results")
    @patch("app.backtest.run_backtest")
    @patch("app.backtest.get_funding", return_value=[])
    @patch("app.backtest.get_candles")
    @patch("app.backtest.fetch_contract_spec")
    def test_web_backtest_uses_each_contracts_personal_fee_and_multiplier(
        self, fetch_spec, get_candles, _get_funding, run, _write_results
    ):
        fetch_spec.side_effect = [
            {"quanto_multiplier": 0.0001, "quantity_step": 1, "minimum_quantity": 1, "leverage_max": 100},
            {"quanto_multiplier": 0.01, "quantity_step": 0.1, "minimum_quantity": 0.1, "leverage_max": 50},
        ]
        get_candles.return_value = [
            Candle(index * 3600, 100, 101, 99, 100, 1) for index in range(120)
        ]
        metrics = {
            "net_pnl": 10,
            "trades": 60,
            "profit_factor": 1.2,
            "max_drawdown": 0.08,
            "average_win_loss_ratio": 2.0,
        }
        run.return_value = ([], metrics)

        result = DashboardHandler._run_backtest(
            {
                "contracts": ["BTC_USDT", "ETH_USDT"],
                "interval": "1h",
                "start": "2026-01-01",
                "end": "2026-01-08",
                "fee_percent": 0.075,
                "fee_rates": {"BTC_USDT": "0.0004", "ETH_USDT": "0.0006"},
            }
        )

        by_contract = {row["contract"]: row for row in result["results"] if row.get("strategy") == "trend"}
        self.assertEqual(by_contract["BTC_USDT"]["fee_rate"], 0.0004)
        self.assertEqual(by_contract["ETH_USDT"]["fee_rate"], 0.0006)
        self.assertEqual(by_contract["BTC_USDT"]["fee_source"], "Gate个人费率")
        first_btc_config = next(call.args[4] for call in run.call_args_list if call.args[0] == "BTC_USDT")
        first_eth_config = next(call.args[4] for call in run.call_args_list if call.args[0] == "ETH_USDT")
        self.assertEqual(first_btc_config.contract_multiplier, 0.0001)
        self.assertEqual(first_eth_config.contract_multiplier, 0.01)
        self.assertEqual(first_btc_config.fee_rate, 0.0004)
        self.assertEqual(first_eth_config.fee_rate, 0.0006)


if __name__ == "__main__":
    unittest.main()
