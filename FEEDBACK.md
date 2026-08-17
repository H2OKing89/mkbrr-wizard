# Pre-Migration Architecture Assessment (Historical)

This review was written against the single-file `mkbrr-wizard.py` script, before
the `src/mkbrr_wizard/` package restructuring it recommends. It is kept for
historical context; see `.github/copilot-instructions.md` for the current
architecture.

The best direction is not a ground-up rewrite. Keep Python and Rich, but turn the project into an installable modular application centered on:

`typed request → validated plan → execution events → structured result`

The baseline is strong: 301 tests pass, Ruff and Mypy are clean, and the runtime/config abstractions have already begun. The limitation is that all production behavior still lives in one 3,501-line file.

## Highest-impact changes

| Priority | Change | Payoff |
|---|---|---|
| 1 | Create a real `src/mkbrr_wizard/` package and console entry point | Normal imports, reliable installation, smaller modules |
| 2 | Make Pydantic the single source for batch models and generated schema | Eliminates validation drift and silently ignored fields |
| 3 | Separate Rich prompts/rendering from planning and execution | Enables interactive, headless, JSON, and future API clients |
| 4 | Add subcommands, dry-run, JSON output, and stable exit codes | Makes the tool useful for scripts, cron, and integrations |
| 5 | Build a disk-aware resumable batch scheduler | Meaningful scalability on Unraid |
| 6 | Add effective-plan and live-queue views | Richer UX where it improves safety and visibility |

Packaging is currently the first blocker: the hyphenated module requires a dynamic test loader ([tests/conftest.py](/mnt/cache/scripts/mkbrr-wizard/tests/conftest.py:16)), there is no console entry point ([pyproject.toml](/mnt/cache/scripts/mkbrr-wizard/pyproject.toml:48)), and I confirmed a wheel build currently fails because setuptools discovers multiple top-level directories.

## Target structure

```text
mkbrr-wizard.py                 # temporary compatibility launcher
src/mkbrr_wizard/
├── cli.py
├── application.py
├── config.py
├── models.py
├── planning/
│   ├── commands.py
│   ├── paths.py
│   ├── unraid.py
│   └── series.py
├── execution/
│   ├── backends.py
│   ├── executor.py
│   └── scheduler.py
├── ui/
│   ├── prompts.py
│   └── rendering.py
└── notifications/
    ├── manager.py
    └── providers.py
```

Extract this incrementally and temporarily re-export existing names from the root script. That keeps the 301-test safety net intact.

## Richness that matters most

1. **Turn batch mode into a real queue.**

   - Import/export YAML or JSON.
   - Discover jobs from folders or globs.
   - Edit, reorder, skip, or retry jobs.
   - Persist a run report and resume failures.
   - Run separate disks concurrently while limiting each HDD to one hashing job.
   - Show per-job progress, duration, output, physical disk, and failure reason.

   Execution is currently strictly serial ([mkbrr-wizard.py](/mnt/cache/scripts/mkbrr-wizard/mkbrr-wizard.py:2852)).

2. **Add an effective-plan screen.**

   Show:

   - Entered, host, container, and physical-disk paths.
   - Preset plus explicit overrides.
   - Privacy, trackers, filters, piece strategy, workers.
   - Output collisions and split-share warnings.
   - Estimated file count and total size.

   Keep the raw command available as an expandable detail.

3. **Add a headless CLI.**

   ```text
   mkbrr-wizard create ...
   mkbrr-wizard batch manifest.yaml --dry-run
   mkbrr-wizard check ...
   mkbrr-wizard inspect ... --json
   mkbrr-wizard doctor
   mkbrr-wizard config validate
   ```

   Currently only config/runtime flags exist, after which the interactive menu is mandatory ([mkbrr-wizard.py](/mnt/cache/scripts/mkbrr-wizard/mkbrr-wizard.py:2396)). Handlers also do not propagate mkbrr failures into dependable process exit codes.

4. **Complete current mkbrr feature coverage.**

   Add quick/advanced create modes for `--name`, `--max-piece-length`, `--target-piece-count`, `--no-creator`, explicit output, filters, and related overrides. Add a safe `modify` workflow afterward. These capabilities are present in the current [create reference](https://mkbrr.com/cli-reference/create), [batch documentation](https://mkbrr.com/features/batch-mode), and [modify reference](https://mkbrr.com/cli-reference/modify). The existing v1.24.1 pin remains current according to the official [mkbrr changelog](https://mkbrr.com/changelog).

5. **Make inspect/check structured.**

   Capture output and progress rather than retaining only return code and duration ([mkbrr-wizard.py](/mnt/cache/scripts/mkbrr-wizard/mkbrr-wizard.py:686)). Then provide metadata summaries, searchable file trees, bad-piece reports, JSON export, and “verify newly created torrent” follow-ups.

## Immediate correctness and operational work

Before larger extraction:

- Replace the hand-maintained batch schema with strict Pydantic models. The runtime supports fields absent from [batch.json](/mnt/cache/scripts/mkbrr-wizard/schema/batch.json:18), unknown fields are accepted, and URI formats are not currently enforced.
- Add CI for Python 3.10–3.13, tests, formatting, Ruff, typing, coverage, and a packaging smoke test.
- Mount Docker source/config paths read-only; leave only torrent output writable.
- Escape and truncate Discord/Pushover content and retry rate-limit/server failures.
- Consolidate contradictory tool versions and repair README/docs drift.
- Move `.env` loading until after the selected config path is known.

The ideal first implementation slice is package scaffolding, console entry point, compatibility launcher, CI, and unified batch models—without changing runtime behavior. That creates the safe foundation for every scalability and UX improvement above. No repository files were changed during this assessment.
