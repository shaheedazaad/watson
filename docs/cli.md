# Command line (advanced)

Most people only need the browser app — this page is for anyone comfortable with a terminal who wants to script runs, batch-process several projects, or reproduce a result exactly.

!!! note "You don't need this"
    Everything on this page is optional. If the browser workflow in [Getting started](quickstart.md) already covers what you need, there's nothing else to learn here.

## Running from the terminal

The command-line runner calls the exact same code as the browser app — it's not a separate, less-tested path.

```sh
watson projects list
GEMINI_API_KEY=... watson run PROJECT_ID --action all
```

Or, to use the key you already saved in the app rather than an environment variable:

```sh
watson run PROJECT_ID --action all
```

By default, a rerun only processes work that's missing or previously failed. Add `--retry-all` to force a complete rerun of everything.

## Code audit

After completing the inventory and preregistration check and adding source files in the browser, run the optional source review with:

```sh
GEMINI_API_KEY=... watson run PROJECT_ID --action code_audit
```

The audit checks reported analyses against both the manuscript and preregistration. It never executes uploaded code or reads raw data. See [Code audit](code-audit.md) for the scope and interpretation of its results.

## Importing files without the browser

```sh
watson projects create "Replication 2" --import-dir "/path/with spaces/materials"
```

## Reproducibility

Every successful run writes a `reproducibility.json` file recording the model used, the thinking level, the seed, concurrency settings, the platform, and the versions of relevant packages. This file is included in exported result archives. It never contains your API key or any upload-cache identifiers.

To rebuild the human-readable Markdown reports from an exported archive (for example, after downloading it on a different machine), run:

```sh
python scripts/rebuild_reports.py /path/to/unzipped-export
```
