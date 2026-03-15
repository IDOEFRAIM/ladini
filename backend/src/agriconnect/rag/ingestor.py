import os
import re
import unicodedata
import logging
import hashlib
from typing import List, Dict
from pathlib import Path

from llama_index.core import (
    Document, 
    VectorStoreIndex, 
    SimpleDirectoryReader, 
    StorageContext, 
    load_index_from_storage
)
from llama_index.core.node_parser import SentenceSplitter
from .config import RAW_DATA_DIR, DB_DIR
from .components import init_settings, get_storage_context, save_index

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Ingestor:
    def __init__(self):
        init_settings()
        # On ne charge pas le storage context ici pour éviter les conflits au reload
        self.chunk_size = 650
        self.chunk_overlap = 120

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

    def load_documents(self) -> List[Document]:
        """Charge les documents depuis les sources PDF et tous les sous-dossiers JSON."""
        # Utilisation de la configuration centralisée
        base_path = RAW_DATA_DIR # ex: backend/sources/raw_data

        if not os.path.exists(base_path):
            logger.warning(f"Répertoire source introuvable : {base_path}")
            return []

        docs_list = []

        # 1. Reader Récursif pour TOUT (PDF + JSON + TXT) dans raw_data et ses sous-dossiers
        # SimpleDirectoryReader avec recursive=True scanne tous les dossiers créés par les scrapers
        try:
            reader = SimpleDirectoryReader(
                input_dir=str(base_path),
                required_exts=[".pdf", ".json", ".txt"],  # Ajout du TXT pour news_articles et technical_resources
                file_metadata=self._metadata_helper,
                recursive=True,  # Scanne tous les sous-dossiers: soil_grids, fews_net, news_articles, fao_publications, etc.
                exclude=["*.tmp", "*.log", "*_meta.json", "scraping_report*.html"]  # Exclure les métadonnées et rapports HTML
            )
            docs_list = reader.load_data()
        except Exception as e:
            logger.warning(f"Erreur lors du chargement récursif : {e}")

        # IMPORTANT : On définit l'ID du document comme étant son nom de fichier + hash du contenu
        # Cela évite d'écraser les pages multiples d'un même fichier (203 docs vs 53 fichiers)
        for doc in docs_list:
            filename = doc.metadata.get("filename", "unknown")
            # Hash stable (MD5) pour unicité (page 1, page 2 etc.) et évite les doublons
            content_bytes = doc.text.encode('utf-8', errors='ignore')
            content_hash = hashlib.md5(content_bytes).hexdigest()
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
    
    def build_index(self):
        """
        Logique d'Idempotence Manuelle :
        1. Charge l'index existant via components (FAISS).
        2. Compare les fichiers entrants avec ceux déjà dans l'index.
        3. Ne traite (Split -> Enrich -> Insert) QUE les nouveaux fichiers.
        """
        # 1. Chargement des fichiers sources
        source_docs = self.load_documents()
        if not source_docs:
            logger.warning("Aucun document source trouvé.")
            return None

        index = None
        new_docs = []

        # 2. Tentative de chargement de l'index existant
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

        # 3. Traitement
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
            
            logger.info(f"✅ Sauvegarde terminée dans {DB_DIR}")
        else:
            logger.info("✅ L'index est déjà à jour.")

        return index

if __name__ == "__main__":
    ingestor = Ingestor()
    ingestor.build_index()