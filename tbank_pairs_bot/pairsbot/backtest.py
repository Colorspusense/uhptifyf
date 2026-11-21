"""Simulation on history: daily backtest, its metrics and walk-forward analysis (chapter 14).

Signals are taken at the close and executed at the next day's open. Stops and targets are checked
against each day's low/high. The 15-minute entry screen cannot be replayed on daily bars, so its
0-5 TQS points come from a daily proxy (Z turning up, an up close, rising volume).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import product

import numpy as np
import pandas as pd

from .data import DailyCache, now_msk
from .risk import RiskState, position_shares, sector_risk
from .screens import market_state, pair_broken, size_factor, statistical_screen, tqs_score
from .stats import Snapshot, coint_pvalue, pair_snapshot, required_history


@dataclass
class History:
    bars: dict    # ticker -> daily OHLCV frame
    index: pd.DataFrame
    lots: dict    # ticker -> lot size


def load_history(cfg, provider, start: date, end: date | None = None) -> History:
    cache = DailyCache(provider, cfg.root, cfg.general.skip_weekends)
    today = pd.Timestamp(now_msk().date())

    def get(t):
        df = cache.daily(t, start, end)
        return df[df.index < today]  # today's candle may be unfinished

    return History({t: get(t) for t in cfg.tickers}, get(cfg.general.index_ticker),
                   {t: provider.instrument(t)["lot"] for t in cfg.tickers})


def precompute(cfg, hist: History) -> dict:
    """(buy, other, sector) -> {date: Snapshot} for both orientations of every pair."""
    st, out = cfg.stats, {}
    need = required_history(st)
    for p in cfg.pairs:
        for buy, other in ((p.a, p.b), (p.b, p.a)):
            df = pd.concat([hist.bars[buy]["close"], hist.bars[other]["close"]], axis=1, join="inner").dropna()
            la, lb = np.log(df.iloc[:, 0].to_numpy(float)), np.log(df.iloc[:, 1].to_numpy(float))
            table, pval = {}, None
            for i in range(need - 1, len(df)):
                if pval is None or (i - need + 1) % st.coint_recalc_days == 0:
                    pval = coint_pvalue(la[i + 1 - st.coint_window:i + 1], lb[i + 1 - st.coint_window:i + 1])
                snap = pair_snapshot(la[:i + 1], lb[:i + 1], st, coint_p=pval)
                if snap:
                    table[df.index[i]] = snap
            out[(buy, other, p.sector)] = table
    return out


@dataclass
class Result:
    trades: pd.DataFrame
    equity: pd.Series
    metrics: dict


def _metrics(trades: pd.DataFrame, equity: pd.Series, capital: float) -> dict:
    m = {"trades": len(trades), "final_equity": round(float(equity.iloc[-1]), 2) if len(equity) else capital}
    m["net_profit"] = round(m["final_equity"] - capital, 2)
    m["return_pct"] = round(100 * m["net_profit"] / capital, 2)
    dd = (equity.cummax() - equity) if len(equity) else pd.Series([0.0])
    m["max_drawdown"] = round(float(dd.max()), 2)
    m["max_drawdown_pct"] = round(float((dd / equity.cummax()).max() * 100), 2) if len(equity) else 0.0
    m["recovery_factor"] = round(m["net_profit"] / m["max_drawdown"], 2) if m["max_drawdown"] > 0 else None
    if len(trades):
        wins, losses = trades[trades.pnl > 0], trades[trades.pnl <= 0]
        streak = longest = 0
        for pnl in trades.pnl:
            streak = streak + 1 if pnl <= 0 else 0
            longest = max(longest, streak)
        m.update(win_rate_pct=round(100 * len(wins) / len(trades), 1),
                 avg_win=round(float(wins.pnl.mean()), 2) if len(wins) else 0.0,
                 avg_loss=round(float(losses.pnl.mean()), 2) if len(losses) else 0.0,
                 max_losing_streak=longest,
                 avg_holding_days=round(float(trades.days_held.mean()), 1),
                 exits=trades.reason.value_counts().to_dict(),
                 pnl_by_pair=trades.groupby("pair").pnl.sum().round(2).to_dict())
    return m


def daily_entry_points(df: pd.DataFrame, d, snap) -> int:
    """Stand-in for the 15-minute entry screen (0-5 TQS points) when only daily bars are available."""
    i = df.index.get_loc(d)
    return int(2 * (snap.z > snap.z_prev) + 2 * (df.close.iloc[i] > df.close.iloc[i - 1])
               + 1 * (df.volume.iloc[i] > df.volume.iloc[i - 1]))


def simulate(cfg, hist: History, pre: dict, start=None, end=None) -> Result:
    st, rk, cost = cfg.stats, cfg.risk, cfg.costs
    capital = cfg.general.capital
    market = market_state(hist.index["close"])
    calendar = hist.index.loc[start:end].index
    cash, positions, pending, trades, curve = capital, [], [], [], {}
    last_close, risk, equity, closed_today = {}, RiskState(), capital, set()

    def close(pos, price, d, reason):
        nonlocal cash
        price *= 1 - cost.slippage
        cash += pos["qty"] * price * (1 - cost.commission)
        pnl = pos["qty"] * (price * (1 - cost.commission) - pos["entry_price"] * (1 + cost.commission))
        positions.remove(pos)
        closed_today.add(pos["ticker"])
        risk.trade_closed(pnl, d.date(), rk)
        trades.append({"pair": pos["pair"], "bought": pos["ticker"], "entry_date": pos["entry_date"].date(),
                       "exit_date": d.date(), "days_held": (d - pos["entry_date"]).days, "qty": pos["qty"],
                       "entry_price": round(pos["entry_price"], 4), "exit_price": round(price, 4),
                       "z_entry": round(pos["z_entry"], 2), "tqs": pos["tqs"], "reason": reason,
                       "pnl": round(pnl, 2)})

    for d in calendar:
        risk.new_day(d.date(), equity)
        closed_today.clear()
        bars = {t: df.loc[d] for t, df in hist.bars.items() if d in df.index}

        # 1. Orders decided at the previous close are executed at today's open: exits first.
        for pos in positions[:]:
            if pos.get("exit_reason") and pos["ticker"] in bars:
                close(pos, bars[pos["ticker"]].open, d, pos["exit_reason"])
        for o in pending:
            if o["ticker"] not in bars:
                continue
            price = bars[o["ticker"]].open * (1 + cost.slippage)
            qty = position_shares(equity, cash / (1 + cost.commission), price, hist.lots[o["ticker"]], rk, o["factor"])
            if qty <= 0:
                continue
            cash -= qty * price * (1 + cost.commission)
            positions.append({**o, "qty": qty, "entry_price": price, "entry_date": d,
                              "stop": price * (1 - rk.stop_pct),
                              "target": price * (1 + rk.take_profit_pct) if rk.take_profit_pct > 0 else None})
        pending = []

        # 2. Protective stop and profit target inside the day; a gap through the level fills at the open.
        for pos in positions[:]:
            bar = bars.get(pos["ticker"])
            if bar is None:
                continue
            if bar.low <= pos["stop"]:
                close(pos, min(pos["stop"], bar.open), d, "stop")
            elif pos["target"] and bar.high >= pos["target"]:
                close(pos, max(pos["target"], bar.open), d, "target")

        # 3. End of day: mark to market, then decide tomorrow's exits and entries.
        for t, bar in bars.items():
            last_close[t] = bar.close
        equity = cash + sum(p["qty"] * last_close[p["ticker"]] for p in positions)
        curve[d] = equity

        for pos in positions:
            snap = pre[pos["key"]].get(d)
            if snap is None:
                continue
            if snap.z >= st.exit_z:
                pos["exit_reason"] = "mean_reversion"
            elif pair_broken(snap, st):
                pos["exit_reason"] = "pair_breakdown"
            elif rk.max_holding_days and (d - pos["entry_date"]).days >= rk.max_holding_days:
                pos["exit_reason"] = "time_stop"

        mstate = market.get(d, "weak")
        if mstate == "weak" or risk.can_open(d.date(), equity, rk):
            continue
        # No re-entry into a stock on the day it was closed.
        held = {p["ticker"] for p in positions} | {p["other"] for p in positions} | closed_today
        candidates = []
        for key, table in pre.items():
            buy, other, sector = key
            snap = table.get(d)
            if snap is None or buy in held or buy not in bars or statistical_screen(snap, st):
                continue
            score = tqs_score(snap, mstate, daily_entry_points(hist.bars[buy], d, snap), st)
            factor = size_factor(score, cfg.tqs)
            if factor > 0:
                candidates.append({"key": key, "ticker": buy, "other": other, "sector": sector,
                                   "pair": f"{buy}/{other}", "z_entry": snap.z, "tqs": score, "factor": factor})
        planned = {}
        for c in sorted(candidates, key=lambda c: -c["tqs"]):  # strictly in descending TQS order
            if c["ticker"] in held or c["other"] in held:
                continue
            new_risk = rk.risk_per_trade * c["factor"]
            used = sector_risk(positions, c["sector"], equity) + planned.get(c["sector"], 0.0)
            if used + new_risk > rk.max_sector_risk + 1e-9:
                continue
            planned[c["sector"]] = planned.get(c["sector"], 0.0) + new_risk
            held |= {c["ticker"], c["other"]}
            pending.append(c)

    equity_curve = pd.Series(curve, dtype=float)
    frame = pd.DataFrame(trades)
    return Result(frame, equity_curve, _metrics(frame, equity_curve, capital))


def run_backtest(cfg, provider, start: date | None = None, end: date | None = None) -> Result:
    end = end or now_msk().date()
    start = start or end - timedelta(days=5 * 365)
    # Extra history in front of the test window warms up the 250-day statistics and EMA200.
    hist = load_history(cfg, provider, start - timedelta(days=600), end)
    short = [t for t, df in hist.bars.items() if len(df) < required_history(cfg.stats)]
    if short:
        print(f"warning: not enough history for {', '.join(short)}; their pairs will never trade")
    return simulate(cfg, hist, precompute(cfg, hist), pd.Timestamp(start), pd.Timestamp(end))


def _score(m: dict) -> float:
    if m["trades"] < 3:
        return float("-inf")
    return m["net_profit"] / max(m["max_drawdown"], 1.0)


def walk_forward(cfg, provider, start: date | None = None, end: date | None = None) -> list:
    """Tune entry_z / exit_z / stop_pct on each training window, then test on the next unseen window."""
    wf = cfg.walkforward
    end = end or now_msk().date()
    start = start or end - timedelta(days=6 * 365)
    hist = load_history(cfg, provider, start - timedelta(days=600), end)
    pre = precompute(cfg, hist)
    days = hist.index.loc[pd.Timestamp(start):pd.Timestamp(end)].index
    folds, i = [], 0
    while i + wf.train_days + wf.test_days <= len(days):
        train = (days[i], days[i + wf.train_days - 1])
        test = (days[i + wf.train_days], days[i + wf.train_days + wf.test_days - 1])
        best, best_score = None, float("-inf")
        for ez, xz, sp in product(wf.entry_z, wf.exit_z, wf.stop_pct):
            c = copy.deepcopy(cfg)
            c.stats.entry_z, c.stats.exit_z, c.risk.stop_pct = ez, xz, sp
            score = _score(simulate(c, hist, pre, *train).metrics)
            if score > best_score:
                best, best_score = c, score
        chosen = best or cfg  # no parameter set traded enough: keep the configured values
        folds.append({"train": f"{train[0].date()}..{train[1].date()}", "test": f"{test[0].date()}..{test[1].date()}",
                      "entry_z": chosen.stats.entry_z, "exit_z": chosen.stats.exit_z,
                      "stop_pct": chosen.risk.stop_pct, "tuned": best is not None,
                      **{k: v for k, v in simulate(chosen, hist, pre, *test).metrics.items()
                         if k in ("trades", "net_profit", "return_pct", "max_drawdown_pct", "win_rate_pct")}})
        i += wf.test_days
    return folds
