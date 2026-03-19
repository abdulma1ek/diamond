# tests/verify_env.py

import asyncio

try:
    # Core components for basic node functionality
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.config import TradingNodeConfig

    # Core enums and objects (including from Rust bindings)
    from nautilus_trader.model.enums import AccountType
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.common.enums import LogColor
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.trading.strategy import Strategy

    print("Successfully imported core NautilusTrader components.")
    print("LogColor from Rust bindings:", LogColor.BLUE)
    print(
        "Environment verification successful: Core components and Rust bindings are accessible."
    )

except ImportError as e:
    print(f"Environment verification failed: Could not import a core component.")
    print(f"Error: {e}")
    raise


async def main():
    print("Running dummy async main to ensure event loop is functional.")
    await asyncio.sleep(0.01)
    print("Async main completed.")


if __name__ == "__main__":
    asyncio.run(main())
