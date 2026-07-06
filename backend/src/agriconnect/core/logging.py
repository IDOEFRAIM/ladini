import logging
import sys
from typing import Optional

def get_logger(name: str) -> logging.Logger:
    """
    Centralized logger factory for AgriConnect.
    Ensures consistent formatting and level settings without resetting configuration.
    """
    logger = logging.getLogger(name)
    
    # We only add a handler if the logger doesn't have any to avoid duplicate logs in some environments
    if not logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        
    return logger
