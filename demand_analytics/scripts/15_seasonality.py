"""
15_seasonality.py

Input  : top20_accounts_weekly.xlsx  (items × week columns, one sheet per account)
Output : top20_accounts_seasonality.xlsx  +  seasonality_demand.parquet

For every item × account × week:

  BASELINE_DEMAND    = trimmed mean of selling weeks (Q10–Q90 cap)
  SEASONALITY_INDEX  = avg actual demand for that week number across all years
                       ÷ BASELINE_DEMAND
                       (>1 = above normal, <1 = below normal)
  SEASONAL_BASELINE  = BASELINE_DEMAND × SEASONALITY_INDEX
                       (week-by-week demand curve anchored to real baseline)

Excel output: same wide structure as source — one sheet per account
  ITEM_ID | ITEM_DESCR | BRAND | VENDOR | PRODFAM
  | ACTUAL 2023-W01 … 2026-W13
  | BASELINE_DEMAND
  | SI_W01 … SI_W52       (seasonality index per week number)
  | SB_2023-W01 … SB_2026-W13  (seasonal baseline per week)

Parquet output: long format for Streamlit

Run from Dataprocessing/:
    python3 15_seasonality.py
"""

import os
import re
import time
import datetime
import numpy as np
import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE    = os.path.join(BASE, "output", "top20_accounts_weekly.xlsx")
OUT_XLSX  = os.path.join(BASE, "output", "top20_accounts_seasonality.xlsx")
OUT_PARQ  = os.path.join(BASE, "output", "seasonality_demand.parquet")

INFO_COLS = ["ITEM_ID", "ITEM_DESCR", "BRAND", "VENDOR", "PRODFAM"]
SPIKE_PCT = 0.90
DIP_PCT   = 0.10

# ── Styling ───────────────────────────────────────────────────────────────────
HEADER_FILL   = PatternFill("solid", fgColor="1F4E79")
TOTAL_FILL    = PatternFill("solid", fgColor="BDD7EE")
BASELINE_FILL = PatternFill("solid", fgColor="E2EFDA")
SI_FILL       = PatternFill("solid", fgColor="FFF2CC")
SB_FILL       = PatternFill("solid", fgColor="FCE4D6")
ZERO_FILL     = PatternFill("solid", fgColor="F2F2F2")
HEADER_FONT   = Font(bold=True, color="FFFFFF", size=10)
BOLD_FONT     = Font(bold=True, size=10)
CENTER        = Alignment(horizontal="center")


def log(msg, level="INFO"):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


def section(title):
    log("─" * 60)
    log(f"  STEP: {title}")
    log("─" * 60)


def compute_baseline(values: np.ndarray) -> float:
    """Trimmed mean: Q10–Q90 cap on non-zero selling weeks."""
    selling = values[values > 0]
    if len(selling) == 0:
        return 0.0
    cap   = np.quantile(selling, SPIKE_PCT)
    floor = np.quantile(selling, DIP_PCT)
    return round(float(np.clip(selling, floor, cap).mean()), 2)


def week_num_from_label(label: str) -> int:
    """Extract integer week number from '2023-W04' → 4."""
    m = re.search(r"W(\d+)", str(label))
    return int(m.group(1)) if m else 0


# ── Load ──────────────────────────────────────────────────────────────────────
section("Load Source File")
xl = pd.ExcelFile(SOURCE)
log(f"Source : {SOURCE}")
log(f"Sheets : {xl.sheet_names}")

section("Process Each Sheet")
t0_total   = time.time()
all_frames = []          # long-format rows for parquet
wb         = openpyxl.Workbook()
wb.remove(wb.active)

