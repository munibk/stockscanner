"""
Nifty Stock Buy/Sell Signal Generator
======================================
Fetches live NSE data and generates signals using:
  - RSI (Relative Strength Index)
  - MACD (Moving Average Convergence Divergence)
  - Moving Averages (20 EMA, 50 EMA, 200 SMA)
  - Volume Analysis
  - Support / Resistance (Swing levels, Pivot Points, Round numbers)

Requirements:
    pip install yfinance pandas ta colorama tabulate requests kiteconnect

Usage:
    python nifty_signals.py                        # Scan all Nifty 50 stocks
    python nifty_signals.py --stocks RELIANCE TCS  # Specific stocks
    python nifty_signals.py --top 10               # Show top 10 BUY signals
    python nifty_signals.py --interval 1h          # Use 1h candles (default: 1d)
    python nifty_signals.py --zerodha-login        # One-time daily login to Zerodha
    python nifty_signals.py --zerodha              # Analyse your Zerodha portfolio
"""

import argparse
import configparser
import json
import os
import sys
import webbrowser
from datetime import date, datetime, timedelta

import time

import pandas as pd
import requests
import yfinance as yf
from colorama import Fore, Style, init
from tabulate import tabulate

init(autoreset=True)

# ── Zerodha / Kite Connect integration ────────────────────────────────────────
CONFIG_FILE  = os.path.join(os.path.dirname(__file__), "zerodha.cfg")
TOKEN_FILE   = os.path.join(os.path.dirname(__file__), ".zerodha_token.json")


def _ensure_config() -> configparser.ConfigParser:
    """Create zerodha.cfg with placeholders if it doesn't exist, then load it."""
    cfg = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        cfg["kite"] = {"api_key": "YOUR_API_KEY", "api_secret": "YOUR_API_SECRET"}
        with open(CONFIG_FILE, "w") as f:
            cfg.write(f)
        print(f"{Fore.YELLOW}  Created {CONFIG_FILE} — fill in your api_key and api_secret "
              f"from https://developers.kite.trade{Style.RESET_ALL}")
        sys.exit(1)
    cfg.read(CONFIG_FILE)
    return cfg


def zerodha_login() -> None:
    """Interactive one-time daily login: opens browser, prompts for request_token."""
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        print("  kiteconnect not installed. Run: pip install kiteconnect")
        sys.exit(1)

    cfg        = _ensure_config()
    api_key    = cfg["kite"]["api_key"].strip()
    api_secret = cfg["kite"]["api_secret"].strip()

    kite     = KiteConnect(api_key=api_key)
    login_url = kite.login_url()
    print(f"\n{Fore.CYAN}  Opening Zerodha login in your browser...{Style.RESET_ALL}")
    print(f"  If it doesn't open, visit:\n  {login_url}\n")
    webbrowser.open(login_url)

    request_token = input("  Paste the request_token from the redirect URL: ").strip()
    session       = kite.generate_session(request_token, api_secret=api_secret)
    access_token  = session["access_token"]

    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": access_token, "date": str(date.today())}, f)
    print(f"{Fore.GREEN}  ✓ Login successful. Token saved for today.{Style.RESET_ALL}\n")


