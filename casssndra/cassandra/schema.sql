DROP KEYSPACE IF EXISTS django_platform;

CREATE KEYSPACE IF NOT EXISTS django_platform
WITH replication = {
  'class': 'NetworkTopologyStrategy',
  'datacenter-1': 1
};


DROP TABLE IF EXISTS django_platform.orders;

CREATE TABLE IF NOT EXISTS django_platform.orders (
  order_id          uuid,
  placed_at         timeuuid,
  item_count        smallint,
  customer_id       int,
  order_total_cents bigint,
  discount_rate     float,
  tax_amount        double,
  confirmed_at      timestamp,
  delivery_date     date,
  order_window      time,
  status            text,
  is_gift           boolean,
  PRIMARY KEY ((order_id, placed_at))
);

DROP TABLE IF EXISTS django_platform.products;

CREATE TABLE IF NOT EXISTS django_platform.products (
  category_id     int,
  sku             text,
  warranty_months smallint,
  stock_qty       int,
  view_count      bigint,
  rating          float,
  weight_kg       double,
  listed_at       timestamp,
  release_date    date,
  restock_time    time,
  title           text,
  is_active       boolean,
  PRIMARY KEY ((category_id, sku))
);

DROP TABLE IF EXISTS django_platform.inventory;

CREATE TABLE IF NOT EXISTS django_platform.inventory (
  warehouse_id     int,
  bin_id           int,
  aisle            smallint,
  quantity_on_hand int,
  total_movements  bigint,
  fill_ratio       float,
  temperature_c    double,
  last_counted_at  timestamp,
  expiry_date      date,
  last_scan_time   time,
  location_label   text,
  needs_restock    boolean,
  PRIMARY KEY ((warehouse_id, bin_id))
);

DROP TABLE IF EXISTS django_platform.shipments;

CREATE TABLE IF NOT EXISTS django_platform.shipments (
  carrier_id      int,
  tracking_code   text,
  leg_no          int,
  leg_count       smallint,
  parcel_count    int,
  distance_meters bigint,
  weight_kg       float,
  shipping_cost   double,
  dispatched_at   timestamp,
  eta_date        date,
  pickup_time     time,
  status          text,
  is_delivered    boolean,
  PRIMARY KEY ((carrier_id, tracking_code, leg_no))
);
