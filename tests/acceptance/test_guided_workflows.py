"""Live, disposable acceptance proofs for the full guided workflow surface."""

from __future__ import annotations

import asyncio
import binascii
import hashlib
import json
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from mcp import Client

from ion_creative.project import create_project
from ion_creative.server import create_server


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)


def _portrait_png(width: int = 400, height: int = 500) -> bytes:
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            shade = (x * 35 // width) + (y * 20 // height)
            row.extend((96 + shade, 116 + shade, 142 + shade))
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(b"".join(rows))) + _chunk(b"IEND", b"")


def _assert_png(path: Path) -> None:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 32
    width, height = struct.unpack(">II", data[16:24])
    assert width > 0 and height > 0


async def _run_workflow(arguments: dict[str, object]) -> dict[str, object]:
    async with Client(create_server()) as client:
        response = await client.call_tool("run_workflow", arguments)
    assert not response.is_error
    return response.structured_content


@pytest.mark.acceptance
def test_guided_personal_brand_portrait_keeps_source_immutable(tmp_path: Path) -> None:
    project = create_project(tmp_path / "portrait-guided")
    source = project.inputs / "portrait.png"
    source.write_bytes(_portrait_png())
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    result = asyncio.run(_run_workflow({"workflow_id": "personal_brand_portrait", "project_root": str(project.root), "source_paths": [source.name]}))

    assert result["status"] == "READY_FOR_REVIEW", result
    assert result["source_hashes"][str(source)] == before
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert len(result["preview_paths"]) >= 2
    for path in result["preview_paths"] + result["output_paths"]:
        _assert_png(Path(path))
    assert result["details"]["aspect_ratio"] == "4:5"


@pytest.mark.acceptance
def test_public_ion_personal_brand_pipeline_finalizes_and_exports(tmp_path: Path) -> None:
    project = create_project(tmp_path / "personal-brand-public")
    source = project.inputs / "portrait.png"
    source.write_bytes(_portrait_png())
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    async def run_public_path() -> tuple[dict[str, object], ...]:
        async with Client(create_server()) as client:
            inspected = await client.call_tool("inspect_asset", {"asset_path": "inputs/portrait.png", "project_root": str(project.root)})
            planned = await client.call_tool("plan_workflow", {"intent": "Create a Jemy personal-brand portrait", "asset_paths": ["inputs/portrait.png"], "project_root": str(project.root)})
            produced = await client.call_tool("run_workflow", {"workflow_id": "personal_brand_portrait", "project_root": str(project.root), "source_paths": ["portrait.png"]})
            workflow = produced.structured_content
            assert workflow["status"] == "READY_FOR_REVIEW", workflow
            candidate = workflow["output_paths"][0]
            preview = await client.call_tool("preview", {"project_root": str(project.root), "artifact_path": candidate})
            assert preview.structured_content["status"] == "SUCCESS", preview.structured_content
            finalized = await client.call_tool("finalize", {"project_root": str(project.root), "artifact_path": candidate})
            assert finalized.structured_content["status"] == "FINALIZED", finalized.structured_content
            exported = await client.call_tool("export", {"project_root": str(project.root), "artifact_path": candidate, "output_name": "personal-brand.png"})
        return inspected.structured_content, planned.structured_content, workflow, preview.structured_content, finalized.structured_content, exported.structured_content

    inspected, planned, workflow, preview, finalized, exported = asyncio.run(run_public_path())

    assert inspected["status"] == "SUCCESS" and inspected["media_type"] == "image"
    assert planned["status"] == "PLANNED" and planned["workflow_id"] == "personal_brand_portrait"
    assert workflow["source_hashes"][str(source)] == source_hash
    assert finalized["status"] == "FINALIZED"
    assert exported["status"] == "EXPORTED"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    _assert_png(Path(preview["preview_paths"][0]))
    candidate_path = Path(exported["output_paths"][0])
    _assert_png(candidate_path)
    assert candidate_path.read_bytes() == Path(workflow["output_paths"][0]).read_bytes()


