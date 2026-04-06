from __future__ import annotations
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from agriconnect.core.schemas import RawDocument, ScraperLog

logger = logging.getLogger(__name__)

class Persister(ABC):
    """Interface de persistance pour le couple Document + Log."""

    @abstractmethod
    def save(self, doc: Optional[RawDocument], log: ScraperLog, category: Optional[str] = None) -> str:
        """Persiste le document et son log, retourne un identifiant unique (URI)."""
        pass

class FilePersister(Persister):
    """
    Persister local pour le développement et le debug.
    Structure : output_dir/category/doc_id.json
    """

    def __init__(self, output_dir: str = "backend/sources/raw_data"):
        self.base_dir = Path(output_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, doc: Optional[RawDocument], log: ScraperLog, category: Optional[str] = None) -> str:
        # Détermination du dossier cible (par catégorie ou scraper)
        target_dir = self.base_dir / (category or log.scraper_name)
        target_dir.mkdir(exist_ok=True)

        # Construction du payload avec model_dump (Pydantic V2)
        payload = {
            "document": doc.model_dump() if doc else None,
            "log": log.model_dump(),
            "metadata": {
                "category": category,
                "engine_version": "2.0.0"
            }
        }

        # Nommage déterministe basé sur l'ID (Hash) du document ou timestamp si échec
        if doc is not None:
            filename = f"{log.scraper_name}_{doc.id}.json"
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"failed_{log.scraper_name}_{timestamp}.json"
        file_path = target_dir / filename

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, sort_keys=True, indent=2)
            return str(file_path)
        except Exception as e:
            logger.error(f"Erreur d'écriture pour {filename}: {e}")
            return f"error://{filename}"

class InMemoryPersister(Persister):
    """
    Le mode 'Pure Usine' : Garde tout en RAM.
    Indispensable pour le pipeline S3/Postgres sans passer par le disque.
    """

    def __init__(self):
        self.store: List[Dict[str, Any]] = []

    def save(self, doc: Optional[RawDocument], log: ScraperLog, category: Optional[str] = None) -> str:
        entry = {
            "document": doc.model_dump() if doc else None,
            "log": log.model_dump(),
            "category": category
        }
        self.store.append(entry)
        index = len(self.store) - 1
        return f"memory://{index}"

    def clear(self):
        self.store = []