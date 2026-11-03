#!/usr/bin/env python3
"""Sync the whitelisted Breeze TTS inference sources into ``btts_cli._vendor``.

This script is deliberately deterministic and never follows ``main`` on its own:

    python scripts/sync_breeze_vendor.py --ref <commit> [--source ../breeze-tts]
    pytest
    # real MPS smoke
    git diff

It copies only the whitelist below, rewrites upstream absolute imports to the
vendored package path, prepends the provenance header, refreshes
``vendor.lock.json`` and regenerates ``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT.parent / "breeze-tts"
VENDOR_ROOT = REPO_ROOT / "src" / "btts_cli" / "_vendor" / "breeze_tts"
LOCK_PATH = REPO_ROOT / "vendor.lock.json"
NOTICES_PATH = REPO_ROOT / "THIRD_PARTY_NOTICES.md"

UPSTREAM_URL = "https://github.com/breezeblue-ai/breeze-tts"
DEFAULT_REF = "daa6cb2efe5471f09369229f322d20bb956cb3c3"
PACKAGE_PREFIX = "btts_cli._vendor.breeze_tts"

WHITELIST = (
    "breeze_infer/__init__.py",
    "breeze_infer/runtime.py",
    "breeze_infer/audio.py",
    "breeze_infer/templates.py",
    "models/__init__.py",
    "models/breeze.py",
    "models/breeze_config.py",
    "models/breeze_base_config.py",
    "models/breeze_backbone_factory.py",
    "models/generation_breeze.py",
    "models/logits_process.py",
    "models/t5gemma2_compat.py",
)

REWRITES = (
    ("from models.", f"from {PACKAGE_PREFIX}.models."),
    ("from breeze_infer.", f"from {PACKAGE_PREFIX}.breeze_infer."),
    ("import models.", f"import {PACKAGE_PREFIX}.models."),
    ("import breeze_infer.", f"import {PACKAGE_PREFIX}.breeze_infer."),
)

HEADER = (
    "# Vendored from breezeblue-ai/breeze-tts at {ref}.\n"
    "# Modified: package-qualified imports for btts_cli.\n"
)

VENDOR_INIT = '''"""Vendored subset of breezeblue-ai/breeze-tts (Apache-2.0).

Only the modules required to load and run Breeze TTS 2 eagerly on Apple MPS
are included. Do not edit these files by hand; run
``scripts/sync_breeze_vendor.py`` instead.
"""

UPSTREAM_URL = "{url}"
UPSTREAM_REF = "{ref}"
'''

# ``from __future__`` must remain the first *statement*; comment lines before it
# are legal, so the provenance header is safe for every file in the whitelist.
IMPORT_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<stmt>from|import)\s+(?P<module>models|breeze_infer)\."
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rewrite_imports(text: str) -> str:
    lines = text.splitlines(keepends=True)
    rewritten: list[str] = []
    for line in lines:
        match = IMPORT_RE.match(line)
        if match:
            statement = match.group("stmt")
            module = match.group("module")
            rest = line[match.end() - len(module) - 1 :]
            line = f"{match.group('indent')}{statement} {PACKAGE_PREFIX}.{rest}"
        rewritten.append(line)
    result = "".join(rewritten)
    for old, new in REWRITES:
        result = result.replace(old, new)
    return result


def source_ref(source: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def write_atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8", newline="\n")
    tmp.replace(path)


def render_notices(ref: str, files: dict[str, dict[str, str]]) -> str:
    rows = "\n".join(
        f"| `{name}` | `{meta['source_sha256'][:16]}...` | `{meta['vendored_sha256'][:16]}...` |"
        for name, meta in files.items()
    )
    return f"""# Third-party notices

## Breeze TTS 2 inference sources

- Upstream: {UPSTREAM_URL}
- Vendored at commit: `{ref}`
- Vendor location: `src/btts_cli/_vendor/breeze_tts/`
- Upstream license: Apache License 2.0, copied verbatim to
  `src/btts_cli/_vendor/breeze_tts/LICENSE`.
- Upstream `NOTICE` file: not present at the vendored commit.

### Copied files

| Upstream path | Source SHA-256 | Vendored SHA-256 |
|---|---|---|
{rows}

### Modifications

1. All absolute imports rooted at `models` or `breeze_infer` were rewritten to
   `{PACKAGE_PREFIX}.models` / `{PACKAGE_PREFIX}.breeze_infer` so the upstream
   generic top-level package names are never installed.
2. A two-line provenance header was prepended to every vendored file. Original
   copyright and license headers are preserved below it.
