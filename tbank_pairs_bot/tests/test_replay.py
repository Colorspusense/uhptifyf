"""Replay several hundred days through the paper engine and check the books add up."""
import copy
import csv
from datetime import datetime

import pandas as pd
import pytest

import pairsbot.live as live
from pairsbot.backtest import load_history, precompute, simulate
from pairsbot.config import ROOT, load_config
from pairsbot.data import SyntheticData
from pairsbot.live import Engine, PaperExecutor


class Replay(SyntheticData):
    """Serves history up to yesterday and today's close as the live price."""

    def __init__(self, cfg):
        super().__init__(cfg.pairs, cfg.general.index_ticker)
        self.full, self.day = self._data, None

    def set_day(self, day) -> None:
        self.day = day
        self._data = {t: df[df.index < day] for t, df in self.full.items()}

    def last_prices(self, tickers):
        return {t: float(self.full[t]["close"].loc[self.day]) for t in tickers}


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    cfg = copy.deepcopy(load_config(ROOT / "config.default.toml"))
    cfg.root = tmp_path_factory.mktemp("replay")
    cfg.entry.enabled = False  # daily replay has no 15-minute bars
    cfg.pairs = cfg.pairs[:3]
    feed = Replay(cfg)
    days = feed.full[cfg.general.index_ticker].index[-520:]
    mp = pytest.MonkeyPatch()
    engine, equity, cash_low, max_open = None, [], float("inf"), 0
    try:
        for i, day in enumerate(days):
            feed.set_day(day)
            mp.setattr(live, "now_msk", lambda day=day: datetime(day.year, day.month, day.day, 12, 0))
            if engine is None or i % 50 == 0:  # restart now and then: everything must come back from disk
                engine = Engine(cfg, feed, "paper", lambda state: PaperExecutor(cfg, state))
            engine.cycle(ignore_session=True)
            equity.append(engine.exec.account(feed.last_prices(cfg.tickers))[0])
            cash_low = min(cash_low, engine.state["cash"])
            max_open = max(max_open, len(engine.state["positions"]))
            held = [p["ticker"] for p in engine.state["positions"]]
            assert len(held) == len(set(held))
    finally:
        mp.undo()
    path = cfg.root / "journal" / "trades.csv"
    trades = list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []
    return cfg, feed, days, engine, trades, pd.Series(equity, index=days), cash_low, max_open


def test_replay_trades_and_books_balance(replay):
    cfg, feed, days, engine, trades, equity, cash_low, max_open = replay
    assert len(trades) >= 5, "the replay should produce trades"
    assert cash_low >= 0, "no leverage: cash never goes negative"
    last = feed.last_prices(cfg.tickers)
    open_pnl = sum(p["qty"] * (last[p["ticker"]] - p["entry_price"]) - p["entry_fee"] for p in engine.state["positions"])
    realised = sum(float(t["pnl"]) for t in trades)
    assert equity.iloc[-1] == pytest.approx(cfg.general.capital + realised + open_pnl, abs=1.0)


def test_replay_respects_the_rules(replay):
    cfg, feed, days, engine, trades, equity, cash_low, max_open = replay
    rk = cfg.risk
    for t in trades:
        qty, entry, exit_, stop = float(t["qty"]), float(t["entry_price"]), float(t["exit_price"]), float(t["stop"])
        assert float(t["z_entry"]) <= cfg.stats.entry_z and float(t["tqs"]) >= cfg.tqs.reduced_min_score
        assert t["reason"] in {"stop", "mean_reversion", "pair_breakdown", "target", "time_stop"}
        assert stop == pytest.approx(entry * (1 - rk.stop_pct), rel=2e-3) and t["entry_date"] < t["exit_date"]
        # Position value follows risk / stop distance (halved for TQS 70-79), never above the cap.
        assert qty * entry <= cfg.general.capital * 1.5 * rk.risk_per_trade / rk.stop_pct * 1.01
        if t["reason"] == "stop":
            assert exit_ <= stop and float(t["pnl"]) < 0
        if t["reason"] == "mean_reversion":
            assert float(t["z_exit"]) >= cfg.stats.exit_z
    # Daily loss stays bounded: positions are small and stopped, so no day loses more than a few percent.
    assert equity.pct_change().min() > -0.05


def test_replay_agrees_with_the_backtest(replay, monkeypatch):
    """Same rules, same data: the live engine and the backtest must find the same entries."""
    import pairsbot.backtest as backtest

    cfg, feed, days, engine, trades, equity, cash_low, max_open = replay
    cfg = copy.deepcopy(cfg)
    cfg.stats.coint_recalc_days = 1                                 # the live engine re-tests every day
    monkeypatch.setattr(backtest, "daily_entry_points", lambda *a: 0)  # and had no entry-screen points here
    hist = load_history(cfg, SyntheticData(cfg.pairs, cfg.general.index_ticker), days[0].date().replace(year=2000))
    res = simulate(cfg, hist, precompute(cfg, hist), days[0], days[-1])
    live_entries = {(t["bought"], t["entry_date"]) for t in trades}
    # The backtest buys at the next open, the replay at the signal day's close: shift by one session.
    index = list(feed.full[cfg.general.index_ticker].index)
    prev = {d.date().isoformat(): index[i - 1].date().isoformat() for i, d in enumerate(index) if i}
    bt_entries = {(r.bought, prev[r.entry_date.isoformat()]) for r in res.trades.itertuples()}
    assert bt_entries, "the backtest should trade on this data"
    # Fill prices differ (close against next open), so stops and pauses can diverge a little afterwards.
    assert len(live_entries & bt_entries) >= 0.8 * max(len(live_entries), len(bt_entries)), \
        (sorted(live_entries - bt_entries), sorted(bt_entries - live_entries))