def _load_access_token() -> str:
    """Load today's access token; prompt re-login if stale or missing."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        if data.get("date") == str(date.today()):
            return data["access_token"]
    print(f"{Fore.YELLOW}  No valid token for today. Run:\n"
          f"  python stockupdate.py --zerodha-login{Style.RESET_ALL}")
    sys.exit(1)


def fetch_zerodha_holdings() -> list[str]:
    """
    Return list of NSE tickers from your Zerodha holdings + open positions.
    """
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        sys.exit(1)  # caught by app.py; st.error() shown there

    cfg       = _ensure_config()
    api_key   = cfg["kite"]["api_key"].strip()
    kite      = KiteConnect(api_key=api_key)
    kite.set_access_token(_load_access_token())

    return _zerodha_tickers_from_kite(kite)


def fetch_zerodha_holdings_with_token(api_key: str, access_token: str) -> list[str]:
    """
    Like fetch_zerodha_holdings but takes credentials directly (no config files).
    Used by the Streamlit UI so it works on the cloud without zerodha.cfg.
    """
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return _zerodha_tickers_from_kite(kite)


def _zerodha_tickers_from_kite(kite) -> list[str]:
    """Shared logic: extract NSE/BSE tickers from holdings + open positions."""
    tickers = set()

    # Long-term holdings
    for h in kite.holdings():
        exch = h.get("exchange", "")
        qty  = h.get("quantity", 0) + h.get("t1_quantity", 0)  # include T1 unsettled
        if exch in ("NSE", "BSE") and qty > 0:
            tickers.add((h["tradingsymbol"], exch))

    # Intraday / open positions
    for p in kite.positions().get("net", []):
        exch = p.get("exchange", "")
        if exch in ("NSE", "BSE") and p.get("quantity", 0) != 0:
            tickers.add((p["tradingsymbol"], exch))

    if not tickers:
        print(Fore.YELLOW + "  No holdings/positions found in your Zerodha account." + Style.RESET_ALL)
        sys.exit(0)

    # Prefer NSE over BSE for the same symbol (NSE has better liquidity data)
    nse = {sym for sym, ex in tickers if ex == "NSE"}
    bse = {sym for sym, ex in tickers if ex == "BSE" and sym not in nse}
    all_tickers = sorted(nse | bse)

    print(f"{Fore.CYAN}  ✓ Loaded {len(all_tickers)} stock(s) from Zerodha portfolio "
          f"(NSE: {len(nse)}, BSE-only: {len(bse)}){Style.RESET_ALL}")
    return all_tickers


# ── Nifty 50 fallback list (used when live fetch fails) ────────────────────
NIFTY_50_FALLBACK = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "HCLTECH",
    "SUNPHARMA", "TITAN", "BAJFINANCE", "ULTRACEMCO", "WIPRO",
    "NESTLEIND", "POWERGRID", "NTPC", "ONGC", "TATAMOTORS",
    "JSWSTEEL", "TATASTEEL", "HDFCLIFE", "BAJAJFINSV", "TECHM",
    "DRREDDY", "CIPLA", "EICHERMOT", "BPCL", "HEROMOTOCO",
    "DIVISLAB", "GRASIM", "ADANIENT", "ADANIPORTS", "COALINDIA",
    "BRITANNIA", "SBILIFE", "SHRIRAMFIN", "TATACONSUM", "APOLLOHOSP",
    "BAJAJ-AUTO", "HINDALCO", "UPL", "INDUSINDBK", "M&M",
]


def fetch_nifty50_tickers() -> list[str]:
    """Fetch live Nifty 50 constituents from NSE India. Falls back to hardcoded list on error."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
        }
        resp = requests.get(
            "https://www.nseindia.com/api/market-data-pre-open?key=NIFTY",
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        tickers = [
            item["metadata"]["symbol"]
            for item in data.get("data", [])
            if item.get("metadata", {}).get("symbol", "")
        ]
        if len(tickers) >= 45:   # sanity check — index should have 50 stocks
            print(f"{Fore.CYAN}  ✓ Fetched {len(tickers)} tickers live from NSE India{Style.RESET_ALL}")
            return tickers
        raise ValueError(f"Unexpected ticker count: {len(tickers)}")
    except Exception as exc:
        print(f"{Fore.YELLOW}  ⚠  Live fetch failed ({exc.__class__.__name__}: {exc}), "
              f"using cached Nifty 50 list{Style.RESET_ALL}")
    return list(NIFTY_50_FALLBACK)

INTERVAL_PERIOD = {
    "1m":  "5d",
    "5m":  "60d",
    "15m": "60d",
    "1h":  "180d",
    "1d":  "1y",
    "1wk": "5y",
}

# Kite Connect equivalents
_KITE_INTERVAL = {
    "1m":  "minute",
    "5m":  "5minute",
    "15m": "15minute",
    "1h":  "60minute",
    "1d":  "day",
    "1wk": "week",
}
_PERIOD_DAYS = {
    "5d":   5,
    "60d":  60,
    "180d": 180,
    "1y":   365,
    "5y":   1825,
}


# ── Data fetch ──────────────────────────────────────────────────────────────
MIN_ROWS = 50   # minimum candles required for reliable indicator calculation

def _yf_download(symbol: str, period: str, interval: str,
                  retries: int = 3, backoff: float = 5.0) -> pd.DataFrame:
    """Wrapper around yf.download with retry on rate-limit errors."""
    for attempt in range(retries):
        try:
            df = yf.download(symbol, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            return df
        except Exception as exc:
            if "rate" in str(exc).lower() or "429" in str(exc):
                wait = backoff * (2 ** attempt)
                time.sleep(wait)
            else:
                raise
    return yf.download(symbol, period=period, interval=interval,
                       progress=False, auto_adjust=True)


def _yf_download_range(symbol: str, start: str, interval: str) -> pd.DataFrame:
    """Download using explicit start/end dates — works for stocks with limited period support."""
    try:
        df = yf.download(symbol, start=start, interval=interval,
                         progress=False, auto_adjust=True)
        return df
    except Exception:
        return pd.DataFrame()


def fetch_data(ticker: str, interval: str = "1d") -> pd.DataFrame | None:
    period = INTERVAL_PERIOD.get(interval, "1y")
    ns_rows = bo_rows = 0

    # Build candidate symbols: also try without -SM suffix for NSE SME stocks
    candidates_ns = [ticker + ".NS"]
    candidates_bo = [ticker + ".BO"]
    if ticker.upper().endswith("-SM"):
        base = ticker[:-3]
        candidates_ns.append(base + ".NS")
        candidates_bo.append(base + ".BO")

    # Periods to try in order (skip 'max' — not supported for all symbols)
    extra_periods = ["2y", "5y"] if interval in ("1d", "1wk") else []

    def _try_symbols(symbols):
        for sym in symbols:
            for per in [period] + extra_periods:
                df = _yf_download(sym, per, interval)
                if df is not None and not df.empty and len(df) >= MIN_ROWS:
                    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                    df.dropna(inplace=True)
                    return df
            # Fallback: explicit start date (bypasses period restriction for new listings)
            if interval in ("1d", "1wk"):
                df = _yf_download_range(sym, "2024-01-01", interval)
                if df is not None and not df.empty and len(df) >= MIN_ROWS:
                    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                    df.dropna(inplace=True)
                    return df
        # Return best partial result for row count reporting
        best_df = None
        for sym in symbols:
            df = _yf_download(sym, period, interval)
            if df is not None and not df.empty:
                if best_df is None or len(df) > len(best_df):
                    best_df = df
        return best_df  # may be < MIN_ROWS

    try:
        df = _try_symbols(candidates_ns)
        if df is not None and not df.empty and len(df) >= MIN_ROWS:
            return df
        ns_rows = 0 if (df is None or df.empty) else len(df)

        df = _try_symbols(candidates_bo)
        if df is not None and not df.empty and len(df) >= MIN_ROWS:
            return df
        bo_rows = 0 if (df is None or df.empty) else len(df)

        # NSE direct API fallback (for SME/Emerge stocks not on Yahoo Finance)
        if interval == "1d":
            nse_df = _fetch_nse_data(ticker)
            if nse_df is not None and len(nse_df) >= MIN_ROWS:
                return nse_df
            if nse_df is not None and len(nse_df) > max(ns_rows, bo_rows):
                ns_rows = len(nse_df)   # update so NewlyListedError carries correct count

        # Try Kite Connect as final fallback
        kite_df = _fetch_kite_data(ticker, interval)
        if kite_df is not None:
            return kite_df

        best = max(ns_rows, bo_rows)
        if best > 0:
            raise _NewlyListedError(ticker, best)
        return None
    except _NewlyListedError:
        raise
    except Exception:
        return None


def _fetch_nse_data(ticker: str) -> pd.DataFrame | None:
    """Fetch daily OHLCV from NSE India historical API — fallback for SME/Emerge stocks."""
    try:
        from datetime import timedelta
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
        }
        session = requests.Session()
        # Warm up session cookie (required by NSE)
        session.get("https://www.nseindia.com", headers=headers, timeout=10)

        sym = ticker.upper().replace("-SM", "")   # NSE API uses bare symbol
        to_dt   = date.today()
        from_dt = to_dt - timedelta(days=730)     # 2 years
        url = (
            f"https://www.nseindia.com/api/historical/cm/equity"
            f'?symbol={sym}&series=["EQ"]'
            f"&from={from_dt.strftime('%d-%m-%Y')}"
            f"&to={to_dt.strftime('%d-%m-%Y')}"
        )
        resp = session.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        rows = resp.json().get("data", [])
        if not rows:
            return None

        records = []
        for r in rows:
            try:
                records.append({
                    "Date":   pd.to_datetime(r["CH_TIMESTAMP"]),
                    "Open":   float(r["CH_OPENING_PRICE"]),
                    "High":   float(r["CH_TRADE_HIGH_PRICE"]),
                    "Low":    float(r["CH_TRADE_LOW_PRICE"]),
                    "Close":  float(r["CH_CLOSING_PRICE"]),
                    "Volume": float(r["CH_TOT_TRADED_QTY"]),
                })
            except (KeyError, ValueError):
                continue

        if not records:
            return None

        df = pd.DataFrame(records).set_index("Date").sort_index()
        df.dropna(inplace=True)
        return df
    except Exception:
        return None


class _NewlyListedError(Exception):
    """Raised when a ticker exists on Yahoo Finance but has too little history."""
    def __init__(self, ticker: str, rows: int):
        self.ticker = ticker
        self.rows   = rows


def _fetch_kite_data(ticker: str, interval: str) -> pd.DataFrame | None:
    """Fetch OHLCV history from Kite Connect — used as fallback when Yahoo lacks data."""
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        return None

    # Need a valid token saved from today's login
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE) as f:
            token_data = json.load(f)
        if token_data.get("date") != str(date.today()):
            return None
        access_token = token_data["access_token"]
    except Exception:
        return None

    # Need a configured api_key
    if not os.path.exists(CONFIG_FILE):
        return None
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    try:
        api_key = cfg["kite"]["api_key"].strip()
        if api_key in ("", "YOUR_API_KEY"):
            return None
    except Exception:
        return None

    # Strip Yahoo-style exchange suffixes to get the bare NSE/BSE symbol
    base = ticker
    for sfx in (".NS", ".BO"):
        if base.upper().endswith(sfx):
            base = base[: -len(sfx)]
            break

    period   = INTERVAL_PERIOD.get(interval, "1y")
    days     = _PERIOD_DAYS.get(period, 365)
    to_dt    = datetime.now()
    from_dt  = to_dt - timedelta(days=days)
    kite_int = _KITE_INTERVAL.get(interval, "day")

    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        # Resolve instrument token — try NSE first, then BSE
        instrument_token = None
        for exchange in ("NSE", "BSE"):
            key = f"{exchange}:{base}"
            try:
                ltp_data = kite.ltp([key])
                if ltp_data and key in ltp_data:
                    instrument_token = ltp_data[key]["instrument_token"]
                    break
            except Exception:
                continue

        if instrument_token is None:
            return None

        candles = kite.historical_data(
            instrument_token,
            from_date=from_dt,
            to_date=to_dt,
            interval=kite_int,
        )
        if not candles or len(candles) < MIN_ROWS:
            return None

        df = pd.DataFrame(candles)
        df.rename(columns={
            "date": "Date", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        }, inplace=True)
        df.set_index("Date", inplace=True)
        df.dropna(inplace=True)
        return df if len(df) >= MIN_ROWS else None
    except Exception:
        return None

