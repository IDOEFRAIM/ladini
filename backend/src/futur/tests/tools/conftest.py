import os
import sys
import logging

logger = logging.getLogger(__name__)

# Ensure backend/src is on sys.path so `agriconnect` package imports work
ROOT = os.path.dirname(os.path.dirname(__file__))
SRC = os.path.join(ROOT, "backend", "src")
if os.path.isdir(SRC):
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
        logger.debug("Added %s to sys.path for tests", SRC)
else:
    logger.warning("Expected source directory not found: %s", SRC)
