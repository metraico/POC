# Demand Analytics — Top 20 Accounts

End-to-end pipeline for cleaning, classifying, and visualising weekly retail demand
across 20 key accounts. Source data flows through a numbered script pipeline and
lands in a Streamlit dashboard.

---

## Folder Structure

```
demand_analytics/
├── scripts/                        # Numbered pipeline scripts (run in order)
│   ├── consolidate_by_account.py   # Step 0 — raw data → consolidated Excel
│   ├── 13_weekly_consolidated.py   # Step 1 — daily → weekly aggregation
│   ├── 18_data_quality.py          # Step 2 — remove low-signal items
│   ├── 15_seasonality.py           # Step 3 — compute seasonality indices
│   ├── 16_velocity_classification.py  # Step 4 — velocity labels
│   ├── 17_lifecycle_classification.py # Step 5 — lifecycle labels
│   └── 19_demand_pattern.py        # Optional audit — ADI × CV² pattern classifier
│
├── output/                         # All generated Excel / Parquet files
│
├── frontend/
│   ├── app.py                      # Streamlit dashboard (main entry point)
│   ├── requirements.txt
│   ├── README.md                   # Frontend setup guide
│   └── data_source/
│       ├── build_velocity_parquet.py   # Step 6 — patch parquets with classifications
│       ├── seasonality_demand.parquet  # Main dashboard data source
│       └── prodfam_seasonality.parquet # Product-family SI profiles
│
└── README.md                       # This file
```

---

## Pipeline — Run Order

Run all scripts from the `scripts/` directory (`cd scripts/`):

```bash
# Step 0 — only needed when raw source data changes
python3 consolidate_by_account.py --top20

# Step 1 — convert daily to weekly
python3 13_weekly_consolidated.py

# Step 2 — remove low-signal items (produces weekly_clean.xlsx)
python3 18_data_quality.py

# Step 3 — seasonality indices
python3 15_seasonality.py

# Step 4 — velocity classification
python3 16_velocity_classification.py

# Step 5 — lifecycle classification
python3 17_lifecycle_classification.py

# Step 6 — patch parquets with all classifications
cd ..
python3 frontend/data_source/build_velocity_parquet.py

# Then restart Streamlit
```

---

## Script Descriptions

### `consolidate_by_account.py`
Reads raw transaction data and consolidates it into one Excel workbook with one
sheet per account. This is the source-of-truth input for all downstream scripts.
Output: `output/top20_accounts_consolidated.xlsx`

### `13_weekly_consolidated.py`
Converts the daily consolidated Excel (date columns) into ISO-week columns by
summing all daily quantities within each calendar week.
Output: `output/top20_accounts_weekly.xlsx`

### `18_data_quality.py`
Removes items that do not meet the minimum signal thresholds. Only items with
**total sales ≥ 500 units AND at least 12 selling weeks** (aggregated across all
accounts and all years) are kept.

- Items failing either condition are labelled **Insufficient Data** and excluded
  from all downstream analysis.
- Produces a 3-sheet audit Excel (`top20_data_quality.xlsx`) showing ALL_ITEMS,
  excluded items (GHOST_SKUS), and the retained set (CLEAN_ITEMS).

Output: `output/top20_accounts_weekly_clean.xlsx` ← used by all scripts below

### `15_seasonality.py`
Computes **Seasonal Index (SI)** profiles at the **Product Family level** (BEER,
WINE, SPIRITS, NON-ALCOHOL, CBD) and attaches them to every item × account × week
row.

How it works:
1. Aggregate all weekly sales within each Product Family across all accounts
2. Compute a trimmed mean baseline (Q10–Q90 clipping of non-zero weeks)
3. SI for week W = average sales in week W / trimmed mean baseline
4. SI > 1.0 → above-average demand (peak season)
   SI < 1.0 → below-average demand (off season)

Outputs:
- `output/top20_accounts_seasonality.xlsx` — compatibility file for scripts 16/17
- `output/top20_prodfam_seasonality.xlsx` — per-family SI profile summary
- `output/seasonality_demand.parquet` + copy in `frontend/data_source/`
- `output/prodfam_seasonality.parquet` + copy in `frontend/data_source/`

### `16_velocity_classification.py`
Classifies each item's **sales velocity** for each year and overall, based on
average weekly baseline demand and selling frequency.

| Class | Criteria |
|---|---|
| High Velocity | Baseline ≥ 50 units/week AND sells in ≥ 70% of weeks |
| Moderate Velocity | Baseline ≥ 15 units/week AND sells in ≥ 50% of weeks |
| Low Velocity | Sells regularly but below moderate thresholds |
| Dormant | Sells in fewer than 10% of weeks in that year |

