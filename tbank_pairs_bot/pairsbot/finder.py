"""Pair search: which same-sector pairs of liquid stocks pass the guide's first-screen conditions today.

The guide asks for the pair list to be re-evaluated monthly; this does the statistical part of that.
"""
from __future__ import annotations

from datetime import timedelta
from itertools import combinations

import numpy as np
import pandas as pd

from .data import DailyCache, now_msk
from .stats import pair_snapshot, required_history, z_history

# Liquid Moscow Exchange shares by sector. A ticker the data source does not know is simply skipped.
UNIVERSE = {
    "banks": ["SBER", "SBERP", "VTBR", "T", "BSPB", "SVCB", "CBOM", "MOEX"],
    "oil_gas": ["LKOH", "ROSN", "GAZP", "NVTK", "SNGS", "SNGSP", "TATN", "TATNP", "SIBN", "BANEP", "TRNFP"],
    "metals": ["GMKN", "NLMK", "CHMF", "MAGN", "RUAL", "PLZL", "ALRS", "SELG", "MTLR", "MTLRP", "RASP"],
    "tech_telecom": ["MTSS", "RTKM", "RTKMP", "YDEX", "VKCO", "OZON", "POSI", "HEAD"],
    "retail": ["MGNT", "X5", "LENT"],
    "power": ["IRAO", "HYDR", "FEES", "UPRO", "MSNG"],
    "chemicals": ["PHOR", "AKRN"],
    "transport": ["AFLT", "FLOT", "NMTP"],
}


def find_pairs(cfg, provider, universe: dict | None = None) -> dict:
    st = cfg.stats
    universe = universe or UNIVERSE
    cache = DailyCache(provider, cfg.root, cfg.general.skip_weekends)
    start = now_msk().date() - timedelta(days=1100)
    closes, skipped = {}, []
    for ticker in dict.fromkeys(t for tickers in universe.values() for t in tickers):
        try:
            s = cache.daily(ticker, start)["close"].astype(float)
        except Exception:
            s = pd.Series(dtype=float)
        if len(s) >= required_history(st) and s.index[-1] >= pd.Timestamp(start) + pd.Timedelta(days=1080):
            closes[ticker] = s
        else:
            skipped.append(ticker)  # unknown, too young, or no longer traded
    configured = {frozenset((p.a, p.b)) for p in cfg.pairs}
    rows = []
    for sector, tickers in universe.items():
        for a, b in combinations([t for t in tickers if t in closes], 2):
            df = pd.concat([closes[a], closes[b]], axis=1, join="inner").dropna()
            la, lb = np.log(df.iloc[:, 0].to_numpy()), np.log(df.iloc[:, 1].to_numpy())
            sides = []
            for buy, x, y in ((a, la, lb), (b, lb, la)):
                snap = pair_snapshot(x, y, st)
                if snap:
                    zs = z_history(x, y, st, 250)
                    sides.append((buy, snap, sum(1 for z in zs if z <= st.entry_z)))
            if len(sides) < 2:
                continue
            # A pair is usable when the relationship holds in both directions, whichever stock lags.
            fails = set()
            for _, s, _ in sides:
                if s.corr <= st.min_corr:
                    fails.add("correlation")
                if s.rolling_state(st.breakdown_corr) == "breakdown" or s.corr_roll_prev - s.corr_roll > st.max_rolling_corr_drop:
                    fails.add("rolling correlation")
                if s.coint_p >= st.max_coint_pvalue:
                    fails.add("cointegration")
                if s.beta <= 0 or s.beta_change >= st.max_beta_change:
                    fails.add("beta")
            (_, sa, na), (_, sb, nb) = sides
            rows.append({"a": a, "b": b, "sector": sector, "corr": sa.corr, "corr60": sa.corr_roll,
                         "coint_p": max(sa.coint_p, sb.coint_p), "beta_chg": max(sa.beta_change, sb.beta_change),
                         "half_life": min(sa.half_life, sb.half_life), "z_a": sa.z, "z_b": sb.z,
                         "signal_days": na + nb, "fails": sorted(fails), "ok": not fails,
                         "configured": frozenset((a, b)) in configured})
    rows.sort(key=lambda r: (not r["ok"], len(r["fails"]), -r["corr"]))
    return {"rows": rows, "skipped": skipped, "source": provider.name}


def format_pairs(result: dict, limit: int = 40) -> str:
    head = f"{'pair':<14}{'sector':<14}{'corr':>6}{'cor60':>6}{'p':>7}{'dBeta':>7}{'HL':>5}{'Z a':>7}{'Z b':>7}{'days':>6}  status"
    out = [head, "-" * len(head)]
    for r in result["rows"][:limit]:
        hl = f"{r['half_life']:.0f}" if np.isfinite(r["half_life"]) else "inf"
        status = "PASSES" if r["ok"] else "fails: " + ", ".join(r["fails"])
        out.append(f"{r['a'] + '/' + r['b']:<14}{r['sector']:<14}{r['corr']:>6.2f}{r['corr60']:>6.2f}{r['coint_p']:>7.3f}"
                   f"{r['beta_chg']:>7.0%}{hl:>5}{r['z_a']:>7.2f}{r['z_b']:>7.2f}{r['signal_days']:>6}  "
                   f"{status}{'  (in config)' if r['configured'] else ''}")
    if result["skipped"]:
        out.append(f"skipped (no or short history): {', '.join(result['skipped'])}")
    return "\n".join(out)