# ── Support / Resistance detection ──────────────────────────────────────────
def find_support_resistance(df: pd.DataFrame, price: float, atr: float) -> dict:
    """
    Identify key support and resistance zones using three methods:
      1. Swing highs / lows  (last 120 candles, window = 5)
      2. Classic pivot points (based on previous candle H/L/C)
      3. Psychological round numbers (₹10 / ₹50 / ₹100 / ₹500 / ₹1000 steps)
    Returns the nearest resistance above price and nearest support below price,
    plus up to 3 levels on each side.
    """
    highs = df["High"].values
    lows  = df["Low"].values

    levels = []   # list of (label, price)

    # 1. Swing highs / lows
    lookback = min(120, len(df) - 6)
    window   = 5
    for i in range(window, lookback):
        idx = len(df) - lookback + i
        if idx < window or idx + window >= len(df):
            continue
        if highs[idx] == max(highs[idx - window: idx + window + 1]):
            levels.append(("swing_high", float(highs[idx])))
        if lows[idx] == min(lows[idx - window: idx + window + 1]):
            levels.append(("swing_low", float(lows[idx])))

    # 2. Classic pivot points from the last complete candle
    ph    = float(df["High"].iloc[-2])
    pl    = float(df["Low"].iloc[-2])
    pc    = float(df["Close"].iloc[-2])
    pivot = (ph + pl + pc) / 3
    for lvl in [pivot,
                2 * pivot - pl,          # R1
                pivot + (ph - pl),       # R2
                2 * pivot - ph,          # S1
                pivot - (ph - pl)]:      # S2
        levels.append(("pivot", float(lvl)))

    # 3. Round number levels
    if price < 100:
        step = 10
    elif price < 500:
        step = 50
    elif price < 2000:
        step = 100
    elif price < 10000:
        step = 500
    else:
        step = 1000
    base = round(price / step) * step
    for m in range(-4, 5):
        lvl = base + m * step
        if lvl > 0:
            levels.append(("round", float(lvl)))

    max_range = price * 0.22

    resistances = sorted({lvl for _, lvl in levels if price * 1.002 < lvl <= price + max_range})
    supports    = sorted({lvl for _, lvl in levels if price - max_range <= lvl < price * 0.998},
                         reverse=True)

    # Cluster levels that are within 0.8% of each other (same zone)
    def cluster(lst: list) -> list:
        if not lst:
            return []
        out = [lst[0]]
        for v in lst[1:]:
            if abs(v - out[-1]) / price > 0.008:
                out.append(v)
        return out

    resistances = cluster(resistances)
    supports    = cluster(list(reversed(cluster(list(reversed(supports))))))

    # Score levels by how many times price has tested them (more touches = stronger zone)
    def touch_count(level: float, all_highs, all_lows, tolerance: float) -> int:
        touches = 0
        for h, l in zip(all_highs, all_lows):
            if abs(h - level) / price < tolerance or abs(l - level) / price < tolerance:
                touches += 1
        return touches

    tol = 0.012  # 1.2% tolerance for a "touch"
    resistances = sorted(resistances,
                         key=lambda v: -touch_count(v, highs, lows, tol))[:3]
    supports    = sorted(supports,
                         key=lambda v: -touch_count(v, highs, lows, tol))[:3]

    return {
        "resistance":  resistances,
        "support":     supports,
        "nearest_res": min(resistances, key=lambda v: v - price) if resistances else None,
        "nearest_sup": max(supports,    key=lambda v: v)         if supports    else None,
    }

