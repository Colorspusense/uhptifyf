"""Scan / paper / sandbox / live engine: one cycle = manage open positions, then look for entries."""
from __future__ import annotations

import csv
import json
import logging
import math
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .data import DailyCache, now_msk
from .risk import RiskState, position_shares, sector_risk
from .screens import entry_screen, market_state, pair_broken, size_factor, statistical_screen, tqs_score
from .stats import coint_pvalue, pair_snapshot

log = logging.getLogger("pairsbot")


class PaperExecutor:
    """Simulated broker: fills against the live quote, keeps cash and positions in the state file."""

    def __init__(self, cfg, state: dict):
        self.cfg, self.state = cfg, state
        state.setdefault("cash", cfg.general.capital)

    def account(self, last: dict) -> tuple:
        held = sum(p["qty"] * last.get(p["ticker"], p["entry_price"]) for p in self.state["positions"])
        return self.state["cash"] + held, self.state["cash"]

    def buy(self, inst: dict, shares: int, limit: float, ask: float) -> tuple | None:
        if ask > limit:
            return None  # the limit order would not have been filled
        fee = shares * ask * self.cfg.costs.commission
        self.state["cash"] -= shares * ask + fee
        return shares, ask, fee

    def sell(self, pos: dict, inst: dict, price: float) -> tuple:
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

    def _wait(self, order_id: str, seconds: int) -> dict:
        deadline = time.time() + seconds
        while True:
            s = self.client.order_state(self.account_id, order_id)
            if s["status"].endswith(("_FILL", "_REJECTED", "_CANCELLED")) or time.time() >= deadline:
                return s
            time.sleep(3)

    def buy(self, inst: dict, shares: int, limit: float, ask: float) -> tuple | None:
        oid = self.client.post_order(self.account_id, inst["uid"], shares // inst["lot"], True, limit)
        s = self._wait(oid, self.cfg.live.order_timeout_seconds)
        if not s["status"].endswith(("_FILL", "_REJECTED", "_CANCELLED")):
            try:
                self.client.cancel_order(self.account_id, oid)
            except Exception as e:  # it may have filled in the meantime
                log.warning("cancel of order %s failed: %s", oid, e)
            s = self.client.order_state(self.account_id, oid)
        if s["lots"] <= 0:
            return None
        return s["lots"] * inst["lot"], s["avg_price"] or limit, s["commission"]

    def sell(self, pos: dict, inst: dict, price: float) -> tuple:
        if pos.get("stop_order_id"):
            try:
                self.client.cancel_stop(self.account_id, pos["stop_order_id"])
            except Exception as e:
                log.warning("cancel of stop order failed (probably already executed): %s", e)
        # Never sell more than is actually held: the broker stop may already have closed the position.
        held = self.client.portfolio(self.account_id)["positions"].get(inst["uid"], 0.0)
        lots = int(min(held, pos["qty"]) // inst["lot"])
        if lots <= 0:
            return pos["stop"], 0.0
        s = self._wait(self.client.post_order(self.account_id, inst["uid"], lots, False, None), 60)
        if s["lots"] < lots:
            raise RuntimeError(f"sell of {pos['ticker']} not confirmed (status {s['status']}); check the terminal")
        return s["avg_price"] or price, s["commission"]

    def place_stop(self, pos: dict, inst: dict) -> str:
        if not self.cfg.live.place_broker_stop:
            return ""
        try:
            return self.client.post_stop_loss(self.account_id, inst["uid"], pos["qty"] // inst["lot"], pos["stop"])
        except Exception as e:
            log.warning("broker stop for %s not placed, the bot will watch the stop itself: %s", pos["ticker"], e)
            return ""


class Engine:
    def __init__(self, cfg, provider, mode: str, executor_factory=None):
        self.cfg, self.p, self.mode = cfg, provider, mode
        self.cache = DailyCache(provider, cfg.root, cfg.general.skip_weekends)
        self.state_path = cfg.root / "state" / f"{mode}.json"
        self.state = {"positions": [], "risk": {}, "passport_day": "", "last_exit": {}}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))
        self.risk = RiskState(**self.state["risk"])
        self.exec = executor_factory(self.state) if executor_factory else None
        self._coint = {}

    def save(self) -> None:
        self.state["risk"] = self.risk.to_dict()
        self.state_path.parent.mkdir(exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")

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
        start = today - timedelta(days=620)
        tickers = self.cfg.tickers
        last = self.p.last_prices(tickers)
        closes = {}
        for t in tickers + [self.cfg.general.index_ticker]:
            s = self.cache.daily(t, start)["close"].astype(float)
            if t in last and today.weekday() < 5:  # today's point is the current price
                s.loc[pd.Timestamp(today)] = last[t]
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

    # ---- one cycle -------------------------------------------------------------------
    def cycle(self, trade: bool = True) -> list:
        cfg, st, rk = self.cfg, self.cfg.stats, self.cfg.risk
        now = now_msk()
        today = now.date()
        closes, last = self._closes(today)
        mstate = market_state(closes[cfg.general.index_ticker]).iloc[-1]
        positions = self.state["positions"]

        if trade:
            for pos in positions[:]:
                self._manage(pos, closes, last, today)
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
                if not fails and buy in held:
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
                if buy in held or other in held:
                    continue
                blocked = self.risk.can_open(today, equity, rk)
                if not blocked and sector_risk(positions, sector, equity) + rk.risk_per_trade * factor \
                        > rk.max_sector_risk + 1e-9:
                    blocked = "sector risk limit"
                if blocked:
                    row["verdict"] = f"signal skipped: {blocked}"
                    log.info("%s/%s: %s", buy, other, row["verdict"])
                    continue
                if self._open(row, snap, buy, other, sector, factor, book, last, equity, cash, today):
                    held |= {buy, other}
                    equity, cash = self.exec.account(last)
            self.save()
        return rows

    def _manage(self, pos: dict, closes: dict, last: dict, today: date) -> None:
        st, rk = self.cfg.stats, self.cfg.risk
        price = last.get(pos["ticker"])
        if not price:
            return
        snap = self._snapshot(closes, pos["ticker"], pos["other"], today)
        reason = None
        if price <= pos["stop"]:
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
        inst = self.p.instrument(pos["ticker"])
        exit_price, fee = self.exec.sell(pos, inst, price)
        pnl = pos["qty"] * (exit_price - pos["entry_price"]) - fee - pos.get("entry_fee", 0.0)
        self.state["positions"].remove(pos)
        self.state["last_exit"][pos["ticker"]] = today.isoformat()
        self.risk.trade_closed(pnl, today, rk)
        self._journal("trades.csv", {
            "mode": self.mode, "pair": pos["pair"], "bought": pos["ticker"], "entry_date": pos["entry_date"],
            "exit_date": today.isoformat(), "qty": pos["qty"], "entry_price": pos["entry_price"],
            "exit_price": round(exit_price, 4), "stop": pos["stop"], "z_entry": pos["z_entry"],
            "z_exit": round(snap.z, 2) if snap else "", "tqs": pos["tqs"], "reason": reason, "pnl": round(pnl, 2)})
        log.info("CLOSED %s x%s at %.4f (%s), PnL %.2f RUB", pos["ticker"], pos["qty"], exit_price, reason, pnl)
        self.save()

    def _open(self, row, snap, buy, other, sector, factor, book, last, equity, cash, today) -> bool:
        rk = self.cfg.risk
        inst = self.p.instrument(buy)
        ask = book[1] if book else last.get(buy)
        if not ask:
            return False
        limit = round(round(ask / inst["step"]) * inst["step"], 9)  # limit order at the current offer
        shares = position_shares(equity, cash / (1 + self.cfg.costs.commission), limit, inst["lot"], rk, factor)
        if shares <= 0:
            row["verdict"] = "signal skipped: not enough cash for one lot"
            return False
        fill = self.exec.buy(inst, shares, limit, ask)
        if not fill:
            row["verdict"] = "limit order not filled"
            log.info("%s: limit order at %.4f not filled, cancelled", buy, limit)
            return False
        qty, price, fee = fill
        step = inst["step"]
        pos = {"pair": f"{buy}/{other}", "ticker": buy, "other": other, "sector": sector, "qty": qty,
               "entry_price": price, "entry_fee": fee, "entry_date": today.isoformat(),
               "stop": round(math.floor(price * (1 - rk.stop_pct) / step) * step, 9),
               "target": round(price * (1 + rk.take_profit_pct), 4) if rk.take_profit_pct > 0 else None,
               "z_entry": round(snap.z, 2), "tqs": row["tqs"], "stop_order_id": ""}
        self.state["positions"].append(pos)
        self.save()  # record the position before anything else can fail
        pos["stop_order_id"] = self.exec.place_stop(pos, inst)
        self.save()
        row["verdict"] = f"BOUGHT {qty} at {price:.4f}, stop {pos['stop']}"
        log.info("OPENED %s x%s at %.4f, stop %s, Z %.2f, TQS %s", buy, qty, price, pos["stop"], snap.z, row["tqs"])
        return True


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


def in_session(cfg, now: datetime) -> bool:
    hm = now.strftime("%H:%M")
    return now.weekday() < 5 and cfg.live.session_start <= hm <= cfg.live.session_end


def run_loop(engine: Engine, once: bool = False) -> None:
    cfg = engine.cfg
    log.info("mode %s, data %s, %d open position(s)", engine.mode, engine.p.name, len(engine.state["positions"]))
    while True:
        now = now_msk()
        if once or in_session(cfg, now):
            try:
                rows = engine.cycle(trade=True)
                log.info("cycle done\n%s", format_rows(rows))
            except Exception:
                log.exception("cycle failed, will retry on the next poll")
        else:
            log.debug("outside the trading session")
        if once:
            return
        time.sleep(cfg.live.poll_seconds)
