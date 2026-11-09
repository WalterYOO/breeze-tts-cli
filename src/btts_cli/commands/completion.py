"""``btts completion``: print or install a shell completion script."""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

import click
from click.shell_completion import ShellComplete, get_completion_class

from btts_cli.commands._common import with_error_handling
from btts_cli.output.errors import UsageError

_ALL_SHELLS = ("bash", "zsh", "fish", "powershell")
# PowerShell completion only exists in newer Click releases; advertise only the
# shells this Click actually implements.
SHELLS = tuple(
    shell for shell in _ALL_SHELLS if get_completion_class(shell) is not None
)
_PROG_NAME = "btts"
_COMPLETE_VAR = "_BTTS_COMPLETE"
_MARKER = "# btts-generated completion file"


def _detect_shell() -> str | None:
    name = Path(os.environ.get("SHELL", "")).name
    return name if name in SHELLS else None


def _generate_script(completion_class: type[ShellComplete]) -> str:
    ctx = click.get_current_context()
    return completion_class(
        ctx.find_root().command, {}, _PROG_NAME, _COMPLETE_VAR
    ).source()


def _filename(shell: str) -> str:
    return {"bash": "btts", "zsh": "_btts", "fish": "btts.fish"}[shell]


def _oh_my_zsh_completion() -> Path | None:
    """Return Oh My Zsh's user completion path, which is already in ``$fpath``.

    Writing there avoids asking the user to edit ``~/.zshrc``: Oh My Zsh adds
    ``$ZSH_CUSTOM/completions`` to ``fpath`` before calling ``compinit``.
    """
    custom = os.environ.get("ZSH_CUSTOM")
    if custom:
        return Path(custom).expanduser() / "completions" / "_btts"
    omz = Path.home() / ".oh-my-zsh"
    if omz.is_dir():
        return omz / "custom" / "completions" / "_btts"
    return None


def _default_install_path(shell: str) -> Path:
    if shell == "bash":
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
        return root / "bash-completion" / "completions" / "btts"
    if shell == "zsh":
        return _oh_my_zsh_completion() or Path.home() / ".zfunc" / "_btts"
    if shell == "fish":
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base).expanduser() if base else Path.home() / ".config"
        return root / "fish" / "completions" / "btts.fish"
    raise UsageError(
        "--install is not supported for powershell",
        hint="append `btts completion powershell` to $PROFILE yourself",
    )


def _install(
    shell: str, script: str, target: Path, *, custom_dir: bool
) -> None:
    if target.is_symlink():
        raise UsageError(f"refusing to write through a symlink: {target}")

    payload = script if script.endswith("\n") else script + "\n"
    payload += f"{_MARKER}; remove with: btts completion --uninstall {shell}\n"

    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(
                f"cannot read existing completion file: {target}: {exc}"
            ) from exc
        if _MARKER not in existing:
            raise UsageError(
                f"completion file already exists and was not created by btts: {target}",
                hint="remove it yourself, or choose another directory with --dir",
            )
        if existing == payload:
            click.echo(f"up to date: {target}")
        else:
            _write_atomic(target, payload)
            click.echo(f"updated: {target}")
    else:
        _write_atomic(target, payload)
        click.echo(f"installed: {target}")

    _print_install_hint(shell, target, custom_dir=custom_dir)


