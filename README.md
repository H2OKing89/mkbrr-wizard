# mkbrr-wizard

`mkbrr-wizard` is an installable Python CLI for creating, inspecting, and checking
torrents with [mkbrr](https://mkbrr.com). It keeps the Rich interactive wizard as
the default experience and also provides headless commands for scripts, cron, and
other automation. It can run mkbrr natively or through Docker and includes
Unraid-aware path resolution and disk scheduling.

## Features

- Interactive create, inspect, check, and batch workflows
- Headless `create`, `check`, `inspect`, `batch`, `plan`, `doctor`, and `schema`
  commands
- Native and Docker runtimes with configurable host/container path mapping
- Strict, versioned YAML or JSON batch manifests and generated JSON Schema
- Effective-plan previews with resolved paths, options, estimates, and warnings
- Machine-readable JSON output and non-interactive dry runs
- Disk-aware batch concurrency, atomic reports, and resumable runs
- Optional Unraid split-share checks, worker tuning, ownership fixes, and alerts

The application code uses the `src/mkbrr_wizard/` package. The root
`mkbrr-wizard.py` file remains only as a compatibility launcher for existing source
checkout workflows.

## Requirements

- Python 3.10 or newer
- Linux or Unraid
- One mkbrr runtime:
  - a native `mkbrr` executable on `PATH`, or
  - Docker and the configured mkbrr image

Docker is optional. With `runtime: auto`, the wizard uses Docker when it is enabled
and available, then falls back to the configured native binary.

## Installation

Clone the repository and install the package into a virtual environment:

```bash
git clone https://github.com/H2OKing89/mkbrr-wizard.git
cd mkbrr-wizard
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
mkbrr-wizard --config ./config.yaml init-config
mkbrr-wizard --help
```

Use `python -m pip install -e ".[ui]"` for the optional prompt-toolkit UI or
`python -m pip install -e ".[dev]"` for tests and code-quality tools.

An installed `mkbrr-wizard` console command and `python -m mkbrr_wizard` are the
preferred entry points. `./mkbrr-wizard.py` still works from a source checkout.

## Configuration

All runtime and path values are configured in YAML; editing Python source is not
required. Create a user configuration with `mkbrr-wizard init-config`, or review
the source [`config.yaml.sample`](config.yaml.sample). Without an explicit
`--config`, the lookup order is:

1. `MKBRR_WIZARD_CONFIG`
2. `config.yaml` in a source checkout
3. `$XDG_CONFIG_HOME/mkbrr-wizard/config.yaml`, or
   `~/.config/mkbrr-wizard/config.yaml`

`init-config` writes to the default user configuration path. Use
`mkbrr-wizard --config /path/to/config.yaml init-config` for another location;
it refuses to overwrite an existing file unless `--force` is supplied.

Common settings include:

| Setting | Default | Purpose |
| --- | --- | --- |
| `runtime` | `auto` | Select `auto`, `docker`, or `native` |
| `docker_support` | `true` | Allow Docker during automatic selection |
| `mkbrr.binary` | `mkbrr` | Native executable name or path |
| `mkbrr.image` | tested image | Docker image used for mkbrr |
| `paths.host_data_root` | `/mnt/user/data` | Host content root |
| `paths.container_data_root` | `/data` | Docker content mount |
| `paths.host_output_dir` | Unraid example | Host torrent output directory |
| `paths.container_output_dir` | `/torrentfiles` | Docker torrent output mount |
| `paths.host_config_dir` | Unraid example | Host preset directory |
| `presets_yaml` | `presets.yaml` | Relative to config directory or absolute |

Use global flags before the command to override runtime or configuration for one
invocation:

```bash
mkbrr-wizard --config /etc/mkbrr-wizard/config.yaml --native doctor
mkbrr-wizard --docker create /mnt/user/data/release -P tracker
```

### Presets

Create `presets.yaml` under `paths.host_config_dir`, or set `presets_yaml` to an
absolute file:

```yaml
version: 1
presets:
  tracker:
    trackers:
      - https://tracker.example.com/announce
    source: EXAMPLE
    private: true
```

### Batch scheduler

The headless scheduler can run different storage devices concurrently while
limiting contention on each device. Conservative defaults serialize work:

```yaml
batch:
  mode: simple
  job_timeout_seconds: null
  max_parallel_jobs: 1
  hdd_parallel_per_device: 1
  ssd_parallel_per_device: 2
  max_total_workers: null
```

| Setting | Meaning |
| --- | --- |
| `mode` | `simple` or `advanced` prompts in the interactive batch builder |
| `job_timeout_seconds` | Optional positive timeout for each mkbrr process |
| `max_parallel_jobs` | Maximum jobs running across all devices |
| `hdd_parallel_per_device` | Maximum concurrent jobs for one detected HDD |
| `ssd_parallel_per_device` | Jobs per detected SSD/NVMe |
| `max_total_workers` | Optional aggregate worker budget across active jobs |

The four concurrency limits also have corresponding `batch` and `plan` command
flags, which override the configuration for that run or preview.

## Usage

Running without a subcommand opens the interactive wizard. The explicit form is
useful in aliases and service definitions:

```bash
mkbrr-wizard
mkbrr-wizard interactive
```

Headless commands never prompt:

| Command | Purpose |
| --- | --- |
| `create PATH -P PRESET` | Create one torrent |
| `inspect TORRENT` | Inspect torrent metadata |
| `check TORRENT CONTENT` | Verify local content |
| `batch MANIFEST -P PRESET` | Validate and execute a batch manifest |
| `plan MANIFEST -P PRESET` | Preview a batch without executing it |
| `doctor` | Validate configuration, runtime, presets, and directories |
| `schema [DESTINATION]` | Print or write the generated batch JSON Schema |

Run `mkbrr-wizard COMMAND --help` for all command-specific options.

### Headless examples

```bash
# Preview a create operation as JSON without running mkbrr.
mkbrr-wizard create /mnt/user/data/release -P tracker --dry-run --json

# Create with explicit metadata and output.
mkbrr-wizard create /mnt/user/data/release -P tracker \
  --output /mnt/user/data/torrents/release.torrent \
  --source EXAMPLE --private --piece-length 22

# Inspect or verify without prompts.
mkbrr-wizard inspect /mnt/user/data/torrents/release.torrent --verbose
mkbrr-wizard check /mnt/user/data/torrents/release.torrent \
  /mnt/user/data/release --workers 2

# Validate readiness and export the manifest schema.
mkbrr-wizard doctor --json
mkbrr-wizard schema batch.schema.json
```

### Batch manifests

The `batch` and `plan` commands accept strict version-1 YAML or JSON. Paths in a
manifest must be absolute, output paths must be unique, and unknown fields are
rejected.

```yaml
version: 1
jobs:
  - path: /mnt/user/data/movies/release-one
    output: /mnt/user/data/torrents/release-one.torrent
  - path: /mnt/user/data/movies/release-two
    output: /mnt/user/data/torrents/release-two.torrent
    trackers:
      - https://tracker.example.com/announce
    source: EXAMPLE
    private: true
    exclude_patterns:
      - "*.nfo"
```

Preview the resolved paths, storage devices, effective options, estimates, and
warnings before execution:

```bash
mkbrr-wizard plan batch.yaml -P tracker --show-command
mkbrr-wizard batch batch.yaml -P tracker --dry-run --json
```

Run the manifest with an atomic report. Reusing that report with `--resume` skips
jobs already recorded as successful:

```bash
mkbrr-wizard batch batch.yaml -P tracker --report runs/batch.json
mkbrr-wizard batch batch.yaml -P tracker --report runs/batch.json --resume
```

### Output and automation options

- `--dry-run` builds and validates a plan without executing mkbrr. `plan` is always
  non-executing.
- `--json` emits machine-readable plans, results, or errors for headless commands.
- `--show-command` includes the raw command in the terminal plan preview.
- `--no-estimate` skips recursive file-count and size estimation.
- `--report FILE` writes batch progress atomically after completed jobs.
- `--resume` requires `--report` and skips successful operations in that report.

Headless exit codes are stable for automation:

| Code | Meaning |
| --- | --- |
| `0` | Every requested operation succeeded or was already complete |
| `1` | An mkbrr operation failed, or a mixed run timed out |
| `2` | Invalid input/setup, or a safety precondition blocked execution |
| `124` | The run timed out without any successful operation |
| `130` | Cancelled by the user |

JSON errors contain `ok`, `exit_code`, and `error`; completed JSON runs also
include the resolved plan, per-operation results, summary counts, and event log.

## Path handling and Unraid

Host and container roots are mappings from `config.yaml`, not fixed constants. For
example, the default sample maps `/mnt/user/data/releases/file.mkv` to
`/data/releases/file.mkv` in Docker. Torrent paths are similarly mapped through
the configured output roots.

When `unraid.enabled` is true, the planner can resolve `/mnt/user` shares to
physical `/mnt/diskN` or `/mnt/cache*` paths, detect split shares before hashing,
and schedule work by physical device. Review the documented options in
[`config.yaml.sample`](config.yaml.sample), especially `mount_priority` and the
`split_share_*` policies.

## mkbrr documentation

The complete upstream documentation is at [mkbrr.com](https://mkbrr.com). Offline
references are included in [`docs/`](docs/), covering installation, create,
check/inspect, presets, and batch mode. Refresh them with:

```bash
bash scripts/update-mkbrr-docs.sh
```

## Development

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy src tests
black --check src tests mkbrr-wizard.py
```

## License

MIT
