"""Credential-state gate for optional ArtCraft generation."""

from __future__ import annotations

import os


class ArtCraftAdapter:
    """Report whether a credential is present without making external requests."""

    def configuration_status(self) -> dict[str, object]:
        configured = bool(os.environ.get("ARTCRAFT_API_KEY", "").strip())
        return {
            "engine": "artcraft",
            "status": "CONFIGURED" if configured else "NOT_CONFIGURED",
            "credentials_detected": configured,
            "generation_supported": False,
            "message": (
                "ArtCraft API credentials are present; generation is not implemented in this phase."
                if configured
                else "ArtCraft API credentials are not configured; generation is unavailable."
            ),
        }
