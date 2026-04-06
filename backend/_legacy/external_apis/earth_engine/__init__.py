"""
Google Earth Engine Integration

Services d'intégration avec Google Earth Engine pour données satellitaires et géospatiales.

Modules:
- engine: Core GEE initialization and data retrieval
- chip: CHIRPS rainfall data processing
- fao: FAO evapotranspiration calculations
- nasa: NASA satellite data integration
- soilgrid: SoilGrids soil properties data
- gee_auth: Authentication utilities

Configuration:
- Requires apikey.json with service account credentials
- Set GEMAIL environment variable
"""

from .engine import initialize_ee, get_chirps_rainfall
from .gee_auth import authenticate_gee

__version__ = "1.0.0"

__all__ = [
    "initialize_ee",
    "get_chirps_rainfall",
    "authenticate_gee"
]
