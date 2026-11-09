"""``btts completion``: print, install or remove shell completion scripts."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
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
_BEGIN = "# >>> btts completion >>>"
_END = "# <<< btts completion <<<"


def _detect_shell() -> str | None:
    name = Path(os.environ.get("SHELL", "")).name
    return name if name in SHELLS else None


def _generate_script(completion_class: type[ShellComplete]) -> str:
    ctx = click.get_current_context()
    return completion_class(
        ctx.find_root().command, {}, _PROG_NAME, _COMPLETE_VAR
    ).source()


def _xdg_data() -> Path:
    value = os.environ.get("XDG_DATA_HOME")
    return Path(value).expanduser() if value else Path.home() / ".local" / "share"


def _xdg_config() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value).expanduser() if value else Path.home() / ".config"


def _writable_dir(directory: Path) -> bool:
    try:
        if directory.exists():
            return directory.is_dir() and os.access(directory, os.W_OK)
        return directory.parent.is_dir() and os.access(directory.parent, os.W_OK)
    except OSError:
        return False


def _oh_my_zsh_completion() -> Path | None:
    """Return Oh My Zsh's user completion path, which is already in ``$fpath``."""
    custom = os.environ.get("ZSH_CUSTOM")
    if custom:
        return Path(custom).expanduser() / "completions" / "_btts"
    omz = Path.home() / ".oh-my-zsh"
    if omz.is_dir():
        return omz / "custom" / "completions" / "_btts"
    return None


def _zsh_rc_path() -> Path:
    zdotdir = os.environ.get("ZDOTDIR")
    base = Path(zdotdir).expanduser() if zdotdir else Path.home()
    return base / ".zshrc"


def _zsh_config_files() -> list[Path]:
    zdotdir = os.environ.get("ZDOTDIR")
    base = Path(zdotdir).expanduser() if zdotdir else Path.home()
    return [base / name for name in (".zshrc", ".zshenv", ".zprofile", ".zlogin")]


_FPATH_RE = re.compile(r"fpath\s*\+?=\s*\(([^)]*)\)")


def _fpath_dirs_from_text(text: str) -> list[Path]:
    directories: list[Path] = []
    for match in _FPATH_RE.finditer(text):
        try:
            parts = shlex.split(match.group(1))
        except ValueError:
            continue
        for part in parts:
            if part in {"$fpath", "${fpath}"}:
                continue
            expanded = os.path.expandvars(os.path.expanduser(part))
            if "$" in expanded:
                continue
            directories.append(Path(expanded))
    return directories


def _strip_managed_block(text: str) -> str:
    """Ignore our own rc block when detecting the user's existing ``$fpath``."""
    return re.sub(
        rf"{re.escape(_BEGIN)}.*?{re.escape(_END)}",
        "",
        text,
        flags=re.DOTALL,
    )


def _zsh_fpath_dirs() -> list[Path]:
    """Directories the user's zsh config already adds to ``$fpath``."""
    directories: list[Path] = []
    seen: set[str] = set()
    for config in _zsh_config_files():
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for directory in _fpath_dirs_from_text(_strip_managed_block(text)):
            key = str(directory)
            if key not in seen:
                seen.add(key)
                directories.append(directory)
    return directories


def _zsh_plan() -> tuple[Path, Path | None]:
    omz = _oh_my_zsh_completion()
    if omz is not None:
        return omz, None
    for directory in _zsh_fpath_dirs():
        if _writable_dir(directory):
            return directory / "_btts", None
    return Path.home() / ".zfunc" / "_btts", _zsh_rc_path()


_BASH_COMPLETION_MARKERS = (
    "/usr/share/bash-completion/bash_completion",
    "/etc/bash_completion",
    "/opt/homebrew/etc/profile.d/bash_completion.sh",
    "/usr/local/etc/profile.d/bash_completion.sh",
    "/usr/share/bash-completion/completions",
)


def _bash_completion_installed() -> bool:
    return any(Path(marker).exists() for marker in _BASH_COMPLETION_MARKERS)


