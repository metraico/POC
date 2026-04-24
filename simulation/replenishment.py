"""
simulation/replenishment.py — Order placement and DC allocation logic.

Step 5: place_store_orders     — stores raise orders to their DC (on order cycle day)
Step 6: dc_allocate_to_stores  — DC allocates available stock proportionally (every day)
Step 7: raise_dc_supplier_pos  — DCs raise POs to suppliers (Mondays only)
"""

import math
from collections import defaultdict
from datetime import date, timedelta

import pandas as pd

from .config import SimConfig
from .data_loader import SimData
from .demand import get_avg_daily, get_case_pack_for_item
from .state import SimState


def place_store_orders(state: SimState, config: SimConfig, data: SimData,
                       ch, current_date: date, dow: int, week_str: str) -> None:
    """Step 5 — runs on each store's order_cycle_day.

    Writes store_orders + store_order_details to ClickHouse and queues
    each line into state.open_store_orders for Step 6 to allocate.
    """
    so_rows  = []
    sod_rows = []

    for store in config.stores:
        scfg      = data.store_cfg[store]
        order_dow = config.day_map[scfg['order_cycle_day'].upper()]
        if dow != order_dow:
            continue

        # Only order items in this store's assortment
        store_assortment = data.store_items.get(store, frozenset())

        items_to_order = []
        for item in config.items:
            if item not in store_assortment:
                continue
            avg_daily     = get_avg_daily(state, config, data.baseline_demand, store, item)
            reorder_point = scfg['reorder_point_weeks'] * avg_daily * 7
            if state.on_hand[store][item] >= reorder_point:
                continue
            target_stock = scfg['target_stock_weeks'] * avg_daily * 7
            order_qty    = max(0, int(round(target_stock - state.on_hand[store][item])))
            if order_qty > 0:
                items_to_order.append((item, order_qty))

        if not items_to_order:
            continue

        state.so_seq += 1
        dc_id  = config.store_dc[store]
        so_num = f"SO_{config.run_id}_{current_date.strftime('%Y%m%d')}_{store}_{state.so_seq:04d}"

        so_rows.append({
            'store_order_number': so_num,
            'simulation_id':      config.sim_id,
            'account_id':         config.account_id,
            'store_id':           store,
            'dc_id':              dc_id,
            'order_week':         week_str,
            'order_date':         current_date,
            'order_status':       'OPEN',
        })
        state.counts['store_orders'] += 1

        for item, order_qty in items_to_order:
            sod_rows.append({
                'store_order_number': so_num,
                'line_number':        config.item_line_num[item],
                'simulation_id':      config.sim_id,
                'account_id':         config.account_id,
                'item_id':            item,
                'order_quantity':     float(order_qty),
                'uom':                'EA',
            })
            state.counts['store_order_details'] += 1

            # Queue for Step 6 DC allocation
            state.open_store_orders.append({
                'dc_id':    dc_id,
                'store_id': store,
                'item_id':  item,
                'so_num':   so_num,
                'line_num': config.item_line_num[item],
                'qty':      order_qty,
            })

    if so_rows:
        ch.insert_df('store_orders', pd.DataFrame(so_rows))
    if sod_rows:
        ch.insert_df('store_order_details', pd.DataFrame(sod_rows))


def dc_allocate_to_stores(state: SimState, config: SimConfig, data: SimData,
                           current_date: date) -> None:
    """Step 6 — runs every day.

    For each DC, collects all open store orders and allocates available
    DC stock proportionally across stores competing for the same item.
    Zero-stock outcomes are recorded as PARTIAL receipts immediately.
    Allocated stock is scheduled as DC→store receipt events.
    """
    if not state.open_store_orders:
        return

    # Group open orders by (dc_id, item_id)
    groups: dict = defaultdict(list)
    for order in state.open_store_orders:
        groups[(order['dc_id'], order['item_id'])].append(order)

    for (dc_id, item_id), orders in groups.items():
        total_need = sum(o['qty'] for o in orders)
        available  = state.on_hand[dc_id][item_id]

        if available == 0:
            # DC stockout — write zero receipts for all stores
            for o in orders:
                state.sr_seq += 1
                state.store_receipts_buf.append({
                    'receipt_id':         f'SR_{config.sim_id}_{state.sr_seq:06d}',
                    'line_number':        o['line_num'],
                    'simulation_id':      config.sim_id,
                    'account_id':         config.account_id,
                    'store_order_number': o['so_num'],
                    'store_id':           o['store_id'],
                    'item_id':            item_id,
                    'receipt_date':       current_date,
                    'received_quantity':  0.0,
                    'unfilled_quantity':  float(o['qty']),
                    'receipt_type':       'PARTIAL',
                })
                state.counts['store_receipts'] += 1

        elif available >= total_need:
            # Full allocation — every store gets exactly what it ordered
            state.on_hand[dc_id][item_id] -= total_need
            for o in orders:
                state.on_order[o['store_id']][item_id] += o['qty']
                state.store_receipt_schedule.append({
                    'store_id':       o['store_id'],
                    'item_id':        item_id,
                    'so_number':      o['so_num'],
                    'qty':            o['qty'],
                    'scheduled_date': current_date,
                    'is_late':        False,
                    'already_partial': False,
                })

        else:
            # Partial DC stock — proportional allocation
            shares: dict = {}
            for o in orders:
                shares[id(o)] = math.floor(o['qty'] / total_need * available)

            # Distribute leftover units (from floor rounding) to stores
            # with the biggest remaining shortfall first
            leftover = available - sum(shares.values())
            if leftover > 0:
                sorted_orders = sorted(orders,
                                       key=lambda o: o['qty'] - shares[id(o)],
                                       reverse=True)
                for o in sorted_orders[:leftover]:
                    shares[id(o)] += 1

            total_shipped = sum(shares.values())
            state.on_hand[dc_id][item_id] -= total_shipped

            for o in orders:
                ship     = shares[id(o)]
                unfilled = o['qty'] - ship

                if ship > 0:
                    state.on_order[o['store_id']][item_id] += ship
                    state.store_receipt_schedule.append({
                        'store_id':        o['store_id'],
                        'item_id':         item_id,
                        'so_number':       o['so_num'],
                        'qty':             ship,
                        'scheduled_date':  current_date,
                        'is_late':         False,
                        'already_partial': False,
                    })

                # Always record the receipt row (even for zero-ship), so
                # the unfilled quantity is visible in the data
                state.sr_seq += 1
                state.store_receipts_buf.append({
                    'receipt_id':         f'SR_{config.sim_id}_{state.sr_seq:06d}',
                    'line_number':        o['line_num'],
                    'simulation_id':      config.sim_id,
                    'account_id':         config.account_id,
                    'store_order_number': o['so_num'],
                    'store_id':           o['store_id'],
                    'item_id':            item_id,
                    'receipt_date':       current_date,
                    'received_quantity':  float(ship),
                    'unfilled_quantity':  float(unfilled),
                    'receipt_type':       'PARTIAL' if unfilled > 0 else 'FULL',
                })
                state.counts['store_receipts'] += 1

    # All open orders have been processed (allocated or zeroed)
    state.open_store_orders = []


