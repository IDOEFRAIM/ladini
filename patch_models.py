import re

f = 'backend/src/agriconnect/services/models_v3.py'
with open(f, 'r', encoding='utf-8') as file:
    c = file.read()

# Add Uuid import
if 'Uuid' not in c:
    c = c.replace('from sqlalchemy import Column', 'from sqlalchemy import Column, Uuid')

# Make `id = Column(String` use Uuid
c = re.sub(r'id\s*=\s*Column\(String,\s*primary_key=True\)', 'id = Column(Uuid(as_uuid=False), primary_key=True)', c)
c = re.sub(r'id\s*=\s*Column\([\'"]id[\'"],\s*String,\s*primary_key=True\)', 'id = Column("id", Uuid(as_uuid=False), primary_key=True)', c)

# Make foreign keys like `user_id = Column("user_id", String` use Uuid
c = re.sub(r'Column\((["\'][A-Za-z0-9_]+_id["\']),\s*String', r'Column(\1, Uuid(as_uuid=False)', c)

with open(f, 'w', encoding='utf-8') as file:
    file.write(c)

print("Patched!")
