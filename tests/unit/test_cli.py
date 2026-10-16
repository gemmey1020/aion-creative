from pathlib import Path
from types import SimpleNamespace


def test_runtime_install_requires_engine_or_all(capsys) -> None:
    project_root = Path(__file__).resolve().parents[2]
    cli_path = project_root / "src" / "ion_creative" / "cli.py"
    assert cli_path.is_file(), "Ion CLI is not implemented"
    from ion_creative.cli import main

    try:
        main(["runtime", "install"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("install without an engine or --all should be rejected")

    assert "choose one engine or --all" in capsys.readouterr().err


def test_runtime_install_all_dispatches_each_pinned_engine(monkeypatch, capsys) -> None:
    project_root = Path(__file__).resolve().parents[2]
    cli_path = project_root / "src" / "ion_creative" / "cli.py"
    assert cli_path.is_file(), "Ion CLI is not implemented"
    from ion_creative import cli

    calls: list[tuple[str, bool]] = []

    def fake_install(spec, *, force: bool = False):
        calls.append((spec.engine, force))
        return SimpleNamespace(healthy=True, to_dict=lambda: {"engine": spec.engine, "healthy": True})

    monkeypatch.setattr(cli, "install_runtime", fake_install)

    exit_code = cli.main(["runtime", "install", "--all"])

    assert exit_code == 0
    assert calls == [
        ("photocraft", False),
        ("filmcraft", False),
        ("effectcraft", False),
        ("lightcraft", False),
        ("vectorcraft", False),
        ("designcraft", False),
        ("pdfcraft", False),
    ]
    assert '"healthy": true' in capsys.readouterr().out


def test_versions_lists_every_pinned_engine(monkeypatch, capsys) -> None:
    from ion_creative import cli

    monkeypatch.setattr(
        cli,
        "doctor_engine",
        lambda engine: SimpleNamespace(observed_version=None, healthy=False),
    )

    assert cli.main(["versions"]) == 0

    output = capsys.readouterr().out
    assert '"vectorcraft"' in output
    assert '"pinned_version": "0.10.0"' in output
    assert '"designcraft"' in output
    assert '"pdfcraft"' in output


def test_doctor_lists_new_engines_as_not_installed(tmp_path, monkeypatch, capsys) -> None:
    import json

    from ion_creative import cli

    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(tmp_path / "runtimes"))

    assert cli.main(["doctor"]) == 1

    statuses = json.loads(capsys.readouterr().out)
    by_engine = {status["engine"]: status for status in statuses}
    assert set(by_engine) == {
        "photocraft",
        "filmcraft",
        "effectcraft",
        "lightcraft",
        "vectorcraft",
        "designcraft",
        "pdfcraft",
    }
    assert by_engine["vectorcraft"]["error_code"] == "NOT_INSTALLED"


def test_desktop_launcher_commands_are_available(monkeypatch, capsys) -> None:
    import json

    from ion_creative import cli

    expected = [{"engine": "photocraft", "installed": True, "verified": True}]
    monkeypatch.setattr(cli, "install_launchers", lambda: expected, raising=False)
    monkeypatch.setattr(cli, "list_launchers", lambda: expected, raising=False)
    monkeypatch.setattr(cli, "verify_launchers", lambda: expected, raising=False)

    for command in ("install-launchers", "list-launchers", "verify-launchers"):
        try:
            exit_code = cli.main(["desktop", command])
        except SystemExit as exc:
            exit_code = exc.code
        assert exit_code == 0
        assert json.loads(capsys.readouterr().out) == expected
