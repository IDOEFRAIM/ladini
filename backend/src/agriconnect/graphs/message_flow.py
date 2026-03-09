"""Compatibility shim: expose the orchestrateur message_flow module at
`agriconnect.graphs.message_flow` for tests and older imports.

Some tests patch internal module-level names (including underscored
names). We therefore import the orchestrateur module as `_mf` and
re-export key symbols into this module namespace so tests that patch
`agriconnect.graphs.message_flow._core_db` / `settings` / `init_tracing`
continue to work.
"""
from .orchestrateur import message_flow as _mf

# Public API: re-export everything that does not start with '_' from the
# underlying orchestrateur.message_flow module.
for _name in (n for n in dir(_mf) if not n.startswith("_")):
	globals()[_name] = getattr(_mf, _name)

# Explicitly re-export internal names that tests expect to patch.
_core_db = getattr(_mf, "_core_db", None)
settings = getattr(_mf, "settings", None)
init_tracing = getattr(_mf, "init_tracing", None)
get_groq_sdk = getattr(_mf, "get_groq_sdk", None)

__all__ = [name for name in globals().keys() if not name.startswith("__")]
