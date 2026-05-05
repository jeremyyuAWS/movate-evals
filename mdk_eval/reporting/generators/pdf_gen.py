"""PDF rendering via WeasyPrint. Best-effort; no-op if not installed."""
from __future__ import annotations

from pathlib import Path

from ...utils.logging import get_logger

log = get_logger()


def write_pdf_report(html_path: Path, pdf_path: Path) -> Path | None:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception as e:
        log.info(f"PDF skipped (weasyprint not available: {e})")
        return None
    try:
        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return pdf_path
    except Exception as e:
        log.warning(f"PDF render failed: {e}")
        return None
