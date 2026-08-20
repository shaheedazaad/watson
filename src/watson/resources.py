from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path
from typing import Iterator
from contextlib import contextmanager


@contextmanager
def default_deviation_guide() -> Iterator[Path]:
    resource = files("watson").joinpath("assets/watson-deviation-guide.yaml")
    if resource.is_file():
        with as_file(resource) as path:
            yield path
        return
    source_guide = Path(__file__).resolve().parents[2] / "watson-deviation-guide.yaml"
    if not source_guide.is_file():
        raise RuntimeError("The bundled deviation guide is missing.")
    yield source_guide


@contextmanager
def rebuild_reports_script() -> Iterator[Path]:
    resource = files("watson").joinpath("assets/rebuild_reports.py")
    if resource.is_file():
        with as_file(resource) as path:
            yield path
        return
    source_script = Path(__file__).resolve().parents[2] / "scripts" / "rebuild_reports.py"
    if not source_script.is_file():
        raise RuntimeError("The report rebuilding script is missing.")
    yield source_script


def templates_dir() -> Path:
    return Path(str(files("watson").joinpath("templates")))


def static_dir() -> Path:
    return Path(str(files("watson").joinpath("static")))


def assert_runtime_assets() -> list[str]:
    required = [
        "assets/watson-deviation-guide.yaml",
        "assets/rebuild_reports.py",
        "templates/base.html",
        "templates/home.html",
        "templates/project.html",
        "templates/report.html",
        "templates/settings.html",
        "templates/global_settings.html",
        "templates/_study_result.html",
        "static/basecoat.css",
        "static/theme.css",
        "static/theme-init.js",
        "static/watson.css",
        "static/settings.css",
        "static/watson.js",
    ]
    package = files("watson")
    missing = []
    for name in required:
        if package.joinpath(name).is_file():
            continue
        if name == "assets/watson-deviation-guide.yaml" and (
            Path(__file__).resolve().parents[2] / "watson-deviation-guide.yaml"
        ).is_file():
            continue
        if name == "assets/rebuild_reports.py" and (
            Path(__file__).resolve().parents[2] / "scripts" / "rebuild_reports.py"
        ).is_file():
            continue
        missing.append(name)
    if missing:
        raise RuntimeError("Missing Watson runtime assets: " + ", ".join(missing))
    return required
