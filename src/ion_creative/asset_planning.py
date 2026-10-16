"""Project-scoped media inspection and deterministic workflow recommendations."""

from __future__ import annotations

import json
import math
import mimetypes
import re
import shutil
import struct
import subprocess
from pathlib import Path

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".heic"}
_VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".m4v",
    ".avi",
    ".3gp",
    ".ogv",
    ".mpeg",
    ".mpg",
}
_AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".aiff",
    ".aif",
}
_VECTOR_EXTENSIONS = {".svg", ".eps", ".ai", ".emf", ".wmf"}
_FORMAT_ALIASES = {".jpg": "jpeg", ".tif": "tiff", ".aif": "aiff"}

_SUGGESTED_ENGINES = {
    "image": ["photocraft", "lightcraft"],
    "video": ["filmcraft", "effectcraft"],
    "audio": ["filmcraft"],
    "vector": ["vectorcraft"],
    "pdf": ["pdfcraft"],
}

_WORKFLOWS: dict[str, dict[str, object]] = {
    "personal_brand_portrait": {
        "engines": ["photocraft", "lightcraft"],
        "steps": [
            ("photocraft", "Import or open a working image copy"),
            ("photocraft", "Compose and apply restrained image corrections"),
            ("lightcraft", "Develop color and apply a measured final grade"),
            ("photocraft", "Prepare a social-ready image output"),
        ],
        "previews": ["After composition and corrections", "After the final color grade"],
        "outputs": ["Reviewable portrait preview", "Final PNG or JPEG after explicit finalize/export"],
    },
    "social_video": {
        "engines": ["filmcraft", "effectcraft"],
        "steps": [
            ("filmcraft", "Import and inspect the source media"),
            ("filmcraft", "Trim and assemble a short sequence"),
            ("effectcraft", "Add supported titles or motion treatments"),
            ("filmcraft", "Render and prepare a social video output"),
        ],
        "previews": ["After sequence assembly", "After titles or motion treatments"],
        "outputs": ["Reviewable frame or video preview", "Final MP4 after explicit finalize/export"],
    },
    "vector_graphic": {
        "engines": ["vectorcraft"],
        "steps": [
            ("vectorcraft", "Create or open a vector document and artboard"),
            ("vectorcraft", "Build the logo, icon, or vector card"),
            ("vectorcraft", "Render a preview and prepare a supported export"),
        ],
        "previews": ["After the first composition", "Before vector or raster export"],
        "outputs": ["Reviewable vector preview", "SVG or another supported vector export"],
    },
    "design_layout": {
        "engines": ["designcraft"],
        "steps": [
            ("designcraft", "Create a single-page document"),
            ("designcraft", "Arrange layout, typography, and visual elements"),
            ("designcraft", "Render the page and prepare a supported export"),
        ],
        "previews": ["After initial page layout", "Before final page export"],
        "outputs": ["Reviewable single-page preview", "Final page in a supported format"],
    },
    "pdf_ops": {
        "engines": ["pdfcraft"],
        "steps": [
            ("pdfcraft", "Inspect the document and its pages"),
            ("pdfcraft", "Apply only the requested supported page operation"),
            ("pdfcraft", "Prepare a PDF export for review"),
        ],
        "previews": ["After document inspection or page changes", "Before PDF export"],
        "outputs": ["PDF page inspection result", "Final PDF after explicit finalize/export"],
    },
}


def _read_prefix(path: Path, limit: int = 4096) -> bytes:
    with path.open("rb") as stream:
        return stream.read(limit)


def _jpeg_dimensions(path: Path) -> tuple[int, int] | None:
    start_of_frame = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"\xff\xd8":
                return None
            while marker_prefix := stream.read(1):
                if marker_prefix != b"\xff":
                    continue
                marker_bytes = stream.read(1)
                while marker_bytes == b"\xff":
                    marker_bytes = stream.read(1)
                if not marker_bytes:
                    return None
                marker = marker_bytes[0]
                if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                    continue
                length_bytes = stream.read(2)
                if len(length_bytes) != 2:
                    return None
                segment_length = int.from_bytes(length_bytes, "big")
                if segment_length < 2:
                    return None
                if marker in start_of_frame:
                    frame = stream.read(min(segment_length - 2, 8))
                    if len(frame) >= 5:
                        height = int.from_bytes(frame[1:3], "big")
                        width = int.from_bytes(frame[3:5], "big")
                        return width, height
                    return None
                stream.seek(segment_length - 2, 1)
    except OSError:
        return None
    return None


