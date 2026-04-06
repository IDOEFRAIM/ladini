"""Compatibility shim: re-export the canonical domain models.

Prefer importing the central `agriconnect.domain.models` definitions. Some
callers still import `agriconnect.services.models_legacy`; keep that path
working but be robust to import-time failures (avoid partially-initialised
modules that lead to `ImportError: cannot import name 'Zone'`).
"""

import logging
logger = logging.getLogger(__name__)

try:
	# Preferred fallback: keep importing the historic services staging
	# module first — it still contains the concrete model definitions
	# (including `Zone`) while `domain.models` is being completed.
	from agriconnect.services.models_v3 import *  # noqa: F401,F403
	logger.debug("Loaded services.models_v3 into models_legacy shim")
except Exception:
	# If services.models_v3 isn't available, try the canonical domain.models
	try:
		from agriconnect.domain.models import *  # noqa: F401,F403
		logger.warning("Imported domain.models as fallback for models_legacy")
	except Exception:
		# Last resort: provide tiny placeholders to avoid import failures.
		logger.exception("Failed to import services.models_v3 and domain.models; using minimal placeholders")

		class Zone:  # minimal placeholder
			pass

		class User:  # minimal placeholder
			pass

# Export public symbols present in this module
__all__ = [name for name in globals() if not name.startswith("_")]

# Ensure a minimal `User` exists for legacy callers expecting it
try:
	User
except NameError:
	from sqlalchemy import Column, String, DateTime
	from sqlalchemy.sql import func
	try:
		from sqlalchemy.types import Uuid
	except Exception:
		from sqlalchemy.types import String as Uuid  # type: ignore

	class User(Base):
		__tablename__ = "users"
		__table_args__ = {"schema": "auth"}

		id = Column(Uuid(as_uuid=False), primary_key=True)
		username = Column(String, nullable=True)
		email = Column(String, nullable=True)
		created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

	__all__.append("User")
