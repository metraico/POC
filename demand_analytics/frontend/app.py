import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

PARQUET = os.path.join(os.path.dirname(__file__), "data_source", "seasonality_demand.parquet")

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

df_all = load_data()

# ── Sidebar filters ───────────────────────────────────────────────────────────
st.sidebar.title("Filters")

st.sidebar.divider()
all_accounts = sorted(df_all["ACCOUNT"].dropna().unique())
sel_accounts = st.sidebar.multiselect(
    f"Account ({len(all_accounts)})", all_accounts, placeholder="All accounts"
)
df = df_all[df_all["ACCOUNT"].isin(sel_accounts)] if sel_accounts else df_all

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

# ── Header ────────────────────────────────────────────────────────────────────
account_label = ", ".join(sel_accounts) if sel_accounts else f"All {len(all_accounts)} accounts"
st.title("Seasonality Dashboard — Top 20 Accounts")
st.caption(
    f"{account_label}  |  "
    f"SI > 1.0 = above baseline (peak season)  |  SI < 1.0 = below baseline (off season)"
)

# ── Item Detail Card (shown only when exactly one item is selected) ────────────
if len(sel_items) == 1:
    _item_rows = df_all[df_all["ITEM_DESCR"] == sel_items[0]]
    if _item_rows.empty:
        _item_rows = df[df["ITEM_DESCR"] == sel_items[0]]
    if _item_rows.empty:
        st.warning(f"No data found for selected item: {sel_items[0]}")
        st.stop()
    item_data = _item_rows.iloc[0]

    velocity_colors  = {"High Velocity": "#00B050", "Moderate Velocity": "#FFC000",
                        "Low Velocity": "#FF6600", "Dormant": "#FF0000"}
    lifecycle_colors = {"New": "#4472C4", "Evergreen": "#00B050",
                        "Declining": "#FF6600", "Discontinued": "#FF0000"}

    vel     = item_data.get("VELOCITY_CLASS", "—")
    overall = item_data.get("LIFECYCLE_OVERALL", "—")
    vcol    = velocity_colors.get(vel, "#888888")
    ocol    = lifecycle_colors.get(overall, "#888888")

    # Per-year lifecycle badges from the full (unfiltered) dataset for this item
    lc_years = (
        df_all[df_all["ITEM_DESCR"] == sel_items[0]]
        .drop_duplicates(subset=["YEAR"])
        .sort_values("YEAR")[["YEAR", "LIFECYCLE_STATUS"]]
    )
    year_badges = "".join(
        f'<span style="background:{lifecycle_colors.get(r.LIFECYCLE_STATUS,"#555")};'
        f'color:#fff;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;margin-right:6px;">'
        f'{int(r.YEAR)}: {r.LIFECYCLE_STATUS}</span>'
        for _, r in lc_years.iterrows()
    )

    st.markdown(
        f"""
        <div style="background:#1e1e2e;border:1px solid #444;border-radius:10px;padding:18px 24px;margin-bottom:16px;">
            <div style="font-size:20px;font-weight:700;color:#ffffff;margin-bottom:6px;">
                {item_data['ITEM_DESCR']}
            </div>
            <div style="font-size:13px;color:#aaaaaa;margin-bottom:14px;">
                Item ID: <b style="color:#fff">{item_data['ITEM_ID']}</b>
                &nbsp;|&nbsp; Brand: <b style="color:#fff">{item_data['BRAND']}</b>
                &nbsp;|&nbsp; Vendor: <b style="color:#fff">{item_data['VENDOR']}</b>
                &nbsp;|&nbsp; Product Family: <b style="color:#fff">{item_data['PRODFAM']}</b>
            </div>
            <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px;">
                <span style="background:{vcol};color:#fff;padding:4px 14px;border-radius:20px;font-size:13px;font-weight:600;">
                    ⚡ {vel}
                </span>
                <span style="background:{ocol};color:#fff;padding:4px 14px;border-radius:20px;font-size:13px;font-weight:600;">
                    🔄 Overall: {overall}
                </span>
                <span style="background:#2a2a3e;color:#ccc;padding:4px 14px;border-radius:20px;font-size:13px;">
                    📦 Total Sales: <b style="color:#fff">{int(df[df['ITEM_DESCR']==sel_items[0]]['ACTUAL_WEEKLY_DEMAND'].sum()):,}</b>
                </span>
                <span style="background:#2a2a3e;color:#ccc;padding:4px 14px;border-radius:20px;font-size:13px;">
                    📈 Baseline/week: <b style="color:#fff">{item_data['BASELINE_DEMAND']:,.1f}</b>
                </span>
            </div>
            <div style="font-size:12px;color:#888;margin-bottom:6px;">Lifecycle by year:</div>
            <div>{year_badges}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
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

# Build a complete week scaffold (W01–W52 for every year in view)
# Generated mathematically so future/missing weeks are always included
_years_in_view = sorted(set(sel_years) if sel_years else set(df_all["YEAR"].unique()))
_full_weeks = [
    f"{yr}-W{wk:02d}"
    for yr in _years_in_view
    for wk in range(1, 53)
]
scaffold = pd.DataFrame({"WEEK_LABEL": _full_weeks})

# Actual bars respect the toggle; baseline lines always use full data so they never drop to 0
df_chart = df if show_zeros else df[df["ACTUAL_WEEKLY_DEMAND"] > 0]

actual = (
    df_chart.groupby("WEEK_LABEL")["ACTUAL_WEEKLY_DEMAND"]
    .sum()
    .reset_index()
    .rename(columns={"ACTUAL_WEEKLY_DEMAND": "ACTUAL"})
)

baselines = (
    df.groupby("WEEK_LABEL")
    .agg(
        BASELINE=("BASELINE_DEMAND", "mean"),
        SEASONAL_BASELINE=("SEASONAL_BASELINE", "sum"),
    )
    .reset_index()
)

trend = scaffold.merge(actual, on="WEEK_LABEL", how="left")
trend = trend.merge(baselines, on="WEEK_LABEL", how="left")
trend["ACTUAL"] = trend["ACTUAL"].fillna(0)
trend = trend.sort_values("WEEK_LABEL").reset_index(drop=True)

fig_trend = go.Figure()
fig_trend.add_bar(
    x=trend["WEEK_LABEL"], y=trend["ACTUAL"],
    name="Actual Demand", marker_color="#4472C4", opacity=0.8,
)
fig_trend.add_scatter(
    x=trend["WEEK_LABEL"], y=trend["SEASONAL_BASELINE"],
    mode="lines", name="Seasonal Baseline",
    line=dict(color="#FF0000", width=2),
)
fig_trend.add_scatter(
    x=trend["WEEK_LABEL"], y=trend["BASELINE"],
    mode="lines", name="Flat Baseline",
    line=dict(color="#70AD47", width=1.5, dash="dash"),
)
fig_trend.update_layout(
    height=400, margin=dict(l=0, r=0, t=30, b=0),
    legend=dict(orientation="h", y=1.08),
)
fig_trend.update_xaxes(tickangle=45, nticks=40)
st.plotly_chart(fig_trend, use_container_width=True)

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

st.dataframe(display_df.head(2000), use_container_width=True, height=400)
st.caption(f"Showing {min(len(display_df), 2000):,} of {len(display_df):,} rows after filters.")
