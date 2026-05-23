import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

PARQUET        = os.path.join(os.path.dirname(__file__), "data_source", "seasonality_demand.parquet")
PRODFAM_PARQUET = os.path.join(os.path.dirname(__file__), "data_source", "prodfam_seasonality.parquet")

st.set_page_config(
    page_title="Seasonality Dashboard",
    page_icon=None,
    layout="wide",
)

@st.cache_data(show_spinner="Loading seasonality dataset...")
def load_data():
    df = pd.read_parquet(PARQUET)
    df["ACCOUNT"] = df["ACCOUNT"].str.title()
    df = df[df["WEEK_NUM"].between(1, 52)]
    for col in ("VELOCITY_CLASS", "VELOCITY_OVERALL", "LIFECYCLE_STATUS", "LIFECYCLE_OVERALL"):
        if col not in df.columns:
            df[col] = "Unclassified"
    return df

@st.cache_data(show_spinner=False)
def load_prodfam():
    return pd.read_parquet(PRODFAM_PARQUET)

df_all = load_data()
df_pf  = load_prodfam()

# ── Sidebar filters ───────────────────────────────────────────────────────────
st.sidebar.title("Filters")

st.sidebar.divider()
all_accounts = sorted(df_all["ACCOUNT"].dropna().unique())
sel_accounts = st.sidebar.multiselect(
    f"Account ({len(all_accounts)})", all_accounts, placeholder="All accounts"
)
df = df_all.copy()
if sel_accounts:
    df = df[df["ACCOUNT"].isin(sel_accounts)]

# ── YEAR + year-specific filters ─────────────────────────────────────────────
st.sidebar.divider()
st.sidebar.markdown("**By Year**")
all_years = sorted(df["YEAR"].dropna().unique().astype(int))
sel_years = st.sidebar.multiselect(f"Year ({len(all_years)})", all_years, placeholder="All years")
if sel_years:
    df = df[df["YEAR"].isin(sel_years)]

year_label    = str(sel_years[0]) if len(sel_years) == 1 else "selected year"
velocity_order = ["High Velocity", "Moderate Velocity", "Low Velocity", "Dormant", "No Data"]
lifecycle_order = ["New", "Evergreen", "Declining", "Discontinued", "No Data"]

all_vel_year = [v for v in velocity_order if v in df["VELOCITY_CLASS"].unique()]
sel_vel_year = st.sidebar.multiselect(
    f"Velocity in {year_label}", all_vel_year, placeholder="All classes"
)
if sel_vel_year:
    df = df[df["VELOCITY_CLASS"].isin(sel_vel_year)]

all_lc_year = [v for v in lifecycle_order if v in df["LIFECYCLE_STATUS"].unique()]
sel_lc_year = st.sidebar.multiselect(
    f"Lifecycle in {year_label}", all_lc_year, placeholder="All statuses"
)
if sel_lc_year:
    df = df[df["LIFECYCLE_STATUS"].isin(sel_lc_year)]

# ── OVERALL filters ───────────────────────────────────────────────────────────
st.sidebar.divider()
st.sidebar.markdown("**Overall (All Years)**")

all_vel_overall = [v for v in velocity_order if v in df["VELOCITY_OVERALL"].unique()]
sel_vel_overall = st.sidebar.multiselect(
    f"Overall Velocity", all_vel_overall, placeholder="All classes"
)
if sel_vel_overall:
    df = df[df["VELOCITY_OVERALL"].isin(sel_vel_overall)]

all_lc_overall = [v for v in lifecycle_order if v in df["LIFECYCLE_OVERALL"].unique()]
sel_lc_overall = st.sidebar.multiselect(
    f"Overall Lifecycle", all_lc_overall, placeholder="All statuses"
)
if sel_lc_overall:
    df = df[df["LIFECYCLE_OVERALL"].isin(sel_lc_overall)]

