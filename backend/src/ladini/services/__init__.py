"""Services package facade.

Import lazy pour éviter de charger la couche DB (et ses 10 mixins)
au simple import du package.
"""

__all__ = ["AgriDatabaseService"]


def __getattr__(name):
    if name == "AgriDatabaseService":
        from ladini.services.database import AgriDatabaseService

        return AgriDatabaseService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
