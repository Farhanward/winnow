# Contributing to Winnow

Thanks for helping improve Winnow. Filters and rules should remove repetitive
noise while keeping errors, summaries, exit status, and the path back to the
full original output.

## Set up

```bash
git clone https://github.com/Farhanward/winnow.git
cd winnow
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Make a change

1. Open an issue for substantial behavior changes.
2. Keep the change focused and add a sanitized, deterministic test fixture.
3. Verify that important errors and final summaries remain visible.
4. Run `python -m pytest -q` and `python benchmarks/benchmark.py`.
5. Open a pull request describing the command output before and after.

Never submit credentials, private logs, customer data, or proprietary source
code. Use generated fixtures that preserve the shape of the problem.

## Add a rule

Declarative rules live in `winnow/rules_data/`. Prefer a YAML rule when the
behavior can be expressed with existing actions. Use a Python filter only when
the command has structure that a line-oriented rule cannot preserve.
