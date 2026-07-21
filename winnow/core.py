"""The compression pipeline — where the layers combine.

For a given command and its raw output, Winnow runs the layers in order and
picks the result, always with a safety valve:

1. **Tee** the full raw output to the recall store (nothing is ever lost).
2. If the output is **JSON**, compress it structurally (line filters would
   corrupt it, so this path is exclusive).
3. Otherwise run the **built-in filter** for the command (if any), then the
   **declarative rules**, which include generic fold/cascade passes.
4. **Safety check**: if the result doesn't save at least ``min_saving`` of the
   tokens, or the output was tiny to begin with, pass the original through
   untouched.

The model sees the compressed body plus a one-line footer telling it how to
recall the full output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import rules as rules_mod
from . import semantic, tokens
from .config import Config
from .filters import detect
from .store import Store


@dataclass
class Result:
    command: str
    raw: str
    body: str          # compressed text (no footer)
    raw_tokens: int
    comp_tokens: int
    saved: int
    pct: float
    handle: Optional[str]
    label: str         # which layer(s) acted, e.g. "git-status+fold-repeats"
    passthrough: bool

    def render(self, footer: bool = True) -> str:
        if not footer or self.passthrough:
            return self.body
        return self.body + "\n" + self.footer()

    def footer(self) -> str:
        if self.passthrough:
            return f"⟨winnow: passthrough ({self.raw_tokens} tok)⟩"
        recall = f" · full: wn recall {self.handle}" if self.handle else ""
        return (
            f"⟨winnow {self.label}: {self.raw_tokens}→{self.comp_tokens} tok, "
            f"saved {self.pct:.0f}%{recall}⟩"
        )


def compress(
    command: str,
    raw: str,
    cfg: Optional[Config] = None,
    store: Optional[Store] = None,
    remember: bool = True,
    exit_code: int = 0,
    cwd: str = "",
) -> Result:
    cfg = cfg or Config.load()
    raw_tokens = tokens.estimate(raw)

    body, label = _pipeline(command, raw, cfg)
    comp_tokens = tokens.estimate(body)

    saved = raw_tokens - comp_tokens
    pct = (saved / raw_tokens * 100) if raw_tokens else 0.0

    # Safety valve: too small, or not enough saved — hand back the original.
    passthrough = (
        raw_tokens < cfg.min_tokens
        or body == raw
        or (raw_tokens and saved / raw_tokens < cfg.min_saving)
    )
    if passthrough:
        return Result(command, raw, raw, raw_tokens, raw_tokens, 0, 0.0,
                      None, "passthrough", True)

    handle = None
    if remember and store is not None:
        handle = store.put(command, cwd, exit_code, raw, raw_tokens,
                           comp_tokens, label)

    return Result(command, raw, body, raw_tokens, comp_tokens, saved, pct,
                  handle, label, False)


def _pipeline(command: str, raw: str, cfg: Config):
    """Return (compressed_body, label)."""
    parts: List[str] = []

    # JSON is structural — exclusive path, line filters would corrupt it.
    js = semantic.compress_json(raw)
    if js is not None:
        body, changed = js
        if changed and len(body) < len(raw):
            return body, "json"

    body = raw

    # Built-in structural filter for this command, if one matches.
    func, name = detect(command)
    if func is not None:
        try:
            out = func(body, cfg)
        except Exception:
            out = None
        if out is not None and out != body:
            body = out
            parts.append(name)

    # Declarative rules (built-in packs + user packs).
    body, applied = rules_mod.apply_rules(command, body, rules_mod.load_rules(), cfg)
    parts.extend(applied)

    label = "+".join(parts) if parts else "none"
    return body, label
