# POC — Retail Supply Chain Simulation: Workflow Guide
**Branch: `nir/update`**

---

## Overview

This project simulates a two-tier retail supply chain (Suppliers → Distribution Centres → Stores) running a full calendar year day-by-day. It has two operating modes:

| Mode | When to use | DB required |
|---|---|---|
| **Debug** (`debug_app.py`) | Quick parameter exploration, no setup needed | No |
| **Production** (`simulation.py` + `app.py`) | Full run with real data, stored in cloud DBs | Yes (PostgreSQL + ClickHouse) |

---

## Architecture

```
PostgreSQL (local)          ClickHouse Cloud
─────────────────           ─────────────────
Master / config data        Analytics output
  items                       demand
  stores                      sales_history
  dcs                         sales_daily
  suppliers                   store_inventory
  promos                      store_inventory_daily
  simulation_config           store_orders / _details
  promo_groups                store_receipts
  promo_group_items           supplier_orders / _details
  promo_stores                supplier_receipts
                              dc_inventory
```

---

## Mode 1 — Debug Dashboard (no database)

### What it does
Runs the entire simulation inside the browser using CSV files from `sample_data/`. Nothing is written to any database. Results are saved as CSV files under `output/`.

### How to run
```bash
.venv/Scripts/python.exe -m streamlit run dashboard/debug_app.py
```

### Sidebar controls
| Control | Effect |
|---|---|
| Dataset | `saltysnack_beverages_small` or full dataset |
| Start / End Date | Simulation date range |
| Replenishment Policy | `trailing_avg_28d`, `promo_aware_7d`, or `baseline_only` |
| Demand Smoothing Window | Rolling window (days) for avg daily demand |
| Store: Min Inventory Trigger | Weeks-of-cover threshold to fire a reorder |
| Store: Target Stock | Weeks-of-cover to replenish up to |
| Store: Starting Stock (days) | Initial on-hand at sim start |
| Store Order Day | Day of week stores place orders |
| DC: Reorder Point / Target / Start Days | Same parameters for DCs |
| DC Review Day | Day of week DCs raise supplier POs |
| Supplier Lead Time Min/Max | Days from PO to DC receipt |
| Supplier On-Time Rate | Probability of on-time delivery |
| Supplier Partial Delivery Rate | Probability of partial shipment |
| DC On-Time / Partial rates | Same for DC → Store leg |
| Random Seed | Reproducibility seed |

### Output charts
1. **Daily**: Demand bars + Sales bars + Lost Sales bars + On-Hand inventory line + On-Order line (dual y-axis). Promo periods shaded in amber.
2. **Weekly**: Same aggregated by ISO week + avg on-hand inventory line.
3. **Inventory Status Heatmap**: All stores × every day for the selected item (green = available, orange = low, red = zero).

### Output files (`output/debug_YYYYMMDD_YYYYMMDD/`)
- `demand_matrix.csv` — daily demand per store/item
- `store_sales_daily.csv` — daily sales, demand, lost sales per store/item
- `store_inventory_daily.csv` — daily on-hand, on-order, WoC status
- `store_receipts.csv` — DC → Store deliveries
- `store_orders.csv` / `store_order_details.csv`
- `supplier_receipts.csv` / `supplier_orders.csv`

---

## Mode 2 — Production Pipeline (with databases)

### Prerequisites
1. **PostgreSQL** (local) — master data loaded by `setup_postgres.py`
2. **ClickHouse Cloud** — output tables created by `setup_clickhouse.py`
3. **`.env` file** in project root with connection credentials

### One-time setup
```bash
# 1. Create tables in PostgreSQL
.venv/Scripts/python.exe setup_postgres.py

# 2. Create tables in ClickHouse
.venv/Scripts/python.exe setup_clickhouse.py
```

### Creating a simulation account
Use the Streamlit dashboard wizard:
```bash
.venv/Scripts/python.exe -m streamlit run dashboard/app.py
```
Go to **Setup Wizard** → fill in stores, DCs, suppliers, items, promos, and config parameters → Save. This writes all master data to PostgreSQL and generates a `config_<account_id>.yaml` file.

### Running a simulation

**Step 1 — Generate demand matrix**
```bash
.venv/Scripts/python.exe demand_gen.py \
  --config config_<account_id>.yaml \
  --sim_id <simulation_id> \
  --account_id <account_id>
```
Outputs `demand_matrix.parquet` (local) and populates the `demand` table in ClickHouse.

