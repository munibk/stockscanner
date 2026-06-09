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

# ── High-liquidity Nifty stocks recommended for intraday trading ───────────
NIFTY_INTRADAY_STOCKS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "AXISBANK", "SBIN", "KOTAKBANK", "BAJFINANCE", "LT",
    "TATAMOTORS", "WIPRO", "HCLTECH", "NTPC", "POWERGRID",
    "ONGC", "BPCL", "HINDUNILVR", "SUNPHARMA", "DRREDDY",
    "TATASTEEL", "JSWSTEEL", "HINDALCO", "COALINDIA", "M&M",
    "MARUTI", "EICHERMOT", "BAJAJ-AUTO", "HEROMOTOCO", "TITAN",
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


def fetch_all_nse_tickers(include_sme: bool = True) -> list[str]:
    """
    Fetch every NSE-listed equity from the latest available daily bhavcopy.
    Mainboard stocks use the bare symbol (e.g. RELIANCE).
    SME/Emerge stocks get a -SM suffix (e.g. PRANIK-SM) when include_sme=True.
    Falls back to the Nifty 50 fallback list if the download fails.
    """
    import io
    import zipfile

    bhav_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.nseindia.com/",
    }

    for days_back in range(10):          # try up to 10 calendar days back
        dt = date.today() - timedelta(days=days_back)
        if dt.weekday() >= 5:            # skip Saturday / Sunday
            continue
        ds  = dt.strftime("%Y%m%d")
        url = (f"https://nsearchives.nseindia.com/content/cm/"
               f"BhavCopy_NSE_CM_0_0_0_{ds}_F_0000.csv.zip")
        try:
            resp = requests.get(url, headers=bhav_headers, timeout=25)
            if resp.status_code != 200:
                continue
            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                bhav = pd.read_csv(z.open(z.namelist()[0]), dtype=str)

            bhav["SctySrs"]  = bhav["SctySrs"].str.strip().str.upper()
            bhav["TckrSymb"] = bhav["TckrSymb"].str.strip().str.upper()

            tickers: list[str] = []
            tickers.extend(bhav.loc[bhav["SctySrs"] == "EQ", "TckrSymb"].tolist())
            if include_sme:
                tickers.extend(
                    sym + "-SM"
                    for sym in bhav.loc[bhav["SctySrs"] == "SM", "TckrSymb"].tolist()
                )

            tickers = sorted(set(tickers))
            if len(tickers) > 100:       # sanity check — expect 1800+ EQ + 600+ SM
                print(f"{Fore.CYAN}  ✓ Loaded {len(tickers)} NSE tickers "
                      f"from bhavcopy ({dt}){Style.RESET_ALL}")
                return tickers
        except Exception as exc:
            print(f"{Fore.YELLOW}  ⚠ bhavcopy fetch failed "
                  f"({exc.__class__.__name__}), trying previous day{Style.RESET_ALL}")

    print(f"{Fore.YELLOW}  ⚠ Could not fetch full NSE list; "
          f"falling back to Nifty 50{Style.RESET_ALL}")
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


