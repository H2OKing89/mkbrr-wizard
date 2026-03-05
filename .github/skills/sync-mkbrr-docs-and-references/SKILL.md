---
name: sync-mkbrr-docs-and-references
description: 'Refresh local mkbrr documentation and reconcile wizard behavior/docs with upstream references. Use for docs drift, flag verification, and release-time doc updates.'
argument-hint: 'Which mkbrr area should be re-validated (install/create/check/inspect/presets/batch)?'
---

# Sync mkbrr Docs And References

## Docs Directory Scope
- Treat `docs/` as the canonical local reference set for mkbrr behavior.
- Prioritize these files during validation:
- `docs/installation.md`
- `docs/cli-reference-create.md`
- `docs/cli-reference-check-inspect.md`
- `docs/presets.md`
- `docs/batch-mode.md`

## When To Use
- Local `docs/*.md` are stale.
- There is uncertainty about mkbrr flag semantics.
- README or wizard help text may not match current mkbrr behavior.

## Procedure
1. Fetch index from `https://mkbrr.com/llms.txt` and identify required upstream pages.
2. Refresh local docs by running `bash scripts/update-mkbrr-docs.sh`.
3. Compare changed docs against behavior in `mkbrr-wizard.py` command builders and prompt text.
4. Update README or inline guidance if wording is stale.
5. Add or adjust tests when behavior changes are required (not just doc text).

## Completion Checks
- `docs/` is updated for relevant pages.
- Claims in README and wizard prompts match refreshed docs.
- Any behavior changes are covered by tests.

## References
- `scripts/update-mkbrr-docs.sh`
- `docs/installation.md`
- `docs/cli-reference-create.md`
- `docs/cli-reference-check-inspect.md`
- `docs/presets.md`
- `docs/batch-mode.md`
