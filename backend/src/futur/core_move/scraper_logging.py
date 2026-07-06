from __future__ import annotations

import logging
import sys
from typing import Any, Dict, Optional
import structlog

def setup_logging(log_level: str = "INFO", structured: bool = True, console_output: bool = True):
    """ Configure le logging pour le Cloud (Structuré JSON) ou le Local (Couleurs)."""
    
    # Processeurs communs (Nettoyage et enrichissement des logs)
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if structured:
        # En PRODUCTION : On sort du JSON pur
        processors.append(structlog.processors.JSONRenderer())
    else:
        # En LOCAL : On sort du texte coloré facile à lire
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper())
        ),
        cache_logger_on_first_use=True,
    )

def get_logger(name: str, context: Optional[Dict[str, Any]] = None, **kwargs) -> Any:
    """ Retourne un logger lié à un contexte (ex: scraper_name, job_id)."""
    bound = dict(context or {})
    bound.update(kwargs)
    return structlog.get_logger(name).bind(**bound)