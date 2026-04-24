"""
simulation/demand.py — Demand lookup and replenishment policy calculations.

Pure utility functions; no side effects on state except get_avg_daily
which reads (but does not write) state.demand_history.
"""

from datetime import date

import numpy as np

from .config import SimConfig
from .state import SimState


def iso_week_str(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def demand(dm_idx: dict, store: str, item: str, day: date) -> int:
    return int(dm_idx.get((store, item, day), 0))


def get_case_pack(config: SimConfig, item_id: str) -> int:
    cat = config.case_pack.get(item_id)
    if cat is not None:
        return cat
    # look up by category via the category_map stored in SimData
    # caller must pass category_map explicitly if needed; default to 'default'
    return config.case_pack.get('default', 1)


def get_case_pack_for_item(config: SimConfig, category_map: dict, item_id: str) -> int:
    cat = category_map.get(item_id, 'default')
    return config.case_pack.get(cat, config.case_pack.get('default', 1))


def get_avg_daily(state: SimState, config: SimConfig, baseline_demand: dict,
                  store: str, item: str) -> float:
    if config.policy == 'trailing_avg_28d':
        hist = state.demand_history[store][item]
        return float(np.mean(hist)) if hist else baseline_demand[store][item]
    return baseline_demand[store][item]
