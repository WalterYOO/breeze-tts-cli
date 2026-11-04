"""``btts doctor``: environment, model, device, memory and license."""

from __future__ import annotations

import contextlib
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from btts_cli import LICENSE_NOTICE, __version__
from btts_cli.commands._common import with_error_handling
from btts_cli.domain.model import read_incomplete_marker, read_marker
from btts_cli.engine.base import (
    compute_model_digest,
    missing_model_files,
    model_dir_hint,
    resolve_model_dir,
)
from btts_cli.output.console import console
from btts_cli.output.errors import EngineError, IntegrityError
from btts_cli.output.json import json_dumps

BENCH_TEXT = "你好，这是 Breeze 语音基准测试。"
BENCH_INSTRUCTION = "一位三十多岁的女性，音色温暖干净，音高中低，语速中慢，语气平静。"


def _system_memory() -> dict[str, int | None]:
    total: int | None = None
    available: int | None = None
    if sys.platform == "darwin":
        try:
            output = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            total = int(output)
        except (OSError, ValueError, subprocess.CalledProcessError):
            total = None
        try:
            stats = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, check=True
            ).stdout
            page_size = 4096
            match = re.search(r"page size of (\d+) bytes", stats)
            if match:
                page_size = int(match.group(1))
            values: dict[str, int] = {}
            for line in stats.splitlines()[1:]:
                name, _, raw = line.partition(":")
                raw = raw.strip().rstrip(".")
                if raw.isdigit():
                    values[name.strip().lower()] = int(raw)
            free_pages = (
                values.get("pages free", 0)
                + values.get("pages inactive", 0)
                + values.get("pages speculative", 0)
            )
            available = free_pages * page_size
        except (OSError, ValueError, subprocess.CalledProcessError):
            available = None
    return {"total_bytes": total, "available_bytes": available}


def _device_report(requested: str | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "requested": requested,
        "torch_installed": False,
        "torch_version": None,
        "mps_built": False,
        "mps_available": False,
        "selected": None,
    }
    try:
        import torch
    except ImportError:
        return report
    report["torch_installed"] = True
    report["torch_version"] = torch.__version__
    try:
        report["mps_built"] = bool(torch.backends.mps.is_built())
        report["mps_available"] = bool(torch.backends.mps.is_available())
    except AttributeError:
        pass
    if requested:
        report["selected"] = requested
    elif report["mps_available"]:
        report["selected"] = "mps"
    return report


def _model_report(model_dir: Path) -> dict[str, Any]:
    exists = model_dir.is_dir()
    missing = missing_model_files(model_dir) if exists else []
    size_bytes = None
    if exists:
        size_bytes = sum(
            path.stat().st_size for path in model_dir.rglob("*") if path.is_file()
        )
    digest = compute_model_digest(model_dir) if exists and not missing else None
    marker = None
    marker_error = None
    incomplete = None
    incomplete_error = None
    if exists:
        try:
            marker = read_marker(model_dir)
        except IntegrityError as exc:
            marker_error = exc.message
        try:
            incomplete = read_incomplete_marker(model_dir)
        except IntegrityError as exc:
            incomplete_error = exc.message
    return {
        "path": str(model_dir),
        "exists": exists,
        "missing": missing,
        "size_bytes": size_bytes,
        "digest": digest,
        "marker_found": marker is not None,
        "marker_error": marker_error,
        "source": marker.source if marker else None,
        "repo": marker.repo if marker else None,
        "revision": marker.revision if marker else None,
        "downloaded_at": marker.downloaded_at if marker else None,
        "download_incomplete": incomplete is not None,
        "download_state_stale": marker is not None and incomplete is not None,
        "download_started_at": incomplete.started_at if incomplete else None,
        "download_error": incomplete.error if incomplete else None,
        "download_state_error": incomplete_error,
    }


def _gb(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value / 1_000_000_000:.1f} GB"


def _render(report: dict[str, Any]) -> None:
    table = Table(title="btts doctor", show_header=False, box=None)
    table.add_column("key", style="bold")
    table.add_column("value")

    table.add_row("cli", f"{report['cli_version']} (Python {report['python']})")
    table.add_row("platform", report["platform"])

    model = report["model"]
    if not model["exists"]:
        table.add_row("model", f"[red]missing: {model['path']}[/red]")
    elif model["missing"]:
        table.add_row("model", f"[red]incomplete: {model['path']}[/red]")
        table.add_row("missing files", ", ".join(model["missing"]))
    else:
        table.add_row("model", f"{model['path']} ({_gb(model['size_bytes'])})")
        table.add_row("model digest", model["digest"] or "unknown")
    if model["marker_found"]:
        table.add_row(
            "download",
            f"{model['source']} · {model['repo']} @ {model['revision']}",
        )
        table.add_row("downloaded", model["downloaded_at"] or "unknown")
    elif model["marker_error"]:
        table.add_row("download", f"[yellow]{model['marker_error']}[/yellow]")
    elif model["download_incomplete"]:
        table.add_row(
            "download",
            "[yellow]incomplete (previous attempt did not finish)[/yellow]",
        )
    elif model["download_state_error"]:
        table.add_row(
            "download", f"[yellow]{model['download_state_error']}[/yellow]"
        )

    device = report["device"]
    if not device["torch_installed"]:
        table.add_row("torch", "[yellow]not installed (base install)[/yellow]")
    else:
        table.add_row("torch", device["torch_version"])
        table.add_row("MPS available", "yes" if device["mps_available"] else "no")
        table.add_row("device", device["selected"] or "none")

    memory = report["memory"]
    table.add_row(
        "memory",
        f"total {_gb(memory['total_bytes'])}, available {_gb(memory['available_bytes'])}",
    )

    if "bench" in report and "error" not in report["bench"]:
        bench = report["bench"]
        table.add_row(
            "bench",
            f"load {bench['load_s']:.1f}s, generate {bench['generate_s']:.1f}s, "
            f"audio {bench['audio_s']:.2f}s, RTF {bench['rtf']:.1f}, "
            f"MPS memory {_gb(bench['mps_memory_bytes'])}",
        )

    console.print(table)
    if report["warnings"]:
        console.print()
        for warning in report["warnings"]:
            console.print(f"[yellow]warning[/yellow] {warning}")
    console.print()
    console.print(f"[yellow]License:[/yellow] {LICENSE_NOTICE}")


