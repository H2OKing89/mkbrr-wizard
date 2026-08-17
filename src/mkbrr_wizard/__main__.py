"""Allow ``python -m mkbrr_wizard`` to behave like the console command."""

from __future__ import annotations

from mkbrr_wizard.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
