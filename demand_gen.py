"""
demand_gen.py — Generate demand_matrix.parquet and write to ClickHouse demand table

Usage:
  python demand_gen.py --config config.yaml \
    --sim_id 30000000-0000-0000-0000-000000000001 \
    --account_id 10000000-0000-0000-0000-000000000001
"""

import argparse
import os
from datetime import date, timedelta

import clickhouse_connect
import numpy as np
import pandas as pd
import psycopg2
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── CLI ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument('--config',     default='configs/config.yaml')
parser.add_argument('--sim_id',     required=True)
parser.add_argument('--account_id', required=True)
args = parser.parse_args()

SIM_ID     = args.sim_id
ACCOUNT_ID = args.account_id

with open(args.config) as f:
    config = yaml.safe_load(f)

SEED = config['seed']
START_DATE = date.fromisoformat(config['start_date'])
END_DATE   = date.fromisoformat(config['end_date'])

# ── Read static data from PostgreSQL ─────────────────────────────────────────

conn = psycopg2.connect(
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

items_df  = pd.read_sql("SELECT * FROM items", conn)
stores_df = pd.read_sql("SELECT * FROM stores", conn)
promos_df = pd.read_sql(
    "SELECT * FROM promos WHERE simulation_id = %s",
    conn, params=(SIM_ID,)
)
pg_items  = pd.read_sql(
    "SELECT * FROM promo_group_items WHERE simulation_id = %s",
    conn, params=(SIM_ID,)
)
ps_stores = pd.read_sql(
    "SELECT * FROM promo_stores WHERE simulation_id = %s",
    conn, params=(SIM_ID,)
)
conn.close()

stores = sorted(stores_df['store_id'].astype(str).tolist())
items  = sorted(items_df['item_id'].astype(str).tolist())
n_stores = len(stores)
n_items  = len(items)

store_idx = {s: i for i, s in enumerate(stores)}
item_idx  = {it: i for i, it in enumerate(items)}

# Build date list
dates = []
d = START_DATE
while d <= END_DATE:
    dates.append(d)
    d += timedelta(days=1)
n_days = len(dates)

# ── Step 2 — Baseline daily demand ───────────────────────────────────────────

baseline = np.zeros((n_stores, n_items), dtype=np.float64)

_idf = items_df.assign(item_id=items_df['item_id'].astype(str))
velocity_map    = _idf.set_index('item_id')['velocity_class'].to_dict()
category_map    = _idf.set_index('item_id')['category'].to_dict()
subcategory_map = _idf.set_index('item_id')['subcategory'].to_dict()

# Default avg_daily_velocity table (overridden by config['velocity_avg_daily'])
_VELOCITY_DEFAULTS = {
    'Salty Snacks_FAST': 12,  'Salty Snacks_MEDIUM': 7,  'Salty Snacks_SLOW': 3,
    'Carbonated Soft Drinks_12 Pack_FAST': 20, 'Carbonated Soft Drinks_12 Pack_MEDIUM': 12,
    'Carbonated Soft Drinks_2 LTR_FAST': 16,  'Carbonated Soft Drinks_2 LTR_MEDIUM': 10,
    'default_FAST': 10, 'default_MEDIUM': 5, 'default_SLOW': 2,
}
_vel_map = {**_VELOCITY_DEFAULTS, **config.get('velocity_avg_daily', {})}

def _avg_daily(category, subcategory, velocity_class):
    vc = str(velocity_class).upper()
    return (  _vel_map.get(f"{category}_{subcategory}_{vc}")
           or _vel_map.get(f"{category}_{vc}")
           or _vel_map.get(f"default_{vc}", 5) )

for ii, item in enumerate(items):
    daily = float(_avg_daily(category_map[item], subcategory_map[item], velocity_map[item]))
    baseline[:, ii] = daily   # uniform across stores; store variation comes from noise

# ── Step 3 — Lifecycle multiplier array ──────────────────────────────────────

lifecycle_map = _idf.set_index('item_id')['lifecycle_profile'].to_dict()

lifecycle_arr = np.ones((n_items, n_days), dtype=np.float64)
day_indices   = np.arange(n_days, dtype=np.float64)

for ii, item in enumerate(items):
    lc = lifecycle_map[item]
    if lc == 'steady':
        pass
    elif lc == 'growth':
        ramp_end = min(90, n_days)
        ramp = 0.3 + (1.0 - 0.3) * (day_indices[:ramp_end] / 90.0)
        lifecycle_arr[ii, :ramp_end] = ramp
    elif lc == 'decay':
        decay_start = max(0, n_days - 90)
        n_decay = n_days - decay_start
        if n_decay > 0:
            decay_idx = np.arange(n_decay, dtype=np.float64)
            ramp = 1.0 + (0.3 - 1.0) * (decay_idx / max(n_decay - 1, 1))
            lifecycle_arr[ii, decay_start:] = ramp

# ── Step 4 — Annual seasonality multiplier ───────────────────────────────────

# Fallback hardcoded weekly indices (used when config has no annual_seasonality)
_SEASONAL_FALLBACK = {}
for _wr, _m in [
    (range(1,  5),  0.85), (range(5,  9),  0.88), (range(9,  14), 0.95),
    (range(14, 18), 1.00), (range(18, 23), 1.05), (range(23, 27), 1.08),
    (range(27, 31), 1.05), (range(31, 36), 1.00), (range(36, 40), 0.95),
    (range(40, 45), 1.10), (range(45, 49), 1.20), (range(49, 53), 1.35),
]:
    for _w in _wr:
        _SEASONAL_FALLBACK[_w] = _m

_annual_weeks = config.get('annual_seasonality', {}).get('weeks', {})
# YAML keys may be loaded as int or str — normalise to int
_annual_weeks = {int(k): float(v) for k, v in _annual_weeks.items()}

if _annual_weeks:
    seasonal_arr = np.array([
        _annual_weeks.get(d.isocalendar().week, 1.0) for d in dates
    ], dtype=np.float64)
else:
    seasonal_arr = np.array([
        _SEASONAL_FALLBACK.get(d.isocalendar().week, 1.0) for d in dates
    ], dtype=np.float64)

# ── Step 4b — Day-of-week seasonality multiplier ─────────────────────────────

_DOW_NAMES  = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
_weekly_cfg = config.get('weekly_seasonality', {})
if _weekly_cfg:
    dow_arr      = np.array([
        float(_weekly_cfg.get(_DOW_NAMES[d.weekday()], 1.0)) for d in dates
    ], dtype=np.float64)
    seasonal_arr = seasonal_arr * dow_arr

# ── Step 5 — Daily noise ─────────────────────────────────────────────────────

noise_arr = np.zeros((n_stores, n_items, n_days), dtype=np.float64)

for si in range(n_stores):
    for ii in range(n_items):
        for di in range(n_days):
            local_seed = SEED + ii * 1000 + si * 100 + di
            rng = np.random.default_rng(local_seed)
            noise_arr[si, ii, di] = rng.lognormal(0.0, 0.15)

# ── Step 6 — Promo multiplier ─────────────────────────────────────────────────

promo_arr    = np.ones((n_stores, n_items, n_days), dtype=np.float64)
promo_id_arr = np.full((n_stores, n_items, n_days), '', dtype=object)

date_to_idx = {d: i for i, d in enumerate(dates)}

for _, promo in promos_df.iterrows():
    promo_id_val     = str(promo['promo_id'])
    promo_item_ids   = pg_items[pg_items['promo_group_id'] == promo['promo_group_id']]['item_id'].astype(str).tolist()
    promo_store_ids  = ps_stores[ps_stores['promo_id'] == promo['promo_id']]['store_id'].astype(str).tolist()

    demand_mult = float(promo['demand_multiplier'])
    decay_days  = int(promo['post_promo_decay_days'])
    decay_shape = promo['post_promo_decay_shape']

    p_start = promo['start_date'] if isinstance(promo['start_date'], date) else promo['start_date'].date()
    p_end   = promo['end_date']   if isinstance(promo['end_date'], date)   else promo['end_date'].date()

    for store in promo_store_ids:
        if store not in store_idx:
            continue
        si = store_idx[store]
        for item_id in promo_item_ids:
            if item_id not in item_idx:
                continue
            ii = item_idx[item_id]

            pd_cur = p_start
            while pd_cur <= p_end:
                if pd_cur in date_to_idx:
                    di = date_to_idx[pd_cur]
                    promo_arr[si, ii, di]    = demand_mult
                    promo_id_arr[si, ii, di] = promo_id_val
                pd_cur += timedelta(days=1)

            if decay_shape == 'LINEAR' and decay_days > 0:
                for k in range(1, decay_days + 1):
                    decay_date = p_end + timedelta(days=k)
                    if decay_date in date_to_idx:
                        di = date_to_idx[decay_date]
                        mult = demand_mult * (1.0 - k / decay_days)
                        mult = max(1.0, mult)
                        promo_arr[si, ii, di]    = mult
                        promo_id_arr[si, ii, di] = promo_id_val

# ── Step 7 — Combine ─────────────────────────────────────────────────────────

base_3d = baseline[:, :, np.newaxis]
lc_3d   = lifecycle_arr[np.newaxis, :, :]
sea_3d  = seasonal_arr[np.newaxis, np.newaxis, :]

raw = base_3d * lc_3d * sea_3d * noise_arr * promo_arr
requested = np.maximum(0, np.round(raw)).astype(np.int64)

# ── Step 8 — Write parquet ────────────────────────────────────────────────────

store_col    = np.repeat(stores, n_items * n_days)
item_col     = np.tile(np.repeat(items, n_days), n_stores)
date_col     = np.tile(dates, n_stores * n_items)
qty_col      = requested.reshape(-1)
promo_id_col = promo_id_arr.reshape(-1)
is_promo_col = (promo_arr > 1.0).reshape(-1)

week_col = [f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}" for d in date_col]

df = pd.DataFrame({
    'store_id':     store_col,
    'item_id':      item_col,
    'date':         date_col,
    'requested_qty': qty_col,
})
df['requested_qty'] = df['requested_qty'].astype('int64')

df.to_parquet('demand_matrix.parquet', index=False)

total_rows = len(df)
zero_pct   = (df['requested_qty'] == 0).mean() * 100

print(f"Demand matrix written: {total_rows:,} rows")
print(f"Stores: {n_stores}")
print(f"Items:  {n_items}")
print(f"Days:   {n_days}")
print(f"Date range: {START_DATE} to {END_DATE}")
print(f"Daily demand stats:")
print(f"  mean:  {df['requested_qty'].mean():.1f} units")
print(f"  max:   {df['requested_qty'].max()} units")

# ── Step 9 — Write demand to ClickHouse ──────────────────────────────────────

print("Writing demand to ClickHouse...")

demand_df = pd.DataFrame({
    'simulation_id':   [str(SIM_ID)]     * total_rows,
    'account_id':      [str(ACCOUNT_ID)] * total_rows,
    'store_id':        [str(s) for s in store_col],
    'item_id':         [str(i) for i in item_col],
    'demand_date':     date_col,
    'demand_week':     week_col,
    'demand_qty':      qty_col.astype('float64'),
    'is_promo_demand': is_promo_col.astype(bool),
    'promo_id':        [str(p) for p in promo_id_col],
})

BATCH = 50000
for start in range(0, total_rows, BATCH):
    ch.insert_df('demand', demand_df.iloc[start:start + BATCH])
    print(f"  demand rows written: {min(start + BATCH, total_rows):,} / {total_rows:,}")

print("Demand written to ClickHouse.")
print(f"  zeros: {zero_pct:.1f}% of rows")
