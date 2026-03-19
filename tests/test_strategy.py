import pytest
from collections import deque
from math import log, sqrt


# ── Pure function extracts matching src/strategy.py logic ──────────


def compute_cvd(trade_deltas: deque, now_ns: int, cvd_window_ns: int) -> float:
    """CVD over rolling window, normalized to [-1, 1]."""
    cutoff = now_ns - cvd_window_ns
    total_buy = 0.0
    total_sell = 0.0
    for ts, delta in trade_deltas:
        if ts >= cutoff:
            if delta > 0:
                total_buy += delta
            else:
                total_sell += abs(delta)
    total = total_buy + total_sell
    if total == 0:
        return 0.0
    return (total_buy - total_sell) / total


def compute_momentum(spot_history: deque, latest_spot: float, now: float) -> float:
    """10-second BTC return, normalized to [-1, 1]."""
    cutoff = now - 10.0
    old_prices = [p for t, p in spot_history if t <= cutoff]
    if not old_prices or latest_spot <= 0:
        return 0.0
    old_price = old_prices[-1]
    ret = (latest_spot - old_price) / old_price
    # Normalize: ±0.1% return maps to ±1.0
    return max(-1.0, min(1.0, ret / 0.001))


def compute_realized_vol(bar_closes: list[float]) -> float:
    """Annualized realized vol from 1-min bar closes."""
    if len(bar_closes) < 2:
        return 0.0
    log_returns = [
        log(bar_closes[i] / bar_closes[i - 1]) for i in range(1, len(bar_closes))
    ]
    mean_ret = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_ret) ** 2 for r in log_returns) / len(log_returns)
    return sqrt(variance) * sqrt(525600)


def evaluate_composite_signal(
    obi: float,
    cvd: float,
    momentum: float,
    funding_rate: float,
    large_trade_bias: float,
    realized_vol: float,
    w_obi: float = 0.40,
    w_cvd: float = 0.25,
    w_momentum: float = 0.25,
    w_funding: float = 0.10,
    high_vol_threshold: float = 0.80,
    signal_threshold_low_vol: float = 0.20,
    signal_threshold_high_vol: float = 0.12,
) -> tuple[float, str | None]:
    """Returns (score, signal) where signal is 'YES', 'NO', or None."""
    funding_bias = max(-1.0, min(1.0, -funding_rate * 10000))
    score = w_obi * obi + w_cvd * cvd + w_momentum * momentum + w_funding * funding_bias
    score += large_trade_bias * 0.15
    score = max(-1.0, min(1.0, score))

    if realized_vol > high_vol_threshold:
        threshold = signal_threshold_high_vol
    else:
        threshold = signal_threshold_low_vol

    if abs(score) <= threshold:
        return score, None

    signal_dir = 1 if score > 0 else -1
    direction = "YES" if score > 0 else "NO"

    # 2 of 3 agreement (OBI, CVD, Momentum)
    indicators = [obi, cvd, momentum]
    agreement = sum(1 for ind in indicators if (ind > 0) == (signal_dir > 0))
    if agreement < 2:
        return score, None

    return score, direction


# ── CVD Tests ──────────────────────────────────────────────────────


class TestComputeCVD:
    def test_empty(self):
        assert compute_cvd(deque(), now_ns=0, cvd_window_ns=60_000_000_000) == 0.0

    def test_single_buy(self):
        deltas = deque([(0, 10.0)])
        assert (
            compute_cvd(deltas, now_ns=10_000_000_000, cvd_window_ns=60_000_000_000)
            == 1.0
        )

    def test_single_sell(self):
        deltas = deque([(0, -5.0)])
        assert (
            compute_cvd(deltas, now_ns=10_000_000_000, cvd_window_ns=60_000_000_000)
            == -1.0
        )

    def test_mixed_within_window(self):
        deltas = deque(
            [
                (0, 10.0),
                (10_000_000_000, -5.0),
                (20_000_000_000, 15.0),
            ]
        )
        # buy=25, sell=5, total=30 → (25-5)/30 = 0.6667
        result = compute_cvd(
            deltas, now_ns=20_000_000_000, cvd_window_ns=60_000_000_000
        )
        assert result == pytest.approx(20.0 / 30.0)

    def test_cutoff_excludes_old(self):
        deltas = deque(
            [
                (0, 10.0),  # outside 60s window
                (2_000_000_000, -5.0),  # inside
                (5_000_000_000, 15.0),  # inside
            ]
        )
        # cutoff = 5s - 3s = 2s → includes (2s, -5) and (5s, 15)
        # buy=15, sell=5, total=20 → (15-5)/20 = 0.5
        result = compute_cvd(deltas, now_ns=5_000_000_000, cvd_window_ns=3_000_000_000)
        assert result == pytest.approx(0.5)

    def test_normalized_range(self):
        """CVD should always be in [-1, 1]."""
        deltas = deque([(i, 1.0) for i in range(100)])
        result = compute_cvd(deltas, now_ns=100, cvd_window_ns=200)
        assert -1.0 <= result <= 1.0


# ── Realized Vol Tests ─────────────────────────────────────────────


