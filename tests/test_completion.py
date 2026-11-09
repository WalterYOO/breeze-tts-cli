from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from btts_cli.commands._complete import (
    complete_bare_voice_name,
    complete_voice_name,
    complete_voice_reference,
)
from btts_cli.commands.completion import SHELLS
from btts_cli.domain.voice import (
    DefaultsBlock,
    DesignBlock,
    IdentityBlock,
    ProvenanceBlock,
    VoiceProfile,
    VoiceStore,
    new_voice_id,
)


def _write_design(store: VoiceStore, name: str) -> Path:
    profile = VoiceProfile(
        voice_id=new_voice_id(),
        name=name,
        kind="design",
        language="zh",
        identity=IdentityBlock(structured={}, rendered_instruction="测试音色。"),
        design=DesignBlock(seed=42, cfg_scale=4.0, instruction="测试音色。"),
        defaults=DefaultsBlock(cfg_scale=4.0, seed=42),
        provenance=ProvenanceBlock(source="designed"),
    )
    directory = store.root / name
    directory.mkdir(parents=True)
    (directory / "profile.json").write_text(
        json.dumps(profile.model_dump(mode="json"), ensure_ascii=True),
        encoding="utf-8",
    )
    return directory


def _values(items) -> list[str]:
    return [item.value for item in items]


@pytest.mark.parametrize(
    ("shell", "marker"),
    [
        ("bash", "complete -o nosort -F _btts_completion btts"),
        ("zsh", "compdef _btts_completion btts"),
        ("fish", "complete --no-files --command btts"),
        pytest.param(
            "powershell",
            "Register-ArgumentCompleter -Native -CommandName btts",
            marks=pytest.mark.skipif(
                "powershell" not in SHELLS,
                reason="this Click release has no PowerShell completion class",
            ),
        ),
    ],
)
def test_completion_prints_a_shell_script(run, shell: str, marker: str) -> None:
    result = run("completion", shell)
    assert result.exit_code == 0, result.output
    assert marker in result.output
    assert "_BTTS_COMPLETE" in result.output


