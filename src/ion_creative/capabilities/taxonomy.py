"""Deterministic normalization rules for live Craft tool metadata."""

from __future__ import annotations

import re
from typing import Any

CAPABILITY_CATEGORIES = (
    "document",
    "import/export",
    "crop/resize",
    "color/develop",
    "retouch",
    "text/vector",
    "layers/composition",
    "effects/motion",
    "timeline/editing",
    "audio",
    "layout",
    "pdf",
    "utility",
)


def normalized_capability_id(engine: str, raw_name: str) -> str:
    """Create a stable engine-scoped ID from a live MCP tool name."""
    engine_slug = re.sub(r"[^a-z0-9]+", "_", engine.casefold()).strip("_")
    name_slug = re.sub(r"[^a-z0-9]+", "_", raw_name.casefold()).strip("_")
    if not engine_slug or not name_slug:
        raise ValueError("Capability IDs require non-empty engine and upstream names")
    return f"{engine_slug}.{name_slug}"


def capability_category(raw_name: str, description: str = "") -> str:
    """Map live names and descriptions into the small Ion capability taxonomy."""
    text = f"{raw_name} {description}".casefold().replace("_", " ").replace(".", " ")
    rules = (
        (("pdf", "acroform", "xfa"), "pdf"),
        (("audio", "sound", "volume", "fade"), "audio"),
        (("timeline", "sequence", "clip", "track", "video"), "timeline/editing"),
        (("import", "export", "save as", "write file"), "import/export"),
        (("crop", "resize", "straighten", "rotate image"), "crop/resize"),
        (
            ("exposure", "contrast", "saturation", "white balance", "color", "colour", "curve", "grade", "highlight", "shadow"),
            "color/develop",
        ),
        (("retouch", "clone", "heal", "blemish", "remove object"), "retouch"),
        (("text", "font", "typography", "vector", "path", "stroke", "fill", "artboard"), "text/vector"),
        (("layout", "page", "spread", "margin", "gutter", "column", "frame"), "layout"),
        (("layer", "group", "composition", "blend", "opacity", "mask"), "layers/composition"),
        (("effect", "filter", "blur", "sharpen", "motion", "keyframe", "transform"), "effects/motion"),
        (("document", "project", "open", "create", "close", "inspect"), "document"),
    )
    for keywords, category in rules:
        if any(keyword in text for keyword in keywords):
            return category
    return "utility"


def summarize_argument_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep useful argument shape without copying every upstream schema detail."""
    required_value = schema.get("required", [])
    required = [value for value in required_value if isinstance(value, str)] if isinstance(required_value, list) else []
    properties_value = schema.get("properties", {})
    properties: dict[str, dict[str, Any]] = {}
    if isinstance(properties_value, dict):
        for name, definition in properties_value.items():
            if not isinstance(name, str) or not isinstance(definition, dict):
                continue
            summary: dict[str, Any] = {}
            for key in ("type", "description", "enum", "default"):
                value = definition.get(key)
                if key in definition and (
                    isinstance(value, (str, int, float, bool, list))
                    or value is None and key == "default"
                ):
                    summary[key] = value
            properties[name] = summary
    return {"required": required, "properties": properties}
