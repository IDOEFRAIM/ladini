from __future__ import annotations
from typing import Dict, List
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class ScraperSettings(BaseSettings):
    """
    Configuration de l'ENGINE V2 : Pureté, Résilience et Zero-IO.
    Définit les paramètres de survie (anti-ban) et les sources de vérité.
    """
    
    # --- RESILIENCE & PERFORMANCE ---
    max_retries: int = Field(default=3, validation_alias="SCRAPER_MAX_RETRIES")
    rate_limit_per_second: int = Field(default=2, validation_alias="SCRAPER_RATE_LIMIT")
    batch_size: int = Field(default=50, validation_alias="SCRAPER_BATCH_SIZE")
    timeout_seconds: int = Field(default=30)
    output_dir: str = Field(default="backend/sources/raw_data", validation_alias="SCRAPER_OUTPUT_DIR")
    
    # --- SURVIE & ANTI-BAN (Circuit Breaker) ---
    circuit_breaker_threshold: int = Field(default=5)
    circuit_breaker_timeout: int = Field(default=300)
    lambda_execution: bool = Field(default=False, validation_alias="SCRAPER_LAMBDA")
    user_agents: List[str] = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "AgriConnectBot/2.0 (+https://agriconnect.ai/bot)"
    ]

    # --- SOURCES DE DONNÉES (Source de Vérité Unique) ---
    google_workspace_links: List[str] = []
    pdf_documents: List[str] = []
    fao_publications_doi: List[str] = []
    news_and_articles: List[str] = []
    statistical_platforms: List[str] = []
    technical_agriculture_resources: List[str] = []

    # Configuration Pydantic pour la lecture automatique des .env
    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def get_sources_as_dict(self) -> Dict[str, List[str]]:
        """Retourne le mapping des sources pour le Registry."""
        return {
            "google_workspace": self.google_workspace_links,
            "pdf": self.pdf_documents,
            "fao": self.fao_publications_doi,
            "news": self.news_and_articles,
            "stats": self.statistical_platforms,
            "tech": self.technical_agriculture_resources,
        }

def get_config() -> ScraperSettings:
    """Factory pour injecter la configuration dans les scrapers."""
    return ScraperSettings()


# Backward compatibility for orchestrator imports
ScraperConfig = ScraperSettings


class SourcesConfig(BaseModel):
    """Compatibility source container exposing orchestrator-friendly API."""

    google_workspace_links: List[str] = Field(default_factory=list)
    pdf_documents: List[str] = Field(default_factory=list)
    fao_publications_doi: List[str] = Field(default_factory=list)
    news_and_articles: List[str] = Field(default_factory=list)
    statistical_platforms: List[str] = Field(default_factory=list)
    technical_agriculture_resources: List[str] = Field(default_factory=list)

    def to_dict(self) -> Dict[str, List[str]]:
        # Keep orchestrator category names stable.
        return {
            "google_workspace_links": self.google_workspace_links,
            "pdf_documents": self.pdf_documents,
            "fao_publications_doi": self.fao_publications_doi,
            "news_and_articles": self.news_and_articles,
            "statistical_platforms": self.statistical_platforms,
            "technical_agriculture_resources": self.technical_agriculture_resources,
        }