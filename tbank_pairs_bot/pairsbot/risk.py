"""Money management: position sizing and the portfolio-level limits of chapter 12."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta


def position_shares(equity: float, cash: float, price: float, lot: int, rk, factor: float = 1.0) -> int:
    """Position value = risk in RUB / stop distance; rounded down to whole lots, never leveraged."""
    value = equity * rk.risk_per_trade * factor / rk.stop_pct
    value = min(value, equity * rk.max_position_pct, cash)
    lots = math.floor(value / (price * lot)) if price > 0 else 0
    return max(lots, 0) * lot


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: float = 0.0
    week: str = ""
    week_start_equity: float = 0.0
    consecutive_losses: int = 0
    halt_until: str = ""
    halt_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def new_day(self, d: date, equity: float) -> None:
        """Call once per cycle with the current equity; rolls the day/week anchors."""
        if self.day != d.isoformat():
            self.day, self.day_start_equity = d.isoformat(), equity
        week = (d - timedelta(days=d.weekday())).isoformat()
        if self.week != week:
            self.week, self.week_start_equity = week, equity
        if self.halt_until and d.isoformat() >= self.halt_until:
            self.halt_until = self.halt_reason = ""
            self.consecutive_losses = 0

    def trade_closed(self, pnl: float, d: date, rk) -> None:
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        if self.consecutive_losses >= rk.max_consecutive_losses:
            self._halt(d + timedelta(days=rk.pause_days), f"{self.consecutive_losses} losses in a row")

    def _halt(self, until: date, reason: str) -> None:
        if until.isoformat() > self.halt_until:
            self.halt_until, self.halt_reason = until.isoformat(), reason

    def can_open(self, d: date, equity: float, rk) -> str:
        """Empty string if new positions are allowed, otherwise the reason they are not."""
        if self.week_start_equity and equity / self.week_start_equity - 1 <= -rk.weekly_drawdown_limit:
            self._halt(d + timedelta(days=7 - d.weekday()), "weekly drawdown limit hit")
        if self.halt_until and d.isoformat() < self.halt_until:
            return f"trading paused until {self.halt_until}: {self.halt_reason}"
        if self.day_start_equity and equity / self.day_start_equity - 1 <= -rk.daily_loss_limit:
            return "daily loss limit hit"
        return ""


def sector_risk(positions: list, sector: str, equity: float) -> float:
    """Open risk (distance to stop) in one sector as a fraction of equity."""
    risk = sum(max(p["entry_price"] - p["stop"], 0) * p["qty"] for p in positions if p["sector"] == sector)
    return risk / equity if equity > 0 else 1.0