3. No behavioural code changes were made. CUDA-only modules
   (`breeze_infer.api`, `breeze_infer.batch`, `models.fast_streaming`,
   `models.cudagraph`, `models.stream_runtime`, `models.text_encoder_graph`,
   `models.warmup_profile`) are intentionally not copied.

### Model weights

Model weights, `audio_tokenizer/` codec weights and `qwen-tts` are **not**
vendored. They are loaded at runtime from a model directory supplied with
`--model` or `BTTS_MODEL_DIR`, under the BreezeBlue Research and
Non-Commercial License. See `README.md` for the license notice.

## Python dependencies

Runtime dependencies are listed in `pyproject.toml`. They retain their own
licenses; this project does not redistribute them.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(str(DEFAULT_SOURCE)),
        help="Path to the upstream breeze-tts checkout (default: ../breeze-tts)",
    )
    parser.add_argument("--ref", default=DEFAULT_REF, help="Expected upstream commit SHA")
    parser.add_argument(
        "--allow-ref-mismatch",
        action="store_true",
        help="Proceed even when the checkout HEAD differs from --ref",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the vendored tree matches the upstream sources without writing",
    )
    args = parser.parse_args()

    source: Path = args.source.resolve()
    if not source.is_dir():
        print(f"error: upstream source not found: {source}", file=sys.stderr)
        return 2

    actual_ref = source_ref(source)
    if actual_ref != args.ref and not args.allow_ref_mismatch:
        print(
            "error: upstream HEAD does not match --ref\n"
            f"  expected: {args.ref}\n"
            f"  actual:   {actual_ref or 'unknown (not a git checkout)'}\n"
            "Pass --allow-ref-mismatch only when intentionally upgrading.",
            file=sys.stderr,
        )
        return 2

    files: dict[str, dict[str, str]] = {}
    mismatches: list[str] = []
    for relative in WHITELIST:
        upstream_path = source / relative
        if not upstream_path.is_file():
            print(f"error: missing upstream file: {relative}", file=sys.stderr)
            return 2
        source_bytes = upstream_path.read_bytes()
        rewritten = rewrite_imports(source_bytes.decode("utf-8"))
        vendored_text = HEADER.format(ref=args.ref) + "\n" + rewritten
        vendored_bytes = vendored_text.encode("utf-8")
        destination = VENDOR_ROOT / relative
        files[relative] = {
            "source_sha256": sha256_bytes(source_bytes),
            "vendored_sha256": sha256_bytes(vendored_bytes),
        }
        if args.check:
            existing = destination.read_bytes() if destination.is_file() else b""
            if existing != vendored_bytes:
                mismatches.append(relative)
        else:
            write_atomic(destination, vendored_text)

    license_source = source / "LICENSE"
    license_destination = VENDOR_ROOT / "LICENSE"
    license_bytes = license_source.read_bytes() if license_source.is_file() else b""
    files["LICENSE"] = {
        "source_sha256": sha256_bytes(license_bytes),
        "vendored_sha256": sha256_bytes(license_bytes),
    }
    init_text = VENDOR_INIT.format(url=UPSTREAM_URL, ref=args.ref)
    init_path = VENDOR_ROOT / "__init__.py"
    if args.check:
        if not license_destination.is_file() or license_destination.read_bytes() != license_bytes:
            mismatches.append("LICENSE")
        if not init_path.is_file() or init_path.read_text(encoding="utf-8") != init_text:
            mismatches.append("__init__.py")
    else:
        write_atomic(license_destination, license_bytes.decode("utf-8"))
        write_atomic(init_path, init_text)

    lock = {
        "upstream": UPSTREAM_URL,
        "ref": args.ref,
        "license": "Apache-2.0",
        "rewrite_rules": [f"{old} -> {new}" for old, new in REWRITES],
        "files": files,
    }
    if args.check:
        existing_lock = (
            json.loads(LOCK_PATH.read_text(encoding="utf-8")) if LOCK_PATH.is_file() else None
        )
        if existing_lock != lock:
            mismatches.append("vendor.lock.json")
        notices = render_notices(args.ref, files)
        if not NOTICES_PATH.is_file() or NOTICES_PATH.read_text(encoding="utf-8") != notices:
            mismatches.append("THIRD_PARTY_NOTICES.md")
        if mismatches:
            print("vendored tree is stale: " + ", ".join(mismatches), file=sys.stderr)
            return 1
        print(f"vendored tree matches {args.ref}")
        return 0

    write_atomic(LOCK_PATH, json.dumps(lock, indent=2, sort_keys=True) + "\n")
    write_atomic(NOTICES_PATH, render_notices(args.ref, files))
    print(f"vendored {len(WHITELIST)} files from {source} at {args.ref}")
    print(f"wrote {LOCK_PATH.relative_to(REPO_ROOT)} and {NOTICES_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
