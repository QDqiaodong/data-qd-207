"""Compatibility import path for the dataset release ledger."""
from .release_ledger import *  # noqa: F401,F403
import seismic_assoc.release_ledger as _ledger

__all__ = _ledger.__all__
