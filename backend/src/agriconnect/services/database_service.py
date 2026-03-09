"""Light wrapper re-exporting the refactored AgriDatabaseService.

The implementation has been split into focused mixins under
`agriconnect.services.database` to improve maintainability. This module
preserves the previous public import path:

    from agriconnect.services.database_service import AgriDatabaseService

"""

from agriconnect.services.database import AgriDatabaseService

__all__ = ["AgriDatabaseService"]
