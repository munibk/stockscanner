"""
Streamlit Web UI for the Nifty Stock Signal Scanner
====================================================
Run with:
    streamlit run app.py
Then open http://localhost:8501 in your browser.
"""

import sys
import os
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stockupdate import (
    fetch_nifty50_tickers,
    fetch_all_nse_tickers,
    fetch_data,
    compute_indicators,
    generate_signal,
    fetch_zerodha_holdings,
    fetch_zerodha_holdings_with_token,
    get_nifty_index_data,
    intraday_setup_score,
    NIFTY_INTRADAY_STOCKS,
    _NewlyListedError,
    MIN_ROWS,
)

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Stock Signal Scanner",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    div[data-testid="metric-container"] { background: #1e1e2e; border-radius: 8px; padding: 8px; }

    /* ── Mobile: stack columns vertically ── */
    @media (max-width: 768px) {
        [data-testid="column"] {
            width: 100% !important;
            flex: 1 1 100% !important;
            min-width: 100% !important;
        }
        .block-container {
            padding-left: 0.75rem !important;
            padding-right: 0.75rem !important;
            padding-top: 0.75rem !important;
        }
        /* Horizontally scrollable table */
        [data-testid="stDataFrame"] > div {
            overflow-x: auto !important;
        }
        div[data-testid="metric-container"] {
            padding: 4px !important;
        }
        div[data-testid="metric-container"] label {
            font-size: 0.75rem !important;
        }
        [data-testid="stExpander"] summary p,
        [data-testid="stExpanderToggleIcon"] + div p {
            font-size: 0.9rem !important;
        }
    }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📈 Stock Scanner")
    st.divider()

    _kite_available = True
    try:
        import kiteconnect  # noqa: F401
    except ImportError:
        _kite_available = False

    _mode_options = [
        "Nifty 50",
        "Intraday Picks (top 30)",
        "All NSE Stocks",
        "All NSE + SME",
        "Custom Tickers",
    ] + (["Zerodha Portfolio"] if _kite_available else [])
    mode = st.radio("Stock List", _mode_options, index=0)

    # ── Zerodha: show login link + request_token input ──
    if mode == "Zerodha Portfolio":
        _has_secrets = "kite" in st.secrets if hasattr(st, "secrets") else False
        if _has_secrets:
            _api_key    = st.secrets["kite"]["api_key"]
            _api_secret = st.secrets["kite"]["api_secret"]
        else:
            # Fallback: read from local zerodha.cfg
            import configparser as _cp
            _cfg = _cp.ConfigParser()
            _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zerodha.cfg")
            _cfg.read(_cfg_path)
            _api_key    = _cfg.get("kite", "api_key", fallback="")
            _api_secret = _cfg.get("kite", "api_secret", fallback="")

        if _api_key and _api_key != "YOUR_API_KEY":
            from kiteconnect import KiteConnect as _KC
            _kite_tmp  = _KC(api_key=_api_key)

            if "kite_access_token" in st.session_state:
                st.success("✅ Logged in for this session.")
                if st.button("🔓 Logout / use new token", key="kite_logout"):
                    del st.session_state["kite_access_token"]
                    del st.session_state["kite_api_key"]
                    st.rerun()
            else:
                _login_url = _kite_tmp.login_url()
                st.markdown(f"**Step 1:** [Open Zerodha login ↗]({_login_url})")
                st.caption("After login, copy the `request_token=...` value from the redirect URL.")
                _req_token = st.text_input(
                    "Step 2: Paste request_token",
                    key="kite_req_token",
                    placeholder="abc123xyz...",
                )
                if st.button("🔑 Connect", key="kite_connect", type="primary",
                             disabled=not bool(_req_token)):
                    try:
                        _sess = _kite_tmp.generate_session(
                            _req_token.strip(), api_secret=_api_secret
                        )
                        st.session_state["kite_access_token"] = _sess["access_token"]
                        st.session_state["kite_api_key"]      = _api_key
                        st.rerun()
                    except Exception as _e:
                        st.error(f"Login failed: {_e}")
                        st.caption("⚠️ request_token is single-use. Go back to Step 1 and get a fresh token.")
        else:
            st.warning("Add `kite.api_key` and `kite.api_secret` to Streamlit secrets (Settings → Secrets).")

    custom_input = ""
    if mode == "Custom Tickers":
        custom_input = st.text_area(
            "Tickers (space or comma or newline separated)",
            placeholder="RELIANCE TCS INFY\nHDFCBANK ICICIBANK VBL",
            height=100,
        )

    interval = st.selectbox(
        "Candle Interval",
        options=["1d", "1wk", "1h", "15m", "5m", "1m"],
        index=0,
        format_func=lambda x: {
            "1m": "1 Minute", "5m": "5 Minutes", "15m": "15 Minutes",
            "1h": "1 Hour", "1d": "1 Day (default)", "1wk": "1 Week",
        }[x],
    )

    top_n = st.number_input("Show top N BUY signals (0 = all)", min_value=0, value=0, step=1)

    st.divider()
    live_monitor = st.toggle("📗 Live Monitor (auto-refresh)", value=False)
    refresh_secs = 60
    if live_monitor:
        refresh_secs = st.slider("Refresh every (seconds)", 30, 300, 60, 30)

    st.divider()
    run = st.button("🔍 Run Scan", width="stretch", type="primary")
    if run and live_monitor:
        st.session_state["live_active"] = True
    elif not live_monitor:
        st.session_state.pop("live_active", None)

    st.divider()
    st.caption("ℹ️ Zerodha Portfolio: add api_key & api_secret to Streamlit Secrets, then log in from the sidebar.")
