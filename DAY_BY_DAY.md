# Day-by-Day Simulation Mechanics

This document explains exactly what happens each day inside the simulation loop — how sales are made, how stock moves, and how deliveries are managed at every level of the supply chain.

---

## The Calendar Pattern

The simulation runs one day at a time from `start_date` to `end_date`. Two days of the week have special behaviour:

| Day | What extra happens |
|---|---|
| **Monday** | Customer orders created, DC raises supplier POs |
| **Every day** | Receipts posted, store needs calculated, DC allocates to stores, customer demand fulfilled |
| **Sunday** | Weekly snapshots written, weekly totals reset |

---

## Every Day — Step by Step

### 1. Supplier Receipts Arrive at DCs

Before anything else, the simulation checks if any scheduled supplier deliveries are due today.

For each expected receipt:

**Can it be late?**
- A random roll is made against `p_late` (10% for DC_01, 20% for DC_02).
- If late, the delivery is pushed forward by 1–3 days (DC_01) or 2–5 days (DC_02) and nothing arrives today.

**Can it be partial?**
- If it wasn't already late, a second roll is made against `p_partial` (8% for DC_01, 15% for DC_02).
- If partial, only a fraction of the order arrives (70–90% for DC_01, 55–80% for DC_02).
- The remainder is re-scheduled to arrive in another 2–5 days (DC_01) or 3–7 days (DC_02).
- A `PARTIAL` receipt record is written to ClickHouse.

**Full delivery:**
- If neither triggered, the full quantity arrives.
- `on_hand[DC][item] += received_qty`
- `on_order[DC][item] -= received_qty`
- A `FULL` receipt record is written to ClickHouse.

```
Example:
  DC_01 expects 120 units of ITEM_003 today.
  Roll p_late (10%) → miss. Not late.
  Roll p_partial (8%) → hit. Fraction = 82%.
  → 98 units arrive today  (on_hand DC_01 ITEM_003 += 98)
  → 22 units rescheduled for 3 days later
```

---

### 2. Customer Orders Created (Mondays only)

On Monday, one customer order is opened per store for the coming week.

- The order quantity per item = sum of daily demand from the demand matrix across all 7 days of that week.
- Order number format: `CO_<run_id>_<YYYY-Www>_<store_code>`
- Written to `customer_orders` (header) and `customer_order_details` (one line per item).
- These orders represent what the store *expects* to sell that week — they are not yet fulfilled here.

---

### 3. Store Replenishment Needs Calculated

For every store and every item, the simulation works out how much stock needs to come down from the DC today.

```
1. Record today's demand in a rolling 28-day history for this store-item.
2. avg_daily = average of that history
3. coverage_target = avg_daily × coverage_days
                     (10 days for 'medium' velocity items, 14 days for 'slow')
4. need = max(0,  coverage_target − current on_hand at store)
```

This is a continuous replenishment model — the store always tries to maintain a certain number of days of cover. If stock is above the target, `need = 0` and nothing is requested.

---

### 4. DC Allocates Stock to Stores

Each DC looks at all the `need` values from its assigned stores for each item and decides how to distribute its on-hand stock.

**If the DC has enough for everyone:**
```
Each store gets exactly what it needs.
DC on_hand -= total_need
```

**If the DC doesn't have enough (shortage):**
```
Each store gets a proportional share:
  store_share = floor( store_need / total_need × dc_available )

Leftover units from rounding are given out one at a time,
prioritising the stores with the biggest remaining shortfall.

DC on_hand → 0
```

Stock moves instantly — DC to store happens same day, no transit lead time.

```
Example:
  DC_01 has 50 units of ITEM_005.
  Store_001 needs 30, Store_002 needs 20, Store_003 needs 10  → total = 60
  DC can't cover all.
  Store_001 gets floor(30/60 × 50) = 25
  Store_002 gets floor(20/60 × 50) = 16
  Store_003 gets floor(10/60 × 50) = 8
  Distributed = 49, leftover = 1 unit → goes to Store_001 (largest shortfall: 5)
  Final: Store_001 +26, Store_002 +16, Store_003 +8
```

---

### 5. Stores Fulfill Customer Demand (Sales)

This is where the actual sale happens. For every store and every item:

```
requested  = demand_matrix[store][item][today]   (pre-generated)
delivered  = min(requested, on_hand[store][item])
unfilled   = requested − delivered

on_hand[store][item] -= delivered
```

