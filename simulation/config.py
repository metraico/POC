"""
simulation/config.py — Load simulation config entirely from PostgreSQL.

All parameters are derived from the database using sim_id and account_id.
No YAML file required.
"""

import argparse
import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List


DAY_MAP = {
    'MONDAY': 0, 'TUESDAY': 1, 'WEDNESDAY': 2, 'THURSDAY': 3,
    'FRIDAY': 4, 'SATURDAY': 5, 'SUNDAY': 6,
}


@dataclass
class SimConfig:
    sim_id:     str
    account_id: str
    run_id:     str
    seed:       int
    policy:     str

    start_date: date
    end_date:   date

    stores: List[str]
    dcs:    List[str]
    items:  List[str]

    dc_assign:   Dict[str, List[str]]   # dc_id -> [store_id, ...]
    dc_supplier: Dict[str, str]         # dc_id -> supplier_id
    store_dc:    Dict[str, str]         # store_id -> dc_id
    item_line_num: Dict[str, int]       # item_id -> line number (1-based)

    day_map:       Dict[str, int]
    dc_review_dow: int

    smoothing_days:          int
    store_start_stock_days:  int
    case_pack:               dict


def load_config(pg_conn) -> SimConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument('--sim_id',     required=True)
    parser.add_argument('--account_id', required=True)
    args = parser.parse_args()

    sim_id     = args.sim_id
    account_id = args.account_id

    cur = pg_conn.cursor()

    # ── Simulation-level settings ─────────────────────────────────────────────
    cur.execute(
        "SELECT simulation_name, start_week, end_week, random_seed, "
        "       store_configs "
        "FROM simulation_config WHERE simulation_id = %s",
        (sim_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"No simulation_config found for sim_id={sim_id}")

    sim_name, start_week, end_week, random_seed, store_cfg_list = row

    # start_week / end_week stored as "YYYY-MM" → convert to dates
    sy, sm = int(start_week[:4]), int(start_week[5:7])
    ey, em = int(end_week[:4]),   int(end_week[5:7])
    start_date = date(sy, sm, 1)
    end_date   = date(ey, em, calendar.monthrange(ey, em)[1])

    # store_start_stock_days — take from first store config, default 5
    store_start_stock_days = 5
    if store_cfg_list:
        store_start_stock_days = store_cfg_list[0].get('start_stock_days', 5)

    # ── Network lists ─────────────────────────────────────────────────────────
    cur.execute("SELECT store_id FROM stores WHERE account_id = %s ORDER BY store_id", (account_id,))
    stores = [r[0] for r in cur.fetchall()]

    cur.execute("SELECT dc_id FROM distribution_centers WHERE account_id = %s ORDER BY dc_id", (account_id,))
    dcs = [r[0] for r in cur.fetchall()]

    cur.execute("SELECT item_id FROM items WHERE account_id = %s ORDER BY item_id", (account_id,))
    items = [r[0] for r in cur.fetchall()]

    # ── Assignments ───────────────────────────────────────────────────────────
    cur.execute("SELECT from_store_id, to_dc_id FROM store_mappings")
    dc_assign: Dict[str, List[str]] = defaultdict(list)
    store_dc:  Dict[str, str]       = {}
    for store_id, dc_id in cur.fetchall():
        if store_id in stores:
            dc_assign[dc_id].append(store_id)
            store_dc[store_id] = dc_id
    dc_assign = dict(dc_assign)

    cur.execute("SELECT from_dc_id, to_node FROM dc_mappings WHERE mapping_type = 'DC_SUPPLIER'")
    dc_supplier = {r[0]: r[1] for r in cur.fetchall() if r[0] in dcs}

    # ── Case pack sizes (by category, compatible with runner.py) ─────────────
    cur.execute("SELECT category, case_pack_size FROM items WHERE account_id = %s", (account_id,))
    cat_packs: Dict[str, List[int]] = defaultdict(list)
    for category, size in cur.fetchall():
        if size:
            cat_packs[category].append(size)

    case_pack = {'default': 6}
    for cat, sizes in cat_packs.items():
        # use the most common size for each category
        case_pack[cat] = max(set(sizes), key=sizes.count)

    cur.close()

    item_line_num  = {item: idx + 1 for idx, item in enumerate(items)}
    dc_review_dow  = DAY_MAP['MONDAY']

    return SimConfig(
        sim_id=sim_id,
        account_id=account_id,
        run_id=sim_name or sim_id[:8],
        seed=random_seed if random_seed is not None else 42,
        policy='trailing_avg_28d',
        start_date=start_date,
        end_date=end_date,
        stores=stores,
        dcs=dcs,
        items=items,
        dc_assign=dc_assign,
        dc_supplier=dc_supplier,
        store_dc=store_dc,
        item_line_num=item_line_num,
        day_map=DAY_MAP,
        dc_review_dow=dc_review_dow,
        smoothing_days=28,
        store_start_stock_days=store_start_stock_days,
        case_pack=case_pack,
    )
