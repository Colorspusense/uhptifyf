"""Minimal T-Bank Invest API client over its REST gateway (https://developer.tbank.ru/invest)."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests

PROD_URL = "https://invest-public-api.tbank.ru/rest"
SANDBOX_URL = "https://sandbox-invest-public-api.tbank.ru/rest"
_PREFIX = "tinkoff.public.invest.api.contract.v1."
_INTERVALS = {"1d": ("CANDLE_INTERVAL_DAY", 2000), "15m": ("CANDLE_INTERVAL_15_MIN", 20)}  # name, days per request


class TBankError(RuntimeError):
    pass


def q2f(q: dict | None) -> float:
    """Quotation / MoneyValue {units, nano} -> float."""
    if not q:
        return 0.0
    return int(q.get("units", 0)) + int(q.get("nano", 0)) / 1e9


def f2q(x: float) -> dict:
    d = Decimal(str(x))
    units = int(d)
    return {"units": str(units), "nano": int((d - units) * Decimal(10 ** 9))}


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ca_bundle(root: Path) -> str | bool:
    """T-Bank hosts use the Russian Trusted Root CA, which certifi does not ship: append it."""
    extra = root / "certs" / "russian_trusted_root_ca.pem"
    if not extra.exists():
        return True
    import certifi

    out = root / "certs" / "_bundle.pem"
    base = Path(certifi.where())
    if not out.exists() or out.stat().st_mtime < max(base.stat().st_mtime, extra.stat().st_mtime):
        out.write_text(base.read_text(encoding="utf-8") + "\n" + extra.read_text(encoding="utf-8"), encoding="utf-8")
    return str(out)


class TBankClient:
    def __init__(self, token: str, sandbox: bool = False, root: Path | None = None):
        self.base = SANDBOX_URL if sandbox else PROD_URL
        self.sandbox = sandbox
        self.http = requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self.http.verify = _ca_bundle(root or Path(__file__).resolve().parent.parent)

    def call(self, service: str, method: str, body: dict | None = None) -> dict:
        url = f"{self.base}/{_PREFIX}{service}/{method}"
        for attempt in range(5):
            try:
                r = self.http.post(url, json=body or {}, timeout=30)
            except requests.RequestException as e:
                if attempt == 4:
                    raise TBankError(f"{method}: {e}") from e
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("x-ratelimit-reset", 2 * (attempt + 1))))
                continue
            if r.status_code != 200:
                try:
                    err = r.json()
                    msg = f"{err.get('message')} ({err.get('description')})"
                except ValueError:
                    msg = r.text[:200]
                raise TBankError(f"{method} failed, HTTP {r.status_code}: {msg}")
            return r.json()
        raise TBankError(f"{method}: no successful response after retries")

    # ---- instruments and market data -------------------------------------------------
    def share(self, ticker: str, class_code: str = "TQBR") -> dict:
        i = self.call("InstrumentsService", "ShareBy",
                      {"idType": "INSTRUMENT_ID_TYPE_TICKER", "classCode": class_code, "id": ticker})["instrument"]
        return {"ticker": ticker, "uid": i["uid"], "lot": int(i["lot"]),
                "step": q2f(i.get("minPriceIncrement")) or 0.01, "sector": i.get("sector", "")}

    def find_index(self, ticker: str) -> dict:
        found = self.call("InstrumentsService", "FindInstrument",
                          {"query": ticker, "instrumentKind": "INSTRUMENT_TYPE_INDEX"}).get("instruments", [])
        for i in found:
            if i.get("ticker") == ticker:
                return {"ticker": ticker, "uid": i["uid"], "lot": 1, "step": 0.01, "sector": "index"}
        raise TBankError(f"index {ticker} not found")

    def candles(self, uid: str, interval: str, start: datetime, end: datetime) -> list:
        """Rows of (time_utc, open, high, low, close, volume, is_complete)."""
        name, chunk_days = _INTERVALS[interval]
        rows, cur = {}, start
        while cur < end:
            nxt = min(cur + timedelta(days=chunk_days), end)
            data = self.call("MarketDataService", "GetCandles",
                             {"instrumentId": uid, "from": _ts(cur), "to": _ts(nxt), "interval": name})
            for c in data.get("candles", []):
                t = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
                rows[t] = (t, q2f(c["open"]), q2f(c["high"]), q2f(c["low"]), q2f(c["close"]),
                           int(c.get("volume", 0)), bool(c.get("isComplete", True)))
            cur = nxt
        return [rows[k] for k in sorted(rows)]

    def last_prices(self, uids: list) -> dict:
        data = self.call("MarketDataService", "GetLastPrices", {"instrumentId": list(uids)})
        return {p["instrumentUid"]: q2f(p.get("price")) for p in data.get("lastPrices", [])}

    def order_book(self, uid: str, depth: int = 1) -> tuple | None:
        ob = self.call("MarketDataService", "GetOrderBook", {"instrumentId": uid, "depth": depth})
        if not ob.get("bids") or not ob.get("asks"):
            return None
        return q2f(ob["bids"][0]["price"]), q2f(ob["asks"][0]["price"])

    # ---- accounts --------------------------------------------------------------------
    def accounts(self) -> list:
        if self.sandbox:
            return self.call("SandboxService", "GetSandboxAccounts").get("accounts", [])
        return self.call("UsersService", "GetAccounts").get("accounts", [])

    def open_sandbox_account(self, rub: float) -> str:
        acc = self.call("SandboxService", "OpenSandboxAccount")["accountId"]
        self.call("SandboxService", "SandboxPayIn", {"accountId": acc, "amount": {"currency": "rub", **f2q(rub)}})
        return acc

    def portfolio(self, account_id: str) -> dict:
        p = self.call("OperationsService", "GetPortfolio", {"accountId": account_id, "currency": "RUB"})
        return {"equity": q2f(p.get("totalAmountPortfolio")), "cash": q2f(p.get("totalAmountCurrencies")),
                "positions": {x["instrumentUid"]: q2f(x.get("quantity")) for x in p.get("positions", [])}}

    # ---- orders ----------------------------------------------------------------------
    def post_order(self, account_id: str, uid: str, lots: int, buy: bool, price: float | None) -> str:
        body = {"instrumentId": uid, "quantity": str(lots), "accountId": account_id,
                "direction": "ORDER_DIRECTION_BUY" if buy else "ORDER_DIRECTION_SELL",
                "orderType": "ORDER_TYPE_LIMIT" if price is not None else "ORDER_TYPE_MARKET",
                "orderId": str(uuid.uuid4())}
        if price is not None:
            body["price"] = f2q(price)
        return self.call("OrdersService", "PostOrder", body)["orderId"]

    def order_state(self, account_id: str, order_id: str) -> dict:
        s = self.call("OrdersService", "GetOrderState", {"accountId": account_id, "orderId": order_id})
        return {"status": s.get("executionReportStatus", ""), "lots": int(s.get("lotsExecuted", 0)),
                "avg_price": q2f(s.get("averagePositionPrice")), "commission": q2f(s.get("executedCommission"))}

    def cancel_order(self, account_id: str, order_id: str) -> None:
        self.call("OrdersService", "CancelOrder", {"accountId": account_id, "orderId": order_id})

    def post_stop_loss(self, account_id: str, uid: str, lots: int, stop_price: float) -> str:
        return self.call("StopOrdersService", "PostStopOrder", {
            "instrumentId": uid, "quantity": str(lots), "stopPrice": f2q(stop_price), "accountId": account_id,
            "direction": "STOP_ORDER_DIRECTION_SELL", "stopOrderType": "STOP_ORDER_TYPE_STOP_LOSS",
            "expirationType": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL"})["stopOrderId"]

    def cancel_stop(self, account_id: str, stop_id: str) -> None:
        self.call("StopOrdersService", "CancelStopOrder", {"accountId": account_id, "stopOrderId": stop_id})
