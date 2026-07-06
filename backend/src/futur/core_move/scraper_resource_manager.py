from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List


class ResourceManager:
    """
    Collecteur de résultats en mémoire pour l'orchestration des scrapers.
    Zéro effet de bord (pas d'écriture disque).
    """

    def __init__(self):
        # On ne passe plus de 'output_dir' ici.
        self.catalog: List[Dict[str, Any]] = []
        self.start_time = datetime.now(timezone.utc)

    def add_resource(self, document: Any, log: Any) -> None:
        """
        Archive le couple (Document, Log) pour le rapport final.
        On utilise les objets Pydantic directement.
        """
        entry = {
            "document_id": document.id if document else None,
            "url": str(document.url) if document is not None and hasattr(document, "url") else "unknown",
            "success": log.success,
            "duration_ms": log.duration_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            # On peut optionnellement stocker le log complet pour analyse
            "log_details": log.model_dump() if hasattr(log, 'model_dump') else log
        }
        self.catalog.append(entry)

    def get_final_status(self) -> Dict[str, Any]:
        """
        Retourne le bilan de l'opération en mémoire.
        C'est cet objet que l'Ingestion pourra transformer en fichier JSON sur S3.
        """
        success_count = sum(1 for r in self.catalog if r["success"])
        return {
            "execution_summary": {
                "start_time": self.start_time.isoformat(),
                "end_time": datetime.now(timezone.utc).isoformat(),
                "total_processed": len(self.catalog),
                "success_count": success_count,
                "failure_count": len(self.catalog) - success_count,
            },
            "details": self.catalog
        }

    def clear(self) -> None:
        """Vide le catalogue après ingestion pour libérer la RAM."""
        self.catalog = []