"""Static assets bundled with the reporting templates.

Helpers for reading binary assets and returning them as base64 data URIs ready
for inline embedding in HTML / PDF artifacts.

Why base64-embed instead of file-reference: reports are deliverables. Customers
download them, attach them to email, archive them. A `<img src="movate-logo.jpg">`
reference breaks the moment the file leaves its origin. A base64 data URI makes
the report a single self-contained file at the cost of ~50KB once.
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

_ASSETS_DIR = Path(__file__).resolve().parent


def asset_path(name: str) -> Path:
    """Absolute path to a bundled asset (e.g. 'movate-logo.jpg')."""
    p = _ASSETS_DIR / name
    if not p.exists():
        raise FileNotFoundError(f"Bundled asset not found: {p}")
    return p


def asset_data_uri(name: str) -> str:
    """Read a bundled asset and return it as a data: URI ready for `<img src=...>`."""
    p = asset_path(name)
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    encoded = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def movate_logo_data_uri() -> str:
    """The Movate logo as a data URI. Cached at first use."""
    global _CACHED_LOGO
    if _CACHED_LOGO is None:
        _CACHED_LOGO = asset_data_uri("movate-logo.jpg")
    return _CACHED_LOGO


_CACHED_LOGO: str | None = None
