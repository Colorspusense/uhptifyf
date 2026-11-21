"""The T-Bank client and the real-order executor, run against a local imitation of the API."""
import copy
import csv
import time

import pytest

import pairsbot.live as live
from fake_tbank import FakeTBank
from pairsbot.config import load_config
from pairsbot.data import TBankData
from pairsbot.live import Engine, TBankExecutor
from pairsbot.tbank import TBankClient, TBankError


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = copy.deepcopy(load_config())
    cfg.root = tmp_path
    cfg.entry.enabled = False
    cfg.pairs = cfg.pairs[:1]
    cfg.live.order_timeout_seconds = 0
    # Force exactly one orientation of the pair through the screens so the order path is exercised.
    monkeypatch.setattr(live, "statistical_screen", lambda s, st: [] if s.beta > 0 and s.z < 0 else ["no"])
    monkeypatch.setattr(live, "market_state", lambda c: c.map(lambda _: "strong"))
    monkeypatch.setattr(live, "tqs_score", lambda *a: 90)
    monkeypatch.setattr(live, "pair_broken", lambda s, st: False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    cfg.stats.exit_z = 99.0
    fake = FakeTBank(cfg, lots={t: 10 for t in cfg.tickers})

    def engine():
        client = TBankClient("good-token", base_url=fake.url)
        return Engine(cfg, TBankData(client, cfg.general.index_ticker), "sandbox",
                      lambda state: TBankExecutor(cfg, client, "acc-1"))

    yield cfg, fake, engine
    fake.close()
    assert fake.spec.errors == [], "\n".join(sorted(set(fake.spec.errors)))


def trades(cfg):
    path = cfg.root / "journal" / "trades.csv"
    return list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []


def test_client_market_data_and_errors(env):
    cfg, fake, _ = env
    client = TBankClient("good-token", base_url=fake.url)
    data = TBankData(client, cfg.general.index_ticker)
    share = client.share("SBER")
    assert share["uid"] == "uid-SBER" and share["lot"] == 10 and share["step"] == pytest.approx(0.01)
    assert client.find_index("IMOEX")["uid"] == "uid-IMOEX"
    from datetime import timedelta

    from pairsbot.data import now_msk

    daily = data.candles("SBER", "1d", now_msk() - timedelta(days=3000), now_msk())
    assert len(daily) == 1500 and daily.index.is_monotonic_increasing and daily.index[0].hour == 0
    assert daily["close"].iloc[-1] == pytest.approx(fake.synth._data["SBER"]["close"].iloc[-1], abs=1e-6)
    intraday = data.candles("SBER", "15m", now_msk() - timedelta(days=14), now_msk())
    assert len(intraday) > 250
    assert data.last_prices(["SBER", "VTBR"])["SBER"] == pytest.approx(fake.last("SBER"))
    bid, ask = data.order_book("SBER")
    assert bid < ask == pytest.approx(fake.last("SBER"))
    assert data.tradable("SBER") is True
    assert client.portfolio("acc-1") == {"equity": 1_000_000.0, "cash": 1_000_000.0, "positions": {}}
    with pytest.raises(TBankError) as e:
        TBankClient("bad", base_url=fake.url).accounts()
    assert e.value.status == 401 and not e.value.not_found
    with pytest.raises(TBankError) as e:
        client.order_state("acc-1", "nope", by_request=True)
    assert e.value.not_found


def test_buy_places_broker_stop_and_books_its_execution(env):
    cfg, fake, engine = env
    eng = engine()
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    uid = "uid-" + pos["ticker"]
    assert pos["qty"] % 10 == 0 and fake.positions[uid] == pos["qty"]
    assert 0.24e6 < pos["qty"] * pos["entry_price"] <= 0.25e6  # 0.5% risk / 2% stop
    (stop,) = fake.stops.values()
    assert stop["lots"] == pos["qty"] // 10 and stop["stop"] == pytest.approx(pos["stop"])
    assert 0.9795 < pos["stop"] / pos["entry_price"] <= 0.98
    assert eng.state["pending"] is None and pos["entry_fee"] > 0

    eng.cycle(ignore_session=True)  # nothing to do: no second buy, no averaging
    assert len(eng.state["positions"]) == 1 and fake.count("PostOrder") == 1

    fake.price[pos["ticker"]] = pos["stop"] - 0.5
    fake.trigger_stops()  # the broker executes the stop while the bot is not looking
    eng = engine()        # and the bot is restarted
    eng.cycle(ignore_session=True)
    assert len(eng.state["positions"]) == 1  # one odd portfolio answer is not enough to drop a position
    eng.cycle(ignore_session=True)
    assert eng.state["positions"] == [] and fake.count("PostOrder") == 1  # the bot did not sell again
    (t,) = trades(cfg)
    assert t["reason"] == "stop" and float(t["pnl"]) < 0 and eng.risk.consecutive_losses == 1


def test_exit_by_the_bot_cancels_the_stop_and_sells_everything(env):
    cfg, fake, engine = env
    eng = engine()
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    cfg.stats.exit_z = -99.0  # the spread is "back at the mean"
    fake.price[pos["ticker"]] = round(pos["entry_price"] * 1.03, 2)
    eng.cycle(ignore_session=True)
    assert eng.state["positions"] == [] and fake.positions["uid-" + pos["ticker"]] == 0
    assert not any(s["active"] for s in fake.stops.values())
    (t,) = trades(cfg)
    assert t["reason"] == "mean_reversion" and float(t["exit_price"]) == pytest.approx(fake.price[pos["ticker"]])
    expected = pos["qty"] * (float(t["exit_price"]) - pos["entry_price"]) * 1 - pos["entry_fee"] \
        - pos["qty"] * float(t["exit_price"]) * 0.0005
    assert float(t["pnl"]) == pytest.approx(expected, abs=0.02)
    assert "closed today" in next(r["verdict"] for r in eng.cycle(ignore_session=True) if "closed" in r["verdict"])
    assert eng.state["positions"] == []  # no re-entry on the day of the exit


def test_lost_buy_answer_is_recovered_without_a_second_order(env):
    cfg, fake, engine = env
    eng = engine()
    fake.drop["PostOrder"] = 5  # the order reaches the broker and fills, but every answer is lost
    with pytest.raises(TBankError):
        eng.cycle(ignore_session=True)
    assert eng.state["positions"] == [] and eng.state["pending"] and len(fake.orders) == 1
    eng = engine()  # restart
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    assert pos["qty"] == fake.positions["uid-" + pos["ticker"]] and eng.state["pending"] is None
    assert len(fake.orders) == 1 and len(fake.stops) == 1


def test_lost_sell_answer_does_not_sell_twice(env):
    cfg, fake, engine = env
    eng = engine()
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    cfg.stats.exit_z = -99.0
    fake.drop["PostOrder"] = 5
    with pytest.raises(TBankError):
        eng.cycle(ignore_session=True)
    assert eng.state["positions"][0]["closing"]["reason"] == "mean_reversion"
    eng = engine()
    eng.cycle(ignore_session=True)
    assert eng.state["positions"] == [] and len(fake.orders) == 2  # one buy, one sell
    assert trades(cfg)[0]["reason"] == "mean_reversion"


def test_unfilled_limit_order_is_cancelled(env):
    cfg, fake, engine = env
    fake.fill = "none"
    eng = engine()
    rows = eng.cycle(ignore_session=True)
    assert eng.state["positions"] == [] and eng.state["pending"] is None
    assert [o["status"] for o in fake.orders.values()] == ["EXECUTION_REPORT_STATUS_CANCELLED"]
    assert any(r["verdict"] == "limit order not filled" for r in rows) and fake.cash == 1_000_000.0


def test_partial_fill_and_rejection(env):
    cfg, fake, engine = env
    fake.fill = "partial"
    eng = engine()
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    (order,) = fake.orders.values()
    assert pos["qty"] == order["done"] * 10 < order["lots"] * 10
    assert order["status"].endswith("_CANCELLED") and next(iter(fake.stops.values()))["lots"] == order["done"]

    fake2_cfg = cfg
    fake.fill = "reject"
    fake.positions.clear()
    eng.state["positions"].clear()
    eng.state["last_exit"].clear()
    eng.cycle(ignore_session=True)
    assert eng.state["positions"] == [] and fake2_cfg is cfg


def test_no_orders_while_the_stock_is_not_trading(env):
    cfg, fake, engine = env
    fake.trading = False
    eng = engine()
    rows = eng.cycle(ignore_session=True)
    assert fake.count("PostOrder") == 0 and any("not trading" in r["verdict"] for r in rows)
    fake.trading = True
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    cfg.stats.exit_z = -99.0
    fake.trading = False
    eng.cycle(ignore_session=True)  # the exit waits instead of sending an order that would be rejected
    assert len(eng.state["positions"]) == 1 and fake.count("PostOrder") == 1 and "closing" not in pos
    fake.trading = True
    eng.cycle(ignore_session=True)
    assert eng.state["positions"] == []


def test_manual_close_request(env):
    cfg, fake, engine = env
    eng = engine()
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    request = cfg.root / "state" / "sandbox.close"
    request.write_text(f'["{pos["ticker"]}"]', encoding="utf-8")
    eng.cycle(ignore_session=True)
    assert eng.state["positions"] == [] and trades(cfg)[0]["reason"] == "manual" and not request.exists()


def test_session_closed_means_no_orders(env, monkeypatch):
    cfg, fake, engine = env
    monkeypatch.setattr(live, "in_session", lambda c, now: False)
    eng = engine()
    rows = eng.cycle()
    assert fake.count("PostOrder") == 0 and eng.note == "session closed, no orders" and rows


def test_entry_screen_on_intraday_bars_from_the_api(env):
    cfg, fake, engine = env
    cfg.entry.enabled = True
    eng = engine()
    rows = eng.cycle(ignore_session=True)
    assert len(eng.state["positions"]) == 1, [r["verdict"] for r in rows]
    assert fake.count("GetCandles") > 3 and fake.count("GetOrderBook") >= 1


def test_sandbox_account_is_opened_and_funded(env, monkeypatch):
    import bot

    cfg, fake, _ = env
    fake.accounts.clear()
    monkeypatch.setenv("TBANK_TOKEN", "good-token")
    monkeypatch.setenv("TBANK_API_URL", fake.url)
    eng = bot._engine(cfg, "sandbox")
    assert eng.exec.account_id == "sandbox-1" and fake.cash == cfg.general.capital
    monkeypatch.setenv("TBANK_ACCOUNT_ID", "someone-else")
    with pytest.raises(SystemExit):
        bot._engine(cfg, "live")


def test_empty_portfolio_answer_while_selling_does_not_erase_the_position(env):
    cfg, fake, engine = env
    eng = engine()
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    uid, shares = "uid-" + pos["ticker"], pos["qty"]
    cfg.stats.exit_z = -99.0
    real = fake.m_GetPortfolio
    answers = iter([real, lambda b: {**real(b), "positions": []}])  # cycle start: fine; inside sell: empty
    fake.m_GetPortfolio = lambda b: next(answers, real)(b)
    eng.cycle(ignore_session=True)
    assert len(eng.state["positions"]) == 1 and fake.positions[uid] == shares and not trades(cfg)
    cfg.stats.exit_z = 99.0
    eng = engine()
    eng.cycle(ignore_session=True)  # the shares are still there: the stop is put back
    assert sum(s["active"] for s in fake.stops.values()) == 1 and eng.state["positions"][0]["stop_order_id"]


def test_stop_that_cannot_be_cancelled_blocks_the_sell(env):
    from fake_tbank import ApiFault

    cfg, fake, engine = env
    eng = engine()
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    cfg.stats.exit_z = -99.0
    fake.fault["CancelStopOrder"] = [ApiFault(400, 3, 30099, "temporary refusal")]
    with pytest.raises(TBankError):
        eng.cycle(ignore_session=True)
    # Still protected and still recorded: the stop was not forgotten, nothing was sold.
    assert eng.state["positions"][0]["stop_order_id"] and sum(s["active"] for s in fake.stops.values()) == 1
    assert fake.positions["uid-" + pos["ticker"]] == pos["qty"]
    engine().cycle(ignore_session=True)
    assert fake.positions["uid-" + pos["ticker"]] == 0 and not any(s["active"] for s in fake.stops.values())
    assert len(trades(cfg)) == 1


def test_partial_sell_keeps_the_rest_tracked_and_protected(env):
    cfg, fake, engine = env
    eng = engine()
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    uid, qty = "uid-" + pos["ticker"], pos["qty"]
    cfg.stats.exit_z = -99.0
    fake.sell_fill = 3  # only 3 lots of the market order are executed
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    assert pos["qty"] == qty - 30 == fake.positions[uid] and "closing" not in pos
    active = [s for s in fake.stops.values() if s["active"]]
    assert len(active) == 1 and active[0]["lots"] == pos["qty"] // 10  # a stop for exactly what is left
    fake.sell_fill = None
    engine().cycle(ignore_session=True)
    rows = trades(cfg)
    assert fake.positions[uid] == 0 and [float(r["qty"]) for r in rows] == [30, qty - 30]
    assert not any(s["active"] for s in fake.stops.values())
    assert fake.cash == pytest.approx(1_000_000 + sum(float(r["pnl"]) for r in rows), abs=1.0)


def test_lost_answer_to_a_stop_order_never_leaves_two_stops(env):
    cfg, fake, engine = env
    fake.drop["PostStopOrder"] = 5  # the broker registers the stop but the bot never hears back
    eng = engine()
    eng.cycle(ignore_session=True)
    assert eng.state["positions"][0]["stop_order_id"] == "" and len(fake.stops) == 1
    engine().cycle(ignore_session=True)  # retry with the same key
    assert len(fake.stops) == 1 and eng.state["positions"] and engine().state["positions"][0]["stop_order_id"]


def test_shares_missing_while_the_stop_still_waits_are_not_booked_quickly(env):
    cfg, fake, engine = env
    eng = engine()
    eng.cycle(ignore_session=True)
    real = fake.m_GetPortfolio
    fake.m_GetPortfolio = lambda b: {**real(b), "positions": []}  # the broker keeps answering "no shares"
    for _ in range(4):
        eng.cycle(ignore_session=True)
    assert len(eng.state["positions"]) == 1 and not trades(cfg)  # its stop order is still active: wait
    fake.m_GetPortfolio = real
    eng.cycle(ignore_session=True)
    assert "missing" not in eng.state["positions"][0]


def test_unsettled_buy_does_not_block_exits(env):
    from fake_tbank import ApiFault

    cfg, fake, engine = env
    eng = engine()
    eng.cycle(ignore_session=True)
    (pos,) = eng.state["positions"]
    eng.state["pending"] = {"key": "k-unknown", "ticker": pos["other"], "other": pos["ticker"], "sector": "banks",
                            "limit": 100.0, "z_entry": -2.1, "tqs": 85, "date": live.now_msk().date().isoformat()}
    eng.save()
    fake.fault["GetOrderState"] = [ApiFault(500, 13, 70001, "internal error")] * 5
    cfg.stats.exit_z = -99.0
    eng = engine()
    eng.cycle(ignore_session=True)
    assert eng.state["positions"] == [] and eng.state["pending"]  # the exit happened, the buy is still open
    assert len(fake.orders) == 2


def test_stale_close_request_is_dropped(env):
    cfg, fake, engine = env
    request = cfg.root / "state" / "sandbox.close"
    request.parent.mkdir(exist_ok=True)
    request.write_text('["SBER", "VTBR"]', encoding="utf-8")
    eng = engine()
    eng.cycle(ignore_session=True)  # opens a position; the old request must not close it
    eng.cycle(ignore_session=True)
    assert len(eng.state["positions"]) == 1 and not request.exists() and not trades(cfg)
