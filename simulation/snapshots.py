"""
simulation/snapshots.py — Weekly snapshot writes and final buffer flush.

Step 6: weekly_snapshot  — flush receipts, write sales/inventory to ClickHouse
        final_flush      — flush any remaining buffers at end of simulation
        print_summary    — print row count summary
"""

from collections import defaultdict
from datetime import date

import pandas as pd

from .config import SimConfig
from .data_loader import SimData
from .demand import get_avg_daily
from .state import SimState


def inventory_status_store(state: SimState, data: SimData,
                            store: str, item: str, avg_daily: float) -> str:
    oh        = state.on_hand[store][item]
    threshold = data.store_cfg[store]['weeks_of_cover_threshold']
    if oh == 0:
        return 'ZERO'
    if avg_daily <= 0:
        return 'AVAILABLE'
    woc = oh / (avg_daily * 7.0)
    return 'LOW' if woc < threshold else 'AVAILABLE'


def inventory_status_dc(state: SimState, data: SimData,
                         dc: str, item: str, dc_avg_daily: float) -> str:
    oh        = state.on_hand[dc][item]
    threshold = data.dc_cfg[dc]['weeks_of_cover_threshold']
    if oh == 0:
        return 'ZERO'
    if dc_avg_daily <= 0:
        return 'AVAILABLE'
    woc = oh / (dc_avg_daily * 7.0)
    return 'LOW' if woc < threshold else 'AVAILABLE'


def weekly_snapshot(state: SimState, config: SimConfig, data: SimData,
                    ch, current_date: date, week_str: str) -> None:
    # Flush receipt buffers
    if state.supplier_receipts_buf:
        ch.insert_df('supplier_receipts', pd.DataFrame(state.supplier_receipts_buf))
        state.supplier_receipts_buf = []
    if state.store_receipts_buf:
        ch.insert_df('store_receipts', pd.DataFrame(state.store_receipts_buf))
        state.store_receipts_buf = []

    # sales_history — weekly aggregate
    sales_rows = []
    for store in config.stores:
        for item in config.items:
            qty = state.weekly_sales[store][item]
            sales_rows.append({
                'simulation_id':  config.sim_id,
                'account_id':     config.account_id,
                'store_id':       store,
                'item_id':        item,
                'sales_week':     week_str,
                'sales_quantity': float(qty),
                'sales_amount':   float(qty) * data.unit_price_map.get(item, 0.0),
                'unit_price':     data.unit_price_map.get(item, 0.0),
                'uom':            'EA',
            })
            state.counts['sales_history'] += 1
    if sales_rows:
        df = pd.DataFrame(sales_rows)
        ch.insert_df('sales_history', df, column_names=df.columns.tolist())

    # store_inventory snapshot
    si_rows = []
    for store in config.stores:
        for item in config.items:
            avg    = get_avg_daily(state, config, data.baseline_demand, store, item)
            status = inventory_status_store(state, data, store, item, avg)
            si_rows.append({
                'simulation_id':      config.sim_id,
                'account_id':         config.account_id,
                'store_id':           store,
                'item_id':            item,
                'inventory_week':     week_str,
                'on_hand_quantity':   float(state.on_hand[store][item]),
                'available_quantity': float(state.on_hand[store][item]),
                'on_order_quantity':  float(state.on_order[store][item]),
                'inventory_status':   status,
            })
            state.counts['store_inventory'] += 1
    if si_rows:
        df = pd.DataFrame(si_rows)
        ch.insert_df('store_inventory', df, column_names=df.columns.tolist())

    # dc_inventory snapshot
    di_rows = []
    for dc in config.dcs:
        dc_stores = config.dc_assign[dc]
        for item in config.items:
            dc_avg = sum(
                get_avg_daily(state, config, data.baseline_demand, s, item)
                for s in dc_stores
            )
            status = inventory_status_dc(state, data, dc, item, dc_avg)
            di_rows.append({
                'simulation_id':      config.sim_id,
                'account_id':         config.account_id,
                'dc_id':              dc,
                'item_id':            item,
                'inventory_week':     week_str,
                'on_hand_quantity':   float(state.on_hand[dc][item]),
                'available_quantity': float(state.on_hand[dc][item]),
                'on_order_quantity':  float(state.on_order[dc][item]),
                'inventory_status':   status,
            })
            state.counts['dc_inventory'] += 1
    if di_rows:
        df = pd.DataFrame(di_rows)
        ch.insert_df('dc_inventory', df, column_names=df.columns.tolist())

    # Reset weekly accumulators
    state.weekly_sales      = defaultdict(lambda: defaultdict(float))
    state.weekly_lost_sales = defaultdict(lambda: defaultdict(float))


def _insert(ch, table: str, rows: list) -> None:
    if rows:
        df = pd.DataFrame(rows)
        ch.insert_df(table, df, column_names=df.columns.tolist())


def final_flush(state: SimState, ch) -> None:
    _insert(ch, 'supplier_orders', state.supplier_orders_buf)
    _insert(ch, 'supplier_order_details', state.supplier_order_details_buf)
    _insert(ch, 'supplier_receipts', state.supplier_receipts_buf)
    _insert(ch, 'store_receipts', state.store_receipts_buf)


def print_summary(config: SimConfig, state: SimState) -> None:
    total_days = (config.end_date - config.start_date).days + 1
    print(f"\nSimulation complete: {config.sim_id}")
    print(f"Days processed: {total_days}")
    print("Rows written:")
    for table, cnt in state.counts.items():
        print(f"  {table}:  {cnt:,}")
