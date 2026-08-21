from __future__ import annotations

import json
import os
import re
import shutil
import sys
import unicodedata
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict, Field, field_validator

from watson.file_support import supported_extensions
from watson.schemas import CodeFileRecord


PROJECTS_DIRNAME = "projects"
PROJECT_METADATA_FILENAME = "project.json"
PROJECT_SETTINGS_FILENAME = "settings.json"
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_REQUEST_BYTES = 200 * 1024 * 1024
MAX_PROJECT_NAME_LENGTH = 100
MAX_FILENAME_LENGTH = 180
CODE_EXTENSIONS = {".py", ".r", ".rmd", ".js", ".ts", ".sh", ".bash", ".sql", ".stan", ".do", ".m", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".json", ".txt", ".md"}


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def get_app_data_dir() -> Path:
    override = os.environ.get("WATSON_DATA_DIR") or os.environ.get("WATSON_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "watson").resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return (base / "Watson").resolve()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (base.expanduser() / "watson").resolve()


class ProjectSettings(BaseModel):
    # "ignore" tolerates older settings.json files that still carry the
    # model/thinking_level fields now stored globally in ConfigStore.
    model_config = ConfigDict(extra="ignore")

    file_context: str = ""

    @field_validator("file_context")
    @classmethod
    def validate_file_context(cls, value: str) -> str:
        if len(value) > 20_000:
            raise ValueError("Project context cannot exceed 20,000 characters.")
        return value.strip()


class ProjectMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    created_at: datetime
    updated_at: datetime


class InputRecord(BaseModel):
    name: str
    size_bytes: int
    modified_at: datetime


class ProjectSummary(BaseModel):
    metadata: ProjectMetadata
    input_count: int = 0
    has_inventory: bool = False
    has_deviation_report: bool = False
    latest_job_state: str | None = None


class ProjectPaths:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.metadata = self.root / PROJECT_METADATA_FILENAME
        self.settings = self.root / PROJECT_SETTINGS_FILENAME
        self.inputs = self.root / "inputs"
        self.code = self.root / "code"
        self.state = self.root / "state"
        self.outputs = self.root / "outputs"
        self.jobs = self.root / "jobs"

    def ensure(self) -> None:
        for path in (self.root, self.inputs, self.code, self.state, self.outputs, self.jobs):
            path.mkdir(parents=True, exist_ok=True)


class ProjectError(ValueError):
    pass


class ProjectNotFoundError(ProjectError):
    pass


class UploadTooLargeError(ProjectError):
    pass


class SettingsInvalidationRequired(ProjectError):
    pass


class ProjectStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or get_app_data_dir()).expanduser().resolve()
        self.projects_dir = self.data_dir / PROJECTS_DIRNAME

    def create(self, name: str) -> ProjectMetadata:
        normalized_name = validate_project_name(name)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        project_id = uuid.uuid4().hex
        paths = ProjectPaths(self.projects_dir / project_id)
        paths.ensure()
        now = utc_now()
        metadata = ProjectMetadata(id=project_id, name=normalized_name, created_at=now, updated_at=now)
        _write_json(paths.metadata, metadata.model_dump(mode="json"))
        _write_json(paths.settings, ProjectSettings().model_dump(mode="json"))
        return metadata

    def paths(self, project_id: str) -> ProjectPaths:
        if not re.fullmatch(r"[0-9a-f]{32}", project_id):
            raise ProjectNotFoundError("Project not found.")
        paths = ProjectPaths(self.projects_dir / project_id)
        if not paths.metadata.is_file():
            raise ProjectNotFoundError("Project not found.")
        return paths

    def get(self, project_id: str) -> ProjectMetadata:
        paths = self.paths(project_id)
        return ProjectMetadata.model_validate_json(paths.metadata.read_text(encoding="utf-8"))

    def list(self, query: str = "", page: int = 1, per_page: int = 20) -> tuple[list[ProjectSummary], int]:
        page = max(1, page)
        per_page = min(100, max(1, per_page))
        needle = query.strip().casefold()
        projects: list[ProjectSummary] = []
        if self.projects_dir.exists():
            for metadata_path in self.projects_dir.glob(f"*/{PROJECT_METADATA_FILENAME}"):
                try:
                    metadata = ProjectMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
                    if needle and needle not in metadata.name.casefold():
                        continue
                    paths = ProjectPaths(metadata_path.parent)
                    projects.append(
                        ProjectSummary(
                            metadata=metadata,
                            input_count=sum(1 for path in paths.inputs.iterdir() if path.is_file()),
                            has_inventory=(paths.state / "inventory.json").is_file(),
                            has_deviation_report=(paths.outputs / "watson-prereg-adherence-report.md").is_file(),
                            latest_job_state=_latest_job_state(paths.jobs),
                        )
                    )
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        projects.sort(key=lambda project: project.metadata.updated_at, reverse=True)
        total = len(projects)
        start = (page - 1) * per_page
        return projects[start : start + per_page], total

    def get_settings(self, project_id: str) -> ProjectSettings:
        path = self.paths(project_id).settings
        if not path.exists():
            return ProjectSettings()
        return ProjectSettings.model_validate_json(path.read_text(encoding="utf-8"))

    def set_settings(
        self,
        project_id: str,
        settings: ProjectSettings,
        *,
        confirm_invalidation: bool = False,
    ) -> bool:
        paths = self.paths(project_id)
        previous = self.get_settings(project_id)
        changed_processing = previous.file_context != settings.file_context
        has_outputs = any(paths.outputs.iterdir()) or (paths.state / "inventory.json").exists()
        if changed_processing and has_outputs and not confirm_invalidation:
            raise SettingsInvalidationRequired(
                "These settings affect processing. Confirm to clear generated results; source inputs will be kept."
            )
        if changed_processing and has_outputs:
            self.invalidate_outputs(project_id)
        _write_json(paths.settings, settings.model_dump(mode="json"))
        self.touch(project_id)
        return changed_processing and has_outputs

    def add_stream(self, project_id: str, filename: str, stream: BinaryIO) -> InputRecord:
        paths = self.paths(project_id)
        safe_name = sanitize_filename(filename)
        destination = unique_destination(paths.inputs, safe_name)
        temporary = paths.inputs / f".{uuid.uuid4().hex}.upload"
        total = 0
        first = b""
        try:
            with temporary.open("xb") as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if not first:
                        first = chunk[:4096]
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise UploadTooLargeError(f"{safe_name} exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit.")
                    output.write(chunk)
            validate_file_signature(safe_name, first)
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        self.invalidate_code_audit(project_id, touch=False)
        self.touch(project_id)
        stat = destination.stat()
        return InputRecord(
            name=destination.name,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    def add_path(self, project_id: str, source: Path) -> InputRecord:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise ProjectError(f"Not a file: {source}")
        with source.open("rb") as stream:
            return self.add_stream(project_id, source.name, stream)

    def add_code_stream(self, project_id: str, relative_path: str, stream: BinaryIO) -> CodeFileRecord:
        paths = self.paths(project_id)
        relative = validate_code_relative_path(relative_path)
        destination = paths.code / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ProjectError(f"A code file already exists at {relative}.")
        temporary = destination.parent / f".{uuid.uuid4().hex}.upload"
        total = 0; digest = hashlib.sha256(); first = b""
        try:
            with temporary.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    if not first: first = chunk[:4096]
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise UploadTooLargeError(f"{relative} exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit.")
                    digest.update(chunk); output.write(chunk)
            if b"\x00" in first:
                raise ProjectError("Code audit accepts plaintext source and configuration files only.")
            temporary.replace(destination)
        finally:
            if temporary.exists(): temporary.unlink()
        self.invalidate_code_audit(project_id, touch=False)
        self.touch(project_id)
        return CodeFileRecord(path=relative, extension=destination.suffix.lower(), size_bytes=total, sha256=digest.hexdigest())

    def list_code_files(self, project_id: str) -> list[CodeFileRecord]:
        code = self.paths(project_id).code
        records = []
        for path in sorted(code.rglob("*"), key=lambda item: str(item).casefold()):
            if path.is_file() and not path.name.startswith("."):
                data = path.read_bytes()
                records.append(CodeFileRecord(path=path.relative_to(code).as_posix(), extension=path.suffix.lower(), size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
        return records

    def remove_code_file(self, project_id: str, relative_path: str) -> None:
        paths = self.paths(project_id); relative = validate_code_relative_path(relative_path)
        target = paths.code / relative
        if not target.is_file() or paths.code not in target.resolve().parents:
            raise ProjectError("Code file not found.")
        target.unlink(); self.invalidate_code_audit(project_id, touch=False); self.touch(project_id)

    def import_directory(self, project_id: str, source: Path) -> list[InputRecord]:
        source = source.expanduser().resolve()
        if not source.is_dir():
            raise ProjectError(f"Not a directory: {source}")
        candidates = sorted(
            (
                path
                for path in source.iterdir()
                if path.is_file()
                and not path.name.startswith(".")
                and path.suffix.lower() in supported_extensions()
            ),
            key=lambda path: path.name.casefold(),
        )
        total = sum(path.stat().st_size for path in candidates)
        if total > MAX_REQUEST_BYTES:
            raise UploadTooLargeError(
                f"Directory import exceeds the {MAX_REQUEST_BYTES // (1024 * 1024)} MB request limit."
            )
        for path in candidates:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise UploadTooLargeError(
                    f"{path.name} exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit."
                )
            safe_name = sanitize_filename(path.name)
            with path.open("rb") as stream:
                validate_file_signature(safe_name, stream.read(4096))
        return [self.add_path(project_id, path) for path in candidates]

    def list_inputs(self, project_id: str) -> list[InputRecord]:
        paths = self.paths(project_id)
        records = []
        for path in sorted(paths.inputs.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file() or path.name.startswith("."):
                continue
            stat = path.stat()
            records.append(
                InputRecord(
                    name=path.name,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
        return records

    def remove_input(self, project_id: str, filename: str) -> None:
        paths = self.paths(project_id)
        if filename != sanitize_filename(filename):
            raise ProjectError("Unsafe input filename.")
        target = paths.inputs / filename
        if not target.is_file() or target.parent.resolve() != paths.inputs:
            raise ProjectError("Input not found.")
        target.unlink()
        self.invalidate_code_audit(project_id, touch=False)
        self.touch(project_id)

    def invalidate_outputs(self, project_id: str) -> None:
        paths = self.paths(project_id)
        for path in paths.outputs.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for name in ("inventory.json", "study-map.json", "deviation-checks.json"):
            path = paths.state / name
            if path.exists():
                path.unlink()
        results_dir = paths.state / "deviation-results"
        if results_dir.exists():
            shutil.rmtree(results_dir)
        self.touch(project_id)

    def invalidate_code_audit(self, project_id: str, *, touch: bool = True) -> None:
        paths = self.paths(project_id)
        for path in (paths.state / "code-audit.json", paths.outputs / "watson-code-audit-report.md"):
            if path.exists(): path.unlink()
        if touch: self.touch(project_id)

    def touch(self, project_id: str) -> None:
        paths = self.paths(project_id)
        metadata = self.get(project_id)
        metadata.updated_at = utc_now()
        _write_json(paths.metadata, metadata.model_dump(mode="json"))

    def rename(self, project_id: str, name: str) -> ProjectMetadata:
        normalized_name = validate_project_name(name)
        paths = self.paths(project_id)
        metadata = self.get(project_id)
        metadata.name = normalized_name
        metadata.updated_at = utc_now()
        _write_json(paths.metadata, metadata.model_dump(mode="json"))
        return metadata

    def delete(self, project_id: str) -> None:
        paths = self.paths(project_id)
        shutil.rmtree(paths.root)


def validate_project_name(name: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFC", name).split())
    if not normalized:
        raise ProjectError("Project name is required.")
    if len(normalized) > MAX_PROJECT_NAME_LENGTH:
        raise ProjectError(f"Project name cannot exceed {MAX_PROJECT_NAME_LENGTH} characters.")
    if any(ord(character) < 32 for character in normalized):
        raise ProjectError("Project name contains invalid characters.")
    return normalized


def sanitize_filename(filename: str) -> str:
    normalized = unicodedata.normalize("NFC", filename).replace("\\", "/").split("/")[-1]
    normalized = "".join(character for character in normalized if ord(character) >= 32 and character != "\x7f")
    normalized = re.sub(r'[<>:"|?*]', "_", normalized)
    normalized = normalized.strip().strip(".")
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized or normalized in {".", ".."}:
        raise ProjectError("Input filename is invalid.")
    suffix = Path(normalized).suffix.lower()
    if suffix not in supported_extensions():
        raise ProjectError(f"Unsupported file type: {suffix or '[no extension]'}.")
    if len(normalized) > MAX_FILENAME_LENGTH:
        stem = Path(normalized).stem[: MAX_FILENAME_LENGTH - len(suffix) - 1].rstrip()
        normalized = f"{stem}{suffix}"
    if Path(normalized).stem.upper() in {
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
        "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
        "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        normalized = f"_{normalized}"
    return normalized

def validate_code_relative_path(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("\\", "/")
    path = Path(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProjectError("Unsafe code file path.")
    if any(ord(char) < 32 for char in value) or len(value) > 1000:
        raise ProjectError("Unsafe code file path.")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        raise ProjectError("CSV files are raw data and are not accepted for code audit. Add them as input documents if needed.")
    if suffix not in CODE_EXTENSIONS:
        raise ProjectError(f"Unsupported code file type: {path.suffix or '[no extension]'}.")
    return path.as_posix()


def unique_destination(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    suffix = Path(filename).suffix
    stem = Path(filename).stem
    number = 2
    while True:
        candidate = directory / f"{stem} ({number}){suffix}"
        if not candidate.exists():
            return candidate
        number += 1


def validate_file_signature(filename: str, first_bytes: bytes) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf" and not first_bytes.startswith(b"%PDF-"):
        raise ProjectError("The uploaded .pdf file does not have a valid PDF signature.")
    if suffix != ".pdf" and b"\x00" in first_bytes:
        raise ProjectError(f"The uploaded {suffix} file appears to be binary data.")
    if suffix == ".json" and first_bytes:
        try:
            first_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectError("The uploaded JSON file is not UTF-8 text.") from exc


def _latest_job_state(jobs_dir: Path) -> str | None:
    jobs: list[tuple[datetime, str]] = []
    if not jobs_dir.exists():
        return None
    for path in jobs_dir.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            jobs.append((datetime.fromisoformat(raw["created_at"]), str(raw["state"])))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return max(jobs, default=(utc_now(), None), key=lambda item: item[0])[1]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