# ── Auto-run when live monitor is active ────────────────────────────────────────
run = run or (live_monitor and st.session_state.get("live_active", False))

# ── Scan result cache (session-state backed, TTL per interval) ────────────────
import time as _time

_CACHE_TTL = {
    "1m":  15 * 60,   "5m":  30 * 60,  "15m": 60 * 60,
    "1h":  2  * 3600, "1d":  8  * 3600, "1wk": 24 * 3600,
}
_ck          = f"{mode}|{interval}"
_ttl         = _CACHE_TTL.get(interval, 4 * 3600)
_sc          = st.session_state.get("scan_cache", {})
_cached      = _sc.get(_ck)
_cache_fresh = (
    _cached is not None
    and not run                          # "Run Scan" click always bypasses cache
    and (_time.time() - _cached["ts"]) < _ttl
)

# ── Welcome screen (skipped when cached data can be shown) ───────────────────
if not run and not _cache_fresh:
    st.title("📈 Nifty Stock Signal Scanner")
    st.markdown("Configure options in the **sidebar** and click **Run Scan** to start.")
    st.divider()
    col1, col2, col3 = st.columns(3)
    col1.info("**Signals**\n\nBUY / HOLD / SELL with confidence score based on RSI, MACD, EMAs, Volume & ADX")
    col2.info("**Price Targets**\n\nSupport & Resistance zones from swing levels, pivot points & round numbers")
    col3.info("**Charts**\n\nInteractive candlestick with EMA20, EMA50, SMA200, Volume & RSI")
    st.stop()

# ── Signal cell styler (shared by live table, final table, and cache path) ────
def _style_signal(val):
    if val == "BUY":
        return "background-color: #0d3b26; color: #00e676; font-weight: bold"
    if val == "SELL":
        return "background-color: #3b0d0d; color: #ff5252; font-weight: bold"
    return "background-color: #3b3b0d; color: #ffd600"

# ── Fast path: load results from session-state cache ─────────────────────────
if _cache_fresh:
    results      = _cached["results"]
    errors       = _cached["errors"]
    newly_listed = _cached["newly_listed"]
    _age         = int(_time.time() - _cached["ts"])
    _mins, _secs = _age // 60, _age % 60
    st.info(
        f"Showing cached results ({_mins}m {_secs}s old) — "
        f"click **Run Scan** to fetch fresh data."
    )

