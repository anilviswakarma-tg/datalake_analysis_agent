"""Put the app directory on sys.path so tests import the modules directly,
matching how Streamlit runs them (app.py's own directory is the import root).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _isolated_run_context():
    """Give every test its own run context and undo any start_run() it makes,
    so one test's model choice or query budget can't leak into the next."""
    import run_state
    token = run_state._CURRENT_RUN.set(run_state.RunContext())
    yield
    run_state._CURRENT_RUN.reset(token)
