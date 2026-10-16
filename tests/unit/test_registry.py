import json
from pathlib import Path

EXPECTED = {
    "photocraft": "0.5.0",
    "filmcraft": "0.7.2",
    "effectcraft": "0.6.0",
    "lightcraft": "0.6.0",
    "vectorcraft": "0.10.0",
    "designcraft": "0.6.0",
    "pdfcraft": "0.7.1",
}

EXPECTED_SHA256 = {
    "vectorcraft": "a55735a192877038c9b4c3069504f7a73a05433a48edddd8e94fc7d2995aa6fc",
    "designcraft": "abb661cd2fc53677d040fe0be3f241bbb8c0fad9619192a83109d53bd514bfb9",
    "pdfcraft": "bc7d651511273720f6a5ccbe0dd5b2c4ddccb6c576a0aff619f39d2c5667a315",
}


def test_registry_declares_exact_pins_and_linux_release_metadata() -> None:
    project_root = Path(__file__).resolve().parents[2]
    manifest_path = project_root / "config" / "runtimes.json"
    assert manifest_path.is_file(), "Pinned runtime registry is not implemented"

    from ion_creative.runtime.registry import load_runtime_specs

    specs = load_runtime_specs()
    assert {name: spec.version for name, spec in specs.items()} == EXPECTED

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for engine, version in EXPECTED.items():
        spec = specs[engine]
        entry = manifest[engine]
        assert spec.github_owner == "storytold"
        assert spec.github_repo == engine
        assert spec.tag == f"v{version}"
        assert spec.asset_pattern == f"{engine}-{version}-linux-x86_64.tar.gz"
        assert spec.desktop_binary == engine
        assert spec.cli_binary == f"{engine}-cli"
        assert entry["asset_pattern"] == spec.asset_pattern
        if engine in EXPECTED_SHA256:
            assert spec.artifact_sha256 == EXPECTED_SHA256[engine]
