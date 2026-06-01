"""Shared cloud-native primitives for scraper jobs."""

from .base_scraper import BaseScraper
from .message_models import ScraperQueueMessage
from .sqs_provider import SQSProvider

__all__ = ["BaseScraper", "ScraperQueueMessage", "SQSProvider"]
