import copy
from datetime import date

import numpy as np
import pytest

from pairsbot.backtest import load_history, precompute, simulate
from pairsbot.config import load_config
from pairsbot.data import SyntheticData
from pairsbot.risk import RiskState, position_shares
from pairsbot.screens import market_state, size_factor, statistical_screen, tqs_score
from pairsbot.stats import Snapshot, half_life, pair_snapshot
from pairsbot.tbank import f2q, q2f


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def snap(**kw):
    base = dict(corr=0.96, corr_roll=0.95, corr_roll_prev=0.95, coint_p=0.008, beta=0.91,
                beta_change=0.02, z=-2.6, z_prev=-2.8, half_life=4.0)
    return Snapshot(**{**base, **kw})


def test_position_size_matches_guide_example(cfg):
    rk = copy.copy(cfg.risk)
    rk.risk_per_trade, rk.stop_pct, rk.max_position_pct = 0.01, 0.02, 1.0
    # 1 000 000 RUB, 1% risk, 2% stop -> 500 000 RUB position
    assert position_shares(1_000_000, 1_000_000, 100.0, 10, rk) == 5000
    assert position_shares(1_000_000, 200_000, 100.0, 10, rk) == 2000  # never more than the cash
    assert position_shares(1_000_000, 1_000_000, 100.0, 10, rk, factor=0.5) == 2500


def test_tqs_scale(cfg):
    st = cfg.stats
    assert tqs_score(snap(), "strong", 5, st) == 100
    assert tqs_score(snap(z=-2.2), "strong", 5, st) == 95
    assert tqs_score(snap(z=-3.4), "strong", 5, st) == 90
    assert tqs_score(snap(corr=0.91, coint_p=0.03, beta_change=0.07), "neutral", 0, st) == 15 + 10 + 12 + 7 + 20 + 10 + 5
    assert size_factor(85, cfg.tqs) == 1.0
    assert size_factor(74, cfg.tqs) == cfg.tqs.reduced_size_factor
    assert size_factor(69, cfg.tqs) == 0.0


def test_statistical_screen_filters(cfg):
    st = cfg.stats
    assert statistical_screen(snap(), st) == []
    assert statistical_screen(snap(corr=0.88), st)
    assert statistical_screen(snap(coint_p=0.07), st)
    assert statistical_screen(snap(beta_change=0.15), st)
    assert statistical_screen(snap(z=-1.1), st)
    assert statistical_screen(snap(z=-2.6, z_prev=-2.1), st)  # still falling
    assert statistical_screen(snap(z=-3.5, z_prev=-3.6), st)  # extreme: needs a news check


def test_snapshot_on_cointegrated_series(cfg):
    rng = np.random.default_rng(1)
    n = 400
    lb = np.log(100) + np.cumsum(rng.normal(0, 0.015, n))
    spread = np.zeros(n)
    for i in range(1, n):
        spread[i] = 0.9 * spread[i - 1] + rng.normal(0, 0.004)
    s = pair_snapshot(lb + spread, lb, cfg.stats)
    assert s.corr > 0.9 and s.coint_p < 0.05 and 0.8 < s.beta < 1.2
    assert 2 < half_life(spread) < 15
    assert pair_snapshot(lb[:100] + spread[:100], lb[:100], cfg.stats) is None


def test_risk_limits(cfg):
    rk = cfg.risk
    r = RiskState()
    d = date(2025, 3, 3)  # Monday
    r.new_day(d, 1_000_000)
    assert r.can_open(d, 1_000_000, rk) == ""
    assert "daily" in r.can_open(d, 979_000, rk)
    assert "paused" in r.can_open(d, 940_000, rk)  # weekly drawdown
    r = RiskState()
    r.new_day(d, 1_000_000)
    for _ in range(3):
        r.trade_closed(-100, d, rk)
    assert "losses in a row" in r.can_open(d, 999_700, rk)


def test_market_state():
    import pandas as pd

    up = pd.Series(np.linspace(100, 200, 400))
    assert market_state(up).iloc[-1] == "strong"
    assert market_state(up[::-1].reset_index(drop=True)).iloc[-1] == "weak"


def test_quotation_roundtrip():
    assert q2f({"units": "285", "nano": 530000000}) == pytest.approx(285.53)
    assert f2q(285.53) == {"units": "285", "nano": 530000000}


def test_backtest_on_synthetic_data(cfg):
    provider = SyntheticData(cfg.pairs, cfg.general.index_ticker)
    hist = load_history(cfg, provider, date(2000, 1, 1))
    res = simulate(cfg, hist, precompute(cfg, hist))
    t = res.trades
    assert res.metrics["trades"] > 0
    assert set(t.reason) <= {"stop", "target", "mean_reversion", "pair_breakdown", "time_stop"}
    assert (t.z_entry <= cfg.stats.entry_z).all() and (t.tqs >= cfg.tqs.reduced_min_score).all()
    # No leverage: a position never exceeds the configured share of the starting equity by much.
    assert (t.qty * t.entry_price).max() <= res.equity.max() * cfg.risk.max_position_pct * 1.01
