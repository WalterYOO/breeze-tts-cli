"""breeze-tts-cli: a local voice-design workbench for Breeze TTS 2.

The package root deliberately imports nothing heavy. In particular, importing
:mod:`btts_cli` or :mod:`btts_cli.cli` must not import ``torch``; the MPS
engine is imported lazily by :func:`btts_cli.engine.load_engine`.
"""

__version__ = "0.2.0"

LICENSE_NOTICE = (
    "Model weights and self-hosted output are covered by the BreezeBlue "
    "Research and Non-Commercial License 1.1. Research/evaluation/prototyping "
    "only; commercial use requires a separate written license from BreezeBlue."
)
