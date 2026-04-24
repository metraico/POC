"""
simulation/state.py — Mutable simulation state.

SimState holds all state that changes during the simulation run.
It is created once and passed by reference to every step function.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import List

import numpy as np

from .config import SimConfig
from .data_loader import SimData


@dataclass
class SimState:
    # Inventory levels: location -> item -> int
    on_hand:  defaultdict
    on_order: defaultdict

    # Pending receipt queues (lists of dicts)
    receipt_schedule:       List[dict]   # Supplier → DC
    store_receipt_schedule: List[dict]   # DC → Store

    # Demand history: store -> item -> list[int] (rolling window)
    demand_history: defaultdict

    # Open store orders waiting for DC allocation (Step 6)
    # Each entry: {dc_id, store_id, item_id, so_num, line_num, qty}
    open_store_orders: List[dict]

    # ClickHouse write buffers
    supplier_orders_buf:        List[dict]
    supplier_order_details_buf: List[dict]
    supplier_receipts_buf:      List[dict]
    store_receipts_buf:         List[dict]

    # Sequence counters (incremented as records are created)
    po_seq:      int
    sr_seq:      int
    so_seq:      int
    receipt_seq: int

    # Row count accumulators per table
    counts: dict

    # Weekly sales and lost sales: store -> item -> float
    weekly_sales:      defaultdict
    weekly_lost_sales: defaultdict

    # Seeded RNG — never recreate mid-run
    rng: np.random.Generator


def build_initial_state(config: SimConfig, data: SimData) -> SimState:
    on_hand  = defaultdict(lambda: defaultdict(int))
    on_order = defaultdict(lambda: defaultdict(int))

    # DC starting stock
    for dc in config.dcs:
        dc_stores  = config.dc_assign[dc]
        start_days = data.dc_cfg[dc]['start_stock_days']
        for item in config.items:
            dc_avg = sum(data.baseline_demand[s][item] for s in dc_stores)
            on_hand[dc][item] = int(round(dc_avg * start_days))

    # Store starting stock
    for store in config.stores:
        for item in config.items:
            on_hand[store][item] = int(
                round(data.baseline_demand[store][item] * config.store_start_stock_days)
            )

    counts = {
        'store_orders': 0, 'store_order_details': 0, 'store_receipts': 0,
        'sales_history': 0, 'supplier_orders': 0, 'supplier_order_details': 0,
        'supplier_receipts': 0, 'store_inventory': 0, 'dc_inventory': 0,
    }

    return SimState(
        on_hand=on_hand,
        on_order=on_order,
        receipt_schedule=[],
        store_receipt_schedule=[],
        demand_history=defaultdict(lambda: defaultdict(list)),
        open_store_orders=[],
        supplier_orders_buf=[],
        supplier_order_details_buf=[],
        supplier_receipts_buf=[],
        store_receipts_buf=[],
        po_seq=0,
        sr_seq=0,
        so_seq=0,
        receipt_seq=0,
        counts=counts,
        weekly_sales=defaultdict(lambda: defaultdict(float)),
        weekly_lost_sales=defaultdict(lambda: defaultdict(float)),
        rng=np.random.default_rng(config.seed),
    )
