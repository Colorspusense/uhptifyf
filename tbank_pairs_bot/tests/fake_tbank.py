"""A small in-process imitation of the T-Invest REST API for tests.

Field names follow the official OpenAPI description. When PAIRSBOT_OPENAPI points to that YAML file,
every request the client sends and every response produced here is also checked against it.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pairsbot.data import SyntheticData

PREFIX = "/tinkoff.public.invest.api.contract.v1."
COMMISSION = 0.0005


def q(x: float) -> dict:
    units = int(x)
    return {"units": str(units), "nano": int(round((x - units) * 1e9))}


def money(x: float) -> dict:
    return {"currency": "rub", **q(x)}


def qf(v: dict) -> float:
    return int(v.get("units", 0)) + int(v.get("nano", 0)) / 1e9


class ApiFault(Exception):
    def __init__(self, status: int, grpc: int, code: int, message: str):
        super().__init__(message)
        self.status, self.body = status, {"code": grpc, "message": message, "description": code}


class Spec:
    """Optional validation against the real OpenAPI file."""

    def __init__(self):
        path = os.environ.get("PAIRSBOT_OPENAPI")
        self.doc = None
        if path and os.path.exists(path):
            import yaml

            with open(path, encoding="utf-8") as f:
                self.doc = yaml.safe_load(f)
        self.errors, self.checked = [], set()

    def _schema(self, ref_holder: dict) -> dict:
        while "$ref" in ref_holder:
            ref_holder = self.doc["components"]["schemas"][ref_holder["$ref"].split("/")[-1]]
        return ref_holder

    def _check(self, value, schema: dict, where: str) -> None:
        schema = self._schema(schema)
        if "enum" in schema:
            if value not in schema["enum"]:
                self.errors.append(f"{where}: {value!r} is not one of {schema['enum'][:6]}")
            return
        kind = schema.get("type")
        if kind == "object" or "properties" in schema:
            if not isinstance(value, dict):
                return self.errors.append(f"{where}: expected an object, got {type(value).__name__}")
            props = schema.get("properties", {})
            for k, v in value.items():
                if k not in props:
                    self.errors.append(f"{where}: unknown field {k!r}")
                else:
                    self._check(v, props[k], f"{where}.{k}")
            for k in schema.get("required", []):
                if k not in value:
                    self.errors.append(f"{where}: missing required field {k!r}")
        elif kind == "array":
            if not isinstance(value, list):
                return self.errors.append(f"{where}: expected a list")
            for i, item in enumerate(value):
                self._check(item, schema.get("items", {}), f"{where}[{i}]")
        elif kind == "string" and not isinstance(value, str):
            self.errors.append(f"{where}: expected a string, got {value!r}")
        elif kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            self.errors.append(f"{where}: expected an integer, got {value!r}")
        elif kind == "boolean" and not isinstance(value, bool):
            self.errors.append(f"{where}: expected a boolean, got {value!r}")

    def check(self, path: str, request: dict, response: dict | None) -> None:
        if not self.doc:
            return
        op = self.doc["paths"].get(path, {}).get("post")
        if not op:
            return self.errors.append(f"{path}: no such method in the API")
        self.checked.add(path)
        self._check(request, op["requestBody"]["content"]["application/json"]["schema"], f"{path} request")
        if response is not None:
            self._check(response, op["responses"]["200"]["content"]["application/json"]["schema"], f"{path} response")


class FakeTBank:
    def __init__(self, cfg, cash: float = 1_000_000.0, lots: dict | None = None):
        self.synth = SyntheticData(cfg.pairs, cfg.general.index_ticker)
        self.index = cfg.general.index_ticker
        self.lots = lots or {}
        self.cash, self.positions = cash, {}        # positions: uid -> shares
        self.orders, self.by_request, self.stops, self.stop_keys = {}, {}, {}, {}
        self.fault = {}                             # method -> ApiFault to raise (once per entry in a list)
        self.sell_fill = None                       # None = whole order, or a number of lots to sell
        self.price = {}                             # ticker -> overridden last price
        self.fill = "fill"                          # fill | none | partial | reject
        self.drop = {}                              # method -> number of responses to lose
        self.trading = True
        self.accounts = [{"id": "acc-1", "name": "Test", "type": "ACCOUNT_TYPE_TINKOFF",
                          "status": "ACCOUNT_STATUS_OPEN", "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"}]
        self.calls, self.spec, self.lock = [], Spec(), threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                path = self.path.removeprefix("/rest")
                method = path.rsplit("/", 1)[-1]
                with fake.lock:
                    fake.calls.append((method, body))
                    try:
                        if self.headers.get("Authorization") != "Bearer good-token":
                            raise ApiFault(401, 16, 40003, "Authentication token is missing or invalid")
                        if fake.fault.get(method):
                            raise fake.fault[method].pop(0)
                        out, status = getattr(fake, "m_" + method)(body), 200
                        fake.spec.check(path, body, out)
                    except ApiFault as e:
                        fake.spec.check(path, body, None)
                        out, status = e.body, e.status
                    if fake.drop.get(method, 0) > 0:  # the request was processed but the answer is lost
                        fake.drop[method] -= 1
                        self.connection.close()
                        return
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/rest"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)

    # ---- helpers ---------------------------------------------------------------------
    def ticker(self, uid: str) -> str:
        if not uid.startswith("uid-"):
            raise ApiFault(404, 5, 50002, "Instrument not found")
        return uid[4:]

    def last(self, ticker: str) -> float:
        return self.price.get(ticker) or round(float(self.synth._data[ticker]["close"].iloc[-1]), 2)

    def lot(self, ticker: str) -> int:
        return self.lots.get(ticker, 1)

    def trigger_stops(self) -> None:
        """Execute every stop order whose level has been reached, like the broker would."""
        with self.lock:
            for sid, s in list(self.stops.items()):
                t = self.ticker(s["uid"])
                if s["active"] and self.last(t) <= s["stop"]:
                    shares = min(s["lots"] * self.lot(t), self.positions.get(s["uid"], 0))
                    self.positions[s["uid"]] = self.positions.get(s["uid"], 0) - shares
                    self.cash += shares * self.last(t) * (1 - COMMISSION)
                    s["active"] = False

    # ---- users / sandbox ---------------------------------------------------------------
    def m_GetAccounts(self, b):
        return {"accounts": self.accounts}

    m_GetSandboxAccounts = m_GetAccounts

    def m_OpenSandboxAccount(self, b):
        self.accounts.append({"id": "sandbox-1", "name": b.get("name", ""), "type": "ACCOUNT_TYPE_TINKOFF",
                              "status": "ACCOUNT_STATUS_OPEN", "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"})
        self.cash = 0.0
        return {"accountId": "sandbox-1"}

    def m_SandboxPayIn(self, b):
        self.cash += qf(b["amount"])
        return {"balance": money(self.cash)}

    # ---- instruments and market data -----------------------------------------------------
    def m_ShareBy(self, b):
        if b.get("idType") != "INSTRUMENT_ID_TYPE_TICKER" or b.get("classCode") != "TQBR" \
                or b["id"] not in self.synth._data:
            raise ApiFault(404, 5, 50002, "Instrument not found")
        t = b["id"]
        return {"instrument": {"uid": "uid-" + t, "figi": "FIGI" + t, "ticker": t, "classCode": "TQBR",
                               "lot": self.lot(t), "minPriceIncrement": q(0.01), "sector": "other",
                               "apiTradeAvailableFlag": True, "buyAvailableFlag": True}}

    def m_FindInstrument(self, b):
        hits = [t for t in self.synth._data if b["query"] in t]
        return {"instruments": [{"uid": "uid-" + t, "ticker": t, "classCode": "SPBXM", "lot": 1,
                                 "instrumentKind": "INSTRUMENT_TYPE_INDEX" if t == self.index
                                 else "INSTRUMENT_TYPE_SHARE", "name": t} for t in hits]}

    def m_GetCandles(self, b):
        t = self.ticker(b["instrumentId"])
        start = datetime.fromisoformat(b["from"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(b["to"].replace("Z", "+00:00"))
        out = []
        if b["interval"] == "CANDLE_INTERVAL_DAY":
            if end - start > timedelta(days=366 * 6 + 5):
                raise ApiFault(400, 3, 30014, "The maximum request period for the given candle interval has been exceeded")
            for day, r in self.synth._data[t].iterrows():
                ts = day.to_pydatetime().replace(hour=4, tzinfo=timezone.utc)
                if start <= ts <= end:
                    out.append({"time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": q(r.open), "high": q(r.high),
                                "low": q(r.low), "close": q(r.close), "volume": str(int(r.volume)),
                                "isComplete": True})
        elif b["interval"] == "CANDLE_INTERVAL_15_MIN":
            if end - start > timedelta(days=21, hours=1):
                raise ApiFault(400, 3, 30014, "The maximum request period for the given candle interval has been exceeded")
            price, ts = self.last(t) * 0.97, start.replace(minute=0, second=0, microsecond=0)
            bars = []
            while ts < end:
                if ts.weekday() < 5 and 7 <= ts.hour < 16:
                    bars.append(ts)
                ts += timedelta(minutes=15)
            step = (self.last(t) - price) / max(len(bars), 1)
            for i, ts in enumerate(bars):  # a steady rise that ends with a small pullback and a recovery
                o = price + step * i
                c = o + step
                tail = len(bars) - i
                if 2 <= tail <= 4:
                    o, c = c, o - step * 0.3
                elif tail == 1:
                    o, c = self.last(t) - 3 * step, self.last(t)
                out.append({"time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": q(o), "high": q(max(o, c) * 1.0005),
                            "low": q(min(o, c) * 0.9995), "close": q(c),
                            "volume": str(500 if 2 <= tail <= 4 else 1500), "isComplete": True})
        else:
            raise ApiFault(400, 3, 30011, "interval is invalid")
        return {"candles": out}

    def m_GetLastPrices(self, b):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"lastPrices": [{"instrumentUid": u, "price": q(self.last(self.ticker(u))), "time": now}
                               for u in b["instrumentId"]]}

    def m_GetOrderBook(self, b):
        p = self.last(self.ticker(b["instrumentId"]))
        return {"instrumentUid": b["instrumentId"], "depth": b["depth"], "lastPrice": q(p),
                "bids": [{"price": q(round(p - 0.01, 2)), "quantity": "100"}],
                "asks": [{"price": q(p), "quantity": "100"}]}

    def m_GetTradingStatus(self, b):
        self.ticker(b["instrumentId"])
        ok = self.trading
        return {"instrumentUid": b["instrumentId"], "limitOrderAvailableFlag": ok, "marketOrderAvailableFlag": ok,
                "apiTradeAvailableFlag": True, "tradingStatus": "SECURITY_TRADING_STATUS_NORMAL_TRADING" if ok
                else "SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING"}

    # ---- portfolio and orders --------------------------------------------------------------
    def m_GetPortfolio(self, b):
        if b["accountId"] not in {a["id"] for a in self.accounts}:
            raise ApiFault(404, 5, 50004, "Account not found")
        held = sum(n * self.last(self.ticker(u)) for u, n in self.positions.items())
        return {"accountId": b["accountId"], "totalAmountPortfolio": money(self.cash + held),
                "totalAmountCurrencies": money(self.cash), "totalAmountShares": money(held),
                "positions": [{"instrumentUid": u, "instrumentType": "share", "quantity": q(n),
                               "quantityLots": q(n / self.lot(self.ticker(u))),
                               "currentPrice": money(self.last(self.ticker(u)))}
                              for u, n in self.positions.items() if n]}

    def _order_view(self, o: dict) -> dict:
        return {"orderId": o["id"], "orderRequestId": o["request"], "executionReportStatus": o["status"],
                "lotsRequested": str(o["lots"]), "lotsExecuted": str(o["done"]), "instrumentUid": o["uid"],
                "direction": o["direction"], "orderType": o["type"],
                "averagePositionPrice": money(o["price"] if o["done"] else 0.0),
                "executedOrderPrice": money(o["price"] * o["done"] * o["lot"]),
                "executedCommission": money(o["price"] * o["done"] * o["lot"] * COMMISSION)}

    def m_PostOrder(self, b):
        key = b["orderId"]
        if len(key) > 36:
            raise ApiFault(400, 3, 30057, "orderId is too long")
        if key in self.by_request:  # idempotency: the same key never creates a second order
            return self._post_view(self.orders[self.by_request[key]])
        t = self.ticker(b["instrumentId"])
        lots, lot, buy = int(b["quantity"]), self.lot(t), b["direction"] == "ORDER_DIRECTION_BUY"
        if lots <= 0:
            raise ApiFault(400, 3, 30051, "quantity is invalid")
        market = b["orderType"] == "ORDER_TYPE_MARKET"
        if not market and "price" not in b:
            raise ApiFault(400, 3, 30016, "price is required for a limit order")
        last = self.last(t)
        if not market and abs(qf(b["price"]) / 0.01 - round(qf(b["price"]) / 0.01)) > 1e-6:
            raise ApiFault(400, 3, 30018, "price is not a multiple of the minimum price increment")
        o = {"id": str(len(self.orders) + 1000), "request": key, "uid": b["instrumentId"], "lots": lots, "lot": lot,
             "done": 0, "status": "EXECUTION_REPORT_STATUS_NEW", "direction": b["direction"], "type": b["orderType"],
             "price": last}
        self.orders[o["id"]] = o
        self.by_request[key] = o["id"]
        if not self.trading or self.fill == "reject":
            o["status"] = "EXECUTION_REPORT_STATUS_REJECTED"
        elif buy:
            if self.fill != "none" and (market or qf(b["price"]) >= last):
                done = lots if self.fill == "fill" else max(lots // 2, 1)
                cost = done * lot * last * (1 + COMMISSION)
                if cost > self.cash:
                    o["status"] = "EXECUTION_REPORT_STATUS_REJECTED"
                else:
                    self.cash -= cost
                    self.positions[o["uid"]] = self.positions.get(o["uid"], 0) + done * lot
                    o["done"] = done
                    o["status"] = "EXECUTION_REPORT_STATUS_FILL" if done == lots \
                        else "EXECUTION_REPORT_STATUS_PARTIALLYFILL"
        else:
            if lots * lot > self.positions.get(o["uid"], 0):
                o["status"] = "EXECUTION_REPORT_STATUS_REJECTED"  # no short selling
            elif self.fill != "none":
                done = lots if self.sell_fill is None else min(self.sell_fill, lots)
                self.positions[o["uid"]] -= done * lot
                self.cash += done * lot * last * (1 - COMMISSION)
                o["done"] = done
                o["status"] = "EXECUTION_REPORT_STATUS_FILL" if done == lots else "EXECUTION_REPORT_STATUS_PARTIALLYFILL"
        return self._post_view(o)

    def _post_view(self, o: dict) -> dict:
        view = self._order_view(o)
        del view["averagePositionPrice"]  # PostOrder's answer has no such field
        return view

    def m_GetOrderState(self, b):
        oid = b["orderId"]
        if b.get("orderIdType") == "ORDER_ID_TYPE_REQUEST":
            oid = self.by_request.get(oid)
        if oid not in self.orders:
            raise ApiFault(404, 5, 50005, "Order not found")
        return self._order_view(self.orders[oid])

    def m_CancelOrder(self, b):
        o = self.orders.get(b["orderId"])
        if not o:
            raise ApiFault(404, 5, 50005, "Order not found")
        if o["status"].endswith(("_NEW", "_PARTIALLYFILL")):
            o["status"] = "EXECUTION_REPORT_STATUS_CANCELLED"
        return {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    def m_PostStopOrder(self, b):
        if b["stopOrderType"] != "STOP_ORDER_TYPE_STOP_LOSS" or b["direction"] != "STOP_ORDER_DIRECTION_SELL":
            raise ApiFault(400, 3, 30052, "unexpected stop order")
        t = self.ticker(b["instrumentId"])
        if int(b["quantity"]) * self.lot(t) > self.positions.get(b["instrumentId"], 0):
            raise ApiFault(400, 3, 30042, "not enough assets for the stop order")
        if b["orderId"] in self.stop_keys:  # idempotency
            return {"stopOrderId": self.stop_keys[b["orderId"]]}
        sid = str(uuid.uuid4())
        self.stop_keys[b["orderId"]] = sid
        self.stops[sid] = {"uid": b["instrumentId"], "lots": int(b["quantity"]), "stop": qf(b["stopPrice"]),
                           "active": True}
        return {"stopOrderId": sid}

    def m_GetStopOrders(self, b):
        if b.get("status") != "STOP_ORDER_STATUS_ACTIVE":
            raise ApiFault(400, 3, 30052, "this imitation lists active stop orders only")
        return {"stopOrders": [{"stopOrderId": sid, "lotsRequested": str(x["lots"]), "instrumentUid": x["uid"],
                                "direction": "STOP_ORDER_DIRECTION_SELL", "stopPrice": money(x["stop"]),
                                "orderType": "STOP_ORDER_TYPE_STOP_LOSS", "status": "STOP_ORDER_STATUS_ACTIVE"}
                               for sid, x in self.stops.items() if x["active"]]}

    def m_CancelStopOrder(self, b):
        s = self.stops.get(b["stopOrderId"])
        if not s or not s["active"]:
            raise ApiFault(404, 5, 50006, "Stop-order not found")
        s["active"] = False
        return {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
