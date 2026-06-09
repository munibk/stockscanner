"""
Walk-forward backtester for the stockupdate signal engine
=========================================================
This is what turns the scanner from "looks reasonable" into "has a measured
edge". It replays `generate_signal` bar-by-bar with NO lookahead, simulates the
exact ATR stop/target trade plan the tool recommends, and reports the metrics
that actually matter before risking real money:

  • number of trades, win rate
  • average R (expectancy per trade) and total R
  • profit factor
  • max drawdown (in R)
  • comparison vs simple buy & hold

Method (important — this is why the numbers are trustworthy):
  • Indicators are causal (RSI/MACD/EMA/ATR/etc. use only past + current bars),
    so they are computed ONCE on the full series and then sliced. The signal at
    bar i is computed from `df.iloc[:i+1]` — it can never see the future.
  • A signal generated on the close of bar i is entered at the OPEN of bar i+1
    (no same-bar fills).
  • Exits use each subsequent bar's High/Low against the ATR stop and target.
    If both are touched in the same bar we assume the stop (worst case).

Usage:
    python backtest.py                                   # Nifty 50, daily
    python backtest.py --stocks RELIANCE TCS INFY        # specific names
    python backtest.py --interval 1h                     # other timeframe
    python backtest.py --stocks RELIANCE --rr-target 2.0 --rr-stop 1.5
"""

import argparse
import sys
from dataclasses import dataclass, field

# Windows consoles default to cp1252 and choke on box-drawing / arrow glyphs.
# Force UTF-8 so the report renders identically everywhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

import pandas as pd
from colorama import Fore, Style, init
from tabulate import tabulate

from stockupdate import (
    MIN_ROWS,
    NIFTY_50_FALLBACK,
    _NewlyListedError,
    compute_indicators,
    fetch_data,
    fetch_nifty50_tickers,
    generate_signal,
)

init(autoreset=True)


@dataclass
class Trade:
    ticker: str
    side: str            # "BUY" or "SELL"
    entry_idx: int
    entry_price: float
    exit_price: float
    stop: float
    target: float
    outcome: str         # "target" | "stop" | "open"
    r_multiple: float
    bars_held: int


@dataclass
class BacktestResult:
    ticker: str
    trades: list = field(default_factory=list)
    bars_total: int = 0
    bars_in_market: int = 0
    buy_hold_pct: float = 0.0
    error: str | None = None


def backtest_ticker(
    ticker: str,
    interval: str = "1d",
    atr_stop: float = 1.5,
    atr_target: float = 2.0,
    warmup: int | None = None,
) -> BacktestResult:
    """Run a single-position-at-a-time walk-forward backtest for one ticker."""
    try:
        df = fetch_data(ticker, interval)
    except _NewlyListedError as e:
        return BacktestResult(ticker, error=f"newly listed ({e.rows} bars)")
    if df is None or len(df) < MIN_ROWS + 5:
        return BacktestResult(ticker, error="insufficient data")

    df = compute_indicators(df, interval)
    n = len(df)
    res = BacktestResult(ticker, bars_total=n)

    opens = df["Open"].values
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    atrs = df["ATR"].values

    warm = warmup if warmup is not None else MIN_ROWS
    warm = max(warm, 30)

    position: dict | None = None

    for i in range(warm, n):
        # ── 1) Manage an open position against THIS bar's range ──────────────
        if position is not None:
            hi, lo = highs[i], lows[i]
            side = position["side"]
            stop, target = position["stop"], position["target"]

            if side == "BUY":
                hit_stop = lo <= stop
                hit_tgt = hi >= target
            else:
                hit_stop = hi >= stop
                hit_tgt = lo <= target

            exit_price = None
            outcome = None
            if hit_stop and hit_tgt:
                exit_price, outcome = stop, "stop"      # worst-case assumption
            elif hit_tgt:
                exit_price, outcome = target, "target"
            elif hit_stop:
                exit_price, outcome = stop, "stop"

            if exit_price is not None:
                entry = position["entry_price"]
                risk = abs(entry - stop)
                if side == "BUY":
                    r_mult = (exit_price - entry) / risk if risk else 0.0
                else:
                    r_mult = (entry - exit_price) / risk if risk else 0.0
                res.trades.append(Trade(
                    ticker, side, position["entry_idx"], entry, exit_price,
                    stop, target, outcome, round(r_mult, 3), i - position["entry_idx"],
                ))
                res.bars_in_market += i - position["entry_idx"]
                position = None

        # ── 2) If flat, evaluate the signal and queue an entry for next open ──
        if position is None and i < n - 1:
            sig = generate_signal(df.iloc[: i + 1], interval)
            s = sig["signal"]
            if s in ("BUY", "SELL"):
                atr = atrs[i]
                if pd.isna(atr) or atr <= 0:
                    continue
                entry_price = float(opens[i + 1])
                if s == "BUY":
                    stop = entry_price - atr_stop * atr
                    target = entry_price + atr_target * atr
                else:
                    stop = entry_price + atr_stop * atr
                    target = entry_price - atr_target * atr
                position = {
                    "side": s,
                    "entry_idx": i + 1,
                    "entry_price": entry_price,
                    "stop": stop,
                    "target": target,
                }

    # Buy & hold benchmark over the tradable window (warmup → end)
    start_px = float(closes[warm])
    end_px = float(closes[-1])
    res.buy_hold_pct = (end_px - start_px) / start_px * 100 if start_px else 0.0
    return res


