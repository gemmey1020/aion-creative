"""Project workspaces, immutable input checks, and contained output paths."""

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ion_creative.paths import data_root

WORKSPACE_DIRECTORIES = (
    "inputs",
    "working",
    "previews",
    "intermediates",
    "exports",
    "ion",
)
WRITABLE_AREAS = frozenset({"working", "previews", "intermediates", "exports"})
READABLE_AREAS = frozenset({"inputs", "working", "previews", "intermediates", "exports"})
PROJECT_METADATA = {
    "manifest.json": {"schema_version": "0.1"},
    "workflow.json": {},
    "qa.json": {},
}


class ProjectSafetyError(ValueError):
    """A project path or workspace layout violates the filesystem boundary."""


PROJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class IonProject:
    root: Path
    inputs: Path
    working: Path
    previews: Path
    intermediates: Path
    exports: Path
    ion: Path

    def output_path(self, path: Path | str, *, area: str) -> Path:
        return safe_project_path(self, path, area=area)

    def input_path(self, path: Path | str) -> Path:
        return safe_input_path(self, path)


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def create_project(root: Path) -> IonProject:
    """Create the standard Ion directory tree without overwriting project metadata."""
    project_root = root.expanduser().resolve(strict=False)
    if project_root.exists() and not project_root.is_dir():
        raise ProjectSafetyError(f"Project root must be a directory: {project_root}")
    project_root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name in WORKSPACE_DIRECTORIES:
        directory = project_root / name
        if directory.is_symlink():
            raise ProjectSafetyError(f"Project workspace area must be a real directory: {directory}")
        if directory.exists() and not directory.is_dir():
            raise ProjectSafetyError(f"Project workspace area must be a directory: {directory}")
        directory.mkdir(exist_ok=True)
        paths[name] = directory

    ion_directory = paths["ion"]
    for name, initial_value in PROJECT_METADATA.items():
        metadata_path = ion_directory / name
        if metadata_path.is_symlink():
            raise ProjectSafetyError(f"Project metadata file must not be a symlink: {metadata_path}")
        if metadata_path.exists():
            if not metadata_path.is_file():
                raise ProjectSafetyError(f"Project metadata path must be a file: {metadata_path}")
            continue
        try:
            with metadata_path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(initial_value, indent=2, sort_keys=True) + "\n")
        except FileExistsError:
            continue

    return IonProject(root=project_root, **paths)


def create_named_project(project_id: str | None) -> IonProject:
    """Create or open a project selected by a bounded ID under Ion-owned storage."""
    selected_id = "default" if project_id is None else project_id
    if (
        not isinstance(selected_id, str)
        or selected_id in {".", ".."}
        or not PROJECT_ID_PATTERN.fullmatch(selected_id)
    ):
        raise ProjectSafetyError(
            "Project ID must start with a letter or number and contain at most "
            "64 letters, numbers, dots, underscores, or hyphens"
        )

    configured_root = os.environ.get("ION_CREATIVE_PROJECTS_ROOT")
    projects_root = (
        Path(configured_root).expanduser() if configured_root else data_root() / "projects"
    )
    if projects_root.is_symlink():
        raise ProjectSafetyError("Ion projects root must not be a symlink")
    projects_root.mkdir(parents=True, exist_ok=True)
    if projects_root.is_symlink() or not projects_root.is_dir():
        raise ProjectSafetyError("Ion projects root must be a real directory")

    project_root = projects_root / selected_id
    if project_root.is_symlink():
        raise ProjectSafetyError("Ion project directory must not be a symlink")
    return create_project(project_root)


def safe_project_path(project: IonProject, path: Path | str, *, area: str) -> Path:
    """Resolve one output path and reject traversal or symlink escapes."""
    if area not in WRITABLE_AREAS:
        raise ProjectSafetyError(f"{area!r} is not a writable project area")
    base = getattr(project, area)
    if base.is_symlink() or not base.is_dir():
        raise ProjectSafetyError(f"Writable project area must be a real directory: {base}")
    base_real = base.resolve(strict=True)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve(strict=False)
    if not _contains(base_real, resolved) or not _contains(project.root, resolved):
        raise ProjectSafetyError(f"Output path resolves outside the project {area} area: {path}")
    return resolved


def safe_input_path(project: IonProject, path: Path | str, *, must_exist: bool = True) -> Path:
    """Resolve a read-only input path and require it to stay inside inputs/."""
    if project.inputs.is_symlink() or not project.inputs.is_dir():
        raise ProjectSafetyError(f"Input project area must be a real directory: {project.inputs}")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = project.inputs / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not _contains(project.inputs.resolve(strict=True), resolved) or not _contains(
        project.root, resolved
    ):
        raise ProjectSafetyError(f"Input path resolves outside the project inputs area: {path}")
    if must_exist and not resolved.is_file():
        raise ProjectSafetyError(f"Project input must be a regular file: {resolved}")
    return resolved