@pytest.mark.acceptance
def test_guided_social_video_assembles_real_disposable_clips(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed; real media import cannot be proved")
    project = create_project(tmp_path / "social-video-guided")
    sources = [project.inputs / "blue.mp4", project.inputs / "orange.mp4"]
    for source, color in zip(sources, ("blue", "orange"), strict=True):
        completed = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=360x640:r=24:d=1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(source)], capture_output=True, text=True, timeout=30, check=False)
        assert completed.returncode == 0, completed.stderr
    before = {str(source): hashlib.sha256(source.read_bytes()).hexdigest() for source in sources}

    result = asyncio.run(_run_workflow({"workflow_id": "social_video", "project_root": str(project.root), "source_paths": [source.name for source in sources]}))

    assert result["status"] == "READY_FOR_REVIEW", result
    assert result["source_hashes"] == before
    assert {str(source): hashlib.sha256(source.read_bytes()).hexdigest() for source in sources} == before
    _assert_png(Path(result["preview_paths"][0]))
    candidate = Path(result["output_paths"][0])
    assert candidate.read_bytes()[4:8] == b"ftyp"
    assert candidate.stat().st_size > 64

    async def preview_candidate() -> dict[str, object]:
        async with Client(create_server()) as client:
            response = await client.call_tool(
                "preview",
                {"project_root": str(project.root), "artifact_path": str(candidate)},
            )
        assert not response.is_error
        return response.structured_content

    public_preview = asyncio.run(preview_candidate())
    assert public_preview["status"] == "SUCCESS", public_preview
    assert public_preview["preview_paths"] == result["preview_paths"]
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "json", str(candidate)], capture_output=True, text=True, timeout=15, check=False)
        assert probe.returncode == 0, probe.stderr
        stream = next(item for item in json.loads(probe.stdout)["streams"] if item.get("codec_type", "video") == "video")
        assert stream["codec_name"] == "h264"
        assert (stream["width"], stream["height"]) == (1080, 1920)


@pytest.mark.acceptance
def test_guided_vector_graphic_renders_and_exports_svg(tmp_path: Path) -> None:
    project = create_project(tmp_path / "vector-guided")
    result = asyncio.run(_run_workflow({"workflow_id": "vector_graphic", "project_root": str(project.root), "brief": "JEMY / Ω — Creative Systems"}))

    assert result["status"] == "READY_FOR_REVIEW", result
    _assert_png(Path(result["preview_paths"][0]))
    output = Path(result["output_paths"][0])
    assert output.suffix == ".svg" and b"<svg" in output.read_bytes()[:2048].lower()


@pytest.mark.acceptance
def test_guided_design_layout_renders_and_exports_pdf(tmp_path: Path) -> None:
    project = create_project(tmp_path / "layout-guided")
    result = asyncio.run(_run_workflow({"workflow_id": "design_layout", "project_root": str(project.root), "brief": "Make creative systems practical"}))

    assert result["status"] == "READY_FOR_REVIEW", result
    _assert_png(Path(result["preview_paths"][0]))
    output = Path(result["output_paths"][0])
    assert output.suffix == ".pdf" and output.read_bytes().startswith(b"%PDF")


@pytest.mark.acceptance
def test_guided_pdf_ops_inspects_previews_and_saves_pdf(tmp_path: Path) -> None:
    project = create_project(tmp_path / "pdf-guided")
    result = asyncio.run(_run_workflow({"workflow_id": "pdf_ops", "project_root": str(project.root), "brief": "JEMY / OMEGA\n\nCreative work, systems thinking, and learning."}))

    assert result["status"] == "READY_FOR_REVIEW", result
    _assert_png(Path(result["preview_paths"][0]))
    output = Path(result["output_paths"][0])
    assert output.suffix == ".pdf" and output.read_bytes().startswith(b"%PDF")
    assert result["details"]["document"]
