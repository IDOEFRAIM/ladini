-- Tests SQL to validate migration_prisma_align_20260226.sql
-- Run after applying migration. These are non-destructive checks and simple inserts.

-- 1) Existence checks
SELECT 'users_email_exists' AS check, COUNT(*) FROM information_schema.columns WHERE table_name='users' AND column_name='email';
SELECT 'products_images_exists' AS check, COUNT(*) FROM information_schema.columns WHERE table_name='products' AND column_name='images';
SELECT 'clients_table' AS check, to_regclass('public.clients');
SELECT 'agent_actions_table' AS check, to_regclass('public.agent_actions');
SELECT 'accounts_table' AS check, to_regclass('public.accounts');

-- 2) Insert a minimal producer + product then select
BEGIN;
-- create a temporary producer for test
INSERT INTO producers (id, user_id, zone_id, status) VALUES ('test-producer-1', 'test-user-1', NULL, 'ACTIVE') ON CONFLICT (id) DO NOTHING;
INSERT INTO products (id, name, producer_id, price, unit, quantity_for_sale) VALUES ('test-product-1', 'Test Grain', 'test-producer-1', 100.0, 'kg', 10) ON CONFLICT (id) DO NOTHING;
SELECT id, name, price, unit, images FROM products WHERE id='test-product-1';
ROLLBACK; -- keep DB clean

-- 3) Simple agents audit insert (non-FK)
BEGIN;
INSERT INTO agent_audit_trails (id, agent_name, action_type, decision_payload) VALUES (gen_random_uuid(), 'TestAgent', 'TEST_ACTION', '{"note":"ok"}') RETURNING id, agent_name, action_type;
ROLLBACK;

-- 4) Orders alterations check
SELECT 'orders_cols' AS check, column_name FROM information_schema.columns WHERE table_name='orders' AND column_name IN ('buyer_id','client_id','payment_method','whatsapp_id','is_agent_order','agent_action_id');

-- End of tests