def safe_project_read_path(project: IonProject, path: Path | str) -> Path:
    """Resolve one existing artifact while confining reads to project media areas."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = project.root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProjectSafetyError(f"Project artifact is missing or unreadable: {path}") from exc
    for area in READABLE_AREAS:
        directory = getattr(project, area)
        if directory.is_symlink() or not directory.is_dir():
            raise ProjectSafetyError(f"Project media area must be a real directory: {directory}")
        if _contains(directory.resolve(strict=True), resolved) and resolved.is_file():
            return resolved
    raise ProjectSafetyError(f"Artifact path resolves outside the project media areas: {path}")


def _workflow_metadata_path(project: IonProject) -> Path:
    if project.ion.is_symlink() or not project.ion.is_dir():
        raise ProjectSafetyError("Ion metadata directory must be a real directory")
    path = project.ion / "workflow.json"
    if path.is_symlink() or not path.is_file():
        raise ProjectSafetyError("Ion workflow metadata must be a regular file")
    return path


def _write_workflow_metadata(path: Path, data: dict[str, object]) -> None:
    temporary = path.parent / f".workflow-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def finalize_artifact(project: IonProject, path: Path | str) -> dict[str, str]:
    """Record the caller's visual-QA approval and fingerprint the chosen candidate."""
    artifact = safe_project_read_path(project, path)
    relative_path = artifact.relative_to(project.root).as_posix()
    if Path(relative_path).parts[0] not in {"working", "intermediates"}:
        raise ProjectSafetyError("Only working or intermediate candidates can be finalized")
    if project.previews.is_symlink() or not project.previews.is_dir():
        raise ProjectSafetyError("Project preview area must be a real directory")
    if not any(
        preview.is_file() and not preview.is_symlink() and preview.stat().st_size > 0
        for preview in project.previews.iterdir()
    ):
        raise ProjectSafetyError("A non-empty preview must exist before finalization")
    workflow_path = _workflow_metadata_path(project)
    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectSafetyError(f"Ion workflow metadata is not valid JSON: {exc}") from exc
    if not isinstance(workflow, dict):
        raise ProjectSafetyError("Ion workflow metadata must contain a JSON object")
    finalizations = workflow.get("finalizations", [])
    if not isinstance(finalizations, list):
        raise ProjectSafetyError("Ion workflow finalizations must be a JSON array")
    receipt = {
        "path": relative_path,
        "sha256": sha256_file(artifact),
        "finalized_at": datetime.now(UTC).isoformat(),
        "visual_review": "confirmed_by_caller",
    }
    finalizations.append(receipt)
    workflow["finalizations"] = finalizations
    _write_workflow_metadata(workflow_path, workflow)
    return receipt


def export_finalized_artifact(
    project: IonProject, path: Path | str, output_name: str
) -> Path:
    """Copy an unchanged finalized candidate into exports without overwriting output."""
    artifact = safe_project_read_path(project, path)
    relative_path = artifact.relative_to(project.root).as_posix()
    if Path(relative_path).parts[0] not in {"working", "intermediates"}:
        raise ProjectSafetyError("Only working or intermediate candidates can be exported")
    if (
        not output_name
        or output_name in {".", ".."}
        or Path(output_name).name != output_name
        or "/" in output_name
        or "\\" in output_name
        or "\x00" in output_name
    ):
        raise ProjectSafetyError("Export output name must be a plain filename")
    workflow_path = _workflow_metadata_path(project)
    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectSafetyError(f"Ion workflow metadata is not valid JSON: {exc}") from exc
    finalizations = workflow.get("finalizations", []) if isinstance(workflow, dict) else None
    digest = sha256_file(artifact)
    finalized = isinstance(finalizations, list) and any(
        isinstance(item, dict)
        and item.get("path") == relative_path
        for item in finalizations
    )
    if not finalized:
        raise ProjectSafetyError("Artifact must be finalized before export")
    unchanged = any(
        isinstance(item, dict)
        and item.get("path") == relative_path
        and item.get("sha256") == digest
        for item in finalizations
    )
    if not unchanged:
        raise ProjectSafetyError("Artifact changed after finalization and cannot be exported")

    requested = Path(output_name)
    destination = project.exports / requested.name
    for index in range(1000):
        if index:
            destination = project.exports / f"{requested.stem}-{index:03d}{requested.suffix}"
        destination = safe_project_path(project, destination, area="exports")
        if not destination.exists() and not destination.is_symlink():
            break
    else:
        raise ProjectSafetyError(f"Unable to allocate export output name: {output_name}")

    temporary = project.exports / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with artifact.open("rb") as source, temporary.open("xb") as target:
            shutil.copyfileobj(source, target)
        if sha256_file(temporary) != digest:
            raise ProjectSafetyError("Export copy does not match the finalized artifact")
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def sha256_file(path: Path) -> str:
    """Return a streaming SHA-256 digest for one file."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
