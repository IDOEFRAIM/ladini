"""Geofencing — service de livraison limité au Burkina Faso.

Rectangle englobant simple (pas un contour précis du pays) — volontairement
permissif aux frontières pour ne jamais rejeter un point légitime proche
d'une frontière à cause d'une imprécision GPS WhatsApp normale. Utilisé à
la fois côté webhook (rejet immédiat d'un point hors zone) et côté DB
(`update_geo_location`/`set_order_delivery_location`) en défense en
profondeur, puisque ces méthodes sont aussi des outils MCP appelables hors
du webhook Twilio.
"""

from __future__ import annotations

BURKINA_FASO_LAT_MIN = 9.3
BURKINA_FASO_LAT_MAX = 15.1
BURKINA_FASO_LON_MIN = -5.5
BURKINA_FASO_LON_MAX = 2.4

OUT_OF_COUNTRY_MESSAGE = (
    "📍 Ce point GPS est hors du Burkina Faso — notre service de livraison "
    "y est limité pour le moment. Merci de partager une position à "
    "l'intérieur du pays (bouton 📎 puis *Localisation*)."
)


def is_within_burkina_faso(lat: float, lon: float) -> bool:
    """True si (lat, lon) tombe dans le rectangle englobant du Burkina Faso."""
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    return (
        BURKINA_FASO_LAT_MIN <= lat_f <= BURKINA_FASO_LAT_MAX
        and BURKINA_FASO_LON_MIN <= lon_f <= BURKINA_FASO_LON_MAX
    )
