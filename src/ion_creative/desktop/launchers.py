"""Manage user-local launchers for installed Craft desktop applications."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ion_creative.paths import runtime_dir
from ion_creative.runtime.registry import RuntimeSpec, load_runtime_specs

MANAGED_MARKER = "X-Ion-Creative=1"
DISPLAY_NAMES = {
    "photocraft": "PhotoCraft",
    "filmcraft": "FilmCraft",
    "effectcraft": "EffectCraft",
    "lightcraft": "LightCraft",
    "vectorcraft": "VectorCraft",
    "designcraft": "DesignCraft",
    "pdfcraft": "PdfCraft",
}
CATEGORIES = {
    "photocraft": "Graphics;",
    "filmcraft": "AudioVideo;",
    "effectcraft": "AudioVideo;",
    "lightcraft": "Graphics;",
    "vectorcraft": "Graphics;",
    "designcraft": "Graphics;",
    "pdfcraft": "Office;",
}


def applications_dir() -> Path:
    """Return the permitted user-local desktop entry directory."""
    return Path.home() / ".local" / "share" / "applications"


def _launcher_path(engine: str, destination_dir: Path) -> Path:
    return destination_dir / f"ion-creative-{engine}.desktop"


def _desktop_binary(spec: RuntimeSpec) -> Path:
    return runtime_dir(spec.engine, spec.version) / "bin" / spec.desktop_binary


def _exec_argument(path: Path) -> str:
    escaped = str(path)
    for character in ("\\", '"', "`", "$", "%"):
        escaped = escaped.replace(character, "\\" + character if character != "%" else "%%")
    return f'"{escaped}"'


def _content(spec: RuntimeSpec, binary: Path) -> str:
    name = DISPLAY_NAMES[spec.engine]
    icon = "application-pdf" if spec.engine == "pdfcraft" else "applications-graphics"
    return (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment=Launch {name}\n"
        f"Exec={_exec_argument(binary)}\n"
        f"TryExec={_exec_argument(binary)}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        f"Categories={CATEGORIES[spec.engine]}\n"
        f"{MANAGED_MARKER}\n"
    )


def _is_managed(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return MANAGED_MARKER in path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False


def _prepare_destination(destination_dir: Path) -> None:
    if destination_dir.is_symlink():
        raise OSError(f"Applications directory cannot be a symlink: {destination_dir}")
    if destination_dir.exists() and not destination_dir.is_dir():
        raise OSError(f"Applications path is not a directory: {destination_dir}")
    if destination_dir == applications_dir():
        home = Path.home()
        for parent in (home / ".local", home / ".local" / "share"):
            if parent.is_symlink():
                raise OSError(f"User applications path cannot pass through a symlink: {parent}")
    destination_dir.mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        temporary_path.chmod(0o755)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _verify_one(spec: RuntimeSpec, destination_dir: Path) -> dict[str, object]:
    path = _launcher_path(spec.engine, destination_dir)
    binary = _desktop_binary(spec)
    base: dict[str, object] = {
        "engine": spec.engine,
        "path": str(path),
        "binary_path": str(binary),
        "installed": path.is_file() and not path.is_symlink(),
        "verified": False,
        "error_code": None,
        "message": "",
    }
    if not binary.is_file() or not binary.stat().st_mode & 0o111:
        base.update(error_code="DESKTOP_BINARY_NOT_FOUND", message=f"Desktop binary is missing: {binary}")
        return base
    if path.is_symlink() or not path.is_file():
        base.update(error_code="MISSING_LAUNCHER", message=f"Managed launcher is missing: {path}")
        return base
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        base.update(error_code="LAUNCHER_UNREADABLE", message=str(exc))
        return base
    expected = _content(spec, binary)
    if MANAGED_MARKER not in content.splitlines():
        base.update(error_code="UNMANAGED_LAUNCHER", message="Launcher is not managed by Ion Creative")
        return base
    if content != expected:
        base.update(error_code="LAUNCHER_MISMATCH", message="Launcher does not match the pinned runtime")
        return base
    if not path.stat().st_mode & 0o111:
        base.update(error_code="LAUNCHER_NOT_EXECUTABLE", message="Desktop launcher is not executable")
        return base
    base.update(verified=True, message="Pinned desktop launcher verified")
    return base


def install_launchers(*, destination_dir: Path | None = None) -> list[dict[str, object]]:
    """Install or update only Ion-managed launchers; preserve other entries."""
    destination = (destination_dir or applications_dir()).expanduser()
    specs = list(load_runtime_specs().values())
    if not any(
        _desktop_binary(spec).is_file() and _desktop_binary(spec).stat().st_mode & 0o111
        for spec in specs
    ):
        return [_verify_one(spec, destination) for spec in specs]
    try:
        _prepare_destination(destination)
    except OSError as exc:
        return [
            {
                "engine": "all",
                "path": str(destination),
                "installed": False,
                "verified": False,
                "error_code": "APPLICATIONS_DIRECTORY_UNAVAILABLE",
                "message": str(exc),
            }
        ]

    results = []
    for spec in specs:
        path = _launcher_path(spec.engine, destination)
        binary = _desktop_binary(spec)
        if not binary.is_file() or not binary.stat().st_mode & 0o111:
            result = _verify_one(spec, destination)
        elif (path.exists() or path.is_symlink()) and not _is_managed(path):
            result = {
                "engine": spec.engine,
                "path": str(path),
                "binary_path": str(binary),
                "installed": False,
                "verified": False,
                "error_code": "UNMANAGED_LAUNCHER_CONFLICT",
                "message": "Existing launcher is not managed by Ion Creative; left untouched",
            }
        else:
            _atomic_write(path, _content(spec, binary))
            result = _verify_one(spec, destination)
            result["installed"] = result["verified"]
        results.append(result)
    return results


def list_launchers(*, destination_dir: Path | None = None) -> list[dict[str, object]]:
    """List the expected Ion launcher paths without changing the filesystem."""
    destination = (destination_dir or applications_dir()).expanduser()
    results = []
    for spec in load_runtime_specs().values():
        path = _launcher_path(spec.engine, destination)
        managed = _is_managed(path)
        results.append(
            {
                "engine": spec.engine,
                "path": str(path),
                "installed": path.is_file() and not path.is_symlink(),
                "managed": managed,
                "binary_path": str(_desktop_binary(spec)),
            }
        )
    return results


def verify_launchers(*, destination_dir: Path | None = None) -> list[dict[str, object]]:
    """Verify every launcher against its pinned desktop binary and entry data."""
    destination = (destination_dir or applications_dir()).expanduser()
    return [_verify_one(spec, destination) for spec in load_runtime_specs().values()]
