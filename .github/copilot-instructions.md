# Copilot Instructions — mkbrr-wizard

## Project overview

`mkbrr-wizard` is an installable Python package that wraps
[mkbrr](https://github.com/autobrr/mkbrr). It supports an interactive Rich wizard,
headless commands, native or Docker execution, strict batch manifests, and
Unraid-aware planning and scheduling.

The root `mkbrr-wizard.py` is only a source-checkout compatibility launcher. New
application code belongs under `src/mkbrr_wizard/`.

## mkbrr documentation

Confirm mkbrr flags and behavior from the local files under `docs/` before changing
builders or validation. The upstream index is <https://mkbrr.com/llms.txt>; refresh
the local copies with `bash scripts/update-mkbrr-docs.sh` when necessary.

## Architecture

- `cli.py`: argparse entry point, headless dispatch, JSON/error presentation
- `application.py`: UI-neutral plan execution and result coordination
- `batch_models.py`: strict batch models and generated JSON Schema
- `models.py`: serializable plan, operation, result, and progress contracts
- `planning/planner.py`: path resolution and effective operation construction
- `planning/presets.py`: preset/default merging for effective views and resume keys
- `execution/scheduler.py`: bounded per-device scheduling and atomic resume reports
- `ui/rendering.py`: caller-console Rich renderers for plans, queues, and results
- `legacy_app.py`: compatibility home of the existing interactive workflow and its
  mature config/path/command helpers; shrink this incrementally without changing
  interactive behavior

The intended dependency flow is:

```text
CLI or prompts -> typed request -> ExecutionPlan -> WizardApplication
               -> DiskAwareScheduler -> OperationResult/ProgressEvent
               -> JSON or Rich rendering
```

Planning, execution models, and the scheduler must never print, prompt, or create a
Rich `Console`. UI code receives a caller-owned console.

## Imports and tests

Use normal package imports, for example:

```python
from mkbrr_wizard.batch_models import BatchManifest
from mkbrr_wizard.planning import PlanBuilder
```

Older interactive characterization tests use the `mkbrr_wizard` fixture from
`tests/conftest.py`; that fixture deliberately exposes `mkbrr_wizard.legacy_app`.
Keep those tests stable while moving new behavior to directly imported modules.

- Use `tmp_path` for filesystem behavior and `monkeypatch` or `unittest.mock` for
  subprocess/runtime boundaries.
- Add focused model tests for validation rules and at least one CLI test for new
  automation behavior.
- Never require Docker, mkbrr, Unraid mounts, or network access in unit tests.

## Single sources of truth

- `BatchJob` and `BatchManifest` own batch validation.
- `generate_batch_json_schema()` owns both tracked `schema/batch.json` copies.
- Shared legacy command builders remain the only functions that assemble mkbrr
  arguments until they are extracted as a unit.
- `ExecutionPlan`, `OperationResult`, and `ProgressEvent` are the boundary types;
  do not add new tuple/dict result protocols.
- Effective plans must show merged preset/default/CLI values, not merely overrides.
- Resume identities must change when effective work changes and remain stable across
  generated Docker container names.

## Key behavior

- Configuration is strict Pydantic v2 with `extra="forbid"`. The legacy typo
  `"ture"` is migrated once with a warning; do not silently accept other typos.
- Docker paths outside configured mounts fail during planning.
- Unraid split-share policy is applied before execution.
- Filtering patterns from presets and CLI are additive; ordinary CLI options
  override preset values.
- Batch output collisions are rejected after path normalization and runtime mapping.
- Spinning disks default to one hashing job per physical device. Global worker
  budgets must also account conservatively for mkbrr's automatic worker mode.
- Headless exit codes are `0` success, `1` execution failure, `2` input/config
  failure, `124` timeout without a successful operation, and `130` cancellation.

## Tooling

- Python 3.10+
- `pytest`
- `ruff check .`
- `black --check src tests mkbrr-wizard.py`
- `mypy src tests`
- Pyright configuration also lives in `pyproject.toml`; do not add a second config

CI installs `.[dev]`, checks Python 3.10–3.13, and builds/installs a wheel. Keep dev
dependencies complete enough for a clean environment rather than relying on local
optional packages.
