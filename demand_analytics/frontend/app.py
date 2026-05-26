import os
import datetime
import numpy as np
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

# ── On-and-off detection helpers ──────────────────────────────────────────────
MONTH_NAMES = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
               7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}

def week_to_month(week: int) -> str:
    try:
        return MONTH_NAMES[datetime.date.fromisocalendar(2024, int(week), 4).month]
    except Exception:
        return ""

def find_clusters(weeks: list, gap: int) -> list:
    if not weeks:
        return []
    weeks = sorted(weeks)
    clusters, cur = [], [weeks[0]]
    for w in weeks[1:]:
        if w - cur[-1] >= gap:
            clusters.append(cur); cur = [w]
        else:
            cur.append(w)
    clusters.append(cur)
    return clusters

@st.cache_data(show_spinner="Detecting on-and-off products…")
def detect_on_off(demand_df: pd.DataFrame, window: int, gap: int,
                  max_active: int = 20, min_years: int = 2) -> pd.DataFrame:
    records = []
    for item_id, grp in demand_df.groupby("ITEM_ID"):
        years = sorted(grp["YEAR"].unique())
        if len(years) < min_years:
            continue
        ywm = {yr: sorted(grp[(grp["YEAR"]==yr) & (grp["ACTUAL_WEEKLY_DEMAND"]>0)]["WEEK_NUM"].unique().tolist())
               for yr in years}
        if max(len(v) for v in ywm.values()) == 0:
            continue
        if max(len(v) for v in ywm.values()) > max_active:
            continue
        yc = {yr: find_clusters(ws, gap) for yr, ws in ywm.items() if ws}
        if not yc:
            continue
        peak_counts  = [len(c) for c in yc.values()]
        common_count = max(set(peak_counts), key=peak_counts.count)
        consistent   = {yr: c for yr, c in yc.items() if len(c) == common_count}
        if len(consistent) < min_years:
            continue
        all_active   = [w for ws in ywm.values() for w in ws]
        spread       = float(np.percentile(all_active, 90) - np.percentile(all_active, 10))
        median_week  = float(np.median(all_active))
        if common_count == 1:
            lo, hi = median_week - window, median_week + window
            yw = [yr for yr, c in consistent.items() if any(lo <= w <= hi for w in c[0])]
            if len(yw) < min_years:
                continue
            pattern  = f"Single-peak W{int(round(median_week))}"
            yrs_conf = len(yw)
        else:
            centers = {yr: [int(np.median(c)) for c in cl] for yr, cl in consistent.items()}
            if not all(max(centers[yr][s] for yr in consistent) - min(centers[yr][s] for yr in consistent) <= window * 2
                       for s in range(common_count)):
                continue
            labels  = [f"W{int(round(np.median([centers[yr][s] for yr in consistent])))}" for s in range(common_count)]
            pattern  = f"{common_count}-peak ({', '.join(labels)})"
            yrs_conf = len(consistent)
        row = grp.iloc[0]
        records.append({
            "ITEM_ID":          item_id,
            "ITEM_DESCR":       row["ITEM_DESCR"],
            "BRAND":            row["BRAND"],
            "VENDOR":           row.get("VENDOR", ""),
            "PRODFAM":          row["PRODFAM"],
            "ACCOUNTS_SELLING": grp["ACCOUNT"].nunique(),
            "ACCOUNTS_LIST":    ", ".join(sorted(grp["ACCOUNT"].unique().tolist())),
            "PEAK_WEEK":        int(round(median_week)),
            "PEAK_MONTH":       week_to_month(int(round(median_week))),
            "PATTERN":          pattern,
            "WEEK_SPREAD":      round(spread, 1),
            "YEARS_CONFIRMED":  yrs_conf,
            "MAX_ACTIVE_WKS_YR": max(len(v) for v in ywm.values()),
        })
    return pd.DataFrame(records)

df_all = load_data()
df_pf  = load_prodfam()

# ── Tabs ─────────────────────────────────────────────────────────────────────
tab_season, tab_onoff = st.tabs(["Seasonality", "In-and-Out Products"])

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

