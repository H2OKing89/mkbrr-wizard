<!-- Auto-generated from https://mkbrr.com/features/presets — run scripts/update-mkbrr-docs.sh to refresh -->

# Presets

> Source: <https://mkbrr.com/features/presets>

Presets allow you to define and reuse common sets of options for creating or modifying torrents. This is particularly useful if you frequently create torrents for specific trackers with consistent settings.

---

## Configuration File (`presets.yaml`)

Presets are defined in a YAML file named `presets.yaml`. mkbrr searches for this file in the following locations, using the first one it finds:

1. Path specified by the `--preset-file` flag (if used)
2. The current working directory (`./presets.yaml`)
3. `~/.config/mkbrr/presets.yaml` (or equivalent user config directory)
4. `~/.mkbrr/presets.yaml` (legacy location)

### Structure

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/autobrr/mkbrr/main/schema/presets.json
version: 1

# Optional: Default settings applied to ALL presets unless overridden
default:
  private: true
  no_date: true
  no_creator: false
  skip_prefix: false
  # comment: "Default comment"
  # source: "DEFAULT_SRC"
  # exclude_patterns: ["*.bak", "temp.*"]
  # include_patterns: ["*.mkv", "*.mp4"]

presets:
  preset-name-1:                           # Name used with -P flag
    trackers:
      - "https://tracker-one.com/announce/..."
    source: "TRACKER1"
    comment: "Uploaded via mkbrr"
    private: true
    exclude_patterns:
      - "*.nfo"
      - "*sample*"

  preset-name-2:
    trackers:
      - "udp://tracker-two.org:6969/announce"
    private: false                         # Override default
    piece_length: 18                       # 256 KiB pieces
    include_patterns:
      - "*.mkv"
      - "*.mp4"
      - "*.avi"
```

**Minimal example:**

```yaml
version: 1
presets:
  ptp:
    trackers:
      - "https://tracker.example.com/announce"
    private: false
    source: "EXAMPLE"
```

---

## Available Options

The following options can be used in both the `default` section and in specific presets:

### Core Options

| Option | Type | Description |
|---|---|---|
| `trackers` | string[] | List of announce URLs. The first is used as the primary tracker. |
| `private` | boolean | Whether the torrent should be private. |
| `source` | string | Source tag string. |
| `comment` | string | Torrent comment string. |

### Piece Settings

| Option | Type | Description |
|---|---|---|
| `piece_length` | integer | Piece size exponent (e.g., `18` for 256 KiB). |
| `max_piece_length` | integer | Maximum piece size exponent for automatic calculation. |

### File Control

| Option | Type | Description |
|---|---|---|
| `exclude_patterns` | string[] | List of glob patterns to exclude files. |
| `include_patterns` | string[] | List of glob patterns to include files (takes precedence over exclude). |
| `skip_prefix` | boolean | Whether to prevent adding the tracker domain prefix to the output filename. |

### Advanced

| Option | Type | Default | Description |
|---|---|---|---|
| `webseeds` | string[] | — | List of web seed URLs. |
| `workers` | number | auto | Number of concurrent goroutines used for hashing. |
| `entropy` | boolean | `false` | Randomize info hash with a unique random key. Useful for cross-seeding identical content on trackers that reject duplicate info hashes. |
| `no_date` | boolean | — | Whether to omit creation date. |
| `no_creator` | boolean | — | Whether to omit creator string. |

> JSON schema for validation: <https://raw.githubusercontent.com/autobrr/mkbrr/main/schema/presets.json>

---

## Using Presets

Specify the preset name using the `-P` (or `--preset`) flag:

```bash
# Create
mkbrr create /path/to/content -P preset-name-1

# Modify
mkbrr modify *.torrent -P preset-name-2
```

---

## Overriding Presets

Command-line flags take precedence over preset settings, with one exception:

> **Filtering patterns are additive** — command-line patterns combine with preset patterns.

**Example:**

Preset definition:

```yaml
presets:
  movies:
    include_patterns:
      - "*.mkv"
      - "*.mp4"
    exclude_patterns:
      - "*sample*"
```

Command with additional filters:

```bash
mkbrr create /path/to/content -P movies --include "*.srt" --exclude "*.nfo"

# Effective filters applied:
# include_patterns: ["*.mkv", "*.mp4", "*.srt"]
# exclude_patterns: ["*sample*", "*.nfo"]
```

Standard flags (like `--private` or `--source`) completely override their preset values, while filtering patterns combine additively.

```bash
# Override source flag
mkbrr create /path/to/content -P preset-name-1 -s "CUSTOM_SRC"

# Override private flag
mkbrr modify existing.torrent -P preset-name-2 --private=true
```

---

## See Also

- [create](cli-reference-create.md) — full create flag reference
- [Batch Mode](batch-mode.md) — create multiple torrents in one command
