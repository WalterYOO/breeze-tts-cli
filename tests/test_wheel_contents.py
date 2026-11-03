from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    importlib.util.find_spec("build") is None
    or importlib.util.find_spec("hatchling") is None,
    reason="build backend not installed",
)
def test_wheel_has_no_weights_and_all_licenses(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(dist),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]

    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert not any(name.endswith(".safetensors") for name in names)
        assert not any("audio_tokenizer/" in name for name in names)
        assert not any("ms-models" in name for name in names)
        assert not any(name.endswith(".bin") for name in names)

        assert any(name.endswith("btts_cli/resources/design_templates.yaml") for name in names)
        assert any(name.endswith("btts_cli/resources/audition.html.j2") for name in names)
        assert any(name.endswith("_vendor/breeze_tts/LICENSE") for name in names)
        assert any(name.endswith("THIRD_PARTY_NOTICES.md") for name in names)

    extracted = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(extracted)

    code = (
        "import sys;"
        f"sys.path.insert(0, {str(extracted)!r});"
        "from click.testing import CliRunner;"
        "from btts_cli.cli import cli;"
        "result = CliRunner().invoke(cli, ['--help']);"
        "assert result.exit_code == 0, result.output;"
        "assert 'torch' not in sys.modules;"
        "print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONNOUSERSITE": "1", "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