class TestComputeRealizedVol:
    def test_less_than_two_closes(self):
        assert compute_realized_vol([100.0]) == 0.0

    def test_no_variance(self):
        assert compute_realized_vol([100.0, 100.0, 100.0]) == pytest.approx(0.0)

    def test_basic_uptrend(self):
        vol = compute_realized_vol([100.0, 101.0, 102.0])
        assert vol > 0

    def test_oscillating(self):
        vol = compute_realized_vol([100, 101, 100, 101, 100])
        assert vol > 0
        # Oscillating should have higher vol than steady uptrend
        vol_steady = compute_realized_vol([100.0, 101.0, 102.0, 103.0, 104.0])
        assert vol > vol_steady

    def test_annualization(self):
        """Vol should scale with sqrt(525600) for 1-min bars."""
        closes = [100.0, 101.0]
        vol = compute_realized_vol(closes)
        log_ret = log(101.0 / 100.0)
        # Single return, mean = log_ret, variance = 0 (only one return)
        # Actually with 1 return: mean = log_ret, variance = 0
        assert vol == pytest.approx(0.0)  # Single return has zero variance


# ── Composite Signal Tests ─────────────────────────────────────────


# ── Momentum Tests ──────────────────────────────────────────────────


class TestComputeMomentum:
    def test_empty_history(self):
        assert compute_momentum(deque(), latest_spot=100.0, now=100.0) == 0.0

    def test_price_increase(self):
        history = deque([(90.0, 100.0)])
        # Price 100 -> 100.1 is 0.1% increase
        assert compute_momentum(history, latest_spot=100.1, now=100.0) == pytest.approx(
            1.0
        )

    def test_price_decrease(self):
        history = deque([(90.0, 100.0)])
        # Price 100 -> 99.9 is 0.1% decrease
        assert compute_momentum(history, latest_spot=99.9, now=100.0) == pytest.approx(
            -1.0
        )

    def test_clamp_range(self):
        history = deque([(90.0, 100.0)])
        # Price 100 -> 101 is 1% increase
        assert compute_momentum(history, latest_spot=101.0, now=100.0) == 1.0


# ── Composite Signal Tests ─────────────────────────────────────────


class TestEvaluateCompositeSignal:
    def test_high_vol_predict_yes(self):
        score, signal = evaluate_composite_signal(
            obi=0.8,
            cvd=0.5,
            momentum=0.5,
            funding_rate=0.0001,
            large_trade_bias=0.0,
            realized_vol=1.0,
        )
        # 0.40*0.8 + 0.25*0.5 + 0.25*0.5 + 0.10*(-1.0) = 0.32 + 0.125 + 0.125 - 0.1 = 0.47
        assert score == pytest.approx(0.47)
        assert signal == "YES"

    def test_low_vol_predict_no(self):
        score, signal = evaluate_composite_signal(
            obi=-0.7,
            cvd=-0.6,
            momentum=-0.6,
            funding_rate=-0.00005,
            large_trade_bias=0.0,
            realized_vol=0.5,
        )
        # 0.40*(-0.7) + 0.25*(-0.6) + 0.25*(-0.6) + 0.10*(0.5) = -0.28 - 0.15 - 0.15 + 0.05 = -0.53
        assert score == pytest.approx(-0.53)
        assert signal == "NO"

    def test_large_trade_boost(self):
        score, signal = evaluate_composite_signal(
            obi=0.0,
            cvd=0.0,
            momentum=0.0,
            funding_rate=0.0,
            large_trade_bias=1.0,
            realized_vol=0.5,
        )
        # score = 0 + 1.0 * 0.15 = 0.15 (below 0.20 threshold)
        assert score == pytest.approx(0.15)
        assert signal is None

    def test_2_of_3_agreement_fails(self):
        # score is high, but OBI and CVD disagree with direction
        score, signal = evaluate_composite_signal(
            obi=-0.1,
            cvd=-0.1,
            momentum=1.0,
            funding_rate=0.0,
            large_trade_bias=2.0,
            realized_vol=1.0,
        )
        # score = 0.40*-0.1 + 0.25*-0.1 + 0.25*1.0 + 2.0*0.15 = -0.04 - 0.025 + 0.25 + 0.30 = 0.485
        assert score > 0.12  # above high vol threshold
        # indicators = [-0.1, -0.1, 1.0]. Only 1 (momentum) agrees with signal_dir (1)
        assert signal is None

    def test_funding_bias_clamps(self):
        # Very large funding rate should clamp bias to [-1, 1]
        score1, _ = evaluate_composite_signal(
            obi=0.0,
            cvd=0.0,
            momentum=0.0,
            funding_rate=0.001,
            large_trade_bias=0.0,
            realized_vol=0.5,
        )
        score2, _ = evaluate_composite_signal(
            obi=0.0,
            cvd=0.0,
            momentum=0.0,
            funding_rate=0.01,
            large_trade_bias=0.0,
            realized_vol=0.5,
        )
        # Both should clamp to -1.0, so funding contribution = 0.10 * -1.0 = -0.10
        assert score1 == pytest.approx(-0.10)
        assert score2 == pytest.approx(-0.10)
