from __future__ import annotations
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

class CheckpointState(BaseModel):
    """État immuable et validé d'une session de scraping."""
    session_id: str
    started_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    
    # Files d'attente
    pending_urls: Dict[str, List[str]] = Field(default_factory=dict)
    completed_urls: Dict[str, Set[str]] = Field(default_factory=dict)
    failed_urls: Dict[str, Set[str]] = Field(default_factory=dict)
    
    # Statistiques
    total_urls: int = 0
    processed_urls: int = 0
    successful_urls: int = 0
    failed_urls_count: int = 0
    execution_count: int = 1
    total_duration_ms: float = 0.0
    total_duration_seconds: float = 0.0

    model_config = {"arbitrary_types_allowed": True}

    def to_json(self) -> str:
        # Conversion personnalisée pour gérer les Sets (non supportés par JSON standard)
        return self.model_dump_json(indent=2)

class CheckpointManager:
    """
    Gère la persistance de l'état. 
    Par défaut en local, mais conçu pour être étendu (S3/Redis).
    """
    def __init__(self, checkpoint_dir: Optional[Path] = None):
        self.checkpoint_dir = checkpoint_dir
        if self.checkpoint_dir:
            self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.state: Optional[CheckpointState] = None

    def _get_path(self, session_id: str) -> Path:
        if not self.checkpoint_dir:
            raise ValueError("CheckpointManager configuré sans répertoire de stockage.")
        return self.checkpoint_dir / f"checkpoint_{session_id}.json"

    def initialize_session(self, session_id: str, sources: Dict[str, List[str]]) -> CheckpointState:
        """Charge une session existante ou en crée une nouvelle (Source de Vérité)."""
        
        # Tentative de chargement si le répertoire existe
        if self.checkpoint_dir:
            path = self._get_path(session_id)
            if path.exists():
                try:
                    self.state = CheckpointState.model_validate_json(path.read_text(encoding="utf-8"))
                    self.state.execution_count += 1
                    self.state.last_updated = datetime.utcnow()
                    logger.info(f"Session {session_id} reprise (Essai {self.state.execution_count})")
                    return self.state
                except Exception as e:
                    logger.error(f"Échec du chargement du checkpoint : {e}. Création d'une nouvelle session.")

        # Initialisation à neuf
        self.state = CheckpointState(
            session_id=session_id,
            pending_urls={k: list(dict.fromkeys(v)) for k, v in sources.items()}, # Déduplication
            total_urls=sum(len(set(v)) for v in sources.values())
        )
        self.save()
        return self.state

    def save(self, state: Optional[CheckpointState] = None):
        """Persiste l'état actuel (si un répertoire est configuré)."""
        if state is not None:
            self.state = state
        if not self.state or not self.checkpoint_dir:
            return
        
        try:
            path = self._get_path(self.state.session_id)
            path.write_text(self.state.to_json(), encoding="utf-8")
        except Exception as e:
            logger.error(f"Impossible de sauvegarder le checkpoint : {e}")

    def update(self, category: str, url: str, success: bool, duration_ms: float = 0.0):
        """Mise à jour atomique après un scrap."""
        if not self.state:
            return

        # 1. Retrait de la file d'attente
        if url in self.state.pending_urls.get(category, []):
            self.state.pending_urls[category].remove(url)

        # 2. Mise à jour des registres et compteurs
        if success:
            self.state.completed_urls.setdefault(category, set()).add(url)
            self.state.successful_urls += 1
        else:
            self.state.failed_urls.setdefault(category, set()).add(url)
            self.state.failed_urls_count += 1

        self.state.processed_urls += 1
        self.state.total_duration_ms += duration_ms
        self.state.total_duration_seconds += duration_ms / 1000.0
        self.state.last_updated = datetime.utcnow()

    @property
    def is_finished(self) -> bool:
        if not self.state: return True
        return all(len(urls) == 0 for urls in self.state.pending_urls.values())

    def get_stats(self) -> dict:
        if not self.state: return {}
        total = self.state.total_urls if self.state.total_urls > 0 else 1
        return {
            "progress": f"{(self.state.processed_urls / total) * 100:.1f}%",
            "success": self.state.successful_urls,
            "failed": self.state.failed_urls_count,
            "remaining": self.state.total_urls - self.state.processed_urls
        }

    # Backward-compatible shims for master_harvester
    def load_or_create(self, session_id: str, sources: Dict[str, List[str]]) -> CheckpointState:
        return self.initialize_session(session_id, sources)

    def mark_completed(self, category: str, url: str, success: bool) -> None:
        self.update(category, url, success)
        self.save()

    def is_complete(self) -> bool:
        return self.is_finished

    def get_progress(self) -> dict:
        return self.get_stats()