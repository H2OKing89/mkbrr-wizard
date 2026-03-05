---
name: mkbrr-flag-implementation
description: 'Implement or update mkbrr CLI option support in mkbrr-wizard. Use for adding create/check/inspect flags, wiring config and prompts, updating command builders, and adding focused tests.'
argument-hint: 'Which mkbrr flag or behavior should be added or changed?'
---

# mkbrr Flag Implementation

## Docs First
- Start from local copies in `docs/` before changing behavior:
- `docs/cli-reference-create.md`
- `docs/cli-reference-check-inspect.md`
- `docs/presets.md`
- `docs/batch-mode.md`
- If local docs look stale, refresh with `bash scripts/update-mkbrr-docs.sh`.

## When To Use
- Add support for a new mkbrr flag in `create`, `check`, or `inspect` flows.
- Change how an existing option is collected in the interactive wizard.
- Align wizard behavior with upstream mkbrr docs.

## Procedure
1. Confirm expected behavior from local docs in `docs/` and upstream pages listed in `https://mkbrr.com/llms.txt`.
2. Locate affected builder function in `mkbrr-wizard.py` (`build_create_command`, `build_check_command`, `build_inspect_command`, or `build_batch_job_create_command`).
3. Apply minimal dataclass/config changes needed in `load_config()` and related config models.
4. Update interactive prompt collection in `main()` only where required by the new option.
5. Add or update tests in relevant files under `tests/` (usually `tests/test_commands_builder.py` plus one integration-style flow test if UI behavior changed).
6. Run `pytest`, then run `ruff check .` for style regressions.

## Completion Checks
- The command list includes the new option only when intended.
- Native and Docker runtime behavior remain consistent.
- Tests assert both inclusion and omission paths for the flag.
- Docs/comments in code avoid guessing and match mkbrr docs.

## References
- `.github/copilot-instructions.md`
- `docs/cli-reference-create.md`
- `docs/cli-reference-check-inspect.md`
