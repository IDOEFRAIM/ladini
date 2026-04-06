BEGIN;
INSERT INTO users (id, name, phone, role, "createdAt", "updatedAt") VALUES ('test-user-producer-20260325', 'Producer Test', '+223700000000', 'PRODUCER', now(), now());
INSERT INTO producers (id, "userId", "businessName", status, "createdAt", "updatedAt") VALUES ('test-producer-20260325', 'test-user-producer-20260325', 'ProducerCo', 'ACTIVE', now(), now());
COMMIT;
