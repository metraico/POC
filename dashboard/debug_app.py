"""
dashboard/debug_app.py — Debug Streamlit dashboard
Runs supply-chain simulation from sample_data CSVs, no database required.
Outputs are saved to output/<run_id>/ as CSVs.
"""

import io
import json
import math
import time
import uuid
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

PROJECT_ROOT = Path(__file__).parent.parent
SAMPLE_DATA_ROOT = PROJECT_ROOT / "sample_data"
OUTPUT_ROOT = PROJECT_ROOT / "output"

st.set_page_config(page_title="Debug Simulation", layout="wide")
st.title("Debug Simulation Dashboard")

# ── Sidebar: configuration ────────────────────────────────────────────────────

st.sidebar.header("Simulation Config")

dataset = st.sidebar.selectbox(
    "Dataset",
    ["saltysnack_beverages_small", "saltysnack_beverages"],
)
DATA_DIR = SAMPLE_DATA_ROOT / dataset

start_date = st.sidebar.date_input("Start Date", value=date(2024, 1, 1))
end_date   = st.sidebar.date_input("End Date",   value=date(2024, 12, 31))

replenishment_policy = st.sidebar.selectbox(
    "Replenishment Policy",
    ["trailing_avg_28d", "promo_aware_7d", "baseline_only"],
)

import re as _re
_policy_day_match = _re.search(r"(\d+)d", replenishment_policy)
_default_smoothing = int(_policy_day_match.group(1)) if _policy_day_match else 28
smoothing_days = st.sidebar.number_input("Demand Smoothing Window (days)", min_value=7, max_value=90, value=_default_smoothing)

st.sidebar.subheader("Store Config")
store_reorder_weeks  = st.sidebar.slider("Min Inventory Trigger (weeks of cover)", 1, 4, 2, 1)
store_target_weeks   = st.sidebar.slider("Store Target Stock (weeks)",           1, 8, 3, 1)
store_start_days     = st.sidebar.number_input("Store Starting Stock (days)", min_value=1, max_value=60, value=14)
store_order_dow      = st.sidebar.selectbox("Store Order Day", ["MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY"], index=0)

st.sidebar.subheader("DC Config")
dc_reorder_weeks     = st.sidebar.slider("DC Reorder Point (weeks of cover)", 1, 6, 2, 1)
dc_target_weeks      = st.sidebar.slider("DC Target Stock (weeks)",           2, 12, 5, 1)
dc_start_days        = st.sidebar.number_input("DC Starting Stock (days)", min_value=1, max_value=90, value=30)
dc_review_dow        = st.sidebar.selectbox("DC Review Day", ["MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY"], index=0)

st.sidebar.subheader("Supplier Config")
sup_lead_min   = st.sidebar.number_input("Lead Time Min (days)", min_value=1, max_value=14, value=3)
sup_lead_max   = st.sidebar.number_input("Lead Time Max (days)", min_value=1, max_value=30, value=7)
sup_on_time    = st.sidebar.slider("Supplier On-Time Rate", 0.5, 1.0, 0.90, 0.05)
sup_partial    = st.sidebar.slider("Supplier Partial Delivery Rate", 0.0, 0.5, 0.10, 0.05)

st.sidebar.subheader("DC → Store Config")
dc_store_lead_days = st.sidebar.number_input("DC → Store Lead Time (days)", min_value=1, max_value=14, value=2)
dc_on_time   = st.sidebar.slider("DC On-Time Rate", 0.5, 1.0, 0.95, 0.05)
dc_partial   = st.sidebar.slider("DC Partial Delivery Rate", 0.0, 0.3, 0.05, 0.05)

seed = st.sidebar.number_input("Random Seed", min_value=0, value=42)

run_btn = st.sidebar.button("▶ Run Simulation", type="primary")

# ── Load static data ──────────────────────────────────────────────────────────

@st.cache_data
def load_static(data_dir_str):
    d = Path(data_dir_str)
    items_df        = pd.read_csv(d / "items.csv",            dtype=str)
    stores_df       = pd.read_csv(d / "stores.csv",           dtype=str)
    dcs_df          = pd.read_csv(d / "dcs.csv",              dtype=str)
    suppliers_df    = pd.read_csv(d / "suppliers.csv",        dtype=str)
    supplier_items  = pd.read_csv(d / "supplier_items.csv",   dtype=str)
    store_mappings  = pd.read_csv(d / "store_mappings.csv",   dtype=str)
    dc_mappings     = pd.read_csv(d / "dc_mappings.csv",      dtype=str)
    promos_df       = pd.read_csv(d / "promos.csv",           dtype=str)
    promo_groups    = pd.read_csv(d / "promo_groups.csv",     dtype=str)
    promo_group_items = pd.read_csv(d / "promo_group_items.csv", dtype=str)
    promo_stores    = pd.read_csv(d / "promo_stores.csv",     dtype=str)
    return (items_df, stores_df, dcs_df, suppliers_df, supplier_items,
            store_mappings, dc_mappings, promos_df, promo_groups,
            promo_group_items, promo_stores)

(items_df, stores_df, dcs_df, suppliers_df, supplier_items_df,
 store_mappings_df, dc_mappings_df, promos_df, promo_groups_df,
 promo_group_items_df, promo_stores_df) = load_static(str(DATA_DIR))

# ── Summary of loaded data ────────────────────────────────────────────────────

col1, col2, col3, col4 = st.columns(4)
col1.metric("Items",     len(items_df))
col2.metric("Stores",    len(stores_df))
col3.metric("DCs",       len(dcs_df))
col4.metric("Suppliers", len(suppliers_df))

# ── Simulation engine ─────────────────────────────────────────────────────────

