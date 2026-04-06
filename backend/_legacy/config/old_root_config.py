# backend/config.py - Configuration centralisée AgriConnect
import os
import logging
from typing import List, Dict, Tuple, Optional
from dotenv import load_dotenv

# Charger les variables d'environnement depuis .env
load_dotenv()

# Configuration des logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Définition du répertoire de base (racine du projet, parent de backend/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ====================================================================
# 1. PARAMÈTRES TECHNIQUES DU NAVIGATEUR (Playwright/Selenium)
# ====================================================================

# Mode d'exécution du navigateur (True = sans interface graphique, False = avec)
HEADLESS_MODE: bool = True
# Timeout pour page.goto() et autres opérations de navigation (en millisecondes)
BROWSER_TIMEOUT: int = 60000
# Timeout pour l'attente de la stabilité du réseau après navigation (en millisecondes)
NETWORK_IDLE_TIMEOUT: int = 15000
# User-Agent réaliste pour éviter le blocage (403 Forbidden)
BROWSER_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)
# Nombre maximal de pages PDF à lire pour éviter de surcharger la mémoire
PDF_MAX_PAGES_TO_READ: int = 3

# ====================================================================
# 1b. PARAMÈTRES DU SCRAPER (requests)
# ====================================================================

START_URL: str = "https://sonagess.bf/?page_id=239"
TIMEOUT: int = 30
SLEEP: float = 0.4
MAX_DEPTH: int = 1
MAX_PAGES: int = 200
OUTPUT_DIR: str = "sonagess_pdfs"
CHECKPOINT_FILE: str = "backend/sources/raw_data/sonagess_checkpoint.json"
DOWNLOAD_WORKERS: int = 4
SCRAPER_USER_AGENT: str = "Mozilla/5.0 (compatible; SonagessScraper/1.0)"

# ====================================================================
# 2. CHEMINS D'ACCÈS ET RÉPERTOIRES DE SORTIE
# ====================================================================

# Dossier de sortie principal pour tous les rapports
BASE_OUTPUT_DIR: str = os.path.join(BASE_DIR, "backend/sources/rapports_scraping")
# Dossier pour les captures d'écran (cartes, preuves visuelles)
CAPTURES_DIR: str = os.path.join(BASE_OUTPUT_DIR, "visuels_satellite")
# Dossier pour les données brutes GeoJSON ou les réponses Fanfar interceptées
RAW_DATA_DIR: str = os.path.join(BASE_OUTPUT_DIR, "donnees_geojson_brutes")
# Dossier pour les fichiers texte/PDF des bulletins agrométéorologiques
BULLETINS_DIR: str = os.path.join(BASE_OUTPUT_DIR, "bulletins_agronomiques")
# Fichier de sortie pour les données agrégées/traitées finales (JSON)
PROCESSED_DATA_FILE: str = os.path.join(BASE_OUTPUT_DIR, "burkina_flood_risks.json")

# Assurer l'existence des dossiers essentiels
for d in (BASE_OUTPUT_DIR, CAPTURES_DIR, RAW_DATA_DIR, BULLETINS_DIR, OUTPUT_DIR):
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        logging.debug("Impossible de créer le dossier de sortie: %s", d)

# --- CONFIGURATION DU CACHE RAG ---
CACHE_DIR: str = os.path.join(BASE_DIR, "cache")
CACHE_DB_FILE: str = "rag_cache.sqlite"
# Durée de vie du cache des résultats (en secondes) - 30 min
RETRIEVAL_TTL_SECONDS: int = 1800

# Assurer l'existence du cache
try:
    os.makedirs(CACHE_DIR, exist_ok=True)
except Exception:
    logging.debug("Impossible de créer le dossier cache: %s", CACHE_DIR)

# ====================================================================
# 3. CONFIGURATION DES URLS
# ====================================================================

# URL de base pour les différentes sections du site
URL_MAP_VIEWER: str = "https://meteoburkina.bf/mapviewer/"
URL_BULLETINS_QUOTIDIEN: str = "https://meteoburkina.bf/produits/bulletin-quotidien/"
URL_SATELLITE: str = "https://meteoburkina.bf/images-satellitaires/"
URL_AGRI_MENSUEL: str = "https://meteoburkina.bf/produits/bulletin-agrometeorologique-mensuel/"
URL_AGRI_DECADAIRE: str = "https://meteoburkina.bf/produits/bulletin-agrometeologique-decadaire/"
URL_PREVISIONS_DETAILLES: str = "https://meteoburkina.bf/previsions/detaillees/"
# Fanfar (Risque Inondation)
URL_FANFAR_PIV: str = "https://fanfar.eu/fr/piv/"