for sheet in xl.sheet_names:
    t0     = time.time()
    df     = xl.parse(sheet)

    avail_info = [c for c in INFO_COLS if c in df.columns]
    week_cols  = sorted([c for c in df.columns
                         if c not in avail_info and c != "GRAND_TOTAL"])

    # Map week label → week number (1–52)
    wnum = {w: week_num_from_label(w) for w in week_cols}   # e.g. "2023-W04" → 4
    week_nums_arr = np.array([wnum[w] for w in week_cols])  # shape (n_weeks,)

    qty = df[week_cols].to_numpy(dtype=float)                # shape (n_items, n_weeks)
    n_items, n_weeks = qty.shape

    # ── Per-item baseline ─────────────────────────────────────────────────────
    baselines = np.array([compute_baseline(qty[i]) for i in range(n_items)])

    # ── Per-item seasonality index for each week number 1–52 ─────────────────
    #
    # For week number W:
    #   avg_actual_W = mean of all actual values where week_number == W
    #                  (across all years, non-zero and zero alike)
    #   SI_W = avg_actual_W / baseline   (0 if baseline == 0)
    #
    # Shape: (n_items, 52)  — index 0 = week 1
    si_matrix = np.zeros((n_items, 52), dtype=float)

    for w in range(1, 53):
        mask = (week_nums_arr == w)
        if not mask.any():
            continue
        avg_w = qty[:, mask].mean(axis=1)          # shape (n_items,)
        safe_base = np.where(baselines > 0, baselines, np.nan)
        si_matrix[:, w - 1] = np.where(
            baselines > 0, np.round(avg_w / safe_base, 4), 0.0
        )

    # ── Seasonal baseline per week ─────────────────────────────────────────────
    # sb[i, j] = baselines[i] × si_matrix[i, wnum[week_cols[j]] - 1]
    sb_matrix = np.zeros((n_items, n_weeks), dtype=float)
    for j, w_label in enumerate(week_cols):
        w = wnum[w_label]
        idx = min(w - 1, 51)   # cap at 51 (week 52 = index 51)
        sb_matrix[:, j] = np.round(baselines * si_matrix[:, idx], 2)

    # ── Build long-format rows for parquet ────────────────────────────────────
    for i in range(n_items):
        item_meta = {c: df[c].iloc[i] for c in avail_info}
        for j, w_label in enumerate(week_cols):
            w = wnum[w_label]
            year = int(w_label[:4])
            all_frames.append({
                "ACCOUNT":            sheet.title(),
                **item_meta,
                "YEAR":               year,
                "WEEK_NUM":           w,
                "WEEK_LABEL":         w_label,
                "ACTUAL_WEEKLY_DEMAND": int(qty[i, j]),
                "BASELINE_DEMAND":    baselines[i],
                "SEASONALITY_INDEX":  si_matrix[i, min(w - 1, 51)],
                "SEASONAL_BASELINE":  sb_matrix[i, j],
            })

    # ── Build Excel sheet ─────────────────────────────────────────────────────
    # Column layout:
    #   INFO_COLS | actual week cols | GRAND_TOTAL | BASELINE_DEMAND
    #   | SI_W01…SI_W52 | SB_week_cols
    si_headers = [f"SI_W{str(w).zfill(2)}" for w in range(1, 53)]
    sb_headers = [f"SB_{w}" for w in week_cols]

    out = df[avail_info + week_cols].copy()
    out["GRAND_TOTAL"]     = out[week_cols].sum(axis=1)
    out["BASELINE_DEMAND"] = baselines
    for w in range(1, 53):
        out[f"SI_W{str(w).zfill(2)}"] = si_matrix[:, w - 1]
    for j, w_label in enumerate(week_cols):
        out[f"SB_{w_label}"] = sb_matrix[:, j]

    out = out.sort_values("GRAND_TOTAL", ascending=False).reset_index(drop=True)

    all_excel_cols = (avail_info + week_cols +
                      ["GRAND_TOTAL", "BASELINE_DEMAND"] +
                      si_headers + sb_headers)

    grand_col_idx    = len(avail_info) + len(week_cols) + 1
    baseline_col_idx = grand_col_idx + 1
    si_start_idx     = baseline_col_idx + 1
    si_end_idx       = si_start_idx + 51
    sb_start_idx     = si_end_idx + 1

    ws = wb.create_sheet(title=sheet[:31])

    # Header row
    for ci, h in enumerate(all_excel_cols, 1):
        cell           = ws.cell(row=1, column=ci, value=str(h))
        cell.font      = HEADER_FONT
        cell.fill      = HEADER_FILL
        cell.alignment = CENTER

    # Data rows
    for ri, row in enumerate(out[all_excel_cols].itertuples(index=False), 2):
        for ci, val in enumerate(row, 1):
            cell = ws.cell(row=ri, column=ci, value=val)
            if ci == grand_col_idx:
                cell.fill = TOTAL_FILL
                cell.font = BOLD_FONT
            elif ci == baseline_col_idx:
                cell.fill = BASELINE_FILL
                cell.font = BOLD_FONT
            elif si_start_idx <= ci <= si_end_idx:
                cell.fill = SI_FILL
            elif ci >= sb_start_idx:
                cell.fill = SB_FILL
            elif ci > len(avail_info) and (val == 0 or val == "0"):
                cell.fill = ZERO_FILL

    ws.freeze_panes = ws.cell(row=2, column=len(avail_info) + 1)
    for ci, h in enumerate(all_excel_cols, 1):
        ws.column_dimensions[get_column_letter(ci)].width = min(len(str(h)) + 2, 20)

    log(f"  {sheet:<30} {n_items:>5} items | "
        f"baseline {baselines[baselines>0].min() if (baselines>0).any() else 0:.1f}"
        f"–{baselines.max():.1f} | "
        f"SI range {si_matrix.min():.2f}–{si_matrix.max():.2f}  "
        f"({time.time()-t0:.1f}s)")

