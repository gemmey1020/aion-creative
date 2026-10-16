"""Installed-runtime video workflow acceptance using FilmCraft's disposable demo."""

import asyncio
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from mcp import Client

from ion_creative.project import create_project
from ion_creative.server import create_server


def _assert_png(path: Path) -> None:
    data = path.read_bytes()
    assert len(data) > 32
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert width > 0 and height > 0


def _assert_mp4(path: Path) -> None:
    assert path.is_file() and path.stat().st_size > 64
    with path.open("rb") as stream:
        stream.seek(4)
        assert stream.read(4) == b"ftyp"
    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None, "ffprobe is required to verify the exported MP4"
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name",
            "-show_entries",
            "stream=codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    stream = next(item for item in payload["streams"] if item.get("codec_type", "video") == "video")
    assert stream["codec_name"] == "h264"
    assert stream["width"] == 640 and stream["height"] == 360
    assert float(payload["format"]["duration"]) == 3.0


@pytest.mark.acceptance
def test_installed_filmcraft_preview_effect_frame_and_mp4_export(tmp_path: Path) -> None:
    project = create_project(tmp_path / "ion-video-acceptance")

    async def run_public_workflow():
        async with Client(create_server()) as client:
            edit = await client.call_tool(
                "edit_video",
                {
                    "project_root": str(project.root),
                    "source_paths": [],
                    "instructions": "smoke",
                },
            )
            edited = edit.structured_content
            assert edited["status"] in {"READY_FOR_REVIEW", "PARTIAL_SUCCESS"}, edited
            candidate = Path(edited["output_paths"][0])
            preview = await client.call_tool(
                "preview",
                {"project_root": str(project.root), "artifact_path": str(candidate)},
            )
            reviewed = preview.structured_content
            assert reviewed["status"] == "SUCCESS", reviewed
            _assert_png(Path(reviewed["preview_paths"][0]))
            finalized = await client.call_tool(
                "finalize",
                {"project_root": str(project.root), "artifact_path": str(candidate)},
            )
            exported = await client.call_tool(
                "export",
                {
                    "project_root": str(project.root),
                    "artifact_path": str(candidate),
                    "output_name": "video-smoke.mp4",
                },
            )
        assert finalized.structured_content["status"] == "FINALIZED"
        assert exported.structured_content["status"] == "EXPORTED"
        return edited, reviewed, Path(exported.structured_content["output_paths"][0])

    edited, reviewed, exported = asyncio.run(run_public_workflow())
    candidate = Path(edited["output_paths"][0])
    _assert_mp4(candidate)
    _assert_mp4(exported)
    assert reviewed["engines"] == ["filmcraft", "effectcraft"]
    report = json.loads((project.ion / "video-smoke-report.json").read_text(encoding="utf-8"))
    assert report["exact_versions"] == {
        "photocraft": "0.5.0",
        "filmcraft": "0.7.2",
        "effectcraft": "0.6.0",
        "lightcraft": "0.6.0",
        "vectorcraft": "0.10.0",
        "designcraft": "0.6.0",
        "pdfcraft": "0.7.1",
    }
    assert report["capabilities"]["filmcraft"]["timeline_command"]
    assert report["capabilities"]["filmcraft"]["export_command"]
    effect = report["capabilities"]["effectcraft"]
    if effect["healthy"]:
        frame = Path(effect["frame_path"])
        assert frame.is_file()
        _assert_png(frame)
    assert not list(project.inputs.iterdir())
