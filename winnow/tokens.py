"""Token estimation.

Uses ``tiktoken`` (cl100k_base) when it is installed for an exact count, and
otherwise falls back to a fast character-based heuristic. The heuristic is
deliberately simple: for English prose and source code, ~4 characters per token
is a well-established approximation, good enough to drive compression decisions
and savings analytics without a hard dependency or a network download.
"""

from __future__ import annotations

_ENCODER = None
_TRIED = False


def _encoder():
    global _ENCODER, _TRIED
    if _TRIED:
        return _ENCODER
    _TRIED = True
    try:  # optional dependency — never required
        import tiktoken

        _ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _ENCODER = None
    return _ENCODER


def estimate(text: str) -> int:
    """Estimate the number of tokens in ``text``."""
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # Heuristic: blend a char/4 estimate with a whitespace word count so that
    # very "wide" lines (long tokens) and very "tall" output both behave well.
    chars = len(text)
    return max(1, round(chars / 4))


def using_exact() -> bool:
    """True when exact tiktoken counting is active (for diagnostics)."""
    return _encoder() is not None