# ── Indicator calculation (pure pandas/numpy — no external ta library) ────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    vol   = df["Volume"]
    open_ = df["Open"]

    # ── RSI (14) ──────────────────────────────────────────────────────────────
    delta     = close.diff()
    gain      = delta.clip(lower=0)
    loss      = (-delta).clip(lower=0)
    avg_gain  = gain.ewm(com=13, adjust=False).mean()
    avg_loss  = loss.ewm(com=13, adjust=False).mean()
    rs        = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"]        = 100 - (100 / (1 + rs))
    df["RSI_smooth"] = df["RSI"].ewm(span=3).mean()

    # ── MACD (12 / 26 / 9) ───────────────────────────────────────────────────
    ema12             = close.ewm(span=12, adjust=False).mean()
    ema26             = close.ewm(span=26, adjust=False).mean()
    df["MACD"]        = ema12 - ema26
    df["MACD_sig"]    = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"]   = df["MACD"] - df["MACD_sig"]
    df["MACD_hist_slope"] = df["MACD_hist"].diff()

    # ── Moving averages ───────────────────────────────────────────────────────
    df["EMA9"]   = close.ewm(span=9,   adjust=False).mean()
    df["EMA20"]  = close.ewm(span=20,  adjust=False).mean()
    df["EMA50"]  = close.ewm(span=50,  adjust=False).mean()
    df["SMA200"] = close.rolling(200).mean()

    # ── Bollinger Bands (20, 2) ───────────────────────────────────────────────
    sma20          = close.rolling(20).mean()
    std20          = close.rolling(20).std()
    df["BB_mid"]   = sma20
    df["BB_up"]    = sma20 + 2 * std20
    df["BB_low"]   = sma20 - 2 * std20
    band_width     = df["BB_up"] - df["BB_low"]
    df["BB_pct"]   = (close - df["BB_low"]) / band_width.replace(0, float("nan"))
    df["BB_width"] = band_width / sma20.replace(0, float("nan"))

    # ── ATR (14) ──────────────────────────────────────────────────────────────
    tr             = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    df["ATR"]      = tr.ewm(com=13, adjust=False).mean()
    df["ATR_pct"]  = df["ATR"] / close * 100

    # ── ADX (14) ──────────────────────────────────────────────────────────────
    up_move   = high.diff()
    down_move = (-low.diff())
    plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    atr14     = tr.ewm(com=13, adjust=False).mean()
    plus_di   = 100 * plus_dm.ewm(com=13,  adjust=False).mean() / atr14.replace(0, float("nan"))
    minus_di  = 100 * minus_dm.ewm(com=13, adjust=False).mean() / atr14.replace(0, float("nan"))
    dx        = (100 * (plus_di - minus_di).abs() /
                 (plus_di + minus_di).replace(0, float("nan")))
    df["ADX"]     = dx.ewm(com=13, adjust=False).mean()
    df["ADX_pos"] = plus_di
    df["ADX_neg"] = minus_di

    # ── Stochastic RSI ────────────────────────────────────────────────────────
    rsi_low   = df["RSI"].rolling(14).min()
    rsi_high  = df["RSI"].rolling(14).max()
    stoch_rsi = ((df["RSI"] - rsi_low) /
                 (rsi_high - rsi_low).replace(0, float("nan")))
    df["StochRSI_K"] = stoch_rsi.rolling(3).mean()
    df["StochRSI_D"] = df["StochRSI_K"].rolling(3).mean()

    # ── Williams %R (14) ─────────────────────────────────────────────────────
    hh = high.rolling(14).max()
    ll = low.rolling(14).min()
    df["WilliamsR"] = -100 * (hh - close) / (hh - ll).replace(0, float("nan"))

    # ── Volume ────────────────────────────────────────────────────────────────
    df["Vol_avg"]   = vol.rolling(20).mean()
    df["Vol_ratio"] = vol / df["Vol_avg"].replace(0, float("nan"))
    direction       = close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    df["OBV"]       = (vol * direction).cumsum()
    df["OBV_ema"]   = df["OBV"].ewm(span=20).mean()

    # ── Candle body analysis ──────────────────────────────────────────────────
    df["Body"]       = (close - open_).abs()
    df["Upper_wick"] = high  - close.where(close >= open_, open_)
    df["Lower_wick"] = close.where(close >= open_, open_) - low
    df["Is_doji"]    = df["Body"] < (df["ATR"] * 0.1)

    return df


