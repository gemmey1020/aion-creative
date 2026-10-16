"""Exact-version executable health checks."""

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from ion_creative.paths import runtime_dir
from ion_creative.runtime.registry import RuntimeSpec, load_runtime_specs

VERSION_PATTERN = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    engine: str
    version: str
    runtime_path: Path
    cli_path: Path
    healthy: bool
    observed_version: str | None = None
    error_code: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["runtime_path"] = str(self.runtime_path)
        result["cli_path"] = str(self.cli_path)
        return result


def _status(
    spec: RuntimeSpec,
    runtime_path: Path,
    cli_path: Path,
    *,
    healthy: bool,
    observed_version: str | None = None,
    error_code: str | None = None,
    message: str = "",
) -> RuntimeStatus:
    return RuntimeStatus(
        engine=spec.engine,
        version=spec.version,
        runtime_path=runtime_path,
        cli_path=cli_path,
        healthy=healthy,
        observed_version=observed_version,
        error_code=error_code,
        message=message,
    )


def doctor_engine(engine: str) -> RuntimeStatus:
    """Check that a pinned CLI exists and reports exactly its declared version."""
    specs = load_runtime_specs()
    if engine not in specs:
        unknown_path = Path()
        return RuntimeStatus(
            engine=engine,
            version="",
            runtime_path=unknown_path,
            cli_path=unknown_path,
            healthy=False,
            error_code="UNKNOWN_ENGINE",
            message=f"Unknown Ion runtime: {engine}",
        )
    spec = specs[engine]
    return doctor_runtime_path(spec, runtime_dir(spec.engine, spec.version))


def doctor_runtime_path(spec: RuntimeSpec, root: Path) -> RuntimeStatus:
    """Check one runtime tree, including an isolated staging tree before promotion."""
    cli = root / "bin" / spec.cli_binary
    if not root.exists():
        return _status(
            spec,
            root,
            cli,
            healthy=False,
            error_code="NOT_INSTALLED",
            message=f"Pinned runtime is not installed: {root}",
        )
    if not cli.is_file() or not cli.stat().st_mode & 0o111:
        return _status(
            spec,
            root,
            cli,
            healthy=False,
            error_code="CLI_NOT_FOUND",
            message=f"Pinned CLI is missing or not executable: {cli}",
        )
    try:
        completed = subprocess.run(
            [str(cli), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _status(
            spec,
            root,
            cli,
            healthy=False,
            error_code="CLI_VERSION_TIMEOUT",
            message="CLI version check timed out",
        )
    except OSError as exc:
        return _status(
            spec,
            root,
            cli,
            healthy=False,
            error_code="CLI_EXEC_FAILED",
            message=str(exc),
        )
    if completed.returncode != 0:
        return _status(
            spec,
            root,
            cli,
            healthy=False,
            error_code="CLI_VERSION_FAILED",
            message=(completed.stderr or completed.stdout).strip()
            or f"CLI exited with status {completed.returncode}",
        )
    output = f"{completed.stdout}\n{completed.stderr}"
    match = VERSION_PATTERN.search(output)
    observed = match.group(1) if match else None
    if observed is None:
        return _status(
            spec,
            root,
            cli,
            healthy=False,
            error_code="VERSION_UNPARSEABLE",
            message="CLI returned no semantic version",
        )
    if observed != spec.version:
        return _status(
            spec,
            root,
            cli,
            healthy=False,
            observed_version=observed,
            error_code="VERSION_MISMATCH",
            message=f"Expected {spec.version}, CLI reported {observed}",
        )
    return _status(
        spec,
        root,
        cli,
        healthy=True,
        observed_version=observed,
        message="Pinned CLI version verified",
    )
