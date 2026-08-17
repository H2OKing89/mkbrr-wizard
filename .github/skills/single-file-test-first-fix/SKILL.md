---
name: package-test-first-fix
description: 'Make safe changes in mkbrr-wizard by writing or updating tests first. Use for bug fixes, behavior changes, and regressions across the package or legacy interactive workflow.'
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

- You need a safe package or interactive-compatibility fix without regressions.
- A behavior is unclear and should be locked with a test before editing logic.
- You are touching prompts, command builders, config loading, or runtime detection.

## Procedure

1. Reproduce the issue in a focused test file under `tests/`.
2. Import new package modules normally. Use the `mkbrr_wizard` fixture only for behavior still owned by `legacy_app.py`.
3. Mock at the module boundary where a dependency is looked up.
4. Run the smallest test slice first (`pytest tests/<file>.py -k <name>`), then implement the change in the owning module under `src/mkbrr_wizard/`.
5. Expand coverage for nearby edge cases only where risk is high.
6. Run full `pytest` and then `ruff check .`.

## Completion Checks

- At least one test fails before the fix and passes after.
- No imports from the root compatibility launcher in tests.
- Existing behavior outside the target area remains green.

## References

- `tests/conftest.py`
- `tests/test_commands_builder.py`
- `tests/test_main_flow.py`
- `pyproject.toml`