# ── Timeline helper ─────────────────────────────────────────────────────────
def candles_to_timestr(candles: float, interval: str) -> str:
    """Convert estimated candle count to a human-readable time string."""
    c = max(1, round(candles))
    if interval == "1wk":
        if c < 5:
            return f"~{c} wk{'s' if c > 1 else ''}"
        return f"~{round(c / 4.3):.0f} mo"
    elif interval == "1d":
        cal = round(c * 7 / 5)          # trading days → calendar days
        if cal <= 14:  return f"~{cal} day{'s' if cal > 1 else ''}"
        if cal <= 60:  return f"~{round(cal/7)} wks"
        return f"~{round(cal/30)} mo"
    elif interval == "1h":
        if c < 7:   return f"~{c} hr{'s' if c > 1 else ''}"
        return f"~{round(c / 6.5)} day{'s' if round(c/6.5) > 1 else ''}"
    elif interval in ("15m", "5m", "1m"):
        mins = c * {"15m": 15, "5m": 5, "1m": 1}[interval]
        if mins < 60:    return f"~{mins} min"
        if mins < 390:   return f"~{round(mins/60)} hrs"
        return f"~{round(mins/390)} day{'s' if round(mins/390) > 1 else ''}"
    return f"~{c} candles"


# ── Signal logic ─────────────────────────────────────────────────────────────
def generate_signal(df: pd.DataFrame, interval: str = "1d") -> dict:
    r = df.iloc[-1]   # latest candle
    p = df.iloc[-2]   # previous candle

    buy_pts  = 0
    sell_pts = 0
    buy_reasons  = []
    sell_reasons = []

    price = float(r["Close"])
    adx   = float(r["ADX"]) if not pd.isna(r["ADX"]) else 15.0
    trend_strong = adx > 25

    # 1. RSI
    rsi = float(r["RSI"])
    if rsi < 35:
        buy_pts += 2
        buy_reasons.append(f"RSI oversold ({rsi:.1f})")
    elif rsi > 65:
        sell_pts += 2
        sell_reasons.append(f"RSI overbought ({rsi:.1f})")

    # 2. MACD crossover
    if float(p["MACD"]) < float(p["MACD_sig"]) and float(r["MACD"]) > float(r["MACD_sig"]):
        buy_pts += 2
        buy_reasons.append("MACD bullish crossover")
    elif float(p["MACD"]) > float(p["MACD_sig"]) and float(r["MACD"]) < float(r["MACD_sig"]):
        sell_pts += 2
        sell_reasons.append("MACD bearish crossover")
    elif float(r["MACD_hist"]) > 0:
        buy_pts += 1
    else:
        sell_pts += 1

    # 3. Moving average alignment
    ema20 = float(r["EMA20"])
    ema50 = float(r["EMA50"])
    if price > ema20 > ema50:
        buy_pts += 2
        buy_reasons.append("Price above EMA20 > EMA50 (uptrend)")
    elif price < ema20 < ema50:
        sell_pts += 2
        sell_reasons.append("Price below EMA20 < EMA50 (downtrend)")

    if not pd.isna(r["SMA200"]):
        if price > float(r["SMA200"]):
            buy_pts += 1
        else:
            sell_pts += 1

    # 4. Bollinger Bands
    if price <= float(r["BB_low"]):
        buy_pts += 1
        buy_reasons.append("Price at lower Bollinger Band (oversold)")
    elif price >= float(r["BB_up"]):
        sell_pts += 1
        sell_reasons.append("Price at upper Bollinger Band (overbought)")

    # 5. Volume confirmation
    vol_ratio = float(r["Vol_ratio"]) if not pd.isna(r["Vol_ratio"]) else 1.0
    if vol_ratio > 1.5:
        if buy_pts > sell_pts:
            buy_pts += 1
            buy_reasons.append(f"High volume ({vol_ratio:.1f}x avg) confirms BUY")
        elif sell_pts > buy_pts:
            sell_pts += 1
            sell_reasons.append(f"High volume ({vol_ratio:.1f}x avg) confirms SELL")

    # ── Price projections ─────────────────────────────────────────────────────
    atr_val = float(r["ATR"]) if (not pd.isna(r["ATR"]) and r["ATR"] > 0) else price * 0.01
    sr          = find_support_resistance(df, price, atr_val)
    nearest_res = sr["nearest_res"]
    nearest_sup = sr["nearest_sup"]

    if nearest_res:
        proj_up     = nearest_res
        proj_up_src = f"R ₹{nearest_res:,.2f}"
        if nearest_sup and (price - nearest_sup) / atr_val < 1.0:
            buy_pts += 1
            buy_reasons.append(f"Near support ₹{nearest_sup:,.2f} (bounce zone)")
    else:
        proj_up     = price + 2.0 * atr_val
        proj_up_src = "ATR"

    if nearest_sup:
        proj_down     = nearest_sup
        proj_down_src = f"S ₹{nearest_sup:,.2f}"
        if nearest_res and (nearest_res - price) / atr_val < 0.5:
            sell_pts += 1
            sell_reasons.append(f"Near resistance ₹{nearest_res:,.2f} (rejection risk)")
    else:
        proj_down     = price - 1.5 * atr_val
        proj_down_src = "ATR"

    proj_up_pct   = (proj_up   - price) / price * 100
    proj_down_pct = (proj_down - price) / price * 100

    # Timeline
    momentum_factor = 0.15 + min(adx / 100, 0.30)
    candles_up      = (proj_up - price) / (atr_val * momentum_factor)
    proj_timeline   = candles_to_timestr(candles_up, interval)

    # ── Final verdict ─────────────────────────────────────────────────────────
    total = buy_pts + sell_pts
    score = (buy_pts / total * 100) if total > 0 else 50

    if score >= 65:
        signal = "BUY"
    elif score <= 35:
        signal = "SELL"
    else:
        signal = "HOLD"

    if signal == "BUY":
        reasons = buy_reasons
        if sell_reasons:
            reasons = reasons + [f"⚠ Against: {', '.join(sell_reasons)}"]
    elif signal == "SELL":
        reasons = sell_reasons
        if buy_reasons:
            reasons = reasons + [f"⚠ Against: {', '.join(buy_reasons)}"]
    else:
        reasons = ([f"▲ {r_}" for r_ in buy_reasons] +
                   [f"▼ {r_}" for r_ in sell_reasons])

    return {
        "price":          round(price, 2),
        "signal":         signal,
        "score":          round(score, 1),
        "buy_pts":        buy_pts,
        "sell_pts":       sell_pts,
        "rsi":            round(rsi, 1),
        "adx":            round(adx, 1) if not pd.isna(r["ADX"]) else None,
        "trend_strong":   trend_strong,
        "vol_ratio":      round(vol_ratio, 2),
        "reasons":        reasons,
        "proj_up":        round(proj_up, 2),
        "proj_down":      round(proj_down, 2),
        "proj_up_pct":    round(proj_up_pct, 1),
        "proj_down_pct":  round(proj_down_pct, 1),
        "proj_timeline":  proj_timeline,
        "proj_up_src":    proj_up_src,
        "proj_down_src":  proj_down_src,
        "resistances":    sr["resistance"],
        "supports":       sr["support"],
        "summary":        _build_summary(signal, score, rsi, adx, vol_ratio,
                                         trend_strong, proj_up_pct, proj_down_pct,
                                         proj_timeline, nearest_res, nearest_sup,
                                         buy_reasons, sell_reasons),
    }


