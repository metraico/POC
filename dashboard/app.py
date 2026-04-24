"""
dashboard/app.py — Streamlit dashboard for retail supply chain simulation
"""

import io
import json
import os
import subprocess
import uuid
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv
import clickhouse_connect
import pandas as pd
import plotly.graph_objects as go
import psycopg2
import streamlit as st

load_dotenv(Path(__file__).parent.parent / ".env")

PROJECT_ROOT = str(Path(__file__).parent.parent)

def safe_int(val, default=0):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default
VENV_PYTHON  = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")

st.set_page_config(page_title="Retail Supply Chain Dashboard", layout="wide")

# ── Connections ───────────────────────────────────────────────────────────────

@st.cache_resource
def get_ch_client():
    return clickhouse_connect.get_client(
        host=os.environ['CH_HOST'],
        port=int(os.environ.get('CH_PORT', 8123)),
        database=os.environ['CH_DB'],
        username=os.environ['CH_USER'],
        password=os.environ['CH_PASSWORD'],
        verify=False
    )

_pg_conn = None

def get_pg():
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed != 0:
        _pg_conn = psycopg2.connect(
            host=os.environ['PG_HOST'],
            port=os.environ.get('PG_PORT', 5432),
            dbname=os.environ['PG_DB'],
            user=os.environ['PG_USER'],
            password=os.environ['PG_PASSWORD'],
            sslmode=os.environ.get('PG_SSLMODE', 'prefer')
        )
    return _pg_conn

client = get_ch_client()

# ── Session state defaults ────────────────────────────────────────────────────

for _k, _v in [('account', None), ('simulation', None), ('wizard', None),
               ('page', 'accounts'), ('confirm_delete', None), ('confirm_delete_sim', None),
               ('new_sim_state', None)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── Wizard helpers ────────────────────────────────────────────────────────────

STEPS = ["Account", "Network", "Items", "Promotions", "Sim Config", "Save & Run"]

def wizard_progress(current_step):
    cols = st.columns(len(STEPS))
    for i, label in enumerate(STEPS):
        n = i + 1
        if n < current_step:
            cols[i].markdown(f"<div style='text-align:center;color:green;font-size:12px'>✓ {label}</div>", unsafe_allow_html=True)
        elif n == current_step:
            cols[i].markdown(f"<div style='text-align:center;font-weight:bold;color:#0068C9;font-size:12px'>● {label}</div>", unsafe_allow_html=True)
        else:
            cols[i].markdown(f"<div style='text-align:center;color:grey;font-size:12px'>○ {label}</div>", unsafe_allow_html=True)
    st.divider()

# ── CSV sample templates (with example rows) ──────────────────────────────────

_SAMPLES = {
    'dcs': (
        "dc_id,dc_name,country_code,dc_type,region,division,district\n"
        "DC_01,East Distribution Center,US,REGIONAL_DC,East,Northeast,NY Metro\n"
        "DC_02,West Distribution Center,US,REGIONAL_DC,West,Pacific,LA Metro"
    ),
    'stores': (
        "store_id,store_name,country_code,store_type,region,division,district\n"
        "Store_001,Manhattan Flagship,US,RETAIL,East,Northeast,NYC\n"
        "Store_002,Brooklyn Store,US,RETAIL,East,Northeast,NYC\n"
        "Store_003,LA Downtown,US,RETAIL,West,Pacific,LA Metro\n"
        "Store_004,SF Union Square,US,RETAIL,West,Pacific,SF Bay"
    ),
    'suppliers': (
        "supplier_id,supplier_name,supplier_country,supplier_region,category\n"
        "SUP_001,US Grocery Co,US,North America,Grocery\n"
        "SUP_002,Asia Apparel Ltd,CN,Asia Pacific,Apparel"
    ),
    'store_mappings': (
        "from_store_id,to_dc_id\n"
        "Store_001,DC_01\n"
        "Store_002,DC_01\n"
        "Store_003,DC_02\n"
        "Store_004,DC_02"
    ),
    'dc_mappings': (
        "from_dc_id,to_node,mapping_type\n"
        "DC_01,SUP_001,DC_SUPPLIER\n"
        "DC_02,SUP_002,DC_SUPPLIER"
    ),
    'items': (
        "item_id,item_description,uom,item_status,category,subcategory,brand,"
        "unit_cost,unit_price,velocity_class,lifecycle_profile,case_pack_size,"
        "size_group,size_rank,is_ecomm_eligible\n"
        "ITEM_001,Organic Oats 500g,EA,ACTIVE,Grocery,Breakfast,Generic,"
        "1.10,1.99,MEDIUM,steady,12,STD,1,FALSE\n"
        "ITEM_002,Whole Milk 1L,EA,ACTIVE,Grocery,Dairy,Generic,"
        "1.92,3.49,MEDIUM,steady,12,STD,1,FALSE\n"
        "ITEM_003,Canned Tomatoes,EA,ACTIVE,Grocery,Canned,Generic,"
        "0.82,1.49,MEDIUM,growth,12,STD,1,FALSE\n"
        "ITEM_004,Cotton T-Shirt S,EA,ACTIVE,Apparel,Tops,Generic,"
        "12.00,29.99,SLOW,growth,1,STD,1,TRUE\n"
        "ITEM_005,Denim Jeans 32,EA,ACTIVE,Apparel,Bottoms,Generic,"
        "22.00,59.99,SLOW,decay,1,STD,1,TRUE"
    ),
    'store_items': (
        "store_id,item_id\n"
        "Store_001,ITEM_001\n"
        "Store_001,ITEM_002\n"
        "Store_002,ITEM_001\n"
        "Store_002,ITEM_004"
    ),
    'dc_items': (
        "dc_id,item_id\n"
        "DC_01,ITEM_001\n"
        "DC_01,ITEM_002\n"
        "DC_01,ITEM_003\n"
        "DC_02,ITEM_004\n"
        "DC_02,ITEM_005"
    ),
    'supplier_items': (
        "supplier_id,item_id\n"
        "SUP_001,ITEM_001\n"
        "SUP_001,ITEM_002\n"
        "SUP_001,ITEM_003\n"
        "SUP_002,ITEM_004\n"
        "SUP_002,ITEM_005"
    ),
    'promo_groups': (
        "promo_group_name,category,brand,description\n"
        "Summer Grocery Promo,Grocery,Generic,Seasonal summer promotion for grocery items\n"
        "Back to School,Apparel,Generic,Back to school apparel promotion"
    ),
    'promos': (
        "promo_name,promo_group_name,event_type,start_date,end_date,"
        "demand_multiplier,post_promo_decay_days,post_promo_decay_shape\n"
        "Summer Sale,Summer Grocery Promo,SEASONAL,2024-06-01,2024-06-07,2.5,3,LINEAR\n"
        "Back to School Aug,Back to School,SEASONAL,2024-08-15,2024-08-25,1.8,5,LINEAR"
    ),
    'promo_group_items': (
        "promo_group_name,item_id\n"
        "Summer Grocery Promo,ITEM_001\n"
        "Summer Grocery Promo,ITEM_002\n"
        "Back to School,ITEM_004"
    ),
    'promo_stores': (
        "promo_name,store_id\n"
        "Summer Sale,Store_001\n"
        "Summer Sale,Store_002\n"
        "Back to School Aug,Store_003"
    ),
}

def _wz_upload(label, wz_key, required_cols, optional=False):
    """Upload widget with sample template download. Returns True if data is loaded."""
    wz = st.session_state.wizard
    sample = _SAMPLES.get(wz_key, ','.join(required_cols))

    c1, c2 = st.columns([1, 4])
    with c1:
        st.download_button(
            "⬇ Download template",
            sample.encode(),
            file_name=f"{wz_key}_template.csv",
            mime="text/csv",
            key=f"dl_{wz_key}",
        )
    if optional:
        with c2:
            st.caption("Optional — skip if not needed")

    uploaded = st.file_uploader(f"Upload {label} CSV", type="csv", key=f"up_{wz_key}")
    if uploaded:
        try:
            df = pd.read_csv(uploaded)
        except Exception as e:
            st.error(f"Could not parse CSV: {e}")
            return bool(wz.get(wz_key))
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            st.error(f"Missing columns: {', '.join(missing)}")
            return bool(wz.get(wz_key))
        if df.empty:
            st.error("CSV has no rows.")
            return bool(wz.get(wz_key))
        wz[wz_key] = df.to_dict('records')
        st.success(f"{len(df)} rows loaded.")

    if wz.get(wz_key):
        st.dataframe(pd.DataFrame(wz[wz_key]), use_container_width=True, hide_index=True)
        return True
    return False

def run_script(script_name, extra_args, output_placeholder):
    env = {**os.environ,
           'PG_HOST':     os.environ.get('PG_HOST', 'localhost'),
           'PG_PORT':     os.environ.get('PG_PORT', '5432'),
           'PG_DB':       os.environ.get('PG_DB', ''),
           'PG_USER':     os.environ.get('PG_USER', ''),
           'PG_PASSWORD': os.environ.get('PG_PASSWORD', ''),
           'CH_HOST':     os.environ.get('CH_HOST', ''),
           'CH_PORT':     os.environ.get('CH_PORT', '8443'),
           'CH_DB':       os.environ.get('CH_DB', ''),
           'CH_USER':     os.environ.get('CH_USER', ''),
           'CH_PASSWORD': os.environ.get('CH_PASSWORD', '')}
    cmd = [VENV_PYTHON, script_name] + extra_args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=PROJECT_ROOT, env=env)
    output_lines = []
    for line in proc.stdout:
        output_lines.append(line)
        output_placeholder.code(''.join(output_lines))
    proc.wait()
    return proc.returncode

