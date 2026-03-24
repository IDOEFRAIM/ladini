"""
TechnicalResourcesExplorer - Exploration récursive de sites techniques agricoles.

Sites ciblés:
- cnrada.org: Centre National de Recherche Agronomique
- usgs.gov: US Geological Survey (Croplands, FEWS)
- oecd.org: OCDE publications agricoles
- cahiersagricultures.fr: Revue scientifique
- reseau-far.com: Réseau de la Formation Agricole et Rurale

Stratégie d'exploration:
1. Crawler intelligent avec limitation de profondeur
2. Détection automatique de documents pertinents (PDFs, articles, fiches techniques)
3. Filtrage par mots-clés agricoles (Burkina Faso, Sahel, céréales, etc.)
4. Respect des robots.txt et throttling pour éviter la surcharge
5. Priorisation des documents récents et pertinents
"""

import requests
from bs4 import BeautifulSoup
import re
import json
import logging
from typing import Dict, Any, Optional, List, Set
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser
import hashlib
import time
from collections import deque

from .pdf_downloader import PdfDownloader
from agriconnect.core.settings import settings
from agriconnect.utils.s3_utils import upload_file_to_s3

logger = logging.getLogger("TechnicalResourcesExplorer")


class TechnicalResourcesExplorer:
    """
    Crawler intelligent pour sites techniques agricoles.
    """

    def __init__(self, 
                 output_dir: str = "backend/sources/raw_data/technical_resources",
                 max_depth: int = 3,
                 max_pages: int = 50,
                 respect_robots: bool = True):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.documents_dir = self.output_dir / "documents"
        self.documents_dir.mkdir(exist_ok=True)
        
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.respect_robots = respect_robots
        
        self.pdf_downloader = PdfDownloader(output_dir=str(self.documents_dir / "pdfs"))
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (compatible; AgribotCrawler/1.0; +http://agribot.bf)'
        }
        
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        # Mots-clés pour filtrage de pertinence
        self.keywords = [
            'burkina', 'faso', 'sahel', 'agriculture', 'céréale', 'mil', 'sorgho',
            'maïs', 'culture', 'irrigation', 'fertilisant', 'semence', 'récolte',
            'climat', 'sécheresse', 'pluviométrie', 'agronomie', 'paysan',
            'production', 'rendement', 'technique', 'pratique'
        ]
        
        # Cache des pages visitées (par domaine)
        self.visited_urls: Dict[str, Set[str]] = {}
        
        # Cache robots.txt (par domaine)
        self.robots_parsers: Dict[str, RobotFileParser] = {}

    def can_fetch(self, url: str) -> bool:
        """Vérifie si l'URL peut être scrapée selon robots.txt."""
        if not self.respect_robots:
            return True
        
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        
        if domain not in self.robots_parsers:
            robots_url = urljoin(domain, '/robots.txt')
            rp = RobotFileParser()
            rp.set_url(robots_url)
            
            try:
                rp.read()
                self.robots_parsers[domain] = rp
            except:
                # En cas d'erreur, on autorise (comportement permissif)
                self.robots_parsers[domain] = None
        
        rp = self.robots_parsers[domain]
        if rp is None:
            return True
        
        return rp.can_fetch(self.headers['User-Agent'], url)

    def is_relevant(self, url: str, text: str) -> bool:
        """Détermine si une page/document est pertinent selon les mots-clés."""
        combined = (url + " " + text).lower()
        
        # Score de pertinence (nombre de mots-clés trouvés)
        score = sum(1 for kw in self.keywords if kw in combined)
        
        return score >= 2  # Au moins 2 mots-clés présents

    def extract_links(self, soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
        """
        Extrait et classe les liens d'une page.
        Distingue les documents (PDFs, etc.) des pages HTML.
        """
        links = []
        
        for a in soup.find_all('a', href=True):
            href = a['href']
            
            # Nettoyer l'URL (enlever les fragments #)
            href, _ = urldefrag(href)
            
            # Construire l'URL absolue
            full_url = urljoin(base_url, href)
            
            # Filtrer les URLs invalides
            if not full_url.startswith(('http://', 'https://')):
                continue
            
            # Vérifier si même domaine (ou sous-domaine raisonnable)
            base_domain = urlparse(base_url).netloc
            link_domain = urlparse(full_url).netloc
            
            # Identifier le type de lien
            link_type = 'page'
            if full_url.lower().endswith('.pdf'):
                link_type = 'pdf'
            elif any(full_url.lower().endswith(ext) for ext in ['.doc', '.docx', '.xls', '.xlsx', '.ppt']):
                link_type = 'document'
            
            link_text = a.get_text(strip=True)
            
            links.append({
                'url': full_url,
                'type': link_type,
                'text': link_text,
                'same_domain': base_domain in link_domain or link_domain in base_domain
            })
        
        return links

    def scrape_page(self, url: str) -> Dict[str, Any]:
        """Scrape une page HTML et extrait son contenu."""
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Titre
            title = soup.find('h1') or soup.find('title')
            title_text = title.get_text(strip=True) if title else ""
            
            # Contenu principal
            main = soup.find('main') or soup.find('article') or soup.find('div', class_=re.compile('content|main', re.I))
            
            if main:
                for tag in main(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                    tag.decompose()
                content = main.get_text(separator='\n', strip=True)
            else:
                content = soup.get_text(separator='\n', strip=True)[:3000]
            
            return {
                'url': url,
                'title': title_text,
                'content': content,
                'soup': soup,  # Pour extraction de liens
                'status': 'success'
            }
            
        except Exception as e:
            logger.error(f"Erreur scraping page {url}: {e}")
            return {'url': url, 'status': 'failed', 'error': str(e)}

    def explore_site(self, start_url: str) -> List[Dict[str, Any]]:
        """
        Exploration BFS (Breadth-First Search) d'un site web.
        Retourne tous les documents pertinents trouvés.
        """
        logger.info(f"🚀 Début exploration: {start_url}")
        
        domain = urlparse(start_url).netloc
        self.visited_urls[domain] = set()
        
        # Queue BFS: (url, depth)
        queue = deque([(start_url, 0)])
        self.visited_urls[domain].add(start_url)
        
        documents_found = []
        pages_processed = 0
        
        while queue and pages_processed < self.max_pages:
            url, depth = queue.popleft()
            
            # Limiter la profondeur
            if depth > self.max_depth:
                continue
            
            # Vérifier robots.txt
            if not self.can_fetch(url):
                logger.debug(f"Bloqué par robots.txt: {url}")
                continue
            
            logger.info(f"[Depth {depth}] Scraping: {url}")
            
            # Scraping de la page
            page_data = self.scrape_page(url)
            pages_processed += 1
            
            if page_data.get('status') != 'success':
                continue
            
            # Vérifier la pertinence
            if self.is_relevant(url, page_data.get('content', '')):
                # Sauvegarder la page pertinente
                url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                filename = f"page_{url_hash}.txt"
                filepath = self.documents_dir / filename
                
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(f"URL: {url}\n")
                    f.write(f"Titre: {page_data.get('title')}\n\n")
                    f.write(page_data.get('content', ''))
                # Upload saved page to S3 when configured
                try:
                    if getattr(settings, 'S3_BUCKET', ''):
                        s3p = upload_file_to_s3(str(filepath))
                        if s3p:
                            documents_found.append({
                                'url': url,
                                'type': 'html_page',
                                'title': page_data.get('title'),
                                'file_path': s3p,
                                'depth': depth
                            })
                            continue
                except Exception:
                    logger.warning("S3 upload failed for page: %s", filepath)
                
                documents_found.append({
                    'url': url,
                    'type': 'html_page',
                    'title': page_data.get('title'),
                    'file_path': str(filepath),
                    'depth': depth
                })
            
            # Extraire les liens
            soup = page_data.get('soup')
            if soup:
                links = self.extract_links(soup, url)
                
                for link in links:
                    link_url = link['url']
                    
                    # Si c'est un PDF, le télécharger directement
                    if link['type'] == 'pdf':
                        if self.is_relevant(link_url, link['text']):
                            logger.info(f"📄 PDF trouvé: {link['text']}")
                            try:
                                pdf_result = self.pdf_downloader.process_pdf(link_url, max_pages=20)
                                if pdf_result.get('status') == 'success':
                                    # pdf_downloader already uploads to S3; pass through
                                    documents_found.append({
                                        'url': link_url,
                                        'type': 'pdf',
                                        'title': pdf_result.get('title'),
                                        'file_path': pdf_result.get('file_path'),
                                        'depth': depth + 1
                                    })
                            except Exception as e:
                                logger.error(f"Erreur téléchargement PDF {link_url}: {e}")
                    
                    # Si c'est une page HTML du même domaine, l'ajouter à la queue
                    elif link['type'] == 'page' and link['same_domain']:
                        if link_url not in self.visited_urls[domain]:
                            self.visited_urls[domain].add(link_url)
                            queue.append((link_url, depth + 1))
            
            # Throttling pour éviter la surcharge du serveur
            time.sleep(0.5)
        
        logger.info(f"✅ Exploration terminée: {len(documents_found)} documents trouvés, {pages_processed} pages visitées")
        
        return documents_found

    def scrape(self, url: str) -> Dict[str, Any]:
        """
        Point d'entrée compatible avec ResourceManager.
        Lance l'exploration et retourne un résumé.
        """
        documents = self.explore_site(url)
        
        result = {
            'status': 'success',
            'url': url,
            'source_type': 'technical_site',
            'documents_found': len(documents),
            'documents': documents,
            'metadata': {
                'max_depth': self.max_depth,
                'max_pages': self.max_pages
            }
        }
        
        # Sauvegarder le résumé de l'exploration
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        summary_path = self.output_dir / f"exploration_{url_hash}.json"
        
        with open(summary_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        # Attempt to upload summary
        try:
            if getattr(settings, 'S3_BUCKET', ''):
                s3sum = upload_file_to_s3(str(summary_path))
                if s3sum:
                    result['summary_file'] = s3sum
                else:
                    result['summary_file'] = str(summary_path)
            else:
                result['summary_file'] = str(summary_path)
        except Exception:
            result['summary_file'] = str(summary_path)
        
        # Pour ResourceManager, on retourne le contenu du premier document significatif
        if documents:
            first_doc = documents[0]
            result['title'] = first_doc.get('title', 'Technical Resources')
            result['content'] = f"Exploration technique: {len(documents)} documents trouvés"
            result['file_path'] = first_doc.get('file_path')
        
        return result


if __name__ == "__main__":
    # Test
    explorer = TechnicalResourcesExplorer(max_depth=2, max_pages=20)
    
    test_url = "https://cnrada.org/fiche-nuisibles/mil-mildiou-ou-lepre-du-mil/"
    
    print(f"Test exploration: {test_url}")
    result = explorer.scrape(test_url)
    print(f"\nDocuments trouvés: {result.get('documents_found')}")
    for doc in result.get('documents', [])[:5]:
        print(f"  - {doc['type']}: {doc['title']}")
