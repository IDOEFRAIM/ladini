import sys

f = 'backend/src/agriconnect/services/models_v3.py'
with open(f, 'r', encoding='utf-8') as file:
    c = file.read()

# Replace the text inside class TransactionStaging:
target = """class TransactionStaging(Base):
    __tablename__ = "transaction_staging"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)"""
replacement = """class TransactionStaging(Base):
    __tablename__ = "transaction_staging"
    __table_args__ = {"schema": "marketplace"}

    id = Column(String, primary_key=True)"""

c = c.replace(target, replacement)

with open(f, 'w', encoding='utf-8') as file:
    file.write(c)

print("Patched TransactionStaging ID to String")
