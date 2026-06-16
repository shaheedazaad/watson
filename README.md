# Watson

Watson is a Textual terminal app for inventorying a directory of psychology
research materials and checking preregistration adherence.

The app helps you choose the research folder, previews the files Watson will
inspect, and then lets you run inventory or preregistration adherence checks. A
Settings screen lets you add, replace, or delete your Gemini API key and change
the default model.

Leave `Overwrite previous results` unchecked to preserve existing completed
results where possible. Check it before running inventory or deviation checking
to overwrite saved Watson outputs for that step.

## Install for development

```bash
python -m pip install -e ".[dev]"
```

## Run The App

```bash
watson
```

or

```bash
/Users/shaheedazaad/Projects/watson/.venv311/bin/watson
```

You can also launch the app explicitly:

```bash
watson tui
```

On first run, Watson prompts for a Gemini API key and saves it for later use.
The preferred storage is the system keyring; if that is unavailable, Watson
falls back to `.watson/config.json`.

Generated files:

- `.watson/inventory.json`
- `.watson/study-map.json`
- `.watson/gemini-files.json`
- `watson-inventory-report.md`
- `.watson/deviation-checks.json`
- `.watson/deviation-results/*.json`
- `watson-prereg-adherence-report.md`

Global deviation guidance lives in `watson-deviation-guide.yaml`. Edit this file
to define the system instruction, the output fields Watson should collect for
each deviation, the allowed deviation types, and examples of each type.

Validate the guide with:

```bash
watson deviation-guide validate
```

You can also run inventory directly:

```bash
watson init --root /path/to/research-folder
```

After inventory, run preregistration adherence checks:

```bash
watson check --root /path/to/research-folder
```

This reads `.watson/study-map.json`, checks each ready preregistered study
against its matched preregistration file, saves each study result as JSON, and
then writes the combined JSON and Markdown reports. Reruns are resumable:
completed study JSON files are skipped unless you pass `--force`.
