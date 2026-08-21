#!/usr/bin/env python3
"""
Seed django_platform tables with N rows each.

Install:  pip install cassandra-driver
Run:      python seed_data.py 20          # 20 rows per table (default)
          python seed_data.py 100 --dry   # print CQL instead of inserting
"""

import sys
import random
import uuid
from datetime import datetime, date, time, timedelta

# ---------------------------------------------------------------- config
HOST = "127.0.0.1"
PORT = 9042
KEYSPACE = "django_platform"

ROW_COUNT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 20
DRY_RUN = "--dry" in sys.argv

STATUSES_ORDER = ["pending", "confirmed", "shipped", "delivered", "cancelled"]
STATUSES_SHIP = ["created", "in_transit", "out_for_delivery", "delivered"]
TITLES = ["Mechanical Keyboard", "Wireless Mouse", "27-inch Monitor",
          "USB-C Hub", "Webcam 4K", "Laptop Stand", "Desk Mat"]

# ---------------------------------------------------------------- helpers
def rand_dt():
    return datetime(2025, 1, 1) + timedelta(
        days=random.randint(0, 240),
        seconds=random.randint(0, 86399),
    )

def rand_date():
    return date(2025, 1, 1) + timedelta(days=random.randint(0, 600))

def rand_time():
    return time(random.randint(0, 23), random.randint(0, 59), random.randint(0, 59))


# ---------------------------------------------------------------- row builders
# each returns a tuple matching the prepared-statement column order

def row_orders(i):
    return (
        uuid.uuid4(),                              # order_id      uuid
        _timeuuid(),                               # placed_at     timeuuid
        random.randint(1, 10),                     # item_count    smallint
        1000 + i,                                  # customer_id   int
        random.randint(9900, 5000000),            # order_total   bigint
        round(random.uniform(0, 0.5), 2),         # discount_rate float
        round(random.uniform(0, 200), 2),         # tax_amount    double
        rand_dt(),                                 # confirmed_at  timestamp
        rand_date(),                               # delivery_date date
        rand_time(),                               # order_window  time
        random.choice(STATUSES_ORDER),             # status        text
        random.choice([True, False]),              # is_gift       boolean
    )

def row_products(i):
    return (
        random.choice([10, 20, 30]),               # category_id   int
        f"SKU-{i:05d}",                            # sku           text
        random.choice([12, 24, 36]),               # warranty_mon  smallint
        random.randint(0, 500),                    # stock_qty     int
        random.randint(0, 200000),                # view_count    bigint
        round(random.uniform(1, 5), 1),           # rating        float
        round(random.uniform(0.05, 10), 2),       # weight_kg     double
        rand_dt(),                                 # listed_at     timestamp
        rand_date(),                               # release_date  date
        rand_time(),                               # restock_time  time
        random.choice(TITLES),                     # title         text
        random.choice([True, False]),              # is_active     boolean
    )

def row_inventory(i):
    return (
        random.choice([1, 2, 3]),                  # warehouse_id  int
        500 + i,                                   # bin_id        int
        random.randint(1, 40),                     # aisle         smallint
        random.randint(0, 1000),                  # qty_on_hand   int
        random.randint(0, 50000),                 # total_moves   bigint
        round(random.uniform(0, 1), 2),           # fill_ratio    float
        round(random.uniform(-5, 30), 1),         # temperature_c double
        rand_dt(),                                 # last_counted  timestamp
        rand_date(),                               # expiry_date   date
        rand_time(),                               # last_scan     time
        f"LOC-{i:04d}",                           # location_lbl  text
        random.choice([True, False]),              # needs_restock boolean
    )

def row_shipments(i):
    return (
        random.choice([7, 8, 9]),                  # carrier_id    int
        f"TRK-{i:06d}",                           # tracking_code text
        random.randint(1, 3),                      # leg_no        int
        random.randint(1, 3),                      # leg_count     smallint
        random.randint(1, 10),                     # parcel_count  int
        random.randint(1000, 900000),             # distance_m    bigint
        round(random.uniform(0.1, 50), 2),        # weight_kg     float
        round(random.uniform(50, 1000), 2),       # shipping_cost double
        rand_dt(),                                 # dispatched_at timestamp
        rand_date(),                               # eta_date      date
        rand_time(),                               # pickup_time   time
        random.choice(STATUSES_SHIP),              # status        text
        random.choice([True, False]),              # is_delivered  boolean
    )


TABLES = {
    "orders": (
        "order_id, placed_at, item_count, customer_id, order_total_cents, "
        "discount_rate, tax_amount, confirmed_at, delivery_date, order_window, "
        "status, is_gift", row_orders),
    "products": (
        "category_id, sku, warranty_months, stock_qty, view_count, rating, "
        "weight_kg, listed_at, release_date, restock_time, title, is_active",
        row_products),
    "inventory": (
        "warehouse_id, bin_id, aisle, quantity_on_hand, total_movements, "
        "fill_ratio, temperature_c, last_counted_at, expiry_date, last_scan_time, "
        "location_label, needs_restock", row_inventory),
    "shipments": (
        "carrier_id, tracking_code, leg_no, leg_count, parcel_count, "
        "distance_meters, weight_kg, shipping_cost, dispatched_at, eta_date, "
        "pickup_time, status, is_delivered", row_shipments),
}


def _timeuuid():
    # unique time-based UUID (random node/clock_seq -> effectively no collisions)
    from cassandra.util import uuid_from_time
    return uuid_from_time(datetime.now(), node=random.getrandbits(48))


# ---------------------------------------------------------------- run
def main():
    if DRY_RUN:
        for table, (cols, builder) in TABLES.items():
            for i in range(ROW_COUNT):
                vals = builder(i)
                rendered = ", ".join(_lit(v) for v in vals)
                print(f"INSERT INTO {KEYSPACE}.{table} ({cols}) VALUES ({rendered});")
        return

    from cassandra.cluster import Cluster
    cluster = Cluster([HOST], port=PORT)
    session = cluster.connect(KEYSPACE)

    for table, (cols, builder) in TABLES.items():
        placeholders = ", ".join(["?"] * len(cols.split(",")))
        stmt = session.prepare(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders})")
        for i in range(ROW_COUNT):
            session.execute(stmt, builder(i))
        print(f"{table}: inserted {ROW_COUNT} rows")

    cluster.shutdown()


def _lit(v):
    # only used for --dry printing
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    return f"'{v}'"


if __name__ == "__main__":
    main()