def get_nifty_index_data(interval: str = "1d") -> pd.DataFrame | None:
    """Fetch Nifty 50 index (^NSEI) OHLCV data for dashboard display."""
    try:
        period = INTERVAL_PERIOD.get(interval, "1y")
        df = yf.download("^NSEI", period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is not None and not df.empty:
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            df.dropna(inplace=True)
            return df
    except Exception:
        pass
    return None


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
    """
    Fetch daily OHLCV from NSE India bhavcopy archive files.
    Works for both mainboard (series EQ) and SME/Emerge (series SM) stocks.
    Downloads daily files in parallel — no Cloudflare issues (nsearchives subdomain).
    """
    import io
    import zipfile
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Parse ticker: "PRANIK-SM" → base="PRANIK", series="SM"
    #               "RELIANCE"  → base="RELIANCE", series="EQ"
    if ticker.upper().endswith("-SM"):
        base_sym = ticker[:-3].upper()
        series   = "SM"
    else:
        base_sym = ticker.upper()
        series   = "EQ"

    bhav_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.nseindia.com/",
    }

    def _fetch_one_day(dt: date):
        """Download bhavcopy for one date, return (date, row_dict) or None."""
        ds  = dt.strftime("%Y%m%d")
        url = (f"https://nsearchives.nseindia.com/content/cm/"
               f"BhavCopy_NSE_CM_0_0_0_{ds}_F_0000.csv.zip")
        try:
            r = requests.get(url, headers=bhav_headers, timeout=15)
            if r.status_code != 200:
                return None
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                df = pd.read_csv(z.open(z.namelist()[0]), dtype=str)
            row = df[
                (df["TckrSymb"].str.strip().str.upper() == base_sym) &
                (df["SctySrs"].str.strip().str.upper() == series)
            ]
            if row.empty:
                return None
            r0 = row.iloc[0]
            return {
                "Date":   pd.to_datetime(r0["TradDt"]),
                "Open":   float(r0["OpnPric"]),
                "High":   float(r0["HghPric"]),
                "Low":    float(r0["LwPric"]),
                "Close":  float(r0["ClsPric"]),
                "Volume": float(r0["TtlTradgVol"]),
            }
        except Exception:
            return None

    # Generate candidate trading dates (skip weekends; holidays → 404 → skipped)
    candidate_dates = []
    dt = date.today()
    cutoff = date.today() - timedelta(days=730)   # up to 2 years back
    while dt >= cutoff:
        if dt.weekday() < 5:
            candidate_dates.append(dt)
        dt -= timedelta(days=1)

    records = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_fetch_one_day, d): d for d in candidate_dates}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                records.append(result)

    if not records:
        return None

    df_out = pd.DataFrame(records).set_index("Date").sort_index()
    df_out.dropna(inplace=True)
    return df_out


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

        # Resolve instrument token via instruments dump
        # (ltp() requires a higher-tier Kite subscription; instruments() is always available)
        instrument_token = None
        try:
            instruments = kite.instruments()
            base_upper = base.upper()
            # Exact match first, then without -SM suffix
            candidates = [base_upper, base_upper.replace("-SM", "")]
            for inst in instruments:
                sym = str(inst.get("tradingsymbol", "")).upper()
                exch = str(inst.get("exchange", ""))
                if sym in candidates and exch in ("NSE", "BSE") and inst.get("instrument_type") == "EQ":
                    instrument_token = inst["instrument_token"]
                    break
        except Exception:
            pass

        # Fallback: try ltp() (works on higher-tier plans)
        if instrument_token is None:
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

    # ── Volume ────────────────────────────────────────────────────────────────
    df["Vol_avg"]   = vol.rolling(20).mean()
    df["Vol_ratio"] = vol / df["Vol_avg"].replace(0, float("nan"))
    direction       = close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    df["OBV"]       = (vol * direction).cumsum()
    df["OBV_ema"]   = df["OBV"].ewm(span=20).mean()

    # ── VWAP (cumulative session approximation) ───────────────────────────────
    typical_price  = (high + low + close) / 3
    df["VWAP"]     = (typical_price * vol).cumsum() / vol.replace(0, float("nan")).cumsum()

    # ── Candle body analysis ──────────────────────────────────────────────────
    df["Body"]       = (close - open_).abs()
    df["Upper_wick"] = high  - close.where(close >= open_, open_)
    df["Lower_wick"] = close.where(close >= open_, open_) - low
    df["Is_doji"]    = df["Body"] < (df["ATR"] * 0.1)

    # ── Break of Structure (BoS) ──────────────────────────────────────────────
    _bos_win = 20
    df["BoS_bull"] = close > high.shift(1).rolling(_bos_win).max()
    df["BoS_bear"] = close < low.shift(1).rolling(_bos_win).min()

    # ── RSI Divergence ────────────────────────────────────────────────────────
    _div_win = 14
    df["RSI_bull_div"] = (
        (close <= close.rolling(_div_win).min() * 1.005) &
        (df["RSI"] > df["RSI"].rolling(_div_win).min() + 5)
    )
    df["RSI_bear_div"] = (
        (close >= close.rolling(_div_win).max() * 0.995) &
        (df["RSI"] < df["RSI"].rolling(_div_win).max() - 5)
    )

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
_COOLDOWN_BARS = {
    "1m": 10, "5m": 8, "15m": 6, "1h": 5, "1d": 4, "1wk": 3,
}