def run_simulation(
    items_df, stores_df, dcs_df, supplier_items_df,
    store_mappings_df, dc_mappings_df,
    promos_df, promo_group_items_df, promo_stores_df,
    start_date, end_date,
    policy, smoothing_days,
    store_reorder_weeks, store_target_weeks, store_start_days, store_order_dow,
    dc_reorder_weeks, dc_target_weeks, dc_start_days, dc_review_dow,
    sup_lead_min, sup_lead_max, sup_on_time, sup_partial,
    dc_on_time, dc_partial, dc_store_lead_days,
    seed,
):
    rng = np.random.default_rng(int(seed))

    # -- Network ----------------------------------------------------------------
    STORES = sorted(stores_df['store_id'].tolist())
    DCS    = sorted(dcs_df['dc_id'].tolist())

    # store → dc
    STORE_DC = dict(zip(store_mappings_df['from_store_id'], store_mappings_df['to_dc_id']))

    # dc → [stores]
    DC_ASSIGN = defaultdict(list)
    for s in STORES:
        DC_ASSIGN[STORE_DC[s]].append(s)

    # dc → supplier  (take first supplier per DC from dc_mappings)
    dc_sup_rows = dc_mappings_df[dc_mappings_df['mapping_type'] == 'DC_SUPPLIER']
    DC_SUPPLIER = {}
    for _, row in dc_sup_rows.iterrows():
        dc = row['from_dc_id']
        if dc not in DC_SUPPLIER:
            DC_SUPPLIER[dc] = row['to_node']

    ITEMS = sorted(items_df['item_id'].tolist())
    ITEM_LINE = {item: i+1 for i, item in enumerate(ITEMS)}

    items_df2 = items_df.set_index('item_id')
    velocity_map   = items_df2['velocity_class'].to_dict()
    category_map   = items_df2['category'].to_dict()
    subcategory_map= items_df2['subcategory'].to_dict()
    lifecycle_map  = items_df2['lifecycle_profile'].to_dict()
    unit_price_map = {k: float(v) for k, v in items_df2['unit_price'].items()}
    unit_cost_map  = {k: float(v) for k, v in items_df2['unit_cost'].items()}
    case_pack_map  = {k: int(v)   for k, v in items_df2['case_pack_size'].items()}

    # -- Dates -----------------------------------------------------------------
    dates = []
    d = start_date
    while d <= end_date:
        dates.append(d)
        d += timedelta(days=1)
    n_days   = len(dates)
    n_stores = len(STORES)
    n_items  = len(ITEMS)
    store_idx = {s: i for i, s in enumerate(STORES)}
    item_idx  = {it: i for i, it in enumerate(ITEMS)}

    # -- Velocity baseline -----------------------------------------------------
    _VEL = {
        'Salty Snacks_FAST': 12, 'Salty Snacks_MEDIUM': 7, 'Salty Snacks_SLOW': 3,
        'Carbonated Soft Drinks_12 Pack_FAST': 20, 'Carbonated Soft Drinks_12 Pack_MEDIUM': 12,
        'Carbonated Soft Drinks_2 LTR_FAST': 16,   'Carbonated Soft Drinks_2 LTR_MEDIUM': 10,
        'default_FAST': 10, 'default_MEDIUM': 5, 'default_SLOW': 2,
    }
    def avg_daily_vel(item):
        cat, sub, vc = category_map[item], subcategory_map[item], str(velocity_map[item]).upper()
        return float(_VEL.get(f"{cat}_{sub}_{vc}") or _VEL.get(f"{cat}_{vc}") or _VEL.get(f"default_{vc}", 5))

    baseline = np.zeros((n_stores, n_items), dtype=np.float64)
    for ii, item in enumerate(ITEMS):
        baseline[:, ii] = avg_daily_vel(item)

    # -- Lifecycle -------------------------------------------------------------
    lifecycle_arr = np.ones((n_items, n_days), dtype=np.float64)
    day_idx = np.arange(n_days, dtype=np.float64)
    for ii, item in enumerate(ITEMS):
        lc = lifecycle_map.get(item, 'steady')
        if lc == 'growth':
            ramp_end = min(90, n_days)
            lifecycle_arr[ii, :ramp_end] = 0.3 + 0.7 * (day_idx[:ramp_end] / 90.0)
        elif lc == 'decay':
            ds = max(0, n_days - 90)
            nd = n_days - ds
            if nd > 0:
                lifecycle_arr[ii, ds:] = 1.0 + (0.3 - 1.0) * (np.arange(nd) / max(nd-1, 1))

    # -- Seasonality -----------------------------------------------------------
    _SEA = {}
    for _wr, _m in [
        (range(1,5),0.85),(range(5,9),0.88),(range(9,14),0.95),(range(14,18),1.00),
        (range(18,23),1.05),(range(23,27),1.08),(range(27,31),1.05),(range(31,36),1.00),
        (range(36,40),0.95),(range(40,45),1.10),(range(45,49),1.20),(range(49,53),1.35),
    ]:
        for w in _wr: _SEA[w] = _m
    seasonal_arr = np.array([_SEA.get(d.isocalendar().week, 1.0) for d in dates], dtype=np.float64)

    # -- Noise -----------------------------------------------------------------
    noise_arr = np.zeros((n_stores, n_items, n_days), dtype=np.float64)
    for si in range(n_stores):
        for ii in range(n_items):
            for di in range(n_days):
                local_rng = np.random.default_rng(int(seed) + ii*1000 + si*100 + di)
                noise_arr[si, ii, di] = local_rng.lognormal(0.0, 0.15)

    # -- Promo multiplier ------------------------------------------------------
    promo_arr = np.ones((n_stores, n_items, n_days), dtype=np.float64)
    date_to_idx = {d: i for i, d in enumerate(dates)}

    # Join promos → promo_group_items → promo_stores
    promos_merged = promos_df.merge(promo_groups_df[['promo_group_name']], on='promo_group_name', how='left')

    for _, promo in promos_merged.iterrows():
        pg_name  = promo['promo_group_name']
        p_items  = promo_group_items_df[promo_group_items_df['promo_group_name'] == pg_name]['item_id'].tolist()
        p_stores = promo_stores_df[promo_stores_df['promo_name'] == promo['promo_name']]['store_id'].tolist()

        mult       = float(promo['demand_multiplier'])
        decay_days = int(promo['post_promo_decay_days'])
        decay_shape= promo['post_promo_decay_shape']
        p_start    = date.fromisoformat(str(promo['start_date'])[:10])
        p_end      = date.fromisoformat(str(promo['end_date'])[:10])

        # Shift promo into the simulation year so dates overlap
        year_delta = start_date.year - p_start.year
        if year_delta != 0:
            try:
                p_start = p_start.replace(year=p_start.year + year_delta)
                p_end   = p_end.replace(year=p_end.year + year_delta)
            except ValueError:
                # Feb 29 edge case — skip to Mar 1
                p_start = p_start.replace(month=3, day=1, year=p_start.year + year_delta)
                p_end   = p_end.replace(month=3, day=1, year=p_end.year + year_delta)

        for store in p_stores:
            if store not in store_idx: continue
            si = store_idx[store]
            for item_id in p_items:
                if item_id not in item_idx: continue
                ii = item_idx[item_id]
                cur = p_start
                while cur <= p_end:
                    if cur in date_to_idx:
                        promo_arr[si, ii, date_to_idx[cur]] = mult
                    cur += timedelta(days=1)
                if decay_shape == 'LINEAR' and decay_days > 0:
                    for k in range(1, decay_days+1):
                        dd = p_end + timedelta(days=k)
                        if dd in date_to_idx:
                            di = date_to_idx[dd]
                            promo_arr[si, ii, di] = max(1.0, mult * (1.0 - k/decay_days))

    # -- Combine demand --------------------------------------------------------
    raw = (baseline[:, :, np.newaxis]
           * lifecycle_arr[np.newaxis, :, :]
           * seasonal_arr[np.newaxis, np.newaxis, :]
           * noise_arr
           * promo_arr)
    demand_arr = np.maximum(0, np.round(raw)).astype(np.int64)

    # -- Build demand + promo lookup dicts -------------------------------------
    dm_idx     = {}
    promo_idx  = {}
    promo_flag = (promo_arr > 1.0)
    for si, store in enumerate(STORES):
        for ii, item in enumerate(ITEMS):
            for di, d in enumerate(dates):
                dm_idx[(store, item, d)]    = int(demand_arr[si, ii, di])
                promo_idx[(store, item, d)] = bool(promo_flag[si, ii, di])

    # -- Demand matrix CSV columns --------------------------------------------
    store_col = np.repeat(STORES, n_items * n_days)
    item_col  = np.tile(np.repeat(ITEMS, n_days), n_stores)
    date_col  = np.tile(dates, n_stores * n_items)
    qty_col   = demand_arr.reshape(-1)
    is_promo  = (promo_arr > 1.0).reshape(-1)

    demand_df = pd.DataFrame({
        'store_id':      store_col,
        'item_id':       item_col,
        'date':          date_col,
        'demand_qty':    qty_col,
        'is_promo':      is_promo,
        'week':          [f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}" for d in date_col],
    })

    # -- Baseline for starting stock ------------------------------------------
    first_n = min(28, n_days)
    baseline_demand = {}
    for store in STORES:
        baseline_demand[store] = {}
        for item in ITEMS:
            vals = [dm_idx.get((store, item, dates[i]), 0) for i in range(first_n)]
            baseline_demand[store][item] = float(np.mean(vals)) if vals else avg_daily_vel(item)

    # -- DAY map ---------------------------------------------------------------
    DAY_MAP = {'MONDAY':0,'TUESDAY':1,'WEDNESDAY':2,'THURSDAY':3,'FRIDAY':4,'SATURDAY':5,'SUNDAY':6}
    STORE_ORDER_DOW = DAY_MAP[store_order_dow.upper()]
    DC_REVIEW_DOW   = DAY_MAP[dc_review_dow.upper()]

    # -- State init -----------------------------------------------------------
    on_hand  = defaultdict(lambda: defaultdict(int))
    on_order = defaultdict(lambda: defaultdict(int))

    demand_history = defaultdict(lambda: defaultdict(list))

    def get_avg_daily(store, item):
        if policy == 'trailing_avg_28d':
            hist = demand_history[store][item]
            return float(np.mean(hist)) if hist else baseline_demand[store][item]
        return baseline_demand[store][item]

    # DC starting stock
    for dc in DCS:
        dc_stores = DC_ASSIGN[dc]
        for item in ITEMS:
            dc_avg = sum(baseline_demand.get(s, {}).get(item, 0) for s in dc_stores)
            on_hand[dc][item] = int(round(dc_avg * dc_start_days))

    # Store starting stock
    for store in STORES:
        for item in ITEMS:
            on_hand[store][item] = int(round(baseline_demand[store][item] * store_start_days))

    # -- Buffers ---------------------------------------------------------------
    receipt_schedule       = []   # supplier → DC
    store_receipt_schedule = []   # DC → store

    supplier_receipts_buf  = []
    store_receipts_buf     = []
    supplier_orders_buf    = []
    supplier_od_buf        = []
    store_orders_buf       = []
    store_od_buf           = []
    daily_sales_buf        = []
    daily_inv_buf          = []
    weekly_sales           = defaultdict(lambda: defaultdict(float))

    po_seq = so_seq = rec_seq = 0

    def iso_week(d):
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    # -- Daily loop -----------------------------------------------------------
    progress = st.progress(0, text="Running simulation…")
    total_days = len(dates)

    for day_num, current_date in enumerate(dates):
        if day_num % max(1, total_days // 100) == 0:
            progress.progress(day_num / total_days, text=f"Simulating {current_date}…")

        dow       = current_date.weekday()
        is_sunday = (dow == 6)
        week_str  = iso_week(current_date)

        # Step 1: Supplier → DC receipts
        still = []
        for entry in receipt_schedule:
            if entry['scheduled_date'] != current_date:
                still.append(entry); continue
            dc_id, item_id, qty, po_num = entry['dc_id'], entry['item_id'], entry['qty'], entry['po_number']
            is_late = entry.get('is_late', False)
            already_partial = entry.get('already_partial', False)

            if not is_late and rng.random() > sup_on_time:
                extra = int(rng.integers(1, 4))
                entry['scheduled_date'] = current_date + timedelta(days=extra)
                entry['is_late'] = True
                still.append(entry); continue

            if not already_partial and rng.random() < sup_partial:
                frac     = rng.uniform(0.5, 0.8)
                received = int(math.floor(qty * frac))
                remainder= qty - received
                on_hand[dc_id][item_id]  += received
                on_order[dc_id][item_id]  = max(0, on_order[dc_id][item_id] - received)
                rec_seq += 1
                supplier_receipts_buf.append({
                    'receipt_id': f'REC_{rec_seq:06d}', 'dc_id': dc_id, 'item_id': item_id,
                    'receipt_date': current_date, 'received_qty': float(received),
                    'receipt_type': 'SUPPLIER_DELIVERY', 'is_late': is_late, 'is_partial': True, 'po_number': po_num,
                })
                still.append({'dc_id':dc_id,'item_id':item_id,'po_number':po_num,'qty':remainder,
                               'scheduled_date':current_date+timedelta(days=3),
                               'is_late':is_late,'already_partial':True})
            else:
                on_hand[dc_id][item_id]  += qty
                on_order[dc_id][item_id]  = max(0, on_order[dc_id][item_id] - qty)
                rec_seq += 1
                supplier_receipts_buf.append({
                    'receipt_id': f'REC_{rec_seq:06d}', 'dc_id': dc_id, 'item_id': item_id,
                    'receipt_date': current_date, 'received_qty': float(qty),
                    'receipt_type': 'SUPPLIER_DELIVERY', 'is_late': is_late, 'is_partial': False, 'po_number': po_num,
                })
        receipt_schedule = still

        # Step 2: DC → Store receipts
        still_sr = []
        for entry in store_receipt_schedule:
            if entry['scheduled_date'] != current_date:
                still_sr.append(entry); continue
            store_id, item_id, qty, so_num = entry['store_id'], entry['item_id'], entry['qty'], entry['so_number']
            order_qty = entry.get('order_qty', float(qty))
            is_late = entry.get('is_late', False)
            already_partial = entry.get('already_partial', False)

            if not is_late and rng.random() > dc_on_time:
                extra = int(rng.integers(1, 3))
                entry['scheduled_date'] = current_date + timedelta(days=extra)
                entry['is_late'] = True
                still_sr.append(entry); continue

            if not already_partial and rng.random() < dc_partial:
                frac     = rng.uniform(0.6, 0.85)
                received = int(math.floor(qty * frac))
                remainder= qty - received
                on_hand[store_id][item_id]  += received
                on_order[store_id][item_id]  = max(0, on_order[store_id][item_id] - received)
                store_receipts_buf.append({
                    'store_id': store_id, 'item_id': item_id, 'so_number': so_num,
                    'delivery_date': current_date, 'delivered_qty': float(received),
                    'unfilled_qty': float(remainder), 'delivery_status': 'PARTIAL_DELIVERY',
                    'order_qty': order_qty,
                })
                still_sr.append({'store_id':store_id,'item_id':item_id,'so_number':so_num,'qty':remainder,
                                  'order_qty': order_qty,
                                  'scheduled_date':current_date+timedelta(days=2),
                                  'is_late':is_late,'already_partial':True})
            else:
                on_hand[store_id][item_id]  += qty
                on_order[store_id][item_id]  = max(0, on_order[store_id][item_id] - qty)
                store_receipts_buf.append({
                    'store_id': store_id, 'item_id': item_id, 'so_number': so_num,
                    'delivery_date': current_date, 'delivered_qty': float(qty),
                    'unfilled_qty': 0.0, 'delivery_status': 'FULL_DELIVERY',
                    'order_qty': order_qty,
                })
        store_receipt_schedule = still_sr

        # Step 3: Sell & track inventory
        for store in STORES:
            for item in ITEMS:
                req  = dm_idx.get((store, item, current_date), 0)
                hist = demand_history[store][item]
                hist.append(req)
                if len(hist) > smoothing_days:
                    hist.pop(0)

                oh   = on_hand[store][item]
                sold = min(req, oh)
                on_hand[store][item] = max(0, oh - sold)
                weekly_sales[store][item] += sold

                avg_d  = get_avg_daily(store, item)
                woc    = (on_hand[store][item] / (avg_d * 7.0)) if avg_d > 0 else 999
                status = 'ZERO' if on_hand[store][item] == 0 else ('LOW' if woc < store_reorder_weeks else 'AVAILABLE')

                daily_sales_buf.append({
                    'store_id': store, 'item_id': item, 'date': current_date,
                    'week': week_str, 'demand_qty': float(req),
                    'sales_qty': float(sold), 'lost_sales_qty': float(req - sold),
                    'sales_amount': float(sold) * unit_price_map.get(item, 0.0),
                })
                daily_inv_buf.append({
                    'store_id': store, 'item_id': item, 'date': current_date,
                    'week': week_str,
                    'on_hand_qty': float(on_hand[store][item]),
                    'on_order_qty': float(on_order[store][item]),
                    'inventory_status': status,
                    'woc': round(woc, 2) if woc != 999 else None,
                })

        # DC inventory snapshot (end-of-day, after fulfilling store orders)
        for dc in DCS:
            for item in ITEMS:
                dc_oh = float(on_hand[dc][item])
                dc_oo = float(on_order[dc][item])
                daily_inv_buf.append({
                    'store_id': dc, 'item_id': item, 'date': current_date,
                    'week': week_str,
                    'on_hand_qty': dc_oh,
                    'on_order_qty': dc_oo,
                    'inventory_status': 'ZERO' if dc_oh == 0 else 'AVAILABLE',
                    'woc': None,
                })

        # Step 4: Store orders
        def _place_store_order(store, items_to_order, order_type='STANDARD'):
            nonlocal so_seq
            if not items_to_order:
                return
            so_seq += 1
            dc_id  = STORE_DC[store]
            so_num = f"SO_{current_date.strftime('%Y%m%d')}_{store}_{so_seq:04d}"
            store_orders_buf.append({'so_number': so_num, 'store_id': store, 'dc_id': dc_id,
                                     'order_date': current_date, 'week': week_str,
                                     'order_type': order_type})
            for item, qty in items_to_order:
                store_od_buf.append({'so_number': so_num, 'store_id': store, 'item_id': item,
                                     'order_qty': float(qty), 'order_date': current_date,
                                     'order_type': order_type})
                avail = on_hand[dc_id][item]
                ship  = min(qty, avail)
                on_hand[dc_id][item] = max(0, avail - ship)
                if ship > 0:
                    on_order[store][item] += ship
                    store_receipt_schedule.append({
                        'store_id': store, 'item_id': item, 'so_number': so_num,
                        'qty': ship, 'order_qty': float(qty),
                        'scheduled_date': current_date + timedelta(days=int(dc_store_lead_days)),
                        'is_late': False, 'already_partial': False,
                    })

        if policy == 'promo_aware_7d':
            # --- Emergency restock: runs every day, not just order cycle day ---
            for store in STORES:
                for item in ITEMS:
                    if promo_idx.get((store, item, current_date), False) and on_hand[store][item] == 0:
                        next_7 = [current_date + timedelta(days=i) for i in range(7)]
                        remaining = sum(dm_idx.get((store, item, d), 0) for d in next_7)
                        emerg_qty = max(0, int(round(remaining - on_hand[store][item])))
                        if emerg_qty > 0:
                            _place_store_order(store, [(item, emerg_qty)], order_type='EMERGENCY')

        if dow == STORE_ORDER_DOW:
            for store in STORES:
                items_to_order = []

                if policy == 'promo_aware_7d':
                    for item in ITEMS:
                        next_7 = [current_date + timedelta(days=i) for i in range(1, 8)]
                        has_promo = any(promo_idx.get((store, item, d), False) for d in next_7)
                        if has_promo:
                            promo_demand = sum(dm_idx.get((store, item, d), 0) for d in next_7)
                            qty = max(0, int(round(promo_demand - on_hand[store][item])))
                            if qty > 0:
                                items_to_order.append((item, qty, 'PROMO'))
                        else:
                            avg_d = get_avg_daily(store, item)
                            reorder_pt = store_reorder_weeks * avg_d * 7
                            if on_hand[store][item] >= reorder_pt:
                                continue
                            target = store_target_weeks * avg_d * 7
                            qty = max(0, int(round(target - on_hand[store][item])))
                            if qty > 0:
                                items_to_order.append((item, qty, 'STANDARD'))
                    # Split by order_type and place separately so each order header is tagged
                    for otype in ('PROMO', 'STANDARD'):
                        batch = [(item, qty) for item, qty, t in items_to_order if t == otype]
                        _place_store_order(store, batch, order_type=otype)

                else:  # trailing_avg_28d / baseline_only
                    for item in ITEMS:
                        avg_d = get_avg_daily(store, item)
                        reorder_pt = store_reorder_weeks * avg_d * 7
                        if on_hand[store][item] >= reorder_pt:
                            continue
                        target = store_target_weeks * avg_d * 7
                        qty = max(0, int(round(target - on_hand[store][item])))
                        if qty > 0:
                            items_to_order.append((item, qty))
                    _place_store_order(store, items_to_order, order_type='STANDARD')

        # Step 5: DC raises supplier POs
        if dow == DC_REVIEW_DOW:
            for dc in DCS:
                supplier = DC_SUPPLIER.get(dc)
                if not supplier:
                    continue
                for item in ITEMS:
                    dc_avg = sum(get_avg_daily(s, item) for s in DC_ASSIGN[dc])
                    reorder_pt = dc_reorder_weeks * dc_avg * 7
                    pos = on_hand[dc][item] + on_order[dc][item]
                    if pos >= reorder_pt:
                        continue
                    target   = dc_target_weeks * dc_avg * 7
                    raw_ord  = max(0, int(round(target - pos)))
                    if raw_ord == 0:
                        continue
                    cp       = case_pack_map.get(item, 1)
                    order_qty= math.ceil(raw_ord / cp) * cp
                    po_seq  += 1
                    po_num   = f"PO_{current_date.strftime('%Y%m%d')}_{dc}_{po_seq:04d}"
                    lead     = int(rng.integers(sup_lead_min, sup_lead_max + 1))
                    exp_date = current_date + timedelta(days=lead)
                    supplier_orders_buf.append({'po_number': po_num, 'dc_id': dc, 'supplier_id': supplier,
                                                 'order_date': current_date, 'expected_date': exp_date})
                    supplier_od_buf.append({'po_number': po_num, 'dc_id': dc, 'item_id': item,
                                             'supplier_id': supplier, 'order_qty': float(order_qty),
                                             'need_qty': float(raw_ord), 'unit_cost': unit_cost_map.get(item, 0.0)})
                    receipt_schedule.append({'dc_id': dc, 'item_id': item, 'po_number': po_num,
                                              'qty': order_qty, 'scheduled_date': exp_date,
                                              'is_late': False, 'already_partial': False})
                    on_order[dc][item] += order_qty

    progress.progress(1.0, text="Simulation complete!")

    # -- Assemble output dataframes -------------------------------------------
    sales_df        = pd.DataFrame(daily_sales_buf)
    inv_df          = pd.DataFrame(daily_inv_buf)
    sup_rec_df      = pd.DataFrame(supplier_receipts_buf) if supplier_receipts_buf else pd.DataFrame()
    str_rec_df      = pd.DataFrame(store_receipts_buf)    if store_receipts_buf    else pd.DataFrame()
    sup_orders_df   = pd.DataFrame(supplier_orders_buf)   if supplier_orders_buf   else pd.DataFrame()
    supplier_od_df  = pd.DataFrame(supplier_od_buf)       if supplier_od_buf       else pd.DataFrame()
    str_orders_df   = pd.DataFrame(store_orders_buf)      if store_orders_buf      else pd.DataFrame()
    store_od_df     = pd.DataFrame(store_od_buf)          if store_od_buf          else pd.DataFrame()

    return (demand_df, sales_df, inv_df, sup_rec_df, str_rec_df,
            sup_orders_df, supplier_od_df, str_orders_df, store_od_df, dict(STORE_DC))


# ── Run ───────────────────────────────────────────────────────────────────────

if run_btn:
    if start_date >= end_date:
        st.error("Start date must be before end date.")
        st.stop()

    _t0 = time.monotonic()
    with st.spinner("Running simulation…"):
        (demand_df, sales_df, inv_df,
         sup_rec_df, str_rec_df,
         sup_orders_df, supplier_od_df,
         str_orders_df, store_od_df, store_dc_map) = run_simulation(
            items_df, stores_df, dcs_df, supplier_items_df,
            store_mappings_df, dc_mappings_df,
            promos_df, promo_group_items_df, promo_stores_df,
            start_date=start_date, end_date=end_date,
            policy=replenishment_policy, smoothing_days=int(smoothing_days),
            store_reorder_weeks=store_reorder_weeks, store_target_weeks=store_target_weeks,
            store_start_days=int(store_start_days),
            store_order_dow=store_order_dow,
            dc_reorder_weeks=dc_reorder_weeks, dc_target_weeks=dc_target_weeks,
            dc_start_days=int(dc_start_days),
            dc_review_dow=dc_review_dow,
            sup_lead_min=int(sup_lead_min), sup_lead_max=int(sup_lead_max),
            sup_on_time=sup_on_time, sup_partial=sup_partial,
            dc_on_time=dc_on_time, dc_partial=dc_partial,
            dc_store_lead_days=int(dc_store_lead_days),
            seed=seed,
        )

    # -- Save to output/ ------------------------------------------------------
    _elapsed = time.monotonic() - _t0
    run_id   = f"debug_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}"
    out_dir  = OUTPUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    demand_df.to_csv(out_dir / "demand_matrix.csv",      index=False)
    sales_df.to_csv( out_dir / "store_sales_daily.csv",  index=False)
    inv_df.to_csv(   out_dir / "store_inventory_daily.csv", index=False)
    if not sup_rec_df.empty:   sup_rec_df.to_csv(   out_dir / "supplier_receipts.csv",  index=False)
    if not str_rec_df.empty:   str_rec_df.to_csv(   out_dir / "store_receipts.csv",     index=False)
    if not sup_orders_df.empty:sup_orders_df.to_csv(out_dir / "supplier_orders.csv",    index=False)
    if not str_orders_df.empty:str_orders_df.to_csv(out_dir / "store_orders.csv",       index=False)

    st.success(f"Outputs saved to `output/{run_id}/`")
    st.session_state['sim_results'] = dict(
        demand_df=demand_df, sales_df=sales_df, inv_df=inv_df,
        sup_rec_df=sup_rec_df, str_rec_df=str_rec_df,
        sup_orders_df=sup_orders_df, supplier_od_df=supplier_od_df,
        str_orders_df=str_orders_df, store_od_df=store_od_df,
        store_dc_map=store_dc_map,
    )
    st.session_state['run_id']      = run_id
    st.session_state['run_elapsed'] = _elapsed
    st.session_state['run_config']  = {
        'seed':        int(seed),
        'start_date':  start_date.isoformat(),
        'end_date':    end_date.isoformat(),
        'store_count': len(stores_df),
        'item_count':  len(items_df),
        'dc_count':    len(dcs_df),
    }

if 'sim_results' in st.session_state:
    _r = st.session_state['sim_results']
    demand_df      = _r['demand_df']
    sales_df       = _r['sales_df']
    inv_df         = _r['inv_df']
    sup_rec_df     = _r['sup_rec_df']
    str_rec_df     = _r['str_rec_df']
    sup_orders_df  = _r['sup_orders_df']
    supplier_od_df = _r['supplier_od_df']
    str_orders_df  = _r['str_orders_df']
    store_od_df    = _r['store_od_df']
    store_dc_map   = _r['store_dc_map']

    # ── Store / Item selectors (drives everything below) ─────────────────────
    st.divider()
    stores_list = sorted(sales_df['store_id'].unique().tolist())
    items_list  = sorted(sales_df['item_id'].unique().tolist())
    item_desc   = items_df.set_index('item_id')['item_description'].to_dict()
    items_display = {f"{iid} — {item_desc.get(iid, iid)}": iid for iid in items_list}

    col_sel1, col_sel2 = st.columns(2)
    sel_store      = col_sel1.selectbox("Store", stores_list)
    sel_item_label = col_sel2.selectbox("Item", list(items_display.keys()))
    sel_item       = items_display[sel_item_label]

    # All filtered views
    s_inv   = inv_df[(inv_df['store_id'] == sel_store) & (inv_df['item_id'] == sel_item)].copy()
    s_sales = sales_df[(sales_df['store_id'] == sel_store) & (sales_df['item_id'] == sel_item)].copy()
    s_demand= demand_df[(demand_df['store_id'] == sel_store) & (demand_df['item_id'] == sel_item)].copy()

    s_inv['date']   = pd.to_datetime(s_inv['date'])
    s_sales['date'] = pd.to_datetime(s_sales['date'])

    # ── Min Inventory Trigger info — policy-aware ────────────────────────────
    # Baseline avg: mean of first smoothing_days days of demand (matches simulation init)
    first_n      = min(int(smoothing_days), len(s_demand))
    baseline_avg = float(s_demand['demand_qty'].iloc[:first_n].mean()) if first_n > 0 else 0.0
    overall_avg  = float(s_sales['demand_qty'].mean()) if not s_sales.empty else 0.0

    if replenishment_policy == 'baseline_only':
        trigger_units = int(round(store_reorder_weeks * baseline_avg * 7))
        target_units  = int(round(store_target_weeks  * baseline_avg * 7))
        st.caption(
            f"**Policy: baseline_only** — trigger is static throughout the simulation. "
            f"Min Inventory Trigger = **{store_reorder_weeks} wks × {baseline_avg:.1f} units/day × 7 = {trigger_units} units**. "
            f"Target Stock = **{store_target_weeks} wks × {baseline_avg:.1f} units/day × 7 = {target_units} units**."
        )

    elif replenishment_policy == 'trailing_avg_28d':
        trigger_early = int(round(store_reorder_weeks * baseline_avg * 7))
        target_early  = int(round(store_target_weeks  * baseline_avg * 7))
        trigger_later = int(round(store_reorder_weeks * overall_avg  * 7))
        target_later  = int(round(store_target_weeks  * overall_avg  * 7))
        st.caption(
            f"**Policy: trailing_{smoothing_days}d avg** — trigger **evolves** as demand history builds. "
            f"Early simulation (first {smoothing_days} days): ~**{trigger_early} units** trigger / **{target_early} units** target "
            f"(avg {baseline_avg:.1f}/day). "
            f"Full-year avg: ~**{trigger_later} units** trigger / **{target_later} units** target "
            f"(avg {overall_avg:.1f}/day). Formula: weeks × rolling_avg_daily × 7."
        )

    elif replenishment_policy == 'promo_aware_7d':
        trigger_std = int(round(store_reorder_weeks * baseline_avg * 7))
        target_std  = int(round(store_target_weeks  * baseline_avg * 7))
        st.caption(
            f"**Policy: promo_aware_7d** — two different triggers apply. "
            f"**Standard items**: Min Trigger ~**{trigger_std} units** / Target ~**{target_std} units** "
            f"(trailing {smoothing_days}-day avg, {baseline_avg:.1f} units/day). "
            f"**Promo items**: weeks-of-cover formula is bypassed — orders cover the full next 7 days of promo demand. "
            f"Emergency restock fires daily if stock = 0 during an active promo."
        )

    # ── KPI summary (filtered to selected store + item) ───────────────────────
    st.subheader(f"KPI — {sel_store} / {sel_item_label}")
    total_demand  = s_sales['demand_qty'].sum()
    total_sales   = s_sales['sales_qty'].sum()
    total_lost    = s_sales['lost_sales_qty'].sum()
    fill_rate     = total_sales / total_demand * 100 if total_demand > 0 else 0
    stockout_days = (s_inv['inventory_status'] == 'ZERO').sum()
    total_revenue = s_sales['sales_amount'].sum()

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Total Demand",   f"{total_demand:,.0f}")
    k2.metric("Total Sales",    f"{total_sales:,.0f}")
    k3.metric("Lost Sales",     f"{total_lost:,.0f}")
    k4.metric("Fill Rate",      f"{fill_rate:.1f}%")
    k5.metric("Stockout Days",  f"{stockout_days:,}")
    k6.metric("Revenue",        f"${total_revenue:,.0f}")

    # ── Compute promo spans from s_demand ────────────────────────────────────
    # Contiguous date ranges where is_promo == True
    promo_date_ranges = []
    if 'is_promo' in s_demand.columns and s_demand['is_promo'].any():
        sd = s_demand.sort_values('date').copy()
        sd['date'] = pd.to_datetime(sd['date'])
        in_promo = False
        span_start = None
        for _, row in sd.iterrows():
            if row['is_promo'] and not in_promo:
                span_start = row['date']
                in_promo = True
            elif not row['is_promo'] and in_promo:
                promo_date_ranges.append((span_start, row['date'] - pd.Timedelta(days=1)))
                in_promo = False
        if in_promo:
            promo_date_ranges.append((span_start, sd['date'].iloc[-1]))

    # Promo weeks set
    promo_weeks = set()
    if 'is_promo' in s_demand.columns:
        promo_weeks = set(s_demand[s_demand['is_promo'] == True]['week'].unique())

    def add_promo_shading_daily(fig):
        added_legend = False
        for x0, x1 in promo_date_ranges:
            fig.add_vrect(
                x0=x0, x1=x1 + pd.Timedelta(days=1),
                fillcolor='rgba(255, 180, 0, 0.18)',
                layer='below', line_width=0,
                name='Promo Period' if not added_legend else None,
                showlegend=not added_legend,
                legendgroup='promo',
            )
            added_legend = True

    def add_promo_shading_weekly(fig, weeks_list):
        added_legend = False
        for i, w in enumerate(weeks_list):
            if w in promo_weeks:
                fig.add_vrect(
                    x0=i - 0.5, x1=i + 0.5,
                    fillcolor='rgba(255, 180, 0, 0.22)',
                    layer='below', line_width=0,
                    name='Promo Week' if not added_legend else None,
                    showlegend=not added_legend,
                    legendgroup='promo_w',
                )
                added_legend = True

    # ── Chart 1: Daily — Demand bar + Sales bar + On-hand line ───────────────
    st.divider()
    st.subheader("Inventory & Sales Charts")
    st.markdown("#### Daily: Demand vs Sales vs Inventory")
    fig_daily = go.Figure()

    s_daily = s_sales.merge(s_inv[['date', 'on_hand_qty']], on='date', how='left')
    lost_colors = ['#db5546' if oh == 0 else '#F1948A' for oh in s_daily['on_hand_qty']]
    day_labels = s_daily['date'].dt.strftime('%a, %b %d %Y')
    inv_day_labels = s_inv['date'].dt.strftime('%a, %b %d %Y')

    fig_daily.add_trace(go.Bar(
        x=s_daily['date'], y=s_daily['demand_qty'],
        name='Demand', marker_color='#BAD7F2', opacity=0.85,
        hovertemplate='<b>%{customdata}</b><br>Demand: %{y}<extra></extra>',
        customdata=day_labels,
    ))
    fig_daily.add_trace(go.Bar(
        x=s_daily['date'], y=s_daily['sales_qty'],
        name='Sales (Fulfilled)', marker_color='#2E86AB', opacity=0.9,
        hovertemplate='<b>%{customdata}</b><br>Sales: %{y}<extra></extra>',
        customdata=day_labels,
    ))
    fig_daily.add_trace(go.Bar(
        x=s_daily['date'], y=s_daily['lost_sales_qty'],
        name='Lost Sales (Unmet Demand)',
        marker_color=lost_colors, opacity=0.9,
        hovertemplate='<b>%{customdata}</b><br>Lost Sales: %{y}<extra></extra>',
        customdata=day_labels,
    ))
    fig_daily.add_trace(go.Scatter(
        x=s_inv['date'], y=s_inv['on_hand_qty'],
        name='On-Hand Inventory', mode='lines',
        line=dict(color='#E84855', width=2),
        yaxis='y2',
        hovertemplate='<b>%{customdata}</b><br>On-Hand: %{y}<extra></extra>',
        customdata=inv_day_labels,
    ))
    fig_daily.add_trace(go.Scatter(
        x=s_inv['date'], y=s_inv['on_order_qty'],
        name='On-Order', mode='lines',
        hovertemplate='<b>%{customdata}</b><br>On-Order: %{y}<extra></extra>',
        customdata=inv_day_labels,
        line=dict(color='#F4A261', width=1.5, dash='dot'),
        yaxis='y2',
    ))

    add_promo_shading_daily(fig_daily)

    # ── Highlight store order days ────────────────────────────────────────────
    if not store_od_df.empty:
        order_qty_by_date = (
            store_od_df[
                (store_od_df['store_id'] == sel_store) &
                (store_od_df['item_id']  == sel_item)
            ]
            .groupby('order_date')['order_qty'].sum()
        )
        order_qty_by_date.index = pd.to_datetime(order_qty_by_date.index)
        added_order_legend = False
        for od, qty in order_qty_by_date.items():
            fig_daily.add_vline(
                x=od.timestamp() * 1000,
                line=dict(color='#A8E6CF', width=1.2, dash='dot'),
                annotation_text=f'{int(qty)} units',
                annotation_font=dict(color='#A8E6CF', size=9),
                annotation_position='top',
            )
            if not added_order_legend:
                fig_daily.add_scatter(
                    x=[None], y=[None], mode='lines',
                    name='Store Order Placed',
                    line=dict(color='#A8E6CF', width=1.2, dash='dot'),
                )
                added_order_legend = True

    fig_daily.update_layout(
        barmode='group',
        xaxis=dict(title='Date'),
        yaxis=dict(title='Units (Demand / Sales)', side='left'),
        yaxis2=dict(title='Units (Inventory)', overlaying='y', side='right', showgrid=False),
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        height=420,
    )
    st.plotly_chart(fig_daily, use_container_width=True)

    # ── Chart 2: Weekly — aggregate sales + demand + avg inventory ───────────
    st.markdown("#### Weekly: Demand vs Sales vs Avg Inventory")

    weekly_sales_df = (
        s_sales.groupby('week', sort=True)
        .agg(demand_qty=('demand_qty','sum'), sales_qty=('sales_qty','sum'), lost_sales_qty=('lost_sales_qty','sum'))
        .reset_index()
    )
    weekly_inv_df = (
        s_inv.groupby('week', sort=True)
        .agg(avg_on_hand=('on_hand_qty','mean'))
        .reset_index()
    )
    weekly_df = weekly_sales_df.merge(weekly_inv_df, on='week', how='left')

    fig_weekly = go.Figure()
    fig_weekly.add_trace(go.Bar(
        x=weekly_df['week'], y=weekly_df['demand_qty'],
        name='Demand', marker_color='#BAD7F2', opacity=0.85,
    ))
    fig_weekly.add_trace(go.Bar(
        x=weekly_df['week'], y=weekly_df['sales_qty'],
        name='Sales', marker_color='#2E86AB', opacity=0.9,
    ))
    fig_weekly.add_trace(go.Bar(
        x=weekly_df['week'], y=weekly_df['lost_sales_qty'],
        name='Lost Sales', marker_color='#E84855', opacity=0.7,
    ))
    fig_weekly.add_trace(go.Scatter(
        x=weekly_df['week'], y=weekly_df['avg_on_hand'],
        name='Avg On-Hand Inventory', mode='lines+markers',
        line=dict(color='#F4A261', width=2),
        yaxis='y2',
    ))

    add_promo_shading_weekly(fig_weekly, weekly_df['week'].tolist())
    fig_weekly.update_layout(
        barmode='group',
        xaxis=dict(title='Week', tickangle=-45),
        yaxis=dict(title='Units (Demand / Sales)', side='left'),
        yaxis2=dict(title='Avg Units (Inventory)', overlaying='y', side='right', showgrid=False),
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        height=420,
    )
    st.plotly_chart(fig_weekly, use_container_width=True)

    # ── Chart 3: Inventory status heatmap (all stores × time, item = sel_item) ─
    st.divider()
    st.subheader(f"Inventory Status — All Stores for {sel_item_label}")
    st.caption(f"Selected store **{sel_store}** is highlighted with a marker on the y-axis.")

    heat_df = inv_df[inv_df['item_id'] == sel_item].copy()
    heat_df['date'] = pd.to_datetime(heat_df['date'])
    status_num_map = {'AVAILABLE': 2, 'LOW': 1, 'ZERO': 0}
    heat_df['status_num'] = heat_df['inventory_status'].map(status_num_map)

    pivot = heat_df.pivot_table(index='store_id', columns='date', values='status_num', aggfunc='first')

    # Mark selected store on y-axis labels
    y_labels = [f"► {s}" if s == sel_store else s for s in pivot.index.tolist()]

    fig_heat = go.Figure(go.Heatmap(
        z=pivot.values,
        x=pivot.columns.astype(str),
        y=y_labels,
        colorscale=[[0,'#E84855'],[0.5,'#F4A261'],[1,'#2E86AB']],
        zmin=0, zmax=2,
        colorbar=dict(
            tickvals=[0,1,2],
            ticktext=['ZERO','LOW','AVAILABLE'],
        ),
    ))
    fig_heat.update_layout(height=max(200, len(stores_list)*35+80), xaxis_title='Date', yaxis_title='Store')
    st.plotly_chart(fig_heat, use_container_width=True)

    # ── Raw data expanders (all filtered to sel_store + sel_item) ────────────
    st.divider()
    st.subheader(f"Raw Output Tables — {sel_store} / {sel_item_label}")

    with st.expander("Demand Matrix"):
        st.dataframe(s_demand.reset_index(drop=True))

    with st.expander("Daily Sales"):
        st.dataframe(s_sales.reset_index(drop=True))

    with st.expander("Daily Store Inventory"):
        st.dataframe(s_inv.reset_index(drop=True))

    if not str_rec_df.empty:
        with st.expander("Store Receipts"):
            filt = str_rec_df[
                (str_rec_df['store_id'] == sel_store) &
                (str_rec_df['item_id']  == sel_item)
            ]
            st.dataframe(filt.reset_index(drop=True))

    if not store_od_df.empty:
        with st.expander("Store Order Details"):
            filt = store_od_df[
                (store_od_df['store_id'] == sel_store) &
                (store_od_df['item_id']  == sel_item)
            ]
            st.dataframe(filt.reset_index(drop=True))

    if not str_orders_df.empty:
        with st.expander("Store Orders (header)"):
            filt = str_orders_df[str_orders_df['store_id'] == sel_store]
            st.dataframe(filt.reset_index(drop=True))

    if not sup_rec_df.empty:
        with st.expander("Supplier Receipts"):
            filt = sup_rec_df[sup_rec_df['item_id'] == sel_item]
            st.dataframe(filt.reset_index(drop=True))

    if not sup_orders_df.empty:
        with st.expander("Supplier Orders (for DC serving this store)"):
            sel_dc = store_dc_map.get(sel_store)
            filt   = sup_orders_df[sup_orders_df['dc_id'] == sel_dc] if sel_dc else sup_orders_df
            st.dataframe(filt.reset_index(drop=True))

    # ── Download buttons ──────────────────────────────────────────────────────
    st.divider()
    st.subheader("Download Data")

    # Per-file CamelCase column renames (applied at write / display time)
    _SPEC_RENAMES = {
        'SiteInformation.csv': {
            'site_code': 'SiteCode', 'site_name': 'SiteName',
            'country_code': 'CountryCode', 'site_type': 'SiteType',
            'region': 'Region', 'division': 'Division', 'district': 'District',
            'assigned_dc': 'AssignedDC',
        },
        'ItemInformation.csv': {
            'item_id': 'ItemCode', 'item_description': 'ItemDescription',
            'uom': 'UOM', 'item_status': 'ItemStatus', 'category': 'Category',
            'subcategory': 'Subcategory', 'brand': 'Brand',
            'unit_cost': 'UnitCost', 'unit_price': 'UnitPrice',
            'velocity_class': 'VelocityClass', 'lifecycle_profile': 'LifecycleProfile',
            'case_pack_size': 'CasePackSize', 'size_group': 'SizeGroup',
            'size_rank': 'SizeRank', 'is_ecomm_eligible': 'IsEcommEligible',
        },
        'SupplierInformation.csv': {
            'supplier_id': 'SupplierCode', 'supplier_name': 'SupplierName',
            'supplier_country': 'SupplierCountry', 'supplier_region': 'SupplierRegion',
            'category': 'Category',
        },
        'InventoryInformation.csv': {
            'store_id': 'SiteCode', 'item_id': 'ItemCode',
            'date': 'SnapshotDate', 'week': 'InventoryWeek',
            'on_hand_qty': 'OnHandQty', 'on_order_qty': 'OnOrderQty',
            'inventory_status': 'InventoryStatus', 'woc': 'WeeksOfCover',
        },
        'SupplierOrderHeader.csv': {
            'po_number': 'PurchaseOrderNumber', 'dc_id': 'SiteCode',
            'supplier_id': 'SupplierCode', 'order_date': 'OrderDate',
            'expected_date': 'ExpectedReceiptDate',
        },
        'SupplierOrderLine.csv': {
            'po_number': 'PurchaseOrderNumber', 'dc_id': 'SiteCode',
            'item_id': 'ItemCode', 'supplier_id': 'SupplierCode',
            'order_qty': 'OrderQuantity', 'need_qty': 'NeedQuantity',
            'unit_cost': 'UnitCost',
        },
        'SupplierReceipts.csv': {
            'receipt_id': 'ReceiptId', 'po_number': 'PurchaseOrderNumber',
            'dc_id': 'SiteCode', 'item_id': 'ItemCode',
            'receipt_date': 'ReceiptDate', 'received_qty': 'ReceivedQuantity',
            'receipt_type': 'ReceiptType', 'is_late': 'IsLate', 'is_partial': 'IsPartial',
        },
        'CustomerOrderHeader.csv': {
            'so_number': 'CustomerOrderNumber', 'store_id': 'SiteCode',
            'order_date': 'OrderDate', 'week': 'OrderWeek',
            'dc_id': 'DCCode', 'order_type': 'OrderType',
        },
        'CustomerOrderLine.csv': {
            'so_number': 'CustomerOrderNumber', 'item_id': 'ItemCode',
            'store_id': 'SiteCode', 'order_qty': 'OrderQuantity',
            'order_date': 'OrderDate', 'order_type': 'OrderType',
        },
        'CustomerOrderDelivery.csv': {
            'so_number': 'CustomerOrderNumber', 'store_id': 'SiteCode',
            'item_id': 'ItemCode', 'delivery_date': 'DeliveryDate',
            'delivered_qty': 'DeliveredQuantity', 'unfilled_qty': 'UnfilledQuantity',
            'delivery_status': 'DeliveryStatus', 'order_qty': 'OrderQuantity',
        },
        'SalesHistoryInformation.csv': {
            'store_id': 'SiteCode', 'item_id': 'ItemCode',
            'date': 'CalendarDate', 'week': 'SalesWeek',
            'sales_qty': 'SalesQuantity', 'sales_amount': 'SalesAmount',
            'demand_qty': 'DemandQuantity', 'lost_sales_qty': 'LostSalesQuantity',
        },
        'CalendarPeriod.csv': {
            'date': 'CalendarDate', 'week': 'WeekId',
            'month': 'MonthId', 'quarter': 'QuarterId', 'year': 'YearId',
        },
        'Currency.csv': {
            'currency_code': 'CurrencyCode', 'currency_name': 'CurrencyName',
            'symbol': 'Symbol',
        },
        'PromoEvents.csv': {
            'promo_name': 'PromoEventId', 'promo_group_name': 'PromoGroupName',
            'event_type': 'EventType', 'start_date': 'PromoStartDate',
            'end_date': 'PromoEndDate', 'demand_multiplier': 'DemandMultiplier',
            'post_promo_decay_days': 'PostPromoDecayDays',
            'post_promo_decay_shape': 'PostPromoDecayShape',
            'item_id': 'ItemCode', 'store_id': 'SiteCode',
        },
    }

    def _prepare_export_dfs(filter_store=None, filter_item=None):
        sel_dc_dl = store_dc_map.get(filter_store) if filter_store else None

        sel_supplier_dl = None
        if sel_dc_dl is not None and not sup_orders_df.empty and 'supplier_id' in sup_orders_df.columns:
            dc_sup = sup_orders_df[sup_orders_df['dc_id'] == sel_dc_dl]['supplier_id']
            if not dc_sup.empty:
                sel_supplier_dl = dc_sup.iloc[0]

        def f_store(df):
            if df is None or df.empty or not filter_store:
                return df if df is not None else pd.DataFrame()
            return df[df['store_id'] == filter_store] if 'store_id' in df.columns else df

        def f_item(df):
            if df is None or df.empty or not filter_item:
                return df if df is not None else pd.DataFrame()
            return df[df['item_id'] == filter_item] if 'item_id' in df.columns else df

        def f_dc(df):
            if df is None or df.empty or not sel_dc_dl:
                return df if df is not None else pd.DataFrame()
            return df[df['dc_id'] == sel_dc_dl] if 'dc_id' in df.columns else df

        cal_df = demand_df[['date', 'week']].drop_duplicates().copy()
        cal_df['date'] = pd.to_datetime(cal_df['date'])
        cal_df['month']   = cal_df['date'].dt.month
        cal_df['quarter'] = cal_df['date'].dt.quarter
        cal_df['year']    = cal_df['date'].dt.year
        cal_df = cal_df.sort_values('date').reset_index(drop=True)

        currency_df = pd.DataFrame([{'currency_code': 'USD', 'currency_name': 'US Dollar', 'symbol': '$'}])

        if not promos_df.empty:
            promo_ev = promos_df.merge(
                promo_group_items_df[['promo_group_name', 'item_id']], on='promo_group_name', how='left'
            ).merge(
                promo_stores_df[['promo_name', 'store_id']], on='promo_name', how='left'
            )
            if filter_store:
                promo_ev = promo_ev[promo_ev['store_id'] == filter_store]
            if filter_item:
                promo_ev = promo_ev[promo_ev['item_id'] == filter_item]
        else:
            promo_ev = pd.DataFrame()

        # Merge stores + DCs into unified SiteInformation schema
        _store_site = stores_df.rename(columns={
            'store_id': 'site_code', 'store_name': 'site_name', 'store_type': 'site_type',
        }).copy()
        _store_site['site_type'] = 'Store'
        _store_site['assigned_dc'] = _store_site['site_code'].map(store_dc_map)

        _dc_site = dcs_df.rename(columns={
            'dc_id': 'site_code', 'dc_name': 'site_name', 'dc_type': 'site_type',
        }).copy()
        _dc_site['site_type'] = 'DC'
        _dc_site['assigned_dc'] = None

        site_df = pd.concat([_store_site, _dc_site], ignore_index=True)
        if filter_store:
            _sel_dc = store_dc_map.get(filter_store)
            _keep = [filter_store] + ([_sel_dc] if _sel_dc else [])
            site_df = site_df[site_df['site_code'].isin(_keep)]

        item_dl_df = items_df.copy()
        if filter_item:
            item_dl_df = item_dl_df[item_dl_df['item_id'] == filter_item]

        sup_info_df = suppliers_df.copy()
        if sel_supplier_dl:
            sup_info_df = sup_info_df[sup_info_df['supplier_id'] == sel_supplier_dl]

        return {
            'SiteInformation.csv':         site_df,
            'ItemInformation.csv':         item_dl_df,
            'SupplierInformation.csv':     sup_info_df,
            'InventoryInformation.csv':    (
                f_item(f_store(inv_df))
                .sort_values('date')
                .groupby(['store_id', 'item_id', 'week'], sort=False)
                .last()
                .reset_index()
            ),
            'SupplierOrderHeader.csv':     f_dc(sup_orders_df),
            'SupplierOrderLine.csv':       f_item(f_dc(supplier_od_df)),
            'SupplierReceipts.csv':        f_item(f_dc(sup_rec_df)),
            'CustomerOrderHeader.csv':     f_store(str_orders_df),
            'CustomerOrderLine.csv':       f_item(f_store(store_od_df)),
            'CustomerOrderDelivery.csv':   f_item(f_store(str_rec_df)),
            'SalesHistoryInformation.csv': f_item(f_store(sales_df)),
            'CalendarPeriod.csv':          cal_df,
            'Currency.csv':                currency_df,
            'PromoEvents.csv':             promo_ev,
        }

    def _build_manifest_and_report():
        run_id_val  = st.session_state.get('run_id', 'unknown')
        run_cfg     = st.session_state.get('run_config', {})
        elapsed     = st.session_state.get('run_elapsed', 0.0)
        generated   = datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        full_dfs = _prepare_export_dfs()

        # feeds block
        feeds = [
            {'name': fname, 'row_count': len(df) if df is not None and not df.empty else 0, 'path': fname}
            for fname, df in full_dfs.items()
        ]

        # ── checks ────────────────────────────────────────────────────────────
        checks = []

        # 1. all feeds non-empty
        empty = [f['name'] for f in feeds if f['row_count'] == 0]
        checks.append({'name': 'all_feeds_non_empty', 'passed': len(empty) == 0,
                        'violations': len(empty), 'details': empty or None})

        # 2. no null SiteCode
        site_exp = full_dfs.get('SiteInformation.csv')
        v = int(site_exp['site_code'].isna().sum()) if site_exp is not None and not site_exp.empty else 0
        checks.append({'name': 'no_null_site_codes', 'passed': v == 0, 'violations': v, 'details': None})

        # 3. no null ItemCode
        item_exp = full_dfs.get('ItemInformation.csv')
        v = int(item_exp['item_id'].isna().sum()) if item_exp is not None and not item_exp.empty else 0
        checks.append({'name': 'no_null_item_codes', 'passed': v == 0, 'violations': v, 'details': None})

        # 4. inventory dates in simulation range
        inv_exp = full_dfs.get('InventoryInformation.csv')
        v = 0
        if inv_exp is not None and not inv_exp.empty:
            start_d = pd.to_datetime(run_cfg.get('start_date'))
            end_d   = pd.to_datetime(run_cfg.get('end_date'))
            dates   = pd.to_datetime(inv_exp['date'])
            v = int(((dates < start_d) | (dates > end_d)).sum())
        checks.append({'name': 'inventory_dates_in_range', 'passed': v == 0, 'violations': v, 'details': None})

        # 5. SupplierReceipts receipt_type always SUPPLIER_DELIVERY
        sr_exp = full_dfs.get('SupplierReceipts.csv')
        v = 0
        if sr_exp is not None and not sr_exp.empty and 'receipt_type' in sr_exp.columns:
            v = int((sr_exp['receipt_type'] != 'SUPPLIER_DELIVERY').sum())
        checks.append({'name': 'supplier_receipt_type_valid', 'passed': v == 0, 'violations': v, 'details': None})

        # 6. CustomerOrderDelivery delivery_status values valid
        cod_exp = full_dfs.get('CustomerOrderDelivery.csv')
        v = 0
        if cod_exp is not None and not cod_exp.empty and 'delivery_status' in cod_exp.columns:
            v = int((~cod_exp['delivery_status'].isin({'FULL_DELIVERY', 'PARTIAL_DELIVERY'})).sum())
        checks.append({'name': 'delivery_status_valid', 'passed': v == 0, 'violations': v, 'details': None})

        # 7. store fill rate >= 50 %
        fill_rate = 1.0
        if cod_exp is not None and not cod_exp.empty:
            total_ord = cod_exp['order_qty'].sum()
            total_del = cod_exp['delivered_qty'].sum()
            fill_rate = float(total_del / total_ord) if total_ord > 0 else 1.0
        fill_ok = fill_rate >= 0.5
        checks.append({'name': 'store_fill_rate_min_50pct', 'passed': fill_ok,
                        'violations': 0 if fill_ok else 1, 'details': None})

        validation_passed = all(c['passed'] for c in checks)

        # ── summary stats ─────────────────────────────────────────────────────
        dc_stockout_rate = 0.0
        if inv_exp is not None and not inv_exp.empty:
            dc_ids = set(dcs_df['dc_id'].tolist())
            dc_inv = inv_exp[inv_exp['store_id'].isin(dc_ids)]
            if not dc_inv.empty:
                dc_stockout_rate = float((dc_inv['on_hand_qty'] == 0).mean())

        summary_stats = {
            'dc_stockout_rate':       round(dc_stockout_rate, 4),
            'store_fill_rate':        round(fill_rate, 4),
            'generation_time_seconds': round(elapsed, 2),
        }

        manifest = {
            'run_id':           run_id_val,
            'generated_at':     generated,
            'spec_version':     '1.1',
            'config':           run_cfg,
            'feeds':            feeds,
            'validation_passed': validation_passed,
        }
        report = {
            'run_id':            run_id_val,
            'generated_at':      generated,
            'validation_passed': validation_passed,
            'checks':            checks,
            'summary_stats':     summary_stats,
        }
        return (
            json.dumps(manifest, indent=2, default=str),
            json.dumps(report,   indent=2, default=str),
        )

    def build_zip(filter_store=None, filter_item=None):
        files = _prepare_export_dfs(filter_store=filter_store, filter_item=filter_item)
        manifest_json, report_json = _build_manifest_and_report()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for fname, df in files.items():
                if df is not None and not df.empty:
                    out = df.rename(columns=_SPEC_RENAMES.get(fname, {}))
                    zf.writestr(fname, out.to_csv(index=False))
                else:
                    zf.writestr(fname, '')
            zf.writestr('run_manifest.json',       manifest_json)
            zf.writestr('data_quality_report.json', report_json)
        buf.seek(0)
        return buf.getvalue()

    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        st.download_button(
            label=f"⬇ Download — {sel_store} / {sel_item} (filtered)",
            data=build_zip(filter_store=sel_store, filter_item=sel_item),
            file_name=f"export_{sel_store}_{sel_item}.zip",
            mime="application/zip",
            use_container_width=True,
        )
    with col_dl2:
        st.download_button(
            label="⬇ Download — All Stores & Items",
            data=build_zip(),
            file_name="export_all.zip",
            mime="application/zip",
            use_container_width=True,
        )

    # ── Validation ────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Validation")
    _manifest_json, _report_json = _build_manifest_and_report()
    _report = json.loads(_report_json)
    _passed  = _report['validation_passed']
    if _passed:
        st.success("validation_passed: true — all checks passed")
    else:
        st.error("validation_passed: false — one or more checks failed")

    _chk_col, _stat_col = st.columns(2)
    with _chk_col:
        st.caption("**Checks**")
        for chk in _report['checks']:
            icon = "✓" if chk['passed'] else "✗"
            detail = f"  ({chk['violations']} violations)" if not chk['passed'] else ""
            st.markdown(f"`{icon}` {chk['name']}{detail}")
    with _stat_col:
        st.caption("**Summary Stats**")
        ss = _report['summary_stats']
        st.markdown(f"- DC stockout rate: **{ss['dc_stockout_rate']:.2%}**")
        st.markdown(f"- Store fill rate: **{ss['store_fill_rate']:.2%}**")
        st.markdown(f"- Generation time: **{ss['generation_time_seconds']}s**")

    with st.expander("run_manifest.json", expanded=False):
        st.code(_manifest_json, language='json')
    with st.expander("data_quality_report.json", expanded=False):
        st.code(_report_json, language='json')

    # ── Output CSV Viewer ─────────────────────────────────────────────────────
    st.divider()
    st.subheader("Output CSV Tables")

    view_mode = st.radio(
        "View mode",
        options=["Filtered (selected store & item)", "All stores & items"],
        horizontal=True,
        key="csv_view_mode",
    )
    if view_mode.startswith("Filtered"):
        _view_dfs = _prepare_export_dfs(filter_store=sel_store, filter_item=sel_item)
        _view_label = f"{sel_store} / {sel_item}"
    else:
        _view_dfs = _prepare_export_dfs()
        _view_label = "All stores & items"

    for csv_name, df in _view_dfs.items():
        table_title = csv_name.replace('.csv', '')
        row_count = len(df) if df is not None and not df.empty else 0
        with st.expander(f"{table_title}  —  {row_count:,} rows  ({_view_label})", expanded=False):
            if row_count > 0:
                display_df = df.rename(columns=_SPEC_RENAMES.get(csv_name, {})).reset_index(drop=True)
                st.dataframe(display_df, use_container_width=True)
            else:
                st.caption("No data for the current selection.")

else:
    st.info("Configure parameters in the sidebar and click **▶ Run Simulation** to start.")

