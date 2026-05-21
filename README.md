# Retail Supply Chain Simulation Engine

A discrete-event, day-by-day retail supply chain simulator. It models a two-tier network (Supplier → DC → Store) over a configurable date range and writes all output to ClickHouse for downstream analytics.

---

## Architecture Overview

```
PostgreSQL (static master data: items, stores, promos)
        │
        ▼
scripts/demand_gen.py ──► demand_matrix.parquet + ClickHouse `demand` table
        │
        ▼
scripts/simulation.py ──► ClickHouse (10 output tables)
                          │
                          ▼
                  dashboard/app.py (Streamlit)
```

**Network topology:**

```
Suppliers (SUP_001, SUP_002)
      │  purchase orders / receipts
      ▼
Distribution Centres (DC_01, DC_02)
      │  daily replenishment allocation
      ▼
Stores (Store_001 … Store_010)
      │  customer demand fulfillment
      ▼
ClickHouse (analytics tables)
```

- Store_001–005 → DC_01 → SUP_001 (domestic — faster, more reliable)
- Store_006–010 → DC_02 → SUP_002 (international — slower, less reliable)

---

## Project Structure

```
retail-sim/
├── config.yaml               # All tunable parameters
├── requirements.txt
├── scripts/
│   ├── setup_postgres.py     # Creates PG tables + seeds master data
│   ├── setup_clickhouse.py   # Creates / recreates ClickHouse tables
│   ├── demand_gen.py         # Phase 1 — generate demand matrix
│   └── simulation.py         # Phase 2 — daily simulation loop
├── dashboard/
│   └── app.py                # Streamlit dashboard
└── docs/
    ├── WORKFLOW.md           # Operational workflow guide
    └── DAY_BY_DAY.md         # Daily simulation logic reference
```

---

## Setup & Run

```bash
# 1. Environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Create ClickHouse output tables (drops and recreates)
python scripts/setup_clickhouse.py

# 3. Generate demand matrix
python scripts/demand_gen.py --config config.yaml --sim_id SIM_001 --account_id ACC_001

# 4. Run simulation
python scripts/simulation.py --config config.yaml --sim_id SIM_001 --account_id ACC_001

# 5. Dashboard
streamlit run dashboard/app.py
```

### Environment Variables

| Variable | Description |
|---|---|
| `PG_HOST`, `PG_PORT`, `PG_DB`, `PG_USER`, `PG_PASSWORD` | PostgreSQL connection |
| `CH_HOST`, `CH_PORT`, `CH_DB`, `CH_USER`, `CH_PASSWORD` | ClickHouse connection |

---

## Phase 1 — Demand Generation (`scripts/demand_gen.py`)

Produces `demand_matrix.parquet`: one row per `(store, item, date)` with a `requested_qty`.

### Demand Formula

```
requested_qty = round( baseline × lifecycle × seasonal × noise × promo )
```

| Factor | How it's computed |
|---|---|
| **baseline** | Per store-item constant. `medium` velocity → Uniform(2, 8) units/day; `slow` → Uniform(3, 21) units/week ÷ 7. Seeded per store-item so results are reproducible. |
| **lifecycle** | Item-level curve across the horizon. `steady` = 1.0; `growth` = ramp 0.3→1.0 over first 90 days; `decay` = ramp 1.0→0.3 over the final 90 days. |
| **seasonal** | ISO-week multiplier. Peaks at 1.35× in weeks 49–52 (Christmas), troughs at 0.85× in weeks 1–4 (January). |
| **noise** | Daily lognormal noise per cell: `LogNormal(0, 0.15)` — roughly ±15% daily variance. |
| **promo** | Multiplier from the `promos` table during the promo period (e.g. 1.5×). Followed by a linear post-promo decay back to 1.0× over `post_promo_decay_days`. |

The matrix is also written to the ClickHouse `demand` table (50 000-row batches) with promo metadata (`promo_id`, `is_promo_demand`).

---

## Phase 2 — Daily Simulation Loop (`scripts/simulation.py`)

Iterates from `start_date` to `end_date` one calendar day at a time. The week boundary is ISO: Monday = start, Sunday = end.

### Initialisation (before loop)

- **Store starting stock**: `round(avg_daily_demand × store_start_stock_days)` per store-item (default: 5 days).
- **DC starting stock**: `round(total_avg_daily_demand_across_dc_stores × dc_start_stock_days)` per DC-item (DC_01 = 30 days, DC_02 = 18 days).
- A `simulation_runs` record is written with status `RUNNING`.

---

### Step 1 — Post Supplier Receipts (every day)

For every receipt in the schedule whose `scheduled_date == today`:

1. **Late check** (first arrival only): roll `p_late`. If triggered, push receipt forward by `Uniform(late_days_min, late_days_max)` days, mark `is_late=True`, keep in queue.
2. **Partial check** (only if not already a split remainder): roll `p_partial`. If triggered:
   - Deliver `floor(qty × Uniform(partial_frac_min, partial_frac_max))` units now — write `PARTIAL` receipt.
   - Re-queue the remainder for `Uniform(remainder_gap_min, remainder_gap_max)` days later.
3. Otherwise: deliver full quantity — write `FULL` receipt.
4. `on_hand[dc][item] += received`, `on_order[dc][item] -= received`.

**DC reliability profiles:**

| | DC_01 | DC_02 |
|---|---|---|
| Lead time | 3–7 days | 7–14 days |
| P(late) | 10% | 20% |
| Late delay | +1–3 days | +2–5 days |
| P(partial) | 8% | 15% |
| Partial fraction | 70–90% | 55–80% |
| Remainder gap | 2–5 days | 3–7 days |

---

### Step 2 — Create Customer Orders (Mondays only)

