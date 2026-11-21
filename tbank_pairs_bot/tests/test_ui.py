import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from pairsbot import config_edit
from pairsbot.config import ROOT, load_config, load_secret
from pairsbot.ui_server import App, Handler


def test_config_edit_keeps_comments_and_validates(tmp_path):
    path = tmp_path / "config.toml"
    text = (ROOT / "config.toml").read_text(encoding="utf-8")
    cfg = load_config(ROOT / "config.toml")
    values = config_edit.coerce(cfg, {"risk": {"risk_per_trade": "0.01", "max_consecutive_losses": 4.0},
                                      "stats": {"allow_extreme_z": True}, "live": {"news_blackout": ["GAZP"]},
                                      "walkforward": {"entry_z": [-2.0, -2.5]}})
    new = config_edit.set_pairs(config_edit.set_values(text, values),
                                [{"a": "SBER", "b": "VTBR", "sector": "banks"}])
    path.write_text(new, encoding="utf-8")
    out = load_config(path)
    assert out.risk.risk_per_trade == 0.01 and out.risk.max_consecutive_losses == 4
    assert out.stats.allow_extreme_z is True and out.live.news_blackout == ["GAZP"]
    assert out.walkforward.entry_z == [-2.0, -2.5]
    assert [(p.a, p.b) for p in out.pairs] == [("SBER", "VTBR")]
    assert "# 0.5% beginner, 1% experienced, never above 1.5%" in new  # comment kept
    assert out.stats.entry_z == cfg.stats.entry_z and "[stats]" in new
    with pytest.raises(ValueError):
        config_edit.coerce(cfg, {"risk": {"stop_pct": "abc"}})
    hints = {f["key"]: f for s in config_edit.describe(text, cfg) for f in s["fields"] if s["name"] == "risk"}
    assert hints["stop_pct"]["type"] == "float" and "stop" in hints["stop_pct"]["hint"]


@pytest.fixture
def server(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    app = App(str(tmp_path / "config.toml"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    Handler.app, Handler.hosts = app, {f"127.0.0.1:{port}"}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(path, body=None, token=app.token, host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     json.dumps(body).encode() if body is not None else None,
                                     {"X-Token": token, "Content-Type": "application/json"})
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read().decode()
                return r.status, json.loads(raw) if path.startswith("/api") else raw
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    yield call, app, tmp_path
    httpd.shutdown()


def test_api_guards_and_settings(server):
    call, app, root = server
    assert call("/api/overview", token="wrong")[0] == 403
    assert call("/api/overview", host="evil.example")[0] == 403
    status, html = call("/")
    assert status == 200 and app.token in html and "__API_TOKEN__" not in html

    status, ov = call("/api/overview")
    assert status == 200 and ov["token_set"] is False and ov["modes"]["paper"]["running"] is False

    assert call("/api/config", {"values": {"risk": {"risk_per_trade": 0.5}}})[0] == 400  # above the 2% ceiling
    assert load_config(root / "config.toml").risk.risk_per_trade == 0.005
    assert call("/api/config", {"values": {"risk": {"risk_per_trade": 0.01}}})[0] == 200
    assert load_config(root / "config.toml").risk.risk_per_trade == 0.01
    assert call("/api/config/raw", {"text": "[risk]\nbogus = 1\n"})[0] == 400

    assert call("/api/token", {"token": "t.abc", "account_id": "123"})[0] == 200
    assert load_secret("TBANK_TOKEN", root) == "t.abc" and load_secret("TBANK_ACCOUNT_ID", root) == "123"
    assert "t.abc" not in json.dumps(call("/api/overview")[1])
    assert call("/api/trade/start", {"mode": "live"})[0] == 400  # no typed confirmation


def test_backtest_job(server):
    call, app, root = server
    job = call("/api/jobs", {"kind": "backtest", "source": "synthetic"})[1]
    for _ in range(200):
        state = call("/api/job?id=" + job["id"])[1]
        if state["status"] != "running":
            break
        time.sleep(0.3)
    assert state["status"] == "done", state
    result = state["result"]
    assert result["metrics"]["trades"] > 0 and len(result["equity"]) > 100 and result["trades"][0]["bought"]
    assert call("/api/backtest/last")[1]["metrics"] == result["metrics"]
