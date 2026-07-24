"""Deterministic, synthetic benchmark for Winnow's compression pipeline."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from winnow.config import Config
from winnow.core import compress
from winnow.tokens import using_exact


def npm_output() -> str:
    warnings = [
        f"npm warn deprecated package-{i}@1.0.{i % 10}: synthetic deprecation notice"
        for i in range(180)
    ]
    return "\n".join(
        warnings
        + [
            "",
            "added 512 packages, and audited 513 packages in 8s",
            "72 packages are looking for funding",
            "found 0 vulnerabilities",
        ]
    )


def pip_output() -> str:
    progress = [
        f"Requirement already satisfied: package-{i} in ./.venv/lib (1.0.{i % 10})"
        for i in range(120)
    ]
    return "\n".join(progress + ["Successfully installed demo-package-2.0.0"])


def json_output() -> str:
    payload = {
        "status": "ok",
        "items": [
            {
                "id": i,
                "name": f"synthetic-item-{i}",
                "active": i % 2 == 0,
                "tags": ["benchmark", "local", f"group-{i % 5}"],
            }
            for i in range(1000)
        ],
    }
    return json.dumps(payload, indent=2)


def log_output() -> str:
    lines = [
        f"2026-07-24T12:{i // 60:02d}:{i % 60:02d}Z INFO worker heartbeat ok id={1000 + i}"
        for i in range(300)
    ]
    lines.extend(
        f"2026-07-24T12:05:{i:02d}Z ERROR database timeout retry={i}"
        for i in range(30)
    )
    return "\n".join(lines)


CASES = [
    ("npm warning wall", "npm install", npm_output()),
    ("pip satisfied chatter", "python -m pip install -r requirements.txt", pip_output()),
    ("JSON API response", "curl https://api.example.test/items", json_output()),
    ("repetitive server log", "docker logs api", log_output()),
]


def main() -> int:
    cfg = Config()
    rows = []
    for name, command, raw in CASES:
        result = compress(command, raw, cfg=cfg, remember=False)
        rows.append(
            (
                name,
                result.raw_tokens,
                result.comp_tokens,
                result.pct,
                result.label,
            )
        )

    print(f"Token counter: {'tiktoken cl100k_base' if using_exact() else 'char/4 heuristic'}")
    print()
    print("| Synthetic case | Before | After | Saved | Pipeline |")
    print("|---|---:|---:|---:|---|")
    for name, before, after, pct, label in rows:
        print(f"| {name} | {before:,} | {after:,} | {pct:.1f}% | `{label}` |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
