"""Installed-runtime image workflow acceptance test using disposable media."""

import asyncio
import binascii
import hashlib
import struct
import zlib
from pathlib import Path

import pytest
from mcp import Client

from ion_creative.project import create_project, sha256_file
from ion_creative.recipes.image_smoke import run_image_smoke
from ion_creative.server import create_server


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def _fixture_png(width: int = 320, height: int = 200) -> bytes:
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend(((x * 255) // width, (y * 255) // height, (x + y) % 256))
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + _png_chunk(b"IEND", b"")
    )


def _assert_png(path: Path) -> None:
    data = path.read_bytes()
    assert len(data) > 32
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert width > 0 and height > 0


@pytest.mark.acceptance
def test_installed_photocraft_image_preview_review_and_export(tmp_path: Path) -> None:
    project = create_project(tmp_path / "ion-image-acceptance")
    source = project.inputs / "disposable-gradient.png"
    source.write_bytes(_fixture_png())
    source_hash_before = hashlib.sha256(source.read_bytes()).hexdigest()

    result = asyncio.run(run_image_smoke(project, source))

    assert result.status == "READY_FOR_REVIEW", result.to_dict()
    assert result.source_hash_before == source_hash_before
    assert result.source_hash_after == source_hash_before
    assert len(result.preview_paths) == 1
    assert len(result.candidate_paths) == 1
    assert Path(result.report_path).is_file()
    preview = Path(result.preview_paths[0])
    candidate = Path(result.candidate_paths[0])
    _assert_png(preview)
    _assert_png(candidate)
    assert candidate.read_bytes() != source.read_bytes()
    assert sha256_file(source) == source_hash_before

    async def review_and_export() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        async with Client(create_server()) as client:
            preview_result = await client.call_tool(
                "preview",
                {"project_root": str(project.root), "artifact_path": str(candidate)},
            )
            finalize_result = await client.call_tool(
                "finalize",
                {"project_root": str(project.root), "artifact_path": str(candidate)},
            )
            export_result = await client.call_tool(
                "export",
                {
                    "project_root": str(project.root),
                    "artifact_path": str(candidate),
                    "output_name": "image-smoke.png",
                },
            )
        assert not preview_result.is_error
        assert not finalize_result.is_error
        assert not export_result.is_error
        return (
            preview_result.structured_content,
            finalize_result.structured_content,
            export_result.structured_content,
        )

    preview_result, finalize_result, export_result = asyncio.run(review_and_export())
    assert preview_result["status"] == "SUCCESS"
    assert finalize_result["status"] == "FINALIZED"
    assert export_result["status"] == "EXPORTED"
    _assert_png(Path(preview_result["preview_paths"][0]))
    exported = Path(export_result["output_paths"][0])

    _assert_png(exported)
    assert exported.read_bytes() == candidate.read_bytes()
    assert sha256_file(source) == source_hash_before