def generate_signal(df: pd.DataFrame, interval: str = "1d") -> dict:
    """
    4-layer regime-aware signal generator.

    Layer 1: Regime filter (ADX gate) — classifies market state, gates signal types.
    Layer 2: HTF daily confluence — checks daily EMA20 bias for LTF entries.
    Layer 3: 5-dimension deduplicated scoring — replaces redundant 7-check system.
    Layer 4: ATR-based trade output — dynamic stops, targets, and R:R ratio.
    """
    r = df.iloc[-1]   # latest candle
    p = df.iloc[-2]   # previous candle

    price    = float(r["Close"])
    atr_val  = float(r["ATR"]) if (not pd.isna(r["ATR"]) and r["ATR"] > 0) else price * 0.01
    adx      = float(r["ADX"]) if not pd.isna(r["ADX"]) else 15.0
    vol_ratio = float(r["Vol_ratio"]) if not pd.isna(r["Vol_ratio"]) else 1.0
    rsi      = float(r["RSI"])
    ema20    = float(r["EMA20"])
    ema50    = float(r["EMA50"])
    sma200   = float(r["SMA200"]) if not pd.isna(r["SMA200"]) else None
    atr_pct  = float(r["ATR_pct"]) if not pd.isna(r["ATR_pct"]) else 0.0

    # Layer 4 pre-compute: ATR trade levels
    long_stop  = round(price - atr_val * 1.5, 2)
    long_tgt1  = round(price + atr_val * 2.0, 2)
    long_tgt2  = round(price + atr_val * 3.5, 2)
    long_rr    = round((long_tgt1 - price) / (price - long_stop), 2) if (price - long_stop) > 0 else 0.0
    short_stop = round(price + atr_val * 1.5, 2)
    short_tgt1 = round(price - atr_val * 2.0, 2)
    short_tgt2 = round(price - atr_val * 3.5, 2)
    short_rr   = round((price - short_tgt1) / (short_stop - price), 2) if (short_stop - price) > 0 else 0.0

    # Projections always point away from price (up = ATR×2 above, down = ATR×1.5 below)
    proj_up_base   = long_tgt1
    proj_down_base = long_stop

    def _sr_result():
        return find_support_resistance(df, price, atr_val)

    def _make_result(signal, score, reasons, regime, stop_loss, target1, target2, rr,
                     bos_bull=False, bos_bear=False, rsi_bull_div=False, rsi_bear_div=False,
                     htf_bullish=None, cooldown=False, vwap=None, vwap_pct=None):
        sr          = _sr_result()
        proj_up     = proj_up_base
        proj_down   = proj_down_base
        pu_pct      = round((proj_up   - price) / price * 100, 1)
        pd_pct      = round((proj_down - price) / price * 100, 1)
        mf          = 0.15 + min(adx / 100, 0.30)
        tl          = candles_to_timestr(abs(proj_up - price) / (atr_val * mf), interval)
        buy_rsns    = [x for x in reasons if "⚠" not in x and any(
            kw in x for kw in ["uptrend", "oversold", "above", "bullish", "MACD bullish",
                                "structure — bullish", "divergence — price at new low",
                                "volume", "VWAP (+"])]
        sell_rsns   = [x for x in reasons if "⚠" not in x and any(
            kw in x for kw in ["downtrend", "overbought", "below", "bearish", "MACD bearish",
                                "structure — bearish", "divergence — price at new high"])]
        return {
            "price":         round(price, 2),
            "signal":        signal,
            "score":         round(score, 1),
            "buy_pts":       0,
            "sell_pts":      0,
            "rsi":           round(rsi, 1),
            "adx":           round(adx, 1),
            "trend_strong":  adx > 25,
            "vol_ratio":     round(vol_ratio, 2),
            "reasons":       reasons,
            "regime":        regime,
            "stop_loss":     round(stop_loss, 2),
            "target1":       round(target1, 2),
            "target2":       round(target2, 2),
            "rr_ratio":      round(rr, 2),
            "proj_up":       round(proj_up, 2),
            "proj_down":     round(proj_down, 2),
            "proj_up_pct":   pu_pct,
            "proj_down_pct": pd_pct,
            "proj_timeline": tl,
            "proj_up_src":   "ATR×2",
            "proj_down_src": "ATR×1.5",
            "resistances":   sr["resistance"],
            "supports":      sr["support"],
            "summary":       _build_summary(
                signal, score, rsi, adx, vol_ratio, adx > 25,
                pu_pct, pd_pct, tl,
                sr["nearest_res"], sr["nearest_sup"],
                buy_rsns, sell_rsns,
                regime, bos_bull, bos_bear,
                rsi_bull_div or rsi_bear_div, rr,
            ),
            "vwap":          round(vwap, 2) if vwap is not None else None,
            "vwap_pct":      round(vwap_pct, 2) if vwap_pct is not None else None,
            "atr_pct":       round(atr_pct, 2),
            "bos_bull":      bos_bull,
            "bos_bear":      bos_bear,
            "rsi_bull_div":  rsi_bull_div,
            "rsi_bear_div":  rsi_bear_div,
            "htf_bullish":   htf_bullish,
            "cooldown":      cooldown,
        }

    # ── Volume Veto ───────────────────────────────────────────────────────────
    if vol_ratio < 0.7:
        return _make_result(
            "VOID", 0.0,
            [f"⚠ Volume too thin ({vol_ratio:.2f}x avg < 0.7x) — signal voided"],
            "void", long_stop, long_tgt1, long_tgt2, long_rr,
        )

    # ── Layer 1: Regime Classification ───────────────────────────────────────
    if adx >= 25:
        regime = "uptrend" if (sma200 and price > sma200) else "downtrend"
    elif adx < 20:
        regime = "ranging"
    else:
        regime = "transitional"

    if regime == "transitional":
        return _make_result(
            "HOLD", 50.0,
            [f"ADX in transition zone ({adx:.1f}) — awaiting regime clarity"],
            "transitional", long_stop, long_tgt1, long_tgt2, long_rr,
        )

    # ── Layer 2: HTF Daily Confluence ─────────────────────────────────────────
    htf_bullish = None
    if interval not in ("1d", "1wk"):
        try:
            daily_close = df["Close"].resample("D").last().dropna()
            if len(daily_close) >= 20:
                daily_ema20 = daily_close.ewm(span=20, adjust=False).mean()
                htf_bullish = bool(float(daily_close.iloc[-1]) > float(daily_ema20.iloc[-1]))
        except Exception:
            pass
    else:
        htf_bullish = price > ema20

    # ── Layer 3: 5-Dimension Scoring ─────────────────────────────────────────
    reasons    = []
    buy_score  = 0.0
    sell_score = 0.0

    # Dim 1: Trend Alignment (30%) — MA stack + MACD crossover
    W_TREND = 0.30
    td_buy = td_sell = 0.0
    if price > ema20 > ema50:
        td_buy += 0.6
        reasons.append("Price above EMA20 > EMA50 (uptrend)")
    elif price < ema20 < ema50:
        td_sell += 0.6
        reasons.append("Price below EMA20 < EMA50 (downtrend)")
    if sma200:
        td_buy  += 0.2 if price > sma200 else 0.0
        td_sell += 0.2 if price < sma200 else 0.0
    macd_now  = float(r["MACD"]);     macd_sig_now  = float(r["MACD_sig"])
    macd_prev = float(p["MACD"]);     macd_sig_prev = float(p["MACD_sig"])
    if macd_prev < macd_sig_prev and macd_now > macd_sig_now:
        td_buy += 0.2
        reasons.append("MACD bullish crossover")
    elif macd_prev > macd_sig_prev and macd_now < macd_sig_now:
        td_sell += 0.2
        reasons.append("MACD bearish crossover")
    elif macd_now > macd_sig_now:
        td_buy  += 0.1
    else:
        td_sell += 0.1
    buy_score  += min(td_buy,  1.0) * W_TREND
    sell_score += min(td_sell, 1.0) * W_TREND

    # Dim 2: Market Structure / BoS (25%)
    W_STRUCT = 0.25
    bos_bull = bool(r.get("BoS_bull")) if not pd.isna(r.get("BoS_bull", float("nan"))) else False
    bos_bear = bool(r.get("BoS_bear")) if not pd.isna(r.get("BoS_bear", float("nan"))) else False
    if bos_bull:
        buy_score  += W_STRUCT
        reasons.append("Break of structure — bullish (price above recent 20-bar swing high)")
    elif bos_bear:
        sell_score += W_STRUCT
        reasons.append("Break of structure — bearish (price below recent 20-bar swing low)")

    # Dim 3: Momentum — RSI + divergence (20%)
    W_MOM = 0.20
    rsi_bull_div = bool(r.get("RSI_bull_div")) if not pd.isna(r.get("RSI_bull_div", float("nan"))) else False
    rsi_bear_div = bool(r.get("RSI_bear_div")) if not pd.isna(r.get("RSI_bear_div", float("nan"))) else False
    md_buy = md_sell = 0.0
    if rsi < 35:
        md_buy = 0.7
        reasons.append(f"RSI oversold ({rsi:.1f})")
    elif rsi < 45:
        md_buy = 0.3
    elif rsi > 65:
        md_sell = 0.7
        reasons.append(f"RSI overbought ({rsi:.1f})")
    elif rsi > 55:
        md_sell = 0.3
    if rsi_bull_div:
        md_buy  = min(md_buy  + 0.3, 1.0)
        reasons.append("RSI bullish divergence — price at new low, RSI showing higher low")
    if rsi_bear_div:
        md_sell = min(md_sell + 0.3, 1.0)
        reasons.append("RSI bearish divergence — price at new high, RSI showing lower high")
    buy_score  += md_buy  * W_MOM
    sell_score += md_sell * W_MOM

    # Dim 4: Mean Reversion — BB + VWAP (15%)
    W_MR   = 0.15
    vwap     = None
    vwap_pct = None
    mr_buy = mr_sell = 0.0
    bb_pct = float(r["BB_pct"]) if not pd.isna(r["BB_pct"]) else 0.5
    if bb_pct <= 0.05:
        mr_buy  = 0.6
        reasons.append("Price at lower Bollinger Band (oversold)")
    elif bb_pct <= 0.20:
        mr_buy  = 0.3
    elif bb_pct >= 0.95:
        mr_sell = 0.6
        reasons.append("Price at upper Bollinger Band (overbought)")
    elif bb_pct >= 0.80:
        mr_sell = 0.3
    if "VWAP" in df.columns and not pd.isna(r["VWAP"]):
        vwap     = float(r["VWAP"])
        vwap_pct = (price - vwap) / vwap * 100
        if price > vwap * 1.003:
            mr_buy  = min(mr_buy  + 0.4, 1.0)
            reasons.append(f"Price above VWAP (+{vwap_pct:.1f}%) — bullish")
        elif price < vwap * 0.997:
            mr_sell = min(mr_sell + 0.4, 1.0)
            reasons.append(f"Price below VWAP ({vwap_pct:.1f}%) — bearish")
    buy_score  += mr_buy  * W_MR
    sell_score += mr_sell * W_MR

    # Dim 5: Volume (10%)
    W_VOL    = 0.10
    obv_bull = False
    if "OBV" in df.columns and "OBV_ema" in df.columns:
        obv_now  = float(r["OBV"])
        obv_ema  = float(r["OBV_ema"])
        obv_prev = float(p["OBV"])
        obv_bull = (obv_now > obv_ema) and (obv_now > obv_prev)
    if vol_ratio >= 1.5:
        if obv_bull:
            buy_score  += W_VOL
            reasons.append(f"High volume ({vol_ratio:.1f}x) + OBV trending up — confirms move")
        else:
            sell_score += W_VOL
            reasons.append(f"High volume ({vol_ratio:.1f}x) + OBV trending down — confirms sell pressure")
    elif vol_ratio >= 0.7:
        buy_score  += W_VOL * 0.5 if obv_bull  else 0.0
        sell_score += W_VOL * 0.5 if not obv_bull else 0.0

    # ── Map to 0–100 score ────────────────────────────────────────────────────
    total_dim = buy_score + sell_score
    score_pct = (buy_score / total_dim * 100) if total_dim > 0 else 50.0

    # ── Regime gating (Layer 1 enforcement) ──────────────────────────────────
    if regime == "uptrend" and score_pct <= 35:
        score_pct = 36.0
        reasons.append(f"⚠ SELL suppressed — regime is uptrend (ADX {adx:.1f}, price above SMA200)")
    elif regime == "downtrend" and score_pct >= 65:
        score_pct = 64.0
        reasons.append(f"⚠ BUY suppressed — regime is downtrend (ADX {adx:.1f}, price below SMA200)")
    elif regime == "ranging" and score_pct >= 65 and mr_buy < 0.3:
        score_pct = 64.0
        reasons.append(f"⚠ Trend BUY suppressed in ranging market (ADX {adx:.1f}) — no mean-reversion confirmation")
    elif regime == "ranging" and score_pct <= 35 and mr_sell < 0.3:
        score_pct = 36.0
        reasons.append(f"⚠ Trend SELL suppressed in ranging market (ADX {adx:.1f}) — no mean-reversion confirmation")

    # ── Initial signal from score ─────────────────────────────────────────────
    if score_pct >= 65:
        signal = "BUY"
    elif score_pct <= 35:
        signal = "SELL"
    else:
        signal = "HOLD"

    # ── Signal Cooldown ───────────────────────────────────────────────────────
    cooldown = False
    cd_bars  = _COOLDOWN_BARS.get(interval, 5)
    if signal in ("BUY", "SELL") and len(df) > cd_bars + 3:
        hist = df["MACD_hist"].iloc[-(cd_bars + 2):-1]
        # A recent MACD crossover (within last 3 bars) makes the signal fresh
        recent_cross = any(
            (df["MACD_hist"].iloc[-i - 1] < 0 < df["MACD_hist"].iloc[-i]) or
            (df["MACD_hist"].iloc[-i - 1] > 0 > df["MACD_hist"].iloc[-i])
            for i in range(2, min(5, len(df)))
        )
        if not recent_cross:
            if signal == "BUY"  and (hist > 0).all():
                cooldown = True
            elif signal == "SELL" and (hist < 0).all():
                cooldown = True
        if cooldown:
            signal = "HOLD"
            reasons.append(f"⚠ Signal cooldown — no fresh MACD crossover in last {cd_bars} bars (stale signal)")

    # ── Layer 2: HTF Filter ───────────────────────────────────────────────────
    if htf_bullish is not None:
        if signal == "BUY" and not htf_bullish:
            signal = "HOLD"
            reasons.append("⚠ HTF daily trend is bearish — LTF BUY downgraded to HOLD")
        elif signal == "SELL" and htf_bullish:
            signal = "HOLD"
            reasons.append("⚠ HTF daily trend is bullish — LTF SELL downgraded to HOLD")

    # ── Layer 4: assign stop/target based on direction ────────────────────────
    if signal == "SELL":
        stop_loss = short_stop; target1 = short_tgt1; target2 = short_tgt2; rr_ratio = short_rr
    else:
        stop_loss = long_stop;  target1 = long_tgt1;  target2 = long_tgt2;  rr_ratio = long_rr

    return _make_result(
        signal, score_pct, reasons, regime,
        stop_loss, target1, target2, rr_ratio,
        bos_bull=bos_bull, bos_bear=bos_bear,
        rsi_bull_div=rsi_bull_div, rsi_bear_div=rsi_bear_div,
        htf_bullish=htf_bullish, cooldown=cooldown,
        vwap=vwap, vwap_pct=vwap_pct,
    )


