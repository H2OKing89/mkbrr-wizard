---
name: single-file-test-first-fix
description: 'Make safe changes in a single-file Python CLI by writing or updating tests first. Use for bug fixes, behavior changes, and regressions in mkbrr-wizard.py with pytest monkeypatch patterns.'
argument-hint: 'What bug or behavior change should be covered first?'
---

# Single-File Test-First Fix

## Docs First

- When behavior depends on mkbrr flags or defaults, verify with:
- `docs/cli-reference-create.md`
- `docs/cli-reference-check-inspect.md`
- `docs/presets.md`
- `docs/batch-mode.md`

## When To Use

- You need a safe fix in `mkbrr-wizard.py` without regressions.
- A behavior is unclear and should be locked with a test before editing logic.
- You are touching prompts, command builders, config loading, or runtime detection.

## Procedure

1. Reproduce the issue in a focused test file under `tests/`.
2. Use the fixture-based import pattern from `tests/conftest.py`: always reference functions and classes via `mkbrr_wizard.<name>`.
3. Prefer `monkeypatch.setattr(mkbrr_wizard, ...)` when mocking module-level behavior.
4. Run the smallest test slice first (`pytest tests/<file>.py -k <name>`), then implement minimal code changes in `mkbrr-wizard.py`.
5. Expand coverage for nearby edge cases only where risk is high.
6. Run full `pytest` and then `ruff check .`.

## Completion Checks

- At least one test fails before the fix and passes after.
- No direct imports from `mkbrr-wizard.py` in tests.
- Existing behavior outside the target area remains green.

## References

- `tests/conftest.py`
- `tests/test_commands_builder.py`
- `tests/test_main_flow.py`
- `pyproject.toml`
