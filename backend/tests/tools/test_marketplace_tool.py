import sys
import os
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from futur.tools import marketplace as mp_mod


def test_marketplace_no_db_initialization_and_session_error():
    tool = mp_mod.MarketplaceTool()
    # When no DB configured, engine and SessionLocal should be None
    assert tool.engine is None
    assert tool.SessionLocal is None

    # _session context manager should raise RuntimeError when entered
    with pytest.raises(RuntimeError):
        with tool._session():
            pass
