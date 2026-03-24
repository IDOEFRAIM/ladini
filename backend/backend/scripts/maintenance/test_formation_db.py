import sys
import os

# Fix Windows console encoding
sys.stdout.reconfigure(encoding='utf-8')

# Add backend/src to path
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agriconnect.tools.formation_advisor import FormationAdvisor
import logging

logging.basicConfig(level=logging.INFO)

advisor = FormationAdvisor()

# Test 1: Maïs Barka (Zone Centre) - Now from DB
print("=== TEST 1: Maïs (Centre, 1.0 ha) FROM DB ===")
canvas = advisor.generate_technical_diagnosis("maïs", "Centre", 1.0)
print(advisor.format_as_markdown(canvas))

# Test 2: Niébé (Zone Nord) - DB check (Should default to generic if not specific found, or finding Niebe generic)
# In migration I added 'niebe-komcalle-centre'. Let's see if it works for Nord if I implemented flexible search.
# My implementation tries "Centre" as default if exact match fails? No, my implementation tries `crop` + `zone` then fallback to `crop`.
# 'niebe-komcalle-centre' has `zone_category='Centre'`.
# If I search for 'Niébé' in 'Nord', it will first try `crop=Niébé, zone=Nord`. Fail.
# Then fallback `crop=Niébé`. It might return 'niebe-komcalle-centre' because it matches crop name.
print("\n=== TEST 2: Niébé (Nord, 0.5 ha) FROM DB FALLBACK ===")
canvas = advisor.generate_technical_diagnosis("niébé", "Nord", 0.5)
print(advisor.format_as_markdown(canvas))

print("\n=== TEST 3: Sésame (Centre, 2.0 ha) FROM DB EXPANSION ===")
canvas = advisor.generate_technical_diagnosis("sésame", "Centre", 2.0)
print(advisor.format_as_markdown(canvas))
