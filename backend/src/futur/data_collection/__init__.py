"""Compatibility shim: re-export data_collection modules from new domain location.

This package preserves old import paths while the codebase migrates to
`agriconnect.domain.ingestion.data_collection`.
"""

__all__ = ["weather", "documents", "market"]
