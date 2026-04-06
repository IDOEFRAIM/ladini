import sqlite3
import json
import logging
import hashlib
import time
import os
from typing import Dict, Any, Optional, List

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ScraperOrchestrator")

# ======================================================================================
# 1. STORAGE MANAGER (StorageManager)
#    Used as context manager (via 'with') for DB safety.
# ======================================================================================

class StorageManager:
    """
    Centralized storage/cache manager using SQLite.
    Ensures persistence of raw data (raw_agent_data) and caches (agent_cache).
    Implements the context manager protocol.
    """

    def __init__(self, db_path: str = "data/agconnect_cache.db"):
        self.conn: Optional[sqlite3.Connection] = None
        self.db_path = db_path
        
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            self.conn = self._connect() 
            self._init_schema()
            
            logging.getLogger("StorageManager").info("StorageManager initialisé avec succès.")
        except Exception as e:
            logging.getLogger("StorageManager").error(f"❌ Erreur critique lors de l'initialisation : {e}")
            if self.conn:
                self.conn.close()
            self.conn = None

    def _connect(self) -> sqlite3.Connection:
        """Établit la connexion et définit la row_factory pour l'accès par nom de colonne."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row 
        return conn

    def _init_schema(self):
        """Initialise les tables de la base de données."""
        if not self.conn: return

        cursor = self.conn.cursor()
        try:
            # Table agent_cache (Cache des résultats intermédiaires)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    category TEXT NOT NULL, 
                    zone_id TEXT NOT NULL,
                    payload TEXT NOT NULL, 
                    created_at REAL NOT NULL
                )
            """)
            
            # Table raw_agent_data (Historique des données brutes scrapées)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS raw_agent_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id TEXT NOT NULL,
                    agent_category TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    effective_date REAL NOT NULL,
                    source_url TEXT,
                    data_hash TEXT UNIQUE,
                    insertion_date REAL DEFAULT (strftime('%s', 'now'))
                )
            """)
            
            self.conn.commit()
        except sqlite3.Error as e:
            logging.getLogger("StorageManager").error(f"❌ Erreur SQLite lors de l'init du schéma : {e}")

    def _hash_data(self, data: Dict[str, Any]) -> str:
        """Génère un hash unique pour un dictionnaire de données (pour déduplication)."""
        json_string = json.dumps(data, sort_keys=True)
        return hashlib.sha256(json_string.encode('utf-8')).hexdigest()

    # Context Manager
    def __enter__(self) -> 'StorageManager':
        if self.conn is None:
            # Tentative de reconnexion si nécessaire
            self.conn = self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        """Ferme la connexion à la base de données."""
        if self.conn:
            try:
                self.conn.close()
                self.conn = None
            except sqlite3.Error:
                pass 

    def save_raw_data(self, zone_id: str, category: str, data: Dict[str, Any], effective_date: float, source_url: Optional[str] = None) -> bool:
        """Sauvegarde les données brutes avec déduplication (INSERT OR IGNORE)."""
        if not self.conn: return False

        try:
            cursor = self.conn.cursor()
            json_content = json.dumps(data, ensure_ascii=False)
            data_hash = self._hash_data(data)
            
            cursor.execute("""
                INSERT OR IGNORE INTO raw_agent_data (zone_id, agent_category, content_json, effective_date, source_url, data_hash)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (zone_id, category, json_content, effective_date, source_url, data_hash))
            
            rows_inserted = cursor.rowcount
            self.conn.commit()
            
            return rows_inserted > 0
                
        except sqlite3.Error as e:
            logging.getLogger("StorageManager").error(f"❌ Erreur SQLite lors de la sauvegarde ({category}/{zone_id}): {e}")
            self.conn.rollback()
            return False

    def get_raw_data(self, zone_id: str, category: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Récupère les données brutes triées par date d'effet la plus récente."""
        if not self.conn: return []
            
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT content_json, effective_date, source_url 
                FROM raw_agent_data 
                WHERE zone_id = ? AND agent_category = ?
                ORDER BY effective_date DESC 
                LIMIT ?
            """, (zone_id, category, limit))
            
            results = []
            for row in cursor.fetchall():
                data = json.loads(row['content_json'])
                data['effective_date'] = row['effective_date']
                data['source_url'] = row['source_url']
                results.append(data)
                
            return results
        except sqlite3.Error as e:
            logging.getLogger("StorageManager").error(f"❌ Erreur SQLite lors de la récupération ({category}/{zone_id}): {e}")
            return []

    # --- MÉTHODES DE CACHE INTELLIGENT (Smart Caching) ---

    def get_agent_cache(self, query: str, agent_role: str, zone_id: str, ttl_minutes: int = 60) -> Optional[List[Dict[str, Any]]]:
        """Récupère une réponse cached valide pour une requête d'agent spécifique."""
        if not self.conn: return None

        query_hash = hashlib.md5(f"{agent_role}:{zone_id}:{query.strip().lower()}".encode()).hexdigest()
        cutoff_time = time.time() - (ttl_minutes * 60)
        
        try:
            cursor = self.conn.cursor()
            # Récupérer les candidats potentiels (récents)
            cursor.execute("""
                SELECT payload, created_at 
                FROM agent_cache 
                WHERE category = ? AND zone_id = ? AND created_at > ?
                ORDER BY created_at DESC
            """, (agent_role, zone_id, cutoff_time))
            
            rows = cursor.fetchall()
            for row in rows:
                try:
                    payload = json.loads(row['payload'])
                    # Vérification stricte du Hash pour éviter les collisions ou faux positifs
                    if payload.get("query_hash") == query_hash:
                        logging.getLogger("StorageManager").info(f"✅ Cache Hit pour {agent_role}/{zone_id}")
                        return payload.get("results")
                except json.JSONDecodeError:
                    continue
                    
            return None

        except sqlite3.Error as e:
            logging.getLogger("StorageManager").error(f"❌ Erreur lecture cache: {e}")
            return None

    def set_agent_cache(self, query: str, agent_role: str, zone_id: str, results: List[Dict[str, Any]]):
        """Sauvegarde les résultats d'un retrieval pour utilisation future."""
        if not self.conn: return
        
        query_hash = hashlib.md5(f"{agent_role}:{zone_id}:{query.strip().lower()}".encode()).hexdigest()
        payload = json.dumps({"query_hash": query_hash, "original_query": query, "results": results}, ensure_ascii=False)
        
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO agent_cache (category, zone_id, payload, created_at)
                VALUES (?, ?, ?, ?)
            """, (agent_role, zone_id, payload, time.time()))
            self.conn.commit()
        except sqlite3.Error as e:
            logging.getLogger("StorageManager").error(f"❌ Erreur SQLite lors de l'écriture cache: {e}")
