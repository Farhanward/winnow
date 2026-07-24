# Winnow benchmark

Run the benchmark from the repository root:

```bash
python benchmarks/benchmark.py
```

The fixtures are synthetic, deterministic, and generated in memory. They
represent output shapes Winnow targets: package-manager chatter, large JSON
arrays, and repetitive logs. The script calls Winnow's real compression
pipeline and token estimator.

Results measure tokens removed from individual command output. They do not
claim an equal reduction in an agent's total context usage, API bill, or task
cost. Install `winnow-cli[tokens]` to use `tiktoken`; otherwise the benchmark
reports the built-in character heuristic.
