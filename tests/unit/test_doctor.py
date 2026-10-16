from pathlib import Path

EXPECTED_VERSIONS = {
    "photocraft": "0.5.0",
    "filmcraft": "0.7.2",
    "effectcraft": "0.6.0",
    "lightcraft": "0.6.0",
}


def _load_doctor(project_root: Path):
    module_path = project_root / "src" / "ion_creative" / "runtime" / "doctor.py"
    assert module_path.is_file(), "Runtime doctor is not implemented"
    from ion_creative.runtime.doctor import doctor_engine

    return doctor_engine


def _fake_cli(runtime_root: Path, engine: str, output: str, exit_code: int = 0) -> Path:
    version = EXPECTED_VERSIONS[engine]
    cli = runtime_root / engine / version / "bin" / f"{engine}-cli"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {exit_code}\n", encoding="utf-8")
    cli.chmod(0o755)
    return cli


def test_doctor_reports_missing_cli(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    doctor_engine = _load_doctor(project_root)
    runtime_root = tmp_path / "runtimes"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))

    status = doctor_engine("photocraft")

    assert status.healthy is False
    assert status.error_code == "NOT_INSTALLED"


def test_doctor_reports_new_pinned_engine_as_not_installed(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    doctor_engine = _load_doctor(project_root)
    runtime_root = tmp_path / "runtimes"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))

    status = doctor_engine("vectorcraft")

    assert status.engine == "vectorcraft"
    assert status.version == "0.10.0"
    assert status.healthy is False
    assert status.error_code == "NOT_INSTALLED"


def test_doctor_reports_cli_version_command_failure(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    doctor_engine = _load_doctor(project_root)
    runtime_root = tmp_path / "runtimes"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))
    _fake_cli(runtime_root, "photocraft", "photocraft-cli 0.5.0", exit_code=3)

    status = doctor_engine("photocraft")

    assert status.healthy is False
    assert status.error_code == "CLI_VERSION_FAILED"


def test_doctor_rejects_manifest_executable_version_mismatch(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    doctor_engine = _load_doctor(project_root)
    runtime_root = tmp_path / "runtimes"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))
    _fake_cli(runtime_root, "photocraft", "photocraft-cli 9.9.9")

    status = doctor_engine("photocraft")

    assert status.healthy is False
    assert status.error_code == "VERSION_MISMATCH"
    assert status.observed_version == "9.9.9"


def test_doctor_accepts_exact_versioned_cli(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    doctor_engine = _load_doctor(project_root)
    runtime_root = tmp_path / "runtimes"
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))
    _fake_cli(runtime_root, "photocraft", "PhotoCraft CLI v0.5.0")

    status = doctor_engine("photocraft")

    assert status.healthy is True
    assert status.version == "0.5.0"
    assert status.observed_version == "0.5.0"