# ── On-and-Off sidebar sliders ────────────────────────────────────────────────
st.sidebar.divider()
st.sidebar.markdown("**In-and-Out Detection**")
oo_window = st.sidebar.slider(
    "Single-peak window (± weeks)", min_value=0, max_value=8, value=3, step=1,
    help="How strictly must the peak week align year-to-year for single-peak items.",
)
oo_gap = st.sidebar.slider(
    "Multi-peak gap threshold (weeks)", min_value=2, max_value=8, value=6, step=1,
    help="Silence of N+ consecutive weeks splits demand into separate seasonal peaks.",
)
st.sidebar.caption(
    f"Single-peak: same week ±{oo_window}w each year.  "
    f"Multi-peak: {oo_gap}+ week gap between bursts, pattern repeats each year."
)

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

with tab_season:
    account_label = ", ".join(sel_accounts) if sel_accounts else f"All {len(all_accounts)} accounts"
    st.title("Seasonality Dashboard — Top 20 Accounts")
    st.caption(
        f"{account_label}  |  "
        f"SI > 1.0 = above baseline (peak season)  |  SI < 1.0 = below baseline (off season)"
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

    # ── KPI cards ─────────────────────────────────────────────────────────────
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

    # ── Chart 1: Actual vs Baseline vs Seasonal Baseline ─────────────────────
    st.subheader("Actual Demand vs Baseline vs Seasonal Baseline")
    _years_in_view = sorted(set(sel_years) if sel_years else set(df_all["YEAR"].unique()))
    _last_week = df_all["WEEK_LABEL"].max()
    _full_weeks = [f"{yr}-W{wk:02d}" for yr in _years_in_view for wk in range(1, 53)
                   if f"{yr}-W{wk:02d}" <= _last_week]
    scaffold = pd.DataFrame({"WEEK_LABEL": _full_weeks})
    actual = (df[df["ACTUAL_WEEKLY_DEMAND"] > 0]
              .groupby("WEEK_LABEL")["ACTUAL_WEEKLY_DEMAND"].sum().reset_index()
              .rename(columns={"ACTUAL_WEEKLY_DEMAND": "ACTUAL"}))
    if show_zeros:
        trend = scaffold.merge(actual, on="WEEK_LABEL", how="left")
        trend["ACTUAL"] = trend["ACTUAL"].fillna(0)
    else:
        trend = actual.copy()
    trend["WEEK_NUM_VAL"] = trend["WEEK_LABEL"].str.extract(r"W(\d+)")[0].astype(int)
    trend = trend.sort_values("WEEK_LABEL").reset_index(drop=True)
    _nonzero  = trend.loc[trend["ACTUAL"] > 0, "ACTUAL"]
    flat_base = float(_nonzero.mean()) if len(_nonzero) > 0 else 0.0
    fig_trend = go.Figure()
    fig_trend.add_bar(x=trend["WEEK_LABEL"], y=trend["ACTUAL"],
                      name="Actual Demand", marker_color="#4472C4", opacity=0.8)
    for i, pf in enumerate(active_prodfams):
        _pf_si = df_pf[df_pf["PRODFAM"] == pf].set_index("WEEK_NUM")["SEASONALITY_INDEX"]
        _color = FAMILY_COLORS.get(pf, _DEFAULT_COLORS[i % len(_DEFAULT_COLORS)])
        fig_trend.add_scatter(x=trend["WEEK_LABEL"],
                              y=trend["WEEK_NUM_VAL"].map(_pf_si).fillna(0) * flat_base,
                              mode="lines", name=f"Seasonal Baseline ({pf})",
                              line=dict(color=_color, width=2))
    for yr in _years_in_view:
        x_pos = f"{yr}-W01"
        fig_trend.add_vline(x=x_pos, line_dash="dash",
                            line_color="rgba(255,255,255,0.25)", line_width=1)
        fig_trend.add_annotation(x=x_pos, y=1, yref="paper", text=str(yr),
                                 showarrow=False, font=dict(color="rgba(255,255,255,0.55)", size=12),
                                 xanchor="left", yanchor="top", xshift=4)
    fig_trend.update_layout(height=400, margin=dict(l=0, r=0, t=30, b=0),
                            legend=dict(orientation="h", y=1.08))
    fig_trend.update_xaxes(tickangle=45, nticks=40)
    st.plotly_chart(fig_trend, use_container_width=True)

    # ── Seasonal Index Profile ────────────────────────────────────────────────
    if active_prodfams:
        title_fams = " vs ".join(active_prodfams)
        st.subheader(f"Seasonal Index Profile — {title_fams}")
        fig_si = go.Figure()
        for i, pf in enumerate(active_prodfams):
            pf_si  = df_pf[df_pf["PRODFAM"] == pf].sort_values("WEEK_NUM").reset_index(drop=True)
            _color = FAMILY_COLORS.get(pf, _DEFAULT_COLORS[i % len(_DEFAULT_COLORS)])
            fig_si.add_scatter(x=pf_si["WEEK_NUM"], y=pf_si["SEASONALITY_INDEX"],
                               mode="lines+markers", name=pf,
                               line=dict(color=_color, width=2.5), marker=dict(size=5))
        fig_si.add_hline(y=1.0, line_dash="dash", line_color="#888888",
                         annotation_text="Baseline (SI = 1.0)", annotation_position="bottom right")
        fig_si.update_layout(height=300, margin=dict(l=0, r=0, t=30, b=0),
                             yaxis_title="Seasonality Index", xaxis_title="Week of Year",
                             legend=dict(orientation="h", y=1.08))
        fig_si.update_xaxes(tickmode="linear", tick0=1, dtick=4,
                            ticktext=[f"W{w:02d}" for w in range(1, 53, 4)],
                            tickvals=list(range(1, 53, 4)))
        st.plotly_chart(fig_si, use_container_width=True)
    st.divider()

    # ── Most Seasonal & Most Stable ───────────────────────────────────────────
    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("Most Seasonal Items (Highest Peak SI)")
        peak_si = (df.groupby(["ITEM_ID", "ITEM_DESCR"])["SEASONALITY_INDEX"]
                   .max().reset_index().nlargest(15, "SEASONALITY_INDEX")
                   .sort_values("SEASONALITY_INDEX"))
        peak_si["LABEL"] = peak_si["ITEM_ID"].astype(str) + " " + peak_si["ITEM_DESCR"]
        fig_peak = px.bar(peak_si, x="SEASONALITY_INDEX", y="LABEL", orientation="h",
                          height=420, color="SEASONALITY_INDEX", color_continuous_scale="Blues")
        fig_peak.update_layout(margin=dict(l=0, r=0, t=20, b=0),
                               coloraxis_showscale=False, yaxis_title="")
        st.plotly_chart(fig_peak, use_container_width=True)
    with col_right:
        st.subheader("Most Stable Items (Lowest SI Variation)")
        stable = (df.groupby(["ITEM_ID", "ITEM_DESCR"])["SEASONALITY_INDEX"]
                  .std().reset_index().dropna()
                  .nsmallest(15, "SEASONALITY_INDEX")
                  .sort_values("SEASONALITY_INDEX", ascending=False))
        stable["LABEL"] = stable["ITEM_ID"].astype(str) + " " + stable["ITEM_DESCR"]
        fig_stable = px.bar(stable, x="SEASONALITY_INDEX", y="LABEL", orientation="h",
                            height=420, color="SEASONALITY_INDEX", color_continuous_scale="Greens_r")
        fig_stable.update_layout(margin=dict(l=0, r=0, t=20, b=0),
                                 coloraxis_showscale=False, yaxis_title="")
        st.plotly_chart(fig_stable, use_container_width=True)
    st.divider()

    # ── Data table ────────────────────────────────────────────────────────────
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

# ── Tab 2: In-and-Out Products ────────────────────────────────────────────────
with tab_onoff:
    st.title("In-and-Out Products — Market-Wide")
    st.caption(
        f"Products with demand clustered in the same weeks across years — "
        f"empty the rest of the year. Aggregated across all {df_all['ACCOUNT'].nunique()} accounts."
    )

    onoff = detect_on_off(df_all, window=oo_window, gap=oo_gap)

    if onoff.empty:
        st.warning("No in-and-out products found. Try increasing the peak window.")
        st.stop()

    # ── KPI row ───────────────────────────────────────────────────────────────
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("In-and-Out Items", len(onoff))
    k2.metric("Single-peak", len(onoff[onoff["PATTERN"].str.startswith("Single")]))
    k3.metric("Multi-peak",  len(onoff[~onoff["PATTERN"].str.startswith("Single")]))
    k4.metric("Avg Accounts / Item", f"{onoff['ACCOUNTS_SELLING'].mean():.1f}")
    st.divider()

    # ── Top items by seasonal sales ───────────────────────────────────────────
    st.subheader("Top Items by Seasonal Sales Volume")
    st.caption("Units sold during the active season window only — all accounts combined.")

    seasonal_rows = []
    for _, meta in onoff.iterrows():
        pw   = meta["PEAK_WEEK"]
        lo_w = max(1, pw - oo_window)
        hi_w = min(52, pw + oo_window)
        grp  = df_all[df_all["ITEM_ID"] == meta["ITEM_ID"]]
        seas = grp[(grp["WEEK_NUM"] >= lo_w) & (grp["WEEK_NUM"] <= hi_w)]["ACTUAL_WEEKLY_DEMAND"].sum()
        tot  = grp["ACTUAL_WEEKLY_DEMAND"].sum()
        seasonal_rows.append({**meta.to_dict(),
            "SEASONAL_UNITS": int(seas),
            "TOTAL_UNITS":    int(tot),
            "SEASONAL_PCT":   round(100 * seas / tot, 1) if tot > 0 else 0.0,
        })
    seasonal_df = pd.DataFrame(seasonal_rows).sort_values("SEASONAL_UNITS", ascending=False).reset_index(drop=True)

    top_n = st.slider("Show top N items", 5, 50, 20, 5, key="oo_topn")
    top   = seasonal_df.head(top_n).copy()
    top["LABEL"] = top["ITEM_DESCR"].str[:38] + "  (" + top["PATTERN"] + ")"

    fig_top = go.Figure()
    fig_top.add_bar(
        y=top["LABEL"], x=top["SEASONAL_UNITS"], orientation="h",
        marker=dict(color=top["SEASONAL_UNITS"], colorscale="Blues", showscale=False),
        text=top["SEASONAL_UNITS"].apply(lambda v: f"{v:,}"),
        textposition="outside",
    )
    fig_top.update_layout(
        height=max(350, 26 * top_n),
        yaxis=dict(autorange="reversed"),
        xaxis_title="Seasonal Units (all accounts)",
        margin=dict(l=0, r=60, t=20, b=0),
    )
    st.plotly_chart(fig_top, use_container_width=True)

    st.divider()

    # ── Product detail view ───────────────────────────────────────────────────
    st.subheader("Product Detail")
    item_options = {
        row["ITEM_ID"]: f"{row['ITEM_DESCR']}  ({row['PATTERN']}, {row['ACCOUNTS_SELLING']} accts)"
        for _, row in seasonal_df.iterrows()
    }
    sel_oo_item = st.selectbox(
        f"Select product  ({len(item_options)} in-and-out items)",
        options=list(item_options.keys()),
        format_func=lambda x: item_options[x],
    )

    meta      = seasonal_df[seasonal_df["ITEM_ID"] == sel_oo_item].iloc[0]
    item_dem  = df_all[df_all["ITEM_ID"] == sel_oo_item]
    item_agg  = item_dem.groupby(["YEAR","WEEK_NUM"])["ACTUAL_WEEKLY_DEMAND"].sum().reset_index()
    years_oo  = sorted(item_dem["YEAR"].unique())
    pw        = int(meta["PEAK_WEEK"])

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Pattern",           meta["PATTERN"])
    m2.metric("Peak Week",         f"W{pw} ({meta['PEAK_MONTH']})")
    m3.metric("Seasonal Units",    f"{meta['SEASONAL_UNITS']:,}")
    m4.metric("Seasonal %",        f"{meta['SEASONAL_PCT']}%")
    m5.metric("Accounts Selling",  meta["ACCOUNTS_SELLING"])
    st.markdown(
        f"**{meta['ITEM_DESCR']}** · Brand: {meta['BRAND']} · "
        f"Vendor: {meta['VENDOR']} · Family: {meta['PRODFAM']}"
    )
    st.markdown(f"*Sold in: {meta['ACCOUNTS_LIST']}*")
    st.divider()

    # Overlaid yearly chart
    col_l, col_r = st.columns([3, 2])
    with col_l:
        st.markdown("**Weekly Demand by Year (overlaid)**")
        colors = px.colors.qualitative.Set2
        fig_ov = go.Figure()
        for i, yr in enumerate(years_oo):
            yd = (item_agg[item_agg["YEAR"]==yr]
                  .set_index("WEEK_NUM")["ACTUAL_WEEKLY_DEMAND"]
                  .reindex(range(1,53), fill_value=0).reset_index())
            yd.columns = ["WK","DEM"]
            fig_ov.add_scatter(x=yd["WK"], y=yd["DEM"], mode="lines+markers",
                               name=str(yr), line=dict(color=colors[i%len(colors)], width=2),
                               marker=dict(size=4))
        fig_ov.add_vrect(x0=max(1,pw-oo_window), x1=min(52,pw+oo_window),
                         fillcolor="gold", opacity=0.15, layer="below", line_width=0,
                         annotation_text=f"Peak W{max(1,pw-oo_window)}–W{min(52,pw+oo_window)}",
                         annotation_position="top left", annotation_font_size=10)
        fig_ov.update_layout(height=320, margin=dict(l=0,r=0,t=30,b=0),
                             xaxis=dict(title="Week", tickmode="linear", dtick=4),
                             yaxis_title="Demand", legend=dict(orientation="h", y=1.1))
        st.plotly_chart(fig_ov, use_container_width=True)

    with col_r:
        st.markdown("**Demand Heatmap (Year × Week)**")
        pivot = (item_agg.pivot_table(index="YEAR", columns="WEEK_NUM",
                                      values="ACTUAL_WEEKLY_DEMAND", fill_value=0)
                 .reindex(columns=range(1,53), fill_value=0))
        fig_h = go.Figure(go.Heatmap(
            z=pivot.values, x=[f"W{w}" for w in pivot.columns],
            y=[str(y) for y in pivot.index], colorscale="Blues",
            hovertemplate="Year: %{y}<br>Week: %{x}<br>Demand: %{z}<extra></extra>",
        ))
        fig_h.update_layout(height=320, margin=dict(l=0,r=0,t=20,b=0),
                            xaxis=dict(tickmode="linear", dtick=4))
        st.plotly_chart(fig_h, use_container_width=True)

    st.divider()

    # ── Full table ────────────────────────────────────────────────────────────
    st.subheader(f"All In-and-Out Products  (window ±{oo_window}w, gap {oo_gap}w)")
    tbl = seasonal_df[[
        "ITEM_ID","ITEM_DESCR","BRAND","VENDOR","PRODFAM",
        "ACCOUNTS_SELLING","ACCOUNTS_LIST","PATTERN","PEAK_WEEK","PEAK_MONTH",
        "SEASONAL_UNITS","TOTAL_UNITS","SEASONAL_PCT","YEARS_CONFIRMED",
    ]].rename(columns={
        "ITEM_ID":"Item ID","ITEM_DESCR":"Description","BRAND":"Brand",
        "VENDOR":"Vendor","PRODFAM":"Family","ACCOUNTS_SELLING":"# Accounts",
        "ACCOUNTS_LIST":"Accounts","PATTERN":"Pattern","PEAK_WEEK":"Peak Wk",
        "PEAK_MONTH":"Month","SEASONAL_UNITS":"Seasonal Units",
        "TOTAL_UNITS":"Total Units","SEASONAL_PCT":"Seasonal %",
        "YEARS_CONFIRMED":"Years",
    })
    st.dataframe(tbl, use_container_width=True, height=450,
        column_config={
            "Seasonal %": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100),
        }
    )
