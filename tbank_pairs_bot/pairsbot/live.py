"""Scan / paper / sandbox / live engine: one cycle = manage open positions, then look for entries."""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .data import DailyCache, now_msk
from .risk import RiskState, position_shares, sector_risk
from .screens import entry_screen, market_state, pair_broken, size_factor, statistical_screen, tqs_score
from .stats import coint_pvalue, pair_snapshot

log = logging.getLogger("pairsbot")
_DONE = ("_FILL", "_REJECTED", "_CANCELLED")


def write_atomic(path: Path, text: str) -> None:
    """Replace a file in one step, so a crash or a concurrent reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: another process has the file open for a moment
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


class PaperExecutor:
    """Simulated broker: fills against the live quote, keeps cash and positions in the state file."""

    def __init__(self, cfg, state: dict):
        self.cfg, self.state = cfg, state
        state.setdefault("cash", cfg.general.capital)

    def account(self, last: dict) -> tuple:
        held = sum(p["qty"] * last.get(p["ticker"], p["entry_price"]) for p in self.state["positions"])
        return self.state["cash"] + held, self.state["cash"]

    def holdings(self) -> dict | None:
        return None

    def buy(self, inst: dict, shares: int, limit: float, ask: float, key: str) -> tuple | None:
        if ask > limit:
            return None  # the limit order would not have been filled
        fee = shares * ask * self.cfg.costs.commission
        self.state["cash"] -= shares * ask + fee
        return shares, ask, fee

    def resolve_buy(self, inst: dict, key: str, ref: float = 0.0) -> tuple | None:
        return None  # a simulated buy is recorded in the same step, nothing can be left open

    def check_sell(self, key: str, ref: float, shares: float) -> tuple:
        return ("gone",)  # a simulated sell is booked in the same step

    def sell(self, pos: dict, inst: dict, price: float, key: str) -> tuple:
        price *= 1 - self.cfg.costs.slippage
        fee = pos["qty"] * price * self.cfg.costs.commission
        self.state["cash"] += pos["qty"] * price - fee
        return price, fee

    def place_stop(self, pos: dict, inst: dict) -> str:
        return ""


class TBankExecutor:
    """Real orders through the T-Bank API (sandbox or production account)."""

    def __init__(self, cfg, client, account_id: str):
        self.cfg, self.client, self.account_id = cfg, client, account_id

    def account(self, last: dict) -> tuple:
        p = self.client.portfolio(self.account_id)
        return p["equity"], p["cash"]

    def holdings(self) -> dict | None:
        return self.client.portfolio(self.account_id)["positions"]

    def _wait(self, order_id: str, seconds: int, by_request: bool = False) -> dict:
        deadline = time.time() + seconds
        while True:
            s = self.client.order_state(self.account_id, order_id, by_request)
            if s["status"].endswith(_DONE) or time.time() >= deadline:
                return s
            time.sleep(3)

    @staticmethod
    def _fill_price(s: dict, ref: float, shares: float) -> float:
        """Average fill price per share. The API description does not say plainly which field holds it,
        so take the first candidate that is close to the price the order was sent at."""
        candidates = [s["avg_price"], s["executed_total"] / shares if shares else 0.0, s["executed_total"]]
        for c in candidates:
            if c > 0 and (not ref or 0.8 < c / ref < 1.25):
                return c
        return ref

    def _settle_buy(self, inst: dict, order_id: str, by_request: bool, seconds: int, ref: float = 0.0) -> tuple | None:
        s = self._wait(order_id, seconds, by_request)
        if not s["status"].endswith(_DONE):
            try:
                self.client.cancel_order(self.account_id, s["order_id"] or order_id)
            except Exception as e:  # it may have filled in the meantime
                log.warning("cancel of order %s failed: %s", order_id, e)
            s = self._wait(order_id, 15, by_request)
            if not s["status"].endswith(_DONE):
                raise RuntimeError(f"order {order_id} is still active after the cancel request")
        if s["lots"] <= 0:
            return None
        shares = s["lots"] * inst["lot"]
        return shares, self._fill_price(s, ref, shares), s["commission"]

    def buy(self, inst: dict, shares: int, limit: float, ask: float, key: str) -> tuple | None:
        order_id = self.client.post_order(self.account_id, inst["uid"], shares // inst["lot"], True, limit, key)
        return self._settle_buy(inst, order_id, False, self.cfg.live.order_timeout_seconds, limit)

    def resolve_buy(self, inst: dict, key: str, ref: float = 0.0) -> tuple | None:
        """What became of a buy whose result was never recorded (crash or network error)."""
        from .tbank import TBankError

        try:
            return self._settle_buy(inst, key, True, 0, ref)
        except TBankError as e:
            if e.not_found:  # the order never reached the broker
                return None
            raise

    def check_sell(self, key: str, ref: float, shares: float) -> tuple:
        """State of a sell sent earlier: ("filled", price, fee), ("open",) or ("gone",)."""
        from .tbank import TBankError

        try:
            s = self._wait(key, 30, True)
        except TBankError as e:
            if e.not_found:  # it never reached the broker
                return ("gone",)
            raise
        if s["status"].endswith("_FILL"):
            return ("filled", self._fill_price(s, ref, shares), s["commission"])
        return ("gone",) if s["status"].endswith(_DONE) else ("open",)

    def sell(self, pos: dict, inst: dict, price: float, key: str) -> tuple:
        if pos.get("stop_order_id"):
            try:
                self.client.cancel_stop(self.account_id, pos["stop_order_id"])
            except Exception as e:
                log.warning("cancel of stop order failed (probably already executed): %s", e)
            pos["stop_order_id"] = ""
        # Never sell more than is actually held: the broker stop may already have closed the position.
        held = self.client.portfolio(self.account_id)["positions"].get(inst["uid"], 0.0)
        lots = int(min(held, pos["qty"]) // inst["lot"])
        if lots <= 0:
            return pos["stop"], pos["qty"] * pos["stop"] * self.cfg.costs.commission
        try:
            order_id = self.client.post_order(self.account_id, inst["uid"], lots, False, None, key)
            s = self._wait(order_id, 60)
            if s["lots"] < lots:
                raise RuntimeError(f"sell of {pos['ticker']} not confirmed (status {s['status']}); check the terminal")
        except Exception:
            # The broker stop was cancelled above: put it back so the position is not left unprotected.
            pos["stop_order_id"] = self.place_stop(pos, inst)
            raise
        return self._fill_price(s, price, lots * inst["lot"]), s["commission"]

    def place_stop(self, pos: dict, inst: dict) -> str:
        if not self.cfg.live.place_broker_stop:
            return ""
        try:
            return self.client.post_stop_loss(self.account_id, inst["uid"], pos["qty"] // inst["lot"], pos["stop"])
        except Exception as e:
            log.warning("broker stop for %s not placed, the bot will watch the stop itself: %s", pos["ticker"], e)
            return ""


def in_session(cfg, now: datetime) -> bool:
    hm = now.strftime("%H:%M")
    return now.weekday() < 5 and cfg.live.session_start <= hm <= cfg.live.session_end


class Engine:
    def __init__(self, cfg, provider, mode: str, executor_factory=None):
        self.cfg, self.p, self.mode = cfg, provider, mode
        self.cache = DailyCache(provider, cfg.root, cfg.general.skip_weekends)
        self.state_path = cfg.root / "state" / f"{mode}.json"
        self.state = {"positions": [], "risk": {}, "passport_day": "", "last_exit": {}, "pending": None}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))
        known = RiskState.__dataclass_fields__
        self.risk = RiskState(**{k: v for k, v in self.state["risk"].items() if k in known})
        self.exec = executor_factory(self.state) if executor_factory else None
        self.last_market, self.note = "", ""
        self._coint = {}

    def save(self) -> None:
        self.state["risk"] = self.risk.to_dict()
        write_atomic(self.state_path, json.dumps(self.state, indent=2, ensure_ascii=False))

    def _journal(self, name: str, row: dict) -> None:
        path = self.cfg.root / "journal" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)

    # ---- data ------------------------------------------------------------------------
    def _closes(self, today: date) -> tuple:
        start = today - timedelta(days=1100)  # ~750 sessions: enough for EMA200 of the index to settle
        tickers = self.cfg.tickers
        last = self.p.last_prices(tickers)
        closes = {}
        for t in tickers + [self.cfg.general.index_ticker]:
            s = self.cache.daily(t, start)["close"].astype(float)
            if t in last and (today.weekday() < 5 or not self.cfg.general.skip_weekends):
                s.loc[pd.Timestamp(today)] = last[t]  # today's point is the current price
            closes[t] = s
        return closes, last

    def _snapshot(self, closes: dict, buy: str, other: str, today: date):
        df = pd.concat([closes[buy], closes[other]], axis=1, join="inner").dropna()
        la, lb = np.log(df.iloc[:, 0].to_numpy()), np.log(df.iloc[:, 1].to_numpy())
        key = (buy, other, today)
        if key not in self._coint:  # cointegration is re-tested once a day
            w = self.cfg.stats.coint_window
            self._coint = {k: v for k, v in self._coint.items() if k[2] == today}
            self._coint[key] = coint_pvalue(la[-w:], lb[-w:]) if len(la) >= w else 1.0
        return pair_snapshot(la, lb, self.cfg.stats, coint_p=self._coint[key])

    def _tradable(self, ticker: str) -> bool:
        """False when the exchange is not trading the stock right now (holiday, halt, auction)."""
        try:
            return self.p.tradable(ticker) is not False
        except Exception as e:
            log.warning("trading status of %s unavailable: %s", ticker, e)
            return True

    def _close_requests(self) -> set:
        path = self.state_path.with_suffix(".close")
        try:
            return set(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()
        except (OSError, ValueError):
            return set()

    def _drop_close_request(self, ticker: str) -> None:
        path = self.state_path.with_suffix(".close")
        left = sorted(self._close_requests() - {ticker})
        if left:
            write_atomic(path, json.dumps(left))
        else:
            path.unlink(missing_ok=True)

    # ---- one cycle -------------------------------------------------------------------
    def cycle(self, trade: bool = True, ignore_session: bool = False, entries: bool = True) -> list:
        cfg, st, rk = self.cfg, self.cfg.stats, self.cfg.risk
        now = now_msk()
        today = now.date()
        self.note = ""
        if trade and not ignore_session and not in_session(cfg, now):
            trade = False  # statistics only: no orders are sent while the exchange session is closed
            self.note = "session closed, no orders"
        closes, last = self._closes(today)
        mstate = self.last_market = market_state(closes[cfg.general.index_ticker]).iloc[-1]
        positions = self.state["positions"]

        if trade:
            self._recover_pending(today)
            holdings = self.exec.holdings()
            requested = self._close_requests()
            for pos in positions[:]:
                self._manage(pos, closes, last, today, holdings, requested)
            equity, cash = self.exec.account(last)
            self.risk.new_day(today, equity)
        else:
            equity = cash = cfg.general.capital

        rows, candidates = [], []
        held = {p["ticker"] for p in positions} | {p["other"] for p in positions}
        for p in cfg.pairs:
            for buy, other in ((p.a, p.b), (p.b, p.a)):
                snap = self._snapshot(closes, buy, other, today)
                if snap is None:
                    rows.append({"pair": f"{buy}/{other}", "verdict": "not enough history"})
                    continue
                fails = statistical_screen(snap, st)
                if not fails and mstate == "weak":
                    fails.append("index below EMA200")
                if not fails and (buy in cfg.live.news_blackout or other in cfg.live.news_blackout):
                    fails.append("news blackout")
                if not fails and (buy in held or other in held):
                    fails.append("already in position")
                if not fails and self.state["last_exit"].get(buy) == today.isoformat():
                    fails.append("closed today, no re-entry")
                row = {"pair": f"{buy}/{other}", "corr": snap.corr, "corr60": snap.corr_roll,
                       "coint_p": snap.coint_p, "beta": snap.beta, "beta_chg": snap.beta_change,
                       "half_life": snap.half_life, "z": snap.z,
                       "tqs": tqs_score(snap, mstate, 0, st), "verdict": "; ".join(fails)}
                rows.append(row)
                if trade and self.state["passport_day"] != today.isoformat():
                    self._journal(f"passports/{buy}_{other}.csv", {"date": today.isoformat(), **{
                        k: round(v, 4) if isinstance(v, float) and math.isfinite(v) else v
                        for k, v in row.items() if k not in ("pair", "verdict")}})
                if not fails:
                    candidates.append((row, snap, buy, other, p.sector))
        if trade:
            self.state["passport_day"] = today.isoformat()

        scored = []
        for row, snap, buy, other, sector in candidates:
            book, pts = None, 0
            try:
                book = self.p.order_book(buy)
            except Exception as e:
                log.warning("order book for %s unavailable: %s", buy, e)
            if cfg.entry.enabled:
                bars = self.p.candles(buy, "15m", now - timedelta(days=14), now + timedelta(minutes=1))
                res = entry_screen(bars, book, cfg.entry)
                pts = res.points
                if not res.ok:
                    row["verdict"] = "; ".join(res.reasons)
                    continue
            row["tqs"] = tqs_score(snap, mstate, pts, st)
            factor = size_factor(row["tqs"], cfg.tqs)
            if factor <= 0:
                row["verdict"] = f"TQS {row['tqs']} below {cfg.tqs.reduced_min_score}"
                continue
            row["verdict"] = "BUY SIGNAL" + (" (reduced size)" if factor < 1 else "")
            scored.append((row, snap, buy, other, sector, factor, book))

        if trade:
            for row, snap, buy, other, sector, factor, book in sorted(scored, key=lambda x: -x[0]["tqs"]):
                if not entries:
                    row["verdict"] = "signal skipped: exits only"
                    continue
                if buy in held or other in held:
                    continue
                blocked = self.risk.can_open(today, equity, rk)
                if not blocked and sector_risk(positions, sector, equity) + rk.risk_per_trade * factor \
                        > rk.max_sector_risk + 1e-9:
                    blocked = "sector risk limit"
                if not blocked and not self._tradable(buy):
                    blocked = "stock is not trading now"
                if blocked:
                    row["verdict"] = f"signal skipped: {blocked}"
                    log.info("%s/%s: %s", buy, other, row["verdict"])
                    continue
                if self._open(row, snap, buy, other, sector, factor, book, last, equity, cash, today):
                    held |= {buy, other}
                    equity, cash = self.exec.account(last)
            self.state["last_cycle"] = now.isoformat(timespec="seconds")
            self.save()
        return rows

    # ---- exits -----------------------------------------------------------------------
    def _manage(self, pos: dict, closes: dict, last: dict, today: date, holdings: dict | None,
                requested: set) -> None:
        st, rk = self.cfg.stats, self.cfg.risk
        inst = self.p.instrument(pos["ticker"])
        price = last.get(pos["ticker"])
        snap = self._snapshot(closes, pos["ticker"], pos["other"], today)
        closing = pos.get("closing")
        if closing:  # a sell was started earlier and its result is unknown: finish it first
            return self._close(pos, inst, price or pos["stop"], closing["reason"], snap, today, closing["key"], True)

        if holdings is not None and holdings.get(inst["uid"], 0.0) < inst["lot"]:
            # The shares are gone from the account: the broker stop fired, or they were sold by hand.
            # Require two cycles in a row so that one bad portfolio answer cannot erase a position.
            pos["missing"] = pos.get("missing", 0) + 1
            if pos["missing"] < 2:
                return self.save()
            reason = "stop" if pos.get("stop_order_id") else "external"
            exit_price = pos["stop"] if reason == "stop" else (price or pos["entry_price"])
            fee = pos["qty"] * exit_price * self.cfg.costs.commission  # the real fill is not known here
            return self._book(pos, exit_price, fee, reason, snap, today)
        pos.pop("missing", None)
        if holdings is not None and not pos.get("stop_order_id"):  # an earlier attempt failed: try again
            pos["stop_order_id"] = self.exec.place_stop(pos, inst)
            if pos["stop_order_id"]:
                self.save()

        if not price:
            return
        reason = None
        if pos["ticker"] in requested:
            reason = "manual"
        elif price <= pos["stop"]:
            reason = "stop"
        elif pos.get("target") and price >= pos["target"]:
            reason = "target"
        elif snap and snap.z >= st.exit_z:
            reason = "mean_reversion"
        elif snap and pair_broken(snap, st):
            reason = "pair_breakdown"
        elif rk.max_holding_days and (today - date.fromisoformat(pos["entry_date"])).days >= rk.max_holding_days:
            reason = "time_stop"
        if not reason:
            return
        if not self._tradable(pos["ticker"]):
            log.info("%s: exit (%s) waits, the stock is not trading now", pos["ticker"], reason)
            return
        key = str(uuid.uuid4())
        pos["closing"] = {"key": key, "reason": reason}
        self.save()  # remember the sell before sending it, so a crash cannot lead to selling twice
        self._close(pos, inst, price, reason, snap, today, key, False)

    def _close(self, pos, inst, price, reason, snap, today, key, retry) -> None:
        if retry:  # an earlier attempt may already have sold: look that order up before selling again
            outcome = self.exec.check_sell(key, price, pos["qty"])
            if outcome[0] == "filled":
                return self._book(pos, outcome[1] or price, outcome[2], reason, snap, today)
            if outcome[0] == "gone":  # rejected, cancelled or never sent: a fresh order is needed
                key = pos["closing"]["key"] = str(uuid.uuid4())
                self.save()
        try:
            exit_price, fee = self.exec.sell(pos, inst, price, key)
        except Exception:
            self.save()  # keeps the id of a re-placed broker stop
            raise
        self._book(pos, exit_price, fee, reason, snap, today)

    def _book(self, pos, exit_price, fee, reason, snap, today) -> None:
        pnl = pos["qty"] * (exit_price - pos["entry_price"]) - fee - pos.get("entry_fee", 0.0)
        self.state["positions"].remove(pos)
        self.state["last_exit"] = {pos["ticker"]: today.isoformat(),
                                   **{k: v for k, v in self.state["last_exit"].items() if v == today.isoformat()}}
        self.risk.trade_closed(pnl, today, self.cfg.risk)
        self.save()
        self._drop_close_request(pos["ticker"])
        self._journal("trades.csv", {
            "mode": self.mode, "pair": pos["pair"], "bought": pos["ticker"], "entry_date": pos["entry_date"],
            "exit_date": today.isoformat(), "qty": pos["qty"], "entry_price": pos["entry_price"],
            "exit_price": round(exit_price, 4), "stop": pos["stop"], "z_entry": pos["z_entry"],
            "z_exit": round(snap.z, 2) if snap else "", "tqs": pos["tqs"], "reason": reason, "pnl": round(pnl, 2)})
        log.info("CLOSED %s x%s at %.4f (%s), PnL %.2f RUB", pos["ticker"], pos["qty"], exit_price, reason, pnl)

    # ---- entries ---------------------------------------------------------------------
    def _recover_pending(self, today: date) -> None:
        """Finish a buy that was sent but never recorded. New entries wait until this is settled."""
        pending = self.state.get("pending")
        if not pending:
            return
        inst = self.p.instrument(pending["ticker"])
        try:
            fill = self.exec.resolve_buy(inst, pending["key"], pending["limit"])
        except Exception:
            # Orders live for one trading day, so an old order that still cannot be read is no longer open.
            if pending["date"] >= (today - timedelta(days=3)).isoformat():
                raise
            log.error("buy %s of %s could not be checked for 3 days, dropping it: verify the account by hand",
                      pending["key"], pending["ticker"])
            fill = None
        if fill:
            log.warning("recovered an unrecorded buy of %s", pending["ticker"])
            self._record(pending, inst, fill)
        else:
            self.state["pending"] = None
            self.save()

    def _open(self, row, snap, buy, other, sector, factor, book, last, equity, cash, today) -> bool:
        rk = self.cfg.risk
        inst = self.p.instrument(buy)
        ask = book[1] if book else last.get(buy)
        if not ask:
            return False
        # Limit order at the current offer, on the price grid and never below the offer.
        limit = round(math.ceil(ask / inst["step"] - 1e-9) * inst["step"], 9)
        shares = position_shares(equity, cash / (1 + self.cfg.costs.commission), limit, inst["lot"], rk, factor)
        if shares <= 0:
            row["verdict"] = "signal skipped: not enough cash for one lot"
            return False
        pending = {"key": str(uuid.uuid4()), "ticker": buy, "other": other, "sector": sector, "limit": limit,
                   "z_entry": round(snap.z, 2), "tqs": row["tqs"], "date": today.isoformat()}
        self.state["pending"] = pending
        self.save()  # remember the order before sending it, so a crash cannot lose a filled buy
        fill = self.exec.buy(inst, shares, limit, ask, pending["key"])
        if not fill:
            self.state["pending"] = None
            self.save()
            row["verdict"] = "limit order not filled"
            log.info("%s: limit order at %.4f not filled, cancelled", buy, limit)
            return False
        pos = self._record(pending, inst, fill)
        row["verdict"] = f"BOUGHT {pos['qty']} at {pos['entry_price']:.4f}, stop {pos['stop']}"
        return True

    def _record(self, pending: dict, inst: dict, fill: tuple) -> dict:
        rk = self.cfg.risk
        qty, price, fee = fill
        price = price or pending["limit"]
        step = inst["step"]
        pos = {"pair": f"{pending['ticker']}/{pending['other']}", "ticker": pending["ticker"],
               "other": pending["other"], "sector": pending["sector"], "qty": qty,
               "entry_price": price, "entry_fee": fee, "entry_date": pending["date"],
               "stop": round(math.floor(price * (1 - rk.stop_pct) / step + 1e-9) * step, 9),
               "target": round(price * (1 + rk.take_profit_pct), 4) if rk.take_profit_pct > 0 else None,
               "z_entry": pending["z_entry"], "tqs": pending["tqs"], "stop_order_id": ""}
        self.state["positions"].append(pos)
        self.state["pending"] = None
        self.save()  # record the position before anything else can fail
        pos["stop_order_id"] = self.exec.place_stop(pos, inst)
        self.save()
        log.info("OPENED %s x%s at %.4f, stop %s, Z %.2f, TQS %s", pos["ticker"], qty, price, pos["stop"],
                 pos["z_entry"], pos["tqs"])
        return pos


def format_rows(rows: list) -> str:
    head = f"{'pair (buy/vs)':<14}{'corr':>6}{'cor60':>6}{'p':>7}{'beta':>6}{'dBeta':>7}{'HL':>6}{'Z':>7}{'TQS':>5}  verdict"
    out = [head, "-" * len(head)]
    for r in rows:
        if "z" not in r:
            out.append(f"{r['pair']:<14}{'':>50}  {r['verdict']}")
            continue
        hl = f"{r['half_life']:.0f}" if math.isfinite(r["half_life"]) else "inf"
        out.append(f"{r['pair']:<14}{r['corr']:>6.2f}{r['corr60']:>6.2f}{r['coint_p']:>7.3f}{r['beta']:>6.2f}"
                   f"{r['beta_chg']:>7.0%}{hl:>6}{r['z']:>7.2f}{r['tqs']:>5}  {r['verdict'] or 'screen 1 passed'}")
    return "\n".join(out)


LOCK_FRESH_SECONDS = 15


def lock_is_fresh(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < LOCK_FRESH_SECONDS
    except OSError:
        return False


class _Lock:
    """One trading process per mode: a file whose modification time is refreshed while the process lives."""

    def __init__(self, path: Path):
        self.path, self._stop = path, threading.Event()

    def __enter__(self):
        busy = SystemExit(f"{self.path.stem} trading is already running in another window")
        if lock_is_fresh(self.path):
            raise busy
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)  # left over from a process that was killed
        try:
            with open(self.path, "x", encoding="utf-8") as f:  # only one of two racing starters gets it
                f.write(str(os.getpid()))
        except FileExistsError:
            raise busy from None
        threading.Thread(target=self._beat, daemon=True).start()
        return self

    def _beat(self) -> None:
        while not self._stop.wait(3):
            try:
                os.utime(self.path)
            except OSError:
                pass

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self.path.unlink(missing_ok=True)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def run_loop(engine: Engine, once: bool = False, entries: bool = True) -> None:
    cfg = engine.cfg
    close_file = engine.state_path.with_suffix(".close")
    # The web interface asks for a clean stop by creating this file; it is honoured between cycles.
    stop = engine.state_path.with_suffix(".stop")
    # When started from the interface, stop once the interface is gone (its window was closed).
    ui_alive = os.environ.get("PAIRSBOT_UI_ALIVE")
    stale = [0]

    def should_stop() -> str:
        if stop.exists():
            return "stop requested"
        # Several checks in a row, so a PC waking from sleep does not look like a closed interface.
        stale[0] = stale[0] + 1 if ui_alive and not lock_is_fresh(Path(ui_alive)) else 0
        return "the interface was closed" if stale[0] >= 10 else ""

    with _Lock(engine.state_path.with_suffix(".lock")):
        stop.unlink(missing_ok=True)
        log.info("mode %s, data %s, %d open position(s)", engine.mode, engine.p.name,
                 len(engine.state["positions"]))
        try:
            while True:
                reason = should_stop()
                if reason:
                    log.info("%s, exiting", reason)
                    return
                if once or in_session(cfg, now_msk()):
                    try:
                        rows = engine.cycle(trade=True, entries=entries)
                        log.info("cycle done%s\n%s", f" ({engine.note})" if engine.note else "", format_rows(rows))
                    except Exception:
                        log.exception("cycle failed, will retry on the next poll")
                if once:
                    return
                # A new manual close request from the interface is handled at once, not at the next poll.
                requests_at, waited = _mtime(close_file), 0
                while waited < cfg.live.poll_seconds and not should_stop() and _mtime(close_file) == requests_at:
                    time.sleep(1)
                    waited += 1
        finally:
            stop.unlink(missing_ok=True)
