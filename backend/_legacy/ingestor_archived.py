import os
import re
import unicodedata
import logging
import hashlib
from datetime import datetime, timezone
from typing import Any, List, Dict, Optional
from pathlib import Path
import tempfile
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from llama_index.core import (
    Document, 
    VectorStoreIndex, 
    SimpleDirectoryReader, 
    StorageContext, 
    load_index_from_storage
)
from llama_index.core.node_parser import SentenceSplitter

try:
    from .config.old_root_config import RAW_DATA_DIR
except ImportError:
    from .config.old_root_config import RAW_DATA_DIR

# Define DB_DIR directly if missing from config
DB_DIR = Path("rag_db/index_storage")

from agriconnect.rag.components import init_settings, get_vector_store

def get_storage_context():
    return StorageContext.from_defaults(vector_store=get_vector_store())

def save_index(index):
    # Persist explicitly if valid directory
    if DB_DIR:
        index.storage_context.persist(persist_dir=str(DB_DIR))

from agriconnect.core.settings import settings

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class IngestionStateRepository:
    """DB-backed state tracker used to prevent re-ingestion of unchanged files."""

    def __init__(self, db_url: Optional[str]):
        self._db_url = db_url
        self._engine = None
        self._session_factory = None
        if not db_url:
            return
        try:
            self._engine = create_engine(db_url, pool_pre_ping=True)
            self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)
            self._ensure_schema()
        except Exception as exc:
            logger.warning("Ingestion state DB disabled (init error): %s", exc)
            self._engine = None
            self._session_factory = None

    @property
    def enabled(self) -> bool:
        return self._session_factory is not None

    def _ensure_schema(self) -> None:
        if not self.enabled:
            return
        with self._session_factory() as session:
            session.execute(text("CREATE SCHEMA IF NOT EXISTS ingestion"))
            session.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS ingestion.document_state (
                        file_path TEXT PRIMARY KEY,
                        md5_hash CHAR(32) NOT NULL,
                        status TEXT NOT NULL DEFAULT 'INGESTED',
                        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            )
            session.commit()

    def get_known_hashes(self, file_paths: List[str]) -> Dict[str, str]:
        if not self.enabled or not file_paths:
            return {}

        query = text(
            """
            SELECT file_path, md5_hash
            FROM ingestion.document_state
            WHERE file_path = ANY(:file_paths)
            """
        )
        with self._session_factory() as session:
            rows = session.execute(query, {"file_paths": file_paths}).fetchall()
        return {r[0]: r[1] for r in rows}

    def upsert_hashes(self, hash_by_file: Dict[str, str], status: str = "INGESTED") -> None:
        if not self.enabled or not hash_by_file:
            return
        query = text(
            """
            INSERT INTO ingestion.document_state (file_path, md5_hash, status, first_seen_at, last_seen_at, last_ingested_at)
            VALUES (:file_path, :md5_hash, :status, NOW(), NOW(), NOW())
            ON CONFLICT (file_path)
            DO UPDATE SET
                md5_hash = EXCLUDED.md5_hash,
                status = EXCLUDED.status,
                last_seen_at = NOW(),
                last_ingested_at = NOW()
            """
        )
        with self._session_factory() as session:
            for file_path, md5_hash in hash_by_file.items():
                session.execute(query, {"file_path": file_path, "md5_hash": md5_hash, "status": status})
            session.commit()

