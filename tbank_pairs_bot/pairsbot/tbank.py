"""Minimal T-Bank Invest API client over its REST gateway (https://developer.tbank.ru/invest)."""
from __future__ import annotations

import os
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
    def __init__(self, message: str, status: int = 0, grpc_code: int = 0, api_code: str = ""):
        super().__init__(message)
        self.status, self.grpc_code, self.api_code = status, grpc_code, str(api_code)

    @property
    def not_found(self) -> bool:
        """The broker does not know the requested object (gRPC NOT_FOUND / 50005 order not found)."""
        return self.status == 404 or self.grpc_code == 5 or self.api_code == "50005"


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


def _ca_bundle() -> str | bool:
    """T-Bank hosts use the Russian Trusted Root CA, which certifi does not ship: append it."""
    certs = Path(__file__).resolve().parent.parent / "certs"
    extra = certs / "russian_trusted_root_ca.pem"
    if not extra.exists():
        return True
    import certifi

    out = certs / "_bundle.pem"
    base = Path(certifi.where())
    if not out.exists() or out.stat().st_mtime < max(base.stat().st_mtime, extra.stat().st_mtime):
        tmp = certs / f"_bundle.{os.getpid()}.tmp"
        tmp.write_text(base.read_text(encoding="utf-8") + "\n" + extra.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            os.replace(tmp, out)
        except OSError:  # another process is writing the same bundle
            tmp.unlink(missing_ok=True)
    return str(out) if out.exists() else True


class TBankClient:
    def __init__(self, token: str, sandbox: bool = False, base_url: str | None = None):
        # TBANK_API_URL points the client at another server; the tests use it for a local fake broker.
        self.base = base_url or os.environ.get("TBANK_API_URL") or (SANDBOX_URL if sandbox else PROD_URL)
        self.sandbox = sandbox
        self.http = requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self.http.verify = _ca_bundle()

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
                err = {}
                try:
                    err = r.json() if isinstance(r.json(), dict) else {}
                    msg = f"{err.get('message')} ({err.get('description')})"
                except ValueError:
                    msg = r.text[:200]
                code = err.get("code")
                raise TBankError(f"{method} failed, HTTP {r.status_code}: {msg}", r.status_code,
                                 code if isinstance(code, int) else 0, err.get("description") or "")
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
            found = self.call("SandboxService", "GetSandboxAccounts").get("accounts", [])
        else:
            found = self.call("UsersService", "GetAccounts").get("accounts", [])
        return [a for a in found if a.get("status", "ACCOUNT_STATUS_OPEN") == "ACCOUNT_STATUS_OPEN"]

    def open_sandbox_account(self, rub: float) -> str:
        acc = self.call("SandboxService", "OpenSandboxAccount")["accountId"]
        self.call("SandboxService", "SandboxPayIn", {"accountId": acc, "amount": {"currency": "rub", **f2q(rub)}})
        return acc

    def portfolio(self, account_id: str) -> dict:
        p = self.call("OperationsService", "GetPortfolio", {"accountId": account_id, "currency": "RUB"})
        return {"equity": q2f(p.get("totalAmountPortfolio")), "cash": q2f(p.get("totalAmountCurrencies")),
                "positions": {x["instrumentUid"]: q2f(x.get("quantity")) for x in p.get("positions", [])}}

    # ---- orders ----------------------------------------------------------------------
    def post_order(self, account_id: str, uid: str, lots: int, buy: bool, price: float | None,
                   key: str | None = None) -> str:
        """key is the idempotency key: sending the same key again never creates a second order."""
        body = {"instrumentId": uid, "quantity": str(lots), "accountId": account_id,
                "direction": "ORDER_DIRECTION_BUY" if buy else "ORDER_DIRECTION_SELL",
                "orderType": "ORDER_TYPE_LIMIT" if price is not None else "ORDER_TYPE_MARKET",
                "orderId": key or str(uuid.uuid4())}
        if price is not None:
            body["price"] = f2q(price)
        return self.call("OrdersService", "PostOrder", body)["orderId"]

    def order_state(self, account_id: str, order_id: str, by_request: bool = False) -> dict:
        """by_request looks the order up by the idempotency key it was sent with."""
        body = {"accountId": account_id, "orderId": order_id}
        if by_request:
            body["orderIdType"] = "ORDER_ID_TYPE_REQUEST"
        s = self.call("OrdersService", "GetOrderState", body)
        return {"status": s.get("executionReportStatus", ""), "lots": int(s.get("lotsExecuted", 0)),
                "order_id": s.get("orderId", ""), "avg_price": q2f(s.get("averagePositionPrice")),
                "executed_total": q2f(s.get("executedOrderPrice")),
                "commission": q2f(s.get("executedCommission"))}

    def trading_status(self, uid: str) -> dict:
        s = self.call("MarketDataService", "GetTradingStatus", {"instrumentId": uid})
        return {"status": s.get("tradingStatus", ""), "limit": bool(s.get("limitOrderAvailableFlag")),
                "market": bool(s.get("marketOrderAvailableFlag")), "api": bool(s.get("apiTradeAvailableFlag"))}

    def cancel_order(self, account_id: str, order_id: str) -> None:
        self.call("OrdersService", "CancelOrder", {"accountId": account_id, "orderId": order_id})

    def post_stop_loss(self, account_id: str, uid: str, lots: int, stop_price: float, key: str | None = None) -> str:
        """key is the idempotency key: repeating the call with it does not create a second stop order."""
        return self.call("StopOrdersService", "PostStopOrder", {
            "instrumentId": uid, "quantity": str(lots), "stopPrice": f2q(stop_price), "accountId": account_id,
            "direction": "STOP_ORDER_DIRECTION_SELL", "stopOrderType": "STOP_ORDER_TYPE_STOP_LOSS",
            "orderId": key or str(uuid.uuid4()),
            "expirationType": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL"})["stopOrderId"]

    def active_stops(self, account_id: str) -> set:
        found = self.call("StopOrdersService", "GetStopOrders",
                          {"accountId": account_id, "status": "STOP_ORDER_STATUS_ACTIVE"})
        return {o["stopOrderId"] for o in found.get("stopOrders", [])}

    def cancel_stop(self, account_id: str, stop_id: str) -> None:
        self.call("StopOrdersService", "CancelStopOrder", {"accountId": account_id, "stopOrderId": stop_id})
