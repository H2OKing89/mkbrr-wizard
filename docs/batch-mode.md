<!-- Auto-generated from https://mkbrr.com/features/batch-mode — run scripts/update-mkbrr-docs.sh to refresh -->

# Batch Mode

> Source: <https://mkbrr.com/features/batch-mode>

Batch mode allows you to create multiple torrents from different source paths with varying settings in a single command execution.

---

## Usage

```bash
mkbrr create -b /path/to/your/batch.yaml
```

> You cannot provide a source path argument directly to `mkbrr create` when using the `-b` flag. All source paths must be defined within the batch file jobs.

---

## Configuration File (`batch.yaml`)

Batch operations are defined in a YAML file, typically named `batch.yaml` (any filename works — specify it with `-b`).

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/autobrr/mkbrr/main/schema/batch.json
version: 1
jobs:
  - output: /torrents/movie1.torrent
    path: /media/movies/movie1.mkv
    trackers:
      - https://tracker-one.com/announce/abc
    private: true
    comment: "4K HDR Release"
    source: "UHD"

  - output: /torrents/movie2.torrent
    path: /media/movies/movie2.mkv
    trackers:
      - https://tracker-one.com/announce/abc
    private: true
    comment: "1080p Release"
    source: "BluRay"
```

---

## Configuration Options

### Required Fields

| Field | Description |
| --- | --- |
| `version` | Must be `1` |
| `jobs` | List of torrent creation tasks |
| `output` | Path for the output `.torrent` file |
| `path` | Source file or directory path |

### Optional Per-Job Settings

| Field | Type | Description |
| --- | --- | --- |
| `trackers` | string[] | Announce URLs list |
| `webseeds` | string[] | Web seed URLs list |
| `private` | boolean | Set to `true` or `false` |
| `piece_length` | integer | Piece size exponent (e.g., `18` for 256 KiB) |
| `comment` | string | Torrent comment string |
| `source` | string | Source tag string |
| `no_date` | boolean | Set `true` to omit creation date |
| `exclude_patterns` | string[] | Glob patterns to exclude files |
| `include_patterns` | string[] | Glob patterns to include files |

> JSON schema for validation: <https://raw.githubusercontent.com/autobrr/mkbrr/main/schema/batch.json>

---

## Examples

### Movies

```yaml
version: 1
jobs:
  - output: /torrents/movie1.torrent
    path: /media/movies/movie1.mkv
    trackers:
      - https://tracker-one.com/announce/abc
    private: true
    comment: "4K HDR Release"
    source: "UHD"

  - output: /torrents/movie2.torrent
    path: /media/movies/movie2.mkv
    trackers:
      - https://tracker-one.com/announce/abc
    private: true
    comment: "1080p Release"
    source: "BluRay"

  - output: /torrents/movie3.torrent
    path: /media/movies/movie3.mkv
    trackers:
      - https://tracker-one.com/announce/abc
    private: true
    comment: "720p Release"
    source: "WEB"
```

### TV Shows

```yaml
version: 1
jobs:
  - output: /torrents/show_s01.torrent
    path: /media/tv/Show/Season 1/
    trackers:
      - udp://tracker-two.org:6969/announce
    private: true
    piece_length: 18
    comment: "Complete Season 1"
    exclude_patterns:
      - "*.nfo"
      - "*.jpg"

  - output: /torrents/show_s02.torrent
    path: /media/tv/Show/Season 2/
    trackers:
      - udp://tracker-two.org:6969/announce
    private: true
    piece_length: 18
    comment: "Complete Season 2"
    exclude_patterns:
      - "*.nfo"
      - "*.jpg"
```

### Mixed Content

```yaml
version: 1
jobs:
  - output: /torrents/album.torrent
    path: /media/music/Artist/Album/
    trackers:
      - udp://tracker-three.org:6969/announce
    private: false
    piece_length: 18
    webseeds:
      - http://seed.example.com/album/
    exclude_patterns:
      - "*.log"
      - "folder.jpg"

  - output: /torrents/game.torrent
    path: /media/games/Game.iso
    trackers:
      - https://tracker-four.com/announce
    private: true
    piece_length: 22   # 4 MiB pieces
    source: "RETAIL"

  - output: /torrents/ebook.torrent
    path: /media/books/Book.pdf
    trackers:
      - https://tracker-five.com/announce
    private: true
    comment: "PDF + Extras"
```

---

## See Also

- [create](cli-reference-create.md) — full create flag reference
- [Presets](presets.md) — reuse common settings across torrents
