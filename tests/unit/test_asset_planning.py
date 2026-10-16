import asyncio
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def _png_bytes(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_payload = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = struct.pack(">I", len(ihdr_payload)) + b"IHDR" + ihdr_payload
    ihdr += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_payload) & 0xFFFFFFFF)
    return signature + ihdr


def _invoke_tool(name: str, arguments: dict[str, object]):
    from mcp import Client

    from ion_creative.server import create_server

    async def invoke():
        async with Client(create_server()) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(invoke()).structured_content


def test_inspect_asset_reports_raster_dimensions_and_suggested_path(tmp_path: Path) -> None:
    from ion_creative.project import create_project

    project = create_project(tmp_path / "portrait-project")
    source = project.inputs / "portrait.png"
    source.write_bytes(_png_bytes(1200, 1600))
    source_before = source.read_bytes()

    result = _invoke_tool(
        "inspect_asset",
        {"asset_path": "portrait.png", "project_root": str(project.root)},
    )

    assert result["status"] == "SUCCESS"
    assert result["media_type"] == "image"
    assert result["format"] == "png"
    assert result["width"] == 1200
    assert result["height"] == 1600
    assert result["suggested_engine_path"] == ["photocraft", "lightcraft"]
    assert source.read_bytes() == source_before


def test_inspect_asset_rejects_files_outside_the_project(tmp_path: Path) -> None:
    from ion_creative.project import create_project

    project = create_project(tmp_path / "safe-project")
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_bytes(32, 32))

    result = _invoke_tool(
        "inspect_asset",
        {"asset_path": str(outside), "project_root": str(project.root)},
    )

    assert result["status"] == "FAILED"
    assert result["errors"][0]["code"] == "PROJECT_PATH_REJECTED"


@pytest.mark.parametrize(
    ("filename", "content", "media_type", "format", "extra"),
    [
        (
            "clip.mp4",
            b"disposable video container",
            "video",
            "mp4",
            {"suggested_engine_path": ["filmcraft", "effectcraft"]},
        ),
        (
            "voice.mp3",
            b"disposable audio container",
            "audio",
            "mp3",
            {"suggested_engine_path": ["filmcraft"]},
        ),
        (
            "mark.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480"/>',
            "vector",
            "svg",
            {"width": 640, "height": 480, "suggested_engine_path": ["vectorcraft"]},
        ),
        (
            "brief.pdf",
            b"%PDF-1.4\n1 0 obj <</Type /Page>>\n2 0 obj <</Type /Page>>",
            "pdf",
            "pdf",
            {"page_count": 2, "suggested_engine_path": ["pdfcraft"]},
        ),
    ],
)
def test_inspect_asset_handles_other_supported_media(
    tmp_path: Path,
    filename: str,
    content: bytes,
    media_type: str,
    format: str,
    extra: dict[str, object],
) -> None:
    from ion_creative.project import create_project

    project = create_project(tmp_path / filename.replace(".", "-"))
    source = project.inputs / filename
    source.write_bytes(content)

    result = _invoke_tool(
        "inspect_asset",
        {"asset_path": filename, "project_root": str(project.root)},
    )

    assert result["status"] == "SUCCESS"
    assert result["media_type"] == media_type
    assert result["format"] == format
    for field, value in extra.items():
        assert result[field] == value


def test_inspect_asset_uses_ffprobe_metadata_for_video(tmp_path: Path, monkeypatch) -> None:
    from ion_creative import asset_planning
    from ion_creative.project import create_project

    project = create_project(tmp_path / "probe-project")
    (project.inputs / "clip.mp4").write_bytes(b"disposable video")
    probe = {
        "streams": [
            {"codec_type": "video", "width": 1080, "height": 1920, "duration": "2.5"},
            {"codec_type": "audio", "duration": "2.5"},
        ],
        "format": {"format_name": "mov,mp4,m4a", "duration": "2.5"},
    }
    monkeypatch.setattr(asset_planning.shutil, "which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        asset_planning.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(probe)),
    )

    result = _invoke_tool(
        "inspect_asset",
        {"asset_path": "clip.mp4", "project_root": str(project.root)},
    )

    assert result["media_type"] == "video"
    assert result["duration_seconds"] == 2.5
    assert result["width"] == 1080
    assert result["height"] == 1920


def test_inspect_asset_uses_ffprobe_dimensions_for_extended_image_formats(
    tmp_path: Path, monkeypatch
) -> None:
    from ion_creative import asset_planning
    from ion_creative.project import create_project

    project = create_project(tmp_path / "extended-image-project")
    (project.inputs / "portrait.avif").write_bytes(b"disposable image")
    monkeypatch.setattr(
        asset_planning,
        "_probe_media",
        lambda path: {"media_type": "video", "width": 900, "height": 1200},
    )

    result = _invoke_tool(
        "inspect_asset",
        {"asset_path": "portrait.avif", "project_root": str(project.root)},
    )

    assert result["media_type"] == "image"
    assert result["width"] == 900
    assert result["height"] == 1200


@pytest.mark.parametrize(
    ("intent", "workflow_id", "engines"),
    [
        (
            "Create a personal brand portrait",
            "personal_brand_portrait",
            ["photocraft", "lightcraft"],
        ),
        ("Assemble a short social video reel", "social_video", ["filmcraft", "effectcraft"]),
        ("Make a vector logo card", "vector_graphic", ["vectorcraft"]),
        ("Design a single-page layout", "design_layout", ["designcraft"]),
        ("Inspect and export this PDF", "pdf_ops", ["pdfcraft"]),
    ],
)
def test_plan_workflow_maps_common_intents_deterministically(
    intent: str, workflow_id: str, engines: list[str]
) -> None:
    result = _invoke_tool("plan_workflow", {"intent": intent})

    assert result["status"] == "PLANNED"
    assert result["workflow_id"] == workflow_id
    assert result["recommended_engines"] == engines
    assert result["candidate_capability_sequence"]
    assert all(
        item["engine"] in engines and item["live_discovery_required"] is True
        for item in result["candidate_capability_sequence"]
    )
    assert result["preview_checkpoints"]
    assert result["expected_outputs"]


def test_plan_workflow_uses_asset_context_when_intent_is_generic(tmp_path: Path) -> None:
    from ion_creative.project import create_project

    project = create_project(tmp_path / "plan-project")
    source = project.inputs / "clip.mp4"
    source.write_bytes(b"disposable video container")

    result = _invoke_tool(
        "plan_workflow",
        {
            "intent": "Help me improve this",
            "asset_paths": ["clip.mp4"],
            "project_root": str(project.root),
        },
    )

    assert result["workflow_id"] == "social_video"
    assert result["asset_context"][0]["media_type"] == "video"


def test_plan_workflow_reports_unsupported_intents_without_guessing() -> None:
    result = _invoke_tool("plan_workflow", {"intent": "transcribe a spreadsheet"})

    assert result["status"] == "UNSUPPORTED_INTENT"
    assert result["workflow_id"] is None
    assert result["candidate_capability_sequence"] == []
