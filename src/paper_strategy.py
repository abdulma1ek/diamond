"""Paper trading strategy synced to live Polymarket 5-min BTC markets.

Dynamically resolves the current 5-min market's token IDs, fetches
real YES/NO prices, simulates buying tokens, and evaluates after
the window settles. No real orders placed.
"""

from datetime import timedelta
from decimal import Decimal
import time

from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.objects import Price, Quantity

from src.execution import PolymarketExecutor
from src.market_calibration import EVCalculator, LongshotBiasFilter
from src.paper_trader import PaperTrader
from src.polymarket_feed import fetch_current_market, MarketWindow
from src.pricing import calibration_adjusted_fair_value, fair_value_yes, edge
from src.strategy import SignalGenerationStrategy, MIN_EDGE

# How often (seconds) to check for settled predictions
SETTLE_INTERVAL_S: int = 30

# Print scoreboard every N settle checks
SCOREBOARD_INTERVAL: int = 10  # every 5 minutes (30s * 10)


class PaperTradingStrategy(SignalGenerationStrategy):
    """Live paper trading synced to Polymarket 5-min BTC markets.

    Each 5-minute window, dynamically fetches the current market's
    token IDs and YES/NO prices from Polymarket. Simulates buying
    tokens and settles after the window closes.
    """

    def __init__(
        self,
        config,
        starting_balance: Decimal = Decimal("10.0"),
        polymarket_executor: PolymarketExecutor | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            config=config,
            executor=polymarket_executor,
            polymarket_token_id=None,  # resolved dynamically
            **kwargs,
        )
        self.paper = PaperTrader(starting_balance=starting_balance)
        self._settle_count: int = 0
        self._current_window: MarketWindow | None = None
        self._last_window_ts: int = 0

    def on_start(self) -> None:
        super().on_start()

        # Timer to settle predictions and refresh market window
        self.clock.set_timer(
            name="settle_predictions",
            interval=timedelta(seconds=SETTLE_INTERVAL_S),
            callback=self._on_settle_timer,
        )

        # Fetch initial market window
        self._refresh_market_window()

        market_status = "CONNECTED" if self.executor else "OFFLINE (using model prices)"
        self.log.warning(
            f"Paper trading active | balance=${self.paper.balance:.2f} | "
            f"Polymarket: {market_status}",
            color=LogColor.BLUE,
        )

    def _refresh_market_window(self) -> None:
        """Fetch the current 5-min market from Polymarket."""
        window = fetch_current_market()
        if window and window.start_ts != self._last_window_ts:
            self._current_window = window
            self._last_window_ts = window.start_ts
            self.log.warning(
                f"MARKET WINDOW: {window.question} | "
                f"YES={window.yes_token_id[:20]}... | "
                f"active={window.active}",
                color=LogColor.BLUE,
            )

    def _on_settle_timer(self, event) -> None:
        """Called every 30s to settle predictions and refresh market."""
        if self.latest_spot is None:
            return

        # Settle old predictions
        settled = self.paper.settle_predictions(Decimal(str(self.latest_spot)))

        # Update RiskManager with new balance
        if self.risk_manager:
            self.risk_manager.update_balance(self.paper.balance)

        # Refresh market window (token IDs rotate every 5 min)
        self._refresh_market_window()

        self._settle_count += 1
        if self._settle_count % SCOREBOARD_INTERVAL == 0 or settled:
            self.log.warning(self.paper.get_scoreboard(), color=LogColor.GREEN)

    # Override: fetch real Polymarket prices and paper trade
    def _price_and_log(self, direction: str, signal_score: Decimal) -> None:
        """Override: use live Polymarket prices for paper trading."""
        if self.latest_spot is None or self.realized_vol <= 0:
            return

        # Ensure we have a valid market window before pricing
        if not self._current_window or self._current_window.closed:
            self._refresh_market_window()
            if not self._current_window:
                self.log.error("SIGNAL BLOCKED | No active market window resolved")
                return

        # Oracle-Lag Protection Check
        if self._is_in_no_trade_zone():
            self.log.warning("SIGNAL BLOCKED: Within 30s No-Trade Zone (Oracle Lag Protection)")
            return

        # Compute our model's raw Black-Scholes fair value
        fv_yes_raw = fair_value_yes(
            S=self.latest_spot,
            K=self.latest_spot,
            sigma=self.realized_vol,
            T=self.T,
        )

        # Fetch real Polymarket prices
        market_yes: Decimal | None = None
        market_no: Decimal | None = None
        window = self._current_window

        if self.executor and window:
            # Force refresh to get absolutely fresh prices on signal
            yes_book = self.executor.get_order_book(
                window.yes_token_id, force_refresh=True
            )
            no_book = self.executor.get_order_book(
                window.no_token_id, force_refresh=True
            )

            if yes_book and no_book:
                market_yes = yes_book.best_ask
                market_no = no_book.best_ask

                # Check for stale data (e.g. price haven't moved or are suspect)
                if market_yes == Decimal("0.99") and market_no == Decimal("0.99"):
                    mid_yes = self.executor.get_midpoint(window.yes_token_id)
                    if mid_yes:
                        market_yes = mid_yes.quantize(Decimal("0.01"))
                        market_no = (Decimal("1.0") - market_yes).quantize(
                            Decimal("0.01")
                        )
                        source = "POLYMARKET_MID"
                    else:
                        source = "POLYMARKET_STALE"
                else:
                    source = "POLYMARKET_LIVE"
            elif yes_book:
                market_yes = yes_book.best_ask
                market_no = (Decimal("1.0") - yes_book.best_bid).quantize(
                    Decimal("0.01")
                )
                source = "POLYMARKET_IMPLIED"
            else:
                source = "MODEL_FALLBACK"
        else:
            source = "NO_EXECUTOR"

        # Fallback: use model fair value as synthetic price if no market data
        if market_yes is None or market_no is None:
            market_yes = Decimal(str(fv_yes_raw)).quantize(Decimal("0.01"))
            market_no = (Decimal("1.0") - market_yes).quantize(Decimal("0.01"))
            source = "MODEL_SYNTHETIC"

        # Apply empirical calibration correction from prediction-market-analysis.
        # polymarket_win_rate_by_price shows longshot bias at extreme prices.
        fv_yes = calibration_adjusted_fair_value(
            raw_fv=fv_yes_raw,
            market_price=market_yes,
            adjuster=self.calibration_adjuster,
        )

        # Compute EV-based edge (ev_yes_vs_no framework: EV = win_rate - market_price).
        if direction == "YES":
            ev_val = EVCalculator.ev_yes(float(fv_yes), float(market_yes))
            model_edge_val = Decimal(str(edge(fv_yes, market_yes)))
            target_price = market_yes
        else:
            fv_no_val = 1.0 - float(fv_yes)
            ev_val = EVCalculator.ev_no(float(fv_yes), float(market_yes))
            model_edge_val = Decimal(str(edge(Decimal(str(fv_no_val)), market_no)))
            target_price = market_no

        # Longshot bias filter: raise min edge in <15%/>85% price territory.
        effective_min_edge = self.longshot_filter.adjusted_min_edge(
            base_min_edge=MIN_EDGE,
            market_price_prob=float(target_price),
        )
        is_longshot = self.longshot_filter.is_longshot(float(target_price))
        calib_bias = self.calibration_adjuster.calibration_bias(float(market_yes))

        time_left = int(window.end_ts - time.time()) if window else 0
        window_label = window.question[-30:] if window else "NO_MARKET"

        # Observability Metrics
        self.log.warning(
            f"SIGNAL [{direction}] | edge={model_edge_val:+.4f} ev={ev_val:+.4f} | "
            f"price={target_price:.2f} FV_raw={fv_yes_raw} FV_cal={fv_yes} | "
            f"calib_bias={calib_bias:+.4f} longshot={is_longshot} min_edge={effective_min_edge:.3f} | "
            f"score={signal_score:+.2f} src={source} time={time_left}s | {window_label}",
            color=LogColor.YELLOW,
        )

        # Only trade if both EV and fee-adjusted edge are positive.
        if ev_val <= 0 or model_edge_val < Decimal(str(effective_min_edge)):
            self.log.info(
                f"EDGE INSUFFICIENT | ev={ev_val:+.4f} edge={model_edge_val:.4f} "
                f"min={effective_min_edge:.3f} — skipping trade"
            )
            return

        # Determine stake fraction via Kelly (use calibration-adjusted win prob)
        stake_fraction = Decimal("0.10")
        if self.risk_manager and self.risk_manager.current_balance > 0:
            win_prob = Decimal(str(float(fv_yes) if direction == "YES" else 1.0 - float(fv_yes)))
            token_price = target_price

            if Decimal("0.01") < token_price < Decimal("0.99"):
                kelly = self.risk_manager.kelly_size(
                    win_prob=win_prob,
                    win_payout=Decimal("1.0") - token_price,
                    loss_amount=token_price,
                )
                if kelly > 0:
                    stake_fraction = min(
                        kelly / self.risk_manager.current_balance, Decimal("0.25")
                    )

        # Make the paper trade (fair_value uses calibration-adjusted estimate)
        pred = self.paper.make_prediction(
            direction=direction,
            btc_spot=self.latest_spot,
            market_yes=market_yes,
            market_no=market_no,
            signal_score=signal_score,
            fair_value=Decimal(str(float(fv_yes))),
            stake_fraction=stake_fraction,
        )

        if pred:
            self.log.warning(
                f"EXECUTION | PAPER BUY {direction} | price={pred.entry_price:.2f} | "
                f"qty={pred.num_tokens:.2f} | stake=${pred.stake:.2f} | "
                f"bal=${self.paper.balance:.2f}",
                color=LogColor.GREEN,
            )

    # Block real order placement
    def _place_order(self, *args, **kwargs) -> None:
        """Disabled — paper trading only."""
        pass

    def on_stop(self) -> None:
        if self.latest_spot is not None:
            self.paper.settle_predictions(Decimal(str(self.latest_spot)))
        self.log.warning(self.paper.get_scoreboard(), color=LogColor.GREEN)
        super().on_stop()
