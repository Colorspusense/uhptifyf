"""Configuration: dataclass defaults overlaid with config.toml."""
from __future__ import annotations

import os
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
    max_position_pct: float = 0.30
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
        r = self.risk
        # Hard ceilings from the guide: they are not meant to be configurable away.
        if not 0 < r.risk_per_trade <= 0.02:
            raise ValueError("risk.risk_per_trade must be in (0, 0.02]; the guide forbids more than 2%")
        if not 0 < r.stop_pct < 0.5:
            raise ValueError("risk.stop_pct must be a fraction such as 0.02")
        if not self.pairs:
            raise ValueError("config has no [[pairs]]")


def _fill(cls, data: dict):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown option(s) in [{cls.__name__.lower()}]: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
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


def load_token(root: Path = ROOT) -> str | None:
    """Token comes from the TBANK_TOKEN environment variable or the .env file, never from config."""
    tok = os.environ.get("TBANK_TOKEN")
    env = root / ".env"
    if not tok and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "TBANK_TOKEN":
                tok = v.strip().strip('"').strip("'")
    return tok or None