def _build_summary(signal, score, rsi, adx, vol_ratio, trend_strong,
                   proj_up_pct, proj_down_pct, proj_timeline,
                   nearest_res, nearest_sup, buy_reasons, sell_reasons) -> str:
    """Plain-English explanation of the signal for non-technical readers."""
    parts = []

    # Trend
    if trend_strong:
        parts.append("The stock is in a strong trend")
    else:
        parts.append("The stock is moving sideways without a clear trend")

    # RSI
    if rsi < 35:
        parts.append(f"and is oversold (RSI {rsi:.0f}), meaning it may have fallen too far and could bounce back")
    elif rsi > 65:
        parts.append(f"and is overbought (RSI {rsi:.0f}), meaning it has risen sharply and may be due for a pullback")
    else:
        parts.append(f"with neutral momentum (RSI {rsi:.0f})")

    # Volume
    if vol_ratio > 1.5:
        parts.append(f"Trading volume is unusually high ({vol_ratio:.1f}× average), confirming the move.")
    elif vol_ratio < 0.5:
        parts.append("Volume is low, so the move may lack conviction.")

    # Key reasons in plain English
    reason_map = {
        "Price above EMA20 > EMA50 (uptrend)":        "Short-term averages are stacked bullishly.",
        "Price below EMA20 < EMA50 (downtrend)":      "Short-term averages are stacked bearishly.",
        "MACD bullish crossover":                      "Momentum has just turned upward (MACD crossover).",
        "MACD bearish crossover":                      "Momentum has just turned downward (MACD crossover).",
        "Price at lower Bollinger Band (oversold)":    "Price is at the lower edge of its normal range.",
        "Price at upper Bollinger Band (overbought)":  "Price is at the upper edge of its normal range.",
    }
    active_reasons = buy_reasons if signal in ("BUY", "HOLD") else sell_reasons
    for raw in active_reasons:
        plain = next((v for k, v in reason_map.items() if k in raw), None)
        if plain:
            parts.append(plain)

    # Target / stop
    if signal == "BUY":
        parts.append(
            f"If the stock moves as expected, it could rise ~{proj_up_pct:.1f}% "
            f"in {proj_timeline}. Risk on the downside is ~{abs(proj_down_pct):.1f}%."
        )
        if nearest_res:
            parts.append(f"The nearest resistance (sell zone) is ₹{nearest_res:,.2f}.")
        if nearest_sup:
            parts.append(f"A good stop-loss level would be around ₹{nearest_sup:,.2f}.")
    elif signal == "SELL":
        parts.append(
            f"The stock could decline ~{abs(proj_down_pct):.1f}% from here. "
            f"Upside risk if wrong is ~{proj_up_pct:.1f}%."
        )
        if nearest_res:
            parts.append(f"Resistance above at ₹{nearest_res:,.2f} is likely to cap any bounce.")
    else:
        parts.append(
            "There are mixed signals — no clear edge in either direction right now. "
            "It may be safer to wait for a clearer setup before taking a position."
        )

    return " ".join(parts)


