"""Checks added after the code reviews: validation, config editing, process control, manual close."""
import copy
import json
import threading
import time

import pytest

from pairsbot import config_edit
from pairsbot.config import ROOT, load_config
from pairsbot.live import TBankExecutor, _Lock, lock_is_fresh, write_atomic
from test_ui import server  # noqa: F401  (fixture)

BASE = (ROOT / "config.toml").read_text(encoding="utf-8")


def load_with(tmp_path, old: str, new: str):
    assert old in BASE
    path = tmp_path / "config.toml"
    path.write_text(BASE.replace(old, new), encoding="utf-8")
    return load_config(path)


@pytest.mark.parametrize("old,new", [
    ("capital = 1000000.0", "capital = inf"),
    ("capital = 1000000.0", "capital = -5.0"),
    ("capital = 1000000.0", 'capital = "<img src=x onerror=alert(1)>"'),
    ("poll_seconds = 300", "poll_seconds = 0"),
    ("poll_seconds = 300", "poll_seconds = 2.5"),
    ('session_start = "10:05"', 'session_start = "99:99"'),
    ('session_end = "18:35"', 'session_end = "09:00"'),
    ("risk_per_trade = 0.005", "risk_per_trade = 0.021"),
    ("stop_pct = 0.02 ", "stop_pct = 0.0 "),
    ("daily_loss_limit = 0.02", "daily_loss_limit = 3.0"),
    ("entry_z = -2.0\n", "entry_z = 2.0\n"),
    ("exit_z = -0.25", "exit_z = -2.5"),
    ("min_score = 80", "min_score = 60"),
    ("news_blackout = []", "news_blackout = [1, 2]"),
    ("news_blackout = []", 'news_blackout = ["<b>"]'),
    ("entry_z = [-2.0, -2.25, -2.5]", 'entry_z = ["a"]'),
    ("entry_z = [-2.0, -2.25, -2.5]", "entry_z = []"),
    ("require_z_turn = true", "require_z_turn = 1"),
    ('a = "SBER"', 'a = "VTBR"'),
    ("[costs]", "[bogus]\nx = 1\n[costs]"),
])
def test_invalid_config_is_rejected(tmp_path, old, new):
    with pytest.raises(ValueError):
        load_with(tmp_path, old, new)


def test_whole_numbers_are_accepted_for_fractions(tmp_path):
    assert load_with(tmp_path, "capital = 1000000.0", "capital = 500000").general.capital == 500000.0
    assert load_config().risk.max_position_pct == 0.5 and load_config().stats.max_rolling_corr_drop == 0.05


def test_config_edit_handles_awkward_but_valid_toml(tmp_path):
    cfg = load_config()
    text = BASE.replace("entry_z = [-2.0, -2.25, -2.5]", "entry_z = [\n  -2.0,   # first\n  -2.25,\n]") \
               .replace("news_blackout = []", 'news_blackout = ["A#B"]  # has "quotes" and # hash') \
               .replace("\n", "\r\n")
    new = config_edit.set_values(text, config_edit.coerce(cfg, {
        "walkforward": {"entry_z": [-2.1], "stop_pct": [0.02]}, "live": {"news_blackout": ["GAZP", "SBER"]},
        "risk": {"stop_pct": 0.03}, "stats": {"entry_z": -2.2}}))
    path = tmp_path / "config.toml"
    path.write_text(new, encoding="utf-8")
    out = load_config(path)
    assert out.walkforward.entry_z == [-2.1] and out.walkforward.stop_pct == [0.02]
    assert out.live.news_blackout == ["GAZP", "SBER"] and '# has "quotes" and # hash' in new
    # Same key name in two sections: each keeps its own value.
    assert out.risk.stop_pct == 0.03 and out.stats.entry_z == -2.2 and out.stats.exit_z == cfg.stats.exit_z
    assert "# first" not in new and "-2.25" not in new.split("[walkforward]")[1].split("exit_z")[0]


def test_fill_price_survives_unexpected_field_meanings():
    f = TBankExecutor._fill_price
    assert f({"avg_price": 101.0, "executed_total": 10100.0}, 100.0, 100) == 101.0
    assert f({"avg_price": 0.0, "executed_total": 10100.0}, 100.0, 100) == 101.0      # only the total is filled
    assert f({"avg_price": 10100.0, "executed_total": 101.0}, 100.0, 100) == 101.0    # the meanings are swapped
    assert f({"avg_price": 0.0, "executed_total": 0.0}, 100.0, 100) == 100.0          # nothing usable


