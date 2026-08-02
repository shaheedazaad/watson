from __future__ import annotations

from pathlib import Path


def test_installers_download_latest_release_assets_without_pinned_version() -> None:
    shell = Path("scripts/install.sh").read_text(encoding="utf-8")
    powershell = Path("scripts/install.ps1").read_text(encoding="utf-8")

    assert "releases/latest/download/watson-source.tar.gz" in shell
    assert "releases/latest/download/watson-source.zip" in powershell
    assert "archive/refs/tags/" not in shell
    assert "archive/refs/tags/" not in powershell
    assert "WATSON_RELEASE_URL" in shell
    assert "WATSON_RELEASE_URL" in powershell
