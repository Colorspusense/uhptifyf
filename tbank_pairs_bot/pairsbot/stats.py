"""First screen: correlation, cointegration, beta, log spread, Z-score, half-life."""
from __future__ import annotations

import math
import warnings
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _corr(x: np.ndarray, y: np.ndarray) -> float:
    if x.std() == 0 or y.std() == 0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _beta(ra: np.ndarray, rb: np.ndarray) -> float:
    """Slope of the regression of A's log returns on B's log returns."""
    var = rb.var(ddof=1)
    return float(np.cov(ra, rb, ddof=1)[0, 1] / var) if var > 0 else float("nan")


def half_life(spread: np.ndarray) -> float:
    """Days for half of a deviation to decay, from an AR(1) fit of the spread."""
    x, y = spread[:-1], spread[1:]
    if x.std() == 0:
        return float("inf")
    phi = float(np.polyfit(x, y, 1)[0])
    if phi >= 1:
        return float("inf")
    if phi <= 0:
        return 1.0
    return -math.log(2) / math.log(phi)


def coint_pvalue(la: np.ndarray, lb: np.ndarray) -> float:
    """Engle-Granger p-value on log prices."""
    from statsmodels.tsa.stattools import coint

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            p = float(coint(la, lb)[1])
        except Exception:
            return 1.0
    return p if math.isfinite(p) else 1.0


def z_history(la: np.ndarray, lb: np.ndarray, st, n: int = 250) -> list:
    """Z-score for each of the last n days, computed with only the data available on that day."""
    out = []
    first = max(st.beta_window + 1, st.z_window, len(la) - n + 1)
    for end in range(first, len(la) + 1):
        beta = _beta(np.diff(la[end - st.beta_window - 1:end]), np.diff(lb[end - st.beta_window - 1:end]))
        spread = la[end - st.z_window:end] - beta * lb[end - st.z_window:end]
        sd = spread.std(ddof=1)
        out.append(float((spread[-1] - spread.mean()) / sd) if math.isfinite(beta) and sd > 0 else float("nan"))
    return out


@dataclass
class Snapshot:
    """Statistics of the pair "buy A against B" on one day."""
    corr: float
    corr_roll: float
    corr_roll_prev: float
    coint_p: float
    beta: float
    beta_change: float
    z: float
    z_prev: float
    half_life: float

    def rolling_state(self, breakdown_corr: float) -> str:
        d = self.corr_roll - self.corr_roll_prev
        if self.corr_roll < breakdown_corr:
            return "breakdown"
        if d >= -0.02:
            return "stable"
        return "slightly_down" if d >= -0.05 else "down"

    def as_dict(self) -> dict:
        return asdict(self)


def required_history(st) -> int:
    return max(st.min_history, st.coint_window, st.z_window,
               st.beta_window + st.beta_stability_lookback + 1,
               st.rolling_corr_window + st.rolling_corr_lookback + 1) + 1


def pair_snapshot(la: np.ndarray, lb: np.ndarray, st, coint_p: float | None = None) -> Snapshot | None:
    """la / lb are aligned log closes ending on the evaluation day. None if history is too short."""
    if len(la) < required_history(st) or len(la) != len(lb):
        return None
    ra, rb = np.diff(la), np.diff(lb)
    rw, lk = st.rolling_corr_window, st.rolling_corr_lookback
    bw, bk = st.beta_window, st.beta_stability_lookback

    beta = _beta(ra[-bw:], rb[-bw:])
    beta_prev = _beta(ra[-bw - bk:-bk], rb[-bw - bk:-bk])
    if not math.isfinite(beta) or not math.isfinite(beta_prev) or beta_prev == 0:
        return None
    spread = la[-st.z_window:] - beta * lb[-st.z_window:]
    sd = spread.std(ddof=1)
    if sd == 0:
        return None
    mu = spread.mean()
    return Snapshot(
        corr=_corr(ra[-st.corr_window:], rb[-st.corr_window:]),
        corr_roll=_corr(ra[-rw:], rb[-rw:]),
        corr_roll_prev=_corr(ra[-rw - lk:-lk], rb[-rw - lk:-lk]),
        coint_p=coint_pvalue(la[-st.coint_window:], lb[-st.coint_window:]) if coint_p is None else coint_p,
        beta=beta,
        beta_change=abs(beta - beta_prev) / abs(beta_prev),
        z=float((spread[-1] - mu) / sd),
        z_prev=float((spread[-2] - mu) / sd),
        half_life=half_life(spread),
    )