def _write_atomic(target: Path, payload: str) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
    except OSError as exc:
        raise UsageError(f"cannot create completion file: {target}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise UsageError(f"cannot write completion file: {target}: {exc}") from exc


def _uninstall(target: Path) -> None:
    if target.is_symlink():
        raise UsageError(f"refusing to remove a symlink: {target}")
    try:
        existing = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        click.echo(f"not installed: {target}")
        return
    except OSError as exc:
        raise UsageError(
            f"cannot read completion file: {target}: {exc}"
        ) from exc
    if _MARKER not in existing:
        raise UsageError(
            f"refusing to remove a file not created by btts: {target}",
            hint="remove it manually if it is yours",
        )
    try:
        target.unlink()
    except OSError as exc:
        raise UsageError(
            f"cannot remove completion file: {target}: {exc}"
        ) from exc
    click.echo(f"removed: {target}")


def _print_install_hint(shell: str, target: Path, *, custom_dir: bool) -> None:
    quoted = shlex.quote(str(target))
    if shell == "bash":
        if custom_dir:
            click.echo(f"add to ~/.bashrc: source {quoted}", err=True)
        else:
            click.echo(
                "bash-completion auto-loads this file; if you do not use "
                f"bash-completion, add to ~/.bashrc: source {quoted}",
                err=True,
            )
    elif shell == "zsh":
        if target == _oh_my_zsh_completion():
            click.echo(
                "Oh My Zsh detected: this directory is already in $fpath; "
                "open a new shell (no rc edit needed).",
                err=True,
            )
        else:
            directory = shlex.quote(str(target.parent))
            click.echo("add to ~/.zshrc before compinit:", err=True)
            click.echo(f"  fpath=({directory} $fpath)", err=True)
            click.echo("  autoload -Uz compinit && compinit", err=True)
    elif shell == "fish":
        if custom_dir:
            click.echo(f"add to config.fish: source {quoted}", err=True)
        else:
            click.echo("fish auto-loads this file in new sessions.", err=True)


@click.command("completion")
@click.argument("shell", required=False, type=click.Choice(SHELLS))
@click.option(
    "--install",
    "install",
    is_flag=True,
    help="Write the script to a per-shell completion file (never edits rc).",
)
@click.option(
    "--uninstall",
    "uninstall",
    is_flag=True,
    help="Remove a file previously written by --install.",
)
@click.option(
    "--dir",
    "dir_path",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Install directory (requires --install or --uninstall).",
)
@with_error_handling
def completion(
    shell: str | None,
    install: bool,
    uninstall: bool,
    dir_path: Path | None,
) -> None:
    """Print or install a shell completion script.

    SHELL is one of the supported shells shown in the usage line; when omitted
    it is detected from $SHELL. The script calls back into ``btts`` on TAB, so
    voice names (``voice show``, ``voice export``, ``say --voice`` and friends)
    complete dynamically from the local store.

    Without --install the script is written to stdout:

    \b
      bash:  btts completion bash > ~/.btts-complete.bash
      zsh:   btts completion zsh > ~/.btts-complete.zsh
      fish:  btts completion fish > ~/.config/fish/completions/btts.fish

    --install writes a dedicated completion file and prints the one-line setup
    needed for zsh; it never edits .bashrc, .zshrc or config.fish.
    """
    if install and uninstall:
        raise UsageError("--install and --uninstall are mutually exclusive")
    if dir_path is not None and not (install or uninstall):
        raise UsageError("--dir requires --install or --uninstall")

    shell = shell or _detect_shell()
    if shell is None:
        raise UsageError(
            "cannot detect the current shell",
            hint="pass one of: " + ", ".join(SHELLS),
        )

    completion_class = get_completion_class(shell)
    if completion_class is None:  # pragma: no cover - guarded by Choice
        raise UsageError(f"unsupported shell: {shell}")

    if (install or uninstall) and shell == "powershell":
        raise UsageError(
            "--install is not supported for powershell",
            hint="append `btts completion powershell` to $PROFILE yourself",
        )

    if install or uninstall:
        target = (
            dir_path / _filename(shell)
            if dir_path is not None
            else _default_install_path(shell)
        )
        if uninstall:
            _uninstall(target)
        else:
            _install(
                shell,
                _generate_script(completion_class),
                target,
                custom_dir=dir_path is not None,
            )
        return

    # Write bytes: text stdout translates LF to CRLF on Windows and breaks
    # PowerShell/bash sourcing. This mirrors click's own completion handler.
    click.echo(_generate_script(completion_class).encode(), nl=False)