# ====================================================================
# 4. SÉLECTEURS CSS (pour l'interaction et le nettoyage de l'interface)
# ====================================================================

# Sélecteur pour le menu des catégories de cartes (utilisé pour les clics et le masquage)
SELECTOR_MAP_MENU: str = ".datasets-menu"
# Sélecteur du conteneur principal de la carte (pour une capture d'écran précise)
SELECTOR_MAP_CONTAINER: str = ".map-container"

# Sélecteurs à masquer pour une capture d'écran propre (Sidebars, contrôles OpenLayers/Leaflet, etc.)
SELECTOR_MAP_SIDEBAR: List[str] = [
    '.sidebar',
    '.left-panel',
    '.control-panel',
    '.ol-control',
    '.floating-panel',
    '#left-panel',
    '.map-controls'
]

# Sélecteurs d'éléments bloquants (popups, modales d'intro, bannières cookies, etc.)
SELECTOR_BLOCKERS: List[str] = [
    '.ReactModal__Overlay',
    '.ReactModalPortal',
    '.modal-overlay',
    '.intro-popup',
    '.welcome-screen',
    '.cookie-banner'
]

# Sélecteurs pour les autres domaines
# Bulletins : Sélecteur pour trouver les liens ou conteneurs de bulletins
SELECTOR_BULLETIN_LINKS: str = (
    'article a[href*="bulletin-agrometeologique-decadaire"], '
    '.bulletin-list a[href*="pdf"], .entry-content a[href*="pdf"], '
    'a[href*="bulletin-agrometeologique-decadaire"]'
)
# Météo : Sélecteur pour le tableau ou le bloc de prévisions
SELECTOR_WEATHER_FORECAST_BLOCK: str = '#weather-data-table, .forecast-summary'

# ====================================================================
# 5. LISTES DE CIBLES (WATCHLISTS)
# ====================================================================

# Catégories de cartes à scanner sur le MapViewer (visuel et feature)
WATCHLIST_MAP_CATEGORIES: List[str] = ["ALERTES", "PLUIE", "INONDATIONS", "SÉCHERESSE", "AGRICULTURE"]
# Nombre de bulletins à scraper (pour limiter la charge)
MAX_BULLETINS_TO_SCRAPE: int = 5
# Villes à scanner sur Fanfar ou autres services de prévisions localisées
WATCHLIST_CITIES: List[str] = [
    "Niamey", "Bamako", "Ouagadougou", "Lagos", "Cotonou",
    "Dakar", "Abidjan", "Accra"
]

# ====================================================================
# 6. MOTS-CLÉS ET FILTRES POUR LE TRAITEMENT DE DONNÉES
# ====================================================================

# Mots-clés pour identifier un bulletin PDF valide (pour le filtrage)
KEYWORDS_BULLETIN_TITLE: List[str] = ["bulletin", "situation", "pluviométrique", "bilan", "agrométéorologique"]
KEYWORDS_BULLETIN_SPECIFICS: List[str] = [
    "n°", "2024", "2025",
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre"
]
# Mots-clés pour trouver un bouton de téléchargement caché dans le DOM
KEYWORDS_DOWNLOAD_LINK: List[str] = ["télécharger", "download", "document", "cliquez ici", "consulter"]
# Mots-clés pour détecter des données de risque dans les réponses JSON (Fanfar)
KEYWORDS_FANFAR_RISK: List[str] = ["severity", "return_period", "risk", "niveau de sévérité", "aleas", "features"]

# Zone géographique (BBOX) : Burkina Faso (min_lon, min_lat, max_lon, max_lat)
PROCESSOR_BBOX: Tuple[float, float, float, float] = (-5.5, 9.4, 2.4, 15.1)
# Mots-clés pour garder un GeoJSON pertinent (relatif aux inondations)
PROCESSOR_FLOOD_KEYWORDS: Dict[str, str] = {
    "flood": "inondation",
    "inond": "inondation",
    "débit": "flux",
    "seuil": "seuil d'alerte",
    "alerte": "alerte crue",
    "crue": "crue",
    "water_level": "niveau d'eau",
    "risk": "risque",
    "vigilance": "vigilance météo"
}
# Nettoyage Images
PROCESSOR_IMG_MIN_SIZE: int = 1024       # Ignorer images < 1KB
PROCESSOR_IMG_THRESHOLD: float = 0.90   # Supprimer si >90% noir ou vert uni

