"""Trading strategies. To add one: subclass Strategy, implement signals(), add it to STRATEGIES,
then set `strategy:` in config.yaml. The bot (live) and the backtester both use this same code."""
import pandas as pd


class Strategy:
    warmup = 1  # minimum number of bars needed before signals are meaningful

    def signals(self, bars: pd.DataFrame) -> pd.Series:
        """bars: 1-minute OHLCV, tz-aware index. Returns a Series aligned to bars:
        +1 = go long, -1 = go short, 0 = nothing. Must only use data up to each bar."""
        raise NotImplementedError


class VwapMaCross(Strategy):
    """LONG when close > VWAP and the fast MA crosses above the slow MA; SHORT on the opposite.
    VWAP and the MAs restart each trading day."""

    def __init__(self, fast_ma=9, slow_ma=21):
        if fast_ma >= slow_ma:
            raise ValueError("fast_ma must be smaller than slow_ma")
        self.fast_ma, self.slow_ma = fast_ma, slow_ma
        self.warmup = slow_ma + 1

    def signals(self, bars):
        day = bars.index.tz_convert("America/New_York").date
        close = bars["close"]
        typical = (bars["high"] + bars["low"] + bars["close"]) / 3
        vwap = (typical * bars["volume"]).groupby(day).cumsum() / bars["volume"].groupby(day).cumsum()
        fast = close.groupby(day).transform(lambda s: s.rolling(self.fast_ma).mean())
        slow = close.groupby(day).transform(lambda s: s.rolling(self.slow_ma).mean())

        crossed_up = (fast > slow) & (fast.shift() <= slow.shift())
        crossed_down = (fast < slow) & (fast.shift() >= slow.shift())
        out = pd.Series(0, index=bars.index)
        out[crossed_up & (close > vwap)] = 1
        out[crossed_down & (close < vwap)] = -1
        return out


STRATEGIES = {"vwap_ma_cross": VwapMaCross}


def load_strategy(name: str, params: dict) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy '{name}'. Available: {', '.join(STRATEGIES)}")
    return STRATEGIES[name](**params)