def _raster_dimensions(path: Path, file_format: str) -> tuple[int, int] | None:
    try:
        with path.open("rb") as stream:
            header = stream.read(32)
        if file_format == "png" and header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
            return struct.unpack(">II", header[16:24])
        if file_format == "gif" and header.startswith((b"GIF87a", b"GIF89a")) and len(header) >= 10:
            return struct.unpack("<HH", header[6:10])
        if file_format == "bmp" and header.startswith(b"BM") and len(header) >= 26:
            width, height = struct.unpack("<ii", header[18:26])
            return abs(width), abs(height)
        if file_format == "jpeg":
            return _jpeg_dimensions(path)
        if (
            file_format == "webp"
            and header.startswith(b"RIFF")
            and header[8:12] == b"WEBP"
            and header[12:16] == b"VP8X"
            and len(header) >= 30
        ):
            width = int.from_bytes(header[24:27], "little") + 1
            height = int.from_bytes(header[27:30], "little") + 1
            return width, height
    except (OSError, struct.error):
        return None
    return None


def _svg_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        header = _read_prefix(path, 65536).decode("utf-8-sig", errors="ignore")
    except OSError:
        return None
    root_match = re.search(r"<svg\b([^>]*)>", header, flags=re.IGNORECASE | re.DOTALL)
    if root_match is None:
        return None
    attributes = root_match.group(1)
    values: dict[str, str] = {}
    for match in re.finditer(r"([\w:-]+)\s*=\s*(['\"])(.*?)\2", attributes, re.DOTALL):
        values[match.group(1).casefold()] = match.group(3).strip()

    def parse_length(value: str | None) -> int | None:
        if value is None:
            return None
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)(?:px)?\s*", value, flags=re.IGNORECASE)
        return round(float(match.group(1))) if match else None

    width = parse_length(values.get("width"))
    height = parse_length(values.get("height"))
    if width and height:
        return width, height
    view_box = values.get("viewbox")
    if view_box:
        parts = re.split(r"[\s,]+", view_box.strip())
        if len(parts) == 4:
            try:
                view_width, view_height = float(parts[2]), float(parts[3])
                if view_width > 0 and view_height > 0:
                    return round(view_width), round(view_height)
            except ValueError:
                pass
    return None


def _probe_media(path: Path) -> dict[str, object] | None:
    executable = shutil.which("ffprobe")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        streams = []
    stream_types = {item.get("codec_type") for item in streams if isinstance(item, dict)}
    media_type = "video" if "video" in stream_types else "audio" if "audio" in stream_types else None
    duration = None
    for candidate in [*streams, payload.get("format", {})]:
        if isinstance(candidate, dict) and candidate.get("duration") is not None:
            try:
                parsed_duration = float(candidate["duration"])
                if math.isfinite(parsed_duration) and parsed_duration >= 0:
                    duration = parsed_duration
                    break
            except (TypeError, ValueError):
                continue
    video_stream = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    width = video_stream.get("width") if video_stream else None
    height = video_stream.get("height") if video_stream else None
    format_details = payload.get("format", {})
    format_name = format_details.get("format_name") if isinstance(format_details, dict) else None
    return {
        "media_type": media_type,
        "duration_seconds": duration,
        "width": width if isinstance(width, int) else None,
        "height": height if isinstance(height, int) else None,
        "format": format_name.split(",", 1)[0] if isinstance(format_name, str) else None,
    }


