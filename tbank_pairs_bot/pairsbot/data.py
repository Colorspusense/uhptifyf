"""Market data providers (T-Bank API, public MOEX ISS, synthetic) with a CSV cache for daily candles.

All candle frames are indexed by naive Moscow time and have open/high/low/close/volume columns.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from .tbank import TBankClient

MSK = timezone(timedelta(hours=3))
COLS = ["open", "high", "low", "close", "volume"]


def now_msk() -> datetime:
    return datetime.now(MSK).replace(tzinfo=None)


def _frame(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["time"] + COLS).set_index("time")
    return df[~df.index.duplicated(keep="last")].sort_index()


class TBankData:
    name = "tbank"

    def __init__(self, client: TBankClient, index_ticker: str):
        self.client, self.index_ticker, self._inst = client, index_ticker, {}

    def instrument(self, ticker: str) -> dict:
        if ticker not in self._inst:
            find = self.client.find_index if ticker == self.index_ticker else self.client.share
            self._inst[ticker] = find(ticker)
        return self._inst[ticker]

    def candles(self, ticker: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        raw = self.client.candles(self.instrument(ticker)["uid"], interval,
                                  start.replace(tzinfo=MSK), end.replace(tzinfo=MSK))
        rows = []
        for t, o, h, l, c, v, complete in raw:
            t = t.astimezone(MSK).replace(tzinfo=None)
            if interval == "1d":
                t = t.replace(hour=0, minute=0, second=0)
            elif not complete:
                continue
            rows.append((t, o, h, l, c, v))
        return _frame(rows)

    def last_prices(self, tickers: list) -> dict:
        by_uid = {self.instrument(t)["uid"]: t for t in tickers}
        return {by_uid[u]: p for u, p in self.client.last_prices(list(by_uid)).items() if u in by_uid and p > 0}

    def order_book(self, ticker: str) -> tuple | None:
        return self.client.order_book(self.instrument(ticker)["uid"])


class MoexData:
    """Public MOEX ISS feed: needs no token, but intraday quotes are delayed by about 15 minutes."""
    name = "moex"
    BASE = "https://iss.moex.com/iss/engines/stock/markets"

    def __init__(self, index_ticker: str):
        self.index_ticker, self._inst, self.http = index_ticker, {}, requests.Session()

    def _path(self, ticker: str) -> str:
        market = "index/boards/SNDX" if ticker == self.index_ticker else "shares/boards/TQBR"
        return f"{self.BASE}/{market}/securities/{ticker}"

    def _get(self, url: str, params: dict) -> dict:
        for attempt in range(4):
            try:
                r = self.http.get(url, params={"iss.meta": "off", **params}, timeout=30)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError):
                if attempt == 3:
                    raise
        return {}

    def _board(self, ticker: str) -> dict:
        d = self._get(self._path(ticker) + ".json", {"iss.only": "marketdata,securities"})
        out = {}
        for block in ("securities", "marketdata"):
            if d[block]["data"]:
                out.update(dict(zip(d[block]["columns"], d[block]["data"][0])))
        return out

    def instrument(self, ticker: str) -> dict:
        if ticker not in self._inst:
            b = {} if ticker == self.index_ticker else self._board(ticker)
            self._inst[ticker] = {"ticker": ticker, "uid": ticker, "lot": int(b.get("LOTSIZE") or 1),
                                  "step": float(b.get("MINSTEP") or 0.01), "sector": ""}
        return self._inst[ticker]

    def candles(self, ticker: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        rows, offset = [], 0
        while True:
            d = self._get(self._path(ticker) + "/candles.json",
                          {"from": start.strftime("%Y-%m-%d"), "till": end.strftime("%Y-%m-%d"),
                           "interval": 24 if interval == "1d" else 1, "start": offset})["candles"]
            col = {c: i for i, c in enumerate(d["columns"])}
            for r in d["data"]:
                rows.append((datetime.fromisoformat(r[col["begin"]]), r[col["open"]], r[col["high"]],
                             r[col["low"]], r[col["close"]], r[col["volume"]]))
            offset += len(d["data"])
            if len(d["data"]) < 500:
                break
        df = _frame(rows)
        if interval == "1d" or df.empty:
            return df
        # ISS has no 15-minute candles: build them from 1-minute ones and drop the unfinished last bar.
        agg = df.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                        "volume": "sum"}).dropna()
        return agg.iloc[:-1]

    def last_prices(self, tickers: list) -> dict:
        out = {}
        for t in tickers:
            b = self._board(t)
            price = b.get("LAST") or b.get("CURRENTVALUE") or b.get("LCURRENTPRICE")
            if price:
                out[t] = float(price)
        return out

    def order_book(self, ticker: str) -> tuple | None:
        b = self._board(ticker)
        return (float(b["BID"]), float(b["OFFER"])) if b.get("BID") and b.get("OFFER") else None


class SyntheticData:
    """Deterministic cointegrated pairs for an offline demonstration of the mechanics. Not market data."""
    name = "synthetic"

    def __init__(self, pairs: list, index_ticker: str, days: int = 1500, seed: int = 7):
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range(end=pd.Timestamp(now_msk().date()) - pd.Timedelta(days=1), periods=days)
        self.index_ticker, self._data = index_ticker, {}
        logs = {}
        for p in pairs:
            spread = np.zeros(days)
            shocks = rng.normal(0, 0.005, days)
            for i in range(1, days):
                spread[i] = 0.93 * spread[i - 1] + shocks[i]
            beta = rng.uniform(0.8, 1.2)
            if p.b not in logs and p.a in logs:
                logs[p.b] = (logs[p.a] - spread) / beta
            else:
                logs.setdefault(p.b, np.log(rng.uniform(50, 500)) + np.cumsum(rng.normal(0.0003, 0.016, days)))
                logs.setdefault(p.a, beta * logs[p.b] + spread + rng.uniform(-1, 1))
        logs[index_ticker] = np.log(2500) + np.cumsum(rng.normal(0.0006, 0.009, days))
        for t, lg in logs.items():
            close = np.exp(lg)
            open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.002, days))
            span = np.abs(rng.normal(0, 0.006, days))
            self._data[t] = pd.DataFrame({
                "open": open_, "high": np.maximum(open_, close) * (1 + span),
                "low": np.minimum(open_, close) * (1 - span), "close": close,
                "volume": rng.integers(1_000_000, 5_000_000, days)}, index=dates)

    def instrument(self, ticker: str) -> dict:
        return {"ticker": ticker, "uid": ticker, "lot": 1, "step": 0.01, "sector": ""}

    def candles(self, ticker: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        if interval != "1d":
            raise ValueError("synthetic data is daily only (backtest / walk-forward)")
        return self._data[ticker].loc[start:end]

    def last_prices(self, tickers: list) -> dict:
        return {t: float(self._data[t]["close"].iloc[-1]) for t in tickers}

    def order_book(self, ticker: str) -> tuple | None:
        return None


class DailyCache:
    """Daily candles cached in data/<source>_<ticker>.csv; only the recent tail is re-downloaded."""

    def __init__(self, provider, root, skip_weekends: bool = True):
        self.p, self.skip_weekends = provider, skip_weekends
        self.dir = root / "data"
        self.dir.mkdir(exist_ok=True)

    def daily(self, ticker: str, start: date, end: date | None = None) -> pd.DataFrame:
        end = end or now_msk().date()
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time()).replace(microsecond=0)
        path = self.dir / f"{self.p.name}_{ticker}.csv"
        cached = pd.DataFrame(columns=COLS)
        if path.exists() and self.p.name != "synthetic":
            cached = pd.read_csv(path, index_col=0, parse_dates=True)
        if len(cached) and cached.index[0] <= pd.Timestamp(start_dt) + pd.Timedelta(days=7):
            fresh = self.p.candles(ticker, "1d", cached.index[-1].to_pydatetime() - timedelta(days=5), end_dt)
            df = pd.concat([cached, fresh])
        else:
            df = self.p.candles(ticker, "1d", start_dt, end_dt)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if self.p.name != "synthetic" and len(df):
            df.to_csv(path)
        if self.skip_weekends:
            df = df[df.index.day_of_week < 5]
        return df.loc[start_dt:end_dt]


def make_provider(cfg, token: str | None, source: str | None = None, sandbox: bool = False):
    source = source or cfg.general.data_source
    if source == "auto":
        source = "tbank" if token else "moex"
    if source == "tbank":
        if not token:
            raise SystemExit("data source 'tbank' needs TBANK_TOKEN (environment variable or .env file)")
        return TBankData(TBankClient(token, sandbox=sandbox, root=cfg.root), cfg.general.index_ticker)
    if source == "moex":
        return MoexData(cfg.general.index_ticker)
    if source == "synthetic":
        return SyntheticData(cfg.pairs, cfg.general.index_ticker)
    raise SystemExit(f"unknown data source: {source}")
