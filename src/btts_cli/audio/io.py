"""WAV reading/writing and one-reel concatenation.

Every write goes to a sibling temporary file and is renamed into place, so a
half-written take is never visible to ``--resume`` or ``report``.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from btts_cli.output.errors import IntegrityError


def write_wav_atomic(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mono = _as_mono_float32(audio)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".wav.tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with sf.SoundFile(
            fd,
            mode="w",
            samplerate=sample_rate,
            channels=1,
            subtype="PCM_16",
            format="WAV",
        ) as handle:
            handle.write(mono)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    try:
        audio, sample_rate = sf.read(path, always_2d=True, dtype="float32")
    except (OSError, RuntimeError) as exc:
        raise IntegrityError(f"cannot read WAV file: {path.name} ({exc})") from exc
    return np.mean(audio, axis=1).astype(np.float32), int(sample_rate)


def concat_with_gap(
    paths: list[Path], *, gap_s: float = 1.0, sample_rate: int = 24000
) -> np.ndarray:
    if not math.isfinite(gap_s) or gap_s < 0 or gap_s > 30:
        raise IntegrityError("gap must be finite and between 0 and 30 seconds")
    gap = np.zeros(int(round(gap_s * sample_rate)), dtype=np.float32)
    chunks: list[np.ndarray] = []
    for index, path in enumerate(paths):
        audio, sr = read_wav(path)
        if sr != sample_rate:
            raise IntegrityError(
                f"cannot concatenate {path.name}: sample rate {sr} != {sample_rate}"
            )
        if index:
            chunks.append(gap)
        chunks.append(audio)
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)


def _as_mono_float32(audio: np.ndarray) -> np.ndarray:
    array = np.asarray(audio)
    if array.ndim > 1:
        array = np.mean(array, axis=-1)
    if array.dtype != np.float32:
        array = array.astype(np.float32)
    return np.ascontiguousarray(np.clip(np.nan_to_num(array), -1.0, 1.0))
