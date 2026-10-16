import pytest


@pytest.mark.acceptance
def test_all_installed_craft_desktop_launchers_verify() -> None:
    from ion_creative.desktop.launchers import verify_launchers

    results = verify_launchers()

    assert len(results) == 7
    assert all(result["verified"] for result in results), results