@click.command("doctor")
@click.option("--bench", is_flag=True, help="Load the model and run one short generation.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option(
    "--model",
    "model_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Breeze-TTS-2 checkpoint directory (default: $BTTS_MODEL_DIR or "
    "./ms-models/Breeze-TTS-2).",
)
@click.option("--device", type=click.Choice(["mps", "cpu"]), default=None)
@with_error_handling
def doctor(bench: bool, as_json: bool, model_dir: Path | None, device: str | None) -> None:
    """Check the environment, model files, device, memory and license."""
    resolved = resolve_model_dir(model_dir)
    report: dict[str, Any] = {
        "cli_version": __version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "model": _model_report(resolved),
        "device": _device_report(device),
        "memory": _system_memory(),
        "warnings": [],
    }

    hint = model_dir_hint()
    if not report["model"]["exists"] and model_dir is None and hint is not None:
        report["warnings"].append(
            f"model not found at the default path; an existing checkpoint was seen at {hint}"
            " — pass --model or set BTTS_MODEL_DIR"
        )

    total = report["memory"]["total_bytes"]
    if total is not None and total <= 17_000_000_000:
        report["warnings"].append(
            "16 GB class machine: the model uses about 8 GB and the system may run short"
        )
    model_report = report["model"]
    if model_report["exists"] and model_report["missing"]:
        if model_report["marker_found"] or model_report["download_incomplete"]:
            report["warnings"].append(
                "model download is incomplete; rerun `btts model download`"
            )
        else:
            report["warnings"].append("model directory is incomplete; inference will fail")
    if (
        model_report["download_incomplete"]
        and not model_report["marker_found"]
        and not (model_report["exists"] and model_report["missing"])
    ):
        detail = model_report["download_error"]
        suffix = f": {detail}" if detail else ""
        report["warnings"].append(
            f"previous model download did not finish{suffix}; "
            "rerun `btts model download`"
        )
    if model_report["download_state_stale"]:
        report["warnings"].append(
            "stale download state exists alongside a valid completion marker; "
            "the marker takes precedence and the state file is safe to remove"
        )
    if model_report["marker_error"]:
        report["warnings"].append(
            f"model completion marker is unreadable: {model_report['marker_error']}"
        )
    if model_report["download_state_error"]:
        report["warnings"].append(
            "model download state is unreadable: "
            f"{model_report['download_state_error']}"
        )

    bench_error: EngineError | None = None
    if bench:
        if as_json:
            # Vendored Breeze code and third-party libraries print diagnostics
            # directly during load/generation. Keep stdout reserved for one JSON
            # document; the diagnostics remain visible on stderr.
            with contextlib.redirect_stdout(sys.stderr):
                bench_error = _run_bench(report, resolved, device)
        else:
            bench_error = _run_bench(report, resolved, device)

    if as_json:
        click.echo(json_dumps(report), nl=False)
    else:
        _render(report)
    if bench_error is not None:
        raise bench_error


def _run_bench(
    report: dict[str, Any], model_dir: Path, device: str | None
) -> EngineError | None:
    from btts_cli.engine import load_engine
    from btts_cli.engine.base import GenerationRequest

    try:
        engine = load_engine(model_dir=model_dir, device=device)
    except EngineError as exc:
        report["bench"] = {"error": exc.message}
        return exc
    try:
        result = engine.generate(
            GenerationRequest(
                text=BENCH_TEXT,
                instruction=BENCH_INSTRUCTION,
                cfg_scale=4.0,
                seed=42,
                max_new_tokens=150,
            )
        )
    except Exception as exc:  # noqa: BLE001 - report instead of crashing
        report["bench"] = {"error": f"{type(exc).__name__}: {exc}"}
        return EngineError(f"bench generation failed: {exc}")
    finally:
        try:
            engine.close()
        except Exception:  # noqa: BLE001
            pass

    mps_memory = None
    try:
        import torch

        if torch.backends.mps.is_available():
            mps_memory = int(torch.mps.driver_allocated_memory())
    except Exception:  # noqa: BLE001
        mps_memory = None

    report["bench"] = {
        "load_s": round(getattr(engine, "load_seconds", 0.0), 2),
        "generate_s": round(result.elapsed_s, 2),
        "audio_s": round(result.duration_s, 2),
        "rtf": round(result.elapsed_s / result.duration_s, 2) if result.duration_s else None,
        "mps_memory_bytes": mps_memory,
        "device": engine.device,
    }
    return None
