"""Credential-state reporting must never claim generation support."""

import json


def test_artcraft_gate_reports_not_configured_without_exposing_credentials(monkeypatch) -> None:
    from ion_creative.adapters.artcraft import ArtCraftAdapter

    monkeypatch.delenv("ARTCRAFT_API_KEY", raising=False)
    status = ArtCraftAdapter().configuration_status()

    assert status == {
        "engine": "artcraft",
        "status": "NOT_CONFIGURED",
        "credentials_detected": False,
        "generation_supported": False,
        "message": "ArtCraft API credentials are not configured; generation is unavailable.",
    }


def test_artcraft_gate_redacts_configured_credentials(monkeypatch) -> None:
    from ion_creative.adapters.artcraft import ArtCraftAdapter

    fixture_value = "artcraft_api_test_secret_value"
    monkeypatch.setenv("ARTCRAFT_API_KEY", fixture_value)
    status = ArtCraftAdapter().configuration_status()

    assert status["status"] == "CONFIGURED"
    assert status["credentials_detected"] is True
    assert status["generation_supported"] is False
    assert fixture_value not in json.dumps(status)


def test_artcraft_cli_status_reports_optional_gate(monkeypatch, capsys) -> None:
    from ion_creative import cli

    monkeypatch.delenv("ARTCRAFT_API_KEY", raising=False)

    assert cli.main(["artcraft", "status"]) == 0

    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "NOT_CONFIGURED"
    assert status["generation_supported"] is False
