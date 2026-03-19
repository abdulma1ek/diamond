"""Polymarket 5-minute BTC market feed.

Dynamically resolves the current/next 5-min BTC market's token IDs
from the Polymarket Gamma API. Token IDs rotate every 5 minutes.

Event slug pattern: btc-updown-5m-{unix_timestamp}
where timestamp = start of the 5-min UTC window.
"""

import json
import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com/events"


@dataclass(frozen=True)
class MarketWindow:
    """A single 5-minute BTC prediction market on Polymarket."""

    slug: str
    question: str
    strike: float
    yes_token_id: str
    no_token_id: str
    start_ts: int
    end_ts: int
    active: bool
    closed: bool


def current_window_ts() -> int:
    """Get the unix timestamp for the current 5-min window start."""
    now = int(time.time())
    return now - (now % 300)


def fetch_market_window(window_ts: int | None = None) -> MarketWindow | None:
    """Fetch the Polymarket event for a specific 5-min window.

    Args:
        window_ts: Unix timestamp of the window start.
                   Defaults to current window.

    Returns:
        MarketWindow with token IDs, or None if not found.
    """
    if window_ts is None:
        window_ts = current_window_ts()

    slug = f"btc-updown-5m-{window_ts}"

    try:
        resp = requests.get(
            GAMMA_API,
            params={"slug": slug},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data:
            log.warning(f"No event found for {slug}")
            return None

        event = data[0]
        markets = event.get("markets", [])
        if not markets:
            return None

        market = markets[0]
        token_ids_raw = market.get("clobTokenIds", "[]")
        if isinstance(token_ids_raw, str):
            token_ids = json.loads(token_ids_raw)
        else:
            token_ids = token_ids_raw

        if len(token_ids) < 2:
            return None

        # Extract strike from question: "Will Bitcoin be above $67,261.20..."
        question = market.get("question", "")
        strike = 0.0
        try:
            import re

            match = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", question)
            if match:
                strike = float(match.group(1).replace(",", ""))
        except:
            pass

        return MarketWindow(
            slug=slug,
            question=question,
            strike=strike,
            yes_token_id=token_ids[0],
            no_token_id=token_ids[1],
            start_ts=window_ts,
            end_ts=window_ts + 300,
            active=market.get("active", False),
            closed=market.get("closed", False),
        )
    except Exception as e:
        log.error(f"Failed to fetch market window {slug}: {e}")
        return None


def fetch_current_market() -> MarketWindow | None:
    """Fetch the current 5-min BTC market."""
    return fetch_market_window(current_window_ts())


def fetch_next_market() -> MarketWindow | None:
    """Fetch the next 5-min BTC market."""
    return fetch_market_window(current_window_ts() + 300)
