from __future__ import annotations

import subprocess
from pathlib import Path


def test_release_workflow_builds_the_assets_expected_by_installers() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert '"v*.*.*"' in workflow
    assert "contents: write" in workflow
    assert "actions/checkout@v6" in workflow
    assert "prefix-dev/setup-pixi@v0.10.0" in workflow
    assert "locked: true" in workflow
    assert "pixi run test" in workflow
    assert "pixi run verify-assets" in workflow
    assert "dist/watson-source.tar.gz" in workflow
    assert "dist/watson-source.zip" in workflow
    assert "gh release create" in workflow


def test_release_script_is_valid_and_guarded() -> None:
    script_path = Path("scripts/release.sh")
    script = script_path.read_text(encoding="utf-8")

    subprocess.run(["sh", "-n", str(script_path)], check=True)
    assert "git status --porcelain" in script
    assert 'CURRENT_BRANCH" = "main' in script
    assert "pyproject.toml" in script
    assert "pixi.toml" in script
    assert 'RELEASE_TAG="v$PACKAGE_VERSION"' in script
    assert "pixi run test" in script
    assert "git push origin main" in script
    assert 'git push origin "$RELEASE_TAG"' in script
