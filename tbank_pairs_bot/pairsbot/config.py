"""Configuration: dataclass defaults overlaid with config.toml."""
from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class General:
    capital: float = 1_000_000.0
    data_source: str = "auto"
    skip_weekends: bool = True
    index_ticker: str = "IMOEX"


@dataclass
class Pair:
    a: str
    b: str
    sector: str = "other"


@dataclass
class Stats:
    min_history: int = 250
    corr_window: int = 120
    rolling_corr_window: int = 60
    rolling_corr_lookback: int = 20
    breakdown_corr: float = 0.70
    max_rolling_corr_drop: float = 0.05
    coint_window: int = 250
    coint_recalc_days: int = 5
    beta_window: int = 120
    beta_stability_lookback: int = 63
    z_window: int = 120
    min_corr: float = 0.90
    max_coint_pvalue: float = 0.05
    max_beta_change: float = 0.10
    entry_z: float = -2.0
    extreme_z: float = -3.0
    allow_extreme_z: bool = False
    require_z_turn: bool = True
    exit_z: float = -0.25
    breakdown_beta_change: float = 0.20


@dataclass
class Entry:
    enabled: bool = True
    min_bars: int = 250
    pullback_lookback: int = 12
    ema_touch_tolerance: float = 0.002
    long_candle_atr: float = 2.0
    require_pullback: bool = True
    max_book_spread: float = 0.001


@dataclass
class Tqs:
    min_score: int = 80
    reduced_min_score: int = 70
    reduced_size_factor: float = 0.5


@dataclass
class Risk:
    risk_per_trade: float = 0.005
    stop_pct: float = 0.02
    take_profit_pct: float = 0.0
    max_position_pct: float = 0.50
    max_sector_risk: float = 0.025
    daily_loss_limit: float = 0.02
    weekly_drawdown_limit: float = 0.05
    max_consecutive_losses: int = 3
    pause_days: int = 5
    max_holding_days: int = 0


@dataclass
class Costs:
    commission: float = 0.0005
    slippage: float = 0.0002


@dataclass
class Live:
    poll_seconds: int = 300
    session_start: str = "10:05"
    session_end: str = "18:35"
    order_timeout_seconds: int = 120
    place_broker_stop: bool = True
    news_blackout: list = field(default_factory=list)


@dataclass
class WalkForward:
    train_days: int = 500
    test_days: int = 125
    entry_z: list = field(default_factory=lambda: [-2.0, -2.25, -2.5])
    exit_z: list = field(default_factory=lambda: [-0.5, -0.25, 0.0])
    stop_pct: list = field(default_factory=lambda: [0.02, 0.03])


@dataclass
class Config:
    general: General = field(default_factory=General)
    pairs: list = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    entry: Entry = field(default_factory=Entry)
    tqs: Tqs = field(default_factory=Tqs)
    risk: Risk = field(default_factory=Risk)
    costs: Costs = field(default_factory=Costs)
    live: Live = field(default_factory=Live)
    walkforward: WalkForward = field(default_factory=WalkForward)
    root: Path = ROOT

    @property
    def tickers(self) -> list:
        out = []
        for p in self.pairs:
            for t in (p.a, p.b):
                if t not in out:
                    out.append(t)
        return out

    def validate(self) -> None:
        for name, (lo, hi, lo_open, hi_open) in _RANGES.items():
            section, key = name.split(".")
            value = getattr(getattr(self, section), key)
            for v in value if isinstance(value, list) else [value]:
                if (v < lo or (lo_open and v == lo)) or (v > hi or (hi_open and v == hi)):
                    note = "; the guide forbids more than 2%" if name == "risk.risk_per_trade" else ""
                    raise ValueError(f"{name} must be in {'(' if lo_open else '['}{lo:g}, {hi:g}"
                                     f"{')' if hi_open else ']'}{note}")
        g, s, t, lv, wf = self.general, self.stats, self.tqs, self.live, self.walkforward
        if g.data_source not in ("auto", "tbank", "moex", "synthetic"):
            raise ValueError("general.data_source must be auto, tbank, moex or synthetic")
        tickers = [g.index_ticker] + lv.news_blackout
        for p in self.pairs:
            tickers += [p.a, p.b]
            if p.a == p.b:
                raise ValueError(f"pair {p.a}/{p.b}: the two stocks must differ")
        for ticker in tickers:
            if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
                raise ValueError(f"{ticker!r} is not a valid ticker (capital letters and digits)")
        if not self.pairs:
            raise ValueError("config has no [[pairs]]")
        if not s.extreme_z < s.entry_z < s.exit_z:
            raise ValueError("stats: extreme_z must be below entry_z, and entry_z below exit_z")
        if t.reduced_min_score > t.min_score:
            raise ValueError("tqs.reduced_min_score must not be above tqs.min_score")
        for name in ("session_start", "session_end"):
            if not _CLOCK.fullmatch(getattr(lv, name)):
                raise ValueError(f"live.{name} must be a time such as 10:05")
        if lv.session_start >= lv.session_end:
            raise ValueError("live.session_start must be earlier than live.session_end")
        for name in ("entry_z", "exit_z", "stop_pct"):
            if not getattr(wf, name):
                raise ValueError(f"walkforward.{name} must list at least one value")