One order header (`CO_<run_id>_<ISO-week>_<store>`) is created per store. Line items = sum of `requested_qty` over the coming 7-day week from the demand matrix. Written to `customer_orders` and `customer_order_details`.

---

### Step 3 — Compute Store Replenishment Needs (every day)

For each store-item:
```
demand_history  ← append today's demand, keep last 28 days
avg_daily       = mean(demand_history)
target          = avg_daily × coverage_days        # medium=10, slow=14
need            = max(0, target − on_hand[store][item])
```

---

### Step 4 — Allocate DC Stock to Stores (every day)

For each DC-item where `total_need > 0` and DC has stock:

- **Full allocation** (DC stock ≥ total need): each store gets exactly its `need`.
- **Proportional allocation** (DC stock < total need): each store gets `floor(need / total_need × available)`. Leftover units from rounding are distributed one at a time to stores ranked by largest remaining shortfall.

DC `on_hand` is reduced by the total amount distributed (floored to 0).

---

### Step 5 — Fulfill Customer Demand at Stores (every day)

```
delivered             = min(requested_qty, on_hand[store][item])
unfilled              = requested_qty − delivered
on_hand[store][item] -= delivered
```

Weekly accumulators (`weekly_delivered`, `weekly_unfilled`, `weekly_ordered`) accumulate each day.

---

### Step 6 — Weekly Sales Tally (Sundays only)

Computes `Σ(delivered × unit_price)` across all stores and items for the week. Logged to stdout.

---

### Step 7 — DC Raises Supplier POs (Mondays only)

For each DC-item:
```
dc_avg_daily  = sum of avg_daily across all stores served by this DC
target_dc     = dc_avg_daily × dc_coverage_days          # 28 days
raw_order     = max(0, target_dc − (on_hand[dc] + on_order[dc]))
order_qty     = ceil(raw_order / case_pack) × case_pack  # round up to full cases
```

Case pack sizes: Grocery = 12 EA, Apparel = 1 EA, default = 6 EA.

A PO (`PO_<run_id>_<date>_<dc>_<seq>`) is created, a random lead time drawn, and the receipt pushed onto `receipt_schedule`. `on_order[dc][item] += order_qty`. Written to `supplier_orders` and `supplier_order_details`.

---

### Step 8 — Weekly Snapshots & Flush (Sundays only)

- **`supplier_receipts`** buffer flushed to ClickHouse.
- **`order_delivery`**: one row per store-item with `order_quantity`, `delivered_quantity`, `unfilled_quantity`, status = `FULL` or `PARTIAL`.
- **`store_inventory`** snapshot — `on_hand`, `available_quantity` (= on_hand), and status:
  - `ZERO` — on_hand = 0
  - `LOW` — weeks-of-cover < 50% of the velocity-based coverage target
  - `AVAILABLE` — otherwise
- **`dc_inventory`** snapshot — `on_hand`, `available_quantity` (= on_hand + on_order), same LOW/ZERO/AVAILABLE logic against the 28-day coverage target.
- All weekly accumulators reset.

---

## ClickHouse Schema (10 tables)

| Table | Grain | Key fields |
|---|---|---|
| `simulation_runs` | 1 row per sim | `simulation_id`, `simulation_status` (RUNNING→COMPLETE) |
| `demand` | store × item × day | `demand_qty`, `promo_id`, `is_promo_demand` |
| `customer_orders` | store × week | `customer_order_number`, `order_status` |
| `customer_order_details` | order × item | `order_quantity` |
| `order_delivery` | store × item × week | `delivered_quantity`, `unfilled_quantity`, `delivery_status` |
| `supplier_orders` | PO header | `dc_code`, `supplier_code`, `expected_receipt_date` |
| `supplier_order_details` | PO × item | `order_quantity` |
| `supplier_receipts` | receipt event | `receipt_type` (FULL/PARTIAL), `is_late` |
| `store_inventory` | store × item × week | `on_hand_quantity`, `inventory_status` |
| `dc_inventory` | DC × item × week | `on_hand_quantity`, `available_quantity` (incl. on-order) |

---

## Configuration Reference (`config.yaml`)

| Key | Default | Description |
|---|---|---|
| `seed` | 42 | RNG seed — controls all randomness for reproducibility |
| `start_date` / `end_date` | 2024-01-01 / 2024-12-31 | Simulation horizon |
| `stores` | 10 | List of store codes |
| `dcs` | DC_01, DC_02 | List of DC codes |
| `dc_assignment` | 5 stores per DC | Which stores each DC serves |
| `dc_supplier_assignment` | one supplier per DC | Supplier for each DC |
| `items` | 10 | List of item codes |
| `store_coverage_days` | medium=10, slow=14 | Store replenishment target in days of cover |
| `dc_coverage_days` | 28 | DC replenishment target in days of cover |
| `demand_smoothing_window_days` | 28 | Rolling window for computing avg_daily at stores |
| `store_start_stock_days` | 5 | Initial store inventory in days of avg demand |
| `dc_profiles` | per DC | Lead times, late/partial probabilities, starting stock |
| `case_pack_sizes` | Grocery=12, Apparel=1, default=6 | Minimum order rounding unit |

---

## Key Invariants

- All on-hand inventory values are integers and never go below 0.
- `demand_matrix.parquet` is read-only once generated; the simulation never modifies it.
- All ClickHouse writes use `insert_df` (batched); supplier receipts and supplier orders are buffered within the week and flushed on Sunday/Monday respectively.
- All week IDs use ISO format: `YYYY-Www` (e.g. `2024-W01`).
- Simulation status transitions: `RUNNING` → `COMPLETE` via an `ALTER TABLE … UPDATE` at the end of the run.
