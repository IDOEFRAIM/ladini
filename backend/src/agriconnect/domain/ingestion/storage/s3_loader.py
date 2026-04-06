"""Compatibility shim for S3Loader.

Canonical loader now lives in `agriconnect.domain.ingestion.loaders.s3_loader`.
"""

from agriconnect.domain.ingestion.loaders.s3_loader import S3Loader

__all__ = ["S3Loader"]
