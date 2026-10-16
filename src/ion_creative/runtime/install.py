"""Verified, versioned installation of pinned Craft runtime assets."""

import hashlib
import json
import os
import posixpath
import re
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ion_creative.paths import runtime_dir, runtime_root
from ion_creative.runtime.doctor import RuntimeStatus, doctor_engine, doctor_runtime_path
from ion_creative.runtime.registry import RuntimeSpec, load_runtime_specs

GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 30
CHUNK_SIZE = 1024 * 1024
SHA256_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+(.+?)\s*$")


class RuntimeInstallError(Exception):
    """An expected upstream, archive, or promotion failure."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    download_url: str
    sha256: str


def _request_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Ion-Creative-Wave1",
        },
    )
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeInstallError(f"Unable to fetch pinned release metadata: {exc}") from exc


def _api_sha256(asset: dict[str, object]) -> str | None:
    digest = asset.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    value = digest.removeprefix("sha256:").lower()
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


def _checksum_file_digest(contents: bytes, asset_name: str) -> str | None:
    try:
        lines = contents.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeInstallError("Upstream SHA256SUMS.txt is not valid UTF-8") from exc
    for line in lines:
        match = SHA256_LINE.fullmatch(line.strip())
        if match and match.group(2).removeprefix("*").strip() == asset_name:
            return match.group(1).lower()
    return None


def _release_checksum(release_assets: list[dict[str, object]], selected: dict[str, object], spec: RuntimeSpec) -> str:
    checksum_asset = next(
        (asset for asset in release_assets if asset.get("name") == "SHA256SUMS.txt"), None
    )
    if checksum_asset is not None:
        checksum_url = checksum_asset.get("browser_download_url")
        if not _is_canonical_release_url(checksum_url, spec, "SHA256SUMS.txt"):
            raise RuntimeInstallError("Pinned release checksum asset has no download URL")
        checksum_bytes = _request_bytes(checksum_url)
        checksum_sha = _api_sha256(checksum_asset)
        if checksum_sha and hashlib.sha256(checksum_bytes).hexdigest() != checksum_sha:
            raise RuntimeInstallError("Pinned release SHA256SUMS.txt failed GitHub digest verification")
        expected = _checksum_file_digest(checksum_bytes, spec.asset_pattern)
        if expected is None:
            raise RuntimeInstallError(f"SHA256SUMS.txt has no entry for {spec.asset_pattern}")
    else:
        expected = _api_sha256(selected)
        if expected is None:
            raise RuntimeInstallError("Pinned artifact has no verifiable upstream SHA-256 source")
    if expected != spec.artifact_sha256:
        raise RuntimeInstallError(
            f"Pinned release SHA-256 differs from the registry for {spec.asset_pattern}"
        )
    return expected


def _is_canonical_release_url(url: object, spec: RuntimeSpec, asset_name: str) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    expected_path = (
        f"/{spec.github_owner}/{spec.github_repo}/releases/download/{spec.tag}/{asset_name}"
    )
    return (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and parsed.path == expected_path
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _resolve_release_asset(spec: RuntimeSpec) -> ReleaseAsset:
    """Resolve one exact tag and asset; never consult GitHub's latest release."""
    url = f"{GITHUB_API}/repos/{spec.github_owner}/{spec.github_repo}/releases/tags/{spec.tag}"
    try:
        release = json.loads(_request_bytes(url))
    except json.JSONDecodeError as exc:
        raise RuntimeInstallError("Pinned release metadata is not valid JSON") from exc
    if not isinstance(release, dict) or release.get("tag_name") != spec.tag:
        raise RuntimeInstallError(f"GitHub did not return the exact pinned tag {spec.tag}")
    if release.get("draft") is True:
        raise RuntimeInstallError(f"Pinned release {spec.tag} is still a draft")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeInstallError("Pinned release metadata has no asset list")
    selected = next(
        (
            asset
            for asset in assets
            if isinstance(asset, dict) and asset.get("name") == spec.asset_pattern
        ),
        None,
    )
    if selected is None:
        raise RuntimeInstallError(f"Pinned Linux x86_64 asset is missing: {spec.asset_pattern}")
    download_url = selected.get("browser_download_url")
    if not _is_canonical_release_url(download_url, spec, spec.asset_pattern):
        raise RuntimeInstallError("Pinned artifact URL is outside its canonical GitHub repository")
    digest = _release_checksum(assets, selected, spec)
    return ReleaseAsset(download_url=download_url, sha256=digest)


