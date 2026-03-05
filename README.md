# mkbrr-wizard

An interactive command-line wizard for working with [mkbrr](https://mkbrr.com) via Docker. This tool simplifies the process of creating, inspecting, and verifying torrent files on Unraid systems.

## Features

- **Create torrents** from local files/folders using configurable presets
- **Batch create torrents** with an interactive job builder (runs per-job `mkbrr create`)
- **Inspect torrents** to view metadata and file structure
- **Check/verify** local data against existing `.torrent` files
- Automatic path translation between host and container paths
- Preset management via `presets.yaml`
- Automatic permission fixing for created `.torrent` files (Unraid's `nobody:users`)

## Requirements

- Python 3.10+
- Docker
- [mkbrr Docker image](https://mkbrr.com/installation#docker) (`ghcr.io/autobrr/mkbrr`)
- Python runtime dependencies: see `requirements.txt` (`pip install -r requirements.txt`)
  - PyYAML (required)
  - rich (UI)
  - prompt_toolkit (optional, enhanced prompts)

## mkbrr Documentation

Full mkbrr docs are available at **<https://mkbrr.com>**. Local offline copies (fetched from the upstream site) are also included in this repository under [`docs/`](docs/):

| File | Description | Online |
| ---- | ----------- | ------ |
| [`docs/installation.md`](docs/installation.md) | Installing mkbrr (binaries, Docker, package managers) | [mkbrr.com/installation](https://mkbrr.com/installation) |
| [`docs/cli-reference-create.md`](docs/cli-reference-create.md) | `mkbrr create` — all flags and options | [mkbrr.com/cli-reference/create](https://mkbrr.com/cli-reference/create) |
| [`docs/cli-reference-check-inspect.md`](docs/cli-reference-check-inspect.md) | `mkbrr check` and `mkbrr inspect` | [mkbrr.com/cli-reference/check](https://mkbrr.com/cli-reference/check) · [inspect](https://mkbrr.com/cli-reference/inspect) |
| [`docs/presets.md`](docs/presets.md) | Presets (`presets.yaml`) — structure, options, overrides | [mkbrr.com/features/presets](https://mkbrr.com/features/presets) |
| [`docs/batch-mode.md`](docs/batch-mode.md) | Batch mode (`batch.yaml`) — structure, options, examples | [mkbrr.com/features/batch-mode](https://mkbrr.com/features/batch-mode) |

To refresh the local copies from upstream:

```bash
bash scripts/update-mkbrr-docs.sh
```

## Installation

1. Clone or download this repository:

   ```bash
   git clone https://github.com/H2OKing89/mkbrr-wizard /mnt/cache/scripts/mkbrr-wizard
   ```

2. Install the required Python dependencies (recommended):

   ```bash
   pip install -r requirements.txt
   ```

   Optional extras:

   - UI enhancements: `prompt_toolkit` — install with:

     ```bash
     pip install prompt_toolkit
     ```

     or if you prefer editable install with extras:

     ```bash
     pip install -e .[ui]
     ```

   - Developer tools (for testing/linting/formatting):

     ```bash
     pip install -e .[dev]
     ```

     or via requirements file:

     ```bash
     pip install -r requirements-dev.txt
     ```

3. Make the script executable:

   ```bash
   chmod +x /mnt/cache/scripts/mkbrr-wizard/mkbrr-wizard.py
   ```

4. (Optional) Create a symbolic link for easier access:

   ```bash
   ln -s /mnt/cache/scripts/mkbrr-wizard/mkbrr-wizard.py /usr/local/bin/mkbrr-wizard
   ```

## Configuration

The wizard uses hardcoded paths that are designed for Unraid systems. You may need to adjust these in the script:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `HOST_DATA_ROOT` | `/mnt/user/data` | Host path for data files |
| `CONTAINER_DATA_ROOT` | `/data` | Container mount point for data |
| `HOST_OUTPUT_DIR` | `/mnt/user/data/downloads/torrents/torrentfiles` | Where `.torrent` files are saved |
| `HOST_CONFIG_DIR` | `/mnt/cache/appdata/mkbrr` | mkbrr config directory (contains `presets.yaml`) |
| `TARGET_UID` / `TARGET_GID` | `99` / `100` | Ownership for created `.torrent` files |

### Presets

Create a `presets.yaml` file in the config directory (`/mnt/cache/appdata/mkbrr/presets.yaml`):

```yaml
presets:
  btn:
    announce: https://tracker.example.com/announce
    source: BTN
    private: true
  mam:
    announce: https://tracker2.example.com/announce
    source: MAM
    private: true
```

### Batch Mode

Configure batch prompt style in `config.yaml`:

```yaml
batch:
  mode: simple
```

- `simple` (default): asks only preset, job count, content path, and output path.
- `advanced`: also asks per-job optional fields (`trackers`, `private`, `piece_length`, etc.).

### Unraid Options

Configure Unraid-specific path and split-share behavior in `config.yaml`:

```yaml
unraid:
  enabled: true
  fuse_root: /mnt/user
  mount_priority: disk_first
  split_share_preflight: fail
  split_share_unmapped_docker_path: warn
  split_share_max_entries: 20000
  split_share_follow_symlinks: false
```

- `mount_priority`
  - `disk_first` (default): prefers `/mnt/diskN` if both disk and cache have the same path.
  - `cache_first`: prefers `/mnt/cache*` first.
- `split_share_preflight`
  - `fail`: aborts before mkbrr when split-share mismatch is detected.
  - `warn`: prints warning and continues.
  - `off`: disables split-share preflight.
- `split_share_unmapped_docker_path`
  - Controls behavior when docker job/content paths are outside `container_data_root` and cannot be safely preflight-checked.
  - Values: `off`, `warn` (default), `fail`.

## Usage

Run the wizard:

```bash
./mkbrr-wizard.py
```

Or if you created a symlink:

```bash
mkbrr-wizard
```

### Main Menu

```text
🧰 What do you want to do?
  [1] Create a torrent from a file/folder   (mkbrr create)
  [2] Inspect an existing .torrent file     (mkbrr inspect)
  [3] Check data against a .torrent file    (mkbrr check)
  [4] Batch create torrents                 (mkbrr create per-job)
  [q] Quit
```

### Creating a Torrent

1. Select option `1` (or press Enter for default)
2. Choose a preset from the list (loaded from `presets.yaml`)
3. Enter the path to the file or folder
4. Confirm the command to run
5. The wizard will automatically fix permissions on created `.torrent` files

### Inspecting a Torrent

1. Select option `2`
2. Enter the path to the `.torrent` file
3. Optionally enable verbose mode for detailed metadata
4. Confirm to run

### Checking/Verifying Data

1. Select option `3`
2. Enter the path to the `.torrent` file
3. Enter the path to the local content to verify
4. Configure options:
   - **Verbose**: Show detailed verification info
   - **Quiet**: Only show final status/percentage
   - **Workers**: Number of parallel workers (leave empty for automatic)
5. Confirm to run

### Batch Creating Torrents

1. Select option `4`
2. Choose a required preset (used for the entire batch run)
3. Enter how many jobs to build
4. For each job, provide:
   - source content path (`path`)
   - output `.torrent` path (`output`) (press Enter to use `host_output_dir/<content-name>.torrent`)
5. In `batch.mode: advanced`, the wizard additionally prompts for optional per-job metadata.
6. The wizard auto-maps paths for the active runtime (native/docker)
7. The generated batch payload is validated against the bundled schema before execution
8. The wizard executes each job as its own `mkbrr create` command, continuing through failures
9. A final results table is printed with per-job exit codes; permission fix runs once at end if any job succeeded

Batch mode in this wizard is interactive-builder only; importing an existing batch file is not included.

## Path Handling

The wizard automatically translates between host paths and container paths:

| Host Path | Container Path |
| ----------- | ---------------- |
| `/mnt/user/data/downloads/file.mkv` | `/data/downloads/file.mkv` |
| `/mnt/user/data/downloads/torrents/torrentfiles/example.torrent` | `/torrentfiles/example.torrent` |

You can enter either format — the wizard will convert as needed.

## Example Session

```text
==========================================
  🧙 mkbrr Helper – Torrent Creator Wizard
==========================================

🧰 What do you want to do?
  [1] Create a torrent from a file/folder   (mkbrr create)
  [2] Inspect an existing .torrent file     (mkbrr inspect)
  [3] Check data against a .torrent file    (mkbrr check)
  [q] Quit

Choose an option [1/2/3/q]: 1

🎛  Preset selection (-P) (from /mnt/cache/appdata/mkbrr/presets.yaml):
  [1] btn
  [2] mam

Choose preset [1-2 or name]: 1

🎚  Selected preset: btn

📂 Enter the path to the file or folder:
   - You can paste a *host* path (e.g. /mnt/user/data/...)
   - Or a *container* path (e.g. /data/downloads/...)

Path: /mnt/user/data/downloads/my-release
✅ Host path exists: /mnt/user/data/downloads/my-release
🧩 Using container path inside mkbrr: /data/downloads/my-release

🚀 About to run:
   docker run --rm -it -w /root/.config/mkbrr -v /mnt/user/data:/data -v /mnt/user/data/downloads/torrents/torrentfiles:/torrentfiles -v /mnt/cache/appdata/mkbrr:/root/.config/mkbrr ghcr.io/autobrr/mkbrr mkbrr create /data/downloads/my-release -P btn --output-dir /torrentfiles

Proceed? [Y/n]: y

🛠  Running mkbrr create... (Ctrl+C to abort)
...
✅ mkbrr create finished.
🔐 Fixing ownership of .torrent files under /mnt/user/data/downloads/torrents/torrentfiles ...
  🔧 chown 99:100 -> /mnt/user/data/downloads/torrents/torrentfiles/my-release.torrent

🔄 Do another operation? [y/N]: n
👋 Bye.
```

## License

MIT
