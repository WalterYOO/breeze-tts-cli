from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

import pytest

import btts_cli.domain.voice as voice_module
from btts_cli.domain.voice import VoiceStore
from btts_cli.output.errors import IntegrityError, UsageError
from tests.helpers import write_transcript, write_wav


def _add_clone(
    run, isolated: Path, name: str, tags: tuple[str, ...] = ()
) -> VoiceStore:
    ref = write_wav(isolated / f"{name}-ref.wav", seconds=8.0)
    transcript = write_transcript(isolated / f"{name}-ref.txt", "这是一段参考音频。")
    args = [
        "voice",
        "add",
        name,
        "--ref",
        str(ref),
        "--text",
        str(transcript),
        "--confirm-transcript",
    ]
    for tag in tags:
        args.extend(["--tag", tag])
    result = run(*args)
    assert result.exit_code == 0, result.output
    return VoiceStore()


def _payload(store: VoiceStore, name: str) -> dict[str, object]:
    path = store.path_for(name) / "profile.json"
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- rename


def test_rename_preserves_fields_and_moves_directory(run, fake, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm", "night"))
    before = _payload(store, "dusk")

    result = run("voice", "rename", "dusk", "evening")
    assert result.exit_code == 0, result.output
    assert "renamed" in result.output

    after = _payload(store, "evening")
    assert after["name"] == "evening"
    for key, value in before.items():
        if key != "name":
            assert after[key] == value
    assert after["voice_id"] == before["voice_id"]
    assert after["tags"] == ["calm", "night"]
    assert after["reference"]["audio"] == "ref.wav"  # relative name, unchanged

    assert not store.path_for("dusk").exists()
    directory = store.path_for("evening")
    for filename in ("profile.json", "LICENSE.txt", "ref.wav", "ref.txt", "preview.wav"):
        assert (directory / filename).is_file()

    listed = run("voice", "list")
    assert "evening" in listed.output
    assert "dusk" not in listed.output

    assert run("voice", "show", "evening").exit_code == 0
    missing = run("voice", "show", "dusk")
    assert missing.exit_code == 2
    assert "voice not found" in missing.stderr

    # `say --voice @NEW` resolves; the old reference no longer does.
    said = run("say", "你好。", "--voice", "@evening", "--out", str(isolated / "o.wav"))
    assert said.exit_code == 0, said.output
    assert fake.calls[-1].ref_audio is not None
    assert fake.calls[-1].ref_audio.is_relative_to(directory)

    stale = run("say", "你好。", "--voice", "@dusk", "--out", str(isolated / "o2.wav"))
    assert stale.exit_code == 2
    assert "voice not found" in stale.stderr


def test_rename_refuses_existing_target(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    dawn_before = _payload(store, "dawn")

    result = run("voice", "rename", "dusk", "dawn")
    assert result.exit_code == 2
    assert "already exists" in result.stderr
    assert store.load("dusk").name == "dusk"
    assert _payload(store, "dawn") == dawn_before


def test_rename_rejects_invalid_new_name(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    result = run("voice", "rename", "dusk", "../escape")
    assert result.exit_code == 2
    assert "invalid voice name" in result.stderr
    assert store.exists("dusk")


def test_rename_rejects_symlinked_source(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    outside = isolated / "outside"
    outside.mkdir()
    (outside / "profile.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(store.path_for("dusk"))
    store.path_for("dusk").symlink_to(outside, target_is_directory=True)

    result = run("voice", "rename", "dusk", "evening")
    assert result.exit_code == 5
    assert "symlink" in result.stderr.lower()
    assert store.path_for("dusk").is_symlink()
    assert not store.path_for("evening").exists()


def test_rename_refuses_symlinked_target(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    outside = isolated / "outside"
    outside.mkdir()
    store.path_for("evening").symlink_to(outside, target_is_directory=True)

    result = run("voice", "rename", "dusk", "evening")
    assert result.exit_code == 2
    assert "already exists" in result.stderr
    assert store.exists("dusk")
    assert outside.is_dir()


def test_rename_missing_voice(run, isolated: Path) -> None:
    result = run("voice", "rename", "ghost", "evening")
    assert result.exit_code == 2
    assert "voice not found" in result.stderr


def test_rename_repairs_crash_window(run, isolated: Path) -> None:
    """A profile that already names the new voice (interrupted before the
    directory rename) is repaired by re-running the same command."""
    store = _add_clone(run, isolated, "dusk")
    path = store.path_for("dusk") / "profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["name"] = "evening"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = run("voice", "rename", "dusk", "evening")
    assert result.exit_code == 0, result.output
    assert store.load("evening").name == "evening"
    assert not store.path_for("dusk").exists()


# ------------------------------------------------------------------------ tag


def test_tag_add_dedupes_and_preserves_other_fields(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm",))
    before = _payload(store, "dusk")

    result = run("voice", "tag", "dusk", "--add", "night", "--add", "calm")
    assert result.exit_code == 0, result.output
    assert "tags: calm, night" in result.output

    after = _payload(store, "dusk")
    assert after["tags"] == ["calm", "night"]
    for key, value in before.items():
        if key != "tags":
            assert after[key] == value


def test_tag_remove_is_idempotent(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm", "night"))
    result = run("voice", "tag", "dusk", "--remove", "night", "--remove", "missing")
    assert result.exit_code == 0, result.output
    assert "tags: calm" in result.output
    assert _payload(store, "dusk")["tags"] == ["calm"]

    again = run("voice", "tag", "dusk", "--remove", "missing")
    assert again.exit_code == 0, again.output
    assert _payload(store, "dusk")["tags"] == ["calm"]


def test_tag_set_replaces_and_dedupes(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm", "night"))
    result = run("voice", "tag", "dusk", "--set", "one", "--set", "two", "--set", "one")
    assert result.exit_code == 0, result.output
    assert "tags: one, two" in result.output
    assert _payload(store, "dusk")["tags"] == ["one", "two"]


def test_tag_can_add_and_remove_together(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm", "night"))
    result = run(
        "voice", "tag", "dusk", "--add", "narrator", "--remove", "calm"
    )
    assert result.exit_code == 0, result.output
    assert _payload(store, "dusk")["tags"] == ["night", "narrator"]


def test_tag_set_is_mutually_exclusive(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm",))
    result = run("voice", "tag", "dusk", "--set", "one", "--add", "two")
    assert result.exit_code == 2
    assert "cannot be combined" in result.stderr
    assert _payload(store, "dusk")["tags"] == ["calm"]


def test_tag_without_operations_lists_empty(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    path = store.path_for("dusk") / "profile.json"
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    result = run("voice", "tag", "dusk")
    assert result.exit_code == 0, result.output
    assert "voice: dusk" in result.output
    assert "tags: (none)" in result.output
    # Listing must not touch the profile.
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_tag_without_operations_lists_existing(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk", tags=("calm", "night"))
    result = run("voice", "tag", "dusk")
    assert result.exit_code == 0, result.output
    assert "tags: calm, night" in result.output


def test_tag_clear(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm", "night"))
    result = run("voice", "tag", "dusk", "--clear")
    assert result.exit_code == 0, result.output
    assert "tags: (none)" in result.output
    assert _payload(store, "dusk")["tags"] == []


def test_tag_clear_then_add(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm", "night"))
    result = run("voice", "tag", "dusk", "--clear", "--add", "fresh")
    assert result.exit_code == 0, result.output
    assert "tags: fresh" in result.output
    assert _payload(store, "dusk")["tags"] == ["fresh"]


@pytest.mark.parametrize(
    "args",
    [
        ("--clear", "--set", "one"),
        ("--clear", "--remove", "one"),
        ("--set", "one", "--remove", "two"),
    ],
)
def test_tag_conflicting_operations(run, isolated: Path, args: tuple[str, ...]) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm",))
    result = run("voice", "tag", "dusk", *args)
    assert result.exit_code == 2
    assert "cannot be combined" in result.stderr
    assert _payload(store, "dusk")["tags"] == ["calm"]


@pytest.mark.parametrize("bad_tag", ["", "   ", "x" * 65])
def test_tag_rejects_invalid_tags(run, isolated: Path, bad_tag: str) -> None:
    store = _add_clone(run, isolated, "dusk")
    result = run("voice", "tag", "dusk", "--add", bad_tag)
    assert result.exit_code == 2
    assert _payload(store, "dusk")["tags"] == []


def test_tag_missing_voice(run, isolated: Path) -> None:
    result = run("voice", "tag", "ghost", "--add", "calm")
    assert result.exit_code == 2
    assert "voice not found" in result.stderr


def _set_tags(store: VoiceStore, name: str, tags: list[str]) -> None:
    path = store.path_for(name) / "profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tags"] = tags
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_voice_add_normalizes_tags(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk", tags=(" padded ", "calm", "calm"))
    assert _payload(store, "dusk")["tags"] == ["padded", "calm"]


@pytest.mark.parametrize("bad_tag", ["", "   ", "x" * 65, "\x01bad"])
def test_voice_add_rejects_invalid_tags(
    run, isolated: Path, bad_tag: str
) -> None:
    ref = write_wav(isolated / "ref.wav", seconds=8.0)
    transcript = write_transcript(isolated / "ref.txt", "这是一段参考音频。")
    result = run(
        "voice",
        "add",
        "dusk",
        "--ref",
        str(ref),
        "--text",
        str(transcript),
        "--confirm-transcript",
        "--tag",
        bad_tag,
    )
    assert result.exit_code == 2
    assert not VoiceStore().exists("dusk")


def test_tag_remove_matches_legacy_unnormalized_tags(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    _set_tags(store, "dusk", [" padded ", "", "legacy"])

    result = run("voice", "tag", "dusk", "--remove", " padded ")
    assert result.exit_code == 0, result.output
    assert _payload(store, "dusk")["tags"] == ["legacy"]


def test_tag_add_dedupes_legacy_variant(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    _set_tags(store, "dusk", [" padded "])

    result = run("voice", "tag", "dusk", "--add", " padded ")
    assert result.exit_code == 0, result.output
    assert _payload(store, "dusk")["tags"] == ["padded"]


# --------------------------------------------------------------------- delete


def test_delete_with_yes(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    result = run("voice", "delete", "dusk", "--yes")
    assert result.exit_code == 0, result.output
    assert "deleted" in result.output
    assert not store.path_for("dusk").exists()

    listed = run("voice", "list")
    assert "dusk" not in listed.output
    missing = run("voice", "show", "dusk")
    assert missing.exit_code == 2
    assert "voice not found" in missing.stderr


def test_delete_without_yes_refuses_non_interactive(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    result = run("voice", "delete", "dusk")
    assert result.exit_code == 2
    assert "confirmation is required" in result.stderr
    # The path is printed before the refusal so the user can inspect it.
    # Rich may wrap the long path, so compare with newlines removed.
    assert str(store.path_for("dusk")) in result.output.replace("\n", "")
    assert store.exists("dusk")


def test_delete_interactive_decline(run, isolated: Path, monkeypatch) -> None:
    store = _add_clone(run, isolated, "dusk")
    monkeypatch.setattr("btts_cli.commands.voice._is_interactive", lambda: True)

    result = run("voice", "delete", "dusk", input="n\n")
    assert result.exit_code == 2
    assert "cancelled" in result.stderr
    assert store.exists("dusk")


def test_delete_interactive_accept(run, isolated: Path, monkeypatch) -> None:
    store = _add_clone(run, isolated, "dusk")
    monkeypatch.setattr("btts_cli.commands.voice._is_interactive", lambda: True)

    result = run("voice", "delete", "dusk", input="y\n")
    assert result.exit_code == 0, result.output
    assert not store.path_for("dusk").exists()


def test_delete_refuses_symlink(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    outside = isolated / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    shutil.rmtree(store.path_for("dusk"))
    store.path_for("dusk").symlink_to(outside, target_is_directory=True)

    result = run("voice", "delete", "dusk", "--yes")
    assert result.exit_code == 5
    assert "symlink" in result.stderr.lower()
    assert store.path_for("dusk").is_symlink()
    assert (outside / "keep.txt").is_file()


@pytest.mark.parametrize("bad_name", ["../outside", "", ".", "..", "*"])
def test_delete_rejects_escape_names(run, isolated: Path, bad_name: str) -> None:
    _add_clone(run, isolated, "dusk")
    result = run("voice", "delete", bad_name, "--yes")
    assert result.exit_code == 2
    assert "invalid voice name" in result.stderr
    assert VoiceStore().exists("dusk")


def test_delete_missing_voice(run, isolated: Path) -> None:
    result = run("voice", "delete", "ghost", "--yes")
    assert result.exit_code == 2
    assert "voice not found" in result.stderr


def test_voice_help_lists_management_commands(run) -> None:
    result = run("voice", "--help")
    assert result.exit_code == 0
    for command in ("add", "list", "show", "rename", "tag", "delete"):
        assert command in result.output


# ------------------------------------------------- crash windows and store state


def test_show_reports_rename_crash_window_forward(
    run, isolated: Path
) -> None:
    """dir=OLD/profile=NEW must exit 5 with a repair hint, not a traceback."""
    store = _add_clone(run, isolated, "dusk")
    path = store.path_for("dusk") / "profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["name"] = "evening"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = run("voice", "show", "dusk")
    assert result.exit_code == 5
    assert "Traceback" not in result.stderr
    assert "rename dusk evening" in result.stderr


def test_show_reports_rename_crash_window_reverse(
    run, isolated: Path
) -> None:
    """dir=NEW/profile=OLD must also exit 5 with a repair hint."""
    store = _add_clone(run, isolated, "evening")
    path = store.path_for("evening") / "profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["name"] = "dusk"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = run("voice", "show", "evening")
    assert result.exit_code == 5
    assert "Traceback" not in result.stderr
    assert "rename evening dusk" in result.stderr

    # Re-running rename in the direction of the profile name repairs it.
    repaired = run("voice", "rename", "evening", "dusk")
    assert repaired.exit_code == 0, repaired.stderr
    assert store.load("dusk").name == "dusk"
    assert not store.path_for("evening").exists()
    assert run("voice", "show", "dusk").exit_code == 0


@pytest.mark.parametrize(
    "args",
    [
        ("voice", "rename", "dusk", "evening"),
        ("voice", "tag", "dusk", "--add", "calm"),
        ("voice", "delete", "dusk", "--yes"),
    ],
)
def test_store_root_as_file_is_a_typed_error(
    run, isolated: Path, args: tuple[str, ...]
) -> None:
    store = VoiceStore()
    store.root.parent.mkdir(parents=True, exist_ok=True)
    store.root.write_text("not a directory", encoding="utf-8")

    result = run(*args)
    assert result.exit_code == 2
    assert "not a directory" in result.stderr
    assert "Traceback" not in result.stderr

    listed = run("voice", "list")
    assert listed.exit_code == 0
    assert "no voices yet" in listed.output


# ---------------------------------------------------------- symlink hardening


def test_delete_toctou_swap_cannot_touch_outside(
    run, isolated: Path, monkeypatch
) -> None:
    """A directory swapped for a symlink after validation must not escape."""
    store = _add_clone(run, isolated, "dusk")
    outside = isolated / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    moved = isolated / "moved"

    original = VoiceStore._voice_dir_or_raise

    def racing(self: VoiceStore, name: str) -> Path:
        directory = original(self, name)
        shutil.move(str(directory), str(moved))
        directory.symlink_to(outside, target_is_directory=True)
        return directory

    monkeypatch.setattr(VoiceStore, "_voice_dir_or_raise", racing)
    with pytest.raises(IntegrityError):
        store.delete("dusk")

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (moved / "profile.json").is_file()
    assert store.path_for("dusk").is_symlink()


@pytest.mark.parametrize("operation", ["rename", "tag"])
def test_profile_mutation_toctou_cannot_write_outside(
    run, isolated: Path, monkeypatch, operation: str
) -> None:
    """A swapped directory must not redirect the profile write outside the store."""
    store = _add_clone(run, isolated, "dusk")
    moved_profile = isolated / "moved" / "profile.json"
    outside = isolated / "outside"
    outside.mkdir()
    # A *valid* clone profile, so the degraded (path-based) fallback would
    # actually write to it instead of failing schema validation first.
    voice_dir = store.path_for("dusk")
    sentinel = outside / "profile.json"
    sentinel.write_text(
        (voice_dir / "profile.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for filename in ("ref.wav", "ref.txt"):
        shutil.copy2(voice_dir / filename, outside / filename)

    original = VoiceStore._voice_dir_or_raise

    def racing(self: VoiceStore, name: str) -> Path:
        directory = original(self, name)
        shutil.move(str(directory), str(isolated / "moved"))
        directory.symlink_to(outside, target_is_directory=True)
        return directory

    monkeypatch.setattr(VoiceStore, "_voice_dir_or_raise", racing)
    with pytest.raises(IntegrityError):
        if operation == "rename":
            store.rename("dusk", "evening")
        else:
            store.update_tags("dusk", add=["calm"])

    sentinel_payload = json.loads(sentinel.read_text(encoding="utf-8"))
    assert sentinel_payload["name"] == "dusk"
    assert sentinel_payload["tags"] == []
    assert moved_profile.is_file()
    assert json.loads(moved_profile.read_text())["name"] == "dusk"


def test_delete_does_not_follow_nested_symlinks(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    outside = isolated / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    directory = store.path_for("dusk")
    (directory / "link-dir").symlink_to(outside, target_is_directory=True)
    (directory / "link-file").symlink_to(outside / "keep.txt")

    result = run("voice", "delete", "dusk", "--yes")
    assert result.exit_code == 0, result.output
    assert (outside / "keep.txt").is_file()
    assert outside.is_dir()
    assert not store.path_for("dusk").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("voice", "rename", "dusk", "evening"),
        ("voice", "tag", "dusk", "--add", "calm"),
        ("voice", "delete", "dusk", "--yes"),
    ],
)
def test_broken_symlink_voice_dir_is_refused(
    run, isolated: Path, args: tuple[str, ...]
) -> None:
    store = _add_clone(run, isolated, "dusk")
    directory = store.path_for("dusk")
    shutil.rmtree(directory)
    directory.symlink_to(isolated / "missing", target_is_directory=True)

    result = run(*args)
    assert result.exit_code == 5
    assert "symlink" in result.stderr.lower()
    assert directory.is_symlink()


@pytest.mark.parametrize(
    "args",
    [
        ("voice", "rename", "dusk", "evening"),
        ("voice", "tag", "dusk", "--add", "calm"),
        ("voice", "delete", "dusk", "--yes"),
    ],
)
def test_symlinked_profile_is_refused(
    run, isolated: Path, args: tuple[str, ...]
) -> None:
    store = _add_clone(run, isolated, "dusk")
    outside = isolated / "outside-profile.json"
    outside.write_text('{"keep": true}', encoding="utf-8")
    profile = store.path_for("dusk") / "profile.json"
    profile.unlink()
    profile.symlink_to(outside)

    result = run(*args)
    assert result.exit_code == 5
    assert "symlink" in result.stderr.lower()
    assert outside.read_text(encoding="utf-8") == '{"keep": true}'


# --------------------------------------------------------- rename edge cases


def test_rename_rolls_back_profile_when_directory_rename_fails(
    run, isolated: Path, monkeypatch
) -> None:
    store = _add_clone(run, isolated, "dusk")

    def failing_rename(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(
        "btts_cli.domain.voice._rename_no_replace", failing_rename
    )

    result = run("voice", "rename", "dusk", "evening")
    assert result.exit_code == 5
    assert "profile was restored" in result.stderr
    assert _payload(store, "dusk")["name"] == "dusk"
    assert store.path_for("dusk").is_dir()
    assert not store.path_for("evening").exists()


def test_case_only_rename_has_clear_behavior(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "Dusk")
    case_insensitive = store.path_for("dusk").exists()

    result = run("voice", "rename", "Dusk", "dusk")
    if case_insensitive:
        assert result.exit_code == 2
        assert "case-insensitive" in result.stderr
        assert "intermediate name" in result.stderr
        assert store.load("Dusk").name == "Dusk"
    else:
        assert result.exit_code == 0, result.output
        assert store.load("dusk").name == "dusk"


def test_rename_target_symlink_to_source_is_already_exists(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dusk")
    store.path_for("evening").symlink_to(
        store.path_for("dusk"), target_is_directory=True
    )

    result = run("voice", "rename", "dusk", "evening")
    assert result.exit_code == 2
    assert "already exists" in result.stderr
    assert "case-insensitive" not in result.stderr
    assert store.exists("dusk")


@pytest.mark.parametrize("old", ["../escape", "", ".", "..", "@evil", "a/b"])
def test_rename_rejects_invalid_old_name(
    run, isolated: Path, old: str
) -> None:
    store = _add_clone(run, isolated, "dusk")
    result = run("voice", "rename", old, "evening")
    assert result.exit_code == 2
    assert "invalid voice name" in result.stderr
    assert store.exists("dusk")


# --------------------------------------------------- round 3: list / tombstones


def _make_tombstone(store: VoiceStore, name: str, *, design: bool = False) -> Path:
    directory = store.path_for(name)
    tombstone = store.root / f".deleting-{name}-deadbeef"
    directory.rename(tombstone)
    if design:
        payload = json.loads((tombstone / "profile.json").read_text())
        payload["kind"] = "design"
        payload["design"] = {
            "seed": 19,
            "cfg_scale": 2.5,
            "instruction": "test instruction",
            "max_new_tokens": 300,
        }
        payload["reference"] = None
        (tombstone / "profile.json").write_text(json.dumps(payload))
    return tombstone


@pytest.mark.parametrize("design", [False, True])
def test_list_skips_leftover_tombstones(
    run, isolated: Path, design: bool
) -> None:
    store = _add_clone(run, isolated, "dusk")
    tombstone = _make_tombstone(store, "dusk", design=design)

    result = run("voice", "list")
    assert result.exit_code == 0, result.stderr
    assert "invalid voice name" not in result.stderr
    assert "leftover delete tombstone" in result.stderr
    assert str(tombstone) in result.stderr.replace("\n", "")
    assert "no voices yet" in result.output


def test_permission_failure_tombstone_does_not_break_list(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dusk")
    directory = store.path_for("dusk")
    os.chmod(directory, 0o555)
    try:
        result = run("voice", "delete", "dusk", "--yes")
        assert result.exit_code == 5
        assert "leftover files were moved to" in result.stderr

        tombstones = [
            path
            for path in store.root.iterdir()
            if path.name.startswith(".deleting-")
        ]
        assert len(tombstones) == 1
        assert (tombstones[0] / "profile.json").is_file()

        listed = run("voice", "list")
        assert listed.exit_code == 0, listed.stderr
        assert "invalid voice name" not in listed.stderr
        assert "leftover delete tombstone" in listed.stderr
        assert "no voices yet" in listed.output
    finally:
        for path in store.root.iterdir():
            if path.is_dir() and path.name.startswith(".deleting-"):
                os.chmod(path, 0o755)
        if directory.exists():
            os.chmod(directory, 0o755)


def test_list_uses_directory_name_in_crash_window(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dusk")
    path = store.path_for("dusk") / "profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["name"] = "evening"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert [profile.name for profile in store.list()] == ["dusk"]

    result = run("voice", "list")
    assert result.exit_code == 0, result.stderr
    assert "voice directory 'dusk' contains profile name 'evening'" in result.stderr
    assert "rename dusk evening" in result.stderr


def test_tag_list_does_not_require_clone_reference(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm",))
    (store.path_for("dusk") / "ref.wav").unlink()

    listed = run("voice", "tag", "dusk")
    assert listed.exit_code == 0, listed.stderr
    assert "tags: calm" in listed.output

    # Mutations still validate reference files.
    mutated = run("voice", "tag", "dusk", "--add", "night")
    assert mutated.exit_code == 5


# ------------------------------------------------- round 3: rename target races


def test_rename_refuses_target_created_after_check(
    run, isolated: Path, monkeypatch
) -> None:
    store = _add_clone(run, isolated, "dusk")
    real = voice_module._rename_no_replace

    def racing(source: Path, target: Path) -> bool:
        target.mkdir()  # simulate a concurrent creator winning the name
        return real(source, target)

    monkeypatch.setattr(voice_module, "_rename_no_replace", racing)

    result = run("voice", "rename", "dusk", "evening")
    assert result.exit_code == 2
    assert "already exists" in result.stderr
    assert store.load("dusk").name == "dusk"
    assert store.path_for("evening").is_dir()
    assert list(store.path_for("evening").iterdir()) == []


def test_concurrent_save_and_rename_do_not_overwrite(
    run, isolated: Path, monkeypatch
) -> None:
    store = _add_clone(run, isolated, "a")
    original = store.load("a")
    added = original.model_copy(update={"voice_id": "v_b", "name": "b"})
    source_dir = store.path_for("a")
    files = {
        "ref.wav": source_dir / "ref.wav",
        "ref.txt": source_dir / "ref.txt",
        "preview.wav": source_dir / "ref.wav",
    }

    in_rename = threading.Event()
    allow_rename = threading.Event()
    real = voice_module._rename_no_replace

    def paused(source: Path, target: Path) -> bool:
        in_rename.set()
        assert allow_rename.wait(5)
        return real(source, target)

    monkeypatch.setattr(voice_module, "_rename_no_replace", paused)

    rename_errors: list[BaseException] = []
    save_errors: list[BaseException] = []

    def do_rename() -> None:
        try:
            store.rename("a", "b")
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            rename_errors.append(exc)

    def do_save() -> None:
        try:
            store.save(added, files)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            save_errors.append(exc)

    renamer = threading.Thread(target=do_rename)
    renamer.start()
    assert in_rename.wait(5), "rename never reached the atomic step"

    saver = threading.Thread(target=do_save)
    saver.start()
    # save must block on the store lock; it cannot create name "b" yet.
    assert not store.path_for("b").exists()

    allow_rename.set()
    renamer.join(5)
    saver.join(5)
    assert not renamer.is_alive() and not saver.is_alive()
    assert rename_errors == []
    assert len(save_errors) == 1
    assert isinstance(save_errors[0], UsageError)
    assert "already exists" in str(save_errors[0])

    # "b" is the renamed "a", not the newly added profile.
    renamed = store.load("b")
    assert renamed.voice_id == original.voice_id
    assert renamed.voice_id != "v_b"


@pytest.mark.parametrize("flag", ["--set=", "--remove="])
def test_tag_rejects_empty_option_value(
    run, isolated: Path, flag: str
) -> None:
    store = _add_clone(run, isolated, "dusk", tags=("calm",))
    result = run("voice", "tag", "dusk", flag)
    assert result.exit_code == 2
    assert _payload(store, "dusk")["tags"] == ["calm"]


# ------------------------------------- round 4: unreadable entries and roots


def test_list_skips_symlink_loop_reference(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    ref = store.path_for("dusk") / "ref.wav"
    ref.unlink()
    ref.symlink_to("ref.wav")  # ELOOP: resolve(strict=True) raises RuntimeError

    result = run("voice", "list")
    assert result.exit_code == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert "dawn" in result.output
    assert "dusk" not in result.output

    shown = run("voice", "show", "dusk")
    assert shown.exit_code == 5
    assert "Traceback" not in shown.stderr
    assert "unresolvable" in shown.stderr


def test_list_survives_unreadable_voice_directory(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    directory = store.path_for("dusk")
    os.chmod(directory, 0o000)
    try:
        result = run("voice", "list")
        assert result.exit_code == 0, result.stderr
        assert "Traceback" not in result.stderr
        assert "dawn" in result.output

        shown = run("voice", "show", "dusk")
        assert shown.exit_code == 2
        assert "Traceback" not in shown.stderr
        assert "cannot access voice" in shown.stderr
    finally:
        os.chmod(directory, 0o755)


def test_unreadable_store_root_is_a_typed_error(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    os.chmod(store.root, 0o000)
    try:
        result = run("voice", "list")
        assert result.exit_code == 2
        assert "cannot read voice store" in result.stderr
        assert "Traceback" not in result.stderr
    finally:
        os.chmod(store.root, 0o755)


def test_unreadable_data_dir_parent_is_a_typed_error(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dusk")
    # The voices root still exists and is stat-able by name, but its parent
    # denies search permission, so Path.is_dir() itself raises EACCES.
    parent = store.root.parent
    os.chmod(parent, 0o000)
    try:
        result = run("voice", "list")
        assert result.exit_code == 2
        assert "cannot read voice store" in result.stderr
        assert "Traceback" not in result.stderr
    finally:
        os.chmod(parent, 0o755)


@pytest.mark.skipif(
    not voice_module.SECURE_DIR_OPS, reason="POSIX fd-relative operations unavailable"
)
def test_delete_uses_fd_safe_removal_when_available(
    run, isolated: Path, monkeypatch
) -> None:
    """The secure POSIX path must not fall back to the path-based rmtree."""
    _add_clone(run, isolated, "dusk")

    def degraded(path: Path) -> None:
        raise AssertionError(f"degraded removal path used for {path}")

    monkeypatch.setattr(voice_module, "_remove_tree_no_follow", degraded)
    result = run("voice", "delete", "dusk", "--yes")
    assert result.exit_code == 0, result.stderr
    assert not VoiceStore().path_for("dusk").exists()
