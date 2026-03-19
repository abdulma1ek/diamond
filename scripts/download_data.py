"""Download historical 1-minute BTCUSDT-PERP candles from Binance Futures.

Fetches klines in 1500-bar chunks and writes them to a CSV file
that the backtest runner can load.

Usage:
    uv run python scripts/download_data.py --days 7
    uv run python scripts/download_data.py --days 30 --output data/btcusdt_1m.csv
"""

import argparse
import csv
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BINANCE_FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
MAX_LIMIT = 1500  # Binance max per request


def fetch_klines(
    start_ms: int,
    end_ms: int,
    limit: int = MAX_LIMIT,
) -> list[list]:
    """Fetch klines from Binance Futures REST API."""
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": limit,
    }
    resp = requests.get(BINANCE_FUTURES_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def download_candles(days: int, output_path: Path) -> int:
    """Download `days` worth of 1-min candles and save to CSV."""
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_candles: list[list] = []
    cursor_ms = start_ms

    print(f"Downloading {days} days of {SYMBOL} 1-min candles...")
    print(f"  From: {start_dt.isoformat()}")
    print(f"  To:   {end_dt.isoformat()}")

    while cursor_ms < end_ms:
        klines = fetch_klines(cursor_ms, end_ms)
        if not klines:
            break

        all_candles.extend(klines)

        # Move cursor past the last candle
        last_close_time = klines[-1][6]  # close time in ms
        cursor_ms = last_close_time + 1

        print(f"  Fetched {len(all_candles)} candles so far...", end="\r")

        # Rate limit: Binance allows 2400 req/min, be conservative
        time.sleep(0.25)

    print(f"\n  Total candles: {len(all_candles)}")

    # Write CSV
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "timestamp_ms",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        )
        for k in all_candles:
            # k = [open_time, open, high, low, close, volume, close_time, ...]
            writer.writerow(
                [
                    k[0],  # open_time ms
                    k[1],  # open
                    k[2],  # high
                    k[3],  # low
                    k[4],  # close
                    k[5],  # volume
                ]
            )

    print(f"  Saved to: {output_path}")
    return len(all_candles)


def main():
    parser = argparse.ArgumentParser(description="Download Binance BTCUSDT 1m candles")
    parser.add_argument("--days", type=int, default=7, help="Number of days to fetch")
    parser.add_argument(
        "--output",
        type=str,
        default="data/btcusdt_1m.csv",
        help="Output CSV path",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    count = download_candles(args.days, output_path)
    print(f"Done. {count} candles written to {output_path}")


if __name__ == "__main__":
    main()
