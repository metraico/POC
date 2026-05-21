import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import clickhouse_connect

client = clickhouse_connect.get_client(
    host=os.environ['CH_HOST'],
    port=int(os.environ.get('CH_PORT', 8123)),
    database=os.environ['CH_DB'],
    username=os.environ['CH_USER'],
    password=os.environ['CH_PASSWORD'],
    verify=False,
    connect_timeout=60,
    send_receive_timeout=300,
)

# Drop old tables in dependency order
drops = [
    'sales_history',
    'store_receipts',
    'store_order_details',
    'store_orders',
    'demand',
    'supplier_receipts',
    'supplier_order_details',
    'supplier_orders',
    'store_inventory',
    'dc_inventory',
    'sales_daily',
    'store_inventory_daily',
    # legacy table names
    'simulation_runs',
    'customer_order_details',
    'order_delivery',
    'customer_orders',
]
for t in drops:
    client.command(f'DROP TABLE IF EXISTS {t}')
    print(f"Dropped {t}")

tables = [
    """
    CREATE TABLE demand (
      simulation_id   String,
      account_id      String,
      store_id        String,
      item_id         String,
      demand_date     Date,
      demand_week     String,
      demand_qty      Float64,
      is_promo_demand Bool,
      promo_id        String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, account_id, store_id, item_id, demand_date)
    """,
    """
    CREATE TABLE store_orders (
      store_order_number String,
      simulation_id      String,
      account_id         String,
      store_id           String,
      dc_id              String,
      order_week         String,
      order_date         Date,
      order_status       String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, store_id, order_week)
    """,
    """
    CREATE TABLE store_order_details (
      store_order_number String,
      line_number        Int32,
      simulation_id      String,
      account_id         String,
      item_id            String,
      order_quantity     Float64,
      uom                String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, store_order_number, item_id)
    """,
    """
    CREATE TABLE store_receipts (
      receipt_id         String,
      line_number        Int32,
      simulation_id      String,
      account_id         String,
      store_order_number String,
      store_id           String,
      item_id            String,
      receipt_date       Date,
      received_quantity  Float64,
      unfilled_quantity  Float64,
      receipt_type       String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, store_id, item_id, receipt_date)
    """,
    """
    CREATE TABLE store_inventory (
      simulation_id      String,
      account_id         String,
      store_id           String,
      item_id            String,
      inventory_week     String,
      on_hand_quantity   Float64,
      available_quantity Float64,
      on_order_quantity  Float64,
      inventory_status   String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, store_id, item_id, inventory_week)
    """,
    """
    CREATE TABLE sales_history (
      simulation_id  String,
      account_id     String,
      store_id       String,
      item_id        String,
      sales_week     String,
      sales_quantity Float64,
      sales_amount   Float64,
      unit_price     Float64,
      uom            String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, account_id, store_id, item_id, sales_week)
    """,
    """
    CREATE TABLE supplier_orders (
      purchase_order_number String,
      simulation_id         String,
      account_id            String,
      dc_id                 String,
      supplier_id           String,
      order_date            Date,
      expected_receipt_date Date,
      order_status          String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, dc_id, order_date)
    """,
    """
    CREATE TABLE supplier_order_details (
      purchase_order_number String,
      line_number           Int32,
      simulation_id         String,
      account_id            String,
      dc_id                 String,
      item_id               String,
      supplier_id           String,
      need_quantity         Int32,
      order_quantity        Float64,
      unit_cost             Float64,
      uom                   String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, purchase_order_number, item_id)
    """,
    """
    CREATE TABLE supplier_receipts (
      receipt_id            String,
      line_number           Int32,
      simulation_id         String,
      account_id            String,
      purchase_order_number String,
      dc_id                 String,
      item_id               String,
      receipt_date          Date,
      received_quantity     Float64,
      unfilled_quantity     Float64,
      receipt_type          String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, dc_id, item_id, receipt_date)
    """,
    """
    CREATE TABLE dc_inventory (
      simulation_id      String,
      account_id         String,
      dc_id              String,
      item_id            String,
      inventory_week     String,
      on_hand_quantity   Float64,
      available_quantity Float64,
      on_order_quantity  Float64,
      inventory_status   String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, dc_id, item_id, inventory_week)
    """,
    """
    CREATE TABLE sales_daily (
      simulation_id  String,
      account_id     String,
      store_id       String,
      item_id        String,
      sales_date     Date,
      sales_qty      Float64,
      sales_amount   Float64,
      unit_price     Float64,
      uom            String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, store_id, item_id, sales_date)
    """,
    """
    CREATE TABLE store_inventory_daily (
      simulation_id      String,
      account_id         String,
      store_id           String,
      item_id            String,
      inventory_date     Date,
      on_hand_quantity   Float64,
      available_quantity Float64,
      on_order_quantity  Float64,
      inventory_status   String
    ) ENGINE = MergeTree()
    ORDER BY (simulation_id, store_id, item_id, inventory_date)
    """,
]

for ddl in tables:
    client.command(ddl.strip())
    print(f"Created table OK")

print("ClickHouse setup complete.")
