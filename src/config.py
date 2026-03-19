import os
import requests
import logging

from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
from nautilus_trader.adapters.binance.config import (
    BinanceDataClientConfig,
    BinanceInstrumentProviderConfig,
)
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.system.config import LoggingConfig

from src.execution import PolymarketExecutor

log = logging.getLogger(__name__)


def check_binance_auth(key: str | None, secret: str | None) -> bool:
    """Check if Binance keys have correct permissions for Futures data."""
    if not key or not secret:
        return False
    try:
        # A simple request to check if keys are rejected by Futures API
        headers = {"X-MBX-APIKEY": key}
        # Hit an endpoint that requires permissions but is low-impact
        response = requests.get(
            "https://fapi.binance.com/fapi/v1/positionSide/dual", headers=headers
        )
        if response.status_code == 401 or (
            response.status_code == 400 and response.json().get("code") == -2015
        ):
            print(
                "WARNING: Binance API keys lack Futures permissions. Falling back to public data."
            )
            return False
        return True
    except Exception:
        return False


def get_trading_config() -> TradingNodeConfig:
    """TradingNodeConfig for live signal generation via Binance and Polymarket."""

    binance_key = os.getenv("BINANCE_API_KEY")
    binance_secret = os.getenv("BINANCE_API_SECRET")

    # Validation fallback
    if binance_key and binance_key.strip():
        if not check_binance_auth(binance_key, binance_secret):
            binance_key = None
            binance_secret = None
    else:
        binance_key = None
        binance_secret = None

    # Ensure logs directory exists
    os.makedirs("logs", exist_ok=True)

    config = TradingNodeConfig(
        instance_id=UUID4(),
        trader_id=TraderId(str(UUID4())),
        logging=LoggingConfig(
            log_level="INFO",
            log_level_file="INFO",
            log_directory="logs",
            log_file_name="nautilus",
            log_colors=False,
            clear_log_file=True,
        ),
        data_clients={
            "BINANCE": BinanceDataClientConfig(
                api_key=binance_key,
                api_secret=binance_secret,
                account_type=BinanceAccountType.USDT_FUTURES,
                instrument_provider=BinanceInstrumentProviderConfig(
                    load_ids=("BTCUSDT-PERP.BINANCE",)
                ),
            ),
        },
        # Infrastructure: Polygon RPC configuration
        exec_clients={
            "POLYMARKET": {
                "rpc_url": os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com"),
                "chain_id": 137,
            }
        },
    )
    return config


def get_polymarket_executor() -> PolymarketExecutor:
    """Build a PolymarketExecutor from environment variables."""
    return PolymarketExecutor(
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY"),
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
        chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
        rpc_url=os.getenv("POLYGON_RPC_URL"),
    )
