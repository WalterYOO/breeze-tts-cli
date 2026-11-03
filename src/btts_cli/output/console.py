"""Shared Rich console and the non-commercial license notice."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from btts_cli import LICENSE_NOTICE

console = Console()
err_console = Console(stderr=True)


def print_license_notice(*, compact: bool = False) -> None:
    if compact:
        console.print(f"[dim]{LICENSE_NOTICE}[/dim]")
        return
    console.print(
        Panel(
            LICENSE_NOTICE
            + "\n\nThis tool is for research, evaluation and non-commercial prototyping. "
            "Do not present self-hosted output as commercially licensed.",
            title="License",
            border_style="yellow",
        )
    )
