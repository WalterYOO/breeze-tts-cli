from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import btts_cli.domain.voice_export as export_module
from btts_cli.domain.voice import (
    DefaultsBlock,
    DesignBlock,
    IdentityBlock,
    ProvenanceBlock,
    VoiceProfile,
    VoiceStore,
    new_voice_id,
)
from btts_cli.output.errors import UsageError
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


def _add_design(store: VoiceStore, isolated: Path, name: str = "dusk") -> None:
    preview = write_wav(isolated / f"{name}-preview.wav", seconds=2.0)
    recipe = isolated / f"{name}-design.yaml"
    recipe.write_text("schema_version: 1\nname: dusk\n", encoding="utf-8")
    profile = VoiceProfile(
        voice_id=new_voice_id(),
        name=name,
        kind="design",
        language="zh",
        identity=IdentityBlock(
            structured={}, rendered_instruction="深夜电台女声，语速偏慢。"
        ),
        design=DesignBlock(
            seed=42, cfg_scale=4.0, instruction="深夜电台女声，语速偏慢。"
        ),
        defaults=DefaultsBlock(cfg_scale=4.0, seed=42),
        provenance=ProvenanceBlock(source="designed"),
    )
    store.save(profile, {"preview.wav": preview, "design.yaml": recipe})


def _manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _assert_hash(path: Path, entry: dict) -> None:
    actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert entry["sha256"] == actual
    assert entry["bytes"] == path.stat().st_size


def _assert_manifest_hashes(root: Path, manifest: dict) -> None:
    for entry in manifest["root_files"]:
        _assert_hash(root / entry["path"], entry)
    for voice in manifest["voices"]:
        for entry in voice["files"]:
            _assert_hash(root / entry["path"], entry)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ------------------------------------------------------------------ full exports


