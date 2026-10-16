---
name: ion-creative
description: Use Ion Creative high-level tools for local image and video workflows with runtime checks and preview-based visual QA.
---

# Ion Creative

Use Ion Creative as the Codex-facing entry point for supported creative work.

## Available tools

- `inspect_asset`
- `inspect_runtime`
- `plan_workflow`
- `preview`
- `edit_image`
- `grade_image`
- `edit_video`
- `grade_video`
- `make_social_video`
- `finalize`
- `export`

## Operating rules

- Call `inspect_runtime` before the first edit in a session. For image edits,
  require healthy PhotoCraft; for video edits, require healthy FilmCraft.
- Use the high-level Ion tools. Do not call raw Craft MCP tools to work around a
  degraded engine or unsupported Ion recipe.
- Keep sources immutable and within the chosen Ion project. Write candidates,
  previews, and exports only to their project directories.
- After an edit returns `READY_FOR_REVIEW`, call `preview` for that candidate.
  Inspect the returned preview before calling `finalize`, then call `export`.
- Treat command success as execution evidence; visual output still needs review.
- Use disposable fixtures for smoke workflows, never personal production media.

## Wave 1 support

- `edit_image` supports only the verified PhotoCraft invert recipe. Use
  `instructions="invert"` and a disposable PNG under the project `inputs/`
  directory.
- `edit_video` supports only the disposable FilmCraft demo. Use
  `source_paths=[]` and `instructions="smoke"`; `preview` returns its rendered
  frame. Finalize and export only after reviewing that frame.
- `grade_image`, `grade_video`, `make_social_video`, arbitrary source-video
  edits, and platform-specific social assembly currently return
  `UNSUPPORTED_CAPABILITY`. Explain the limit and stop that requested workflow.
- If asked for a disposable video smoke, use the supported `edit_video` demo
  workflow. Do not describe its MP4 as a platform-specific social edit.

`inspect_runtime` reports all four pinned Craft engines and their live MCP
capabilities. A degraded unrelated engine does not block a supported workflow.
