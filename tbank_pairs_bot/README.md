# T-Bank pairs bot

Long-only statistical pairs trading on the Moscow Exchange through the
[T-Bank Invest API](https://developer.tbank.ru/invest), built from the rules of
"Практическое руководство по статистическому парному трейдингу без шортов" (v1.0).
It buys the temporarily lagging stock of a related pair. No shorts, no leverage, no averaging down.

This is software, not investment advice. Profit is not guaranteed; run it in simulation first.

## Quick start (Windows)

Double-click `ui.bat`. It installs the dependencies on the first run, starts the bot's local
interface and opens it in your browser (normally http://127.0.0.1:8791; the address is printed in its window). Keep its window open while you work;
close it (or press Ctrl+C) to stop.

The interface covers everything: start and stop paper / sandbox / live trading, positions and risk
state, the scanner with Z-score charts, backtest and walk-forward, the trade journal, all settings
and the T-Bank token. Switch between English and Russian with the EN / RU buttons in the bottom-left
corner (Russian guide: [README.ru.md](README.ru.md)). It is served only to this computer. `ui.html` cannot be opened directly as a
file, because a web page on its own cannot run the bot.

## Command line

The same functions without the interface.
Open the folder in a terminal. The leading `.\` is required in PowerShell and also works in cmd.

```
.\setup.bat                  one-time: creates .venv and installs the dependencies
.\run.bat scan               today's statistics and signals for every pair, no orders
.\run.bat backtest           simulation on daily history
.\run.bat walkforward        walk-forward check of the main parameters
.\run.bat paper              simulation on live quotes with a virtual account (Ctrl+C to stop)
.\run.bat status             saved positions and risk state
```

`scan`, `backtest`, `walkforward` and `paper` work without a token: they then read the public MOEX
ISS feed, whose intraday quotes are about 15 minutes late. Add `--source synthetic` to `backtest` or
`walkforward` to see the mechanics on generated data (this is a demo, not market evidence).

## Connecting T-Bank

1. Create a token in T-Investments: Settings -> Tokens of T-Invest API. Start with a read-only or
   sandbox token; a full-access token is needed only for real orders.
2. Copy `.env.example` to `.env` and put the token there (`TBANK_TOKEN=...`). If the token sees
   several accounts, also set `TBANK_ACCOUNT_ID`.
3. With a token, market data comes from T-Bank in real time.

```
.\run.bat sandbox                       orders in the T-Bank sandbox (test contour, no real money)
.\run.bat live --confirm-real-money     REAL orders on your account
```

Add `--once` to `paper`, `sandbox` or `live` to run a single cycle, e.g. from Task Scheduler.
T-Bank hosts use the Russian Trusted Root CA; it is shipped in `certs/` and trusted only for this bot.

## Simulation modes

| Mode | Quotes | Orders | Purpose |
|---|---|---|---|
| `backtest` / `walkforward` | daily history | simulated, next-day open | does the idea hold on history |
| `paper` | live | simulated locally | forward test with a virtual account |
| `sandbox` | live (T-Bank) | T-Bank sandbox | test the real order plumbing |
| `live` | live (T-Bank) | real | trading |

## What the bot does each cycle

1. **Screen 1, statistics** (daily closes, both directions of every pair): 120-day correlation of
   log returns > 0.90, 60-day rolling correlation not broken down, Engle-Granger p < 0.05, beta from
   120 days of log returns with a 3-month change < 10%, `Z = (spread - mean) / sd <= -2.0` where
   `spread = ln(P1) - beta * ln(P2)`. Z must have stopped falling; Z below -3 is skipped as
   "check the news" unless `allow_extreme_z` is set.
2. **Screen 2, market**: IMOEX above its daily EMA200 (EMA50 above EMA200 scores as a strong market).
3. **Screen 3, entry** (15-minute bars): price > EMA20 > EMA50 > EMA200, a completed pullback, no
   entry after a long rising candle; volume pattern and bid/ask spread add TQS points.
4. **TQS** 0-100 by the chapter 11 table. 80+ trades at full size, 70-79 at half size, below 70 is
   skipped. Signals are taken in descending TQS order.
5. **Order**: a limit buy at the current offer, cancelled if not filled; a stop-loss 2% below the
   entry, also registered at the broker in live mode.
6. **Exit**, always the whole position: Z back near zero (`exit_z`), stop, optional profit target,
   or breakdown of the pair (rolling correlation or beta).

Money management: position value = risk per trade / stop distance (0.5% risk by default), capped by
cash; sector risk <= 2.5%; no new entries after a 2% daily loss; pause after a 5% weekly drawdown or
three losses in a row (`.\run.bat status --reset-halt` clears the pause after your review).

Every closed trade goes to `journal/trades.csv`; daily pair statistics go to `journal/passports/`.

## What stays manual

The bot cannot read news. Put affected tickers into `live.news_blackout` in `config.toml`, and review
the pair list monthly as the guide requires. The guide gives no numbers for the profit target, the
half-life scale, "rolling correlation slightly/noticeably falling" or pullback detection; the values
used are in `config.toml` and `pairsbot/screens.py` and are assumptions to tune.

The backtest replays daily bars, so screen 3 is replaced by a daily proxy there.

## Layout

```
bot.py               command line
ui.bat, ui.html      web interface (pairsbot/ui_server.py serves it)
config.toml          every threshold
pairsbot/stats.py    correlation, cointegration, beta, Z-score, half-life
pairsbot/screens.py  three screens and TQS
pairsbot/risk.py     position sizing and limits
pairsbot/backtest.py backtest, metrics, walk-forward
pairsbot/live.py     scan / paper / sandbox / live engine
pairsbot/tbank.py    T-Bank Invest API client (REST)
pairsbot/data.py     T-Bank, MOEX ISS and synthetic data
tests/               run with: .venv\Scripts\python -m pytest
```