**Year-specific** labels (VC_2023, VC_2024 …) capture how an item's activity
changed over time. **VELOCITY_OVERALL** is assigned by the most common label
across all years.

Output: `output/top20_accounts_velocity.xlsx`

### `17_lifecycle_classification.py`
Classifies each item's **lifecycle stage** for each year, tracking its trajectory
from launch through maturity to decline.

| Stage | Criteria |
|---|---|
| New | First year the item appears with meaningful sales |
| Evergreen | Consistent sales across multiple years — stable performer |
| Declining | Sales trending downward vs prior year |
| Discontinued | Was selling, now has zero or near-zero activity |

**Year-specific** labels (LC_2023, LC_2024 …) and **LIFECYCLE_OVERALL** (dominant
pattern across all years) are both stored.

Output: `output/top20_accounts_lifecycle.xlsx`

### `build_velocity_parquet.py`
Patches the Parquet files used by the dashboard with the velocity and lifecycle
classifications produced by scripts 16 and 17. Run this after either
classification script is updated.

Re-run whenever scripts 16 or 17 are re-run.

### `19_demand_pattern.py` *(optional audit tool)*
Classifies items by demand pattern using the **ADI × CV² (Syntetos-Boylan)** matrix:

|  | CV² ≤ 0.49 (consistent qty) | CV² > 0.49 (variable qty) |
|---|---|---|
| **ADI ≤ 1.32** (sells often) | Smooth | Erratic |
| **ADI > 1.32** (sells rarely) | Intermittent | Lumpy |

This script is **not** part of the active pipeline — the noise filtering is handled
by script 18's sales + weeks thresholds. Run it for auditing purposes only.
Output: `output/top20_demand_pattern.xlsx`

---

## Output Files Reference

| File | Produced by | Used by |
|---|---|---|
| `top20_accounts_consolidated.xlsx` | consolidate_by_account.py | script 13 |
| `top20_accounts_weekly.xlsx` | script 13 | script 18 |
| `top20_accounts_weekly_clean.xlsx` | script 18 | scripts 15, 16, 17 |
| `top20_data_quality.xlsx` | script 18 | audit only |
| `top20_accounts_seasonality.xlsx` | script 15 | scripts 16, 17 |
| `top20_prodfam_seasonality.xlsx` | script 15 | audit only |
| `top20_accounts_velocity.xlsx` | script 16 | build_velocity_parquet.py |
| `top20_accounts_lifecycle.xlsx` | script 17 | build_velocity_parquet.py |
| `seasonality_demand.parquet` | scripts 15 → build_velocity_parquet.py | app.py |
| `prodfam_seasonality.parquet` | script 15 | app.py |

---

## Data Filtering Criteria

Items are excluded from all analysis if they do not pass **both** of these thresholds
(aggregated across all 20 accounts and all years of data):

| Threshold | Value | Meaning |
|---|---|---|
| Minimum total sales | ≥ 500 units | Enough volume to be analytically meaningful |
| Minimum selling weeks | ≥ 12 weeks | Sold across at least 12 distinct weeks (not a one-off) |

Of the original **3,052 items**, **634** pass this filter and enter the analysis.

---

## Dashboard (app.py)

Streamlit app at `frontend/app.py`. Run with:

```bash
cd frontend
streamlit run app.py
```

### Sidebar Filters

| Filter | Description |
|---|---|
| Account | Filter to one or more of the 20 accounts |
| Year | Filter to a specific year |
| Velocity (year) | Filter by velocity class in the selected year |
| Lifecycle (year) | Filter by lifecycle stage in the selected year |
| Overall Velocity | Filter by overall velocity across all years |
| Overall Lifecycle | Filter by overall lifecycle stage |
| Product Family | Filter to BEER / WINE / SPIRITS / NON-ALCOHOL / CBD |
| Brand / Supplier | Drill into a brand or vendor |
| Item | Select one or more specific items |
| Show zero-demand weeks | Toggle to include/exclude zero-demand weeks in the bar chart |

### Charts

1. **Actual Demand vs Seasonal Baseline** — weekly bar chart with an overlaid
   seasonal baseline curve per product family. Multiple families shown in distinct
   colours when more than one is in context.
2. **Seasonal Index Profile** — line chart of the SI curve (week 1–52) for each
   active product family. SI = 1.0 is the neutral baseline.
3. **Most Seasonal Items** — top 15 items by peak SI value.
4. **Most Stable Items** — top 15 items by lowest SI standard deviation.

### Item Detail Cards

Selecting one or more items shows a detail card per item with velocity badge,
lifecycle stage, total sales, baseline demand, and per-year lifecycle badges.
Multiple items are displayed two per row.