def _build_summary(signal, score, rsi, adx, vol_ratio, trend_strong,
                   proj_up_pct, proj_down_pct, proj_timeline,
                   nearest_res, nearest_sup, buy_reasons, sell_reasons,
                   regime="unknown", bos_bull=False, bos_bear=False,
                   rsi_divergence=False, rr_ratio=0.0) -> str:
    """Plain-English explanation of the signal for non-technical readers."""
    lines = []

    if signal == "VOID":
        return "Signal voided: volume too thin to confirm any move."

    # Opening verdict
    if signal == "BUY":
        confidence = "strongly" if score >= 75 else "moderately"
        lines.append(f"Our analysis {confidence} suggests this stock is worth buying right now.")
    elif signal == "SELL":
        confidence = "strongly" if score <= 25 else "moderately"
        lines.append(f"Our analysis {confidence} suggests avoiding or exiting this stock right now.")
    else:
        lines.append("This stock is sending mixed signals — it's not a clear buy or sell at the moment.")

    # Regime context
    _regime_ctx = {
        "uptrend":      "The market is in a confirmed uptrend (ADX > 25, price above 200-day average).",
        "downtrend":    "The market is in a confirmed downtrend (ADX > 25, price below 200-day average).",
        "ranging":      "The market is ranging without a strong trend (ADX < 20).",
        "transitional": "The market regime is unclear — ADX is between trending and ranging.",
    }
    if regime in _regime_ctx:
        lines.append(_regime_ctx[regime])

    # Trend in plain words
    if trend_strong:
        if buy_reasons:
            lines.append("The stock has been steadily climbing and is in a healthy upward trend.")
        else:
            lines.append("The stock has been steadily falling and is in a downward trend.")
    else:
        lines.append("The stock has been moving without a strong direction lately.")

    # RSI
    if rsi < 35:
        lines.append(
            f"It looks oversold (momentum score: {rsi:.0f}/100) — meaning it may have dropped "
            f"more than it deserved and could bounce back soon."
        )
    elif rsi > 70:
        lines.append(
            f"It looks overbought (momentum score: {rsi:.0f}/100) — meaning it has risen a lot "
            f"in a short time and might take a breather or pull back."
        )
    elif rsi > 60:
        lines.append(f"Momentum is positive (score: {rsi:.0f}/100) — buyers are in control.")
    elif rsi < 40:
        lines.append(f"Momentum is weak (score: {rsi:.0f}/100) — sellers have the upper hand.")
    else:
        lines.append(f"Momentum is neutral (score: {rsi:.0f}/100) — neither buyers nor sellers dominate.")

    # RSI divergence
    if rsi_divergence:
        lines.append(
            "RSI divergence detected — price and momentum are moving in opposite directions, "
            "which is one of the highest-probability reversal setups."
        )

    # Break of structure
    if bos_bull:
        lines.append(
            "The stock just broke above its recent swing high — a bullish structural break "
            "suggesting the uptrend may be continuing or accelerating."
        )
    elif bos_bear:
        lines.append(
            "The stock just broke below its recent swing low — a bearish structural break "
            "suggesting the downtrend may be continuing or accelerating."
        )

    # Volume participation
    if vol_ratio > 2.0:
        lines.append(
            f"Trading activity is very high today ({vol_ratio:.1f}× the usual) — "
            f"this means a lot of people are buying/selling, which adds confidence to the signal."
        )
    elif vol_ratio > 1.4:
        lines.append(
            f"Trading activity is above average ({vol_ratio:.1f}× normal), "
            f"which gives the signal more credibility."
        )
    elif vol_ratio < 0.5:
        lines.append(
            "Trading activity is very low today — take this signal with caution, "
            "as few participants means the move may not hold."
        )

    # Key reason translations
    reason_map = {
        "Price above EMA20 > EMA50 (uptrend)":       "The price is sitting above its 20-day and 50-day averages — a classic sign of an uptrend.",
        "Price below EMA20 < EMA50 (downtrend)":     "The price has fallen below its 20-day and 50-day averages — a classic sign of a downtrend.",
        "MACD bullish crossover":                     "A key momentum indicator just flipped from bearish to bullish — often an early buy signal.",
        "MACD bearish crossover":                     "A key momentum indicator just flipped from bullish to bearish — often an early sell signal.",
        "Price at lower Bollinger Band (oversold)":   "The price has dropped to the very bottom of its normal trading range — a potential bounce point.",
        "Price at upper Bollinger Band (overbought)": "The price has risen to the very top of its normal trading range — a potential pullback point.",
        "Near support":      f"The price is sitting near a key support level (₹{nearest_sup:,.0f}) where buyers tend to step in." if nearest_sup else "",
        "Near resistance":   f"The price is approaching a key resistance level (₹{nearest_res:,.0f}) where sellers tend to push back." if nearest_res else "",
    }
    active_reasons = buy_reasons if signal in ("BUY", "HOLD") else sell_reasons
    seen = set()
    for raw in active_reasons:
        for k, v in reason_map.items():
            if k in raw and v and k not in seen:
                seen.add(k)
                lines.append(v)
                break

    # Action guidance with ATR levels
    if signal == "BUY":
        lines.append(
            f"If you buy now, the stock could potentially gain around {proj_up_pct:.1f}% "
            f"(ATR×2 target) over the next {proj_timeline}."
        )
        lines.append(
            f"Place a stop-loss at ATR×1.5 below entry ({abs(proj_down_pct):.1f}% risk). "
            + (f"Risk:Reward ratio: {rr_ratio:.1f}:1." if rr_ratio > 0 else "")
        )
        if nearest_sup:
            lines.append(
                f"The nearest support zone is around ₹{nearest_sup:,.0f} — "
                f"if the stock falls there, reassess the trade."
            )
    elif signal == "SELL":
        lines.append(
            f"The stock could fall around {abs(proj_down_pct):.1f}% from here (ATR×2 target). "
            f"If you hold it, you risk losses of that amount."
        )
        if nearest_res:
            lines.append(
                f"If the stock bounces back up to ₹{nearest_res:,.0f}, that would be a resistance "
                f"level — likely a good place to exit or sell."
            )
    else:
        lines.append(
            "The best move right now is to wait and watch. "
            "Let the stock pick a clear direction before putting money in."
        )

    return " ".join(lines)




