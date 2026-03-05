---
name: path-mapping-unraid-debug
description: 'Debug host/container path conversion, Unraid disk or cache path resolution, and split-share preflight behavior. Use for mapping bugs, unexpected docker paths, and share mismatch handling.'
argument-hint: 'What path(s), runtime, and observed mismatch are you debugging?'
---

# Path Mapping And Unraid Debug

## Docs First

- Check local docs that affect path and batch behavior:
- `docs/installation.md`
- `docs/cli-reference-create.md`
- `docs/batch-mode.md`
- `docs/presets.md`
- Refresh local copies with `bash scripts/update-mkbrr-docs.sh` if needed.

## When To Use

- A host path is mapped to the wrong container path.
- `split_share_preflight` fails or warns unexpectedly.
- Docker jobs include unmapped paths outside configured roots.

## Procedure

1. Capture runtime (`docker` or `native`) and relevant config (`paths`, `unraid`, and `batch`).
2. Trace conversion through `map_content_path()` and `map_torrent_path()` in `mkbrr-wizard.py`.
3. For Unraid issues, trace helper logic that resolves `/mnt/user/*` to physical mounts and review `mount_priority` behavior.
4. Reproduce with focused unit tests in `tests/test_path_conversion.py` and `tests/test_unraid.py`.
5. If behavior is intentional, improve warning/error clarity. If behavior is wrong, patch mapping logic with minimal surface area.
6. Re-run `pytest` and verify edge-case tests still pass.

## Completion Checks

- Path conversion is deterministic for both directions (host -> container and container -> host).
- Split-share behavior matches configured policy (`fail`, `warn`, `off`).
- Tests cover at least one normal case and one mismatch edge case.

## References

- `mkbrr-wizard.py`
- `tests/test_path_conversion.py`
- `tests/test_unraid.py`
- `config.yaml.sample`
