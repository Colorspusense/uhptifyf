"""The three screens and the Trade Quality Score (TQS)."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .stats import Snapshot, ema


def statistical_screen(s: Snapshot, st) -> list:
    """Screen 1. Returns the list of failed conditions; empty means passed."""
    fails = []
    if s.corr <= st.min_corr:
        fails.append(f"corr {s.corr:.2f} <= {st.min_corr}")
    if s.rolling_state(st.breakdown_corr) == "breakdown":
        fails.append(f"rolling corr broke down ({s.corr_roll:.2f})")
    if s.coint_p >= st.max_coint_pvalue:
        fails.append(f"not cointegrated (p={s.coint_p:.3f})")
    if s.beta <= 0:
        fails.append(f"beta {s.beta:.2f} <= 0")
    if s.beta_change >= st.max_beta_change:
        fails.append(f"beta unstable ({s.beta_change:.0%})")
    if s.z > st.entry_z:
        fails.append(f"Z {s.z:.2f} > {st.entry_z}")
    elif s.z < st.extreme_z and not st.allow_extreme_z:
        fails.append(f"Z {s.z:.2f} is extreme, check the news")
    elif st.require_z_turn and s.z < s.z_prev:
        fails.append(f"Z still falling ({s.z_prev:.2f} -> {s.z:.2f})")
    return fails


def pair_broken(s: Snapshot, st) -> bool:
    """The statistical model no longer holds: grounds for closing an open position."""
    return s.rolling_state(st.breakdown_corr) == "breakdown" or s.beta_change > st.breakdown_beta_change


def market_state(index_close: pd.Series) -> pd.Series:
    """Screen 2 for every day: 'strong' (index > EMA200 and EMA50 > EMA200), 'neutral' or 'weak'."""
    e50, e200 = ema(index_close, 50), ema(index_close, 200)
    state = pd.Series("weak", index=index_close.index)
    state[index_close > e200] = "neutral"
    state[(index_close > e200) & (e50 > e200)] = "strong"
    state.iloc[:200] = "weak"  # EMA200 is not meaningful before it has enough data
    return state


@dataclass
class EntryResult:
    ok: bool
    points: int
    reasons: list


def entry_screen(bars: pd.DataFrame, book: tuple | None, en) -> EntryResult:
    """Screen 3 on completed 15-minute bars. book = (best_bid, best_ask) or None."""
    if len(bars) < en.min_bars:
        return EntryResult(False, 0, [f"only {len(bars)} intraday bars"])
    c = bars["close"]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    last, prev = bars.iloc[-1], bars.iloc[-2]
    reasons = []
    if not (last.close > e20.iloc[-1] > e50.iloc[-1] > e200.iloc[-1]):
        reasons.append("15m trend not aligned (price > EMA20 > EMA50 > EMA200)")

    n = en.pullback_lookback
    win = bars.iloc[-n - 1:-1]
    avg_range = float((bars["high"] - bars["low"]).iloc[-15:-1].mean())
    if avg_range > 0 and (last.close - last.open) > en.long_candle_atr * avg_range:
        reasons.append("last candle is a long rising candle")

    down = win[win["close"] < win["open"]]
    touched = bool((win["low"] <= e20.iloc[-n - 1:-1] * (1 + en.ema_touch_tolerance)).any())
    had_pullback = touched or len(down) >= 2
    finished = last.close > last.open and last.close > prev.close
    pullback_done = had_pullback and finished
    if en.require_pullback and not pullback_done:
        reasons.append("no completed pullback")

    before = bars["volume"].iloc[-2 * n - 1:-n - 1].mean()
    volume_ok = len(down) > 0 and down["volume"].mean() < before and last.volume > down["volume"].mean()

    book_ok = False
    if book and book[0] and book[1]:
        book_ok = (book[1] - book[0]) / ((book[1] + book[0]) / 2) <= en.max_book_spread
        if not book_ok:
            reasons.append("order book spread too wide")

    points = 2 * pullback_done + 2 * bool(volume_ok) + 1 * book_ok
    return EntryResult(not reasons, int(points), reasons)


def tqs_score(s: Snapshot, market: str, entry_points: int, st) -> int:
    """Trade Quality Score, 0-100, by the scale of chapter 11."""
    score = 0
    score += 20 if s.corr >= 0.95 else 18 if s.corr >= 0.93 else 15 if s.corr >= 0.90 else 10 if s.corr >= 0.85 else 0
    score += {"stable": 10, "slightly_down": 6, "down": 2, "breakdown": 0}[s.rolling_state(st.breakdown_corr)]
    score += 15 if s.coint_p < 0.01 else 12 if s.coint_p < 0.05 else 5 if s.coint_p < 0.10 else 0
    score += 10 if s.beta_change < 0.05 else 7 if s.beta_change < 0.10 else 3 if s.beta_change < 0.20 else 0
    if s.z < -3.0:
        score += 10
    elif s.z <= -2.5:
        score += 20
    elif s.z <= -2.0:
        score += 15
    hl = s.half_life
    score += 10 if hl <= 5 else 8 if hl <= 10 else 5 if hl <= 20 else 2 if hl <= 40 else 0
    score += {"strong": 10, "neutral": 5, "weak": 0}[market]
    score += max(0, min(5, int(entry_points)))
    return score


def size_factor(score: int, tq) -> float:
    """1.0 for a normal signal, reduced for 70-79, 0 when the trade is not allowed."""
    if score >= tq.min_score:
        return 1.0
    return tq.reduced_size_factor if score >= tq.reduced_min_score else 0.0