def build_config(wz):
    stores    = [r['store_id']    for r in wz.get('stores', [])]
    dcs       = [r['dc_id']       for r in wz.get('dcs', [])]
    items     = [r['item_id']     for r in wz.get('items', [])]
    suppliers = [r['supplier_id'] for r in wz.get('suppliers', [])]

    # dc_assignment: {dc_id: [store_ids]} from store_mappings
    dc_assignment = {}
    for m in wz.get('store_mappings', []):
        dc_assignment.setdefault(m['to_dc_id'], []).append(m['from_store_id'])

    # dc_supplier_assignment: {dc_id: supplier_id} from dc_mappings
    dc_supplier = {}
    for m in wz.get('dc_mappings', []):
        dc_supplier[m['from_dc_id']] = m['to_node']

    sim_cfg = wz.get('sim_config', {})

    return {
        'seed':    int(sim_cfg.get('random_seed', 42)),
        'run_id':  'RUN01',
        'start_date': str(sim_cfg.get('start_date', '2024-01-01')),
        'end_date':   str(sim_cfg.get('end_date',   '2024-12-31')),
        'stores':    stores,
        'dcs':       dcs,
        'suppliers': suppliers,
        'items':     items,
        'dc_assignment':          dc_assignment,
        'dc_supplier_assignment': dc_supplier,
        'velocity_mix':           {'medium': 0.60, 'slow': 0.40},
        'replenishment_policy':   'trailing_avg_28d',
        'store_coverage_days':    {'medium': 10, 'slow': 14},
        'dc_coverage_days':       28,
        'demand_smoothing_window_days': 28,
        'dc_review_dow':          'Monday',
        'store_start_stock_days': 5,
        'case_pack_sizes':        {'Grocery': 12, 'Apparel': 1, 'default': 6},
        'inventory_snapshot_dow': 'Sunday',
        'dc_to_store_same_day':   True,
        'velocity_avg_daily': {
            'Salty Snacks_FAST': 12, 'Salty Snacks_MEDIUM': 7, 'Salty Snacks_SLOW': 3,
            'Carbonated Soft Drinks_12 Pack_FAST': 20, 'Carbonated Soft Drinks_12 Pack_MEDIUM': 12,
            'Carbonated Soft Drinks_2 LTR_FAST': 16,  'Carbonated Soft Drinks_2 LTR_MEDIUM': 10,
            'default_FAST': 10, 'default_MEDIUM': 5, 'default_SLOW': 2,
        },
        'annual_seasonality': {
            'type': 'weekly_index',
            'weeks': {
                1:1.059, 2:0.883, 3:0.924, 4:0.923, 5:1.046, 6:1.323, 7:0.834,
                8:0.863, 9:0.858, 10:0.919, 11:0.881, 12:0.950, 13:0.980, 14:0.990,
                15:1.010, 16:1.020, 17:1.030, 18:1.040, 19:1.050, 20:1.060, 21:1.059,
                22:1.059, 23:1.059, 24:1.059, 25:1.068, 26:1.105, 27:1.440, 28:1.015,
                29:1.062, 30:1.016, 31:1.042, 32:1.082, 33:1.043, 34:1.006, 35:1.073,
                36:1.100, 37:0.978, 38:0.950, 39:0.922, 40:0.972, 41:0.976, 42:0.978,
                43:0.965, 44:0.994, 45:0.928, 46:0.906, 47:0.946, 48:1.033, 49:0.908,
                50:1.043, 51:1.040, 52:1.046,
            },
        },
        'weekly_seasonality': {
            'monday': 1.00, 'tuesday': 1.00, 'wednesday': 1.02,
            'thursday': 1.03, 'friday': 1.08, 'saturday': 1.12, 'sunday': 1.05,
        },
    }

def _wz_cancel_cleanup(wz):
    """Delete any data already saved to Postgres for this wizard session."""
    account_id = wz.get('account', {}).get('account_id')
    if not account_id:
        return  # nothing was saved yet
    try:
        cur = get_pg().cursor()
        cur.execute("DELETE FROM retailer_accounts WHERE account_id = %s", (account_id,))
        get_pg().commit()
        cur.close()
    except Exception:
        try:
            get_pg().rollback()
        except Exception:
            pass


