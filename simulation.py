"""
simulation.py — Daily simulation loop

Usage:
  python simulation.py --config config.yaml \
    --sim_id 30000000-0000-0000-0000-000000000001 \
    --account_id 10000000-0000-0000-0000-000000000001
"""

import argparse
import math
import os
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2
import yaml
import clickhouse_connect

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── CLI ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument('--config',     default='config.yaml')
parser.add_argument('--sim_id',     required=True)
parser.add_argument('--account_id', required=True)
args = parser.parse_args()

with open(args.config) as f:
    cfg = yaml.safe_load(f)

SIM_ID     = args.sim_id
ACCOUNT_ID = args.account_id
RUN_ID     = cfg['run_id']
SEED       = cfg['seed']
POLICY     = cfg.get('replenishment_policy', 'trailing_avg_28d')

START_DATE = date.fromisoformat(cfg['start_date'])
END_DATE   = date.fromisoformat(cfg['end_date'])

STORES    = cfg['stores']
DCS       = cfg['dcs']
ITEMS     = cfg['items']
DC_ASSIGN   = cfg['dc_assignment']
DC_SUPPLIER = cfg['dc_supplier_assignment']

SMOOTHING_DAYS         = cfg['demand_smoothing_window_days']
STORE_START_STOCK_DAYS = cfg['store_start_stock_days']
CASE_PACK              = cfg['case_pack_sizes']

DAY_MAP = {
    'MONDAY': 0, 'TUESDAY': 1, 'WEDNESDAY': 2, 'THURSDAY': 3,
    'FRIDAY': 4, 'SATURDAY': 5, 'SUNDAY': 6,
}
DC_REVIEW_DOW = DAY_MAP[cfg.get('dc_review_dow', 'Monday').upper()]

# Store → DC lookup
STORE_DC = {}
for dc, store_list in DC_ASSIGN.items():
    for s in store_list:
        STORE_DC[s] = dc

# Item → stable line number
ITEM_LINE_NUM = {item: idx + 1 for idx, item in enumerate(ITEMS)}

# ── RNG ──────────────────────────────────────────────────────────────────────

rng = np.random.default_rng(SEED)

# ── Connections ───────────────────────────────────────────────────────────────

pg_conn = psycopg2.connect(
    host=os.environ['PG_HOST'],
    port=os.environ.get('PG_PORT', 5432),
    dbname=os.environ['PG_DB'],
    user=os.environ['PG_USER'],
    password=os.environ['PG_PASSWORD'],
    sslmode=os.environ.get('PG_SSLMODE', 'prefer')
)

ch = clickhouse_connect.get_client(
    host=os.environ['CH_HOST'],
    port=int(os.environ.get('CH_PORT', 8123)),
    database=os.environ['CH_DB'],
    username=os.environ['CH_USER'],
    password=os.environ['CH_PASSWORD'],
    verify=False
)

# ── Read simulation_config and items from NeonDB ──────────────────────────────

pg_cur = pg_conn.cursor()
pg_cur.execute(
    "SELECT dc_configs, store_configs, supplier_configs FROM simulation_config "
    "WHERE simulation_id = %s",
    (SIM_ID,)
)
row = pg_cur.fetchone()
if row is None:
    raise SystemExit(f"No simulation_config found for sim_id={SIM_ID}")

dc_cfg_list, store_cfg_list, supplier_cfg_list = row[0], row[1], row[2]
dc_cfg       = {d['dc_id']:       d for d in dc_cfg_list}
store_cfg    = {s['store_id']:    s for s in store_cfg_list}
supplier_cfg = {s['supplier_id']: s for s in supplier_cfg_list}

items_df = pd.read_sql("SELECT * FROM items", pg_conn)
pg_conn.close()

velocity_map   = items_df.set_index('item_id')['velocity_class'].to_dict()
category_map   = items_df.set_index('item_id')['category'].to_dict()
unit_price_map = {k: float(v) for k, v in items_df.set_index('item_id')['unit_price'].items()}
unit_cost_map  = {k: float(v) for k, v in items_df.set_index('item_id')['unit_cost'].items()}

def get_case_pack(item_id):
    cat = category_map.get(item_id, 'default')
    return CASE_PACK.get(cat, CASE_PACK['default'])

