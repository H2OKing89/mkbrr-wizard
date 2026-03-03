#!/usr/bin/env bash
# update-mkbrr-docs.sh — Fetch upstream mkbrr docs and write cleaned Markdown to docs/
#
# Usage:
#   bash scripts/update-mkbrr-docs.sh
#
# Requirements: curl, python3 (standard library only)
#
# The mkbrr docs site (mkbrr.com) serves each page as MDX (Mintlify) when the
# .md suffix is appended to any URL. This script fetches those pages, strips
# MDX/JSX component tags while preserving prose, code fences, and headings,
# then writes the result to the matching file under docs/.
#
# Each output file gets an auto-generated header that records the source URL and
# instructs readers to re-run this script to refresh the content.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="$REPO_ROOT/docs"

# ---------------------------------------------------------------------------
# Page map: output filename -> source .md URL
# ---------------------------------------------------------------------------
declare -A PAGES=(
  ["installation.md"]="https://mkbrr.com/installation.md"
  ["cli-reference-create.md"]="https://mkbrr.com/cli-reference/create.md"
  ["cli-reference-check-inspect.md"]="https://mkbrr.com/cli-reference/check.md https://mkbrr.com/cli-reference/inspect.md"
  ["presets.md"]="https://mkbrr.com/features/presets.md"
  ["batch-mode.md"]="https://mkbrr.com/features/batch-mode.md"
)

# ---------------------------------------------------------------------------
# MDX → Markdown cleaner (Python, stdlib only)
# Removes JSX/MDX component tags while keeping prose, headings, and code blocks
# ---------------------------------------------------------------------------
read -r -d '' MDX_CLEANER << 'PYEOF' || true
import sys, re

text = sys.stdin.read()

# ---- Pass 1: protect fenced code blocks (don't touch their content) --------
CODE_PLACEHOLDER = "\x00CODE{%d}\x00"
code_blocks: list[str] = []

def save_code(m: re.Match) -> str:
    idx = len(code_blocks)
    code_blocks.append(m.group(0))
    return CODE_PLACEHOLDER % idx

text = re.sub(r'```[\s\S]*?```', save_code, text)

# ---- Pass 2: remove JSX/MDX component tags ---------------------------------
# Self-closing tags:  <Foo />  <Foo attr="x" />
text = re.sub(r'<[A-Z][A-Za-z0-9]*(?:\s[^>]*)?\s*/>', '', text)
# Opening tags with attributes (possibly multiline):  <Foo attr="x">
text = re.sub(r'<[A-Z][A-Za-z0-9]*(?:\s[^>]*)?>',  '', text, flags=re.S)
# Closing tags:  </Foo>
text = re.sub(r'</[A-Z][A-Za-z0-9]*>', '', text)
# <Terminal text="..." />  style (already caught above, but belt-and-suspenders)
text = re.sub(r'<Terminal\s+text="([^"]+)"\s*/>', r'```\n\1\n```', text)

# ---- Pass 3: strip mdx import/export lines ---------------------------------
text = re.sub(r'^(?:import|export)\s+.*$', '', text, flags=re.M)

# ---- Pass 4: collapse excess blank lines (≥3 → 2) -------------------------
text = re.sub(r'\n{3,}', '\n\n', text)

# ---- Pass 5: restore code blocks -------------------------------------------
for idx, block in enumerate(code_blocks):
    text = text.replace(CODE_PLACEHOLDER % idx, block)

print(text.strip())
PYEOF

# ---------------------------------------------------------------------------
# Helper: strip the "Documentation Index" preamble injected by the doc site
# ---------------------------------------------------------------------------
strip_preamble() {
  # The preamble is everything before the first `# ` heading
  python3 - <<'PYEOF'
import sys, re
text = sys.stdin.read()
# Drop the inline "## Documentation Index" block the site prepends
text = re.sub(r'^.*?(?=^# )', '', text, count=1, flags=re.S | re.M)
print(text.strip())
PYEOF
}

# ---------------------------------------------------------------------------
# Fetch and clean a single URL, returns cleaned markdown on stdout
# ---------------------------------------------------------------------------
fetch_and_clean() {
  local url="$1"
  curl -fsSL "$url" | python3 -c "$MDX_CLEANER" | strip_preamble
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
mkdir -p "$DOCS_DIR"

for filename in "${!PAGES[@]}"; do
  urls="${PAGES[$filename]}"
  outfile="$DOCS_DIR/$filename"

  echo "Updating $filename ..."

  # Determine the primary URL for the header comment
  primary_url=$(echo "$urls" | awk '{print $1}' | sed 's/\.md$//')

  # For files built from multiple URLs, concatenate with a separator
  combined=""
  for url in $urls; do
    content=$(fetch_and_clean "$url")
    if [[ -n "$combined" ]]; then
      combined+=$'\n\n---\n\n'"$content"
    else
      combined="$content"
    fi
  done

  # Build final file: auto-generated header + cleaned content
  {
    echo "<!-- Auto-generated from ${primary_url} — run scripts/update-mkbrr-docs.sh to refresh -->"
    echo ""
    echo "$combined"
  } > "$outfile"

  echo "  -> $outfile"
done

echo ""
echo "Done. All docs updated under $DOCS_DIR"
