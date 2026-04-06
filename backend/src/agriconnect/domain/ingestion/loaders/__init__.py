"""Ingestion loaders.

Loaders are consumer-only readers for already collected raw data.
"""

from .s3_loader import S3Loader

__all__ = ["S3Loader"]
