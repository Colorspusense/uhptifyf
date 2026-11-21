"""Local web interface: serves ui.html and a small JSON API, on 127.0.0.1 only."""
from __future__ import annotations

import csv
import json
import math
import secrets
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import asdict
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from . import config_edit
from .config import ROOT, load_config, load_secret
from .data import DailyCache, make_provider, now_msk

MODES = ("paper", "sandbox", "live")


class ApiError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def clean(o):
    """Make a result JSON-safe: NaN / inf become null, numpy and date types become plain ones."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return float(o) if math.isfinite(o) else None
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return o


def _date(value) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        raise ApiError(f"bad date: {value}") from None


def _read_csv(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class App:
    def __init__(self, config_path: str | None):
        self.cfg_file = Path(config_path).resolve() if config_path else ROOT / "config.toml"
        self.root = self.cfg_file.parent
        self.token = secrets.token_urlsafe(24)
        self.jobs, self.procs, self.lock = {}, {}, threading.Lock()
        self._quotes = (0.0, (), {})

    def cfg(self):
        try:
            return load_config(self.cfg_file)
        except Exception as e:
            raise ApiError(f"config.toml is invalid: {e}") from None

    def _provider(self, cfg, source: str | None, sandbox: bool = False):
        try:
            return make_provider(cfg, load_secret("TBANK_TOKEN", self.root), source or None, sandbox)
        except SystemExit as e:
            raise ApiError(str(e)) from None

    # ---- overview --------------------------------------------------------------------
    def _state(self, mode: str) -> dict:
        path = self.root / "state" / f"{mode}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            return {}

    def _running(self, mode: str) -> bool:
        p = self.procs.get(mode)
        return bool(p) and p.poll() is None

    def _proc_error(self, mode: str) -> str:
        p = self.procs.get(mode)
        if not p or p.poll() in (None, 0):
            return ""
        err = self.root / "logs" / f"ui_{mode}.err"
        text = err.read_text(encoding="utf-8", errors="replace").strip() if err.exists() else ""
        return text[-600:] or f"process exited with code {p.returncode}"

    def _last_prices(self, cfg, tickers: set) -> dict:
        key = tuple(sorted(tickers))
        if not key:
            return {}
        at, cached_key, cached = self._quotes
        if cached_key == key and time.time() - at < 30:
            return cached
        try:
            prices = self._provider(cfg, None).last_prices(list(key))
        except Exception:
            prices = {}
        self._quotes = (time.time(), key, prices)
        return prices

    def get_overview(self, q: dict) -> dict:
        from .live import in_session

        cfg, now = self.cfg(), now_msk()
        token = load_secret("TBANK_TOKEN", self.root)
        modes, held = {}, set()
        for m in MODES:
            st = self._state(m)
            running = self._running(m)
            modes[m] = {"running": running, "error": self._proc_error(m),
                        "stopping": running and (self.root / "state" / f"{m}.stop").exists(),
                        "positions": st.get("positions", []), "risk": st.get("risk", {}),
                        "cash": st.get("cash"), "last_cycle": st.get("last_cycle")}
            held |= {p["ticker"] for p in modes[m]["positions"]}
        source = cfg.general.data_source
        return {"time": now.isoformat(timespec="seconds"), "in_session": in_session(cfg, now),
                "token_set": bool(token), "account_id_set": bool(load_secret("TBANK_ACCOUNT_ID", self.root)),
                "source": ("tbank" if token else "moex") if source == "auto" else source,
                "modes": modes, "quotes": self._last_prices(cfg, held), "folder": str(self.root),
                "config": {k: asdict(getattr(cfg, k)) for k in ("general", "stats", "tqs", "risk", "live", "costs")}}

    # ---- background jobs: scan, backtest, walk-forward, pair history -------------------
    def post_jobs(self, body: dict) -> dict:
        kind = body.get("kind")
        fn = {"scan": self._job_scan, "backtest": self._job_backtest, "walkforward": self._job_walkforward,
              "pair": self._job_pair}.get(kind)
        if not fn:
            raise ApiError("unknown job")
        with self.lock:
            if any(j["kind"] == kind and j["status"] == "running" for j in self.jobs.values()):
                raise ApiError(f"a {kind} job is already running")
            for old in list(self.jobs)[:-30]:
                self.jobs.pop(old)
            job = {"id": secrets.token_hex(6), "kind": kind, "status": "running", "started": time.time()}
            self.jobs[job["id"]] = job

        def run():
            try:
                job["result"] = clean(fn(body))
                job["status"] = "done"
            except (Exception, SystemExit) as e:
                job["error"] = str(e) or type(e).__name__
                job["status"] = "error"
                if not isinstance(e, (ApiError, SystemExit)):
                    self._log_error(traceback.format_exc())
            job["seconds"] = round(time.time() - job["started"], 1)

        threading.Thread(target=run, daemon=True).start()
        return {"id": job["id"]}

    def get_job(self, q: dict) -> dict:
        job = self.jobs.get(q.get("id", ""))
        if not job:
            raise ApiError("job not found", 404)
        return job

    def _log_error(self, text: str) -> None:
        (self.root / "logs").mkdir(exist_ok=True)
        with (self.root / "logs" / "ui.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\n{text}\n")

    def _job_scan(self, body: dict) -> dict:
        from .live import Engine

        cfg = self.cfg()
        if body.get("source") == "synthetic":
            raise ApiError("synthetic data is only for backtests")
        engine = Engine(cfg, self._provider(cfg, body.get("source")), "scan")
        rows = engine.cycle(trade=False)
        return {"rows": rows, "market": engine.last_market, "source": engine.p.name,
                "time": now_msk().isoformat(timespec="seconds")}

    def _job_backtest(self, body: dict) -> dict:
        from .backtest import run_backtest

        cfg = self.cfg()
        provider = self._provider(cfg, body.get("source"))
        res = run_backtest(cfg, provider, _date(body.get("start")), _date(body.get("end")))
        out = self.root / "reports"
        out.mkdir(exist_ok=True)
        res.trades.to_csv(out / "backtest_trades.csv", index=False)
        res.equity.to_csv(out / "backtest_equity.csv", header=["equity"])
        result = clean({"metrics": res.metrics, "source": provider.name, "capital": cfg.general.capital,
                        "time": now_msk().isoformat(timespec="seconds"),
                        "equity": [[d.date().isoformat(), round(v, 2)] for d, v in res.equity.items()],
                        "trades": res.trades.to_dict("records")})
        (out / "backtest_last.json").write_text(json.dumps(result), encoding="utf-8")
        return result

    def get_backtest_last(self, q: dict) -> dict:
        path = self.root / "reports" / "backtest_last.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except ValueError:
            return {}

    def _job_walkforward(self, body: dict) -> dict:
        from .backtest import walk_forward

        cfg = self.cfg()
        provider = self._provider(cfg, body.get("source"))
        folds = walk_forward(cfg, provider, _date(body.get("start")), _date(body.get("end")))
        return {"folds": folds, "source": provider.name, "total": sum(f["net_profit"] for f in folds)}

    def _job_pair(self, body: dict) -> dict:
        from .stats import z_history

        cfg = self.cfg()
        buy, other = body.get("buy"), body.get("other")
        if buy not in cfg.tickers or other not in cfg.tickers:
            raise ApiError("unknown pair")
        source = body.get("source")
        cache = DailyCache(self._provider(cfg, None if source == "synthetic" else source), cfg.root,
                           cfg.general.skip_weekends)
        start = now_msk().date() - timedelta(days=760)
        df = pd.concat([cache.daily(buy, start)["close"], cache.daily(other, start)["close"]],
                       axis=1, join="inner").dropna().astype(float)
        z = z_history(np.log(df.iloc[:, 0].to_numpy()), np.log(df.iloc[:, 1].to_numpy()), cfg.stats)
        if not z:
            raise ApiError("not enough history for this pair")
        tail = df.iloc[-len(z):]
        return {"buy": buy, "other": other, "dates": [d.date().isoformat() for d in tail.index], "z": z,
                "a": (tail.iloc[:, 0] / tail.iloc[0, 0] * 100).round(2).tolist(),
                "b": (tail.iloc[:, 1] / tail.iloc[0, 1] * 100).round(2).tolist()}

    # ---- trading processes -----------------------------------------------------------
    def post_trade_start(self, body: dict) -> dict:
        mode = body.get("mode")
        if mode not in MODES:
            raise ApiError("unknown mode")
        if self._running(mode):
            raise ApiError(f"{mode} is already running")
        if mode == "live" and body.get("confirm") != "LIVE":
            raise ApiError("live trading needs the typed confirmation")
        if mode != "paper" and not load_secret("TBANK_TOKEN", self.root):
            raise ApiError("add a T-Bank token in Settings first")
        self.cfg()
        (self.root / "logs").mkdir(exist_ok=True)
        (self.root / "state").mkdir(exist_ok=True)
        (self.root / "state" / f"{mode}.stop").unlink(missing_ok=True)
        cmd = [sys.executable, str(ROOT / "bot.py"), "--config", str(self.cfg_file), mode]
        cmd += ["--confirm-real-money"] if mode == "live" else []
        cmd += ["--once"] if body.get("once") else []
        with (self.root / "logs" / f"ui_{mode}.err").open("w", encoding="utf-8") as err:
            self.procs[mode] = subprocess.Popen(
                cmd, cwd=self.root, stdout=subprocess.DEVNULL, stderr=err, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": True}

    def post_trade_stop(self, body: dict) -> dict:
        mode = body.get("mode")
        if mode not in MODES or not self._running(mode):
            raise ApiError("not running")
        # Ask for a clean stop: the bot exits between cycles, never in the middle of an order.
        (self.root / "state" / f"{mode}.stop").write_text("stop", encoding="utf-8")
        return {"ok": True}

    def post_risk_reset(self, body: dict) -> dict:
        mode = body.get("mode")
        path = self.root / "state" / f"{mode}.json"
        if mode not in MODES or not path.exists():
            raise ApiError("no saved state for this mode")
        if self._running(mode):
            raise ApiError("stop trading first, then clear the pause")
        state = json.loads(path.read_text(encoding="utf-8"))
        state.setdefault("risk", {}).update(halt_until="", halt_reason="", consecutive_losses=0)
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"ok": True}

    def shutdown(self) -> None:
        running = [m for m in MODES if self._running(m)]
        for m in running:
            (self.root / "state" / f"{m}.stop").write_text("stop", encoding="utf-8")
        if running:
            print("Waiting for trading processes to finish their cycle ...")
        for m in running:
            try:
                self.procs[m].wait(timeout=200)
            except subprocess.TimeoutExpired:
                self.procs[m].terminate()

    # ---- journal and logs ------------------------------------------------------------
    def get_journal(self, q: dict) -> dict:
        folder = self.root / "journal" / "passports"
        names = sorted(p.stem for p in folder.glob("*.csv")) if folder.exists() else []
        return {"trades": _read_csv(self.root / "journal" / "trades.csv"), "passports": names}

    def get_passport(self, q: dict) -> dict:
        folder = self.root / "journal" / "passports"
        name = q.get("name", "")
        if not folder.exists() or name not in {p.stem for p in folder.glob("*.csv")}:
            raise ApiError("unknown passport", 404)
        return {"rows": _read_csv(folder / f"{name}.csv")}

    def get_log(self, q: dict) -> dict:
        path = self.root / "logs" / "bot.log"
        if not path.exists():
            return {"lines": []}
        n = max(1, min(int(q.get("n", 300)), 2000))
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400 * n))
            text = f.read().decode("utf-8", errors="replace")
        return {"lines": text.splitlines()[-n:]}

    # ---- settings --------------------------------------------------------------------
    def get_config(self, q: dict) -> dict:
        text = self.cfg_file.read_text(encoding="utf-8") if self.cfg_file.exists() else ""
        try:
            cfg = load_config(self.cfg_file)
        except Exception as e:
            return {"error": str(e), "raw": text, "sections": [], "pairs": []}
        return {"raw": text, "sections": config_edit.describe(text, cfg), "pairs": [asdict(p) for p in cfg.pairs]}

    def _write_config(self, text: str) -> dict:
        tmp = self.cfg_file.with_name("config.check.toml")
        tmp.write_text(text, encoding="utf-8")
        try:
            load_config(tmp)
        except Exception as e:
            raise ApiError(f"not saved: {e}") from None
        finally:
            tmp.unlink(missing_ok=True)
        if self.cfg_file.exists():
            self.cfg_file.with_suffix(".toml.bak").write_text(self.cfg_file.read_text(encoding="utf-8"),
                                                              encoding="utf-8")
        self.cfg_file.write_text(text, encoding="utf-8")
        return {"ok": True, "restart": [m for m in MODES if self._running(m)]}

    def post_config(self, body: dict) -> dict:
        text = self.cfg_file.read_text(encoding="utf-8")
        try:
            text = config_edit.set_values(text, config_edit.coerce(self.cfg(), body.get("values", {})))
        except ValueError as e:
            raise ApiError(str(e)) from None
        if "pairs" in body:
            pairs = [{k: str(p.get(k, "")).strip() for k in ("a", "b", "sector")} for p in body["pairs"]]
            for p in pairs:
                p["a"], p["b"] = p["a"].upper(), p["b"].upper()
                if not p["a"] or not p["b"] or p["a"] == p["b"]:
                    raise ApiError("every pair needs two different tickers")
                p["sector"] = p["sector"] or "other"
            text = config_edit.set_pairs(text, pairs)
        return self._write_config(text)

    def post_config_raw(self, body: dict) -> dict:
        return self._write_config(str(body.get("text", "")))

    def post_token(self, body: dict) -> dict:
        updates = {}
        if body.get("token"):
            updates["TBANK_TOKEN"] = str(body["token"]).strip()
        if body.get("clear"):
            updates["TBANK_TOKEN"] = ""
        if "account_id" in body:
            updates["TBANK_ACCOUNT_ID"] = str(body["account_id"]).strip()
        if any(c in v for v in updates.values() for c in "\r\n\"' "):
            raise ApiError("the value contains spaces or quotes")
        env = self.root / ".env"
        lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
        lines = [l for l in lines if l.partition("=")[0].strip().lstrip("# ") not in updates]
        lines += [f"{k}={v}" for k, v in updates.items() if v]
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"ok": True}

    def post_token_test(self, body: dict) -> dict:
        from .tbank import TBankClient, TBankError

        token = load_secret("TBANK_TOKEN", self.root)
        if not token:
            raise ApiError("no token saved")
        try:
            accounts = TBankClient(token, sandbox=bool(body.get("sandbox")), root=ROOT).accounts()
        except TBankError as e:
            raise ApiError(str(e)) from None
        return {"accounts": [{"id": a.get("id"), "name": a.get("name", ""), "type": a.get("type", ""),
                              "access": a.get("accessLevel", "")} for a in accounts]}


class Handler(BaseHTTPRequestHandler):
    app: App = None
    hosts: set = set()

    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api(self, method: str, read_body) -> None:
        url = urlparse(self.path)
        # The Host check blocks DNS rebinding; the per-start token blocks requests from other pages.
        if self.headers.get("Host") not in self.hosts:
            return self._send(403, b'{"error": "forbidden"}')
        if method == "GET" and url.path in ("/", "/index.html", "/ui.html"):
            html = (ROOT / "ui.html").read_text(encoding="utf-8").replace("__API_TOKEN__", self.app.token)
            return self._send(200, html.encode("utf-8"), "text/html")
        fn = getattr(self.app, method.lower() + "_" + url.path.removeprefix("/api/").replace("/", "_"), None)
        if not url.path.startswith("/api/") or not fn:
            return self._send(404, b'{"error": "not found"}')
        if not secrets.compare_digest(self.headers.get("X-Token", ""), self.app.token):
            return self._send(403, b'{"error": "session expired, reload the page"}')
        try:
            arg = read_body() if method == "POST" else {k: v[0] for k, v in parse_qs(url.query).items()}
            self._send(200, json.dumps(clean(fn(arg)), ensure_ascii=False).encode("utf-8"))
        except ApiError as e:
            self._send(e.code, json.dumps({"error": str(e)}).encode("utf-8"))
        except Exception as e:
            self.app._log_error(traceback.format_exc())
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode("utf-8"))

    def do_GET(self) -> None:
        self._api("GET", None)

    def do_POST(self) -> None:
        def body():
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError("bad request") from None
            if not isinstance(data, dict):
                raise ApiError("bad request")
            return data

        self._api("POST", body)


class _Server(ThreadingHTTPServer):
    # Windows lets a second program bind a port that is already in use unless this is off.
    allow_reuse_address = False


def serve(config_path: str | None, port: int = 8791, open_browser: bool = True) -> None:
    Handler.app = app = App(config_path)
    httpd = None
    for candidate in range(port, port + 20):  # another program may already use the port: take the next one
        try:
            httpd = _Server(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"No free port between {port} and {port + 19}; start with --port <number>.")
    port = candidate
    Handler.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    url = f"http://127.0.0.1:{port}"
    print(f"Interface: {url}\nKeep this window open while you use it. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        httpd.server_close()
