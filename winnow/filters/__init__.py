"""Fast, structural built-in filters (the hard-coded first layer).

These are hand-written for a handful of extremely common, extremely noisy
commands where a purpose-built transform beats any generic rule — installers
that print a wall of "already satisfied", git status help hints, and so on.
Anything not covered here falls through to the declarative rule engine, the
pattern collapser and the structure-aware compressors.
"""

from .registry import detect, register, all_filters  # noqa: F401
from . import builtins  # noqa: F401  (registers the built-ins on import)
