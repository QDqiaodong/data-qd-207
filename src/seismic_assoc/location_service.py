"""Compatibility import path for location diagnostics."""
from .location import *  # noqa: F401,F403
from .location import __getattr__  # noqa: F401
from .location import evaluate_location_candidates  # noqa: F401
