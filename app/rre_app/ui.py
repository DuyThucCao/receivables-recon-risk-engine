"""Presentation helpers shared by the screens: headers, data-label badges, number formats, the
"not built yet" notice and a restrained Plotly style that reads in light and dark themes."""
from __future__ import annotations

from typing import Iterable

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

LABEL_TEXT = {
    "synthetic_record": "simulated record",
    "model_estimate": "model estimate",
    "financial_assumption": "financial assumption",
    "observed_public": "observed public data",
    "published_aggregate": "published aggregate",
}
LABEL_COLOR = {  # Streamlit badge colours; the label name is always printed, colour is secondary
    "synthetic_record": "gray", "model_estimate": "blue", "financial_assumption": "orange",
    "observed_public": "green", "published_aggregate": "violet",
}

# Categorical slots in fixed order (validated palette, mid-tone steps legible on light and dark).
# Colour follows the entity: each payer keeps its slot on every screen.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#c98500", "#d55181", "#0ca30c", "#6250d6", "#e34948"]
PAYER_COLOR = {f"P0{i + 1}": SERIES[i] for i in range(6)}
NEUTRAL = "#898781"
ACCENT = SERIES[0]
NEGATIVE = "#d03b3b"


def badge(label: str) -> str:
    """Markdown badge for a data label, e.g. :blue-badge[model_estimate]."""
    return f":{LABEL_COLOR.get(label, 'gray')}-badge[{label}]"


def header(job_title: str, question: str, intro: str | None = None) -> None:
    st.markdown(f"<div class='rre-kicker'>{job_title}</div>", unsafe_allow_html=True)
    st.title(question)
    if intro:
        st.markdown(intro)


def simulated_note() -> None:
    st.caption("All records are simulated for a fictional hospital (Meridian Health System) and six fictional payers. "
               "Numbers show how the method works; they are not findings about real payers, banks or drugs.")


def labelled(text: str, label: str) -> None:
    st.markdown(f"{text} &nbsp; {badge(label)}")


def metric(col, title: str, value: str, label: str, help: str | None = None, delta: str | None = None) -> None:
    with col:
        st.metric(title, value, delta=delta, help=help, delta_color="off")
        st.markdown(badge(label))


def not_built(marts: Iterable[str], what: str = "This screen") -> None:
    names = ", ".join(f"`{m}`" for m in marts)
    st.warning(f"**Not built yet.** {what} needs {names}, which the model has not written to this database. "
               "Run `python run_all.py --skip-generate` (or the model script) and "
               "press **Reset demo database** on the reconciliation desk to pick up the new tables.")


def usd(x, digits: int = 0) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "n/a"
    x = float(x)
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.{digits}f}"


def usd_short(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "n/a"
    x = float(x)
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.2f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.0f}K"
    return f"{sign}${a:,.0f}"


def pct(x, digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "n/a"
    return f"{100 * float(x):.{digits}f}%"


def style(fig: go.Figure, height: int = 360, y_title: str | None = None, x_title: str | None = None,
          money: bool = True, legend: bool = True) -> go.Figure:
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=36, b=8), hovermode="closest",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None) if legend else None,
                      showlegend=legend, bargap=0.25, font=dict(size=13))
    fig.update_xaxes(title=x_title, showgrid=False)
    fig.update_yaxes(title=y_title, gridwidth=1, zeroline=True, zerolinewidth=1)
    if money:
        fig.update_yaxes(tickprefix="$", separatethousands=True)
    return fig


def chart(fig: go.Figure, label: str, source: str, key: str | None = None) -> None:
    """Plot with the data label and source printed under it (every chart carries one)."""
    st.plotly_chart(fig, theme="streamlit", key=key)
    st.caption(f"Data label: **{label}** ({LABEL_TEXT.get(label, label)}). Source: {source}")


def money_col(label: str | None = None):
    """Money column: thousands separators and two decimals regardless of the browser locale
    (Streamlit's "dollar" format follows the viewer's locale; "accounting" does not)."""
    return st.column_config.NumberColumn(f"{label} ($)" if label else None, format="accounting")


def table(df: pd.DataFrame, label: str, source: str, money_cols: Iterable[str] = (), pct_cols: Iterable[str] = (),
          height: int | None = None, hide_index: bool = True, **kw) -> None:
    df = df.copy()
    cfg = {c: money_col(c) for c in money_cols if c in df}
    for c in pct_cols:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce") * 100
            cfg[c] = st.column_config.NumberColumn(f"{c} (%)", format="%.2f")
    st.dataframe(df, hide_index=hide_index, column_config=cfg, height=height or "auto", **kw)
    st.caption(f"Data label: **{label}** ({LABEL_TEXT.get(label, label)}). Source: {source}")


CSS = """
<style>
.rre-kicker { font-size: 0.8rem; letter-spacing: 0.06em; text-transform: uppercase; opacity: 0.7; margin-bottom: -0.6rem; }
h1 { font-size: 1.85rem !important; font-weight: 650 !important; }
h2, h3 { font-weight: 600 !important; }
[data-testid="stMetricValue"] { font-size: 1.6rem; }
section[data-testid="stSidebar"] .rre-side { font-size: 0.82rem; opacity: 0.8; }
</style>
"""


def page_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
