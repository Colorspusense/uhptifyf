"""Command line entry point. Run `python bot.py --help`."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date

from pairsbot.config import load_config, load_token
from pairsbot.data import make_provider


def _setup_logging(root) -> None:
    (root / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(root / "logs" / "bot.log", encoding="utf-8")])


def _print_metrics(m: dict) -> None:
    for k, v in m.items():
        print(f"  {k:<20}{v}")


def cmd_backtest(cfg, args) -> None:
    from pairsbot.backtest import run_backtest

    provider = make_provider(cfg, load_token(cfg.root), args.source)
    print(f"Backtest on {provider.name} data, capital {cfg.general.capital:,.0f} RUB ...")
    res = run_backtest(cfg, provider, args.start, args.end)
    out = cfg.root / "reports"
    out.mkdir(exist_ok=True)
    res.trades.to_csv(out / "backtest_trades.csv", index=False)
    res.equity.to_csv(out / "backtest_equity.csv", header=["equity"])
    _print_metrics(res.metrics)
    if res.metrics["trades"] == 0:
        print("No trades: no pair passed all mandatory filters in this period (run `scan` to see why).")
    print(f"Trades and equity curve saved to {out}")


def cmd_walkforward(cfg, args) -> None:
    from pairsbot.backtest import walk_forward

    provider = make_provider(cfg, load_token(cfg.root), args.source)
    folds = walk_forward(cfg, provider, args.start, args.end)
    if not folds:
        print("Not enough history for one train + test window.")
    for f in folds:
        print(json.dumps(f, ensure_ascii=False))
    print(f"Out-of-sample total: {sum(f['net_profit'] for f in folds):,.2f} RUB over {len(folds)} window(s)")


def _engine(cfg, mode: str, source: str | None = None):
    from pairsbot.live import Engine, PaperExecutor, TBankExecutor

    token = load_token(cfg.root)
    if mode in ("scan", "paper"):
        provider = make_provider(cfg, token, source)
        if provider.name == "synthetic":
            raise SystemExit("synthetic data is only for backtest / walkforward")
        factory = (lambda state: PaperExecutor(cfg, state)) if mode == "paper" else None
        return Engine(cfg, provider, mode, factory)

    provider = make_provider(cfg, token, "tbank", sandbox=(mode == "sandbox"))
    client = provider.client
    accounts = client.accounts()
    wanted = os.environ.get("TBANK_ACCOUNT_ID")
    if mode == "sandbox" and not accounts:
        account_id = client.open_sandbox_account(cfg.general.capital)
        print(f"Opened sandbox account {account_id} with {cfg.general.capital:,.0f} RUB")
    elif wanted:
        if wanted not in [a["id"] for a in accounts]:
            raise SystemExit(f"TBANK_ACCOUNT_ID={wanted} is not among this token's accounts")
        account_id = wanted
    elif len(accounts) == 1 or mode == "sandbox":
        account_id = accounts[0]["id"]
    else:
        listing = "\n".join(f"  {a['id']}  {a.get('name', '')}  {a.get('type', '')}" for a in accounts)
        raise SystemExit(f"Several accounts found; set TBANK_ACCOUNT_ID to one of:\n{listing}")
    return Engine(cfg, provider, mode, lambda state: TBankExecutor(cfg, client, account_id))


def cmd_scan(cfg, args) -> None:
    from pairsbot.live import format_rows

    engine = _engine(cfg, "scan", args.source)
    print(f"Scan on {engine.p.name} data (no orders are placed)")
    print(format_rows(engine.cycle(trade=False)))


def cmd_trade(cfg, args) -> None:
    from pairsbot.live import run_loop

    if args.command == "live" and not args.confirm_real_money:
        raise SystemExit("Live mode places real orders with real money.\n"
                         "Re-run with --confirm-real-money once paper / sandbox results satisfy you.")
    _setup_logging(cfg.root)
    run_loop(_engine(cfg, args.command, getattr(args, "source", None)), once=args.once)


def cmd_status(cfg, args) -> None:
    path = cfg.root / "state" / f"{args.mode}.json"
    if not path.exists():
        print(f"No state for mode '{args.mode}' yet.")
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    if args.reset_halt:
        state["risk"].update(halt_until="", halt_reason="", consecutive_losses=0)
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        print("Trading pause cleared.")
    print(json.dumps(state, indent=2, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description="Long-only statistical pairs trading bot (MOEX, T-Bank Invest API)")
    ap.add_argument("--config", help="path to config.toml (default: next to bot.py)")
    sub = ap.add_subparsers(dest="command", required=True)
    sources = ["auto", "tbank", "moex", "synthetic"]

    for name, fn, text in (("backtest", cmd_backtest, "simulate the strategy on daily history"),
                           ("walkforward", cmd_walkforward, "walk-forward analysis of the main parameters")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD")
        p.add_argument("--end", type=date.fromisoformat, help="YYYY-MM-DD")
        p.add_argument("--source", choices=sources)
        p.set_defaults(fn=fn)

    p = sub.add_parser("scan", help="show today's statistics and signals for every pair, no orders")
    p.add_argument("--source", choices=sources[:3])
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("paper", help="simulation on live quotes with a virtual account")
    p.add_argument("--source", choices=sources[:3])
    p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    p.set_defaults(fn=cmd_trade)

    p = sub.add_parser("sandbox", help="trade in the T-Bank sandbox (test contour, no real money)")
    p.add_argument("--once", action="store_true")
    p.set_defaults(fn=cmd_trade)

    p = sub.add_parser("live", help="REAL orders on your T-Bank account")
    p.add_argument("--confirm-real-money", action="store_true")
    p.add_argument("--once", action="store_true")
    p.set_defaults(fn=cmd_trade)

    p = sub.add_parser("status", help="show saved positions and risk state")
    p.add_argument("--mode", choices=["paper", "sandbox", "live"], default="paper")
    p.add_argument("--reset-halt", action="store_true", help="clear a trading pause after your review")
    p.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    args.fn(load_config(args.config), args)


if __name__ == "__main__":
    main()
