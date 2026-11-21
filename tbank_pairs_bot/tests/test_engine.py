import copy
import csv

import pairsbot.live as live
from pairsbot.config import load_config
from pairsbot.data import SyntheticData
from pairsbot.live import Engine, PaperExecutor


class FakeFeed(SyntheticData):
    """Synthetic history plus a controllable last price."""

    def __init__(self, cfg):
        super().__init__(cfg.pairs, cfg.general.index_ticker)
        self.override = {}

    def last_prices(self, tickers):
        return {**super().last_prices(tickers), **self.override}


def test_paper_engine_opens_and_stops_out(tmp_path, monkeypatch):
    cfg = copy.deepcopy(load_config())
    cfg.root = tmp_path
    cfg.entry.enabled = False
    cfg.pairs = cfg.pairs[:1]
    # Force exactly one orientation through screen 1 so the order path is exercised.
    monkeypatch.setattr(live, "statistical_screen", lambda s, st: [] if s.beta > 0 and s.z < 0 else ["no"])
    monkeypatch.setattr(live, "market_state", lambda c: c.map(lambda _: "strong"))
    monkeypatch.setattr(live, "tqs_score", lambda *a: 90)
    monkeypatch.setattr(live, "pair_broken", lambda s, st: False)
    cfg.stats.exit_z = 99.0

    feed = FakeFeed(cfg)
    engine = Engine(cfg, feed, "paper", lambda state: PaperExecutor(cfg, state))
    rows = engine.cycle()
    assert len(engine.state["positions"]) == 1, rows
    pos = engine.state["positions"][0]
    value = pos["qty"] * pos["entry_price"]
    # 0.5% risk / 2% stop = 25% of capital, and cash went down by the cost plus commission.
    assert 0.24 * cfg.general.capital < value <= 0.25 * cfg.general.capital
    assert engine.state["cash"] < cfg.general.capital - value
    assert abs(pos["stop"] / pos["entry_price"] - 0.98) < 0.001

    # State survives a restart, and a second cycle does not average into the position.
    engine = Engine(cfg, feed, "paper", lambda state: PaperExecutor(cfg, state))
    engine.cycle()
    assert len(engine.state["positions"]) == 1

    feed.override = {pos["ticker"]: pos["stop"] * 0.999}
    engine.cycle()
    assert engine.state["positions"] == []
    trades = list(csv.DictReader((tmp_path / "journal" / "trades.csv").open(encoding="utf-8")))
    assert len(trades) == 1 and trades[0]["reason"] == "stop" and float(trades[0]["pnl"]) < 0
    assert engine.risk.consecutive_losses == 1
