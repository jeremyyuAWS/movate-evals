"""mdk-eval doctor — diagnose setup so users don't waste API tokens debugging.

One command. Rich table. Each row: PASS / WARN / FAIL with a one-line hint
explaining how to fix it. Exit code 1 only if any FAIL row.
"""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.table import Table

from ..utils.logging import console


@dataclass
class CheckRow:
    name: str
    status: str          # "PASS" | "WARN" | "FAIL"
    detail: str
    hint: str = ""


# ----------------------------- individual checks -----------------------------


def _check_python() -> CheckRow:
    v = sys.version_info
    if v >= (3, 10):
        return CheckRow("Python", "PASS", f"{v.major}.{v.minor}.{v.micro}")
    return CheckRow("Python", "FAIL", f"{v.major}.{v.minor}.{v.micro}",
                    hint="mdk-eval requires Python >= 3.10. Upgrade or use pyenv.")


def _check_env(var: str, *, level: str = "WARN", purpose: str) -> CheckRow:
    if os.getenv(var):
        return CheckRow(var, "PASS", "set", "")
    return CheckRow(
        var, level, "not set",
        hint=f"Required for {purpose}. Set in .env or export. "
             f"Optional if you don't use that capability."
    )


def _check_extra(pkg: str, *, purpose: str) -> CheckRow:
    try:
        importlib.import_module(pkg)
        return CheckRow(pkg, "PASS", "installed", "")
    except Exception:
        return CheckRow(
            pkg, "WARN", "not installed",
            hint=f"Optional. Install for: {purpose}. e.g. `pip install '.[judges]'`."
        )


def _check_dataset(path: Optional[str]) -> CheckRow | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return CheckRow("Dataset", "FAIL", str(p), hint="File not found.")
    try:
        from ..scenarios import load_scenarios

        scenarios = load_scenarios(p)
        return CheckRow("Dataset", "PASS", f"{p} ({len(scenarios)} scenarios)")
    except Exception as e:
        return CheckRow("Dataset", "FAIL", str(p), hint=f"Loader error: {e}")


def _check_endpoint(url: Optional[str], timeout: float = 5.0) -> CheckRow | None:
    if not url:
        return None
    try:
        import httpx

        with httpx.Client(timeout=timeout) as c:
            try:
                r = c.get(url)
                ok = r.status_code < 500
            except Exception:
                # retry HEAD
                r = c.head(url)
                ok = r.status_code < 500
        return CheckRow(
            "Endpoint", "PASS" if ok else "WARN", f"{url} → HTTP {r.status_code}",
            hint=("" if ok else "5xx — check the agent backend.")
        )
    except Exception as e:
        return CheckRow("Endpoint", "FAIL", url, hint=f"Unreachable: {type(e).__name__}: {e}")


# ----------------------------- runner -----------------------------


def run_doctor(*, dataset: Optional[str] = None, endpoint: Optional[str] = None) -> int:
    """Run all checks; print table; return exit code (0 ok, 1 if any FAIL)."""
    rows: list[CheckRow] = []
    rows.append(_check_python())

    # Judge providers (warn level — judges are optional)
    rows.append(_check_env("OPENAI_API_KEY", purpose="OpenAI judges + DeepEval"))
    rows.append(_check_env("ANTHROPIC_API_KEY", purpose="Anthropic judges + meta-judge"))
    # Adapter-specific env (warn — only matters if you use those adapters)
    rows.append(_check_env("LYZR_API_KEY", purpose="Lyzr adapter"))
    rows.append(_check_env("LYZR_USER_ID", purpose="Lyzr adapter"))
    # Observability
    rows.append(_check_env("LANGFUSE_PUBLIC_KEY", purpose="Langfuse trace export"))
    rows.append(_check_env("LANGFUSE_SECRET_KEY", purpose="Langfuse trace export"))

    # Optional extras
    rows.append(_check_extra("openai", purpose="OpenAI judges"))
    rows.append(_check_extra("anthropic", purpose="Anthropic judges + meta-judge"))
    rows.append(_check_extra("deepeval", purpose="DeepEval metric bridge"))
    rows.append(_check_extra("ragas", purpose="Ragas grounding triangulation"))
    rows.append(_check_extra("trulens", purpose="TruLens grounding triangulation"))
    rows.append(_check_extra("weasyprint", purpose="PDF report rendering"))
    rows.append(_check_extra("langfuse", purpose="Langfuse trace export"))
    rows.append(_check_extra("langgraph", purpose="LangGraph adapter"))

    if (r := _check_dataset(dataset)) is not None:
        rows.append(r)
    if (r := _check_endpoint(endpoint)) is not None:
        rows.append(r)

    _render(rows)
    return 1 if any(r.status == "FAIL" for r in rows) else 0


def _render(rows: list[CheckRow]) -> None:
    table = Table(title="mdk-eval doctor", title_style="bold", show_lines=False)
    table.add_column("", justify="center", width=4)
    table.add_column("Check", style="bold")
    table.add_column("Detail")
    table.add_column("Hint", style="dim")

    badge = {
        "PASS": "[green]✓[/green]",
        "WARN": "[yellow]![/yellow]",
        "FAIL": "[red]✗[/red]",
    }
    for r in rows:
        table.add_row(badge.get(r.status, "?"), r.name, r.detail, r.hint)

    console().print(table)

    n_pass = sum(1 for r in rows if r.status == "PASS")
    n_warn = sum(1 for r in rows if r.status == "WARN")
    n_fail = sum(1 for r in rows if r.status == "FAIL")
    summary_color = "red" if n_fail else ("yellow" if n_warn else "green")
    console().print(
        f"\n[{summary_color}][bold]Summary:[/bold] {n_pass} pass · {n_warn} warn · {n_fail} fail[/{summary_color}]"
    )
    if n_fail:
        console().print("[red]Resolve FAIL rows before running.[/red]")
    elif n_warn:
        console().print("[yellow]WARNs are optional. Set them only if you use that capability.[/yellow]")
