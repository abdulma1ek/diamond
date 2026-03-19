import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import time
from datetime import datetime

from src.dashboard_api import (
    get_model_state,
    is_bot_running,
    get_latest_evaluations,
    get_decisions,
    get_settled_trades,
    get_open_trades,
    get_system_logs,
    get_technical_events,
    compute_performance,
    get_signal_timeseries,
    get_trade_annotations,
    get_balance_history,
)

# --- Page Config ---
st.set_page_config(
    page_title="DIAMOND v3.0 | LIVE",
    page_icon="terminal",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# --- Styling ---
st.markdown(
    """
<style>
    /* Global Background and Text */
    .stApp {
        background-color: #0a0a0a;
        color: #00e5ff;
        font-family: 'IBM Plex Mono', 'JetBrains Mono', Courier, monospace;
    }
    
    /* Hide top bar and footer */
    header {visibility: hidden;}
    footer {visibility: hidden;}

    /* Typography & Colors */
    h1, h2, h3, h4, p, div, span, label {
        font-family: 'IBM Plex Mono', 'JetBrains Mono', Courier, monospace !important;
    }
    .text-green { color: #00ff41; text-shadow: 0 0 5px rgba(0,255,65,0.4); }
    .text-amber { color: #ffb000; text-shadow: 0 0 5px rgba(255,176,0,0.4); }
    .text-red { color: #ff073a; text-shadow: 0 0 5px rgba(255,7,58,0.4); }
    .text-cyan { color: #00e5ff; }
    .text-dim { color: #555555; }

    /* ASCII Borders & Boxes */
    .ascii-box {
        border: 1px solid #333;
        padding: 15px;
        border-radius: 2px;
        background-color: #0f0f0f;
        margin-bottom: 15px;
    }
    
    .status-bar {
        border: 1px solid #333;
        padding: 10px 15px;
        background-color: #0a0a0a;
        margin-bottom: 20px;
        font-weight: bold;
        display: flex;
        justify-content: space-between;
    }

    /* Terminal Logs Container */
    .terminal-log {
        background-color: #050505;
        border: 1px solid #222;
        padding: 10px;
        font-size: 12px;
        height: 400px;
        overflow-y: auto;
        white-space: pre-wrap;
        color: #00ff41;
        line-height: 1.4;
    }
    
    /* Blinking Cursor */
    .cursor {
        animation: blink 1s step-end infinite;
    }
    @keyframes blink {
        0%, 100% { opacity: 1; }
        50% { opacity: 0; }
    }

    /* Streamlit overrides */
    .stTabs [data-baseweb="tab-list"] { gap: 2px; }
    .stTabs [data-baseweb="tab"] {
        height: 30px;
        white-space: pre-wrap;
        background-color: #1a1a1a;
        border-radius: 0px 0px 0 0;
        color: #00e5ff;
        border: 1px solid #333;
    }
    .stTabs [aria-selected="true"] {
        background-color: #333;
        color: #00ff41;
        border-bottom-color: transparent !important;
    }
    
    /* Gauges */
    .gauge-container {
        width: 100%;
        background-color: #1a1a1a;
        border: 1px solid #333;
        height: 15px;
        position: relative;
        margin-top: 5px;
    }
    .gauge-fill {
        height: 100%;
        position: absolute;
        top: 0;
    }
    
    /* Tables */
    table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
    }
    th, td {
        border: 1px solid #333;
        padding: 8px;
        text-align: right;
    }
    th {
        background-color: #111;
        color: #00e5ff;
    }
    td:first-child, th:first-child {
        text-align: left;
    }

</style>
""",
    unsafe_allow_html=True,
)


# --- Helpers ---
def format_ts(ts: float) -> str:
    if not ts:
        return "--:--:--"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def format_dur(seconds: float) -> str:
    if not seconds:
        return "--m --s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def get_color_class(val: float, threshold: float = 0.0) -> str:
    if val > threshold:
        return "text-green"
    if val < -threshold:
        return "text-red"
    return "text-cyan"


def draw_horizontal_bar(val: float, min_val: float = -1.0, max_val: float = 1.0) -> str:
    # Clamp
    val = max(min_val, min(val, max_val))
    # Normalize to 0-1
    range_span = max_val - min_val
    norm_val = (val - min_val) / range_span
    pct = norm_val * 100

    color = "#00ff41" if val >= 0 else "#ff073a"

    # Draw from center
    center_pct = 50
    if val >= 0:
        left = center_pct
        width = pct - center_pct
    else:
        left = pct
        width = center_pct - pct

    return f"""
    <div class="gauge-container">
        <div style="position:absolute; left:50%; top:0; bottom:0; width:1px; background:#555;"></div>
        <div class="gauge-fill" style="left: {left}%; width: {width}%; background-color: {color};"></div>
    </div>
    """


# --- Data Loading ---
state = get_model_state()
running = is_bot_running()
perf = compute_performance()

# --- Section 1: Top Status Bar ---
status_col = "text-green" if running else "text-red"
status_txt = state.get("engine_status", "RUNNING") if running else "OFFLINE"

# Refine status color based on granular status
if running:
    if "WAITING" in status_txt:
        status_col = "text-amber"
    elif status_txt == "RUNNING":
        status_col = "text-green"

btc_price = state.get("btc_price", 0.0)
strike = state.get("strike", 0.0)
time_left = state.get("time_left_s", 0)
bal = state.get("balance", 0.0)
roi = state.get("roi_pct", 0.0)
pnl = state.get("total_pnl", 0.0)
uptime = state.get("uptime_s", 0)

st.markdown(
    f"""
<div class="status-bar">
    <span>DIAMOND v3.0</span>
    <span>STATUS: [<span class="{status_col}">{status_txt}</span>]</span>
    <span>UPTIME: {format_dur(uptime)}</span>
</div>
<div class="status-bar" style="border-top:none; margin-top:-21px;">
    <span>BTC: <span class="text-cyan">${btc_price:,.2f}</span></span>
    <span>STRIKE: <span class="text-amber">${strike:,.2f}</span></span>
    <span>WINDOW: <span class="text-cyan">{format_dur(time_left)}</span> left</span>
</div>
<div class="status-bar" style="border-top:none; margin-top:-21px;">
    <span>BALANCE: <span class="text-cyan">${bal:.2f}</span></span>
    <span>ROI: <span class="{get_color_class(roi)}">{roi:+.2f}%</span></span>
    <span>PNL: <span class="{get_color_class(pnl)}">${pnl:+.2f}</span></span>
</div>
""",
    unsafe_allow_html=True,
)


# --- Section 2: Signal Gauges ---
st.markdown("### ════ SIGNAL TELEMETRY ════")
col1, col2, col3 = st.columns(3)

with col1:
    st.markdown('<div class="ascii-box">', unsafe_allow_html=True)
    st.markdown("**COMPONENTS**")

    obi_raw = state.get("obi_raw", 0.0)
    obi_ema = state.get("obi_ema", 0.0)
    cvd_raw = state.get("cvd_raw", 0.0)
    cvd_ema = state.get("cvd_ema", 0.0)
    fund = state.get("funding_raw", 0.0)

    st.markdown(
        f"OBI (EMA: <span class='{get_color_class(obi_ema)}'>{obi_ema:+.4f}</span> | RAW: <span class='text-dim'>{obi_raw:+.4f}</span>)",
        unsafe_allow_html=True,
    )
    st.markdown(draw_horizontal_bar(obi_ema), unsafe_allow_html=True)

    st.markdown(f"CVD (EMA: <span class='{get_color_class(cvd_ema)}'>{cvd_ema:+.4f}</span> | RAW: <span class='text-dim'>{cvd_raw:+.4f}</span>)", unsafe_allow_html=True)
    st.markdown(draw_horizontal_bar(cvd_ema), unsafe_allow_html=True)
    
    mom_ema = state.get("momentum_ema", 0.0)
    st.markdown(f"MOMENTUM (<span class='{get_color_class(mom_ema)}'>{mom_ema:+.4f}</span>)", unsafe_allow_html=True)
    st.markdown(draw_horizontal_bar(mom_ema), unsafe_allow_html=True)

    ltb = state.get("large_trade_bias", 0.0)
    st.markdown(f"WHALE BIAS (<span class='{get_color_class(ltb)}'>{ltb:+.4f}</span>)", unsafe_allow_html=True)
    st.markdown(draw_horizontal_bar(ltb), unsafe_allow_html=True)

    st.markdown(
        f"FUNDING BIAS (<span class='{get_color_class(-fund)}'>{-fund * 10000:+.2f}</span>)",
        unsafe_allow_html=True,
    )
    st.markdown(draw_horizontal_bar(-fund * 10000), unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

with col2:
    st.markdown(
        '<div class="ascii-box" style="text-align:center; height:100%;">',
        unsafe_allow_html=True,
    )
    st.markdown("**COMPOSITE SIGNAL**")

    score = state.get("signal_score", 0.0)
    thresh = state.get("signal_threshold", 0.0)
    direc = state.get("signal_direction", "FLAT")

    dir_col = (
        "text-green" if direc == "YES" else "text-red" if direc == "NO" else "text-dim"
    )

    st.markdown(
        f"<h2 style='margin-bottom:0;' class='{get_color_class(score, thresh)}'>{score:+.4f}</h2>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div style='font-size:12px;' class='text-dim'>Threshold: ±{thresh:.4f}</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div style='margin-top:15px;'>DIRECTION: [<span class='{dir_col}'>{direc}</span>]</div>",
        unsafe_allow_html=True,
    )

    vol = state.get("realized_vol", 0.0)
    regime = state.get("vol_regime", "UNKNOWN")
    st.markdown(
        f"<div style='margin-top:10px; font-size:12px;'>VOLATILITY: {vol:.2f} ({regime})</div>",
        unsafe_allow_html=True,
    )

    st.markdown("</div>", unsafe_allow_html=True)

with col3:
    st.markdown('<div class="ascii-box">', unsafe_allow_html=True)
    st.markdown("**PRICING & EDGE**")

    fv_yes = state.get("fair_value_yes", 0.5)
    fv_no = state.get("fair_value_no", 0.5)
    mkt_yes = state.get("market_yes")
    mkt_no = state.get("market_no")
    edge_yes = state.get("edge_yes", 0.0)
    edge_no = state.get("edge_no", 0.0)
    src = state.get("price_source", "NONE")

    st.markdown(
        f"""
    <table>
        <tr>
            <th>DIR</th>
            <th>MODEL FV</th>
            <th>MARKET</th>
            <th>EDGE</th>
        </tr>
        <tr>
            <td class="text-green">YES</td>
            <td>{fv_yes:.4f}</td>
            <td>{f"${mkt_yes:.2f}" if mkt_yes else "---"}</td>
            <td class="{get_color_class(edge_yes)}">{edge_yes:+.4f}</td>
        </tr>
        <tr>
            <td class="text-red">NO</td>
            <td>{fv_no:.4f}</td>
            <td>{f"${mkt_no:.2f}" if mkt_no else "---"}</td>
            <td class="{get_color_class(edge_no)}">{edge_no:+.4f}</td>
        </tr>
    </table>
    <div style="margin-top:10px; font-size:12px; text-align:right;" class="text-dim">
        SOURCE: [{src}]
    </div>
    """,
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)


# --- Section 3: Charts ---
st.markdown("### ════ TACTICAL DISPLAY ════")
ts_data = get_signal_timeseries(limit=200)
if ts_data:
    df_ts = pd.DataFrame(ts_data)
    df_ts["dt"] = pd.to_datetime(df_ts["ts"], unit="s")

    fig = go.Figure()

    # Price
    fig.add_trace(
        go.Scatter(
            x=df_ts["dt"],
            y=df_ts["price"],
            line=dict(color="#00e5ff", width=1),
            name="BTC Price",
        )
    )

    # Strike
    if strike > 0:
        fig.add_hline(
            y=strike,
            line_dash="dash",
            line_color="#ffb000",
            annotation_text="STRIKE",
            annotation_position="top right",
        )

    # Annotations
    annos = get_trade_annotations()
    for a in annos:
        color = "#00ff41" if a["direction"] == "YES" else "#ff073a"
        symbol = "triangle-up" if a["direction"] == "YES" else "triangle-down"
        if a["action"] == "TRADE":
            fig.add_trace(
                go.Scatter(
                    x=[pd.to_datetime(a["ts"], unit="s")],
                    y=[a["price"]],
                    mode="markers",
                    marker=dict(
                        symbol=symbol,
                        size=12,
                        color=color,
                        line=dict(color="#0a0a0a", width=1),
                    ),
                    name=f"TRADE {a['direction']}",
                    hovertext=f"Edge: {a['edge']:+.4f}<br>Reason: {a['reason']}",
                )
            )

    fig.update_layout(
        height=300,
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="#0a0a0a",
        plot_bgcolor="#0a0a0a",
        font=dict(family="monospace", color="#555"),
        xaxis=dict(showgrid=True, gridcolor="#1a1a1a", gridwidth=1),
        yaxis=dict(showgrid=True, gridcolor="#1a1a1a", gridwidth=1),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.markdown(
        "<div class='ascii-box' style='text-align:center; color:#555;'>[WAITING FOR TACTICAL DATA...]</div>",
        unsafe_allow_html=True,
    )


# --- Section 4: Brain / Performance ---
col_L, col_R = st.columns(2)

with col_L:
    st.markdown("### ════ MODEL REASONING ════")
    t1, t2 = st.tabs(["[ DECISIONS ]", "[ EVALUATIONS ]"])

    with t1:
        decs = get_decisions(limit=20)
        lines = []
        for d in reversed(decs):
            ts_str = format_ts(d.get("ts"))
            act = d.get("action")
            if act == "TRADE":
                c = "text-green"
                head = f">>> TRADE {d.get('direction')} <<<"
            elif act == "SKIP":
                c = "text-amber"
                head = f"--- SKIP ---"
            else:
                c = "text-dim"
                head = f"~~~ {act} ~~~"

            lines.append(
                f"<span class='text-dim'>[{ts_str}]</span> <span class='{c}'>{head}</span>"
            )
            if act in ("TRADE", "SKIP"):
                lines.append(
                    f"  Score: {d.get('score', 0):+.4f} | Thresh: {d.get('threshold', 0):.4f}"
                )
                lines.append(
                    f"  Edge:  {d.get('edge', 0):+.4f} (FV: {d.get('fv', 0):.4f}, Mkt: {d.get('target_price', 0):.4f})"
                )
            if "reason" in d:
                lines.append(f"  Reason: <span class='text-cyan'>{d['reason']}</span>")
            lines.append("")

        if not lines:
            lines = ["No decisions logged yet."]
        st.markdown(
            f'<div class="terminal-log">{"<br>".join(lines)}<span class="cursor">_</span></div>',
            unsafe_allow_html=True,
        )

    with t2:
        evals = get_latest_evaluations(limit=30)
        lines = []
        for e in reversed(evals):
            ts_str = format_ts(e.get("ts"))
            sc = e.get("score", 0)
            c = "text-green" if sc > 0 else "text-red" if sc < 0 else "text-dim"
            lines.append(
                f"<span class='text-dim'>[{ts_str}]</span> <span class='{c}'>SCORE: {sc:+.4f}</span> | OBI: {e.get('obi_ema', 0):+.2f} CVD: {e.get('cvd_ema', 0):+.2f} | P: {e.get('price', 0):.1f}"
            )

        if not lines:
            lines = ["No evaluations logged yet."]
        st.markdown(
            f'<div class="terminal-log" style="font-size:10px;">{"<br>".join(lines)}<span class="cursor">_</span></div>',
            unsafe_allow_html=True,
        )


with col_R:
    st.markdown("### ════ SCOREBOARD ════")

    st.markdown(
        f"""
    <div class="ascii-box">
        <table style="font-size:14px;">
            <tr><td>Total Trades</td><td class="text-cyan">{perf.total_trades}</td></tr>
            <tr><td>Win Rate</td><td class="text-cyan">{perf.win_rate:.1f}%</td></tr>
            <tr><td>Total PnL</td><td class="{get_color_class(perf.total_pnl)}">${perf.total_pnl:+.2f}</td></tr>
            <tr><td>Profit Factor</td><td class="text-cyan">{perf.profit_factor:.2f}</td></tr>
            <tr><td>Avg PnL/Trade</td><td class="{get_color_class(perf.avg_pnl_per_trade)}">${perf.avg_pnl_per_trade:+.2f}</td></tr>
            <tr><td>Avg Edge@Entry</td><td class="text-cyan">{perf.avg_edge_at_entry:+.4f}</td></tr>
            <tr><td>Current Streak</td><td class="text-cyan">{perf.consecutive_wins}W / {perf.consecutive_losses}L</td></tr>
            <tr><td>Pending Trades</td><td class="text-amber">{perf.pending}</td></tr>
        </table>
    </div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown("**RECENT TRADES**")
    settled = get_settled_trades()
    if settled:
        trs = ""
        for t in reversed(settled[-5:]):
            c = "text-green" if t.get("correct") else "text-red"
            trs += f"""
            <tr>
                <td>{format_ts(t.get("ts"))}</td>
                <td>{t.get("direction")}</td>
                <td>${t.get("entry_price", 0):.2f}</td>
                <td class="{c}">${t.get("pnl", 0):+.2f}</td>
            </tr>
            """
        st.markdown(
            f"""
        <table style="font-size:12px;">
            <tr><th>TIME</th><th>DIR</th><th>ENTRY</th><th>PNL</th></tr>
            {trs}
        </table>
        """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<span class='text-dim'>[NO COMPLETED TRADES]</span>",
            unsafe_allow_html=True,
        )


# --- Section 7: System Logs ---
st.markdown("### ════ SYSTEM DIAGNOSTICS ════")
with st.expander("[ VIEW RAW SYSTEM LOGS ]"):
    sys_logs = get_system_logs(limit=100)
    formatted_logs = []
    for l in reversed(sys_logs):
        if "[ERROR]" in l:
            c = "text-red"
        elif "[WARN]" in l:
            c = "text-amber"
        else:
            c = "text-dim"
        formatted_logs.append(f"<span class='{c}'>{l.strip()}</span>")

    st.markdown(
        f'<div class="terminal-log" style="height:300px;">{"<br>".join(formatted_logs)}<span class="cursor">_</span></div>',
        unsafe_allow_html=True,
    )

# Auto-refresh
time.sleep(2)
st.rerun()