# ── Resolve tickers + scan (skipped on cache hit) ────────────────────────────
if not _cache_fresh:
    if mode == "Nifty 50":
        with st.spinner("Fetching Nifty 50 constituents from NSE..."):
            tickers = fetch_nifty50_tickers()

    elif mode == "Intraday Picks (top 30)":
        tickers = list(NIFTY_INTRADAY_STOCKS)

    elif mode == "All NSE Stocks":
        with st.spinner("Downloading NSE bhavcopy to get all listed stocks..."):
            tickers = fetch_all_nse_tickers(include_sme=False)
        st.info(f"📂 {len(tickers)} NSE mainboard stocks loaded. Large scan — may take several minutes.")

    elif mode == "All NSE + SME":
        with st.spinner("Downloading NSE bhavcopy (mainboard + SME/Emerge)..."):
            tickers = fetch_all_nse_tickers(include_sme=True)
        st.info(f"📂 {len(tickers)} stocks loaded (mainboard + SME). Very large scan — may take 10–20+ minutes.")

    elif mode == "Custom Tickers":
        raw     = custom_input.replace(",", " ").replace("\n", " ").split()
        tickers = [t.upper().strip() for t in raw if t.strip()]
        if not tickers:
            st.error("Enter at least one ticker in the sidebar.")
            st.stop()

    else:  # Zerodha Portfolio
        if "kite_access_token" not in st.session_state:
            st.warning("Please log in to Zerodha using the sidebar first.")
            st.stop()
        try:
            tickers = fetch_zerodha_holdings_with_token(
                st.session_state["kite_api_key"],
                st.session_state["kite_access_token"],
            )
        except Exception as e:
            st.error(f"Failed to fetch Zerodha holdings: {e}")
            st.stop()
        if not tickers:
            st.warning("No holdings or open positions found in your Zerodha account.")
            st.stop()

    # ── Scan ──────────────────────────────────────────────────────────────────
    results      = []
    errors       = []
    newly_listed = []

    progress_bar  = st.progress(0, text="Starting scan...")
    live_status   = st.empty()
    live_table_ph = st.empty()

    # refresh the live table ~20 times across the full list
    _UPDATE_EVERY = max(1, min(10, max(1, len(tickers) // 20)))

    for i, ticker in enumerate(tickers):
        pct = (i + 1) / len(tickers)
        progress_bar.progress(pct, text=f"Scanning {ticker}  ({i+1}/{len(tickers)})")
        try:
            df = fetch_data(ticker, interval)
        except _NewlyListedError as e:
            newly_listed.append((e.ticker, e.rows))
            continue
        if df is None:
            errors.append(ticker)
            continue
        df  = compute_indicators(df)
        sig = generate_signal(df, interval)
        sig["ticker"] = ticker
        sig["_df"]    = df
        results.append(sig)

        # live table update every _UPDATE_EVERY stocks (and always on the last one)
        if results and (len(results) % _UPDATE_EVERY == 0 or i == len(tickers) - 1):
            _b = sum(1 for _r in results if _r["signal"] == "BUY")
            _s = sum(1 for _r in results if _r["signal"] == "SELL")
            _h = sum(1 for _r in results if _r["signal"] == "HOLD")
            _skipped = len(newly_listed) + len(errors)
            live_status.markdown(
                f"**Scanned {i+1} / {len(tickers)}** "
                f"&nbsp;|&nbsp; BUY **{_b}** &nbsp; HOLD **{_h}** &nbsp; SELL **{_s}**"
                + (f" &nbsp;|&nbsp; Skipped {_skipped}" if _skipped else "")
            )
            _partial = sorted(
                results,
                key=lambda x: ({"BUY": 0, "HOLD": 1, "SELL": 2}[x["signal"]], -x["score"])
            )[:20]
            _live_rows = [
                {
                    "Ticker":    _r["ticker"],
                    "Price":     f"\u20b9{_r['price']:,.2f}",
                    "Signal":    _r["signal"],
                    "Score":     _r["score"],
                    "RSI":       _r["rsi"],
                    "Vol":       f"{_r['vol_ratio']}x",
                    "Target":    f"\u20b9{_r['proj_up']:,.2f} ({_r['proj_up_pct']:+.1f}%)",
                    "Stop":      f"\u20b9{_r['proj_down']:,.2f} ({_r['proj_down_pct']:+.1f}%)",
                }
                for _r in _partial
            ]
            with live_table_ph.container():
                st.caption(
                    f"Live results — top 20 of {len(results)} scanned "
                    f"(refreshes every {_UPDATE_EVERY} stocks)"
                )
                st.dataframe(
                    pd.DataFrame(_live_rows).style.map(_style_signal, subset=["Signal"]),
                    use_container_width=True, hide_index=True,
                )

    progress_bar.empty()
    live_status.empty()
    live_table_ph.empty()   # full results section renders below

    # ── Save scan results to session-state cache ───────────────────────────────
    st.session_state.setdefault("scan_cache", {})[_ck] = {
        "ts":          _time.time(),
        "results":     results,
        "errors":      errors,
        "newly_listed": newly_listed,
    }

# ── Warnings / errors ────────────────────────────────────────────────────────
for tkr, rows in newly_listed:
    hint = " — NSE SME/Emerge stock. Yahoo Finance has no history for these; use **Zerodha Portfolio** mode instead." if tkr.upper().endswith("-SM") else ""
    st.warning(f"⚠ {tkr}: newly listed — only {rows} candle(s) available, need {MIN_ROWS}+ for analysis.{hint}")

if errors:
    st.error(f"Could not fetch data for: {', '.join(errors)}")

if not results:
    st.error("No data could be fetched. Check your internet connection.")
    st.stop()

# ── Sort ──────────────────────────────────────────────────────────────────────
order = {"BUY": 0, "HOLD": 1, "SELL": 2}
results.sort(key=lambda x: (order[x["signal"]], -x["score"]))

if top_n > 0:
    results = [r for r in results if r["signal"] == "BUY"][:int(top_n)]
# ── Nifty 50 Index live widget ───────────────────────────────────────────────────
with st.spinner("Loading Nifty 50 index data..."):
    _nifty_df = get_nifty_index_data(interval)

if _nifty_df is not None and len(_nifty_df) >= 2:
    _nl  = float(_nifty_df["Close"].iloc[-1])
    _np  = float(_nifty_df["Close"].iloc[-2])
    _nc  = _nl - _np
    _ncp = _nc / _np * 100
    _nh  = float(_nifty_df["High"].iloc[-1])
    _nlo = float(_nifty_df["Low"].iloc[-1])
    st.markdown("### 📊 Nifty 50 Index")
    _ni1, _ni2, _ni3, _ni4 = st.columns(4)
    _ni1.metric("Nifty 50",  f"{_nl:,.2f}",  f"{_nc:+.2f} ({_ncp:+.2f}%)")
    _ni2.metric("Day High",   f"{_nh:,.2f}")
    _ni3.metric("Day Low",    f"{_nlo:,.2f}")
    _ni4.metric("Market",     "🟢 Up" if _nc >= 0 else "🔴 Down")
    with st.expander("📈 Nifty 50 Chart (last 60 candles)", expanded=False):
        _nt = _nifty_df.tail(60)
        _nf = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            row_heights=[0.75, 0.25], vertical_spacing=0.02)
        _nf.add_trace(go.Candlestick(
            x=_nt.index, open=_nt["Open"], high=_nt["High"],
            low=_nt["Low"], close=_nt["Close"], name="Nifty 50",
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
        ), row=1, col=1)
        _nf.add_trace(go.Scatter(
            x=_nt.index, y=_nt["Close"].ewm(span=20).mean(),
            line=dict(color="#2196f3", width=1.5), name="EMA 20",
        ), row=1, col=1)
        _nf.add_trace(go.Scatter(
            x=_nt.index, y=_nt["Close"].ewm(span=50).mean(),
            line=dict(color="#ff9800", width=1.5), name="EMA 50",
        ), row=1, col=1)
        _vol_c = ["#ef5350" if c < o else "#26a69a"
                  for c, o in zip(_nt["Close"], _nt["Open"])]
        _nf.add_trace(go.Bar(
            x=_nt.index, y=_nt["Volume"], marker_color=_vol_c,
            name="Volume", showlegend=False,
        ), row=2, col=1)
        _nf.update_layout(
            height=360, template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=10, b=0),
            xaxis_rangeslider_visible=False,
            legend=dict(orientation="h", y=1.08),
        )
        st.plotly_chart(_nf, use_container_width=True)
    st.divider()

# ── Apply intraday rank if in Intraday Picks mode ──────────────────────────────
if mode in ("Intraday Picks (top 30)", "All NSE Stocks", "All NSE + SME"):
    results = intraday_setup_score(results)
# ── Summary metrics ───────────────────────────────────────────────────────────
st.title("📈 Scan Results")

buys  = sum(1 for r in results if r["signal"] == "BUY")
sells = sum(1 for r in results if r["signal"] == "SELL")
holds = sum(1 for r in results if r["signal"] == "HOLD")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Stocks Scanned", len(results))
m2.metric("🟢 BUY",  buys)
m3.metric("🟡 HOLD", holds)
m4.metric("🔴 SELL", sells)

st.divider()

# ── Results table ─────────────────────────────────────────────────────────────
st.subheader("Summary Table")

table_rows = []
for r in results:
    row = {
        "Ticker":     r["ticker"],
        "Price (₹)":  f"₹{r['price']:,.2f}",
        "Signal":     r["signal"],
        "Score":      r["score"],
        "RSI":        r["rsi"],
        "ADX":        r["adx"] if r["adx"] else None,
        "Vol Ratio":  r["vol_ratio"],
        "Target":     f"₹{r['proj_up']:,.2f}  ({r['proj_up_pct']:+.1f}%)",
        "Stop":       f"₹{r['proj_down']:,.2f}  ({r['proj_down_pct']:+.1f}%)",
        "Timeline":   r["proj_timeline"],
    }
    if mode in ("Intraday Picks (top 30)", "All NSE Stocks", "All NSE + SME"):
        row["Intraday★"] = r.get("intraday_pts", 0)
        row["ATR%"]      = f"{r.get('atr_pct', 0):.1f}%"
        _vp = r.get("vwap_pct")
        row["vs VWAP"]   = f"{_vp:+.1f}%" if _vp is not None else "—"
    table_rows.append(row)

df_table = pd.DataFrame(table_rows)

styled_table = df_table.style.map(_style_signal, subset=["Signal"])
st.dataframe(styled_table, use_container_width=True, hide_index=True)

st.divider()

# ── Per-stock detail + chart ──────────────────────────────────────────────────
st.subheader("📊 Stock Details & Charts")

for r in results:
    sig_icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}[r["signal"]]
    label    = f"{sig_icon}  **{r['ticker']}**  —  {r['signal']}  ({r['score']:.0f}/100)  ₹{r['price']:,.2f}"

    with st.expander(label, expanded=(len(results) == 1)):
        info_col, chart_col = st.columns([1, 3])

        # ── Info panel (shown first so it stacks on top on mobile) ──
        with info_col:
            st.markdown(f"### {r['ticker']}")
            sig_color = {"BUY": "green", "SELL": "red", "HOLD": "orange"}[r["signal"]]
            st.markdown(f"**Signal:** :{sig_color}[{r['signal']}] &nbsp; Score: **{r['score']:.0f}/100**")
            st.markdown(f"**RSI:** {r['rsi']}  |  **ADX:** {r['adx'] or '\u2014'}  |  **Vol:** {r['vol_ratio']}x")
            st.markdown("---")
            st.markdown(f"🎯 **Target:** ₹{r['proj_up']:,.2f}  ({r['proj_up_pct']:+.1f}%)  *{r['proj_timeline']}*")
            st.markdown(f"🛑 **Stop:**   ₹{r['proj_down']:,.2f}  ({r['proj_down_pct']:+.1f}%)")
            if r["resistances"]:
                st.markdown("🔴 **Resistance:** " + " | ".join(f"₹{v:,.2f}" for v in r["resistances"]))
            if r["supports"]:
                st.markdown("🟢 **Support:**    " + " | ".join(f"₹{v:,.2f}" for v in r["supports"]))
            st.markdown("---")
            if r.get("summary"):
                icon = "🟢" if r["signal"] == "BUY" else ("🔴" if r["signal"] == "SELL" else "🟡")
                st.info(f"{icon} **In plain English:** {r['summary']}")
                st.markdown("---")
            st.markdown("**Reasons:**")
            for reason in r["reasons"]:
                # strip colorama tags if any leaked through
                clean = reason.replace("⚠ Against: ", "⚠️ *Against:* ")
                st.markdown(f"- {clean}")

            # ── Intraday & technical indicators ──────────────────────────────
            st.markdown("---")
            st.markdown("**Technical Indicators:**")
            _vwap_val = r.get("vwap")
            _vwap_pct = r.get("vwap_pct")
            _sk       = r.get("stoch_k", 50)
            _sd       = r.get("stoch_d", 50)
            _atr      = r.get("atr_pct", 0)
            if _vwap_val:
                _vc = "green" if (_vwap_pct or 0) >= 0 else "red"
                st.markdown(f"🟡 **VWAP:** ₹{_vwap_val:,.2f} &nbsp; :{_vc}[{_vwap_pct:+.2f}%]")
            _sk_col = "green" if _sk < 30 else ("red" if _sk > 70 else "orange")
            st.markdown(f"📊 **StochRSI:** K={_sk:.0f} &nbsp; D={_sd:.0f}")
            st.markdown(f"📌 **ATR%:** {_atr:.2f}% (expected daily move)")
            if mode in ("Intraday Picks (top 30)", "All NSE Stocks", "All NSE + SME") and r.get("intraday_reasons"):
                st.markdown(f"🎯 **Intraday Score:** {r.get('intraday_pts', 0)} pts")
                for _ir in r["intraday_reasons"]:
                    st.markdown(f"  - {_ir}")

            # ── Buy / Sell signal badge ───────────────────────────────────────
            st.markdown("---")
            _badge_css = {
                "BUY":  "background:#0d3b26;color:#00e676;padding:6px 14px;border-radius:8px;font-weight:bold;font-size:1.1rem",
                "SELL": "background:#3b0d0d;color:#ff5252;padding:6px 14px;border-radius:8px;font-weight:bold;font-size:1.1rem",
                "HOLD": "background:#3b3b0d;color:#ffd600;padding:6px 14px;border-radius:8px;font-weight:bold;font-size:1.1rem",
            }[r["signal"]]
            st.markdown(f'<span style="{_badge_css}">{r["signal"]} &nbsp; {r["score"]:.0f}/100</span>', unsafe_allow_html=True)

        # ── Candlestick chart (4 panels: Price, Volume, RSI, MACD) ──
        with chart_col:
            df_c = r["_df"].tail(150).copy()

            fig = make_subplots(
                rows=4, cols=1,
                shared_xaxes=True,
                row_heights=[0.50, 0.17, 0.16, 0.17],
                vertical_spacing=0.02,
                subplot_titles=("", "Volume", "RSI (14)", "MACD (12/26/9)"),
            )

            # Candlestick
            fig.add_trace(go.Candlestick(
                x=df_c.index,
                open=df_c["Open"], high=df_c["High"],
                low=df_c["Low"],  close=df_c["Close"],
                name="Price",
                increasing_line_color="#26a69a",
                decreasing_line_color="#ef5350",
                showlegend=False,
            ), row=1, col=1)

            # EMAs
            fig.add_trace(go.Scatter(
                x=df_c.index, y=df_c["EMA20"],
                line=dict(color="#2196f3", width=1.2), name="EMA 20",
            ), row=1, col=1)
            fig.add_trace(go.Scatter(
                x=df_c.index, y=df_c["EMA50"],
                line=dict(color="#ff9800", width=1.2), name="EMA 50",
            ), row=1, col=1)
            if "SMA200" in df_c.columns:
                fig.add_trace(go.Scatter(
                    x=df_c.index, y=df_c["SMA200"],
                    line=dict(color="#ce93d8", width=1, dash="dot"), name="SMA 200",
                ), row=1, col=1)

            # VWAP
            if "VWAP" in df_c.columns:
                fig.add_trace(go.Scatter(
                    x=df_c.index, y=df_c["VWAP"],
                    line=dict(color="#ffeb3b", width=1.5, dash="dash"), name="VWAP",
                ), row=1, col=1)

            # Bollinger Bands
            fig.add_trace(go.Scatter(
                x=df_c.index, y=df_c["BB_up"],
                line=dict(color="rgba(100,100,255,0.4)", width=1, dash="dot"),
                name="BB Upper", showlegend=False,
            ), row=1, col=1)
            fig.add_trace(go.Scatter(
                x=df_c.index, y=df_c["BB_low"],
                line=dict(color="rgba(100,100,255,0.4)", width=1, dash="dot"),
                name="BB Lower",
                fill="tonexty", fillcolor="rgba(100,100,255,0.05)",
                showlegend=False,
            ), row=1, col=1)

            # S/R horizontal lines
            for res in r["resistances"]:
                fig.add_hline(y=res, line_dash="dash", line_color="rgba(239,83,80,0.6)",
                              line_width=1, row=1, col=1)
            for sup in r["supports"]:
                fig.add_hline(y=sup, line_dash="dash", line_color="rgba(38,166,154,0.6)",
                              line_width=1, row=1, col=1)

            # Target / stop annotations
            fig.add_hline(y=r["proj_up"],   line_dash="dot", line_color="rgba(0,230,118,0.8)",
                          line_width=1.5, row=1, col=1,
                          annotation_text=f"🎯 ₹{r['proj_up']:,.0f}",
                          annotation_position="top right")
            fig.add_hline(y=r["proj_down"], line_dash="dot", line_color="rgba(255,82,82,0.8)",
                          line_width=1.5, row=1, col=1,
                          annotation_text=f"🛑 ₹{r['proj_down']:,.0f}",
                          annotation_position="bottom right")

            # Volume bars (green/red)
            vol_colors = [
                "#ef5350" if c < o else "#26a69a"
                for c, o in zip(df_c["Close"], df_c["Open"])
            ]
            fig.add_trace(go.Bar(
                x=df_c.index, y=df_c["Volume"],
                marker_color=vol_colors, name="Volume", showlegend=False,
            ), row=2, col=1)
            # Volume average line
            if "Vol_avg" in df_c.columns:
                fig.add_trace(go.Scatter(
                    x=df_c.index, y=df_c["Vol_avg"],
                    line=dict(color="rgba(255,235,59,0.7)", width=1, dash="dot"),
                    name="Vol Avg", showlegend=False,
                ), row=2, col=1)

            # RSI
            fig.add_trace(go.Scatter(
                x=df_c.index, y=df_c["RSI"],
                line=dict(color="#e91e63", width=1.5), name="RSI",
            ), row=3, col=1)
            if "RSI_smooth" in df_c.columns:
                fig.add_trace(go.Scatter(
                    x=df_c.index, y=df_c["RSI_smooth"],
                    line=dict(color="rgba(233,30,99,0.4)", width=1), name="RSI Smooth",
                    showlegend=False,
                ), row=3, col=1)
            fig.add_hline(y=70, line_dash="dot", line_color="rgba(239,83,80,0.6)",  row=3, col=1,
                          annotation_text="70", annotation_position="right")
            fig.add_hline(y=30, line_dash="dot", line_color="rgba(38,166,154,0.6)", row=3, col=1,
                          annotation_text="30", annotation_position="right")
            fig.add_hrect(y0=30, y1=70, fillcolor="rgba(255,255,255,0.02)",
                          line_width=0, row=3, col=1)

            # MACD histogram + lines
            macd_colors = [
                "#26a69a" if v >= 0 else "#ef5350"
                for v in df_c["MACD_hist"].fillna(0)
            ]
            fig.add_trace(go.Bar(
                x=df_c.index, y=df_c["MACD_hist"],
                marker_color=macd_colors, name="MACD Hist", showlegend=False,
            ), row=4, col=1)
            fig.add_trace(go.Scatter(
                x=df_c.index, y=df_c["MACD"],
                line=dict(color="#2196f3", width=1.5), name="MACD",
            ), row=4, col=1)
            fig.add_trace(go.Scatter(
                x=df_c.index, y=df_c["MACD_sig"],
                line=dict(color="#ff9800", width=1.5), name="Signal",
            ), row=4, col=1)
            fig.add_hline(y=0, line_dash="dot", line_color="rgba(255,255,255,0.25)", row=4, col=1)

            fig.update_layout(
                height=600,
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=0, r=70, t=20, b=0),
                xaxis_rangeslider_visible=False,
                legend=dict(orientation="h", y=1.06, x=0, font_size=11),
            )
            fig.update_yaxes(title_text="Volume", row=2, col=1, title_font_size=10)
            fig.update_yaxes(title_text="RSI",    row=3, col=1, range=[0, 100], title_font_size=10)
            fig.update_yaxes(title_text="MACD",   row=4, col=1, title_font_size=10)
            fig.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,0.05)")
            fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.05)")

            st.plotly_chart(fig, use_container_width=True)

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption("⚠️ This tool is for educational purposes only. Always do your own research before trading.")
# ── Live monitor auto-refresh ──────────────────────────────────────────────────
if live_monitor and st.session_state.get("live_active", False):
    import time
    _next = datetime.now().strftime("%H:%M:%S")
    st.info(f"📗 Live monitor active — refreshing every {refresh_secs}s | Last scan: {_next}")
    time.sleep(refresh_secs)
    st.rerun()