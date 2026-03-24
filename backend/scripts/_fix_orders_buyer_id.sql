-- Fix orders.buyer_id type mismatch
ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_id UUID;
-- create index for buyer_id
CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_id);
