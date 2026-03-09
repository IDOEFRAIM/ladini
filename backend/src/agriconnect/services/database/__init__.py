from .auth import AuthMixin
from .utils import UtilsMixin
from .marketplace import MarketplaceMixin
from .transactions import TransactionsMixin
from .intelligence import IntelligenceMixin
from .dashboards import DashboardsMixin

import inspect
from functools import wraps
from .common import get_db


class AgriDatabaseService(AuthMixin, UtilsMixin, MarketplaceMixin, TransactionsMixin, IntelligenceMixin, DashboardsMixin):
    """Facade service composed from focused mixins.

    This class exposes the same public API but centralizes session management:
    any mixin coroutine whose first parameter is `session` will be wrapped so
    that the service opens a DB session (`get_db`) and passes it to the mixin
    implementation.
    """


def _wrap_mixin_methods_with_session():
    bases = (AuthMixin, UtilsMixin, MarketplaceMixin, TransactionsMixin, IntelligenceMixin, DashboardsMixin)
    for base in bases:
        for name, func in base.__dict__.items():
            if not callable(func) or not inspect.iscoroutinefunction(func):
                continue
            sig = inspect.signature(func)
            params = list(sig.parameters.keys())
            if not params or params[0] != "session":
                # only wrap methods whose first parameter is `session`
                continue

            # avoid double-wrapping
            if hasattr(AgriDatabaseService, name) and getattr(AgriDatabaseService, name) is not func:
                # create wrapper bound to the original function from the base
                def make_wrapper(f):
                    @wraps(f)
                    async def wrapper(self, *args, **kwargs):
                        # If caller passed an AsyncSession as first arg, reuse it.
                        from sqlalchemy.ext.asyncio import AsyncSession
                        if args and isinstance(args[0], AsyncSession):
                            return await f(self, *args, **kwargs)
                        async with get_db() as session:
                            return await f(self, session, *args, **kwargs)
                    return wrapper

                setattr(AgriDatabaseService, name, make_wrapper(func))


# perform the wrapping at import time
_wrap_mixin_methods_with_session()

__all__ = ["AgriDatabaseService"]
