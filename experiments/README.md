# Experiments Sandbox (Fase 3.5)

This directory is gitignored by default (`.gitignore` keeps only this
`README.md`). It exists so the auto-research loop
([scripts/auto_research.py](../scripts/auto_research.py)) — or a human
following up on one of its proposals — has a place to write throwaway
prototypes without touching `gpc/`.

## Rules

- Nothing under `experiments/` is imported by `gpc/` or by any file under
  `tests/`. It is not part of the shipped package.
- The auto-research loop never writes here automatically today; this
  directory is prepared for that future use and for humans investigating a
  proposal from `docs/proposals/`.
- Any prototype that proves useful must be promoted through a normal human
  pull request that moves the real implementation into `gpc/`, adds
  coverage under `tests/` or `tests/smoke/`, and passes
  `scripts/run_smoke_tests.sh` green — never merged directly from here.
