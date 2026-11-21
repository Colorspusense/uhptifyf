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
.\run.bat pairs              search liquid stocks for pairs that pass the statistical filters
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
   log returns > 0.90, 60-day rolling correlation not broken down and not falling by more than 0.05, Engle-Granger p < 0.05, beta from
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

## Choosing pairs

The five pairs in `config.toml` are the guide's candidates, which it says must be verified on current
data. On Moscow Exchange history up to November 2026 most of them do not meet the guide's own
thresholds (for example Sber/VTB return correlation is about 0.7 against the required 0.9), so the bot
rarely or never trades them. "Pair search" on the Scanner page (or `.\run.bat pairs`) tests about 50
liquid stocks sector by sector and shows which pairs pass today; "Add to list" puts one into your
settings. Run it monthly, as the guide requires, and backtest a pair before trading it.

## What stays manual

The bot cannot read news. Put affected tickers into `live.news_blackout` in `config.toml`, and review
the pair list monthly as the guide requires. The guide gives no numbers for the profit target, the
half-life scale, "rolling correlation slightly/noticeably falling" or pullback detection; the values
used are in `config.toml` and `pairsbot/screens.py` and are assumptions to tune.

The backtest replays daily bars, so screen 3 is replaced by a daily proxy there.

## Before trading real money

- **Use a separate account for the bot.** It compares its own records with the shares on the account.
  If you hold or trade the same stocks by hand on that account, it can mistake your shares for its own.
- **Run the sandbox first.** The order code is tested against an imitation of the T-Bank API built
  from its official description, not against the real service. T-Bank may issue a separate sandbox
  token; put it in the "Sandbox token" field (`TBANK_SANDBOX_TOKEN`).
- **Dividends.** Price history is not adjusted for dividends, so an ex-dividend gap looks like a real
  price drop: it can produce a false signal or trigger a stop. Add the ticker to the news blackout
  around its ex-dividend date.
- **Commission.** `costs.commission` (0.05% by default) is used by the simulations; set it to your tariff.
- **Stops.** In sandbox and live modes the stop-loss is also placed at the broker, so it works when
  the bot is off. A dot next to the stop price in the interface means the broker refused it and only
  the running bot watches that stop. In paper mode stops are checked once per cycle.
- **What keeps running.** Trading started from the interface stops when the interface window is
  closed. For unattended work start it from the command line (`.\run.bat live ...`).

## How the bot protects itself

- Only one trading process per mode can run; a second start is refused.
- Every order is written down before it is sent. If the bot or the connection dies in the middle,
  the next cycle finds out what happened to that order before doing anything else, so a buy cannot
  be lost or repeated and a sell cannot be sent twice.
- If the shares disappear from the account (the broker stop fired, or you sold by hand), the position
  is closed in the bot's records after two consecutive checks.
- No orders are sent outside the configured session or while the exchange is not trading the stock.
- "Close" in the interface sells one position (the guide's fourth exit ground: the fundamentals
  changed). It never opens new trades.
- Settings are checked before saving: values outside sensible ranges are refused.

## Layout

```
bot.py               command line
ui.bat, ui.html      web interface (pairsbot/ui_server.py serves it)
config.toml          every threshold (config.default.toml is an untouched copy)
pairsbot/stats.py    correlation, cointegration, beta, Z-score, half-life
pairsbot/screens.py  three screens and TQS
pairsbot/risk.py     position sizing and limits
pairsbot/backtest.py backtest, metrics, walk-forward
pairsbot/live.py     scan / paper / sandbox / live engine
pairsbot/tbank.py    T-Bank Invest API client (REST)
pairsbot/finder.py   pair search over a built-in list of liquid stocks
pairsbot/data.py     T-Bank, MOEX ISS and synthetic data
tests/               run with: .venv\Scripts\python -m pytest
```
