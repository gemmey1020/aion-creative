"""Preview-first guided workflow entry points."""

from ion_creative.workflows import design_layout, pdf_ops, portrait, social_video, vector_graphic

WORKFLOWS = {
    portrait.WORKFLOW_ID: portrait.run,
    social_video.WORKFLOW_ID: social_video.run,
    vector_graphic.WORKFLOW_ID: vector_graphic.run,
    design_layout.WORKFLOW_ID: design_layout.run,
    pdf_ops.WORKFLOW_ID: pdf_ops.run,
}

__all__ = ["WORKFLOWS"]
