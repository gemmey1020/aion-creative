import asyncio
import json
from pathlib import Path

import pytest

from ion_creative.adapters.designcraft import DesignCraftAdapter
from ion_creative.adapters.effectcraft import EffectCraftAdapter
from ion_creative.adapters.filmcraft import FilmCraftAdapter
from ion_creative.adapters.lightcraft import LightCraftAdapter
from ion_creative.adapters.pdfcraft import PdfCraftAdapter
from ion_creative.adapters.photocraft import PhotoCraftAdapter
from ion_creative.adapters.vectorcraft import VectorCraftAdapter


@pytest.mark.acceptance
def test_all_pinned_craft_mcp_servers_initialize_and_discover_live_tools(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = Path.home() / ".local" / "share" / "ion-creative" / "runtimes"
    isolated_home = tmp_path / "isolated-home"
    isolated_config = isolated_home / ".config"
    isolated_data = isolated_home / ".local" / "share"
    isolated_cache = isolated_home / ".cache"
    isolated_runtime = isolated_home / ".runtime"
    for directory in (isolated_config, isolated_data, isolated_cache, isolated_runtime):
        directory.mkdir(parents=True, exist_ok=True)
    isolated_runtime.chmod(0o700)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(isolated_data))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_cache))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(isolated_runtime))
    monkeypatch.setenv("ION_CREATIVE_RUNTIME_ROOT", str(runtime_root))

    photo_project = tmp_path / "photocraft-project"
    photo_project.mkdir()
    vector_project = tmp_path / "vectorcraft-project"
    vector_project.mkdir()
    design_project = tmp_path / "designcraft-project"
    design_project.mkdir()
    pdf_project = tmp_path / "pdfcraft-project"
    pdf_project.mkdir()
    adapters = [
        PhotoCraftAdapter(photo_project),
        FilmCraftAdapter(demo=True),
        EffectCraftAdapter(demo=True),
        LightCraftAdapter(demo=True),
        VectorCraftAdapter(vector_project),
        DesignCraftAdapter(design_project),
        PdfCraftAdapter(pdf_project),
    ]

    async def inspect_engines():
        results = {}
        for adapter in adapters:
            health = await adapter.health()
            results[health.engine] = health
        return results

    health_by_engine = asyncio.run(inspect_engines())
    assert set(health_by_engine) == {
        "photocraft",
        "filmcraft",
        "effectcraft",
        "lightcraft",
        "vectorcraft",
        "designcraft",
        "pdfcraft",
    }
    for health in health_by_engine.values():
        assert health.healthy is True, health.to_dict()
        assert health.mcp_initialized is True
        assert health.capability_report is not None
        assert health.capability_report.status == "HEALTHY"

    project_root = Path(__file__).resolve().parents[2]
    artifact_path = project_root / "work" / "acceptance" / "craft-tools.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            {engine: health.to_dict() for engine, health in health_by_engine.items()},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                engine: {
                    "version": health.version,
                    "tools": len(health.capabilities),
                    "bindings": health.capability_report.tool_bindings,
                }
                for engine, health in health_by_engine.items()
            },
            sort_keys=True,
        )
    )
    print(f"live tool schemas: {artifact_path}")
