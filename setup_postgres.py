import json
import os
from datetime import date, timedelta

import psycopg2
from psycopg2.extras import execute_batch

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PG_HOST = os.environ['PG_HOST']
PG_PORT = os.environ.get('PG_PORT', 5432)
PG_DB   = os.environ['PG_DB']
PG_USER = os.environ['PG_USER']
PG_PASS = os.environ['PG_PASSWORD']

conn = psycopg2.connect(
    host=PG_HOST, port=PG_PORT, dbname=PG_DB,
    user=PG_USER, password=PG_PASS,
    sslmode=os.environ.get('PG_SSLMODE', 'prefer')
)
conn.autocommit = True
cur = conn.cursor()

# ── Kill any blocking connections before dropping tables ──────────────────────

cur.execute("""
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE pid <> pg_backend_pid()
  AND datname = current_database()
  AND state IN ('idle', 'idle in transaction', 'idle in transaction (aborted)')
""")
print("Cleared blocking connections.")

# ── Drop existing tables (children first) ────────────────────────────────────

drops = [
    'promo_stores', 'promo_group_items', 'promos', 'promo_groups',
    'store_items', 'dc_items', 'supplier_items',
    'site_mappings', 'store_mappings', 'dc_mappings',
    'items', 'stores', 'distribution_centers', 'suppliers',
    'simulation_config',
    'currency', 'calendar_period', 'calendar',
    'retailer_accounts', 'accounts',
    'users',
]
for t in drops:
    cur.execute(f'DROP TABLE IF EXISTS {t} CASCADE')
    print(f"Dropped {t}")

# ── Create tables ─────────────────────────────────────────────────────────────

cur.execute("""
CREATE TABLE users (
  user_id    UUID PRIMARY KEY,
  username   VARCHAR,
  email      VARCHAR,
  created_at DATE
)
""")

cur.execute("""
CREATE TABLE retailer_accounts (
  account_id    UUID PRIMARY KEY,
  account_name  VARCHAR,
  account_type  VARCHAR,
  country_code  VARCHAR,
  region        VARCHAR,
  currency_code VARCHAR,
  is_active     BOOLEAN
)
""")

cur.execute("""
CREATE TABLE distribution_centers (
  dc_id        VARCHAR PRIMARY KEY,
  account_id   UUID REFERENCES retailer_accounts(account_id),
  dc_name      VARCHAR,
  country_code VARCHAR,
  dc_type      VARCHAR,
  region       VARCHAR,
  division     VARCHAR,
  district     VARCHAR
)
""")

cur.execute("""
CREATE TABLE stores (
  store_id     VARCHAR PRIMARY KEY,
  account_id   UUID REFERENCES retailer_accounts(account_id),
  store_name   VARCHAR,
  country_code VARCHAR,
  store_type   VARCHAR,
  region       VARCHAR,
  division     VARCHAR,
  district     VARCHAR
)
""")

cur.execute("""
CREATE TABLE suppliers (
  supplier_id      VARCHAR PRIMARY KEY,
  account_id       UUID REFERENCES retailer_accounts(account_id),
  supplier_name    VARCHAR,
  supplier_country VARCHAR,
  supplier_region  VARCHAR,
  category         VARCHAR
)
""")

cur.execute("""
CREATE TABLE dc_mappings (
  from_dc_id   VARCHAR,
  to_node      VARCHAR,
  mapping_type VARCHAR,
  PRIMARY KEY (from_dc_id, to_node, mapping_type)
)
""")

cur.execute("""
CREATE TABLE store_mappings (
  from_store_id VARCHAR,
  to_dc_id      VARCHAR,
  mapping_type  VARCHAR,
  PRIMARY KEY (from_store_id, to_dc_id)
)
""")

cur.execute("""
CREATE TABLE items (
  item_id           VARCHAR PRIMARY KEY,
  account_id        UUID REFERENCES retailer_accounts(account_id),
  item_description  VARCHAR,
  uom               VARCHAR,
  item_status       VARCHAR,
  category          VARCHAR,
  subcategory       VARCHAR,
  brand             VARCHAR,
  unit_cost         DECIMAL(10,2),
  unit_price        DECIMAL(10,2),
  velocity_class    VARCHAR,
  lifecycle_profile VARCHAR,
  case_pack_size    INT,
  size_group        VARCHAR,
  size_rank         INT,
  is_ecomm_eligible BOOLEAN
)
""")