_TICKER = re.compile(r"[A-Z0-9]{1,12}")
_CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
# name: (low, high, low is excluded, high is excluded). The risk ceilings come from the guide.
_RANGES = {
    "general.capital": (0, 1e13, True, False),
    "stats.min_history": (60, 5000, False, False), "stats.corr_window": (20, 2000, False, False),
    "stats.rolling_corr_window": (10, 1000, False, False), "stats.rolling_corr_lookback": (1, 500, False, False),
    "stats.breakdown_corr": (-1, 1, False, False), "stats.max_rolling_corr_drop": (0, 2, True, False),
    "stats.coint_window": (60, 5000, False, False), "stats.coint_recalc_days": (1, 250, False, False),
    "stats.beta_window": (20, 2000, False, False), "stats.beta_stability_lookback": (1, 1000, False, False),
    "stats.z_window": (20, 2000, False, False), "stats.min_corr": (-1, 1, False, True),
    "stats.max_coint_pvalue": (0, 1, True, False), "stats.max_beta_change": (0, 10, True, False),
    "stats.entry_z": (-20, 0, False, True), "stats.extreme_z": (-50, 0, False, True),
    "stats.exit_z": (-20, 20, False, False), "stats.breakdown_beta_change": (0, 10, True, False),
    "entry.min_bars": (210, 5000, False, False), "entry.pullback_lookback": (2, 200, False, False),
    "entry.ema_touch_tolerance": (0, 0.2, False, False), "entry.long_candle_atr": (0, 100, True, False),
    "entry.max_book_spread": (0, 1, True, False),
    "tqs.min_score": (0, 100, False, False), "tqs.reduced_min_score": (0, 100, False, False),
    "tqs.reduced_size_factor": (0, 1, True, False),
    "risk.risk_per_trade": (0, 0.02, True, False), "risk.stop_pct": (0, 0.5, True, True),
    "risk.take_profit_pct": (0, 10, False, False), "risk.max_position_pct": (0, 1, True, False),
    "risk.max_sector_risk": (0, 1, True, False), "risk.daily_loss_limit": (0, 1, True, False),
    "risk.weekly_drawdown_limit": (0, 1, True, False), "risk.max_consecutive_losses": (1, 1000, False, False),
    "risk.pause_days": (0, 365, False, False), "risk.max_holding_days": (0, 3650, False, False),
    "costs.commission": (0, 0.05, False, False), "costs.slippage": (0, 0.05, False, False),
    "live.poll_seconds": (10, 86400, False, False), "live.order_timeout_seconds": (0, 3600, False, False),
    "walkforward.train_days": (50, 5000, False, False), "walkforward.test_days": (10, 5000, False, False),
    "walkforward.entry_z": (-20, 0, False, True), "walkforward.exit_z": (-20, 20, False, False),
    "walkforward.stop_pct": (0, 0.5, True, True),
}


def _typed(cls, key: str, value, default):
    """TOML values must have the type of the option's default; whole numbers are fine for fractions."""
    where = f"{cls.__name__.lower()}.{key}"
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, float):
        ok = number and math.isfinite(value)
        value = float(value) if ok else value
    elif isinstance(default, list):
        numeric = bool(default) and isinstance(default[0], (int, float))
        ok = isinstance(value, list) and all(
            (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)) if numeric
            else isinstance(v, str) for v in value)
    else:
        ok = isinstance(value, str)
    if not ok:
        raise ValueError(f"{where}: {value!r} is not a valid {type(default).__name__}")
    return value


def _fill(cls, data: dict):
    if not isinstance(data, dict):
        raise ValueError(f"[{cls.__name__.lower()}] must be a table")
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown option(s) in [{cls.__name__.lower()}]: {sorted(unknown)}")
    if cls is Pair:
        if not all(isinstance(v, str) for v in data.values()) or not {"a", "b"} <= set(data):
            raise ValueError("every [[pairs]] entry needs text values a and b")
        return cls(**data)
    defaults = cls()
    return cls(**{k: _typed(cls, k, v, getattr(defaults, k)) for k, v in data.items()})


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    known = {"general", "pairs", "stats", "entry", "tqs", "risk", "costs", "live", "walkforward"}
    if set(raw) - known:
        raise ValueError(f"unknown section(s): {sorted(set(raw) - known)}")
    if not isinstance(raw.get("pairs", []), list):
        raise ValueError("pairs must be written as [[pairs]] blocks")
    cfg = Config(
        general=_fill(General, raw.get("general", {})),
        pairs=[_fill(Pair, p) for p in raw.get("pairs", [])],
        stats=_fill(Stats, raw.get("stats", {})),
        entry=_fill(Entry, raw.get("entry", {})),
        tqs=_fill(Tqs, raw.get("tqs", {})),
        risk=_fill(Risk, raw.get("risk", {})),
        costs=_fill(Costs, raw.get("costs", {})),
        live=_fill(Live, raw.get("live", {})),
        walkforward=_fill(WalkForward, raw.get("walkforward", {})),
        root=path.resolve().parent if path.exists() else ROOT,
    )
    cfg.validate()
    return cfg


def load_secret(name: str, root: Path = ROOT) -> str | None:
    """Secrets come from the environment or the .env file, never from config.toml."""
    value = os.environ.get(name)
    env = root / ".env"
    if not value and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name:
                value = v.strip().strip('"').strip("'")
    return value or None


def load_token(root: Path = ROOT) -> str | None:
    return load_secret("TBANK_TOKEN", root)
