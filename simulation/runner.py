"""
simulation/runner.py — Main daily simulation loop.

Wires all step modules together. This is the only file that calls
receipts, demand fulfillment, replenishment, and snapshots in sequence.
"""

from datetime import timedelta

from .config import SimConfig
from .data_loader import SimData
from .demand import demand, get_avg_daily, iso_week_str
from .receipts import process_supplier_receipts, process_store_receipts
from .replenishment import place_store_orders, dc_allocate_to_stores, raise_dc_supplier_pos
from .snapshots import final_flush, print_summary, weekly_snapshot
from .state import SimState


def run(config: SimConfig, state: SimState, data: SimData, ch) -> None:
    current_date = config.start_date

    while current_date <= config.end_date:
        dow      = current_date.weekday()   # Monday=0, Sunday=6
        week_str = iso_week_str(current_date)

        if dow == 0:
            print(f"  {current_date}  ({week_str})")

        # Step 1: Supplier → DC receipts
        process_supplier_receipts(state, config, data, current_date)

        # Step 2: DC → Store receipts
        process_store_receipts(state, config, data, current_date)

        # Step 3 & 4: Update demand history, sell to customers
        for store in config.stores:
            store_assortment = data.store_items.get(store, frozenset())
            for item in config.items:
                if item not in store_assortment:
                    continue

                req = demand(data.dm_idx, store, item, current_date)

                # Step 3: rolling demand history
                hist = state.demand_history[store][item]
                hist.append(req)
                if len(hist) > config.smoothing_days:
                    hist.pop(0)

                # Step 4: sell to customers
                oh   = state.on_hand[store][item]
                sold = min(req, oh)
                state.on_hand[store][item] = max(0, oh - sold)
                state.weekly_sales[store][item]      += sold
                state.weekly_lost_sales[store][item] += (req - sold)

        # Step 5: Store order placement (on each store's order cycle day)
        place_store_orders(state, config, data, ch, current_date, dow, week_str)

        # Step 6: DC allocates open store orders (every day)
        dc_allocate_to_stores(state, config, data, current_date)

        # Step 7: DC raises supplier POs (on DC review day — Monday)
        raise_dc_supplier_pos(state, config, data, ch, current_date, dow)

        # Step 8: Weekly snapshot (Sundays)
        if dow == 6:
            weekly_snapshot(state, config, data, ch, current_date, week_str)

        current_date += timedelta(days=1)

    final_flush(state, ch)
    print_summary(config, state)