# ── Colour helpers ────────────────────────────────────────────────────────────
def colour_signal(sig: str) -> str:
    if sig == "BUY":
        return Fore.GREEN + Style.BRIGHT + sig + Style.RESET_ALL
    elif sig == "SELL":
        return Fore.RED + Style.BRIGHT + sig + Style.RESET_ALL
    return Fore.YELLOW + sig + Style.RESET_ALL


def colour_rsi(rsi: float) -> str:
    if rsi < 35:
        return Fore.GREEN + f"{rsi:.1f}" + Style.RESET_ALL
    elif rsi > 65:
        return Fore.RED + f"{rsi:.1f}" + Style.RESET_ALL
    return f"{rsi:.1f}"


# ── Main scan ─────────────────────────────────────────────────────────────────
def scan(tickers: list[str], interval: str, top: int | None) -> None:
    print(f"\n{Fore.CYAN}{'═'*65}")
    print(f"  📈  NIFTY STOCK SIGNAL SCANNER  |  {datetime.now().strftime('%d %b %Y  %H:%M')}")
    print(f"  Interval: {interval}  |  Stocks: {len(tickers)}")
    print(f"{'═'*65}{Style.RESET_ALL}\n")

    results = []
    errors  = []

    newly_listed: list[str] = []
    for i, ticker in enumerate(tickers, 1):
        print(f"  Fetching {ticker:15s} ({i}/{len(tickers)})...", end="\r")
        try:
            df = fetch_data(ticker, interval)
        except _NewlyListedError as e:
            newly_listed.append((e.ticker, e.rows))
            continue
        if df is None:
            errors.append(ticker)
            continue
        df = compute_indicators(df)
        sig = generate_signal(df, interval)
        sig["ticker"] = ticker
        results.append(sig)

    print(" " * 50, end="\r")  # clear progress line

    for tkr, rows in newly_listed:
        hint = ""
        if tkr.upper().endswith("-SM"):
            hint = " (NSE SME/Emerge stock — Yahoo Finance has no history. Login to Zerodha and retry.)"
        print(Fore.YELLOW + f"  ⚠  {tkr}: newly listed — only {rows} candle(s) available, "
              f"need {MIN_ROWS}+ for analysis.{hint}" + Style.RESET_ALL)

    if not results:
        if newly_listed and not errors:
            print(Fore.YELLOW + "  No analysis possible: all requested stocks are newly listed "
                  "with insufficient price history." + Style.RESET_ALL)
        else:
            print(Fore.RED + "No data could be fetched. Check your internet connection.")
        return

    # Sort: BUY first (by score desc), then HOLD, then SELL
    order = {"BUY": 0, "HOLD": 1, "SELL": 2}
    results.sort(key=lambda x: (order[x["signal"]], -x["score"]))

    if top:
        buy_results = [r for r in results if r["signal"] == "BUY"]
        results = buy_results[:top]

    # Build table
    rows = []
    for r in results:
        up_col   = Fore.GREEN if r["proj_up_pct"]   > 0 else Fore.RED
        down_col = Fore.RED   if r["proj_down_pct"] < 0 else Fore.GREEN
        proj_str = (f"{up_col}↑{r['proj_up_pct']:+.1f}% ₹{r['proj_up']:,.2f} "
                    f"[{r['proj_up_src']}] ({r['proj_timeline']}){Style.RESET_ALL} / "
                    f"{down_col}↓{r['proj_down_pct']:+.1f}% ₹{r['proj_down']:,.2f} "
                    f"[{r['proj_down_src']}]{Style.RESET_ALL}")
        rows.append([
            r["ticker"],
            f"₹{r['price']:,.2f}",
            colour_signal(r["signal"]),
            f"{r['score']:.0f}/100",
            colour_rsi(r["rsi"]),
            str(r["adx"]) if r["adx"] else "—",
            f"{r['vol_ratio']}x",
            "✅ Strong" if r["trend_strong"] else "Weak",
            proj_str,
        ])

    headers = ["Ticker", "Price (₹)", "Signal", "Score", "RSI", "ADX", "Vol Ratio", "Trend", "Projection"]
    print(tabulate(rows, headers=headers, tablefmt="rounded_outline"))

    # Print reasons for BUY/SELL
    print()
    for r in results:
        if r["signal"] in ("BUY", "SELL") and r["reasons"]:
            col = Fore.GREEN if r["signal"] == "BUY" else Fore.RED
            print(f"{col}  {r['signal']} {r['ticker']:12s}{Style.RESET_ALL}  →  {' | '.join(r['reasons'])}")
            print(f"       {'Target':8s} {Fore.GREEN}↑ ₹{r['proj_up']:,.2f} ({r['proj_up_pct']:+.1f}%, {r['proj_timeline']}) [{r['proj_up_src']}]{Style.RESET_ALL}   "
                  f"Stop/Risk {Fore.RED}↓ ₹{r['proj_down']:,.2f} ({r['proj_down_pct']:+.1f}%) [{r['proj_down_src']}]{Style.RESET_ALL}")
            if r["resistances"]:
                res_str = "  ".join(f"₹{v:,.2f}" for v in r["resistances"])
                print(f"       {Fore.YELLOW}Resistance zones: {res_str}{Style.RESET_ALL}")
            if r["supports"]:
                sup_str = "  ".join(f"₹{v:,.2f}" for v in r["supports"])
                print(f"       {Fore.CYAN}Support zones:    {sup_str}{Style.RESET_ALL}")
            if r.get("summary"):
                print(f"       {Fore.WHITE}💬 {r['summary']}{Style.RESET_ALL}")

    # Summary
    buys  = sum(1 for r in results if r["signal"] == "BUY")
    sells = sum(1 for r in results if r["signal"] == "SELL")
    holds = sum(1 for r in results if r["signal"] == "HOLD")
    print(f"\n  Summary: {Fore.GREEN}BUY {buys}{Style.RESET_ALL}  |  "
          f"{Fore.YELLOW}HOLD {holds}{Style.RESET_ALL}  |  "
          f"{Fore.RED}SELL {sells}{Style.RESET_ALL}")

    if errors:
        print(f"\n  {Fore.RED}Could not fetch: {', '.join(errors)}{Style.RESET_ALL}")

    print(f"\n{Fore.CYAN}{'─'*65}{Style.RESET_ALL}")
    print("  ⚠️  This tool is for educational purposes only.")
    print("  Always do your own research before trading.\n")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Nifty 50 Buy/Sell Signal Generator"
    )
    parser.add_argument(
        "--zerodha", action="store_true",
        help="Fetch and analyse stocks from your Zerodha portfolio"
    )
    parser.add_argument(
        "--zerodha-login", action="store_true",
        help="One-time daily Zerodha login to generate an access token"
    )
    parser.add_argument(
        "--stocks", nargs="+", metavar="TICKER",
        help="Specific NSE tickers (without .NS), e.g. RELIANCE TCS INFY"
    )
    parser.add_argument(
        "--top", type=int, default=None,
        help="Show only top N BUY signals"
    )
    parser.add_argument(
        "--interval", default="1d",
        choices=["1m", "5m", "15m", "1h", "1d", "1wk"],
        help="Candle interval (default: 1d)"
    )
    args = parser.parse_args()

    if args.zerodha_login:
        zerodha_login()
        return

    if args.zerodha:
        tickers = fetch_zerodha_holdings()
    elif args.stocks:
        tickers = [t.upper() for t in args.stocks]
    else:
        tickers = fetch_nifty50_tickers()

    scan(tickers, args.interval, args.top)


if __name__ == "__main__":
    main()