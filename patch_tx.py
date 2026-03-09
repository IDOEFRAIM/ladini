import sys

f = 'backend/src/agriconnect/services/models_v3.py'
with open(f, 'r', encoding='utf-8') as file:
    c = file.read()

c = c.replace('transaction_id = Column("transaction_id", Uuid(as_uuid=False)', 'transaction_id = Column("transaction_id", String')

with open(f, 'w', encoding='utf-8') as file:
    file.write(c)

print("Patched transaction_id!")
