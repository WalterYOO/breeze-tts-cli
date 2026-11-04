"""Engine loading. Importing this module never imports ``torch``.

The MPS implementation is imported lazily inside :func:`load_engine`, so
``btts --help``, ``design lint`` and every unit test using a fake engine
work in a base install without the ``[mps]`` extras.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from btts_cli.output.errors import EngineError

if TYPE_CHECKING:  # pragma: no cover
    from btts_cli.engine.base import Engine


def load_engine(*, model_dir: Path | None = None, device: str | None = None) -> Engine:
    """Load the local MPS engine (never falls back to CPU silently)."""
    from btts_cli.engine.base import resolve_model_dir

    resolved = resolve_model_dir(model_dir)
    try:
        from btts_cli.engine.mps import MpsEngine
    except ImportError as exc:  # torch / transformers / qwen-tts missing
        raise EngineError(
            "local inference dependencies are not installed",
            hint="install them with: pip install 'breeze-tts-cli[mps]'",
        ) from exc
    return MpsEngine.load(resolved, device=device)


__all__ = ["load_engine"]