# ── Intraday setup evaluator ────────────────────────────────────────────────
def intraday_setup_score(results: list[dict]) -> list[dict]:
    """
    Rank stocks by intraday trading potential.
    Boosts stocks with high ATR%, volume surge, VWAP alignment,
    and actionable RSI. Sorted descending by intraday score.
    """
    for r in results:
        pts     = 0
        reasons = []

        # High ATR% = volatile = better intraday range
        atr_pct = r.get("atr_pct", 0)
        if atr_pct >= 2.0:
            pts += 3
            reasons.append(f"High volatility (ATR {atr_pct:.1f}%) — good intraday range")
        elif atr_pct >= 1.0:
            pts += 1
            reasons.append(f"Moderate volatility (ATR {atr_pct:.1f}%)")

        # Volume surge = strong participation
        vol_ratio = r.get("vol_ratio", 1.0)
        if vol_ratio >= 2.5:
            pts += 3
            reasons.append(f"Volume surge ({vol_ratio:.1f}x) — strong participation")
        elif vol_ratio >= 1.5:
            pts += 1
            reasons.append(f"Above-avg volume ({vol_ratio:.1f}x)")

        # RSI in actionable zone
        rsi = r.get("rsi", 50)
        if rsi < 35:
            pts += 2
            reasons.append(f"RSI oversold ({rsi}) — bounce candidate")
        elif rsi > 65:
            pts += 1
            reasons.append(f"RSI strong ({rsi}) — momentum continuation")
        elif 40 <= rsi <= 60:
            pts += 1
            reasons.append(f"RSI neutral ({rsi}) — room to move")

        # VWAP alignment with signal
        vwap_pct = r.get("vwap_pct")
        signal   = r.get("signal", "HOLD")
        if vwap_pct is not None:
            if signal == "BUY" and vwap_pct > 0.3:
                pts += 2
                reasons.append(f"Above VWAP (+{vwap_pct:.1f}%) — intraday bullish bias")
            elif signal == "SELL" and vwap_pct < -0.3:
                pts += 2
                reasons.append(f"Below VWAP ({vwap_pct:.1f}%) — intraday bearish bias")

        # Break of structure adds strong directional bias
        if r.get("bos_bull"):
            pts += 2
            reasons.append("Break of structure bullish — strong intraday entry candidate")
        elif r.get("bos_bear"):
            pts += 2
            reasons.append("Break of structure bearish — intraday short candidate")

        # RSI divergence suggests high-probability reversal setup
        if r.get("rsi_bull_div") or r.get("rsi_bear_div"):
            pts += 1
            div_type = "bullish" if r.get("rsi_bull_div") else "bearish"
            reasons.append(f"RSI {div_type} divergence — potential reversal setup")

        # Base signal strength bonus
        base_score = r.get("score", 50)
        if base_score >= 70:
            pts += 2
        elif base_score >= 60:
            pts += 1

        r["intraday_pts"]     = pts
        r["intraday_reasons"] = reasons

    results.sort(key=lambda x: -x.get("intraday_pts", 0))
    return results


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

    # Sort: BUY first (by score desc), then HOLD, then SELL, then VOID
    order = {"BUY": 0, "HOLD": 1, "SELL": 2, "VOID": 3}
    results.sort(key=lambda x: (order.get(x["signal"], 4), -x["score"]))

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
        "--all-nse", action="store_true",
        help="Scan all NSE mainboard (EQ) stocks fetched from today's bhavcopy"
    )
    parser.add_argument(
        "--all-nse-sme", action="store_true",
        help="Scan all NSE stocks including SME/Emerge (very large — slow)"
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

    if args.all_nse_sme:
        print(f"{Fore.CYAN}  Fetching all NSE + SME tickers from bhavcopy...{Style.RESET_ALL}")
        tickers = fetch_all_nse_tickers(include_sme=True)
    elif args.all_nse:
        print(f"{Fore.CYAN}  Fetching all NSE mainboard tickers from bhavcopy...{Style.RESET_ALL}")
        tickers = fetch_all_nse_tickers(include_sme=False)
    elif args.zerodha:
        tickers = fetch_zerodha_holdings()
    elif args.stocks:
        tickers = [t.upper() for t in args.stocks]
    else:
        tickers = fetch_nifty50_tickers()

    scan(tickers, args.interval, args.top)


if __name__ == "__main__":
    main()