def _wz_save_all(wz):
    """Save all wizard data to Postgres in FK order."""
    new_account_id = str(uuid.uuid4())
    new_sim_id     = str(uuid.uuid4())
    wz['account']['account_id'] = new_account_id
    wz['account']['sim_id']     = new_sim_id

    cur  = get_pg().cursor()
    acct = wz['account']

    # 1. retailer_accounts
    cur.execute("""
        INSERT INTO retailer_accounts
          (account_id, account_name, account_type, country_code, region, currency_code, is_active)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, (new_account_id, acct['account_name'], acct.get('account_type','RETAILER'),
          acct.get('country_code','US'), acct.get('region',''),
          acct.get('currency_code','USD'), True))

    # 2. distribution_centers
    for d in wz.get('dcs', []):
        cur.execute("""
            INSERT INTO distribution_centers
              (dc_id, account_id, dc_name, country_code, dc_type, region, division, district)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (d['dc_id'], new_account_id, d['dc_name'],
              d.get('country_code',''), d.get('dc_type','REGIONAL_DC'),
              d.get('region',''), d.get('division',''), d.get('district','')))

    # 3. stores
    for s in wz.get('stores', []):
        cur.execute("""
            INSERT INTO stores
              (store_id, account_id, store_name, country_code, store_type, region, division, district)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (s['store_id'], new_account_id, s['store_name'],
              s.get('country_code',''), s.get('store_type','RETAIL'),
              s.get('region',''), s.get('division',''), s.get('district','')))

    # 4. suppliers
    for s in wz.get('suppliers', []):
        cur.execute("""
            INSERT INTO suppliers
              (supplier_id, account_id, supplier_name, supplier_country, supplier_region, category)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (s['supplier_id'], new_account_id, s['supplier_name'],
              s.get('supplier_country',''), s.get('supplier_region',''),
              s.get('category','')))

    # 5. items
    for i in wz.get('items', []):
        cur.execute("""
            INSERT INTO items
              (item_id, account_id, item_description, uom, item_status, category, subcategory,
               brand, unit_cost, unit_price, velocity_class, lifecycle_profile,
               case_pack_size, size_group, size_rank, is_ecomm_eligible)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (i['item_id'], new_account_id, i['item_description'],
              i.get('uom','EA'), i.get('item_status','ACTIVE'), i['category'],
              i.get('subcategory', i['category']), i.get('brand','Generic'),
              float(i['unit_cost']), float(i['unit_price']),
              str(i['velocity_class']).upper(), str(i['lifecycle_profile']).lower(),
              safe_int(i['case_pack_size'], 1), i.get('size_group','STD'),
              safe_int(i.get('size_rank', 1), 1), bool(str(i.get('is_ecomm_eligible','FALSE')).upper() == 'TRUE')))

    # 6. store_items (upload or default: all stores × all items)
    store_items = wz.get('store_items')
    if store_items:
        for si in store_items:
            cur.execute("INSERT INTO store_items (store_id,item_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (si['store_id'], si['item_id']))
    else:
        for s in wz.get('stores', []):
            for i in wz.get('items', []):
                cur.execute("INSERT INTO store_items (store_id,item_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                            (s['store_id'], i['item_id']))

    # 7. dc_items (upload or default: all DCs × all items)
    dc_items = wz.get('dc_items')
    if dc_items:
        for di in dc_items:
            cur.execute("INSERT INTO dc_items (dc_id,item_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (di['dc_id'], di['item_id']))
    else:
        for d in wz.get('dcs', []):
            for i in wz.get('items', []):
                cur.execute("INSERT INTO dc_items (dc_id,item_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                            (d['dc_id'], i['item_id']))

    # 8. supplier_items
    for si in wz.get('supplier_items', []):
        cur.execute("INSERT INTO supplier_items (supplier_id,item_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (si['supplier_id'], si['item_id']))

    # 9. store_mappings
    for m in wz.get('store_mappings', []):
        cur.execute("INSERT INTO store_mappings (from_store_id,to_dc_id,mapping_type) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (m['from_store_id'], m['to_dc_id'], 'STORE_DC'))

    # 10. dc_mappings
    for m in wz.get('dc_mappings', []):
        cur.execute("INSERT INTO dc_mappings (from_dc_id,to_node,mapping_type) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (m['from_dc_id'], m['to_node'], m.get('mapping_type','DC_SUPPLIER')))

    # 11. simulation_config (must exist before promos due to FK)
    sim_cfg = wz.get('sim_config', {})
    start_str = str(sim_cfg.get('start_date', '2024-01-01'))
    end_str   = str(sim_cfg.get('end_date',   '2024-12-31'))

    cur.execute("""
        INSERT INTO simulation_config
          (simulation_id, account_id, simulation_name, config_name,
           created_at, simulation_status, start_week, end_week, random_seed,
           dc_configs, store_configs, supplier_configs)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
    """, (new_sim_id, new_account_id,
          sim_cfg.get('simulation_name', acct['account_name'] + ' Simulation'),
          'Default Config',
          date.today(), 'PENDING',
          start_str[:7], end_str[:7],
          int(sim_cfg.get('random_seed', 42)),
          json.dumps(wz.get('dc_configs', [])),
          json.dumps(wz.get('store_configs', [])),
          json.dumps(wz.get('supplier_configs', []))))

    # 12. promos (resolve names → UUIDs)
    pg_name_to_uuid    = {}
    promo_name_to_uuid = {}

    for pg in wz.get('promo_groups', []):
        pg_uuid = str(uuid.uuid4())
        pg_name_to_uuid[pg['promo_group_name']] = pg_uuid
        cur.execute("""
            INSERT INTO promo_groups
              (promo_group_id, account_id, simulation_id, promo_group_name, category, brand, description)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (pg_uuid, new_account_id, new_sim_id,
              pg['promo_group_name'], pg.get('category',''), pg.get('brand',''), pg.get('description','')))

    for p in wz.get('promos', []):
        p_uuid  = str(uuid.uuid4())
        promo_name_to_uuid[p['promo_name']] = p_uuid
        pg_uuid = pg_name_to_uuid.get(p.get('promo_group_name',''), None)
        cur.execute("""
            INSERT INTO promos
              (promo_id, account_id, simulation_id, promo_name, promo_group_id,
               event_type, start_date, end_date, demand_multiplier,
               post_promo_decay_days, post_promo_decay_shape)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (p_uuid, new_account_id, new_sim_id,
              p['promo_name'], pg_uuid,
              p.get('event_type','SEASONAL'),
              p.get('start_date'), p.get('end_date'),
              float(p.get('demand_multiplier', 1.5)),
              safe_int(p.get('post_promo_decay_days', 0), 0),
              p.get('post_promo_decay_shape','LINEAR')))

    for pgi in wz.get('promo_group_items', []):
        pg_uuid = pg_name_to_uuid.get(pgi.get('promo_group_name',''), None)
        if pg_uuid:
            cur.execute("""
                INSERT INTO promo_group_items (promo_group_id, item_id, simulation_id)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
            """, (pg_uuid, pgi['item_id'], new_sim_id))

    for ps in wz.get('promo_stores', []):
        p_uuid = promo_name_to_uuid.get(ps.get('promo_name',''), None)
        if p_uuid:
            cur.execute("""
                INSERT INTO promo_stores (promo_id, store_id, simulation_id)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
            """, (p_uuid, ps['store_id'], new_sim_id))

    get_pg().commit()
    cur.close()

# ── Page: New Account Wizard ──────────────────────────────────────────────────

def _dc_config_defaults(dcs):
    return [
        {
            'dc_id': d['dc_id'],
            'lead_time_min': 3, 'lead_time_max': 7,
            'on_time_rate': 0.90, 'partial_delivery_rate': 0.08,
            'late_days_min': 1, 'late_days_max': 3,
            'partial_frac_min': 0.70, 'partial_frac_max': 0.90,
            'remainder_gap_min': 2, 'remainder_gap_max': 5,
            'start_stock_days': 28,
            'reorder_point_weeks': 2, 'safety_stock_weeks': 1,
            'target_stock_weeks': 8, 'weeks_of_cover_threshold': 3,
        }
        for d in dcs
    ]

def _store_config_defaults(stores):
    return [
        {
            'store_id': s['store_id'],
            'order_cycle_day': 'MONDAY',
            'order_cycle_days': 7,
            'coverage_days_medium': 10,
            'coverage_days_slow': 14,
            'start_stock_days': 5,
            'reorder_point_weeks': 1,
            'target_stock_weeks': 2,
            'weeks_of_cover_threshold': 1,
        }
        for s in stores
    ]

def _supplier_config_defaults(suppliers):
    return [
        {
            'supplier_id': s['supplier_id'],
            'lead_time_min': 7, 'lead_time_max': 14,
            'on_time_rate': 0.90, 'partial_delivery_rate': 0.10,
            'late_days_min': 1, 'late_days_max': 5,
            'partial_frac_min': 0.60, 'partial_frac_max': 0.90,
            'remainder_gap_min': 2, 'remainder_gap_max': 7,
        }
        for s in suppliers
    ]

def new_account_page():
    wz = st.session_state.wizard

    col_title, col_cancel = st.columns([5, 1])
    col_title.title("New Account Setup")
    if col_cancel.button("✕ Cancel", use_container_width=True):
        _wz_cancel_cleanup(wz)
        st.session_state.wizard = None
        st.rerun()

    # ── Completion flags ──────────────────────────────────────────────────────
    has_account = bool(wz.get('account'))
    has_network = bool(wz.get('dcs') and wz.get('stores') and wz.get('suppliers')
                       and wz.get('store_mappings') and wz.get('dc_mappings'))
    has_items   = bool(wz.get('items') and wz.get('supplier_items'))
    has_promos  = wz.get('promo_groups') is not None   # empty list = skipped/done
    has_sim_cfg = bool(wz.get('sim_config'))

    def _lbl(title, done, optional=False):
        mark = "✓" if done else "●" if not optional else "○"
        suffix = " (optional)" if optional else ""
        return f"{mark}  {title}{suffix}"

    # ── 1. Account ────────────────────────────────────────────────────────────
    with st.expander(_lbl("Account", has_account), expanded=not has_account):
        saved = wz.get('account', {})
        for k, v in [('wz_acc_name',     saved.get('account_name',  '')),
                     ('wz_acc_type',     saved.get('account_type',  'RETAILER')),
                     ('wz_acc_region',   saved.get('region',        '')),
                     ('wz_acc_country',  saved.get('country_code',  'US')),
                     ('wz_acc_currency', saved.get('currency_code', 'USD'))]:
            if k not in st.session_state:
                st.session_state[k] = v

        c1, c2 = st.columns(2)
        c1.text_input("Account Name *", key='wz_acc_name', placeholder="My Retail Co.")
        c2.selectbox("Account Type", ["RETAILER", "WHOLESALER", "DISTRIBUTOR"], key='wz_acc_type')
        c3, c4, c5 = st.columns(3)
        c3.text_input("Region *",       key='wz_acc_region',  placeholder="North America")
        c4.text_input("Country Code *", key='wz_acc_country', placeholder="US", max_chars=2)
        c5.selectbox("Currency", ["USD", "EUR", "GBP", "INR", "AUD"], key='wz_acc_currency')

        if st.button("Confirm Account", type="primary"):
            name   = st.session_state.wz_acc_name.strip()
            region = st.session_state.wz_acc_region.strip()
            if not name or not region:
                st.error("Account Name and Region are required.")
            else:
                wz['account'] = {
                    'account_name':  name,
                    'account_type':  st.session_state.wz_acc_type,
                    'region':        region,
                    'country_code':  (st.session_state.wz_acc_country.strip().upper() or 'US'),
                    'currency_code': st.session_state.wz_acc_currency,
                }
                st.rerun()

    # ── 2. Network ────────────────────────────────────────────────────────────
    with st.expander(_lbl("Network", has_network), expanded=has_account and not has_network):
        st.caption("Upload all five CSVs. Download templates for the expected format.")

        _wz_upload("Distribution Centers", 'dcs',
                   ['dc_id','dc_name','country_code','dc_type','region','division','district'])
        st.divider()
        _wz_upload("Stores", 'stores',
                   ['store_id','store_name','country_code','store_type','region','division','district'])
        st.divider()
        _wz_upload("Suppliers", 'suppliers',
                   ['supplier_id','supplier_name','supplier_country','supplier_region','category'])
        st.divider()
        _wz_upload("Store → DC Mappings", 'store_mappings', ['from_store_id','to_dc_id'])
        st.divider()
        _wz_upload("DC → Supplier Mappings", 'dc_mappings', ['from_dc_id','to_node','mapping_type'])

        if has_network:
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("DCs",            len(wz.get('dcs',            [])))
            m2.metric("Stores",         len(wz.get('stores',         [])))
            m3.metric("Suppliers",      len(wz.get('suppliers',      [])))
            m4.metric("Store Mappings", len(wz.get('store_mappings', [])))
            m5.metric("DC Mappings",    len(wz.get('dc_mappings',    [])))

    # ── 3. Items ──────────────────────────────────────────────────────────────
    with st.expander(_lbl("Items", has_items), expanded=has_network and not has_items):
        st.markdown("**Item Catalog** (required)")
        _wz_upload("Items", 'items',
            ['item_id','item_description','uom','item_status','category','subcategory','brand',
             'unit_cost','unit_price','velocity_class','lifecycle_profile','case_pack_size',
             'size_group','size_rank','is_ecomm_eligible'])
        st.divider()

        st.markdown("**Store ↔ Item Assignments** (optional — defaults to all stores × all items)")
        _wz_upload("Store Items", 'store_items', ['store_id','item_id'], optional=True)
        st.divider()

        st.markdown("**DC ↔ Item Assignments** (optional — defaults to all DCs × all items)")
        _wz_upload("DC Items", 'dc_items', ['dc_id','item_id'], optional=True)
        st.divider()

        st.markdown("**Supplier ↔ Item Assignments** (required)")
        _wz_upload("Supplier Items", 'supplier_items', ['supplier_id','item_id'])

    # ── 4. Promotions ─────────────────────────────────────────────────────────
    with st.expander(_lbl("Promotions", has_promos, optional=True),
                     expanded=has_items and not has_promos):
        st.caption("All sections are optional. Click Skip to proceed without promotions.")

        st.markdown("**Promo Groups**")
        _wz_upload("Promo Groups", 'promo_groups',
                   ['promo_group_name','category','brand','description'], optional=True)
        st.divider()

        st.markdown("**Promos**")
        _wz_upload("Promos", 'promos',
                   ['promo_name','promo_group_name','event_type','start_date','end_date',
                    'demand_multiplier','post_promo_decay_days','post_promo_decay_shape'],
                   optional=True)
        st.divider()

        st.markdown("**Promo Group Items**")
        _wz_upload("Promo Group Items", 'promo_group_items',
                   ['promo_group_name','item_id'], optional=True)
        st.divider()

        st.markdown("**Promo Stores**")
        _wz_upload("Promo Stores", 'promo_stores', ['promo_name','store_id'], optional=True)

        st.divider()
        col_skip, col_done = st.columns(2)
        if col_skip.button("Skip Promotions"):
            wz.setdefault('promo_groups', [])
            wz.setdefault('promos', [])
            wz.setdefault('promo_group_items', [])
            wz.setdefault('promo_stores', [])
            st.rerun()
        if col_done.button("Done with Promotions", type="primary"):
            wz.setdefault('promo_groups', wz.get('promo_groups', []))
            st.rerun()

    # ── 5. Sim Config ─────────────────────────────────────────────────────────
    with st.expander(_lbl("Sim Config", has_sim_cfg),
                     expanded=has_items and has_promos and not has_sim_cfg):
        saved_cfg = wz.get('sim_config', {})
        for k, v in [('wz_sim_name',  saved_cfg.get('simulation_name',
                                          (wz.get('account') or {}).get('account_name','') + ' Simulation')),
                     ('wz_start_dt',  saved_cfg.get('start_date', '2024-01-01')),
                     ('wz_end_dt',    saved_cfg.get('end_date',   '2024-12-31')),
                     ('wz_rand_seed', saved_cfg.get('random_seed', 42))]:
            if k not in st.session_state:
                st.session_state[k] = v

        c1, c2, c3, c4 = st.columns(4)
        c1.text_input("Simulation Name",          key='wz_sim_name')
        c2.text_input("Start Date (YYYY-MM-DD)",  key='wz_start_dt')
        c3.text_input("End Date (YYYY-MM-DD)",    key='wz_end_dt')
        c4.number_input("Random Seed", min_value=0, key='wz_rand_seed')

        st.divider()
        st.markdown("**DC Reliability & Stock Configs**")
        if not wz.get('dc_configs'):
            wz['dc_configs'] = _dc_config_defaults(wz.get('dcs', []))
        dc_edited = st.data_editor(pd.DataFrame(wz['dc_configs']),
                                   use_container_width=True, num_rows="fixed",
                                   key='wz_dc_cfg_editor')
        wz['dc_configs'] = dc_edited.to_dict('records')

        st.divider()
        st.markdown("**Store Replenishment Configs**")
        if not wz.get('store_configs'):
            wz['store_configs'] = _store_config_defaults(wz.get('stores', []))
        st_edited = st.data_editor(pd.DataFrame(wz['store_configs']),
                                   use_container_width=True, num_rows="fixed",
                                   key='wz_st_cfg_editor')
        wz['store_configs'] = st_edited.to_dict('records')

        st.divider()
        st.markdown("**Supplier Reliability Configs**")
        if not wz.get('supplier_configs'):
            wz['supplier_configs'] = _supplier_config_defaults(wz.get('suppliers', []))
        sup_edited = st.data_editor(pd.DataFrame(wz['supplier_configs']),
                                    use_container_width=True, num_rows="fixed",
                                    key='wz_sup_cfg_editor')
        wz['supplier_configs'] = sup_edited.to_dict('records')

        st.divider()
        if st.button("Save Sim Config", type="primary"):
            wz['sim_config'] = {
                'simulation_name': st.session_state.wz_sim_name.strip(),
                'start_date':      st.session_state.wz_start_dt.strip(),
                'end_date':        st.session_state.wz_end_dt.strip(),
                'random_seed':     int(st.session_state.wz_rand_seed),
            }
            st.rerun()

    # ── 6. Save & Run ─────────────────────────────────────────────────────────
    can_save = has_account and has_network and has_items and has_sim_cfg
    with st.expander(_lbl("Save & Run", wz.get('sim_done', False)),
                     expanded=can_save and not wz.get('sim_done')):

        if not can_save:
            missing = []
            if not has_account: missing.append("Account")
            if not has_network: missing.append("Network")
            if not has_items:   missing.append("Items")
            if not has_sim_cfg: missing.append("Sim Config")
            st.info(f"Complete these sections first: {', '.join(missing)}")
        else:
            acct    = wz.get('account', {})
            sim_cfg = wz.get('sim_config', {})
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("DCs",       len(wz.get('dcs',       [])))
            c2.metric("Stores",    len(wz.get('stores',     [])))
            c3.metric("Suppliers", len(wz.get('suppliers',  [])))
            c4.metric("Items",     len(wz.get('items',      [])))
            c5.metric("Promos",    len(wz.get('promos',     [])))
            st.caption(
                f"**{acct.get('account_name','')}** | "
                f"{sim_cfg.get('simulation_name','')} | "
                f"{sim_cfg.get('start_date','')} → {sim_cfg.get('end_date','')}"
            )
            st.divider()

            # 6a. Save ────────────────────────────────────────────────────────
            if not wz.get('saved'):
                if st.button("💾 Save All to PostgreSQL", type="primary"):
                    try:
                        _wz_save_all(wz)
                        wz['saved'] = True
                        st.success(
                            f"Saved! Account `{wz['account']['account_id']}` | "
                            f"Simulation `{wz['account']['sim_id']}`"
                        )
                        st.rerun()
                    except Exception as e:
                        try: get_pg().rollback()
                        except Exception: pass
                        st.error(f"Save failed: {e}")
            else:
                st.success(f"Saved — Account `{wz['account']['account_id']}` | "
                           f"Simulation `{wz['account']['sim_id']}`")

            # 6b. Generate Demand ─────────────────────────────────────────────
            if wz.get('saved') and not wz.get('demand_done'):
                st.divider()
                st.markdown("**Generate demand matrix**")
                cfg_dict = build_config(wz)
                with st.expander("Preview config.yaml"):
                    st.code(yaml.dump(cfg_dict, default_flow_style=False), language='yaml')
                demand_out = st.empty()
                if st.button("▶ Generate Demand", type="primary"):
                    cfg_path = os.path.join(PROJECT_ROOT, f"config_{wz['account']['account_id']}.yaml")
                    with open(cfg_path, 'w') as f:
                        yaml.dump(cfg_dict, f, default_flow_style=False)
                    wz['config_path'] = cfg_path
                    with st.spinner("Running demand_gen.py..."):
                        rc = run_script('demand_gen.py', [
                            '--config',     cfg_path,
                            '--sim_id',     wz['account']['sim_id'],
                            '--account_id', wz['account']['account_id'],
                        ], demand_out)
                    if rc == 0:
                        wz['demand_done'] = True
                        st.success("Demand matrix generated."); st.rerun()
                    else:
                        st.error("demand_gen.py failed — check output above.")

            if wz.get('demand_done'):
                st.success("Demand matrix ready.")

            # 6c. Run Simulation ──────────────────────────────────────────────
            if wz.get('demand_done') and not wz.get('sim_done'):
                st.divider()
                st.markdown("**Run simulation**")
                sim_out = st.empty()
                if st.button("▶ Run Simulation", type="primary"):
                    account_id = wz['account']['account_id']
                    sim_id     = wz['account']['sim_id']
                    with st.spinner("Running simulation..."):
                        rc = run_script('simulate.py',
                                        ['--sim_id', sim_id, '--account_id', account_id],
                                        sim_out)
                    if rc == 0:
                        try:
                            cur = get_pg().cursor()
                            cur.execute(
                                "UPDATE simulation_config SET simulation_status='COMPLETED', "
                                "simulation_run_date=%s WHERE simulation_id=%s",
                                (date.today(), sim_id)
                            )
                            get_pg().commit(); cur.close()
                        except Exception:
                            pass
                        wz['sim_done'] = True
                        st.success("Simulation complete!"); st.rerun()
                    else:
                        st.error("simulate.py failed — check output above.")

            if wz.get('sim_done'):
                st.success("Simulation complete.")

            # 6d. View Dashboard ──────────────────────────────────────────────
            if wz.get('sim_done'):
                st.divider()
                if st.button("✓ View Simulations", type="primary"):
                    st.session_state.account = {
                        'id':   wz['account']['account_id'],
                        'name': wz['account']['account_name'],
                    }
                    st.session_state.wizard = None
                    st.session_state.page   = 'simulations'
                    st.rerun()


# ── Delete account ───────────────────────────────────────────────────────────

def delete_account(account_id: str):
    """Delete all data for an account from ClickHouse then PostgreSQL."""
    # 1. Get simulation IDs for this account
    cur = get_pg().cursor()
    cur.execute("SELECT simulation_id::text FROM simulation_config WHERE account_id = %s", (account_id,))
    sim_ids = [r[0] for r in cur.fetchall()]
    cur.close()

    # 2. Delete from ClickHouse (all simulation data)
    ch_tables = [
        'sales_history', 'store_orders', 'store_order_details',
        'store_receipts', 'supplier_orders', 'supplier_order_details',
        'supplier_receipts', 'store_inventory', 'dc_inventory',
    ]
    for sim_id in sim_ids:
        for tbl in ch_tables:
            try:
                client.command(f"ALTER TABLE {tbl} DELETE WHERE simulation_id = %(sid)s",
                               parameters={'sid': sim_id})
            except Exception:
                pass  # table may not exist or may have no rows — skip

    # 3. Delete from PostgreSQL in FK-safe order
    cur = get_pg().cursor()
    for sim_id in sim_ids:
        cur.execute("DELETE FROM promo_stores        WHERE simulation_id = %s", (sim_id,))
        cur.execute("DELETE FROM promo_group_items   WHERE simulation_id = %s", (sim_id,))
        cur.execute("DELETE FROM promos              WHERE simulation_id = %s", (sim_id,))
        cur.execute("DELETE FROM promo_groups        WHERE simulation_id = %s", (sim_id,))

    cur.execute("DELETE FROM simulation_config WHERE account_id = %s", (account_id,))

    # Resolve store/dc/supplier/item IDs owned by this account
    cur.execute("SELECT store_id    FROM stores    WHERE account_id = %s", (account_id,))
    store_ids = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT dc_id       FROM distribution_centers WHERE account_id = %s", (account_id,))
    dc_ids = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT supplier_id FROM suppliers WHERE account_id = %s", (account_id,))
    sup_ids = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT item_id     FROM items     WHERE account_id = %s", (account_id,))
    item_ids = [r[0] for r in cur.fetchall()]

    for sid in store_ids:
        cur.execute("DELETE FROM store_mappings WHERE from_store_id = %s", (sid,))
        cur.execute("DELETE FROM store_items    WHERE store_id      = %s", (sid,))
    for did in dc_ids:
        cur.execute("DELETE FROM dc_mappings WHERE from_dc_id = %s", (did,))
        cur.execute("DELETE FROM dc_items    WHERE dc_id      = %s", (did,))
    for sup in sup_ids:
        cur.execute("DELETE FROM supplier_items WHERE supplier_id = %s", (sup,))

    cur.execute("DELETE FROM stores               WHERE account_id = %s", (account_id,))
    cur.execute("DELETE FROM distribution_centers WHERE account_id = %s", (account_id,))
    cur.execute("DELETE FROM suppliers            WHERE account_id = %s", (account_id,))
    cur.execute("DELETE FROM items                WHERE account_id = %s", (account_id,))
    cur.execute("DELETE FROM retailer_accounts    WHERE account_id = %s", (account_id,))

    get_pg().commit()
    cur.close()


# ── Delete simulation ────────────────────────────────────────────────────────

def delete_simulation(sim_id: str):
    """Delete a single simulation from ClickHouse and PostgreSQL."""
    ch_tables = [
        'sales_history', 'store_orders', 'store_order_details',
        'store_receipts', 'supplier_orders', 'supplier_order_details',
        'supplier_receipts', 'store_inventory', 'dc_inventory',
    ]
    for tbl in ch_tables:
        try:
            client.command(f"ALTER TABLE {tbl} DELETE WHERE simulation_id = %(sid)s",
                           parameters={'sid': sim_id})
        except Exception:
            pass

    cur = get_pg().cursor()
    cur.execute("DELETE FROM promo_stores      WHERE simulation_id = %s", (sim_id,))
    cur.execute("DELETE FROM promo_group_items WHERE simulation_id = %s", (sim_id,))
    cur.execute("DELETE FROM promos            WHERE simulation_id = %s", (sim_id,))
    cur.execute("DELETE FROM promo_groups      WHERE simulation_id = %s", (sim_id,))
    cur.execute("DELETE FROM simulation_config WHERE simulation_id = %s", (sim_id,))
    get_pg().commit()
    cur.close()

    # Also delete demand rows in ClickHouse
    try:
        client.command("ALTER TABLE demand DELETE WHERE simulation_id = %(sid)s",
                       parameters={'sid': sim_id})
    except Exception:
        pass


# ── Page: Accounts ────────────────────────────────────────────────────────────

def accounts_page():
    st.title("Retail Supply Chain Simulation")

    col_title, col_cfg, col_btn = st.columns([4, 1, 1])
    with col_title:
        st.subheader("Accounts")
    with col_cfg:
        st.write("")
        if st.button("⚙ Config", use_container_width=True):
            st.session_state.page = 'config'; st.rerun()
    with col_btn:
        st.write("")
        if st.button("+ New Account", use_container_width=True):
            st.session_state.wizard = {}; st.rerun()

    cur = get_pg().cursor()
    cur.execute("""
        SELECT account_id, account_name, account_type, region, currency_code, is_active
        FROM retailer_accounts ORDER BY account_name
    """)
    rows = cur.fetchall()
    cur.close()

    if not rows:
        st.warning("No accounts found. Add one above.")
        return

    for account_id, account_name, account_type, region, currency_code, is_active in rows:
        with st.container(border=True):
            col1, col2 = st.columns([4, 1])
            with col1:
                st.markdown(f"### {account_name}")
                st.caption(f"ID: `{account_id}`")
                cols = st.columns(3)
                cols[0].markdown(f"**Type:** {account_type}")
                cols[1].markdown(f"**Region:** {region}")
                cols[2].markdown(f"**Currency:** {currency_code}")
            with col2:
                st.markdown(f"**Status:** {'Active' if is_active else 'Inactive'}")
                st.write("")
                if st.button("View Simulations", key=f"view_{account_id}"):
                    st.session_state.account = {'id': str(account_id), 'name': account_name}
                    st.session_state.page    = 'simulations'
                    st.rerun()
                if st.button("🗑 Delete", key=f"del_{account_id}",
                             use_container_width=True):
                    st.session_state.confirm_delete = str(account_id)
                    st.rerun()

            # Confirmation prompt (shown inline under the card)
            if st.session_state.confirm_delete == str(account_id):
                st.warning(
                    f"Delete **{account_name}** and all its data from PostgreSQL and "
                    f"ClickHouse? This cannot be undone."
                )
                c_yes, c_no = st.columns(2)
                if c_yes.button("Yes, delete everything", type="primary",
                                key=f"confirm_yes_{account_id}"):
                    try:
                        delete_account(str(account_id))
                        st.session_state.confirm_delete = None
                        st.success(f"Account '{account_name}' deleted.")
                        st.rerun()
                    except Exception as e:
                        try: get_pg().rollback()
                        except Exception: pass
                        st.error(f"Delete failed: {e}")
                if c_no.button("Cancel", key=f"confirm_no_{account_id}"):
                    st.session_state.confirm_delete = None
                    st.rerun()


# ── Page: Simulations List ────────────────────────────────────────────────────

def simulations_page():
    account_id   = st.session_state.account['id']
    account_name = st.session_state.account['name']

    col_title, col_back, col_new = st.columns([4, 1, 1])
    col_title.title(f"Simulations — {account_name}")
    with col_back:
        st.write("")
        if st.button("← Accounts", use_container_width=True):
            st.session_state.account = None
            st.session_state.page    = 'accounts'
            st.rerun()
    with col_new:
        st.write("")
        if st.button("+ New Simulation", use_container_width=True):
            st.session_state.new_sim_state = {}
            st.session_state.page = 'new_sim'
            st.rerun()

    cur = get_pg().cursor()
    cur.execute("""
        SELECT simulation_id::text, simulation_name, simulation_status,
               start_week, end_week, created_at
        FROM simulation_config
        WHERE account_id = %s
        ORDER BY created_at DESC
    """, (account_id,))
    rows = cur.fetchall()
    cur.close()

    if not rows:
        st.info("No simulations yet. Click '+ New Simulation' to create one.")
        return

    for sim_id, sim_name, sim_status, start_week, end_week, created_at in rows:
        with st.container(border=True):
            col1, col2 = st.columns([4, 1])
            with col1:
                st.markdown(f"### {sim_name}")
                st.caption(f"ID: `{sim_id}`")
                c1, c2, c3, c4 = st.columns(4)
                status_color = {'COMPLETED': 'green', 'PENDING': 'orange', 'RUNNING': 'blue'}.get(sim_status, 'grey')
                c1.markdown(f"**Status:** :{status_color}[{sim_status}]")
                c2.markdown(f"**Start:** {start_week or '—'}")
                c3.markdown(f"**End:** {end_week or '—'}")
                c4.markdown(f"**Created:** {str(created_at)[:10] if created_at else '—'}")
            with col2:
                st.write("")
                if st.button("View Dashboard", key=f"simview_{sim_id}", use_container_width=True):
                    st.session_state.simulation = {'id': sim_id, 'name': sim_name}
                    st.session_state.page       = 'dashboard'
                    st.rerun()
                if st.button("🗑 Delete", key=f"simdel_{sim_id}", use_container_width=True):
                    st.session_state.confirm_delete_sim = sim_id
                    st.rerun()

            if st.session_state.confirm_delete_sim == sim_id:
                st.warning(f"Delete **{sim_name}** and all its simulation data? This cannot be undone.")
                cy, cn = st.columns(2)
                if cy.button("Yes, delete", type="primary", key=f"simdelyes_{sim_id}"):
                    try:
                        delete_simulation(sim_id)
                        st.session_state.confirm_delete_sim = None
                        st.success(f"Simulation '{sim_name}' deleted.")
                        st.rerun()
                    except Exception as e:
                        try: get_pg().rollback()
                        except Exception: pass
                        st.error(f"Delete failed: {e}")
                if cn.button("Cancel", key=f"simdelno_{sim_id}"):
                    st.session_state.confirm_delete_sim = None
                    st.rerun()


# ── Page: New Simulation ──────────────────────────────────────────────────────

def new_simulation_page():
    account_id   = st.session_state.account['id']
    account_name = st.session_state.account['name']
    ns = st.session_state.new_sim_state  # mutable dict carried in session

    col_title, col_cancel = st.columns([5, 1])
    col_title.title("New Simulation")
    col_title.caption(f"Account: **{account_name}**")
    if col_cancel.button("✕ Cancel", use_container_width=True):
        st.session_state.new_sim_state = None
        st.session_state.page = 'simulations'
        st.rerun()

    # ── Load defaults from most recent simulation for this account ─────────
    if not ns.get('_defaults_loaded'):
        cur = get_pg().cursor()
        cur.execute("""
            SELECT dc_configs, store_configs, supplier_configs,
                   start_week, end_week, random_seed
            FROM simulation_config
            WHERE account_id = %s
            ORDER BY created_at DESC LIMIT 1
        """, (account_id,))
        prev = cur.fetchone()
        cur.execute("SELECT dc_id FROM distribution_centers WHERE account_id = %s", (account_id,))
        dcs = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT store_id FROM stores WHERE account_id = %s", (account_id,))
        stores_list = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT supplier_id FROM suppliers WHERE account_id = %s", (account_id,))
        suppliers_list = [r[0] for r in cur.fetchall()]
        cur.close()

        if prev:
            ns['dc_configs']       = prev[0] or _dc_config_defaults([{'dc_id': d} for d in dcs])
            ns['store_configs']    = prev[1] or _store_config_defaults([{'store_id': s} for s in stores_list])
            ns['supplier_configs'] = prev[2] or _supplier_config_defaults([{'supplier_id': s} for s in suppliers_list])
            # Pre-fill dates/seed from last sim
            ns.setdefault('_def_start', (prev[3] + '-01') if prev[3] else '2024-01-01')
            ns.setdefault('_def_end',   (prev[4] + '-01') if prev[4] else '2024-12-31')
            ns.setdefault('_def_seed',  int(prev[5]) if prev[5] else 42)
        else:
            ns['dc_configs']       = _dc_config_defaults([{'dc_id': d} for d in dcs])
            ns['store_configs']    = _store_config_defaults([{'store_id': s} for s in stores_list])
            ns['supplier_configs'] = _supplier_config_defaults([{'supplier_id': s} for s in suppliers_list])
            ns.setdefault('_def_start', '2024-01-01')
            ns.setdefault('_def_end',   '2024-12-31')
            ns.setdefault('_def_seed',  42)
        ns['_defaults_loaded'] = True

    # ── Sim Config ─────────────────────────────────────────────────────────
    st.subheader("Simulation Settings")
    for k, v in [('ns_sim_name',  ''),
                 ('ns_start_dt',  ns.get('_def_start', '2024-01-01')),
                 ('ns_end_dt',    ns.get('_def_end',   '2024-12-31')),
                 ('ns_rand_seed', ns.get('_def_seed',  42))]:
        if k not in st.session_state:
            st.session_state[k] = v

    c1, c2, c3, c4 = st.columns(4)
    c1.text_input("Simulation Name", key='ns_sim_name', placeholder="My New Run")
    c2.text_input("Start Date (YYYY-MM-DD)", key='ns_start_dt')
    c3.text_input("End Date (YYYY-MM-DD)",   key='ns_end_dt')
    c4.number_input("Random Seed", min_value=0, key='ns_rand_seed')

    st.divider()

    # ── DC / Store / Supplier config editors ──────────────────────────────

    st.markdown("**DC Reliability & Stock Configs**")
    dc_edited  = st.data_editor(pd.DataFrame(ns['dc_configs']),
                                use_container_width=True, num_rows="fixed", key='ns_dc_editor')
    ns['dc_configs'] = dc_edited.to_dict('records')

    st.divider()
    st.markdown("**Store Replenishment Configs**")
    st_edited = st.data_editor(pd.DataFrame(ns['store_configs']),
                                use_container_width=True, num_rows="fixed", key='ns_st_editor')
    ns['store_configs'] = st_edited.to_dict('records')

    st.divider()
    st.markdown("**Supplier Reliability Configs**")
    sup_edited = st.data_editor(pd.DataFrame(ns['supplier_configs']),
                                use_container_width=True, num_rows="fixed", key='ns_sup_editor')
    ns['supplier_configs'] = sup_edited.to_dict('records')

    st.divider()

    regen_demand = st.checkbox(
        "Regenerate demand matrix",
        value=False,
        key='ns_regen_demand',
        help="Re-run demand_gen.py before the simulation. Use this if you changed dates, seed, or want fresh demand. "
             "If unchecked, the existing demand_matrix.parquet is reused."
    )

    # ── Save & Run ─────────────────────────────────────────────────────────
    sim_out = st.empty()

    col_save, col_run = st.columns(2)
    if not ns.get('saved'):
        if col_save.button("💾 Save Simulation", type="primary"):
            sim_name = st.session_state.ns_sim_name.strip()
            if not sim_name:
                st.error("Simulation name is required.")
            else:
                try:
                    new_sim_id = str(uuid.uuid4())
                    cur = get_pg().cursor()
                    cur.execute("""
                        INSERT INTO simulation_config
                          (simulation_id, account_id, simulation_name, config_name,
                           created_at, simulation_status, start_week, end_week, random_seed,
                           dc_configs, store_configs, supplier_configs)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
                    """, (new_sim_id, account_id, sim_name, 'Default Config',
                          date.today(), 'PENDING',
                          st.session_state.ns_start_dt[:7], st.session_state.ns_end_dt[:7],
                          int(st.session_state.ns_rand_seed),
                          json.dumps(ns['dc_configs']),
                          json.dumps(ns['store_configs']),
                          json.dumps(ns['supplier_configs'])))
                    get_pg().commit(); cur.close()
                    ns['saved']      = True
                    ns['sim_id']     = new_sim_id
                    ns['sim_name']   = sim_name
                    ns['start_date'] = st.session_state.ns_start_dt.strip()
                    ns['end_date']   = st.session_state.ns_end_dt.strip()
                    ns['seed']       = int(st.session_state.ns_rand_seed)
                    st.success(f"Saved — `{new_sim_id}`")
                    st.rerun()
                except Exception as e:
                    try: get_pg().rollback()
                    except Exception: pass
                    st.error(f"Save failed: {e}")
    else:
        st.success(f"Saved — `{ns['sim_id']}`")

    if ns.get('saved') and not ns.get('sim_done'):
        if col_run.button("▶ Run Simulation", type="primary"):
                ok = True
                if st.session_state.get('ns_regen_demand'):
                    with st.spinner("Regenerating demand matrix..."):
                        rc_d = run_script('demand_gen.py', [
                            '--sim_id',     ns['sim_id'],
                            '--account_id', account_id,
                        ], sim_out)
                    if rc_d != 0:
                        st.error("demand_gen.py failed — check output above.")
                        ok = False

                if ok:
                    with st.spinner("Running simulation..."):
                        rc = run_script('simulate.py',
                                        ['--sim_id',     ns['sim_id'],
                                         '--account_id', account_id],
                                        sim_out)
                    if rc == 0:
                        try:
                            cur = get_pg().cursor()
                            cur.execute(
                                "UPDATE simulation_config SET simulation_status='COMPLETED', "
                                "simulation_run_date=%s WHERE simulation_id=%s",
                                (date.today(), ns['sim_id'])
                            )
                            get_pg().commit(); cur.close()
                        except Exception:
                            pass
                        ns['sim_done'] = True
                        st.success("Simulation complete!"); st.rerun()
                    else:
                        st.error("simulate.py failed — check output above.")

    if ns.get('sim_done'):
        st.success("Simulation complete.")
        if st.button("← Back to Simulations", type="primary"):
            st.session_state.new_sim_state = None
            st.session_state.page = 'simulations'
            st.rerun()


# ── Page: Dashboard ───────────────────────────────────────────────────────────

def dashboard_page():
    account_id   = st.session_state.account['id']
    account_name = st.session_state.account['name']
    selected_sim = st.session_state.simulation['id']
    sim_name     = st.session_state.simulation['name']

    st.title("Simulation Dashboard")
    st.caption(f"Account: **{account_name}** | Simulation: **{sim_name}**")

    if st.button("← Back to Simulations"):
        st.session_state.simulation = None
        st.session_state.page       = 'simulations'
        st.rerun()

    @st.cache_data
    def load_filter_options(sim_id):
        def _col(query, col):
            df = client.query_df(query, parameters={'sid': sim_id})
            return df[col].tolist() if col in df.columns and not df.empty else []

        stores = _col(
            "SELECT DISTINCT store_id FROM sales_history "
            "WHERE simulation_id = %(sid)s ORDER BY store_id", 'store_id')
        items = _col(
            "SELECT DISTINCT item_id FROM sales_history "
            "WHERE simulation_id = %(sid)s ORDER BY item_id", 'item_id')
        weeks = _col(
            "SELECT DISTINCT sales_week FROM sales_history "
            "WHERE simulation_id = %(sid)s ORDER BY sales_week", 'sales_week')
        return stores, items, weeks

    @st.cache_data
    def load_sales(sim_id, store_filter, item_filter, w_from, w_to):
        params = {'sid': sim_id, 'wf': w_from, 'wt': w_to}
        where_extra = ""
        if store_filter != "All":
            where_extra += " AND store_id = %(store)s"
            params['store'] = store_filter
        if item_filter != "All":
            where_extra += " AND item_id = %(item)s"
            params['item'] = item_filter
        return client.query_df(
            "SELECT sales_week, sum(sales_quantity) AS total_sales "
            "FROM sales_history "
            f"WHERE simulation_id = %(sid)s{where_extra} "
            "  AND sales_week >= %(wf)s AND sales_week <= %(wt)s "
            "GROUP BY sales_week ORDER BY sales_week",
            parameters=params
        )

    @st.cache_data
    def load_sim_configs(sim_id):
        conn = psycopg2.connect(
            host=os.environ['PG_HOST'], port=os.environ.get('PG_PORT', 5432),
            dbname=os.environ['PG_DB'], user=os.environ['PG_USER'],
            password=os.environ['PG_PASSWORD'],
            sslmode=os.environ.get('PG_SSLMODE', 'prefer')
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT dc_configs, store_configs, supplier_configs "
            "FROM simulation_config WHERE simulation_id = %s",
            (sim_id,)
        )
        row = cur.fetchone()
        cur.close(); conn.close()
        if row is None:
            return [], [], []
        return row[0] or [], row[1] or [], row[2] or []

    @st.cache_data
    def load_inventory(sim_id, store_filter, item_filter, w_from, w_to):
        params = {'sid': sim_id, 'wf': w_from, 'wt': w_to}
        where_extra = ""
        if store_filter != "All":
            where_extra += " AND store_id = %(store)s"
            params['store'] = store_filter
        if item_filter != "All":
            where_extra += " AND item_id = %(item)s"
            params['item'] = item_filter
        return client.query_df(
            "SELECT inventory_week, sum(on_hand_quantity) AS total_inventory "
            "FROM store_inventory "
            f"WHERE simulation_id = %(sid)s{where_extra} "
            "  AND inventory_week >= %(wf)s AND inventory_week <= %(wt)s "
            "GROUP BY inventory_week ORDER BY inventory_week",
            parameters=params
        )

    @st.cache_data
    def load_demand(sim_id, stores_filter, items_filter, w_from, w_to):
        params = {'sid': sim_id, 'wf': w_from, 'wt': w_to}
        where_extra = ""
        if stores_filter:
            where_extra += " AND store_id IN %(stores)s"
            params['stores'] = tuple(stores_filter)
        if items_filter:
            where_extra += " AND item_id IN %(items)s"
            params['items'] = tuple(items_filter)
        return client.query_df(
            "SELECT store_id, item_id, demand_week, "
            "  sum(demand_qty) AS total_qty, "
            "  countIf(is_promo_demand) AS promo_days "
            "FROM demand "
            f"WHERE simulation_id = %(sid)s{where_extra} "
            "  AND demand_week >= %(wf)s AND demand_week <= %(wt)s "
            "GROUP BY store_id, item_id, demand_week "
            "ORDER BY demand_week, store_id, item_id "
            "LIMIT 5001",
            parameters=params
        )

    with st.sidebar:
        st.header("Filters")
        stores, items, weeks = load_filter_options(selected_sim)
        if not weeks:
            st.warning("No sales data for this simulation yet."); st.stop()

        selected_store = st.selectbox("Store", ["All"] + stores)
        selected_item  = st.selectbox("Item",  ["All"] + items)

        week_start_idx, week_end_idx = st.select_slider(
            "Week range",
            options=range(len(weeks)),
            value=(0, len(weeks) - 1),
            format_func=lambda i: weeks[i]
        )
        week_from = weeks[week_start_idx]
        week_to   = weeks[week_end_idx]

    tab_sales, tab_config, tab_demand, tab_rerun = st.tabs(["Sales", "Config", "Demand Matrix", "Re-run"])

    # ── Tab: Sales ────────────────────────────────────────────────────────────
    with tab_sales:
        with st.spinner("Loading..."):
            sales_df = load_sales(selected_sim, selected_store, selected_item, week_from, week_to)
            inv_df   = load_inventory(selected_sim, selected_store, selected_item, week_from, week_to)

        if sales_df.empty:
            st.info("No sales data for the selected filters.")
        else:
            title = "Total Sales — All Stores / All Items"
            if selected_store != "All" and selected_item != "All":
                title = f"Sales — {selected_store} / {selected_item}"
            elif selected_store != "All":
                title = f"Sales — {selected_store} (all items)"
            elif selected_item != "All":
                title = f"Sales — {selected_item} (all stores)"

            fig = go.Figure(go.Bar(
                x=sales_df['sales_week'],
                y=sales_df['total_sales'],
                marker_color='steelblue',
                name='Units Sold',
            ))

            if not inv_df.empty:
                fig.add_trace(go.Scatter(
                    x=inv_df['inventory_week'],
                    y=inv_df['total_inventory'],
                    mode='lines+markers',
                    name='Inventory (on-hand)',
                    line=dict(color='orange', width=2),
                    marker=dict(size=4),
                    yaxis='y2',
                ))
                fig.update_layout(
                    yaxis2=dict(
                        title="Inventory (units)",
                        overlaying='y',
                        side='right',
                        showgrid=False,
                    )
                )

            fig.update_layout(
                title=title,
                xaxis_title="Week",
                yaxis_title="Units Sold",
                height=420,
                margin=dict(l=40, r=20, t=50, b=60),
                xaxis=dict(tickangle=-45),
                legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
            )
            st.plotly_chart(fig, use_container_width=True)

            total = int(sales_df['total_sales'].sum())
            avg   = round(sales_df['total_sales'].mean(), 1)
            peak  = sales_df.loc[sales_df['total_sales'].idxmax()]
            c1, c2, c3 = st.columns(3)
            c1.metric("Total Units Sold", f"{total:,}")
            c2.metric("Avg per Week",     f"{avg:,}")
            c3.metric("Peak Week",        f"{peak['sales_week']} ({int(peak['total_sales']):,} units)")

    # ── Tab: Config ───────────────────────────────────────────────────────────
    with tab_config:
        dc_data, store_data, sup_data = load_sim_configs(selected_sim)
        sub_dc, sub_store, sub_sup = st.tabs(["DC Configs", "Store Configs", "Supplier Configs"])

        with sub_dc:
            if dc_data:
                st.dataframe(pd.DataFrame(dc_data), use_container_width=True, hide_index=True)
            else:
                st.info("No DC config data for this simulation.")

        with sub_store:
            if store_data:
                st.dataframe(pd.DataFrame(store_data), use_container_width=True, hide_index=True)
            else:
                st.info("No store config data for this simulation.")

        with sub_sup:
            if sup_data:
                st.dataframe(pd.DataFrame(sup_data), use_container_width=True, hide_index=True)
            else:
                st.info("No supplier config data for this simulation.")

    # ── Tab: Demand Matrix ────────────────────────────────────────────────────
    with tab_demand:
        st.caption("Aggregated demand per store / item / week for the selected simulation.")

        d_c1, d_c2, d_c3 = st.columns(3)
        dm_stores = d_c1.multiselect("Stores", stores, placeholder="All stores")
        dm_items  = d_c2.multiselect("Items",  items,  placeholder="All items")
        if weeks:
            dm_week_idx = d_c3.select_slider(
                "Week range",
                options=range(len(weeks)),
                value=(0, len(weeks) - 1),
                format_func=lambda i: weeks[i],
                key="dm_week_slider"
            )
            dm_week_from = weeks[dm_week_idx[0]]
            dm_week_to   = weeks[dm_week_idx[1]]
        else:
            dm_week_from = dm_week_to = None

        if dm_week_from is not None:
            with st.spinner("Loading demand matrix..."):
                demand_df = load_demand(
                    selected_sim,
                    tuple(dm_stores) if dm_stores else (),
                    tuple(dm_items)  if dm_items  else (),
                    dm_week_from, dm_week_to
                )

            truncated = len(demand_df) > 5000
            if truncated:
                demand_df = demand_df.head(5000)

            m1, m2 = st.columns(2)
            m1.metric("Rows shown", f"{len(demand_df):,}" + (" (capped at 5,000)" if truncated else ""))
            m2.metric("Total demand qty", f"{int(demand_df['total_qty'].sum()):,}" if not demand_df.empty else "0")

            if demand_df.empty:
                st.info("No demand data for the selected filters.")
            else:
                if truncated:
                    st.warning("Result exceeds 5,000 rows — apply filters to narrow the selection.")
                st.dataframe(demand_df, use_container_width=True, hide_index=True)

    # ── Tab: Re-run ───────────────────────────────────────────────────────────
    with tab_rerun:
        st.caption(
            "Modify DC / Store / Supplier configs and re-run the simulation. "
            "The existing demand matrix is reused — no new demand generation needed."
        )

        dc_data, store_data, sup_data = load_sim_configs(selected_sim)

        rr_dc_edited  = st.data_editor(pd.DataFrame(dc_data)    if dc_data    else pd.DataFrame(),
                                       use_container_width=True, num_rows="fixed", key='rr_dc_editor',
                                       disabled=not bool(dc_data))
        st.divider()
        rr_st_edited  = st.data_editor(pd.DataFrame(store_data) if store_data else pd.DataFrame(),
                                       use_container_width=True, num_rows="fixed", key='rr_st_editor',
                                       disabled=not bool(store_data))
        st.divider()
        rr_sup_edited = st.data_editor(pd.DataFrame(sup_data)   if sup_data   else pd.DataFrame(),
                                       use_container_width=True, num_rows="fixed", key='rr_sup_editor',
                                       disabled=not bool(sup_data))
        st.divider()

        rr_name = st.text_input("New Simulation Name", value=sim_name + " (re-run)", key='rr_sim_name')
        rr_out  = st.empty()

        if st.button("▶ Re-run Simulation", type="primary", key='rr_run_btn'):
            if not rr_name.strip():
                st.error("Simulation name is required.")
            else:
                try:
                    # 1. Create new simulation_config row with edited configs
                    new_sim_id = str(uuid.uuid4())
                    cur = get_pg().cursor()
                    cur.execute("""
                        INSERT INTO simulation_config
                          (simulation_id, account_id, simulation_name, config_name,
                           created_at, simulation_status, start_week, end_week, random_seed,
                           dc_configs, store_configs, supplier_configs)
                        SELECT %s, account_id, %s, config_name,
                               current_date, 'PENDING', start_week, end_week, random_seed,
                               %s::jsonb, %s::jsonb, %s::jsonb
                        FROM simulation_config WHERE simulation_id = %s
                    """, (new_sim_id, rr_name.strip(),
                          json.dumps(rr_dc_edited.to_dict('records')  if not rr_dc_edited.empty  else []),
                          json.dumps(rr_st_edited.to_dict('records')  if not rr_st_edited.empty  else []),
                          json.dumps(rr_sup_edited.to_dict('records') if not rr_sup_edited.empty else []),
                          selected_sim))
                    get_pg().commit(); cur.close()

                    # 2. Run simulation (demand_matrix.parquet already on disk)
                    with st.spinner("Running simulation..."):
                        rc = run_script('simulate.py',
                                        ['--sim_id',     new_sim_id,
                                         '--account_id', account_id],
                                        rr_out)
                    if rc == 0:
                        cur = get_pg().cursor()
                        cur.execute(
                            "UPDATE simulation_config SET simulation_status='COMPLETED', "
                            "simulation_run_date=%s WHERE simulation_id=%s",
                            (date.today(), new_sim_id)
                        )
                        get_pg().commit(); cur.close()
                        st.success(f"Re-run complete! New simulation: `{new_sim_id}`")
                        # Navigate to new simulation's dashboard
                        st.session_state.simulation = {'id': new_sim_id, 'name': rr_name.strip()}
                        st.rerun()
                    else:
                        try: get_pg().rollback()
                        except Exception: pass
                        st.error("simulate.py failed — check output above.")
                except Exception as e:
                    try: get_pg().rollback()
                    except Exception: pass
                    st.error(f"Error: {e}")


# ── Page: Config ──────────────────────────────────────────────────────────────

def config_page():
    st.title("Simulation Config")

    if st.button("← Back to Accounts"):
        st.session_state.page = 'accounts'; st.rerun()

    # Load simulations from Postgres
    cur = get_pg().cursor()
    cur.execute("""
        SELECT simulation_id::text, simulation_name, simulation_status,
               start_week, end_week, random_seed
        FROM simulation_config ORDER BY simulation_name
    """)
    sim_rows = cur.fetchall()
    cur.close()

    if not sim_rows:
        st.warning("No simulations found.")
        return

    sim_options = {r[0]: f"{r[1]} ({r[2]})" for r in sim_rows}
    selected_sim_id = st.selectbox(
        "Select Simulation",
        options=list(sim_options.keys()),
        format_func=lambda k: sim_options.get(k, k)
    )

    sim_row = next(r for r in sim_rows if r[0] == selected_sim_id)

    tab1, tab2 = st.tabs(["Simulation Config", "DC / Store / Supplier Configs"])

    # ── Tab 1: Simulation Config ──────────────────────────────────────────────
    with tab1:
        st.markdown(f"**Status:** `{sim_row[2]}`")

        c1, c2, c3 = st.columns(3)
        new_name  = c1.text_input("Simulation Name", value=sim_row[1],    key='cfg_name')
        new_start = c2.text_input("Start Week",       value=sim_row[3] or '', key='cfg_start')
        new_end   = c3.text_input("End Week",         value=sim_row[4] or '', key='cfg_end')
        new_seed  = st.number_input("Random Seed", value=int(sim_row[5] or 42), key='cfg_seed')

        col_save, col_new = st.columns(2)

        if col_save.button("💾 Save Changes", type="primary"):
            try:
                cur = get_pg().cursor()
                cur.execute("""
                    UPDATE simulation_config
                    SET simulation_name=%s, start_week=%s, end_week=%s, random_seed=%s
                    WHERE simulation_id=%s
                """, (new_name, new_start or None, new_end or None, new_seed, selected_sim_id))
                get_pg().commit(); cur.close()
                st.success("Saved.")
                load_simulations.clear() if hasattr(load_simulations, 'clear') else None
            except Exception as e:
                get_pg().rollback(); st.error(f"Error: {e}")

        if col_new.button("+ New Simulation Run"):
            try:
                new_id = str(uuid.uuid4())
                cur    = get_pg().cursor()
                cur.execute("""
                    SELECT account_id FROM simulation_config WHERE simulation_id=%s
                """, (selected_sim_id,))
                acct_row = cur.fetchone()
                cur.execute("""
                    INSERT INTO simulation_config
                      (simulation_id, account_id, simulation_name, config_name,
                       created_at, simulation_status, random_seed,
                       dc_configs, store_configs, supplier_configs)
                    SELECT %s, account_id, simulation_name || ' (copy)', config_name,
                           current_date, 'PENDING', random_seed,
                           dc_configs, store_configs, supplier_configs
                    FROM simulation_config WHERE simulation_id=%s
                """, (new_id, selected_sim_id))
                get_pg().commit(); cur.close()
                st.success(f"New simulation created: `{new_id}`")
                st.rerun()
            except Exception as e:
                get_pg().rollback(); st.error(f"Error: {e}")

    # ── Tab 2: JSONB Config Editor ────────────────────────────────────────────
    with tab2:
        cur = get_pg().cursor()
        cur.execute("""
            SELECT dc_configs, store_configs, supplier_configs
            FROM simulation_config WHERE simulation_id=%s
        """, (selected_sim_id,))
        cfg_row = cur.fetchone()
        cur.close()

        if cfg_row is None:
            st.warning("No config found."); return

        dc_data, store_data, sup_data = cfg_row

        sub_tab1, sub_tab2, sub_tab3 = st.tabs(["DC Configs", "Store Configs", "Supplier Configs"])

        def jsonb_editor(data, label, save_field, key_prefix):
            df = pd.DataFrame(data) if data else pd.DataFrame()
            if df.empty:
                st.info(f"No {label} config data.")
                return None
            edited = st.data_editor(df, use_container_width=True, num_rows="fixed",
                                    key=f"{key_prefix}_editor")
            if st.button(f"💾 Save {label} Changes", key=f"{key_prefix}_save"):
                try:
                    cur = get_pg().cursor()
                    cur.execute(
                        f"UPDATE simulation_config SET {save_field}=%s::jsonb WHERE simulation_id=%s",
                        (json.dumps(edited.to_dict('records')), selected_sim_id)
                    )
                    get_pg().commit(); cur.close()
                    st.success(f"{label} config saved.")
                except Exception as e:
                    get_pg().rollback(); st.error(f"Error: {e}")

        with sub_tab1:
            jsonb_editor(dc_data,    "DC",       "dc_configs",       "dc")
        with sub_tab2:
            jsonb_editor(store_data, "Store",    "store_configs",    "store")
        with sub_tab3:
            jsonb_editor(sup_data,   "Supplier", "supplier_configs", "sup")


# ── Router ────────────────────────────────────────────────────────────────────

if st.session_state.wizard is not None:
    new_account_page()
elif st.session_state.page == 'new_sim' and st.session_state.account is not None:
    new_simulation_page()
elif st.session_state.page == 'config':
    config_page()
elif st.session_state.page == 'simulations' and st.session_state.account is not None:
    simulations_page()
elif st.session_state.page == 'dashboard' and st.session_state.account is not None and st.session_state.simulation is not None:
    dashboard_page()
else:
    accounts_page()
