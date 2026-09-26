"""Put the app directory on sys.path so tests import the modules directly,
matching how Streamlit runs them (app.py's own directory is the import root).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
