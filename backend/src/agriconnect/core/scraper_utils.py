from __future__ import annotations

import hashlib
import random
import re
import time
import unicodedata
import logging
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional, Dict
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import requests
try:
    import ftfy
except ImportError:
    ftfy = None

try:
    import trafilatura
except ImportError:
    trafilatura = None
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

# On essaie d'importer fake-useragent pour la rotation dynamique en production
try:
    from fake_useragent import UserAgent
except ImportError:
    UserAgent = None

logger = logging.getLogger(__name__)

# Liste de secours pour le local ou si fake-useragent échoue
DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)

@dataclass
class HttpPolicy:
    timeout: int = 20
    min_delay_s: float = 1.0  # Augmenté légèrement pour le Cloud
    max_delay_s: float = 3.0
    max_attempts: int = 4
    respect_robots: bool = True
    # La factory va tenter de générer un UA dynamique
    user_agents: Iterable[str] = field(default_factory=lambda: DEFAULT_USER_AGENTS)

_ROBOTS_CACHE: Dict[str, RobotFileParser] = {}

def get_random_user_agent() -> str:
    """Génère un UA frais si possible, sinon pioche dans la liste par défaut."""
    if UserAgent is not None:
        try:
            # Lazy init to avoid network/disk side effects during module import.
            ua_generator = UserAgent(browsers=['chrome', 'edge', 'firefox'])
            return ua_generator.random
        except Exception:
            return random.choice(DEFAULT_USER_AGENTS)
    return random.choice(DEFAULT_USER_AGENTS)

def create_session(user_agent: Optional[str] = None) -> requests.Session:
    """Crée une session avec des headers cohérents pour éviter la détection Cloud."""
    session = requests.Session()
    ua = user_agent or get_random_user_agent()
    
    # Headers modernes indispensables pour passer les barrières type Cloudflare
    session.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    })
    return session

def apply_throttle(policy: HttpPolicy) -> None:
    """Délai aléatoire pour casser les patterns de bot détectables sur le Cloud."""
    delay = random.uniform(policy.min_delay_s, policy.max_delay_s)
    time.sleep(delay)

def can_fetch_url(url: str, user_agent: str) -> bool:
    """Vérifie le robots.txt avec cache pour ne pas surcharger le réseau Cloud."""
    # Dev/testing bypass: set SCRAPER_IGNORE_ROBOTS=1 to skip robots.txt checks
    try:
        if os.getenv("SCRAPER_IGNORE_ROBOTS", "").lower() in ("1", "true", "yes"):
            logger.debug("SCRAPER_IGNORE_ROBOTS enabled: bypassing robots.txt checks")
            return True
    except Exception:
        # Be defensive: if env lookup fails for any reason, fall through to normal behavior
        pass
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    
    if base_url not in _ROBOTS_CACHE:
        parser = RobotFileParser()
        parser.set_url(f"{base_url}/robots.txt")
        try:
            # Timeout court : on ne veut pas bloquer le scrap pour un robots.txt
            response = requests.get(f"{base_url}/robots.txt", timeout=5)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            _ROBOTS_CACHE[base_url] = parser
        except Exception as e:
            logger.debug(f"Could not read robots.txt for {base_url}: {e}")
            return True
            
    return _ROBOTS_CACHE[base_url].can_fetch(user_agent, url)

def normalize_text(text: str) -> str:
    """Nettoyage Gold Data sans compromis."""
    if not text:
        return ""
    
    if ftfy is None:
        raise ImportError("ftfy is required for normalize_text(). Install with: pip install ftfy")

    # ftfy et unicodedata sont obligatoires
    text = ftfy.fix_text(text)
    text = unicodedata.normalize("NFC", text)
    
    # Nettoyage des caractères invisibles et normalisation des espaces
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    
    return text.strip()

def content_sha256(text: str) -> str:
    """ID unique pour déduplication Cross-Cloud."""
    if not text:
        return "empty_content"
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

def extract_markdown_from_html(html: str, include_links: bool = True) -> str:
    """Extraction sémantique via trafilatura."""
    if not html:
        return ""

    if trafilatura is None:
        raise ImportError("trafilatura is required for extract_markdown_from_html(). Install with: pip install trafilatura")
    
    extracted = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=include_links,
        include_tables=True,
        include_images=False,
        favor_recall=True,
        no_fallback=False,
    )
    return extracted or ""

def content_density(markdown: str, raw_html: str) -> float:
    """Métrique de monitoring pour détecter les blocages Cloud (vide vs plein)."""
    if not raw_html or len(raw_html) == 0:
        return 0.0
    return round(min(1.0, len(markdown or "") / len(raw_html)), 4)

def with_retry(policy: HttpPolicy):
    """Retry avec gigue (jitter) indispensable pour le Cloud."""
    return retry(
        reraise=True,
        stop=stop_after_attempt(max(1, policy.max_attempts)),
        wait=wait_exponential_jitter(initial=2, max=15),
        retry=retry_if_exception_type((
            requests.Timeout, 
            requests.ConnectionError,
            requests.HTTPError
        )),
    )


def canonicalize_url(url: str) -> str:
    """Return a canonical URL for stable cross-source deduplication.

    Rules:
    - lowercase scheme and host
    - remove fragment (#...)
    - remove tracking query params starting with `utm_`
    - normalize trailing slash (except root path)
    - sort query parameters for deterministic representation
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()

    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Remove tracking params and sort for deterministic dedupe.
    query_items = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k.lower().startswith("utm_"):
            continue
        query_items.append((k, v))
    query_items.sort(key=lambda kv: (kv[0], kv[1]))
    query = urlencode(query_items, doseq=True)

    normalized = urlunparse((scheme, netloc, path, "", query, ""))
    return normalized