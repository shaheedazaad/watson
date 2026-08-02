"""Rebuild Watson Markdown reports from an exported result archive.

Usage: python rebuild_reports.py /path/to/unzipped-export
Run inside Watson's locked Pixi environment so the renderer version matches.
"""

from __future__ import annotations

import sys
from pathlib import Path

from watson.deviation_check import render_deviation_markdown
from watson.report import render_inventory_report
from watson.schemas import DeviationCheckRun, InventoryResult


def main(export_dir: Path) -> None:
    data_dir = export_dir / "data"
    reports_dir = export_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    inventory_path = data_dir / "inventory.json"
    deviation_path = data_dir / "deviation-checks.json"
    if inventory_path.exists():
        inventory = InventoryResult.model_validate_json(inventory_path.read_text(encoding="utf-8"))
        (reports_dir / "watson-inventory-report.md").write_text(
            render_inventory_report(inventory), encoding="utf-8"
        )
    if deviation_path.exists():
        run = DeviationCheckRun.model_validate_json(deviation_path.read_text(encoding="utf-8"))
        (reports_dir / "watson-prereg-adherence-report.md").write_text(
            render_deviation_markdown(run), encoding="utf-8"
        )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python rebuild_reports.py /path/to/unzipped-export")
    main(Path(sys.argv[1]).expanduser().resolve())
