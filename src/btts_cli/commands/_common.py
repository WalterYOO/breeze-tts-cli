"""Shared command decorators."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

import click

from btts_cli.output.console import err_console
from btts_cli.output.errors import BttsError, EngineError

F = TypeVar("F", bound=Callable[..., Any])

DEVICE_HELP = "auto (default), mps, cuda[:N] or cpu (debug only)."


def device_option(func: F) -> F:
    """Shared ``--device`` flag; validation happens in the engine layer."""
    return click.option(
        "--device",
        type=str,
        default=None,
        help=DEVICE_HELP,
    )(func)


def generate_or_raise_engine_error(
    engine: Any, request: Any, *, stage: str = "generation"
) -> Any:
    """Run ``engine.generate`` and normalise inference failures.

    Domain errors and Ctrl-C keep their existing semantics; every other
    exception becomes an ``EngineError`` so the CLI exits 3 with a readable
    message instead of leaking a traceback or exit 1.
    """
    try:
        return engine.generate(request)
    except (BttsError, KeyboardInterrupt):
        raise
    except Exception as exc:
        raise EngineError(
            f"{stage} failed: {type(exc).__name__}: {exc}",
            hint="check the device, memory pressure and the model checkpoint",
        ) from exc


def with_error_handling(func: F) -> F:
    """Turn typed domain errors into stable exit codes."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except BttsError as exc:
            err_console.print(f"[bold red]error[/bold red] {exc.message}")
            if exc.hint:
                err_console.print(f"      [dim]{exc.hint}[/dim]")
            raise click.exceptions.Exit(exc.exit_code) from exc
        except KeyboardInterrupt as exc:
            err_console.print("[yellow]interrupted[/yellow]")
            raise click.exceptions.Exit(130) from exc

    return wrapper  # type: ignore[return-value]
