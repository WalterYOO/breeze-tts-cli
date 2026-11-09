"""Dynamic shell-completion helpers for voice names."""

from __future__ import annotations

from collections.abc import Callable

import click
from click.shell_completion import CompletionItem

from btts_cli.domain.voice import VoiceStore
from btts_cli.domain.voice_filter import is_addressable_name


def _voice_names() -> list[str]:
    """Return sorted, addressable, non-symlinked voice names.

    Completion runs on every TAB from inside a shell, where stdout must stay
    machine-readable and a broken store must not produce an error banner.
    Any store problem therefore yields no candidates; running the actual
    command still reports the problem normally.
    """
    try:
        store = VoiceStore()
        profiles, _, _ = store.scan()
    except Exception:
        return []

    names: list[str] = []
    for profile in profiles:
        name = profile.name
        if not is_addressable_name(name):
            continue
        directory = store.root / name
        try:
            if directory.is_symlink() or (directory / "profile.json").is_symlink():
                continue
        except OSError:
            continue
        names.append(name)
    return sorted(names)


def _complete_voice(incomplete: str, *, at: str) -> list[CompletionItem]:
    """Build completion items for ``incomplete``.

    ``at`` controls the ``@`` prefix: ``auto`` mirrors what the user typed,
    ``always`` emits the ``@NAME`` form documented for ``say --voice``, and
    ``never`` offers bare names only (commands that reject ``@``).
    """
    if at == "never" and incomplete.startswith("@"):
        return []

    if incomplete.startswith("@"):
        prefix = incomplete[1:]
        marker = "@"
    else:
        prefix = incomplete
        marker = "@" if at == "always" else ""

    return [
        CompletionItem(marker + name)
        for name in _voice_names()
        if name.startswith(prefix)
    ]


def complete_voice_name(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete a NAME argument that accepts an optional leading ``@``."""
    return _complete_voice(incomplete, at="auto")


def complete_bare_voice_name(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete a NAME argument that rejects a leading ``@``."""
    return _complete_voice(incomplete, at="never")


def complete_voice_reference(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete ``say --voice`` as ``@NAME``, matching the documented form."""
    return _complete_voice(incomplete, at="always")


Completer = Callable[[click.Context, click.Parameter, str], list[CompletionItem]]