cur.execute("""
CREATE TABLE store_items (
  store_id VARCHAR REFERENCES stores(store_id),
  item_id  VARCHAR REFERENCES items(item_id),
  PRIMARY KEY (store_id, item_id)
)
""")

cur.execute("""
CREATE TABLE dc_items (
  dc_id   VARCHAR REFERENCES distribution_centers(dc_id),
  item_id VARCHAR REFERENCES items(item_id),
  PRIMARY KEY (dc_id, item_id)
)
""")

cur.execute("""
CREATE TABLE supplier_items (
  supplier_id VARCHAR REFERENCES suppliers(supplier_id),
  item_id     VARCHAR REFERENCES items(item_id),
  PRIMARY KEY (supplier_id, item_id)
)
""")

cur.execute("""
CREATE TABLE simulation_config (
  simulation_id       UUID PRIMARY KEY,
  account_id          UUID REFERENCES retailer_accounts(account_id),
  simulation_name     VARCHAR,
  config_name         VARCHAR,
  created_by          UUID REFERENCES users(user_id),
  created_at          DATE,
  simulation_run_date DATE,
  simulation_status   VARCHAR,
  notes               VARCHAR,
  start_week          VARCHAR,
  end_week            VARCHAR,
  random_seed         INT,
  dc_configs          JSONB,
  store_configs       JSONB,
  supplier_configs    JSONB
)
""")

cur.execute("""
CREATE TABLE promo_groups (
  promo_group_id   UUID PRIMARY KEY,
  account_id       UUID REFERENCES retailer_accounts(account_id),
  simulation_id    UUID REFERENCES simulation_config(simulation_id),
  promo_group_name VARCHAR,
  category         VARCHAR,
  brand            VARCHAR,
  description      VARCHAR
)
""")

cur.execute("""
CREATE TABLE promos (
  promo_id               UUID PRIMARY KEY,
  account_id             UUID REFERENCES retailer_accounts(account_id),
  simulation_id          UUID REFERENCES simulation_config(simulation_id),
  promo_name             VARCHAR,
  promo_group_id         UUID REFERENCES promo_groups(promo_group_id),
  event_type             VARCHAR,
  start_date             DATE,
  end_date               DATE,
  demand_multiplier      DECIMAL(5,2),
  post_promo_decay_days  INT,
  post_promo_decay_shape VARCHAR
)
""")

cur.execute("""
CREATE TABLE promo_group_items (
  promo_group_id UUID REFERENCES promo_groups(promo_group_id),
  item_id        VARCHAR REFERENCES items(item_id),
  simulation_id  UUID REFERENCES simulation_config(simulation_id),
  PRIMARY KEY (promo_group_id, item_id, simulation_id)
)
""")

cur.execute("""
CREATE TABLE promo_stores (
  promo_id      UUID REFERENCES promos(promo_id),
  store_id      VARCHAR REFERENCES stores(store_id),
  simulation_id UUID REFERENCES simulation_config(simulation_id),
  PRIMARY KEY (promo_id, store_id, simulation_id)
)
""")

cur.execute("""
CREATE TABLE currency (
  currency_code        VARCHAR PRIMARY KEY,
  currency_name        VARCHAR,
  exchange_rate_to_usd DECIMAL(10,6),
  effective_date       DATE
)
""")

cur.execute("""
CREATE TABLE calendar (
  calendar_date   DATE PRIMARY KEY,
  week_id         VARCHAR,
  week_start_date DATE,
  month_id        VARCHAR,
  quarter_id      VARCHAR,
  year_id         INT,
  period_name     VARCHAR,
  period_number   INT,
  day_of_week     VARCHAR,
  is_weekend      BOOLEAN
)
""")

print("Tables created.")

# ── Seed data ─────────────────────────────────────────────────────────────────

