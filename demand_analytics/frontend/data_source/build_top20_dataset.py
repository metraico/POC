"""
build_top20_dataset.py
Reads top20_accounts_consolidated.xlsx (one sheet per account, wide format)
and converts it to a long-format parquet for the Streamlit dashboard.

Run from walmart_frontend/:
    python3 data_source/build_top20_dataset.py
"""

import os
import pandas as pd

BASE     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE   = os.path.join(BASE, "output", "top20_accounts_consolidated.xlsx")
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "top20_demand.parquet")

INFO_COLS = ["ITEM_ID", "ITEM_DESCR", "BRAND", "VENDOR", "PRODFAM"]

print(f"Loading: {SOURCE}")
xl = pd.ExcelFile(SOURCE)
print(f"Sheets found: {xl.sheet_names}\n")

all_long = []

for sheet in xl.sheet_names:
    print(f"  Processing sheet: '{sheet}' ...", end=" ", flush=True)
    df_wide = xl.parse(sheet)

    date_cols = [c for c in df_wide.columns if c not in INFO_COLS and c != "GRAND_TOTAL"]
    avail_info = [c for c in INFO_COLS if c in df_wide.columns]

    df_long = df_wide[avail_info + date_cols].melt(
        id_vars=avail_info,
        var_name="DATE",
        value_name="QUANTITY",
    )

    # Normalise string columns — mixed types across sheets cause parquet errors
    for col in avail_info:
        if col != "ITEM_ID":
            df_long[col] = df_long[col].astype(str).replace("nan", "")

    df_long["ACCOUNT"]    = sheet
    df_long["DATE"]       = pd.to_datetime(df_long["DATE"], errors="coerce")
    df_long["YEAR"]       = df_long["DATE"].dt.year
    df_long["MONTH"]      = df_long["DATE"].dt.month
    df_long["MONTH_NAME"] = df_long["DATE"].dt.strftime("%b")
    df_long["WEEK_NUM"]   = df_long["DATE"].dt.isocalendar().week.astype(int)
    df_long["WEEK_LABEL"] = df_long["DATE"].dt.strftime("%Y-W%W")
    df_long["ITEM_ID"]    = pd.to_numeric(df_long["ITEM_ID"], errors="coerce").fillna(0).astype(int)
    df_long["QUANTITY"]   = pd.to_numeric(df_long["QUANTITY"], errors="coerce").fillna(0).astype(int)

    non_zero = (df_long["QUANTITY"] > 0).sum()
    print(f"{len(df_wide)} items, {len(date_cols)} dates, {non_zero:,} non-zero rows")
    all_long.append(df_long)

print("\nCombining all accounts...")
combined = pd.concat(all_long, ignore_index=True)

print(f"  Total rows    : {len(combined):,}")
print(f"  Non-zero rows : {(combined['QUANTITY'] > 0).sum():,}")
print(f"  Accounts      : {combined['ACCOUNT'].nunique()}")
print(f"  Unique items  : {combined['ITEM_ID'].nunique():,}")
print(f"  Date range    : {combined['DATE'].min().date()} → {combined['DATE'].max().date()}")

combined.to_parquet(OUT_FILE, index=False)
print(f"\nDone. Written to:\n  {OUT_FILE}")
