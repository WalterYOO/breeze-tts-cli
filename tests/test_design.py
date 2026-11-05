from __future__ import annotations

import csv
import json
import shlex
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

from btts_cli.domain.design import (
    compile_design,
    design_from_template,
    dump_design_spec,
    has_inline_event,
    lint_design,
    load_design_spec,
)
from btts_cli.domain.run import DEFAULT_SEEDS
from tests.helpers import make_design


def test_init_writes_editable_yaml_and_lints(run, isolated: Path) -> None:
    design = isolated / "dusk.yaml"
    result = run("design", "init", str(design))
    assert result.exit_code == 0, result.output
    assert design.is_file()
    payload = yaml.safe_load(design.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["template"] == "zh-late-night-radio/v1"
    assert payload["identity"]["timbre"] == ["warm", "clean", "slightly_husky"]

    lint = run("design", "lint", str(design), "--show-rendered")
    assert lint.exit_code == 0, lint.output
    assert "100/100" in lint.output
    assert "三十多岁" in lint.output


def test_init_refuses_overwrite_without_force(run, isolated: Path) -> None:
    design = isolated / "dusk.yaml"
    assert run("design", "init", str(design)).exit_code == 0
    assert run("design", "init", str(design)).exit_code == 2
    assert run("design", "init", str(design), "--force").exit_code == 0


def test_init_supports_english_template(run, isolated: Path) -> None:
    design = isolated / "host.yaml"
    result = run(
        "design", "init", str(design), "--template", "en-podcast-host/v1", "--name", "host"
    )
    assert result.exit_code == 0, result.output
    spec = load_design_spec(design)
    assert spec.language == "en"
    compiled = compile_design(spec)
    assert "podcast host" in compiled.rendered_instruction


def test_lint_blocks_inline_event(run, isolated: Path) -> None:
    design = isolated / "bad.yaml"
    spec = design_from_template("zh-late-night-radio/v1", "bad")
    spec.identity.timbre = ["[笑]"]
    dump_design_spec(spec, design)

    result = run("design", "lint", str(design))
    assert result.exit_code == 1
    assert "inline-event" in result.output


def test_lint_blocks_language_mismatch(run, isolated: Path) -> None:
    design = isolated / "bad.yaml"
    spec = design_from_template("zh-late-night-radio/v1", "bad")
    spec.template = "en-podcast-host/v1"
    dump_design_spec(spec, design)

    result = run("design", "lint", str(design))
    assert result.exit_code == 2
    assert "language mismatch" in result.stderr


def test_lint_blocks_dialogue_pasted_into_descriptor(run, isolated: Path) -> None:
    design = isolated / "bad.yaml"
    spec = design_from_template("zh-late-night-radio/v1", "bad")
    spec.notes = "这是完整的一整段台词，本应该放在 covers 里而不是音色描述中。" * 3
    dump_design_spec(spec, design)

    report = lint_design(compile_design(load_design_spec(design)))
    assert report.blocked
    assert any(issue.code == "dialogue-in-instruction" for issue in report.issues)


def test_lint_reports_vague_terms(run, isolated: Path) -> None:
    design = isolated / "vague.yaml"
    spec = design_from_template("zh-late-night-radio/v1", "vague")
    spec.identity.timbre = ["高级"]
    dump_design_spec(spec, design)

    report = lint_design(compile_design(load_design_spec(design)))
    codes = {issue.code for issue in report.issues}
    assert "vague-term" in codes
    assert report.score < 100


def test_explore_default_matrix_writes_atomic_candidates(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "runs" / "dusk-01"

    result = run("design", "explore", str(design), "--out", str(run_dir))
    assert result.exit_code == 0, result.output
    assert len(fake.calls) == 4

    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert len(manifest["candidates"]) == 4
    seeds = manifest["matrix"]["seeds"]
    assert len(seeds) == 2
    assert len(set(seeds)) == 2
    assert all(seed in DEFAULT_SEEDS for seed in seeds)
    assert manifest["matrix"]["cfgs"] == [2.5, 4.0]
    assert manifest["completed"] == ["c001", "c002", "c003", "c004"]

    for take in ("c001", "c002", "c003", "c004"):
        assert (run_dir / "candidates" / f"{take}.wav").is_file()
        recipe = json.loads(
            (run_dir / "candidates" / f"{take}.recipe.json").read_text(encoding="utf-8")
        )
        assert recipe["command"] == "design.explore"
        assert recipe["operation"]["kind"] == "design"
        assert recipe["output"]["sha256"].startswith("sha256:")
        assert recipe["model"]["sample_rate"] == 24000
        assert "/" not in recipe["output"]["file"]
        assert str(isolated) not in json.dumps(recipe)

    assert str(isolated) not in (run_dir / "run.json").read_text(encoding="utf-8")


def test_explore_wide_generates_eight(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    result = run(
        "design", "explore", str(design), "--out", str(isolated / "run"), "--wide"
    )
    assert result.exit_code == 0, result.output
    assert len(fake.calls) == 8


def test_explore_large_matrix_requires_yes(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    args = [
        "design",
        "explore",
        str(design),
        "--out",
        str(isolated / "run"),
        "--seeds",
        "1,2,3,4,5,6,7,8,9",
        "--cfgs",
        "2,4",
    ]
    refused = run(*args)
    assert refused.exit_code == 2
    assert fake.calls == []

    allowed = run(*args, "--yes")
    assert allowed.exit_code == 0, allowed.output
    assert len(fake.calls) == 18


def test_explore_blocks_on_lint_error(run, fake, isolated: Path) -> None:
    design = isolated / "bad.yaml"
    spec = design_from_template("zh-late-night-radio/v1", "bad")
    spec.identity.timbre = ["[笑]"]
    dump_design_spec(spec, design)

    result = run("design", "explore", str(design), "--out", str(isolated / "run"))
    assert result.exit_code == 2
    assert fake.calls == []


def test_report_writes_csv_markdown_reel_and_html(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0

    result = run("design", "report", str(run_dir), "--metrics", "basic")
    assert result.exit_code == 0, result.output

    report = run_dir / "report"
    assert (report / "metrics.csv").is_file()
    assert (report / "report.md").is_file()
    assert (report / "all_candidates.wav").is_file()
    assert (report / "audition.html").is_file()

    with (report / "metrics.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["take_id"] for row in rows] == ["c001", "c002", "c003", "c004"]
    assert all(float(row["duration_s"]) > 0 for row in rows)

    html = (report / "audition.html").read_text(encoding="utf-8")
    assert "../candidates/c001.wav" in html
    assert "c004" in html
    assert "not an" in html.lower() or "aesthetic" in html.lower()

    page = parse_audition(html)
    assert page.audios == [
        "../candidates/c001.wav",
        "../candidates/c002.wav",
        "../candidates/c003.wav",
        "../candidates/c004.wav",
    ]
    checked = [radio for radio in page.radios if "checked" in radio]
    assert len(checked) == 1
    assert "Metric pick" in html
    assert "n/a" in html
    assert page.payload is not None
    assert checked[0]["value"] == page.payload["recommended"]
    assert len(page.payload["candidates"]) == 4
    for candidate in page.payload["candidates"]:
        assert candidate["text"]
        assert f"--take {candidate['take_id']}" in candidate["freeze_command"]
        assert "--as clone" in candidate["freeze_command"]
        assert "--confirm-transcript" not in candidate["freeze_command"]

    markdown = (report / "report.md").read_text(encoding="utf-8")
    assert "## Recommendation" in markdown
    assert "metric-based, not aesthetic" in markdown
    assert "metric pick (not aesthetic)" in result.output


@pytest.mark.parametrize(
    "text",
    [
        "(clears throat)",
        "[清嗓子]",
        "（叹气）",
        "【咳嗽】",
        "[]",
        "（ ）",
        "（笑声）",
        "[sighs softly]",
    ],
)
def test_inline_event_detection_covers_all_bracket_styles(text: str) -> None:
    assert has_inline_event(text)


def test_inline_event_detection_ignores_plain_text() -> None:
    assert not has_inline_event("你好，世界。请保持温暖、平静的语气。")
    assert not has_inline_event("（这不是事件描述）")
    assert not has_inline_event("Hello, world. Speak calmly.")


def test_wide_requires_two_covers(run, fake, isolated: Path) -> None:
    design = isolated / "single.yaml"
    spec = design_from_template("zh-late-night-radio/v1", "single")
    spec.covers = ["只有一句测试台词。"]
    dump_design_spec(spec, design)

    refused = run(
        "design", "explore", str(design), "--out", str(isolated / "wide"), "--wide"
    )
    assert refused.exit_code == 2
    assert "at least two cover" in refused.stderr
    assert fake.calls == []

    normal = run("design", "explore", str(design), "--out", str(isolated / "normal"))
    assert normal.exit_code == 0, normal.output
    assert len(fake.calls) == 4


@pytest.mark.parametrize("gap", ["-1", "31", "nan", "inf"])
def test_report_rejects_invalid_gap(run, fake, isolated: Path, gap: str) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0

    result = run("design", "report", str(run_dir), "--gap", gap)
    assert result.exit_code == 2
    assert "gap" in result.stderr.lower()
    assert not (run_dir / "report" / "metrics.csv").exists()


class AuditionPage(HTMLParser):
    """Collect the structural bits of audition.html we promise in tests."""

    def __init__(self) -> None:
        super().__init__()
        self.audios: list[str | None] = []
        self.radios: list[dict[str, str | None]] = []
        self.payload: dict[str, object] | None = None
        self._in_payload = False
        self._payload_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "audio":
            self.audios.append(values.get("src"))
        elif tag == "input" and values.get("name") == "take":
            self.radios.append(values)
        elif tag == "script" and values.get("id") == "audition-payload":
            self._in_payload = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_payload:
            self._in_payload = False
            self.payload = json.loads("".join(self._payload_chunks))

    def handle_data(self, data: str) -> None:
        if self._in_payload:
            self._payload_chunks.append(data)


def parse_audition(html: str) -> AuditionPage:
    page = AuditionPage()
    page.feed(html)
    return page


def test_audition_escapes_script_and_preserves_payload(run, fake, isolated: Path) -> None:
    design = isolated / "dusk.yaml"
    tricky = "</script><script>alert('x')</script> 引号\" & 符号\n第二行"
    spec = design_from_template("zh-late-night-radio/v1", "dusk")
    spec.covers = [tricky]
    dump_design_spec(spec, design)
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0
    assert run("design", "report", str(run_dir)).exit_code == 0

    html = (run_dir / "report" / "audition.html").read_text(encoding="utf-8")
    page = parse_audition(html)
    assert page.payload is not None
    assert page.payload["candidates"][0]["text"] == tricky
    assert html.count("</script>") == 2


def test_audition_freeze_command_quotes_path_and_guards_invalid_name(
    run, fake, isolated: Path
) -> None:
    design = make_design(isolated / "dusk.yaml", name="夜色")
    run_dir = isolated / "run with spaces; and $dollar"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0
    assert run("design", "report", str(run_dir)).exit_code == 0

    page = parse_audition((run_dir / "report" / "audition.html").read_text(encoding="utf-8"))
    assert page.payload is not None
    quoted_run = shlex.quote(str(run_dir.resolve()))
    for candidate in page.payload["candidates"]:
        command = candidate["freeze_command"]
        assert quoted_run in command
        assert "--name YOUR_VOICE_NAME" in command


def test_audition_uses_design_profile_for_inline_event_scripts(run, fake, isolated: Path) -> None:
    design = isolated / "dusk.yaml"
    spec = design_from_template("zh-late-night-radio/v1", "dusk")
    spec.covers = ["[笑] 你好，今天过得怎么样？"]
    dump_design_spec(spec, design)
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0
    assert run("design", "report", str(run_dir)).exit_code == 0

    page = parse_audition((run_dir / "report" / "audition.html").read_text(encoding="utf-8"))
    assert page.payload is not None
    assert all(
        "--as design" in candidate["freeze_command"] for candidate in page.payload["candidates"]
    )
