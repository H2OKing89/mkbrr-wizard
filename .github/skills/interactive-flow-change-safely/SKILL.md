---
name: interactive-flow-change-safely
description: 'Change Rich prompt flow in the interactive CLI while preserving behavior. Use for menu changes, prompt defaults, branch logic updates, and end-to-end main-loop regressions.'
argument-hint: 'Which menu or prompt sequence should change?'
---

# Interactive Flow Change Safely

## Docs First
- Validate prompt wording and defaults against local docs before changing flow:
- `docs/cli-reference-create.md`
- `docs/cli-reference-check-inspect.md`
- `docs/batch-mode.md`
- `docs/presets.md`

## When To Use
- Modify menu options or prompt order in `main()`.
- Add or remove user prompts in create/check/inspect/batch flows.
- Investigate regressions caused by prompt branching.

## Procedure
1. Document current and desired interaction sequence as input/output steps.
2. Update prompt/branch logic in `mkbrr-wizard.py` with minimal edits.
3. Add or update flow tests (`tests/test_main_flow.py`, `tests/test_main_docker_flow.py`, `tests/test_main_edge_cases.py`, `tests/test_main_failures.py`, `tests/test_interactive.py`).
4. Use monkeypatch for prompt responses, runtime detection, and subprocess calls so tests stay deterministic.
5. Verify command builders still receive expected values from prompts.
6. Run `pytest` and review failures for branch-order assumptions.

## Completion Checks
- New flow is fully covered by at least one integration-style test.
- Default choices and quit paths are tested.
- No unexpected behavior changes in unaffected menu branches.

## References
- `mkbrr-wizard.py`
- `tests/test_interactive.py`
- `tests/test_main_flow.py`
- `tests/test_main_docker_flow.py`
