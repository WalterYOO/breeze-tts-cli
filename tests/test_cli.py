from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

from tests.fake_engine import FakeEngine


def test_help_works(run) -> None:
    result = run("--help")
    assert result.exit_code == 0
    assert "completion" in result.output
    assert "design" in result.output
    assert "doctor" in result.output
    assert "model" in result.output
    assert "say" in result.output
    assert "voice" in result.output


def test_importing_cli_does_not_import_torch() -> None:
    code = "import btts_cli.cli, sys; print('torch' in sys.modules)"
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"


def test_doctor_json_reports_missing_model(run, isolated: Path) -> None:
    result = run("doctor", "--json", "--model", str(isolated / "missing"))
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["model"]["exists"] is False
    assert report["cli_version"]

    human = run("doctor", "--model", str(isolated / "missing"))
    assert human.exit_code == 0, human.output
    assert "non-commercial" in human.output.lower()


def test_doctor_bench_fails_clearly_without_model(run, isolated: Path) -> None:
    result = run(
        "doctor",
        "--bench",
        "--json",
        "--model",
        str(isolated / "missing"),
    )
    assert result.exit_code == 3
    report = json.loads(result.stdout)
    assert "error" in report["bench"]
    assert result.stderr.strip()
    assert "model directory not found" in result.stderr


def test_doctor_bench_json_success_keeps_stdout_clean(
    run, isolated: Path, monkeypatch
) -> None:
    class ChattyEngine(FakeEngine):
        def generate(self, request):
            print("chatty generate diagnostic")
            return super().generate(request)

    engine = ChattyEngine()

    def load_engine(**kwargs):
        print("chatty load diagnostic")
        return engine

    monkeypatch.setattr("btts_cli.engine.load_engine", load_engine)

    fake_torch = types.ModuleType("torch")
    fake_torch.__version__ = "2.9.1"
    fake_torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_built=lambda: True, is_available=lambda: False)
    )
    fake_torch.mps = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    result = run(
        "doctor",
        "--bench",
        "--json",
        "--model",
        str(isolated / "model"),
    )
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["bench"]["audio_s"] > 0
    assert "chatty" not in result.stdout
    assert "chatty load diagnostic" in result.stderr
    assert "chatty generate diagnostic" in result.stderr


def test_parse_meminfo_reads_bytes() -> None:
    from btts_cli.commands.doctor import _parse_meminfo

    text = (
        "MemTotal:       197784512 kB\n"
        "MemFree:         1234567 kB\n"
        "MemAvailable:   150000000 kB\n"
        "HugePages_Total:       0\n"
    )
    total, available = _parse_meminfo(text)
    assert total == 197784512 * 1024
    assert available == 150000000 * 1024


def test_parse_meminfo_tolerates_missing_fields() -> None:
    from btts_cli.commands.doctor import _parse_meminfo

    assert _parse_meminfo("") == (None, None)
