"""Preview-first personal-brand portrait workflow using PhotoCraft and LightCraft."""

from __future__ import annotations

from ion_creative.adapters.base import PreviewRequest
from ion_creative.adapters.lightcraft import LightCraftAdapter
from ion_creative.adapters.photocraft import PhotoCraftAdapter
from ion_creative.asset_planning import inspect_asset_file
from ion_creative.mcp.stdio_client import MCPClientError
from ion_creative.project import IonProject, ProjectSafetyError, safe_project_path
from ion_creative.recipes.image_smoke import _is_png
from ion_creative.workflows.common import (
    WorkflowFailure,
    execute_live_command,
    failed,
    file_hashes,
    input_path,
    invoke_tool,
    payload,
    preview_bytes,
    project_relative,
    result,
    source_hashes_unchanged,
    write_preview,
)

WORKFLOW_ID = "personal_brand_portrait"


def _portrait_crop(width: int, height: int) -> tuple[int, int, int, int] | None:
    target = 4 / 5
    ratio = width / height
    if abs(ratio - target) <= 0.002:
        return None
    if ratio > target:
        crop_width = max(1, round(height * target))
        return (width - crop_width) // 2, 0, crop_width, height
    crop_height = max(1, round(width / target))
    return 0, (height - crop_height) // 2, width, crop_height


