# Demand Analytics Frontend

Streamlit dashboard for visualising demand data across top 20 accounts — includes Seasonality, Baseline Demand, and Velocity/Lifecycle views.

---

## Requirements

- Python 3.9+
- Dependencies listed in `requirements.txt`

---

## Setup

### 1. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

---

## Prepare the data

The app reads from `.parquet` files in `data_source/`. These must be built before running the app.

### Step 1 — Build baseline and top-20 parquets

Run from the `frontend/` directory:

```bash
python3 data_source/build_baseline_dataset.py
python3 data_source/build_top20_dataset.py
```

These scripts expect an Excel source file at:
```
demand_analytics/output/top20_accounts_baseline.xlsx
```

### Step 2 — Merge velocity and lifecycle classifications

After running the upstream classification scripts (`16_velocity_classification.py` and `17_lifecycle_classification.py`), merge their outputs into the parquets:

```bash
python3 data_source/build_velocity_parquet.py
```

This updates both `top20_demand.parquet` and `seasonality_demand.parquet` with `VELOCITY_CLASS` and `LIFECYCLE_STATUS` columns.

---

## Run the app

From the `frontend/` directory:

```bash
streamlit run app.py
```

The app opens at `http://localhost:8501` by default.

---

## Directory structure

```
frontend/
├── app.py                        # Main Streamlit app (Seasonality Dashboard)
├── requirements.txt
├── data_source/
│   ├── build_baseline_dataset.py # Builds baseline_demand.parquet from Excel
│   ├── build_top20_dataset.py    # Builds top20_demand.parquet from Excel
│   ├── build_velocity_parquet.py # Merges velocity/lifecycle into parquets
│   ├── baseline_demand.parquet
│   ├── top20_demand.parquet
│   └── seasonality_demand.parquet
```

---

## Rebuilding parquets

Whenever the upstream Excel source or classification scripts change, re-run the build scripts in order:

```bash
python3 data_source/build_baseline_dataset.py
python3 data_source/build_top20_dataset.py
python3 data_source/build_velocity_parquet.py
```

Then restart the Streamlit app to pick up the new data.