# Fixed UUIDs — idempotent across runs
USER_UUID    = '20000000-0000-0000-0000-000000000001'
ACCOUNT_UUID = '10000000-0000-0000-0000-000000000001'
SIM_UUID     = '30000000-0000-0000-0000-000000000001'
PG_UUID      = '40000000-0000-0000-0000-000000000001'
PROMO_UUID   = '50000000-0000-0000-0000-000000000001'

# User
cur.execute("""
INSERT INTO users (user_id, username, email, created_at)
VALUES (%s, %s, %s, %s)
""", (USER_UUID, 'demo_user', 'demo@metrai.io', date(2024, 1, 1)))

# Retailer account
cur.execute("""
INSERT INTO retailer_accounts (account_id, account_name, account_type, country_code, region, currency_code, is_active)
VALUES (%s, %s, %s, %s, %s, %s, %s)
""", (ACCOUNT_UUID, 'Metrai Demo Retail', 'RETAILER', 'US', 'North America', 'USD', True))

# DCs
for dc_id, region in [('DC_01', 'East'), ('DC_02', 'West')]:
    cur.execute("""
    INSERT INTO distribution_centers (dc_id, account_id, dc_name, country_code, dc_type, region, division, district)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (dc_id, ACCOUNT_UUID, f'Distribution Center {dc_id}', 'US', 'REGIONAL_DC', region, region, region))

# Stores
for i in range(1, 11):
    store_id = f'Store_{i:03d}'
    region   = 'East' if i <= 5 else 'West'
    cur.execute("""
    INSERT INTO stores (store_id, account_id, store_name, country_code, store_type, region, division, district)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (store_id, ACCOUNT_UUID, f'Store {i:03d}', 'US', 'RETAIL', region, region, region))

# Suppliers
cur.execute("""
INSERT INTO suppliers (supplier_id, account_id, supplier_name, supplier_country, supplier_region, category)
VALUES (%s, %s, %s, %s, %s, %s)
""", ('SUP_001', ACCOUNT_UUID, 'Domestic Supplier', 'US', 'North America', 'Grocery'))

cur.execute("""
INSERT INTO suppliers (supplier_id, account_id, supplier_name, supplier_country, supplier_region, category)
VALUES (%s, %s, %s, %s, %s, %s)
""", ('SUP_002', ACCOUNT_UUID, 'International Supplier', 'CN', 'Asia Pacific', 'Apparel'))

# DC mappings — DC_01 → SUP_001, DC_02 → SUP_002
for dc_id, sup_id in [('DC_01', 'SUP_001'), ('DC_02', 'SUP_002')]:
    cur.execute("""
    INSERT INTO dc_mappings (from_dc_id, to_node, mapping_type) VALUES (%s, %s, %s)
    """, (dc_id, sup_id, 'DC_SUPPLIER'))

# Store mappings — stores 1-5 → DC_01, stores 6-10 → DC_02
for i in range(1, 6):
    cur.execute("INSERT INTO store_mappings (from_store_id, to_dc_id, mapping_type) VALUES (%s, %s, %s)",
                (f'Store_{i:03d}', 'DC_01', 'STORE_DC'))
for i in range(6, 11):
    cur.execute("INSERT INTO store_mappings (from_store_id, to_dc_id, mapping_type) VALUES (%s, %s, %s)",
                (f'Store_{i:03d}', 'DC_02', 'STORE_DC'))

# Items
item_configs = [
    # item_id, description, category, velocity, lifecycle, unit_price, case_pack
    ('ITEM_001', 'Grocery Item 001', 'Grocery', 'MEDIUM', 'steady',  1.99, 12),
    ('ITEM_002', 'Grocery Item 002', 'Grocery', 'MEDIUM', 'steady',  3.49, 12),
    ('ITEM_003', 'Grocery Item 003', 'Grocery', 'MEDIUM', 'growth',  4.99, 12),
    ('ITEM_004', 'Grocery Item 004', 'Grocery', 'MEDIUM', 'growth',  2.99, 12),
    ('ITEM_005', 'Grocery Item 005', 'Grocery', 'MEDIUM', 'decay',   8.99, 12),
    ('ITEM_006', 'Apparel Item 006', 'Apparel', 'MEDIUM', 'decay',  19.99,  1),
    ('ITEM_007', 'Apparel Item 007', 'Apparel', 'SLOW',   'steady', 29.99,  1),
    ('ITEM_008', 'Apparel Item 008', 'Apparel', 'SLOW',   'steady', 39.99,  1),
    ('ITEM_009', 'Apparel Item 009', 'Apparel', 'SLOW',   'growth', 24.99,  1),
    ('ITEM_010', 'Apparel Item 010', 'Apparel', 'SLOW',   'decay',  49.99,  1),
]