**Step 2 — Run simulation**
```bash
.venv/Scripts/python.exe simulation.py \
  --config config_<account_id>.yaml \
  --sim_id <simulation_id> \
  --account_id <account_id>
```
Writes all output tables to ClickHouse.

**For "Test 123" specifically:**
```bash
.venv/Scripts/python.exe demand_gen.py \
  --config config_354aaab1-2479-4c90-aa98-53e707f08f40.yaml \
  --sim_id 614d7903-7269-4d94-8e21-db839308fbd2 \
  --account_id 354aaab1-2479-4c90-aa98-53e707f08f40

.venv/Scripts/python.exe simulation.py \
  --config config_354aaab1-2479-4c90-aa98-53e707f08f40.yaml \
  --sim_id 614d7903-7269-4d94-8e21-db839308fbd2 \
  --account_id 354aaab1-2479-4c90-aa98-53e707f08f40
```

### View results
```bash
.venv/Scripts/python.exe -m streamlit run dashboard/app.py
```
Select the account → go to the **Sales** tab for the demand vs sales vs inventory chart.

---

## How the simulation works (daily loop)

Each day from `start_date` to `end_date` the engine runs these steps in order:

```
Step 1  Fire Supplier → DC receipts
        Any PO scheduled for today is received at the DC.
        May be late (probability = 1 - on_time_rate, delayed by 1-3 days).
        May be partial (probability = partial_delivery_rate, remainder rescheduled).

Step 2  Fire DC → Store receipts
        Any store order scheduled for today is received at the store.
        Same late / partial logic.

Step 3  Sell to customers + record daily data
        For each store × item:
          demand  = requested_qty from demand matrix (pre-generated)
          sold    = min(demand, on_hand)          ← sales can never exceed demand
          lost    = demand - sold                 ← tracked as lost sales
        Writes daily rows to sales_daily + store_inventory_daily.

Step 4  Stores place replenishment orders (on their configured order day)
        If on_hand < reorder_point (weeks_of_cover × avg_daily × 7):
          order up to target_stock.
          DC ships available stock immediately → receipt scheduled for next day.

Step 5  DCs raise supplier POs (on DC review day)
        Same reorder-point logic using aggregate demand across DC's stores.
        Lead time drawn from [lead_time_min, lead_time_max].

Step 6  Sunday flush — weekly aggregates
        Writes sales_history (weekly totals) and store_inventory snapshot
        to ClickHouse. Resets weekly accumulator.
```

### Demand generation (`demand_gen.py`)
```
baseline velocity (by category × velocity class)
  × lifecycle multiplier  (growth: ramp 0.3→1.0 over 90 days; decay: 1.0→0.3 last 90 days)
  × annual seasonality    (weekly index, weeks 49-52 peak at 1.35×)
  × day-of-week factor
  × lognormal noise       (σ=0.15, per store/item/day)
  × promo multiplier      (if item/store in an active promo)
→ round → int → requested_qty
```

### Replenishment policies
| Policy | Logic |
|---|---|
| `trailing_avg_28d` | Reorder point based on rolling 28-day avg demand |
| `baseline_only` | Uses static baseline velocity, ignores history |
| `promo_aware_7d` | Looks ahead 7 days; if promo upcoming, orders to cover promo demand. Emergency restock on promo day if stock = 0 |

---

## Clearing and re-running

If you need a clean run (e.g., to fix duplicate data):

```python
# In Python — delete all rows for a sim_id from ClickHouse
tables = [
    'demand', 'sales_history', 'sales_daily',
    'store_inventory', 'store_inventory_daily',
    'store_receipts', 'store_orders', 'store_order_details',
    'supplier_orders', 'supplier_order_details',
    'supplier_receipts', 'dc_inventory'
]
for t in tables:
    ch.command(f"ALTER TABLE {t} DELETE WHERE simulation_id = '{SIM_ID}'")
```

Then re-run `demand_gen.py` and `simulation.py` once.

> **Important**: Never run `simulation.py` twice against the same `sim_id` without clearing first — ClickHouse appends rows with no deduplication, which inflates sales vs demand figures.

---

## Key IDs (Test 123)

| Field | Value |
|---|---|
| account_id | `354aaab1-2479-4c90-aa98-53e707f08f40` |
| simulation_id | `614d7903-7269-4d94-8e21-db839308fbd2` |
| config file | `config_354aaab1-2479-4c90-aa98-53e707f08f40.yaml` |
| branch | `nir/update` |