def summarize(trades: list) -> dict:
    """Compute headline metrics from a list of closed trades."""
    closed = [t for t in trades if t.outcome in ("target", "stop")]
    n = len(closed)
    if n == 0:
        return {"trades": 0}

    wins = [t for t in closed if t.r_multiple > 0]
    losses = [t for t in closed if t.r_multiple <= 0]
    total_r = sum(t.r_multiple for t in closed)
    gross_win = sum(t.r_multiple for t in wins)
    gross_loss = abs(sum(t.r_multiple for t in losses))

    # Max drawdown on the cumulative-R equity curve
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in closed:
        equity += t.r_multiple
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n * 100,
        "avg_r": total_r / n,
        "total_r": total_r,
        "avg_win_r": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss_r": (-gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_dd_r": max_dd,
        "avg_bars_held": sum(t.bars_held for t in closed) / n,
    }


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def run(tickers: list, interval: str, atr_stop: float, atr_target: float) -> None:
    print(f"\n{Fore.CYAN}{'=' * 72}")
    print(f"  WALK-FORWARD BACKTEST  |  interval={interval}  |  "
          f"stop={atr_stop}xATR  target={atr_target}xATR")
    print(f"  Stocks: {len(tickers)}   (single position per stock, no lookahead)")
    print(f"{'=' * 72}{Style.RESET_ALL}\n")

    all_trades = []
    per_ticker_rows = []

    for idx, tkr in enumerate(tickers, 1):
        print(f"  Backtesting {tkr:15s} ({idx}/{len(tickers)})...", end="\r")
        res = backtest_ticker(tkr, interval, atr_stop, atr_target)
        if res.error:
            continue
        m = summarize(res.trades)
        all_trades.extend(res.trades)
        if m["trades"] == 0:
            per_ticker_rows.append([tkr, 0, "—", "—", "—", "—", f"{res.buy_hold_pct:+.1f}%"])
            continue
        per_ticker_rows.append([
            tkr,
            m["trades"],
            f"{m['win_rate']:.0f}%",
            f"{m['avg_r']:+.2f}",
            f"{m['total_r']:+.1f}",
            _fmt_pf(m["profit_factor"]),
            f"{res.buy_hold_pct:+.1f}%",
        ])

    print(" " * 60, end="\r")

    headers = ["Ticker", "Trades", "Win%", "Avg R", "Total R", "PF", "Buy&Hold"]
    print(tabulate(per_ticker_rows, headers=headers, tablefmt="rounded_outline"))

    agg = summarize(all_trades)
    print(f"\n{Fore.CYAN}{'-' * 72}{Style.RESET_ALL}")
    print(f"{Style.BRIGHT}  AGGREGATE (all stocks combined){Style.RESET_ALL}")
    if agg["trades"] == 0:
        print(f"  {Fore.YELLOW}No trades were generated over the test window.{Style.RESET_ALL}")
        return

    edge = agg["avg_r"]
    edge_col = Fore.GREEN if edge > 0 else Fore.RED
    print(f"  Trades            : {agg['trades']}")
    print(f"  Win rate          : {agg['win_rate']:.1f}%  "
          f"({agg['wins']}W / {agg['losses']}L)")
    print(f"  Expectancy / trade: {edge_col}{agg['avg_r']:+.3f} R{Style.RESET_ALL}")
    print(f"  Total R           : {agg['total_r']:+.1f} R")
    print(f"  Avg win / avg loss: {agg['avg_win_r']:+.2f} R / {agg['avg_loss_r']:+.2f} R")
    print(f"  Profit factor     : {_fmt_pf(agg['profit_factor'])}")
    print(f"  Max drawdown      : {agg['max_dd_r']:.1f} R")
    print(f"  Avg bars held     : {agg['avg_bars_held']:.0f}")

    print(f"\n{Fore.CYAN}{'-' * 72}{Style.RESET_ALL}")
    if edge > 0.05 and agg["profit_factor"] > 1.3 and agg["trades"] >= 30:
        print(f"  {Fore.GREEN}Positive expectancy with a usable sample. The edge looks real,{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}but forward-test on paper before committing capital.{Style.RESET_ALL}")
    elif agg["trades"] < 30:
        print(f"  {Fore.YELLOW}Too few trades to trust these numbers - widen the universe / window.{Style.RESET_ALL}")
    else:
        print(f"  {Fore.RED}No reliable positive edge in this configuration. Do NOT trade it live{Style.RESET_ALL}")
        print(f"  {Fore.RED}as-is; tune thresholds and re-test, or use signals as alerts only.{Style.RESET_ALL}")
    print(f"\n  WARNING: Past performance is not indicative of future results.\n")


def main():
    parser = argparse.ArgumentParser(description="Walk-forward backtester for stockupdate signals")
    parser.add_argument("--stocks", nargs="+", metavar="TICKER",
                        help="Specific NSE tickers (without .NS). Default: Nifty 50.")
    parser.add_argument("--interval", default="1d",
                        choices=["1m", "5m", "15m", "1h", "1d", "1wk"],
                        help="Candle interval (default: 1d)")
    parser.add_argument("--rr-stop", type=float, default=1.5,
                        help="Stop distance in ATR multiples (default: 1.5)")
    parser.add_argument("--rr-target", type=float, default=2.0,
                        help="Target distance in ATR multiples (default: 2.0)")
    args = parser.parse_args()

    if args.stocks:
        tickers = [t.upper() for t in args.stocks]
    else:
        try:
            tickers = fetch_nifty50_tickers()
        except Exception:
            tickers = list(NIFTY_50_FALLBACK)

    run(tickers, args.interval, args.rr_stop, args.rr_target)


if __name__ == "__main__":
    main()
