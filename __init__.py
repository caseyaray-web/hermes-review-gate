"""Hermes directory-plugin entry point."""
from pathlib import Path
import sys

_root = str(Path(__file__).resolve().parent)
if _root not in sys.path:
    sys.path.insert(0, _root)
from local_first_review import register

__all__ = ["register"]
