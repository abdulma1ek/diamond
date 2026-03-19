# Polymarket 5-Min BTC Market Research

## 1. Fee Structures on Polymarket 5-min BTC Markets

Polymarket has introduced taker fees for its crypto markets, including BTC. The fee structure is variable and is highest when the probability is around 50%, decreasing as the probability approaches 0% or 100%.

- **Maximum Taker Fee:** For 15-minute crypto markets, the maximum effective taker fee can be up to 3%. It is highly probable that 5-minute markets follow a similar fee structure as "short-duration crypto markets." Exact explicit fees for 5-min markets are not clearly stated, but it can be inferred they fall within the same range (up to 3%). This fee is intended to fund a Maker Rebates Program, incentivizing liquidity providers.

## 2. Liquidity and Spread

Polymarket operates on an order book model, meaning liquidity relies on other traders placing bids and asks.

- **Generally Low Liquidity:** Short-term markets (under a day) on Polymarket often experience low liquidity. Over 63% of such markets have no trading volume within 24 hours. This is a significant concern for 5-minute markets, as low liquidity can lead to:
    - Misleading prices.
    - Difficulty in executing trades at desired prices (higher slippage).
    - Challenges in exiting positions.
- **Liquidity Incentives:** Polymarket offers a Liquidity Rewards Program to incentivize users to place limit orders near the market's midpoint, aiming to improve market depth.
- **Exploitation Risk:** Instances of traders exploiting thin liquidity during low-volume periods have been observed.

## 3. UMA Liveness Window for 5-min Resolution Markets

This is a critical distinction for 5-minute BTC markets:

- **Chainlink Oracles for Immediate Settlement:** Polymarket's 5-minute BTC markets **do not** use UMA's Optimistic Oracle (OO) for settlement. Instead, they utilize **Chainlink oracles for immediate settlement** at the close of each 5-minute interval.
- **No UMA Liveness Period:** Consequently, there is **no UMA liveness period** (challenge period) to account for with the UMA Data Verification Mechanism (DVM) for these specific markets. This significantly simplifies the bot's logic regarding settlement for 5-minute markets. For other markets that *do* use UMA's Optimistic Oracle, a typical liveness period of 2 hours exists, which can escalate to 24-72 hours if disputed.

## 4. How does `py-clob-client` work for fetching Polymarket order books?

`py-clob-client` is a Python library designed to interact with the Polymarket Central Limit Order Book (CLOB) API. It provides an interface to fetch order book data, as well as current prices and midpoints.

- **Installation:** `pip install py-clob-client`
- **Basic Usage:**
    - Initialize `ClobClient` with the CLOB API endpoint (e.g., `"https://clob.polymarket.com"`).
    - Use `client.get_order_book(token_id)` to retrieve the order book for a specific market `token_id`.
    - Use `client.get_midpoint(token_id)` or `client.get_price(token_id, side="BUY"|"SELL")` for quick price checks.
- **"Gotchas":**
    - **Stale Data:** `get_order_book()` has been reported to occasionally return stale data. `get_midpoint()` and `get_price()` might be more reliable for current price information.
    - **Token ID:** Requires a valid Polymarket market `token_id`.

## 5. Any rate limits or gotchas for the Polymarket API?

Polymarket's API implements rate limits, often throttling requests instead of outright rejecting them, which can lead to increased latency. Limits are enforced based on sliding time windows and vary significantly based on the access tier and specific API endpoint.

- **Rate Limit Tiers:**
    - **Unverified (Default):** Limited to 100 transactions per day.
    - **Verified:** Offers 3,000 transactions per day (requires manual approval).
    - **Partner:** Provides the highest limits, potentially unlimited transactions per day.
- **API-Specific Limits:**
    - **Data API:** Offers "generous rate limits (up to 1,000 calls/hour for non-trading queries)" for basic access.
    - **Free Tiers:** Generally limited to 100 requests per minute, which can scale with trading volume.
- **Best Practices/Mitigation:**
    - Implement robust client-side rate limiting and exponential backoff for retries.
    - Utilize caching to reduce the number of API calls.
    - Employ efficient data retrieval methods: pagination (`limit`, `offset`), filtering by market slug or tags.
    - Store API responses and log usage/errors for monitoring.
    - Consider dedicated RPC endpoints to mitigate Polygon RPC slowness.