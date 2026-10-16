import hashlib
import importlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_installer(project_root: Path):
    module_path = project_root / "src" / "ion_creative" / "runtime" / "install.py"
    assert module_path.is_file(), "Runtime installer is not implemented"
    return importlib.import_module("ion_creative.runtime.install")


def _archive(spec) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        cli = f"#!/bin/sh\nprintf '%s\\n' '{spec.engine}-cli {spec.version}'\n".encode()
        desktop = b"#!/bin/sh\nexit 0\n"
        for name, content in (
            (f"{spec.engine}-{spec.version}/bin/{spec.cli_binary}", cli),
            (f"{spec.engine}-{spec.version}/{spec.desktop_binary}", desktop),
        ):
            entry = tarfile.TarInfo(name)
            entry.mode = 0o755
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return payload.getvalue()


def _fake_download(monkeypatch, module, payload: bytes, digest: str | None = None) -> list[Path]:
    expected_digest = digest or hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        module,
        "_resolve_release_asset",
        lambda spec: SimpleNamespace(
            download_url="https://github.com/storytold/example/releases/download/v1/example.tar.gz",
            sha256=expected_digest,
        ),
    )
    destinations: list[Path] = []

    def download(_url: str, destination: Path) -> None:
        destinations.append(destination)
        destination.write_bytes(payload)

    monkeypatch.setattr(module, "_download_asset", download)
    return destinations


def _load_spec():
    from ion_creative.runtime.registry import load_runtime_specs

    return load_runtime_specs()["photocraft"]


def test_install_uses_versioned_destination_and_preserves_sibling_versions(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    module = _load_installer(project_root)
    spec = _load_spec()
    runtime_root = tmp_path / "runtimes"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))
    sibling = runtime_root / spec.engine / "0.4.9"
    sibling.mkdir(parents=True)
    marker = sibling / "keep.txt"
    marker.write_text("older version", encoding="utf-8")
    _fake_download(monkeypatch, module, _archive(spec))

    status = module.install_runtime(spec)

    destination = runtime_root / spec.engine / spec.version
    assert status.healthy is True
    assert status.runtime_path == destination
    assert (destination / "bin" / spec.cli_binary).is_file()
    assert (runtime_root / spec.engine / "current").is_symlink()
    assert (runtime_root / spec.engine / "current").resolve() == destination
    assert marker.read_text(encoding="utf-8") == "older version"


def test_existing_exact_version_is_reused_unless_forced(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    module = _load_installer(project_root)
    spec = _load_spec()
    runtime_root = tmp_path / "runtimes"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))
    cli = runtime_root / spec.engine / spec.version / "bin" / spec.cli_binary
    cli.parent.mkdir(parents=True)
    cli.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{spec.engine}-cli {spec.version}'\n", encoding="utf-8"
    )
    cli.chmod(0o755)
    monkeypatch.setattr(
        module,
        "_resolve_release_asset",
        lambda _spec: pytest.fail("healthy exact version should be reused"),
    )

    status = module.install_runtime(spec)

    assert status.healthy is True
    assert (runtime_root / spec.engine / "current").resolve() == cli.parents[1]


def test_force_reinstalls_exact_version_without_discarding_previous_payload(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    module = _load_installer(project_root)
    spec = _load_spec()
    runtime_root = tmp_path / "runtimes"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))
    old_cli = runtime_root / spec.engine / spec.version / "bin" / spec.cli_binary
    old_cli.parent.mkdir(parents=True)
    old_cli.write_text("old payload", encoding="utf-8")
    old_payload = _archive(spec)
    _fake_download(monkeypatch, module, old_payload)

    status = module.install_runtime(spec, force=True)

    backups = list((runtime_root / spec.engine).glob(f"{spec.version}.backup-*"))
    assert status.healthy is True
    assert backups
    assert (backups[0] / "bin" / spec.cli_binary).read_text(encoding="utf-8") == "old payload"
    assert (runtime_root / spec.engine / "current").resolve() == old_cli.parents[1]


def test_checksum_is_verified_before_archive_extraction(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    module = _load_installer(project_root)
    spec = _load_spec()
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    _fake_download(monkeypatch, module, b"not the signed archive", digest="0" * 64)
    extracted = False

    def extract(*_args, **_kwargs):
        nonlocal extracted
        extracted = True

    monkeypatch.setattr(module, "_safe_extract", extract)

    status = module.install_runtime(spec)

    assert status.healthy is False
    assert status.error_code == "CHECKSUM_MISMATCH"
    assert extracted is False


def test_failed_checksum_leaves_current_symlink_unchanged(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    module = _load_installer(project_root)
    spec = _load_spec()
    runtime_root = tmp_path / "runtimes"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))
    engine_root = runtime_root / spec.engine
    engine_root.mkdir(parents=True)
    previous = engine_root / "0.4.9"
    previous.mkdir()
    current = engine_root / "current"
    current.symlink_to("0.4.9")
    _fake_download(monkeypatch, module, b"bad", digest="0" * 64)

    status = module.install_runtime(spec)

    assert status.error_code == "CHECKSUM_MISMATCH"
    assert current.is_symlink()
    assert current.readlink() == Path("0.4.9")
    assert previous.is_dir()
    assert not (engine_root / spec.version).exists()


def test_release_resolution_uses_exact_tag_asset_and_upstream_checksum(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    module = _load_installer(project_root)
    spec = _load_spec()
    release_url = (
        f"https://api.github.com/repos/{spec.github_owner}/{spec.github_repo}/"
        f"releases/tags/{spec.tag}"
    )
    artifact_url = (
        f"https://github.com/{spec.github_owner}/{spec.github_repo}/releases/download/"
        f"{spec.tag}/{spec.asset_pattern}"
    )
    checksum_url = (
        f"https://github.com/{spec.github_owner}/{spec.github_repo}/releases/download/"
        f"{spec.tag}/SHA256SUMS.txt"
    )
    checksum_contents = f"{spec.artifact_sha256}  {spec.asset_pattern}\n".encode()
    release = {
        "tag_name": spec.tag,
        "draft": False,
        "assets": [
            {
                "name": spec.asset_pattern,
                "browser_download_url": artifact_url,
                "digest": f"sha256:{spec.artifact_sha256}",
            },
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": checksum_url,
                "digest": f"sha256:{hashlib.sha256(checksum_contents).hexdigest()}",
            },
        ],
    }
    requested: list[str] = []

    def fake_request(url: str) -> bytes:
        requested.append(url)
        if url == release_url:
            return json.dumps(release).encode()
        if url == checksum_url:
            return checksum_contents
        raise AssertionError(f"Unexpected upstream URL: {url}")

    monkeypatch.setattr(module, "_request_bytes", fake_request)

    asset = module._resolve_release_asset(spec)

    assert asset.download_url == artifact_url
    assert asset.sha256 == spec.artifact_sha256
    assert requested == [release_url, checksum_url]


def test_release_resolution_rejects_tag_drift(monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    module = _load_installer(project_root)
    spec = _load_spec()
    monkeypatch.setattr(module, "_request_bytes", lambda _url: b'{"tag_name":"v9.9.9"}')

    with pytest.raises(module.RuntimeInstallError, match="exact pinned tag"):
        module._resolve_release_asset(spec)


def test_safe_extraction_rejects_parent_traversal(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    module = _load_installer(project_root)
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        entry = tarfile.TarInfo("../../outside.txt")
        entry.size = 0
        archive.addfile(entry, io.BytesIO())

    with pytest.raises(module.RuntimeInstallError, match="escapes runtime root"):
        module._safe_extract(archive_path, tmp_path / "extract")