class Ingestor:
    def __init__(
        self,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
        state_db_url: Optional[str] = None,
    ):
        init_settings()
        # On ne charge pas le storage context ici pour éviter les conflits au reload
        env_chunk_size = os.getenv("AGRICONNECT_CHUNK_SIZE")
        env_chunk_overlap = os.getenv("AGRICONNECT_CHUNK_OVERLAP")
        self.chunk_size = int(chunk_size or env_chunk_size or 650)
        self.chunk_overlap = int(chunk_overlap or env_chunk_overlap or 120)
        self.state_repo = IngestionStateRepository(state_db_url or settings.DATABASE_URL)

    def _metadata_helper(self, file_path: str) -> Dict:
        """Extrait les métadonnées basées sur le nom du fichier et la structure des dossiers."""
        filename = os.path.basename(file_path)
        path_parts = Path(file_path).parts
        
        # Déterminer la catégorie et le type de document selon le dossier
        if "news_articles" in path_parts:
            doc_type = "news_article"
            category = "actualites_agricoles"
        elif "fao_publications" in path_parts:
            doc_type = "fao_publication"
            category = "recherche_fao"
        elif "technical_resources" in path_parts:
            doc_type = "technical_resource"
            category = "ressources_techniques"
        elif "data_platforms" in path_parts:
            doc_type = "statistical_data"
            category = "donnees_statistiques"
        elif "fews_net" in path_parts:
            doc_type = "fews_report"
            category = "securite_alimentaire"
        elif "soil_grids" in path_parts:
            doc_type = "soil_data"
            category = "pedologie"
        elif "bulletin" in filename.lower() or "meteo" in filename.lower():
            doc_type = "weather_bulletin"
            category = "climat"
        else:
            doc_type = "agronomy_knowledge"
            category = "technique_culturale"
        
        # Déterminer la source plus précisément
        source_folder = None
        for part in path_parts:
            if part in ["news_articles", "fao_publications", "technical_resources", 
                       "data_platforms", "fews_net", "soil_grids"]:
                source_folder = part
                break
        
        return {
            "filename": filename,
            "doc_type": doc_type,
            "category": category,
            "source_folder": source_folder or "raw_data",
            "file_path": str(file_path)
        }

    def _iter_source_files(self) -> List[Path]:
        # Support local path OR s3:// prefix
        raw = RAW_DATA_DIR
        try:
            raw_s = str(raw)
        except Exception:
            raw_s = raw

        # If path is an S3 URI, download objects under prefix to a temp dir
        if isinstance(raw_s, str) and raw_s.startswith("s3://"):
            try:
                import boto3
                s3 = boto3.resource('s3')
                parsed = urlparse(raw_s)
                bucket = parsed.netloc
                prefix = parsed.path.lstrip('/')
                tmpdir = Path(tempfile.mkdtemp(prefix='agriconnect_raw_'))
                bucket_obj = s3.Bucket(bucket)
                for obj in bucket_obj.objects.filter(Prefix=prefix):
                    key = obj.key
                    if key.endswith('/'):
                        continue
                    dest = tmpdir / Path(key).name
                    bucket_obj.download_file(key, str(dest))
                base_path = tmpdir
            except Exception as e:
                logger.warning("S3 download failed: %s", e)
                return []
        else:
            base_path = Path(raw)

        if not base_path.exists():
            return []
        out: List[Path] = []
        for p in base_path.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in {".pdf", ".json", ".txt"}:
                continue
            if p.name.endswith("_meta.json"):
                continue
            if p.name.startswith("scraping_report") and p.suffix.lower() == ".html":
                continue
            out.append(p)
        return out

    def _compute_md5(self, file_path: Path) -> str:
        md5 = hashlib.md5()
        with file_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                md5.update(chunk)
        return md5.hexdigest()

    def _discover_changed_files(self, force_reingest: bool = False) -> Dict[str, str]:
        files = self._iter_source_files()
        if not files:
            return {}

        current_hashes: Dict[str, str] = {}
        for p in files:
            try:
                current_hashes[str(p)] = self._compute_md5(p)
            except Exception as exc:
                logger.warning("Unable to hash file %s: %s", p, exc)

        if force_reingest or not self.state_repo.enabled:
            return current_hashes

        known = self.state_repo.get_known_hashes(list(current_hashes.keys()))
        changed = {fp: h for fp, h in current_hashes.items() if known.get(fp) != h}
        return changed

    def load_documents(self, only_files: Optional[List[str]] = None, file_hashes: Optional[Dict[str, str]] = None) -> List[Document]:
        """Charge les documents depuis les sources PDF et tous les sous-dossiers JSON."""
        # Utilisation de la configuration centralisée
        base_path = RAW_DATA_DIR # ex: backend/sources/raw_data

        # If RAW_DATA_DIR is an S3 URI we expect _iter_source_files to have downloaded files
        if isinstance(base_path, str) and base_path.startswith('s3://'):
            logger.info('RAW_DATA_DIR points to S3; files should be downloaded by _iter_source_files')

        if not os.path.exists(str(base_path)):
            logger.warning(f"Répertoire source introuvable : {base_path}")
            return []

        docs_list = []

        # 1. Reader Récursif pour TOUT (PDF + JSON + TXT) dans raw_data et ses sous-dossiers
        # SimpleDirectoryReader avec recursive=True scanne tous les dossiers créés par les scrapers
        try:
            reader_kwargs = {
                "required_exts": [".pdf", ".json", ".txt"],
                "file_metadata": self._metadata_helper,
                "exclude": ["*.tmp", "*.log", "*_meta.json", "scraping_report*.html"],
            }
            if only_files:
                reader = SimpleDirectoryReader(
                    input_files=[str(p) for p in only_files],
                    **reader_kwargs,
                )
            else:
                reader = SimpleDirectoryReader(
                    input_dir=str(base_path),
                    recursive=True,
                    **reader_kwargs,
                )
            docs_list = reader.load_data()
        except Exception as e:
            logger.warning(f"Erreur lors du chargement récursif : {e}")

        # IMPORTANT : On définit l'ID du document comme étant son nom de fichier + hash du contenu
        # Cela évite d'écraser les pages multiples d'un même fichier (203 docs vs 53 fichiers)
        for doc in docs_list:
            filename = doc.metadata.get("filename", "unknown")
            file_path = str(doc.metadata.get("file_path") or "")
            file_md5 = None
            if file_hashes and file_path:
                file_md5 = file_hashes.get(file_path)
            if not file_md5 and file_path and os.path.exists(file_path):
                try:
                    file_md5 = self._compute_md5(Path(file_path))
                except Exception:
                    file_md5 = None
            # Hash stable (MD5) pour unicité (page 1, page 2 etc.) et évite les doublons
            content_bytes = doc.text.encode('utf-8', errors='ignore')
            content_hash = hashlib.md5(content_bytes).hexdigest()
            if file_md5:
                doc.metadata["file_md5"] = file_md5
                doc.id_ = f"{file_md5}_{content_hash}"
            else:
                doc.id_ = f"{filename}_{content_hash}"

        logger.info(f"Chargement source : {len(docs_list)} documents trouvés (PDF & JSON).")
        return docs_list

    def enrich_nodes(self, nodes):
        """Pipeline d'enrichissement sémantique (Contextual Chunking)."""
        canonical_map = {
            "mais": "maïs", "niebe": "niébé",
            "secheresse": "sécheresse", "temperature": "température",
            "humidite": "humidité", "innondation": "inondation"
        }

        def normalize(text):
            if not text: return ""
            nfd = unicodedata.normalize('NFD', text)
            return ''.join(c for c in nfd if not unicodedata.combining(c)).lower()

        def _clean_excerpt(text: str) -> str:
            if not text:
                return ""
            # remove JSON-like fragments (simple heuristic)
            try:
                text = re.sub(r"\{[^\}]{0,800}\}", " ", text)
            except Exception:
                pass
            # remove URLs
            text = re.sub(r"https?://\S+", " ", text)
            # remove Windows-style absolute paths like C:\Users\... or C:/path
            text = re.sub(r"[A-Za-z]:\\\\[^\s]{2,300}", " ", text)
            text = re.sub(r"[A-Za-z]:/[^\s]{2,300}", " ", text)
            # remove unix-like file paths ending with common extensions
            text = re.sub(r"/[^\s]{3,300}\\.(?:pdf|txt|json)", " ", text)
            # collapse whitespace and strip
            text = re.sub(r"\s+", " ", text).strip()
            return text

        for node in nodes:
            doc_type = node.metadata.get("doc_type", "inconnu")
            category = node.metadata.get("category", "général")
            filename = node.metadata.get("filename", "source")
            source_folder = node.metadata.get("source_folder", "")

            # Header Contextuel enrichi avec la catégorie et source
            header = f"CONTEXTE DOCUMENTAIRE : [Source: {filename}, Type: {doc_type}, Catégorie: {category}"
            if source_folder:
                header += f", Dossier: {source_folder}"
            header += "]\n"
            
            # Extraction Mots-clés
            content_norm = normalize(node.get_content())
            
            # Cultures mentionnées
            crops_hits = re.findall(r"(mais|sorgho|mil|coton|arachide|niebe|riz|oignon|tomate|manioc|sesame)", content_norm)
            # Facteurs météorologiques
            weather_hits = re.findall(r"(pluie|secheresse|inondation|innondation|temperature|vent|humidite|climat)", content_norm)
            # Thématiques agricoles (nouvelles avec les scrapers)
            themes_hits = re.findall(r"(marche|prix|subvention|irrigation|semences|engrais|pesticide|bio|elevage)", content_norm)

            crops = list(set([canonical_map.get(c, c) for c in crops_hits]))
            weather = list(set([canonical_map.get(w, w) for w in weather_hits]))
            themes = list(set(themes_hits))

            node.metadata["crops"] = crops
            node.metadata["weather_factors"] = weather
            node.metadata["themes"] = themes
            
            # Marqueur de pertinence selon le type de document
            if doc_type in ["news_article", "fao_publication", "technical_resource"]:
                node.metadata["priority"] = "high"  # Nouveaux contenus scraped
            elif doc_type in ["weather_bulletin", "fews_report"]:
                node.metadata["priority"] = "medium"
            else:
                node.metadata["priority"] = "normal"

                # Ensure canonical path/title/source metadata for downstream consumers
                file_path = node.metadata.get("file_path") or node.metadata.get("file_path", None)
                node.metadata["file_path"] = str(file_path) if file_path else node.metadata.get("file_path")
                node.metadata["source"] = node.metadata.get("source") or node.metadata.get("file_path") or filename
                # Derive a readable title from filename when absent
                try:
                    base = os.path.basename(filename)
                    title = os.path.splitext(base)[0]
                except Exception:
                    title = filename
                node.metadata["title"] = node.metadata.get("title") or title

                # Clean the excerpt/text to remove extraction artifacts
                cleaned = _clean_excerpt(node.get_content())
                node.text = header + cleaned
            
        return nodes
    
    def build_index(
        self,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
        force_reingest: bool = False,
    ):
        """
        Logique d'Idempotence Manuelle :
        1. Charge l'index existant via components (FAISS).
        2. Compare les fichiers entrants avec ceux déjà dans l'index.
        3. Ne traite (Split -> Enrich -> Insert) QUE les nouveaux fichiers.
        """
        if chunk_size:
            self.chunk_size = int(chunk_size)
        if chunk_overlap:
            self.chunk_overlap = int(chunk_overlap)

        # 1. Détection des fichiers modifiés (idempotence par MD5)
        changed_files = self._discover_changed_files(force_reingest=force_reingest)
        if not changed_files and not force_reingest:
            logger.info("✅ Aucun changement détecté dans les documents source (MD5 stable).")
            return None

        # 2. Chargement des documents modifiés
        source_docs = self.load_documents(only_files=list(changed_files.keys()), file_hashes=changed_files)
        if not source_docs:
            logger.warning("Aucun document source trouvé.")
            return None

        index = None
        new_docs = []

        # 3. Tentative de chargement de l'index existant
        # On utilise get_storage_context qui configure déjà FAISS et la persistence
        storage_context = get_storage_context()
        
        try:
            # On tente de charger l'index si les infos de structure existent
            # Note: get_storage_context regarde si docstore.json existe pour attacher un persist_dir
            # Si persist_dir est défini, load_index_from_storage fonctionnera.
            index = load_index_from_storage(storage_context)
            
            # Récupération des IDs déjà présents
            existing_doc_ids = index.ref_doc_info.keys()
            
            # Filtrage
            new_docs = [d for d in source_docs if d.id_ not in existing_doc_ids]

            # Force rebuild if environment requests it
            try:
                import os as _os
                if _os.environ.get("AGRICONNECT_FORCE_REBUILD") == "1":
                    logger.info("AGRICONNECT_FORCE_REBUILD=1 -> Forcing full reprocessing of source documents.")
                    new_docs = source_docs
            except Exception:
                pass

            logger.info(f"Index FAISS chargé. {len(existing_doc_ids)} docs existants. {len(new_docs)} nouveaux à traiter.")
            
        except Exception as e:
            logger.info(f"Pas d'index existant ou erreur ({e}). Création d'un nouvel index FAISS.")
            new_docs = source_docs # Tout traiter

        # 4. Traitement
        if new_docs:
            logger.info(f"Traitement de {len(new_docs)} documents...")
            
            # A. Splitting
            parser = SentenceSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
            nodes = parser.get_nodes_from_documents(new_docs)
            
            # B. Enrichissement
            nodes = self.enrich_nodes(nodes)
            
            # C. Insertion
            if index is None:
                index = VectorStoreIndex(nodes, storage_context=storage_context)
            else:
                index.insert_nodes(nodes)
            
            # D. Sauvegarde
            # On sauvegarde le StorageContext (Docstore, IndexStore)
            index.storage_context.persist(persist_dir=str(DB_DIR))
            
            # Et on force la sauvegarde FAISS (géré par components.save_index ou implicite ?)
            # FaissVectorStore de LlamaIndex a une méthode persist, mais components.save_index le fait manuellement
            # Vérifions save_index dans components
            save_index(index)

            # Marquer uniquement les fichiers effectivement traités.
            if changed_files:
                self.state_repo.upsert_hashes(changed_files, status="INGESTED")
            
            logger.info(f"✅ Sauvegarde terminée dans {DB_DIR}")
        else:
            logger.info("✅ L'index est déjà à jour.")

        return index

if __name__ == "__main__":
    ingestor = Ingestor()
    ingestor.build_index()