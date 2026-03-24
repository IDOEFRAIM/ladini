"""
NewsScraper - Extraction intelligente d'articles de presse agricole.

Sites supportés avec détection automatique:
- lefaso.net: Principal site d'actualité du Burkina Faso
- sidwaya.info: Journal officiel
- cirad.fr: Actualités scientifiques agricoles
- Agri-mutuel.com: Assurance agricole
- Wikipedia: Articles de référence

Stratégies d'extraction:
1. Détection automatique de la structure (article, titre, date, auteur)
2. Nettoyage intelligent des éléments non pertinents (pubs, menus, etc.)
3. Extraction du texte principal avec préservation des paragraphes
4. Gestion des articles multi-pages
5. Extraction des métadonnées structurées (JSON-LD, OpenGraph, etc.)
"""

import requests
from bs4 import BeautifulSoup
import re
import json
import logging
from typing import Dict, Any, Optional, List
from pathlib import Path
from urllib.parse import urlparse, urljoin
from datetime import datetime
import hashlib
from agriconnect.core.settings import settings
from agriconnect.utils.s3_utils import upload_file_to_s3

logger = logging.getLogger("NewsScraper")


class NewsScraper:
    """
    Scraper universel pour articles de presse avec détection automatique de structure.
    """

    def __init__(self, output_dir: str = "backend/sources/raw_data/news_articles"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8'
        }
        
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        # Sélecteurs par domaine (patterns optimisés)
        self.site_patterns = {
            'lefaso.net': {
                'article': ['.article-content', 'article', '.post-content'],
                'title': ['h1.article-title', 'h1', '.entry-title'],
                'date': ['.date', '.published', 'time'],
                'author': ['.author', '.by-author', '.post-author']
            },
            'sidwaya.info': {
                'article': ['article', '.post-content', '.entry-content'],
                'title': ['h1', '.entry-title'],
                'date': ['.published', 'time', '.post-date'],
                'author': ['.author', '.post-author']
            },
            'cirad.fr': {
                'article': ['article', '.article-body', '.content'],
                'title': ['h1', '.article-title'],
                'date': ['.date', 'time'],
                'author': ['.author']
            },
            'default': {
                'article': ['article', 'main', '.content', '.post', '.article'],
                'title': ['h1', 'h2.title', '.title'],
                'date': ['time', '.date', '.published'],
                'author': ['.author', '.by', '.writer']
            }
        }

    def identify_site(self, url: str) -> str:
        """Identifie le site source pour appliquer les bons sélecteurs."""
        domain = urlparse(url).netloc.lower()
        
        for site_key in self.site_patterns.keys():
            if site_key in domain:
                return site_key
        
        return 'default'

    def extract_with_patterns(self, soup: BeautifulSoup, patterns: List[str]) -> Optional[Any]:
        """Essaie plusieurs sélecteurs CSS jusqu'à trouver un élément."""
        for pattern in patterns:
            elem = soup.select_one(pattern)
            if elem:
                return elem
        return None

    def extract_metadata(self, soup: BeautifulSoup, url: str) -> Dict[str, Any]:
        """
        Extrait les métadonnées structurées (JSON-LD, OpenGraph, Meta tags).
        """
        metadata = {
            'url': url,
            'scraped_at': datetime.now().isoformat()
        }
        
        # JSON-LD (format privilégié pour les articles structurés)
        json_ld_scripts = soup.find_all('script', {'type': 'application/ld+json'})
        for script in json_ld_scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict):
                    if data.get('@type') in ['NewsArticle', 'Article', 'BlogPosting']:
                        metadata['title'] = data.get('headline') or data.get('name')
                        metadata['author'] = data.get('author', {}).get('name') if isinstance(data.get('author'), dict) else data.get('author')
                        metadata['date_published'] = data.get('datePublished')
                        metadata['date_modified'] = data.get('dateModified')
                        metadata['description'] = data.get('description')
                        break
            except:
                pass
        
        # OpenGraph tags
        og_tags = {
            'og:title': 'title',
            'og:description': 'description',
            'og:type': 'type',
            'og:published_time': 'date_published',
            'article:published_time': 'date_published',
            'article:author': 'author'
        }
        
        for meta in soup.find_all('meta'):
            prop = meta.get('property') or meta.get('name', '')
            content = meta.get('content')
            
            if prop in og_tags and content:
                key = og_tags[prop]
                if key not in metadata or not metadata[key]:
                    metadata[key] = content
        
        # Meta tags standard
        for meta in soup.find_all('meta', {'name': True}):
            name = meta['name'].lower()
            content = meta.get('content')
            
            if 'description' in name and content:
                if 'description' not in metadata:
                    metadata['description'] = content
            elif 'author' in name and content:
                if 'author' not in metadata:
                    metadata['author'] = content
            elif 'date' in name and content:
                if 'date_published' not in metadata:
                    metadata['date_published'] = content
        
        return metadata

    def clean_article_text(self, article_elem) -> str:
        """
        Nettoie et extrait le texte d'un article en préservant la structure.
        """
        if not article_elem:
            return ""
        
        # Supprimer les éléments non pertinents
        for tag in article_elem(['script', 'style', 'nav', 'header', 'footer', 'aside', 
                                 'form', 'button', '.advertisement', '.ad', '.sidebar',
                                 '.related-posts', '.comments', '.social-share']):
            tag.decompose()
        
        # Extraire le texte avec structure
        paragraphs = []
        for elem in article_elem.find_all(['p', 'h2', 'h3', 'h4', 'li', 'blockquote']):
            text = elem.get_text(strip=True)
            if len(text) > 20:  # Filtrer les paragraphes trop courts
                paragraphs.append(text)
        
        return '\n\n'.join(paragraphs)

    def scrape_article(self, url: str) -> Dict[str, Any]:
        """
        Scrape un article avec détection automatique de structure.
        """
        result = {
            'status': 'failed',
            'url': url,
            'source_type': 'news_article'
        }
        
        try:
            logger.info(f"Scraping: {url}")
            response = self.session.get(url, timeout=20)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Identification du site
            site_type = self.identify_site(url)
            patterns = self.site_patterns.get(site_type, self.site_patterns['default'])
            
            # Extraction des métadonnées
            metadata = self.extract_metadata(soup, url)
            result.update(metadata)
            
            # Extraction du titre (avec fallback)
            title_elem = self.extract_with_patterns(soup, patterns['title'])
            if title_elem:
                result['title'] = title_elem.get_text(strip=True)
            elif not result.get('title'):
                # Fallback: titre de la page
                title_tag = soup.find('title')
                result['title'] = title_tag.get_text(strip=True) if title_tag else "Article sans titre"
            
            # Extraction de la date
            date_elem = self.extract_with_patterns(soup, patterns['date'])
            if date_elem:
                date_text = date_elem.get('datetime') or date_elem.get_text(strip=True)
                if not result.get('date_published'):
                    result['date_published'] = date_text
            
            # Extraction de l'auteur
            author_elem = self.extract_with_patterns(soup, patterns['author'])
            if author_elem and not result.get('author'):
                result['author'] = author_elem.get_text(strip=True)
            
            # Extraction du contenu de l'article
            article_elem = self.extract_with_patterns(soup, patterns['article'])
            
            if article_elem:
                content = self.clean_article_text(article_elem)
                result['content'] = content
                result['content_length'] = len(content)
                
                # Sauvegarder l'article
                url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                filename = f"article_{url_hash}.txt"
                filepath = self.output_dir / filename
                
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(f"Titre: {result.get('title')}\n")
                    f.write(f"Source: {url}\n")
                    f.write(f"Date: {result.get('date_published', 'N/A')}\n")
                    f.write(f"Auteur: {result.get('author', 'N/A')}\n")
                    f.write(f"\n{'-'*60}\n\n")
                    f.write(content)
                
                result['file_path'] = str(filepath)
                
                # Sauvegarder les métadonnées
                meta_filename = f"article_{url_hash}_meta.json"
                meta_path = self.output_dir / meta_filename
                
                with open(meta_path, 'w', encoding='utf-8') as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)
                
                result['metadata_file'] = str(meta_path)
                result['status'] = 'success'
                # Upload to S3 when configured
                try:
                    if getattr(settings, 'S3_BUCKET', ''):
                        s = upload_file_to_s3(result['file_path'])
                        if s:
                            result['s3_path'] = s
                            result['file_path'] = s
                        ms = upload_file_to_s3(result['metadata_file'])
                        if ms:
                            result['metadata_s3'] = ms
                            result['metadata_file'] = ms
                except Exception:
                    logger.warning("S3 upload failed for article: %s", url)

                logger.info(f"✅ Article extrait: {result['title'][:50]}... ({len(content)} chars)")
            else:
                # Fallback: Extraction générique
                logger.warning(f"Structure non détectée, extraction générique pour {url}")
                body = soup.find('body')
                if body:
                    for tag in body(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                        tag.decompose()
                    content = body.get_text(separator='\n', strip=True)
                    result['content'] = content
                    result['content_length'] = len(content)
                    result['extraction_method'] = 'generic_fallback'
                    result['status'] = 'partial'
                else:
                    result['error'] = 'Impossible d\'extraire le contenu'
            
            return result
            
        except Exception as e:
            logger.error(f"Erreur scraping {url}: {e}")
            result['error'] = str(e)
            return result

    def scrape(self, url: str) -> Dict[str, Any]:
        """Point d'entrée compatible avec ResourceManager."""
        return self.scrape_article(url)


if __name__ == "__main__":
    # Test
    scraper = NewsScraper()
    
    test_urls = [
        "https://lefaso.net/spip.php?article141676",
        "https://www.cirad.fr/les-actualites-du-cirad/actualites/2023/les-mils-cereales-pour-une-agriculture-resiliente"
    ]
    
    for url in test_urls:
        print(f"\n{'='*60}")
        result = scraper.scrape_article(url)
        print(f"Status: {result.get('status')}")
        print(f"Titre: {result.get('title')}")
        print(f"Contenu: {len(result.get('content', ''))} caractères")
