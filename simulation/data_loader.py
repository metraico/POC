"""
simulation/data_loader.py — Load reference data from PostgreSQL and parquet.

Returns a frozen SimData dataclass with all read-only reference data
needed by the simulation. Closes DB connections when done.
"""

from dataclasses import dataclass
from typing import Dict

import numpy as np
import pandas as pd

from .config import SimConfig
from .connections import Connections


@dataclass(frozen=True)
class SimData:
    dc_cfg:       dict
    store_cfg:    dict
    supplier_cfg: dict

    # demand matrix: (store_id, item_id, date) -> requested_qty
    dm_idx: dict

    # item metadata
    velocity_map:   dict
    category_map:   dict
    unit_price_map: Dict[str, float]
    unit_cost_map:  Dict[str, float]

    # baseline daily demand: store -> item -> float
    baseline_demand: dict

    # assortment filters: node -> frozenset of item_ids
    store_items:    dict   # store_id -> frozenset[item_id]
    dc_items:       dict   # dc_id    -> frozenset[item_id]


def load_data(conns: Connections, config: SimConfig) -> SimData:
    pg_cur = conns.pg_conn.cursor()
    pg_cur.execute(
        "SELECT dc_configs, store_configs, supplier_configs FROM simulation_config "
        "WHERE simulation_id = %s",
        (config.sim_id,),
    )
    row = pg_cur.fetchone()
    if row is None:
        raise SystemExit(f"No simulation_config found for sim_id={config.sim_id}")

    dc_cfg_list, store_cfg_list, supplier_cfg_list = row[0], row[1], row[2]
    dc_cfg       = {d['dc_id']:       d for d in dc_cfg_list}
    store_cfg    = {s['store_id']:    s for s in store_cfg_list}
    supplier_cfg = {s['supplier_id']: s for s in supplier_cfg_list}

    # store_items and dc_items assortment filters
    from collections import defaultdict as _dd
    pg_cur.execute(
        "SELECT store_id, item_id FROM store_items WHERE store_id = ANY(%s)",
        (list(config.stores),)
    )
    _si: dict = _dd(set)
    for store_id, item_id in pg_cur.fetchall():
        _si[store_id].add(str(item_id))
    store_items = {k: frozenset(v) for k, v in _si.items()}

    pg_cur.execute(
        "SELECT dc_id, item_id FROM dc_items WHERE dc_id = ANY(%s)",
        (list(config.dcs),)
    )
    _di: dict = _dd(set)
    for dc_id, item_id in pg_cur.fetchall():
        _di[dc_id].add(str(item_id))
    dc_items = {k: frozenset(v) for k, v in _di.items()}
    pg_cur.close()

    items_df = pd.read_sql("SELECT * FROM items", conns.pg_engine)
    conns.pg_conn.close()
    conns.pg_engine.dispose()

    items_df = items_df.copy()
    items_df['item_id'] = items_df['item_id'].astype(str)
    velocity_map   = items_df.set_index('item_id')['velocity_class'].to_dict()
    category_map   = items_df.set_index('item_id')['category'].to_dict()
    unit_price_map = {k: float(v) for k, v in items_df.set_index('item_id')['unit_price'].items()}
    unit_cost_map  = {k: float(v) for k, v in items_df.set_index('item_id')['unit_cost'].items()}

    print("Loading demand matrix...")
    dm = pd.read_parquet('demand_matrix.parquet').copy()
    if hasattr(dm['date'].iloc[0], 'date'):
        dm['date'] = dm['date'].apply(lambda x: x.date() if hasattr(x, 'date') else x)
    dm['store_id'] = dm['store_id'].astype(str)
    dm['item_id']  = dm['item_id'].astype(str)
    dm_idx = dm.set_index(['store_id', 'item_id', 'date'])['requested_qty'].to_dict()

    # Compute baseline demand from first 28 days of simulation
    from datetime import timedelta
    first_28 = [config.start_date + timedelta(days=i) for i in range(28)]
    baseline_demand: dict = {}
    for store in config.stores:
        baseline_demand[store] = {}
        for item in config.items:
            vals = [int(dm_idx.get((store, item, d), 0)) for d in first_28]
            baseline_demand[store][item] = float(np.mean(vals)) if vals else 0.0

    return SimData(
        dc_cfg=dc_cfg,
        store_cfg=store_cfg,
        supplier_cfg=supplier_cfg,
        dm_idx=dm_idx,
        velocity_map=velocity_map,
        category_map=category_map,
        unit_price_map=unit_price_map,
        unit_cost_map=unit_cost_map,
        baseline_demand=baseline_demand,
        store_items=store_items,
        dc_items=dc_items,
    )