def raise_dc_supplier_pos(state: SimState, config: SimConfig, data: SimData,
                           ch, current_date: date, dow: int) -> None:
    """Step 7 — runs on Mondays only."""
    if dow != config.dc_review_dow:
        return

    for dc in config.dcs:
        dc_stores = config.dc_assign[dc]
        supplier  = config.dc_supplier[dc]
        dcfg      = data.dc_cfg[dc]
        sup_prof  = data.supplier_cfg[supplier]

        # Only raise POs for items in this DC's assortment
        dc_assortment = data.dc_items.get(dc, frozenset())

        for item in config.items:
            if item not in dc_assortment:
                continue

            dc_avg = sum(
                get_avg_daily(state, config, data.baseline_demand, s, item)
                for s in dc_stores
            )

            reorder_point  = dcfg['reorder_point_weeks'] * dc_avg * 7
            stock_position = state.on_hand[dc][item] + state.on_order[dc][item]
            if stock_position >= reorder_point:
                continue

            target_stock = dcfg['target_stock_weeks'] * dc_avg * 7
            raw_order    = max(0, int(round(target_stock - stock_position)))
            if raw_order == 0:
                continue

            case_pack = get_case_pack_for_item(config, data.category_map, item)
            order_qty = math.ceil(raw_order / case_pack) * case_pack

            state.po_seq += 1
            po_num = (f"PO_{config.run_id}_{current_date.strftime('%Y%m%d')}"
                      f"_{dc}_{state.po_seq:04d}")

            lead_time        = int(state.rng.integers(sup_prof['lead_time_min'],
                                                       sup_prof['lead_time_max'] + 1))
            expected_receipt = current_date + timedelta(days=lead_time)

            state.supplier_orders_buf.append({
                'purchase_order_number': po_num,
                'simulation_id':         config.sim_id,
                'account_id':            config.account_id,
                'dc_id':                 dc,
                'supplier_id':           supplier,
                'order_date':            current_date,
                'expected_receipt_date': expected_receipt,
                'order_status':          'OPEN',
            })
            state.counts['supplier_orders'] += 1

            state.supplier_order_details_buf.append({
                'purchase_order_number': po_num,
                'line_number':           1,
                'simulation_id':         config.sim_id,
                'account_id':            config.account_id,
                'dc_id':                 dc,
                'item_id':               item,
                'supplier_id':           supplier,
                'need_quantity':         int(raw_order),
                'order_quantity':        float(order_qty),
                'unit_cost':             data.unit_cost_map.get(item, 0.0),
                'uom':                   'EA',
            })
            state.counts['supplier_order_details'] += 1

            state.receipt_schedule.append({
                'dc_id':           dc,
                'item_id':         item,
                'po_number':       po_num,
                'qty':             order_qty,
                'scheduled_date':  expected_receipt,
                'is_late':         False,
                'already_partial': False,
            })

            state.on_order[dc][item] += order_qty

    if state.supplier_orders_buf:
        ch.insert_df('supplier_orders', pd.DataFrame(state.supplier_orders_buf))
        state.supplier_orders_buf = []
    if state.supplier_order_details_buf:
        ch.insert_df('supplier_order_details', pd.DataFrame(state.supplier_order_details_buf))
        state.supplier_order_details_buf = []