for item_id, desc, category, velocity, lifecycle, unit_price, case_pack in item_configs:
    cost_mult = 0.55 if category == 'Grocery' else 0.40
    unit_cost = round(unit_price * cost_mult, 2)
    cur.execute("""
    INSERT INTO items
      (item_id, account_id, item_description, uom, item_status, category, subcategory,
       brand, unit_cost, unit_price, velocity_class, lifecycle_profile,
       case_pack_size, size_group, size_rank, is_ecomm_eligible)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (item_id, ACCOUNT_UUID, desc, 'EA', 'ACTIVE', category, category, 'Generic',
          unit_cost, unit_price, velocity, lifecycle, case_pack, 'STD', 1,
          category == 'Apparel'))

all_store_ids = [f'Store_{i:03d}' for i in range(1, 11)]
all_item_ids  = [c[0] for c in item_configs]

# store_items: all 10 stores × all 10 items = 100 rows
execute_batch(cur,
    "INSERT INTO store_items (store_id, item_id) VALUES (%s, %s)",
    [(s, it) for s in all_store_ids for it in all_item_ids])

# dc_items: both DCs × all items = 20 rows
execute_batch(cur,
    "INSERT INTO dc_items (dc_id, item_id) VALUES (%s, %s)",
    [('DC_01', it) for it in all_item_ids] + [('DC_02', it) for it in all_item_ids])

# supplier_items: SUP_001 → grocery, SUP_002 → apparel
execute_batch(cur,
    "INSERT INTO supplier_items (supplier_id, item_id) VALUES (%s, %s)",
    [('SUP_001', it) for it in all_item_ids[:5]] +
    [('SUP_002', it) for it in all_item_ids[5:]])

# simulation_config
dc_configs = [
    {
        "dc_id": "DC_01",
        "on_time_rate": 0.90, "partial_delivery_rate": 0.08,
        "late_days_min": 1, "late_days_max": 3,
        "partial_frac_min": 0.70, "partial_frac_max": 0.90,
        "remainder_gap_min": 2, "remainder_gap_max": 5,
        "lead_time_min": 3, "lead_time_max": 7,
        "start_stock_days": 30,
        "reorder_point_weeks": 2, "safety_stock_weeks": 1,
        "target_stock_weeks": 8, "weeks_of_cover_threshold": 3,
    },
    {
        "dc_id": "DC_02",
        "on_time_rate": 0.80, "partial_delivery_rate": 0.15,
        "late_days_min": 2, "late_days_max": 5,
        "partial_frac_min": 0.55, "partial_frac_max": 0.80,
        "remainder_gap_min": 3, "remainder_gap_max": 7,
        "lead_time_min": 7, "lead_time_max": 14,
        "start_stock_days": 18,
        "reorder_point_weeks": 2, "safety_stock_weeks": 1,
        "target_stock_weeks": 8, "weeks_of_cover_threshold": 3,
    },
]
store_configs = [
    {
        "store_id": f"Store_{i:03d}", "order_cycle_day": "MONDAY",
        "reorder_point_weeks": 2, "target_stock_weeks": 6,
        "weeks_of_cover_threshold": 2,
    }
    for i in range(1, 11)
]
supplier_configs = [
    {
        "supplier_id": "SUP_001",
        "on_time_rate": 0.95, "partial_delivery_rate": 0.05,
        "late_days_min": 1, "late_days_max": 3,
        "partial_frac_min": 0.80, "partial_frac_max": 0.95,
        "remainder_gap_min": 2, "remainder_gap_max": 5,
        "lead_time_min": 3, "lead_time_max": 7,
    },
    {
        "supplier_id": "SUP_002",
        "on_time_rate": 0.75, "partial_delivery_rate": 0.25,
        "late_days_min": 2, "late_days_max": 7,
        "partial_frac_min": 0.55, "partial_frac_max": 0.80,
        "remainder_gap_min": 3, "remainder_gap_max": 10,
        "lead_time_min": 7, "lead_time_max": 14,
    },
]

cur.execute("""
INSERT INTO simulation_config
  (simulation_id, account_id, simulation_name, config_name, created_by,
   created_at, simulation_run_date, simulation_status,
   notes, start_week, end_week, random_seed,
   dc_configs, store_configs, supplier_configs)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
""", (SIM_UUID, ACCOUNT_UUID, 'Demo Simulation 2024', 'Demo Config v1',
      USER_UUID, date(2024, 1, 1), date(2024, 1, 1), 'COMPLETED',
      'Seed simulation for 2024 demo run', '2024-W01', '2024-W52', 42,
      json.dumps(dc_configs), json.dumps(store_configs), json.dumps(supplier_configs)))

# promo_groups
cur.execute("""
INSERT INTO promo_groups (promo_group_id, account_id, simulation_id, promo_group_name, category, brand, description)
VALUES (%s,%s,%s,%s,%s,%s,%s)
""", (PG_UUID, ACCOUNT_UUID, SIM_UUID,
      'Summer Grocery Promo', 'Grocery', 'Generic', 'Summer promotion for grocery items'))

# promos
cur.execute("""
INSERT INTO promos
  (promo_id, account_id, simulation_id, promo_name, promo_group_id,
   event_type, start_date, end_date, demand_multiplier, post_promo_decay_days, post_promo_decay_shape)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
""", (PROMO_UUID, ACCOUNT_UUID, SIM_UUID,
      'Summer Promo June 2024', PG_UUID,
      'SEASONAL', date(2024, 6, 1), date(2024, 6, 7), 2.5, 3, 'LINEAR'))

# promo_group_items — ITEM_001 and ITEM_002
for item_id in ['ITEM_001', 'ITEM_002']:
    cur.execute("INSERT INTO promo_group_items (promo_group_id, item_id, simulation_id) VALUES (%s,%s,%s)",
                (PG_UUID, item_id, SIM_UUID))

# promo_stores — stores 1-5
for i in range(1, 6):
    cur.execute("INSERT INTO promo_stores (promo_id, store_id, simulation_id) VALUES (%s,%s,%s)",
                (PROMO_UUID, f'Store_{i:03d}', SIM_UUID))

# Calendar — full year 2024
MONTH_NAMES = ['January','February','March','April','May','June',
               'July','August','September','October','November','December']
DAY_NAMES   = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']

cal_rows = []
d   = date(2024, 1, 1)
end = date(2024, 12, 31)
while d <= end:
    iso         = d.isocalendar()
    week_id     = f"{iso[0]}-W{iso[1]:02d}"
    week_start  = d - timedelta(days=d.weekday())
    quarter_num = (d.month - 1) // 3 + 1
    cal_rows.append((
        d, week_id, week_start,
        d.strftime('%Y-%m'),
        f"{d.year}-Q{quarter_num}",
        d.year,
        f"{MONTH_NAMES[d.month - 1]} {d.year}",
        d.month,
        DAY_NAMES[d.weekday()],
        d.weekday() >= 5,
    ))
    d += timedelta(days=1)

execute_batch(cur, """
INSERT INTO calendar
  (calendar_date, week_id, week_start_date, month_id, quarter_id, year_id,
   period_name, period_number, day_of_week, is_weekend)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
""", cal_rows)

# Currency
cur.execute("""
INSERT INTO currency (currency_code, currency_name, exchange_rate_to_usd, effective_date)
VALUES (%s,%s,%s,%s)
""", ('USD', 'US Dollar', 1.0, date(2024, 1, 1)))

cur.close()
conn.close()

print("Seed data inserted.")
print("PostgreSQL setup complete.")
print(f"\nUUIDs to use for CLI:")
print(f"  --account_id {ACCOUNT_UUID}")
print(f"  --sim_id     {SIM_UUID}")