# ── Load demand matrix ────────────────────────────────────────────────────────

print("Loading demand matrix...")
dm = pd.read_parquet('demand_matrix.parquet')
if hasattr(dm['date'].iloc[0], 'date'):
    dm['date'] = dm['date'].apply(lambda x: x.date() if hasattr(x, 'date') else x)

dm_idx = dm.set_index(['store_id', 'item_id', 'date'])['requested_qty'].to_dict()

def demand(store, item, day):
    return int(dm_idx.get((store, item, day), 0))

# ── Baseline demand (for starting inventory) ──────────────────────────────────

first_28 = [START_DATE + timedelta(days=i) for i in range(28)]

baseline_demand = {}
for store in STORES:
    baseline_demand[store] = {}
    for item in ITEMS:
        vals = [demand(store, item, d) for d in first_28]
        baseline_demand[store][item] = float(np.mean(vals)) if vals else 0.0

# ── Replenishment policy ──────────────────────────────────────────────────────

demand_history = defaultdict(lambda: defaultdict(list))

def get_avg_daily(store, item):
    if POLICY == 'trailing_avg_28d':
        hist = demand_history[store][item]
        return float(np.mean(hist)) if hist else baseline_demand[store][item]
    return baseline_demand[store][item]

# ── Initialise state ──────────────────────────────────────────────────────────

on_hand  = defaultdict(lambda: defaultdict(int))
on_order = defaultdict(lambda: defaultdict(int))

receipt_schedule       = []   # Supplier → DC receipts
store_receipt_schedule = []   # DC → Store receipts

# DC starting stock — from dc_cfg[dc]['start_stock_days']
for dc in DCS:
    dc_stores  = DC_ASSIGN[dc]
    start_days = dc_cfg[dc]['start_stock_days']
    for item in ITEMS:
        dc_avg = sum(baseline_demand[s][item] for s in dc_stores)
        on_hand[dc][item] = int(round(dc_avg * start_days))

# Store starting stock
for store in STORES:
    for item in ITEMS:
        on_hand[store][item] = int(round(baseline_demand[store][item] * STORE_START_STOCK_DAYS))

# ── Counters ──────────────────────────────────────────────────────────────────

counts = {
    'store_orders': 0, 'store_order_details': 0, 'store_receipts': 0,
    'sales_history': 0, 'supplier_orders': 0, 'supplier_order_details': 0,
    'supplier_receipts': 0, 'store_inventory': 0, 'dc_inventory': 0,
}

po_seq = 0   # PO sequence
sr_seq = 0   # Store receipt sequence
so_seq = 0   # Store order sequence

# ── Weekly sales accumulator ──────────────────────────────────────────────────

weekly_sales = defaultdict(lambda: defaultdict(float))

# ── Buffers ───────────────────────────────────────────────────────────────────

supplier_orders_buf        = []
supplier_order_details_buf = []
supplier_receipts_buf      = []
store_receipts_buf         = []

# ── Helpers ───────────────────────────────────────────────────────────────────

def iso_week_str(d):
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"

def inventory_status_store(store, item, avg_daily):
    oh        = on_hand[store][item]
    threshold = store_cfg[store]['weeks_of_cover_threshold']
    if oh == 0:
        return 'ZERO'
    if avg_daily <= 0:
        return 'AVAILABLE'
    woc = oh / (avg_daily * 7.0)
    return 'LOW' if woc < threshold else 'AVAILABLE'

def inventory_status_dc(dc, item, dc_avg_daily):
    oh        = on_hand[dc][item]
    threshold = dc_cfg[dc]['weeks_of_cover_threshold']
    if oh == 0:
        return 'ZERO'
    if dc_avg_daily <= 0:
        return 'AVAILABLE'
    woc = oh / (dc_avg_daily * 7.0)
    return 'LOW' if woc < threshold else 'AVAILABLE'

# ── Daily loop ────────────────────────────────────────────────────────────────