def _bash_version() -> tuple[int, int] | None:
    """Return the major/minor of ``bash`` on PATH without loading any rc."""
    try:
        completed = subprocess.run(
            [
                "bash",
                "-c",
                "printf '%s %s' \"${BASH_VERSINFO[0]}\" \"${BASH_VERSINFO[1]}\"",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = completed.stdout.split()
    if completed.returncode != 0 or len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _bash_rc_path() -> Path:
    bashrc = Path.home() / ".bashrc"
    if bashrc.exists():
        return bashrc
    profile = Path.home() / ".bash_profile"
    if profile.exists():
        return profile
    return bashrc


def _bash_plan() -> tuple[Path, Path | None, list[str]]:
    warnings: list[str] = []
    version = _bash_version()
    too_old = False
    if version is not None and version < (4, 4):
        too_old = True
        warnings.append(
            f"bash {version[0]}.{version[1]} cannot run Click's completion script "
            "(it needs bash 4.4+); install a newer bash or use zsh"
        )

    user_dir = os.environ.get("BASH_COMPLETION_USER_DIR")
    if user_dir:
        return Path(user_dir).expanduser() / "completions" / "btts", None, warnings

    auto_target = _xdg_data() / "bash-completion" / "completions" / "btts"
    if _bash_completion_installed():
        return auto_target, None, warnings

    file_target = _xdg_data() / "btts" / "completion.bash"
    if too_old:
        # Do not add a source line that would fail on every shell startup.
        return file_target, None, warnings
    return file_target, _bash_rc_path(), warnings


def _fish_plan() -> tuple[Path, Path | None, list[str]]:
    return _xdg_config() / "fish" / "completions" / "btts.fish", None, []


def _plan(shell: str) -> tuple[Path, Path | None, list[str]]:
    if shell == "zsh":
        target, rc_path = _zsh_plan()
        return target, rc_path, []
    if shell == "bash":
        return _bash_plan()
    return _fish_plan()


def _payload(script: str, shell: str) -> str:
    text = script if script.endswith("\n") else script + "\n"
    return f"{text}{_MARKER}; remove with: btts completion --uninstall {shell}\n"


def _write_file(target: Path, payload: str) -> bool:
    """Write ``payload`` atomically; return whether the content changed."""
    if target.is_symlink():
        raise UsageError(f"refusing to write through a symlink: {target}")
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else None
    except OSError as exc:
        raise UsageError(
            f"cannot read existing completion file: {target}: {exc}"
        ) from exc
    if existing is not None and _MARKER not in existing:
        raise UsageError(
            f"completion file already exists and was not created by btts: {target}",
            hint="remove it yourself, or choose another directory with --dir",
        )
    if existing == payload:
        return False

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
    return True


def _read_rc(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc}") from exc


def _write_rc(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot write {path}: {exc}") from exc


def _block_body(shell: str, target: Path) -> str:
    quoted = shlex.quote(str(target))
    if shell == "zsh":
        directory = shlex.quote(str(target.parent))
        # Re-run compinit so the block works even when appended after the
        # user's existing compinit call.
        return (
            f"fpath=({directory} $fpath)\n"
            "autoload -Uz compinit && compinit\n"
        )
    return f"if [ -r {quoted} ]; then . {quoted}; fi\n"


def _ensure_rc_block(path: Path, body: str) -> bool:
    """Create or replace the marked block; return whether the file changed."""
    text = _read_rc(path)
    block = f"{_BEGIN}\n{body}{_END}\n"
    start = text.find(_BEGIN)
    end = text.find(_END, start + 1)
    if start >= 0 and end >= 0:
        end += len(_END)
        if text[end : end + 1] == "\n":
            end += 1
        updated = text[:start] + block + text[end:]
    else:
        prefix = text if not text or text.endswith("\n") else text + "\n"
        updated = prefix + ("\n" if prefix else "") + block
    if updated == text:
        return False
    _write_rc(path, updated)
    return True


def _remove_rc_block(path: Path) -> bool:
    text = _read_rc(path)
    pattern = re.compile(
        rf"(?:^|\n){re.escape(_BEGIN)}.*?{re.escape(_END)}\n?",
        re.DOTALL,
    )
    updated, count = pattern.subn("\n", text, count=1)
    if count == 0:
        return False
    _write_rc(path, updated)
    return True


def _rc_files(shell: str) -> list[Path]:
    if shell == "zsh":
        return [_zsh_rc_path()]
    if shell == "bash":
        return [Path.home() / ".bashrc", Path.home() / ".bash_profile"]
    return []


def _known_targets(shell: str) -> list[Path]:
    if shell == "zsh":
        targets = [
            Path.home() / ".zfunc" / "_btts",
            Path.home() / ".zsh" / "completions" / "_btts",
            Path.home() / ".config" / "zsh" / "completions" / "_btts",
            Path.home() / ".oh-my-zsh" / "completions" / "_btts",
        ]
        omz = _oh_my_zsh_completion()
        if omz is not None:
            targets.append(omz)
        return targets
    if shell == "bash":
        targets = [
            _xdg_data() / "bash-completion" / "completions" / "btts",
            _xdg_data() / "btts" / "completion.bash",
        ]
        user_dir = os.environ.get("BASH_COMPLETION_USER_DIR")
        if user_dir:
            targets.append(Path(user_dir).expanduser() / "completions" / "btts")
        return targets
    return [_xdg_config() / "fish" / "completions" / "btts.fish"]


def _remove_completion_file(path: Path) -> bool:
    """Remove ``path`` only when it carries the btts marker."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if _MARKER not in text:
        return False
    try:
        path.unlink()
    except OSError as exc:
        raise UsageError(f"cannot remove completion file: {path}: {exc}") from exc
    return True


def _remove_rc_blocks(shell: str) -> list[Path]:
    removed: list[Path] = []
    for path in _rc_files(shell):
        try:
            if path.exists() and _remove_rc_block(path):
                removed.append(path)
        except UsageError:
            raise
    return removed


def _cleanup_stale(shell: str, keep: Path) -> None:
    """Remove files/blocks left by earlier installs at other locations."""
    for path in _known_targets(shell):
        if path != keep:
            _remove_completion_file(path)
    _remove_rc_blocks(shell)


def _completion_ready(shell: str) -> bool:
    if shell != "bash":
        return True
    version = _bash_version()
    return version is None or version >= (4, 4)


def _install_default(shell: str, script: str) -> None:
    target, rc_path, warnings = _plan(shell)
    for warning in warnings:
        click.echo(f"warning: {warning}", err=True)

    changed = _write_file(target, _payload(script, shell))
    state = "updated" if changed else "up to date"
    click.echo(f"{state}: {target}")

    if rc_path is not None:
        rc_changed = _ensure_rc_block(rc_path, _block_body(shell, target))
        click.echo(
            f"{'updated' if rc_changed else 'up to date'}: {rc_path} "
            f"({_BEGIN.strip('# ')} block)"
        )
    else:
        _cleanup_stale(shell, keep=target)
    if _completion_ready(shell):
        click.echo("open a new shell to enable completion.", err=True)
    else:
        click.echo(
            "completion will not activate until bash 4.4+ is available.",
            err=True,
        )


def _install_to_dir(shell: str, script: str, target: Path) -> None:
    changed = _write_file(target, _payload(script, shell))
    state = "updated" if changed else "up to date"
    click.echo(f"{state}: {target}")
    _print_dir_hint(shell, target)


def _uninstall_default(shell: str) -> None:
    removed_files: list[Path] = []
    targets = list(_known_targets(shell))
    target, _, _ = _plan(shell)
    if target not in targets:
        targets.append(target)
    for path in targets:
        if _remove_completion_file(path):
            removed_files.append(path)
    removed_rc = _remove_rc_blocks(shell)
    for path in removed_files:
        click.echo(f"removed: {path}")
    for path in removed_rc:
        click.echo(f"removed block: {path}")
    if not removed_files and not removed_rc:
        click.echo("nothing to remove.")


def _uninstall_from_dir(target: Path) -> None:
    if _remove_completion_file(target):
        click.echo(f"removed: {target}")
    else:
        click.echo(f"not installed: {target}")


def _print_dir_hint(shell: str, target: Path) -> None:
    quoted = shlex.quote(str(target))
    if shell == "bash":
        click.echo(f"add to ~/.bashrc: source {quoted}", err=True)
    elif shell == "zsh":
        directory = shlex.quote(str(target.parent))
        click.echo("add to ~/.zshrc before compinit:", err=True)
        click.echo(f"  fpath=({directory} $fpath)", err=True)
        click.echo("  autoload -Uz compinit && compinit", err=True)
    elif shell == "fish":
        click.echo(f"add to config.fish: source {quoted}", err=True)


@click.command("completion")
@click.argument("shell", required=False, type=click.Choice(SHELLS))
@click.option(
    "--install",
    "install",
    is_flag=True,
    help="Install a completion file and configure the shell when needed.",
)
@click.option(
    "--uninstall",
    "uninstall",
    is_flag=True,
    help="Remove a file/block previously written by --install.",
)
@click.option(
    "--dir",
    "dir_path",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Only write the completion file here; do not touch shell config.",
)
@with_error_handling
def completion(
    shell: str | None,
    install: bool,
    uninstall: bool,
    dir_path: Path | None,
) -> None:
    """Print, install or remove a shell completion script.

    SHELL is one of the supported shells shown in the usage line; when omitted
    it is detected from $SHELL. The script calls back into ``btts`` on TAB, so
    voice names (``voice show``, ``voice export``, ``say --voice`` and friends)
    complete dynamically from the local store.

    Without --install the script is written to stdout:

    \b
      bash:  btts completion bash
      zsh:   btts completion zsh
      fish:  btts completion fish

    --install is automatic and idempotent: it reuses a directory already in the
    shell's completion path (for example Oh My Zsh's custom/completions, or an
    fpath entry in .zshrc), and otherwise writes a marked block to .zshrc or
    .bashrc. Re-running replaces the block instead of duplicating it;
    --uninstall removes both the file and the block. --dir only writes the
    file and prints the manual setup line.
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

    if install or uninstall:
        if shell == "powershell":
            raise UsageError(
                "--install is not supported for powershell",
                hint="append `btts completion powershell` to $PROFILE yourself",
            )
        if dir_path is not None:
            target = dir_path / {"bash": "btts", "zsh": "_btts", "fish": "btts.fish"}[
                shell
            ]
            if uninstall:
                _uninstall_from_dir(target)
            else:
                _install_to_dir(shell, _generate_script(completion_class), target)
        elif uninstall:
            _uninstall_default(shell)
        else:
            _install_default(shell, _generate_script(completion_class))
        return

    # Write bytes: text stdout translates LF to CRLF on Windows and breaks
    # PowerShell/bash sourcing. This mirrors click's own completion handler.
    click.echo(_generate_script(completion_class).encode(), nl=False)
