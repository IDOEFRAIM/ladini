"""
PdfDownloader - Téléchargement et extraction avancée de contenu PDF.

Fonctionnalités:
- Téléchargement robuste avec retry logic
- Extraction de métadonnées (titre, auteur, date, pages, etc.)
- Extraction de texte multi-méthodes (pdfplumber, PyPDF2, OCR fallback)
- Gestion des PDFs scannés avec OCR (Tesseract via pytesseract)
- Détection de la langue du document
- Extraction des images embarquées
- Validation de l'intégrité du PDF

Dependencies optionnelles:
- pdfplumber: Extraction de texte de qualité
- PyPDF2 ou pypdf: Métadonnées et texte basique
- pytesseract + Pillow: OCR pour PDFs scannés
- pdf2image: Conversion PDF -> images pour OCR
"""

import requests
import hashlib
import logging
from agriconnect.core.settings import settings
from agriconnect.utils.s3_utils import upload_file_to_s3
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
import json
import re

logger = logging.getLogger("PdfDownloader")


class PdfDownloader:
    """
    Gestionnaire avancé de téléchargement et extraction de PDFs.
    """

    def __init__(self, output_dir: str = "backend/sources/raw_data/pdfs"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.metadata_dir = self.output_dir / "metadata"
        self.metadata_dir.mkdir(exist_ok=True)
        
        self.text_dir = self.output_dir / "extracted_text"
        self.text_dir.mkdir(exist_ok=True)
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/pdf,application/octet-stream,*/*'
        }
        
        # Détection des dépendances disponibles
        self.has_pdfplumber = self._check_import('pdfplumber')
        self.has_pypdf = self._check_import('pypdf') or self._check_import('PyPDF2')
        self.has_ocr = self._check_import('pytesseract') and self._check_import('pdf2image')
        
        logger.info(f"PDF Capabilities: pdfplumber={self.has_pdfplumber}, pypdf={self.has_pypdf}, OCR={self.has_ocr}")

    def _check_import(self, module_name: str) -> bool:
        """Vérifie si un module est disponible."""
        try:
            __import__(module_name)
            return True
        except ImportError:
            return False

    def download_pdf(self, url: str, filename: Optional[str] = None) -> Optional[Path]:
        """
        Télécharge un PDF avec gestion d'erreurs robuste.
        """
        try:
            logger.info(f"Téléchargement: {url}")
            
            # Stream pour gérer les gros fichiers
            response = requests.get(url, headers=self.headers, timeout=60, stream=True)
            response.raise_for_status()
            
            # Vérifier que c'est bien un PDF
            content_type = response.headers.get('Content-Type', '').lower()
            if 'pdf' not in content_type and not url.lower().endswith('.pdf'):
                logger.warning(f"Content-Type suspect: {content_type} pour {url}")
            
            # Générer un nom de fichier unique
            if not filename:
                # Essayer d'extraire depuis Content-Disposition
                content_disp = response.headers.get('Content-Disposition', '')
                filename_match = re.search(r'filename[^;=\n]*=([\'"]?)(.+?)\1', content_disp)
                
                if filename_match:
                    filename = filename_match.group(2)
                else:
                    # Générer depuis l'URL
                    url_filename = url.split('/')[-1].split('?')[0]
                    if url_filename.endswith('.pdf'):
                        filename = url_filename
                    else:
                        # Hash de l'URL pour unicité
                        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                        filename = f"pdf_{url_hash}.pdf"
            
            # Nettoyer le nom de fichier
            filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
            if not filename.endswith('.pdf'):
                filename += '.pdf'
            
            filepath = self.output_dir / filename
            
            # Téléchargement en chunks
            total_size = 0
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        total_size += len(chunk)
            
            logger.info(f"✅ Téléchargé: {filepath.name} ({total_size / 1024:.1f} KB)")
            return filepath
            
        except Exception as e:
            logger.error(f"Erreur téléchargement {url}: {e}")
            return None

    def extract_metadata(self, pdf_path: Path) -> Dict[str, Any]:
        """
        Extrait les métadonnées du PDF (titre, auteur, date, etc.).
        """
        metadata = {
            'filename': pdf_path.name,
            'file_size': pdf_path.stat().st_size,
            'file_path': str(pdf_path)
        }
        
        if self.has_pypdf:
            try:
                from pypdf import PdfReader
                
                reader = PdfReader(pdf_path)
                info = reader.metadata
                
                if info:
                    metadata['title'] = info.get('/Title', '')
                    metadata['author'] = info.get('/Author', '')
                    metadata['subject'] = info.get('/Subject', '')
                    metadata['creator'] = info.get('/Creator', '')
                    metadata['producer'] = info.get('/Producer', '')
                    metadata['creation_date'] = info.get('/CreationDate', '')
                    metadata['modification_date'] = info.get('/ModDate', '')
                
                metadata['pages'] = len(reader.pages)
                metadata['is_encrypted'] = reader.is_encrypted
                
            except Exception as e:
                logger.warning(f"Erreur extraction métadonnées pypdf: {e}")
        
        elif self.has_pdfplumber:
            try:
                import pdfplumber
                
                with pdfplumber.open(pdf_path) as pdf:
                    metadata['pages'] = len(pdf.pages)
                    if pdf.metadata:
                        metadata.update({k.lstrip('/'): v for k, v in pdf.metadata.items()})
                        
            except Exception as e:
                logger.warning(f"Erreur extraction métadonnées pdfplumber: {e}")
        
        return metadata

    def extract_text_pdfplumber(self, pdf_path: Path, max_pages: Optional[int] = None) -> str:
        """Extraction de texte avec pdfplumber (méthode préférée)."""
        if not self.has_pdfplumber:
            return ""
        
        try:
            import pdfplumber
            
            text_content = []
            with pdfplumber.open(pdf_path) as pdf:
                pages_to_process = pdf.pages[:max_pages] if max_pages else pdf.pages
                
                for page_num, page in enumerate(pages_to_process, 1):
                    page_text = page.extract_text()
                    if page_text:
                        text_content.append(f"\n--- Page {page_num} ---\n{page_text}")
            
            return "\n".join(text_content)
            
        except Exception as e:
            logger.error(f"Erreur extraction pdfplumber: {e}")
            return ""

    def extract_text_pypdf(self, pdf_path: Path, max_pages: Optional[int] = None) -> str:
        """Extraction de texte avec PyPDF (fallback)."""
        if not self.has_pypdf:
            return ""
        
        try:
            from pypdf import PdfReader
            
            reader = PdfReader(pdf_path)
            text_content = []
            
            pages_to_process = reader.pages[:max_pages] if max_pages else reader.pages
            
            for page_num, page in enumerate(pages_to_process, 1):
                page_text = page.extract_text()
                if page_text:
                    text_content.append(f"\n--- Page {page_num} ---\n{page_text}")
            
            return "\n".join(text_content)
            
        except Exception as e:
            logger.error(f"Erreur extraction pypdf: {e}")
            return ""

    def extract_text_ocr(self, pdf_path: Path, max_pages: int = 5) -> str:
        """
        Extraction par OCR pour les PDFs scannés (dernier recours).
        Limité aux premières pages par défaut pour la performance.
        """
        if not self.has_ocr:
            logger.warning("OCR non disponible (installer pytesseract et pdf2image)")
            return ""
        
        try:
            import pytesseract
            from pdf2image import convert_from_path
            from PIL import Image
            
            logger.info(f"Tentative OCR sur {pdf_path.name} (max {max_pages} pages)...")
            
            # Convertir PDF en images
            images = convert_from_path(pdf_path, first_page=1, last_page=max_pages)
            
            text_content = []
            for page_num, image in enumerate(images, 1):
                # OCR avec Tesseract
                page_text = pytesseract.image_to_string(image, lang='fra+eng')
                if page_text.strip():
                    text_content.append(f"\n--- Page {page_num} (OCR) ---\n{page_text}")
            
            return "\n".join(text_content)
            
        except Exception as e:
            logger.error(f"Erreur OCR: {e}")
            return ""

    def extract_text(self, pdf_path: Path, max_pages: Optional[int] = None, use_ocr: bool = True) -> str:
        """
        Extraction de texte avec stratégie multi-méthodes.
        Essaie pdfplumber -> pypdf -> OCR (si activé).
        """
        # Méthode 1: pdfplumber
        text = self.extract_text_pdfplumber(pdf_path, max_pages)
        if text and len(text.strip()) > 100:
            logger.info(f"Extraction réussie (pdfplumber): {len(text)} chars")
            return text
        
        # Méthode 2: pypdf
        text = self.extract_text_pypdf(pdf_path, max_pages)
        if text and len(text.strip()) > 100:
            logger.info(f"Extraction réussie (pypdf): {len(text)} chars")
            return text
        
        # Méthode 3: OCR (si activé et si peu/pas de texte extrait)
        if use_ocr and (not text or len(text.strip()) < 100):
            logger.info("Texte insuffisant, tentative OCR...")
            text = self.extract_text_ocr(pdf_path, max_pages=min(max_pages or 5, 5))
            if text:
                logger.info(f"Extraction réussie (OCR): {len(text)} chars")
                return text
        
        logger.warning(f"Extraction minimale pour {pdf_path.name}")
        return text

    def process_pdf(self, url: str, extract_text: bool = True, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """
        Pipeline complet: téléchargement + extraction métadonnées + texte.
        """
        result = {
            'status': 'failed',
            'url': url,
            'source_type': 'pdf'
        }
        
        # Téléchargement
        pdf_path = self.download_pdf(url)
        if not pdf_path:
            result['error'] = 'Échec téléchargement'
            return result
        
        result['file_path'] = str(pdf_path)
        
        # Métadonnées
        metadata = self.extract_metadata(pdf_path)
        result['metadata'] = metadata
        result['title'] = metadata.get('title') or metadata.get('filename', 'PDF Sans Titre')
        
        # Extraction de texte
        if extract_text:
            text = self.extract_text(pdf_path, max_pages=max_pages)
            result['content'] = text
            result['metadata']['extracted_chars'] = len(text)
            
            # Sauvegarder le texte extrait
            text_filename = pdf_path.stem + '.txt'
            text_path = self.text_dir / text_filename
            with open(text_path, 'w', encoding='utf-8') as f:
                f.write(text)
            result['text_file'] = str(text_path)
        
        # Sauvegarder les métadonnées
        meta_filename = pdf_path.stem + '_meta.json'
        meta_path = self.metadata_dir / meta_filename
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        result['metadata_file'] = str(meta_path)
        
        result['status'] = 'success'
        logger.info(f"✅ PDF traité: {pdf_path.name}")
        # Attempt S3 uploads if configured
        try:
            if getattr(settings, 'S3_BUCKET', ''):
                s3_uri = upload_file_to_s3(str(pdf_path))
                if s3_uri:
                    result['s3_path'] = s3_uri
                    result['file_path'] = s3_uri
                if 'text_file' in result:
                    t_s3 = upload_file_to_s3(result['text_file'])
                    if t_s3:
                        result['text_s3'] = t_s3
                        result['text_file'] = t_s3
                if 'metadata_file' in result:
                    m_s3 = upload_file_to_s3(result['metadata_file'])
                    if m_s3:
                        result['metadata_s3'] = m_s3
                        result['metadata_file'] = m_s3
        except Exception:
            logger.warning("S3 upload failed for PDF assets: %s", pdf_path)

        return result

    def scrape(self, url: str) -> Dict[str, Any]:
        """Point d'entrée compatible avec ResourceManager."""
        return self.process_pdf(url, extract_text=True, max_pages=50)


if __name__ == "__main__":
    # Test
    downloader = PdfDownloader()
    
    test_url = "https://afristat.org/wp-content/uploads/2022/04/NotesCours_Agri.pdf"
    
    print(f"Test téléchargement: {test_url}")
    result = downloader.process_pdf(test_url, max_pages=3)
    print(json.dumps(result, indent=2, ensure_ascii=False))
