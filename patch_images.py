import sys

f = 'backend/src/agriconnect/services/models_v3.py'
with open(f, 'r', encoding='utf-8') as file:
    c = file.read()

if 'from sqlalchemy.dialects.postgresql import ARRAY' not in c:
    c = c.replace('from sqlalchemy import Column, Uuid, Integer, String, Float, DateTime, Boolean, ForeignKey, func, Text, JSON', 'from sqlalchemy import Column, Uuid, Integer, String, Float, DateTime, Boolean, ForeignKey, func, Text, JSON\nfrom sqlalchemy.dialects.postgresql import ARRAY')

c = c.replace('images = Column(JSON, default=list)', 'images = Column(ARRAY(String), default=list)')

with open(f, 'w', encoding='utf-8') as file:
    file.write(c)

print("Patched Product.images to use ARRAY(String)")