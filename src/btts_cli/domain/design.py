"""Structured voice designs: schema, template compiler and deterministic lint.

A design file is user intent. The compiler turns it into the exact instruction
string sent to Breeze and records the template name plus compiler version so an
older recipe stays traceable after a template upgrade.
"""

from __future__ import annotations

import copy
import functools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from btts_cli.output.errors import UsageError

SCHEMA_VERSION = 1
RESOURCE_DIR = Path(__file__).resolve().parents[1] / "resources"
TEMPLATES_PATH = RESOURCE_DIR / "design_templates.yaml"

LANGUAGES = ("zh", "en")
CJK_RE = re.compile(r"[一-鿿]")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Bracket tokens such as ``[laugh]``, ``(clears throat)``, ``（叹气）`` and
# ``【咳嗽】`` are inline performance events. A finite verb list is not enough:
# any bracketed token whose content is (or contains) an event term is treated as
# an event, and empty brackets are rejected too.
BRACKET_RE = re.compile(r"[\[\(（【]([^\[\]\(\)（）【】]*)[\]\)）】]")
EVENT_TERMS = (
    "laugh",
    "chuckle",
    "giggle",
    "sigh",
    "breath",
    "cough",
    "sob",
    "cry",
    "crying",
    "sniff",
    "scream",
    "gasp",
    "grunt",
    "clears throat",
    "throat",
    "silence",
    "pause",
    "music",
    "noise",
    "applause",
    "sound effect",
    "笑",
    "叹气",
    "叹息",
    "咳嗽",
    "清嗓",
    "呼吸",
    "哽咽",
    "哭",
    "抽泣",
    "吸气",
    "沉默",
    "停顿",
    "音乐",
    "掌声",
    "噪音",
    "音效",
    "旁白",
)


def inline_events(text: str) -> list[str]:
    """Return bracket tokens that describe an inline performance event."""
    events: list[str] = []
    for match in BRACKET_RE.finditer(text):
        inner = match.group(1).strip().casefold()
        if not inner or any(term in inner for term in EVENT_TERMS):
            events.append(match.group(0))
    return events


def has_inline_event(text: str) -> bool:
    return bool(inline_events(text))

VAGUE_TERMS = {
    "zh": (
        "高级",
        "有质感",
        "专业",
        "好听",
        "完美",
        "顶级",
        "舒服",
        "情感丰富",
        "真实感",
        "氛围感",
    ),
    "en": (
        "professional",
        "high quality",
        "high-quality",
        "premium",
        "studio quality",
        "perfect",
        "best",
        "emotional",
        "realistic",
    ),
}


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    age: str | None = None
    gender_presentation: str | None = None
    timbre: list[str] = Field(default_factory=list)
    pitch: str | None = None
    articulation: str | None = None
    persona: str | None = None

    @field_validator("timbre", mode="before")
    @classmethod
    def _split_timbre(cls, value: Any) -> Any:
        return _split_list(value)

    def is_empty(self) -> bool:
        return not any(
            [
                self.age,
                self.gender_presentation,
                self.timbre,
                self.pitch,
                self.articulation,
                self.persona,
            ]
        )


class DeliveryBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pace: str | None = None
    energy: str | None = None
    tone: str | None = None

    def is_empty(self) -> bool:
        return not any([self.pace, self.energy, self.tone])


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proximity: str | None = None
    room: str | None = None

    def is_empty(self) -> bool:
        return not any([self.proximity, self.room])


