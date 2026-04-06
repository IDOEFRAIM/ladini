
import sys
from pathlib import Path
# Add project source to path (3 levels up from <category>/script.py -> backend/src)
sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

import re
import csv

vcf_path = r"c:/Users/LENOVO T14s/AppData/Local/Packages/5319275A.WhatsAppDesktop_cv1g1gvanyjgm/LocalState/sessions/EDDDD72610A2CB36B85915CC1E8FF31A86ED28E5/transfers/2026-07/vCards iCloud.vcf"
csv_path = r"c:/Users/LENOVO T14s/Documents/projet/Agribot-AI/contacts.csv"


# Utilisation d'un set pour éviter les doublons de numéro (le nom peut être répété)
phones_set = set()
contacts = []

with open(vcf_path, encoding="utf-8") as f:
    vcard = {}
    for line in f:
        line = line.strip()
        if line.startswith("FN:"):
            vcard["name"] = line[3:].strip('"')
        elif line.startswith("TEL"):
            match = re.search(r"([+]?\d{8,15})", line)
            if match:
                vcard.setdefault("phones", []).append(match.group(1))
        elif line == "END:VCARD":
            if "name" in vcard and "phones" in vcard:
                for phone in vcard["phones"]:
                    if phone not in phones_set:
                        phones_set.add(phone)
                        contacts.append({"name": vcard["name"], "phone": phone})
            vcard = {}

with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
    writer = csv.DictWriter(csvfile, fieldnames=["name", "phone"])
    writer.writeheader()
    for contact in contacts:
        writer.writerow(contact)

print(f"Exported {len(contacts)} contacts to {csv_path}")
