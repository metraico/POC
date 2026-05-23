"""
Merges VELOCITY_CLASS, LIFECYCLE_STATUS (year-specific), and LIFECYCLE_OVERALL into:
  - top20_demand.parquet
  - seasonality_demand.parquet

Run this whenever 16_velocity_classification.py or 17_lifecycle_classification.py is re-run.
"""
import pandas as pd
import os

BASE         = os.path.dirname(__file__)
VELOCITY_XL  = os.path.join(BASE, "../../output/top20_accounts_velocity.xlsx")
LIFECYCLE_XL = os.path.join(BASE, "../../output/top20_accounts_lifecycle.xlsx")

# ── Load velocity (ITEM_ID, per-year VC_ cols, VELOCITY_OVERALL) ──────────────
print("Loading velocity classifications...")
vel_all = pd.read_excel(VELOCITY_XL, sheet_name="VELOCITY")
vel_all["ITEM_ID"] = vel_all["ITEM_ID"].astype(int)
vc_year_cols = [c for c in vel_all.columns if c.startswith("VC_")]
velocity = vel_all[["ITEM_ID"] + vc_year_cols + ["VELOCITY_OVERALL"]].drop_duplicates(subset=["ITEM_ID"])
print(f"  {len(velocity)} unique items, years: {[c.replace('VC_','') for c in vc_year_cols]}")
print("  Overall: " + velocity["VELOCITY_OVERALL"].value_counts().to_string().replace("\n", "\n           "))

# ── Load lifecycle wide format (ITEM_ID + LC_YYYY cols + LIFECYCLE_OVERALL) ────
print("\nLoading lifecycle classifications...")
lc_all = pd.read_excel(LIFECYCLE_XL, sheet_name="LIFECYCLE")
lc_all["ITEM_ID"] = lc_all["ITEM_ID"].astype(int)

# Item-level dedup — drop ACCOUNT, keep one row per ITEM_ID
lc_year_cols = [c for c in lc_all.columns if c.startswith("LC_")]
lc_item = lc_all[["ITEM_ID"] + lc_year_cols + ["LIFECYCLE_OVERALL"]].drop_duplicates(subset=["ITEM_ID"])

years_available = [int(c.replace("LC_", "")) for c in lc_year_cols]
print(f"  {len(lc_item)} unique items, years: {years_available}")
print("  Overall: " + lc_item["LIFECYCLE_OVERALL"].value_counts().to_string().replace("\n", "\n           "))


def patch_parquet(parquet_path):
    print(f"\nPatching: {parquet_path}")
    df = pd.read_parquet(parquet_path)
    df["ITEM_ID"] = df["ITEM_ID"].astype(int)
    df["YEAR"]    = df["YEAR"].astype(int)

    # Drop old columns
    for col in ("VELOCITY_CLASS", "VELOCITY_OVERALL", "LIFECYCLE_STATUS", "LIFECYCLE_OVERALL",
                "DEMAND_PATTERN", "ADI", "CV2"):
        if col in df.columns:
            df = df.drop(columns=[col])

    # Merge velocity wide columns
    df = df.merge(velocity, on="ITEM_ID", how="left")
    df["VELOCITY_OVERALL"] = df["VELOCITY_OVERALL"].fillna("Unclassified")

    # Derive VELOCITY_CLASS from the correct VC_{YEAR} column per row
    def get_vel_year(row):
        col = f"VC_{int(row['YEAR'])}"
        val = row.get(col, None)
        if pd.isna(val):
            return "No Data"
        return val

    df["VELOCITY_CLASS"] = df.apply(get_vel_year, axis=1)
    df = df.drop(columns=[c for c in vc_year_cols if c in df.columns])

    # Merge lifecycle wide columns
    df = df.merge(lc_item, on="ITEM_ID", how="left")
    df["LIFECYCLE_OVERALL"] = df["LIFECYCLE_OVERALL"].fillna("Unclassified")

    # Derive LIFECYCLE_STATUS from the correct LC_{YEAR} column per row
    def get_lc_year(row):
        col = f"LC_{int(row['YEAR'])}"
        val = row.get(col, None)
        if pd.isna(val):
            return "No Data"
        return val

    df["LIFECYCLE_STATUS"] = df.apply(get_lc_year, axis=1)
    df = df.drop(columns=[c for c in lc_year_cols if c in df.columns])

    print(f"  Rows: {len(df)}")
    print("  VELOCITY_CLASS   — " + df["VELOCITY_CLASS"].value_counts().to_string().replace("\n", "\n                     "))
    print("  VELOCITY_OVERALL — " + df["VELOCITY_OVERALL"].value_counts().to_string().replace("\n", "\n                     "))
    print("  LIFECYCLE_STATUS — " + df["LIFECYCLE_STATUS"].value_counts().to_string().replace("\n", "\n                     "))
    print("  LIFECYCLE_OVERALL— " + df["LIFECYCLE_OVERALL"].value_counts().to_string().replace("\n", "\n                     "))

    df.to_parquet(parquet_path, index=False)
    print("  Saved.")


patch_parquet(os.path.join(BASE, "top20_demand.parquet"))
patch_parquet(os.path.join(BASE, "seasonality_demand.parquet"))

print("\nDone.")
