<!-- Auto-generated from https://mkbrr.com/cli-reference/check and https://mkbrr.com/cli-reference/inspect — run scripts/update-mkbrr-docs.sh to refresh -->

# check & inspect

> Sources: <https://mkbrr.com/cli-reference/check> · <https://mkbrr.com/cli-reference/inspect>

---

## check

Verify the integrity of content against a torrent file.

### Usage

```bash
mkbrr check <torrent-file> <content-path> [flags]
```

### Arguments

| Argument | Description |
| --- | --- |
| `<torrent-file>` | (Required) Path to the `.torrent` file |
| `<content-path>` | (Required) Path to the directory or file containing the data to check |

### Flags

| Flag | Type | Default | Description |
| --- | --- | --- | --- |
| `--verbose, -v` | bool | `false` | Be verbose. Shows detailed info including bad piece indices. |
| `--quiet` | bool | `false` | Print only the final torrent file path upon success. Useful for scripts. |
| `--workers` | number | auto | Number of concurrent goroutines used for hashing. |

### Examples

**Basic check** — verify `my_download_folder` against `my_torrent.torrent`:

```bash
mkbrr check my_torrent.torrent my_download_folder
```

**Verbose check** — also show indices of any bad pieces:

```bash
mkbrr check -v my_torrent.torrent /path/to/data
```

**Quiet check** — print only final status:

```bash
mkbrr check --quiet my_torrent.torrent /path/to/data
```

---

## inspect

Inspect a torrent file and display its metadata and file structure.

### Usage

```bash
mkbrr inspect <torrent-file> [flags]
```

### Arguments

| Argument | Description |
| --- | --- |
| `<torrent-file>` | (Required) Path to the `.torrent` file to inspect |

### Flags

| Flag | Type | Default | Description |
| --- | --- | --- | --- |
| `--verbose, -v` | bool | `false` | Show all metadata fields, including non-standard ones. |

---

## See Also

- [create](cli-reference-create.md) — create a new torrent
- [Presets](presets.md) — reuse common settings
- [Batch Mode](batch-mode.md) — create multiple torrents in one command
