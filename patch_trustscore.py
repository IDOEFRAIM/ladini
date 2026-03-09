import sys

f = 'backend/src/agriconnect/services/models_v3.py'
with open(f, 'r', encoding='utf-8') as file:
    c = file.read()

c = c.replace('"quality_index": self.quality_index,', '"quality_index": self.quality_index, "compliance_index": self.compliance_index, "resilience_bonus": self.resilience_bonus,')

with open(f, 'w', encoding='utf-8') as file:
    file.write(c)

print("Patched models_v3.py for TrustScore.to_dict()")