def test_export_all_full_layout_and_manifest(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dawn", tags=("bright",))
    _add_clone(run, isolated, "dusk", tags=("calm",))

    out = isolated / "bundle"
    result = run("voice", "export", "--all", "-o", str(out), "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["mode"] == "full"
    assert report["archive"] is False
    assert [voice["name"] for voice in report["voices"]] == ["dawn", "dusk"]

    for filename in ("manifest.json", "LICENSE.txt", "README.txt"):
        assert (out / filename).is_file()
    for voice in ("dawn", "dusk"):
        directory = out / voice
        for filename in (
            "profile.json",
            "LICENSE.txt",
            "preview.wav",
            "ref.wav",
            "ref.txt",
        ):
            assert (directory / filename).is_file(), (voice, filename)

    manifest = _manifest(out)
    assert manifest["kind"] == "btts-voice-export"
    assert manifest["mode"] == "full"
    assert manifest["exported_at"].endswith("Z")
    assert manifest["license"]["model"]
    _assert_manifest_hashes(out, manifest)
    assert [voice["name"] for voice in manifest["voices"]] == ["dawn", "dusk"]
    assert manifest["voices"][1]["tags"] == ["calm"]
    assert store.load("dusk").voice_id == manifest["voices"][1]["voice_id"]


def test_export_selected_names_accepts_at_and_dedupes(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    out = isolated / "selected"
    result = run(
        "voice", "export", "@dusk", "dusk", "dawn", "-o", str(out), "--json"
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert [voice["name"] for voice in report["voices"]] == ["dawn", "dusk"]


def test_export_design_voice_includes_recipe(run, isolated: Path) -> None:
    store = VoiceStore()
    _add_design(store, isolated)
    out = isolated / "design"
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 0, result.output
    assert (out / "dusk" / "design.yaml").is_file()
    manifest = _manifest(out)
    assert manifest["voices"][0]["kind"] == "design"


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--all", "dusk"),
        ("ghost",),
        ("../escape",),
    ],
)
def test_export_selection_errors_write_nothing(
    run, isolated: Path, args: tuple[str, ...]
) -> None:
    _add_clone(run, isolated, "dusk")
    out = isolated / "never"
    result = run("voice", "export", *args, "-o", str(out))
    assert result.exit_code == 2
    assert not out.exists()


def test_export_all_empty_store_is_usage_error(run, isolated: Path) -> None:
    out = isolated / "empty"
    result = run("voice", "export", "--all", "-o", str(out))
    assert result.exit_code == 2
    assert "no voices" in result.stderr
    assert not out.exists()


def test_export_all_fails_on_broken_voice_without_partial_output(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    (store.path_for("dawn") / "ref.wav").unlink()

    out = isolated / "partial"
    result = run("voice", "export", "--all", "-o", str(out))
    assert result.exit_code == 5
    assert "dawn" in result.stderr
    assert not out.exists()


def test_export_reports_name_directory_mismatch(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    path = store.path_for("dusk") / "profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["name"] = "evening"
    path.write_text(json.dumps(payload), encoding="utf-8")

    out = isolated / "mismatch"
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 5
    assert "voice rename dusk evening" in result.stderr
    assert not out.exists()


def test_export_missing_preview_is_integrity_error(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    (store.path_for("dusk") / "preview.wav").unlink()
    out = isolated / "no-preview"
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 5
    assert not out.exists()


def test_export_detects_transcript_changed_after_validation(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _add_clone(run, isolated, "dusk")
    original = export_module._verify_transcript_source

    def tamper(source, expected, label):
        digest = original(source, expected, label)
        path = store.path_for("dusk") / "ref.txt"
        text = path.read_text(encoding="utf-8")
        # Same byte length, different content: the copied raw hash must fail.
        path.write_text(text.replace("。", "！"), encoding="utf-8")
        return digest

    monkeypatch.setattr(export_module, "_verify_transcript_source", tamper)
    out = isolated / "tampered"
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 5
    assert not out.exists()


def test_export_unreadable_reference_is_integrity_error(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dusk")
    reference = store.path_for("dusk") / "ref.wav"
    os.chmod(reference, 0o000)
    try:
        out = isolated / "unreadable-ref"
        result = run("voice", "export", "dusk", "-o", str(out))
    finally:
        os.chmod(reference, 0o600)
    assert result.exit_code == 5
    assert "ref.wav" in result.stderr
    assert not out.exists()


def test_export_all_fails_on_inaccessible_voice(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    target = store.path_for("dusk")
    os.chmod(target, 0o000)
    try:
        out = isolated / "inaccessible"
        result = run("voice", "export", "--all", "-o", str(out))
    finally:
        os.chmod(target, 0o700)
    assert result.exit_code == 5
    assert "dusk" in result.stderr
    assert not out.exists()


def test_export_clone_hash_mismatch_is_integrity_error(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dusk")
    with (store.path_for("dusk") / "ref.wav").open("ab") as handle:
        handle.write(b"corrupt")
    out = isolated / "bad-hash"
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 5
    assert not out.exists()


# ----------------------------------------------------------------------- wav mode


def test_export_wav_mode_is_audio_only(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    out = isolated / "samples"
    result = run(
        "voice", "export", "dusk", "--mode", "wav", "-o", str(out), "--json"
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["mode"] == "wav"
    assert (out / "dusk.wav").is_file()
    assert not (out / "dusk").exists()
    assert not (out / "dusk" / "profile.json").exists()

    manifest = _manifest(out)
    assert manifest["mode"] == "wav"
    entry = manifest["voices"][0]["files"][0]
    assert entry["path"] == "dusk.wav"
    assert entry["source"] == "preview.wav"
    _assert_manifest_hashes(out, manifest)
    # WAV mode keeps the profile hash as metadata even though it is not exported.
    assert manifest["voices"][0]["profile_sha256"].startswith("sha256:")


def test_export_design_wav_mode(run, isolated: Path) -> None:
    store = VoiceStore()
    _add_design(store, isolated)
    out = isolated / "design-wav"
    result = run("voice", "export", "dusk", "--mode", "wav", "-o", str(out))
    assert result.exit_code == 0, result.output
    assert (out / "dusk.wav").is_file()
    assert not (out / "dusk").exists()


# -------------------------------------------------------------------------- zip


def test_export_zip_layout_and_manifest_hashes(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    archive = isolated / "voices.zip"
    result = run("voice", "export", "--all", "--zip", "-o", str(archive), "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["archive"] is True

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert names[0] == "voices/"
        assert "voices/manifest.json" in names
        assert "voices/dusk/profile.json" in names
        assert all(not name.startswith("/") and ".." not in name for name in names)
        manifest = json.loads(bundle.read("voices/manifest.json"))
        for entry in manifest["root_files"]:
            data = bundle.read(f"voices/{entry['path']}")
            assert entry["sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()
        for voice in manifest["voices"]:
            for entry in voice["files"]:
                data = bundle.read(f"voices/{entry['path']}")
                assert entry["sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()


def test_export_zip_requires_zip_extension(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    result = run("voice", "export", "dusk", "--zip", "-o", str(isolated / "plain"))
    assert result.exit_code == 2
    assert ".zip" in result.stderr
    assert not (isolated / "plain").exists()


def test_export_zip_create_failure_cleans_temp(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_clone(run, isolated, "dusk")

    def explode(self) -> None:
        raise OSError("simulated archive failure")

    monkeypatch.setattr(export_module._ZipBundle, "_add_directory_entry", explode)
    archive = isolated / "broken.zip"
    result = run("voice", "export", "dusk", "--zip", "-o", str(archive))
    assert result.exit_code == 2
    assert not archive.exists()
    leftovers = [
        path
        for path in isolated.iterdir()
        if path.name.startswith(f".{archive.name}.btts-")
    ]
    assert leftovers == []


def test_export_accepts_crlf_transcript(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    transcript = store.path_for("dusk") / "ref.txt"
    text = "这是一段参考音频。"
    transcript.write_bytes(text.encode("utf-8") + b"\r\n")

    out = isolated / "crlf"
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 0, result.output
    manifest = _manifest(out)
    entry = next(
        item
        for item in manifest["voices"][0]["files"]
        if item["path"] == "dusk/ref.txt"
    )
    assert entry["sha256"] == "sha256:" + hashlib.sha256(
        text.encode("utf-8") + b"\r\n"
    ).hexdigest()


def test_export_zip_clamps_pre_1980_clock(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_clone(run, isolated, "dusk")

    def fake_localtime(seconds=None):
        return (1979, 1, 1, 0, 0, 0, 0, 0, 0)

    monkeypatch.setattr(export_module.time, "localtime", fake_localtime)
    archive = isolated / "old-clock.zip"
    result = run("voice", "export", "dusk", "--zip", "-o", str(archive))
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(archive) as bundle:
        assert "old-clock/dusk/ref.txt" in bundle.namelist()


def test_export_zip_clamps_future_mtime(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    future = datetime(2200, 1, 1, tzinfo=timezone.utc).timestamp()
    transcript = store.path_for("dusk") / "ref.txt"
    os.utime(transcript, (future, future))

    archive = isolated / "future.zip"
    result = run("voice", "export", "dusk", "--zip", "-o", str(archive))
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(archive) as bundle:
        assert "future/dusk/ref.txt" in bundle.namelist()


# -------------------------------------------------------------------- destination


def test_export_refuses_existing_output(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    out = isolated / "existing"
    out.mkdir()
    marker = out / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 2
    assert "already exists" in result.stderr
    assert marker.read_text(encoding="utf-8") == "keep"


def test_export_refuses_broken_symlink_output(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    out = isolated / "broken-link"
    out.symlink_to(isolated / "missing")
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 2
    assert out.is_symlink()


def test_export_refuses_destination_inside_store(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    out = store.root / "export"
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 2
    assert "inside the voice store" in result.stderr
    assert not out.exists()


def test_export_refuses_symlinked_voice_directory(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    outside = isolated / "outside"
    outside.mkdir()
    shutil.move(str(store.path_for("dusk")), str(outside / "dusk"))
    store.path_for("dusk").symlink_to(outside / "dusk", target_is_directory=True)

    out = isolated / "symlink-dir"
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 5
    assert "symlink" in result.stderr.lower()
    assert not out.exists()


def test_export_refuses_symlinked_profile(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    profile = store.path_for("dusk") / "profile.json"
    copy = isolated / "profile.json"
    shutil.copyfile(profile, copy)
    profile.unlink()
    profile.symlink_to(copy)

    out = isolated / "symlink-profile"
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 5
    assert "symlink" in result.stderr.lower()
    assert not out.exists()


def test_export_refuses_symlink_inside_voice_tree(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    outside = write_wav(isolated / "outside.wav", seconds=1.0)
    (store.path_for("dusk") / "extra.wav").symlink_to(outside)

    out = isolated / "symlink-file"
    result = run("voice", "export", "dusk", "-o", str(out))
    assert result.exit_code == 5
    assert "symlink" in result.stderr.lower()
    assert outside.is_file()
    assert not out.exists()


# --------------------------------------------------------------- store hygiene


def test_export_does_not_modify_store(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    before = _tree_bytes(store.root)
    result = run("voice", "export", "--all", "-o", str(isolated / "copy"))
    assert result.exit_code == 0, result.output
    assert _tree_bytes(store.root) == before


def test_export_all_skips_tombstone_with_warning(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    tombstone = store.root / ".deleting-old-abc123"
    tombstone.mkdir()
    (tombstone / "junk.txt").write_text("junk", encoding="utf-8")

    out = isolated / "with-tombstone"
    result = run("voice", "export", "--all", "-o", str(out), "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert any("tombstone" in warning for warning in report["warnings"])
    assert not (out / ".deleting-old-abc123").exists()
    assert [voice["name"] for voice in report["voices"]] == ["dusk"]


def test_export_json_is_single_document(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    result = run(
        "voice", "export", "dusk", "-o", str(isolated / "json-out"), "--json"
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["kind"] == "btts-voice-export"
    assert report["manifest_sha256"].startswith("sha256:")
    assert "License" not in result.stdout
    assert "non-commercial" not in result.stdout


def test_export_partial_failure_cleans_temp(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    original = export_module._DirectoryBundle.add_file
    calls = {"count": 0}

    def failing(self, relpath, source, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise UsageError("simulated copy failure")
        return original(self, relpath, source, **kwargs)

    monkeypatch.setattr(export_module._DirectoryBundle, "add_file", failing)

    out = isolated / "interrupted"
    result = run("voice", "export", "--all", "-o", str(out))
    assert result.exit_code == 2
    assert not out.exists()
    leftovers = [
        path
        for path in isolated.iterdir()
        if path.name.startswith(f".{out.name}.btts-")
    ]
    assert leftovers == []