# ====================================================================
# 7. EXPORT GLOBAL POUR L'ORCHESTRATEUR
# ====================================================================

SCRAPER_CONFIG: Dict[str, object] = {
    # 1. Techniques
    "HEADLESS_MODE": HEADLESS_MODE,
    "BROWSER_TIMEOUT": BROWSER_TIMEOUT,
    "NETWORK_IDLE_TIMEOUT": NETWORK_IDLE_TIMEOUT,
    "BROWSER_USER_AGENT": BROWSER_USER_AGENT,
    "PDF_MAX_PAGES_TO_READ": PDF_MAX_PAGES_TO_READ,

    # 1b. Scraper
    "START_URL": START_URL,
    "TIMEOUT": TIMEOUT,
    "SLEEP": SLEEP,
    "MAX_DEPTH": MAX_DEPTH,
    "MAX_PAGES": MAX_PAGES,
    "OUTPUT_DIR": OUTPUT_DIR,
    "CHECKPOINT_FILE": CHECKPOINT_FILE,
    "DOWNLOAD_WORKERS": DOWNLOAD_WORKERS,
    "SCRAPER_USER_AGENT": SCRAPER_USER_AGENT,

    # 2. Chemins
    "BASE_OUTPUT_DIR": BASE_OUTPUT_DIR,
    "CAPTURES_DIR": CAPTURES_DIR,
    "RAW_DATA_DIR": RAW_DATA_DIR,
    "BULLETINS_DIR": BULLETINS_DIR,
    "PROCESSED_DATA_FILE": PROCESSED_DATA_FILE,
    "CACHE_DIR": CACHE_DIR,
    "CACHE_DB_FILE": CACHE_DB_FILE,
    "RETRIEVAL_TTL_SECONDS": RETRIEVAL_TTL_SECONDS,

    # 3. URLs
    "URL_MAP_VIEWER": URL_MAP_VIEWER,
    "URL_BULLETINS_QUOTIDIEN": URL_BULLETINS_QUOTIDIEN,
    "URL_SATELLITE": URL_SATELLITE,
    "URL_AGRI_MENSUEL": URL_AGRI_MENSUEL,
    "URL_AGRI_DECADAIRE": URL_AGRI_DECADAIRE,
    "URL_PREVISIONS_DETAILLES": URL_PREVISIONS_DETAILLES,
    "URL_FANFAR_PIV": URL_FANFAR_PIV,

    # 4. Sélecteurs
    "SELECTOR_MAP_MENU": SELECTOR_MAP_MENU,
    "SELECTOR_MAP_CONTAINER": SELECTOR_MAP_CONTAINER,
    "SELECTOR_MAP_SIDEBAR": SELECTOR_MAP_SIDEBAR,
    "SELECTOR_BLOCKERS": SELECTOR_BLOCKERS,
    "SELECTOR_BULLETIN_LINKS": SELECTOR_BULLETIN_LINKS,
    "SELECTOR_WEATHER_FORECAST_BLOCK": SELECTOR_WEATHER_FORECAST_BLOCK,

    # 5. Watchlists
    "WATCHLIST_MAP_CATEGORIES": WATCHLIST_MAP_CATEGORIES,
    "MAX_BULLETINS_TO_SCRAPE": MAX_BULLETINS_TO_SCRAPE,
    "WATCHLIST_CITIES": WATCHLIST_CITIES,

    # 6. Mots-clés/Filtres
    "KEYWORDS_BULLETIN_TITLE": KEYWORDS_BULLETIN_TITLE,
    "KEYWORDS_BULLETIN_SPECIFICS": KEYWORDS_BULLETIN_SPECIFICS,
    "KEYWORDS_DOWNLOAD_LINK": KEYWORDS_DOWNLOAD_LINK,
    "KEYWORDS_FANFAR_RISK": KEYWORDS_FANFAR_RISK,
    "PROCESSOR_BBOX": PROCESSOR_BBOX,
    "PROCESSOR_FLOOD_KEYWORDS": PROCESSOR_FLOOD_KEYWORDS,
    "PROCESSOR_IMG_MIN_SIZE": PROCESSOR_IMG_MIN_SIZE,
    "PROCESSOR_IMG_THRESHOLD": PROCESSOR_IMG_THRESHOLD,
}