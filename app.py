"""
Streamlit Web UI for the Nifty Stock Signal Scanner
====================================================
Run with:
    streamlit run app.py
Then open http://localhost:8501 in your browser.
"""

import sys
import os

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stockupdate import (
    fetch_nifty50_tickers,
    fetch_data,
    compute_indicators,
    generate_signal,
    fetch_zerodha_holdings,
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

    mode = st.radio(
        "Stock List",
        ["Nifty 50", "Custom Tickers", "Zerodha Portfolio"],
        index=0,
    )

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
    run = st.button("🔍 Run Scan", width="stretch", type="primary")

    st.divider()
    st.caption("ℹ️ For Zerodha Portfolio, run `python stockupdate.py --zerodha-login` once daily first.")

# ── Welcome screen ────────────────────────────────────────────────────────────
if not run:
    st.title("📈 Nifty Stock Signal Scanner")
    st.markdown("Configure options in the **sidebar** and click **Run Scan** to start.")
    st.divider()
    col1, col2, col3 = st.columns(3)
    col1.info("**Signals**\n\nBUY / HOLD / SELL with confidence score based on RSI, MACD, EMAs, Volume & ADX")
    col2.info("**Price Targets**\n\nSupport & Resistance zones from swing levels, pivot points & round numbers")
    col3.info("**Charts**\n\nInteractive candlestick with EMA20, EMA50, SMA200, Volume & RSI")
    st.stop()

# ── Resolve ticker list ───────────────────────────────────────────────────────
if mode == "Nifty 50":
    with st.spinner("Fetching Nifty 50 constituents from NSE..."):
        tickers = fetch_nifty50_tickers()

elif mode == "Custom Tickers":
    raw     = custom_input.replace(",", " ").replace("\n", " ").split()
    tickers = [t.upper().strip() for t in raw if t.strip()]
    if not tickers:
        st.error("Enter at least one ticker in the sidebar.")
        st.stop()

else:  # Zerodha Portfolio
    try:
        tickers = fetch_zerodha_holdings()
    except SystemExit:
        st.error("Zerodha Portfolio is not available on the cloud deployment (requires kiteconnect + local login). Use **Nifty 50** or **Custom Tickers** instead.")
        st.stop()

# ── Scan ──────────────────────────────────────────────────────────────────────
results      = []
errors       = []
newly_listed = []

progress_bar = st.progress(0, text="Starting scan...")
status_text  = st.empty()

for i, ticker in enumerate(tickers):
    pct = (i + 1) / len(tickers)
    progress_bar.progress(pct, text=f"Fetching {ticker}  ({i+1}/{len(tickers)})")
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

progress_bar.empty()
status_text.empty()

# ── Warnings / errors ────────────────────────────────────────────────────────
for tkr, rows in newly_listed:
    st.warning(f"⚠ {tkr}: newly listed — only {rows} candle(s) available, need {MIN_ROWS}+ for analysis")

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
    table_rows.append({
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
    })

df_table = pd.DataFrame(table_rows)

def _style_signal(val):
    if val == "BUY":
        return "background-color: #0d3b26; color: #00e676; font-weight: bold"
    if val == "SELL":
        return "background-color: #3b0d0d; color: #ff5252; font-weight: bold"
    return "background-color: #3b3b0d; color: #ffd600"

styled_table = df_table.style.map(_style_signal, subset=["Signal"])
st.dataframe(styled_table, width="stretch", hide_index=True)

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

        # ── Candlestick chart ──
        with chart_col:
            df_c = r["_df"].tail(120).copy()

            fig = make_subplots(
                rows=3, cols=1,
                shared_xaxes=True,
                row_heights=[0.6, 0.2, 0.2],
                vertical_spacing=0.02,
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

            # Target / stop annotations on price axis
            fig.add_hline(y=r["proj_up"],   line_dash="dot", line_color="rgba(0,230,118,0.8)",
                          line_width=1.5, row=1, col=1,
                          annotation_text=f"Target ₹{r['proj_up']:,.0f}",
                          annotation_position="right")
            fig.add_hline(y=r["proj_down"], line_dash="dot", line_color="rgba(255,82,82,0.8)",
                          line_width=1.5, row=1, col=1,
                          annotation_text=f"Stop ₹{r['proj_down']:,.0f}",
                          annotation_position="right")

            # Volume bars
            vol_colors = [
                "#ef5350" if c < o else "#26a69a"
                for c, o in zip(df_c["Close"], df_c["Open"])
            ]
            fig.add_trace(go.Bar(
                x=df_c.index, y=df_c["Volume"],
                marker_color=vol_colors, name="Volume", showlegend=False,
            ), row=2, col=1)

            # RSI
            fig.add_trace(go.Scatter(
                x=df_c.index, y=df_c["RSI"],
                line=dict(color="#e91e63", width=1.2), name="RSI",
            ), row=3, col=1)
            fig.add_hline(y=70, line_dash="dot", line_color="rgba(239,83,80,0.5)",  row=3, col=1)
            fig.add_hline(y=30, line_dash="dot", line_color="rgba(38,166,154,0.5)", row=3, col=1)
            fig.add_hrect(y0=30, y1=70, fillcolor="rgba(255,255,255,0.02)",
                          line_width=0, row=3, col=1)

            fig.update_layout(
                height=420,
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=0, r=60, t=10, b=0),
                xaxis_rangeslider_visible=False,
                legend=dict(orientation="h", y=1.08, x=0, font_size=11),
            )
            fig.update_yaxes(title_text="Volume", row=2, col=1, title_font_size=10)
            fig.update_yaxes(title_text="RSI",    row=3, col=1, range=[0, 100], title_font_size=10)

            st.plotly_chart(fig, width="stretch")

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption("⚠️ This tool is for educational purposes only. Always do your own research before trading.")