def _pdf_page_count(path: Path) -> int | None:
    executable = shutil.which("pdfinfo")
    if executable is not None:
        try:
            result = subprocess.run(
                [executable, str(path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
            if result.returncode == 0:
                match = re.search(r"^Pages:\s*(\d+)\s*$", result.stdout, re.MULTILINE)
                if match:
                    return int(match.group(1))
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            return None
        contents = path.read_bytes()
    except OSError:
        return None
    pages = re.findall(rb"/Type\s*/Page(?!s)\b", contents)
    return len(pages) or None


def _detect_media_type(path: Path, header: bytes) -> tuple[str, str | None]:
    suffix = path.suffix.casefold()
    if header.startswith(b"%PDF-") or suffix == ".pdf":
        return "pdf", "pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image", "jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image", "gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image", "webp"
    if suffix in _VECTOR_EXTENSIONS:
        return "vector", _FORMAT_ALIASES.get(suffix, suffix.removeprefix("."))
    if suffix in _IMAGE_EXTENSIONS:
        return "image", _FORMAT_ALIASES.get(suffix, suffix.removeprefix("."))
    if suffix in _VIDEO_EXTENSIONS:
        return "video", _FORMAT_ALIASES.get(suffix, suffix.removeprefix("."))
    if suffix in _AUDIO_EXTENSIONS:
        return "audio", _FORMAT_ALIASES.get(suffix, suffix.removeprefix("."))
    if suffix == ".svg" or re.search(rb"<svg\b", header, re.IGNORECASE):
        return "vector", "svg"
    if header.startswith(b"%!PS"):
        return "vector", "eps"
    guessed_type, _ = mimetypes.guess_type(path.name)
    if guessed_type:
        if guessed_type.startswith("image/"):
            return "image", suffix.removeprefix(".") or guessed_type.partition("/")[2]
        if guessed_type.startswith("video/"):
            return "video", suffix.removeprefix(".") or guessed_type.partition("/")[2]
        if guessed_type.startswith("audio/"):
            return "audio", suffix.removeprefix(".") or guessed_type.partition("/")[2]
    return "unknown", suffix.removeprefix(".") or None


def inspect_asset_file(path: Path) -> dict[str, object]:
    """Inspect one already-confined asset without modifying it."""
    header = _read_prefix(path)
    media_type, file_format = _detect_media_type(path, header)
    result: dict[str, object] = {
        "status": "SUCCESS" if media_type != "unknown" else "UNSUPPORTED",
        "tool": "inspect_asset",
        "message": "Asset metadata inspected." if media_type != "unknown" else "Asset type is not supported for inspection.",
        "asset_path": str(path),
        "media_type": media_type,
        "format": file_format or "unknown",
        "width": None,
        "height": None,
        "duration_seconds": None,
        "page_count": None,
        "suggested_engine_path": _SUGGESTED_ENGINES.get(media_type, []),
        "errors": [],
    }

    if media_type == "image":
        dimensions = _raster_dimensions(path, file_format or "")
        if dimensions:
            result["width"], result["height"] = dimensions
        else:
            probe = _probe_media(path)
            if probe:
                result["width"] = probe.get("width")
                result["height"] = probe.get("height")
    elif media_type == "vector" and file_format == "svg":
        dimensions = _svg_dimensions(path)
        if dimensions:
            result["width"], result["height"] = dimensions
    elif media_type == "pdf":
        result["page_count"] = _pdf_page_count(path)
    elif media_type in {"video", "audio"}:
        probe = _probe_media(path)
        if probe:
            detected_type = probe.get("media_type")
            if detected_type in {"video", "audio"}:
                media_type = detected_type
                result["media_type"] = detected_type
                result["suggested_engine_path"] = _SUGGESTED_ENGINES[detected_type]
            result["duration_seconds"] = probe.get("duration_seconds")
            result["width"] = probe.get("width")
            result["height"] = probe.get("height")
            if probe.get("format"):
                result["format"] = probe["format"]
    return result


def _has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    for keyword in keywords:
        if any("\u0600" <= char <= "\u06ff" for char in keyword):
            if keyword in text:
                return True
        elif re.search(rf"\b{re.escape(keyword)}\b", text):
            return True
    return False


def _classify_intent(intent: str, asset_context: list[dict[str, object]]) -> str | None:
    text = " ".join(intent.casefold().split())
    classifiers = (
        ("pdf_ops", ("pdf", "acrobat", "بي دي اف", "مستند")),
        (
            "social_video",
            ("social video", "short video", "video", "reel", "clip", "فيديو", "ريلز", "ريل"),
        ),
        (
            "personal_brand_portrait",
            (
                "personal brand",
                "portrait",
                "headshot",
                "profile photo",
                "صورة شخصية",
                "صورة بروفايل",
                "بروفايل",
            ),
        ),
        ("vector_graphic", ("vector", "logo", "svg", "icon", "فيكتور", "شعار", "لوجو")),
        (
            "design_layout",
            ("single page", "one page", "layout", "poster", "card", "design", "صفحة", "تصميم", "بوستر"),
        ),
    )
    for workflow_id, keywords in classifiers:
        if _has_keyword(text, keywords):
            return workflow_id
    media_types = {item.get("media_type") for item in asset_context}
    if "pdf" in media_types:
        return "pdf_ops"
    if "video" in media_types or "audio" in media_types:
        return "social_video"
    if "vector" in media_types:
        return "vector_graphic"
    if "image" in media_types:
        return "personal_brand_portrait"
    return None


def plan_workflow_data(intent: str, asset_context: list[dict[str, object]]) -> dict[str, object]:
    """Build a deterministic high-level plan without inventing Craft command IDs."""
    workflow_id = _classify_intent(intent, asset_context)
    if workflow_id is None:
        return {
            "status": "UNSUPPORTED_INTENT",
            "tool": "plan_workflow",
            "message": "No supported Ion workflow matches this intent or its assets.",
            "intent": intent,
            "workflow_id": None,
            "asset_context": asset_context,
            "recommended_engines": [],
            "candidate_capability_sequence": [],
            "preview_checkpoints": [],
            "expected_outputs": [],
            "qa_needed": False,
        }

    blueprint = _WORKFLOWS[workflow_id]
    engines = list(blueprint["engines"])
    steps = [
        {
            "engine": engine,
            "operation": operation,
            "live_discovery_required": True,
        }
        for engine, operation in blueprint["steps"]
    ]
    return {
        "status": "PLANNED",
        "tool": "plan_workflow",
        "message": "A deterministic workflow candidate is ready; resolve each operation against live Craft capabilities before execution.",
        "intent": intent,
        "workflow_id": workflow_id,
        "asset_context": asset_context,
        "recommended_engines": engines,
        "candidate_capability_sequence": steps,
        "preview_checkpoints": list(blueprint["previews"]),
        "expected_outputs": list(blueprint["outputs"]),
        "qa_needed": True,
    }
