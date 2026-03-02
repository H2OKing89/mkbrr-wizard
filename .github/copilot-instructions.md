# Copilot Instructions — mkbrr-wizard

## Project Overview

Single-file Python CLI wizard (`mkbrr-wizard.py`, ~2400 lines) that wraps the [mkbrr](https://github.com/autobrr/mkbrr) torrent creator. It drives mkbrr via either Docker or a native binary, with interactive Rich UI prompts, Unraid-specific path handling, and push notifications (Pushover/Discord).

## mkbrr Documentation

When answering questions about mkbrr's CLI flags, configuration, or features, consult the upstream docs rather than guessing:

- **Full doc index for LLMs**: <https://mkbrr.com/llms.txt> — lists every available page as a `.md` URL (e.g. `https://mkbrr.com/installation.md`). Fetch this first to discover the right page, then fetch the specific `.md` URL to get the content.
- **Local offline copies** are committed under [`docs/`](../docs/) for the most commonly needed pages (installation, `create`/`check`/`inspect` CLI reference, presets, batch mode). These can be refreshed by running `bash scripts/update-mkbrr-docs.sh`.

## Architecture

Everything lives in `mkbrr-wizard.py` — there are no packages or submodules. The file is organized into labeled sections (grep for `# ---` comment dividers):

1. **Config + parsing** — `@dataclass(frozen=True)` hierarchy (`AppCfg` → `PathsCfg`, `MkbrrCfg`, `OwnershipCfg`, `BatchCfg`, `UnraidCfg`, `WorkersCfg`, `NotificationsCfg`, etc.) loaded from `config.yaml` via `load_config()`.
2. **Runtime detection** — `pick_runtime()`, `docker_available()`, `native_available()` decide docker vs native.
3. **Path mapping** — `map_content_path()` / `map_torrent_path()` translate between host (`/mnt/user/data/...`) and container (`/data/...`) paths bidirectionally based on runtime. Unraid helpers resolve FUSE share paths to physical disk paths.
4. **Workers auto-tune** — `detect_storage_type()` reads `/sys/block/*/queue/rotational` to choose HDD vs SSD worker counts.
5. **Command builders** — Pure functions (`build_create_command`, `build_inspect_command`, `build_check_command`, `build_batch_job_create_command`) that return `(cmd_list, cwd)`. These are the primary unit-test targets.
6. **Notifications** — `NotificationManager` sends async HTTP via `httpx` to Pushover/Discord. Uses a background `asyncio` event loop on a daemon thread.
7. **Interactive UI** — `main()` loop using `rich` prompts. Batch mode collects jobs interactively and validates against `schema/batch.json` (JSON Schema draft-07).

## Module Import Pattern

The filename contains a hyphen (`mkbrr-wizard.py`), so it cannot be imported normally. Tests use `importlib.util.spec_from_file_location` in `tests/conftest.py` to load it as `mkbrr_wizard`. All test files receive the module through the `mkbrr_wizard` pytest fixture:

```python
def test_example(mkbrr_wizard: ModuleType) -> None:
    cfg = mkbrr_wizard.AppCfg(...)
    result = mkbrr_wizard.map_content_path(cfg, "docker", "/mnt/user/data/file")
```

Always access functions/classes via `mkbrr_wizard.<name>` in tests — never use bare imports.

## Testing

- **Framework**: pytest with `monkeypatch` for mocking (no `unittest.TestCase`).
- **Run tests**: `pytest` (configured in `pyproject.toml` — `testpaths = ["tests"]`, `-v --tb=short`).
- **Test structure**: Each test file focuses on one functional area — `test_path_conversion.py`, `test_commands_builder.py`, `test_workers.py`, `test_unraid.py`, `test_notifications.py`, `test_config.py`, `test_batch_mode.py`, etc.
- **Config in tests**: Build `AppCfg` dataclass instances directly (don't rely on YAML files). Use `tmp_path` for files that must exist on disk.
- **Mocking pattern**: `monkeypatch.setattr(mkbrr_wizard, "function_name", ...)` or `monkeypatch.setattr(mkbrr_wizard.os.path, "exists", ...)` since the module has its own `os` reference.
- **Integration tests** (`test_main_flow.py`): Monkeypatch `parse_args`, user prompts, and `subprocess.run` to drive `main()` end-to-end without actual Docker/mkbrr.

## Code Style & Tooling

- **Python ≥ 3.10** — uses `X | Y` union syntax, `match` is not used but `|` type hints are.
- **Line length**: 100 (Black + Ruff + isort all configured in `pyproject.toml`).
- **Linting**: `ruff check .` — selected rules: E, W, F, I, B, C4, UP.
- **Formatting**: `black .` and `isort .`.
- **Type checking**: `pyright` (basic mode) — `reportMissingImports = "warning"`, several `Unknown*` reports suppressed.
- **All dataclasses are `frozen=True`** — never mutate config after construction.

## Key Conventions

- **Bool coercion**: `_coerce_bool()` intentionally accepts typo `"ture"` as `True` (this is deliberate, not a bug).
- **Path cleaning**: `_clean_user_path()` strips quotes and expands `~`/`$VARS`; `_expand_env()` expands env vars without path normalization (used for URLs/tokens).
- **Env var expansion**: Notification tokens in `config.yaml` use `${VAR}` syntax, expanded at config load time via `os.path.expandvars`.
- **Optional dependencies**: `prompt_toolkit`, `httpx`, `python-dotenv` are wrapped in `try/except ImportError` with feature flags (`_has_prompt_toolkit`, `_has_httpx`).
- **Batch schema validation**: `schema/batch.json` is loaded and validated using `jsonschema.Draft7Validator` before execution.
- **No output flag**: mkbrr commands avoid `-o` flag; instead, native uses `cwd=host_output_dir` and docker uses `-w container_output_dir` for output placement.