- `delivered` = units actually sold to customers.
- `unfilled` = lost sales (no backorders — if stock isn't there, the sale is lost).
- Both are accumulated into weekly totals (`weekly_delivered`, `weekly_unfilled`, `weekly_ordered`).

```
Example:
  Store_004 has 8 units of ITEM_002 on hand.
  Today's demand = 11 units.
  delivered = min(11, 8) = 8    → 8 units sold
  unfilled  = 11 − 8 = 3        → 3 units lost
  on_hand → 0
```

---

### 6. DC Raises Supplier Purchase Orders (Mondays only)

After stores have been served, each DC checks whether it needs to reorder from its supplier.

```
dc_avg_daily  = sum of avg_daily across all stores this DC serves
target        = dc_avg_daily × 28 days  (28-day cover target)
in_pipeline   = on_hand[DC] + on_order[DC]
raw_order     = max(0, target − in_pipeline)
order_qty     = ceil(raw_order / case_pack) × case_pack   (round up to full cases)
```

Case pack sizes: Grocery = 12 EA, Apparel = 1 EA, everything else = 6 EA.

- If `raw_order = 0` (already enough stock + inbound), no PO is raised.
- Lead time is drawn randomly: 3–7 days for DC_01, 7–14 days for DC_02.
- The expected receipt date = today + lead_time.
- `on_order[DC][item] += order_qty` — the on-order balance is updated immediately so that next Monday's calculation doesn't double-order.
- PO written to `supplier_orders` and `supplier_order_details`.
- Receipt entry added to the schedule (will be processed on the expected date in Step 1).

```
Example:
  DC_02, ITEM_007 on a Monday.
  5 stores average 3 units/day each → dc_avg_daily = 15
  target = 15 × 28 = 420 units
  on_hand = 180, on_order = 60  → in_pipeline = 240
  raw_order = 420 − 240 = 180
  case_pack = 6  → order_qty = ceil(180/6) × 6 = 180
  Lead time roll = 10 days  → expected receipt: today + 10
```

---

## Sunday — End of Week Flush

At the end of each ISO week (Sunday), the simulation writes a full snapshot to ClickHouse and resets weekly counters.

### Order Delivery Records

For every store-item, a row is written to `order_delivery`:

| Field | Value |
|---|---|
| `order_quantity` | Total units demanded across the week |
| `delivered_quantity` | Total units actually sold |
| `unfilled_quantity` | Total lost sales |
| `delivery_status` | `FULL` if unfilled = 0, else `PARTIAL` |

### Store Inventory Snapshot

For every store-item, a row is written to `store_inventory`:

| Status | Condition |
|---|---|
| `ZERO` | on_hand = 0 |
| `LOW` | weeks-of-cover < 50% of the velocity coverage target |
| `AVAILABLE` | everything else |

Weeks of cover = `on_hand / (avg_daily × 7)`.

### DC Inventory Snapshot

Same logic for DCs, written to `dc_inventory`:

- `on_hand_quantity` = physical stock at DC
- `available_quantity` = on_hand + on_order (includes stock in transit from supplier)
- Status uses the same LOW/ZERO/AVAILABLE thresholds against the 28-day cover target.

### Reset

All weekly accumulators (`weekly_delivered`, `weekly_unfilled`, `weekly_ordered`) are zeroed so the next week starts clean.

---

## Summary: What Moves Where Each Day

```
Monday:
  [Supplier receipts posted to DC]        ← Step 1
  [Customer orders created for the week]  ← Step 2
  [Store needs calculated]                ← Step 3
  [DC allocates stock to stores]          ← Step 4
  [Stores sell to customers]              ← Step 5
  [DCs raise POs to suppliers]            ← Step 6

Tuesday–Saturday:
  [Supplier receipts posted to DC]        ← Step 1
  [Store needs calculated]                ← Step 3
  [DC allocates stock to stores]          ← Step 4
  [Stores sell to customers]              ← Step 5

Sunday:
  [Supplier receipts posted to DC]        ← Step 1
  [Store needs calculated]                ← Step 3
  [DC allocates stock to stores]          ← Step 4
  [Stores sell to customers]              ← Step 5
  [Weekly snapshots written to CH]        ← Step 8
  [Weekly totals reset]
```

---

## Stock Flow Diagram (Single Day)

```
Supplier
   │
   │  (if receipt due today)
   ▼
DC on_hand  ──────────────────────────────────────────────────────────┐
   │                                                                   │
   │  Step 4: DC allocates to stores                                  │
   │  (proportional if short)                                         │
   ▼                                                                   │
Store on_hand                                                          │
   │                                                                   │
   │  Step 5: Customer buys                                            │
   │  delivered = min(demand, on_hand)                                 │
   ▼                                                                   │
Customer ← sold units                                                  │
   ✗ ← lost sales (unfilled)                                          │
                                                                       │
(Monday) DC checks if on_hand + on_order < 28-day target ─────────────┘
         → raises PO to supplier → joins receipt_schedule
```

---

## Key Numbers at a Glance

| Parameter | Value |
|---|---|
| Store coverage target (medium items) | 10 days |
| Store coverage target (slow items) | 14 days |
| DC coverage target | 28 days |
| Demand smoothing window | 28 days |
| Store starting stock | 5 days of avg demand |
| DC_01 starting stock | 30 days of avg demand |
| DC_02 starting stock | 18 days of avg demand |
| DC_01 lead time | 3–7 days |
| DC_02 lead time | 7–14 days |
| Inventory snapshot frequency | Weekly (every Sunday) |
| Replenishment review frequency | Daily (needs) + Weekly (POs on Monday) |
