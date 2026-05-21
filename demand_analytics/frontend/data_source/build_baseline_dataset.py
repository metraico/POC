"""
build_baseline_dataset.py
Converts top20_accounts_baseline.xlsx (wide: items x weeks, one sheet per account)
into a long-format parquet for the Streamlit baseline demand page.

Run from walmart_frontend/:
    python3 data_source/build_baseline_dataset.py
"""

import os
import pandas as pd

SOURCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "output", "top20_accounts_baseline.xlsx",
)
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline_demand.parquet")

INFO_COLS = ["ITEM_ID", "ITEM_DESCR", "BRAND", "VENDOR", "PRODFAM"]

print(f"Loading {SOURCE} ...")
xl = pd.ExcelFile(SOURCE)
print(f"  Sheets: {xl.sheet_names}")

frames = []
for sheet in xl.sheet_names:
    df_wide    = xl.parse(sheet)
    week_cols  = [c for c in df_wide.columns if c not in INFO_COLS and c not in ("GRAND_TOTAL", "BASELINE_DEMAND")]
    avail_info = [c for c in INFO_COLS if c in df_wide.columns]

    # Keep BASELINE_DEMAND as a per-item value before melting
    baseline_col = df_wide[avail_info + ["BASELINE_DEMAND"]].copy()

    # Melt week columns → long
    df_long = df_wide[avail_info + week_cols].melt(
        id_vars=avail_info,
        var_name="WEEK_LABEL",
        value_name="ACTUAL_WEEKLY_DEMAND",
    )

    # Re-attach BASELINE_DEMAND (same value for all weeks of that item)
    df_long = df_long.merge(baseline_col, on=avail_info, how="left")

    df_long["ACCOUNT"] = sheet.title()

    # Parse YEAR and WEEK_NUM from WEEK_LABEL e.g. "2023-W01"
    df_long["YEAR"]     = df_long["WEEK_LABEL"].str[:4].astype(int)
    df_long["WEEK_NUM"] = df_long["WEEK_LABEL"].str.extract(r"W(\d+)")[0].astype(int)

    # Normalize types
    df_long["ITEM_ID"]               = pd.to_numeric(df_long["ITEM_ID"], errors="coerce").fillna(0).astype(int)
    df_long["ACTUAL_WEEKLY_DEMAND"]  = pd.to_numeric(df_long["ACTUAL_WEEKLY_DEMAND"], errors="coerce").fillna(0).astype(int)
    df_long["BASELINE_DEMAND"]       = pd.to_numeric(df_long["BASELINE_DEMAND"], errors="coerce").fillna(0).round(2)
    for col in avail_info:
        if col != "ITEM_ID":
            df_long[col] = df_long[col].astype(str).str.strip().replace("nan", "")

    non_zero = (df_long["ACTUAL_WEEKLY_DEMAND"] > 0).sum()
    print(f"  {sheet:<30} {len(df_wide):>5} items × {len(week_cols):>3} weeks  |  {non_zero:,} non-zero rows")
    frames.append(df_long)

df = pd.concat(frames, ignore_index=True)

col_order = [
    "ACCOUNT", "ITEM_ID", "ITEM_DESCR", "BRAND", "VENDOR", "PRODFAM",
    "YEAR", "WEEK_NUM", "WEEK_LABEL", "ACTUAL_WEEKLY_DEMAND", "BASELINE_DEMAND",
]
df = df[col_order]

print(f"\nCombined rows     : {len(df):,}")
print(f"Non-zero rows     : {(df['ACTUAL_WEEKLY_DEMAND'] > 0).sum():,}")
print(f"Accounts          : {df['ACCOUNT'].nunique()}")
print(f"Unique items      : {df['ITEM_ID'].nunique():,}")
print(f"Week range        : {df['WEEK_LABEL'].min()} → {df['WEEK_LABEL'].max()}")

df.to_parquet(OUT_FILE, index=False)
print(f"\nDone. Written to:\n  {OUT_FILE}")
