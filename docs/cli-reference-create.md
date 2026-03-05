<!-- Auto-generated from https://mkbrr.com/cli-reference/create — run scripts/update-mkbrr-docs.sh to refresh -->

# create

> Source: <https://mkbrr.com/cli-reference/create>

Create a new torrent file from a file or directory.

---

## Usage

```bash
mkbrr create /path/to/content [flags]
```

> Create torrents in two modes:
>
> - Single file/directory mode (default)
> - [Batch mode](batch-mode.md) using a YAML config file
>
> Common settings can be saved and reused with [presets](presets.md).
>
> By default, when a tracker URL is provided, the output filename will be prefixed with the tracker
> domain (e.g. `example_filename.torrent`). Use `--skip-prefix` to disable this behavior.

---

## Arguments

| Argument | Description |
| --- | --- |
| `/path/to/content` | (Required unless using `-b`) Path to the source file or directory |

---

## Flags

### Input & Output

| Flag | Type | Description |
| --- | --- | --- |
| `--tracker, -t` | string | Tracker URL. Required unless using a preset with trackers defined. |
| `--output, -o` | string | Optional. Set output path (default: `<n>.torrent` or `<tracker-prefix>_<n>.torrent`). |
| `--skip-prefix` | bool | Don't add tracker domain prefix to output filename. |

### Advanced Input (Batch & Presets)

| Flag | Type | Description |
| --- | --- | --- |
| `--batch, -b` | string | Batch config file (YAML). Cannot be used with a path argument. See [Batch Mode](batch-mode.md). |
| `--preset, -P` | string | Use preset from config. See [Presets](presets.md). |
| `--preset-file` | string | Preset config file (default `~/.config/mkbrr/presets.yaml`). |

### Content Selection

| Flag | Type | Description |
| --- | --- | --- |
| `--exclude` | array | Exclude files matching these patterns (e.g., `"*.nfo,*.jpg"` or `--exclude "*.nfo" --exclude "*.jpg"`). Patterns are additive with preset patterns. |
| `--include` | array | Include only files matching these patterns. Activates whitelist mode. Patterns are additive with preset patterns. |

### Torrent Internals

| Flag | Type | Default | Description |
| --- | --- | --- | --- |
| `--private, -p` | bool | `true` | Make torrent private. Enabled by default for tracker compliance. |
| `--piece-length, -l` | number | auto | Set piece length to 2^n bytes (16–27). Automatic if not specified. |
| `--max-piece-length, -m` | number | — | Limit maximum automatically calculated piece length to 2^n bytes (16–27). |
| `--entropy, -e` | bool | `false` | Randomize info hash by adding a unique random `entropy` key. Useful for cross-seeding. |

### Seeding & Metadata

| Flag | Type | Description |
| --- | --- | --- |
| `--web-seed, -w` | array | Specify web seed URLs. Can be used multiple times. |
| `--source, -s` | string | Specify the source string. Some trackers require specific source tags. |
| `--comment, -c` | string | Specify a comment. |
| `--no-date, -d` | bool | Omit the creation date from torrent metadata. |
| `--no-creator` | bool | Omit the creator string from torrent metadata. |

### Execution & Output Control

| Flag | Type | Default | Description |
| --- | --- | --- | --- |
| `--workers` | number | auto | Number of concurrent goroutines used for hashing. |
| `--verbose, -v` | bool | `false` | Be verbose. |
| `--quiet` | bool | `false` | Print only the final torrent file path upon success. Useful for scripts. |

---

## See Also

- [Presets](presets.md) — reuse common settings across torrents
- [Batch Mode](batch-mode.md) — create multiple torrents in one command
- [check](cli-reference-check-inspect.md) — verify content against a torrent
- [inspect](cli-reference-check-inspect.md) — view torrent metadata