# ── PRODUCT filters ───────────────────────────────────────────────────────────
st.sidebar.divider()
st.sidebar.markdown("**Product**")
all_fam = sorted(df["PRODFAM"].replace("", pd.NA).dropna().unique())
sel_fam = st.sidebar.multiselect(f"Product Family ({len(all_fam)})", all_fam, placeholder="All families")
if sel_fam:
    df = df[df["PRODFAM"].isin(sel_fam)]

all_brands = sorted(df["BRAND"].replace("", pd.NA).dropna().unique())
sel_brands = st.sidebar.multiselect(f"Brand ({len(all_brands)})", all_brands, placeholder="All brands")
if sel_brands:
    df = df[df["BRAND"].isin(sel_brands)]

all_vendors = sorted(df["VENDOR"].replace("", pd.NA).dropna().unique())
sel_vendors = st.sidebar.multiselect(f"Supplier ({len(all_vendors)})", all_vendors, placeholder="All suppliers")
if sel_vendors:
    df = df[df["VENDOR"].isin(sel_vendors)]

all_items = sorted(df["ITEM_DESCR"].replace("", pd.NA).dropna().unique())
sel_items = st.sidebar.multiselect(f"Item ({len(all_items)})", all_items, placeholder="All items")
if sel_items:
    df = df[df["ITEM_DESCR"].isin(sel_items)]

st.sidebar.divider()
show_zeros = st.sidebar.toggle("Show zero-demand weeks in chart", value=False)

# ── Determine active product families for SI profile chart ───────────────────
# Collect all families in context: from direct family selection or inferred from items
if sel_fam:
    active_prodfams = list(sel_fam)
elif sel_items:
    active_prodfams = sorted(
        df_all[df_all["ITEM_DESCR"].isin(sel_items)]["PRODFAM"].dropna().unique()
    )
else:
    active_prodfams = []

# Keep single-family alias for backward compat (item detail card uses it)
active_prodfam = active_prodfams[0] if len(active_prodfams) == 1 else None

FAMILY_COLORS = {
    "BEER":        "#F4A300",
    "WINE":        "#8B1A4A",
    "SPIRITS":     "#1F77B4",
    "NON-ALCOHOL": "#2CA02C",
    "CBD":         "#9467BD",
}
_DEFAULT_COLORS = ["#E377C2", "#17BECF", "#BCBD22", "#FF7F0E", "#D62728"]

# ── Header ────────────────────────────────────────────────────────────────────
account_label = ", ".join(sel_accounts) if sel_accounts else f"All {len(all_accounts)} accounts"
st.title("Seasonality Dashboard — Top 20 Accounts")
st.caption(
    f"{account_label}  |  "
    f"SI > 1.0 = above baseline (peak season)  |  SI < 1.0 = below baseline (off season)"
)

