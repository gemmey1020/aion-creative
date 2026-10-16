from pathlib import Path


def _load_launchers(project_root: Path):
    module_path = project_root / "src" / "ion_creative" / "desktop" / "launchers.py"
    assert module_path.is_file(), "Ion desktop launchers are not implemented"
    from ion_creative.desktop.launchers import install_launchers, verify_launchers

    return install_launchers, verify_launchers


def _create_desktop_binaries(runtime_root: Path) -> None:
    from ion_creative.runtime.registry import load_runtime_specs

    for engine, spec in load_runtime_specs().items():
        binary = runtime_root / engine / spec.version / "bin" / spec.desktop_binary
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)


def test_install_launchers_writes_verified_user_local_entries(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    install_launchers, verify_launchers = _load_launchers(project_root)
    runtime_root = tmp_path / "runtimes"
    applications = tmp_path / "applications"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))
    _create_desktop_binaries(runtime_root)

    installed = install_launchers(destination_dir=applications)
    verified = verify_launchers(destination_dir=applications)

    assert len(installed) == 7
    assert all(item["installed"] for item in installed)
    assert len(verified) == 7
    assert all(item["verified"] for item in verified)
    entry = applications / "ion-creative-photocraft.desktop"
    content = entry.read_text(encoding="utf-8")
    assert "X-Ion-Creative=1" in content
    assert f'Exec="{runtime_root}/photocraft/0.5.0/bin/photocraft"' in content
    assert entry.stat().st_mode & 0o111


def test_install_launchers_preserves_unmanaged_entry(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    install_launchers, _ = _load_launchers(project_root)
    runtime_root = tmp_path / "runtimes"
    applications = tmp_path / "applications"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))
    _create_desktop_binaries(runtime_root)
    applications.mkdir()
    existing = applications / "ion-creative-photocraft.desktop"
    existing.write_text("user-owned launcher\n", encoding="utf-8")

    results = {item["engine"]: item for item in install_launchers(destination_dir=applications)}

    assert results["photocraft"]["error_code"] == "UNMANAGED_LAUNCHER_CONFLICT"
    assert existing.read_text(encoding="utf-8") == "user-owned launcher\n"
    assert results["vectorcraft"]["installed"] is True


def test_install_launchers_reports_missing_desktop_binary(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    install_launchers, verify_launchers = _load_launchers(project_root)
    applications = tmp_path / "applications"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))

    results = install_launchers(destination_dir=applications)

    assert len(results) == 7
    assert all(item["error_code"] == "DESKTOP_BINARY_NOT_FOUND" for item in results)
    assert verify_launchers(destination_dir=applications)[0]["verified"] is False
    assert not applications.exists()
