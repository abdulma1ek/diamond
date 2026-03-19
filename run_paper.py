"""Paper trading runner — live data, real Polymarket prices, simulated balance.

Connects to Binance Futures for real-time signals and fetches live
YES/NO prices from Polymarket's rotating 5-min BTC markets.
Token IDs are resolved dynamically each window — no static config needed.

No real orders are placed. No real money is risked.

Usage:
    uv run python run_paper.py
    uv run python run_paper.py --balance 10 --duration 3600
    uv run python run_paper.py --balance 10 --duration 0    # run indefinitely
"""

import argparse
import asyncio
import os
import signal
import sys

from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
from nautilus_trader.live.node import TradingNode
from nautilus_trader.trading.config import StrategyConfig

from src.config import get_trading_config, get_polymarket_executor
from src.polymarket_feed import fetch_current_market
from src.risk import RiskManager
from src.log_engine import LogEngine

from dotenv import load_dotenv

load_dotenv()


async def main(balance: float, duration: int):
    if not os.getenv("BINANCE_API_KEY") or not os.getenv("BINANCE_API_SECRET"):
        print("Error: BINANCE_API_KEY and/or BINANCE_API_SECRET not found in .env")
        sys.exit(1)

    # Polymarket executor (read-only — no orders placed)
    executor = get_polymarket_executor()

    # Check current market
    current = fetch_current_market()

    print("=" * 60)
    print("  5-MIN BTC PREDICTION BOT — PAPER TRADING")
    print("=" * 60)
    print(f"  Starting balance:  ${balance:.2f}")
    if duration > 0:
        print(f"  Duration:          {duration}s ({duration // 60} min)")
    else:
        print("  Duration:          indefinite (Ctrl+C to stop)")
    print(f"  Binance data:      BTCUSDT-PERP (live)")
    if executor:
        print(f"  Polymarket:        CONNECTED (read-only)")
    else:
        print(f"  Polymarket:        OFFLINE (using model prices)")
    if current:
        print(f"  Current market:    {current.question}")
    else:
        print(f"  Current market:    (fetching on start...)")
    print(f"  Token resolution:  DYNAMIC (auto-rotates every 5 min)")
    print(f"  Orders:            SIMULATED ONLY — no real money")
    print(f"  Dashboard:         streamlit run dashboard.py")
    print(f"  Logs:              logs/thinking.jsonl (model reasoning)")
    print(f"                     logs/technical.jsonl (system events)")
    print(f"                     logs/trades.jsonl (trade ledger)")
    print("=" * 60)
    print()

    config = get_trading_config()
    node = TradingNode(config=config)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)

    # V3 Production Infrastructure: Bridge Polymarket WSS to Nautilus
    from src.polymarket_wss import PolymarketWSSActor

    asset_ids = []
    if current:
        asset_ids = [current.yes_token_id, current.no_token_id]

    poly_actor = PolymarketWSSActor(asset_ids=asset_ids)
    node.trader.add_actor(poly_actor)

    from decimal import Decimal

    decimal_balance = Decimal(str(balance))

    risk_manager = RiskManager()
    risk_manager.on_daily_open(decimal_balance)

    log_engine = LogEngine(clear_on_start=True)
    log_engine.technical(
        "session_start",
        {
            "balance": balance,
            "duration": duration,
            "polymarket": "connected" if executor else "offline",
            "market": current.question if current else "pending",
        },
    )

    from src.v3_strategy import V3ProductionStrategy

    strategy_config = StrategyConfig(strategy_id="V3-PROD-001")
    strategy = V3ProductionStrategy(
        config=strategy_config,
        starting_balance=decimal_balance,
        polymarket_executor=executor,
        polymarket_actor=poly_actor,
        risk_manager=risk_manager,
        log_engine=log_engine,
    )

    node.build()
    node.trader.add_strategy(strategy)

    try:
        # run_async blocks forever (awaits engine queue tasks),
        # so launch it as a background task and control duration ourselves.
        node_task = asyncio.create_task(node.run_async())

        if duration > 0:
            print(f"\nRunning for {duration} seconds...")
            await asyncio.sleep(duration)
        else:
            print("\nRunning indefinitely. Press Ctrl+C to stop.")
            stop_event = asyncio.Event()
            loop = asyncio.get_event_loop()
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)
            await stop_event.wait()

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopping...")
    finally:
        # Graceful shutdown of the trading node
        await node.stop_async()

        # Stop the background runner task
        if node_task:
            node_task.cancel()
            try:
                # Wait for cancellation to propagate
                await asyncio.wait_for(node_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        node.dispose()

        # Final scoreboard
        print(strategy.paper.get_scoreboard())
        if strategy.paper.predictions:
            print("\n  Full Trade History:")
            print(
                f"  {'#':>3}  {'Dir':3}  {'Result':6}  "
                f"{'Entry':>6}  {'Tokens':>7}  {'Stake':>7}  "
                f"{'BTC Entry':>12}  {'BTC Exit':>12}  {'PnL':>8}"
            )
            print("  " + "-" * 82)
            for i, pred in enumerate(strategy.paper.predictions, 1):
                if pred.settled:
                    result = "WIN" if pred.correct else "LOSS"
                else:
                    result = "PEND"
                print(
                    f"  {i:3d}  {pred.direction:3s}  {result:6s}  "
                    f"${pred.entry_price:.2f}  {pred.num_tokens:7.2f}  "
                    f"${pred.stake:6.2f}  "
                    f"${pred.btc_entry:11,.2f}  ${pred.btc_exit:11,.2f}  "
                    f"${pred.pnl:+7.2f}"
                )
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paper trade 5-min BTC predictions")
    parser.add_argument(
        "--balance", type=float, default=10.0, help="Starting balance in USD"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=3600,
        help="Run duration in seconds (0 = indefinite)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args.balance, args.duration))
    except RuntimeError as e:
        if str(e) != "Event loop stopped before Future completed.":
            raise
    except KeyboardInterrupt:
        pass
