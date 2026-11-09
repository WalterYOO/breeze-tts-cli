"""Pure predicates for selecting voices in ``voice list`` / ``voice filter``.

The matcher is intentionally small and deterministic:

- ``fuzzy``  — case-insensitive contiguous substring of the addressable name.
- ``prefix`` — case-insensitive ``startswith``.
- ``exact``  — case-insensitive full-name equality.

Name matching is always on the addressable name (the directory name that
``VoiceStore.scan()`` reports), never on a mismatched ``profile.name``. Tags,
kind and language are separate structured filters; every predicate is AND-ed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from btts_cli.domain.design import NAME_RE
from btts_cli.domain.voice import VoiceProfile, normalize_tag
from btts_cli.output.errors import UsageError

MatchMode = Literal["fuzzy", "prefix", "exact"]


@dataclass(frozen=True)
class VoiceFilter:
    query: str | None = None
    match: MatchMode = "fuzzy"
    tags: tuple[str, ...] = ()
    kind: str | None = None
    language: str | None = None

    @property
    def has_criteria(self) -> bool:
        return (
            self.query is not None
            or bool(self.tags)
            or self.kind is not None
            or self.language is not None
        )

    def matches(self, profile: VoiceProfile) -> bool:
        if self.query is not None and not is_addressable_name(profile.name):
            # QUERY matches addressable directory names only. Unaddressable
            # entries can still be listed for repair, but never match a query.
            return False
        name = profile.name.casefold()
        if self.query is not None:
            query = self.query.casefold()
            if self.match == "fuzzy":
                if query not in name:
                    return False
            elif self.match == "prefix":
                if not name.startswith(query):
                    return False
            elif name != query:
                return False
        if self.kind is not None and profile.kind != self.kind:
            return False
        if self.language is not None and profile.language != self.language:
            return False
        if self.tags:
            stored = _normalized_stored_tags(profile.tags)
            if not all(tag in stored for tag in self.tags):
                return False
        return True


def normalize_query(value: str | None) -> str | None:
    """Trim, strip one leading ``@``, and reject an empty effective query."""
    if value is None:
        return None
    query = value.strip()
    if query.startswith("@"):
        query = query[1:].strip()
    if not query:
        raise UsageError(
            "filter query is empty",
            hint="pass a name fragment, or omit the query to list every voice",
        )
    return query


def is_addressable_name(name: str) -> bool:
    """Whether ``--names-only`` can safely emit this name for shell use."""
    return NAME_RE.fullmatch(name) is not None


def _normalized_stored_tags(tags: list[str]) -> set[str]:
    normalized: set[str] = set()
    for tag in tags:
        try:
            normalized.add(normalize_tag(tag))
        except UsageError:
            # Legacy unnormalized tags are ignored, matching the convergence
            # rule in VoiceStore.update_tags().
            continue
    return normalized
