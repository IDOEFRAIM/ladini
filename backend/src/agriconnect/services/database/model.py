"""Compatibility shim for historic imports.

Do not add new models here.

This module exists only to keep legacy imports working:
`from agriconnect.services.database.model import <Model>`

Canonical ORM definitions live in `agriconnect.domain.models`.
"""

from agriconnect.domain.models import *

import logging

logger = logging.getLogger("service.database.model")

# Re-export all public classes/objects defined in domain.models so that
# `from agriconnect.services.database.model import *` provides the
# canonical set of ORM models and helpers. This keeps legacy imports
# across the codebase working without duplicating model definitions.
__all__ = [
  name
  for name, val in globals().items()
  if not name.startswith("_") and name[0].isupper()
]

# TransactionStaging is now canonical in domain.models; keep shim thin.
