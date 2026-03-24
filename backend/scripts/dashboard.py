# AgriConnect Ingestion CLI Dashboard
# Provides observability into Redis Vector Store state.

import logging
import argparse
import sys
from datetime import datetime
from collections import defaultdict
from tqdm import tqdm

from agriconnect.rag.components import get_vector_store
from agriconnect.rag.config import TOP_K_RETRIEVAL

# Configure logging
logging.basicConfig(level=logging.ERROR) # Only crucial errors

def sizeof_fmt(num, suffix="B"):
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"

def check_redis_health():
    """Checks the number of vectors and metadata distribution in Redis."""
    print("🚀 AgriConnect Ingestion Dashboard")
    print("==================================\n")

    try:
        store = get_vector_store()
        client = store.client # Low-level Redis client
        
        # Check if index exists
        try:
            info = client.ft("agriconnect").info()
            num_docs = int(info["num_docs"])
            # hash_indexing_failures = int(info["hash_indexing_failures"])
            print(f"✅ Redis Connecté: OK")
            print(f"📊 Total Vectors: {num_docs}")

        except Exception as e:
            print(f"❌ Redis Index 'agriconnect' introuvable ou erreur: {e}")
            return

        # Simple aggregation to count by source/category
        # Note: FT.AGGREGATE is heavy on large datasets, use with caution or sampling.
        # For dashboard, we might just scan keys or use a small agg.
        
        print("\n🔍 Analyse de la Fraîcheur (Echantillon top 10)...")
        # Getting recent docs
        res = client.ft("agriconnect").search("*", limit=0, with_scores=False) # Total count check
        
        # Breakdown by category (using aggregation)
        req = client.ft("agriconnect").aggregate("*").groupby("@category", reduce_count=[]) 
        # Output: [Total, [cat1, count1], [cat2, count2]]
        
        stats = defaultdict(int)
        if req and len(req.rows) > 0:
            print("\n📂 Documents par Catégorie :")
            for row in req.rows:
                cat = row[1] if len(row) > 1 else "Unknown"
                count = row[3] if len(row) > 3 else 0 # Format depends on redis-py version
                # Usually row is [cat_key, cat_val, count_key, count_val]
                # Let's try flexible parsing or just listing
                print(f" - {row}")

        else:
             print(" (Aucune donnée d'agrégation disponible ou index vide)")

    except Exception as e:
        print(f"❌ Erreur critique lors de la connexion Redis: {e}")

if __name__ == "__main__":
    check_redis_health()
