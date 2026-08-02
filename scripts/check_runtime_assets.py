from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SOURCE_FILES = {
    "watson-deviation-guide.yaml",
    "scripts/rebuild_reports.py",
    "src/watson/templates/base.html",
    "src/watson/templates/home.html",
    "src/watson/templates/project.html",
    "src/watson/templates/report.html",
    "src/watson/templates/settings.html",
    "src/watson/templates/_study_result.html",
    "src/watson/static/watson.css",
    "src/watson/static/watson.js",
}


def main() -> None:
    missing = sorted(path for path in REQUIRED_SOURCE_FILES if not (ROOT / path).is_file())
    if missing:
        raise SystemExit("Missing runtime assets: " + ", ".join(missing))
    source_controlled = set(
        subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
    )
    unavailable = sorted(REQUIRED_SOURCE_FILES - source_controlled)
    if unavailable:
        raise SystemExit(
            "Runtime assets are ignored or unavailable to source control: " + ", ".join(unavailable)
        )
    print("All required runtime assets exist and are available to source control.")


if __name__ == "__main__":
    main()
