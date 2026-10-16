import hashlib
from pathlib import Path

import pytest


def _load_project(project_root: Path):
    module_path = project_root / "src" / "ion_creative" / "project.py"
    assert module_path.is_file(), "Ion project safety is not implemented"
    from ion_creative.project import (
        ProjectSafetyError,
        create_project,
        export_finalized_artifact,
        finalize_artifact,
        safe_project_path,
        safe_project_read_path,
        sha256_file,
    )

    return (
        ProjectSafetyError,
        create_project,
        export_finalized_artifact,
        finalize_artifact,
        safe_project_read_path,
        safe_project_path,
        sha256_file,
    )


def test_create_project_builds_the_ion_workspace(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    _, create_project, _, _, _, _, _ = _load_project(project_root)

    project = create_project(tmp_path / "creative-project")

    assert project.root.is_dir()
    for directory in (
        project.inputs,
        project.working,
        project.previews,
        project.intermediates,
        project.exports,
        project.ion,
    ):
        assert directory.is_dir()
        assert directory.parent == project.root
    assert (project.ion / "manifest.json").is_file()
    assert (project.ion / "workflow.json").is_file()
    assert (project.ion / "qa.json").is_file()


def test_create_named_project_uses_bounded_ids_under_ion_storage(tmp_path, monkeypatch) -> None:
    from ion_creative.project import create_named_project

    monkeypatch.setenv("ION_CREATIVE_PROJECTS_ROOT", str(tmp_path / "projects"))
    project = create_named_project("brand-probe")

    assert project.root == tmp_path / "projects" / "brand-probe"
    assert project.inputs.is_dir()


@pytest.mark.parametrize("project_id", ["", ".", "..", "../outside", "/tmp/outside", "a" * 65])
def test_create_named_project_rejects_unsafe_ids(tmp_path, monkeypatch, project_id) -> None:
    from ion_creative.project import ProjectSafetyError, create_named_project

    monkeypatch.setenv("ION_CREATIVE_PROJECTS_ROOT", str(tmp_path / "projects"))

    with pytest.raises(ProjectSafetyError):
        create_named_project(project_id)


def test_create_named_project_rejects_a_symlinked_workspace(tmp_path, monkeypatch) -> None:
    from ion_creative.project import ProjectSafetyError, create_named_project

    target = tmp_path / "outside"
    target.mkdir()
    projects_root = tmp_path / "projects"
    projects_root.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("ION_CREATIVE_PROJECTS_ROOT", str(projects_root))

    with pytest.raises(ProjectSafetyError):
        create_named_project("brand-probe")


def test_source_hash_can_be_snapshotted_without_mutating_input(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    _, create_project, _, _, _, _, sha256_file = _load_project(project_root)
    project = create_project(tmp_path / "creative-project")
    source = project.inputs / "source.bin"
    source.write_bytes(b"disposable source fixture")
    before = sha256_file(source)

    assert before == hashlib.sha256(b"disposable source fixture").hexdigest()
    assert sha256_file(source) == before
    assert source.read_bytes() == b"disposable source fixture"


def test_project_rejects_mutation_destinations_outside_allowed_roots(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    ProjectSafetyError, create_project, _, _, _, safe_project_path, _ = _load_project(project_root)
    project = create_project(tmp_path / "creative-project")
    outside = tmp_path / "outside.png"

    with pytest.raises(ProjectSafetyError, match="outside the project"):
        safe_project_path(project, outside, area="exports")

    with pytest.raises(ProjectSafetyError, match="not a writable project area"):
        safe_project_path(project, project.inputs / "source.png", area="inputs")


def test_project_rejects_symlink_escape_from_writable_area(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    ProjectSafetyError, create_project, _, _, _, safe_project_path, _ = _load_project(project_root)
    project = create_project(tmp_path / "creative-project")
    outside = tmp_path / "outside"
    outside.mkdir()
    (project.working / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectSafetyError, match="outside the project"):
        safe_project_path(project, project.working / "escape" / "output.png", area="working")


def test_project_rejects_a_symlinked_workspace_directory(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    ProjectSafetyError, create_project, _, _, _, _, _ = _load_project(project_root)
    root = tmp_path / "creative-project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "exports").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectSafetyError, match="must be a real directory"):
        create_project(root)


def test_finalized_artifact_can_be_exported_without_overwriting_existing_output(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    _, create_project, export_finalized_artifact, finalize_artifact, _, _, sha256_file = _load_project(
        project_root
    )
    project = create_project(tmp_path / "creative-project")
    candidate = project.intermediates / "candidate.png"
    candidate.write_bytes(b"valid candidate fixture")
    (project.previews / "candidate-preview.png").write_bytes(b"reviewed preview fixture")
    prior_output = project.exports / "final.png"
    prior_output.write_bytes(b"previous valid output")

    receipt = finalize_artifact(project, candidate)
    exported = export_finalized_artifact(project, candidate, "final.png")

    assert receipt["sha256"] == sha256_file(candidate)
    assert exported.name == "final-001.png"
    assert exported.read_bytes() == candidate.read_bytes()
    assert prior_output.read_bytes() == b"previous valid output"


def test_export_requires_current_finalization_and_rejects_path_traversal(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    ProjectSafetyError, create_project, export_finalized_artifact, finalize_artifact, _, _, _ = (
        _load_project(project_root)
    )
    project = create_project(tmp_path / "creative-project")
    candidate = project.working / "candidate.bin"
    candidate.write_bytes(b"candidate")
    (project.previews / "candidate-preview.png").write_bytes(b"reviewed preview fixture")

    with pytest.raises(ProjectSafetyError, match="must be finalized"):
        export_finalized_artifact(project, candidate, "candidate.bin")

    finalize_artifact(project, candidate)
    with pytest.raises(ProjectSafetyError, match="output name"):
        export_finalized_artifact(project, candidate, "../outside.bin")

    candidate.write_bytes(b"changed after finalization")
    with pytest.raises(ProjectSafetyError, match="changed after finalization"):
        export_finalized_artifact(project, candidate, "changed.bin")


def test_preview_read_path_is_confined_to_project_areas(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    ProjectSafetyError, create_project, _, _, safe_project_read_path, _, _ = _load_project(
        project_root
    )
    project = create_project(tmp_path / "creative-project")
    source = project.inputs / "source.png"
    source.write_bytes(b"fixture")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")

    assert safe_project_read_path(project, source) == source
    with pytest.raises(ProjectSafetyError, match="outside the project"):
        safe_project_read_path(project, outside)


def test_finalization_requires_a_generated_preview(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    ProjectSafetyError, create_project, _, finalize_artifact, _, _, _ = _load_project(
        project_root
    )
    project = create_project(tmp_path / "creative-project")
    candidate = project.intermediates / "candidate.bin"
    candidate.write_bytes(b"candidate")

    with pytest.raises(ProjectSafetyError, match="preview must exist"):
        finalize_artifact(project, candidate)
