"""Load and validate the pinned Craft runtime registry."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENGINE_NAMES = (
    "photocraft",
    "filmcraft",
    "effectcraft",
    "lightcraft",
    "vectorcraft",
    "designcraft",
    "pdfcraft",
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    engine: str
    version: str
    github_owner: str
    github_repo: str
    tag: str
    asset_pattern: str
    artifact_sha256: str
    desktop_binary: str
    cli_binary: str


def _manifest_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "runtimes.json"


def _parse_spec(engine: str, data: Any) -> RuntimeSpec:
    if not isinstance(data, dict):
        raise TypeError(f"Runtime spec for {engine!r} must be an object")
    required = {
        "version",
        "github_owner",
        "github_repo",
        "tag",
        "asset_pattern",
        "artifact_sha256",
        "desktop_binary",
        "cli_binary",
    }
    missing = required - data.keys()
    if missing:
        raise ValueError(f"Runtime spec for {engine!r} is missing: {', '.join(sorted(missing))}")
    values = {key: data[key] for key in required}
    if not all(isinstance(value, str) and value for value in values.values()):
        raise ValueError(f"Runtime spec for {engine!r} contains empty or non-string fields")
    if not VERSION_PATTERN.fullmatch(values["version"]):
        raise ValueError(f"Runtime spec for {engine!r} has an invalid pinned version")
    if values["tag"] != f"v{values['version']}":
        raise ValueError(f"Runtime spec for {engine!r} tag does not match its version")
    if not SHA256_PATTERN.fullmatch(values["artifact_sha256"]):
        raise ValueError(f"Runtime spec for {engine!r} has an invalid SHA-256 digest")
    expected_asset = f"{engine}-{values['version']}-linux-x86_64.tar.gz"
    if values["asset_pattern"] != expected_asset:
        raise ValueError(f"Runtime spec for {engine!r} must name its pinned Linux x86_64 tarball")
    if values["github_repo"] != engine:
        raise ValueError(f"Runtime spec for {engine!r} points at an unexpected repository")
    return RuntimeSpec(engine=engine, **values)


def load_runtime_specs(path: Path | None = None) -> dict[str, RuntimeSpec]:
    """Load every pinned Craft runtime from JSON and reject incomplete or extra engines."""
    manifest_path = path or _manifest_path()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to load runtime registry at {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != set(ENGINE_NAMES):
        raise ValueError(f"Runtime registry must define exactly: {', '.join(ENGINE_NAMES)}")
    return {engine: _parse_spec(engine, raw[engine]) for engine in ENGINE_NAMES}