def _download_asset(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "Ion-Creative-Wave1"})
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT) as response, destination.open("wb") as output:
            while chunk := response.read(CHUNK_SIZE):
                output.write(chunk)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeInstallError(f"Unable to download pinned runtime asset: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive_path: Path, destination: Path) -> None:
    """Extract only ordinary archive members that remain inside destination."""
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                member_path = PurePosixPath(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise RuntimeInstallError(f"Archive path escapes runtime root: {member.name}")
                if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                    raise RuntimeInstallError(f"Unsupported archive member type: {member.name}")
                if member.issym() or member.islnk():
                    link_path = PurePosixPath(member.linkname)
                    if link_path.is_absolute():
                        raise RuntimeInstallError(f"Archive link is absolute: {member.name}")
                    base = member_path.parent if member.issym() else PurePosixPath()
                    resolved = PurePosixPath(posixpath.normpath(str(base / link_path)))
                    if ".." in resolved.parts:
                        raise RuntimeInstallError(f"Archive link escapes runtime root: {member.name}")
            archive.extractall(destination, members=members, filter="data")
    except (tarfile.TarError, EOFError) as exc:
        raise RuntimeInstallError(f"Pinned runtime asset is not a valid tar.gz archive: {exc}") from exc


def _payload_root(extracted: Path, spec: RuntimeSpec) -> Path:
    candidates = {
        cli.parents[1]
        for cli in extracted.rglob(spec.cli_binary)
        if cli.is_file() and cli.parent.name == "bin"
    }
    if len(candidates) != 1:
        raise RuntimeInstallError(
            f"Archive must contain one bin/{spec.cli_binary}; found {len(candidates)}"
        )
    return candidates.pop()


def _status(
    spec: RuntimeSpec,
    *,
    healthy: bool,
    error_code: str,
    message: str,
    observed_version: str | None = None,
) -> RuntimeStatus:
    root = runtime_dir(spec.engine, spec.version)
    return RuntimeStatus(
        engine=spec.engine,
        version=spec.version,
        runtime_path=root,
        cli_path=root / "bin" / spec.cli_binary,
        healthy=healthy,
        observed_version=observed_version,
        error_code=None if healthy else error_code,
        message=message,
    )


def _validate_current_link(engine_root: Path, current: Path) -> str | None:
    if current.exists() and not current.is_symlink():
        return "current exists but is not an Ion-managed symlink"
    if current.is_symlink():
        target = current.readlink()
        if target.is_absolute() or len(target.parts) != 1:
            return "current symlink points outside the engine runtime directory"
        resolved = (engine_root / target).resolve(strict=False)
        if resolved.parent != engine_root.resolve(strict=False):
            return "current symlink points outside the engine runtime directory"
    return None


def _set_current(engine_root: Path, current: Path, version: str) -> None:
    temporary_link = engine_root / f".current-{uuid.uuid4().hex}"
    try:
        temporary_link.symlink_to(version)
        os.replace(temporary_link, current)
    finally:
        temporary_link.unlink(missing_ok=True)


def _backup_path(engine_root: Path, version: str) -> Path:
    while True:
        candidate = engine_root / f"{version}.backup-{uuid.uuid4().hex[:12]}"
        if not candidate.exists():
            return candidate


def _rebase_status(status: RuntimeStatus, destination: Path, spec: RuntimeSpec) -> RuntimeStatus:
    return RuntimeStatus(
        engine=spec.engine,
        version=spec.version,
        runtime_path=destination,
        cli_path=destination / "bin" / spec.cli_binary,
        healthy=status.healthy,
        observed_version=status.observed_version,
        error_code=status.error_code,
        message=status.message,
    )


def install_runtime(spec: RuntimeSpec, *, force: bool = False) -> RuntimeStatus:
    """Install one manifest-pinned runtime without touching older versions."""
    specs = load_runtime_specs()
    if specs.get(spec.engine) != spec:
        return _status(
            spec,
            healthy=False,
            error_code="REGISTRY_MISMATCH",
            message="Runtime spec does not match the checked-in pinned registry",
        )
    root = runtime_root()
    engine_root = root / spec.engine
    destination = runtime_dir(spec.engine, spec.version)
    current = engine_root / "current"
    link_error = _validate_current_link(engine_root, current)
    if link_error:
        return _status(
            spec,
            healthy=False,
            error_code="CURRENT_LINK_CONFLICT",
            message=link_error,
        )

    if destination.exists() and not force:
        existing = doctor_engine(spec.engine)
        if not existing.healthy:
            return existing
        try:
            _set_current(engine_root, current, spec.version)
        except OSError as exc:
            return _status(
                spec,
                healthy=False,
                error_code="CURRENT_UPDATE_FAILED",
                message=f"Pinned runtime is healthy but current could not be updated: {exc}",
                observed_version=existing.observed_version,
            )
        return existing

    try:
        engine_root.mkdir(parents=True, exist_ok=True)
        resolved = _resolve_release_asset(spec)
        with tempfile.TemporaryDirectory(
            prefix=f".{spec.version}.install-", dir=engine_root
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            archive_path = temporary_root / "runtime.tar.gz"
            _download_asset(resolved.download_url, archive_path)
            actual_digest = _sha256_file(archive_path)
            if actual_digest != resolved.sha256:
                return _status(
                    spec,
                    healthy=False,
                    error_code="CHECKSUM_MISMATCH",
                    message=(
                        f"Downloaded runtime SHA-256 {actual_digest} does not match "
                        f"verified release digest {resolved.sha256}"
                    ),
                )
            extracted = temporary_root / "extracted"
            extracted.mkdir()
            _safe_extract(archive_path, extracted)
            payload = _payload_root(extracted, spec)
            staged_runtime = temporary_root / "payload"
            os.replace(payload, staged_runtime)
            staged_status = doctor_runtime_path(spec, staged_runtime)
            if not staged_status.healthy:
                return _rebase_status(staged_status, destination, spec)

            backup = None
            if destination.exists() or destination.is_symlink():
                backup = _backup_path(engine_root, spec.version)
                os.replace(destination, backup)
            try:
                os.replace(staged_runtime, destination)
            except OSError:
                if backup is not None and backup.exists():
                    os.replace(backup, destination)
                raise

        try:
            _set_current(engine_root, current, spec.version)
        except OSError as exc:
            return _status(
                spec,
                healthy=False,
                error_code="CURRENT_UPDATE_FAILED",
                message=f"Pinned runtime is installed but current could not be updated: {exc}",
                observed_version=staged_status.observed_version,
            )
        return _rebase_status(staged_status, destination, spec)
    except RuntimeInstallError as exc:
        return _status(
            spec,
            healthy=False,
            error_code="INSTALL_FAILED",
            message=str(exc),
        )
    except (OSError, tarfile.TarError) as exc:
        return _status(
            spec,
            healthy=False,
            error_code="INSTALL_FAILED",
            message=f"Pinned runtime installation failed: {exc}",
        )
