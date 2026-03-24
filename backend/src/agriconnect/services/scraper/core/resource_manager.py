"""
Resource Manager - Architecture centralisée pour orchestrer le scraping de ressources diversifiées.

Philosophy:
- Modularité: Chaque type de ressource a son propre scraper spécialisé
- Robustesse: Gestion d'erreurs, retry, timeout pour chaque source
- Traçabilité: Metadata complète pour chaque ressource scrapée
- Extensibilité: Facile d'ajouter de nouveaux types de sources

Structure de données standardisée:
{
    "source_type": str,  # "google", "pdf", "doi", "news", "data_platform", "technical"
    "url": str,
    "title": str,
    "content": str,
    "metadata": {
        "scraped_at": datetime,
        "content_length": int,
        "file_path": str (si téléchargement),
        "error": str (si échec)
    }
}
"""

import logging
from agriconnect.core.settings import settings
from agriconnect.utils.s3_utils import upload_file_to_s3
import json
import os
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# Configuration
BASE_OUTPUT_DIR = "backend/sources/raw_data"
RESOURCE_CATALOG_FILE = "backend/sources/resource_catalog.json"
MAX_WORKERS = 5
RETRY_ATTEMPTS = 3
REQUEST_TIMEOUT = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ResourceManager")


class ResourceManager:
    """
    Orchestrateur principal pour gérer l'exploration et le scraping de ressources diversifiées.
    """

    def __init__(self, output_dir: str = BASE_OUTPUT_DIR):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.catalog_file = Path(RESOURCE_CATALOG_FILE)
        self.catalog_file.parent.mkdir(parents=True, exist_ok=True)
        
        self.catalog: List[Dict[str, Any]] = self._load_catalog()
        
        # Statistiques de session
        self.session_stats = {
            "start_time": datetime.now(),
            "total_sources": 0,
            "successful": 0,
            "failed": 0,
            "skipped": 0,
            "by_type": {}
        }

    def _load_catalog(self) -> List[Dict[str, Any]]:
        """Charge le catalogue existant ou crée un nouveau."""
        if self.catalog_file.exists():
            try:
                with open(self.catalog_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Erreur chargement catalogue: {e}. Nouveau catalogue créé.")
                return []
        return []

    def _save_catalog(self):
        """Sauvegarde le catalogue avec backup."""
        try:
            # Backup de l'ancien catalogue
            if self.catalog_file.exists():
                backup_path = self.catalog_file.with_suffix('.json.backup')
                self.catalog_file.rename(backup_path)
            
            with open(self.catalog_file, 'w', encoding='utf-8') as f:
                json.dump(self.catalog, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Catalogue sauvegardé: {len(self.catalog)} entrées")
        except Exception as e:
            logger.error(f"Erreur sauvegarde catalogue: {e}")

    def add_to_catalog(self, resource: Dict[str, Any]):
        """Ajoute une ressource au catalogue avec déduplication."""
        # Déduplication basée sur l'URL
        if not any(r.get('url') == resource.get('url') for r in self.catalog):
            self.catalog.append(resource)

    def process_sources(self, sources_dict: Dict[str, List[str]], scrapers_map: Dict[str, Any]) -> Dict[str, Any]:
        """
        Traite un dictionnaire de sources avec les scrapers appropriés.
        
        Args:
            sources_dict: {"category": ["url1", "url2", ...]}
            scrapers_map: {"category": ScraperClass}
        
        Returns:
            Rapport de session avec statistiques détaillées
        """
        self.session_stats['start_time'] = datetime.now()
        
        for category, urls in sources_dict.items():
            logger.info(f"\n{'='*60}")
            logger.info(f"Catégorie: {category.upper()} ({len(urls)} sources)")
            logger.info(f"{'='*60}")

            if category not in scrapers_map:
                logger.warning(f"Aucun scraper défini pour: {category}. Ignoré.")
                self.session_stats['skipped'] += len(urls)
                continue

            scraper = self._get_scraper_instance(scrapers_map[category], category)

            # Traitement parallèle des URLs de cette catégorie
            self.session_stats['total_sources'] += len(urls)
            self.session_stats['by_type'][category] = {"total": len(urls), "success": 0, "failed": 0}

            self._process_category_urls(urls, scraper, category)
        
        # Sauvegarde finale
        self._save_catalog()
        
        # Génération du rapport
        self.session_stats['end_time'] = datetime.now()
        self.session_stats['duration_seconds'] = (
            self.session_stats['end_time'] - self.session_stats['start_time']
        ).total_seconds()
        
        return self.session_stats

    def _process_single_source(self, url: str, scraper: Any, category: str) -> Optional[Dict[str, Any]]:
        """Traite une source unique avec retry logic."""
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                logger.info(f"[{category}] Tentative {attempt}/{RETRY_ATTEMPTS}: {url}")
                result = scraper.scrape(url)
                
                if result and result.get('status') == 'success':
                    result['category'] = category
                    result['scraped_at'] = datetime.now().isoformat()
                    # If scraper saved a local file, upload it to S3 and rewrite the file_path
                    try:
                        fp = result.get('file_path')
                        if fp and getattr(settings, 'S3_BUCKET', ''):
                            s3_uri = upload_file_to_s3(fp)
                            if s3_uri:
                                result['s3_path'] = s3_uri
                                result['file_path'] = s3_uri
                    except Exception as _:
                        logger.warning("S3 upload after scrape failed for %s", url)

                    logger.info(f"✅ [{category}] Succès: {url}")
                    return result
                else:
                    logger.warning(f"⚠️ [{category}] Échec (tentative {attempt}): {url}")
                    
            except Exception as e:
                logger.error(f"❌ [{category}] Erreur (tentative {attempt}/{RETRY_ATTEMPTS}): {e}")
                if attempt < RETRY_ATTEMPTS:
                    time.sleep(2 ** attempt)  # Backoff exponentiel
        
        return {
            'url': url,
            'category': category,
            'status': 'failed',
            'error': f'Échec après {RETRY_ATTEMPTS} tentatives',
            'scraped_at': datetime.now().isoformat()
        }

    def _get_scraper_instance(self, scraper_or_class: Any, category: str) -> Any:
        """Return a scraper instance: if a class is provided, instantiate it with category output_dir."""
        # If a class (type) is provided, instantiate it. If an instance is provided, return it.
        try:
            is_type = isinstance(scraper_or_class, type)
        except Exception:
            is_type = False

        if is_type:
            return scraper_or_class(output_dir=str(self.output_dir / category))
        return scraper_or_class

    def _process_category_urls(self, urls: List[str], scraper: Any, category: str) -> None:
        """Process a list of URLs for a given category using a ThreadPoolExecutor and collect results."""
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(self._process_single_source, url, scraper, category): url for url in urls}

            for future in as_completed(futures):
                url = futures[future]
                try:
                    result = future.result(timeout=REQUEST_TIMEOUT * 2)
                    if result and result.get('status') == 'success':
                        self.add_to_catalog(result)
                        self.session_stats['successful'] += 1
                        self.session_stats['by_type'][category]['success'] += 1
                    else:
                        self.session_stats['failed'] += 1
                        self.session_stats['by_type'][category]['failed'] += 1
                except Exception as e:
                    logger.error(f"Erreur traitement {url}: {e}")
                    self.session_stats['failed'] += 1
                    self.session_stats['by_type'][category]['failed'] += 1

    def generate_report(self, output_path: Optional[str] = None) -> str:
        """Génère un rapport HTML détaillé de la session."""
        if not output_path:
            output_path = str(self.output_dir / f"scraping_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
        
        stats = self.session_stats
        
        html = f"""
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <title>Rapport de Scraping - {stats['start_time'].strftime('%Y-%m-%d %H:%M')}</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 40px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
        h2 {{ color: #34495e; margin-top: 30px; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 20px 0; }}
        .stat-card {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 8px; }}
        .stat-card.success {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .stat-card.failed {{ background: linear-gradient(135deg, #eb3349 0%, #f45c43 100%); }}
        .stat-card h3 {{ margin: 0; font-size: 14px; opacity: 0.9; }}
        .stat-card .value {{ font-size: 32px; font-weight: bold; margin: 10px 0; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background: #3498db; color: white; }}
        tr:hover {{ background: #f5f5f5; }}
        .success {{ color: #27ae60; font-weight: bold; }}
        .failed {{ color: #e74c3c; font-weight: bold; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Rapport de Scraping de Ressources Agricoles</h1>
        <p><strong>Session:</strong> {stats['start_time'].strftime('%Y-%m-%d %H:%M:%S')}</p>
        <p><strong>Durée:</strong> {stats.get('duration_seconds', 0):.2f} secondes</p>
        
        <div class="stats">
            <div class="stat-card">
                <h3>Total Sources</h3>
                <div class="value">{stats['total_sources']}</div>
            </div>
            <div class="stat-card success">
                <h3>Succès</h3>
                <div class="value">{stats['successful']}</div>
            </div>
            <div class="stat-card failed">
                <h3>Échecs</h3>
                <div class="value">{stats['failed']}</div>
            </div>
        </div>
        
        <h2>Détail par Catégorie</h2>
        <table>
            <thead>
                <tr>
                    <th>Catégorie</th>
                    <th>Total</th>
                    <th>Succès</th>
                    <th>Échecs</th>
                    <th>Taux de Réussite</th>
                </tr>
            </thead>
            <tbody>
"""
        
        for category, data in stats.get('by_type', {}).items():
            total = data['total']
            success = data['success']
            failed = data['failed']
            rate = (success / total * 100) if total > 0 else 0
            
            html += f"""
                <tr>
                    <td><strong>{category}</strong></td>
                    <td>{total}</td>
                    <td class="success">{success}</td>
                    <td class="failed">{failed}</td>
                    <td>{rate:.1f}%</td>
                </tr>
"""
        
        html += """
            </tbody>
        </table>
        
        <h2>Ressources Collectées</h2>
        <p>Total dans le catalogue: <strong>{}</strong> ressources</p>
    </div>
</body>
</html>
""".format(len(self.catalog))
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
        
        logger.info(f"📄 Rapport généré: {output_path}")
        return output_path


if __name__ == "__main__":
    # Test basique
    manager = ResourceManager()
    print(f"Catalogue chargé: {len(manager.catalog)} ressources")