async def run(project: IonProject, source_path: str) -> dict[str, object]:
    source = input_path(project, source_path)
    inspected = inspect_asset_file(source)
    if inspected.get("media_type") != "image":
        return failed(WORKFLOW_ID, "ion", "inspect", "UNSUPPORTED_ASSET", "Portrait workflow requires one raster image.")
    width, height = inspected.get("width"), inspected.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or min(width, height) <= 0:
        return failed(
            WORKFLOW_ID,
            "ion",
            "inspect",
            "IMAGE_DIMENSIONS_UNAVAILABLE",
            "Portrait dimensions could not be read safely.",
        )
    before = file_hashes([source])
    engines: list[str] = []
    versions: dict[str, str] = {}
    stages: list[dict[str, str]] = []
    photo_session = None
    light_session = None
    try:
        photo = PhotoCraftAdapter(project.root)
        photo_health = await photo.health()
        if not photo_health.healthy:
            raise WorkflowFailure("photocraft", "health", "ENGINE_UNHEALTHY", "; ".join(photo_health.errors))
        engines.append("photocraft")
        versions["photocraft"] = photo_health.version
        photo_session = await photo.open_session()
        photo_report = photo_session.report

        await invoke_tool(
            photo_session,
            photo_report,
            "photocraft",
            "doc_open",
            {"path": source.relative_to(project.root).as_posix()},
        )
        stages.append({"engine": "photocraft", "stage": "open_source_copy", "status": "complete"})

        crop = _portrait_crop(width, height)
        if crop is not None:
            x, y, crop_width, crop_height = crop
            await execute_live_command(
                photo_session,
                photo_report,
                "photocraft",
                "image.crop",
                {"x": x, "y": y, "width": crop_width, "height": crop_height, "deleteCroppedPixels": True},
            )
            stages.append({"engine": "photocraft", "stage": "4x5_composition", "status": "complete"})
            width, height = crop_width, crop_height

        await execute_live_command(
            photo_session,
            photo_report,
            "photocraft",
            "image.adjustments.brightnessContrast",
            {"brightness": 0, "contrast": 6, "legacy": False},
        )
        await execute_live_command(
            photo_session,
            photo_report,
            "photocraft",
            "image.adjustments.vibrance",
            {"vibrance": 5, "saturation": 1},
        )
        await execute_live_command(
            photo_session,
            photo_report,
            "photocraft",
            "type.create",
            {
                "x": max(24, width - 76),
                "y": max(30, height - 32),
                "text": "Ω",
                "name": "Ion Omega mark",
                "size": 18,
                "color": "#d8b36a",
            },
        )
        stages.append({"engine": "photocraft", "stage": "restrained_tone_and_omega_mark", "status": "complete"})

        photo_preview_result = await photo_session.preview(PreviewRequest(arguments={"max_side": 1024}))
        photo_preview = preview_bytes(photo_preview_result)
        if photo_preview is None or not _is_png(photo_preview):
            raise WorkflowFailure("photocraft", "preview", "INVALID_PREVIEW", "PhotoCraft returned no valid PNG preview.")
        photo_preview_path = write_preview(project, "portrait-photocraft-preview.png", photo_preview)

        photo_candidate = safe_project_path(project, "portrait-photocraft.png", area="working")
        export_tool = next(
            (name for name in ("doc_export", "doc_save") if name in photo_report.tool_names),
            None,
        )
        if export_tool is None:
            raise WorkflowFailure("photocraft", "discover", "UNSUPPORTED_CAPABILITY", "PhotoCraft has no live document export tool.")
        export_arguments: dict[str, object] = {"path": project_relative(project, photo_candidate)}
        export_properties = photo_report.tool_schemas.get(export_tool, {}).get("properties", {})
        if "format" in export_properties:
            export_arguments["format"] = "png"
        await invoke_tool(photo_session, photo_report, "photocraft", export_tool, export_arguments)
        if not photo_candidate.is_file() or not _is_png(photo_candidate.read_bytes()):
            raise WorkflowFailure("photocraft", "export", "INVALID_EXPORT", "PhotoCraft did not write a valid PNG candidate.")
        stages.append({"engine": "photocraft", "stage": "working_candidate", "status": "complete"})
        await photo_session.close()
        photo_session = None

        light = LightCraftAdapter(demo=True, project_root=project.root)
        light_health = await light.health()
        if not light_health.healthy:
            raise WorkflowFailure("lightcraft", "health", "ENGINE_UNHEALTHY", "; ".join(light_health.errors))
        engines.append("lightcraft")
        versions["lightcraft"] = light_health.version
        light_session = await light.open_session()
        light_report = light_session.report
        import_result = await invoke_tool(
            light_session,
            light_report,
            "lightcraft",
            "cmd_library_import",
            {
                "paths": [str(photo_candidate)],
                "mode": "copy",
                "destination": str(project.working / "lightcraft-originals"),
                "organize": "flat",
            },
        )
        import_payload = payload(import_result)
        imported = import_payload.get("imported", []) if isinstance(import_payload, dict) else []
        if not isinstance(imported, list) or not imported or not isinstance(imported[0], int):
            raise WorkflowFailure("lightcraft", "import", "IMPORT_FAILED", "LightCraft did not return an imported photo ID.")
        photo_id = imported[0]
        await invoke_tool(light_session, light_report, "lightcraft", "select_photos", {"ids": [photo_id]})

        controls_result = await invoke_tool(light_session, light_report, "lightcraft", "cmd_develop_controls", {})
        controls_payload = payload(controls_result)
        control_ids = {
            item.get("id")
            for item in controls_payload
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        } if isinstance(controls_payload, list) else set()
        proposed_values = {
            "light.exposure": 0.12,
            "light.contrast": 5.0,
            "light.highlights": -10.0,
            "light.shadows": 6.0,
            "color.vibrance": 5.0,
        }
        values = {key: value for key, value in proposed_values.items() if key in control_ids}
        if len(values) < 3:
            raise WorkflowFailure("lightcraft", "discover", "UNSUPPORTED_CAPABILITY", "LightCraft live controls did not expose the expected develop settings.")
        await invoke_tool(
            light_session,
            light_report,
            "lightcraft",
            "cmd_develop_set",
            {"id": photo_id, "values": values},
        )
        stages.append({"engine": "lightcraft", "stage": "live_develop_grade", "status": "complete"})

        preview_path = safe_project_path(project, "portrait-lightcraft-preview.png", area="previews")
        rendered = await invoke_tool(
            light_session,
            light_report,
            "lightcraft",
            "render_photo",
            {"id": photo_id, "path": str(preview_path), "format": "png", "size": 1350},
        )
        if not preview_path.is_file() or not _is_png(preview_path.read_bytes()):
            raise WorkflowFailure("lightcraft", "preview", "INVALID_PREVIEW", "LightCraft did not write a valid preview PNG.")
        stages.append({"engine": "lightcraft", "stage": "preview", "status": "complete"})

        candidate = safe_project_path(project, "portrait-candidate.png", area="intermediates")
        exported = await invoke_tool(
            light_session,
            light_report,
            "lightcraft",
            "export",
            {
                "id": photo_id,
                "path": str(candidate),
                "format": "png",
                "width": 1080,
                "height": 1350,
                "conflict": "unique",
                "metadata": "none",
            },
        )
        if not candidate.is_file() or not _is_png(candidate.read_bytes()):
            raise WorkflowFailure("lightcraft", "export", "INVALID_EXPORT", "LightCraft did not write a valid portrait candidate.")
        stages.append({"engine": "lightcraft", "stage": "review_candidate", "status": "complete"})
        if not source_hashes_unchanged([source], before):
            raise WorkflowFailure("ion", "source_integrity", "SOURCE_MUTATED", "Portrait input changed during the workflow.")
        final_info = inspect_asset_file(candidate)
        final_width, final_height = final_info.get("width"), final_info.get("height")
        if not isinstance(final_width, int) or not isinstance(final_height, int) or abs(final_width / final_height - 0.8) > 0.01:
            raise WorkflowFailure("ion", "composition", "INVALID_ASPECT_RATIO", "Portrait candidate is not 4:5.")
        stages.append({"engine": "ion", "stage": "source_integrity", "status": "unchanged"})
        return result(
            WORKFLOW_ID,
            status="READY_FOR_REVIEW",
            message="Portrait candidate and previews are ready for visual review; finalize and export remain separate steps.",
            engines=engines,
            exact_versions=versions,
            output_paths=[str(candidate)],
            preview_paths=[str(photo_preview_path), str(preview_path)],
            stages=stages,
            source_hashes=before,
            details={
                "format": "png",
                "width": final_width,
                "height": final_height,
                "aspect_ratio": "4:5",
                "photocraft_export": str(photo_candidate),
                "lightcraft_photo_id": photo_id,
                "develop_controls": values,
                "lightcraft_export": payload(exported),
                "lightcraft_preview": payload(rendered),
            },
        )
    except (WorkflowFailure, MCPClientError, OSError, ProjectSafetyError, TypeError, ValueError, RuntimeError) as exc:
        from ion_creative.workflows.common import failure_from_exception

        failure = failure_from_exception(WORKFLOW_ID, exc, fallback_engine="photocraft")
        failure["engines"] = engines
        failure["exact_versions"] = versions
        failure["source_hashes"] = before
        return failure
    finally:
        if photo_session is not None:
            await photo_session.close()
        if light_session is not None:
            await light_session.close()