class DesignSpec(BaseModel):
    """Editable YAML schema loaded by ``design lint/explore`` and ``say``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    name: str
    language: str = "zh"
    template: str
    identity: Identity = Field(default_factory=Identity)
    delivery_baseline: DeliveryBaseline = Field(default_factory=DeliveryBaseline)
    scene: Scene = Field(default_factory=Scene)
    avoid: list[str] = Field(default_factory=list)
    covers: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _check_schema(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {value}; expected {SCHEMA_VERSION}")
        return value

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        if value not in LANGUAGES:
            raise ValueError(f"language must be one of {LANGUAGES}")
        return value

    @field_validator("avoid", "covers", mode="before")
    @classmethod
    def _split_lines(cls, value: Any) -> Any:
        return _split_list(value)


@dataclass(frozen=True)
class UnknownTerm:
    kind: str
    value: str
    fallback: str


@dataclass(frozen=True)
class CompiledDesign:
    spec: DesignSpec
    structured: dict[str, Any]
    rendered_instruction: str
    template: str
    compiler_version: str
    language: str
    covers: list[str]
    unknown_terms: tuple[UnknownTerm, ...] = ()


@dataclass
class TemplateInfo:
    name: str
    language: str
    label: str
    description: str
    render: str
    default_covers: list[str]
    starter: dict[str, Any]


@dataclass
class TemplateRegistry:
    compiler_version: str
    defaults: dict[str, str]
    vocab: dict[str, dict[str, dict[str, str]]]
    templates: dict[str, TemplateInfo]

    def get(self, name: str) -> TemplateInfo:
        try:
            return self.templates[name]
        except KeyError as exc:
            raise UsageError(
                f"unknown design template: {name}",
                hint=f"available templates: {', '.join(sorted(self.templates))}",
            ) from exc

    def names(self) -> list[str]:
        return sorted(self.templates)

    def default_for(self, language: str) -> TemplateInfo:
        return self.get(self.defaults[language])


@functools.lru_cache(maxsize=1)
def load_registry() -> TemplateRegistry:
    try:
        raw = yaml.safe_load(TEMPLATES_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:  # pragma: no cover - packaging failure
        raise RuntimeError(f"cannot load design templates from {TEMPLATES_PATH}") from exc

    templates: dict[str, TemplateInfo] = {}
    for name, data in raw["templates"].items():
        templates[name] = TemplateInfo(
            name=name,
            language=data["language"],
            label=data.get("label", name),
            description=data.get("description", ""),
            render=data["render"],
            default_covers=list(data.get("default_covers", [])),
            starter=dict(data.get("starter", {})),
        )
    return TemplateRegistry(
        compiler_version=str(raw.get("compiler_version", "1")),
        defaults=dict(raw["defaults"]),
        vocab=raw["vocab"],
        templates=templates,
    )


def template_names() -> list[str]:
    return load_registry().names()


def load_design_spec(path: Path) -> DesignSpec:
    if not path.is_file():
        raise UsageError(f"design file not found: {path.name}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise UsageError(
            f"invalid YAML in {path.name}{where}: {getattr(exc, 'problem', exc)}"
        ) from exc
    except OSError as exc:
        raise UsageError(f"cannot read {path.name}: {exc}") from exc
    if raw is None:
        raise UsageError(f"{path.name} is empty")
    if not isinstance(raw, dict):
        raise UsageError(f"{path.name} must contain a YAML mapping")
    try:
        return DesignSpec.model_validate(raw)
    except ValidationError as exc:
        details = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            details.append(f"{location}: {error['msg']}")
        raise UsageError(
            f"invalid design file {path.name}",
            hint="; ".join(details),
        ) from exc


def dump_design_spec(spec: DesignSpec, path: Path) -> None:
    from btts_cli.output.json import write_text_atomic

    payload = spec.model_dump(mode="json", exclude_none=True)
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, text)


def design_from_template(
    template_name: str | None, name: str, language: str | None = None
) -> DesignSpec:
    registry = load_registry()
    if template_name:
        template = registry.get(template_name)
        if language and template.language != language:
            raise UsageError(
                f"template {template_name} is {template.language}, but --language is {language}"
            )
    else:
        if language is None:
            language = "zh"
        template = registry.default_for(language)
    starter = copy.deepcopy(template.starter)
    starter["name"] = name
    starter["language"] = template.language
    starter["template"] = template.name
    starter.setdefault("covers", list(template.default_covers))
    return DesignSpec.model_validate({"schema_version": SCHEMA_VERSION, **starter})


def compile_design(spec: DesignSpec) -> CompiledDesign:
    registry = load_registry()
    template = registry.get(spec.template)
    if template.language != spec.language:
        raise UsageError(
            f"language mismatch: design language is {spec.language} "
            f"but template {spec.template} renders {template.language}",
            hint="use a template in the same language as the target text",
        )

    vocab = registry.vocab[spec.language]
    sep = "、" if spec.language == "zh" else ", "
    unknown: list[UnknownTerm] = []

    def phrase(kind: str, value: str | None) -> str:
        if value is None:
            return ""
        cleaned = value.strip()
        if not cleaned:
            return ""
        mapped = vocab.get(kind, {}).get(cleaned)
        if mapped is not None:
            return mapped
        fallback = cleaned.replace("_", " ")
        unknown.append(UnknownTerm(kind=kind, value=cleaned, fallback=fallback))
        return fallback

    def phrase_list(kind: str, values: list[str]) -> str:
        return sep.join(phrase(kind, value) for value in values if value and value.strip())

    identity = spec.identity
    delivery = spec.delivery_baseline
    scene = spec.scene

    context = {
        "name": spec.name,
        "language": spec.language,
        "identity": {
            "age": phrase("age", identity.age),
            "gender": phrase("gender", identity.gender_presentation),
            "timbre": phrase_list("timbre", identity.timbre),
            "pitch": phrase("pitch", identity.pitch),
            "articulation": phrase_list("articulation", _split_text(identity.articulation)),
            "persona": phrase("persona", identity.persona),
        },
        "delivery": {
            "pace": phrase("pace", delivery.pace),
            "energy": phrase("energy", delivery.energy),
            "tone": phrase_list("tone", _split_text(delivery.tone)),
        },
        "scene": {
            "proximity": phrase("proximity", scene.proximity),
            "room": phrase("room", scene.room),
        },
        "avoid": phrase_list("avoid", spec.avoid),
    }

    env = Environment(
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        autoescape=False,
    )
    rendered = env.from_string(template.render).render(**context)
    rendered = _normalise_instruction(rendered, spec.language)

    covers = [cover for cover in spec.covers if cover and cover.strip()]
    if not covers:
        covers = list(template.default_covers)

    structured = spec.model_dump(mode="json", exclude_none=True)
    structured["covers"] = covers

    return CompiledDesign(
        spec=spec,
        structured=structured,
        rendered_instruction=rendered,
        template=template.name,
        compiler_version=registry.compiler_version,
        language=spec.language,
        covers=covers,
        unknown_terms=tuple(unknown),
    )


# --------------------------------------------------------------------------- lint


@dataclass
class LintIssue:
    severity: str  # block | warn | info
    code: str
    message: str


@dataclass
class LintReport:
    score: int
    dimensions: dict[str, int]
    issues: list[LintIssue] = field(default_factory=list)
    rendered_instruction: str = ""

    @property
    def blocked(self) -> bool:
        return any(issue.severity == "block" for issue in self.issues)

    def counts(self) -> dict[str, int]:
        result = {"block": 0, "warn": 0, "info": 0}
        for issue in self.issues:
            result[issue.severity] += 1
        return result


def lint_design(compiled: CompiledDesign) -> LintReport:
    spec = compiled.spec
    text = compiled.rendered_instruction
    issues: list[LintIssue] = []

    def add(severity: str, code: str, message: str) -> None:
        issues.append(LintIssue(severity=severity, code=code, message=message))

    # --- hard errors -------------------------------------------------------
    if not text.strip():
        add("block", "empty-instruction", "compiled instruction is empty; add perceptual anchors")
    events = inline_events(text)
    if events:
        add(
            "block",
            "inline-event",
            "instruction contains inline event markers "
            f"({', '.join(events[:3])}); events belong in the spoken text, "
            "not the voice description",
        )

    if spec.language == "zh" and text and not CJK_RE.search(text):
        add(
            "block",
            "language-mismatch",
            "design language is zh but the instruction has no Chinese",
        )
    if spec.language == "en" and CJK_RE.search(text):
        add(
            "block",
            "language-mismatch",
            "design language is en but the instruction contains Chinese",
        )

    for path, value in _leaf_strings(spec):
        if len(value) > 60:
            add(
                "block",
                "dialogue-in-instruction",
                f"{path} is {len(value)} characters long; keep descriptors short and put "
                "dialogue lines in covers",
            )

    if spec.identity.is_empty() and spec.delivery_baseline.is_empty() and spec.scene.is_empty():
        add("block", "no-anchors", "no identity, delivery or scene fields are set")

    # --- specificity -------------------------------------------------------
    timbre_count = len([item for item in spec.identity.timbre if item.strip()])
    anchors = {
        "age": 15 if spec.identity.age else 0,
        "gender": 10 if spec.identity.gender_presentation else 0,
        "timbre": 20 if timbre_count >= 2 else (10 if timbre_count == 1 else 0),
        "pitch": 15 if spec.identity.pitch else 0,
        "articulation": 10 if spec.identity.articulation else 0,
        "persona": 10 if spec.identity.persona else 0,
        "pace": 5 if spec.delivery_baseline.pace else 0,
        "energy": 5 if spec.delivery_baseline.energy else 0,
        "tone": 5 if spec.delivery_baseline.tone else 0,
        "scene": 5
        if spec.scene.proximity and spec.scene.room
        else (2 if spec.scene.proximity or spec.scene.room else 0),
    }
    specificity = min(100, sum(anchors.values()))
    missing = [name for name, score in anchors.items() if score == 0]
    if missing:
        add("warn", "missing-anchors", "no value for: " + ", ".join(missing))

    # --- executability -----------------------------------------------------
    executability = 100
    lowered = text.lower()
    for term in VAGUE_TERMS[spec.language]:
        if term.lower() in lowered:
            executability -= 25
            add(
                "warn",
                "vague-term",
                f"vague term '{term}' is hard to map to an acoustic target",
            )
    for term in compiled.unknown_terms:
        executability -= 5
        add(
            "info",
            "unknown-vocab",
            f"{term.kind} value '{term.value}' has no phrase mapping; "
            f"rendered as '{term.fallback}'",
        )
    executability = max(0, executability)

    # --- consistency -------------------------------------------------------
    consistency = 100
    for message in _conflicts(spec):
        consistency -= 40
        add("warn", "conflict", message)
    consistency = max(0, consistency)

    # --- language match ----------------------------------------------------
    language_score = 100
    if text and spec.language == "zh" and not CJK_RE.search(text):
        language_score = 0
    if text and spec.language == "en" and CJK_RE.search(text):
        language_score = 0
    for index, cover in enumerate(spec.covers, start=1):
        if not cover.strip():
            continue
        if spec.language == "zh" and not CJK_RE.search(cover):
            language_score = max(0, language_score - 30)
            add("warn", "cover-language", f"cover {index} is not Chinese")
        if spec.language == "en" and CJK_RE.search(cover):
            language_score = max(0, language_score - 30)
            add("warn", "cover-language", f"cover {index} contains Chinese")

    # --- testability -------------------------------------------------------
    testability = 0
    if compiled.covers:
        testability += 40
        if len(compiled.covers) >= 2:
            testability += 10
    else:
        add("warn", "no-covers", "no cover script; explore cannot build an audition set")
    covers_ok = all(_cover_length_ok(cover, spec.language) for cover in compiled.covers)
    if compiled.covers and covers_ok:
        testability += 20
    elif compiled.covers:
        add("warn", "cover-length", "cover scripts should be short (about 2-3 seconds of speech)")
    if spec.avoid:
        testability += 15
    else:
        add("info", "no-avoid", "consider an avoid list to keep the search bounded")
    if not spec.scene.is_empty():
        testability += 15
    testability = min(100, testability)

    if len(text) > 300:
        add(
            "warn",
            "long-instruction",
            f"instruction is {len(text)} characters; shorter is easier to debug",
        )

    if not NAME_RE.match(spec.name):
        add("warn", "name-format", "name should match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")

    dimensions = {
        "specificity": int(specificity),
        "executability": int(executability),
        "consistency": int(consistency),
        "language_match": int(language_score),
        "testability": int(testability),
    }
    score = int(round(sum(dimensions.values()) / len(dimensions)))
    severity_order = {"block": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda issue: severity_order[issue.severity])
    return LintReport(
        score=score,
        dimensions=dimensions,
        issues=issues,
        rendered_instruction=text,
    )


def _conflicts(spec: DesignSpec) -> list[str]:
    messages: list[str] = []
    identity = spec.identity
    delivery = spec.delivery_baseline
    scene = spec.scene

    pitch = (identity.pitch or "").strip()
    if pitch in {"low", "low_mid"} and _contains_any(identity.timbre, {"bright"}):
        messages.append("pitch is low but timbre asks for a bright quality")
    if pitch in {"high", "high_mid"} and _contains_any(identity.timbre, {"deep", "dark"}):
        messages.append("pitch is high but timbre asks for a deep/dark quality")

    energy = (delivery.energy or "").strip()
    pace = (delivery.pace or "").strip()
    if energy in {"low", "medium_low"} and pace in {"fast", "medium_fast"}:
        messages.append("energy is low but pace is fast; pick one direction")
    if energy in {"high", "medium_high"} and pace == "slow":
        messages.append("energy is high but pace is slow; pick one direction")

    proximity = (scene.proximity or "").strip()
    if proximity not in {"", "close_mic", "mid_distance", "far_field"}:
        messages.append(f"unknown proximity '{proximity}'")

    gender = (identity.gender_presentation or "").strip()
    if gender not in {"", "female", "male", "neutral"}:
        messages.append(f"unknown gender_presentation '{gender}'")

    age = (identity.age or "").strip()
    if age == "child" and (identity.persona or "").strip() in {
        "late_night_host",
        "documentary_narrator",
    }:
        messages.append("a child voice with an adult-host persona is inconsistent")

    return messages


def _contains_any(values: list[str], candidates: set[str]) -> bool:
    return bool({value.strip() for value in values} & candidates)


def _leaf_strings(spec: DesignSpec) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, str):
            result.append((prefix, value))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(f"{prefix}[{index}]", item)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(f"{prefix}.{key}" if prefix else key, item)

    for section in ("identity", "delivery_baseline", "scene"):
        walk(section, getattr(spec, section).model_dump(mode="json"))
    walk("avoid", spec.avoid)
    if spec.notes:
        result.append(("notes", spec.notes))
    return result


def _cover_length_ok(cover: str, language: str) -> bool:
    stripped = cover.strip()
    if not stripped:
        return False
    if language == "zh":
        return 4 <= len(stripped) <= 40
    return 5 <= len(stripped) <= 120


def _split_list(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, str):
        return _split_text(value)
    return value


def _split_text(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.split(r"[,，、]", value)
    return [part.strip() for part in parts if part.strip()]


def _normalise_instruction(text: str, language: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([，。、；：！？])", r"\1", text)
    text = re.sub(r"([，。、；：！？])\1+", r"\1", text)
    text = re.sub(r"[，、；：]\s*。", "。", text)
    text = re.sub(r"^[，。、；：\s]+", "", text)
    if language == "zh":
        text = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", text)
        text = re.sub(r"(?<=[一-鿿])\s+(?=[，。、；：！？])", "", text)
        text = re.sub(r"(?<=[，。、；：！？])\s+(?=[一-鿿])", "", text)
    else:
        text = re.sub(r"\s+([.!?,;:])", r"\1", text)
        text = re.sub(r"([.!?])\1+", r"\1", text)
    if text and text[-1] not in "。.!?！？":
        text += "。" if language == "zh" else "."
    return text.strip()
