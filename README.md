# Ion Creative

Ion Creative is a local-first orchestration layer that exposes a bounded set of image and video workflows to Codex through an MCP server. It discovers capabilities from installed Craft command-line runtimes, validates requests, keeps project files inside a controlled workspace, and separates candidate review from final export.

## Overview

The project addresses a practical integration problem: creative runtimes expose different commands, schemas, and health states, while an agent needs a stable interface for planning work and presenting reviewable outputs. Ion normalizes that interface without claiming that every underlying runtime capability is available or supported.

## Core capabilities

- Discover installed runtimes and their live tools, schemas, and health status.
- Plan supported workflows from user intent and available asset metadata.
- Route supported operations through per-engine adapters and high-level MCP tools.
- Keep source assets unchanged; write intermediate candidates, previews, and exports into bounded project areas.
- Require an explicit review/finalization step before export and check the candidate hash before writing the final artifact.
- Report unsupported or unavailable capabilities instead of inventing a successful result.

The adapter catalog is runtime-dependent. Consult `ion capabilities` and `ion doctor` for the current machine's available tools. The unit suite validates integration behavior with fakes; it does not establish that every Craft runtime is installed or that every adapter works against a live runtime.

## Architecture

```text
Codex plugin / MCP client
          |
          v
Ion MCP server and workflow API
          |
          +--> capability catalog and runtime health discovery
          +--> request validation and workflow planning
          +--> engine adapters and isolated local MCP clients
          |
          v
Installed Craft command-line runtimes

Ion project areas: inputs/ | intermediates/ | previews/ | exports/
```

The server exposes high-level tools and delegates engine-specific details to adapters. Runtime versions and release checksums are recorded in `config/runtimes.json`; the installer downloads those third-party runtimes but does not include their binaries in this repository. The local plugin entry point is configured through `.codex-plugin/plugin.json` and `.mcp.json`.

## Key workflows

1. Inspect the runtime and discover the tools that are actually available.
2. Create or select an Ion project and keep source inputs within its workspace.
3. Plan and run a supported workflow to create a review candidate.
4. Review its preview, finalize the candidate, then export without overwriting an existing destination.

The image and video smoke workflows are narrow demonstrations. Unsupported requests return structured status; the project does not promise arbitrary image editing, arbitrary source-video editing, or every feature named by an installed runtime.

## Integrations

- Codex local plugin and MCP over stdio.
- Locally installed Craft runtimes, discovered and launched as separate processes.
- Optional ArtCraft credential detection exists, but generation is not implemented or claimed as supported.

No hosted service or provider API is required by the documented local workflows. Runtime installation contacts the configured third-party release source to fetch pinned packages.

## Setup

Requirements: Python 3.12+, `uv`, Codex with local plugin support, and a supported operating system/runtime combination.

```bash
uv sync --locked
uv run ion doctor
uv run ion capabilities
```

To install the pinned Craft runtimes, review `config/runtimes.json` and then run:

```bash
./scripts/install-runtimes.sh
uv run ion doctor
```

Runtime installation downloads third-party executables into the user-local Ion runtime directory and verifies published hashes. Review the installer and runtime registry before running them. To register the local plugin from this repository root:

```bash
codex plugin marketplace add .
codex plugin add ion-creative@ion-creative-local
```

Start a fresh Codex session after installation. Exact plugin commands can vary with the installed Codex version.

## Configuration

Runtime pins and checksums live in `config/runtimes.json`. The repository contains no provider credentials. If an optional credential is configured locally, keep it in the process environment or an external secret manager; never commit it. No `.env` file is required by the project.

## Testing and verification

Verified in the publication staging copy:

- `uv run pytest tests/unit -v` — 110 passed.
- `uv run ruff check .` — passed.

Acceptance tests and `ion doctor` were not run against installed Craft runtimes in this publication pass. The unit tests use mocked/disposable runtime behavior and do not prove live engine compatibility or successful creative exports.

## Security and privacy

- Source files are not overwritten by the documented workflows.
- Project paths are checked against bounded workspace areas; preview, intermediate, and export outputs are separated.
- Export requires a finalized candidate whose content still matches its reviewed hash.
- Runtime health and capability checks should be performed before execution.
- Treat imported media and runtime binaries as untrusted inputs; install third-party runtimes only after reviewing their source and checksum metadata.
- This repository does not include user media, runtime binaries, credentials, or production data.

## Limitations and status

Status: experimental local integration. Live compatibility depends on the installed Craft runtime versions and their exposed tools. The included tests establish unit-level behavior only. Acceptance-level integration, desktop launcher behavior, and end-to-end output parity have not been established here. Some adapters may report unavailable or unsupported capabilities. No external production adoption is claimed.

## Ownership, provenance, and license

The project is published by Mohamed Gamal, who has declared authority to publish the project assets in this repository. Craft runtime packages are separate third-party software; this repository does not redistribute their binaries. No license file is included, so this publication does not grant a general license to reuse the source. Contact the author for usage permission.