# ── Item Detail Cards (one per selected item) ─────────────────────────────────
def render_item_card(item_name, container=st):
    velocity_colors  = {"High Velocity": "#00B050", "Moderate Velocity": "#FFC000",
                        "Low Velocity": "#FF6600", "Dormant": "#FF0000"}
    lifecycle_colors = {"New": "#4472C4", "Evergreen": "#00B050",
                        "Declining": "#FF6600", "Discontinued": "#FF0000"}

    _rows = df_all[df_all["ITEM_DESCR"] == item_name]
    if _rows.empty:
        _rows = df[df["ITEM_DESCR"] == item_name]
    if _rows.empty:
        container.warning(f"No data found for: {item_name}")
        return
    item_data = _rows.iloc[0]

    vel     = item_data.get("VELOCITY_CLASS", "—")
    overall = item_data.get("LIFECYCLE_OVERALL", "—")
    vcol    = velocity_colors.get(vel, "#888888")
    ocol    = lifecycle_colors.get(overall, "#888888")

    lc_years = (
        df_all[df_all["ITEM_DESCR"] == item_name]
        .drop_duplicates(subset=["YEAR"])
        .sort_values("YEAR")[["YEAR", "LIFECYCLE_STATUS"]]
    )
    year_badges = "".join(
        f'<span style="background:{lifecycle_colors.get(r.LIFECYCLE_STATUS,"#555")};'
        f'color:#fff;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;margin-right:6px;">'
        f'{int(r.YEAR)}: {r.LIFECYCLE_STATUS}</span>'
        for _, r in lc_years.iterrows()
    )
    total_sales = int(df[df["ITEM_DESCR"] == item_name]["ACTUAL_WEEKLY_DEMAND"].sum())

    container.markdown(
        f"""
        <div style="background:#1e1e2e;border:1px solid #444;border-radius:10px;padding:18px 24px;margin-bottom:16px;">
            <div style="font-size:18px;font-weight:700;color:#ffffff;margin-bottom:6px;">
                {item_data['ITEM_DESCR']}
            </div>
            <div style="font-size:12px;color:#aaaaaa;margin-bottom:12px;">
                ID: <b style="color:#fff">{item_data['ITEM_ID']}</b>
                &nbsp;|&nbsp; Brand: <b style="color:#fff">{item_data['BRAND']}</b>
                &nbsp;|&nbsp; Vendor: <b style="color:#fff">{item_data['VENDOR']}</b>
                &nbsp;|&nbsp; Family: <b style="color:#fff">{item_data['PRODFAM']}</b>
            </div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;">
                <span style="background:{vcol};color:#fff;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:600;">
                    ⚡ {vel}
                </span>
                <span style="background:{ocol};color:#fff;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:600;">
                    🔄 {overall}
                </span>
                <span style="background:#2a2a3e;color:#ccc;padding:3px 12px;border-radius:20px;font-size:12px;">
                    📦 <b style="color:#fff">{total_sales:,}</b> units
                </span>
                <span style="background:#2a2a3e;color:#ccc;padding:3px 12px;border-radius:20px;font-size:12px;">
                    📈 <b style="color:#fff">{item_data['BASELINE_DEMAND']:,.1f}</b>/wk baseline
                </span>
            </div>
            <div style="font-size:11px;color:#888;margin-bottom:5px;">Lifecycle by year:</div>
            <div>{year_badges}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

if sel_items:
    if len(sel_items) == 1:
        render_item_card(sel_items[0])
    else:
        for i in range(0, len(sel_items), 2):
            cols = st.columns(2)
            render_item_card(sel_items[i], cols[0])
            if i + 1 < len(sel_items):
                render_item_card(sel_items[i + 1], cols[1])
    st.divider()

# ── KPI cards ─────────────────────────────────────────────────────────────────
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total Actual Demand",   f"{df['ACTUAL_WEEKLY_DEMAND'].sum():,.0f}")
k2.metric("Avg Baseline / Week",   f"{df['BASELINE_DEMAND'].mean():,.1f}" if not df.empty else "—")
k3.metric("Avg Seasonality Index", f"{df['SEASONALITY_INDEX'].mean():,.2f}" if not df.empty else "—")

si_by_week = df.groupby("WEEK_NUM")["SEASONALITY_INDEX"].mean().dropna()
if not si_by_week.empty:
    k4.metric("Peak SI Week",   f"W{int(si_by_week.idxmax()):02d} ({si_by_week.max():.2f})")
    k5.metric("Trough SI Week", f"W{int(si_by_week.idxmin()):02d} ({si_by_week.min():.2f})")
else:
    k4.metric("Peak SI Week",   "—")
    k5.metric("Trough SI Week", "—")

st.divider()

# ── Chart 1: Actual vs Baseline vs Seasonal Baseline ─────────────────────────
st.subheader("Actual Demand vs Baseline vs Seasonal Baseline")

# Build week scaffold up to the last week that has actual data
_years_in_view = sorted(set(sel_years) if sel_years else set(df_all["YEAR"].unique()))
_last_week = df_all["WEEK_LABEL"].max()  # e.g. "2026-W13"
_full_weeks = [
    f"{yr}-W{wk:02d}"
    for yr in _years_in_view
    for wk in range(1, 53)
    if f"{yr}-W{wk:02d}" <= _last_week
]
scaffold = pd.DataFrame({"WEEK_LABEL": _full_weeks})

actual = (
    df[df["ACTUAL_WEEKLY_DEMAND"] > 0]
    .groupby("WEEK_LABEL")["ACTUAL_WEEKLY_DEMAND"]
    .sum()
    .reset_index()
    .rename(columns={"ACTUAL_WEEKLY_DEMAND": "ACTUAL"})
)

if show_zeros:
    trend = scaffold.merge(actual, on="WEEK_LABEL", how="left")
    trend["ACTUAL"] = trend["ACTUAL"].fillna(0)
else:
    trend = actual.copy()
trend["WEEK_NUM_VAL"] = trend["WEEK_LABEL"].str.extract(r"W(\d+)")[0].astype(int)
trend = trend.sort_values("WEEK_LABEL").reset_index(drop=True)

# Flat baseline anchors seasonal lines to the actual demand scale
_nonzero  = trend.loc[trend["ACTUAL"] > 0, "ACTUAL"]
flat_base = float(_nonzero.mean()) if len(_nonzero) > 0 else 0.0

fig_trend = go.Figure()
fig_trend.add_bar(
    x=trend["WEEK_LABEL"], y=trend["ACTUAL"],
    name="Actual Demand", marker_color="#4472C4", opacity=0.8,
)


for i, pf in enumerate(active_prodfams):
    _pf_si = df_pf[df_pf["PRODFAM"] == pf].set_index("WEEK_NUM")["SEASONALITY_INDEX"]
    _color = FAMILY_COLORS.get(pf, _DEFAULT_COLORS[i % len(_DEFAULT_COLORS)])
    fig_trend.add_scatter(
        x=trend["WEEK_LABEL"],
        y=trend["WEEK_NUM_VAL"].map(_pf_si).fillna(0) * flat_base,
        mode="lines", name=f"Seasonal Baseline ({pf})",
        line=dict(color=_color, width=2),
    )

# Year separator lines — vertical dashed line + label at W01 of each year
for yr in _years_in_view:
    x_pos = f"{yr}-W01"
    fig_trend.add_vline(
        x=x_pos, line_dash="dash", line_color="rgba(255,255,255,0.25)", line_width=1,
    )
    fig_trend.add_annotation(
        x=x_pos, y=1, yref="paper",
        text=str(yr), showarrow=False,
        font=dict(color="rgba(255,255,255,0.55)", size=12),
        xanchor="left", yanchor="top", xshift=4,
    )

fig_trend.update_layout(
    height=400, margin=dict(l=0, r=0, t=30, b=0),
    legend=dict(orientation="h", y=1.08),
)
fig_trend.update_xaxes(tickangle=45, nticks=40)
st.plotly_chart(fig_trend, use_container_width=True)

# ── Seasonal Index Profile (one line per active family) ───────────────────────
if active_prodfams:
    title_fams = " vs ".join(active_prodfams)
    st.subheader(f"Seasonal Index Profile — {title_fams}")
    fig_si = go.Figure()
    for i, pf in enumerate(active_prodfams):
        pf_si  = df_pf[df_pf["PRODFAM"] == pf].sort_values("WEEK_NUM").reset_index(drop=True)
        _color = FAMILY_COLORS.get(pf, _DEFAULT_COLORS[i % len(_DEFAULT_COLORS)])
        fig_si.add_scatter(
            x=pf_si["WEEK_NUM"], y=pf_si["SEASONALITY_INDEX"],
            mode="lines+markers", name=pf,
            line=dict(color=_color, width=2.5),
            marker=dict(size=5),
        )
    fig_si.add_hline(
        y=1.0, line_dash="dash", line_color="#888888",
        annotation_text="Baseline (SI = 1.0)", annotation_position="bottom right",
    )
    fig_si.update_layout(
        height=300, margin=dict(l=0, r=0, t=30, b=0),
        yaxis_title="Seasonality Index",
        xaxis_title="Week of Year",
        legend=dict(orientation="h", y=1.08),
    )
    fig_si.update_xaxes(tickmode="linear", tick0=1, dtick=4,
                        ticktext=[f"W{w:02d}" for w in range(1, 53, 4)],
                        tickvals=list(range(1, 53, 4)))
    st.plotly_chart(fig_si, use_container_width=True)

st.divider()

# ── Charts: Most Seasonal & Most Stable ──────────────────────────────────────
col_left, col_right = st.columns(2)

with col_left:
    st.subheader("Most Seasonal Items (Highest Peak SI)")
    peak_si = (
        df.groupby(["ITEM_ID", "ITEM_DESCR"])["SEASONALITY_INDEX"]
        .max().reset_index()
        .nlargest(15, "SEASONALITY_INDEX")
        .sort_values("SEASONALITY_INDEX")
    )
    peak_si["LABEL"] = peak_si["ITEM_ID"].astype(str) + " " + peak_si["ITEM_DESCR"]
    fig_peak = px.bar(
        peak_si, x="SEASONALITY_INDEX", y="LABEL", orientation="h",
        labels={"SEASONALITY_INDEX": "Peak Seasonality Index", "LABEL": "Item"},
        height=420, color="SEASONALITY_INDEX", color_continuous_scale="Blues",
    )
    fig_peak.update_layout(margin=dict(l=0, r=0, t=20, b=0), coloraxis_showscale=False, yaxis_title="")
    st.plotly_chart(fig_peak, use_container_width=True)

with col_right:
    st.subheader("Most Stable Items (Lowest SI Variation)")
    stable = (
        df.groupby(["ITEM_ID", "ITEM_DESCR"])["SEASONALITY_INDEX"]
        .std().reset_index()
        .dropna()
        .nsmallest(15, "SEASONALITY_INDEX")
        .sort_values("SEASONALITY_INDEX", ascending=False)
    )
    stable["LABEL"] = stable["ITEM_ID"].astype(str) + " " + stable["ITEM_DESCR"]
    fig_stable = px.bar(
        stable, x="SEASONALITY_INDEX", y="LABEL", orientation="h",
        labels={"SEASONALITY_INDEX": "SI Std Dev (lower = more stable)", "LABEL": "Item"},
        height=420, color="SEASONALITY_INDEX", color_continuous_scale="Greens_r",
    )
    fig_stable.update_layout(margin=dict(l=0, r=0, t=20, b=0), coloraxis_showscale=False, yaxis_title="")
    st.plotly_chart(fig_stable, use_container_width=True)

st.divider()

# ── Data table ────────────────────────────────────────────────────────────────
st.subheader("Filtered Data")

display_df = (
    df[["ACCOUNT", "ITEM_ID", "ITEM_DESCR", "YEAR", "WEEK_NUM", "WEEK_LABEL",
        "BRAND", "VENDOR", "PRODFAM", "VELOCITY_CLASS",
        "LIFECYCLE_OVERALL", "LIFECYCLE_STATUS",
        "ACTUAL_WEEKLY_DEMAND", "BASELINE_DEMAND",
        "SEASONALITY_INDEX", "SEASONAL_BASELINE"]]
    .sort_values(["ACCOUNT", "ITEM_DESCR", "YEAR", "WEEK_NUM"])
    .reset_index(drop=True)
)
display_df.columns = [
    "Account", "Item ID", "Item", "Year", "Week No", "Week",
    "Brand", "Vendor", "Product Family", "Velocity Class",
    "Lifecycle (Overall)", "Lifecycle (Year)",
    "Actual Weekly Demand", "Baseline Demand",
    "Seasonality Index", "Seasonal Baseline",
]

st.dataframe(display_df, use_container_width=True, height=400)
st.caption(f"Showing {len(display_df):,} rows after filters.")