# ── Save Excel ────────────────────────────────────────────────────────────────
section("Save Excel")
wb.save(OUT_XLSX)
log(f"Saved : {OUT_XLSX}  ({os.path.getsize(OUT_XLSX)/1024:,.1f} KB)")

# ── Build & Save Parquet ──────────────────────────────────────────────────────
section("Build & Save Parquet")
t0 = time.time()
long_df = pd.DataFrame(all_frames)

# Type cleanup
long_df["ITEM_ID"]               = pd.to_numeric(long_df["ITEM_ID"], errors="coerce").fillna(0).astype(int)
long_df["ACTUAL_WEEKLY_DEMAND"]  = long_df["ACTUAL_WEEKLY_DEMAND"].astype(int)
long_df["BASELINE_DEMAND"]       = long_df["BASELINE_DEMAND"].astype(float)
long_df["SEASONALITY_INDEX"]     = long_df["SEASONALITY_INDEX"].astype(float).round(4)
long_df["SEASONAL_BASELINE"]     = long_df["SEASONAL_BASELINE"].astype(float).round(2)
for col in ["ITEM_DESCR", "BRAND", "VENDOR", "PRODFAM"]:
    long_df[col] = long_df[col].astype(str).str.strip().replace("nan", "")

col_order = [
    "ACCOUNT", "ITEM_ID", "ITEM_DESCR", "BRAND", "VENDOR", "PRODFAM",
    "YEAR", "WEEK_NUM", "WEEK_LABEL",
    "ACTUAL_WEEKLY_DEMAND", "BASELINE_DEMAND",
    "SEASONALITY_INDEX", "SEASONAL_BASELINE",
]
long_df = long_df[col_order].sort_values(
    ["ACCOUNT", "ITEM_ID", "YEAR", "WEEK_NUM"]
).reset_index(drop=True)

long_df.to_parquet(OUT_PARQ, index=False, engine="pyarrow")

log(f"Rows              : {len(long_df):,}")
log(f"Non-zero actual   : {(long_df['ACTUAL_WEEKLY_DEMAND'] > 0).sum():,}")
log(f"Accounts          : {long_df['ACCOUNT'].nunique()}")
log(f"Unique items      : {long_df['ITEM_ID'].nunique():,}")
log(f"Week range        : {long_df['WEEK_LABEL'].min()} → {long_df['WEEK_LABEL'].max()}")
log(f"Saved : {OUT_PARQ}  ({os.path.getsize(OUT_PARQ)/1024:,.1f} KB)  ({time.time()-t0:.1f}s)")

log("")
log("─" * 60)
log("  DONE")
log(f"  Excel  : {OUT_XLSX}")
log(f"  Parquet: {OUT_PARQ}")
log(f"  Total runtime: {time.time()-t0_total:.1f}s")
log("─" * 60)