current_date = START_DATE
while current_date <= END_DATE:
    dow       = current_date.weekday()   # Monday=0, Sunday=6
    is_sunday = (dow == 6)
    week_str  = iso_week_str(current_date)

    if dow == 0:
        print(f"  {current_date}  ({week_str})")

    # ── Step 1: Fire Supplier → DC receipts ──────────────────────────────────

    still_pending = []
    for entry in receipt_schedule:
        if entry['scheduled_date'] != current_date:
            still_pending.append(entry)
            continue

        dc_id   = entry['dc_id']
        item_id = entry['item_id']
        qty     = entry['qty']
        po_num  = entry['po_number']

        sup_id   = DC_SUPPLIER[dc_id]
        sup_prof = supplier_cfg[sup_id]

        is_late_flag    = entry.get('is_late', False)
        already_partial = entry.get('already_partial', False)

        # Late check
        if not is_late_flag and rng.random() > sup_prof['on_time_rate']:
            extra = int(rng.integers(sup_prof['late_days_min'], sup_prof['late_days_max'] + 1))
            entry['scheduled_date'] = current_date + timedelta(days=extra)
            entry['is_late'] = True
            still_pending.append(entry)
            continue

        # Partial check
        if not already_partial and rng.random() < sup_prof['partial_delivery_rate']:
            frac      = rng.uniform(sup_prof['partial_frac_min'], sup_prof['partial_frac_max'])
            received  = int(math.floor(qty * frac))
            remainder = qty - received

            on_hand[dc_id][item_id]  += received
            on_order[dc_id][item_id]  = max(0, on_order[dc_id][item_id] - received)

            receipt_seq += 1
            supplier_receipts_buf.append({
                'receipt_id':            f'REC_{SIM_ID}_{receipt_seq:06d}',
                'line_number':           1,
                'simulation_id':         SIM_ID,
                'account_id':            ACCOUNT_ID,
                'purchase_order_number': po_num,
                'dc_id':                 dc_id,
                'item_id':               item_id,
                'receipt_date':          current_date,
                'received_quantity':     float(received),
                'unfilled_quantity':     float(remainder),
                'receipt_type':          'PARTIAL',
            })
            counts['supplier_receipts'] += 1

            gap = int(rng.integers(sup_prof['remainder_gap_min'], sup_prof['remainder_gap_max'] + 1))
            still_pending.append({
                'dc_id':           dc_id,
                'item_id':         item_id,
                'po_number':       po_num,
                'qty':             remainder,
                'scheduled_date':  current_date + timedelta(days=gap),
                'is_late':         is_late_flag,
                'already_partial': True,
            })
        else:
            # Full delivery
            on_hand[dc_id][item_id]  += qty
            on_order[dc_id][item_id]  = max(0, on_order[dc_id][item_id] - qty)

            receipt_seq += 1
            supplier_receipts_buf.append({
                'receipt_id':            f'REC_{SIM_ID}_{receipt_seq:06d}',
                'line_number':           1,
                'simulation_id':         SIM_ID,
                'account_id':            ACCOUNT_ID,
                'purchase_order_number': po_num,
                'dc_id':                 dc_id,
                'item_id':               item_id,
                'receipt_date':          current_date,
                'received_quantity':     float(qty),
                'unfilled_quantity':     0.0,
                'receipt_type':          'FULL',
            })
            counts['supplier_receipts'] += 1

    receipt_schedule = still_pending

    # ── Step 2: Fire DC → Store receipts ─────────────────────────────────────

    still_pending_sr = []
    for entry in store_receipt_schedule:
        if entry['scheduled_date'] != current_date:
            still_pending_sr.append(entry)
            continue

        store_id = entry['store_id']
        item_id  = entry['item_id']
        qty      = entry['qty']
        so_num   = entry['so_number']
        dc_id    = STORE_DC[store_id]
        dcfg     = dc_cfg[dc_id]

        is_late_flag    = entry.get('is_late', False)
        already_partial = entry.get('already_partial', False)

        # Late check
        if not is_late_flag and rng.random() > dcfg['on_time_rate']:
            extra = int(rng.integers(dcfg['late_days_min'], dcfg['late_days_max'] + 1))
            entry['scheduled_date'] = current_date + timedelta(days=extra)
            entry['is_late'] = True
            still_pending_sr.append(entry)
            continue

        # Partial check
        if not already_partial and rng.random() < dcfg['partial_delivery_rate']:
            frac      = rng.uniform(dcfg['partial_frac_min'], dcfg['partial_frac_max'])
            received  = int(math.floor(qty * frac))
            remainder = qty - received

            on_hand[store_id][item_id]  += received
            on_order[store_id][item_id]  = max(0, on_order[store_id][item_id] - received)

            sr_seq += 1
            store_receipts_buf.append({
                'receipt_id':         f'SR_{SIM_ID}_{sr_seq:06d}',
                'line_number':        ITEM_LINE_NUM[item_id],
                'simulation_id':      SIM_ID,
                'account_id':         ACCOUNT_ID,
                'store_order_number': so_num,
                'store_id':           store_id,
                'item_id':            item_id,
                'receipt_date':       current_date,
                'received_quantity':  float(received),
                'unfilled_quantity':  float(remainder),
                'receipt_type':       'PARTIAL',
            })
            counts['store_receipts'] += 1

            gap = int(rng.integers(dcfg['remainder_gap_min'], dcfg['remainder_gap_max'] + 1))
            still_pending_sr.append({
                'store_id':        store_id,
                'item_id':         item_id,
                'so_number':       so_num,
                'qty':             remainder,
                'scheduled_date':  current_date + timedelta(days=gap),
                'is_late':         is_late_flag,
                'already_partial': True,
            })
        else:
            # Full delivery
            on_hand[store_id][item_id]  += qty
            on_order[store_id][item_id]  = max(0, on_order[store_id][item_id] - qty)

            sr_seq += 1
            store_receipts_buf.append({
                'receipt_id':         f'SR_{SIM_ID}_{sr_seq:06d}',
                'line_number':        ITEM_LINE_NUM[item_id],
                'simulation_id':      SIM_ID,
                'account_id':         ACCOUNT_ID,
                'store_order_number': so_num,
                'store_id':           store_id,
                'item_id':            item_id,
                'receipt_date':       current_date,
                'received_quantity':  float(qty),
                'unfilled_quantity':  0.0,
                'receipt_type':       'FULL',
            })
            counts['store_receipts'] += 1

    store_receipt_schedule = still_pending_sr

    # ── Step 3: Update demand history + sell to customers ────────────────────

    for store in STORES:
        for item in ITEMS:
            req = demand(store, item, current_date)

            # Rolling demand history
            hist = demand_history[store][item]
            hist.append(req)
            if len(hist) > SMOOTHING_DAYS:
                hist.pop(0)

            # Sell
            oh   = on_hand[store][item]
            sold = min(req, oh)
            on_hand[store][item] = max(0, oh - sold)
            weekly_sales[store][item] += sold

    # ── Step 4: Store order placement (per store's order_cycle_day) ──────────

    so_rows  = []
    sod_rows = []

    for store in STORES:
        scfg      = store_cfg[store]
        order_dow = DAY_MAP[scfg['order_cycle_day'].upper()]
        if dow != order_dow:
            continue

        # Identify items below reorder point
        items_to_order = []
        for item in ITEMS:
            avg_daily     = get_avg_daily(store, item)
            reorder_point = scfg['reorder_point_weeks'] * avg_daily * 7
            if on_hand[store][item] >= reorder_point:
                continue
            target_stock = scfg['target_stock_weeks'] * avg_daily * 7
            order_qty    = max(0, int(round(target_stock - on_hand[store][item])))
            if order_qty > 0:
                items_to_order.append((item, order_qty))

        if not items_to_order:
            continue

        so_seq += 1
        dc_id  = STORE_DC[store]
        so_num = f"SO_{RUN_ID}_{current_date.strftime('%Y%m%d')}_{store}_{so_seq:04d}"

        so_rows.append({
            'store_order_number': so_num,
            'simulation_id':      SIM_ID,
            'account_id':         ACCOUNT_ID,
            'store_id':           store,
            'dc_id':              dc_id,
            'order_week':         week_str,
            'order_date':         current_date,
            'order_status':       'OPEN',
        })
        counts['store_orders'] += 1

        for item, order_qty in items_to_order:
            sod_rows.append({
                'store_order_number': so_num,
                'line_number':        ITEM_LINE_NUM[item],
                'simulation_id':      SIM_ID,
                'account_id':         ACCOUNT_ID,
                'item_id':            item,
                'order_quantity':     float(order_qty),
                'uom':                'EA',
            })
            counts['store_order_details'] += 1

            # DC allocation: ship available stock immediately
            dc_available = on_hand[dc_id][item]
            ship_qty     = min(order_qty, dc_available)
            on_hand[dc_id][item] = max(0, dc_available - ship_qty)

            if ship_qty > 0:
                on_order[store][item] += ship_qty
                store_receipt_schedule.append({
                    'store_id':        store,
                    'item_id':         item,
                    'so_number':       so_num,
                    'qty':             ship_qty,
                    'scheduled_date':  current_date,   # fires next loop iteration
                    'is_late':         False,
                    'already_partial': False,
                })

            unfilled_qty = order_qty - ship_qty
            if unfilled_qty > 0:
                # DC stockout — record zero-received receipt immediately
                sr_seq += 1
                store_receipts_buf.append({
                    'receipt_id':         f'SR_{SIM_ID}_{sr_seq:06d}',
                    'line_number':        ITEM_LINE_NUM[item],
                    'simulation_id':      SIM_ID,
                    'account_id':         ACCOUNT_ID,
                    'store_order_number': so_num,
                    'store_id':           store,
                    'item_id':            item,
                    'receipt_date':       current_date,
                    'received_quantity':  0.0,
                    'unfilled_quantity':  float(unfilled_qty),
                    'receipt_type':       'PARTIAL',
                })
                counts['store_receipts'] += 1

    if so_rows:
        ch.insert_df('store_orders', pd.DataFrame(so_rows))
    if sod_rows:
        ch.insert_df('store_order_details', pd.DataFrame(sod_rows))

    # ── Step 5: DC raises supplier POs (on DC review day, reorder-point check)

    if dow == DC_REVIEW_DOW:
        for dc in DCS:
            dc_stores = DC_ASSIGN[dc]
            supplier  = DC_SUPPLIER[dc]
            dcfg      = dc_cfg[dc]
            sup_prof  = supplier_cfg[supplier]

            for item in ITEMS:
                dc_avg = sum(get_avg_daily(s, item) for s in dc_stores)

                reorder_point  = dcfg['reorder_point_weeks'] * dc_avg * 7
                stock_position = on_hand[dc][item] + on_order[dc][item]
                if stock_position >= reorder_point:
                    continue

                target_stock = dcfg['target_stock_weeks'] * dc_avg * 7
                raw_order    = max(0, int(round(target_stock - stock_position)))
                if raw_order == 0:
                    continue

                case_pack = get_case_pack(item)
                order_qty = math.ceil(raw_order / case_pack) * case_pack

                po_seq += 1
                po_num = f"PO_{RUN_ID}_{current_date.strftime('%Y%m%d')}_{dc}_{po_seq:04d}"

                lead_time        = int(rng.integers(sup_prof['lead_time_min'], sup_prof['lead_time_max'] + 1))
                expected_receipt = current_date + timedelta(days=lead_time)

                supplier_orders_buf.append({
                    'purchase_order_number': po_num,
                    'simulation_id':         SIM_ID,
                    'account_id':            ACCOUNT_ID,
                    'dc_id':                 dc,
                    'supplier_id':           supplier,
                    'order_date':            current_date,
                    'expected_receipt_date': expected_receipt,
                    'order_status':          'OPEN',
                })
                counts['supplier_orders'] += 1

                supplier_order_details_buf.append({
                    'purchase_order_number': po_num,
                    'line_number':           1,
                    'simulation_id':         SIM_ID,
                    'account_id':            ACCOUNT_ID,
                    'dc_id':                 dc,
                    'item_id':               item,
                    'supplier_id':           supplier,
                    'need_quantity':         int(raw_order),
                    'order_quantity':        float(order_qty),
                    'unit_cost':             unit_cost_map.get(item, 0.0),
                    'uom':                   'EA',
                })
                counts['supplier_order_details'] += 1

                receipt_schedule.append({
                    'dc_id':           dc,
                    'item_id':         item,
                    'po_number':       po_num,
                    'qty':             order_qty,
                    'scheduled_date':  expected_receipt,
                    'is_late':         False,
                    'already_partial': False,
                })

                on_order[dc][item] += order_qty

        if supplier_orders_buf:
            ch.insert_df('supplier_orders', pd.DataFrame(supplier_orders_buf))
            supplier_orders_buf = []
        if supplier_order_details_buf:
            ch.insert_df('supplier_order_details', pd.DataFrame(supplier_order_details_buf))
            supplier_order_details_buf = []

    # ── Step 6: Weekly snapshot (Sundays) ─────────────────────────────────────

    if is_sunday:
        # Flush receipt buffers
        if supplier_receipts_buf:
            ch.insert_df('supplier_receipts', pd.DataFrame(supplier_receipts_buf))
            supplier_receipts_buf = []
        if store_receipts_buf:
            ch.insert_df('store_receipts', pd.DataFrame(store_receipts_buf))
            store_receipts_buf = []

        # sales_history — weekly aggregate
        sales_rows = []
        for store in STORES:
            for item in ITEMS:
                qty = weekly_sales[store][item]
                sales_rows.append({
                    'simulation_id':  SIM_ID,
                    'account_id':     ACCOUNT_ID,
                    'store_id':       store,
                    'item_id':        item,
                    'sales_week':     week_str,
                    'sales_quantity': float(qty),
                    'sales_amount':   float(qty) * unit_price_map.get(item, 0.0),
                    'unit_price':     unit_price_map.get(item, 0.0),
                    'uom':            'EA',
                })
                counts['sales_history'] += 1

        ch.insert_df('sales_history', pd.DataFrame(sales_rows))

        # store_inventory snapshot
        si_rows = []
        for store in STORES:
            for item in ITEMS:
                avg    = get_avg_daily(store, item)
                status = inventory_status_store(store, item, avg)
                si_rows.append({
                    'simulation_id':      SIM_ID,
                    'account_id':         ACCOUNT_ID,
                    'store_id':           store,
                    'item_id':            item,
                    'inventory_week':     week_str,
                    'on_hand_quantity':   float(on_hand[store][item]),
                    'available_quantity': float(on_hand[store][item]),
                    'on_order_quantity':  float(on_order[store][item]),
                    'inventory_status':   status,
                })
                counts['store_inventory'] += 1

        ch.insert_df('store_inventory', pd.DataFrame(si_rows))

        # dc_inventory snapshot
        di_rows = []
        for dc in DCS:
            dc_stores = DC_ASSIGN[dc]
            for item in ITEMS:
                dc_avg = sum(get_avg_daily(s, item) for s in dc_stores)
                status = inventory_status_dc(dc, item, dc_avg)
                di_rows.append({
                    'simulation_id':      SIM_ID,
                    'account_id':         ACCOUNT_ID,
                    'dc_id':              dc,
                    'item_id':            item,
                    'inventory_week':     week_str,
                    'on_hand_quantity':   float(on_hand[dc][item]),
                    'available_quantity': float(on_hand[dc][item]),
                    'on_order_quantity':  float(on_order[dc][item]),
                    'inventory_status':   status,
                })
                counts['dc_inventory'] += 1

        ch.insert_df('dc_inventory', pd.DataFrame(di_rows))

        # Reset weekly accumulator
        weekly_sales = defaultdict(lambda: defaultdict(float))

    current_date += timedelta(days=1)

# ── Flush remaining buffers ───────────────────────────────────────────────────

if supplier_orders_buf:
    ch.insert_df('supplier_orders', pd.DataFrame(supplier_orders_buf))
if supplier_order_details_buf:
    ch.insert_df('supplier_order_details', pd.DataFrame(supplier_order_details_buf))
if supplier_receipts_buf:
    ch.insert_df('supplier_receipts', pd.DataFrame(supplier_receipts_buf))
if store_receipts_buf:
    ch.insert_df('store_receipts', pd.DataFrame(store_receipts_buf))

# ── Final summary ─────────────────────────────────────────────────────────────

total_days = (END_DATE - START_DATE).days + 1
print(f"\nSimulation complete: {SIM_ID}")
print(f"Days processed: {total_days}")
print(f"Rows written:")
for table, cnt in counts.items():
    print(f"  {table}:  {cnt:,}")
