
-- ORDERS
INSERT INTO django_platform.orders (order_id, placed_at, item_count, customer_id, order_total_cents, discount_rate, tax_amount, confirmed_at, delivery_date, order_window, status, is_gift)
VALUES (uuid(), now(), 3, 1001, 459900, 0.10, 41.39, '2025-08-01 10:15:00', '2025-08-05', '10:15:00', 'confirmed', false);

INSERT INTO django_platform.orders (order_id, placed_at, item_count, customer_id, order_total_cents, discount_rate, tax_amount, confirmed_at, delivery_date, order_window, status, is_gift)
VALUES (uuid(), now(), 1, 1002, 129000, 0.00, 11.61, '2025-08-02 14:30:00', '2025-08-06', '14:30:00', 'shipped', true);

INSERT INTO django_platform.orders (order_id, placed_at, item_count, customer_id, order_total_cents, discount_rate, tax_amount, confirmed_at, delivery_date, order_window, status, is_gift)
VALUES (uuid(), now(), 5, 1003, 899950, 0.25, 67.49, '2025-08-03 09:05:00', '2025-08-08', '09:05:00', 'pending', false);

-- PRODUCTS
INSERT INTO django_platform.products (category_id, sku, warranty_months, stock_qty, view_count, rating, weight_kg, listed_at, release_date, restock_time, title, is_active)
VALUES (10, 'SKU-KB-001', 24, 150, 48210, 4.6, 0.85, '2025-01-10 08:00:00', '2025-01-15', '06:00:00', 'Mechanical Keyboard', true);

INSERT INTO django_platform.products (category_id, sku, warranty_months, stock_qty, view_count, rating, weight_kg, listed_at, release_date, restock_time, title, is_active)
VALUES (10, 'SKU-MS-002', 12, 0, 15320, 4.1, 0.12, '2025-02-01 08:00:00', '2025-02-05', '07:30:00', 'Wireless Mouse', false);

INSERT INTO django_platform.products (category_id, sku, warranty_months, stock_qty, view_count, rating, weight_kg, listed_at, release_date, restock_time, title, is_active)
VALUES (20, 'SKU-MN-100', 36, 42, 98745, 4.9, 5.40, '2025-03-12 08:00:00', '2025-03-20', '05:45:00', '27-inch Monitor', true);

-- INVENTORY
INSERT INTO django_platform.inventory (warehouse_id, bin_id, aisle, quantity_on_hand, total_movements, fill_ratio, temperature_c, last_counted_at, expiry_date, last_scan_time, location_label, needs_restock)
VALUES (1, 501, 12, 340, 12890, 0.78, 21.5, '2025-08-01 22:00:00', '2026-08-01', '22:00:00', 'A-12-501', false);

INSERT INTO django_platform.inventory (warehouse_id, bin_id, aisle, quantity_on_hand, total_movements, fill_ratio, temperature_c, last_counted_at, expiry_date, last_scan_time, location_label, needs_restock)
VALUES (1, 502, 12, 15, 30541, 0.09, 21.7, '2025-08-01 22:05:00', '2025-12-31', '22:05:00', 'A-12-502', true);

INSERT INTO django_platform.inventory (warehouse_id, bin_id, aisle, quantity_on_hand, total_movements, fill_ratio, temperature_c, last_counted_at, expiry_date, last_scan_time, location_label, needs_restock)
VALUES (2, 210, 3, 900, 4021, 0.95, 4.0, '2025-08-02 06:30:00', '2027-01-15', '06:30:00', 'B-03-210', false);

-- SHIPMENTS
INSERT INTO django_platform.shipments (carrier_id, tracking_code, leg_no, leg_count, parcel_count, distance_meters, weight_kg, shipping_cost, dispatched_at, eta_date, pickup_time, status, is_delivered)
VALUES (7, 'TRK-AA-9001', 1, 3, 2, 480000, 3.20, 250.00, '2025-08-01 07:00:00', '2025-08-04', '07:00:00', 'in_transit', false);

INSERT INTO django_platform.shipments (carrier_id, tracking_code, leg_no, leg_count, parcel_count, distance_meters, weight_kg, shipping_cost, dispatched_at, eta_date, pickup_time, status, is_delivered)
VALUES (7, 'TRK-AA-9001', 2, 3, 2, 120000, 3.20, 250.00, '2025-08-02 11:20:00', '2025-08-04', '11:20:00', 'in_transit', false);

INSERT INTO django_platform.shipments (carrier_id, tracking_code, leg_no, leg_count, parcel_count, distance_meters, weight_kg, shipping_cost, dispatched_at, eta_date, pickup_time, status, is_delivered)
VALUES (9, 'TRK-BB-5500', 1, 1, 5, 65000, 12.75, 480.50, '2025-08-03 16:45:00', '2025-08-05', '16:45:00', 'delivered', true);