def test_only_one_process_per_mode(tmp_path):
    path = tmp_path / "state" / "paper.lock"
    with _Lock(path):
        assert lock_is_fresh(path)
        with pytest.raises(SystemExit):
            _Lock(path).__enter__()
    assert not lock_is_fresh(path)  # released: a new start is allowed
    with _Lock(path):
        pass
    winners, errors = [], []

    def starter():
        try:
            lock = _Lock(path)
            lock.__enter__()
            winners.append(lock)
        except SystemExit:
            errors.append(1)

    threads = [threading.Thread(target=starter) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1 and len(errors) == 7
    winners[0].__exit__()


def test_api_rejects_malformed_requests(server):
    call, app, root = server
    assert call("/api/overview", token="t\xe9st\xff")[0] == 403
    for body in ({"values": []}, {"values": {"risk": 5}}, {"pairs": "x"}, {"pairs": [1]}, {"values": {"risk": {"stop_pct": None}}}):
        assert call("/api/config", body)[0] == 400, body
    assert call("/api/jobs", {"kind": "pair", "buy": ["x"], "other": {}})[0] in (200, 400)
    assert call("/api/position/close", {"mode": "paper", "ticker": "SBER"})[0] == 400  # no such position
    assert call("/api/trade/start", {"mode": "sandbox"})[0] == 400                      # no token
    assert load_config(root / "config.toml").risk.stop_pct == 0.02


def _wait(call, predicate, seconds=60):
    for _ in range(seconds * 4):
        ov = call("/api/overview")[1]
        if predicate(ov):
            return ov
        time.sleep(0.25)
    raise AssertionError(json.dumps(ov["modes"])[:800])


def test_manual_close_through_the_interface(server, monkeypatch):
    import pairsbot.live as live
    import pairsbot.ui_server as ui

    call, app, root = server
    pos = {"pair": "SBER/VTBR", "ticker": "SBER", "other": "VTBR", "sector": "banks", "qty": 10,
           "entry_price": 100.0, "entry_fee": 0.5, "entry_date": "2026-01-05", "stop": 98.0, "target": None,
           "z_entry": -2.3, "tqs": 85, "stop_order_id": ""}
    write_atomic(root / "state" / "paper.json", json.dumps({"positions": [pos], "risk": {}, "cash": 999_000.0}))
    assert call("/api/position/close", {"mode": "live", "ticker": "SBER"})[0] == 400
    monkeypatch.setattr(live, "in_session", lambda cfg, now: False)
    status, out = call("/api/position/close", {"mode": "paper", "ticker": "SBER"})
    assert status == 200 and out["now"] is False
    ov = call("/api/overview")[1]
    assert ov["modes"]["paper"]["closing"] == ["SBER"] and not ov["modes"]["paper"]["running"]
    assert call("/api/position/close", {"mode": "paper", "ticker": "SBER", "cancel": True})[0] == 200
    assert call("/api/overview")[1]["modes"]["paper"]["closing"] == []
    assert ui  # imported for clarity: the endpoint lives there


def test_two_simultaneous_starts_launch_one_process(server, monkeypatch):
    call, app, root = server
    started = []
    monkeypatch.setattr(app, "_spawn_locked", lambda mode, once, exits_only: (
        started.append(mode), (root / "state").mkdir(exist_ok=True), time.sleep(0.2),
        _Lock(root / "state" / f"{mode}.lock").__enter__()))
    results = []
    threads = [threading.Thread(target=lambda: results.append(call("/api/trade/start", {"mode": "paper"})[0]))
               for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert started == ["paper"] and sorted(results) == [200, 400, 400, 400]


def test_sandbox_cycle_started_from_the_interface_reaches_the_broker(server, monkeypatch):
    """The whole chain: interface -> bot process -> T-Bank client -> (imitated) API."""
    from fake_tbank import FakeTBank

    call, app, root = server
    fake = FakeTBank(load_config(root / "config.toml"))
    try:
        monkeypatch.setenv("TBANK_API_URL", fake.url)
        assert call("/api/token", {"sandbox_token": "good-token"})[0] == 200
        ov = call("/api/overview")[1]
        assert ov["sandbox_token_set"] and not ov["token_set"]
        status, out = call("/api/token/test", {"sandbox": True})
        assert status == 200 and out["accounts"][0]["id"] == "acc-1"
        assert call("/api/trade/start", {"mode": "live", "confirm": "LIVE"})[0] == 400  # no live token
        assert call("/api/trade/start", {"mode": "sandbox", "once": True})[0] == 200
        _wait(call, lambda ov: ov["modes"]["sandbox"]["running"] or fake.count("GetCandles"))
        ov = _wait(call, lambda ov: not ov["modes"]["sandbox"]["running"])
        assert ov["modes"]["sandbox"]["error"] == "", ov["modes"]["sandbox"]["error"]
        assert fake.count("GetSandboxAccounts") >= 1 and fake.count("GetCandles") >= 6
        log = (root / "logs" / "bot.log").read_text(encoding="utf-8")
        assert "cycle done" in log and "Traceback" not in log
    finally:
        fake.close()
    assert fake.spec.errors == [], "\n".join(sorted(set(fake.spec.errors)))
    assert copy  # keep the import used
