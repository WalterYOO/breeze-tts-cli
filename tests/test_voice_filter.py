from __future__ import annotations

import json
from pathlib import Path

from btts_cli.domain.voice import (
    DefaultsBlock,
    DesignBlock,
    IdentityBlock,
    ProvenanceBlock,
    VoiceProfile,
    VoiceStore,
    new_voice_id,
)
from tests.helpers import write_transcript, write_wav


def _add_clone(
    run,
    isolated: Path,
    name: str,
    *,
    language: str = "zh",
    tags: tuple[str, ...] = (),
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
        "--language",
        language,
    ]
    for tag in tags:
        args.extend(["--tag", tag])
    result = run(*args)
    assert result.exit_code == 0, result.output
    return VoiceStore()


def _write_design_profile(
    store: VoiceStore,
    name: str,
    *,
    voice_id: str | None = None,
) -> Path:
    """Write a design profile directly, bypassing name validation."""
    profile = VoiceProfile(
        voice_id=voice_id or new_voice_id(),
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
    path = directory / "profile.json"
    path.write_text(
        json.dumps(profile.model_dump(mode="json"), ensure_ascii=True),
        encoding="utf-8",
    )
    return path


def _add_design(
    store: VoiceStore, isolated: Path, name: str, *, language: str = "zh"
) -> None:
    preview = write_wav(isolated / f"{name}-preview.wav", seconds=2.0)
    recipe = isolated / f"{name}-design.yaml"
    recipe.write_text("schema_version: 1\nname: design\n", encoding="utf-8")
    profile = VoiceProfile(
        voice_id=new_voice_id(),
        name=name,
        kind="design",
        language=language,
        identity=IdentityBlock(structured={}, rendered_instruction="测试音色。"),
        design=DesignBlock(seed=42, cfg_scale=4.0, instruction="测试音色。"),
        defaults=DefaultsBlock(cfg_scale=4.0, seed=42),
        provenance=ProvenanceBlock(source="designed"),
    )
    store.save(profile, {"preview.wav": preview, "design.yaml": recipe})


# --------------------------------------------------------------------- matching


def test_list_fuzzy_is_case_insensitive_substring(run, isolated: Path) -> None:
    _add_clone(run, isolated, "std-girl")
    _add_clone(run, isolated, "std-boy")
    _add_clone(run, isolated, "narrator")

    result = run("voice", "list", "girl", "--names-only")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["std-girl"]

    upper = run("voice", "list", "GIRL", "--names-only")
    assert upper.exit_code == 0, upper.output
    assert upper.stdout.splitlines() == ["std-girl"]


def test_list_fuzzy_does_not_match_subsequence(run, isolated: Path) -> None:
    _add_clone(run, isolated, "std-girl")
    result = run("voice", "list", "g-r-l", "--names-only")
    assert result.exit_code == 0, result.output
    assert result.stdout == ""


def test_list_prefix_is_case_insensitive(run, isolated: Path) -> None:
    _add_clone(run, isolated, "std-girl")
    _add_clone(run, isolated, "std-boy")
    _add_clone(run, isolated, "narrator")

    result = run("voice", "list", "--match", "prefix", "STD-", "--names-only")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["std-boy", "std-girl"]


def test_list_exact_is_full_name_and_case_insensitive(run, isolated: Path) -> None:
    _add_clone(run, isolated, "std-girl")
    _add_clone(run, isolated, "narrator")

    partial = run("voice", "list", "--match", "exact", "std", "--names-only")
    assert partial.exit_code == 0, partial.output
    assert partial.stdout == ""

    exact = run("voice", "list", "--match", "exact", "STD-GIRL", "--names-only")
    assert exact.exit_code == 0, exact.output
    assert exact.stdout.splitlines() == ["std-girl"]


def test_query_normalizes_whitespace_and_at_prefix(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    result = run("voice", "list", " @dusk ", "--names-only")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["dusk"]


def test_empty_effective_query_is_usage_error(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    for query in ("", "   ", "@", "  @  "):
        result = run("voice", "list", query)
        assert result.exit_code == 2, (query, result.output)
        assert "empty" in result.stderr


# ------------------------------------------------------------ structured filters


def test_structured_filters_are_anded_with_query(run, isolated: Path) -> None:
    _add_clone(run, isolated, "std-girl", language="zh")
    _add_clone(run, isolated, "std-boy", language="en")
    store = VoiceStore()
    _add_design(store, isolated, "std-narrator")

    result = run(
        "voice",
        "list",
        "std-",
        "--kind",
        "clone",
        "--language",
        "en",
        "--names-only",
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["std-boy"]


def test_multiple_tags_require_all_tags(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk", tags=("calm", "night"))
    _add_clone(run, isolated, "dawn", tags=("calm",))

    result = run(
        "voice", "list", "--tag", "calm", "--tag", "night", "--names-only"
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["dusk"]


def test_invalid_tag_is_usage_error(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    result = run("voice", "list", "--tag", "   ")
    assert result.exit_code == 2
    assert "tag" in result.stderr.lower()


# ------------------------------------------------------------------- output modes


def test_list_without_criteria_keeps_existing_table(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    result = run("voice", "list")
    assert result.exit_code == 0, result.output
    assert "voices" in result.output
    assert "dusk" in result.output
    assert "store:" in result.output


def test_names_only_is_clean_sorted_and_has_no_footer(
    run, isolated: Path
) -> None:
    _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    result = run("voice", "list", "--names-only")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["dawn", "dusk"]
    assert "store:" not in result.stdout
    assert "voices" not in result.stdout


def test_json_is_one_document_with_stable_shape(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk", tags=("calm",))
    _add_clone(run, isolated, "dawn")
    result = run("voice", "list", "dusk", "--tag", "calm", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["kind"] == "btts-voice-list"
    assert payload["query"] == "dusk"
    assert payload["match"] == "fuzzy"
    assert payload["filters"] == {"tags": ["calm"], "kind": None, "language": None}
    assert payload["count"] == 1
    assert payload["voices"][0]["name"] == "dusk"
    assert payload["voices"][0]["cfg_scale"] == 1.0
    assert payload["voices"][0]["tags"] == ["calm"]
    assert payload["warnings"] == []


def test_no_matches_is_exit_zero(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")

    table = run("voice", "list", "zzz")
    assert table.exit_code == 0, table.output
    assert "no matching voices" in table.output

    names = run("voice", "list", "zzz", "--names-only")
    assert names.exit_code == 0, names.output
    assert names.stdout == ""

    payload = run("voice", "list", "zzz", "--json")
    assert payload.exit_code == 0, payload.output
    report = json.loads(payload.stdout)
    assert report["count"] == 0
    assert report["voices"] == []


def test_names_only_and_json_are_mutually_exclusive(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk")
    result = run("voice", "list", "--names-only", "--json")
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr
    assert result.stdout == ""


def test_empty_store_message_is_preserved(run, isolated: Path) -> None:
    result = run("voice", "list")
    assert result.exit_code == 0, result.output
    assert "no voices yet" in result.output


# ------------------------------------------------------------------ scan hygiene


def test_filter_alias_matches_list(run, isolated: Path) -> None:
    _add_clone(run, isolated, "std-girl")
    _add_clone(run, isolated, "narrator")

    listed = run("voice", "list", "girl", "--json")
    filtered = run("voice", "filter", "girl", "--json")
    assert listed.exit_code == 0, listed.output
    assert filtered.exit_code == 0, filtered.output
    assert json.loads(listed.stdout)["voices"] == json.loads(filtered.stdout)["voices"]


def test_list_keeps_tombstone_and_mismatch_warnings(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    (store.root / ".deleting-old-abc123").mkdir()
    profile_path = store.path_for("dusk") / "profile.json"
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    payload["name"] = "evening"
    profile_path.write_text(json.dumps(payload), encoding="utf-8")

    result = run("voice", "list", "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["voices"][0]["name"] == "dusk"
    assert any("tombstone" in warning for warning in report["warnings"])
    assert any("voice rename dusk evening" in warning for warning in report["warnings"])
    assert "tombstone" in result.stderr


def test_list_skips_broken_voice_without_traceback(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    _add_clone(run, isolated, "dawn")
    (store.path_for("dusk") / "ref.wav").unlink()

    result = run("voice", "list", "--names-only")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["dawn"]
    assert "Traceback" not in result.output


def test_list_escapes_rich_markup_in_tags(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk", tags=("[red]evil",))

    result = run("voice", "list")
    assert result.exit_code == 0, result.output
    assert "[red]evil" in result.output


def test_names_only_skips_symlinked_voice(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    (store.root / "linked").symlink_to(
        store.path_for("dusk"), target_is_directory=True
    )

    result = run("voice", "list", "--names-only")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["dusk"]
    assert "symlinked voice" in result.stderr


def test_query_never_matches_unaddressable_directory_name(
    run, isolated: Path
) -> None:
    store = _add_clone(run, isolated, "dusk")
    _write_design_profile(store, "bad name")

    listing = run("voice", "list", "--json")
    assert listing.exit_code == 0, listing.output
    report = json.loads(listing.stdout)
    assert {voice["name"] for voice in report["voices"]} == {"dusk", "bad name"}
    assert any("not addressable" in warning for warning in report["warnings"])

    query = run("voice", "list", "bad", "--json")
    assert query.exit_code == 0, query.output
    assert json.loads(query.stdout)["count"] == 0

    names = run("voice", "list", "bad", "--names-only")
    assert names.exit_code == 0, names.output
    assert names.stdout == ""

    all_names = run("voice", "list", "--names-only")
    assert all_names.exit_code == 0, all_names.output
    assert all_names.stdout.splitlines() == ["dusk"]
    assert "unaddressable" in all_names.stderr


def test_mismatch_repair_hint_quotes_shell_metacharacters() -> None:
    from btts_cli.commands.voice import _scan_warnings

    warnings = _scan_warnings([], [("dusk", "evening; touch /tmp/PWNED")])
    assert "rename dusk 'evening; touch /tmp/PWNED'" in warnings[0]
    assert "rename dusk evening; touch" not in warnings[0]

    spaced = _scan_warnings([], [("bad name", "evening")])
    assert "rename 'bad name' evening" in spaced[0]


def test_table_preserves_zwj_emoji(run, isolated: Path) -> None:
    _add_clone(run, isolated, "dusk", tags=("👩‍💻",))

    result = run("voice", "list")
    assert result.exit_code == 0, result.output
    assert "👩‍💻" in result.output


def test_store_path_is_sanitized(run, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BTTS_DATA_DIR", str(tmp_path / "data\x1b[2J"))

    result = run("voice", "list", "zzz")
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output
    assert "\\x1b[2J" in result.output


def test_table_sanitizes_control_characters(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    profile_path = store.path_for("dusk") / "profile.json"
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    payload["voice_id"] = "v_\x1b[2Jevil"
    profile_path.write_text(
        json.dumps(payload, ensure_ascii=True), encoding="utf-8"
    )

    result = run("voice", "list")
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output
    assert "v_\\x1b[2Jevil" in result.output


def test_json_survives_lone_surrogate(run, isolated: Path) -> None:
    store = _add_clone(run, isolated, "dusk")
    profile_path = store.path_for("dusk") / "profile.json"
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    payload["voice_id"] = "v_\ud800bad"
    profile_path.write_text(
        json.dumps(payload, ensure_ascii=True), encoding="utf-8"
    )

    result = run("voice", "list", "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["voices"][0]["voice_id"] == "v_\ud800bad"

    table = run("voice", "list")
    assert table.exit_code == 0, table.output
    assert "v_?bad" in table.output
