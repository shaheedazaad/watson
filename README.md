# Watson

> **[Read the full documentation →](https://shaheedazaad.github.io/watson/)** for a guided walkthrough with screenshots, aimed at researchers rather than developers.

Watson is a private, local browser app for organizing psychology research materials and reviewing preregistration adherence. Source documents and generated results stay on the computer. Watson sends document content to the configured Gemini model only when the user starts processing.

## Install

macOS or Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/shaheedazaad/watson/main/scripts/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/shaheedazaad/watson/main/scripts/install.ps1 | iex
```

The installer uses Pixi's locked, multi-platform environment. Re-run the same command to update to the latest release.

## Run

```sh
watson
```

Watson binds only to `127.0.0.1`, selects an available port, creates a new random access token, opens the default browser, and remains attached to the terminal. Press Ctrl+C to stop it. `watson web` is an explicit alias.

In the browser:

1. Create a named project.
2. Add the article, preregistration, and relevant supplemental files.
3. Open project settings to save the Gemini API key in the operating-system credential store and load it for the current Watson session.
4. Start processing and follow live progress.
5. Read the inventory and preregistration reports in the browser, expand individual studies to inspect every finding, or download reports and machine-readable data.

Projects live in the normal per-user application-data directory. The browser workflow never requires editing that directory. API keys are stored in macOS Keychain, Windows Credential Manager, or Linux Secret Service. Watson selects these native stores directly, so keyring configuration cannot redirect the key to a plaintext backend. Watson never reads the credential store during launch, page loads, updates, or processing. The user must explicitly save or load the key in project settings; after an update, the same explicit load action reconnects the saved key and makes any operating-system permission prompt expected. Once loaded, the key remains only in process memory until Watson closes. Credentials are never included in project files or exports.

Supported inputs are PDF, TXT, CSV, HTML/HTM, XML, JSON, and RTF. Each file is limited to 50 MB and each upload request to 200 MB.

## How the preregistration check works

For each study with a matched preregistration, Watson runs four separate model requests instead of one combined judgement:

1. **Inventory the preregistration.** Every commitment the researchers made about what they would do and how, with the concrete specification given for each and whether it is fully specified, partially specified, or unspecified.
2. **Inventory what was done.** Every action the article and its supplemental materials report for that study, with how the article frames it — confirmatory, exploratory, robustness, or unclear.
3. **Diff the two inventories.** Items promised but never reported, items reported but never preregistered, and items present in both that were executed differently from the plan.
4. **Audit the preregistration for degrees of freedom.** Commitments left open enough that more than one defensible result was available, whether or not the article exploits them.

The report carries all four as separate sections, plus both inventories as appendices. Each study's documents are uploaded once and held in a Gemini context cache shared by all four stages, so the article and preregistration are not re-sent per request; when the API declines to cache them, Watson attaches the documents to each stage instead.

## Reproducible command-line runs

The noninteractive command calls the same runner as the browser app:

```sh
watson projects list
GEMINI_API_KEY=... watson run PROJECT_ID --action all
# Or explicitly permit this command to read the saved system credential:
watson run PROJECT_ID --action all --use-keychain
```

By default, a rerun processes only missing or failed work. Use `--retry-all` for a complete rerun. Each successful run writes `reproducibility.json` with the effective model, thinking level, seed, concurrency, platform, and relevant package versions. Result archives include source documents, machine-readable results, reports, and this metadata; credentials and Gemini upload-cache identifiers are excluded.

For an explicit directory import:

```sh
watson projects create "Replication 2" --import-dir "/path/with spaces/materials"
```

## Development

With Pixi:

```sh
pixi install
pixi run test
pixi run watson
```

Python-only editable setup:

```sh
python -m pip install -e ".[dev]"
pytest
watson
```

Validate bundled runtime assets with `pixi run verify-assets`. The human-editable default deviation guide is `watson-deviation-guide.yaml`; validate it with `watson deviation-guide validate`.

## Publishing a release

There is no manual build or archive step. Update the matching version in `pyproject.toml` and `pixi.toml`, refresh `pixi.lock` if needed, commit the changes on `main`, and run:

```sh
scripts/release.sh
```

The script reads the version from the manifests, refuses to release a dirty worktree or a version mismatch, runs the locked test suite, pushes `main`, and pushes the version tag. The tag starts the GitHub Actions release workflow, which tests the tagged source again, creates both installer archives, and publishes them to the GitHub release. The existing install commands automatically download the newest release.
