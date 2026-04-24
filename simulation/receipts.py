"""
simulation/receipts.py — Receipt processing for Supplier→DC and DC→Store shipments.

Step 1: process_supplier_receipts — fires scheduled supplier deliveries
Step 2: process_store_receipts    — fires scheduled DC→store deliveries

Both functions mutate state in place (on_hand, on_order, schedules, buffers).
"""

import math
from datetime import date, timedelta

from .config import SimConfig
from .data_loader import SimData
from .state import SimState


def process_supplier_receipts(state: SimState, config: SimConfig,
                               data: SimData, current_date: date) -> None:
    still_pending = []
    for entry in state.receipt_schedule:
        if entry['scheduled_date'] != current_date:
            still_pending.append(entry)
            continue

        dc_id   = entry['dc_id']
        item_id = entry['item_id']
        qty     = entry['qty']
        po_num  = entry['po_number']

        sup_id   = config.dc_supplier[dc_id]
        sup_prof = data.supplier_cfg[sup_id]

        is_late_flag    = entry.get('is_late', False)
        already_partial = entry.get('already_partial', False)

        # Late check
        if not is_late_flag and state.rng.random() > sup_prof['on_time_rate']:
            extra = int(state.rng.integers(sup_prof['late_days_min'], sup_prof['late_days_max'] + 1))
            entry['scheduled_date'] = current_date + timedelta(days=extra)
            entry['is_late'] = True
            still_pending.append(entry)
            continue

        # Partial check
        if not already_partial and state.rng.random() < sup_prof['partial_delivery_rate']:
            frac      = state.rng.uniform(sup_prof['partial_frac_min'], sup_prof['partial_frac_max'])
            received  = int(math.floor(qty * frac))
            remainder = qty - received

            state.on_hand[dc_id][item_id]  += received
            state.on_order[dc_id][item_id]  = max(0, state.on_order[dc_id][item_id] - received)

            state.receipt_seq += 1
            state.supplier_receipts_buf.append({
                'receipt_id':            f'REC_{config.sim_id}_{state.receipt_seq:06d}',
                'line_number':           1,
                'simulation_id':         config.sim_id,
                'account_id':            config.account_id,
                'purchase_order_number': po_num,
                'dc_id':                 dc_id,
                'item_id':               item_id,
                'receipt_date':          current_date,
                'received_quantity':     float(received),
                'unfilled_quantity':     float(remainder),
                'receipt_type':          'PARTIAL',
            })
            state.counts['supplier_receipts'] += 1

            gap = int(state.rng.integers(sup_prof['remainder_gap_min'], sup_prof['remainder_gap_max'] + 1))
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
            state.on_hand[dc_id][item_id]  += qty
            state.on_order[dc_id][item_id]  = max(0, state.on_order[dc_id][item_id] - qty)

            state.receipt_seq += 1
            state.supplier_receipts_buf.append({
                'receipt_id':            f'REC_{config.sim_id}_{state.receipt_seq:06d}',
                'line_number':           1,
                'simulation_id':         config.sim_id,
                'account_id':            config.account_id,
                'purchase_order_number': po_num,
                'dc_id':                 dc_id,
                'item_id':               item_id,
                'receipt_date':          current_date,
                'received_quantity':     float(qty),
                'unfilled_quantity':     0.0,
                'receipt_type':          'FULL',
            })
            state.counts['supplier_receipts'] += 1

    state.receipt_schedule = still_pending


def process_store_receipts(state: SimState, config: SimConfig,
                            data: SimData, current_date: date) -> None:
    still_pending_sr = []
    for entry in state.store_receipt_schedule:
        if entry['scheduled_date'] != current_date:
            still_pending_sr.append(entry)
            continue

        store_id = entry['store_id']
        item_id  = entry['item_id']
        qty      = entry['qty']
        so_num   = entry['so_number']
        dc_id    = config.store_dc[store_id]
        dcfg     = data.dc_cfg[dc_id]

        is_late_flag    = entry.get('is_late', False)
        already_partial = entry.get('already_partial', False)

        # Late check
        if not is_late_flag and state.rng.random() > dcfg['on_time_rate']:
            extra = int(state.rng.integers(dcfg['late_days_min'], dcfg['late_days_max'] + 1))
            entry['scheduled_date'] = current_date + timedelta(days=extra)
            entry['is_late'] = True
            still_pending_sr.append(entry)
            continue

        # Partial check
        if not already_partial and state.rng.random() < dcfg['partial_delivery_rate']:
            frac      = state.rng.uniform(dcfg['partial_frac_min'], dcfg['partial_frac_max'])
            received  = int(math.floor(qty * frac))
            remainder = qty - received

            state.on_hand[store_id][item_id]  += received
            state.on_order[store_id][item_id]  = max(0, state.on_order[store_id][item_id] - received)

            state.sr_seq += 1
            state.store_receipts_buf.append({
                'receipt_id':         f'SR_{config.sim_id}_{state.sr_seq:06d}',
                'line_number':        config.item_line_num[item_id],
                'simulation_id':      config.sim_id,
                'account_id':         config.account_id,
                'store_order_number': so_num,
                'store_id':           store_id,
                'item_id':            item_id,
                'receipt_date':       current_date,
                'received_quantity':  float(received),
                'unfilled_quantity':  float(remainder),
                'receipt_type':       'PARTIAL',
            })
            state.counts['store_receipts'] += 1

            gap = int(state.rng.integers(dcfg['remainder_gap_min'], dcfg['remainder_gap_max'] + 1))
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
            state.on_hand[store_id][item_id]  += qty
            state.on_order[store_id][item_id]  = max(0, state.on_order[store_id][item_id] - qty)

            state.sr_seq += 1
            state.store_receipts_buf.append({
                'receipt_id':         f'SR_{config.sim_id}_{state.sr_seq:06d}',
                'line_number':        config.item_line_num[item_id],
                'simulation_id':      config.sim_id,
                'account_id':         config.account_id,
                'store_order_number': so_num,
                'store_id':           store_id,
                'item_id':            item_id,
                'receipt_date':       current_date,
                'received_quantity':  float(qty),
                'unfilled_quantity':  0.0,
                'receipt_type':       'FULL',
            })
            state.counts['store_receipts'] += 1

    state.store_receipt_schedule = still_pending_sr
