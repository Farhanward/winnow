"""Structure-aware compression.

Two kinds of structured input get special treatment instead of blind line
trimming:

* **JSON** — arrays are truncated to a sample, long strings are clipped and deep
  nesting is elided, while the overall shape (keys, types) is preserved. A 4000
  element API response collapses to its schema plus a handful of examples.
* **Python source** — reduced to a skeleton of imports, class/function
  signatures and docstrings with bodies elided, so "show me this module" costs a
  fraction of the tokens while staying readable.

Both are lossy *views*; the untouched original is always in the recall store.
"""

from __future__ import annotations

import ast
import json
from typing import Any, Optional, Tuple


def try_json(text: str) -> Optional[Any]:
    """Parse ``text`` as JSON, tolerating leading/trailing whitespace."""
    s = text.strip()
    if not s or s[0] not in "[{":
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def _compress_value(value: Any, max_array: int, max_str: int, depth: int) -> Any:
    if depth <= 0:
        return "…"
    if isinstance(value, str):
        if len(value) > max_str:
            return value[:max_str] + f"… ⟨+{len(value) - max_str} chars⟩"
        return value
    if isinstance(value, list):
        if len(value) > max_array:
            head = [
                _compress_value(v, max_array, max_str, depth - 1)
                for v in value[:max_array]
            ]
            head.append(f"… ⟨{len(value) - max_array} more of {len(value)} items⟩")
            return head
        return [_compress_value(v, max_array, max_str, depth - 1) for v in value]
    if isinstance(value, dict):
        return {
            k: _compress_value(v, max_array, max_str, depth - 1)
            for k, v in value.items()
        }
    return value


def compress_json(
    text: str, max_array: int = 8, max_str: int = 200, max_depth: int = 8
) -> Optional[Tuple[str, bool]]:
    """Return ``(compressed_json_text, changed)`` or ``None`` if not JSON."""
    data = try_json(text)
    if data is None:
        return None
    compressed = _compress_value(data, max_array, max_str, max_depth)
    out = json.dumps(compressed, indent=2, ensure_ascii=False)
    return out, out != text.strip()


def skim_python(source: str) -> Optional[str]:
    """Reduce Python source to a signature+docstring skeleton, or None on error."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    lines: list[str] = []

    def emit(node: ast.AST, indent: int) -> None:
        pad = "    " * indent
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            lines.append(pad + _segment(source, node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            sig = _signature(node)
            lines.append(f"{pad}{prefix} {node.name}{sig}:")
            doc = ast.get_docstring(node)
            if doc:
                first = doc.strip().split("\n")[0]
                lines.append(f'{pad}    """{first}"""')
            lines.append(f"{pad}    ...")
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(_unparse(b) for b in node.bases)
            head = f"{pad}class {node.name}" + (f"({bases}):" if bases else ":")
            lines.append(head)
            doc = ast.get_docstring(node)
            if doc:
                first = doc.strip().split("\n")[0]
                lines.append(f'{pad}    """{first}"""')
            body = [n for n in node.body if isinstance(
                n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            if body:
                for child in body:
                    emit(child, indent + 1)
            else:
                lines.append(f"{pad}    ...")

    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        lines.append(f'"""{mod_doc.strip().splitlines()[0]}"""')
    for node in tree.body:
        emit(node, 0)
    return "\n".join(lines) if lines else None


def _signature(node: ast.AST) -> str:
    try:
        args = ast.unparse(node.args)  # type: ignore[attr-defined]
        return f"({args})"
    except Exception:
        return "(...)"


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def _segment(source: str, node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return source.split("\n")[node.lineno - 1].strip()
