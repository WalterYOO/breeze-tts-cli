"""Top-level CLI. Importing this module must not import torch."""

from __future__ import annotations

import click

from btts_cli import __version__
from btts_cli.commands import design, doctor, model, say, voice


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="btts_cli")
def cli() -> None:
    """Local voice-design workbench for Breeze TTS 2 (research / non-commercial)."""


cli.add_command(doctor.doctor)
cli.add_command(model.model)
cli.add_command(design.design)
cli.add_command(voice.voice)
cli.add_command(say.say)


def main() -> None:
    cli(prog_name="btts_cli")


if __name__ == "__main__":  # pragma: no cover
    main()
