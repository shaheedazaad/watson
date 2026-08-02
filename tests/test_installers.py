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


def test_installers_add_the_watson_launcher_directory_to_user_path() -> None:
    shell = Path("scripts/install.sh").read_text(encoding="utf-8")
    powershell = Path("scripts/install.ps1").read_text(encoding="utf-8")

    assert 'PATH_LINE=\'export PATH="$HOME/.local/bin:$PATH"\'' in shell
    assert 'grep -Fqx "$PATH_LINE" "$SHELL_PROFILE"' in shell
    assert '[Environment]::SetEnvironmentVariable("Path", $NewUserPath, "User")' in powershell


def test_launchers_can_find_pixi_before_a_new_shell_is_started() -> None:
    shell_launcher = Path("scripts/watson-launcher").read_text(encoding="utf-8")
    powershell = Path("scripts/install.ps1").read_text(encoding="utf-8")

    assert '$HOME/.pixi/bin/pixi' in shell_launcher
    assert "%USERPROFILE%\\.pixi\\bin\\pixi.exe" in powershell