def test_completion_autodetects_the_shell(run, monkeypatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    result = run("completion")
    assert result.exit_code == 0, result.output
    assert "#compdef btts" in result.output


def test_completion_rejects_an_unknown_shell(run) -> None:
    result = run("completion", "tcsh")
    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_completion_reports_an_undetectable_shell(run, monkeypatch) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    result = run("completion")
    assert result.exit_code == 2
    assert "cannot detect the current shell" in result.output


def test_voice_name_completion_lists_sorted_addressable_names(isolated: Path) -> None:
    store = VoiceStore()
    _write_design(store, "dusk")
    _write_design(store, "dawn")
    assert _values(complete_voice_name(None, None, "")) == ["dawn", "dusk"]
    assert _values(complete_voice_name(None, None, "du")) == ["dusk"]


def test_voice_name_completion_mirrors_the_at_prefix(isolated: Path) -> None:
    store = VoiceStore()
    _write_design(store, "dusk")
    assert _values(complete_voice_name(None, None, "@du")) == ["@dusk"]
    assert _values(complete_voice_name(None, None, "du")) == ["dusk"]


def test_voice_name_completion_can_force_at(isolated: Path) -> None:
    store = VoiceStore()
    _write_design(store, "dusk")
    assert _values(complete_voice_reference(None, None, "du")) == ["@dusk"]
    assert _values(complete_voice_reference(None, None, "@du")) == ["@dusk"]


def test_bare_voice_name_completion_refuses_at(isolated: Path) -> None:
    store = VoiceStore()
    _write_design(store, "dusk")
    assert _values(complete_bare_voice_name(None, None, "du")) == ["dusk"]
    assert _values(complete_bare_voice_name(None, None, "@du")) == []


def test_completion_skips_unaddressable_and_symlinked_voices(isolated: Path) -> None:
    store = VoiceStore()
    _write_design(store, "dusk")
    _write_design(store, "bad name")
    link = store.root / "alias"
    try:
        link.symlink_to(store.root / "dusk", target_is_directory=True)
    except OSError:  # pragma: no cover - platforms without symlink support
        pytest.skip("symlinks are not available")
    assert _values(complete_voice_name(None, None, "")) == ["dusk"]


def test_completion_is_silent_when_the_store_is_broken(isolated: Path) -> None:
    store = VoiceStore()
    store.root.parent.mkdir(parents=True, exist_ok=True)
    store.root.write_text("not a directory", encoding="utf-8")
    assert complete_voice_name(None, None, "") == []


def _run_completion(shell: str, words: str, cword: int, data_dir: Path) -> str:
    env = {
        **os.environ,
        "BTTS_DATA_DIR": str(data_dir),
        "_BTTS_COMPLETE": f"{shell}_complete",
        "COMP_WORDS": words,
        "COMP_CWORD": str(cword),
    }
    completed = subprocess.run(
        [sys.executable, "-m", "btts_cli"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_click_resolves_voice_names_for_show(isolated: Path) -> None:
    store = VoiceStore()
    _write_design(store, "dusk")
    _write_design(store, "dawn")
    output = _run_completion(
        "bash", "btts voice show ", 3, isolated / "data"
    )
    assert output.splitlines() == ["plain,dawn", "plain,dusk"]


def test_click_resolves_at_voice_for_say(isolated: Path) -> None:
    store = VoiceStore()
    _write_design(store, "dusk")
    output = _run_completion(
        "bash", "btts say hello --voice du", 4, isolated / "data"
    )
    assert output.splitlines() == ["plain,@dusk"]


def test_completion_keeps_stdout_clean_when_the_store_is_broken(
    isolated: Path,
) -> None:
    store = VoiceStore()
    store.root.parent.mkdir(parents=True, exist_ok=True)
    store.root.write_text("not a directory", encoding="utf-8")
    output = _run_completion(
        "bash", "btts voice show ", 3, isolated / "data"
    )
    # Click's completion protocol ends every response with a newline.
    assert output.strip() == ""
    assert "error" not in output.lower()


def test_completion_matches_click_builtin_source(run) -> None:
    for shell in ("bash", "zsh", "fish"):
        result = run("completion", shell)
        assert result.exit_code == 0, result.output
        env = {**os.environ, "_BTTS_COMPLETE": f"{shell}_source"}
        builtin = subprocess.run(
            [sys.executable, "-m", "btts_cli"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert builtin.returncode == 0, builtin.stderr
        assert result.stdout == builtin.stdout


@pytest.mark.parametrize(
    ("shell", "filename"),
    [("bash", "btts"), ("zsh", "_btts"), ("fish", "btts.fish")],
)
def test_completion_install_then_uninstall(
    run, tmp_path: Path, shell: str, filename: str
) -> None:
    target_dir = tmp_path / "completions"
    result = run("completion", shell, "--install", "--dir", str(target_dir))
    assert result.exit_code == 0, result.output
    target = target_dir / filename
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "_BTTS_COMPLETE" in text
    assert "# btts-generated completion file" in text
    if shell == "zsh":
        assert text.startswith("#compdef btts")

    again = run("completion", shell, "--install", "--dir", str(target_dir))
    assert again.exit_code == 0, again.output
    assert "up to date" in again.output

    removed = run("completion", shell, "--uninstall", "--dir", str(target_dir))
    assert removed.exit_code == 0, removed.output
    assert not target.exists()

    missing = run("completion", shell, "--uninstall", "--dir", str(target_dir))
    assert missing.exit_code == 0, missing.output
    assert "not installed" in missing.output


def test_completion_install_refuses_a_foreign_file(run, tmp_path: Path) -> None:
    target_dir = tmp_path / "completions"
    target_dir.mkdir()
    (target_dir / "btts").write_text("my own completions\n", encoding="utf-8")
    result = run("completion", "bash", "--install", "--dir", str(target_dir))
    assert result.exit_code == 2
    assert "was not created by btts" in result.output
    assert (target_dir / "btts").read_text(encoding="utf-8") == "my own completions\n"


def test_completion_install_refuses_a_symlink_target(run, tmp_path: Path) -> None:
    target_dir = tmp_path / "completions"
    target_dir.mkdir()
    real = tmp_path / "elsewhere"
    real.write_text("do not touch\n", encoding="utf-8")
    try:
        (target_dir / "btts").symlink_to(real)
    except OSError:  # pragma: no cover - platforms without symlink support
        pytest.skip("symlinks are not available")
    result = run("completion", "bash", "--install", "--dir", str(target_dir))
    assert result.exit_code == 2
    assert "refusing to write through a symlink" in result.output


def test_completion_dir_requires_an_install_action(run, tmp_path: Path) -> None:
    result = run("completion", "bash", "--dir", str(tmp_path))
    assert result.exit_code == 2
    assert "--dir requires --install or --uninstall" in result.output


def test_completion_install_and_uninstall_are_mutually_exclusive(
    run,
) -> None:
    result = run("completion", "bash", "--install", "--uninstall")
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


@pytest.mark.skipif(
    "powershell" not in SHELLS, reason="no PowerShell completion class"
)
def test_completion_install_rejects_powershell(run, tmp_path: Path) -> None:
    result = run(
        "completion", "powershell", "--install", "--dir", str(tmp_path)
    )
    assert result.exit_code == 2
    assert "not supported for powershell" in result.output
